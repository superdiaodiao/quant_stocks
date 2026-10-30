"""Position management, day loop and export of the single-stock 0DTE grid (docs/research_ledger_stock_0dte.md, 0.3),
the second engine file next to sim.py (QuantConnect caps one file at 32,000 characters): opening and managing one
position (stops, take-profits, the 15:50 flatten, hold / FLAT15 values), the five same-day entries, the overnight
arm (enter at the previous close, manage on the expiry day), the opening-range breakout, the delta-hedged straddle
and the 10-integer row packing. main.py builds one DaySimX per backtest; tests/test_stock_0dte_engine.py runs it."""
import math
import time as _time

import numpy as np
from scipy.special import ndtr

import sim as Z
from optmath import implied_s, shares_at

ORB_RANGE = 5                                  # opening range: the open and the bars ending 09:31..09:35
ORB_LAST = Z.mindex(12, 0)                     # no breakout entry after 12:00
ORB_STOP = Z.mindex(15, 30)                    # time stop
REAL_BAR = 5                                   # an entry needs a real stock bar within this many minutes


class DaySimX(Z.DaySim):
    """One instance per backtest. simulate_day() turns one name-day's minute quotes into NB x NS export rows;
    on_enter() opens the overnight arm at the previous session's close for the next expiry."""

    def simulate_day(self, D, S, contracts, sig, kmp, S_open, on_state=None, bars=None):
        """Rows for every (block, shape): blocks 0..4 the same-day entries, block 5 the overnight positions opened
        at the previous close (on_state from on_enter, None if there were none); plus the at-the-money straddle mid
        and the open-interest pin strike at each entry, and the number of contracts loaded. bars marks the minutes
        with a real stock trade (None: all)."""
        t1 = _time.time()
        extra = sorted({k for st in (on_state or []) if isinstance(st, dict) for _, k, _ in st["legs"]})
        q = self.loader(D, contracts, S, sig, extra, None)
        if q is None:
            self.cnt["noquote"] += 1
            return None
        K, B, A, AGE, OI, nload = q
        MID = 0.5 * (B + A)
        SPR = A - B
        M = self.M
        mflat = M - 1 - Z.FLAT_BEFORE
        S_close = float(S[M - 1])
        ok = np.ones(M, bool) if bars is None else np.asarray(bars, bool)
        rows, strad, pins = [], [0.0] * Z.NE, [0.0] * Z.NE
        for ei, (hh, mm) in enumerate(Z.ENTRY):
            mS = Z.mindex(hh, mm)
            mF = mS + 1
            if mF >= mflat - 2:
                rows.extend([[-9.0]] * Z.NS)
                continue
            if not ok[max(0, mS - REAL_BAR):mS + 1].any() or not np.isfinite(S[mS]):
                rows.extend([[-4.0]] * Z.NS)                   # no real stock trade just before the entry
                continue
            sel, info = self.select(S, K, B, A, MID, SPR, AGE, OI, mS, sig, kmp)
            strad[ei] = info.get("strad", 0.0)
            pins[ei] = float(K[info["kpin"]]) if info["kpin"] >= 0 else 0.0
            for si, shape in enumerate(Z.SHAPES):
                if shape[0] == 20:
                    rows.append(self.run_orb(si, shape, K, B, A, MID, SPR, AGE, OI, S, S_open, mflat, S_close, sig, kmp,
                                             ok) if ei == 0 else [-9.0])
                    continue
                legs, code = self.legs_for(shape, sel, K, B, A, AGE, mS, info)
                if legs is None:
                    self.cnt[{2: "wingmiss", 3: "covmiss", 4: "stale"}.get(code, "nosel")] += 1
                    rows.append([-float(code)])
                    continue
                rows.append(self.open_and_manage(si, shape, legs, K, B, A, MID, AGE, S, mF, mflat, S_close, info))
        rows.extend(self.resume_overnight(on_state, K, B, A, MID, AGE, S, mflat, S_close))
        self.tsim += _time.time() - t1
        return rows, strad, pins, nload

    def open_and_manage(self, si, shape, legs, K, B, A, MID, AGE, S, mF, mflat, S_close, info):
        e = self.entry_fill(legs, B, A, AGE, mF)
        if e is None:
            self.cnt["stale"] += 1
            return [-5.0]
        cash0, fee0, imp0, spr0 = e
        S0 = float(S[mF])
        den = self.risk_base(shape[0], legs, K, cash0 - fee0, S0)
        if den is None:
            self.cnt["nocredit"] += 1
            return [-5.0]
        self.cnt["open"] += 1
        if Z.KIND[shape[0]] == 4:
            return self.run_sdh(legs, K, B, A, MID, AGE, S, mF, mflat, S_close, cash0, fee0, imp0, spr0, den, info, shape)
        return self.manage(si, shape, legs, K, B, A, MID, AGE, S, mF, mflat, S_close, info, cash0, fee0, imp0, spr0,
                           S0, den)

    # ------------------------------------------------------------------ one position
    def entry_fill(self, legs, B, A, AGE, mF):
        """(cash per share, fees, improvement over the touch, summed spread) of opening the legs at mF, or None."""
        cash0, fee0, imp0, spr0 = 0.0, 0.0, 0.0, 0.0
        tk = Z.tick0()
        for r, k, qn in legs:
            b, a, ag = B[r, k, mF], A[r, k, mF], AGE[r, k, mF]
            q = abs(qn)
            spr0 += a - b
            if not (ag <= Z.AGE_FILL and Z.strict(b, a)):
                return None
            if qn < 0:
                if not (b >= tk):
                    return None
                px = self.px_sell(b, a, Z.F_ENTRY)
                cash0 += px * q
                imp0 += (px - b) * q
            else:
                px = self.px_buy(b, a, Z.F_ENTRY)
                cash0 -= px * q
                imp0 += (a - px) * q
            fee0 += float(Z.fee_v(px)) * q / 100.0
        return cash0, fee0, imp0, spr0

    @staticmethod
    def risk_base(f, legs, K, net, S0):
        """Unit denominator (ledger 0.5) or None when the position is not worth opening (net credit < 1 tick)."""
        kind = Z.KIND[f]
        tk = Z.tick0()
        if kind == 0:
            width = max(abs(K[legs[j][1]] - K[legs[j + 1][1]]) for j in range(0, len(legs), 2))
            den = width - net
            return den if (net >= tk and den > 0) else None
        if kind == 6:
            # 1:3:2 call fly: worth h_down at the body, h_down - 2 h_up above the top strike, 0 below the bottom one
            hd = float(K[legs[1][1]] - K[legs[0][1]])
            hu = float(K[legs[2][1]] - K[legs[1][1]])
            if hd <= 0 or hu <= 0:
                return None
            return max(max(2 * hu - hd, 0.0) - net, 0.25 * hd)
        if kind == 1:
            return S0 if net >= tk else None
        if kind == 2:
            return -net if net < 0 else None
        return S0

    def manage(self, si, shape, legs, K, B, A, MID, AGE, S, m0, mflat, S_close, info, cash0, fee0, imp0, spr0, S0,
               den):
        """Marks, stops / take-profits from m0 + 1, the 15:50 flatten, the hold-to-close value, the share delivery
        and the FLAT15 sensitivity of one opened position; returns its export row."""
        f = shape[0]
        kind = Z.KIND[f]
        tk = Z.tick0()
        net = cash0 - fee0
        V = np.zeros(self.M)
        vok = np.ones(self.M, bool)
        for r, k, qn in legs:
            b = np.nan_to_num(B[r, k])
            V += qn * np.nan_to_num(MID[r, k], nan=0.0)
            # markable: a recent quote, or no bid at all; never before the leg's first quote; a quote that passes
            # only the exit floor (not the entry test) cannot mark a leg that shows a bid
            vok &= ((AGE[r, k] <= Z.AGE_MARK) | (b < tk)) & (AGE[r, k] < 10_000)
            vok &= (b < tk) | Z.strict(b, np.nan_to_num(A[r, k]))
        pairs = kind == 0 or f in Z.VERT_DEBIT
        fcash, ffee, fn, rest, nord, sur, stuck = self.flatten(legs, K, B, A, AGE, S, mflat, S_close, pairs)
        pnl_flat = net + fcash - ffee
        pnl_hold = net + self.settle_value(legs, K, S_close)
        nsh_hold = shares_at(legs, K, S_close)
        nsh_flat = shares_at(rest, K, S_close)
        if nsh_flat:
            self.cnt["leftitm"] += 1
        # FLAT15: a position that would deliver shares at the 15:00 price is closed then (IBKR may liquidate an
        # account that cannot take delivery)
        d15 = 0.0
        m15 = Z.mindex(15, 0)
        if m0 < m15 < mflat and shares_at(legs, K, float(S[m15])) != 0:
            c15, f15 = self.flatten(legs, K, B, A, AGE, S, m15, S_close, pairs)[:2]
            d15 = (net + c15 - f15) - pnl_flat
            self.cnt["f15"] += 1
        pnl = [pnl_flat, pnl_flat, pnl_flat, pnl_flat]
        xmin, ximp, nx, xfee = [0, 0, 0], [0.0, 0.0, 0.0], [0, 0, 0], [0.0, 0.0, 0.0]
        rng = np.zeros(self.M, bool)
        if mflat - 2 >= m0 + 1:
            rng[m0 + 1:mflat - 1] = True
        rng &= vok
        if kind in (0, 1):
            b = max(net, tk)                                   # the credit actually kept after entry fees
            Vx = self.exit_value(legs, B, A, AGE, Z.F_TP, K if pairs else None)
            trig = [rng & (-V >= 2 * b), rng & (-V >= 3 * b), rng & (np.nan_to_num(-Vx, nan=1e9) <= 0.75 * b)]
            fills = (Z.F_STOP, Z.F_STOP, Z.F_TP)
        elif kind == 2:
            b = max(-net, tk)                                  # the debit actually paid including entry fees
            Vx = self.exit_value(legs, B, A, AGE, Z.F_TP, K if pairs else None)
            trig = [rng & (V <= 0.5 * b), rng & (np.nan_to_num(Vx, nan=-1e9) >= 2 * b),
                    rng & (np.nan_to_num(Vx, nan=-1e9) >= 3 * b)]
            fills = (Z.F_STOP, Z.F_TP, Z.F_TP)
        elif kind == 6:
            Vx = self.exit_value(legs, B, A, AGE, Z.F_TP)
            gain = net + np.nan_to_num(Vx, nan=-1e9)
            trig = [rng & (net + V <= -0.5 * den), rng & (gain >= 0.5 * den), rng & (gain >= 0.3 * den)]
            fills = (Z.F_STOP, Z.F_TP, Z.F_TP)
        else:
            pm = net + V
            trig = [rng & (pm <= -0.005 * S0), rng & (pm <= -0.010 * S0), rng & (pm >= 0.010 * S0)]
            fills = (Z.F_STOP, Z.F_STOP, Z.F_TP)
        if m0 < 0:
            # overnight arm: the first slot exits at 09:45 on the expiry day (only the overnight is held); slot 2
            # keeps its stop, which the per-side-stop composites are built from
            t = np.zeros(self.M, bool)
            t[Z.mindex(9, 45) - 1] = True
            trig[0] = t
            fills = (Z.F_FLAT, fills[1], fills[2])
        for j in range(3):
            t = trig[j]
            if not t.any():
                continue
            self.cnt["trig"] += 1
            mt = int(np.argmax(t))
            for m in range(mt + 1, mflat):
                res = self.close_cash(legs, B, A, AGE, m, fills[j], Z.AGE_FILL, K if pairs else None)
                if res is None:
                    continue
                c, fe, im, nn, held = res
                hc = 0.0
                if held:                                       # longs that could not be sold: the 15:50 flatten
                    self.cnt["held"] += 1
                    hcash, hfee = self.flatten(held, K, B, A, AGE, S, mflat, S_close)[:2]
                    hc = hcash - hfee
                pnl[j + 1] = net + c - fe + hc
                xmin[j] = m + 1
                ximp[j] = im
                nx[j] = nn
                xfee[j] = fe
                if m > mt + 1:
                    self.cnt["defer"] += 1
                break
        if Z.MECH and nsh_flat and len(self.ftrace) < 15:
            q = lambda r, k, m: f"{B[r, k, m]:.2f}/{A[r, k, m]:.2f}a{min(99, int(AGE[r, k, m]))}"
            self.ftrace.append(f"{self.day:%m%d} s{si} S{S[mflat]:.2f}>{S_close:.2f} " + " ".join(
                f"{'PC'[r]}{K[k]:g}{qn:+d}:{q(r, k, mflat)}>{q(r, k, self.M - 2)}" for r, k, qn in legs))
        return self.pack(cash0, den, pnl, imp0, pnl_hold - pnl_flat, xmin, ximp, legs, fn, nx, info, shape, fee0, ffee,
                         spr0, nsh_hold, nsh_flat, d15, S0, xfee, nord, sur, stuck)

    # ------------------------------------------------------------------ overnight arm
    def on_enter(self, D, S, contracts, sig, kmp):
        """At the previous session D's close (ON_BEFORE minutes before it), open every shape on the contracts that
        expire on the next session. Returns per shape a dict (legs by strike value, entry cash, fees, risk base, the
        stock price and parity price at the fill, the short leg's delta, the trade direction) or an int code, and the
        straddle mid."""
        M = self.M
        mS = M - 1 - Z.ON_BEFORE
        mF = mS + 1
        q = self.loader(D, contracts, S, sig, [], mS - 10)
        if q is None:
            return None, 0.0
        K, B, A, AGE, OI, nload = q
        MID = 0.5 * (B + A)
        SPR = A - B
        sel, info = self.select(S, K, B, A, MID, SPR, AGE, OI, mS, sig, kmp)
        out = []
        for si, shape in enumerate(Z.SHAPES):
            if shape[0] in Z.NO_ON:
                out.append(9)
                continue
            legs, code = self.legs_for(shape, sel, K, B, A, AGE, mS, info)
            if legs is None:
                out.append(code)
                continue
            e = self.entry_fill(legs, B, A, AGE, mF)
            if e is None:
                out.append(5)
                continue
            cash0, fee0, imp0, spr0 = e
            den = self.risk_base(shape[0], legs, K, cash0 - fee0, float(S[mF]))
            if den is None:
                out.append(5)
                continue
            sh = [(r, k) for r, k, qn in legs if qn < 0] or [(r, k) for r, k, qn in legs]
            out.append({"legs": [(r, float(K[k]), qn) for r, k, qn in legs], "cash0": cash0, "fee0": fee0,
                        "imp0": imp0, "spr0": spr0, "den": den, "S0": float(S[mF]), "s_impl": info["s_impl"],
                        "dl": info["dlk"].get(sh[0], 0.0), "dir": self.direction(shape[0], legs, K, info)})
        return out, info.get("strad", 0.0)

    def resume_overnight(self, on_state, K, B, A, MID, AGE, S, mflat, S_close):
        """Block 5: manage each overnight position on its expiry day from the first bar (stops, the flatten, hold);
        a position whose strikes have no quotes that day settles at intrinsic (every leg counted as stuck)."""
        if on_state is None:
            return [[-8.0]] * Z.NS
        rows = []
        kix = {float(k): i for i, k in enumerate(K)}
        for si, st in enumerate(on_state):
            if not isinstance(st, dict):
                rows.append([-float(st)])
                continue
            if any(k not in kix for _, k, _ in st["legs"]):
                rows.append(self.settle_overnight(si, st, S_close))
                continue
            legs = [(r, kix[k], qn) for r, k, qn in st["legs"]]
            shape = Z.SHAPES[si]
            info = {"K": K, "s_impl": st["s_impl"], "dlk": {}, "dir": st.get("dir", 0)}
            sh = [(r, k) for r, k, qn in legs if qn < 0] or [(r, k) for r, k, qn in legs]
            info["dlk"][sh[0]] = st["dl"]
            self.cnt["open"] += 1
            rows.append(self.manage(si, shape, legs, K, B, A, MID, AGE, S, -1, mflat, S_close, info, st["cash0"],
                                    st["fee0"], st["imp0"], st["spr0"], st["S0"], st["den"]))
        return rows

    def settle_overnight(self, si, st, S_close):
        """An overnight position whose expiry day cannot be simulated: every leg at intrinsic at S_close."""
        K = np.array([k for _, k, _ in st["legs"]], float)
        legs = [(r, i, qn) for i, (r, _, qn) in enumerate(st["legs"])]
        net = st["cash0"] - st["fee0"]
        pnl = net + self.settle_value(legs, K, S_close)
        nsh = shares_at(legs, K, S_close)
        self.cnt["onsettle"] = self.cnt.get("onsettle", 0) + 1
        sh = [(r, k) for r, k, qn in legs if qn < 0] or [(r, k) for r, k, qn in legs]
        info = {"K": K, "s_impl": st["s_impl"], "dlk": {sh[0]: st["dl"]}, "dir": st.get("dir", 0)}
        return self.pack(st["cash0"], st["den"], [pnl] * 4, st["imp0"], 0.0, [0, 0, 0], [0.0] * 3, legs, 0, [0, 0, 0],
                         info, Z.SHAPES[si], st["fee0"], 0.0, st["spr0"], nsh, nsh, 0.0, st["S0"], [0.0] * 3, 0, 0.0,
                         len(legs))

    @staticmethod
    def direction(f, legs, K, info):
        """1 = the trade profits from a rise (MPD calls; pin / max-pain body above the stock), 2 = a fall, else 0."""
        if f == 19:
            return 1 if legs[0][0] == 1 else 2
        if f in (17, 18):
            return 1 if K[legs[0][1]] > info["s_impl"] else 2
        return 0

    # ------------------------------------------------------------------ opening-range breakout
    def run_orb(self, si, shape, K, B, A, MID, SPR, AGE, OI, S, S_open, mflat, S_close, sig, kmp, ok):
        """Range = the open and the closes of the first five bars (real ones only). The first real close outside it
        before 12:00 buys the at-the-money call (above) or put (below) one minute later; exit at +100% (sold at F_TP)
        or -50% of the debit (mid, sold at F_STOP), else at 15:30 (F_FLAT); the same value fills all four management
        slots. No open price, no trade."""
        if not S_open or not ok[:ORB_RANGE].any():
            return [-1.0]
        rng = [S_open] + [float(S[m]) for m in range(ORB_RANGE) if ok[m]]
        hi, lo = max(rng), min(rng)
        mS, r = None, None
        for m in range(ORB_RANGE, min(ORB_LAST, mflat - 3) + 1):
            if not ok[m]:
                continue
            if S[m] > hi:
                mS, r = m, 1
                break
            if S[m] < lo:
                mS, r = m, 0
                break
        if mS is None:
            self.cnt["nosel"] += 1
            return [-1.0]
        sel, info = self.select(S, K, B, A, MID, SPR, AGE, OI, mS, sig, kmp)
        k0 = info["k0"]
        if not info["atm"] or not self.buyable(r, k0, A, AGE, mS):
            return [-4.0]
        legs = [(r, k0, 1)]
        mF = mS + 1
        e = self.entry_fill(legs, B, A, AGE, mF)
        if e is None:
            return [-5.0]
        cash0, fee0, imp0, spr0 = e
        debit = fee0 - cash0
        if debit <= 0:
            return [-5.0]
        self.cnt["open"] += 1
        Vx = self.exit_value(legs, B, A, AGE, Z.F_TP)
        V = np.nan_to_num(MID[r, k0], nan=0.0)
        mk = (AGE[r, k0] <= Z.AGE_MARK) | (np.nan_to_num(B[r, k0]) < Z.tick0())
        live = np.zeros(self.M, bool)
        live[mF + 1:ORB_STOP] = True
        hit = live & mk & ((np.nan_to_num(Vx, nan=-1e9) >= 2 * debit) | (V <= 0.5 * debit))
        m_exit, F = (int(np.argmax(hit)) + 1, None) if hit.any() else (ORB_STOP, Z.F_FLAT)
        pnl, xm, ximp, nx, rest, xfee, nord, sur, stuck = None, 0, 0.0, 0, legs, 0.0, 0, 0.0, 0
        for m in range(m_exit, mflat):
            FF = F if F is not None else (Z.F_TP if Vx[m - 1] >= 2 * debit else Z.F_STOP)
            res = self.close_cash(legs, B, A, AGE, m, FF, Z.AGE_FILL)
            if res is None or res[4]:
                continue
            c, fe, im, nn, held = res
            pnl, xm, ximp, nx, rest, xfee = cash0 - fee0 + c - fe, m + 1, im, nn, [], fe
            break
        if pnl is None:                                        # never sold: the 15:50 flatten / intrinsic
            fcash, ffee, fn, rest, nord, sur, stuck = self.flatten(legs, K, B, A, AGE, S, mflat, S_close)
            pnl, nx, xfee = cash0 - fee0 + fcash - ffee, fn, ffee
        # slots: exit minute, entry (fill) minute, direction (1 call, 2 put); the exit order's fee in the first slot
        return self.pack(cash0, debit, [pnl] * 4, imp0, 0.0, [xm, mF + 1, 1 if r == 1 else 2], [ximp] * 3, legs, nx,
                         [nx] * 3, info, shape, fee0, 0.0, spr0, 0, shares_at(rest, K, S_close), 0.0, float(S[mF]),
                         [xfee, 0.0, 0.0], nord, sur, stuck)

    # ------------------------------------------------------------------ delta-hedged straddle (report only)
    def run_sdh(self, legs, K, B, A, MID, AGE, S, mF, mflat, S_close, cash0, fee0, imp0, spr0, den, info, shape):
        """Short ATM straddle hedged with the stock every 15 minutes to the Black-Scholes delta (report only)."""
        k0 = legs[0][1]
        cost = 0.0001                        # per unit of stock traded, as a fraction of S (1 bp)
        h, hcash = 0.0, 0.0
        for m in range(mF, mflat, 15):
            if AGE[1, k0, m] > Z.AGE_MARK:
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
        fcash, ffee, fn, rest, nord, sur, stuck = self.flatten(legs, K, B, A, AGE, S, mflat, S_close)
        net = cash0 - fee0
        pnl_flat = net + fcash - ffee + hcash + h * Sf - abs(h) * cost * Sf
        pnl_hold = net + self.settle_value(legs, K, S_close) + hcash + h * S_close - abs(h) * cost * S_close
        return self.pack(cash0, den, [pnl_flat] * 4, imp0, pnl_hold - pnl_flat, [0, 0, 0], [0.0, 0.0, 0.0], legs, fn,
                         [0, 0, 0], info, shape, fee0, ffee, spr0, shares_at(legs, K, S_close),
                         shares_at(rest, K, S_close), 0.0, float(S[mF]), [0.0, 0.0, 0.0], nord, sur, stuck)

    # ------------------------------------------------------------------ export (10 exact integers per position)
    def pack(self, cash0, den, pnl, imp0, d_close, xmin, ximp, legs, fn, nx, info, shape, fee0, ffee, spr0,
             nsh_hold, nsh_flat, d15, s_fill, xfee, nord, sur, stuck):
        U, OFF = Z.U, Z.OFF

        def p7(x):
            return min(9_999_999, max(0, int(round(x * U)) + OFF))

        def cn(x, top):                                        # cents, 0..top
            return min(top, max(0, int(round(x * 100))))

        x1 = float(p7(cash0) * 10**7 + min(9_999_999, max(0, int(round(den * U)))))
        x2 = float(p7(pnl[0]) * 10**7 + p7(pnl[1]))
        x3 = float(p7(pnl[2]) * 10**7 + p7(pnl[3]))
        x4 = float(min(9_999_999, int(round(imp0 * U))) * 10**7 + p7(d_close))
        x5 = float(min(9, stuck) * 10**13 + min(999, xmin[0]) * 10**10 + min(999, xmin[1]) * 10**7
                   + min(999, xmin[2]) * 10**4 + cn(spr0, 9999))
        x6 = float(cn(ximp[0], 9999) * 10**8 + cn(ximp[1], 9999) * 10**4 + cn(ximp[2], 9999))
        f = shape[0]
        sh = [(r, k) for r, k, qn in legs if qn < 0] or [(r, k) for r, k, qn in legs]
        dd = info["dlk"].get(sh[0]) if sh else None
        dl = min(999, int(round(dd * 1000))) if dd else 0
        wbp = 0
        if Z.KIND[f] in (0, 6) or f in Z.VERT_DEBIT:
            wbp = min(9999, int(round(1e4 * abs(info["K"][legs[0][1]] - info["K"][legs[1][1]]) / info["s_impl"])))
        nc = sum(abs(q) for _, _, q in legs)                   # contracts at entry (the ratio fly trades 6)
        nsh = (max(-2, min(2, nsh_hold)) + 2) * 10 + (max(-2, min(2, nsh_flat)) + 2)
        dr = info.get("dir")
        if dr is None:
            dr = self.direction(f, legs, info["K"], info)
        x7 = float(dr * 10**14 + nsh * 10**12 + dl * 10**9 + wbp * 10**5 + min(9, nc) * 10**4 + min(9, fn) * 10**3
                   + min(9, nx[0]) * 100 + min(9, nx[1]) * 10 + min(9, nx[2]))
        x8 = float(min(9_999_999, int(round(fee0 * U))) * 10**7 + min(9_999_999, int(round(ffee * U))))
        x9 = float(min(99_999_999, max(0, int(round(s_fill * 100)))) * 10**7 + p7(d15))
        # the flatten's orders and their 1-lot $1-minimum surcharge (cents), then each M exit's fees ($ per order)
        x10 = float(min(9, nord) * 10**12 + cn(sur, 999) * 10**9 + cn(xfee[0] * 100, 999) * 10**6
                    + cn(xfee[1] * 100, 999) * 10**3 + cn(xfee[2] * 100, 999))
        return [x1, x2, x3, x4, x5, x6, x7, x8, x9, x10]
