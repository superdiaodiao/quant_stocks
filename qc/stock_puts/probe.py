# region imports
from AlgorithmImports import *
# endregion
# Diagnostic only (no returns): point-in-time monthly top-10 membership (all US / Nasdaq-listed) and
# option-chain availability (expiry windows, IV, delta) per member; QQQ long-dated expiries.
from datetime import date, timedelta

class Probe(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2012, 1, 25)
        self.set_end_date(2026, 7, 9)
        self.set_cash(100000)
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = DataNormalizationMode.RAW
        self.q = self.add_equity("QQQ", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.RAW).symbol
        self.add_universe(self.select)
        self.lastm = None
        self.mem = {}
        self.prev = {"A": [], "N": []}
        self.pending = False
        self.out = []
        self.chk = []
        self.leaps = []
        self.errs = []
        self.wk = []
        self.sticky = set()
        self.names = set()
        self.maxu = 0
        self.nchg = 0

    def select(self, fundamental):
        ym = (self.time.year, self.time.month)
        if ym == self.lastm:
            return Universe.UNCHANGED
        self.lastm = ym
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
            p = cand.get(cid)
            if p is None or (f.symbol in self.sticky) or (p.symbol not in self.sticky and f.dollar_volume > p.dollar_volume):
                cand[cid] = f
        rows = sorted(cand.values(), key=lambda f: float(f.market_cap), reverse=True)
        A = [f.symbol for f in rows[:10]]
        N = [f.symbol for f in rows if f.security_reference.exchange_id == "NAS"][:10]
        line = f"{ym[0]}{ym[1]:02d}"
        for k, L in (("A", A), ("N", N)):
            old = set(s.value for s in self.prev[k]); new = [s.value for s in L]
            add = [t for t in new if t not in old]; rem = [t for t in old if t not in new]
            line += f" {k}+{','.join(add)}-{','.join(rem)}"
            self.prev[k] = L
        self.out.append(line)
        self.sticky = set(A + N)
        u = set(A + N)
        self.names |= set(x.value for x in u)
        self.maxu = max(self.maxu, len(u))
        self.nchg += line.count(",") + sum(1 for k in ("A+", "N+") if (k + "-") not in line)
        self.pending = True
        return list({s: 1 for s in A + N}.keys())

    def on_data(self, data):
        d = self.time.date()
        if self.pending and d.month in (1, 7):
            self.pending = False
            syms = list({s: 1 for s in self.prev["A"] + self.prev["N"]}.keys())
            parts = []
            for s in syms:
                try:
                    cs = list(self.option_chain(s))
                except Exception as e:
                    self.errs.append(str(e)[:40]); continue
                ex = sorted({(c.expiry.date() - d).days for c in cs})
                nw = sum(1 for x in ex if 0 < x <= 53)
                ivok = sum(1 for c in cs if c.implied_volatility and c.implied_volatility > 0)
                parts.append((s.value, nw, ivok > 0, len(cs)))
            wk = sum(1 for p in parts if p[1] >= 5)
            self.chk.append(f"{d:%y%m}:{len(parts)}/{wk}/{sum(1 for p in parts if p[2])}/{sum(1 for p in parts if p[3] == 0)}")
            if d.month == 1:
                self.wk.append(f"{d:%y}:" + ",".join(p[0] for p in parts if p[1] >= 5))
            try:
                cs = list(self.option_chain(self.q))
                ex = sorted({(c.expiry.date() - d).days for c in cs})
                self.leaps.append(f"{d:%y%m}:" + ",".join(str(x) for x in ex if x > 60))
            except Exception as e:
                self.errs.append("q" + str(e)[:40])
        elif self.pending and d.month not in (1, 7):
            self.pending = False

    def on_end_of_algorithm(self):
        for i, x in enumerate(self.out): self.log("M " + x)
        def put(prefix, items):
            t = " ".join(items)
            for i in range(0, min(len(t), 200 * 12), 200):
                self.set_runtime_statistic(f"{prefix}{i // 200}", t[i:i + 200])
        put("C", self.chk)
        put("L", self.leaps)
        put("W", self.wk)
        put("U", [f"max{self.maxu}"] + sorted(self.names))
        self.set_runtime_statistic("ERR", (" | ".join(self.errs[:5]) or "none")[:200])
        self.set_runtime_statistic("N", f"{len(self.out)} {len(self.chk)} {len(self.leaps)}")
