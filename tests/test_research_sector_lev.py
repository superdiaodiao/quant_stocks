"""Tests for scripts/research_sector_lev.py: no look-ahead, next-close execution, leg splicing, rebalancing, metrics."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import scripts.research_sector_lev as sl

LEGS = list(sl.ETFS)


def synthetic_data(n: int = 1400, seed: int = 0) -> sl.Data:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2000-01-03", periods=n)
    r = pd.DataFrame(rng.normal(0.0004, 0.012, size=(n, len(LEGS))), index=idx, columns=LEGS)
    adj = (1 + r).cumprod() * 50
    adj.iloc[0] = 50.0
    # late listings: SMH after 300 sessions, QLD after 500, ONEQ after 200
    for t, k in (("SMH", 300), ("QLD", 500), ("ONEQ", 200)):
        adj.iloc[:k, adj.columns.get_loc(t)] = np.nan
    rf = pd.Series(0.0001, index=idx)
    ixic = (1 + pd.Series(rng.normal(0.0003, 0.012, n), index=idx)).cumprod() * 100
    rets, px, _ = sl.build_legs(adj, adj.copy(), ixic, rf)
    p = adj["QQQ"].copy()
    return sl.Data(sessions=idx, adj=adj, rets=rets, close=px, p_qqq=p, rf=rf, raw={}, guard={})


def truncated(d: sl.Data, cut) -> sl.Data:
    return sl.Data(sessions=d.sessions[d.sessions <= cut], adj=d.adj.loc[:cut], rets=d.rets.loc[:cut],
                   close=d.close.loc[:cut], p_qqq=d.p_qqq.loc[:cut], rf=d.rf.loc[:cut], raw={}, guard={})


# ---------------------------------------------------------------- no look-ahead

@pytest.mark.parametrize("name", [s.name for s in sl.SPECS])
def test_targets_unchanged_by_truncation(name):
    d = synthetic_data()
    w_full, f_full = sl.targets(name, d)
    for cut in (d.sessions[700], d.sessions[1001], d.sessions[1250]):
        w_cut, f_cut = sl.targets(name, truncated(d, cut))
        a, b = w_full.loc[:cut].iloc[:-1], w_cut.iloc[:-1]       # the last row's month-end status is unknown
        pd.testing.assert_frame_equal(a, b, check_freq=False)
        pd.testing.assert_series_equal(f_full.loc[:cut].iloc[:-1], f_cut.iloc[:-1], check_freq=False, check_names=False)


def test_future_shock_does_not_change_past_targets():
    d = synthetic_data()
    w0, _ = sl.targets("A1", d)
    d2 = synthetic_data()
    d2.adj.iloc[1000:, d2.adj.columns.get_loc("XLE")] *= 5.0
    w1, _ = sl.targets("A1", d2)
    pd.testing.assert_frame_equal(w0.iloc[:999], w1.iloc[:999])


# ---------------------------------------------------------------- momentum and ranking

def test_momentum_definitions():
    idx = pd.date_range("2001-01-31", periods=14, freq="ME")
    me = pd.DataFrame({"X": np.arange(1.0, 15.0)}, index=idx)
    m12 = sl.momentum(me, "12-1")["X"]
    assert np.isnan(m12.iloc[11])
    assert m12.iloc[12] == pytest.approx(me["X"].iloc[11] / me["X"].iloc[0] - 1)    # M_1 / M_12
    m6 = sl.momentum(me, "6")["X"]
    assert m6.iloc[6] == pytest.approx(me["X"].iloc[6] / me["X"].iloc[0] - 1)


def test_top_n_and_eligibility():
    idx = pd.date_range("2001-01-31", periods=2, freq="ME")
    cols = list(sl.SECTORS)
    mom = pd.DataFrame([[0.1 * i for i in range(11)], [0.1 * i for i in range(11)]], index=idx, columns=cols)
    mom.iloc[0, cols.index("SOXX")] = np.nan     # not yet listed: cannot be ranked
    w = sl.top_n_weights(mom, 3)
    assert w.iloc[0][["SMH", "XLB", "XLU"]].sum() == pytest.approx(1.0)
    assert w.iloc[1][["SOXX", "SMH", "XLB"]].tolist() == pytest.approx([1 / 3] * 3)
    assert w.sum(axis=1).tolist() == pytest.approx([1.0, 1.0])
    few = mom.copy()
    few.iloc[0, :5] = np.nan
    few.iloc[0, 9:] = np.nan
    assert sl.top_n_weights(few, 3).iloc[0].isna().all()     # fewer than 9 rankable ETFs


def test_a4_goes_to_ief_when_spy_below_sma10():
    d = synthetic_data()
    spy = d.adj["SPY"].copy()
    d.adj.loc[d.sessions[900]:, "SPY"] = spy.loc[d.sessions[900]:] * 0.5     # crash, below the 10-month SMA
    w, _ = sl.targets("A4", d)
    me = d.sessions[sl.month_end_mask(d.sessions)]
    nxt = me[me > d.sessions[905]][0]
    assert w.loc[nxt, "IEFX"] == 1.0 and w.loc[nxt, list(sl.SECTORS)].sum() == 0.0


# ---------------------------------------------------------------- legs

def test_leg_splicing():
    d = synthetic_data()
    fq = d.rets["QLD"].first_valid_index()
    before = d.rets.index[d.rets.index < fq]
    syn = 2 * d.rets["QQQ"] - (d.rf + sl.SPREAD_2X / 252) - sl.LEV_ER / 252
    pd.testing.assert_series_equal(d.rets["QLDX"].loc[before].iloc[1:], syn.loc[before].iloc[1:], check_names=False)
    assert (d.rets["QLDX"].loc[fq:] == d.rets["QLD"].loc[fq:]).all()
    fo = d.rets["ONEQ"].first_valid_index()
    assert (d.rets["BENCH"].loc[fo:] == d.rets["ONEQ"].loc[fo:]).all()
    assert d.rets["BENCH"].loc[:fo].iloc[1:-1].notna().all()


def test_exposure_weights():
    assert sl.exposure_weights(1.25) == {"QQQ": 0.75, "QLDX": 0.25, "CASH": 0.0}
    assert sl.exposure_weights(1.5) == {"QQQ": 0.5, "QLDX": 0.5, "CASH": 0.0}
    assert sl.exposure_weights(0.5) == {"QQQ": 0.5, "QLDX": 0.0, "CASH": 0.5}
    assert sl.exposure_weights(1.0) == {"QQQ": 1.0, "QLDX": 0.0, "CASH": 0.0}


def test_b3_switches_with_trend_and_flags_change():
    d = synthetic_data()
    p = pd.Series(np.r_[np.linspace(100, 200, 700), np.linspace(200, 80, 700)], index=d.sessions)
    w, flag = sl.part_b_targets("B3", p)
    assert w.iloc[650]["QLDX"] == 0.5 and w.iloc[1300]["QLDX"] == 0.0 and w.iloc[1300]["QQQ"] == 1.0
    change = w["QQQ"].diff().fillna(0).ne(0)
    assert flag[change].all()


# ---------------------------------------------------------------- simulation

def test_next_close_execution_and_monthly_rebalance():
    idx = pd.bdate_range("2020-01-01", periods=60)
    rets = pd.DataFrame({"QQQ": 0.01, "QLDX": 0.02, "CASH": 0.0, "BENCH": 0.0}, index=idx)
    close = pd.DataFrame(50.0, index=idx, columns=rets.columns)
    w = pd.DataFrame({"QQQ": 0.5, "QLDX": 0.5, "CASH": 0.0}, index=idx)
    flag = pd.Series(sl.month_end_mask(idx), index=idx)
    sim = sl.simulate(w, flag, rets, close, 1, costs=False)
    held = sim["held"]
    me = idx[sl.month_end_mask(idx)][0]
    after = idx[idx.get_loc(me) + 2]                       # rebalanced at the close of me+1 -> weights 50/50 at me+2
    assert held.loc[after, "QLDX"] == pytest.approx(0.5)
    assert held.loc[me, "QLDX"] > 0.5                      # drifted before the rebalance
    assert sim["trades"] >= 2


def test_lag_one_ignores_same_day_signal():
    idx = pd.bdate_range("2020-01-01", periods=10)
    rets = pd.DataFrame({"QQQ": [0.0] * 5 + [0.10] + [0.0] * 4, "CASH": 0.0}, index=idx)
    close = pd.DataFrame(50.0, index=idx, columns=rets.columns)
    w = pd.DataFrame({"QQQ": [0.0] * 4 + [1.0] * 6, "CASH": [1.0] * 4 + [0.0] * 6}, index=idx)
    flag = pd.Series(False, index=idx)
    sim = sl.simulate(w, flag, rets, close, 1, costs=False)
    assert sim["value"].iloc[-1] == pytest.approx(sl.START_EQUITY)      # decided day 4, bought close of day 5, missed +10%
    sim0 = sl.simulate(w, flag, rets, close, 1, costs=False, lag=0)
    assert sim0["value"].iloc[-1] == pytest.approx(sl.START_EQUITY * 1.10)


def test_costs_reduce_value():
    d = synthetic_data()
    w, flag = sl.targets("A1", d)
    s = sl.entry_index(w, flag, d, True)
    a = sl.simulate(w, flag, d.rets, d.close, s)
    b = sl.simulate(w, flag, d.rets, d.close, s, costs=False)
    assert a["value"].iloc[-1] < b["value"].iloc[-1]
    assert a["cost_frac"] > 0


# ---------------------------------------------------------------- metrics

def test_drawdown_helpers():
    idx = pd.bdate_range("2000-01-03", periods=8)
    r = pd.Series([0.0, -0.5, 0.0, 0.5, 0.4, 0.0, -0.1, 0.2], index=idx)
    v = sl.equity0(r)
    assert sl.longest_drawdown(v) == 3                      # 0.5, 0.5, 0.75 below the peak of 1.0
    assert sl.drawdown_episode(v, "2000-01-01", "2000-01-31") is None     # window too short
    long_idx = pd.bdate_range("2000-01-03", periods=60)
    rr = pd.Series(0.0, index=long_idx)
    rr.iloc[5] = -0.5
    rr.iloc[40] = 1.0
    ep = sl.drawdown_episode(sl.equity0(rr), "2000-01-01", "2000-12-31")
    assert ep["depth"] == pytest.approx(-0.5)
    assert ep["trough"] == str(long_idx[5].date()) and ep["recovered"] == str(long_idx[40].date())


def test_evaluate_criteria():
    base = {"cagr": 0.12, "oneq_cagr": 0.10, "dd_shallower_than_oneq_pp": 0.0, "t_monthly_excess_vs_oneq": 2.1}
    assert sl.evaluate(base, base, base)["A"]
    assert not sl.evaluate({**base, "t_monthly_excess_vs_oneq": 1.9}, base, base)["pass"]
    safe = {"cagr": 0.08, "oneq_cagr": 0.10, "dd_shallower_than_oneq_pp": 12.0, "t_monthly_excess_vs_oneq": -1}
    assert sl.evaluate(safe, safe, safe)["B"]
    assert not sl.evaluate(safe, {**safe, "cagr": 0.06}, safe)["pass"]


def test_period_metrics_beta_one_for_benchmark():
    d = synthetic_data()
    b = d.rets["QQQ"].iloc[1:]
    m = sl.period_metrics(b, b, b, d.rf)
    assert m["beta_vs_oneq"] == pytest.approx(1.0)
    assert m["alpha_ann_vs_oneq"] == pytest.approx(0.0, abs=1e-12)
    assert m["cagr_minus_oneq"] == pytest.approx(0.0)
