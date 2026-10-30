"""Offline checks of the single-stock 0DTE engine (qc/stock_0dte/sim.py + simx.py, used by qc/stock_0dte/main.py on
QuantConnect) on synthetic days of Black-Scholes quotes: selection, fills, triggers, flatten / hold settlement, the
open-interest structures, the pin ratio fly, the breakout, the overnight arm, the FLAT15 sensitivity, share delivery
and export packing run unchanged; scipy.special is stubbed when the local venv lacks it."""
from __future__ import annotations

import importlib
import math
import sys
import types
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load_engine():
    if "scipy.special" not in sys.modules:
        try:
            import scipy.special  # noqa: F401
        except ImportError:       # the QuantConnect image has scipy; the local venv may not
            sp = types.ModuleType("scipy")
            sps = types.ModuleType("scipy.special")
            erf = np.vectorize(math.erf, otypes=[float])
            sps.ndtr = lambda x: 0.5 * (1.0 + erf(np.asarray(x, float) / math.sqrt(2.0)))
            sp.special = sps
            sys.modules.setdefault("scipy", sp)
            sys.modules["scipy.special"] = sps
    qc_dir = str(ROOT / "qc" / "stock_0dte")
    if qc_dir not in sys.path:
        sys.path.insert(0, qc_dir)        # the engine files import each other by name, as on QuantConnect
    z = importlib.import_module("sim")
    x = importlib.import_module("simx")
    z.configure()
    z.configure_day(True)
    return z, x


Z, X = _load_engine()
M = 390
SIG = 0.02                        # daily sigma of the synthetic stock
DAY = date(2024, 8, 9)


def _bs(S, K, s, put):
    return Z.bs(S, K, np.maximum(s, 1e-6), put)


def synthetic_day(path, step=1.0, lo=80.0, hi=120.0, sig=SIG, extra_days=0.0):
    """Strikes lo..hi; every minute a two-sided quote around the Black-Scholes mid, one or two cents wide.
    extra_days adds that much time to expiry (the overnight arm's entry day)."""
    K = np.arange(lo, hi + 1e-9, step)
    nk = len(K)
    B = np.zeros((2, nk, M))
    A = np.zeros((2, nk, M))
    for m in range(M):
        s = sig * math.sqrt(max(M - 1 - m, 0.5) / M + extra_days)
        for r in (0, 1):
            mid = _bs(path[m], K, s, r == 0)
            half = np.where(mid > 0.5, 0.01, 0.005)
            b = np.maximum(0.0, np.floor((mid - half) * 100 + 1e-9) / 100)
            a = np.maximum(0.01, np.ceil((mid + half) * 100 - 1e-9) / 100)
            B[r, :, m], A[r, :, m] = b, a
    AGE = np.zeros((2, nk, M), int)
    return K, B, A, AGE


def make_algo(path, oi=None, **kw):
    K, B, A, AGE = synthetic_day(path, **kw)
    OI = np.zeros((2, len(K))) if oi is None else oi(K)
    algo = X.DaySimX(lambda D, contracts, S, sig, extra, sel_from: (K, B, A, AGE, OI, 2 * len(K)))
    algo.M = M
    algo.day = DAY
    return algo, (K, B, A, AGE, OI)


def run(algo, path, kmp=None, on=None):
    return algo.simulate_day(DAY, path, [], SIG, kmp, float(path[0]), on)


def unpack(row):
    def two(x):
        x = int(round(x))
        return x // 10**7, x % 10**7

    c, den = two(row[0])
    h, s1 = two(row[1])
    s2, tp = two(row[2])
    imp0, dc = two(row[3])
    fe0, fef = two(row[7])
    off = Z.OFF
    x7 = int(round(row[6]))
    return {"cash0": (c - off) / Z.U, "den": den / Z.U, "pnl": [(v - off) / Z.U for v in (h, s1, s2, tp)],
            "imp0": imp0 / Z.U, "d_close": (dc - off) / Z.U, "fee0": fe0 / Z.U, "fee_flat": fef / Z.U,
            "xmin": [(int(row[4]) // 10**10) % 1000, (int(row[4]) // 10**7) % 1000, (int(row[4]) // 10**4) % 1000],
            "stuck": int(row[4]) // 10**13, "dir": int(round(row[6])) // 10**14,
            "spr0": (int(row[4]) % 10**4) / 100, "nsh_hold": (x7 // 10**13) % 10 - 2, "nsh_flat": (x7 // 10**12) % 10 - 2,
            "dl": (x7 // 10**9) % 1000, "wbp": (x7 // 10**5) % 10**4, "ne": (x7 // 10**4) % 10,
            "d15": ((int(round(row[8])) % 10**7) - off) / Z.U, "s_fill": (int(round(row[8])) // 10**7) / 100,
            "xfee": [((int(round(row[9])) // 10**6) % 1000) / 100, ((int(round(row[9])) // 10**3) % 1000) / 100,
                     (int(round(row[9])) % 1000) / 100],
            "nord": int(round(row[9])) // 10**12, "sur": ((int(round(row[9])) // 10**9) % 1000) / 100}


def row_of(rows, shape, entry=0):
    return rows[entry * Z.NS + Z.SHAPES.index(shape)]


CRASH = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 95.0, 200), np.full(M - 260, 95.0)])
RALLY = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 105.0, 200), np.full(M - 260, 105.0)])


def test_shape_catalogue():
    assert Z.NS == 81 and len(set(Z.SHAPES)) == Z.NS
    assert len(Z.FAM) == len(Z.KIND) == 22 and Z.NB == Z.NE + 1


def test_rows_cover_every_shape_and_block():
    path = np.full(M, 100.0)
    algo, _ = make_algo(path, oi=lambda K: np.vstack([np.where(K == 99.0, 5000.0, 10.0), np.where(K == 103.0, 9000.0, 10.0)]))
    rows, strad, pins, nload = run(algo, path, 101.0)
    assert len(rows) == Z.NS * Z.NB
    assert all(len(r) in (1, 10) for r in rows)
    assert all(r[0] < 0 for r in rows if len(r) == 1)
    assert sum(len(r) == 10 for r in rows[:Z.NS * Z.NE]) > 0.6 * Z.NS * Z.NE
    assert all(r == [-8.0] for r in rows[Z.NS * Z.NE:])                # no overnight positions were opened
    assert all(x > 0 for x in strad)
    assert all(p == 99.0 for p in pins)                  # 99 has the most open interest within max(2%, 1 sd)
    orb = Z.SHAPES.index((20, 0.50, 0.0))
    assert all(rows[e * Z.NS + orb] == [-9.0] for e in range(1, Z.NE))  # the breakout only has a 09:45-block row


def test_ticks_by_class():
    Z.configure_day(False)
    try:
        assert float(Z.tick_v(1.0)) == 0.05 and float(Z.tick_v(3.2)) == 0.10 and Z.tick0() == 0.05
        assert abs(Z.DaySim.px_sell(1.00, 1.20, 0.25) - 1.05) < 1e-9 and abs(Z.DaySim.px_buy(1.00, 1.20, 0.25) - 1.15) < 1e-9
    finally:
        Z.configure_day(True)
    assert float(Z.tick_v(1.0)) == 0.01 and float(Z.tick_v(3.2)) == 0.05 and Z.tick0() == 0.01
    assert abs(Z.DaySim.px_sell(1.00, 1.04, 0.25) - 1.01) < 1e-12
    assert abs(Z.DaySim.px_sell(3.10, 3.30, 0.25) - 3.15) < 1e-9      # 0.05 grid at or above 3 dollars


def test_sane_quotes():
    assert bool(Z.sane(0.27, 0.50)) and bool(Z.sane(0.03, 0.15))        # real late-day single-stock quotes
    assert not bool(Z.sane(3.90, 7.80)) and not bool(Z.sane(1.50, 5.75))
    assert not bool(Z.sane(0.50, 0.40)) and not bool(Z.sane(0.0, 0.0))


def test_max_pain_and_shares():
    k = [95.0, 100.0, 105.0]
    assert Z.max_pain(k, [0, 0, 0], [0, 0, 0]) is None
    assert Z.max_pain(k, [0, 100, 100], [100, 100, 0]) == 100.0       # holders are paid least at 100 (1,000 vs 1,500)
    assert Z.max_pain(k, [0, 0, 500], [0, 0, 0]) == 105.0
    K = np.array([95.0, 100.0, 105.0])
    assert Z.shares_at([(0, 1, -1), (0, 0, 1)], K, 97.0) == 1           # short 100 put assigned: receive shares
    assert Z.shares_at([(0, 1, -1), (0, 0, 1)], K, 90.0) == 0           # both puts exercised: net zero
    assert Z.shares_at([(1, 2, -1)], K, 110.0) == -1                    # short call assigned: deliver shares
    assert Z.shares_at([(1, 2, -1)], K, 105.005) == 0                   # less than a cent in the money: not exercised
    assert Z.shares_at([(1, 0, 1), (1, 1, -3), (1, 2, 2)], K, 110.0) == 0


def test_flat_day_short_put_spread_keeps_credit_and_matches_hand_calculation():
    path = np.full(M, 100.0)
    algo, (K, B, A, AGE, OI) = make_algo(path)
    rows, *_ = run(algo, path)
    shape = (0, 0.16, 0.02)
    u = unpack(row_of(rows, shape))
    mF = 15
    sel, info = algo.select(path, K, B, A, 0.5 * (B + A), A - B, AGE, OI, mF - 1, SIG, None)
    legs, code = algo.legs_for(shape, sel, K, B, A, AGE, mF - 1, info)
    (r, ks, _), (_, kl, _) = legs
    sell = Z.DaySim.px_sell(B[r, ks, mF], A[r, ks, mF], Z.F_ENTRY)
    buy = Z.DaySim.px_buy(B[r, kl, mF], A[r, kl, mF], Z.F_ENTRY)
    assert abs(u["cash0"] - (sell - buy)) < 1e-3
    assert 0 < u["pnl"][0] <= u["cash0"]
    assert u["d_close"] >= -1e-9
    assert u["nsh_hold"] == 0 and u["nsh_flat"] == 0 and u["d15"] == 0.0


def test_crash_day_stops_and_caps_spread_loss_and_naked_put_is_assigned():
    algo, _ = make_algo(CRASH)
    rows, *_ = run(algo, CRASH)
    u = unpack(row_of(rows, (0, 0.16, 0.02)))
    assert u["xmin"][0] > 15 and u["xmin"][1] >= u["xmin"][0]
    assert u["pnl"][1] > u["pnl"][0]
    assert -u["den"] - 0.06 <= u["pnl"][0] < -0.8 * u["den"]
    un = unpack(row_of(rows, (4, 0.16, 0.0)))
    assert un["pnl"][0] < u["pnl"][0]
    assert un["nsh_hold"] == 1 and un["nsh_flat"] == 0                  # held: assigned 100 shares; flattened: none
    # FLAT15: the naked put is deep in the money at 15:00, so it is closed then; on this flat afternoon the 15:00 and
    # 15:50 quotes are the same intrinsic value, so the sensitivity is (about) zero
    assert algo.cnt["f15"] > 0 and abs(un["d15"]) < 0.05


def test_long_put_gains_on_the_crash_and_long_call_loses():
    algo, _ = make_algo(CRASH)
    rows, *_ = run(algo, CRASH)
    lp = unpack(row_of(rows, (13, 0.25, 0.0)))
    lc = unpack(row_of(rows, (12, 0.25, 0.0)))
    assert lp["pnl"][0] > 0 > lc["pnl"][0]
    assert lc["pnl"][0] >= -lc["den"] - 1e-9                            # a long option loses at most what it cost
    assert lp["ne"] == 1 and 150 <= lp["dl"] <= 350


def test_debit_vertical_is_bounded_by_width_and_debit():
    algo, (K, *_rest) = make_algo(RALLY)
    rows, *_ = run(algo, RALLY)
    u = unpack(row_of(rows, (15, 0.50, 0.0)))                          # call debit vertical on a rally
    width = u["wbp"] * 100.0 / 1e4
    assert u["pnl"][0] > 0
    assert u["pnl"][0] <= width + 1e-6 and u["pnl"][0] >= -u["den"] - 0.03
    d = unpack(row_of(rows, (16, 0.50, 0.0)))                          # put debit vertical loses at most the debit
    assert -d["den"] - 0.03 <= d["pnl"][0] < 0


def test_pin_and_max_pain_flies_centre_on_their_strikes():
    path = np.full(M, 100.4)
    oi = lambda K: np.vstack([np.where(K == 99.0, 8000.0, 50.0), np.where(K == 101.0, 3000.0, 50.0)])
    algo, (K, B, A, AGE, OI) = make_algo(path, oi=oi)
    sel, info = algo.select(path, K, B, A, 0.5 * (B + A), A - B, AGE, OI, 14, SIG, 103.0)
    assert K[info["kpin"]] == 99.0 and K[info["kmpi"]] == 103.0
    legs, code = algo.legs_for((17, 0.50, 0.0), sel, K, B, A, AGE, 14, info)
    assert code == 0 and K[legs[0][1]] == 99.0 and K[legs[2][1]] == 99.0 and K[legs[1][1]] == 98.0 and K[legs[3][1]] == 100.0
    legs, code = algo.legs_for((18, 0.50, 0.02), sel, K, B, A, AGE, 14, info)
    assert code == 0 and K[legs[0][1]] == 103.0 and K[legs[1][1]] == 101.0 and K[legs[3][1]] == 105.0
    legs, code = algo.legs_for((19, 0.50, 0.0), sel, K, B, A, AGE, 14, info)
    assert code == 0 and legs[0][0] == 1 and K[legs[0][1]] == 103.0 and legs[0][2] == -1 and legs[1][2] == 1
    sel, info = algo.select(path, K, B, A, 0.5 * (B + A), A - B, AGE, OI, 14, SIG, 100.0)
    assert algo.legs_for((19, 0.50, 0.0), sel, K, B, A, AGE, 14, info)[0] is None
    sel, info = algo.select(path, K, B, A, 0.5 * (B + A), A - B, AGE, np.zeros_like(OI), 14, SIG, None)
    assert info["kpin"] == -1 and info["kmpi"] == -1


def test_pin_ratio_fly_legs_fees_and_payoff():
    path = np.full(M, 100.05)                                          # pinned next to the 100 strike all day
    algo, (K, B, A, AGE, OI) = make_algo(path)
    shape = (21, 0.50, 0.0)
    sel, info = algo.select(path, K, B, A, 0.5 * (B + A), A - B, AGE, OI, 14, SIG, None)
    legs, code = algo.legs_for(shape, sel, K, B, A, AGE, 14, info)
    assert code == 0 and [(r, K[k], q) for r, k, q in legs] == [(1, 99.0, 1), (1, 100.0, -3), (1, 101.0, 2)]
    cash0, fee0, imp0, spr0 = algo.entry_fill(legs, B, A, AGE, 15)
    assert abs(fee0 - 6 * (0.65 + Z.OTHER_FEE) / 100) < 1e-9 or fee0 > 5 * 0.30 / 100   # six contracts are charged
    rows, *_ = run(algo, path)
    u = unpack(row_of(rows, shape, 2))
    assert u["ne"] == 6 and u["pnl"][0] > 0                            # six contracts; the pin pays the fly
    mid = np.full(M, 100.45)                                           # half-way between strikes: no trade
    algo2, _ = make_algo(mid)
    rows2, *_ = run(algo2, mid)
    assert row_of(rows2, shape, 2) == [-1.0]


def test_opening_range_breakout():
    path = np.concatenate([np.full(10, 100.0), np.linspace(100.0, 102.5, 120), np.full(M - 130, 102.5)])
    algo, _ = make_algo(path)
    rows, *_ = run(algo, path)
    u = unpack(row_of(rows, (20, 0.50, 0.0)))
    assert u["ne"] == 1 and u["pnl"][0] > 0 and u["pnl"] == [u["pnl"][0]] * 4
    assert 10 < u["xmin"][0] <= Z.mindex(15, 30) + 1                    # exited at +100% or by the 15:30 time stop
    flat = np.full(M, 100.0)
    algo2, _ = make_algo(flat)
    rows2, *_ = run(algo2, flat)
    assert row_of(rows2, (20, 0.50, 0.0)) == [-1.0]                     # no breakout, no trade


def test_overnight_arm_enters_at_the_previous_close_and_is_managed_on_expiry_day():
    prev = np.full(M, 100.0)
    algo_prev, _ = make_algo(prev, extra_days=1.0)
    states, strad = algo_prev.on_enter(DAY, prev, [], SIG, None)
    assert len(states) == Z.NS and strad > 0
    pcs = Z.SHAPES.index((0, 0.16, 0.02))
    st = states[pcs]
    assert isinstance(st, dict) and st["cash0"] > 0 and st["den"] > 0
    assert states[Z.SHAPES.index((11, 0.50, 0.0))] == 9 and states[Z.SHAPES.index((20, 0.50, 0.0))] == 9
    algo_n, _ = make_algo(CRASH)                                       # the expiry day gaps down and keeps falling
    rows, *_ = run(algo_n, CRASH, on=states)
    u = unpack(rows[Z.NE * Z.NS + pcs])
    assert abs(u["cash0"] - st["cash0"]) < 1e-3 and abs(u["den"] - st["den"]) < 1e-3
    assert u["pnl"][0] < 0 and u["xmin"][0] >= 1                       # the stop can fire from the first bar
    algo_q, _ = make_algo(np.full(M, 100.0))
    rows_q, *_ = run(algo_q, np.full(M, 100.0), on=states)
    assert unpack(rows_q[Z.NE * Z.NS + pcs])["pnl"][0] > 0             # a quiet expiry day keeps the credit


def test_pack_roundtrip_is_exact():
    algo, _ = make_algo(np.full(M, 100.0))
    info = {"dlk": {(0, 3): 0.161}, "K": np.array([95.0, 96.0, 97.0, 98.0]), "s_impl": 100.0}
    legs = [(0, 3, -1), (0, 2, 1)]
    row = algo.pack(0.432, 0.568, [0.123, -0.234, -0.345, 0.012], 0.007, -0.004, [17, 200, 0],
                    [0.01, 0.0, 0.0], legs, 2, [2, 2, 0], info, (0, 0.16, 0.0), 0.014, 0.007, 0.03, 1, -1, -0.021,
                    612.37, [0.014, 0.0, 0.0065], 2, 0.35, 1)
    u = unpack(row)
    assert u["cash0"] == 0.432 and u["den"] == 0.568
    assert u["pnl"] == [0.123, -0.234, -0.345, 0.012]
    assert u["imp0"] == 0.007 and u["d_close"] == -0.004 and u["xmin"] == [17, 200, 0]
    assert u["fee0"] == 0.014 and u["fee_flat"] == 0.007 and u["spr0"] == 0.03 and u["d15"] == -0.021
    assert u["nsh_hold"] == 1 and u["nsh_flat"] == -1 and u["dl"] == 161 and u["wbp"] == 100 and u["ne"] == 2
    assert u["s_fill"] == 612.37 and u["xfee"] == [1.40, 0.0, 0.65]
    assert u["nord"] == 2 and u["sur"] == 0.35 and u["stuck"] == 1 and u["dir"] == 0
    assert all(float(x).is_integer() and x < 2**53 for x in row)
    row = algo.pack(1.0, 5000.0, [0.0] * 4, 0.0, 0.0, [0, 0, 0], [0.0] * 3, [(1, 2, -1)], 0, [0, 0, 0],
                    {"dlk": {}, "K": np.array([1.0, 2.0, 3.0]), "s_impl": 5000.0}, (5, 0.10, 0.0), 0.0, 0.0, 0.0, 0, 0, 0.0,
                    5000.0, [0.0] * 3, 0, 0.0, 0)
    assert unpack(row)["den"] == 5000.0


def test_wider_strike_grid_and_late_move_leaves_no_silent_assignment():
    path = np.concatenate([np.full(370, 100.0), np.full(M - 370, 100.8)])
    algo, (K, B, A, AGE, OI) = make_algo(path, step=2.5)
    rows, *_ = run(algo, path)
    u = unpack(row_of(rows, (5, 0.50, 0.0)))                            # naked ATM call
    assert u["nsh_flat"] == 0                                          # closed at 15:50, nothing left to assign


def test_worthless_far_wing_with_a_wide_quote_does_not_block_the_flatten():
    path = np.full(M, 100.0)
    algo, (K, B, A, AGE, OI) = make_algo(path)
    k0, kp, kc = (int(np.where(K == x)[0][0]) for x in (100.0, 98.0, 102.0))
    legs = [(0, k0, -1), (0, kp, 1), (1, k0, -1), (1, kc, 1)]
    mflat = M - 1 - Z.FLAT_BEFORE
    B2, A2 = B.copy(), A.copy()
    for r, k in ((0, kp), (1, kc)):
        B2[r, k, mflat:], A2[r, k, mflat:] = 0.01, 0.30            # worth nothing, and a wide quote
    cash, fees, n, rest, nord, sur, stuck = algo.flatten(legs, K, B2, A2, AGE, path, mflat, 100.0, pairs=True)
    assert n == 2 and sorted(rest) == sorted([(0, kp, 1), (1, kc, 1)]) and stuck == 0
    assert nord == 2 and abs(sur - 2 * (1.0 - 0.65)) < 1e-9          # two one-leg orders, each below the $1 minimum
    assert Z.shares_at(rest, K, 100.0) == 0


def test_export_stream_roundtrip_through_the_decoder():
    """A day record and one name block laid out as qc/stock_0dte/main.py queues them decode back exactly."""
    from quant.strategies import stock_0dte as Q

    prev = np.full(M, 100.0)
    algo_prev, _ = make_algo(prev, extra_days=1.0)
    states, strad_on = algo_prev.on_enter(DAY, prev, [], SIG, None)
    algo, _ = make_algo(CRASH, oi=lambda K: np.vstack([np.where(K == 99.0, 800.0, 5.0), np.where(K == 101.0, 700.0, 5.0)]))
    rows, strad, pins, nload = run(algo, CRASH, 101.0, states)
    U = Z.U
    ymd = (DAY.year % 100) * 10_000 + DAY.month * 100 + DAY.day
    s_entry = [float(CRASH[Z.mindex(hh, mm) + 1]) for hh, mm in Z.ENTRY]
    head = [900_000_000_000_000.0 + ymd * 1000 + 7, float(0 * 10**12 + 0 * 10**11 + 84 * 10**6 + nload * 10**2 + 1 * 10 + 1),
            round(100.02 * U), round(95.0 * U), round(100.5 * U), round(0.02 * 1e6), round(101.0 * U), 3 * 10**6 + 390, 1850.0]
    head += [float(round(x * U)) for x in s_entry] + [float(round(x * U)) for x in strad] + [float(round(x * U)) for x in pins]
    head += [4.0, float(round(100.0 * U)), float(round(strad_on * U)), 1720.0, float(len(rows))]
    assert len(head) == Q.HEADER
    vals = [950_000_000_000_000.0 + ymd * 100 + 1] + head + [v for r in rows for v in r] + [920_000_000_000_000.0]
    names, recs, days = Q.parse_stream([int(v) for v in vals], {7: "TSLA"})
    assert len(days) == 1 and days.loc[0, "n_names"] == 1
    assert len(names) == 1 and names.loc[0, "name"] == "TSLA" and names.loc[0, "rank"] == 3 and names.loc[0, "rank_on"] == 4
    assert names.loc[0, "S_close"] == 95.0 and abs(names.loc[0, "sig"] - 0.02) < 1e-9 and names.loc[0, "kmp"] == 101.0
    assert names.loc[0, "vix_prev"] == 18.5 and names.loc[0, "pin_e0"] == pins[0] and names.loc[0, "S_on"] == 100.0
    assert names.loc[0, "vix_on"] == 17.2
    assert len(recs) == Z.NS * Z.NB
    si = Z.SHAPES.index((4, 0.16, 0.0))
    got = recs[(recs["entry"] == 0) & (recs["shape"] == si)].iloc[0]
    want = unpack(rows[si])
    assert got["code"] == 0 and abs(got["pnl_H"] - want["pnl"][0]) < 1e-12 and got["nsh_hold"] == want["nsh_hold"]
    assert abs(got["d15"] - want["d15"]) < 1e-12
    P = Q.build_panel(names, recs)
    assert P.opened[si, 0, 0] and abs(P.pnl[si, 0, 0, 0] - want["pnl"][0]) < 1e-12
    assert abs(P.hold[si, 0, 0, 0] - (want["pnl"][0] + want["d_close"])) < 1e-9
    pcs = Z.SHAPES.index((0, 0.16, 0.02))
    assert P.opened[pcs, Z.NE, 0]                                      # the overnight block decodes into entry 5


def test_next_open_records_attach_to_their_name_day():
    from quant.strategies import stock_0dte as Q
    ymd = 240809
    head = [900_000_000_000_000 + ymd * 1000 + 3, 0, 100_000, 99_000, 100_500, 20_000, 0, 10**6 + 390, 1850]
    head += [0] * 19 + [0]
    vals = [950_000_000_000_000 + ymd * 100 + 1] + head + [970_000_000_000_000 + ymd * 1000 + 3, 98_500, 0, 99_010]
    names, recs, days = Q.parse_stream(vals, {3: "AMD"})
    assert names.loc[0, "next_open"] == 98.5 and names.loc[0, "split_next"] == 0 and len(recs) == 0
    assert names.loc[0, "close_official"] == 99.01


def test_overnight_exit_at_0945_and_no_trigger_before_the_first_quote():
    prev = np.full(M, 100.0)
    algo_prev, _ = make_algo(prev, extra_days=1.0)
    states, _ = algo_prev.on_enter(DAY, prev, [], SIG, None)
    pcs = Z.SHAPES.index((0, 0.16, 0.02))
    path = np.full(M, 100.0)
    algo, (K, B, A, AGE, OI) = make_algo(path)
    AGE[:, :, :5] = 10_000                                             # no quotes before 09:36
    B[:, :, :5], A[:, :, :5] = np.nan, np.nan
    rows, *_ = run(algo, path, on=states)
    u = unpack(rows[Z.NE * Z.NS + pcs])
    assert u["xmin"][1] == 0                                           # no stop from a leg that had no quote yet
    assert u["xmin"][0] == Z.mindex(9, 45) + 1                         # the overnight-only slot (M1) exits at 09:45
    assert -u["den"] < u["pnl"][1] < u["cash0"]                        # bought back at 09:45: bounded by the credit


def test_ratio_fly_risk_base_with_unequal_steps():
    K = np.array([97.5, 100.0, 105.0])
    legs = [(1, 0, 1), (1, 1, -3), (1, 2, 2)]
    # above 105 the fly pays 2.5 - 2 x 5 = -7.5: the risk base is 7.5 minus the credit, not one step
    assert abs(X.DaySimX.risk_base(21, legs, K, 0.10, 100.0) - 7.40) < 1e-9
    K2 = np.array([99.0, 100.0, 101.0])
    assert abs(X.DaySimX.risk_base(21, legs, K2, -0.05, 100.0) - 1.05) < 1e-9


def test_overnight_position_without_quotes_settles_at_intrinsic_and_counts_as_stuck():
    prev = np.full(M, 100.0)
    algo_prev, _ = make_algo(prev, extra_days=1.0)
    states, _ = algo_prev.on_enter(DAY, prev, [], SIG, None)
    pcs = Z.SHAPES.index((0, 0.16, 0.02))
    st = dict(states[pcs])
    st["legs"] = [(r, k + 1000.0, q) for r, k, q in st["legs"]]          # strikes the expiry day does not quote
    on = list(states)
    on[pcs] = st
    algo, _ = make_algo(CRASH)
    rows, *_ = run(algo, CRASH, on=on)
    u = unpack(rows[Z.NE * Z.NS + pcs])
    assert u["stuck"] == 2 and u["pnl"] == [u["pnl"][0]] * 4
    net = st["cash0"] - st["fee0"]
    width = abs(st["legs"][0][1] - st["legs"][1][1])
    assert abs(u["pnl"][0] - (net - width)) < 2e-3                      # both puts deep in the money: the full width lost


def test_direction_digit_for_max_pain_and_pin_trades():
    path = np.full(M, 100.4)
    oi = lambda K: np.vstack([np.where(K == 99.0, 8000.0, 50.0), np.where(K == 101.0, 3000.0, 50.0)])
    algo, _ = make_algo(path, oi=oi)
    rows, *_ = run(algo, path, kmp=103.0)
    assert unpack(row_of(rows, (19, 0.50, 0.0), 2))["dir"] == 1          # max pain above: a call vertical, a rise
    assert unpack(row_of(rows, (17, 0.50, 0.0), 2))["dir"] == 2          # pin at 99, below the stock: a fall
    assert unpack(row_of(rows, (0, 0.16, 0.02), 2))["dir"] == 0


def test_floor_only_quote_fills_at_the_touch():
    algo, _ = make_algo(np.full(M, 100.0))
    K = np.array([100.0])
    B = np.zeros((2, 1, M))
    A = np.zeros((2, 1, M))
    AGE = np.zeros((2, 1, M), int)
    B[1, 0, :], A[1, 0, :] = 0.05, 0.25                                  # passes only the $0.25 exit floor
    res = algo.close_cash([(1, 0, 1)], B, A, AGE, 100, Z.F_TP, Z.AGE_FILL)
    assert res is not None and abs(res[0] - 0.05) < 1e-12               # sold at the bid, not mid - 0.25 x spread
    B[1, 0, :], A[1, 0, :] = 0.10, 0.14                                  # a normal quote keeps F = 0.25
    res = algo.close_cash([(1, 0, 1)], B, A, AGE, 100, Z.F_TP, Z.AGE_FILL)
    assert abs(res[0] - 0.11) < 1e-12
