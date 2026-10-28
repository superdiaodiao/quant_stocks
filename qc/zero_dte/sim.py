"""Pure-numpy day simulator for the 0DTE grid (docs/research_ledger_zero_dte.md, section 0.3).
QuantConnect project file next to main.py; it has no LEAN dependency, so tests/test_zero_dte_engine.py runs it
locally. main.py calls configure(UNDER) once, then DaySim.simulate_day(...) at each 0DTE day's close."""
import math
import time as _time

import numpy as np
from scipy.special import ndtr

ENTRY = ((9, 45), (10, 30), (12, 0), (13, 30), (15, 0))
FLAT = (15, 50)
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


def tick_of(px):
    if not IS_INDEX:
        return 0.01
    return 0.05 if px < 3.0 else 0.10


def floor_tick(x):
    t = tick_of(x)
    return math.floor(x / t + 1e-9) * t


def ceil_tick(x):
    t = tick_of(x)
    return math.ceil(x / t - 1e-9) * t


def fee(px):
    return (0.65 if px >= 0.10 else (0.50 if px >= 0.05 else 0.25)) + OTHER_FEE


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
                                   "flatdefer", "itmflat", "noquote")}
        self.trace = []
        self.tsim = 0.0
        self.M = 390
        self.day = None

    def simulate_day(self, D, S, contracts, vix):
        t1 = _time.time()
        q = self.loader(D, contracts, S, vix)
        if q is None:
            self.cnt["noquote"] += 1
            return None
        K, B, A, AGE, nload = q
        nk = len(K)
        MID = 0.5 * (B + A)
        SPR = A - B
        M = self.M
        mflat = (FLAT[0] * 60 + FLAT[1]) - (9 * 60 + 30) - 1
        if mflat >= M - 1:
            mflat = M - 2
        S_close = float(S[M - 1])
        rows, strad = [], [0.0] * NE
        for ei, (hh, mm) in enumerate(ENTRY):
            mS = (hh * 60 + mm) - (9 * 60 + 30) - 1
            mF = mS + 1
            if mF >= mflat - 2:
                for si in range(NS):
                    rows.append([-9.0])
                continue
            sel, info = self.select(S, K, B, A, MID, SPR, AGE, mS, vix)
            strad[ei] = info.get("strad", 0.0)
            for si, shape in enumerate(SHAPES):
                legs, code = self.legs_for(shape, sel, K, A, AGE, mS, info)
                if legs is None:
                    self.cnt[{2: "wingmiss", 3: "covmiss", 4: "stale"}.get(code, "nosel")] += 1
                    rows.append([-float(code)])
                    continue
                r = self.run_position(si, shape, legs, K, B, A, MID, SPR, AGE, S, mF, mflat, S_close, info)
                if r is None:
                    rows.append([-5.0])
                    continue
                rows.append(r)
                self.cnt["open"] += 1
        self.tsim += _time.time() - t1
        return rows, strad, nload

    def select(self, S, K, B, A, MID, SPR, AGE, mS, vix):
        """Short-leg candidates at minute mS: {(right, delta): strike index or "cov"}, plus the parity ATM strike.
        Only quotes at or before mS are used."""
        Sm = float(S[mS])
        nk = len(K)
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
        sd = (vix if vix else 30.0) / 100.0 / math.sqrt(252.0)
        band = max(0.04, 4.0 * sd) + 0.012
        tk = tick_of(0.0)
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
            # the outermost loaded strike on this side: if the furthest candidate is it and is still too rich, the
            # band (not the market) limited the choice
            edge = 0 if put else nk - 1
            for d in DELTAS[:-1]:
                if not has:
                    continue
                tol = max(0.03, 0.3 * d)
                dev = np.abs(dl - d)
                m = np.nanmin(dev)
                if m > tol:
                    far = int(cand[np.nanargmin(dl)])
                    if np.nanmin(dl) > d + tol and far == edge:
                        sel[(r, d)] = "cov"
                    continue
                tie = cand[dev <= m + 1e-12]
                ks = int(tie.min() if put else tie.max())          # ties go to the further OTM strike
                sel[(r, d)] = ks
                info["dlk"][(r, ks)] = float(dl[list(cand).index(ks)])
            # ATM (0.50): the parity strike, if it can be sold on this side
            if okq[r, k0] and np.nan_to_num(B[r, k0, mS]) >= tk:
                sel[(r, 0.50)] = k0
                info["dlk"][(r, k0)] = 0.5
        return sel, info

    def wing(self, r, ks, w, K, A, AGE, mS, s_impl):
        nk = len(K)
        if w == 0.0:
            j = ks - 1 if r == 0 else ks + 1
            if j < 0 or j >= nk:
                return None, 3
        else:
            tgt = K[ks] - w * s_impl if r == 0 else K[ks] + w * s_impl
            idx = np.arange(nk)
            beyond = idx[(K < K[ks])] if r == 0 else idx[(K > K[ks])]
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
        s_impl = info["s_impl"]

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
            kl, c = self.wing(r, ks, w, K, A, AGE, mS, s_impl)
            return (None, c) if kl is None else ([(r, ks, -1), (r, kl, 1)], 0)
        if f in (2, 3):
            kp, c1 = short(0, d)
            kc, c2 = short(1, d)
            if kp is None or kc is None:
                return None, (c1 or c2)
            lp, c1 = self.wing(0, kp, w, K, A, AGE, mS, s_impl)
            lc, c2 = self.wing(1, kc, w, K, A, AGE, mS, s_impl)
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
        if not (b >= tick_of(0.0)):
            return 0.0
        m, s = 0.5 * (a + b), a - b
        return max(b, floor_tick(m - F * s))

    @staticmethod
    def px_buy(b, a, F):
        m, s = 0.5 * (a + b), a - b
        return min(a, ceil_tick(m + F * s))

    def close_cash(self, legs, B, A, AGE, m, F, age_max):
        """Cash per share from closing every leg at minute m (+ sells longs, - buys back shorts), fees, spread paid,
        contracts traded; None if a short leg cannot be bought back here."""
        cash, fees, spr, n = 0.0, 0.0, 0.0, 0
        for r, k, qn in legs:
            b, a, ag = B[r, k, m], A[r, k, m], AGE[r, k, m]
            if qn < 0:
                if not (ag <= age_max and a > 0):
                    return None
                px = self.px_buy(b, a, F)
                cash -= px
                fees += fee(px) / 100.0
                spr += a - b
                n += 1
            else:
                if ag <= age_max and b >= tick_of(0.0):
                    px = self.px_sell(b, a, F)
                    cash += px
                    fees += fee(px) / 100.0
                    spr += a - b
                    n += 1
        return cash, fees, spr, n

    def settle_value(self, legs, K, Sx):
        v = 0.0
        for r, k, qn in legs:
            intr = max(K[k] - Sx, 0.0) if r == 0 else max(Sx - K[k], 0.0)
            v += qn * intr
        return v

    def flatten(self, legs, K, B, A, AGE, S, mflat, S_close):
        """15:50 convention: close legs that still matter (bid >= FLAT_BID or within 0.5% of S) at F_FLAT, retrying
        a short leg until the bar before the close; everything else is settled at intrinsic at the close."""
        cash, fees, spr, n = 0.0, 0.0, 0.0, 0
        rest = []
        M = self.M
        for r, k, qn in legs:
            Sm = float(S[mflat])
            matter = (B[r, k, mflat] >= FLAT_BID) or abs(K[k] / Sm - 1) <= 0.005
            if not matter or np.isnan(B[r, k, mflat]):
                rest.append((r, k, qn))
                continue
            done = False
            for m in range(mflat, M - 1):
                res = self.close_cash([(r, k, qn)], B, A, AGE, m, F_FLAT, AGE_FILL)
                if res is not None:
                    c, fe, sp, nn = res
                    cash += c
                    fees += fe
                    spr += sp
                    n += nn
                    done = True
                    if m > mflat:
                        self.cnt["flatdefer"] += 1
                    break
            if not done:
                rest.append((r, k, qn))
                self.cnt["itmflat"] += 1
        cash += self.settle_value(rest, K, S_close)
        return cash, fees, spr, n

    def run_position(self, si, shape, legs, K, B, A, MID, SPR, AGE, S, mF, mflat, S_close, info):
        f = shape[0]
        kind = KIND[f]
        # entry fill at mF (one minute after selection)
        cash0, fee0, spr0 = 0.0, 0.0, 0.0
        for r, k, qn in legs:
            b, a, ag = B[r, k, mF], A[r, k, mF], AGE[r, k, mF]
            if not (ag <= AGE_FILL and a > 0):
                self.cnt["stale"] += 1
                return None
            if qn < 0:
                if not (b >= tick_of(0.0)):
                    self.cnt["stale"] += 1
                    return None
                px = self.px_sell(b, a, F_ENTRY)
                cash0 += px
            else:
                px = self.px_buy(b, a, F_ENTRY)
                cash0 -= px
            fee0 += fee(px) / 100.0
            spr0 += a - b
        S0 = float(S[mF])
        tk = tick_of(0.0)
        if kind == 0:
            width = max(abs(K[legs[j][1]] - K[legs[j + 1][1]]) for j in range(0, len(legs), 2))
            den = width - (cash0 - fee0)
            if cash0 - fee0 < tk or den <= 0:
                self.cnt["nocredit"] += 1
                return None
        elif kind == 1:
            if cash0 - fee0 < tk:
                self.cnt["nocredit"] += 1
                return None
            den = S0
        elif kind == 2:
            den = -cash0 + fee0
        else:
            den = S0
        # marks: value to us of the open legs (long +mid, short -mid), valid where every leg has a recent quote
        n = len(legs)
        V = np.zeros(self.M)
        vok = np.ones(self.M, bool)
        xok = np.ones(self.M, bool)
        for r, k, qn in legs:
            V += qn * np.nan_to_num(MID[r, k], nan=0.0)
            vok &= AGE[r, k] <= AGE_MARK
            if qn < 0:
                xok &= (AGE[r, k] <= AGE_FILL) & (A[r, k] > 0)
        base_mid = -V[mF]                    # credit at mid at the entry minute (debit structures: negative)
        # flatten (primary for QQQ) and hold-to-close (cash) for the whole position
        fl = self.flatten(legs, K, B, A, AGE, S, mflat, S_close) if kind != 4 else None
        hold_val = self.settle_value(legs, K, S_close)
        pnl = [0.0, 0.0, 0.0, 0.0]
        xmin = [0, 0, 0]
        sx = [0.0, 0.0, 0.0]
        nx = [0, 0, 0]
        if kind == 4:
            return self.run_sdh(legs, K, B, A, MID, AGE, S, mF, mflat, S_close, cash0, fee0, spr0, den, info)
        fcash, ffee, fspr, fn = fl
        pnl_flat = cash0 - fee0 + fcash - ffee
        pnl_hold = cash0 - fee0 + hold_val
        pnl[0] = pnl_flat
        lo, hi = mF + 1, mflat - 2
        rng = np.zeros(self.M, bool)
        if hi >= lo:
            rng[lo:hi + 1] = True
        rng &= vok
        if kind in (0, 1):
            b = max(base_mid, tk)
            trig = [rng & (-V >= max(2 * b, b + 2 * tk)), rng & (-V >= max(3 * b, b + 3 * tk)), rng & (-V <= 0.75 * b)]
            fills = (F_STOP, F_STOP, F_TP)
        elif kind == 2:
            b = max(-base_mid, tk)
            trig = [rng & (V <= 0.5 * b), rng & (V >= 2 * b), rng & (V >= 3 * b)]
            fills = (F_STOP, F_TP, F_TP)
        else:
            pm = cash0 + V
            trig = [rng & (pm <= -0.005 * S0), rng & (pm <= -0.010 * S0), rng & (pm >= 0.010 * S0)]
            fills = (F_STOP, F_STOP, F_TP)
        for j in range(3):
            t = trig[j]
            if not t.any():
                pnl[j + 1] = pnl_flat
                continue
            self.cnt["trig"] += 1
            m0 = int(np.argmax(t))
            done = False
            for m in range(m0 + 1, mflat):
                if not xok[m]:
                    continue
                res = self.close_cash(legs, B, A, AGE, m, fills[j], AGE_FILL)
                if res is None:
                    continue
                c, fe, sp, nn = res
                pnl[j + 1] = cash0 - fee0 + c - fe
                xmin[j] = m + 1
                sx[j] = sp
                nx[j] = nn
                done = True
                if m > m0 + 1:
                    self.cnt["defer"] += 1
                break
            if not done:
                pnl[j + 1] = pnl_flat
        d_close = pnl_hold - pnl_flat
        if MECH and len(self.trace) < 40 and si in (6, 21, 33, 46, 50, 54, 56) and mF in (15, 150):
            self.trace.append(f"{self.day:%m%d} s{si} m{mF} " + "/".join(f"{'PC'[r]}{K[k]:g}{'+' if q > 0 else '-'}" for r, k, q in legs)
                              + f" S{S0:.2f} c{cash0:.3f} f{fee0:.3f} den{den:.3f} p" + "/".join(f"{x:.3f}" for x in pnl)
                              + f" h{pnl_hold:.3f} x{xmin} Sc{S_close:.2f}")
        return self.pack(cash0, den, pnl, spr0, d_close, xmin, fspr, sx, legs, fn, nx, info, shape)

    def run_sdh(self, legs, K, B, A, MID, AGE, S, mF, mflat, S_close, cash0, fee0, spr0, den, info):
        """Short ATM straddle hedged with the underlying every 15 minutes to the Black-Scholes delta (report only)."""
        k0 = legs[0][1]
        cost = 0.00002                       # per unit of underlying traded, as a fraction of S
        h, hp, hcash = 0.0, None, 0.0
        for m in range(mF, mflat, 15):
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
        res = self.close_cash(legs, B, A, AGE, mflat, F_FLAT, AGE_FILL)
        if res is None:
            fcash, ffee, fspr, fn = self.settle_value(legs, K, Sf), 0.0, 0.0, 0
        else:
            fcash, ffee, fspr, fn = res
        pnl_flat = cash0 - fee0 + fcash - ffee + hcash + h * Sf - abs(h) * cost * Sf
        pnl_hold = cash0 - fee0 + self.settle_value(legs, K, S_close) + hcash + h * S_close - abs(h) * cost * S_close
        pnl = [pnl_flat, pnl_flat, pnl_flat, pnl_flat]
        return self.pack(cash0, den, pnl, spr0, pnl_hold - pnl_flat, [0, 0, 0], fspr, [0.0, 0.0, 0.0], legs, fn, [0, 0, 0],
                         info, SHAPES[-1])

    # ------------------------------------------------------------------ export
    def pack(self, cash0, den, pnl, spr0, d_close, xmin, fspr, sx, legs, fn, nx, info, shape):
        def p7(x):
            return min(9_999_999, max(0, int(round(x * U)) + OFF))

        x1 = float(p7(cash0) * 10**7 + min(9_999_999, max(0, int(round(den * U)))))
        x2 = float(p7(pnl[0]) * 10**7 + p7(pnl[1]))
        x3 = float(p7(pnl[2]) * 10**7 + p7(pnl[3]))
        x4 = float(min(9_999_999, int(round(spr0 * U))) * 10**7 + p7(d_close))
        x5 = float(min(999, xmin[0]) * 10**10 + min(999, xmin[1]) * 10**7 + min(999, xmin[2]) * 10**4
                   + min(9999, int(round(fspr * U / 100))))
        x6 = float(min(9999, int(round(sx[0] * U / 100))) * 10**8 + min(9999, int(round(sx[1] * U / 100))) * 10**4
                   + min(9999, int(round(sx[2] * U / 100))))
        # diagnostics: realised |delta| of the first short leg (x1000), realised width / S (bp), legs counts
        f = shape[0]
        sh = [(r, k) for r, k, qn in legs if qn < 0]
        dd = info["dlk"].get(sh[0]) if sh else None
        dl = min(999, int(round(dd * 1000))) if dd else 0
        wbp = 0
        if KIND[f] == 0:
            wbp = min(9999, int(round(1e4 * abs(info["K"][legs[0][1]] - info["K"][legs[1][1]]) / info["s_impl"])))
        ne = len(legs)
        x7 = float(dl * 10**9 + wbp * 10**5 + ne * 10**4 + min(9, fn) * 10**3 + min(9, nx[0]) * 100 + min(9, nx[1]) * 10 + min(9, nx[2]))
        return [x1, x2, x3, x4, x5, x6, x7]

