# region imports
from AlgorithmImports import *
# endregion
# Diagnostic only (no strategy returns): which trading days have a same-day (0DTE) expiry for QQQ / SPY / SPXW,
# how dense and how wide the minute quotes of those contracts are, where QuantConnect's minute data ends, and
# whether the Object Store can carry the grid's daily output. Ledger: docs/research_ledger_zero_dte.md, section 0.1.
from datetime import date, datetime, time, timedelta

CHECK_TIMES = (time(9, 45), time(10, 30), time(12, 0), time(13, 30), time(15, 0), time(15, 59))
SAMPLE_MONTHS = (3, 9)


class ZeroDteProbe(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2012, 2, 1)
        self.set_end_date(2026, 7, 31)
        self.set_cash(100_000)
        raw = DataNormalizationMode.RAW
        self.und = {t: self.add_equity(t, Resolution.DAILY, data_normalization_mode=raw).symbol for t in ("QQQ", "SPY")}
        self.spx = self.add_index("SPX", Resolution.DAILY).symbol
        self.spxw = Symbol.create_canonical_option(self.spx, "SPXW", Market.USA, "?SPXW")
        self.year = {}         # (ticker, year) -> [trading days, 0DTE days, per weekday counts]
        self.first = {}        # (ticker, weekday) -> first date with a same-day expiry
        self.sampled = set()
        self.samples = []
        self.errs = []
        self.last = None
        self.last_minute = None
        self.store = "untested"

    def log_err(self, tag, e):
        if len(self.errs) < 10:
            self.errs.append(f"{tag}:{str(e)[:50]}")

    def on_data(self, data):
        if self.time.hour < 12:
            return
        d = self.time.date()
        if d == self.last:
            return
        self.last = d
        wd = d.weekday()
        same = {}
        for t, sym in list(self.und.items()) + [("SPXW", self.spxw)]:
            try:
                cs = list(self.option_chain(sym))
            except Exception as e:
                self.log_err(t, e)
                continue
            z = [c for c in cs if c.expiry.date() == d]
            row = self.year.setdefault((t, d.year), [0, 0, [0] * 5])
            row[0] += 1
            if z:
                row[1] += 1
                row[2][wd] += 1
                self.first.setdefault((t, wd), d)
            same[t] = z
        # minute quote check: second Wednesday of March / September, the first Tuesday with a QQQ 0DTE,
        # and a few stress days
        key = None
        if d.month in SAMPLE_MONTHS and wd == 2 and 8 <= d.day <= 14:
            key = "std"
        elif d in (date(2020, 3, 16), date(2022, 6, 13), date(2024, 8, 5), date(2025, 4, 9), date(2026, 6, 17)):
            key = "stress"
        elif wd == 1 and same.get("QQQ") and ("tue", 0) not in self.sampled:
            key = "tue"
            self.sampled.add(("tue", 0))
        if key and same.get("QQQ"):
            self.sample_day(d, key, same["QQQ"])

    def sample_day(self, d, key, z):
        try:
            u = self.und["QQQ"]
            start = datetime.combine(d, time(9, 30))
            end = datetime.combine(d, time(16, 0))
            ub = self.history(TradeBar, u, start, end, Resolution.MINUTE)
            if ub is None or ub.empty:
                self.samples.append(f"{d:%y%m%d}{key[0]} nounder")
                return
            closes = ub["close"].values
            times = [ix[-1] if isinstance(ix, tuple) else ix for ix in ub.index]
            s0 = float(closes[0])
            strikes = sorted({float(c.strike) for c in z})
            near = [k for k in strikes if abs(k / s0 - 1) <= 0.03]
            step = min((b - a for a, b in zip(near, near[1:])), default=0)
            atm = min(strikes, key=lambda k: abs(k - s0))
            k2 = min(strikes, key=lambda k: abs(k - 0.98 * s0))
            k5 = min(strikes, key=lambda k: abs(k - 0.95 * s0))
            pick = {}
            for c in z:
                if c.right == OptionRight.PUT and float(c.strike) in (atm, k2, k5):
                    pick[float(c.strike)] = c.symbol
            qb = self.history(QuoteBar, list(pick.values()), start, end, Resolution.MINUTE)
            parts = [f"{d:%y%m%d}{key[0]} S{s0:.2f} n{len(closes)} st{step:g} nz{len(strikes)} nr{len(near)}"]
            if qb is None or qb.empty:
                parts.append("noquotes")
            else:
                for k, sym in sorted(pick.items(), reverse=True):
                    try:
                        q = qb.loc[sym]
                    except Exception:
                        parts.append(f"K{k:g}:none")
                        continue
                    ok = int(((q["bidclose"] > 0) & (q["askclose"] >= q["bidclose"])).sum())
                    cells = []
                    for ct in CHECK_TIMES:
                        sel = q[[ix.time() <= ct for ix in q.index]]
                        if sel.empty:
                            cells.append("-")
                            continue
                        b, a = float(sel["bidclose"].iloc[-1]), float(sel["askclose"].iloc[-1])
                        cells.append(f"{b:.2f}/{a:.2f}")
                    parts.append(f"K{k:g}[{ok}/{len(q)}]" + ",".join(cells))
            self.samples.append(" ".join(parts))
            t_last = max(times)
            if self.last_minute is None or t_last > self.last_minute:
                self.last_minute = t_last
        except Exception as e:
            self.log_err("sample", e)

    def on_end_of_algorithm(self):
        try:
            blob = "0123456789" * 200_000
            ok = self.object_store.save("zero_dte_probe_test", blob)
            back = self.object_store.read("zero_dte_probe_test")
            self.store = f"save {ok} read {len(back) if back else 0}"
            self.object_store.delete("zero_dte_probe_test")
        except Exception as e:
            self.store = "err " + str(e)[:80]
        try:
            h = self.history(TradeBar, self.und["QQQ"], timedelta(days=10), Resolution.MINUTE)
            lm = max(ix[-1] if isinstance(ix, tuple) else ix for ix in h.index) if h is not None and not h.empty else None
        except Exception as e:
            lm = "err " + str(e)[:40]
        self.set_runtime_statistic("STORE", self.store)
        self.set_runtime_statistic("LASTMIN", f"{lm} sampled {self.last_minute}")
        for t in ("QQQ", "SPY", "SPXW"):
            rows = [f"{y % 100}:{v[1]}/{v[0]}({'.'.join(str(x) for x in v[2])})" for (tt, y), v in sorted(self.year.items()) if tt == t]
            s = " ".join(rows)
            for i in range(0, len(s), 200):
                self.set_runtime_statistic(f"Y{t}{i // 200}", s[i:i + 200])
            self.set_runtime_statistic(f"F{t}", " ".join(f"{w}:{self.first[(t, w)]:%Y%m%d}" for w in range(5) if (t, w) in self.first))
        for i, s in enumerate(self.samples):
            self.log("SAMPLE " + s)
        self.set_runtime_statistic("NSAMP", str(len(self.samples)))
        self.set_runtime_statistic("ERR", (" | ".join(self.errs) or "none")[:200])
