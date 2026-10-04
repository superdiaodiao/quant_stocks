# region imports
from AlgorithmImports import *
# endregion
import math
from datetime import date, timedelta


class PiotroskiValue(QCAlgorithm):
    """S2, registered in docs/research_ledger_qc_smallcap.md before the run. Textbook, untuned.

    Piotroski (2000) F-score on cheap stocks. Universe (point-in-time, delisted names included):
    US common stocks (security_type ST00000001, not depositary receipts, company country USA),
    primary listing NYSE / Nasdaq / AMEX (NYS / NAS / ASE), price >= $5, daily dollar volume >= $1M,
    market cap $300M-$5B, excluding financials (Morningstar sector 103; their statements lack
    current ratio / gross margin). Book-to-market = 1 / valuation_ratios.pb_ratio (pb > 0); keep
    the top 20% (cheapest). F-score from annual (twelve_months) statements; t = latest fiscal year
    as known on the rebalance day, t-1 and t-2 = the latest fiscal year as known 365 and 730 days
    earlier (historical Fundamental snapshots); fiscal period ends must be 300-430 days apart.
      ROA = net income / beginning total assets; CFO = operating cash flow;
      1 ROA > 0, 2 CFO > 0, 3 ROA > ROA(t-1), 4 CFO / beginning assets > ROA,
      5 long-term debt / average total assets fell (missing debt = 0),
      6 current ratio rose, 7 no common stock issued in the year (cash-flow statement),
      8 gross margin rose, 9 asset turnover (sales / beginning assets) rose.
    Hold up to 10 names with F >= 8, by highest F then highest book-to-market; fewer if fewer
    qualify (the rest stays in cash). Rebalance on the first trading day of May each year.
    Execution: market-on-close orders, sells first; buys once sells have filled and cash has
    settled (cash account), retried for up to 10 sessions; 1/10 of equity per new name, whole
    shares, capped by settled cash; names kept from the last list are not resized.
    IBKR fee model, cash account, $10,000.
    """

    TOP_N = 10
    MIN_MCAP = 300e6
    MAX_MCAP = 5e9
    MIN_PRICE = 5.0
    MIN_DOLLAR_VOLUME = 1e6
    EXCHANGES = ("NYS", "NAS", "ASE")
    EXCLUDED_SECTORS = (103,)   # financial services
    EXCLUDE_REIT = False
    BM_QUANTILE = 0.20
    MIN_F = 8
    REBALANCE_MONTHS = (5,)
    BUY_WINDOW = 10

    def initialize(self):
        self.set_start_date(2014, 1, 1)
        self.set_end_date(2026, 8, 31)
        self.set_cash(10_000)
        self.set_brokerage_model(BrokerageName.INTERACTIVE_BROKERS_BROKERAGE, AccountType.CASH)
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = DataNormalizationMode.ADJUSTED
        self.add_universe(self.select)
        self.oneq = self.add_equity("ONEQ", Resolution.DAILY).symbol
        self.qqq = self.add_equity("QQQ", Resolution.DAILY).symbol
        self.set_benchmark(self.oneq)
        self.last_month = None
        self.targets = None        # ordered list of symbols chosen at the last rebalance
        self.need_sells = False
        self.buy_days_left = 0
        self.daily = []            # (date, equity, oneq close, qqq close)

    # ----- universe -------------------------------------------------------------------------
    def is_rebalance_month(self, month):
        return month in self.REBALANCE_MONTHS

    def base_ok(self, f):
        if not f.has_fundamental_data:
            return False
        if f.price < self.MIN_PRICE or f.dollar_volume < self.MIN_DOLLAR_VOLUME:
            return False
        sr = f.security_reference
        cr = f.company_reference
        if sr is None or cr is None:
            return False
        if sr.security_type != "ST00000001" or sr.exchange_id not in self.EXCHANGES:
            return False
        if sr.is_depositary_receipt or cr.country_id != "USA":
            return False
        if self.EXCLUDED_SECTORS or self.EXCLUDE_REIT:
            ac = f.asset_classification
            if ac is None or ac.morningstar_sector_code in self.EXCLUDED_SECTORS:
                return False
            if self.EXCLUDE_REIT and cr.is_reit:
                return False
        mcap = f.market_cap
        return mcap is not None and self.MIN_MCAP <= mcap <= self.MAX_MCAP

    def held(self):
        return [s for s in self.portfolio.keys()
                if self.portfolio[s].invested and s not in (self.oneq, self.qqq)]

    def select(self, fundamental):
        month = self.time.month
        if month == self.last_month:
            return Universe.UNCHANGED
        self.last_month = month
        if not self.is_rebalance_month(month):
            return Universe.UNCHANGED
        chosen = self.rank([f for f in fundamental if self.base_ok(f)])
        if not chosen:
            return Universe.UNCHANGED   # nothing ranked (data gap): keep the current holdings
        self.targets = chosen
        self.need_sells = True
        self.buy_days_left = self.BUY_WINDOW
        return list(set(self.targets) | set(self.held()))

    @staticmethod
    def num(x):
        """float(x), or None when missing (Morningstar doubles are NaN when absent, longs are 0)."""
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    def snapshot(self, f):
        """Annual (twelve_months) values from one Fundamental object, or None if incomplete."""
        try:
            fs = f.financial_statements
            inc, bs, cf = fs.income_statement, fs.balance_sheet, fs.cash_flow_statement
            snap = {
                "end": fs.period_ending_date.twelve_months,
                "ta": self.num(bs.total_assets.twelve_months),
                "ni": self.num(inc.net_income_continuous_operations.twelve_months),
                "cfo": self.num(cf.operating_cash_flow.twelve_months),
                "ltd": self.num(bs.long_term_debt.twelve_months),
                "ca": self.num(bs.current_assets.twelve_months),
                "cl": self.num(bs.current_liabilities.twelve_months),
                "rev": self.num(inc.total_revenue.twelve_months),
                "gp": self.num(inc.gross_profit.twelve_months),
                "issue": self.num(cf.common_stock_issuance.twelve_months),
            }
        except Exception:
            return None
        if snap["ni"] is None:   # fall back to net income when the continuing-ops line is absent
            try:
                snap["ni"] = self.num(f.financial_statements.income_statement.net_income.twelve_months)
            except Exception:
                return None
        snap["ltd"] = snap["ltd"] or 0.0
        snap["issue"] = snap["issue"] or 0.0
        for k in ("ta", "ni", "cfo", "ca", "cl", "rev", "gp"):
            if snap[k] is None:
                return None
        if snap["ta"] <= 0 or snap["cl"] <= 0 or snap["rev"] <= 0:
            return None
        try:
            if snap["end"] is None or snap["end"].year < 1990:
                return None
        except Exception:
            return None
        return snap

    def past(self, symbol, days):
        """The Fundamental snapshot as known about `days` days ago (last object in a 14-day window)."""
        end = self.time - timedelta(days=days)
        try:
            items = self.history[Fundamental](symbol, end - timedelta(days=14), end)
            last = None
            for x in items:
                last = x
        except Exception:
            return None
        if last is None or not hasattr(last, "financial_statements"):
            return None
        return self.snapshot(last)

    @staticmethod
    def gap_ok(later, earlier):
        try:
            gap = (later - earlier).days
        except Exception:
            return False
        return 300 <= gap <= 430

    def rank(self, candidates):
        cheap = []
        for f in candidates:
            pb = self.num(f.valuation_ratios.pb_ratio) if f.valuation_ratios is not None else None
            if pb is not None and pb > 0:
                cheap.append((1.0 / pb, f))
        cheap.sort(key=lambda x: x[0], reverse=True)
        cheap = cheap[:int(math.ceil(len(cheap) * self.BM_QUANTILE))]
        scored = []
        for bm, f in cheap:
            c = self.snapshot(f)
            if c is None:
                continue
            # signals 1, 2, 4 and 7 need only year t (with beginning assets > 0, ROA > 0 <=> NI > 0
            # and CFO / assets > ROA <=> CFO > NI); F >= 8 allows at most one miss in total
            early = (c["ni"] > 0) + (c["cfo"] > 0) + (c["cfo"] > c["ni"]) + (c["issue"] <= 0)
            if early < 4 - (9 - self.MIN_F):
                continue
            p1 = self.past(f.symbol, 365)
            if p1 is None or not self.gap_ok(c["end"], p1["end"]):
                continue
            p2 = self.past(f.symbol, 730)
            if p2 is None or not self.gap_ok(p1["end"], p2["end"]):
                continue
            roa = c["ni"] / p1["ta"]
            roa_prev = p1["ni"] / p2["ta"]
            lev = c["ltd"] / ((c["ta"] + p1["ta"]) / 2)
            lev_prev = p1["ltd"] / ((p1["ta"] + p2["ta"]) / 2)
            score = (int(roa > 0) + int(c["cfo"] > 0) + int(roa > roa_prev)
                     + int(c["cfo"] / p1["ta"] > roa)
                     + int(lev < lev_prev)
                     + int(c["ca"] / c["cl"] > p1["ca"] / p1["cl"])
                     + int(c["issue"] <= 0)
                     + int(c["gp"] / c["rev"] > p1["gp"] / p1["rev"])
                     + int(c["rev"] / p1["ta"] > p1["rev"] / p2["ta"]))
            if score >= self.MIN_F:
                scored.append((score, bm, f.symbol))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [s for _, _, s in scored[:self.TOP_N]]

    # ----- trading --------------------------------------------------------------------------
    def bar_date(self):
        t = self.time
        return t.date() if t.hour >= 9 else (t - timedelta(days=1)).date()

    def on_data(self, data):
        self.record()
        if self.targets is None:
            return
        open_orders = self.transactions.get_open_orders()
        pending = {o.symbol for o in open_orders}
        if self.need_sells:
            for sym in self.held():
                if sym not in self.targets and sym not in pending:
                    self.market_on_close_order(sym, -self.portfolio[sym].quantity)
            self.need_sells = False
            return
        if self.buy_days_left <= 0:
            return
        self.buy_days_left -= 1
        if any(o.quantity < 0 for o in open_orders):
            return  # wait for the sells to fill first
        held = set(self.held())
        missing = [s for s in self.targets if s not in held and s not in pending]
        if not missing:
            self.buy_days_left = 0
            return
        equity = self.portfolio.total_portfolio_value
        budget = self.portfolio.cash_book["USD"].amount - sum(
            o.quantity * self.securities[o.symbol].price for o in open_orders if o.quantity > 0)
        for sym in missing:
            if sym not in self.securities:
                continue
            price = self.securities[sym].price
            if price <= 0:
                continue
            qty = int(min(equity / self.TOP_N, budget * 0.98) // price)
            if qty >= 1:
                self.market_on_close_order(sym, qty)
                budget -= qty * price

    def record(self):
        d = self.bar_date()
        oneq = self.securities[self.oneq].price
        qqq = self.securities[self.qqq].price
        row = (d, self.portfolio.total_portfolio_value, oneq, qqq)
        if self.daily and self.daily[-1][0] == d:
            self.daily[-1] = row
        else:
            self.daily.append(row)

    # ----- statistics -----------------------------------------------------------------------
    def on_end_of_algorithm(self):
        rows = [r for r in self.daily if r[2] > 0 and r[3] > 0]
        if len(rows) < 2:
            return

        def cagr(a, b, years):
            return (b / a) ** (1 / years) - 1 if a > 0 and years > 0 else float("nan")

        def mdd(seg, i):
            peak, worst = 0, 0
            for r in seg:
                peak = max(peak, r[i])
                worst = min(worst, r[i] / peak - 1)
            return worst

        def segment(start, end):
            idx = [k for k, r in enumerate(rows) if start <= r[0] <= end]
            if len(idx) < 2:
                return None
            first = idx[0] - 1 if idx[0] > 0 else idx[0]   # base = last close before the window
            return rows[first:idx[-1] + 1]

        def window(start, end):
            seg = segment(start, end)
            if seg is None:
                return
            yrs = (seg[-1][0] - seg[0][0]).days / 365.25
            s, o, q = [cagr(seg[0][i], seg[-1][i], yrs) for i in (1, 2, 3)]
            label = f"{start.year}-{end.year}"
            self.set_runtime_statistic(f"{label} strat/ONEQ/QQQ/MDD",
                                       f"{s:.2%} / {o:.2%} / {q:.2%} / {mdd(seg, 1):.1%}")
            self.set_runtime_statistic(f"{label} MDD ONEQ/QQQ",
                                       f"{mdd(seg, 2):.1%} / {mdd(seg, 3):.1%}")

        window(date(2014, 1, 1), date(2019, 12, 31))
        window(date(2020, 1, 1), date(2026, 8, 31))
        window(date(2014, 1, 1), date(2026, 8, 31))
        for y in range(2014, 2027):
            seg = segment(date(y, 1, 1), date(y, 12, 31))
            if seg is not None:
                self.set_runtime_statistic(
                    f"Y{y} strat/ONEQ/QQQ",
                    f"{seg[-1][1] / seg[0][1] - 1:+.1%} / {seg[-1][2] / seg[0][2] - 1:+.1%} / "
                    f"{seg[-1][3] / seg[0][3] - 1:+.1%}")

        # monthly returns (month-end closes; the first month's base is the first recorded close)
        month_end = {}
        for r in rows:
            month_end[(r[0].year, r[0].month)] = r
        keys = sorted(month_end)
        monthly = []   # (year, month, strategy return, ONEQ return)
        prev = rows[0]
        for k in keys:
            cur = month_end[k]
            monthly.append((k[0], k[1], cur[1] / prev[1] - 1, cur[2] / prev[2] - 1))
            prev = cur
        for y in range(2014, 2027):
            items = [m for m in monthly if m[0] == y]
            if items:
                self.set_runtime_statistic(
                    f"M{y} strat:ONEQ %",
                    " ".join(f"{m[2] * 100:.2f}:{m[3] * 100:.2f}" for m in items))

        def excess_t(items, label):
            ex = [m[2] - m[3] for m in items]
            n = len(ex)
            if n < 3:
                return
            mean = sum(ex) / n
            sd = math.sqrt(sum((x - mean) ** 2 for x in ex) / (n - 1))
            t = mean / (sd / math.sqrt(n)) if sd > 0 else float("nan")
            self.set_runtime_statistic(f"{label} monthly excess mean/t/n",
                                       f"{mean:.2%} / {t:.2f} / {n}")

        excess_t([m for m in monthly if m[0] <= 2019], "2014-2019")
        excess_t([m for m in monthly if m[0] >= 2020], "2020-2026")
        excess_t(monthly, "2014-2026")
