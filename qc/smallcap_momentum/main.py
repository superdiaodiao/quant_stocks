# region imports
from AlgorithmImports import *
# endregion
import math
from datetime import date, timedelta


class SmallCapMomentum(QCAlgorithm):
    """S3, registered in docs/research_ledger_qc_smallcap.md before the run. Textbook, untuned.

    Universe (point-in-time, delisted names included): US common stocks (security_type ST00000001,
    not depositary receipts, company country USA) with primary listing on NYSE / Nasdaq / AMEX
    (exchange_id NYS / NAS / ASE), price >= $5, daily dollar volume >= $1M, market cap $300M-$3B.
    No sector exclusion (Jegadeesh-Titman momentum uses all sectors).
    Signal: 12-1 month momentum = close[t-21] / close[t-252] - 1 (adjusted daily closes, t = the
    last close before the rebalance). Hold the top 10, equal weight (1/10 of equity per new name,
    whole shares, capped by settled cash). Rebalance on the first trading day of every month.
    Execution: market-on-close orders, sells first; buys once sells have filled and cash has
    settled (cash account), retried for up to 10 sessions. Names kept from the last list are not
    resized. IBKR fee model, cash account, $10,000.
    """

    TOP_N = 10
    MIN_MCAP = 300e6
    MAX_MCAP = 3e9
    MIN_PRICE = 5.0
    MIN_DOLLAR_VOLUME = 1e6
    EXCHANGES = ("NYS", "NAS", "ASE")
    EXCLUDED_SECTORS = ()      # Morningstar sector codes; none for momentum
    EXCLUDE_REIT = False
    LOOKBACK = 252
    SKIP = 21
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
        return True

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

    def rank(self, candidates):
        symbols = [f.symbol for f in candidates]
        if not symbols:
            return []
        keys = {}
        for s in symbols:
            keys[s] = s
            keys[str(s)] = s
            keys[str(s.id)] = s
            keys[s.value] = s
        hist = self.history(symbols, self.LOOKBACK + 1, Resolution.DAILY)
        if hist is None or hist.empty or "close" not in hist:
            return []
        closes = hist["close"].unstack(level=0)
        if len(closes) < self.LOOKBACK + 1:
            return []
        start = closes.iloc[-(self.LOOKBACK + 1)]
        end = closes.iloc[-(self.SKIP + 1)]
        scores = []
        for col in closes.columns:
            sym = keys.get(col) or keys.get(str(col))
            a, b = float(start[col]), float(end[col])
            if sym is None or not (a > 0 and b > 0) or math.isnan(a) or math.isnan(b):
                continue
            scores.append((b / a - 1, sym))
        scores.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scores[:self.TOP_N]]

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
