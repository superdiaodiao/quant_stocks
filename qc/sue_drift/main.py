# region imports
from AlgorithmImports import *
# endregion
import math
from datetime import date, timedelta

# Registered in docs/research_ledger_qc_round2.md (T1) before the run.
EXCL_FLAGGED = False   # True ONLY in the registered data-error sensitivity run (flagged candidates skipped)
BENCH = ("QQQ", "ONEQ")


class SlippageInit(BrokerageModelSecurityInitializer):
    """IBKR brokerage defaults (fees, cash buying power, settlement) plus a constant 0.1% slippage."""

    def __init__(self, brokerage_model, seeder):
        super().__init__(brokerage_model, seeder)

    def initialize(self, security):
        super().initialize(security)
        security.set_slippage_model(ConstantSlippageModel(0.001))


class SueDrift(QCAlgorithm):
    """T1: post-earnings-announcement drift in small / mid caps (Bernard-Thomas SUE), real trading.

    Snapshot: once a month, on the universe selection two trading sessions before the first
    trading day E of the month (data as of the close before the snapshot day). Every snapshot
    caches, for each company with fundamental data and market cap >= $100M, the latest quarter's
    Morningstar basic EPS (earning_reports.basic_eps.three_months; exactly 0 / non-finite = missing)
    keyed by its fiscal period end (earning_reports.period_ending_date, mapped to a calendar month
    index by period end + 15 days). Only the first value seen for a period is kept.
    "Reported in the last month" = the period was first visible in this snapshot (not in the
    previous one), the company was already scanned in the previous snapshot, and the period
    ended within 200 days of the snapshot. QuantConnect's file_date field is not
    used for timing: the diagnostic run showed it is often stale (Q4 rows keep the Q3 date).
    SUE = (EPS_q - EPS_q-4) / sample std of that difference over the previous 8 quarters
    (q-1..q-8; >= 6 valid differences; std > 0); period lookups allow +/- 1 month.
    Universe at the snapshot: price >= $5, dollar volume >= $1M, market cap $300M-$3B, common stock
    (ST00000001), not a depositary receipt, company country USA, no exchange filter, one share
    class per company (largest dollar volume). Rank new reporters with a valid SUE by SUE, highest
    first, ties larger market cap first; hold the top 10 equal weight (keep holdings when < 20 valid).
    Execution at E (two sessions after the snapshot), 30 minutes after the open, market-on-close
    orders; otherwise identical to qc/sp_top10_trade/main.py (2% band, settled cash x 0.98, whole
    shares, buy retries for 10 sessions, IBKR fees, cash account, $10,000, 0.1% slippage, raw prices).
    Caching runs from 1998-01; trading from the 2003-01 rebalance.
    """

    TOP_N = 10
    MIN_MCAP = 300e6
    MAX_MCAP = 3e9
    CACHE_MCAP = 100e6
    MIN_PRICE = 5.0
    MIN_DV = 1e6
    MIN_VALID = 20
    MIN_DIFFS = 6
    FRESH_DAYS = 200
    BAND = 0.02
    BUY_WINDOW = 10
    CASH_BUFFER = 0.98
    SLIP = 0.001
    PAPER_COST = 0.01
    ENTRY_WAIT = 5
    FIRST_TRADE = (2003, 1)

    def initialize(self):
        self.set_start_date(1998, 1, 1)
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

        self.eps = {}              # company -> {period month index: eps}
        self.prev_scanned = set()  # companies scanned (cache-eligible) in the previous snapshot
        self.last_target = None
        self.pending = None
        self.pending_ym = None
        self.sel_date = None
        self.targets = {}
        self.target_order = []
        self.cur_ym = None
        self.buy_days_left = 0
        self.trim = False
        self.paper_roll = False
        self.marks = []
        self.daily = []
        self.paper = None
        self.paper_miss = {}
        self.paper_rows = []
        self.paper_noentry = 0
        self.paper_noexit = 0
        self.delist_px = {}
        self.traded = {}
        self.fees = {}
        self.nfill = {}
        self.n_invalid = 0
        self.n_late = 0
        self.n_ontime = 0
        self.hold_days = 0
        self.hold_sum = 0
        self.cash_sum = 0.0
        self.low_days = 0
        self.skipped = 0
        self.univ = []             # band universe size per formation
        self.nnew = []             # new reporters in band per formation
        self.nvalid = []           # valid SUE per formation
        self.flags = []
        self.n_newbuy = 0
        self.n_flagbuy = 0
        self.n_excl = 0
        self.hy = {}               # year -> {ticker: months held as target}

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

    @staticmethod
    def mi(d):
        d2 = d + timedelta(days=15)
        return d2.year * 12 + d2.month - 1

    @staticmethod
    def look(h, k):
        for j in (k, k - 1, k + 1):
            if j in h:
                return h[j]
        return None

    def sue(self, h, q):
        cur = h.get(q)
        prev = self.look(h, q - 12)
        if cur is None or prev is None:
            return None
        diffs = []
        for k in range(1, 9):
            a = self.look(h, q - 3 * k)
            b = self.look(h, q - 3 * k - 12)
            if a is not None and b is not None:
                diffs.append(a - b)
        if len(diffs) < self.MIN_DIFFS:
            return None
        mu = sum(diffs) / len(diffs)
        sd = math.sqrt(sum((x - mu) ** 2 for x in diffs) / (len(diffs) - 1))
        if not sd > 0:
            return None
        return (cur - prev) / sd

    def check_ratios(self, f, mcap, price):
        """Data-error diagnostics: k = market cap / (shares outstanding x price); rv = reported TTM
        revenue / (S/P x market cap)."""
        k = rv = None
        try:
            so = self.num(f.company_profile.shares_outstanding)
            if so and so > 0 and price > 0:
                k = mcap / (so * price)
        except Exception:
            pass
        try:
            sy = self.nz(f.valuation_ratios.sales_yield)
            line = self.nz(f.financial_statements.income_statement.total_revenue.twelve_months)
            if sy is not None and line is not None:
                rv = line / (sy * mcap)
        except Exception:
            pass
        return k, rv

    @staticmethod
    def is_flag(k, rv):
        return (rv is not None and (rv < 0.5 or rv > 2.0)) or (k is not None and k > 2.0)

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
        hrs = self.securities[self.bench["QQQ"]].exchange.hours
        n1 = hrs.get_next_trading_day(self.time)
        n2 = hrs.get_next_trading_day(n1)
        tgt = (n2.year, n2.month)
        if n2.month != self.time.month and tgt != self.last_target:
            self.last_target = tgt
            return self.form(fundamental, tgt, n2.date())
        if self.trim:
            self.trim = False
            return list(set(self.targets) | set(self.held()))
        return Universe.UNCHANGED

    def form(self, fundamental, ym, exec_date):
        now = self.time.date()
        cand = {}
        freshc = {}                # company -> period month index first visible in this snapshot
        scanned = set()
        for f in fundamental:
            if not f.has_fundamental_data:
                continue
            mcap = float(f.market_cap)
            if mcap < self.CACHE_MCAP:
                continue
            cr = f.company_reference
            cid = (cr.company_id if cr is not None else None) or str(f.symbol.id)
            scanned.add(cid)
            try:
                er = f.earning_reports
                pe = er.period_ending_date.value
                e3 = self.nz(er.basic_eps.three_months)
                if pe is not None and pe.year > 1900 and e3 is not None:
                    ped = pe.date()
                    q = self.mi(ped)
                    h = self.eps.setdefault(cid, {})
                    if q not in h:
                        h[q] = e3
                        if (now - ped).days <= self.FRESH_DAYS and cid in self.prev_scanned:
                            freshc[cid] = q
                        for old in [x for x in h if x < q - 60]:
                            del h[old]
            except Exception:
                pass
            p = f.price
            if p < self.MIN_PRICE or f.dollar_volume < self.MIN_DV or mcap < self.MIN_MCAP or mcap > self.MAX_MCAP:
                continue
            sr = f.security_reference
            if sr is None or cr is None:
                continue
            if sr.security_type != "ST00000001" or sr.is_depositary_receipt or cr.country_id != "USA":
                continue
            prev = cand.get(cid)
            if prev is None or f.dollar_volume > prev.dollar_volume:
                cand[cid] = f
        self.prev_scanned = scanned
        rows = []
        nnew = 0
        for cid, f in cand.items():
            fresh = freshc.get(cid)
            if fresh is None:
                continue
            nnew += 1
            s = self.sue(self.eps[cid], fresh)
            if s is not None and math.isfinite(s):
                rows.append((s, float(f.market_cap), f))
        if ym < self.FIRST_TRADE:
            return Universe.UNCHANGED
        self.univ.append(len(cand))
        self.nnew.append(nnew)
        self.nvalid.append(len(rows))
        if len(rows) < self.MIN_VALID:
            self.skipped += 1
            return Universe.UNCHANGED
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        cur = set(self.targets)
        picks = []
        for v, mc, f in rows:
            if len(picks) >= self.TOP_N:
                break
            price = float(f.price)
            k, rv = self.check_ratios(f, mc, price)
            bad = self.is_flag(k, rv)
            if bad and EXCL_FLAGGED:
                self.n_excl += 1
                continue
            try:
                name = (f.company_reference.short_name or "")[:16]
            except Exception:
                name = ""
            try:
                sec = int(f.asset_classification.morningstar_sector_code)
            except Exception:
                sec = 0
            try:
                e3 = float(f.earning_reports.basic_eps.three_months)
            except Exception:
                e3 = float("nan")
            picks.append({"sym": f.symbol, "r": len(picks) + 1, "v": v, "mc": mc, "p": price, "k": k, "rv": rv,
                          "sec": sec, "name": name, "e": e3})
            if f.symbol not in cur:
                self.n_newbuy += 1
                if bad:
                    self.n_flagbuy += 1
                    self.flags.append(f"{f.symbol.value}{ym[0] % 100:02d}{ym[1]:02d}")
        hy = self.hy.setdefault(ym[0], {})
        for d in picks:
            hy[d["sym"].value] = hy.get(d["sym"].value, 0) + 1
        self.pending = picks
        self.pending_ym = ym
        self.sel_date = exec_date
        self.log(f"{ym[0]}{ym[1]:02d} " + " ".join(
            f"{d['sym'].value}:{d['v']:.1f}:{d['mc'] / 1e6:.0f}" for d in picks))
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
        return (f"{base} r{d['r']} v{d['v']:.4g} e{d['e']:.3g} mc{d['mc'] / 1e6:.0f} p{d['p']:.2f} k{k} rv{rv} "
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
        new = [s for s in self.target_order
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
        if d.year < self.FIRST_TRADE[0]:
            return
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
        months = []
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

        # daily judged-benchmark index: ONEQ day return when both closes exist, else QQQ
        bidx = []
        lvl = 1.0
        for i, r in enumerate(self.daily):
            if i > 0:
                a = self.daily[i - 1]
                if a[2] > 0 and r[2] > 0:
                    lvl *= r[2] / a[2]
                elif a[3] > 0 and r[3] > 0:
                    lvl *= r[3] / a[3]
            bidx.append((r[0], r[1], lvl))

        def daily_mdd(d0, d1, col):
            pk, worst = 0.0, 0.0
            for r in bidx:
                if d0 <= r[0] <= d1:
                    pk = max(pk, r[col])
                    worst = min(worst, r[col] / pk - 1)
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
            dd = daily_mdd(w[0][4], w[-1][5], 1)
            bdd = daily_mdd(w[0][4], w[-1][5], 2)
            res[lab] = (cs ** (12.0 / n) - 1, cj ** (12.0 / n) - 1, t, dd, bdd)
            self.set_runtime_statistic(lab, (
                f"S {p(cs ** (12.0 / n) - 1)},{p(ms)},dd{p(dd)} J {p(cj ** (12.0 / n) - 1)},{p(mj)},dd{p(bdd)} "
                f"Q {p(cq ** (12.0 / n) - 1)},{p(mq)} ex {mu * 100:.2f}% t {t:.2f} n {n}"))
        ok = all(k in res for k in ("H1", "H2", "F"))
        pa = ok and res["H1"][0] > res["H1"][1] and res["H2"][0] > res["H2"][1] and res["F"][2] >= 2.39
        pb = ok and all(res[h][3] - res[h][4] >= 0.10 and res[h][0] >= res[h][1] - 0.03 for h in ("H1", "H2"))
        self.set_runtime_statistic("PASS", f"A {int(bool(pa))} B {int(bool(pb))} excl {int(EXCL_FLAGGED)} n_excl {self.n_excl}")

        jm = {m[0]: m[2] for m in months}
        for lab, a, b in (("PH1", 200301, 201212), ("PH2", 201301, 202606), ("PF", 200301, 202606)):
            pr = [(r - self.PAPER_COST / 12.0, jm[ym]) for ym, r in self.paper_rows
                  if r is not None and ym in jm and a <= ym[0] * 100 + ym[1] <= b]
            if len(pr) < 12:
                continue
            c, mdd = self.chain([x[0] for x in pr])
            mu, t = tstat([x[0] - x[1] for x in pr])
            self.set_runtime_statistic(lab, f"{p(c ** (12.0 / len(pr)) - 1)},{p(mdd)} t {t:.2f} n {len(pr)}")

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

        def q3(v):
            u = sorted(v)
            return f"{u[0]}/{u[len(u) // 2]}/{u[-1]}" if u else "x"
        self.set_runtime_statistic("MISC", (
            f"months {len(months)} marks {len(self.marks)} skipped {self.skipped} avgheld {self.hold_sum / hd:.2f} "
            f"cash {self.cash_sum / hd * 100:.1f}% low {self.low_days} pnoentry {self.paper_noentry} "
            f"pnoexit {self.paper_noexit} univ {q3(self.univ)} new {q3(self.nnew)} valid {q3(self.nvalid)} "
            f"end {self.portfolio.total_portfolio_value:.0f}"))
        self.set_runtime_statistic("NFLAG", f"newbuys {self.n_newbuy} flagged {self.n_flagbuy} "
                                            f"share {self.n_flagbuy / max(self.n_newbuy, 1) * 100:.1f}%")
        fl = " ".join(self.flags)
        for i in range(3):
            if fl[i * 200:(i + 1) * 200]:
                self.set_runtime_statistic(f"FLAG{i + 1}", fl[i * 200:(i + 1) * 200])
        # compact holdings per year: most frequent target names (ticker:months), two years per key
        hys = sorted(self.hy)
        for i in range(0, len(hys), 2):
            seg = []
            for y in hys[i:i + 2]:
                top = sorted(self.hy[y].items(), key=lambda kv: -kv[1])
                seg.append(f"{y % 100:02d}:" + ",".join(f"{t}{c}" for t, c in top))
            body = []
            budget = 196 // max(len(seg), 1)
            for s in seg:
                body.append(s[:budget])
            self.set_runtime_statistic(f"HY{hys[i] % 100:02d}", " ".join(body))
