# region imports
from AlgorithmImports import *
# endregion
# Diagnostic only (no strategy returns), for the single-stock 0DTE study (docs/research_ledger_stock_0dte.md, 0.1):
#  - a point-in-time monthly list of the most traded US stocks (mean daily dollar volume over the previous month);
#  - for each member, which Fridays (and any other weekdays) have a same-day expiry, from the chain listed on Fridays
#    (a Friday chain also shows next week's Monday..Thursday expiries if any exist);
#  - minute-quote quality of 0DTE contracts on sampled Fridays (spread in ticks and as a share of the mid, quote
#    presence, strike step, price grid of sub-$3 quotes) and whether the chain carries open interest.
from datetime import date, datetime, time, timedelta
import statistics as st

TOPN = 30                      # monthly list length (the study registers its pools from this list)
QN = 10                        # members sampled for quote quality
CHECK = (time(9, 45), time(10, 30), time(12, 0), time(13, 30), time(15, 0), time(15, 50))
SAMPLE_MONTHS = (3, 9)
STRESS = (date(2015, 8, 28), date(2018, 2, 9), date(2020, 3, 13), date(2020, 3, 20), date(2022, 5, 13),
          date(2024, 8, 9), date(2025, 4, 4), date(2025, 4, 11))


class StockZeroDteProbe(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2012, 1, 3)
        self.set_end_date(2026, 7, 31)
        self.set_cash(100_000)
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = DataNormalizationMode.RAW
        self.add_universe(self.select)
        self.spy = self.add_equity("SPY", Resolution.DAILY).symbol
        self.dv = {}            # symbol -> [sum of daily dollar volume this month, days]
        self.month = None
        self.members = []       # current monthly list (symbols, ranked)
        self.prev = []
        self.mlog = []
        self.names = set()
        self.year = {}          # year -> [member-Fridays, with 0DTE, members with 0DTE on >= 1 Friday]
        self.first = {}         # ticker -> first date with a same-day expiry
        self.other_wd = {}      # (ticker, weekday) -> first date of a non-Friday expiry seen in a Friday chain
        self.other_n = {}       # weekday -> count of (ticker, date) non-Friday expiries
        self.byname = {}        # ticker -> [Fridays as member, with 0DTE]
        self.samples = []
        self.oi = [0, 0]        # 0DTE near-money contracts seen on sampled days, with open interest > 0
        self.errs = []
        self.done_day = None
        self.t0 = datetime.now()

    def err(self, tag, e):
        if len(self.errs) < 12:
            self.errs.append(f"{tag}:{str(e)[:60]}")

    # ------------------------------------------------------------------ universe
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
            old = set(x.value for x in self.prev)
            new = [x.value for x in keep]
            self.mlog.append(f"{ym[0]}{ym[1]:02d} " + ",".join(new) + " | +" + ",".join(t for t in new if t not in old))
            self.prev = keep
            self.members = keep
            self.names |= set(new)
        for f in fundamental:
            if f.price >= 5.0 and f.dollar_volume > 0:
                v = self.dv.get(f.symbol)
                if v is None:
                    self.dv[f.symbol] = [float(f.dollar_volume), 1]
                else:
                    v[0] += float(f.dollar_volume)
                    v[1] += 1
        return self.members if self.members else [self.spy]

    # ------------------------------------------------------------------ daily check (Fridays and holiday Thursdays)
    def on_data(self, data):
        if self.time.hour < 12:
            return
        d = self.time.date()
        if d == self.done_day or not self.members:
            return
        self.done_day = d
        nxt = d + timedelta(days=1)
        is_last_of_week = d.weekday() == 4 or (d.weekday() == 3 and not self.securities[self.spy].exchange.hours.is_date_open(datetime(nxt.year, nxt.month, nxt.day)))
        if not is_last_of_week:
            return
        y = self.year.setdefault(d.year, [0, 0, set()])
        sample = (d.month in SAMPLE_MONTHS and 8 <= d.day <= 14) or d in STRESS
        for rank, s in enumerate(self.members):
            t = s.value
            try:
                cs = list(self.option_chain(s))
            except Exception as e:
                self.err(t, e)
                continue
            y[0] += 1
            bn = self.byname.setdefault(t, [0, 0])
            bn[0] += 1
            z = [c for c in cs if c.expiry.date() == d or (c.expiry.date() - d).days in (1, 2) and c.expiry.weekday() == 5]
            for c in cs:
                e = c.expiry.date()
                if d < e <= d + timedelta(days=7) and e.weekday() in (0, 1, 2, 3):
                    key = (t, e.weekday())
                    if key not in self.other_wd:
                        self.other_wd[key] = e
                    self.other_n[(t, e)] = 1
            if z:
                y[1] += 1
                y[2].add(t)
                bn[1] += 1
                self.first.setdefault(t, d)
                if sample and rank < QN:
                    self.sample(d, s, z)

    def sample(self, d, s, z):
        try:
            start = datetime.combine(d, time(9, 30))
            end = datetime.combine(d, time(16, 0))
            ub = self.history(TradeBar, s, start, end, Resolution.MINUTE)
            if ub is None or ub.empty:
                self.samples.append(f"{d:%y%m%d} {s.value} nounder")
                return
            s0 = float(ub["close"].values[14]) if len(ub) > 15 else float(ub["close"].values[0])
            strikes = sorted({float(c.strike) for c in z})
            near = [k for k in strikes if abs(k / s0 - 1) <= 0.05]
            step = min((b - a for a, b in zip(near, near[1:])), default=0)
            atm = min(strikes, key=lambda k: abs(k - s0))
            kp2 = min(strikes, key=lambda k: abs(k - 0.98 * s0))
            kc2 = min(strikes, key=lambda k: abs(k - 1.02 * s0))
            kp5 = min(strikes, key=lambda k: abs(k - 0.95 * s0))
            want = {("P", atm), ("C", atm), ("P", kp2), ("C", kc2), ("P", kp5)}
            pick = {}
            for c in z:
                r = "P" if c.right == OptionRight.PUT else "C"
                k = float(c.strike)
                if (r, k) in want:
                    pick[(r, k)] = c.symbol
                if abs(k / s0 - 1) <= 0.03:
                    self.oi[0] += 1
                    try:
                        if c.open_interest and float(c.open_interest) > 0:
                            self.oi[1] += 1
                    except Exception as e:
                        self.err("oi", e)
            if not pick:
                self.samples.append(f"{d:%y%m%d} {s.value} nopick")
                return
            qb = self.history(QuoteBar, list(pick.values()), start, end, Resolution.MINUTE, fill_forward=False)
            parts = [f"{d:%y%m%d} {s.value} S{s0:.2f} st{step:g} nk{len(near)}"]
            if qb is None or qb.empty:
                parts.append("noq")
                self.samples.append(" ".join(parts))
                return
            sub3, grid01 = 0, 0
            for (r, k), sym in sorted(pick.items()):
                try:
                    q = qb.loc[sym]
                except Exception:
                    parts.append(f"{r}{k:g}:none")
                    continue
                tt = [ix[-1] if isinstance(ix, tuple) else ix for ix in q.index]
                b = q["bidclose"].values if "bidclose" in q.columns else None
                a = q["askclose"].values if "askclose" in q.columns else None
                if a is None:
                    parts.append(f"{r}{k:g}:noask")
                    continue
                cells = []
                for ct in CHECK:
                    j = [i for i, x in enumerate(tt) if x.time() <= ct]
                    if not j:
                        cells.append("-")
                        continue
                    i = j[-1]
                    bb = 0.0 if (b is None or b[i] != b[i]) else float(b[i])
                    aa = float(a[i])
                    cells.append(f"{bb:.2f}/{aa:.2f}")
                for i in range(len(a)):
                    aa = a[i]
                    if aa == aa and 0 < aa < 3:
                        sub3 += 1
                        if abs(round(aa * 100) % 5) != 0:
                            grid01 += 1
                parts.append(f"{r}{k:g}[{len(a)}]" + ",".join(cells))
            parts.append(f"g{grid01}/{sub3}")
            self.samples.append(" ".join(parts))
        except Exception as e:
            self.err("sample", e)

    def on_end_of_algorithm(self):
        for x in self.mlog:
            self.log("M " + x)
        for x in self.samples:
            self.log("Q " + x)
        for (t, e) in sorted(self.other_n, key=lambda k: (k[1], k[0])):
            self.log(f"O {t} {e:%Y-%m-%d} wd{e.weekday()}")
        self.log("F " + " ".join(f"{t}:{v:%Y%m%d}" for t, v in sorted(self.first.items())))
        self.log("B " + " ".join(f"{t}:{v[1]}/{v[0]}" for t, v in sorted(self.byname.items())))

        def put(prefix, items, n=10):
            s = " ".join(items)
            for i in range(0, min(len(s), 200 * n), 200):
                self.set_runtime_statistic(f"{prefix}{i // 200}", s[i:i + 200])

        put("Y", [f"{y % 100}:{v[1]}/{v[0]}/{len(v[2])}" for y, v in sorted(self.year.items())])
        put("W", [f"{t}{w}:{e:%y%m%d}" for (t, w), e in sorted(self.other_wd.items(), key=lambda kv: kv[1])], 6)
        self.set_runtime_statistic("OI", f"{self.oi[1]}/{self.oi[0]}")
        self.set_runtime_statistic("N", f"months {len(self.mlog)} names {len(self.names)} samples {len(self.samples)} other {len(self.other_n)}")
        self.set_runtime_statistic("T", f"{(datetime.now() - self.t0).total_seconds():.0f}s")
        self.set_runtime_statistic("ERR", (" | ".join(self.errs) or "none")[:200])
