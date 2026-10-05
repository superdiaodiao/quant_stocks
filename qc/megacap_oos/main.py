# region imports
from AlgorithmImports import *
# endregion
import math
from datetime import date, timedelta


class MegacapOOS(QCAlgorithm):
    """Out-of-sample test (1999-2013) of the frozen mega-cap rules M1 / M2 / M6, registered in
    docs/research_ledger_megacap.md (section "样本外检验") before any run. Rules unchanged from the
    2014-2026 study (scripts/research_megacap.py).

    M1: top 10 by market cap, equal weight.  M2: top 5, equal weight.  M6: top 10, cap-weighted.
    Universe (point-in-time, delisted names included): Nasdaq-primary (NAS) common stocks
    (ST00000001), not depositary receipts, company country USA, market cap from f.market_cap > 0;
    one share class per company (the one with the largest dollar volume).
    Monthly: the first trading day's selection uses the prior close's data. Market-on-close
    orders. Sells (dropped names, and trims of kept names more than 25% above target) on day 1;
    buys (new names, and top-ups of kept names more than 25% below target) from day 2, capped by
    settled cash (scaled pro rata when short), retried for up to 10 sessions; whole shares.
    Kept names within +/-25% of target are not traded (REBALANCE_BAND = 0.25).
    IBKR fee model, cash account, $10,000. QQQ (from 1999-03) and ONEQ (from 2003-10) recorded daily.
    The variant is the VARIANT line (one project per variant) unless a 'variant' parameter is set.
    """

    VARIANT = "M1"
    RULES = {"M1": (10, False), "M2": (5, False), "M6": (10, True)}   # (names, cap-weighted)
    BAND = 0.25
    BUY_WINDOW = 10

    def initialize(self):
        self.set_start_date(1999, 1, 1)
        self.set_end_date(2013, 12, 31)
        self.set_cash(10_000)
        self.set_brokerage_model(BrokerageName.INTERACTIVE_BROKERS_BROKERAGE, AccountType.CASH)
        v = self.get_parameter("variant")
        self.variant = v if v in self.RULES else self.VARIANT
        self.top_n, self.capw = self.RULES[self.variant]
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = DataNormalizationMode.ADJUSTED
        self.add_universe(self.select)
        self.qqq = self.add_equity("QQQ", Resolution.DAILY).symbol
        self.oneq = self.add_equity("ONEQ", Resolution.DAILY).symbol
        self.spy = self.add_equity("SPY", Resolution.DAILY).symbol   # QC benchmark only (exists in 1999)
        self.set_benchmark(self.spy)
        self.last_month = None
        self.weights = None        # symbol -> target weight from the last rebalance
        self.topup = set()         # kept names that need a top-up at this rebalance
        self.need_sells = False
        self.buy_days_left = 0
        self.n_rebalances = 0
        self.daily = []            # (date, equity, qqq close, oneq close)

    # ----- universe -------------------------------------------------------------------------
    def base_ok(self, f):
        if not f.has_fundamental_data or f.price <= 0:
            return False
        sr = f.security_reference
        cr = f.company_reference
        if sr is None or cr is None:
            return False
        if sr.security_type != "ST00000001" or sr.exchange_id != "NAS":
            return False
        if sr.is_depositary_receipt or cr.country_id != "USA":
            return False
        mc = self.num(f.market_cap)
        return mc is not None and mc > 0

    @staticmethod
    def num(x):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    def held(self):
        return [s for s in self.portfolio.keys()
                if self.portfolio[s].invested and s not in (self.qqq, self.oneq, self.spy)]

    def select(self, fundamental):
        month = self.time.month
        if month == self.last_month:
            return Universe.UNCHANGED
        self.last_month = month
        best = {}   # company id -> (dollar volume, symbol, market cap)
        for f in fundamental:
            if not self.base_ok(f):
                continue
            cid = f.company_reference.company_id or str(f.symbol)
            dv = self.num(f.dollar_volume) or 0.0
            if cid not in best or dv > best[cid][0]:
                best[cid] = (dv, f.symbol, float(f.market_cap))
        rows = sorted(best.values(), key=lambda r: (-r[2], str(r[1])))[:self.top_n]
        if len(rows) < self.top_n:
            return Universe.UNCHANGED   # data gap: keep the current holdings
        if self.capw:
            tot = sum(r[2] for r in rows)
            self.weights = {r[1]: r[2] / tot for r in rows}
        else:
            self.weights = {r[1]: 1.0 / self.top_n for r in rows}
        self.need_sells = True
        self.buy_days_left = self.BUY_WINDOW
        self.n_rebalances += 1
        return list(set(self.weights) | set(self.held()))

    # ----- trading --------------------------------------------------------------------------
    def bar_date(self):
        t = self.time
        return t.date() if t.hour >= 9 else (t - timedelta(days=1)).date()

    def on_data(self, data):
        self.record()
        if self.weights is None:
            return
        open_orders = self.transactions.get_open_orders()
        pending = {o.symbol for o in open_orders}
        if self.need_sells:
            self.need_sells = False
            equity = self.portfolio.total_portfolio_value
            self.topup = set()
            for sym in self.held():
                if sym in pending:
                    continue
                qty = self.portfolio[sym].quantity
                if sym not in self.weights:
                    self.market_on_close_order(sym, -qty)
                    continue
                tv = self.weights[sym] * equity
                cur = self.portfolio[sym].holdings_value
                if abs(tv - cur) > self.BAND * tv:
                    if cur > tv:
                        price = self.securities[sym].price
                        n = int((cur - tv) // price) if price > 0 else 0
                        if n >= 1:
                            self.market_on_close_order(sym, -min(n, qty))
                    else:
                        self.topup.add(sym)
            return
        if self.buy_days_left <= 0:
            return
        self.buy_days_left -= 1
        if any(o.quantity < 0 for o in open_orders):
            return  # wait for the sells to fill first
        equity = self.portfolio.total_portfolio_value
        held = set(self.held())
        need = {}   # symbol -> (dollars still wanted, price)
        for sym, w in self.weights.items():
            if sym in held and sym not in self.topup:
                continue
            if sym not in self.securities:
                continue
            price = self.securities[sym].price
            if price <= 0:
                continue
            on_order = sum(o.quantity for o in open_orders if o.symbol == sym and o.quantity > 0)
            gap = w * equity - (self.portfolio[sym].quantity + on_order) * price
            if gap >= price:
                need[sym] = (gap, price)
        if not need:
            self.buy_days_left = 0
            return
        budget = self.portfolio.cash_book["USD"].amount - sum(
            o.quantity * self.securities[o.symbol].price for o in open_orders if o.quantity > 0)
        total = sum(g for g, _ in need.values())
        scale = min(1.0, budget * 0.98 / total) if total > 0 else 0.0
        for sym in sorted(need, key=str):
            gap, price = need[sym]
            qty = int(gap * scale // price)
            if qty >= 1:
                self.market_on_close_order(sym, qty)

    def record(self):
        d = self.bar_date()
        row = (d, self.portfolio.total_portfolio_value,
               self.securities[self.qqq].price, self.securities[self.oneq].price)
        if self.daily and self.daily[-1][0] == d:
            self.daily[-1] = row
        else:
            self.daily.append(row)

    # ----- statistics -----------------------------------------------------------------------
    def on_end_of_algorithm(self):
        rows = self.daily
        if len(rows) < 2:
            return
        st = self.set_runtime_statistic
        st("variant / rebalances", f"{self.variant} / {self.n_rebalances}")

        def cagr(seg, i):
            yrs = (seg[-1][0] - seg[0][0]).days / 365.25
            a, b = seg[0][i], seg[-1][i]
            return (b / a) ** (1 / yrs) - 1 if a > 0 and yrs > 0 else float("nan")

        def mdd(seg, i):
            peak, worst = 0, 0
            for r in seg:
                peak = max(peak, r[i])
                if peak > 0:
                    worst = min(worst, r[i] / peak - 1)
            return worst

        def segment(start, end, need=None):
            """Rows in [start, end] (only where column `need` > 0), based on the last close before."""
            idx = [k for k, r in enumerate(rows)
                   if start <= r[0] <= end and (need is None or r[need] > 0)]
            if len(idx) < 2:
                return None
            first = idx[0] - 1 if idx[0] > 0 and (need is None or rows[idx[0] - 1][need] > 0) else idx[0]
            return rows[first:idx[-1] + 1]

        def window(label, start, end):
            full = segment(start, end)
            if full is not None:
                st(f"{label} strat CAGR/MDD (full)", f"{cagr(full, 1):.2%} / {mdd(full, 1):.1%}")
            for i, name in ((2, "QQQ"), (3, "ONEQ")):
                seg = segment(start, end, i)
                if seg is None:
                    continue
                st(f"{label} vs {name} [{seg[0][0]}..{seg[-1][0]}] strat/{name} CAGR",
                   f"{cagr(seg, 1):.2%} / {cagr(seg, i):.2%}")
                st(f"{label} vs {name} strat/{name} MDD", f"{mdd(seg, 1):.1%} / {mdd(seg, i):.1%}")

        window("1999-2002", date(1999, 1, 1), date(2002, 12, 31))
        window("2003-2007", date(2003, 1, 1), date(2007, 12, 31))
        window("2008-2013", date(2008, 1, 1), date(2013, 12, 31))
        window("1999-2013", date(1999, 1, 1), date(2013, 12, 31))

        for y in range(1999, 2014):
            seg = segment(date(y, 1, 1), date(y, 12, 31))
            if seg is None:
                continue

            def ret(i):
                s = [r for r in seg if r[i] > 0]
                return f"{s[-1][i] / s[0][i] - 1:+.1%}" if len(s) > 1 else "na"
            st(f"Y{y} strat/QQQ/ONEQ", f"{seg[-1][1] / seg[0][1] - 1:+.1%} / {ret(2)} / {ret(3)}")

        # monthly returns from month-end closes
        month_end = {}
        for r in rows:
            month_end[(r[0].year, r[0].month)] = r
        keys = sorted(month_end)
        monthly = []   # (year, month, strat, qqq or None, oneq or None)
        prev = rows[0]
        for k in keys:
            cur = month_end[k]
            q = cur[2] / prev[2] - 1 if prev[2] > 0 and cur[2] > 0 else None
            o = cur[3] / prev[3] - 1 if prev[3] > 0 and cur[3] > 0 else None
            monthly.append((k[0], k[1], cur[1] / prev[1] - 1, q, o))
            prev = cur
        for y in range(1999, 2014):
            items = [m for m in monthly if m[0] == y]
            if items:
                st(f"M{y} strat:QQQ %", " ".join(
                    f"{m[2] * 100:.2f}:" + (f"{m[3] * 100:.2f}" if m[3] is not None else "na")
                    for m in items))

        def excess_t(items, j, label):
            ex = [m[2] - m[j] for m in items if m[j] is not None]
            n = len(ex)
            if n < 3:
                return
            mean = sum(ex) / n
            sd = math.sqrt(sum((x - mean) ** 2 for x in ex) / (n - 1))
            t = mean / (sd / math.sqrt(n)) if sd > 0 else float("nan")
            st(f"{label} monthly excess mean/t/n", f"{mean:.2%} / {t:.2f} / {n}")

        excess_t(monthly, 3, "1999-2013 vs QQQ")
        excess_t([m for m in monthly if m[0] <= 2002], 3, "1999-2002 vs QQQ")
        excess_t([m for m in monthly if 2003 <= m[0] <= 2007], 3, "2003-2007 vs QQQ")
        excess_t([m for m in monthly if m[0] >= 2008], 3, "2008-2013 vs QQQ")
        excess_t(monthly, 4, "2003.10-2013 vs ONEQ")
