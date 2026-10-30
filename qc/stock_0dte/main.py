# region imports
from AlgorithmImports import *
# endregion
import math
import time as _time
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

import sim as Z
from simx import DaySimX

# Registered in docs/research_ledger_stock_0dte.md (section 0) before any grid result.
# Only BOOK and MECH differ between the registered runs (one run per chunk of book days).
BOOK = (date(2024, 7, 29), date(2024, 8, 9))   # book days [first, last]; the algorithm runs longer to rank and drain
MECH = True                                    # True only for the registered mechanism checks (adds traces)
TOPN = 20                                      # names simulated each 0DTE day: the month's top 20 by dollar volume
NCH = 100                                      # export charts x 10 series, one point per series per emit (chartcap.py)
MAX_EMITS = 3990                               # a series keeps 4,000 points; later ones would be thinned away
HEADER = 29                                    # values per name-day header (the last one is the row count)
MAX_PRICE = 10_000.0                           # BRK.A (no listed options) would otherwise take a slot
DATA_END = date(2026, 7, 30)
NY = "America/New_York"
Z.configure(MECH)
U = Z.U
NE = Z.NE


class StockZeroDte(QCAlgorithm):
    """Paper books (no orders) for the single-stock 0DTE grid in sim.py / simx.py. At each session's close every
    member of the month's dollar-volume list with same-day-expiry contracts (Fridays and holiday Thursdays; from 2026
    also Mondays and Wednesdays for the largest names) is simulated from one minute-QuoteBar history pull, and every
    member whose next session is an expiry opens the overnight arm on those contracts. Header and position rows are
    queued and drained into chart points over the following minutes (SPY minute bars are the clock)."""

    def initialize(self):
        if BOOK[1] >= DATA_END:
            raise ValueError("BOOK must end before the last data day so the queue can drain")
        # two whole months before BOOK[0]'s month: the previous month needs its own ranking for the overnight arm
        # opened on the session before BOOK[0]
        m1 = (BOOK[0].replace(day=1) - timedelta(days=1)).replace(day=1)
        first = (m1 - timedelta(days=1)).replace(day=1) - timedelta(days=3)
        last = min(DATA_END, BOOK[1] + timedelta(days=10))
        self.set_start_date(first.year, first.month, first.day)
        self.set_end_date(last.year, last.month, last.day)
        self.set_cash(100_000)
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = DataNormalizationMode.RAW
        self.add_universe(self.select)
        self.spy = self.add_equity("SPY", Resolution.MINUTE).symbol
        self.vix = self.add_index("VIX", Resolution.DAILY).symbol
        self.hours = self.securities[self.spy].exchange.hours
        self.dv = {}
        self.month = None
        self.members = []
        self.ids = {}
        self.cik = {}
        self.openc = {}
        self.opt_tz = None
        self.errs = []
        self.cnt = {k: 0 for k in ("days", "names", "z", "half", "split", "nosplitchk", "thin", "rows", "penny0",
                                   "on_open", "on_lost")}
        self.t0 = _time.time()
        self.thist = 0.0
        self.queue = []
        self.qpos = 0
        self.seq = 0
        self.emitted = 0
        self.sim = DaySimX(self.load_quotes)
        self.cur_day = None
        self.close_t = time(16, 0)
        self.done_day = None
        self.pending_open = None
        self.on_book = {}                              # symbol -> overnight positions expiring today (from yesterday)
        self.penny = 1
        self.final = False

    def err(self, tag, e):
        if len(self.errs) < 10:
            self.errs.append(f"{tag}:{str(e)[:70]}")

    def tid(self, t):
        if t not in self.ids:
            self.ids[t] = len(self.ids) + 1
        return self.ids[t]

    # ------------------------------------------------------------------ universe (same rule as probe.py)
    def select(self, fundamental):
        fundamental = list(fundamental)
        ym = (self.time.year, self.time.month)
        if self.month is None:
            self.month = ym
        if ym != self.month:
            self.month = ym
            ranked = sorted(((v[0] / v[1], s) for s, v in self.dv.items() if v[1] >= 10), reverse=True)
            self.dv = {}
            keep, seen_co = [], set()
            fmap = {f.symbol: f for f in fundamental}
            for _, s in ranked:
                f = fmap.get(s)
                if f is None or not f.has_fundamental_data or f.price > MAX_PRICE:
                    continue
                sr, cr = f.security_reference, f.company_reference
                if sr is None or cr is None or sr.security_type != "ST00000001":
                    continue
                co = cr.company_id or str(s.id)
                if co in seen_co:
                    continue                      # one share class per company (the most traded)
                seen_co.add(co)
                keep.append(s)
                try:
                    self.cik.setdefault(s.value, str(int(cr.cik)) if cr.cik else "")
                except Exception:
                    self.cik.setdefault(s.value, "")
                if len(keep) >= TOPN:
                    break
            self.members = keep
        for f in fundamental:
            if f.price >= 5.0 and f.dollar_volume > 0:
                v = self.dv.get(f.symbol)
                if v is None:
                    self.dv[f.symbol] = [float(f.dollar_volume), 1]
                else:
                    v[0] += float(f.dollar_volume)
                    v[1] += 1
        return list({s: 1 for s in self.members + list(self.on_book)}.keys()) or [self.spy]

    # ------------------------------------------------------------------ clock
    def on_data(self, data):
        self.emit()
        t = self.time
        D = t.date()
        if D != self.cur_day:
            self.cur_day = D
            nc = self.hours.get_next_market_close(datetime.combine(D, time(9, 31)), False)
            self.close_t = min(nc.time(), time(16, 0)) if nc.date() == D else time(16, 0)
            if self.pending_open and t.time() >= time(9, 31):
                self.next_opens(D)                        # at the first bar, so the records drain the same day
            if D > BOOK[1] and not self.final and not self.pending_open:
                self.final = True
                self.queue.append(920_000_000_000_000.0)
        if t.time() < self.close_t or D == self.done_day:
            return
        self.done_day = D
        N = self.next_session(D)
        if self.members and (BOOK[0] <= D <= BOOK[1] or BOOK[0] <= N <= BOOK[1]):
            low = D.weekday() == 4 or (D.weekday() == 3 and not self.is_open(D + timedelta(days=1)))
            try:
                self.process_day(D, N, low, BOOK[0] <= D <= BOOK[1], BOOK[0] <= N <= BOOK[1])
            except Exception as e:
                self.err(f"day{D:%y%m%d}", e)

    # ------------------------------------------------------------------ calendar
    def is_open(self, d):
        v = self.openc.get(d)
        if v is None:
            try:
                v = bool(self.hours.is_date_open(datetime(d.year, d.month, d.day)))
            except Exception as e:
                self.err("open", e)
                v = d.weekday() < 5
            self.openc[d] = v
        return v

    def next_session(self, D):
        d = D + timedelta(days=1)
        for _ in range(10):
            if self.is_open(d):
                return d
            d += timedelta(days=1)
        return d

    def last_trading_day(self, e):
        """Last trading day of a contract that expires on calendar date e (Saturday-dated monthlies and
        holiday-shifted expiries trade for the last time on the previous open day)."""
        d = e
        for _ in range(7):
            if self.is_open(d):
                return d
            d -= timedelta(days=1)
        return e

    # ------------------------------------------------------------------ one session
    def vix_prev(self, D):
        try:
            h = self.history(self.vix, 10, Resolution.DAILY)
            best = None
            for idx, row in h.iterrows():
                tt = idx[-1] if isinstance(idx, tuple) else idx
                if (tt - timedelta(hours=1)).date() < D:
                    best = float(row["close"])
            return best
        except Exception as e:
            self.err("vix", e)
            return None

    def bars(self, sym, n, mode, D):
        """(dates, opens, closes) of the daily bars before and on D in the given normalisation. QuantConnect's
        split-adjusted history is scaled by every split up to the end of its data (also splits after D), so it is
        used only for ratios, never compared with raw levels."""
        h = self.history(sym, n, Resolution.DAILY, data_normalization_mode=mode)
        out = []
        if h is not None and not h.empty:
            for idx, row in h.iterrows():
                tt = idx[-1] if isinstance(idx, tuple) else idx
                d = (tt - timedelta(hours=1)).date()
                if d <= D:
                    out.append((d, float(row["open"]), float(row["close"])))
        return out

    def daily_ctx(self, sym, D, s_open, check_split=True):
        """Raw previous close, the trailing 20-day sd of daily log returns (split-adjusted closes) and whether a split
        took effect today: the raw open / previous close and the adjusted ones differ by more than 5%."""
        try:
            raw = self.bars(sym, 5, DataNormalizationMode.RAW, D)
            adj = self.bars(sym, 30, DataNormalizationMode.SPLIT_ADJUSTED, D)
        except Exception as e:
            self.err("hist", e)
            return None
        rp = [x for x in raw if x[0] < D]
        ap = [x for x in adj if x[0] < D]
        if not rp or not ap:
            return None
        prev = rp[-1][2]
        c = np.array([x[2] for x in ap][-21:])
        r = np.diff(np.log(c))
        sig = float(np.std(r, ddof=1)) if len(r) >= 10 else None
        split = False
        if not check_split:
            return prev, sig, split
        at = [x for x in adj if x[0] == D]
        if not at:
            try:
                start = datetime.combine(D, time(9, 30))
                hm = self.history(TradeBar, sym, start, start + timedelta(minutes=5), Resolution.MINUTE,
                                  data_normalization_mode=DataNormalizationMode.SPLIT_ADJUSTED)
                if hm is not None and not hm.empty:
                    at = [(D, float(hm["open"].values[0]), 0.0)]
            except Exception as e:
                self.err("adjopen", e)
        if at and s_open > 0 and prev > 0 and ap[-1][2] > 0 and at[0][1] > 0:
            split = abs(math.log((s_open / prev) / (at[0][1] / ap[-1][2]))) > 0.05
        else:
            self.cnt["nosplitchk"] += 1
        return prev, sig, split

    def chain(self, sym, days):
        """Contracts (strike, right, symbol, open interest) per last trading day in `days`, and the open interest by
        strike for max pain."""
        out = {d: ([], {}, {}) for d in days}
        try:
            for c in self.option_chain(sym):
                e = c.expiry.date()
                if e < min(days) or e > max(days) + timedelta(days=4):
                    continue
                ltd = self.last_trading_day(e)
                if ltd not in out:
                    continue
                k = float(c.strike)
                oi = float(c.open_interest or 0)
                r = 0 if c.right == OptionRight.PUT else 1
                lst, oi_p, oi_c = out[ltd]
                lst.append((k, r, c.symbol, oi))
                (oi_p if r == 0 else oi_c)[k] = oi
        except Exception as e:
            self.err("chain" + sym.value, e)
        return out

    def stock_path(self, sym, D, M, start_min=0):
        """Raw closes of the minute bars from start_min to the close (forward-filled), and the first bar's open."""
        S = np.full(M, np.nan)
        S_open = 0.0
        start = datetime.combine(D, time(9, 30)) + timedelta(minutes=start_min)
        end = datetime.combine(D, self.close_t)
        h = self.history(TradeBar, sym, start, end, Resolution.MINUTE)
        if h is not None and not h.empty:
            for idx, row in h.iterrows():
                tt = idx[-1] if isinstance(idx, tuple) else idx
                m = (tt.hour * 60 + tt.minute) - (9 * 60 + 30) - 1
                if 0 <= m < M:
                    S[m] = float(row["close"])
                    if m == 0:
                        S_open = float(row["open"])
        ok = ~np.isnan(S)
        nreal = int(ok[start_min:].sum())
        if ok.any():
            first = int(np.argmax(ok))
            S[:first] = S[first]
            for i in range(first + 1, M):
                if np.isnan(S[i]):
                    S[i] = S[i - 1]
        return S, S_open, nreal, ok

    def process_day(self, D, N, low, book_today, book_next):
        """Same-day names (members with contracts expiring today, plus names with overnight positions expiring today)
        are simulated and exported; then members whose next session is an expiry open the overnight arm."""
        M = (self.close_t.hour * 60 + self.close_t.minute) - (9 * 60 + 30)
        half = self.close_t < time(15, 59)
        today = {}
        nxt = {}
        for rank, sym in enumerate(self.members[:TOPN]):
            ch = self.chain(sym, [D, N])
            if ch[D][0]:
                today[sym] = (rank + 1, ch[D])
            if book_next and ch[N][0]:
                nxt[sym] = (rank + 1, ch[N])
        carried = self.on_book
        self.on_book = {}
        for sym in carried:
            if sym not in today:
                ch = self.chain(sym, [D])
                if ch[D][0]:
                    today[sym] = (0, ch[D])                    # no longer a member: only its overnight block counts
                else:
                    self.cnt["on_lost"] += 1
        vix = None
        if book_today and (today or low):
            self.cnt["days"] += 1
            if half:
                self.cnt["half"] += 1
            vix = self.vix_prev(D)
            self.queue.append(950_000_000_000_000.0 + ((D.year % 100) * 10_000 + D.month * 100 + D.day) * 100 + len(today))
            self.pending_open = (D, list(today))
            for sym, (rank, (contracts, oi_p, oi_c)) in today.items():
                self.cnt["names"] += 1
                try:
                    self.process_name(D, M, half, rank, sym, contracts, oi_p, oi_c, vix, carried.get(sym))
                except Exception as e:
                    self.err(f"{sym.value}{D:%y%m%d}", e)
                    self.queue.extend([900_000_000_000_000.0 + ((D.year % 100) * 10_000 + D.month * 100 + D.day) * 1000
                                       + self.tid(sym.value), 9.0 * 10**11] + [0.0] * (HEADER - 3) + [0.0])
        for sym, (rank, (contracts, oi_p, oi_c)) in nxt.items():
            try:
                self.open_overnight(D, M, rank, sym, contracts, oi_p, oi_c)
            except Exception as e:
                self.err(f"on{sym.value}{D:%y%m%d}", e)

    def open_overnight(self, D, M, rank, sym, contracts, oi_p, oi_c):
        mS = M - 1 - Z.ON_BEFORE
        S, S_open, nreal, ok = self.stock_path(sym, D, M)
        if not ok[mS - 5:mS + 2].any():
            return
        ctx = self.daily_ctx(sym, D, S_open)                        # the same 20 days as a same-day entry on D
        if not ctx or not ctx[1] or ctx[2]:
            if ctx and ctx[2]:
                self.cnt["split"] += 1                              # no overnight entry on a split day
            return
        sig = ctx[1]
        ks = sorted(set(oi_p) | set(oi_c))
        kmp = Z.max_pain(ks, [oi_p.get(k, 0.0) for k in ks], [oi_c.get(k, 0.0) for k in ks])
        self.sim.M, self.sim.day = M, D
        states, strad = self.sim.on_enter(D, S, contracts, sig, kmp)
        if states is None:
            return
        self.cnt["on_open"] += sum(1 for s in states if isinstance(s, dict))
        self.on_book[sym] = {"states": states, "rank": rank, "S": float(S[mS + 1]), "strad": strad,
                             "vix": self.vix_prev(D)}                 # VIX known before the entry (the day before D)

    def next_opens(self, D):
        """For the assignment convention (ledger 0.3): each name simulated on the previous 0DTE day gets a record
        with this session's raw opening price (first minute bar) and a split flag (the raw and the split-adjusted
        overnight gaps differ by more than 5%). Runs at this session's first bar."""
        prev_day, syms = self.pending_open
        self.pending_open = None
        ymd = (prev_day.year % 100) * 10_000 + prev_day.month * 100 + prev_day.day
        start = datetime.combine(D, time(9, 30))
        for sym in syms:
            o, flag = 0.0, 0
            try:
                op = {}
                for mode in (DataNormalizationMode.RAW, DataNormalizationMode.SPLIT_ADJUSTED):
                    h = self.history(TradeBar, sym, start, start + timedelta(minutes=2), Resolution.MINUTE,
                                     data_normalization_mode=mode)
                    if h is not None and not h.empty:
                        op[mode] = float(h["open"].values[0])
                rp = [x for x in self.bars(sym, 3, DataNormalizationMode.RAW, prev_day) if x[0] == prev_day]
                ap = [x for x in self.bars(sym, 3, DataNormalizationMode.SPLIT_ADJUSTED, prev_day) if x[0] == prev_day]
                o = op.get(DataNormalizationMode.RAW, 0.0)
                oa = op.get(DataNormalizationMode.SPLIT_ADJUSTED, 0.0)
                if o > 0 and oa > 0 and rp and ap and rp[0][2] > 0 and ap[0][2] > 0:
                    flag = int(abs(math.log((o / rp[0][2]) / (oa / ap[0][2]))) > 0.05)
            except Exception as e:
                self.err("open" + sym.value, e)
            self.queue.extend([970_000_000_000_000.0 + ymd * 1000 + self.tid(sym.value), float(round(o * U)),
                               float(flag)])

    def process_name(self, D, M, half, rank, sym, contracts, oi_p, oi_c, vix, on):
        S, S_open, nreal, ok = self.stock_path(sym, D, M)
        status = 0
        if nreal == 0:
            status = 2
        else:
            last_real = int(np.where(ok)[0].max())
            if last_real < M - 6 or nreal < 0.90 * M:
                status = 3                                     # too few real stock bars to simulate
                self.cnt["thin"] += 1
        ctx = self.daily_ctx(sym, D, S_open)
        prev, sig, split = (ctx if ctx else (0.0, None, False))
        if split:
            status = 4                                         # split effective today: skipped
            self.cnt["split"] += 1
        ks = sorted(set(oi_p) | set(oi_c))
        kmp = Z.max_pain(ks, [oi_p.get(k, 0.0) for k in ks], [oi_c.get(k, 0.0) for k in ks])
        rows, strad, pins, nload = [], [0.0] * NE, [0.0] * NE, 0
        self.penny = 1
        if status in (0, 3) and sig:                             # thin days are simulated too (status kept)
            self.sim.M, self.sim.day = M, D
            res = self.sim.simulate_day(D, S, contracts, sig, kmp, S_open, on["states"] if on else None)
            if res is not None:
                rows, strad, pins, nload = res
        if not rows:
            rows = [[-7.0]] * (Z.NS * Z.NB)                      # not simulated: every position, overnight ones too
            if on:
                self.cnt["on_lost"] += sum(1 for x in on["states"] if isinstance(x, dict))
        self.cnt["z"] += 1
        self.cnt["rows"] += len(rows)
        s_entry = [float(S[Z.mindex(hh, mm) + 1]) if nreal and Z.mindex(hh, mm) + 1 < M else 0.0 for hh, mm in Z.ENTRY]
        out = [900_000_000_000_000.0 + ((D.year % 100) * 10_000 + D.month * 100 + D.day) * 1000 + self.tid(sym.value),
               float((1 if half else 0) * 10**12 + status * 10**11 + min(9999, len(contracts)) * 10**6
                     + min(9999, nload) * 10**2 + self.penny * 10 + (1 if sig else 0)),
               float(round(S_open * U)), float(round(float(S[M - 1]) * U)) if nreal else 0.0, float(round(prev * U)),
               float(round((sig or 0.0) * 1e6)), float(round((kmp or 0.0) * U)), float(rank * 10**6 + nreal),
               float(round((vix or 0.0) * 100))]
        out += [float(round(x * U)) for x in s_entry]
        out += [float(round(x * U)) for x in strad]
        out += [float(round(x * U)) for x in pins]
        out += [float(on["rank"] if on else 0), float(round((on["S"] if on else 0.0) * U)),
                float(round((on["strad"] if on else 0.0) * U)), float(round(((on or {}).get("vix") or 0.0) * 100))]
        out.append(float(len(rows)))
        if len(out) != HEADER:
            raise ValueError(f"header {len(out)}")
        for r in rows:
            out.extend(r)
        self.queue.extend(out)

    # ------------------------------------------------------------------ quotes
    def load_quotes(self, D, contracts, S, sig, extra, sel_from):
        """Minute quotes of the day's contracts whose strikes an entry could use (decided from S at the entry
        selection minutes only; for the overnight arm from sel_from on) plus the strikes in `extra` (overnight legs
        to manage), as forward-filled [right, strike, minute] arrays with their age, and the open interest per
        [right, strike]. A row with an ask but no bid is a zero bid (LEAN drops all-zero bid bars). Sets the class's
        tick grid: penny unless (almost) every sub-3-dollar quote sits on the 0.05 grid."""
        M = self.sim.M
        band = Z.DaySim.band(sig) + max(Z.WINGS) + 0.01
        if sel_from is None:
            sel_px = [S[Z.mindex(hh, mm)] for hh, mm in Z.ENTRY if Z.mindex(hh, mm) < M]
        else:
            sel_px = [S[m] for m in range(sel_from, M)]
        lo_s, hi_s = float(np.nanmin(sel_px)), float(np.nanmax(sel_px))
        ext = set(extra or [])
        keep = [c for c in contracts if lo_s * (1 - band) <= c[0] <= hi_s * (1 + band) or c[0] in ext]
        if not keep:
            return None
        strikes = sorted({c[0] for c in keep})
        kidx = {k: i for i, k in enumerate(strikes)}
        nk = len(strikes)
        OI = np.zeros((2, nk))
        for k, r, s, oi in keep:
            OI[r, kidx[k]] = oi
        syms = [c[2] for c in keep]
        start = datetime.combine(D, time(9, 30)) + timedelta(minutes=max(0, sel_from or 0))
        end = datetime.combine(D, self.close_t)
        t1 = _time.time()
        qh = self.history(QuoteBar, syms, start, end, Resolution.MINUTE, fill_forward=False)
        self.thist += _time.time() - t1
        if qh is None or qh.empty:
            return None
        if self.opt_tz is None:
            try:
                self.opt_tz = str(self.market_hours_database.get_exchange_hours(
                    Market.USA, syms[0], syms[0].security_type).time_zone.id)
            except Exception as e:
                self.err("tz", e)
                self.opt_tz = NY
        names = list(qh.index.names)
        tl = qh.index.get_level_values(names.index("time") if "time" in names else len(names) - 1)
        sl = qh.index.get_level_values(names.index("symbol") if "symbol" in names else 0)
        smap = {}
        for k, r, s, oi in keep:
            for key in (s, str(s), s.value):
                smap[key] = (r, kidx[k])
        rr = np.empty(len(qh), int)
        kk = np.empty(len(qh), int)
        miss = 0
        cache = {}
        for i, s in enumerate(sl):
            v = cache.get(s)
            if v is None:
                v = smap.get(s) or smap.get(str(s)) or (-1, -1)
                cache[s] = v
            rr[i], kk[i] = v
            if v[0] < 0:
                miss += 1
        if miss:
            self.err("symmap", f"{miss} rows unmapped")
        tt = pd.DatetimeIndex(pd.to_datetime(tl))
        if self.opt_tz != NY:
            tt = tt.tz_localize(self.opt_tz).tz_convert(NY).tz_localize(None)
        mins = ((tt.hour * 60 + tt.minute) - (9 * 60 + 30) - 1).to_numpy()
        good = (rr >= 0) & (mins >= 0) & (mins < M)
        bid = np.full((2, nk, M), np.nan)
        ask = np.full((2, nk, M), np.nan)
        bc = qh["bidclose"].to_numpy(float) if "bidclose" in qh.columns else np.full(len(qh), np.nan)
        ac = qh["askclose"].to_numpy(float) if "askclose" in qh.columns else np.full(len(qh), np.nan)
        bid[rr[good], kk[good], mins[good]] = bc[good]
        ask[rr[good], kk[good], mins[good]] = ac[good]
        bid = np.where(np.isnan(bid) & ~np.isnan(ask), 0.0, bid)
        m_first = Z.mindex(*Z.ENTRY[0]) if sel_from is None else M - 1 - Z.ON_BEFORE
        early = good & (mins <= m_first)                         # the tick grid from quotes before the first fill
        px = np.concatenate([bc[early], ac[early]])
        px = px[(px > 0) & (px < 3.0) & ~np.isnan(px)]
        if len(px) < 20:
            px = np.concatenate([bc[good], ac[good]])
            px = px[(px > 0) & (px < 3.0) & ~np.isnan(px)]
        off = int((np.abs(np.round(px * 100) % 5) > 0).sum())
        self.penny = 1 if off >= max(3, 0.01 * len(px)) else 0
        if not self.penny:
            self.cnt["penny0"] += 1
        Z.configure_day(self.penny)
        valid = (~np.isnan(ask)) & (ask > 0) & (bid >= 0) & (bid <= ask)
        ar = np.arange(M)
        last = np.maximum.accumulate(np.where(valid, ar, -1), axis=2)
        seen = last >= 0
        li = np.where(seen, last, 0)
        B = np.where(seen, np.take_along_axis(bid, li, axis=2), np.nan)
        A = np.where(seen, np.take_along_axis(ask, li, axis=2), np.nan)
        age = np.where(seen, ar - last, 10_000)
        return np.array(strikes), B, A, age, OI, len(keep)

    # ------------------------------------------------------------------ export
    def emit(self):
        cap = NCH * 10 - 1
        k = min(cap, len(self.queue) - self.qpos)
        if k <= 0 or self.seq >= MAX_EMITS:
            return
        self.seq += 1
        self.plot("P00", "s0", float(800_000_000_000_000 + self.seq * 1000 + k))
        for i in range(k):
            sl = i + 1
            self.plot(f"P{sl // 10:02d}", f"s{sl % 10}", self.queue[self.qpos + i])
        self.qpos += k
        self.emitted += k
        if self.qpos > 200_000:
            self.queue = self.queue[self.qpos:]
            self.qpos = 0

    def on_end_of_algorithm(self):
        left = len(self.queue) - self.qpos
        self.set_runtime_statistic("RUN", f"book {BOOK[0]}..{BOOK[1]} mech{int(MECH)} top{TOPN} nch{NCH} shapes {Z.NS} seq {self.seq} emitted {self.emitted} left {left} final {int(self.final)} tz {self.opt_tz}")
        self.set_runtime_statistic("TIME", f"total {_time.time() - self.t0:.0f}s hist {self.thist:.0f}s sim {self.sim.tsim:.0f}s")
        self.set_runtime_statistic("CNT", " ".join(f"{k}:{v}" for k, v in self.cnt.items())[:200])
        self.set_runtime_statistic("SIM", " ".join(f"{k}:{v}" for k, v in self.sim.cnt.items())[:200])
        self.set_runtime_statistic("ERR", (" | ".join(self.errs) or "none")[:200])
        d = " ".join(f"{v}:{k}:{self.cik.get(k, '')}" for k, v in sorted(self.ids.items(), key=lambda kv: kv[1]))
        for i in range(0, min(len(d), 200 * 12), 200):
            self.set_runtime_statistic(f"D{i // 200:02d}", d[i:i + 200])
        for i, x in enumerate(self.sim.trace[:8]):
            self.set_runtime_statistic(f"TR{i:02d}", x[:200])
        for i, x in enumerate(self.sim.ftrace[:8]):
            self.set_runtime_statistic(f"FT{i:02d}", x[:200])
