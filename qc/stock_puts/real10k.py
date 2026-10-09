# region imports
from AlgorithmImports import *
# endregion
# Feasibility run (not a trial), docs/research_ledger_qc_stock_puts.md section 0.6:
# real 10,000 USD, whole contracts, the configurations listed in CFGS (picked after the grid results).
import math
from datetime import date, timedelta

# (universe, structure, delta, dte, mgmt, filter): fold-1 and fold-2 A picks, top full-period t, O3-like reference
CFGS = [("N5", "PO", 0.40, 45, "TP", "ALL"), ("N5", "PO", 0.25, 7, "H", "ALL"),
        ("N10", "PO", 0.10, 7, "H", "ALL"), ("A10", "CSP", 0.25, 30, "H", "ALL")]
DELTAS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40)
TDTE = (7, 14, 30, 45)
WIN = ((4, 10), (10, 18), (24, 38), (38, 52))
ROLL = {7: 3, 14: 7, 30: 14, 45: 21}
CASH0 = 10_000.0
EQC = 0.0011
F = 0.25
START = date(2012, 2, 1)
END_BOOKS = date(2026, 5, 29)


def fee(n, px):
    per = 0.65 if px >= 0.10 else (0.50 if px >= 0.05 else 0.25)
    return max(1.0, per * n)


class Book:
    def __init__(self, cfg):
        self.u, self.s, self.d, self.t, self.m, self.f = cfg
        self.cash = CASH0
        self.sh = {}      # symbol -> shares
        self.pos = {}     # symbol -> dict(n, K, eff, rt, sv, sym, mark)
        self.nav = CASH0
        self.navm = CASH0
        self.rets = []
        self.pk = CASH0
        self.dd = 0.0
        self.npos = 0
        self.zero = 0
        self.days = 0
        self.sold = 0

    def name(self):
        return f"{self.u}-{self.s}-{self.d:.2f}-{self.t}-{self.m}-{self.f}"


class Real10k(QCAlgorithm):
    def initialize(self):
        self.set_start_date(START.year, START.month, START.day)
        self.set_end_date(2026, 6, 5)
        self.set_cash(100_000)
        self.settings.daily_precise_end_time = True
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = DataNormalizationMode.RAW
        self.oneq = self.add_equity("ONEQ", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.ADJUSTED).symbol
        self.bil = self.add_equity("BIL", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.ADJUSTED).symbol
        self.vix = self.add_index("VIX", Resolution.DAILY).symbol
        self.add_universe(self.select)
        self.books = [Book(c) for c in CFGS]
        self.need = set(c[0] for c in CFGS)
        self.lastm = None
        self.sticky = set()
        self.newmem = None
        self.mem = {}
        self.opt = {}
        self.ucl = {}
        self.ivs = {}
        self.pS = {}
        self.vixs = []
        self.pdiv = {}
        self.psplit = {}
        self.bil_prev = None
        self.last_D = None
        self.last_proc = None
        self.oneq_m = None
        self.oneq_r = []
        self.errs = []
        self.done = False
        self.qt = {}

    def select(self, fundamental):
        ym = (self.time.year, self.time.month)
        if ym == self.lastm:
            return Universe.UNCHANGED
        self.lastm = ym
        cand = {}
        for f in fundamental:
            if f.price <= 0 or not f.has_fundamental_data or float(f.market_cap) <= 0:
                continue
            sr, cr = f.security_reference, f.company_reference
            if sr is None or cr is None or sr.security_type != "ST00000001" or sr.is_depositary_receipt or cr.country_id != "USA":
                continue
            cid = cr.company_id or str(f.symbol.id)
            p = cand.get(cid)
            if p is None or (f.symbol in self.sticky) or (p.symbol not in self.sticky and f.dollar_volume > p.dollar_volume):
                cand[cid] = f
        rows = sorted(cand.values(), key=lambda f: float(f.market_cap), reverse=True)
        A = [f.symbol for f in rows[:10]]
        N = [f.symbol for f in rows if f.security_reference.exchange_id == "NAS"][:10]
        self.sticky = set(A + N)
        self.newmem = {"A5": A[:5], "A10": A, "N5": N[:5], "N10": N}
        keep = []
        for u in self.need:
            keep += self.newmem[u]
        return list({s: 1 for s in keep}.keys())

    def on_securities_changed(self, changes):
        for sec in changes.added_securities:
            s = sec.symbol
            if s.security_type != SecurityType.EQUITY or s in (self.oneq, self.bil) or s in self.opt:
                continue
            o = self.add_option(s, Resolution.DAILY)
            o.set_filter(lambda u, s=s: u.include_weeklys().expiration(0, 53).contracts(lambda cs: self.keep(cs)))
            self.opt[s] = o.symbol
            try:
                h = self.history(s, 260, Resolution.DAILY, data_normalization_mode=DataNormalizationMode.SPLIT_ADJUSTED)
                today = self.time.date()
                self.ucl[s] = [float(v) for idx, v in h["close"].items() if (idx[-1] if isinstance(idx, tuple) else idx).date() < today][-252:]
            except Exception as e:
                self.ucl[s] = []
                if len(self.errs) < 5:
                    self.errs.append("hist:" + str(e)[:40])

    def on_data(self, data):
        for kv in data.dividends:
            self.pdiv[kv.key] = self.pdiv.get(kv.key, 0.0) + float(kv.value.distribution)
        for kv in data.splits:
            if kv.value.type == SplitType.SPLIT_OCCURRED:
                self.psplit[kv.key] = float(kv.value.split_factor)
        if self.time.hour < 12:
            return
        D = self.time.date()
        if D == self.last_proc or self.done:
            return
        self.last_proc = D
        b = self.securities[self.vix].get_last_data()
        if b is not None and float(b.value) > 0:
            self.vixs.append(float(b.value))
        if D > END_BOOKS:
            self.month_end()
            self.done = True
            return
        if self.last_D is not None and D.month != self.last_D.month:
            self.month_end()
        if self.newmem is not None:
            self.mem = self.newmem
            self.newmem = None
        self.step(D, data)
        self.last_D = D

    def month_end(self):
        if self.last_D is not None and self.last_D.month == 12:
            for i, bk in enumerate(self.books):
                self.set_runtime_statistic(f"P{i}", f"{self.last_D:%Y} nav {bk.nav:.0f} dd {bk.dd:.3f} avgPuts {bk.npos / max(1, bk.days):.2f} zero {bk.zero}/{bk.days} sold {bk.sold}")
        P1 = float(self.securities[self.oneq].close)
        if self.oneq_m:
            self.oneq_r.append(P1 / self.oneq_m - 1)
        self.oneq_m = P1
        for bk in self.books:
            bk.rets.append(bk.nav / bk.navm - 1)
            bk.navm = bk.nav

    def keep(self, cs):
        """Puts only: per needed window the expiry closest to target, the contract closest to each needed delta,
        plus every contract still held by a book (memory-light; replaces the first version's wide delta filter)."""
        try:
            today = self.time.date()
            held = set()
            for bk in self.books:
                for p in bk.pos.values():
                    held.add(p["sym"].value)
            need_t = sorted(set(TDTE.index(c[3]) for c in CFGS))
            need_d = sorted(set(c[2] for c in CFGS))
            exps = {}
            out = []
            for c in cs:
                if c.symbol.value in held:
                    out.append(c)
                    continue
                if c.symbol.id.option_right != OptionRight.PUT or c.greeks is None:
                    continue
                e = c.symbol.id.date.date()
                if e.weekday() not in (3, 4, 5):
                    continue
                exps.setdefault(e, []).append(c)
            for wi in need_t:
                lo, hi = WIN[wi]
                best = None
                for e in exps:
                    dte = (e - today).days
                    if lo <= dte <= hi and (best is None or (abs(dte - TDTE[wi]), dte) < best[0]):
                        best = ((abs(dte - TDTE[wi]), dte), e)
                if best is None:
                    continue
                lst = [(abs(float(c.greeks.delta)), c) for c in exps[best[1]]]
                for t in need_d:
                    x = min(lst, key=lambda y: abs(y[0] - t), default=None)
                    if x is not None and abs(x[0] - t) <= max(0.03, 0.3 * t):
                        out.append(x[1])
            return out
        except Exception as e:
            if len(self.errs) < 5:
                self.errs.append("keep:" + str(e)[:40])
            return []

    def chains(self, data, D):
        """name -> {(right, window): [(absdelta, K, sym, bid, ask, eff)]}, plus ATM IV."""
        out, iv = {}, {}
        for s, o in self.opt.items():
            ch = data.option_chains.get(o)
            if ch is None:
                continue
            exps = sorted({c.expiry.date() for c in ch if c.expiry.date().weekday() in (3, 4, 5)})
            chosen = {}
            for wi, (lo, hi) in enumerate(WIN):
                best = None
                for e in exps:
                    dte = (e - D).days
                    if lo <= dte <= hi and (best is None or (abs(dte - TDTE[wi]), dte) < best[0]):
                        best = ((abs(dte - TDTE[wi]), dte), e)
                if best:
                    chosen[best[1]] = wi
            d = {}
            ivp = []
            for c in ch:
                e = c.expiry.date()
                b, a = float(c.bid_price), float(c.ask_price)
                if e not in chosen or c.greeks is None or not (b > 0 and a >= b):
                    continue
                r = 0 if c.right == OptionRight.PUT else 1
                dl = abs(float(c.greeks.delta))
                eff = e - timedelta(days=1) if e.weekday() == 5 else e
                d.setdefault((r, chosen[e]), []).append((dl, float(c.strike), c.symbol, b, a, eff))
                if chosen[e] == 2 and abs(dl - 0.5) <= 0.15 and c.implied_volatility:
                    ivp.append((abs(dl - 0.5), float(c.implied_volatility)))
            out[s] = d
            if ivp:
                iv[s] = min(ivp)[1]
        return out, iv

    def pick(self, lst, tgt, r):
        tol = max(0.03, 0.3 * tgt)
        best = None
        for x in lst or []:
            dev = abs(x[0] - tgt)
            if dev <= tol and (best is None or (dev, x[1] if r == 0 else -x[1]) < best[0]):
                best = ((dev, x[1] if r == 0 else -x[1]), x)
        return best[1] if best else None

    def quote(self, sym):
        return self.qt.get(sym.value)

    def step(self, D, data):
        sec = self.securities
        BP = float(sec[self.bil].close)
        rb = BP / self.bil_prev - 1 if self.bil_prev and BP > 0 else 0.0
        if BP > 0:
            self.bil_prev = BP
        nd = D + timedelta(days=1)
        while nd.weekday() >= 5:
            nd += timedelta(days=1)
        ch, iv = self.chains(data, D)
        self.qt = {}
        for o in self.opt.values():
            cc = data.option_chains.get(o)
            if cc is None:
                continue
            for c in cc:
                b, a = float(c.bid_price), float(c.ask_price)
                if b > 0 and a >= b:
                    self.qt[c.symbol.value] = (b, a)
        v20 = len(self.vixs) >= 2 and self.vixs[-2] > 20
        flt = {}
        for s in self.opt:
            c = self.ucl.get(s, [])
            w = self.ivs.get(s, [])[-252:]
            flt[s] = [True, v20,
                      len(w) >= 126 and max(w) > min(w) and w[-1] >= min(w) + (max(w) - min(w)) * 2 / 3,
                      len(c) >= 200 and c[-1] > sum(c[-200:]) / 200,
                      len(c) >= 200 and c[-1] <= 0.9 * max(c[-252:])]
        px = {s: float(sec[s].close) for s in self.opt if sec.contains_key(s) and float(sec[s].close) > 0}
        newmonth = self.last_D is None or D.month != self.last_D.month
        FI = ("ALL", "V20", "IVR", "UP", "DD10")
        for bk in self.books:
            bk.cash += bk.cash * rb
            mem = self.mem.get(bk.u, [])
            for s in set(bk.pos.keys()) | set(bk.sh.keys()):   # each name once (v2 divided shares twice)
                if s in self.psplit:
                    f = self.psplit[s]
                    if s in bk.sh:
                        bk.sh[s] /= f
                    p = bk.pos.pop(s, None)
                    if p:
                        bk.cash -= p["n"] * 100 * p["mark"]
            for s, q in list(bk.sh.items()):
                bk.cash += q * self.pdiv.get(s, 0.0)
            # positions: expiry, early assignment, management
            for s, p in list(bk.pos.items()):
                S = px.get(s)
                if S is None:
                    continue
                intr = (p["K"] - S) if p["rt"] == 0 else (S - p["K"])
                q = self.quote(p["sym"])
                early = q is not None and p["rt"] == 0 and intr > 0 and q[0] <= intr
                if p["eff"] < nd or early:
                    if intr > 0:
                        if bk.s == "WH":
                            if p["rt"] == 0:
                                bk.cash -= p["n"] * 100 * p["K"]; bk.sh[s] = bk.sh.get(s, 0) + 100 * p["n"]
                            else:
                                bk.cash += p["n"] * 100 * p["K"]; bk.sh[s] = bk.sh.get(s, 0) - 100 * p["n"]
                                if bk.sh[s] <= 0:
                                    del bk.sh[s]
                        else:
                            bk.cash -= p["n"] * 100 * intr
                    del bk.pos[s]
                    continue
                if q is None:
                    p["mark"] = max(p["mark"], intr)
                    continue
                mid, spr = (q[0] + q[1]) / 2, q[1] - q[0]
                p["mark"] = mid
                buy = mid + F * spr
                close = (bk.m == "TP" and buy <= 0.5 * p["sv"]) or (bk.m == "SL" and mid >= 2 * p["sv"]) or \
                        (bk.m == "R" and (p["eff"] - D).days <= ROLL[bk.t])
                if close:
                    bk.cash -= p["n"] * 100 * buy + fee(p["n"], buy)
                    del bk.pos[s]
                    if bk.m in ("TP", "SL"):
                        p["block"] = D
                        bk.__dict__.setdefault("blk", {})[s] = D
            # monthly: sell stock of names that left (WH), rebalance PO basket
            if newmonth:
                if bk.s == "WH":
                    for s in list(bk.sh.keys()):
                        if s not in mem and s not in bk.pos and s in px:
                            bk.cash += bk.sh[s] * px[s] * (1 - EQC); del bk.sh[s]
                if bk.s == "PO" and mem:
                    navx = bk.cash + sum(q * px.get(s, 0) for s, q in bk.sh.items()) - sum(p["n"] * 100 * p["mark"] for p in bk.pos.values())
                    tgt = {s: navx / len(mem) / px[s] for s in mem if s in px}
                    for s in set(bk.sh) | set(tgt):
                        dq = tgt.get(s, 0) - bk.sh.get(s, 0)
                        if s in px:
                            bk.cash -= dq * px[s] + abs(dq) * px[s] * EQC
                            bk.sh[s] = tgt.get(s, 0)
                    bk.sh = {s: q for s, q in bk.sh.items() if q > 1e-9}
            # entries in market-cap order
            fi = FI.index(bk.f)
            di = DELTAS.index(bk.d)
            ti = TDTE.index(bk.t)
            stockval = sum(q * px.get(s, 0) for s, q in bk.sh.items())
            nav = bk.cash + stockval - sum(p["n"] * 100 * p["mark"] for p in bk.pos.values())
            for s in mem:
                if s in bk.pos or s not in px or not flt.get(s, [False] * 5)[fi] or bk.__dict__.get("blk", {}).get(s) == D:
                    continue
                r = 1 if (bk.s == "WH" and bk.sh.get(s, 0) >= 100) else 0
                x = self.pick(ch.get(s, {}).get((r, ti)), bk.d, r)
                if x is None:
                    continue
                dl, K, sym, b, a, eff = x
                p0 = (b + a) / 2 - F * (a - b)
                if p0 <= 0:
                    continue
                if r == 1:
                    n = int(bk.sh[s] // 100)
                elif bk.s == "PO":
                    notional = sum(p["n"] * 100 * p["K"] for p in bk.pos.values() if p["rt"] == 0)
                    n = 1 if notional + K * 100 <= nav else 0
                else:
                    secured = sum(p["n"] * 100 * p["K"] for p in bk.pos.values() if p["rt"] == 0)
                    n = 1 if bk.cash - secured >= K * 100 else 0
                if n <= 0:
                    continue
                bk.cash += n * 100 * p0 - fee(n, p0)
                bk.pos[s] = {"n": n, "K": K, "eff": eff, "rt": r, "sv": p0, "sym": sym, "mark": (a + b) / 2}
                bk.sold += n
            stockval = sum(q * px.get(s, 0) for s, q in bk.sh.items())
            bk.nav = bk.cash + stockval - sum(p["n"] * 100 * p["mark"] for p in bk.pos.values())
            bk.pk = max(bk.pk, bk.nav)
            bk.dd = min(bk.dd, bk.nav / bk.pk - 1)
            bk.days += 1
            bk.npos += sum(p["n"] for p in bk.pos.values() if p["rt"] == 0)
            bk.zero += 1 if not bk.pos else 0
        for s in self.opt:
            if s in iv:
                self.ivs.setdefault(s, []).append(iv[s])
                self.ivs[s] = self.ivs[s][-260:]
            if s in px:
                self.ucl.setdefault(s, []).append(px[s])
                self.ucl[s] = self.ucl[s][-260:]
        self.pdiv, self.psplit = {}, {}

    def on_end_of_algorithm(self):
        def ann(r):
            g = 1.0
            for x in r:
                g *= 1 + x
            return g ** (12 / len(r)) - 1 if r else 0.0
        # months 2012-03.. ; H1 = first 82 months
        self.set_runtime_statistic("ONEQ", f"F {ann(self.oneq_r):.4f} H1 {ann(self.oneq_r[:82]):.4f} H2 {ann(self.oneq_r[82:]):.4f} n {len(self.oneq_r)}")
        for i, bk in enumerate(self.books):
            r = bk.rets[-len(self.oneq_r):] if self.oneq_r else bk.rets
            yr = []
            self.set_runtime_statistic(f"B{i}", f"{bk.name()} F {ann(r):.4f} H1 {ann(r[:82]):.4f} H2 {ann(r[82:]):.4f} dd {bk.dd:.4f} nav {bk.nav:.0f} avgPuts {bk.npos / max(1, bk.days):.2f} zeroDays {bk.zero}/{bk.days} sold {bk.sold}")
            m = " ".join(f"{x * 100:.1f}" for x in r)
            for j in range(0, min(len(m), 1200), 200):
                self.set_runtime_statistic(f"M{i}_{j // 200}", m[j:j + 200])
        self.set_runtime_statistic("ERR", (" | ".join(self.errs) or "none")[:200])
