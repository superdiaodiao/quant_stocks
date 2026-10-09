# region imports
from AlgorithmImports import *
# endregion
import math
import time as _time
from datetime import date, timedelta
import numpy as np
from grid import *
import grid as _g

class StockPuts(QCAlgorithm):
    def initialize(self):
        self.set_start_date(START.year, START.month, START.day)
        self.set_end_date(END_ALGO.year, END_ALGO.month, END_ALGO.day)
        self.set_cash(100_000)
        self.settings.daily_precise_end_time = True
        raw = DataNormalizationMode.RAW
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = raw
        self.qq = self.add_equity("QQQ", Resolution.DAILY, data_normalization_mode=raw).symbol
        self.oneq = self.add_equity("ONEQ", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.ADJUSTED).symbol
        self.bil = self.add_equity("BIL", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.ADJUSTED).symbol
        self.vix = self.add_index("VIX", Resolution.DAILY).symbol
        self.set_benchmark(self.oneq)
        self.hours = self.securities[self.qq].exchange.hours
        self.add_universe(self.select)
        self.openc = {}
        self.errs = []
        self.lastm = None
        self.sticky = set()
        self.newmem = None          # membership chosen by this month's selection: {"A": [...], "N": [...]}
        self.mem = [[] for _ in range(NU)]
        self.sl = {}                # Symbol -> Sleeve
        self.opt = {}               # Symbol -> canonical option Symbol
        self.held = {}              # canonical option Symbol -> set of held contract values
        self.vixs = []
        self.pdiv = {}
        self.psplit = {}
        self.pclose = {}
        self.blev = [None, 1.0, None]   # ONEQ adj, QQQ TR, BIL adj
        self.bprevm = [None, None, None]
        self.bnav = np.full((NU, C), 1.0)
        self.bprev = np.full((NU, C), 1.0)
        self.bnav0 = np.full((NU, C), 1.0)
        self.pkH = np.ones((NU, C)); self.ddH = np.zeros((NU, C))
        self.pkF = np.ones((NU, C)); self.ddF = np.zeros((NU, C))
        self.bk = np.ones(NU); self.bk0 = np.ones(NU); self.bkprev = np.ones(NU)
        self.bkdd = np.zeros((NU + 2, 4))   # baskets + ONEQ + QQQ: pkH, ddH, pkF, ddF
        self.bkdd[:, 0] = 1.0; self.bkdd[:, 2] = 1.0
        self.d_ent = np.zeros(C); self.d_in = np.zeros(C); self.d_days = np.zeros(C)
        self.d_itm = np.zeros(C); self.d_early = np.zeros(C); self.d_cr = np.zeros(C)
        self.queue = []
        self.qpos = 0
        self.seq = 0
        self.last_proc = None
        self.last_D = None
        self.ddon = False
        self.done = False
        self.ndays = 0
        self.cnt = {"settle": 0, "itm": 0, "early_p": 0, "early_c": 0, "tp": 0, "sl": 0, "roll": 0, "entry": 0, "split": 0, "nochain": 0, "chg": 0}
        self.trace = []
        self.t0 = _time.time()
        self.tbooks = 0.0
        self.plotted = 0
        self.union_max = 0
        self.names_seen = set()
        self.memlog = []
        self.bil_prev = None

    # ------------------------------------------------------------------ universe
    def select(self, fundamental):
        ym = (self.time.year, self.time.month)
        if ym == self.lastm:
            return Universe.UNCHANGED
        self.lastm = ym
        cand = {}
        for f in fundamental:
            if f.price <= 0 or not f.has_fundamental_data:
                continue
            mc = float(f.market_cap)
            if mc <= 0:
                continue
            sr, cr = f.security_reference, f.company_reference
            if sr is None or cr is None:
                continue
            if sr.security_type != "ST00000001" or sr.is_depositary_receipt or cr.country_id != "USA":
                continue
            cid = cr.company_id or str(f.symbol.id)
            p = cand.get(cid)
            if p is None or (f.symbol in self.sticky) or (p.symbol not in self.sticky and f.dollar_volume > p.dollar_volume):
                cand[cid] = f
        rows = sorted(cand.values(), key=lambda f: float(f.market_cap), reverse=True)
        A = [f.symbol for f in rows[:10]]
        N = [f.symbol for f in rows if f.security_reference.exchange_id == "NAS"][:10]
        self.sticky = set(A + N)
        self.newmem = {"A": A, "N": N}
        return list({s: 1 for s in A + N}.keys())

    def on_securities_changed(self, changes):
        for sec in changes.added_securities:
            s = sec.symbol
            if s.security_type != SecurityType.EQUITY or s in (self.qq, self.oneq, self.bil) or s in self.opt:
                continue
            try:
                o = self.add_option(s, Resolution.DAILY)
                o.set_filter(lambda u, s=s: self.flt(u, s))
                self.opt[s] = o.symbol
                self.held[o.symbol] = set()
            except Exception as e:
                if len(self.errs) < 8:
                    self.errs.append("addopt:" + str(e)[:50])

    def is_open(self, d):
        v = self.openc.get(d)
        if v is None:
            try:
                v = bool(self.hours.is_date_open(datetime(d.year, d.month, d.day)))
            except Exception:
                v = d.weekday() < 5
            self.openc[d] = v
        return v

    def expok(self, e):
        wd = e.weekday()
        return wd in (4, 5) or (wd == 3 and not self.is_open(e + timedelta(days=1)))

    def flt(self, u, s):
        return u.include_weeklys().expiration(3, 53).contracts(lambda cs: self.keep(cs, s))

    def keep(self, cs, s):
        """Per window keep the expiry closest to its target; in it, per right, the contract closest to each
        target delta (and to 0.5 for IV); plus every contract still held by some sleeve."""
        try:
            today = self.time.date()
            cs = list(cs)
            sl = self.sl.get(s)
            heldv = self.held.get(self.opt.get(s), set())
            exps = {}
            for c in cs:
                e = c.symbol.id.date.date()
                if e not in exps:
                    exps[e] = self.expok(e)
            chosen = {}
            for wi, (lo, hi) in enumerate(WIN):
                best = None
                for e, ok in exps.items():
                    if not ok:
                        continue
                    dte = (e - today).days
                    if lo <= dte <= hi:
                        k = (abs(dte - TDTE[wi]), dte)
                        if best is None or k < best[0]:
                            best = (k, e)
                if best is not None:
                    chosen[wi] = best[1]
            if sl is not None:
                sl.chosen = chosen
            ce = set(chosen.values())
            groups = {}
            out = []
            for c in cs:
                if c.symbol.value in heldv:
                    out.append(c)
                    continue
                e = c.symbol.id.date.date()
                if e not in ce:
                    continue
                g = c.greeks
                if g is None:
                    continue
                dl = abs(float(g.delta))
                if not (0.02 <= dl <= 0.62):
                    continue
                r = 0 if c.symbol.id.option_right == OptionRight.PUT else 1
                groups.setdefault((r, e), []).append((dl, float(c.symbol.id.strike_price), c))
            tg = DELTAS + (0.5,)
            for (r, e), lst in groups.items():
                pick = set()
                for t in tg:
                    tol = max(0.03, 0.3 * t)
                    bk = None
                    for x in lst:
                        dev = abs(x[0] - t)
                        if dev > tol:
                            continue
                        k = (dev, x[1] if r == 0 else -x[1])
                        if bk is None or k < bk[0]:
                            bk = (k, x)
                    if bk is not None:
                        pick.add(id(bk[1][2]))
                for x in lst:
                    if id(x[2]) in pick:
                        out.append(x[2])
            return out
        except Exception as ex:
            if len(self.errs) < 8:
                self.errs.append("keep:" + str(ex)[:50])
            return []

    # ------------------------------------------------------------------ data
    def on_data(self, data):
        for kv in data.dividends:
            self.pdiv[kv.key] = self.pdiv.get(kv.key, 0.0) + float(kv.value.distribution)
        for kv in data.splits:
            if kv.value.type == SplitType.SPLIT_OCCURRED:
                self.psplit[kv.key] = float(kv.value.split_factor)
        if self.time.hour < 12:
            return
        D = self.time.date()
        if D == self.last_proc:
            return
        self.last_proc = D
        try:
            b = self.securities[self.vix].get_last_data()
            if b is not None and float(b.value) > 0 and (not self.vixs or self.vixs[-1][0] < b.time.date()):
                self.vixs.append((b.time.date(), float(b.value)))
        except Exception as e:
            if len(self.errs) < 8:
                self.errs.append("vix:" + str(e)[:40])
        if not self.done:
            if D > END_BOOKS:
                self.finish()
            else:
                self.process(D, data)
        self.emit()

    def prior_vix(self, D):
        for i in range(len(self.vixs) - 1, -1, -1):
            if self.vixs[i][0] < D:
                return self.vixs[i][1]
        return None

    # ------------------------------------------------------------------ membership transitions
    def transition(self, L):
        """Month boundary (L = last processed day, None at start). Rebalance books to the new membership."""
        nm = self.newmem
        self.newmem = None
        if nm is None:
            return
        A, N = nm["A"], nm["N"]
        newm = [A[:5], A[:10], N[:5], N[:10]]
        union = list({s: 1 for s in A + N}.keys())
        self.cnt["chg"] += sum(len(set(newm[u]) ^ set(self.mem[u])) for u in range(NU)) // 2 if L else 0
        # liquidation cost fraction per existing sleeve
        lc = {}
        for s, sl in self.sl.items():
            S = sl.pS or 0.0
            nav = np.where(sl.nav > 0, sl.nav, 1.0)
            lc[s] = (sl.n * 100 * FILLV * sl.spr + sl.n * fees(np.maximum(sl.mark, 0.0)) + np.abs(sl.sh) * S * EQC) / nav
        if L is not None:
            for u in range(NU):
                old = self.mem[u]
                if not old:
                    continue
                g = {s: self.sl[s].nav / self.sl[s].base for s in old}
                tot = sum(g.values())
                wold = {s: g[s] / tot for s in old}
                wn = 1.0 / len(newm[u])
                cost = np.zeros(C)
                for s in set(old) | set(newm[u]):
                    w1 = wn if s in newm[u] else 0.0
                    w0 = wold.get(s, 0.0)
                    if s in lc:
                        cost += np.abs(w1 - w0) * lc[s]
                self.bnav[u] *= (1.0 - cost)
                # basket benchmark turnover cost
                gb = {s: self.sl[s].tri / self.sl[s].trib for s in old}
                tb = sum(gb.values())
                turn = sum(abs((wn if s in newm[u] else 0.0) - gb.get(s, 0.0) / tb) for s in set(old) | set(newm[u]))
                self.bk[u] *= (1.0 - turn * EQC)
        # drop sleeves no longer in any universe
        for s in list(self.sl.keys()):
            if s not in union:
                del self.sl[s]
                o = self.opt.pop(s, None)
                if o is not None:
                    self.held.pop(o, None)
                    try:
                        self.remove_security(o)
                    except Exception as e:
                        if len(self.errs) < 8:
                            self.errs.append("rmopt:" + str(e)[:40])
        for s in union:
            if s not in self.sl:
                sl = Sleeve(s)
                try:
                    h = self.history(s, 260, Resolution.DAILY, data_normalization_mode=DataNormalizationMode.SPLIT_ADJUSTED)
                    if h is not None and not h.empty:
                        today = self.time.date()
                        sl.ucl = [float(v) for idx, v in h["close"].items()
                                  if (idx[-1] if isinstance(idx, tuple) else idx).date() < today][-252:]
                except Exception as e:
                    if len(self.errs) < 8:
                        self.errs.append("hist:" + str(e)[:40])
                self.sl[s] = sl
                self.names_seen.add(s.value)
                if s not in self.opt:
                    try:
                        o = self.add_option(s, Resolution.DAILY)
                        o.set_filter(lambda u, s=s: self.flt(u, s))
                        self.opt[s] = o.symbol
                        self.held[o.symbol] = set()
                    except Exception as e:
                        if len(self.errs) < 8:
                            self.errs.append("addopt2:" + str(e)[:40])
        for s, sl in self.sl.items():
            sl.base = sl.nav.copy()
            sl.trib = sl.tri
            sl.compact()
        self.mem = newm
        self.union_max = max(self.union_max, len(union))
        self.bnav0 = self.bnav.copy()
        self.bk0 = self.bk.copy()

    # ------------------------------------------------------------------ daily step
    def process(self, D, data):
        sec = self.securities
        P1 = float(sec[self.oneq].close)
        BP = float(sec[self.bil].close)
        if P1 <= 0:
            return
        if self.last_D is None:
            self.transition(None)
        elif D.month != self.last_D.month:
            self.month_end(self.last_D)
            self.transition(self.last_D)
        rb = BP / self.bil_prev - 1.0 if (self.bil_prev and BP > 0) else 0.0
        if BP > 0:
            self.bil_prev = BP
        p = float(sec[self.qq].close)
        dv = self.pdiv.get(self.qq, 0.0)
        pp = self.pclose.get(self.qq)
        if pp and p > 0:
            self.blev[1] *= (p + dv) / pp
        if p > 0:
            self.pclose[self.qq] = p
        self.blev[0] = P1
        self.blev[2] = BP if BP > 0 else self.blev[2]
        nd = D + timedelta(days=1)
        for _ in range(10):
            if self.is_open(nd):
                break
            nd += timedelta(days=1)
        ndo = nd.toordinal()
        Do = D.toordinal()
        v = self.prior_vix(D)
        v20 = v is not None and v > 20
        t1 = _time.time()
        for s, sl in self.sl.items():
            self.step_sleeve(sl, D, Do, ndo, rb, v20, data)
        self.tbooks += _time.time() - t1
        self.pdiv = {}
        self.psplit = {}
        # books (daily NAV from month-start weights)
        for u in range(NU):
            ms = self.mem[u]
            if not ms:
                continue
            g = np.zeros(C)
            gb = 0.0
            for s in ms:
                sl = self.sl[s]
                g += sl.nav / sl.base
                gb += sl.tri / sl.trib
            self.bnav[u] = self.bnav0[u] * g / len(ms)
            self.bk[u] = self.bk0[u] * gb / len(ms)
        if self.ddon:
            np.maximum(self.pkH, self.bnav, out=self.pkH)
            np.minimum(self.ddH, self.bnav / self.pkH - 1, out=self.ddH)
            np.maximum(self.pkF, self.bnav, out=self.pkF)
            np.minimum(self.ddF, self.bnav / self.pkF - 1, out=self.ddF)
            lv = list(self.bk) + [self.blev[0], self.blev[1]]
            for j, x in enumerate(lv):
                z = self.bkdd[j]
                z[0] = max(z[0], x); z[1] = min(z[1], x / z[0] - 1)
                z[2] = max(z[2], x); z[3] = min(z[3], x / z[2] - 1)
        self.last_D = D
        self.ndays += 1

    def step_sleeve(self, sl, D, Do, ndo, rb, v20, data):
        sec = self.securities
        s = sl.sym
        if not sec.contains_key(s):
            return
        S = float(sec[s].close)
        if S <= 0:
            return
        cnt = self.cnt
        div = self.pdiv.get(s, 0.0)
        spf = self.psplit.get(s)
        cash, sh, n = sl.cash, sl.sh, sl.n
        # interest
        cash += cash * np.where(cash > 0, rb, rb + NEG)
        if sl.new:
            m = ISPO
            sh[m] = cash[m] * (1 - EQC) / S
            cash[m] = 0.0
            sl.new = False
        # split: settle open options at the last mark, scale shares and price history
        if spf:
            cnt["split"] += 1
            o = n > 0
            cash[o] -= n[o] * 100 * sl.mark[o]
            n[o] = 0.0
            sh /= spf
            sl.ucl = [x * spf for x in sl.ucl]
            if sl.pS:
                sl.pS *= spf
            sl.block = 2
        # early call assignment before ex-dividend (wheel): ITM at prior close and time value < dividend
        if div > 0 and sl.pS:
            o = (n > 0) & (sl.rt == 1)
            if o.any():
                intr = sl.pS - sl.K
                ea = o & (intr > 0) & (sl.mark - intr < div)
                if ea.any():
                    cash[ea] += n[ea] * 100 * sl.K[ea]
                    sh[ea] -= n[ea] * 100
                    sh[ea & (np.abs(sh) < 1e-9)] = 0.0
                    n[ea] = 0.0
                    self.d_early += ea
                    cnt["early_c"] += int(ea.sum())
        if div > 0:
            cash += sh * div
        # total-return index
        if sl.pS:
            sl.tri *= (S + div) / sl.pS
        # chain
        ch = data.option_chains.get(self.opt.get(s)) if s in self.opt else None
        qb = None
        qa = None
        sel_ok = np.zeros(2 * ND * NT, dtype=bool)
        selK = np.zeros(2 * ND * NT); selb = np.zeros(2 * ND * NT); sela = np.zeros(2 * ND * NT)
        sele = np.zeros(2 * ND * NT, dtype=np.int64); selc = np.zeros(2 * ND * NT, dtype=np.int64)
        iv = None
        if ch is None:
            cnt["nochain"] += 1
        else:
            nk = len(sl.csym)
            qb = {}
            cand = {}
            for c in ch:
                try:
                    b = float(c.bid_price); a = float(c.ask_price)
                    if not (b > 0 and a >= b):
                        continue
                    k = sl.cidx(c)
                    qb[k] = (b, a)
                    g = c.greeks
                    if g is None:
                        continue
                    dl = abs(float(g.delta))
                    if not (dl > 0) or not math.isfinite(dl):
                        continue
                    r = 0 if c.right == OptionRight.PUT else 1
                    e = c.expiry.date()
                    cand.setdefault((r, e), []).append((dl, float(c.strike), k, b, a, float(c.implied_volatility or 0.0)))
                except Exception as ex:
                    if len(self.errs) < 8:
                        self.errs.append("ch:" + str(ex)[:40])
            ivp = []
            for wi, e in sl.chosen.items():
                eff = e - timedelta(days=1) if e.weekday() == 5 else e
                for r in (0, 1):
                    lst = cand.get((r, e))
                    if not lst:
                        continue
                    for di, tgt in enumerate(DELTAS):
                        tol = max(0.03, 0.3 * tgt)
                        bk = None
                        for x in lst:
                            dev = abs(x[0] - tgt)
                            if dev > tol:
                                continue
                            kk = (dev, x[1] if r == 0 else -x[1])
                            if bk is None or kk < bk[0]:
                                bk = (kk, x)
                        if bk is not None:
                            j = (r * ND + di) * NT + wi
                            x = bk[1]
                            sel_ok[j] = True; selK[j] = x[1]; selb[j] = x[3]; sela[j] = x[4]
                            sele[j] = eff.toordinal(); selc[j] = x[2]
                    if wi == 2:
                        x = min(lst, key=lambda y: abs(y[0] - 0.5))
                        if abs(x[0] - 0.5) <= 0.15 and x[5] > 0:
                            ivp.append(x[5])
            if ivp:
                iv = sum(ivp) / len(ivp)
            nk2 = len(sl.csym)
            qb_arr = np.zeros(nk2); qa_arr = np.zeros(nk2)
            for k, (b, a) in qb.items():
                qb_arr[k] = b; qa_arr[k] = a
            qb, qa = qb_arr, qa_arr
        # filters (prior closes / prior IV)
        fl = np.zeros(NF, dtype=bool)
        fl[0] = True
        fl[1] = v20
        if len(sl.ivs) >= 126:
            w = sl.ivs[-252:]
            mn, mx = min(w), max(w)
            fl[2] = mx > mn and w[-1] >= mn + (mx - mn) * 2.0 / 3.0
        c_ = sl.ucl
        if len(c_) >= 200:
            fl[3] = c_[-1] > sum(c_[-200:]) / 200.0
            fl[4] = c_[-1] <= 0.9 * max(c_[-252:])
        # open positions
        o = n > 0
        block = np.zeros(C, dtype=bool)
        if o.any():
            self.d_in += o
            intr = np.where(sl.rt == 0, sl.K - S, S - sl.K)
            exp = o & (sl.eff < ndo)
            if exp.any():
                cnt["settle"] += int(exp.sum())
                itm = exp & (intr > 0)
                self.d_itm += itm
                cnt["itm"] += int(itm.sum())
                self.assign(sl, itm, intr)
                n[exp] = 0.0
                if REPL in np.nonzero(exp)[0] and s.value == "AAPL" and len(self.trace) < 30:
                    self.trace.append(f"X{D:%y%m%d}K{sl.K[REPL]:g}S{S:.2f}")
            live = n > 0
            if live.any() and qb is not None:
                cid = np.where(sl.cid >= 0, sl.cid, 0)
                cid = np.minimum(cid, len(qb) - 1) if len(qb) else cid
                b = qb[cid] if len(qb) else np.zeros(C)
                a = qa[cid] if len(qa) else np.zeros(C)
                hq = live & (b > 0) & (sl.cid < len(qb))
                mid = (b + a) * 0.5
                spr = a - b
                sl.mark = np.where(hq, mid, sl.mark)
                sl.spr = np.where(hq, spr, sl.spr)
                # early put assignment: bid at or below intrinsic
                ep = hq & (sl.rt == 0) & (intr > 0) & (b <= intr)
                if ep.any():
                    self.d_early += ep
                    cnt["early_p"] += int(ep.sum())
                    self.assign(sl, ep, intr)
                    n[ep] = 0.0
                    hq &= ~ep
                F = FILLV
                buy = mid + F * spr
                tp = hq & (CM == 1) & (buy <= 0.5 * sl.sv)
                st = hq & (CM == 3) & (mid >= 2.0 * sl.sv)
                ro = hq & (CM == 2) & ((sl.eff - Do) <= ROLLV)
                cl = tp | st | ro
                if cl.any():
                    cash[cl] -= n[cl] * 100 * buy[cl] + n[cl] * fees(buy[cl])
                    n[cl] = 0.0
                    block = tp | st
                    cnt["tp"] += int(tp.sum()); cnt["sl"] += int(st.sum()); cnt["roll"] += int(ro.sum())
                nq = (n > 0) & ~hq
                sl.mark = np.where(nq, np.maximum(sl.mark, intr), sl.mark)
            elif live.any():
                sl.mark = np.where(live, np.maximum(sl.mark, intr), sl.mark)
        # entries
        if sl.block > 0:
            sl.block -= 1
        elif sel_ok.any():
            rt = np.where(ISWH & (sh > 1e-9), 1, 0)
            j = rt * (ND * NT) + DT
            ok = (n == 0) & ~block & fl[CF] & sel_ok[j]
            if ok.any():
                bid, ask = selb[j], sela[j]
                px = (bid + ask) * 0.5 - FILLV * (ask - bid)
                ok &= px > 0
                K = selK[j]
                po = ok & ISPO
                if po.any():
                    nav = cash[po] + sh[po] * S
                    cost = np.abs(nav / S - sh[po]) * S * EQC
                    sh[po] = (nav - cost) / S
                    cash[po] = 0.0
                nn = np.where(ISCSP, cash / (K * 100 + 1e-12),
                              np.where(ISWH, np.where(rt == 1, sh / 100, np.maximum(cash, 0) / (K * 100 + 1e-12)),
                                       sh * S / (K * 100 + 1e-12)))
                ok &= nn > 0
                if ok.any():
                    cash[ok] += nn[ok] * 100 * px[ok] - nn[ok] * fees(px[ok])
                    n[ok] = nn[ok]
                    sl.cid[ok] = selc[j][ok]
                    sl.sv[ok] = px[ok]
                    sl.K[ok] = K[ok]
                    sl.eff[ok] = sele[j][ok]
                    sl.rt[ok] = rt[ok]
                    sl.mark[ok] = ((bid + ask) * 0.5)[ok]
                    sl.spr[ok] = (ask - bid)[ok]
                    self.d_ent += ok
                    self.d_cr += np.where(ok, px / np.maximum(K, 1e-9), 0.0)
                    cnt["entry"] += int(ok.sum())
                    if ok[REPL] and s.value == "AAPL" and len(self.trace) < 30:
                        self.trace.append(f"E{D:%y%m%d}K{K[REPL]:g}S{S:.2f}p{px[REPL]:.2f}")
        # flat PO sleeves: keep stock at 100% of NAV
        fp = ISPO & (n == 0)
        navp = cash + sh * S
        drift = fp & (np.abs(cash) > 0.02 * np.maximum(navp, 1e-9))
        if drift.any():
            cost = np.abs(navp[drift] / S - sh[drift]) * S * EQC
            sh[drift] = (navp[drift] - cost) / S
            cash[drift] = 0.0
        sl.nav = cash + sh * S - n * 100 * sl.mark
        self.d_days += 1
        # held contract set for the universe filter
        o = self.opt.get(s)
        if o is not None:
            hc = np.unique(sl.cid[n > 0])
            self.held[o] = set(sl.csym[int(k)].value for k in hc if 0 <= k < len(sl.csym))
        if iv is not None:
            sl.ivs.append(iv)
            if len(sl.ivs) > 300:
                sl.ivs = sl.ivs[-260:]
        sl.ucl.append(S)
        if len(sl.ucl) > 300:
            sl.ucl = sl.ucl[-260:]
        sl.pS = S

    def assign(self, sl, m, intr):
        """Settle at intrinsic (CSP / PO: cash; wheel: put -> buy stock at K, call -> deliver stock at K)."""
        if not m.any():
            return
        n = sl.n
        wh = m & ISWH
        ot = m & ~ISWH
        if ot.any():
            sl.cash[ot] -= n[ot] * 100 * np.maximum(intr[ot], 0.0)
        if wh.any():
            p = wh & (sl.rt == 0)
            c = wh & (sl.rt == 1)
            sl.cash[p] -= n[p] * 100 * sl.K[p]
            sl.sh[p] += n[p] * 100
            sl.cash[c] += n[c] * 100 * sl.K[c]
            sl.sh[c] -= n[c] * 100
            z = c & (np.abs(sl.sh) < 1e-9)
            sl.sh[z] = 0.0

    # ------------------------------------------------------------------ monthly output
    @staticmethod
    def rcode(r):
        return np.clip(np.round(r * 1e4) + 50000, 0, 99999).astype(np.int64)

    @staticmethod
    def pack(codes, k, base):
        m = len(codes)
        pad = (-m) % k
        if pad:
            codes = np.concatenate([codes, np.zeros(pad, dtype=np.int64)])
        codes = codes.reshape(-1, k)
        out = np.zeros(codes.shape[0], dtype=np.int64)
        for i in range(k):
            out = out * base + codes[:, i]
        return [float(x) for x in out]

    def month_end(self, L):
        ym = (L.year, L.month)
        out = []
        if self.ddon:
            out.append(900000000000000.0 + L.year * 100 + L.month)
            for j in range(3):
                a, b = self.bprevm[j], self.blev[j]
                r = b / a - 1 if (a and b) else 0.0
                out.append(float(round(r * 1e8) + 5_000_000_000))
            out.append(float(round((self.prior_vix(L + timedelta(days=1)) or 0.0) * 100)))
            for u in range(NU):
                r = self.bk[u] / self.bkprev[u] - 1
                out.append(float(round(r * 1e8) + 5_000_000_000))
            out.append(float(sum(len(self.mem[u]) * 100 ** u for u in range(NU))))
            r = (self.bnav / self.bprev - 1).reshape(-1)
            out.extend(self.pack(self.rcode(r), 3, 100000))
        if self.ddon and ym == (2018, 12):
            out.append(930000000000000.0)
            out.extend(self.pack(np.clip(np.round(-self.ddH[:, CX == 0].reshape(-1) * 1e3), 0, 999).astype(np.int64), 5, 1000))
            for j in range(NU + 2):
                out.append(float(min(9999, round(-self.bkdd[j][1] * 1e4))))
            self.pkH = self.bnav.copy(); self.ddH[:] = 0.0
            for j in range(NU + 2):
                self.bkdd[j][0] = ([*self.bk, self.blev[0], self.blev[1]])[j]; self.bkdd[j][1] = 0.0
        if self.ddon and ym == DIAG_YM:
            out.append(940000000000000.0)
            dd = np.maximum(self.d_days, 1)
            e = np.minimum(self.d_ent, 99999).astype(np.int64)
            it = np.minimum(self.d_itm, 99999).astype(np.int64)
            ip = np.minimum(np.round(1000 * self.d_in / dd), 999).astype(np.int64)
            ea = np.minimum(self.d_early, 99999).astype(np.int64)
            cr = np.minimum(np.round(1e5 * self.d_cr / np.maximum(self.d_ent, 1)), 99999).astype(np.int64)
            out.extend(float(x) for x in (e * 100000000 + it * 1000 + ip))
            out.extend(float(x) for x in (ea * 100000 + cr))
        if not self.ddon:
            self.ddon = True
            self.pkH = self.bnav.copy(); self.pkF = self.bnav.copy()
            self.ddH[:] = 0.0; self.ddF[:] = 0.0
            lv = [*self.bk, self.blev[0], self.blev[1]]
            for j in range(NU + 2):
                self.bkdd[j] = [lv[j], 0.0, lv[j], 0.0]
        self.bprevm = list(self.blev)
        self.bkprev = self.bk.copy()
        self.bprev = self.bnav.copy()
        self.memlog.append(f"{L:%y%m}:" + "/".join(str(len(m)) for m in self.mem))
        self.queue.extend(out)

    def finish(self):
        self.month_end(self.last_D)
        out = [950000000000000.0]
        m0 = (CX == 0)
        out.extend(self.pack(np.clip(np.round(-self.ddH[:, m0].reshape(-1) * 1e3), 0, 999).astype(np.int64), 5, 1000))
        out.extend(self.pack(np.clip(np.round(-self.ddF[:, m0].reshape(-1) * 1e3), 0, 999).astype(np.int64), 5, 1000))
        for j in range(NU + 2):
            out.append(float(min(9999, round(-self.bkdd[j][1] * 1e4)) * 10000 + min(9999, round(-self.bkdd[j][3] * 1e4))))
        out.append(960000000000000.0)
        self.queue.extend(out)
        self.done = True
        for o in list(self.opt.values()):
            try:
                self.remove_security(o)
            except Exception:
                pass

    def emit(self):
        k = min(SLOTS - 1, len(self.queue) - self.qpos)
        self.seq += 1
        self.plot("P00", "s0", float(800000000000000 + self.seq * 1000 + k))
        for i in range(k):
            sl = i + 1
            self.plot(f"P{sl // 10:02d}", f"s{sl % 10}", self.queue[self.qpos + i])
        self.qpos += k
        self.plotted += k
        if self.qpos > 200000:
            self.queue = self.queue[self.qpos:]
            self.qpos = 0

    def on_end_of_algorithm(self):
        left = len(self.queue) - self.qpos
        self.set_runtime_statistic("RUN", f"mech{int(MECH)} days {self.ndays} seq {self.seq} plotted {self.plotted} left {left} done {int(self.done)} umax {self.union_max} names {len(self.names_seen)}")
        self.set_runtime_statistic("TIME", f"total {_time.time() - self.t0:.0f}s sleeves {self.tbooks:.0f}s")
        self.set_runtime_statistic("CNT", " ".join(f"{k}:{v}" for k, v in self.cnt.items())[:200])
        tr = " ".join(self.trace)
        for i in range(3):
            if tr[i * 200:(i + 1) * 200]:
                self.set_runtime_statistic(f"TR{i}", tr[i * 200:(i + 1) * 200])
        ml = " ".join(self.memlog[-12:])
        self.set_runtime_statistic("MEM", ml[:200])
        self.set_runtime_statistic("NAMES", " ".join(sorted(self.names_seen))[:200])
        self.set_runtime_statistic("ERR", (" | ".join(self.errs[:8]) or "none")[:200])
