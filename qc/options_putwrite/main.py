# region imports
from AlgorithmImports import *
# endregion
import math
from datetime import date, timedelta

# Registered in docs/research_ledger_qc_options.md (section 0) before any strategy run.
# Only these three constants differ between the registered runs.
MODE = "O1"        # "O1" QQQ cash-secured put | "O2" QQQ covered call | "O3" large-cap puts | "O4" QQQ wheel
CASH0 = 1_000_000  # O1/O2/O4 scaled account 1,000,000; O3 and the O1 feasibility run 10,000
FILL_F = 0.25      # option fill = mid -/+ FILL_F x (ask - bid); 0.25 primary, 0.5 (= at the bid) sensitivity

UNDER = "QQQ"
TARGET_DELTA = 0.25
MIN_DTE = 14
TARGET_DTE = 30
EQ_SLIP = 0.001
TOP_N = 10
H1 = (201203, 201812)
H2 = (201901, 202607)
FULL = (201203, 202607)


FILL_CALLS = [0, 0]   # [market_fill calls, fills priced from quotes]


class MidSpreadFill(ImmediateFillModel):
    """Market orders on options fill at mid - F x spread (sells) or mid + F x spread (buys),
    using the contract's latest hourly quote."""

    def __init__(self, f):
        super().__init__()
        self.f = f

    def market_fill(self, asset, order):
        FILL_CALLS[0] += 1
        fill = super().market_fill(asset, order)
        b, a = float(asset.bid_price), float(asset.ask_price)
        if b > 0 and a >= b and fill.status == OrderStatus.FILLED:
            mid, sp = (a + b) / 2.0, a - b
            fill.fill_price = mid - self.f * sp if order.quantity < 0 else mid + self.f * sp
            FILL_CALLS[1] += 1
        return fill

    MarketFill = market_fill


class Init(BrokerageModelSecurityInitializer):
    def __init__(self, brokerage_model, seeder):
        super().__init__(brokerage_model, seeder)

    def initialize(self, security):
        super().initialize(security)
        if security.type == SecurityType.OPTION:
            security.set_fill_model(MidSpreadFill(FILL_F))
        elif security.type == SecurityType.EQUITY:
            security.set_slippage_model(ConstantSlippageModel(EQ_SLIP))


class OptionsPutWrite(QCAlgorithm):
    """O1: each cycle sell floor(cash / (K x 100)) QQQ puts, |delta| closest to 0.25, standard monthly
    expiry closest to 30 days (>= 14), hold to expiry; assigned shares are sold at the next session.
    O2: hold floor(NAV / (S x 100)) lots of QQQ and sell one 0.25-delta call per lot; if called away,
    buy back at the next session. O3: top-10 US common stocks by market cap; in market-cap order sell
    ONE 0.25-delta put per name while free cash >= K x 100; assigned shares sold next session.
    O4 wheel: covered calls on every 100 shares held, cash-secured puts with the remaining cash,
    shares are never sold except by call assignment.
    A new cycle starts on the first session (10:35) with no open option position.
    Idle cash earns the BIL total return (credited daily). IBKR fee model, margin account type
    (needed for short options; never levered: puts are fully cash-secured)."""

    def initialize(self):
        self.set_start_date(2012, 2, 1)
        self.set_end_date(2026, 7, 31)
        self.set_cash(CASH0)
        self.set_brokerage_model(BrokerageName.INTERACTIVE_BROKERS_BROKERAGE, AccountType.MARGIN)
        self.set_security_initializer(Init(self.brokerage_model, FuncSecuritySeeder(self.get_last_known_prices)))
        self.q = self.add_equity(UNDER, Resolution.HOUR, data_normalization_mode=DataNormalizationMode.RAW).symbol
        self.oneq = self.add_equity("ONEQ", Resolution.HOUR, data_normalization_mode=DataNormalizationMode.ADJUSTED).symbol
        self.bil = self.add_equity("BIL", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.ADJUSTED).symbol
        self.set_benchmark(self.oneq)
        self.universe_settings.resolution = Resolution.HOUR
        self.universe_settings.data_normalization_mode = DataNormalizationMode.RAW
        self.top = []                  # O3 current top-10 symbols (market-cap order)
        self.last_month = None
        if MODE == "O3":
            self.add_universe(self.select)
        q = self.q
        self.schedule.on(self.date_rules.every_day(q), self.time_rules.after_market_open(q, 5), self.prepare)
        self.schedule.on(self.date_rules.every_day(q), self.time_rules.after_market_open(q, 65), self.trade)
        self.schedule.on(self.date_rules.every_day(q), self.time_rules.at(16, 5), self.eod)

        self.plan = []                 # [(option symbol, underlying, K, delta, S, dte)]
        self.marks = []                # [date, nav, nav_noint, oneq, qqq_tr]
        self.mid = {}                  # option symbol -> last valid mid
        self.qtr = 1.0
        self.q_prev = None
        self.bil_prev = None
        self.interest = {}
        self.int_cum = 0.0
        self.add_ok = None
        self.prem = {}                 # year -> premium received
        self.fees = {}
        self.n_sold = {}               # year -> contracts sold
        self.n_assign = {}             # year -> contracts assigned
        self.assign_detail = []
        self.cycles = 0
        self.idle_cycles = 0           # cycles where nothing was affordable / available
        self.no_quote = 0
        self.mny = []                  # K / S - 1 at sale
        self.sprd = []                 # spread / mid at sale
        self.dte = []
        self.dl = []
        self.afford10k = [0, 0]        # QQQ cycles: [K x 100 <= 10,000, total]
        self.util = []                 # collateral (puts) or stock (calls) / NAV at cycle start
        self.errs = []
        self.names = {}                # O3: ticker -> puts sold
        self.split_events = 0

    # ---------------------------------------------------------------- universe (O3)
    def select(self, fundamental):
        ym = (self.time.year, self.time.month)
        if ym == self.last_month:
            return Universe.UNCHANGED
        self.last_month = ym
        cand = {}
        for f in fundamental:
            if f.price <= 0 or not f.has_fundamental_data:
                continue
            mc = float(f.market_cap)
            if mc <= 0:
                continue
            sr, cr = f.security_reference, f.company_reference
            if sr is None or cr is None:
                continue
            if sr.security_type != "ST00000001" or sr.is_depositary_receipt or cr.country_id != "USA":
                continue
            cid = cr.company_id or str(f.symbol.id)
            prev = cand.get(cid)
            if prev is None or f.dollar_volume > prev.dollar_volume:
                cand[cid] = f
        rows = sorted(cand.values(), key=lambda f: float(f.market_cap), reverse=True)[:TOP_N]
        self.top = [f.symbol for f in rows]
        return self.top

    # ---------------------------------------------------------------- helpers
    def held(self):
        out = []
        for s in list(self.portfolio.keys()):
            h = self.portfolio[s]
            if h.quantity != 0:
                out.append((s, h.quantity))
        return out

    def short_options(self):
        return [s for s, qty in self.held() if s.security_type == SecurityType.OPTION]

    def stock_lots(self):
        return [(s, qty) for s, qty in self.held()
                if s.security_type == SecurityType.EQUITY and s not in (self.oneq, self.bil)]

    @staticmethod
    def monthly(d):
        wd = d.weekday()
        return (wd == 4 and 15 <= d.day <= 21) or (wd == 3 and 14 <= d.day <= 20) or (wd == 5 and 16 <= d.day <= 22)

    def pick(self, und, right):
        """Contract with |delta| closest to 0.25 on the standard monthly expiry closest to 30 days."""
        try:
            cs = list(self.option_chain(und))
        except Exception as e:
            self.errs.append(f"ch{und.value}:{str(e)[:30]}")
            return None
        today = self.time.date()
        cs = [c for c in cs if c.right == right]
        exps = sorted({c.expiry.date() for c in cs
                       if self.monthly(c.expiry.date()) and (c.expiry.date() - today).days >= MIN_DTE})
        if not exps:
            return None
        ex = min(exps, key=lambda e: (abs((e - today).days - TARGET_DTE), e))
        best, key = None, None
        for c in cs:
            if c.expiry.date() != ex or c.greeks is None:
                continue
            d = float(c.greeks.delta)
            if d == 0 or not math.isfinite(d):
                continue
            k = (abs(abs(d) - TARGET_DELTA), float(c.strike) if right == OptionRight.PUT else -float(c.strike))
            if key is None or k < key:
                best, key = c, k
        if best is None:
            return None
        return (best.symbol, und, float(best.strike), float(best.greeks.delta), (ex - today).days)

    def quote(self, s):
        if not self.securities.contains_key(s):
            return None
        sec = self.securities[s]
        b, a = float(sec.bid_price), float(sec.ask_price)
        if b > 0 and a >= b:
            return b, a
        return None

    # ---------------------------------------------------------------- daily cycle
    def prepare(self):
        """09:35: if no option is open, choose contracts and subscribe (hourly)."""
        self.plan = []
        if self.short_options():
            return
        if MODE == "O3":
            # efficiency shortcut: skip names whose 0.7 x spot x 100 already exceeds the cash available
            free = float(self.portfolio.cash) + sum(q * float(self.securities[s].price) for s, q in self.stock_lots())
            unds = [s for s in self.top if self.securities.contains_key(s)
                    and 0 < float(self.securities[s].price) * 70 <= free]
            rights = [OptionRight.PUT] * len(unds)
        elif MODE in ("O1",):
            unds, rights = [self.q], [OptionRight.PUT]
        elif MODE == "O2":
            unds, rights = [self.q], [OptionRight.CALL]
        else:
            held = self.portfolio[self.q].quantity
            unds, rights = [], []
            if held >= 100:
                unds.append(self.q)
                rights.append(OptionRight.CALL)
            unds.append(self.q)
            rights.append(OptionRight.PUT)
        for u, r in zip(unds, rights):
            p = self.pick(u, r)
            if p is None:
                continue
            self.add_option_contract(p[0], Resolution.HOUR)
            self.plan.append(p)

    def trade(self):
        """10:35: sell assigned shares (O1/O3), resize stock (O2), then sell the planned options."""
        if self.short_options():
            return
        nav = self.nav()
        if MODE in ("O1", "O3"):
            for s, qty in self.stock_lots():
                self.market_order(s, -qty, tag="sell assigned")
        if MODE == "O2":
            s = float(self.securities[self.q].price)
            if s > 0:
                lots = int(nav // (s * (1 + EQ_SLIP) * 100 * 1.001))
                diff = lots * 100 - self.portfolio[self.q].quantity
                if diff != 0:
                    self.market_order(self.q, diff, tag="stock to lots")
        if not self.plan:
            return
        ready = [p for p in self.plan if self.quote(p[0]) is not None]
        if len(ready) < len(self.plan):
            self.no_quote += len(self.plan) - len(ready)
        if not ready:
            return  # retry tomorrow (new selection)
        free = float(self.portfolio.cash)
        used = 0.0
        sold_any = False
        for (sym, und, k, d, dte) in ready:
            spot = float(self.securities[und].price)
            if und == self.q and sym.id.option_right == OptionRight.PUT:
                self.afford10k[1] += 1
                if k * 100 <= 10_000:
                    self.afford10k[0] += 1
            if sym.id.option_right == OptionRight.PUT:
                n = int(free // (k * 100))
                if MODE == "O3":
                    n = min(n, 1)
                if n < 1:
                    continue
                free -= n * k * 100
                used += n * k * 100
            else:
                n = int(self.portfolio[und].quantity // 100)
                if n < 1:
                    continue
                used += n * 100 * spot
            b, a = self.quote(sym)
            self.market_order(sym, -n, tag=f"{MODE} K{k:g} d{d:.2f} S{spot:.2f} dte{dte} b{b:.2f} a{a:.2f}")
            sold_any = True
            self.mny.append(k / spot - 1 if spot > 0 else 0.0)
            self.sprd.append((a - b) / ((a + b) / 2) if a + b > 0 else 0.0)
            self.dte.append(dte)
            self.dl.append(abs(d))
            if MODE == "O3":
                self.names[und.value] = self.names.get(und.value, 0) + 1
        if sold_any:
            self.cycles += 1
            self.util.append(used / nav if nav > 0 else 0.0)
        self.plan = []

    def on_order_event(self, e):
        if e.status != OrderStatus.FILLED and e.status != OrderStatus.PARTIALLY_FILLED:
            return
        y = self.time.year
        self.fees[y] = self.fees.get(y, 0.0) + float(e.order_fee.value.amount)
        if e.symbol.security_type == SecurityType.OPTION:
            if e.is_assignment:
                self.n_assign[y] = self.n_assign.get(y, 0) + abs(int(e.fill_quantity))
                if len(self.assign_detail) < 60:
                    r = "P" if e.symbol.id.option_right == OptionRight.PUT else "C"
                    self.assign_detail.append(f"{self.time.year % 100}{self.time.month:02d}{self.time.day:02d}{r}"
                                              f"{e.symbol.underlying.value[:4]}")
            elif e.fill_quantity < 0:
                self.prem[y] = self.prem.get(y, 0.0) + abs(float(e.fill_quantity)) * float(e.fill_price) * 100
                self.n_sold[y] = self.n_sold.get(y, 0) + abs(int(e.fill_quantity))

    def on_data(self, data):
        for kv in data.dividends:
            if kv.key == self.q and self.q_prev:
                self.q_div = float(kv.value.distribution)
        for kv in data.splits:
            if kv.value.type == SplitType.SPLIT_OCCURRED:
                for s in self.short_options():
                    if s.underlying == kv.key:
                        self.split_events += 1

    def nav(self):
        v = float(self.portfolio.cash)
        for s, qty in self.held():
            if s.security_type == SecurityType.OPTION:
                qt = self.quote(s)
                if qt is not None:
                    self.mid[s] = (qt[0] + qt[1]) / 2
                m = self.mid.get(s, float(self.securities[s].price))
                v += qty * m * 100
            else:
                v += qty * float(self.securities[s].price)
        return v

    def eod(self):
        # BIL interest on positive cash
        bp = float(self.securities[self.bil].price)
        cash = float(self.portfolio.cash)
        if self.bil_prev and bp > 0 and cash > 0:
            x = cash * (bp / self.bil_prev - 1)
            try:
                self.portfolio.cash_book["USD"].add_amount(x)
                self.add_ok = True
            except Exception as ex:
                self.add_ok = False
                self.errs.append("add:" + str(ex)[:40])
            y = self.time.year
            self.interest[y] = self.interest.get(y, 0.0) + x
            self.int_cum += x
        if bp > 0:
            self.bil_prev = bp
        # QQQ total return from raw close + cash dividends
        qp = float(self.securities[self.q].price)
        div = getattr(self, "q_div", 0.0)
        self.q_div = 0.0
        if self.q_prev and qp > 0:
            self.qtr *= (qp + div) / self.q_prev
        if qp > 0:
            self.q_prev = qp
        nav = self.nav()
        if not self.short_options():
            self.idle_cycles += 1
        self.marks.append([self.time.date(), nav, nav - self.int_cum, float(self.securities[self.oneq].price), self.qtr])

    # ---------------------------------------------------------------- statistics
    def on_end_of_algorithm(self):
        def p(x):
            return "x" if x is None else f"{x * 100:.1f}"

        m_end = {}
        for r in self.marks:
            m_end[(r[0].year, r[0].month)] = r
        yms = sorted(m_end)
        months = []    # (ym, S, S_noint, ONEQ, QQQ)
        for i in range(1, len(yms)):
            a, b = m_end[yms[i - 1]], m_end[yms[i]]
            if a[1] <= 0 or a[3] <= 0:
                continue
            months.append((yms[i], b[1] / a[1] - 1, b[2] / a[2] - 1, b[3] / a[3] - 1, b[4] / a[4] - 1))

        def code(ym):
            return ym[0] * 100 + ym[1]

        def chain(rs):
            c = 1.0
            for r in rs:
                c *= 1 + r
            return c

        def ddd(lo, hi, col):
            pk, w = 0.0, 0.0
            for r in self.marks:
                c = r[0].year * 100 + r[0].month
                if lo <= c <= hi and r[col] > 0:
                    pk = max(pk, r[col])
                    w = min(w, r[col] / pk - 1)
            return w

        res = {}
        for lab, (lo, hi) in (("H1", H1), ("H2", H2), ("F", FULL)):
            w = [m for m in months if lo <= code(m[0]) <= hi]
            n = len(w)
            if n < 12:
                self.set_runtime_statistic(lab, f"n {n}")
                continue
            ann = [chain([m[j] for m in w]) ** (12.0 / n) - 1 for j in (1, 2, 3, 4)]
            ex = [m[1] - m[3] for m in w]
            mu = sum(ex) / n
            sd = math.sqrt(sum((x - mu) ** 2 for x in ex) / (n - 1))
            t = mu / (sd / math.sqrt(n)) if sd > 0 else 0.0
            exq = [m[1] - m[4] for m in w]
            muq = sum(exq) / n
            sdq = math.sqrt(sum((x - muq) ** 2 for x in exq) / (n - 1))
            tq = muq / (sdq / math.sqrt(n)) if sdq > 0 else 0.0
            mS = sum(m[1] for m in w) / n
            sdS = math.sqrt(sum((m[1] - mS) ** 2 for m in w) / (n - 1)) * math.sqrt(12)
            dS, dJ, dQ = ddd(lo, hi, 1), ddd(lo, hi, 3), ddd(lo, hi, 4)
            res[lab] = (ann[0], ann[2], t, dS, dJ)
            self.set_runtime_statistic(lab, (
                f"S {p(ann[0])} dd{p(dS)} vol{p(sdS)} noint {p(ann[1])} | J {p(ann[2])} dd{p(dJ)} | "
                f"Q {p(ann[3])} dd{p(dQ)} | exJ {mu * 100:.2f}% t {t:.2f} | exQ {muq * 100:.2f}% t {tq:.2f} n {n}"))
        ok = all(k in res for k in ("H1", "H2", "F"))
        A = ok and res["H1"][0] > res["H1"][1] and res["H2"][0] > res["H2"][1] and res["F"][2] >= 2.50
        B = ok and all(res[h][3] - res[h][4] >= 0.10 and res[h][0] >= res[h][1] - 0.03 for h in ("H1", "H2"))
        self.set_runtime_statistic("PASS", f"{MODE} F{FILL_F} cash{CASH0} A{int(A)} B{int(B)}")

        yrs = sorted({m[0][0] for m in months})
        parts = []
        for y in yrs:
            w = [m for m in months if m[0][0] == y]
            parts.append(f"{y % 100:02d}:{p(chain([m[1] for m in w]) - 1)}/{p(chain([m[3] for m in w]) - 1)}/"
                         f"{p(chain([m[4] for m in w]) - 1)}")
        for i in range(0, len(parts), 8):
            self.set_runtime_statistic(f"YR{i // 8 + 1}", " ".join(parts[i:i + 8]))

        # per-year premium, interest, fees as % of average NAV; contracts sold / assigned
        avg = {}
        for r in self.marks:
            avg.setdefault(r[0].year, []).append(r[1])
        avg = {y: sum(v) / len(v) for y, v in avg.items()}
        pr, it, fe, ns = [], [], [], []
        for y in sorted(avg):
            e = avg[y]
            pr.append(f"{y % 100:02d}:{self.prem.get(y, 0.0) / e * 100:.1f}")
            it.append(f"{y % 100:02d}:{self.interest.get(y, 0.0) / e * 100:.2f}")
            fe.append(f"{y % 100:02d}:{self.fees.get(y, 0.0) / e * 100:.2f}")
            ns.append(f"{y % 100:02d}:{self.n_sold.get(y, 0)}/{self.n_assign.get(y, 0)}")
        self.set_runtime_statistic("PREM%", " ".join(pr)[:200])
        self.set_runtime_statistic("INT%", " ".join(it)[:200])
        self.set_runtime_statistic("FEE%", " ".join(fe)[:200])
        self.set_runtime_statistic("SOLD/ASG", " ".join(ns)[:200])
        ad = " ".join(self.assign_detail)
        for i in range(2):
            if ad[i * 200:(i + 1) * 200]:
                self.set_runtime_statistic(f"ASG{i + 1}", ad[i * 200:(i + 1) * 200])

        def avgl(xs):
            return sum(xs) / len(xs) if xs else 0.0

        self.set_runtime_statistic("TOT", (
            f"prem {sum(self.prem.values()):.0f} int {sum(self.interest.values()):.0f} fees {sum(self.fees.values()):.0f} "
            f"sold {sum(self.n_sold.values())} asg {sum(self.n_assign.values())} end {self.marks[-1][1]:.0f} "
            f"qcTPV {float(self.portfolio.total_portfolio_value):.0f} addok {self.add_ok} fillmodel {FILL_CALLS[0]}/{FILL_CALLS[1]}"))
        self.set_runtime_statistic("TRD", (
            f"cycles {self.cycles} days_noopt {self.idle_cycles}/{len(self.marks)} noquote {self.no_quote} mny {avgl(self.mny) * 100:.2f}% "
            f"spr {avgl(self.sprd) * 100:.1f}% dte {avgl(self.dte):.1f} delta {avgl(self.dl):.3f} "
            f"util {avgl(self.util) * 100:.0f}% afford10k {self.afford10k[0]}/{self.afford10k[1]} split {self.split_events}"))
        if self.names:
            nm = " ".join(f"{k}:{v}" for k, v in sorted(self.names.items(), key=lambda kv: -kv[1]))
            self.set_runtime_statistic("NAMES", nm[:200])
        self.set_runtime_statistic("ERR", (" ".join(self.errs[:8]) or "none")[:200])
        for i in range(0, len(yrs), 3):
            grp = [m for m in months if m[0][0] in yrs[i:i + 3]]
            self.set_runtime_statistic(f"MS{yrs[i] % 100:02d}", " ".join(f"{m[1] * 100:.1f}" for m in grp)[:200])
