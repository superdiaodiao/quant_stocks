# region imports
from AlgorithmImports import *
# endregion
from collections import deque


class Donchian20Wide(QCAlgorithm):
    """The frozen owner rule O10 (docs/research_ledger_indicators.md), unchanged, on a wider universe.

    Universe: Nasdaq-listed common stocks with market cap >= $300M and price >= $10 (QuantConnect's
    point-in-time fundamentals, delisted names included). Buy: close above the prior 20 sessions'
    highest close. Sell: close below the prior 20 sessions' lowest close. Signals at day t's close,
    filled at day t+1's close (market-on-close). Up to 5 names, each 1/5 of equity; candidates taken
    in order of dollar volume. IBKR fees, cash account, $10,000.
    """

    WINDOW = 20
    MAX_NAMES = 5
    MIN_MCAP = 300e6
    MIN_PRICE = 10.0

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
        self.closes = {}          # symbol -> deque of the last WINDOW+1 closes
        self.dollar_volume = {}   # symbol -> latest dollar volume, for priority
        self.members = set()
        self.daily = []           # (date, equity, oneq close, qqq close)

    def select(self, fundamental):
        chosen = []
        for f in fundamental:
            if not f.has_fundamental_data or f.price < self.MIN_PRICE:
                continue
            ref = f.security_reference
            if ref is None or ref.exchange_id != "NAS" or ref.security_type != "ST00000001":
                continue
            if f.market_cap is None or f.market_cap < self.MIN_MCAP:
                continue
            self.dollar_volume[f.symbol] = f.dollar_volume
            chosen.append(f.symbol)
        self.members = set(chosen)
        return chosen

    def on_securities_changed(self, changes):
        for sec in changes.added_securities:
            sym = sec.symbol
            if sym in (self.oneq, self.qqq) or sym in self.closes:
                continue
            hist = self.history(sym, self.WINDOW + 1, Resolution.DAILY)
            dq = deque(maxlen=self.WINDOW + 1)
            if not hist.empty and "close" in hist:
                for c in hist["close"].values[-(self.WINDOW + 1):]:
                    dq.append(float(c))
            self.closes[sym] = dq

    def on_data(self, data):
        buys, sells = [], []
        for sym, dq in self.closes.items():
            bar = data.bars.get(sym)
            if bar is None:
                continue
            close = float(bar.close)
            if len(dq) >= self.WINDOW:
                prior = list(dq)[-self.WINDOW:]
                held = self.portfolio[sym].invested
                if held and close < min(prior):
                    sells.append(sym)
                elif not held and close > max(prior) and sym in self.members:
                    buys.append(sym)
            dq.append(close)
        # exits first (filled at the next close), then fill free slots by dollar volume;
        # names with an unfilled order count as taken, so nothing is ordered twice
        pending = {o.symbol for o in self.transactions.get_open_orders()}
        for sym in sells:
            if sym not in pending:
                self.market_on_close_order(sym, -self.portfolio[sym].quantity)
        taken = {s for s in self.portfolio.keys()
                 if self.portfolio[s].invested and s not in (self.oneq, self.qqq)} | pending
        free = self.MAX_NAMES - len(taken - set(sells))
        if free > 0 and buys:
            buys = [s for s in buys if s not in taken]
            buys.sort(key=lambda s: self.dollar_volume.get(s, 0), reverse=True)
            equity = self.portfolio.total_portfolio_value
            budget = self.portfolio.cash_book["USD"].amount - sum(
                o.quantity * self.securities[o.symbol].price
                for o in self.transactions.get_open_orders() if o.quantity > 0)
            for sym in buys[:free]:
                price = self.securities[sym].price
                if price <= 0:
                    continue
                qty = int(min(equity / self.MAX_NAMES, budget * 0.98) // price)
                if qty >= 1:
                    self.market_on_close_order(sym, qty)
                    budget -= qty * price
        oneq = self.securities[self.oneq].price
        qqq = self.securities[self.qqq].price
        self.daily.append((self.time.date(), self.portfolio.total_portfolio_value, oneq, qqq))

    def on_end_of_algorithm(self):
        def cagr(a, b, years):
            return (b / a) ** (1 / years) - 1 if a > 0 and years > 0 else float("nan")

        def window(start, end):
            rows = [r for r in self.daily if start <= r[0] <= end and r[2] > 0 and r[3] > 0]
            if len(rows) < 2:
                return
            yrs = (rows[-1][0] - rows[0][0]).days / 365.25
            s, o, q = [cagr(rows[0][i], rows[-1][i], yrs) for i in (1, 2, 3)]
            peak, mdd = 0, 0
            for r in rows:
                peak = max(peak, r[1])
                mdd = min(mdd, r[1] / peak - 1)
            self.set_runtime_statistic(f"{start.year}-{end.year} strat/ONEQ/QQQ/MDD",
                                       f"{s:.2%} / {o:.2%} / {q:.2%} / {mdd:.1%}")

        from datetime import date
        window(date(2014, 1, 1), date(2016, 12, 31))
        window(date(2017, 1, 1), date(2022, 12, 31))
        window(date(2023, 1, 1), date(2026, 8, 31))
        window(date(2014, 1, 1), date(2026, 8, 31))
        for y in range(2014, 2027):
            rows = [r for r in self.daily if r[0].year == y and r[2] > 0 and r[3] > 0]
            if len(rows) > 1:
                self.set_runtime_statistic(f"Y{y} strat/ONEQ/QQQ",
                                           f"{rows[-1][1] / rows[0][1] - 1:+.1%} / {rows[-1][2] / rows[0][2] - 1:+.1%} / "
                                           f"{rows[-1][3] / rows[0][3] - 1:+.1%}")
