# region imports
from AlgorithmImports import *
# endregion
import math
import time as _time
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

import sim as Z

# Registered in docs/research_ledger_zero_dte.md (section 0) before any grid result.
# Only UNDER, BOOK and MECH differ between the registered runs (one run per underlying x chunk).
UNDER = "QQQ"                                  # "QQQ" (main, selectable) | "SPXW" (reference)
BOOK = (date(2024, 7, 29), date(2024, 8, 9))   # book days [first, last]; the algorithm runs a few days longer to drain
MECH = True                                    # True only for the registered mechanism checks (adds traces)
DATA_END = date(2026, 7, 30)
NY = "America/New_York"
Z.configure(UNDER, MECH)
U = Z.U
NE = Z.NE


class ZeroDte(QCAlgorithm):
    """Paper books (no orders) for the 0DTE grid in sim.py. Each 0DTE day is simulated at that day's close from one
    minute-QuoteBar history pull; per-position rows are queued and drained into chart points over the following
    minutes (QQQ's extended-hours bars and SPX's 16:01-16:15 bars drain the same evening)."""

    def initialize(self):
        if BOOK[1] >= DATA_END:
            raise ValueError("BOOK must end before the last data day so the queue can drain")
        first = BOOK[0] - timedelta(days=3)
        last = min(DATA_END, BOOK[1] + timedelta(days=7))
        self.set_start_date(first.year, first.month, first.day)
        self.set_end_date(last.year, last.month, last.day)
        self.set_cash(100_000)
        if Z.IS_INDEX:
            self.u = self.add_index("SPX", Resolution.MINUTE).symbol
            self.canon = Symbol.create_canonical_option(self.u, "SPXW", Market.USA, "?SPXW")
        else:
            self.u = self.add_equity(UNDER, Resolution.MINUTE, data_normalization_mode=DataNormalizationMode.RAW,
                                     extended_market_hours=True).symbol
            self.canon = self.u                  # option_chain(underlying) lists the equity options
        self.vix = self.add_index("VIX", Resolution.DAILY).symbol
        # session calendar in New York time for both runs (the SPX index hours are kept in Chicago time)
        self.hours = self.market_hours_database.get_exchange_hours(
            Market.USA, Symbol.create("SPY", SecurityType.EQUITY, Market.USA), SecurityType.EQUITY)
        self.opt_tz = None
        self.openc = {}
        self.errs = []
        self.cnt = {k: 0 for k in ("days", "zdays", "half", "nochain", "rows", "thin")}
        self.t0 = _time.time()
        self.thist = 0.0
        self.queue = []
        self.qpos = 0
        self.seq = 0
        self.emitted = 0
        self.sim = Z.DaySim(self.load_quotes)
        self.day = None
        self.final = False

    def err(self, tag, e):
        if len(self.errs) < 10:
            self.errs.append(f"{tag}:{str(e)[:70]}")

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

    def last_trading_day(self, e):
        """Last trading day of a contract that expires on calendar date e (Saturday-dated monthlies and
        holiday-shifted expiries trade for the last time on the previous open day)."""
        d = e
        for _ in range(7):
            if self.is_open(d):
                return d
            d -= timedelta(days=1)
        return e

    # ------------------------------------------------------------------ day state
    def new_day(self, D):
        self.day = D
        self.cnt["days"] += 1
        nxt = self.hours.get_next_market_close(datetime.combine(D, time(9, 31)), False)
        self.close_t = min(nxt.time(), time(16, 0)) if nxt.date() == D else time(16, 0)
        self.half = self.close_t < time(15, 59)
        self.M = (self.close_t.hour * 60 + self.close_t.minute) - (9 * 60 + 30)     # bars ending 09:31 .. close
        self.S = np.full(self.M, np.nan)
        self.S_open = None
        self.processed = False

    @staticmethod
    def minute_index(t):
        return (t.hour * 60 + t.minute) - (9 * 60 + 30) - 1

    # ------------------------------------------------------------------ data
    def on_data(self, data):
        t = self.time
        if t.hour < 9 or (t.hour == 9 and t.minute < 31):
            self.emit()
            return
        D = t.date()
        if D != self.day:
            self.new_day(D)
        bar = data.bars.get(self.u)
        if bar is not None and not bar.is_fill_forward:
            m = self.minute_index(t.time())
            if 0 <= m < self.M:
                self.S[m] = float(bar.close)
                if self.S_open is None and m == 0:
                    self.S_open = float(bar.open)
        if not self.processed and t.time() >= self.close_t:
            self.processed = True
            if BOOK[0] <= D <= BOOK[1]:
                try:
                    self.process_day(D)
                except Exception as e:
                    self.err(f"day{D:%y%m%d}", e)
                nxt = [D + timedelta(days=i) for i in range(1, 6)]
                if not any(self.is_open(x) and x <= BOOK[1] for x in nxt) and not self.final:
                    self.final = True
                    self.queue.append(920_000_000_000_000.0)
        self.emit()

    # ------------------------------------------------------------------ one day
    def vix_prev(self, D):
        try:
            h = self.history(self.vix, 10, Resolution.DAILY)
            best = None
            for idx, row in h.iterrows():
                tt = idx[-1] if isinstance(idx, tuple) else idx
                dd = (tt - timedelta(hours=1)).date()          # daily bars end at 16:15 or midnight of the next day
                if dd < D:
                    best = float(row["close"])
            return best
        except Exception as e:
            self.err("vix", e)
            return None

    def process_day(self, D):
        self.cnt["rows"] += 1
        S = self.S.copy()
        ok = ~np.isnan(S)
        nreal = int(ok.sum())
        status = 0
        if self.half:
            self.cnt["half"] += 1
        if nreal == 0:
            status = 2
        else:
            last_real = int(np.where(ok)[0].max())
            if last_real < self.M - 6 or nreal < 0.95 * self.M:
                status = 3                                     # too few real underlying bars to simulate
                self.cnt["thin"] += 1
            first = int(np.argmax(ok))
            S[:first] = S[first]
            for i in range(first + 1, self.M):
                if np.isnan(S[i]):
                    S[i] = S[i - 1]
        vix = self.vix_prev(D)
        hdr_s_entry = [0.0] * NE
        hdr_strad = [0.0] * NE
        z, nchain, nload = 0, 0, 0
        rows = []
        contracts = []
        try:
            for c in self.option_chain(self.canon):
                e = c.expiry.date()
                if e < D or e > D + timedelta(days=4):
                    continue
                if self.last_trading_day(e) != D:
                    continue
                contracts.append((float(c.strike), 0 if c.right == OptionRight.PUT else 1, c.symbol))
        except Exception as e:
            self.err("chain", e)
        nchain = len(contracts)
        if nchain:
            z = 1
            self.cnt["zdays"] += 1
        else:
            self.cnt["nochain"] += 1
        if nreal:
            for ei, (hh, mm) in enumerate(Z.ENTRY):
                mF = Z.mindex(hh, mm) + 1
                if 0 <= mF < self.M:
                    hdr_s_entry[ei] = float(S[mF])
        if z and status == 0:
            self.sim.M, self.sim.day = self.M, D
            res = self.sim.simulate_day(D, S, contracts, vix)
            if res is not None:
                rows, hdr_strad, nload = res
        sig = 1 if (vix or 0) > 0 else 0
        out = [900_000_000_000_000.0 + D.year * 10_000 + D.month * 100 + D.day,
               float(z * 10**13 + (1 if self.half else 0) * 10**12 + status * 10**11 + min(9999, nchain) * 10**6
                     + min(9999, nload) * 10**2 + sig),
               float(round((self.S_open or 0.0) * U)), float(round(float(S[self.M - 1]) * U)) if nreal else 0.0,
               float(round((vix or 0.0) * 100))]
        out += [float(round(x * U)) for x in hdr_s_entry]
        out += [float(round(x * U)) for x in hdr_strad]
        out.append(float(nreal))
        out.append(float(len(rows)))
        for r in rows:
            out.extend(r)
        self.queue.extend(out)

    # ------------------------------------------------------------------ quotes
    def load_quotes(self, D, contracts, S, vix):
        """Minute quotes of the day's contracts whose strikes any entry could use (decided from S at the entry
        selection minutes only), as forward-filled [right, strike, minute] arrays with their age. A row with an ask
        but no bid is a zero bid (LEAN drops all-zero bid bars)."""
        band = Z.DaySim.band(vix) + max(Z.WINGS) + 0.01
        sel_px = [S[Z.mindex(hh, mm)] for hh, mm in Z.ENTRY if Z.mindex(hh, mm) < self.M]
        lo_s, hi_s = float(np.nanmin(sel_px)), float(np.nanmax(sel_px))
        keep = [c for c in contracts if lo_s * (1 - band) <= c[0] <= hi_s * (1 + band)]
        if not keep:
            return None
        strikes = sorted({c[0] for c in keep})
        kidx = {k: i for i, k in enumerate(strikes)}
        nk = len(strikes)
        syms = [c[2] for c in keep]
        start = datetime.combine(D, time(9, 30))
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
        for k, r, s in keep:
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
        good = (rr >= 0) & (mins >= 0) & (mins < self.M)
        bid = np.full((2, nk, self.M), np.nan)
        ask = np.full((2, nk, self.M), np.nan)
        bc = qh["bidclose"].to_numpy(float) if "bidclose" in qh.columns else np.full(len(qh), np.nan)
        ac = qh["askclose"].to_numpy(float) if "askclose" in qh.columns else np.full(len(qh), np.nan)
        bid[rr[good], kk[good], mins[good]] = bc[good]
        ask[rr[good], kk[good], mins[good]] = ac[good]
        bid = np.where(np.isnan(bid) & ~np.isnan(ask), 0.0, bid)
        valid = (~np.isnan(ask)) & (ask > 0) & (bid >= 0) & (bid <= ask)
        ar = np.arange(self.M)
        last = np.maximum.accumulate(np.where(valid, ar, -1), axis=2)
        seen = last >= 0
        li = np.where(seen, last, 0)
        B = np.where(seen, np.take_along_axis(bid, li, axis=2), np.nan)
        A = np.where(seen, np.take_along_axis(ask, li, axis=2), np.nan)
        age = np.where(seen, ar - last, 10_000)
        return np.array(strikes), B, A, age, len(keep)

    # ------------------------------------------------------------------ export
    def emit(self):
        k = min(399, len(self.queue) - self.qpos)
        if k <= 0:
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
        self.set_runtime_statistic("RUN", f"{UNDER} book {BOOK[0]}..{BOOK[1]} mech{int(MECH)} shapes {Z.NS} seq {self.seq} emitted {self.emitted} left {left} final {int(self.final)} tz {self.opt_tz}")
        self.set_runtime_statistic("TIME", f"total {_time.time() - self.t0:.0f}s hist {self.thist:.0f}s sim {self.sim.tsim:.0f}s")
        self.set_runtime_statistic("CNT", " ".join(f"{k}:{v}" for k, v in self.cnt.items())[:200])
        self.set_runtime_statistic("SIM", " ".join(f"{k}:{v}" for k, v in self.sim.cnt.items())[:200])
        self.set_runtime_statistic("ERR", (" | ".join(self.errs) or "none")[:200])
        for i, x in enumerate(self.sim.trace[:30]):
            self.set_runtime_statistic(f"TR{i:02d}", x[:200])
