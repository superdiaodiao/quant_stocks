# region imports
from AlgorithmImports import *
# endregion
# Diagnostic only (no strategy returns), for the single-stock 0DTE study (docs/research_ledger_stock_0dte.md, 0.1):
#  - a point-in-time monthly list of the most traded US stocks (mean daily dollar volume over the previous month);
#  - for each member and month, how many Fridays had a same-day expiry (a Friday chain also lists next week's
#    Monday..Thursday expiries, so any non-Friday single-stock expiry shows up as well; a Thursday expiry before a
#    Friday holiday is counted apart as a holiday shift);
#  - minute-quote quality of 0DTE contracts on sampled Fridays: bid / ask at fixed times for the at-the-money put and
#    call and puts / calls 1%, 2% and 4% out of the money, quote presence, strike step, open interest.
# v1 wrote its tables to the log, which the free tier cuts at 10 KB; v2 sends them through the chart channel of
# qc/zero_dte (400 series, one point per series per minute, read back with qc/zero_dte/fetch.js).
from datetime import date, datetime, time, timedelta

TOPN = 30                      # monthly list length (the study registers its pools from this list)
CHECK = (time(9, 45), time(10, 30), time(12, 0), time(13, 30), time(15, 0), time(15, 50), time(15, 59))
SAMPLE_MONTHS = (2, 5, 8, 11)
STRESS = (date(2015, 8, 28), date(2018, 2, 9), date(2020, 3, 13), date(2020, 3, 20), date(2022, 5, 13),
          date(2024, 8, 9), date(2025, 4, 4), date(2025, 4, 11))
LAST_DAY = date(2026, 7, 24)   # last Friday processed; the remaining days drain the queue


class StockZeroDteProbe(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2012, 1, 3)
        self.set_end_date(2026, 7, 31)
        self.set_cash(100_000)
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = DataNormalizationMode.RAW
        self.add_universe(self.select)
        self.spy = self.add_equity("SPY", Resolution.MINUTE).symbol          # the drain clock
        self.dv = {}            # symbol -> [sum of daily dollar volume this month, days]
        self.month = None
        self.members = []
        self.ids = {}           # ticker -> id (order of first appearance)
        self.avail = {}         # ticker -> [member Fridays, Fridays with a same-day expiry, contracts] (this month)
        self.avail_ym = 0
        self.cur_day = None
        self.close_t = time(16, 0)
        self.other = {}         # (ticker, weekday) -> [first date, count] of Monday..Thursday expiries
        self.oi = [0, 0]
        self.errs = []
        self.done_day = None
        self.queue = []
        self.qpos = 0
        self.seq = 0
        self.emitted = 0
        self.nsamp = 0
        self.t0 = datetime.now()

    def err(self, tag, e):
        if len(self.errs) < 12:
            self.errs.append(f"{tag}:{str(e)[:60]}")

    def tid(self, t):
        if t not in self.ids:
            self.ids[t] = len(self.ids) + 1
        return self.ids[t]

    # ------------------------------------------------------------------ universe
    def select(self, fundamental):
        fundamental = list(fundamental)
        ym = (self.time.year, self.time.month)
        if self.month is None:
            self.month = ym
        if ym != self.month:
            self.month = ym
            self.flush_month()
            ranked = sorted(((v[0] / v[1], s) for s, v in self.dv.items() if v[1] >= 10), reverse=True)
            self.dv = {}
            keep, seen_co = [], set()
            fmap = {f.symbol: f for f in fundamental}
            for _, s in ranked:
                f = fmap.get(s)
                if f is None or not f.has_fundamental_data:
                    continue
                sr, cr = f.security_reference, f.company_reference
                if sr is None or cr is None or sr.security_type != "ST00000001":
                    continue
                co = cr.company_id or str(s.id)
                if co in seen_co:
                    continue                      # one share class per company (the most traded)
                seen_co.add(co)
                keep.append(s)
                if len(keep) >= TOPN:
                    break
            self.members = keep
            ids = [self.tid(s.value) for s in keep]
            self.queue.append(700_000_000_000_000.0 + (ym[0] * 100 + ym[1]) * 100 + len(ids))
            for i in range(0, len(ids), 5):
                chunk = ids[i:i + 5] + [0] * (5 - len(ids[i:i + 5]))
                v = 0
                for x in chunk:
                    v = v * 1000 + x
                self.queue.append(float(v))
        for f in fundamental:
            if f.price >= 5.0 and f.dollar_volume > 0:
                v = self.dv.get(f.symbol)
                if v is None:
                    self.dv[f.symbol] = [float(f.dollar_volume), 1]
                else:
                    v[0] += float(f.dollar_volume)
                    v[1] += 1
        return self.members if self.members else [self.spy]

    # ------------------------------------------------------------------ Fridays (and holiday Thursdays) at the close
    def on_data(self, data):
        self.emit()
        t = self.time
        d = t.date()
        if d != self.cur_day:
            self.cur_day = d
            nc = self.securities[self.spy].exchange.hours.get_next_market_close(datetime.combine(d, time(9, 31)), False)
            self.close_t = nc.time() if nc.date() == d else time(16, 0)
        if t.time() < self.close_t or d == self.done_day or not self.members or d > LAST_DAY:
            return
        self.done_day = d
        nxt = d + timedelta(days=1)
        last_of_week = d.weekday() == 4 or (d.weekday() == 3 and not self.securities[self.spy].exchange.hours.is_date_open(datetime(nxt.year, nxt.month, nxt.day)))
        if last_of_week:
            self.fridays(d)
        if d == LAST_DAY:
            self.flush_month()

    def flush_month(self):
        """Availability of the month that just ended: header 5e14 + yyyymm * 100 + rows, then one value per member:
        id * 1e6 + member Fridays * 1e3 + Fridays with a same-day expiry."""
        if not self.avail:
            return
        ym = self.avail_ym
        self.queue.append(500_000_000_000_000.0 + ym * 100 + len(self.avail))
        for tk, (n, z, nc) in self.avail.items():
            self.queue.append(float(self.tid(tk) * 1_000_000 + n * 1000 + z))
        self.avail = {}

    def fridays(self, d):
        ym = d.year * 100 + d.month
        self.avail_ym = ym
        sample = (d.month in SAMPLE_MONTHS and 8 <= d.day <= 14) or d in STRESS
        hours = self.securities[self.spy].exchange.hours
        for s in self.members:
            tk = s.value
            try:
                cs = list(self.option_chain(s))
            except Exception as e:
                self.err(tk, e)
                continue
            a = self.avail.setdefault(tk, [0, 0, 0])
            a[0] += 1
            z = [c for c in cs if c.expiry.date() == d or ((c.expiry.date() - d).days in (1, 2) and c.expiry.weekday() == 5)]
            for e in {c.expiry.date() for c in cs}:
                if d < e <= d + timedelta(days=7) and e.weekday() in (0, 1, 2, 3):
                    f1 = e + timedelta(days=1)
                    shifted = e.weekday() == 3 and not hours.is_date_open(datetime(f1.year, f1.month, f1.day))
                    o = self.other.setdefault((tk, e.weekday() + (5 if shifted else 0)), [e, 0])
                    o[1] += 1
            if z:
                a[1] += 1
                a[2] += len(z)
                if sample:
                    self.sample(d, s, z)

    def sample(self, d, s, z):
        try:
            start = datetime.combine(d, time(9, 30))
            end = datetime.combine(d, time(16, 0))
            ub = self.history(TradeBar, s, start, end, Resolution.MINUTE)
            if ub is None or ub.empty or len(ub) < 20:
                return
            closes = ub["close"].values
            s0 = float(closes[14])
            strikes = sorted({float(c.strike) for c in z})
            near = [k for k in strikes if abs(k / s0 - 1) <= 0.05]
            step = min((b - a for a, b in zip(near, near[1:])), default=0)
            atm = min(strikes, key=lambda k: abs(k - s0))
            want = [("P", atm), ("C", atm)]
            for x in (0.01, 0.02, 0.04):
                want.append(("P", min(strikes, key=lambda k: abs(k - (1 - x) * s0))))
                want.append(("C", min(strikes, key=lambda k: abs(k - (1 + x) * s0))))
            pick, oi = {}, {}
            for c in z:
                r = "P" if c.right == OptionRight.PUT else "C"
                k = float(c.strike)
                if (r, k) in want:
                    pick[(r, k)] = c.symbol
                    try:
                        oi[(r, k)] = int(c.open_interest or 0)
                    except Exception:
                        oi[(r, k)] = -1
                if abs(k / s0 - 1) <= 0.03:
                    self.oi[0] += 1
                    try:
                        if c.open_interest and float(c.open_interest) > 0:
                            self.oi[1] += 1
                    except Exception as e:
                        self.err("oi", e)
            qb = self.history(QuoteBar, list(pick.values()), start, end, Resolution.MINUTE, fill_forward=False) if pick else None
            self.nsamp += 1
            rec = [600_000_000_000_000.0 + ((d.year % 100) * 10_000 + d.month * 100 + d.day) * 1000 + self.tid(s.value),
                   float(round(s0 * 100)), float(round(step * 100)) * 1000 + min(999, len(near)), float(len(want))]
            for (r, k) in want:
                sym = pick.get((r, k))
                q = None
                if sym is not None and qb is not None and not qb.empty:
                    try:
                        q = qb.loc[sym]
                    except Exception:
                        q = None
                head = round(k * 100) * 100_000 + (1 if r == "C" else 0) * 10_000
                cells = [0.0] * len(CHECK)
                pres = 0
                if q is not None and "askclose" in q.columns:
                    tt = [ix[-1] if isinstance(ix, tuple) else ix for ix in q.index]
                    bb = q["bidclose"].values if "bidclose" in q.columns else [float("nan")] * len(tt)
                    aa = q["askclose"].values
                    for i in range(len(tt)):
                        if aa[i] == aa[i] and aa[i] > 0 and (bb[i] != bb[i] or bb[i] <= aa[i]):
                            pres += 1
                    for j, ct in enumerate(CHECK):
                        idx = [i for i, x in enumerate(tt) if x.time() <= ct]
                        if not idx:
                            continue
                        i = idx[-1]
                        b = 0.0 if bb[i] != bb[i] else float(bb[i])
                        a = float(aa[i]) if aa[i] == aa[i] else 0.0
                        age = min(99, int((datetime.combine(d, ct) - tt[i]).total_seconds() // 60))
                        cells[j] = float(min(999_999, round(b * 100)) * 10**8 + min(999_999, round(a * 100)) * 100 + age)
                rec.append(float(head + min(9999, pres)))
                rec.append(float(max(-1, min(10**9, oi.get((r, k), -1)))))
                rec.extend(cells)
            self.queue.extend(rec)
        except Exception as e:
            self.err("sample", e)

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

    def on_end_of_algorithm(self):
        left = len(self.queue) - self.qpos

        def put(prefix, items, n):
            s = " ".join(items)
            for i in range(0, min(len(s), 200 * n), 200):
                self.set_runtime_statistic(f"{prefix}{i // 200}", s[i:i + 200])

        put("D", [f"{v}:{k}" for k, v in sorted(self.ids.items(), key=lambda kv: kv[1])], 16)
        put("W", [f"{t}{w}:{v[0]:%y%m%d}:{v[1]}" for (t, w), v in sorted(self.other.items(), key=lambda kv: kv[1][0]) if w != 8], 4)
        thu = sum(v[1] for (t, w), v in self.other.items() if w == 8)
        self.set_runtime_statistic("OI", f"{self.oi[1]}/{self.oi[0]}")
        self.set_runtime_statistic("N", f"names {len(self.ids)} samples {self.nsamp} thu {thu} seq {self.seq} emitted {self.emitted} left {left}")
        self.set_runtime_statistic("T", f"{(datetime.now() - self.t0).total_seconds():.0f}s")
        self.set_runtime_statistic("ERR", (" | ".join(self.errs) or "none")[:200])
