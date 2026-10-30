"""Offline checks of the single-stock 0DTE day simulator (qc/stock_0dte/sim.py, used by qc/stock_0dte/main.py on
QuantConnect) on a synthetic day of Black-Scholes quotes: selection, fills, triggers, flatten / hold settlement,
the open-interest structures, share delivery and export packing run unchanged; scipy.special is stubbed when the
local venv lacks it."""
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
    qc_dir = str(ROOT / "qc" / "stock_0dte")
    if qc_dir not in sys.path:
        sys.path.insert(0, qc_dir)        # sim.py imports its sibling optmath.py, as on QuantConnect
    spec = importlib.util.spec_from_file_location("stock_0dte_sim", ROOT / "qc" / "stock_0dte" / "sim.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.configure()
    mod.configure_day(True)
    return mod


Z = _load_engine()
M = 390
SIG = 0.02                        # daily sigma of the synthetic stock
DAY = date(2024, 8, 9)


def _bs(S, K, s, put):
    return Z.bs(S, K, np.maximum(s, 1e-6), put)


def synthetic_day(path, step=1.0, lo=80.0, hi=120.0):
    """Strikes lo..hi; every minute a two-sided quote around the Black-Scholes mid, one or two cents wide."""
    K = np.arange(lo, hi + 1e-9, step)
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


def make_algo(path, oi=None, **kw):
    K, B, A, AGE = synthetic_day(path, **kw)
    OI = np.zeros((2, len(K))) if oi is None else oi(K)
    algo = Z.DaySim(lambda D, contracts, S, sig: (K, B, A, AGE, OI, 2 * len(K)))
    algo.M = M
    algo.day = DAY
    return algo, (K, B, A, AGE, OI)


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
            "xmin": [int(row[4]) // 10**10, (int(row[4]) // 10**7) % 1000, (int(row[4]) // 10**4) % 1000],
            "spr0": (int(row[4]) % 10**4) / 100, "nsh_hold": x7 // 10**13 - 2, "nsh_flat": (x7 // 10**12) % 10 - 2,
            "dl": (x7 // 10**9) % 1000, "wbp": (x7 // 10**5) % 10**4, "ne": (x7 // 10**4) % 10}


def row_of(rows, shape, entry=0):
    return rows[entry * Z.NS + Z.SHAPES.index(shape)]


CRASH = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 95.0, 200), np.full(M - 260, 95.0)])
RALLY = np.concatenate([np.full(60, 100.0), np.linspace(100.0, 105.0, 200), np.full(M - 260, 105.0)])


def test_shape_catalogue():
    assert Z.NS == 79 and len(set(Z.SHAPES)) == Z.NS
    assert len(Z.FAM) == len(Z.KIND) == 20


def test_rows_cover_every_shape_and_entry():
    path = np.full(M, 100.0)
    algo, _ = make_algo(path, oi=lambda K: np.vstack([np.where(K == 99.0, 5000.0, 10.0), np.where(K == 103.0, 9000.0, 10.0)]))
    rows, strad, pins, nload = algo.simulate_day(DAY, path, [], SIG, 101.0)
    assert len(rows) == Z.NS * Z.NE
    assert all(len(r) in (1, 8) for r in rows)
    assert all(r[0] < 0 for r in rows if len(r) == 1)
    assert sum(len(r) == 8 for r in rows) > 0.6 * len(rows)
    assert all(x > 0 for x in strad)
    assert all(p == 99.0 for p in pins)                  # 99 has the most open interest within max(2%, 1 sd)


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


def test_flat_day_short_put_spread_keeps_credit_and_matches_hand_calculation():
    path = np.full(M, 100.0)
    algo, (K, B, A, AGE, OI) = make_algo(path)
    rows, *_ = algo.simulate_day(DAY, path, [], SIG, None)
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
    assert u["nsh_hold"] == 0 and u["nsh_flat"] == 0


def test_crash_day_stops_and_caps_spread_loss_and_naked_put_is_assigned():
    algo, _ = make_algo(CRASH)
    rows, *_ = algo.simulate_day(DAY, CRASH, [], SIG, None)
    u = unpack(row_of(rows, (0, 0.16, 0.02)))
    assert u["xmin"][0] > 15 and u["xmin"][1] >= u["xmin"][0]
    assert u["pnl"][1] > u["pnl"][0]
    assert -u["den"] - 0.06 <= u["pnl"][0] < -0.8 * u["den"]
    un = unpack(row_of(rows, (4, 0.16, 0.0)))
    assert un["pnl"][0] < u["pnl"][0]
    assert un["nsh_hold"] == 1 and un["nsh_flat"] == 0                  # held: assigned 100 shares; flattened: none


def test_long_put_gains_on_the_crash_and_long_call_loses():
    algo, _ = make_algo(CRASH)
    rows, *_ = algo.simulate_day(DAY, CRASH, [], SIG, None)
    lp = unpack(row_of(rows, (13, 0.25, 0.0)))
    lc = unpack(row_of(rows, (12, 0.25, 0.0)))
    assert lp["pnl"][0] > 0 > lc["pnl"][0]
    assert lc["pnl"][0] >= -lc["den"] - 1e-9                            # a long option loses at most what it cost
    assert lp["ne"] == 1 and 150 <= lp["dl"] <= 350


def test_debit_vertical_is_bounded_by_width_and_debit():
    algo, (K, *_rest) = make_algo(RALLY)
    rows, *_ = algo.simulate_day(DAY, RALLY, [], SIG, None)
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
    # max pain within half a percent of the stock: no directional trade
    sel, info = algo.select(path, K, B, A, 0.5 * (B + A), A - B, AGE, OI, 14, SIG, 100.0)
    assert algo.legs_for((19, 0.50, 0.0), sel, K, B, A, AGE, 14, info)[0] is None
    # no open interest at all: no pin
    sel, info = algo.select(path, K, B, A, 0.5 * (B + A), A - B, AGE, np.zeros_like(OI), 14, SIG, None)
    assert info["kpin"] == -1 and info["kmpi"] == -1


def test_pack_roundtrip_is_exact():
    algo, _ = make_algo(np.full(M, 100.0))
    info = {"dlk": {(0, 3): 0.161}, "K": np.array([95.0, 96.0, 97.0, 98.0]), "s_impl": 100.0}
    legs = [(0, 3, -1), (0, 2, 1)]
    row = algo.pack(0.432, 0.568, [0.123, -0.234, -0.345, 0.012], 0.007, -0.004, [17, 200, 0],
                    [0.01, 0.0, 0.0], legs, 2, [2, 2, 0], info, (0, 0.16, 0.0), 0.014, 0.007, 0.03, 1, -1)
    u = unpack(row)
    assert u["cash0"] == 0.432 and u["den"] == 0.568
    assert u["pnl"] == [0.123, -0.234, -0.345, 0.012]
    assert u["imp0"] == 0.007 and u["d_close"] == -0.004 and u["xmin"] == [17, 200, 0]
    assert u["fee0"] == 0.014 and u["fee_flat"] == 0.007 and u["spr0"] == 0.03
    assert u["nsh_hold"] == 1 and u["nsh_flat"] == -1 and u["dl"] == 161 and u["wbp"] == 100 and u["ne"] == 2
    assert all(float(x).is_integer() and x < 2**53 for x in row)
    # a 5,000-dollar stock still fits the naked-structure denominator
    row = algo.pack(1.0, 5000.0, [0.0] * 4, 0.0, 0.0, [0, 0, 0], [0.0] * 3, [(1, 2, -1)], 0, [0, 0, 0],
                    {"dlk": {}, "K": np.array([1.0, 2.0, 3.0]), "s_impl": 5000.0}, (5, 0.10, 0.0), 0.0, 0.0, 0.0, 0, 0)
    assert unpack(row)["den"] == 5000.0


def test_wider_strike_grid_and_late_move_leaves_no_silent_assignment():
    # a 2.5-dollar grid: the flatten must still close an at-the-money short call when the stock ends 0.8% above it
    path = np.concatenate([np.full(370, 100.0), np.full(M - 370, 100.8)])
    algo, (K, B, A, AGE, OI) = make_algo(path, step=2.5)
    rows, *_ = algo.simulate_day(DAY, path, [], SIG, None)
    u = unpack(row_of(rows, (5, 0.50, 0.0)))                            # naked ATM call
    assert u["nsh_flat"] == 0                                          # closed at 15:50, nothing left to assign


def test_export_stream_roundtrip_through_the_decoder():
    """A day record and one name block laid out as qc/stock_0dte/main.py queues them decode back exactly."""
    from quant.strategies import stock_0dte as Q

    algo, _ = make_algo(CRASH, oi=lambda K: np.vstack([np.where(K == 99.0, 800.0, 5.0), np.where(K == 101.0, 700.0, 5.0)]))
    rows, strad, pins, nload = algo.simulate_day(DAY, CRASH, [], SIG, 101.0)
    U = Z.U
    ymd = (DAY.year % 100) * 10_000 + DAY.month * 100 + DAY.day
    s_entry = [float(CRASH[Z.mindex(hh, mm) + 1]) for hh, mm in Z.ENTRY]
    head = [900_000_000_000_000.0 + ymd * 1000 + 7, float(0 * 10**12 + 0 * 10**11 + 84 * 10**6 + nload * 10**2 + 1 * 10 + 1),
            round(100.02 * U), round(95.0 * U), round(100.5 * U), round(0.02 * 1e6), round(101.0 * U), 3 * 10**6 + 390, 1850.0]
    head += [float(round(x * U)) for x in s_entry] + [float(round(x * U)) for x in strad] + [float(round(x * U)) for x in pins]
    head.append(float(len(rows)))
    assert len(head) == Q.HEADER
    vals = [950_000_000_000_000.0 + ymd * 100 + 1] + head + [v for r in rows for v in r] + [920_000_000_000_000.0]
    names, recs, days = Q.parse_stream([int(v) for v in vals], {7: "TSLA"})
    assert len(days) == 1 and days.loc[0, "n_names"] == 1
    assert len(names) == 1 and names.loc[0, "name"] == "TSLA" and names.loc[0, "rank"] == 3
    assert names.loc[0, "S_close"] == 95.0 and abs(names.loc[0, "sig"] - 0.02) < 1e-9 and names.loc[0, "kmp"] == 101.0
    assert names.loc[0, "vix_prev"] == 18.5 and names.loc[0, "pin_e0"] == pins[0]
    assert len(recs) == Z.NS * Z.NE
    si = Z.SHAPES.index((4, 0.16, 0.0))
    got = recs[(recs["entry"] == 0) & (recs["shape"] == si)].iloc[0]
    want = unpack(rows[si])
    assert got["code"] == 0 and abs(got["pnl_H"] - want["pnl"][0]) < 1e-12 and got["nsh_hold"] == want["nsh_hold"]
    P = Q.build_panel(names, recs)
    assert P.opened[si, 0, 0] and abs(P.pnl[si, 0, 0, 0] - want["pnl"][0]) < 1e-12
    assert abs(P.hold[si, 0, 0, 0] - (want["pnl"][0] + want["d_close"])) < 1e-9


def test_next_open_records_attach_to_their_name_day():
    from quant.strategies import stock_0dte as Q
    ymd = 240809
    head = [900_000_000_000_000 + ymd * 1000 + 3, 0, 100_000, 99_000, 100_500, 20_000, 0, 10**6 + 390, 1850]
    head += [0] * 15 + [0]
    vals = [950_000_000_000_000 + ymd * 100 + 1] + head + [970_000_000_000_000 + ymd * 1000 + 3, 98_500, 1_000_000]
    names, recs, days = Q.parse_stream(vals, {3: "AMD"})
    assert names.loc[0, "next_open"] == 98.5 and names.loc[0, "split_ratio"] == 1.0 and len(recs) == 0
