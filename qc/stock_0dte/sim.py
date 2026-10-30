"""Pure-numpy day simulator for the single-stock 0DTE grid (docs/research_ledger_stock_0dte.md, section 0.3).
QuantConnect project file next to main.py, tested locally by tests/test_stock_0dte_engine.py. Derived from
qc/zero_dte/sim.py (the frozen QQQ / SPXW engine, untouched); the single-stock changes are listed in ledger 0.3:
per-class tick grid, a band from the stock's own volatility, 2% / 4% wings, buyer and open-interest structures,
a 1% flatten window, and the shares a position would deliver if held (next-open assignment convention).
Minute m: the bar ending 09:31 is m = 0. Quotes: arrays [right (0 put, 1 call), strike, minute], forward-filled
with their age in minutes; a zero bid is a quote.
"""
import math
import time as _time

import numpy as np
from scipy.special import ndtr

from optmath import abs_delta, bs, implied_s, max_pain, shares_at  # noqa: F401

ENTRY = ((9, 45), (10, 30), (12, 0), (13, 30), (15, 0))
FLAT_BEFORE = 10                               # flatten this many minutes before the option close (15:50, 12:50)
DELTAS = (0.05, 0.10, 0.16, 0.25, 0.50)
WINGS = (0.0, 0.02, 0.04)                      # 0.0 = the adjacent listed strike (~1% of S), else a share of S
F_ENTRY, F_TP, F_STOP, F_FLAT = 0.25, 0.25, 0.5, 0.5
AGE_FILL, AGE_MARK = 5, 15                     # max quote age (minutes) to fill / to mark

# families: kind 0 defined-risk credit, 1 naked credit, 2 debit, 3 risk reversal, 4 delta-hedged straddle
FAM = ("PCS", "CCS", "IC", "IF", "SP", "SC", "SG", "SD", "LD", "RR", "RRB", "SDH",
       "LC", "LP", "LG", "DVC", "DVP", "IFP", "IFM", "MPD")
KIND = (0, 0, 0, 0, 1, 1, 1, 1, 2, 3, 3, 4, 2, 2, 2, 2, 2, 0, 0, 2)
VERT_DEBIT = (15, 16, 19)                      # debit verticals, closed as one combo like the credit verticals
U = 1e3                                        # export scale (dollars per share x U); stocks reach 5,000 dollars
OTHER_FEE = 0.05                               # per contract on top of the IBKR tier (exchange / regulatory)
FLAT_BID = 0.05                                # at the flatten, legs with at least this bid ...
FLAT_NEAR = 0.01                               # ... or a strike within this share of the stock are closed
NEAR_PIN = 0.02                                # the open-interest pin is looked for within max(2%, 1 daily sd)
MPD_MIN = 0.005                                # the max-pain vertical needs max pain at least 0.5% away
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
    out.append((7, 0.50, 0.0))
    out.append((8, 0.50, 0.0))
    out.append((9, 0.25, 0.0))
    out.append((10, 0.50, 0.0))
    out.append((11, 0.50, 0.0))
    for f in (12, 13):
        for d in (0.05, 0.10, 0.25, 0.50):
            out.append((f, d, 0.0))
    out.append((14, 0.25, 0.0))
    out.append((15, 0.50, 0.0))
    out.append((16, 0.50, 0.0))
    for f in (17, 18):
        for w in WINGS:
            out.append((f, 0.50, w))
    out.append((19, 0.50, 0.0))
    return out


SHAPES = build_shapes()                        # 79
NS, NE = len(SHAPES), len(ENTRY)


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
    """A quote we are willing to trade on: ask > 0, 0 <= bid <= ask, spread <= max(5 ticks, half the mid)."""
    b, a = np.nan_to_num(np.asarray(b, float), nan=-1.0), np.nan_to_num(np.asarray(a, float), nan=0.0)
    return (a > 0) & (b >= 0) & (b <= a) & ((a - b) <= np.maximum(5 * tick0(), 0.25 * (a + b)))


class DaySim:
    """One instance per backtest. simulate_day() turns one name-day's minute quotes into export rows."""

    def __init__(self, loader):
        self.loader = loader                   # (D, contracts, S, sig) -> (K, B, A, AGE, OI, n_loaded) or None
        self.cnt = {k: 0 for k in ("open", "nosel", "wingmiss", "covmiss", "stale", "nocredit", "trig", "defer",
                                   "flatdefer", "itmflat", "held", "noquote", "leftitm")}
        self.trace = []
        self.ftrace = []
        self.tsim = 0.0
        self.M = 390                           # minutes from 09:31 to the option close (inclusive)
        self.day = None

    def simulate_day(self, D, S, contracts, sig, kmp):
        """Rows for every (entry, shape), the at-the-money straddle mid and the open-interest pin strike at each
        entry, and the number of contracts loaded. sig = trailing daily sd of the stock; kmp = max-pain strike."""
        t1 = _time.time()
        q = self.loader(D, contracts, S, sig)
        if q is None:
            self.cnt["noquote"] += 1
            return None
        K, B, A, AGE, OI, nload = q
        MID = 0.5 * (B + A)
        SPR = A - B
        M = self.M
        mflat = M - 1 - FLAT_BEFORE            # 15:50 (12:50 on a 13:00 close)
        S_close = float(S[M - 1])
        rows, strad, pins = [], [0.0] * NE, [0.0] * NE
        for ei, (hh, mm) in enumerate(ENTRY):
            mS = mindex(hh, mm)
            mF = mS + 1
            if mF >= mflat - 2:
                rows.extend([[-9.0]] * NS)
                continue
            sel, info = self.select(S, K, B, A, MID, SPR, AGE, OI, mS, sig, kmp)
            strad[ei] = info.get("strad", 0.0)
            pins[ei] = float(K[info["kpin"]]) if info["kpin"] >= 0 else 0.0
            for si, shape in enumerate(SHAPES):
                legs, code = self.legs_for(shape, sel, K, B, A, AGE, mS, info)
                if legs is None:
                    self.cnt[{2: "wingmiss", 3: "covmiss", 4: "stale"}.get(code, "nosel")] += 1
                    rows.append([-float(code)])
                    continue
                r = self.run_position(si, shape, legs, K, B, A, MID, AGE, S, mF, mflat, S_close, info)
                if r is None:
                    rows.append([-5.0])
                    continue
                rows.append(r)
                self.cnt["open"] += 1
        self.tsim += _time.time() - t1
        return rows, strad, pins, nload

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
        # 19: debit vertical from the at-the-money strike towards max pain (calls if max pain is above the stock)
        kb, k0 = info["kmpi"], info["k0"]
        if kb < 0 or kb == k0 or abs(K[kb] / s_impl - 1) < MPD_MIN:
            return None, 1
        r = 1 if K[kb] > s_impl else 0
        if (r == 1 and K[kb] <= K[k0]) or (r == 0 and K[kb] >= K[k0]):
            return None, 1
        if not (self.buyable(r, k0, A, AGE, mS) and self.sellable(r, kb, B, AGE, mS)):
            return None, 4
        return [(r, kb, -1), (r, k0, 1)], 0

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
        cash, fees, imp, n, held = 0.0, 0.0, 0.0, 0, []
        tk = tick0()
        part = []
        for r, k, qn in legs:
            b, a, ag = B[r, k, m], A[r, k, m], AGE[r, k, m]
            if qn < 0:
                if not (ag <= age_max and sane(b, a)):
                    return None
                px = self.px_buy(b, a, F)
                part.append(-px)
                fees += float(fee_v(px)) / 100.0
                imp += a - px
                n += 1
            elif ag <= age_max and b >= tk and sane(b, a):
                px = self.px_sell(b, a, F)
                part.append(px)
                fees += float(fee_v(px)) / 100.0
                imp += px - b
                n += 1
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
        """Close the legs that still matter (bid >= FLAT_BID or a strike within FLAT_NEAR of S at mflat) at F_FLAT
        from mflat, retrying until the bar before the close; the others are left to expire. Whatever is left or never
        fills settles at intrinsic at the close (and is returned, for the assignment convention). With pairs, a unit
        whose legs all matter is closed as one combo (see close_cash); a far wing that does not matter never blocks
        the close of the legs that do."""
        cash, fees, n = 0.0, 0.0, 0
        rest = []
        M = self.M
        Sm = float(S[mflat])

        def matters(leg):
            r, k, _ = leg
            return (np.nan_to_num(B[r, k, mflat]) >= FLAT_BID) or abs(K[k] / Sm - 1) <= FLAT_NEAR

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
                if MECH and len(self.ftrace) < 20:
                    q = lambda r, k, m: f"{B[r, k, m]:.2f}/{A[r, k, m]:.2f}a{min(99, int(AGE[r, k, m]))}"
                    self.ftrace.append(f"{self.day:%m%d} S{Sm:.2f} " + " ".join(
                        f"{'PC'[r]}{K[k]:g}{'+' if qn > 0 else '-'}:{q(r, k, mflat)}>{q(r, k, M - 2)}" for r, k, qn in live))
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
            if qn < 0:
                px = px_buy_v(b, a, F)
                v = -px - fee_v(px) / 100.0
                V += np.where((ag <= AGE_FILL) & ok, v, np.nan)
            else:
                px = px_sell_v(b, a, F)
                v = np.where((b >= tk) & ok & (ag <= AGE_FILL), px - fee_v(px) / 100.0, 0.0)
                V += v
        return V

    # ------------------------------------------------------------------ one position
    def run_position(self, si, shape, legs, K, B, A, MID, AGE, S, mF, mflat, S_close, info):
        f = shape[0]
        kind = KIND[f]
        tk = tick0()
        # entry fill at mF (one minute after selection)
        cash0, fee0, imp0, spr0 = 0.0, 0.0, 0.0, 0.0
        for r, k, qn in legs:
            b, a, ag = B[r, k, mF], A[r, k, mF], AGE[r, k, mF]
            spr0 += a - b
            if not (ag <= AGE_FILL and sane(b, a)):
                self.cnt["stale"] += 1
                return None
            if qn < 0:
                if not (b >= tk):
                    self.cnt["stale"] += 1
                    return None
                px = self.px_sell(b, a, F_ENTRY)
                cash0 += px
                imp0 += px - b
            else:
                px = self.px_buy(b, a, F_ENTRY)
                cash0 -= px
                imp0 += a - px
            fee0 += float(fee_v(px)) / 100.0
        S0 = float(S[mF])
        net = cash0 - fee0
        if kind == 0:
            width = max(abs(K[legs[j][1]] - K[legs[j + 1][1]]) for j in range(0, len(legs), 2))
            den = width - net
            if net < tk or den <= 0:
                self.cnt["nocredit"] += 1
                return None
        elif kind == 1:
            if net < tk:
                self.cnt["nocredit"] += 1
                return None
            den = S0
        elif kind == 2:
            den = -net
            if den <= 0:
                self.cnt["nocredit"] += 1
                return None
        else:
            den = S0
        if kind == 4:
            return self.run_sdh(legs, K, B, A, MID, AGE, S, mF, mflat, S_close, cash0, fee0, imp0, spr0, den, info, shape)
        # marks: value to us of the legs at mid; a leg that still shows a bid must have a recent quote
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
        pnl = [pnl_flat, pnl_flat, pnl_flat, pnl_flat]
        xmin, ximp, nx = [0, 0, 0], [0.0, 0.0, 0.0], [0, 0, 0]
        rng = np.zeros(self.M, bool)
        if mflat - 2 >= mF + 1:
            rng[mF + 1:mflat - 1] = True
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
        else:
            pm = net + V
            trig = [rng & (pm <= -0.005 * S0), rng & (pm <= -0.010 * S0), rng & (pm >= 0.010 * S0)]
            fills = (F_STOP, F_STOP, F_TP)
        for j in range(3):
            t = trig[j]
            if not t.any():
                continue
            self.cnt["trig"] += 1
            m0 = int(np.argmax(t))
            for m in range(m0 + 1, mflat):
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
                if m > m0 + 1:
                    self.cnt["defer"] += 1
                break
        if MECH and len(self.trace) < 40 and si in (6, 21, 33, 46, 50, 63, 70, 73, 78) and mF in (15, 150):
            self.trace.append(f"{self.day:%m%d} s{si} m{mF} " + "/".join(f"{'PC'[r]}{K[k]:g}{'+' if q > 0 else '-'}" for r, k, q in legs)
                              + f" S{S0:.2f} c{cash0:.3f} f{fee0:.3f} den{den:.3f} p" + "/".join(f"{x:.3f}" for x in pnl)
                              + f" h{pnl_hold:.3f} n{nsh_hold}/{nsh_flat} x{xmin} Sc{S_close:.2f}")
        return self.pack(cash0, den, pnl, imp0, pnl_hold - pnl_flat, xmin, ximp, legs, fn, nx, info, shape, fee0, ffee,
                         spr0, nsh_hold, nsh_flat)

    def run_sdh(self, legs, K, B, A, MID, AGE, S, mF, mflat, S_close, cash0, fee0, imp0, spr0, den, info, shape):
        """Short ATM straddle hedged with the underlying every 15 minutes to the Black-Scholes delta (report only)."""
        k0 = legs[0][1]
        cost = 0.0001                        # per unit of underlying traded, as a fraction of S (1 bp for stocks)
        h, hcash = 0.0, 0.0
        for m in range(mF, mflat, 15):
            if AGE[1, k0, m] > AGE_MARK:
                continue
            Sm = float(S[m])
            cm = MID[1, k0, m]
            if not np.isnan(cm) and cm > 0:
                sv = implied_s(Sm, np.array([K[k0]]), np.array([cm]), False)[0]
                if not np.isnan(sv):
                    d1 = math.log(Sm / K[k0]) / sv + 0.5 * sv
                    tgt = 2.0 * float(ndtr(d1)) - 1.0
                    hcash -= (tgt - h) * Sm + abs(tgt - h) * cost * Sm
                    h = tgt
        Sf = float(S[mflat])
        fcash, ffee, fn, rest = self.flatten(legs, K, B, A, AGE, S, mflat, S_close)
        net = cash0 - fee0
        pnl_flat = net + fcash - ffee + hcash + h * Sf - abs(h) * cost * Sf
        pnl_hold = net + self.settle_value(legs, K, S_close) + hcash + h * S_close - abs(h) * cost * S_close
        pnl = [pnl_flat, pnl_flat, pnl_flat, pnl_flat]
        return self.pack(cash0, den, pnl, imp0, pnl_hold - pnl_flat, [0, 0, 0], [0.0, 0.0, 0.0], legs, fn, [0, 0, 0],
                         info, shape, fee0, ffee, spr0, shares_at(legs, K, S_close), shares_at(rest, K, S_close))

    # ------------------------------------------------------------------ export (8 exact integers per position)
    def pack(self, cash0, den, pnl, imp0, d_close, xmin, ximp, legs, fn, nx, info, shape, fee0, ffee, spr0,
             nsh_hold, nsh_flat):
        def p7(x):
            return min(9_999_999, max(0, int(round(x * U)) + OFF))

        def c4(x):                                             # cents, 0..9999
            return min(9999, max(0, int(round(x * 100))))

        x1 = float(p7(cash0) * 10**7 + min(9_999_999, max(0, int(round(den * U)))))
        x2 = float(p7(pnl[0]) * 10**7 + p7(pnl[1]))
        x3 = float(p7(pnl[2]) * 10**7 + p7(pnl[3]))
        x4 = float(min(9_999_999, int(round(imp0 * U))) * 10**7 + p7(d_close))
        x5 = float(min(999, xmin[0]) * 10**10 + min(999, xmin[1]) * 10**7 + min(999, xmin[2]) * 10**4 + c4(spr0))
        x6 = float(c4(ximp[0]) * 10**8 + c4(ximp[1]) * 10**4 + c4(ximp[2]))
        f = shape[0]
        sh = [(r, k) for r, k, qn in legs if qn < 0] or [(r, k) for r, k, qn in legs]
        dd = info["dlk"].get(sh[0]) if sh else None
        dl = min(999, int(round(dd * 1000))) if dd else 0
        wbp = 0
        if KIND[f] == 0 or f in VERT_DEBIT:
            wbp = min(9999, int(round(1e4 * abs(info["K"][legs[0][1]] - info["K"][legs[1][1]]) / info["s_impl"])))
        ne = len(legs)
        nsh = (max(-2, min(2, nsh_hold)) + 2) * 10 + (max(-2, min(2, nsh_flat)) + 2)
        x7 = float(nsh * 10**12 + dl * 10**9 + wbp * 10**5 + ne * 10**4 + min(9, fn) * 10**3 + min(9, nx[0]) * 100
                   + min(9, nx[1]) * 10 + min(9, nx[2]))
        x8 = float(min(9_999_999, int(round(fee0 * U))) * 10**7 + min(9_999_999, int(round(ffee * U))))
        return [x1, x2, x3, x4, x5, x6, x7, x8]
