"""Pure-numpy position engine of the single-stock 0DTE grid (docs/research_ledger_stock_0dte.md, section 0.3).
QuantConnect project file next to main.py and simx.py (the day loop, overnight arm, breakout, export); tested
locally by tests/test_stock_0dte_engine.py. Derived from qc/zero_dte/sim.py (the frozen QQQ / SPXW engine, left
untouched); the single-stock changes are listed in ledger 0.3.
Minute m: the bar ending 09:31 is m = 0. Quotes: arrays [right (0 put, 1 call), strike, minute], forward-filled
with their age in minutes; a zero bid is a quote. A leg is (right, strike index, signed contracts).
"""
import numpy as np

from optmath import abs_delta, bs, implied_s, max_pain, shares_at  # noqa: F401

ENTRY = ((9, 45), (10, 30), (12, 0), (13, 30), (15, 0))
ON_BEFORE = 15                                 # overnight arm: enter this many minutes before the previous close
FLAT_BEFORE = 10                               # flatten this many minutes before the option close (15:50, 12:50)
DELTAS = (0.05, 0.10, 0.16, 0.25, 0.50)
WINGS = (0.0, 0.02, 0.04)                      # 0.0 = the adjacent listed strike (~1% of S), else a share of S
F_ENTRY, F_TP, F_STOP, F_FLAT = 0.25, 0.25, 0.5, 0.5
AGE_FILL, AGE_MARK = 5, 15                     # max quote age (minutes) to fill / to mark

# kind 0 defined-risk credit, 1 naked credit, 2 debit, 3 risk reversal, 4 delta-hedged straddle, 6 ratio fly
FAM = ("PCS", "CCS", "IC", "IF", "SP", "SC", "SG", "SD", "LD", "RR", "RRB", "SDH",
       "LC", "LP", "LG", "DVC", "DVP", "IFP", "IFM", "MPD", "ORB", "PRF")
KIND = (0, 0, 0, 0, 1, 1, 1, 1, 2, 3, 3, 4, 2, 2, 2, 2, 2, 0, 0, 2, 2, 6)
VERT_DEBIT = (15, 16, 19)                      # debit verticals, closed as one combo like the credit verticals
NO_ON = (10, 11, 20)                           # RRB, SDH and the breakout have no overnight version
U = 1e3                                        # export scale (dollars per share x U); stocks reach $5,000
OTHER_FEE = 0.05                               # per contract on top of the IBKR tier (exchange / regulatory)
FLAT_BID = 0.05                                # at the flatten, legs with at least max(this, 2 ticks) bid ...
FLAT_NEAR = 0.01                               # ... or a strike within this share of the stock are closed
NEAR_PIN = 0.02                                # the open-interest pin is looked for within max(2%, 1 daily sd)
MPD_MIN = 0.005                                # the max-pain vertical needs max pain at least 0.5% away
SANE_ABS = 0.25                                # a tradable spread may always be this wide (dollars per share)
OFF = 5_000_000
MECH = False
PENNY = True                                   # tick grid of the class being simulated (configure_day)


def configure(mech=False):
    global MECH
    MECH = mech


def configure_day(penny):
    """Penny classes quote in 0.01 below 3 dollars and 0.05 at or above; other classes in 0.05 / 0.10."""
    global PENNY
    PENNY = bool(penny)


def build_shapes():
    out = []
    for f in (0, 1):
        for d in DELTAS:
            for w in WINGS:
                out.append((f, d, w))
    for d in DELTAS[:-1]:
        for w in WINGS:
            out.append((2, d, w))
    for w in WINGS:
        out.append((3, 0.50, w))
    for f in (4, 5):
        for d in (0.10, 0.16, 0.25, 0.50):
            out.append((f, d, 0.0))
    for d in (0.10, 0.16, 0.25):
        out.append((6, d, 0.0))
    out += [(7, 0.50, 0.0), (8, 0.50, 0.0), (9, 0.25, 0.0), (10, 0.50, 0.0), (11, 0.50, 0.0)]
    for f in (12, 13):
        for d in (0.05, 0.10, 0.25, 0.50):
            out.append((f, d, 0.0))
    out += [(14, 0.25, 0.0), (15, 0.50, 0.0), (16, 0.50, 0.0)]
    for f in (17, 18):
        for w in WINGS:
            out.append((f, 0.50, w))
    out += [(19, 0.50, 0.0), (20, 0.50, 0.0), (21, 0.50, 0.0)]
    return out


SHAPES = build_shapes()                        # 81
NS, NE = len(SHAPES), len(ENTRY)
NB = NE + 1                                    # row blocks per name-day: the five entries, then the overnight arm


def mindex(hh, mm):
    return hh * 60 + mm - 571


def tick_v(x):
    x = np.asarray(x, float)
    lo, hi = (0.01, 0.05) if PENNY else (0.05, 0.10)
    return np.where(x < 3.0, lo, hi)


def tick0():
    return 0.01 if PENNY else 0.05


def px_sell_v(b, a, F):
    """Sell at mid - F x spread, rounded to the nearest tick (ties toward the bid), inside [bid, ask];
    0 where the bid is below one tick (nothing to sell)."""
    b, a = np.asarray(b, float), np.asarray(a, float)
    x = 0.5 * (a + b) - F * (a - b)
    t = tick_v(x)
    px = np.floor(x / t + 0.5 - 1e-9) * t
    px = np.minimum(np.maximum(px, b), a)
    return np.where(b >= tick0(), px, 0.0)


def px_buy_v(b, a, F):
    """Buy at mid + F x spread, rounded to the nearest tick (ties toward the ask), inside [bid, ask]."""
    b, a = np.asarray(b, float), np.asarray(a, float)
    x = 0.5 * (a + b) + F * (a - b)
    t = tick_v(x)
    px = np.ceil(x / t - 0.5 + 1e-9) * t
    return np.minimum(np.maximum(px, b), a)


def fee_v(px):
    px = np.asarray(px, float)
    return np.where(px >= 0.10, 0.65, np.where(px >= 0.05, 0.50, 0.25)) + OTHER_FEE


def sane(b, a):
    """Tradable quote: ask > 0, 0 <= bid <= ask, spread <= max(SANE_ABS, mid / 2). QQQ's 5-tick floor blocked real
    late-day single-stock quotes such as 0.27 / 0.50 (mechanism round 4)."""
    b, a = np.nan_to_num(np.asarray(b, float), nan=-1.0), np.nan_to_num(np.asarray(a, float), nan=0.0)
    return (a > 0) & (b >= 0) & (b <= a) & ((a - b) <= np.maximum(SANE_ABS, 0.25 * (a + b)))


class DaySim:
    """Selection, fills, management and settlement of one position; simx.DaySimX adds the day loop and export."""

    def __init__(self, loader):
        self.loader = loader
        self.cnt = {k: 0 for k in ("open", "nosel", "wingmiss", "covmiss", "stale", "nocredit", "trig", "defer",
                                   "flatdefer", "itmflat", "held", "noquote", "leftitm", "f15")}
        self.trace = []
        self.ftrace = []
        self.tsim = 0.0
        self.M = 390                           # minutes from 09:31 to the option close (inclusive)
        self.day = None

    # ------------------------------------------------------------------ selection (data at or before mS only)
    @staticmethod
    def band(sig):
        return max(0.06, 6.0 * max(sig or 0.0, 0.01))

    def select(self, S, K, B, A, MID, SPR, AGE, OI, mS, sig, kmp):
        """Short-leg candidates at minute mS: {(right, delta): strike index or "cov"}, plus the parity ATM strike,
        the open-interest pin strike and the max-pain strike (indices, -1 if none)."""
        Sm = float(S[mS])
        info = {"S": Sm, "K": K, "dlk": {}}
        okq = (AGE[:, :, mS] <= AGE_FILL) & ~np.isnan(MID[:, :, mS])
        mid = MID[:, :, mS]
        both = okq[0] & okq[1] & (np.abs(K / Sm - 1) <= 0.02)
        if both.any():
            j = np.where(both)[0]
            k0 = int(j[np.argmin(np.abs(mid[1, j] - mid[0, j]))])
            s_impl = float(K[k0] + mid[1, k0] - mid[0, k0])
        else:
            k0 = int(np.argmin(np.abs(K - Sm)))
            s_impl = Sm
        info["k0"] = k0
        info["s_impl"] = s_impl
        if okq[0, k0] and okq[1, k0]:
            info["strad"] = float(mid[0, k0] + mid[1, k0])
        band = self.band(sig)
        # the strikes this entry may use: decided from s_impl at mS, never from later prices
        inb = np.abs(K / s_impl - 1) <= band + max(WINGS) + 0.005
        info["inb"] = inb
        # open-interest pin: the largest put + call open interest within max(2%, 1 sd) (ties: nearest the stock)
        tot = np.nan_to_num(OI[0]) + np.nan_to_num(OI[1])
        near = np.abs(K / s_impl - 1) <= max(NEAR_PIN, sig or 0.0)
        kpin = -1
        if near.any() and tot[near].max() > 0:
            j = np.where(near & (tot == tot[near].max()))[0]
            kpin = int(j[np.argmin(np.abs(K[j] - s_impl))])
        info["kpin"] = kpin
        kmi = -1
        if kmp is not None and kmp > 0 and abs(kmp / s_impl - 1) <= band:
            j = np.where(inb)[0]
            if len(j):
                kmi = int(j[np.argmin(np.abs(K[j] - kmp))])
        info["kmpi"] = kmi
        tk = tick0()
        sel = {}
        for r in (0, 1):
            put = r == 0
            otm = (K < s_impl) if put else (K > s_impl)
            elig = otm & okq[r] & (np.nan_to_num(B[r, :, mS]) >= tk) & (np.abs(K / s_impl - 1) <= band)
            elig &= np.nan_to_num(SPR[r, :, mS], nan=1e9) <= np.maximum(5 * tk, 0.5 * np.nan_to_num(mid[r]))
            cand = np.where(elig)[0]
            dl = np.full(len(cand), np.nan)
            if len(cand):
                sv = implied_s(s_impl, K[cand], mid[r, cand], put)
                good = ~np.isnan(sv)
                dl[good] = abs_delta(s_impl, K[cand][good], sv[good], put)
            has = len(cand) > 0 and np.any(~np.isnan(dl))
            side = np.where(inb & otm)[0]
            edge = int(side.min() if put else side.max()) if len(side) else -1
            for d in DELTAS[:-1]:
                if not has:
                    continue
                tol = max(0.03, 0.3 * d)
                dev = np.abs(dl - d)
                m = np.nanmin(dev)
                if m > tol:
                    # coverage miss: the furthest candidate is the outermost usable strike and is still too rich
                    if np.nanmin(dl) > d + tol and int(cand[np.nanargmin(dl)]) == edge:
                        sel[(r, d)] = "cov"
                    continue
                tie = cand[dev <= m + 1e-12]
                ks = int(tie.min() if put else tie.max())          # ties go to the further OTM strike
                sel[(r, d)] = ks
                info["dlk"][(r, ks)] = float(dl[list(cand).index(ks)])
            if okq[r, k0] and np.nan_to_num(B[r, k0, mS]) >= tk:
                sel[(r, 0.50)] = k0
                info["dlk"].setdefault((r, k0), 0.5)
        return sel, info

    def wing(self, r, ks, w, K, A, AGE, mS, s_impl, inb):
        nk = len(K)
        if w == 0.0:
            j = ks - 1 if r == 0 else ks + 1
            if j < 0 or j >= nk or not inb[j]:
                return None, 3
        else:
            tgt = K[ks] - w * s_impl if r == 0 else K[ks] + w * s_impl
            idx = np.arange(nk)
            beyond = idx[((K < K[ks]) if r == 0 else (K > K[ks])) & inb]
            if len(beyond) == 0:
                return None, 3
            j = int(beyond[np.argmin(np.abs(K[beyond] - tgt))])
            if abs(K[j] - tgt) > 0.5 * w * s_impl:
                return None, 2
        if not (AGE[r, j, mS] <= AGE_FILL and A[r, j, mS] > 0):
            return None, 2
        return j, 0

    @staticmethod
    def buyable(r, k, A, AGE, mS):
        return AGE[r, k, mS] <= AGE_FILL and np.nan_to_num(A[r, k, mS]) > 0

    @staticmethod
    def sellable(r, k, B, AGE, mS):
        return AGE[r, k, mS] <= AGE_FILL and np.nan_to_num(B[r, k, mS]) >= tick0()

    def legs_for(self, shape, sel, K, B, A, AGE, mS, info):
        f, d, w = shape
        s_impl, inb = info["s_impl"], info["inb"]

        def short(r, dd):
            v = sel.get((r, dd))
            if v == "cov":
                return None, 3
            if v is None:
                return None, 1
            return v, 0

        if f in (0, 1):
            r = 0 if f == 0 else 1
            ks, c = short(r, d)
            if ks is None:
                return None, c
            kl, c = self.wing(r, ks, w, K, A, AGE, mS, s_impl, inb)
            return (None, c) if kl is None else ([(r, ks, -1), (r, kl, 1)], 0)
        if f in (2, 3):
            kp, c1 = short(0, d)
            kc, c2 = short(1, d)
            if kp is None or kc is None:
                return None, (c1 or c2)
            lp, c1 = self.wing(0, kp, w, K, A, AGE, mS, s_impl, inb)
            lc, c2 = self.wing(1, kc, w, K, A, AGE, mS, s_impl, inb)
            if lp is None or lc is None:
                return None, (c1 or c2)
            return [(0, kp, -1), (0, lp, 1), (1, kc, -1), (1, lc, 1)], 0
        if f in (4, 5):
            r = 0 if f == 4 else 1
            ks, c = short(r, d)
            return (None, c) if ks is None else ([(r, ks, -1)], 0)
        if f in (6, 7, 11):
            kp, c1 = short(0, d)
            kc, c2 = short(1, d)
            if kp is None or kc is None:
                return None, (c1 or c2)
            return [(0, kp, -1), (1, kc, -1)], 0
        if f == 8:
            k0 = info["k0"]
            if not (self.buyable(0, k0, A, AGE, mS) and self.buyable(1, k0, A, AGE, mS)):
                return None, 4
            return [(0, k0, 1), (1, k0, 1)], 0
        if f in (9, 10):
            # risk reversals: long call d, short put d
            kp, c1 = short(0, d)
            kc = sel.get((1, d))
            if kp is None or not isinstance(kc, int):
                return None, (c1 or 1)
            if not self.buyable(1, kc, A, AGE, mS):
                return None, 4
            return [(1, kc, 1), (0, kp, -1)], 0
        if f in (12, 13):
            # long call / long put at the target delta (the same strike choice as a short leg of that delta)
            r = 1 if f == 12 else 0
            k, c = short(r, d)
            if k is None:
                return None, c
            if not self.buyable(r, k, A, AGE, mS):
                return None, 4
            return [(r, k, 1)], 0
        if f == 14:
            kp, c1 = short(0, d)
            kc, c2 = short(1, d)
            if kp is None or kc is None:
                return None, (c1 or c2)
            if not (self.buyable(0, kp, A, AGE, mS) and self.buyable(1, kc, A, AGE, mS)):
                return None, 4
            return [(0, kp, 1), (1, kc, 1)], 0
        if f in (15, 16):
            # debit vertical: long the at-the-money option, short the 0.25-delta one; legs in (short, long) order
            r = 1 if f == 15 else 0
            kl = sel.get((r, 0.50))
            ks, c = short(r, 0.25)
            if ks is None:
                return None, c
            if not isinstance(kl, int) or kl == ks:
                return None, 1
            if not self.buyable(r, kl, A, AGE, mS):
                return None, 4
            return [(r, ks, -1), (r, kl, 1)], 0
        if f in (17, 18):
            # iron fly with its body at the open-interest pin (17) or at max pain (18)
            kb = info["kpin"] if f == 17 else info["kmpi"]
            if kb < 0:
                return None, 1
            if not (self.sellable(0, kb, B, AGE, mS) and self.sellable(1, kb, B, AGE, mS)):
                return None, 4
            lp, c1 = self.wing(0, kb, w, K, A, AGE, mS, s_impl, inb)
            lc, c2 = self.wing(1, kb, w, K, A, AGE, mS, s_impl, inb)
            if lp is None or lc is None:
                return None, (c1 or c2)
            return [(0, kb, -1), (0, lp, 1), (1, kb, -1), (1, lc, 1)], 0
        if f == 19:
            # debit vertical from the at-the-money strike towards max pain (calls if max pain is above the stock)
            kb, k0 = info["kmpi"], info["k0"]
            if kb < 0 or kb == k0 or abs(K[kb] / s_impl - 1) < MPD_MIN:
                return None, 1
            r = 1 if K[kb] > s_impl else 0
            if (r == 1 and K[kb] <= K[k0]) or (r == 0 and K[kb] >= K[k0]):
                return None, 1
            if not (self.buyable(r, k0, A, AGE, mS) and self.sellable(r, kb, B, AGE, mS)):
                return None, 4
            return [(r, kb, -1), (r, k0, 1)], 0
        if f == 21:
            # pin ratio fly (Augen): +1 call one strike below the nearest strike, -3 at it, +2 one strike above;
            # skipped when the stock sits within the middle half between two strikes
            kn = int(np.argmin(np.abs(K - s_impl)))
            if kn < 1 or kn + 1 >= len(K) or not (inb[kn - 1] and inb[kn + 1]):
                return None, 3
            if abs(s_impl - K[kn]) > 0.25 * min(K[kn + 1] - K[kn], K[kn] - K[kn - 1]):
                return None, 1
            if not (self.buyable(1, kn - 1, A, AGE, mS) and self.buyable(1, kn + 1, A, AGE, mS)
                    and self.sellable(1, kn, B, AGE, mS)):
                return None, 4
            return [(1, kn - 1, 1), (1, kn, -3), (1, kn + 1, 2)], 0
        return None, 9                                         # the breakout (20) has its own entry rule (simx)

    # ------------------------------------------------------------------ fills
    @staticmethod
    def px_sell(b, a, F):
        return float(px_sell_v(b, a, F))

    @staticmethod
    def px_buy(b, a, F):
        return float(px_buy_v(b, a, F))

    def close_cash(self, legs, B, A, AGE, m, F, age_max, K=None):
        """Close every leg at minute m: cash per share, fees, improvement over the touch, contracts traded, and the
        longs that could not be sold (held to the close at intrinsic). None if a short cannot be bought back. With K,
        (short, long) pairs close as one combo whose net cost is capped at the vertical's width."""
        fees, imp, n, held = 0.0, 0.0, 0, []
        tk = tick0()
        part = []
        for r, k, qn in legs:
            b, a, ag = B[r, k, m], A[r, k, m], AGE[r, k, m]
            q = abs(qn)
            if qn < 0:
                if not (ag <= age_max and sane(b, a)):
                    return None
                px = self.px_buy(b, a, F)
                part.append(-px * q)
                fees += float(fee_v(px)) * q / 100.0
                imp += (a - px) * q
                n += q
            elif ag <= age_max and b >= tk and sane(b, a):
                px = self.px_sell(b, a, F)
                part.append(px * q)
                fees += float(fee_v(px)) * q / 100.0
                imp += (px - b) * q
                n += q
            else:
                part.append(None)
                held.append((r, k, qn))        # never given away: held to the close and settled at intrinsic
        cash = sum(x for x in part if x is not None)
        if K is not None:
            ws = []
            for j in range(0, len(legs) - 1, 2):
                cs, cl = part[j], part[j + 1]
                w = abs(K[legs[j][1]] - K[legs[j + 1][1]])
                ws.append(w)
                if cs is not None and cl is not None and cs + cl < -w:
                    cash += -w - (cs + cl)
            # an iron condor / fly closed in one order: its two verticals cannot both finish in the money
            if len(ws) == 2 and not held and cash < -max(ws):
                cash = -max(ws)
        return cash, fees, imp, n, held

    @staticmethod
    def settle_value(legs, K, Sx):
        v = 0.0
        for r, k, qn in legs:
            intr = max(K[k] - Sx, 0.0) if r == 0 else max(Sx - K[k], 0.0)
            v += qn * intr
        return v

    def flatten(self, legs, K, B, A, AGE, S, mflat, S_close, pairs=False):
        """Close the legs that still matter (bid >= max(FLAT_BID, 2 ticks) or a strike within FLAT_NEAR of S at
        mflat) at F_FLAT from mflat, retrying until the bar before the close; the others are left to expire. What is
        left or never fills settles at intrinsic at the close (and is returned, for the assignment convention). With
        pairs, a unit whose legs all matter closes as one combo; a far wing that does not matter never blocks it."""
        cash, fees, n = 0.0, 0.0, 0
        rest = []
        M = self.M
        Sm = float(S[mflat])
        bmin = max(FLAT_BID, 2 * tick0())

        def matters(leg):
            r, k, _ = leg
            return (np.nan_to_num(B[r, k, mflat]) >= bmin) or abs(K[k] / Sm - 1) <= FLAT_NEAR

        units = [legs[j:j + 2] for j in range(0, len(legs), 2)] if pairs else [[leg] for leg in legs]
        if pairs and len(units) == 2 and all(matters(x) for x in legs):
            units = [legs]                            # a condor / fly that matters everywhere: one order, capped together
        for unit in units:
            live = [x for x in unit if matters(x)]
            rest.extend(x for x in unit if not matters(x))
            if not live:
                continue
            combo = pairs and len(live) == len(unit)
            done = False
            for m in range(mflat, M - 1):
                res = self.close_cash(live, B, A, AGE, m, F_FLAT, AGE_FILL, K if combo else None)
                if res is None or any(np.nan_to_num(B[r, k, m]) >= tick0() and matters((r, k, q)) for r, k, q in res[4]):
                    continue                          # retry: a short cannot be bought, or a long that matters cannot be sold
                c, fe, _, nn, held = res
                cash += c
                fees += fe
                n += nn
                rest.extend(held)                     # longs that cannot be sold: held to the close
                done = True
                if m > mflat:
                    self.cnt["flatdefer"] += 1
                break
            if not done:
                rest.extend(live)
                self.cnt["itmflat"] += 1
        cash += self.settle_value(rest, K, S_close)
        return cash, fees, n, rest

    def exit_value(self, legs, B, A, AGE, F, K=None):
        """Per minute: cash per share from closing everything at F, net of fees; nan where a short cannot be bought
        back, 0 for a long that cannot be sold. With K, each vertical's cost is capped at its width."""
        tk = tick0()
        if K is not None:
            V = np.zeros(self.M)
            fmax = len(legs) * (0.65 + OTHER_FEE) / 100.0
            ws = []
            for j in range(0, len(legs), 2):
                vj = self.exit_value(legs[j:j + 2], B, A, AGE, F)
                w = abs(K[legs[j][1]] - K[legs[j + 1][1]])
                ws.append(w)
                V += np.where(np.isnan(vj), np.nan, np.maximum(vj, -w - 2 * (0.65 + OTHER_FEE) / 100.0))
            return np.where(np.isnan(V), np.nan, np.maximum(V, -max(ws) - fmax))
        V = np.zeros(self.M)
        for r, k, qn in legs:
            b, a, ag = np.nan_to_num(B[r, k]), np.nan_to_num(A[r, k]), AGE[r, k]
            ok = sane(b, a)
            q = abs(qn)
            if qn < 0:
                px = px_buy_v(b, a, F)
                V += np.where((ag <= AGE_FILL) & ok, (-px - fee_v(px) / 100.0) * q, np.nan)
            else:
                px = px_sell_v(b, a, F)
                V += np.where((b >= tk) & ok & (ag <= AGE_FILL), (px - fee_v(px) / 100.0) * q, 0.0)
        return V

    # ------------------------------------------------------------------ one position
    def entry_fill(self, legs, B, A, AGE, mF):
        """(cash per share, fees, improvement over the touch, summed spread) of opening the legs at mF, or None."""
        cash0, fee0, imp0, spr0 = 0.0, 0.0, 0.0, 0.0
        tk = tick0()
        for r, k, qn in legs:
            b, a, ag = B[r, k, mF], A[r, k, mF], AGE[r, k, mF]
            q = abs(qn)
            spr0 += a - b
            if not (ag <= AGE_FILL and sane(b, a)):
                return None
            if qn < 0:
                if not (b >= tk):
                    return None
                px = self.px_sell(b, a, F_ENTRY)
                cash0 += px * q
                imp0 += (px - b) * q
            else:
                px = self.px_buy(b, a, F_ENTRY)
                cash0 -= px * q
                imp0 += (a - px) * q
            fee0 += float(fee_v(px)) * q / 100.0
        return cash0, fee0, imp0, spr0

    @staticmethod
    def risk_base(f, legs, K, net, S0):
        """Unit denominator (ledger 0.5) or None when the position is not worth opening (net credit < 1 tick)."""
        kind = KIND[f]
        tk = tick0()
        if kind == 0:
            width = max(abs(K[legs[j][1]] - K[legs[j + 1][1]]) for j in range(0, len(legs), 2))
            den = width - net
            return den if (net >= tk and den > 0) else None
        if kind == 6:
            step = float(K[legs[2][1]] - K[legs[1][1]])           # 1:3:2 call fly: worst case loses one strike step
            return max(step - net, 0.25 * step) if step > 0 else None
        if kind == 1:
            return S0 if net >= tk else None
        if kind == 2:
            return -net if net < 0 else None
        return S0

    def manage(self, si, shape, legs, K, B, A, MID, AGE, S, m0, mflat, S_close, info, cash0, fee0, imp0, spr0, S0,
               den):
        """Marks, stops / take-profits from m0 + 1, the 15:50 flatten, the hold-to-close value, the share delivery
        and the FLAT15 sensitivity of one opened position; returns its export row (simx.pack)."""
        f = shape[0]
        kind = KIND[f]
        tk = tick0()
        net = cash0 - fee0
        V = np.zeros(self.M)
        vok = np.ones(self.M, bool)
        for r, k, qn in legs:
            V += qn * np.nan_to_num(MID[r, k], nan=0.0)
            vok &= (AGE[r, k] <= AGE_MARK) | (np.nan_to_num(B[r, k]) < tk)
        pairs = kind == 0 or f in VERT_DEBIT
        fcash, ffee, fn, rest = self.flatten(legs, K, B, A, AGE, S, mflat, S_close, pairs)
        pnl_flat = net + fcash - ffee
        pnl_hold = net + self.settle_value(legs, K, S_close)
        nsh_hold = shares_at(legs, K, S_close)
        nsh_flat = shares_at(rest, K, S_close)
        if nsh_flat:
            self.cnt["leftitm"] += 1
        # FLAT15: a position that would deliver shares at the 15:00 price is closed then (IBKR may liquidate an
        # account that cannot take delivery)
        d15 = 0.0
        m15 = mindex(15, 0)
        if m0 < m15 < mflat and shares_at(legs, K, float(S[m15])) != 0:
            c15, f15, _, _ = self.flatten(legs, K, B, A, AGE, S, m15, S_close, pairs)
            d15 = (net + c15 - f15) - pnl_flat
            self.cnt["f15"] += 1
        pnl = [pnl_flat, pnl_flat, pnl_flat, pnl_flat]
        xmin, ximp, nx = [0, 0, 0], [0.0, 0.0, 0.0], [0, 0, 0]
        rng = np.zeros(self.M, bool)
        if mflat - 2 >= m0 + 1:
            rng[m0 + 1:mflat - 1] = True
        rng &= vok
        if kind in (0, 1):
            b = max(net, tk)                                   # the credit actually kept after entry fees
            Vx = self.exit_value(legs, B, A, AGE, F_TP, K if pairs else None)
            trig = [rng & (-V >= 2 * b), rng & (-V >= 3 * b), rng & (np.nan_to_num(-Vx, nan=1e9) <= 0.75 * b)]
            fills = (F_STOP, F_STOP, F_TP)
        elif kind == 2:
            b = max(-net, tk)                                  # the debit actually paid including entry fees
            Vx = self.exit_value(legs, B, A, AGE, F_TP, K if pairs else None)
            trig = [rng & (V <= 0.5 * b), rng & (np.nan_to_num(Vx, nan=-1e9) >= 2 * b),
                    rng & (np.nan_to_num(Vx, nan=-1e9) >= 3 * b)]
            fills = (F_STOP, F_TP, F_TP)
        elif kind == 6:
            Vx = self.exit_value(legs, B, A, AGE, F_TP)
            gain = net + np.nan_to_num(Vx, nan=-1e9)
            trig = [rng & (net + V <= -0.5 * den), rng & (gain >= 0.5 * den), rng & (gain >= 0.3 * den)]
            fills = (F_STOP, F_TP, F_TP)
        else:
            pm = net + V
            trig = [rng & (pm <= -0.005 * S0), rng & (pm <= -0.010 * S0), rng & (pm >= 0.010 * S0)]
            fills = (F_STOP, F_STOP, F_TP)
        for j in range(3):
            t = trig[j]
            if not t.any():
                continue
            self.cnt["trig"] += 1
            mt = int(np.argmax(t))
            for m in range(mt + 1, mflat):
                res = self.close_cash(legs, B, A, AGE, m, fills[j], AGE_FILL, K if pairs else None)
                if res is None:
                    continue
                c, fe, im, nn, held = res
                if held:
                    self.cnt["held"] += 1
                pnl[j + 1] = net + c - fe + self.settle_value(held, K, S_close)
                xmin[j] = m + 1
                ximp[j] = im
                nx[j] = nn
                if m > mt + 1:
                    self.cnt["defer"] += 1
                break
        if MECH and nsh_flat and len(self.ftrace) < 15:
            q = lambda r, k, m: f"{B[r, k, m]:.2f}/{A[r, k, m]:.2f}a{min(99, int(AGE[r, k, m]))}"
            self.ftrace.append(f"{self.day:%m%d} s{si} S{S[mflat]:.2f}>{S_close:.2f} " + " ".join(
                f"{'PC'[r]}{K[k]:g}{qn:+d}:{q(r, k, mflat)}>{q(r, k, self.M - 2)}" for r, k, qn in legs))
        return self.pack(cash0, den, pnl, imp0, pnl_hold - pnl_flat, xmin, ximp, legs, fn, nx, info, shape, fee0, ffee,
                         spr0, nsh_hold, nsh_flat, d15)
