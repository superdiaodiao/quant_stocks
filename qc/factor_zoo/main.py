# region imports
from AlgorithmImports import *
# endregion
import math
from datetime import date, timedelta

# (code, sign): +1 = highest values are best, -1 = lowest values are best
FACTORS = [
    ("EP", 1), ("BM", 1), ("SP", 1), ("CFP", 1), ("EBITEV", 1),
    ("FCFY", 1), ("ROE", 1), ("ROA", 1), ("ROIC", 1), ("GPA", 1),
    ("GM", 1), ("OM", 1), ("SG", -1), ("EPSG", 1), ("AG", -1),
    ("NSI", -1), ("ACC", -1), ("DE", -1), ("CR", 1), ("AT", 1),
]
BENCH = ("QQQ", "ONEQ", "SPY")


class FactorZoo(QCAlgorithm):
    """Registered in docs/research_ledger_qc_factors.md before the run. Paper portfolios only (no orders).

    Universe each month (point-in-time, delisted names included): price >= $5, dollar volume >= $1M,
    has fundamental data, market cap >= $300M, common stock (ST00000001), not a depositary receipt,
    company country USA; no exchange filter (exchange_id is the current listing); one share class
    per company (largest dollar volume). On the month's first selection (data as of the prior
    close) rank 20 factors; for each form the top 10 and the top decile (equal weight, ties: larger
    market cap first, factor skipped when < 50 valid names). Holding period: close of the month's
    first trading day to the close of the next month's first trading day (adjusted prices).
    Benchmarks QQQ / ONEQ / SPY over the same dates. Results written as runtime statistics.
    """

    TOP_N = 10
    MIN_MCAP = 300e6
    MIN_PRICE = 5.0
    MIN_DV = 1e6
    MIN_VALID = 50
    FIRST_FORMATION = date(1999, 1, 1)
    ENTRY_WAIT = 5

    def initialize(self):
        self.set_start_date(1998, 1, 1)
        self.set_end_date(2026, 7, 31)
        self.set_cash(10_000)
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = DataNormalizationMode.ADJUSTED
        self.add_universe(self.select)
        self.bench = {k: self.add_equity(k, Resolution.DAILY).symbol for k in BENCH}
        self.set_benchmark(self.bench["SPY"])
        self.last_month = None
        self.shares_hist = {}      # (y, m) -> {symbol: split-adjusted shares}
        self.pending = None        # {key: [symbols]} formed at this month's selection
        self.pending_ym = None
        self.pending_meta = None
        self.sel_date = None
        self.current = None        # open paper portfolios
        self.entry = {}            # symbol -> entry price for the open portfolios
        self.miss = {}             # symbol -> days left to find an entry price
        self.delist_px = {}
        self.trim = False
        self.rows = []             # (ym, meta, bench returns, portfolio returns, n_noentry, n_noexit)

    # ----- helpers --------------------------------------------------------------------------
    @staticmethod
    def num(x):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    def nz(self, x):
        v = self.num(x)
        return None if v is None or v == 0 else v

    def latest(self, field):
        v = self.nz(field.three_months)
        return v if v is not None else self.nz(field.twelve_months)

    def factor_values(self, f, prev_sh, shares):
        out = {}

        def put(code, fn):
            try:
                out[code] = fn()
            except Exception:
                out[code] = None

        vr = f.valuation_ratios
        orat = f.operation_ratios
        fs = f.financial_statements
        put("EP", lambda: self.nz(vr.earning_yield))
        put("BM", lambda: self.nz(vr.book_value_yield))
        put("SP", lambda: self.nz(vr.sales_yield))
        put("CFP", lambda: self.nz(vr.cf_yield))
        put("FCFY", lambda: self.nz(vr.fcf_yield))
        put("ROE", lambda: self.nz(orat.roe.one_year))
        put("ROA", lambda: self.nz(orat.roa.one_year))
        put("ROIC", lambda: self.nz(orat.roic.one_year))
        put("GM", lambda: self.nz(orat.gross_margin.one_year))
        put("OM", lambda: self.nz(orat.operation_margin.one_year))
        put("SG", lambda: self.nz(orat.revenue_growth.one_year))
        put("EPSG", lambda: self.nz(f.earning_ratios.diluted_eps_growth.one_year))
        put("AG", lambda: self.nz(orat.total_assets_growth.one_year))
        put("CR", lambda: self.nz(orat.current_ratio.one_year))
        put("AT", lambda: self.nz(orat.assets_turnover.one_year))

        def de():
            v = self.num(orat.total_debt_equity_ratio.one_year)
            return v if v is not None and v >= 0 else None
        put("DE", de)

        def ebitev():
            ebit = self.nz(fs.income_statement.ebit.twelve_months)
            ev = self.num(f.company_profile.enterprise_value)
            return ebit / ev if ebit is not None and ev is not None and ev > 0 else None
        put("EBITEV", ebitev)

        try:
            ta = self.latest(fs.balance_sheet.total_assets)
        except Exception:
            ta = None
        if ta is not None and ta > 0:
            put("GPA", lambda: (lambda gp: gp / ta if gp is not None else None)(
                self.nz(fs.income_statement.gross_profit.twelve_months)))

            def acc():
                ni = self.num(fs.income_statement.net_income.twelve_months)
                cfo = self.num(fs.cash_flow_statement.operating_cash_flow.twelve_months)
                return (ni - cfo) / ta if ni is not None and cfo is not None else None
            put("ACC", acc)
        else:
            out["GPA"] = None
            out["ACC"] = None

        s = f.symbol
        if prev_sh is not None and s in prev_sh and s in shares and prev_sh[s] > 0 and shares[s] > 0:
            out["NSI"] = math.log(shares[s] / prev_sh[s])
        else:
            out["NSI"] = None
        return out

    def current_symbols(self):
        syms = set()
        if self.current is not None:
            for lst in self.current["ports"].values():
                syms.update(lst)
        return syms

    # ----- universe / formation -------------------------------------------------------------
    def select(self, fundamental):
        ym = (self.time.year, self.time.month)
        if ym != self.last_month:
            self.last_month = ym
            return self.form(fundamental, ym)
        if self.trim:
            self.trim = False
            return list(self.current_symbols())
        return Universe.UNCHANGED

    def form(self, fundamental, ym):
        n_price = n_fund = 0
        cand = {}
        shares = {}
        for f in fundamental:
            p = f.price
            if p <= 0:
                continue
            hasf = f.has_fundamental_data
            mcap = float(f.market_cap) if hasf else 0.0
            if mcap > 0:
                sf = float(f.split_factor)
                if sf > 0:
                    shares[f.symbol] = mcap / (p * sf)
            if p < self.MIN_PRICE or f.dollar_volume < self.MIN_DV:
                continue
            n_price += 1
            if mcap <= 0:
                continue
            n_fund += 1
            if mcap < self.MIN_MCAP:
                continue
            sr = f.security_reference
            cr = f.company_reference
            if sr is None or cr is None:
                continue
            if sr.security_type != "ST00000001" or sr.is_depositary_receipt or cr.country_id != "USA":
                continue
            cid = cr.company_id or str(f.symbol.id)
            prev = cand.get(cid)
            if prev is None or f.dollar_volume > prev.dollar_volume:
                cand[cid] = f
        self.shares_hist[ym] = shares
        for k in list(self.shares_hist):
            if (ym[0] - k[0]) * 12 + ym[1] - k[1] > 12:
                del self.shares_hist[k]
        if date(ym[0], ym[1], 1) < self.FIRST_FORMATION:
            return []

        prev_sh = self.shares_hist.get((ym[0] - 1, ym[1]))
        vals = {code: [] for code, _ in FACTORS}
        for f in cand.values():
            mc = float(f.market_cap)
            v = self.factor_values(f, prev_sh, shares)
            for code, _ in FACTORS:
                x = v.get(code)
                if x is not None and math.isfinite(x):
                    vals[code].append((x, mc, f.symbol))
        ports = {}
        counts = []
        for code, sign in FACTORS:
            rows = vals[code]
            counts.append(len(rows))
            if len(rows) < self.MIN_VALID:
                continue
            rows.sort(key=lambda r: (sign * r[0], r[1]), reverse=True)
            ports[code + "_t"] = [r[2] for r in rows[:self.TOP_N]]
            ports[code + "_d"] = [r[2] for r in rows[:int(math.ceil(len(rows) / 10.0))]]
        self.pending = ports
        self.pending_ym = ym
        self.pending_meta = (n_price, n_fund, len(cand), counts)
        self.sel_date = self.time.date()
        syms = set()
        for lst in ports.values():
            syms.update(lst)
        return list(syms | self.current_symbols())

    # ----- paper accounting -----------------------------------------------------------------
    def on_data(self, data):
        for kv in data.delistings:
            d = kv.value
            if d is not None and float(d.price) > 0:
                self.delist_px[kv.key] = float(d.price)
        bars = data.bars
        if self.miss:
            for s in list(self.miss):
                if bars.contains_key(s):
                    self.entry[s] = float(bars[s].close)
                    del self.miss[s]
                else:
                    self.miss[s] -= 1
                    if self.miss[s] <= 0:
                        del self.miss[s]
        if self.pending is None or not bars.contains_key(self.bench["SPY"]):
            return
        trade_date = (self.time - timedelta(hours=1)).date()
        if trade_date < self.sel_date:
            return
        # close the open portfolios at today's close
        if self.current is not None:
            n_noexit = 0
            exit_px = {}
            for s in self.current_symbols():
                if bars.contains_key(s):
                    exit_px[s] = float(bars[s].close)
                elif s in self.delist_px:
                    exit_px[s] = self.delist_px[s]
                elif self.securities.contains_key(s) and self.securities[s].price > 0:
                    exit_px[s] = float(self.securities[s].price)
                else:
                    n_noexit += 1
            n_noentry = 0
            rets = {}
            for key, lst in self.current["ports"].items():
                rs = []
                for s in lst:
                    e = self.entry.get(s)
                    x = exit_px.get(s)
                    if e is None:
                        n_noentry += 1
                    elif x is not None and e > 0:
                        rs.append(x / e - 1)
                rets[key] = sum(rs) / len(rs) if rs else None
            brets = {}
            for k, sym in self.bench.items():
                e = self.current["bench"].get(k)
                brets[k] = (float(bars[sym].close) / e - 1) if e and bars.contains_key(sym) else None
            self.rows.append((self.current["ym"], self.current["meta"], brets, rets, n_noentry, n_noexit))
        # open the new portfolios at today's close
        self.entry = {}
        self.miss = {}
        self.delist_px = {}
        new_syms = set()
        for lst in self.pending.values():
            new_syms.update(lst)
        for s in new_syms:
            if bars.contains_key(s):
                self.entry[s] = float(bars[s].close)
            else:
                self.miss[s] = self.ENTRY_WAIT
        bench = {k: (float(bars[sym].close) if bars.contains_key(sym) else None)
                 for k, sym in self.bench.items()}
        self.current = {"ym": self.pending_ym, "meta": self.pending_meta,
                        "ports": self.pending, "bench": bench}
        self.pending = None
        self.trim = True

    # ----- output ---------------------------------------------------------------------------
    # Runtime statistics on the free tier keep only ~50 keys of <= 200 characters each, so the
    # registered summary statistics are computed here (rules unchanged); the monthly rows are
    # also written to the log (may be truncated by the log quota).
    @staticmethod
    def bench_ret(brets):
        return brets["ONEQ"] if brets["ONEQ"] is not None else brets["QQQ"]

    def pairs(self, key, a, b, cost):
        out = []
        for ym, meta, brets, rets, ne, nx in self.rows:
            k = ym[0] * 100 + ym[1]
            if k < a or k > b:
                continue
            bm = self.bench_ret(brets)
            r = rets.get(key)
            if bm is None or r is None:
                continue
            out.append((r - cost / 12.0, bm))
        return out

    @staticmethod
    def stats(pairs):
        n = len(pairs)
        if n < 12:
            return None
        cs = cb = pk = pkb = 1.0
        mdd = mddb = 0.0
        ex, rr = [], []
        for r, b in pairs:
            cs *= 1 + r
            cb *= 1 + b
            pk = max(pk, cs)
            pkb = max(pkb, cb)
            mdd = min(mdd, cs / pk - 1)
            mddb = min(mddb, cb / pkb - 1)
            ex.append(r - b)
            rr.append(r)

        def mean(a):
            return sum(a) / len(a)

        def sd(a):
            m = mean(a)
            return math.sqrt(sum((x - m) ** 2 for x in a) / (len(a) - 1))
        sde, sdr = sd(ex), sd(rr)
        return {"n": n, "c": max(cs, 1e-12) ** (12.0 / n) - 1, "b": cb ** (12.0 / n) - 1,
                "t": mean(ex) / (sde / math.sqrt(n)) if sde > 0 else 0.0,
                "mdd": mdd, "mddb": mddb, "sh": mean(rr) / sdr * math.sqrt(12) if sdr > 0 else 0.0}

    def passes(self, key, js, cost):
        h1 = self.stats(self.pairs(key, js, 201212, cost))
        h2 = self.stats(self.pairs(key, 201301, 202606, cost))
        fu = self.stats(self.pairs(key, js, 202606, cost))
        return bool(h1 and h2 and fu and h1["c"] > h1["b"] and h2["c"] > h2["b"] and fu["t"] >= 3.02)

    def on_end_of_algorithm(self):
        def p(x):
            return "x" if x is None else f"{x * 100:.1f}"

        def bp(r):
            return "x" if r is None else str(int(round(r * 10000)))

        # coverage per year and the registered judged start
        cov = {}
        for ym, meta, brets, rets, ne, nx in self.rows:
            cov.setdefault(ym[0], []).append(meta[1] / meta[0] if meta[0] else 0.0)
        cov_y = {y: sum(v) / len(v) for y, v in cov.items()}
        early_bad = any(cov_y.get(y, 1.0) < 0.70 for y in (1999, 2000, 2001, 2002))
        js = 200301 if early_bad else 199904
        self.set_runtime_statistic("COV", " ".join(f"{y % 100:02d}:{cov_y[y] * 100:.0f}" for y in sorted(cov_y)))
        self.set_runtime_statistic("JS", f"{js} months={len(self.rows)}")
        # factor coverage: min count 1999-2002, min and median count 2003+
        cnt = []
        for i, (code, _) in enumerate(FACTORS):
            e = [r[1][3][i] for r in self.rows if r[0][0] <= 2002]
            l = sorted(r[1][3][i] for r in self.rows if r[0][0] >= 2003)
            cnt.append(f"{min(e) if e else 0}/{min(l) if l else 0}/{l[len(l) // 2] if l else 0}")
        self.set_runtime_statistic("CNT1", " ".join(cnt[:10]))
        self.set_runtime_statistic("CNT2", " ".join(cnt[10:]))
        uni = sorted(r[1][2] for r in self.rows)
        self.set_runtime_statistic("MISC", "univ min/med/max {}/{}/{} noentry {} noexit {}".format(
            uni[0] if uni else 0, uni[len(uni) // 2] if uni else 0, uni[-1] if uni else 0,
            sum(r[4] for r in self.rows), sum(r[5] for r in self.rows)))
        # benchmark windows (judged benchmark = ONEQ else QQQ; SPY for reference)
        def bwin(a, b, name):
            items = [r for r in self.rows if a <= r[0][0] * 100 + r[0][1] <= b]
            vals = [self.bench_ret(r[2]) if name == "J" else r[2][name] for r in items]
            vals = [v for v in vals if v is not None]
            if len(vals) < 12:
                return "x"
            c = 1.0
            pk, mdd = 1.0, 0.0
            for v in vals:
                c *= 1 + v
                pk = max(pk, c)
                mdd = min(mdd, c / pk - 1)
            return f"{p(c ** (12.0 / len(vals)) - 1)},{p(mdd)}"
        wins = (("E", 199904, 200212), ("H1", 200301, 201212), ("H2", 201301, 202606), ("F", 200301, 202606))
        for i in range(2):
            self.set_runtime_statistic(f"BENCH{i + 1}", " ".join(
                f"{lab} J{bwin(a, b, 'J')} Q{bwin(a, b, 'QQQ')} S{bwin(a, b, 'SPY')}" for lab, a, b in wins[2 * i:2 * i + 2]))
        for code, _ in FACTORS:
            for kind in ("_t", "_d"):
                key = code + kind
                cost = 0.01 if kind == "_t" else 0.0
                h1 = self.stats(self.pairs(key, 200301, 201212, cost))
                h2 = self.stats(self.pairs(key, 201301, 202606, cost))
                fu = self.stats(self.pairs(key, 200301, 202606, cost))
                e = self.stats(self.pairs(key, 199904, 200212, cost))
                a1 = self.stats(self.pairs(key, 199904, 201212, cost))
                af = self.stats(self.pairs(key, 199904, 202606, cost))
                costs = (0.005, 0.01, 0.015) if kind == "_t" else (0.0,)
                flags = "".join(str(int(self.passes(key, j, c))) for c in costs for j in (200301, 199904))
                parts = []
                parts.append("H1 " + (f"{p(h1['c'])},{p(h1['b'])},{h1['t']:.2f}" if h1 else "x"))
                parts.append("H2 " + (f"{p(h2['c'])},{p(h2['b'])},{h2['t']:.2f}" if h2 else "x"))
                parts.append("F " + (f"{p(fu['c'])},{p(fu['b'])},{fu['t']:.2f},{p(fu['mdd'])},{p(fu['mddb'])},{fu['sh']:.2f}"
                                     if fu else "x"))
                parts.append("E " + (f"{p(e['c'])},{p(e['b'])},{p(e['mdd'])},{e['n']}" if e else "x"))
                parts.append("A1 " + (f"{p(a1['c'])},{p(a1['b'])},{a1['t']:.2f}" if a1 else "x"))
                parts.append("AF " + (f"{af['t']:.2f}" if af else "x"))
                parts.append("P " + flags)
                self.set_runtime_statistic(key, " ".join(parts)[:200])
        # monthly rows to the log (compact; may be truncated by the free-tier log quota)
        for ym, meta, brets, rets, ne, nx in self.rows:
            self.log(f"{ym[0]}{ym[1]:02d} {meta[0]} {meta[1]} {meta[2]} {bp(brets['QQQ'])} {bp(brets['ONEQ'])} "
                     f"{bp(brets['SPY'])} " + ",".join(f"{bp(rets.get(c + '_t'))},{bp(rets.get(c + '_d'))}"
                                                       for c, _ in FACTORS))
