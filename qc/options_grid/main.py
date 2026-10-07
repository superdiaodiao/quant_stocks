# region imports
from AlgorithmImports import *
# endregion
import math
import time as _time
from datetime import date, timedelta

# Registered in docs/research_ledger_qc_options_grid.md (section 0) before any grid result.
# Only these constants differ between the registered runs.
UNDER = "QQQ"      # "QQQ" (main, selectable) | "SPY" (reference)
FILL_F = 0.25      # sell at mid - F x spread, buy back at mid + F x spread; 0.25 main, 0.5 sensitivity
MECH = False       # True only for the registered 2012-02..2012-06 mechanism check

STRATS = ("CSP", "CC", "WH", "PO")
DELTAS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50)
TDTE = (7, 14, 30, 45)
WIN = ((4, 10), (10, 18), (24, 38), (38, 52))
ROLL = (3, 7, 14, 21)
MGMT = ("H", "TP", "R", "SL")
FILTS = ("ALL", "V20", "V25", "VT3", "BW", "DD10", "UP")
NCFG = len(STRATS) * len(DELTAS) * len(TDTE) * len(MGMT) * len(FILTS)   # 3584

CASH0 = 1_000_000.0
EQC = 0.0011                  # stock trade cost (0.1% slippage + ~0.01% IBKR per-share fee)
NEG = 0.015 / 252             # extra daily financing on negative cash (benchmark + 1.5%/yr)
START = date(2012, 2, 1)
END_BOOKS = date(2012, 6, 29) if MECH else date(2026, 5, 29)   # QC daily data ends 2026-07-09 (see ledger 1.2)
END_ALGO = date(2012, 8, 31) if MECH else date(2026, 7, 31)
H2_START = (2019, 1)
SLOTS = 400                   # 40 charts x 10 series, slot 0 = header
REPLICA = None                # index of CSP-0.25-30-H-ALL, set in initialize


def fee(px):
    return 0.65 if px >= 0.10 else (0.50 if px >= 0.05 else 0.25)


def cfg_index(s, d, t, m, f):
    return (((s * 8 + d) * 4 + t) * 4 + m) * 7 + f


class OptionsGrid(QCAlgorithm):
    """Paper books (no orders) for a 3,584-config option-selling grid on one underlying.
    Daily close quotes. Each config's monthly return and running half / full drawdowns are
    encoded into chart points (one point per trading day per slot) for offline decoding."""

    def initialize(self):
        global REPLICA
        self.set_start_date(START.year, START.month, START.day)
        self.set_end_date(END_ALGO.year, END_ALGO.month, END_ALGO.day)
        self.set_cash(100_000)
        self.settings.daily_precise_end_time = True
        raw = DataNormalizationMode.RAW
        self.u = self.add_equity(UNDER, Resolution.DAILY, data_normalization_mode=raw).symbol
        self.qq = self.u if UNDER == "QQQ" else self.add_equity("QQQ", Resolution.DAILY, data_normalization_mode=raw).symbol
        self.sp = self.u if UNDER == "SPY" else self.add_equity("SPY", Resolution.DAILY, data_normalization_mode=raw).symbol
        self.oneq = self.add_equity("ONEQ", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.ADJUSTED).symbol
        self.bil = self.add_equity("BIL", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.ADJUSTED).symbol
        self.vix = self.add_index("VIX", Resolution.DAILY).symbol
        self.vix3 = self.add_index("VIX3M", Resolution.DAILY).symbol
        self.set_benchmark(self.oneq)
        self.hours = self.securities[self.u].exchange.hours
        opt = self.add_option(UNDER, Resolution.DAILY)
        opt.set_filter(self.flt)
        self.osym = opt.symbol
        self.expok = {}
        self.openc = {}
        self.errs = []

        REPLICA = cfg_index(0, DELTAS.index(0.25), TDTE.index(30), 0, 0)
        self.books = []
        for s in range(4):
            for d in range(8):
                for t in range(4):
                    for m in range(4):
                        for f in range(7):
                            # 0 s,1 d,2 t,3 m,4 f,5 cash,6 sh,7 n,8 sv,9 entry,10 K,11 eff,12 right,13 mark,
                            # 14 navm,15 pkH,16 ddH,17 pkF,18 ddF,19 entries,20 inpos,21 credit,22 itm,23 nav
                            self.books.append([s, d, t, m, f, CASH0, 0.0, 0.0, "", 0.0, 0.0, None, 0, 0.0,
                                               CASH0, CASH0, 0.0, CASH0, 0.0, 0, 0, 0.0, 0, CASH0])
        # initial positions for stock-holding strategies are bought on the first processing day
        self.first = True

        # signals (lists of prior closes)
        self.ucl = []
        self.vixs = []     # [(date, value)]
        self.vix3s = []
        try:
            for sym, lst in ((self.u, None), (self.vix, self.vixs), (self.vix3, self.vix3s)):
                h = self.history(sym, 300, Resolution.DAILY)
                if h is None or h.empty:
                    self.errs.append(f"hist0 {sym.value}")
                    continue
                for idx, row in h.iterrows():
                    t = idx[-1] if isinstance(idx, tuple) else idx
                    dd = t.date()
                    if lst is None:
                        self.ucl.append(float(row["close"]))
                    else:
                        lst.append((dd, float(row["close"])))
        except Exception as e:
            self.errs.append("hist:" + str(e)[:60])

        # benchmarks: levels [ONEQ adj, QQQ TR, SPY TR, BIL adj]
        self.blev = [None, 1.0, 1.0, None]
        self.bprevm = [None, None, None, None]
        self.bdd = [[None, 0.0, None, 0.0] for _ in range(3)]   # pkH, ddH, pkF, ddF
        self.pclose = {}      # raw previous closes for TR
        self.pdiv = {}        # pending dividends per symbol (per share)
        self.bil_prev = None

        self.queue = []
        self.qpos = 0
        self.seq = 0
        self.last_proc = None
        self.last_D = None
        self.ddon = False
        self.done = False
        self.added = set()
        self.symobj = {}
        self.ndays = 0
        self.nochain = 0
        self.cnt = {"settle": 0, "itm": 0, "tp": 0, "sl": 0, "roll": 0, "entry": 0, "nosel": 0}
        self.trace = []
        self.t0 = _time.time()
        self.tbooks = 0.0
        self.plotted = 0
        self.nsel_ok = [0, 0]

    # ------------------------------------------------------------------ universe
    def is_open(self, d):
        v = self.openc.get(d)
        if v is None:
            try:
                v = bool(self.hours.is_date_open(datetime(d.year, d.month, d.day)))
            except Exception as e:
                if len(self.errs) < 6:
                    self.errs.append("open:" + str(e)[:40])
                v = d.weekday() < 5
            self.openc[d] = v
        return v

    def keep(self, c):
        try:
            g = c.greeks
            if g is None:
                return False
            dl = abs(float(g.delta))
            if dl < 0.02 or dl > 0.62:
                return False
            e = c.symbol.id.date.date()
            ok = self.expok.get(e)
            if ok is None:
                wd = e.weekday()
                ok = wd in (4, 5) or (wd == 3 and not self.is_open(e + timedelta(days=1)))
                self.expok[e] = ok
            return ok
        except Exception as ex:
            if len(self.errs) < 6:
                self.errs.append("keep:" + str(ex)[:40])
            return False

    def flt(self, u):
        return u.include_weeklys().expiration(3, 53).contracts(lambda cs: [c for c in cs if self.keep(c)])

    # ------------------------------------------------------------------ data
    def on_data(self, data):
        for kv in data.dividends:
            self.pdiv[kv.key] = self.pdiv.get(kv.key, 0.0) + float(kv.value.distribution)
        for kv in data.splits:
            if kv.value.type == SplitType.SPLIT_OCCURRED and len(self.errs) < 8:
                self.errs.append(f"split {kv.key.value} {self.time:%Y%m%d}")
        if self.time.hour < 12:      # skip midnight (dividend) slices; half-day bars end 13:00
            return
        D = self.time.date()
        if D == self.last_proc:
            return
        self.last_proc = D
        self.record_index(self.vix, self.vixs)
        self.record_index(self.vix3, self.vix3s)
        if not self.done:
            if D > END_BOOKS:
                self.finish_books()
            else:
                ch = data.option_chains.get(self.osym)
                self.process(D, ch)
        self.emit()

    def record_index(self, sym, lst):
        try:
            b = self.securities[sym].get_last_data()
            if b is None:
                return
            dd = b.time.date()
            v = float(b.value)
            if v > 0 and (not lst or lst[-1][0] < dd):
                lst.append((dd, v))
        except Exception as e:
            if len(self.errs) < 8:
                self.errs.append("idx:" + str(e)[:40])

    @staticmethod
    def prior(lst, D):
        for i in range(len(lst) - 1, -1, -1):
            if lst[i][0] < D:
                return lst[i][1], i
        return None, -1

    def filters(self, D):
        fl = [True, False, False, False, False, False, False]
        v, iv = self.prior(self.vixs, D)
        v3, _ = self.prior(self.vix3s, D)
        if v is not None:
            fl[1] = v > 20
            fl[2] = v > 25
            w = [x[1] for x in self.vixs[max(0, iv - 251):iv + 1]]
            mn, mx = min(w), max(w)
            fl[3] = mx > mn and v >= mn + (mx - mn) * 2.0 / 3.0
            if v3 is not None:
                fl[4] = v > v3
        c = self.ucl
        if len(c) >= 200:
            fl[5] = c[-1] <= 0.9 * max(c[-252:])
            fl[6] = c[-1] > sum(c[-200:]) / 200.0
        return fl

    # ------------------------------------------------------------------ daily step
    def process(self, D, chain):
        sec = self.securities
        S = float(sec[self.u].close)
        P1 = float(sec[self.oneq].close)
        BP = float(sec[self.bil].close)
        if S <= 0 or P1 <= 0:
            if len(self.errs) < 8:
                self.errs.append(f"px0 {D}")
            return
        # month change: yesterday was a month end (levels and book NAVs are still yesterday's)
        if self.last_D is not None and D.month != self.last_D.month:
            self.month_end(self.last_D)
        rb = BP / self.bil_prev - 1.0 if (self.bil_prev and BP > 0) else 0.0
        if BP > 0:
            self.bil_prev = BP
        # benchmark levels (TR from raw close + cash dividends)
        for j, sym in ((1, self.qq), (2, self.sp)):
            p = float(sec[sym].close)
            dv = self.pdiv.get(sym, 0.0)
            pp = self.pclose.get(sym)
            if pp and p > 0:
                self.blev[j] *= (p + dv) / pp
            if p > 0:
                self.pclose[sym] = p
        div = self.pdiv.get(self.u, 0.0)
        self.pdiv = {}
        self.blev[0] = P1
        self.blev[3] = BP if BP > 0 else self.blev[3]

        # next trading day after D (for settlement of expiries falling before it)
        nd = D + timedelta(days=1)
        for _ in range(10):
            if self.is_open(nd):
                break
            nd += timedelta(days=1)

        # chain -> quotes and candidates
        qt = {}
        cand = {}       # (right, expiry) -> [(absdelta, strike, sv, sym)]
        if chain is None:
            self.nochain += 1
        else:
            for c in chain:
                try:
                    bid = float(c.bid_price)
                    ask = float(c.ask_price)
                    sv = c.symbol.value
                    if bid > 0 and ask >= bid:
                        qt[sv] = (bid, ask)
                    else:
                        continue
                    g = c.greeks
                    if g is None:
                        continue
                    dl = abs(float(g.delta))
                    if not (dl > 0) or not math.isfinite(dl):
                        continue
                    r = 0 if c.right == OptionRight.PUT else 1
                    e = c.expiry.date()
                    cand.setdefault((r, e), []).append((dl, float(c.strike), sv, c.symbol))
                except Exception as ex:
                    if len(self.errs) < 8:
                        self.errs.append("ch:" + str(ex)[:40])
        # selections sel[r][d][t] = (sv, K, eff, bid, ask)
        sel = [[[None] * 4 for _ in range(8)] for _ in range(2)]
        for r in (0, 1):
            exps = sorted(e for (rr, e) in cand if rr == r)
            for ti, T in enumerate(TDTE):
                lo, hi = WIN[ti]
                best = None
                for e in exps:
                    dte = (e - D).days
                    if lo <= dte <= hi:
                        k = (abs(dte - T), dte)
                        if best is None or k < best[0]:
                            best = (k, e)
                if best is None:
                    continue
                e = best[1]
                eff = e - timedelta(days=1) if e.weekday() == 5 else e
                lst = cand[(r, e)]
                for di, tgt in enumerate(DELTAS):
                    tol = max(0.03, 0.3 * tgt)
                    bk = None
                    for x in lst:
                        dev = abs(x[0] - tgt)
                        if dev > tol:
                            continue
                        k = (dev, x[1] if r == 0 else -x[1])
                        if bk is None or k < bk[0]:
                            bk = (k, x)
                    self.nsel_ok[1] += 1
                    if bk is not None:
                        x = bk[1]
                        b, a = qt[x[2]]
                        sel[r][di][ti] = (x[2], x[1], eff, b, a)
                        self.symobj[x[2]] = x[3]
                        self.nsel_ok[0] += 1
        # quotes for held contracts outside today's filtered chain
        for bk in self.books:
            if bk[7] and bk[8] not in qt:
                so = self.symobj.get(bk[8])
                if so is not None and sec.contains_key(so):
                    b, a = float(sec[so].bid_price), float(sec[so].ask_price)
                    if b > 0 and a >= b:
                        qt[bk[8]] = (b, a)
                    else:
                        qt[bk[8]] = None
                else:
                    qt[bk[8]] = None

        fl = self.filters(D)
        t1 = _time.time()
        self.step_books(D, S, P1, rb, div, fl, sel, qt, nd)
        self.tbooks += _time.time() - t1

        # benchmark drawdowns (from March 2012)
        if self.ddon:
            for j in range(3):
                lv = self.blev[j]
                z = self.bdd[j]
                if z[0] is None:
                    z[0] = lv
                    z[2] = lv
                z[0] = max(z[0], lv)
                z[1] = min(z[1], lv / z[0] - 1)
                z[2] = max(z[2], lv)
                z[3] = min(z[3], lv / z[2] - 1)
        self.ucl.append(S)
        if len(self.ucl) > 400:
            self.ucl = self.ucl[-300:]
        self.last_D = D
        self.ndays += 1

    def step_books(self, D, S, P1, rb, div, fl, sel, qt, nd):
        F = FILL_F
        need = []
        cnt = self.cnt
        ddon = self.ddon
        first = self.first
        self.first = False
        for bk in self.books:
            s = bk[0]
            cash = bk[5]
            cash += cash * (rb if cash > 0 else rb + NEG)
            sh = bk[6]
            if first and s in (1, 3):
                px0 = S if s == 1 else P1
                sh = cash * (1 - EQC) / px0
                cash = 0.0
            if div and sh and (s == 1 or s == 2):
                cash += sh * div
            n = bk[7]
            block = False
            if n:
                bk[20] += 1
                K = bk[10]
                rt = bk[12]
                if bk[11] < nd:          # last trading day on / before the effective expiry
                    cnt["settle"] += 1
                    intr = (K - S) if rt == 0 else (S - K)
                    if intr > 0:
                        bk[22] += 1
                        cnt["itm"] += 1
                        if s == 2:
                            if rt == 0:
                                cash -= n * 100 * K
                                sh += n * 100
                            else:
                                cash += n * 100 * K
                                sh -= n * 100
                                if abs(sh) < 1e-6:
                                    sh = 0.0
                        else:
                            cash -= n * 100 * intr
                    n = 0.0
                    if bk[0] == 0 and len(self.trace) < 40 and bk is self.books[REPLICA]:
                        self.trace.append(f"X{D:%m%d}K{K:g}S{S:.2f}")
                else:
                    q = qt.get(bk[8])
                    if q is not None:
                        bid, ask = q
                        mid = (bid + ask) * 0.5
                        spr = ask - bid
                        bk[13] = mid
                        m = bk[3]
                        close = 0
                        if m == 1:
                            if mid + F * spr <= 0.5 * bk[9]:
                                close = 1
                        elif m == 3:
                            if mid >= 2.0 * bk[9]:
                                close = 3
                        elif m == 2:
                            if (bk[11] - D).days <= ROLL[bk[2]]:
                                close = 2
                        if close:
                            buy = mid + F * spr
                            cash -= n * 100 * buy + n * fee(buy)
                            n = 0.0
                            if close == 1:
                                cnt["tp"] += 1
                                block = True
                            elif close == 3:
                                cnt["sl"] += 1
                                block = True
                            else:
                                cnt["roll"] += 1
                    else:
                        intr = (K - S) if rt == 0 else (S - K)
                        if intr > bk[13]:
                            bk[13] = intr
            # entry
            if not n and not block and fl[bk[4]]:
                if s == 1:
                    rt = 1
                elif s == 2:
                    rt = 1 if sh > 1e-9 else 0
                else:
                    rt = 0
                p = sel[rt][bk[1]][bk[2]]
                if p is None:
                    cnt["nosel"] += 1
                else:
                    sv, K, eff, bid, ask = p
                    px = (bid + ask) * 0.5 - F * (ask - bid)
                    if px > 0:
                        if s == 1 or s == 3:
                            pr = S if s == 1 else P1
                            nav = cash + sh * pr
                            tgt = nav / pr
                            cost = abs(tgt - sh) * pr * EQC
                            sh = (nav - cost) / pr
                            cash = 0.0
                        if s == 0:
                            nn = cash / (K * 100)
                        elif s == 1:
                            nn = sh / 100
                        elif s == 2:
                            nn = sh / 100 if rt == 1 else (cash / (K * 100) if cash > 0 else 0.0)
                        else:
                            nn = sh * P1 / (K * 100)
                        if nn > 0:
                            cash += nn * 100 * px - nn * fee(px)
                            n = nn
                            bk[8] = sv
                            bk[9] = px
                            bk[10] = K
                            bk[11] = eff
                            bk[12] = rt
                            bk[13] = (bid + ask) * 0.5
                            bk[19] += 1
                            bk[21] += px / K
                            cnt["entry"] += 1
                            need.append(sv)
                            if bk is self.books[REPLICA] and len(self.trace) < 40:
                                self.trace.append(f"E{D:%m%d}K{K:g}S{S:.2f}p{px:.2f}e{eff:%m%d}")
            # flat stock-holding books: keep ~100% invested
            if not n and (s == 1 or s == 3):
                pr = S if s == 1 else P1
                nav = cash + sh * pr
                if nav > 0 and abs(cash) > 0.02 * nav:
                    tgt = nav / pr
                    cost = abs(tgt - sh) * pr * EQC
                    sh = (nav - cost) / pr
                    cash = 0.0
            bk[5] = cash
            bk[6] = sh
            bk[7] = n
            nav = cash + sh * (P1 if s == 3 else S) - (n * 100 * bk[13] if n else 0.0)
            bk[23] = nav
            if ddon:
                if nav > bk[15]:
                    bk[15] = nav
                elif bk[15] > 0:
                    x = nav / bk[15] - 1
                    if x < bk[16]:
                        bk[16] = x
                if nav > bk[17]:
                    bk[17] = nav
                elif bk[17] > 0:
                    x = nav / bk[17] - 1
                    if x < bk[18]:
                        bk[18] = x
        for sv in need:
            if sv not in self.added:
                so = self.symobj.get(sv)
                if so is not None:
                    try:
                        self.add_option_contract(so, Resolution.DAILY)
                    except Exception as e:
                        if len(self.errs) < 8:
                            self.errs.append("add:" + str(e)[:40])
                self.added.add(sv)

    # ------------------------------------------------------------------ monthly output
    def month_end(self, L):
        ym = (L.year, L.month)
        emit = self.ddon
        out = []
        if emit:
            out.append(900000000000000.0 + L.year * 100 + L.month)
            for j in range(4):
                a, b = self.bprevm[j], self.blev[j]
                r = b / a - 1 if (a and b) else 0.0
                out.append(float(round(r * 1e8) + 5_000_000_000))
            v, _ = self.prior(self.vixs, L + timedelta(days=1))
            out.append(float(round((v or 0.0) * 100)))
            for j in range(3):
                z = self.bdd[j]
                out.append(float(min(9999, round(-z[1] * 1e4)) * 10000 + min(9999, round(-z[3] * 1e4))))
        reset_h = ym == (H2_START[0] - 1, 12)
        for bk in self.books:
            nav = bk[23]
            if emit:
                r = nav / bk[14] - 1 if bk[14] > 0 else 0.0
                c1 = min(99999, max(0, round(r * 1e4) + 50000))
                c2 = min(9999, max(0, round(-bk[16] * 1e4)))
                c3 = min(9999, max(0, round(-bk[18] * 1e4)))
                out.append(float(c1 * 100000000 + c2 * 10000 + c3))
            bk[14] = nav
            if not self.ddon or reset_h:
                bk[15] = nav
                bk[16] = 0.0
            if not self.ddon:
                bk[17] = nav
                bk[18] = 0.0
        if emit and reset_h:
            for j in range(3):
                self.bdd[j][0] = self.blev[j]
                self.bdd[j][1] = 0.0
        if not self.ddon:
            self.ddon = True      # first month (Feb 2012) is the base, returns emitted from March 2012
            for j in range(3):
                self.bdd[j] = [self.blev[j], 0.0, self.blev[j], 0.0]
        self.bprevm = list(self.blev)
        self.queue.extend(out)

    def finish_books(self):
        self.month_end(self.last_D)
        out = [910000000000000.0]
        nd = max(1, self.ndays)
        for bk in self.books:
            e = min(9999, bk[19])
            ip = min(999, round(1000.0 * bk[20] / nd))
            cr = min(99999, round(1e5 * bk[21] / bk[19])) if bk[19] else 0
            it = min(999, bk[22])
            out.append(float(e * 100000000000 + ip * 100000000 + cr * 1000 + it))
        out.append(920000000000000.0)
        self.queue.extend(out)
        self.done = True
        try:
            self.remove_security(self.osym)
        except Exception as ex:
            self.errs.append("rm:" + str(ex)[:40])

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
        rb = self.books[REPLICA]
        self.set_runtime_statistic("RUN", f"{UNDER} F{FILL_F} mech{int(MECH)} days {self.ndays} seq {self.seq} plotted {self.plotted} left {left} done {int(self.done)}")
        self.set_runtime_statistic("TIME", f"total {_time.time() - self.t0:.0f}s books {self.tbooks:.0f}s nochain {self.nochain} added {len(self.added)} sel {self.nsel_ok[0]}/{self.nsel_ok[1]}")
        self.set_runtime_statistic("CNT", " ".join(f"{k}:{v}" for k, v in self.cnt.items()))
        self.set_runtime_statistic("REPL", f"nav {rb[23]:.0f} entries {rb[19]} itm {rb[22]} inpos {rb[20]}")
        tr = " ".join(self.trace)
        for i in range(4):
            if tr[i * 200:(i + 1) * 200]:
                self.set_runtime_statistic(f"TR{i}", tr[i * 200:(i + 1) * 200])
        self.set_runtime_statistic("ERR", (" | ".join(self.errs[:8]) or "none")[:200])
        self.set_runtime_statistic("SIG", f"vix {len(self.vixs)} {self.vixs[-1] if self.vixs else None} vix3 {len(self.vix3s)} ucl {len(self.ucl)}"[:200])
