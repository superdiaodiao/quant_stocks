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
    spr0, dc = two(row[3])
    off = Z.OFF
    return {"cash0": (c - off) / Z.U, "den": den / Z.U, "pnl": [(v - off) / Z.U for v in (h, s1, s2, tp)],
            "spr0": spr0 / Z.U, "d_close": (dc - off) / Z.U,
            "xmin": [int(row[4]) // 10**10, (int(row[4]) // 10**7) % 1000, (int(row[4]) // 10**4) % 1000]}


def test_rows_cover_every_shape_and_entry():
    path = np.full(M, 100.0)
    algo, _ = make_algo(path)
    rows, strad, nload = algo.simulate_day(date(2024, 8, 5), path, [], 18.0)
    assert len(rows) == Z.NS * Z.NE
    assert all(len(r) in (1, 7) for r in rows)
    assert all(r[0] < 0 for r in rows if len(r) == 1)
    assert sum(len(r) == 7 for r in rows) > 0.6 * len(rows)
    assert all(x > 0 for x in strad)


def test_flat_day_short_put_spread_keeps_credit_and_matches_hand_calculation():
    path = np.full(M, 100.0)
    algo, (K, B, A, AGE) = make_algo(path)
    rows, _, _ = algo.simulate_day(date(2024, 8, 5), path, [], 18.0)
    si = Z.SHAPES.index((0, 0.16, 0.005))
    row = rows[0 * Z.NS + si]                                     # 09:45 entry
    assert len(row) == 7
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
    row = algo.pack(0.4321, 0.5679, [0.1234, -0.2345, -0.3456, 0.0123], 0.03, -0.0042, [17, 200, 0], 0.02,
                    [0.04, 0.05, 0.0], legs, 2, [2, 2, 0], info, (0, 0.16, 0.0))
    u = unpack(row)
    assert u["cash0"] == 0.4321 and u["den"] == 0.5679
    assert u["pnl"] == [0.1234, -0.2345, -0.3456, 0.0123]
    assert u["spr0"] == 0.03 and u["d_close"] == -0.0042 and u["xmin"] == [17, 200, 0]
    assert all(float(x).is_integer() and x < 2**53 for x in row)
