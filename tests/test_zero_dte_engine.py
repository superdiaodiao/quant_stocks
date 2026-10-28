"""Offline checks of the 0DTE day simulator (qc/zero_dte/sim.py, used by qc/zero_dte/main.py on QuantConnect)
on a synthetic day of Black-Scholes quotes: selection, fills, triggers, flatten / hold settlement and export packing
run unchanged; scipy.special is stubbed when the local venv lacks it."""
from __future__ import annotations

import importlib.util
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
    spec = importlib.util.spec_from_file_location("zero_dte_sim", ROOT / "qc" / "zero_dte" / "sim.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.configure("QQQ")
    return mod


Z = _load_engine()
M = 390
SIG = 0.012                       # daily sigma of the synthetic underlying


def _bs(S, K, s, put):
    return Z.bs(S, K, np.maximum(s, 1e-6), put)


def synthetic_day(path):
    """Strikes 95..105 step 0.5; every minute a two-sided quote around the Black-Scholes mid, one cent wide or two."""
    K = np.arange(95.0, 105.01, 0.5)
    nk = len(K)
    B = np.zeros((2, nk, M))
    A = np.zeros((2, nk, M))
    for m in range(M):
        s = SIG * math.sqrt(max(M - 1 - m, 0.5) / M)
        for r in (0, 1):
            mid = _bs(path[m], K, s, r == 0)
            half = np.where(mid > 0.5, 0.01, 0.005)
            b = np.maximum(0.0, np.floor((mid - half) * 100 + 1e-9) / 100)
            a = np.maximum(0.01, np.ceil((mid + half) * 100 - 1e-9) / 100)
            B[r, :, m], A[r, :, m] = b, a
    AGE = np.zeros((2, nk, M), int)
    return K, B, A, AGE


def make_algo(path):
    K, B, A, AGE = synthetic_day(path)
    algo = Z.DaySim(lambda D, contracts, S, vix: (K, B, A, AGE, 2 * len(K)))
    algo.M = M
    algo.day = date(2024, 8, 5)
    return algo, (K, B, A, AGE)


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
    return {"cash0": (c - off) / Z.U, "den": den / Z.U, "pnl": [(v - off) / Z.U for v in (h, s1, s2, tp)],
            "imp0": imp0 / Z.U, "d_close": (dc - off) / Z.U, "fee0": fe0 / Z.U, "fee_flat": fef / Z.U,
            "xmin": [int(row[4]) // 10**10, (int(row[4]) // 10**7) % 1000, (int(row[4]) // 10**4) % 1000],
            "spr0": (int(row[4]) % 10**4) / 100}


def test_rows_cover_every_shape_and_entry():
    path = np.full(M, 100.0)
    algo, _ = make_algo(path)
    rows, strad, nload = algo.simulate_day(date(2024, 8, 5), path, [], 18.0)
    assert len(rows) == Z.NS * Z.NE
    assert all(len(r) in (1, 8) for r in rows)
    assert all(r[0] < 0 for r in rows if len(r) == 1)
    assert sum(len(r) == 8 for r in rows) > 0.6 * len(rows)
    assert all(x > 0 for x in strad)


def test_flat_day_short_put_spread_keeps_credit_and_matches_hand_calculation():
    path = np.full(M, 100.0)
    algo, (K, B, A, AGE) = make_algo(path)
    rows, _, _ = algo.simulate_day(date(2024, 8, 5), path, [], 18.0)
    si = Z.SHAPES.index((0, 0.16, 0.005))
    row = rows[0 * Z.NS + si]                                     # 09:45 entry
    assert len(row) == 8
    u = unpack(row)
    mF = 15                                                       # bar ending 09:46
    sel, info = algo.select(path, K, B, A, 0.5 * (B + A), A - B, AGE, mF - 1, 18.0)
    legs, code = algo.legs_for(Z.SHAPES[si], sel, K, A, AGE, mF - 1, info)
    (r, ks, _), (_, kl, _) = legs
    sell = Z.DaySim.px_sell(B[r, ks, mF], A[r, ks, mF], Z.F_ENTRY)
    buy = Z.DaySim.px_buy(B[r, kl, mF], A[r, kl, mF], Z.F_ENTRY)
    assert abs(u["cash0"] - (sell - buy)) < 1e-4
    # spot never moves: both legs expire worthless; anything still worth >= 5 cents at 15:50 is bought back
    assert u["pnl"][0] <= u["cash0"]
    assert u["pnl"][0] > 0
    assert u["d_close"] >= -1e-9                                   # holding to the close cannot be worse here


def test_crash_day_triggers_stops_after_entry_and_caps_spread_loss():
    path = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 96.0, 200), np.full(M - 260, 96.0)])
    algo, _ = make_algo(path)
    rows, _, _ = algo.simulate_day(date(2024, 8, 5), path, [], 18.0)
    si = Z.SHAPES.index((0, 0.16, 0.01))
    u = unpack(rows[0 * Z.NS + si])
    assert u["xmin"][0] > 15 and u["xmin"][1] >= u["xmin"][0]      # SL1 fires after the fill, SL2 no earlier
    assert u["pnl"][1] > u["pnl"][0]                               # stopping out beats holding into the crash
    # a defined-risk loss never exceeds its max loss by more than the 15:50 exit costs (two spreads + two fees)
    assert -u["den"] - 0.06 <= u["pnl"][0] < -0.8 * u["den"]
    # the naked put loses far more than the spread when held
    sp = Z.SHAPES.index((4, 0.16, 0.0))
    un = unpack(rows[0 * Z.NS + sp])
    assert un["pnl"][0] < u["pnl"][0]


def test_long_straddle_gains_on_the_crash_and_the_short_straddle_loses():
    path = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 96.0, 200), np.full(M - 260, 96.0)])
    algo, _ = make_algo(path)
    rows, _, _ = algo.simulate_day(date(2024, 8, 5), path, [], 18.0)
    ld = unpack(rows[0 * Z.NS + Z.SHAPES.index((8, 0.50, 0.0))])
    sd = unpack(rows[0 * Z.NS + Z.SHAPES.index((7, 0.50, 0.0))])
    assert ld["pnl"][0] > 0 > sd["pnl"][0]


def test_pack_roundtrip_is_exact():
    algo, _ = make_algo(np.full(M, 100.0))
    info = {"dlk": {(0, 3): 0.161}, "K": np.array([95.0, 96.0, 97.0, 98.0]), "s_impl": 100.0}
    legs = [(0, 3, -1), (0, 2, 1)]
    row = algo.pack(0.4321, 0.5679, [0.1234, -0.2345, -0.3456, 0.0123], 0.0075, -0.0042, [17, 200, 0],
                    [0.01, 0.0, 0.0], legs, 2, [2, 2, 0], info, (0, 0.16, 0.0), 0.014, 0.007, 0.03)
    u = unpack(row)
    assert u["cash0"] == 0.4321 and u["den"] == 0.5679
    assert u["pnl"] == [0.1234, -0.2345, -0.3456, 0.0123]
    assert u["imp0"] == 0.0075 and u["d_close"] == -0.0042 and u["xmin"] == [17, 200, 0]
    assert u["fee0"] == 0.014 and u["fee_flat"] == 0.007 and u["spr0"] == 0.03
    assert all(float(x).is_integer() and x < 2**53 for x in row)


def test_fills_round_to_the_nearest_tick_inside_the_quote():
    # 1-tick market: both F give the touch; 3- and 4-tick markets: F = 0.25 improves on the touch, F = 0.5 does not
    assert Z.DaySim.px_sell(0.10, 0.11, 0.25) == 0.10 and Z.DaySim.px_buy(0.10, 0.11, 0.25) == 0.11
    assert abs(Z.DaySim.px_sell(0.10, 0.13, 0.25) - 0.11) < 1e-12 and Z.DaySim.px_sell(0.10, 0.13, 0.5) == 0.10
    assert abs(Z.DaySim.px_buy(0.10, 0.14, 0.25) - 0.13) < 1e-12 and Z.DaySim.px_buy(0.10, 0.14, 0.5) == 0.14
    assert Z.DaySim.px_sell(0.0, 0.01, 0.25) == 0.0                 # nothing to sell into a zero bid


def test_stale_long_leg_is_held_to_the_close_not_given_away():
    path = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 96.0, 200), np.full(M - 260, 96.0)])
    algo, (K, B, A, AGE) = make_algo(path)
    ks, kl = int(np.where(K == 98.0)[0][0]), int(np.where(K == 97.0)[0][0])
    legs = [(0, ks, -1), (0, kl, 1)]
    mflat = M - 1 - Z.FLAT_BEFORE
    fresh = algo.flatten(legs, K, B, A, AGE, path, mflat, 96.0)
    AGE2 = AGE.copy()
    AGE2[0, kl, mflat - 10:] = np.arange(10, 10 + M - (mflat - 10))   # the long 97P stops quoting before 15:50
    stale = algo.flatten(legs, K, B, A, AGE2, path, mflat, 96.0)
    # with fresh quotes the spread is closed near its width; with a stale wing the wing is settled at intrinsic
    assert abs(fresh[0] + 1.0) < 0.05
    assert abs(stale[0] - (-B[0, ks, mflat] - 0.0 + 1.0)) < 0.06 or stale[0] > -1.1


def test_far_wing_without_a_bid_does_not_switch_off_the_stop():
    path = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 96.0, 200), np.full(M - 260, 96.0)])
    algo, (K, B, A, AGE) = make_algo(path)
    si = Z.SHAPES.index((2, 0.16, 0.01))                            # iron condor
    base = unpack(algo.simulate_day(date(2024, 8, 5), path, [], 18.0)[0][si])
    # the call wing loses its bid after the drop and stops updating: it should not block the stop
    kc = int(np.where(K >= 102.0)[0][0])
    B2, AGE2 = B.copy(), AGE.copy()
    B2[1, kc:, 70:] = 0.0
    AGE2[1, kc:, 70:] = np.arange(0, M - 70)
    algo2 = Z.DaySim(lambda D, c, S, v: (K, B2, A, AGE2, 2 * len(K)))
    algo2.M, algo2.day = M, date(2024, 8, 5)
    u = unpack(algo2.simulate_day(date(2024, 8, 5), path, [], 18.0)[0][si])
    assert base["xmin"][0] > 0 and u["xmin"][0] > 0                 # SL1 still fires


def test_take_profit_is_measured_on_the_credit_actually_kept():
    path = np.full(M, 100.0)
    algo, _ = make_algo(path)
    rows, _, _ = algo.simulate_day(date(2024, 8, 5), path, [], 18.0)
    for si, shape in enumerate(Z.SHAPES):
        if Z.KIND[shape[0]] not in (0, 1) or len(rows[si]) != 8:
            continue
        u = unpack(rows[si])
        if u["xmin"][2]:
            # a take-profit exit keeps at least ~25% of the net credit after all costs, minus one tick of slippage
            net = u["cash0"] - u["fee0"]
            assert u["pnl"][3] >= 0.25 * net - 0.011, (si, u)


def test_in_the_money_wing_with_a_zero_bid_is_settled_not_abandoned():
    # 15:50 data glitch: the deep in-the-money long put shows bid 0; the spread must still lose at most its width
    path = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 96.0, 200), np.full(M - 260, 96.0)])
    algo, (K, B, A, AGE) = make_algo(path)
    ks, kl = int(np.where(K == 98.0)[0][0]), int(np.where(K == 97.5)[0][0])
    legs = [(0, ks, -1), (0, kl, 1)]
    mflat = M - 1 - Z.FLAT_BEFORE
    B2 = B.copy()
    B2[0, kl, mflat:] = 0.0
    cash, fees, n = algo.flatten(legs, K, B2, A, AGE, path, mflat, 96.0)
    assert cash >= -0.5 - 0.05                 # width 0.5 plus at most one exit spread


def test_flatten_does_not_trade_on_an_absurdly_wide_quote():
    # at 15:50 the in-the-money short put is quoted 3.90 / 7.80 for a few minutes: the flatten waits for a sane quote
    path = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 96.0, 200), np.full(M - 260, 96.0)])
    algo, (K, B, A, AGE) = make_algo(path)
    ks, kl = int(np.where(K == 100.0)[0][0]), int(np.where(K == 99.5)[0][0])
    legs = [(0, ks, -1), (0, kl, 1)]
    mflat = M - 1 - Z.FLAT_BEFORE
    A2 = A.copy()
    A2[0, ks, mflat:mflat + 3] = 7.80
    cash, fees, n = algo.flatten(legs, K, B, A2, AGE, path, mflat, 96.0)
    assert -0.56 <= cash <= -0.44                   # about the 0.5 width, not 7.80 - 3.49


def test_vertical_is_closed_as_a_combo_capped_at_its_width():
    # crash-day quotes: both put legs deep in the money with 1.5-dollar-wide (but sane) markets
    path = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 96.0, 200), np.full(M - 260, 96.0)])
    algo, (K, B, A, AGE) = make_algo(path)
    ks, kl = int(np.where(K == 100.0)[0][0]), int(np.where(K == 99.5)[0][0])
    legs = [(0, ks, -1), (0, kl, 1)]
    mflat = M - 1 - Z.FLAT_BEFORE
    B2, A2 = B.copy(), A.copy()
    B2[0, ks, mflat:], A2[0, ks, mflat:] = 3.25, 4.75
    B2[0, kl, mflat:], A2[0, kl, mflat:] = 2.75, 4.25
    cash, fees, n = algo.flatten(legs, K, B2, A2, AGE, path, mflat, 96.0, pairs=True)
    assert cash >= -0.5 - 1e-9 and n == 2               # pays at most the 0.5 width (fees separately)
    legs_cash = algo.flatten(legs, K, B2, A2, AGE, path, mflat, 96.0, pairs=False)[0]
    assert legs_cash < -1.5                              # legging out at the touch would have paid two spreads


def test_iron_fly_close_costs_at_most_one_wing():
    # S at the body at 15:50 with wide quotes on all four legs: the fly is closed as one order for at most one width
    path = np.full(M, 100.0)
    algo, (K, B, A, AGE) = make_algo(path)
    k0, kp, kc = (int(np.where(K == x)[0][0]) for x in (100.0, 99.5, 100.5))
    legs = [(0, k0, -1), (0, kp, 1), (1, k0, -1), (1, kc, 1)]
    mflat = M - 1 - Z.FLAT_BEFORE
    B2, A2 = B.copy(), A.copy()
    for r, k in ((0, k0), (1, k0)):
        B2[r, k, mflat:], A2[r, k, mflat:] = 0.45, 0.70
    for r, k in ((0, kp), (1, kc)):
        B2[r, k, mflat:], A2[r, k, mflat:] = 0.05, 0.10
    cash, fees, n = algo.flatten(legs, K, B2, A2, AGE, path, mflat, 100.0, pairs=True)
    assert n == 4 and cash >= -0.5 - 1e-9          # one wing width (0.5), not 2 x (0.70 - 0.05)
