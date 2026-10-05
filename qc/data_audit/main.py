# region imports
from AlgorithmImports import *
# endregion
"""Data audit: independent cross-check of the frozen local data v1 against QuantConnect.

Pre-registered in docs/audit_data_v1_vs_quantconnect.md (section 1, amended design 1.8). Not a strategy
test: no orders, no signal, no cross-stock aggregate of returns.

Nothing from the local data is embedded here. QuantConnect draws its own sample from its own data and
returns QC values only (tickers, week numbers, returns in basis points, split ratios, dates); the
comparison with data v1 is done locally by scripts/audit_data_v1_qc.py.

Each run covers the years PART_YEARS. At every QC week end W (the last trading day of a calendar week):
  M_W  = top 400 by QC daily dollar volume among symbols with fundamental data, raw price >= $10 and
         security_reference.exchange_id == "NAS" (QC's current listing, not the historical one)
         and security_type == "ST00000001" (common stock).
  For s in M_W also present at the previous week end: weekly total return from adjusted_price, price
  return from price x split_factor, split ratio sf_W / sf_W-1, cash-distribution size 1 - pf_W-1 / pf_W.
  Emitted: every member-week with a split or a distribution > 10% (strata S, D), and a crc32 hash sample
  at RATE (stratum R).
  Delisting (stratum L): s in M_W-1 absent from the next five week-end snapshots; QC's last bar date and
  the return from the W-1 close to the last close come from an ADJUSTED history request.
  Mode W (a second, wide scan for the delisting check): members are the top 1,500 NAS common stocks by
  daily dollar volume at any price, no pairs are emitted, and a delisting is emitted only when the last
  week-end raw close was below $5 (distressed names such as data v1's D5 bankruptcies).
  Mode L (delisting rerun): both member sets at once, pairs not emitted, and the history request for the
  last bar uses fill_forward=False (modes A and W used the default fill-forward, which can carry a delisted
  symbol's last price forward to the request end and so postpone its "last bar"). Wide-only delistings are
  tagged ".W".
  Universe diagnostic: on DIAG dates, the top 300 NAS names by 50-session median raw dollar volume.
Token: TICKER.week36.tr[.p<price return>][.s<ratio>][.d<distribution bp>][.L<last date weekday 0-4>]
week = (W - 2011-12-26).days // 7 (Monday-based, so a short week keeps its number), base 36.
"""
from datetime import datetime, timedelta, date
import zlib

PART_YEARS = (2012, 2014)
MODE = "A"                # "A": strata R, S, D, L as above; "W": wide delisting scan only (see below);
                          # "L": delisting rerun, both member sets, no fill-forward in the history request
WIDE = 1500               # mode W: top 1,500 NAS common stocks by daily dollar volume, any price
WIDE_PRE = 5000
LOW_PRICE = 5.0           # mode W: only names whose last week-end raw close was below $5 are emitted
RATE = 50                 # per 10,000 member-weeks
SEED = "20261005"
TOP = 400
PRE = 1500
DIAG = (date(2014, 6, 27), date(2019, 6, 28), date(2024, 6, 28))
BASE = date(2011, 12, 26)
CONFIRM_WEEKS = 5


def b36(n):
    if n < 0:      # warm-up weeks before BASE (an unguarded negative n looped forever in the first runs)
        return "-" + b36(-n)
    s = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = s[r] + out
        if n == 0:
            return out


class DataAuditV1(QCAlgorithm):

    def initialize(self):
        y0, y1 = PART_YEARS
        self.lo = date(y0, 1, 1)
        self.hi = date(y1, 12, 31)
        self.set_start_date(y0 - 1, 12, 1)
        end = min(date(y1 + 1, 2, 28), date(2026, 7, 31))
        self.set_end_date(end.year, end.month, end.day)
        self.set_cash(10000)
        self.universe_settings.resolution = Resolution.DAILY
        self.add_universe(self.select)
        self.hours = self.market_hours_database.get_exchange_hours(Market.USA, None, SecurityType.EQUITY)
        self.snap_prev = None      # Symbol -> (ticker, price, adj, sf, pf) at the previous week end
        self.mem_prev = []         # M at the previous week end
        self.wide_prev = set()     # mode L: members of the previous week that are in the wide set only
        self.wide_flag = set()     # mode L: pending delistings that came from the wide set
        self.pending = []          # [Symbol, ticker, W-1 date, absent count, W-1 adj]
        self.tokens = []
        self.diag = {}
        self.n_weeks = 0
        self.n_member_weeks = 0
        self.n_pairs = 0
        self.n_s = self.n_d = self.n_l = self.n_r = 0
        self.n_gap = 0

    def select(self, fundamental):
        # Process a week end on its own day: the Fundamental objects are only read on the day they are
        # served (holding the previous day's list and reading it a day later stalled the first run).
        fl = list(fundamental)
        if not fl:
            return []
        dt = fl[0].time
        nx = self.hours.get_next_trading_day(dt)
        if nx.date().isocalendar()[:2] != dt.date().isocalendar()[:2]:
            self.week_end(dt.date(), fl)
        return []

    def week_end(self, W, fl):
        snap = {}
        cand = []
        for f in fl:
            p = f.price
            if p <= 0:
                continue
            s = f.symbol
            snap[s] = (s.value, p, f.adjusted_price, f.split_factor, f.price_factor)
            if (p >= 10 or MODE != "A") and f.has_fundamental_data:
                cand.append((f.dollar_volume, s, f))
        cand.sort(key=lambda x: -x[0])
        mem = []
        top, pre = (TOP, PRE) if MODE == "A" else (WIDE, WIDE_PRE)
        for dv, s, f in cand[:pre]:
            try:
                sr = f.security_reference
                ok = sr.exchange_id == "NAS" and sr.security_type == "ST00000001"
            except Exception:
                ok = False
            if ok:
                mem.append(s)
                if len(mem) >= top:
                    break
        wide_now = set()
        if MODE == "L":
            # the mode-A top 400 (raw price >= $10) plus the wide set (any price; the < $5 rule applies)
            narrow = [s for s in mem if snap[s][1] >= 10][:TOP]
            wide_now = set(mem) - set(narrow)
            mem = narrow + [s for s in mem if s in wide_now]
        inside = self.lo <= W <= self.hi
        wk = b36((W - BASE).days // 7)

        # delisting confirmation
        still = []
        for item in self.pending:
            sym, tick, wprev, absent, aprev = item
            if sym in snap:
                self.n_gap += 1
                continue
            absent += 1
            if absent >= CONFIRM_WEEKS:
                self.confirm_delisting(sym, tick, wprev)
            else:
                still.append([sym, tick, wprev, absent, aprev])
        self.pending = still

        if self.snap_prev is not None:
            if inside:
                self.n_weeks += 1
            for s in (mem if MODE == "A" else []):
                cur = snap[s]
                old = self.snap_prev.get(s)
                if old is None or not inside:
                    continue
                self.n_member_weeks += 1
                tick, p, adj, sf, pf = cur
                _, p0, adj0, sf0, pf0 = old
                if adj0 <= 0 or sf0 <= 0 or pf <= 0:
                    continue
                tr = (adj / adj0 - 1) * 1e4
                pr = (p * sf / (p0 * sf0) - 1) * 1e4
                ratio = sf / sf0
                dist = (1 - pf0 / pf) * 1e4
                is_s = abs(ratio - 1) > 0.001
                is_d = dist > 1000
                is_r = zlib.crc32((SEED + str(s.id) + W.isoformat()).encode()) % 10000 < RATE
                if not (is_s or is_d or is_r):
                    continue
                tok = "{}.{}.{}".format(tick, wk, int(round(tr)))
                if abs(pr - tr) >= 1:
                    tok += ".p{}".format(int(round(pr)))
                if is_s:
                    tok += ".s{:.4g}".format(ratio)
                    self.n_s += 1
                if is_d:
                    tok += ".d{}".format(int(round(dist)))
                    self.n_d += 1
                if is_r:
                    self.n_r += 1
                self.tokens.append(tok)
                self.n_pairs += 1
            # members of the previous week that are not trading now: pending delisting
            if inside:
                for s in self.mem_prev:
                    if s not in snap:
                        old = self.snap_prev[s]
                        wide = MODE == "W" or (MODE == "L" and s in self.wide_prev)
                        if wide and old[1] >= LOW_PRICE:
                            continue
                        if wide and MODE == "L":
                            self.wide_flag.add(s)
                        self.pending.append([s, old[0], self.prev_we, 1, old[2]])

        if W in DIAG and MODE == "A":
            self.universe_diag(W, cand)
        self.snap_prev = snap
        self.mem_prev = mem
        self.wide_prev = wide_now
        self.prev_we = W

    def confirm_delisting(self, sym, tick, wprev):
        try:
            h = self.history([sym], datetime.combine(wprev - timedelta(days=10), datetime.min.time()),
                             self.time, Resolution.DAILY, fill_forward=(MODE != "L"),
                             data_normalization_mode=DataNormalizationMode.ADJUSTED)
            s = h.loc[sym]["close"]
        except Exception:
            self.tokens.append("{}.{}.na.L9".format(tick, b36((wprev - BASE).days // 7 + 1)))
            self.n_l += 1
            return
        ds = [(t.date() if t.hour != 0 else (t - timedelta(days=1)).date()) for t in s.index]
        cs = [float(c) for c in s.values]
        i0 = None
        for i, x in enumerate(ds):
            if x <= wprev:
                i0 = i
        if i0 is None or not ds:
            self.tokens.append("{}.{}.na.L8".format(tick, b36((wprev - BASE).days // 7 + 1)))
            self.n_l += 1
            return
        last = ds[-1]
        tr = (cs[-1] / cs[i0] - 1) * 1e4
        self.tokens.append("{}.{}.{}.L{}".format(tick, b36((last - BASE).days // 7), int(round(tr)), last.weekday())
                           + (".W" if sym in self.wide_flag else ""))
        self.n_l += 1

    def universe_diag(self, W, cand):
        syms = [s for _, s, _ in cand[:PRE]]
        try:
            h = self.history(syms, datetime.combine(W - timedelta(days=80), datetime.min.time()),
                             datetime.combine(W + timedelta(days=1), datetime.min.time()),
                             Resolution.DAILY, data_normalization_mode=DataNormalizationMode.RAW)
        except Exception as e:
            self.diag[W] = "err " + str(e)[:150]
            return
        rows = []
        exmap = {s: f for _, s, f in cand[:PRE]}
        for s in syms:
            try:
                x = h.loc[s]
            except Exception:
                continue
            dv = sorted((x["close"] * x["volume"]).values[-50:])
            if len(dv) < 25:
                continue
            med = dv[len(dv) // 2] if len(dv) % 2 else 0.5 * (dv[len(dv) // 2 - 1] + dv[len(dv) // 2])
            try:
                sr = exmap[s].security_reference
                ok = sr.exchange_id == "NAS" and sr.security_type == "ST00000001"
            except Exception:
                ok = False
            if ok:
                rows.append((med, s.value))
        rows.sort(key=lambda r: -r[0])
        self.diag[W] = " ".join(t for _, t in rows[:300])

    def on_end_of_algorithm(self):
        st = self.set_runtime_statistic
        st("SUM", "years {}-{} weeks {} member_weeks {} tokens {} R {} S {} D {} L {} pending_left {} gaps {}".format(
            PART_YEARS[0], PART_YEARS[1], self.n_weeks, self.n_member_weeks, self.n_pairs + self.n_l,
            self.n_r, self.n_s, self.n_d, self.n_l, len(self.pending), self.n_gap))
        if self.pending:
            st("PEND", " ".join("{}.{}".format(p[1], b36((p[2] - BASE).days // 7)) for p in self.pending)[:200])
        txt = " ".join(self.tokens)
        n_keys = 0
        for c in range(0, min(len(txt), 36 * 200), 200):
            st("P%02d" % (c // 200), txt[c:c + 200])
            n_keys += 1
        st("PLEN", "{} keys {}".format(len(txt), n_keys))
        u = 0
        for W, t in sorted(self.diag.items()):
            for c in range(0, min(len(t), 8 * 200), 200):
                st("U{}{}".format(W.year % 100, c // 200), t[c:c + 200])
                u += 1
