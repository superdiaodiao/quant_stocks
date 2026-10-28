"""Pure-numpy day simulator for the 0DTE grid (docs/research_ledger_zero_dte.md, section 0.3).
QuantConnect project file next to main.py; it has no LEAN dependency, so tests/test_zero_dte_engine.py runs it
locally. main.py calls configure(UNDER) once, then DaySim.simulate_day(...) at each 0DTE day's close.

Minute index m: the bar ending 09:31 is m = 0, so the bar ending hh:mm is (hh*60 + mm) - 571. Quotes arrive as
arrays [right (0 put, 1 call), strike, minute], forward-filled with their age in minutes; a zero bid is a quote.
"""
import math
import time as _time

import numpy as np
from scipy.special import ndtr

ENTRY = ((9, 45), (10, 30), (12, 0), (13, 30), (15, 0))
FLAT_BEFORE = 10                               # flatten this many minutes before the option close (15:50, 12:50)
DELTAS = (0.05, 0.10, 0.16, 0.25, 0.50)
WINGS = (0.0, 0.005, 0.01)                     # 0.0 = the adjacent listed strike
F_ENTRY, F_TP, F_STOP, F_FLAT = 0.25, 0.25, 0.5, 0.5
AGE_FILL, AGE_MARK = 5, 15                     # max quote age (minutes) to fill / to mark

# families: kind 0 defined-risk credit, 1 naked credit, 2 debit, 3 risk reversal, 4 delta-hedged straddle
FAM = ("PCS", "CCS", "IC", "IF", "SP", "SC", "SG", "SD", "LD", "RR", "RRB", "SDH")
KIND = (0, 0, 0, 0, 1, 1, 1, 1, 2, 3, 3, 4)
IS_INDEX = False
U = 1e4                                        # export scale (dollars per share x U)
OTHER_FEE = 0.05                               # per contract on top of the IBKR tier (exchange / regulatory)
FLAT_BID = 0.05                                # at the flatten, legs with at least this bid (or near the money) are closed
OFF = 5_000_000
MECH = False


def configure(under, mech=False):
    """QQQ: equity options ($0.01 ticks); SPXW: index options ($0.05 / $0.10 ticks, Cboe proprietary fee)."""
    global IS_INDEX, U, OTHER_FEE, FLAT_BID, MECH
    IS_INDEX = under == "SPXW"
    U = 1e3 if IS_INDEX else 1e4
    OTHER_FEE = 0.65 if IS_INDEX else 0.05
    FLAT_BID = 0.50 if IS_INDEX else 0.05
    MECH = mech


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
    return out


SHAPES = build_shapes()                        # 61
NS, NE = len(SHAPES), len(ENTRY)


def mindex(hh, mm):
    return hh * 60 + mm - 571


def tick_v(x):
    x = np.asarray(x, float)
    if not IS_INDEX:
        return np.full(x.shape, 0.01)
    return np.where(x < 3.0, 0.05, 0.10)


def px_sell_v(b, a, F):
    """Sell at mid - F x spread, rounded to the nearest tick (ties toward the bid), inside [bid, ask];
    0 where the bid is below one tick (nothing to sell)."""
    b, a = np.asarray(b, float), np.asarray(a, float)
    x = 0.5 * (a + b) - F * (a - b)
    t = tick_v(x)
    px = np.floor(x / t + 0.5 - 1e-9) * t
    px = np.minimum(np.maximum(px, b), a)
    return np.where(b >= tick_v(0.0), px, 0.0)


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


def tick0():
    return 0.05 if IS_INDEX else 0.01


def sane(b, a):
    """A quote we are willing to trade on: ask > 0, 0 <= bid <= ask, spread <= max(5 ticks, half the mid)."""
    b, a = np.nan_to_num(np.asarray(b, float), nan=-1.0), np.nan_to_num(np.asarray(a, float), nan=0.0)
    return (a > 0) & (b >= 0) & (b <= a) & ((a - b) <= np.maximum(5 * tick0(), 0.25 * (a + b)))


def bs(S, K, s, put):
    d1 = np.log(S / K) / s + 0.5 * s
    d2 = d1 - s
    if put:
        return K * ndtr(-d2) - S * ndtr(-d1)
    return S * ndtr(d1) - K * ndtr(d2)


def implied_s(S, K, mid, put):
    """sigma*sqrt(T) from Black-Scholes with r = q = 0 (vectorised bisection); nan where mid is not attainable."""
    K = np.asarray(K, float)
    mid = np.asarray(mid, float)
    lo = np.full(K.shape, 1e-6)
    hi = np.full(K.shape, 1.0)
    top = bs(S, K, hi, put)
    for _ in range(50):
        s = 0.5 * (lo + hi)
        up = bs(S, K, s, put) > mid
        hi = np.where(up, s, hi)
        lo = np.where(up, lo, s)
    s = 0.5 * (lo + hi)
    return np.where((mid > 0) & (mid < top), s, np.nan)


def abs_delta(S, K, s, put):
    d1 = np.log(S / K) / s + 0.5 * s
    return np.abs(ndtr(d1) - 1.0) if put else ndtr(d1)


class DaySim:
    """One instance per backtest. simulate_day() turns one day's minute quotes into export rows."""

    def __init__(self, loader):
        self.loader = loader                   # (D, contracts, S, vix) -> (K, B, A, AGE, n_loaded) or None
        self.cnt = {k: 0 for k in ("open", "nosel", "wingmiss", "covmiss", "stale", "nocredit", "trig", "defer",
                                   "flatdefer", "itmflat", "held", "noquote")}
        self.trace = []
        self.tsim = 0.0
        self.M = 390                           # minutes from 09:31 to the option close (inclusive)
        self.day = None

    def simulate_day(self, D, S, contracts, vix):
        t1 = _time.time()
        q = self.loader(D, contracts, S, vix)
        if q is None:
            self.cnt["noquote"] += 1
            return None
        K, B, A, AGE, nload = q
        MID = 0.5 * (B + A)
        SPR = A - B
        M = self.M
        mflat = M - 1 - FLAT_BEFORE            # 15:50 (12:50 on a 13:00 close)
        S_close = float(S[M - 1])
        rows, strad = [], [0.0] * NE
        for ei, (hh, mm) in enumerate(ENTRY):
            mS = mindex(hh, mm)
            mF = mS + 1
            if mF >= mflat - 2:
                rows.extend([[-9.0]] * NS)
                continue
            sel, info = self.select(S, K, B, A, MID, SPR, AGE, mS, vix)
            strad[ei] = info.get("strad", 0.0)
            for si, shape in enumerate(SHAPES):
                legs, code = self.legs_for(shape, sel, K, A, AGE, mS, info)
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
        return rows, strad, nload

    # ------------------------------------------------------------------ selection (data at or before mS only)
    @staticmethod
    def band(vix):
        sd = (vix if vix else 30.0) / 100.0 / math.sqrt(252.0)
        return max(0.04, 4.0 * sd) + 0.012

    def select(self, S, K, B, A, MID, SPR, AGE, mS, vix):
        """Short-leg candidates at minute mS: {(right, delta): strike index or "cov"}, plus the parity ATM strike."""
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
        band = self.band(vix)
        # the strikes this entry may use: decided from s_impl at mS, never from later prices
        inb = np.abs(K / s_impl - 1) <= band + max(WINGS) + 0.005
        info["inb"] = inb
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

    def legs_for(self, shape, sel, K, A, AGE, mS, info):
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
            if not (AGE[0, k0, mS] <= AGE_FILL and AGE[1, k0, mS] <= AGE_FILL and A[0, k0, mS] > 0 and A[1, k0, mS] > 0):
                return None, 4
            return [(0, k0, 1), (1, k0, 1)], 0
        # risk reversals: long call d, short put d
        kp, c1 = short(0, d)
        kc = sel.get((1, d))
        if kp is None or not isinstance(kc, int):
            return None, (c1 or 1)
        if not (AGE[1, kc, mS] <= AGE_FILL and A[1, kc, mS] > 0):
            return None, 4
        return [(1, kc, 1), (0, kp, -1)], 0

    # ------------------------------------------------------------------ fills
    @staticmethod
    def px_sell(b, a, F):
        return float(px_sell_v(b, a, F))

    @staticmethod
    def px_buy(b, a, F):
        return float(px_buy_v(b, a, F))

    def close_cash(self, legs, B, A, AGE, m, F, age_max):
        """Close every leg at minute m: cash per share (+ longs sold, - shorts bought back), fees, improvement over
        the touch, contracts traded, and the long legs that could not be sold here (no bid, or a stale quote):
        they are held to the close and settled at intrinsic. None if a short leg cannot be bought back."""
        cash, fees, imp, n, held = 0.0, 0.0, 0.0, 0, []
        tk = tick0()
        for r, k, qn in legs:
            b, a, ag = B[r, k, m], A[r, k, m], AGE[r, k, m]
            if qn < 0:
                if not (ag <= age_max and sane(b, a)):
                    return None
                px = self.px_buy(b, a, F)
                cash -= px
                fees += float(fee_v(px)) / 100.0
                imp += a - px
                n += 1
            elif ag <= age_max and b >= tk and sane(b, a):
                px = self.px_sell(b, a, F)
                cash += px
                fees += float(fee_v(px)) / 100.0
                imp += px - b
                n += 1
            else:
                held.append((r, k, qn))        # never given away: held to the close and settled at intrinsic
        return cash, fees, imp, n, held

    @staticmethod
    def settle_value(legs, K, Sx):
        v = 0.0
        for r, k, qn in legs:
            intr = max(K[k] - Sx, 0.0) if r == 0 else max(Sx - K[k], 0.0)
            v += qn * intr
        return v

    def flatten(self, legs, K, B, A, AGE, S, mflat, S_close):
        """Close legs that still matter (bid >= FLAT_BID or within 0.5% of S) at F_FLAT from mflat, retrying a leg
        until the bar before the close; the rest, and anything that never fills, settles at intrinsic at the close."""
        cash, fees, n = 0.0, 0.0, 0
        rest = []
        M = self.M
        Sm = float(S[mflat])
        for leg in legs:
            r, k, qn = leg
            matter = (np.nan_to_num(B[r, k, mflat]) >= FLAT_BID) or abs(K[k] / Sm - 1) <= 0.005
            if not matter:
                rest.append(leg)
                continue
            done = False
            for m in range(mflat, M - 1):
                res = self.close_cash([leg], B, A, AGE, m, F_FLAT, AGE_FILL)
                if res is None or (res[4] and np.nan_to_num(B[r, k, m]) >= tick0()):
                    continue                          # retry a leg that still shows a bid but cannot trade yet
                if res[4]:
                    break                             # no bid: hold to the close (falls through to rest)
                c, fe, _, nn, _ = res
                cash += c
                fees += fe
                n += nn
                done = True
                if m > mflat:
                    self.cnt["flatdefer"] += 1
                break
            if not done:
                rest.append(leg)
                self.cnt["itmflat"] += 1
        cash += self.settle_value(rest, K, S_close)
        return cash, fees, n

    def exit_value(self, legs, B, A, AGE, F):
        """Per minute: cash per share from closing everything now at F, net of fees; nan where a short leg cannot
        be bought back or a long leg that still shows a bid is stale."""
        tk = tick0()
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
        fcash, ffee, fn = self.flatten(legs, K, B, A, AGE, S, mflat, S_close)
        pnl_flat = net + fcash - ffee
        pnl_hold = net + self.settle_value(legs, K, S_close)
        pnl = [pnl_flat, pnl_flat, pnl_flat, pnl_flat]
        xmin, ximp, nx = [0, 0, 0], [0.0, 0.0, 0.0], [0, 0, 0]
        rng = np.zeros(self.M, bool)
        if mflat - 2 >= mF + 1:
            rng[mF + 1:mflat - 1] = True
        rng &= vok
        if kind in (0, 1):
            b = max(net, tk)                                   # the credit actually kept after entry fees
            Vx = self.exit_value(legs, B, A, AGE, F_TP)
            trig = [rng & (-V >= 2 * b), rng & (-V >= 3 * b), rng & (np.nan_to_num(-Vx, nan=1e9) <= 0.75 * b)]
            fills = (F_STOP, F_STOP, F_TP)
        elif kind == 2:
            b = max(-net, tk)                                  # the debit actually paid including entry fees
            Vx = self.exit_value(legs, B, A, AGE, F_TP)
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
                res = self.close_cash(legs, B, A, AGE, m, fills[j], AGE_FILL)
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
        if MECH and len(self.trace) < 40 and si in (6, 21, 33, 46, 50, 54, 56) and mF in (15, 150):
            self.trace.append(f"{self.day:%m%d} s{si} m{mF} " + "/".join(f"{'PC'[r]}{K[k]:g}{'+' if q > 0 else '-'}" for r, k, q in legs)
                              + f" S{S0:.2f} c{cash0:.3f} f{fee0:.3f} den{den:.3f} p" + "/".join(f"{x:.3f}" for x in pnl)
                              + f" h{pnl_hold:.3f} x{xmin} Sc{S_close:.2f}")
        return self.pack(cash0, den, pnl, imp0, pnl_hold - pnl_flat, xmin, ximp, legs, fn, nx, info, shape, fee0, ffee, spr0)

    def run_sdh(self, legs, K, B, A, MID, AGE, S, mF, mflat, S_close, cash0, fee0, imp0, spr0, den, info, shape):
        """Short ATM straddle hedged with the underlying every 15 minutes to the Black-Scholes delta (report only)."""
        k0 = legs[0][1]
        cost = 0.00002                       # per unit of underlying traded, as a fraction of S
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
        fcash, ffee, fn = self.flatten(legs, K, B, A, AGE, S, mflat, S_close)
        net = cash0 - fee0
        pnl_flat = net + fcash - ffee + hcash + h * Sf - abs(h) * cost * Sf
        pnl_hold = net + self.settle_value(legs, K, S_close) + hcash + h * S_close - abs(h) * cost * S_close
        pnl = [pnl_flat, pnl_flat, pnl_flat, pnl_flat]
        return self.pack(cash0, den, pnl, imp0, pnl_hold - pnl_flat, [0, 0, 0], [0.0, 0.0, 0.0], legs, fn, [0, 0, 0],
                         info, shape, fee0, ffee, spr0)

    # ------------------------------------------------------------------ export (8 exact integers per position)
    def pack(self, cash0, den, pnl, imp0, d_close, xmin, ximp, legs, fn, nx, info, shape, fee0, ffee, spr0):
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
        sh = [(r, k) for r, k, qn in legs if qn < 0]
        dd = info["dlk"].get(sh[0]) if sh else None
        dl = min(999, int(round(dd * 1000))) if dd else 0
        wbp = 0
        if KIND[f] == 0:
            wbp = min(9999, int(round(1e4 * abs(info["K"][legs[0][1]] - info["K"][legs[1][1]]) / info["s_impl"])))
        ne = len(legs)
        x7 = float(dl * 10**9 + wbp * 10**5 + ne * 10**4 + min(9, fn) * 10**3 + min(9, nx[0]) * 100 + min(9, nx[1]) * 10 + min(9, nx[2]))
        x8 = float(min(9_999_999, int(round(fee0 * U))) * 10**7 + min(9_999_999, int(round(ffee * U))))
        return [x1, x2, x3, x4, x5, x6, x7, x8]
