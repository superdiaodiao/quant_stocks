# region imports
from AlgorithmImports import *
# endregion
import math
from datetime import date, timedelta

# Registered in docs/research_ledger_qc_factors.md (section 3) before the run.
FACTOR = "SP"      # "SP" = registered primary (sales_yield); "BM" = secondary, reported only (book_value_yield)
EXCLUDE = ()       # str(symbol.id) values excluded ONLY in the registered data-error sensitivity run
BENCH = ("QQQ", "ONEQ")


class SlippageInit(BrokerageModelSecurityInitializer):
    """IBKR brokerage defaults (fees, cash buying power, settlement) plus a constant 0.1% slippage."""

    def __init__(self, brokerage_model, seeder):
        super().__init__(brokerage_model, seeder)

    def initialize(self, security):
        super().initialize(security)
        security.set_slippage_model(ConstantSlippageModel(0.001))


class SpTop10Trade(QCAlgorithm):
    """Real-execution check of the frozen factor-zoo rule "S/P top 10" (qc/factor_zoo/main.py, SP_t).

    Selection is identical to the factor zoo: on the month's first universe selection (data as of
    the prior close), universe = price >= $5, dollar volume >= $1M, has fundamental data, market cap
    >= $300M, common stock (ST00000001), not a depositary receipt, company country USA, no exchange
    filter, one share class per company (largest dollar volume); rank by valuation_ratios.sales_yield
    (zero / non-finite = missing), highest first, ties larger market cap first, hold the top 10
    (skip the month when < 50 valid names).
    Execution: on the first trading day >= the selection date, 30 minutes after the open (prices
    known = prior close), market-on-close orders, so fills are at that session's close, as in the
    paper study. Sells of dropped names; equal-weight target = equity / 10; kept names are resized
    only when off target by more than 2% of equity; new names first (rank order), then top-ups,
    whole shares, sized by settled cash x 0.98 (cash account, unsettled proceeds cannot be used);
    unfilled buys are retried at the same time of day for up to 10 sessions as cash settles.
    IBKR fee model, cash account, $10,000, constant 0.1% slippage, raw prices (dividends paid in
    cash, splits applied by the engine). Benchmarks QQQ / ONEQ use adjusted (total return) prices.
    """

    TOP_N = 10
    MIN_MCAP = 300e6
    MIN_PRICE = 5.0
    MIN_DV = 1e6
    MIN_VALID = 50
    BAND = 0.02
    BUY_WINDOW = 10
    CASH_BUFFER = 0.98
    SLIP = 0.001
    PAPER_COST = 0.01
    ENTRY_WAIT = 5

    def initialize(self):
        self.set_start_date(2003, 1, 1)
        self.set_end_date(2026, 7, 31)
        self.set_cash(10_000)
        self.set_brokerage_model(BrokerageName.INTERACTIVE_BROKERS_BROKERAGE, AccountType.CASH)
        self.set_security_initializer(SlippageInit(self.brokerage_model,
                                                   FuncSecuritySeeder(self.get_last_known_prices)))
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = DataNormalizationMode.RAW
        self.add_universe(self.select)
        self.bench = {k: self.add_equity(k, Resolution.DAILY,
                                         data_normalization_mode=DataNormalizationMode.ADJUSTED).symbol
                      for k in BENCH}
        self.bench_set = set(self.bench.values())
        self.set_benchmark(self.bench["QQQ"])
        q = self.bench["QQQ"]
        self.schedule.on(self.date_rules.every_day(q), self.time_rules.after_market_open(q, 30), self.trade)

        self.last_month = None
        self.pending = None        # list of pick dicts formed at this month's selection
        self.pending_ym = None
        self.sel_date = None
        self.targets = {}          # symbol -> pick dict (current month)
        self.target_order = []
        self.cur_ym = None
        self.buy_days_left = 0
        self.trim = False
        self.paper_roll = False
        self.marks = []            # (ym, rebalance date)
        self.daily = []            # [date, equity, ONEQ adj close, QQQ adj close]
        # paper replica of the same picks (equal weight, close to close, dividends in cash)
        self.paper = None          # {"ym": ym, "pos": {symbol: [entry, units, cash]}}
        self.paper_miss = {}
        self.paper_rows = []       # (ym, return)
        self.paper_noentry = 0
        self.paper_noexit = 0
        self.delist_px = {}
        # execution statistics
        self.traded = {}           # year -> traded value
        self.fees = {}             # year -> fees
        self.nfill = {}            # year -> filled orders
        self.n_invalid = 0
        self.n_late = 0
        self.n_ontime = 0
        self.hold_days = 0
        self.hold_sum = 0
        self.cash_sum = 0.0
        self.low_days = 0
        self.skipped = 0
        self.univ = []
        self.flags = []

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

    def factor_value(self, f):
        try:
            vr = f.valuation_ratios
            return self.nz(vr.sales_yield if FACTOR == "SP" else vr.book_value_yield)
        except Exception:
            return None

    def check_ratios(self, f, v, mcap, price):
        """Data-error diagnostics (reported only): implied sales (or book) from the ratio vs the
        statement line, and market cap vs shares outstanding x price."""
        k = rv = None
        try:
            so = self.num(f.company_profile.shares_outstanding)
            if so and so > 0 and price > 0:
                k = mcap / (so * price)
        except Exception:
            pass
        try:
            if FACTOR == "SP":
                line = self.nz(f.financial_statements.income_statement.total_revenue.twelve_months)
            else:
                eq = f.financial_statements.balance_sheet.stockholders_equity
                line = self.nz(eq.three_months)
                if line is None:
                    line = self.nz(eq.twelve_months)
            if line is not None and v * mcap != 0:
                rv = line / (v * mcap)
        except Exception:
            pass
        return k, rv

    def held(self):
        return [s for s in self.portfolio.keys()
                if self.portfolio[s].invested and s not in self.bench_set]

    def px(self, s):
        p = self.securities[s].price if self.securities.contains_key(s) else 0
        if p and p > 0:
            return float(p)
        info = self.targets.get(s)
        return info["p"] if info else 0.0

    def bar_date(self):
        t = self.time
        return t.date() if t.hour >= 9 else (t - timedelta(days=1)).date()

    # ----- universe / formation -------------------------------------------------------------
    def select(self, fundamental):
        ym = (self.time.year, self.time.month)
        if ym != self.last_month:
            self.last_month = ym
            return self.form(fundamental, ym)
        if self.trim:
            self.trim = False
            return list(set(self.targets) | set(self.held()))
        return Universe.UNCHANGED

    def form(self, fundamental, ym):
        cand = {}
        for f in fundamental:
            p = f.price
            if p <= 0 or p < self.MIN_PRICE or f.dollar_volume < self.MIN_DV:
                continue
            if not f.has_fundamental_data:
                continue
            mcap = float(f.market_cap)
            if mcap <= 0 or mcap < self.MIN_MCAP:
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
        rows = []
        for f in cand.values():
            v = self.factor_value(f)
            if v is not None and math.isfinite(v):
                rows.append((v, float(f.market_cap), f))
        self.univ.append(len(rows))
        if len(rows) < self.MIN_VALID:
            self.skipped += 1
            return Universe.UNCHANGED
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        if EXCLUDE:
            rows = [r for r in rows if str(r[2].symbol.id) not in EXCLUDE]
        picks = []
        for i, (v, mc, f) in enumerate(rows[:self.TOP_N]):
            price = float(f.price)
            k, rv = self.check_ratios(f, v, mc, price)
            try:
                name = (f.company_reference.short_name or "")[:16]
            except Exception:
                name = ""
            try:
                sec = int(f.asset_classification.morningstar_sector_code)
            except Exception:
                sec = 0
            picks.append({"sym": f.symbol, "r": i + 1, "v": v, "mc": mc, "p": price, "k": k, "rv": rv,
                          "sec": sec, "name": name})
            bad = v > 10 or (k is not None and (k > 1.5 or k < 0.67)) or (rv is not None and (rv > 1.5 or rv < 0.67))
            if bad:
                self.flags.append(f"{f.symbol.value}{ym[0] % 100:02d}{ym[1]:02d}")
        self.pending = picks
        self.pending_ym = ym
        self.sel_date = self.time.date()
        self.log(f"{ym[0]}{ym[1]:02d} " + " ".join(
            f"{d['sym'].value}:{d['v']:.2f}:{d['mc'] / 1e6:.0f}:{d['p']:.1f}" for d in picks))
        keep = set(self.held()) | set(self.targets)
        if self.paper:
            keep |= set(self.paper["pos"])
        return list(keep | {d["sym"] for d in picks})

    # ----- trading --------------------------------------------------------------------------
    def tag(self, kind, s):
        d = self.targets.get(s)
        ym = self.cur_ym
        base = f"{kind} {ym[0]}{ym[1]:02d}"
        if d is None:
            return base
        k = "x" if d["k"] is None else f"{d['k']:.2f}"
        rv = "x" if d["rv"] is None else f"{d['rv']:.2f}"
        return (f"{base} r{d['r']} v{d['v']:.4g} mc{d['mc'] / 1e6:.0f} p{d['p']:.2f} k{k} rv{rv} "
                f"s{d['sec']} {d['name']}")

    def trade(self):
        today = self.time.date()
        if self.pending is not None and self.sel_date is not None and today >= self.sel_date:
            self.rebalance(today)
            return
        if self.buy_days_left > 0:
            self.buy_days_left -= 1
            self.buy_pass()

    def rebalance(self, today):
        self.transactions.cancel_open_orders()
        self.targets = {d["sym"]: d for d in self.pending}
        self.target_order = [d["sym"] for d in self.pending]
        self.cur_ym = self.pending_ym
        self.pending = None
        self.marks.append((self.cur_ym, today))
        equity = float(self.portfolio.total_portfolio_value)
        tgt = equity / self.TOP_N
        for s in self.held():
            q = self.portfolio[s].quantity
            if s not in self.targets:
                self.market_on_close_order(s, -q, tag=self.tag("X", s))
                continue
            p = self.px(s)
            if p <= 0:
                continue
            excess = q * p - tgt
            if excess > self.BAND * equity:
                n = int(excess // p)
                if 1 <= n < q:
                    self.market_on_close_order(s, -n, tag=self.tag("T", s))
        self.buy_pass()
        self.buy_days_left = self.BUY_WINDOW
        self.paper_roll = True

    def buy_pass(self):
        if not self.target_order:
            return
        equity = float(self.portfolio.total_portfolio_value)
        tgt = equity / self.TOP_N
        open_orders = self.transactions.get_open_orders()
        pend = {}
        for o in open_orders:
            pend[o.symbol] = pend.get(o.symbol, 0) + o.quantity
        budget = float(self.portfolio.cash_book["USD"].amount) - sum(
            o.quantity * self.px(o.symbol) * (1 + self.SLIP) for o in open_orders if o.quantity > 0)
        new =[s for s in self.target_order
               if (self.portfolio[s].quantity if self.portfolio.contains_key(s) else 0) + pend.get(s, 0) <= 0]
        old = [s for s in self.target_order if s not in new]
        for s in new + old:
            if budget <= 0:
                break
            if not self.securities.contains_key(s):
                continue
            sec = self.securities[s]
            if sec.is_delisted or not sec.is_tradable:
                continue
            p = self.px(s)
            if p <= 0:
                continue
            q = (self.portfolio[s].quantity if self.portfolio.contains_key(s) else 0) + pend.get(s, 0)
            deficit = tgt - q * p
            if q > 0 and deficit <= self.BAND * equity:
                continue
            if deficit <= 0:
                continue
            n = int(min(deficit, budget * self.CASH_BUFFER) // (p * (1 + self.SLIP)))
            if n >= 1:
                self.market_on_close_order(s, n, tag=self.tag("B" if q <= 0 else "U", s))
                budget -= n * p * (1 + self.SLIP)

    def on_order_event(self, e):
        if e.status == OrderStatus.INVALID:
            self.n_invalid += 1
            return
        if e.status != OrderStatus.FILLED and e.status != OrderStatus.PARTIALLY_FILLED:
            return
        y = self.time.year
        val = abs(float(e.fill_quantity) * float(e.fill_price))
        self.traded[y] = self.traded.get(y, 0.0) + val
        self.fees[y] = self.fees.get(y, 0.0) + float(e.order_fee.value.amount)
        if e.status == OrderStatus.FILLED:
            self.nfill[y] = self.nfill.get(y, 0) + 1
            try:
                o = self.transactions.get_order_by_id(e.order_id)
                if o is not None and o.type == OrderType.MARKET_ON_CLOSE:
                    if (e.utc_time - o.time).total_seconds() > 12 * 3600:
                        self.n_late += 1
                    else:
                        self.n_ontime += 1
            except Exception:
                pass

    # ----- daily records and paper replica --------------------------------------------------
    def on_data(self, data):
        for kv in data.delistings:
            d = kv.value
            if d is not None and float(d.price) > 0:
                self.delist_px[kv.key] = float(d.price)
        if self.paper:
            pos = self.paper["pos"]
            for kv in data.splits:
                s = kv.key
                if s in pos and kv.value.type == SplitType.SPLIT_OCCURRED and float(kv.value.split_factor) > 0:
                    pos[s][1] /= float(kv.value.split_factor)
            for kv in data.dividends:
                s = kv.key
                if s in pos:
                    pos[s][2] += pos[s][1] * float(kv.value.distribution)
        bars = data.bars
        if not bars.contains_key(self.bench["QQQ"]):
            return
        d = self.bar_date()
        eq = float(self.portfolio.total_portfolio_value)
        row = [d, eq, float(self.securities[self.bench["ONEQ"]].price),
               float(self.securities[self.bench["QQQ"]].price)]
        if self.daily and self.daily[-1][0] == d:
            self.daily[-1] = row
        else:
            self.daily.append(row)
        h = self.held()
        self.hold_days += 1
        self.hold_sum += len(h)
        self.cash_sum += (float(self.portfolio.cash) + float(self.portfolio.unsettled_cash)) / eq if eq > 0 else 0.0
        if self.target_order and len(h) < 8:
            self.low_days += 1
        if self.paper_miss:
            for s in list(self.paper_miss):
                if bars.contains_key(s):
                    self.paper["pos"][s] = [float(bars[s].close), 1.0, 0.0]
                    del self.paper_miss[s]
                else:
                    self.paper_miss[s] -= 1
                    if self.paper_miss[s] <= 0:
                        del self.paper_miss[s]
                        self.paper_noentry += 1
        if self.paper_roll:
            self.paper_roll = False
            if self.paper is not None:
                rs = []
                for s, (entry, units, cash) in self.paper["pos"].items():
                    if bars.contains_key(s):
                        x = float(bars[s].close)
                    elif s in self.delist_px:
                        x = self.delist_px[s]
                    elif self.securities.contains_key(s) and self.securities[s].price > 0:
                        x = float(self.securities[s].price)
                    else:
                        self.paper_noexit += 1
                        continue
                    if entry > 0:
                        rs.append((units * x + cash) / entry - 1)
                self.paper_rows.append((self.paper["ym"], sum(rs) / len(rs) if rs else None))
            self.paper = {"ym": self.cur_ym, "pos": {}}
            self.paper_miss = {}
            self.delist_px = {}
            for s in self.target_order:
                if bars.contains_key(s):
                    self.paper["pos"][s] = [float(bars[s].close), 1.0, 0.0]
                else:
                    self.paper_miss[s] = self.ENTRY_WAIT
            self.trim = True

    # ----- statistics -----------------------------------------------------------------------
    @staticmethod
    def chain(rets):
        c = pk = 1.0
        mdd = 0.0
        for r in rets:
            c *= 1 + r
            pk = max(pk, c)
            mdd = min(mdd, c / pk - 1)
        return c, mdd

    def on_end_of_algorithm(self):
        def p(x):
            return "x" if x is None else f"{x * 100:.1f}"

        byd = {r[0]: r for r in self.daily}
        months = []   # (ym, strategy, judged bench, QQQ, start date, end date)
        for i in range(len(self.marks) - 1):
            ym, d0 = self.marks[i]
            d1 = self.marks[i + 1][1]
            a, b = byd.get(d0), byd.get(d1)
            if a is None or b is None or a[1] <= 0:
                continue
            s = b[1] / a[1] - 1
            qq = b[3] / a[3] - 1 if a[3] > 0 and b[3] > 0 else None
            j = b[2] / a[2] - 1 if a[2] > 0 and b[2] > 0 else qq
            if j is None or qq is None:
                continue
            months.append((ym, s, j, qq, d0, d1))

        def win(a, b):
            return [m for m in months if a <= m[0][0] * 100 + m[0][1] <= b]

        def tstat(ex):
            n = len(ex)
            if n < 3:
                return 0.0, 0.0
            mu = sum(ex) / n
            sd = math.sqrt(sum((x - mu) ** 2 for x in ex) / (n - 1))
            return mu, (mu / (sd / math.sqrt(n)) if sd > 0 else 0.0)

        def daily_mdd(d0, d1):
            pk, worst = 0.0, 0.0
            for r in self.daily:
                if d0 <= r[0] <= d1:
                    pk = max(pk, r[1])
                    worst = min(worst, r[1] / pk - 1)
            return worst

        res = {}
        for lab, a, b in (("H1", 200301, 201212), ("H2", 201301, 202606), ("F", 200301, 202606)):
            w = win(a, b)
            n = len(w)
            if n < 12:
                self.set_runtime_statistic(lab, f"n {n}")
                continue
            cs, ms = self.chain([m[1] for m in w])
            cj, mj = self.chain([m[2] for m in w])
            cq, mq = self.chain([m[3] for m in w])
            mu, t = tstat([m[1] - m[2] for m in w])
            dd = daily_mdd(w[0][4], w[-1][5])
            res[lab] = (cs ** (12.0 / n) - 1, cj ** (12.0 / n) - 1, t)
            self.set_runtime_statistic(lab, (
                f"S {p(cs ** (12.0 / n) - 1)},{p(ms)},dd{p(dd)} J {p(cj ** (12.0 / n) - 1)},{p(mj)} "
                f"Q {p(cq ** (12.0 / n) - 1)},{p(mq)} ex {mu * 100:.2f}% t {t:.2f} n {n}"))
        ok = all(k in res for k in ("H1", "H2", "F"))
        passed = ok and res["H1"][0] > res["H1"][1] and res["H2"][0] > res["H2"][1] and res["F"][2] >= 3.02
        self.set_runtime_statistic("PASS", f"{FACTOR} {'1' if passed else '0'} excl {len(EXCLUDE)}")

        # paper replica of the same picks, minus 1%/yr (should reproduce the factor zoo SP_t / BM_t)
        jm = {m[0]: m[2] for m in months}
        for lab, a, b in (("PH1", 200301, 201212), ("PH2", 201301, 202606), ("PF", 200301, 202606)):
            pr = [(r - self.PAPER_COST / 12.0, jm[ym]) for ym, r in self.paper_rows
                  if r is not None and ym in jm and a <= ym[0] * 100 + ym[1] <= b]
            if len(pr) < 12:
                continue
            c, mdd = self.chain([x[0] for x in pr])
            mu, t = tstat([x[0] - x[1] for x in pr])
            self.set_runtime_statistic(lab, f"{p(c ** (12.0 / len(pr)) - 1)},{p(mdd)} t {t:.2f} n {len(pr)}")

        # yearly returns (holding months of each year): strategy / judged bench / QQQ
        yrs = {}
        for m in months:
            yrs.setdefault(m[0][0], []).append(m)
        ys = sorted(yrs)
        parts = []
        for y in ys:
            w = yrs[y]
            parts.append(f"{y % 100:02d}:{p(self.chain([m[1] for m in w])[0] - 1)}/"
                         f"{p(self.chain([m[2] for m in w])[0] - 1)}/{p(self.chain([m[3] for m in w])[0] - 1)}")
        for i in range(0, len(parts), 8):
            self.set_runtime_statistic(f"YR{i // 8 + 1}", " ".join(parts[i:i + 8]))

        # monthly returns in % (strategy and judged bench), 3 years per key
        for i in range(0, len(ys), 3):
            grp = [m for m in months if m[0][0] in ys[i:i + 3]]
            self.set_runtime_statistic(f"MS{ys[i] % 100:02d}", " ".join(f"{m[1] * 100:.1f}" for m in grp)[:200])
            self.set_runtime_statistic(f"MJ{ys[i] % 100:02d}", " ".join(f"{m[2] * 100:.1f}" for m in grp)[:200])

        # costs and turnover
        avg_eq = {}
        for r in self.daily:
            avg_eq.setdefault(r[0].year, []).append(r[1])
        avg_eq = {y: sum(v) / len(v) for y, v in avg_eq.items()}
        turn = []
        feep = []
        for y in sorted(avg_eq):
            e = avg_eq[y]
            turn.append(f"{y % 100:02d}:{self.traded.get(y, 0.0) / e * 100:.0f}")
            feep.append(f"{y % 100:02d}:{self.fees.get(y, 0.0) / e * 100:.2f}")
        self.set_runtime_statistic("TURN", " ".join(turn)[:200])
        self.set_runtime_statistic("FEE%", " ".join(feep)[:200])
        tot_tr = sum(self.traded.values())
        self.set_runtime_statistic("COST", (
            f"fees {sum(self.fees.values()):.0f} slip~{tot_tr * self.SLIP:.0f} traded {tot_tr:.0f} "
            f"fills {sum(self.nfill.values())} invalid {self.n_invalid} moc ontime {self.n_ontime} late {self.n_late}"))
        hd = max(self.hold_days, 1)
        u = sorted(self.univ)
        self.set_runtime_statistic("MISC", (
            f"months {len(months)} marks {len(self.marks)} skipped {self.skipped} avgheld {self.hold_sum / hd:.2f} "
            f"cash {self.cash_sum / hd * 100:.1f}% low {self.low_days} pnoentry {self.paper_noentry} "
            f"pnoexit {self.paper_noexit} univ {u[0] if u else 0}/{u[len(u) // 2] if u else 0}/{u[-1] if u else 0} "
            f"end {self.portfolio.total_portfolio_value:.0f}"))
        fl = " ".join(self.flags)
        for i in range(3):
            if fl[i * 200:(i + 1) * 200]:
                self.set_runtime_statistic(f"FLAG{i + 1}", fl[i * 200:(i + 1) * 200])
        self.set_runtime_statistic("NFLAG", str(len(self.flags)))
