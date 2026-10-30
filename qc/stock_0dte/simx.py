"""Day loop and export of the single-stock 0DTE grid (docs/research_ledger_stock_0dte.md, 0.3), the second engine file
next to sim.py (QuantConnect caps one file at 32,000 characters): the five same-day entries, the overnight arm (enter
at the previous close, manage on the expiry day), the opening-range breakout, the delta-hedged straddle and the
9-integer row packing. main.py builds one DaySimX per backtest; tests/test_stock_0dte_engine.py runs it locally."""
import math
import time as _time

import numpy as np
from scipy.special import ndtr

import sim as Z
from optmath import implied_s, shares_at

ORB_RANGE = 5                                  # opening range: the open and the bars ending 09:31..09:35
ORB_LAST = Z.mindex(12, 0)                     # no breakout entry after 12:00
ORB_STOP = Z.mindex(15, 30)                    # time stop


class DaySimX(Z.DaySim):
    """One instance per backtest. simulate_day() turns one name-day's minute quotes into NB x NS export rows;
    on_enter() opens the overnight arm at the previous session's close for the next expiry."""

    def simulate_day(self, D, S, contracts, sig, kmp, S_open, on_state=None):
        """Rows for every (block, shape): blocks 0..4 the same-day entries, block 5 the overnight positions opened
        at the previous close (on_state from on_enter, None if there were none); plus the at-the-money straddle mid
        and the open-interest pin strike at each entry, and the number of contracts loaded."""
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
        rows, strad, pins = [], [0.0] * Z.NE, [0.0] * Z.NE
        for ei, (hh, mm) in enumerate(Z.ENTRY):
            mS = Z.mindex(hh, mm)
            mF = mS + 1
            if mF >= mflat - 2:
                rows.extend([[-9.0]] * Z.NS)
                continue
            sel, info = self.select(S, K, B, A, MID, SPR, AGE, OI, mS, sig, kmp)
            strad[ei] = info.get("strad", 0.0)
            pins[ei] = float(K[info["kpin"]]) if info["kpin"] >= 0 else 0.0
            for si, shape in enumerate(Z.SHAPES):
                if shape[0] == 20:
                    rows.append(self.run_orb(si, shape, K, B, A, MID, SPR, AGE, OI, S, S_open, mflat, S_close, sig, kmp)
                                if ei == 0 else [-9.0])
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

    # ------------------------------------------------------------------ overnight arm
    def on_enter(self, D, S, contracts, sig, kmp):
        """At the previous session D's close (ON_BEFORE minutes before it), open every shape on the contracts that
        expire on the next session. Returns per shape a dict (legs by strike value, entry cash, fees, risk base, the
        stock price and parity price at the fill, the short leg's delta) or an int code, and the straddle mid."""
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
                        "dl": info["dlk"].get(sh[0], 0.0)})
        return out, info.get("strad", 0.0)

    def resume_overnight(self, on_state, K, B, A, MID, AGE, S, mflat, S_close):
        """Block 5: manage each overnight position on its expiry day from the first bar (stops, the flatten, hold)."""
        if on_state is None:
            return [[-8.0]] * Z.NS
        rows = []
        kix = {float(k): i for i, k in enumerate(K)}
        for si, st in enumerate(on_state):
            if not isinstance(st, dict):
                rows.append([-float(st)])
                continue
            try:
                legs = [(r, kix[k], qn) for r, k, qn in st["legs"]]
            except KeyError:
                rows.append([-4.0])                            # a leg's strike has no quotes on the expiry day
                continue
            shape = Z.SHAPES[si]
            info = {"K": K, "s_impl": st["s_impl"], "dlk": {}}
            sh = [(r, k) for r, k, qn in legs if qn < 0] or [(r, k) for r, k, qn in legs]
            info["dlk"][sh[0]] = st["dl"]
            self.cnt["open"] += 1
            rows.append(self.manage(si, shape, legs, K, B, A, MID, AGE, S, -1, mflat, S_close, info, st["cash0"],
                                    st["fee0"], st["imp0"], st["spr0"], st["S0"], st["den"]))
        return rows

    # ------------------------------------------------------------------ opening-range breakout
    def run_orb(self, si, shape, K, B, A, MID, SPR, AGE, OI, S, S_open, mflat, S_close, sig, kmp):
        """Range = the open and the closes of the first five bars. The first close outside it before 12:00 buys the
        at-the-money call (above) or put (below) one minute later; exit at +100% (sold at F_TP) or -50% of the debit
        (mid, sold at F_STOP), else at 15:30 (F_FLAT); the same value fills all four management slots."""
        rng = [S_open or S[0]] + [float(x) for x in S[:ORB_RANGE]]
        hi, lo = max(rng), min(rng)
        mS, r = None, None
        for m in range(ORB_RANGE, min(ORB_LAST, mflat - 3) + 1):
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
        if not self.buyable(r, k0, A, AGE, mS):
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
        ok = (AGE[r, k0] <= Z.AGE_MARK) | (np.nan_to_num(B[r, k0]) < Z.tick0())
        live = np.zeros(self.M, bool)
        live[mF + 1:ORB_STOP] = True
        hit = live & ok & ((np.nan_to_num(Vx, nan=-1e9) >= 2 * debit) | (V <= 0.5 * debit))
        m_exit, F = (int(np.argmax(hit)) + 1, None) if hit.any() else (ORB_STOP, Z.F_FLAT)
        pnl, xm, ximp, nx, rest, xfee = None, 0, 0.0, 0, legs, 0.0
        for m in range(m_exit, mflat):
            FF = F if F is not None else (Z.F_TP if Vx[m - 1] >= 2 * debit else Z.F_STOP)
            res = self.close_cash(legs, B, A, AGE, m, FF, Z.AGE_FILL)
            if res is None or res[4]:
                continue
            c, fe, im, nn, held = res
            pnl, xm, ximp, nx, rest, xfee = cash0 - fee0 + c - fe, m + 1, im, nn, [], fe
            break
        if pnl is None:                                        # never sold: the 15:50 flatten / intrinsic
            fcash, ffee, fn, rest = self.flatten(legs, K, B, A, AGE, S, mflat, S_close)
            pnl, nx, xfee = cash0 - fee0 + fcash - ffee, fn, ffee
        # slots: exit minute, entry (fill) minute, direction (1 call, 2 put); the exit order's fee in the first slot
        return self.pack(cash0, debit, [pnl] * 4, imp0, 0.0, [xm, mF + 1, 1 if r == 1 else 2], [ximp] * 3, legs, nx,
                         [nx] * 3, info, shape, fee0, 0.0, spr0, 0, shares_at(rest, K, S_close), 0.0, float(S[mF]),
                         [xfee, 0.0, 0.0])

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
        fcash, ffee, fn, rest = self.flatten(legs, K, B, A, AGE, S, mflat, S_close)
        net = cash0 - fee0
        pnl_flat = net + fcash - ffee + hcash + h * Sf - abs(h) * cost * Sf
        pnl_hold = net + self.settle_value(legs, K, S_close) + hcash + h * S_close - abs(h) * cost * S_close
        return self.pack(cash0, den, [pnl_flat] * 4, imp0, pnl_hold - pnl_flat, [0, 0, 0], [0.0, 0.0, 0.0], legs, fn,
                         [0, 0, 0], info, shape, fee0, ffee, spr0, shares_at(legs, K, S_close),
                         shares_at(rest, K, S_close), 0.0, float(S[mF]), [0.0, 0.0, 0.0])

    # ------------------------------------------------------------------ export (10 exact integers per position)
    def pack(self, cash0, den, pnl, imp0, d_close, xmin, ximp, legs, fn, nx, info, shape, fee0, ffee, spr0,
             nsh_hold, nsh_flat, d15, s_fill, xfee):
        U, OFF = Z.U, Z.OFF

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
        if Z.KIND[f] in (0, 6) or f in Z.VERT_DEBIT:
            wbp = min(9999, int(round(1e4 * abs(info["K"][legs[0][1]] - info["K"][legs[1][1]]) / info["s_impl"])))
        nc = sum(abs(q) for _, _, q in legs)                   # contracts at entry (the ratio fly trades 6)
        nsh = (max(-2, min(2, nsh_hold)) + 2) * 10 + (max(-2, min(2, nsh_flat)) + 2)
        x7 = float(nsh * 10**12 + dl * 10**9 + wbp * 10**5 + min(9, nc) * 10**4 + min(9, fn) * 10**3
                   + min(9, nx[0]) * 100 + min(9, nx[1]) * 10 + min(9, nx[2]))
        x8 = float(min(9_999_999, int(round(fee0 * U))) * 10**7 + min(9_999_999, int(round(ffee * U))))
        x9 = float(min(99_999_999, max(0, int(round(s_fill * 100)))) * 10**7 + p7(d15))
        x10 = float(c4(xfee[0] * 100) * 10**8 + c4(xfee[1] * 100) * 10**4 + c4(xfee[2] * 100))   # exit fees, $ per order
        return [x1, x2, x3, x4, x5, x6, x7, x8, x9, x10]
