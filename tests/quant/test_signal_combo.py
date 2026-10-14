"""Synthetic-data tests of quant.strategies.signal_combo, the ``equity`` argument of the two T engines and
``deflated_sharpe_n`` (docs/research_ledger_signal_combo.md)."""
import inspect
import math

import numpy as np
import pandas as pd
import pytest

from quant.data.ohlc import OHLCSeries
from quant.evaluation.criteria import bonferroni_t_student, deflated_sharpe, deflated_sharpe_n
from quant.strategies import selective_t as st
from quant.strategies import signal_combo as sc
from quant.strategies import t_grid as g


# ------------------------------------------------------------------ weights

def test_equal_risk_weights_hit_the_te_target_when_uncapped():
    sig = pd.Series({"A": 0.10, "B": 0.02, "C": 0.05})
    rho = pd.DataFrame(np.eye(3), index=sig.index, columns=sig.index)
    h, info = sc.equal_risk_weights(sig, rho, 0.04, {}, regt_cap=10.0, mn_parts=(), t_parts=())
    c = 0.04 / math.sqrt(3)
    assert h == pytest.approx({"A": c / 0.10, "B": c / 0.02, "C": c / 0.05})
    assert info["te_ex_ante"] == pytest.approx(0.04)
    # every part contributes the same risk
    assert {k: h[k] * sig[k] for k in h} == pytest.approx({k: c for k in h})


def test_equal_risk_weights_correlation_lowers_the_scale():
    sig = pd.Series({"A": 0.1, "B": 0.1})
    rho = pd.DataFrame([[1, 0.5], [0.5, 1]], index=sig.index, columns=sig.index)
    h, info = sc.equal_risk_weights(sig, rho, 0.04, {}, regt_cap=10.0, mn_parts=(), t_parts=())
    assert info["c"] == pytest.approx(0.04 / math.sqrt(3.0))
    assert info["te_ex_ante"] == pytest.approx(0.04)


def test_caps_are_not_redistributed_and_regt_scales_the_levered_parts():
    sig = pd.Series({"SP": 0.2, "MISP": 0.01, "RSI2": 0.01, "IDX": 0.001})
    rho = pd.DataFrame(np.eye(4), index=sig.index, columns=sig.index)
    caps = {"SP": 0.5, "MISP": 0.5, "RSI2": 0.25, "IDX": 0.5}
    h, info = sc.equal_risk_weights(sig, rho, 0.04, caps, regt_cap=0.95, mn_parts=("MISP",), t_parts=("RSI2",))
    assert h["IDX"] == 0.5                       # capped, the excess is not given to anyone
    assert h["SP"] == pytest.approx(0.02 / 0.2)  # untouched
    # MISP capped at 0.5, RSI2 at 0.25: 0.5 * (1 + 0.5 + 0.25 + 0.5) = 1.125 > 0.95 -> scaled
    assert info["regt_initial"] == pytest.approx(0.95)
    assert h["MISP"] / h["RSI2"] == pytest.approx(0.5 / 0.25)
    assert set(info["capped"]) == {"MISP", "RSI2", "IDX"}


def test_regt_initial_of_equal_notional_ten_percent():
    h = {c: 0.10 for c in ("SP", "MISP", "PROF", "ML", "RSI2", "S3Yb", "IDX")}
    assert sc.regt_initial(h) == pytest.approx(0.90)


# ------------------------------------------------------------------ book mechanics

def test_financing_charges_previous_close_value_by_calendar_days():
    idx = pd.DatetimeIndex(["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"])
    extra = pd.Series([1000.0, 1000.0, -50.0, 0.0], index=idx)
    effr = pd.Series(0.02, index=idx)
    f = sc.financing(extra, effr, 0.015)
    day = 1000 * 0.035 / 360
    assert f.iloc[0] == 0.0
    assert f.iloc[1] == pytest.approx(day * 1)
    assert f.iloc[2] == pytest.approx(day * 1 + day * 3)      # Friday close -> Monday: 3 days
    assert f.iloc[3] == pytest.approx(day * 4)                # negative extra pays nothing


def test_assemble_sums_parts_and_flags_breaches():
    idx = pd.bdate_range("2020-01-01", periods=3)
    z = pd.Series(0.0, index=idx)
    base = pd.Series([7000.0, 7100.0, 7200.0], index=idx)
    ov = pd.Series([0.0, -100.0, -9000.0], index=idx)
    parts = [sc.Part("ONEQ", base, base, base, z),
             sc.Part("MISP", ov, z, pd.Series(1000.0, index=idx), pd.Series(1000.0, index=idx))]
    b = sc.assemble(idx, parts, cash=500.0)
    assert list(b["book"]) == [7500.0, 7500.0, -1300.0]
    assert list(b["twin"]) == [7500.0, 7600.0, 7700.0]
    assert list(b["breach"]) == [False, False, True]
    with pytest.raises(ValueError):
        sc.assemble(idx, [sc.Part("X", base.iloc[:2], base, base, z)])


def test_fold_stats_book_equal_to_oneq_has_no_excess():
    idx = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(1)
    v = pd.Series(10_000 * np.cumprod(1 + rng.normal(0.0005, 0.01, 300)), index=idx)
    m, ex, ext = sc.fold_stats(v, v, v)
    assert m["excess_vs_oneq"] == pytest.approx(0.0)
    assert np.allclose(ex, 0.0) and np.allclose(ext, 0.0)
    assert not m["A"] and not m["B"]
    m2, ex2, _ = sc.fold_stats(v * 1.0 + np.arange(300) * 5.0, v, v)
    assert m2["cagr"] > m2["oneq_cagr"] and ex2.mean() > 0


def test_level_and_moments():
    r = pd.Series([0.1, -0.1], index=pd.bdate_range("2020-01-02", periods=2))
    lv = sc.level_of(r, pd.Timestamp("2020-01-01"), 100.0)
    assert list(lv) == pytest.approx([100.0, 110.0, 99.0])
    m = sc.moments(pd.Series([1.0, -1.0, 1.0, -1.0]))
    assert m["skew"] == pytest.approx(0.0) and m["kurt"] == pytest.approx(1.0)


# ------------------------------------------------------------------ statistics

def test_deflated_sharpe_n_equals_the_listed_trials_version():
    trials = np.array([0.1, 0.3, -0.2, 0.05, 0.2])
    a = deflated_sharpe(0.25, 120, -0.3, 4.0, trials)
    b = deflated_sharpe_n(0.25, 120, -0.3, 4.0, len(trials), float(np.var(trials, ddof=1)))
    assert b["dsr"] == pytest.approx(a["dsr"]) and b["sr0"] == pytest.approx(a["sr0_monthly"])


def test_family_hurdle_is_much_higher_than_the_local_one():
    assert bonferroni_t_student(127, 3) < 2.5 < 5.0 < bonferroni_t_student(127, 184_545)


# ------------------------------------------------------------------ the equity argument of the T engines

def synthetic_market(n=420, seed=3):
    rng = np.random.default_rng(seed)
    adj = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, n)))
    o = np.r_[adj[0], adj[:-1]] * np.exp(rng.normal(0, 0.006, n))
    h = np.maximum(o, adj) * (1 + np.abs(rng.normal(0, 0.006, n)))
    lo = np.minimum(o, adj) * (1 - np.abs(rng.normal(0, 0.006, n)))
    dates = pd.bdate_range("2015-01-01", periods=n)
    split, div = np.ones(n), np.zeros(n)
    s = OHLCSeries("AAPL", dates, o, h, lo, adj, np.r_[np.nan, adj[:-1]], split, div, np.full(n, 0.02),
                   pd.Series(np.r_[np.nan, adj[1:] / adj[:-1] - 1], index=dates), {})
    sma = pd.Series(adj).rolling(20, min_periods=20).mean().to_numpy()
    return st.Market("AAPL", s, np.ones(n), sma, np.r_[np.nan, sma[:-1]], np.full(n, 50.0)), adj, sma


def test_engine_defaults_are_the_frozen_ten_thousand():
    assert inspect.signature(st.simulate).parameters["equity"].default == st.START_EQUITY == 10_000.0
    assert inspect.signature(g.simulate_grid).parameters["equity"].default == g.START_EQUITY == 10_000.0
    assert inspect.signature(g.exact_run).parameters["equity"].default == g.START_EQUITY


@pytest.mark.parametrize("equity", [10_000.0, 2_500.0, 40_000.0])
def test_selective_t_and_t_grid_agree_at_any_equity(equity):
    mk, adj, sma = synthetic_market()
    s, n = mk.s, len(adj)
    fam = st.Fam("S3", "Y", 0.03, z=0.03, max_hold=20)
    ref = st.simulate(mk, 20, n - 1, st.Spec((fam,)), None, equity=equity)
    assert ref["value"].iloc[0] == equity
    if equity == 10_000.0:
        dflt = st.simulate(mk, 20, n - 1, st.Spec((fam,)), None)
        pd.testing.assert_series_equal(ref["value"], dflt["value"])
    # t_grid B leg with a D20-style trigger equal to the S3-Y rule, SMA exit capped by the target, reserve 25%
    with np.errstate(invalid="ignore"):
        trig = adj <= 0.97 * sma
    T = n
    active = np.zeros((1, T), bool)
    active[0, 20:] = True
    init = np.zeros((1, T), bool)
    init[0, 20] = True
    last = np.zeros((1, T), bool)
    last[0, -1] = True
    sig_lo = np.zeros((1, 32, T), bool)
    sig_lo[0, 4] = trig
    rows = g.Rows("X", s.sessions, ["X"], np.array([2.0]), s.open[None], s.high[None], s.low[None], s.close[None],
                  np.r_[s.close[0], s.close[:-1]][None], np.ones((1, T)), np.zeros((1, T)),
                  np.r_[0.0, adj[1:] / adj[:-1] - 1][None], active, init, last, np.zeros((1, 32, T), bool), sig_lo,
                  np.zeros((1, 8, T), bool), np.zeros((1, 8, T), bool), np.vstack([mk.sma_prev] * 3)[None],
                  np.ones((1, T), bool), np.full((1, T), 0.02), np.zeros(T), np.full(T, np.nan))
    codes = {"measure": np.array([1, 0]), "level": np.array([0, 0]), "direction": np.array([1, g.DIR_NONE]),
             "exit": np.array([0, 4]), "hold": np.array([2, 0]), "frac": np.array([1, 0]),
             "reserve": np.array([2, 2]), "regime": np.array([0, 0])}
    sim = g.simulate_grid(rows, codes, record=True, equity=equity)
    hb = st.simulate(mk, 20, n - 1, st.Spec(()), None, equity=equity)
    got_hb = equity * np.cumprod(1 + np.nan_to_num(sim["rec"]["R"][:, 1]))[20:]
    np.testing.assert_allclose(got_hb, hb["value"].to_numpy()[1:], rtol=1e-9)
    # bigger accounts hold proportionally more shares (whole-share rounding aside)
    if equity == 40_000.0:
        small = st.simulate(mk, 20, n - 1, st.Spec(()), None, equity=10_000.0)
        assert hb["value"].iloc[-1] / equity == pytest.approx(small["value"].iloc[-1] / 10_000.0, rel=2e-3)
