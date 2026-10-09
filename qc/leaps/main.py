# region imports
from AlgorithmImports import *
# endregion
import math
import time as _time
from datetime import date, timedelta
import numpy as np

# Registered in docs/research_ledger_qc_leaps.md (section 0) before any result.
MECH = False       # True only for the registered 2012-02..2012-06 mechanism check

LEVS = (1.25, 1.5, 2.0)
EXPS = (91, 182, 365, 730)                                  # target calendar days to expiry
EWIN = ((70, 120), (140, 230), (300, 450), (600, 900))
DELS = (0.6, 0.7, 0.8, 0.9)
ROLLS = ("R13", "R60", "R30", "RB")
REBS = ("MON", "QTR")
FILLS = (0.25, 0.5)
NL, NE, NDL, NR, NRB, NX = 3, 4, 4, 4, 2, 2
K = NL * NE * NDL * NR * NRB                 # 384 configs (trials)
NO = K * NX                                  # option books (fill 0.25 and 0.5)
NI = K                                       # integer-contract $10k books (fill 0.25)
CLEV = tuple(round(1.0 + 0.05 * i, 2) for i in range(33))   # constant-leverage QQQ books 1.00..2.60
NCL = len(CLEV) * 2 * 2                      # x rebalance (MON/QTR) x financing (free / +1.5%)
NBK = NO + NI + NCL

CASH0 = 1_000_000.0
CASHI = 10_000.0
EQC = 0.0011
NEG = 0.015 / 252
DTOL = 0.05
START = date(2012, 2, 1)
END_BOOKS = date(2012, 6, 29) if MECH else date(2026, 5, 29)
END_ALGO = date(2012, 8, 31) if MECH else date(2026, 7, 10)
SLOTS = 400

# option config k = (((l*NE + e)*NDL + dl)*NR + r)*NRB + rb ; option book = 2k + x
_k = np.arange(K)
KRB = _k % NRB
KR = (_k // NRB) % NR
KDL = (_k // (NRB * NR)) % NDL
KE = (_k // (NRB * NR * NDL)) % NE
KL = _k // (NRB * NR * NDL * NE)
# option books arrays (NO + NI): book b < NO -> config b//2, fill b%2 ; b >= NO -> config b-NO, fill 0, integer
BK_K = np.concatenate([np.repeat(_k, 2), _k])
BK_X = np.concatenate([np.tile([0, 1], K), np.zeros(K, dtype=np.int64)])
BK_INT = np.concatenate([np.zeros(NO, dtype=bool), np.ones(NI, dtype=bool)])
NOB = NO + NI
FV = np.array(FILLS)[BK_X]
LV = np.array(LEVS)[KL[BK_K]]
EV = KE[BK_K]
DLV = np.array(DELS)[KDL[BK_K]]
RV = KR[BK_K]
RBV = KRB[BK_K]
SEL = EV * NDL + KDL[BK_K]                   # selection cell (expiry window, delta)


def fees(n, integer):
    f = 0.65 * n
    return np.where(integer & (n > 0), np.maximum(f, 1.0), f)


class Leaps(QCAlgorithm):
    def initialize(self):
        self.set_start_date(START.year, START.month, START.day)
        self.set_end_date(END_ALGO.year, END_ALGO.month, END_ALGO.day)
        self.set_cash(100_000)
        self.settings.daily_precise_end_time = True
        raw = DataNormalizationMode.RAW
        self.q = self.add_equity("QQQ", Resolution.DAILY, data_normalization_mode=raw).symbol
        self.oneq = self.add_equity("ONEQ", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.ADJUSTED).symbol
        self.bil = self.add_equity("BIL", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.ADJUSTED).symbol
        self.set_benchmark(self.oneq)
        self.hours = self.securities[self.q].exchange.hours
        o = self.add_option("QQQ", Resolution.DAILY)
        o.set_filter(self.flt)
        self.osym = o.symbol
        self.openc = {}
        self.errs = []
        self.chosen = {}
        self.heldv = set()
        n = NOB
        self.cash = np.where(BK_INT, CASHI, CASH0).astype(float)
        self.n = np.zeros(n)
        self.cid = np.full(n, -1, dtype=np.int64)
        self.K = np.zeros(n)
        self.eff = np.zeros(n, dtype=np.int64)
        self.dte0 = np.zeros(n)
        self.mark = np.zeros(n)
        self.spr = np.zeros(n)
        self.dl = np.zeros(n)
        self.ext = np.zeros(n)
        self.nav = self.cash.copy()
        # constant-leverage books: index c = (li*2 + reb)*2 + fin
        self.cl_lev = np.repeat(np.array(CLEV), 4)
        self.cl_reb = np.tile(np.repeat([0, 1], 2), len(CLEV))
        self.cl_fin = np.tile([0, 1], 2 * len(CLEV))
        self.cl_cash = np.full(NCL, CASH0)
        self.cl_sh = np.zeros(NCL)
        self.cl_nav = np.full(NCL, CASH0)
        self.ctab = {}
        self.csym = []
        # diagnostics per option / integer book
        self.s_exp = np.zeros(n); self.s_bor = np.zeros(n); self.s_theta = np.zeros(n)
        self.s_cost = np.zeros(n); self.s_days = np.zeros(n); self.s_zero = np.zeros(n)
        self.s_rolls = np.zeros(n); self.s_noq = 0
        self.navm = None
        self.allnav_prevm = None
        self.pk = None
        self.blev = [None, 1.0, None]
        self.bprevm = [None, None, None]
        self.pdiv = 0.0
        self.pclose = None
        self.bil_prev = None
        self.queue = []
        self.qpos = 0
        self.seq = 0
        self.last_proc = None
        self.last_D = None
        self.ddon = False
        self.done = False
        self.ndays = 0
        self.cnt = {"settle": 0, "roll": 0, "entry": 0, "reb": 0, "nosel": 0, "noq": 0}
        self.trace = []
        self.t0 = _time.time()
        self.plotted = 0
        self.selcnt = np.zeros(NE * NDL)

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

    def flt(self, u):
        return u.expiration(0, 900).contracts(self.keep)   # 0: keep held contracts after they fall below 60 days

    def keep(self, cs):
        try:
            today = self.time.date()
            cs = [c for c in cs if c.symbol.id.option_right == OptionRight.CALL]
            exps = {}
            for c in cs:
                e = c.symbol.id.date.date()
                if e not in exps:
                    exps[e] = self.expok(e)
            ch = {}
            for wi, (lo, hi) in enumerate(EWIN):
                best = None
                for e, ok in exps.items():
                    dte = (e - today).days
                    if ok and lo <= dte <= hi:
                        k = (abs(dte - EXPS[wi]), dte)
                        if best is None or k < best[0]:
                            best = (k, e)
                if best is not None:
                    ch[wi] = best[1]
            self.chosen = ch
            ce = set(ch.values())
            out = []
            grp = {}
            for c in cs:
                if c.symbol.value in self.heldv:
                    out.append(c)
                    continue
                e = c.symbol.id.date.date()
                if e not in ce or c.greeks is None:
                    continue
                dl = float(c.greeks.delta)
                if 0.45 <= dl <= 0.99:
                    grp.setdefault(e, []).append((dl, c))
            for e, lst in grp.items():
                pick = set()
                for t in DELS:
                    bk = min(lst, key=lambda x: abs(x[0] - t))
                    if abs(bk[0] - t) <= DTOL:
                        pick.add(id(bk[1]))
                out.extend(x[1] for x in lst if id(x[1]) in pick)
            return out
        except Exception as ex:
            if len(self.errs) < 8:
                self.errs.append("keep:" + str(ex)[:50])
            return []

    def cidx(self, c):
        v = c.symbol.value
        k = self.ctab.get(v)
        if k is None:
            k = len(self.csym)
            self.ctab[v] = k
            self.csym.append(c.symbol)
        return k

    def on_data(self, data):
        for kv in data.dividends:
            if kv.key == self.q:
                self.pdiv += float(kv.value.distribution)
        for kv in data.splits:
            if kv.value.type == SplitType.SPLIT_OCCURRED and len(self.errs) < 8:
                self.errs.append(f"split {kv.key.value} {self.time:%Y%m%d}")
        if self.time.hour < 12:
            return
        D = self.time.date()
        if D == self.last_proc:
            return
        self.last_proc = D
        if not self.done:
            if D > END_BOOKS:
                self.finish()
            else:
                self.process(D, data.option_chains.get(self.osym))
        self.emit()

    def process(self, D, chain):
        sec = self.securities
        S = float(sec[self.q].close)
        P1 = float(sec[self.oneq].close)
        BP = float(sec[self.bil].close)
        if S <= 0 or P1 <= 0:
            return
        newm = self.last_D is not None and D.month != self.last_D.month
        if newm:
            self.month_end(self.last_D)
        reb_day = self.last_D is None or newm
        rb = BP / self.bil_prev - 1.0 if (self.bil_prev and BP > 0) else 0.0
        if BP > 0:
            self.bil_prev = BP
        div = self.pdiv
        self.pdiv = 0.0
        if self.pclose:
            self.blev[1] *= (S + div) / self.pclose
        self.pclose = S
        self.blev[0] = P1
        self.blev[2] = BP if BP > 0 else self.blev[2]
        nd = D + timedelta(days=1)
        for _ in range(10):
            if self.is_open(nd):
                break
            nd += timedelta(days=1)
        ndo, Do = nd.toordinal(), D.toordinal()
        # chain
        qb = {}
        sel = [None] * (NE * NDL)
        if chain is not None:
            cand = {}
            for c in chain:
                try:
                    if c.right != OptionRight.CALL:
                        continue
                    b, a = float(c.bid_price), float(c.ask_price)
                    if not (b > 0 and a >= b):
                        continue
                    g = c.greeks
                    dl = float(g.delta) if g is not None else float("nan")
                    k = self.cidx(c)
                    qb[k] = (b, a, dl)
                    if math.isfinite(dl):
                        cand.setdefault(c.expiry.date(), []).append((dl, float(c.strike), k, b, a))
                except Exception as ex:
                    if len(self.errs) < 8:
                        self.errs.append("ch:" + str(ex)[:40])
            for wi, e in self.chosen.items():
                lst = cand.get(e)
                if not lst:
                    continue
                eff = e - timedelta(days=1) if e.weekday() == 5 else e
                for di, t in enumerate(DELS):
                    x = min(lst, key=lambda y: (abs(y[0] - t), -y[1]))
                    if abs(x[0] - t) <= DTOL:
                        sel[wi * NDL + di] = (x[2], x[1], x[3], x[4], x[0], eff.toordinal(), (e - D).days)
                        self.selcnt[wi * NDL + di] += 1
        nk = len(self.csym)
        QB, QA, QD = np.zeros(nk + 1), np.zeros(nk + 1), np.full(nk + 1, np.nan)
        for k, (b, a, dl) in qb.items():
            QB[k], QA[k], QD[k] = b, a, dl
        cash, n = self.cash, self.n
        cash += cash * np.where(cash > 0, rb, rb + NEG)
        # settle expiries
        o = n > 0
        exp = o & (self.eff < ndo)
        if exp.any():
            self.cnt["settle"] += int(exp.sum())
            self.s_theta += np.where(exp, self.ext * n * 100 / np.where(self.nav > 0, self.nav, 1.0), 0.0)
            cash[exp] += n[exp] * 100 * np.maximum(S - self.K[exp], 0.0)
            n[exp] = 0.0
        # quotes for held
        live = n > 0
        cidx = np.where(self.cid >= 0, self.cid, nk)
        b, a, dq = QB[cidx], QA[cidx], QD[cidx]
        hq = live & (b > 0)
        mid = (b + a) * 0.5
        intr = np.maximum(S - self.K, 0.0)
        noq = live & ~hq
        if noq.any():
            self.cnt["noq"] += int(noq.sum())
        newmark = np.where(hq, mid, np.maximum(self.mark, intr))
        newext = np.maximum(newmark - intr, 0.0)
        # time-value decay on held positions (before any trade today), as a fraction of NAV
        navp = np.where(self.nav > 0, self.nav, 1.0)
        self.s_theta += np.where(live, -(newext - self.ext) * n * 100 / navp, 0.0)
        self.mark = np.where(live, newmark, self.mark)
        self.ext = np.where(live, newext, self.ext)
        self.spr = np.where(hq, a - b, self.spr)
        self.dl = np.where(hq & np.isfinite(dq), dq, self.dl)
        # roll rules
        dte = self.eff - Do
        r13 = (RV == 0) & (dte <= self.dte0 / 3.0)
        r60 = (RV == 1) & (dte <= 60)
        r30 = (RV == 2) & (dte <= 30)
        rbd = (RV == 3) & ((dte <= 30) | (np.abs(self.dl - DLV) > 0.10))
        roll = hq & (r13 | r60 | r30 | rbd)
        integ = BK_INT
        if roll.any():
            px = mid - FV * (a - b)
            c = n * 100 * px - fees(n, integ)
            cash[roll] += c[roll]
            self.s_cost += np.where(roll, (n * 100 * FV * (a - b) + fees(n, integ)) / navp, 0.0)
            n[roll] = 0.0
            self.s_rolls += roll
            self.cnt["roll"] += int(roll.sum())
        nav = cash + n * 100 * self.mark
        # rebalance held positions
        isq = D.month in (1, 4, 7, 10)
        rebm = reb_day & ((RBV == 0) | isq)
        rr = rebm & (n > 0) & hq & (self.dl > 0.05)
        if rr.any():
            tgt = LV * nav / (100 * np.maximum(self.dl, 0.05) * S)
            tgt = np.where(integ, np.floor(tgt + 0.5), tgt)
            dn = np.where(rr, tgt - n, 0.0)
            buy = dn > 0
            prc = np.where(buy, mid + FV * (a - b), mid - FV * (a - b))
            cost_cash = dn * 100 * prc + fees(np.abs(dn), integ)
            afford = ~integ | ~buy | (cost_cash <= cash)
            dn = np.where(afford, dn, 0.0)
            cost_cash = np.where(afford, cost_cash, 0.0)
            cash -= cost_cash
            self.s_cost += np.abs(dn) * 100 * FV * (a - b) / np.where(nav > 0, nav, 1.0) + np.where(dn != 0, fees(np.abs(dn), integ), 0.0) / np.where(nav > 0, nav, 1.0)
            n += dn
            self.cnt["reb"] += int((dn != 0).sum())
        # entries
        flat = n == 0
        if flat.any():
            ok_any = False
            for cell in range(NE * NDL):
                s = sel[cell]
                if s is None:
                    continue
                m = flat & (SEL == cell)
                if not m.any():
                    continue
                ok_any = True
                k, Kx, bb, aa, dl, effo, dd = s
                px = (bb + aa) * 0.5 + FV[m] * (aa - bb)
                navm = (cash + n * 100 * self.mark)[m]
                tgt = LV[m] * navm / (100 * dl * S)
                im = integ[m]
                tgt = np.where(im, np.floor(tgt + 0.5), tgt)
                cst = tgt * 100 * px + fees(tgt, im)
                over = cst > cash[m]
                tgt = np.where(over & im, np.floor(np.maximum(cash[m] - 1.0, 0) / (100 * px + 0.65)), tgt)
                cst = tgt * 100 * px + fees(tgt, im)
                cash[m] -= cst
                n[m] = tgt
                idx = np.nonzero(m)[0]
                got = tgt > 0
                self.cid[idx] = k
                self.K[idx] = Kx
                self.eff[idx] = effo
                self.dte0[idx] = dd
                self.mark[idx] = (bb + aa) * 0.5
                self.spr[idx] = aa - bb
                self.dl[idx] = dl
                self.ext[idx] = max((bb + aa) * 0.5 - max(S - Kx, 0.0), 0.0)
                self.s_cost[idx] += np.where(got, (tgt * 100 * FV[m] * (aa - bb) + fees(tgt, im)) / np.maximum(navm, 1.0), 0.0)
                self.cnt["entry"] += int(got.sum())
                if m[0] and len(self.trace) < 25:
                    self.trace.append(f"E{D:%y%m%d}K{Kx:g}S{S:.2f}d{dl:.2f}n{tgt[0]:.0f}t{dd}")
            if not ok_any:
                self.cnt["nosel"] += 1
        self.nav = cash + n * 100 * self.mark
        self.heldv = set(self.csym[int(k)].value for k in np.unique(self.cid[n > 0]) if 0 <= k < len(self.csym))
        # diagnostics
        navs = np.where(self.nav > 0, self.nav, 1.0)
        ex = n * 100 * self.dl * S / navs
        self.s_exp += ex
        self.s_bor += np.maximum(n * 100 * self.dl * S - n * 100 * self.mark, 0.0) / navs
        self.s_zero += (n == 0)
        self.s_days += 1
        # constant-leverage QQQ books (TR: dividends to cash)
        cc, sh = self.cl_cash, self.cl_sh
        cc += cc * np.where(cc > 0, rb, rb + np.where(self.cl_fin == 1, NEG, 0.0))
        cc += sh * div
        if reb_day:
            m = (self.cl_reb == 0) | isq
            navc = cc + sh * S
            tsh = self.cl_lev * navc / S
            tr = np.where(m, tsh - sh, 0.0)
            cc -= tr * S + np.abs(tr) * S * EQC
            sh += tr
        self.cl_nav = cc + sh * S
        allnav = np.concatenate([self.nav, self.cl_nav])
        if self.ddon:
            np.maximum(self.pkH, allnav, out=self.pkH)
            np.minimum(self.ddH, allnav / self.pkH - 1, out=self.ddH)
            np.maximum(self.pkF, allnav, out=self.pkF)
            np.minimum(self.ddF, allnav / self.pkF - 1, out=self.ddF)
            lv = np.array([self.blev[0], self.blev[1]])
            np.maximum(self.bpk, lv, out=self.bpk)
            np.minimum(self.bdd, lv / self.bpk - 1, out=self.bdd)
            np.maximum(self.bpkF, lv, out=self.bpkF)
            np.minimum(self.bddF, lv / self.bpkF - 1, out=self.bddF)
        self.last_D = D
        self.ndays += 1

    @staticmethod
    def pack(codes, k, base):
        pad = (-len(codes)) % k
        if pad:
            codes = np.concatenate([codes, np.zeros(pad, dtype=np.int64)])
        codes = codes.reshape(-1, k)
        out = np.zeros(codes.shape[0], dtype=np.int64)
        for i in range(k):
            out = out * base + codes[:, i]
        return [float(x) for x in out]

    def ddblock(self, marker, dd):
        out = [marker]
        out.extend(self.pack(np.clip(np.round(-dd * 1e3), 0, 999).astype(np.int64), 5, 1000))
        return out

    def month_end(self, L):
        allnav = np.concatenate([self.nav, self.cl_nav])
        out = []
        if self.ddon:
            out.append(900000000000000.0 + L.year * 100 + L.month)
            for j in range(3):
                a, b = self.bprevm[j], self.blev[j]
                out.append(float(round((b / a - 1 if (a and b) else 0.0) * 1e8) + 5_000_000_000))
            r = allnav / self.allnav_prevm - 1
            out.extend(self.pack(np.clip(np.round(r * 1e4) + 50000, 0, 99999).astype(np.int64), 3, 100000))
            if (L.year, L.month) == (2018, 12):
                out.extend(self.ddblock(930000000000000.0, self.ddH))
                out.append(float(min(9999, round(-self.bdd[0] * 1e4)) * 10000 + min(9999, round(-self.bdd[1] * 1e4))))
                self.pkH = allnav.copy(); self.ddH[:] = 0.0
                self.bpk = np.array([self.blev[0], self.blev[1]]); self.bdd[:] = 0.0
        else:
            self.ddon = True
            self.pkH = allnav.copy(); self.pkF = allnav.copy()
            self.ddH = np.zeros(len(allnav)); self.ddF = np.zeros(len(allnav))
            self.bpk = np.array([self.blev[0], self.blev[1]]); self.bpkF = self.bpk.copy()
            self.bdd = np.zeros(2); self.bddF = np.zeros(2)
        self.allnav_prevm = allnav.copy()
        self.bprevm = list(self.blev)
        self.queue.extend(out)

    def finish(self):
        self.month_end(self.last_D)
        out = self.ddblock(950000000000000.0, self.ddH)
        out.extend(self.ddblock(951000000000000.0, self.ddF)[1:])
        for j in range(2):
            out.append(float(min(9999, round(-self.bdd[j] * 1e4)) * 10000 + min(9999, round(-self.bddF[j] * 1e4))))
        d = np.maximum(self.s_days, 1)
        # per option/integer book: avg exposure x1e4, avg borrowed x1e4, theta/yr x1e5, cost/yr x1e5, zero-days permille, rolls
        ex = np.clip(np.round(1e4 * self.s_exp / d), 0, 99999).astype(np.int64)
        bo = np.clip(np.round(1e4 * self.s_bor / d), 0, 99999).astype(np.int64)
        th = np.clip(np.round(1e5 * self.s_theta / d * 252) + 50000, 0, 99999).astype(np.int64)
        co = np.clip(np.round(1e5 * self.s_cost / d * 252), 0, 99999).astype(np.int64)
        zr = np.clip(np.round(1e3 * self.s_zero / d), 0, 999).astype(np.int64)
        ro = np.clip(self.s_rolls, 0, 9999).astype(np.int64)
        out.append(960000000000000.0)
        out.extend(float(x) for x in (ex * 10000000000 + bo * 100000 + th))
        out.extend(float(x) for x in (co * 10000000 + zr * 10000 + ro))
        out.append(970000000000000.0)
        self.queue.extend(out)
        self.done = True
        try:
            self.remove_security(self.osym)
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

    def on_end_of_algorithm(self):
        left = len(self.queue) - self.qpos
        self.set_runtime_statistic("RUN", f"mech{int(MECH)} days {self.ndays} seq {self.seq} plotted {self.plotted} left {left} done {int(self.done)} contracts {len(self.csym)}")
        self.set_runtime_statistic("TIME", f"total {_time.time() - self.t0:.0f}s")
        self.set_runtime_statistic("CNT", " ".join(f"{k}:{v}" for k, v in self.cnt.items()))
        self.set_runtime_statistic("SEL", " ".join(str(int(x)) for x in self.selcnt))
        tr = " ".join(self.trace)
        for i in range(3):
            if tr[i * 200:(i + 1) * 200]:
                self.set_runtime_statistic(f"TR{i}", tr[i * 200:(i + 1) * 200])
        self.set_runtime_statistic("ERR", (" | ".join(self.errs[:8]) or "none")[:200])
