"""Offline tests for scripts/research_walk_forward.py (synthetic data only)."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts import research_indicators as ri
from scripts import research_walk_forward as wf


def _rets(seed=0, start="2010-01-01", end="2015-12-31", keys=((5, 20), (6, 30), (7, 40))):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    return {k: pd.Series(rng.normal(0.0004 * (j + 1), 0.01, len(idx)), index=idx) for j, k in enumerate(keys)}


# ---------------------------------------------------------------- the pick uses only past data

def test_pick_ignores_everything_on_or_after_the_switch():
    rets = _rets()
    base = {(y, m): wf.pick(rets, y, 3, m) for y in (2013, 2014, 2015) for m in wf.METRICS}
    for y in (2013, 2014, 2015):
        future = {}
        for j, (k, r) in enumerate(rets.items()):
            r2 = r.copy()
            r2[r2.index >= f"{y}-01-01"] = (-0.05 if j == 0 else 0.08)       # make the future extreme
            future[k] = r2
        for m in wf.METRICS:
            p = wf.pick(future, y, 3, m)
            assert p["key"] == wf.pick(rets, y, 3, m)["key"]
            assert pd.Timestamp(p["window_last"]) < pd.Timestamp(f"{y}-01-01")
            assert pd.Timestamp(p["window_first"]) >= pd.Timestamp(f"{y - 3}-01-01")
    assert len(base) == 6


def test_pick_ignores_data_older_than_the_lookback():
    rets = _rets()
    k_bad = (5, 20)
    old = {k: r.copy() for k, r in rets.items()}
    old[k_bad][old[k_bad].index < "2013-01-01"] = 0.05      # a fantastic but too-old history
    assert wf.pick(old, 2014, 1, "sharpe")["key"] == wf.pick(rets, 2014, 1, "sharpe")["key"]


def test_pick_chooses_the_best_trailing_sharpe_and_breaks_ties_by_grid_order():
    idx = pd.bdate_range("2012-01-01", "2013-12-31")
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 0.01, len(idx))
    rets = {(3, 20): pd.Series(noise, index=idx), (4, 20): pd.Series(noise + 0.002, index=idx),
            (5, 20): pd.Series(noise + 0.002, index=idx)}
    assert wf.pick(rets, 2014, 1, "sharpe")["key"] == (4, 20)       # tie with (5, 20): first in order
    flat = {(9, 9): pd.Series(0.0, index=idx), (3, 20): rets[(3, 20)]}
    assert wf.pick(flat, 2014, 1, "sharpe")["key"] == (3, 20)       # always-flat (std 0) ranks last


def test_trailing_window_guard():
    r = pd.Series(0.01, index=pd.bdate_range("2012-12-20", "2013-01-10"))
    w = wf.trailing_window(r, 2013, 1)
    assert w.index.max() < pd.Timestamp("2013-01-01")


def test_sharpe_uses_excess_over_rf_when_given():
    idx = pd.bdate_range("2012-01-01", "2012-12-31")
    w = pd.Series(np.r_[0.001, 0.003] .repeat(len(idx) // 2 + 1)[:len(idx)], index=idx)
    rf = pd.Series(0.001, index=idx)
    assert wf.score(w, "sharpe", rf) < wf.score(w, "sharpe", None)


# ---------------------------------------------------------------- switching

def test_decision_year_uses_the_next_session():
    s = pd.DatetimeIndex(["2001-12-28", "2001-12-31", "2002-01-02", "2002-01-03"])
    assert wf.decision_year(s).tolist() == [2001, 2002, 2002, 2002]


def test_index_stitch_switches_at_the_last_close_of_the_prior_year():
    s = pd.bdate_range("2001-12-24", "2003-01-10")
    t_a = pd.Series(1.0, index=s)
    t_b = pd.Series(0.0, index=s)
    picks = {2002: {"key": (1, 1)}, 2003: {"key": (2, 2)}}
    tgt, lab = wf.stitch_index_target({(1, 1): t_a, (2, 2): t_b}, picks, s)
    assert tgt[s < "2001-12-31"].isna().all()                  # before the walk-forward: no position wanted
    assert tgt["2001-12-31"] == 1.0 and tgt["2002-12-30"] == 1.0
    last_2002 = s[s.year == 2002][-1]
    assert tgt[last_2002] == 0.0 and lab[last_2002] == "2/2"   # 2003's parameters decide at 2002's last close
    assert (tgt[s.year == 2003] == 0.0).all()


def test_index_stitch_trades_the_switch_at_the_next_close_and_pays_for_it():
    s = pd.bdate_range("2001-12-24", "2003-01-31")
    r1 = pd.Series(0.001, index=s)
    data = SimpleNamespace(r1={"COMP": r1}, rf=pd.Series(0.0, index=s), price={"COMP": pd.Series(50.0, index=s)})
    picks = {2002: {"key": (1, 1)}, 2003: {"key": (2, 2)}}
    tgt, _ = wf.stitch_index_target({(1, 1): pd.Series(1.0, index=s), (2, 2): pd.Series(0.0, index=s)}, picks, s)
    sim = ri.index_sim(tgt, data, "COMP", "2001-12-31", "2003-01-31")
    e = sim["exposure_held"]
    s1 = s[s > "2001-12-31"]                                     # sessions after the start close
    first_2003 = s[s.year == 2003][0]
    assert e[s1[0]] == 0.0                     # cash on the start close; first decision (2002 pick) fills at s1[0]
    assert e[s1[1]:"2002-12-31"].eq(1).all()
    assert e[first_2003] == 1.0                # 2003's pick decides at 2002's last close, fills at 2003's first
    assert e[e.index > first_2003].eq(0).all()
    assert sim["rebalances"] == 2 and sim["costs"] > 0     # enter in 2002, exit at the switch


def _stock_data(n=30, cols=("A", "B", "C")):
    idx = pd.bdate_range("2020-01-01", periods=n)
    rng = np.random.default_rng(5)
    I = pd.DataFrame({c: np.exp(np.cumsum(rng.normal(0.001, 0.01, n))) for c in cols}, index=idx)
    data = SimpleNamespace(sessions=idx, spec={"perf_start": str(idx[0].date()), "effective_end": str(idx[-1].date())},
                           perf_idx=I, close=I * 20, last_row=pd.Series([pd.NaT] * len(cols), index=list(cols)))
    return idx, list(cols), data


def test_stock_engine_constant_schedule_equals_reference_engine():
    idx, cols, data = _stock_data(60)
    rng = np.random.default_rng(9)
    buy = pd.DataFrame(rng.random((len(idx), len(cols))) < 0.15, index=idx, columns=cols)
    sell = pd.DataFrame(rng.random((len(idx), len(cols))) < 0.10, index=idx, columns=cols)
    elig = {idx[0]: cols, idx[20]: ["C", "A"]}
    rank_of = {idx[0]: {"A": 1, "B": 2, "C": 3}, idx[20]: {"C": 1, "A": 2}}
    ref = ri.stock_sim(ri.RULE_BY_CODE["T2"], data, buy, sell, elig, rank_of, k=2)
    sig = {0: (buy.to_numpy(), sell.to_numpy())}
    mine = wf.stock_engine(data, sig, np.zeros(len(idx), int), elig, rank_of, k=2)
    pd.testing.assert_series_equal(mine["nav"], ref["nav"])
    assert mine["counts"]["buy"] == ref["counts"]["buy"] and mine["counts"]["switch_sell"] == 0


def test_stock_switch_sells_what_the_new_parameters_would_not_hold_and_buys_their_holdings():
    idx, cols, data = _stock_data(20)
    F = np.zeros((len(idx), len(cols)), bool)
    b_old = F.copy()
    b_old[1, 0] = True                         # old parameters buy A on day 1 (filled day 2)
    old, new = (b_old, F.copy()), (F.copy(), F.copy())
    sched = np.empty(len(idx), dtype=object)
    for i in range(len(idx)):
        sched[i] = "old" if i < 10 else "new"
    elig = {idx[0]: cols}
    res = wf.stock_engine(data, {"old": old, "new": new}, sched, elig, {}, switch_targets={10: {"B"}}, k=2,
                          record_at={idx[5], idx[11]})
    tr = res["trades"]
    sw = tr[tr["reason"] == "switch"]
    assert len(sw) == 1 and sw.iloc[0]["sid"] == "A" and sw.iloc[0]["exit"] == idx[11]   # decided day 10, fill 11
    assert res["counts"]["switch_buy"] == 1 and res["switch_cost"] > 0
    assert res["holdings"][idx[5]] == {"A"} and res["holdings"][idx[11]] == {"B"}
    # without a switch target, the old position is simply carried through the year boundary
    res2 = wf.stock_engine(data, {"old": old, "new": new}, sched, elig, {}, k=2)
    assert res2["open"] == ["A"] and res2["counts"]["switch_sell"] == 0


def test_stock_switch_keeps_names_both_parameter_sets_hold():
    idx, cols, data = _stock_data(20)
    F = np.zeros((len(idx), len(cols)), bool)
    b = F.copy()
    b[1, 0] = True
    sched = np.empty(len(idx), dtype=object)
    for i in range(len(idx)):
        sched[i] = "old" if i < 10 else "new"
    res = wf.stock_engine(data, {"old": (b, F.copy()), "new": (F.copy(), F.copy())}, sched, {idx[0]: cols}, {},
                          switch_targets={10: {"A"}}, k=2)
    assert res["counts"]["switch_sell"] == 0 and res["counts"]["switch_buy"] == 0 and res["open"] == ["A"]


def test_stock_switch_does_not_buy_a_target_with_a_sell_signal():
    idx, cols, data = _stock_data(20)
    F = np.zeros((len(idx), len(cols)), bool)
    s_new = F.copy()
    s_new[10, 1] = True
    sched = np.empty(len(idx), dtype=object)
    for i in range(len(idx)):
        sched[i] = "new"
    res = wf.stock_engine(data, {"new": (F.copy(), s_new)}, sched, {idx[0]: cols}, {}, switch_targets={10: {"B"}})
    assert res["counts"]["buy"] == 0


def test_judge_needs_both_halves_for_a():
    full = {"cagr": 0.20, "bench_cagr": 0.10, "max_dd": -0.30, "bench_max_dd": -0.35, "t_monthly_excess": 2.5}
    assert wf.judge(full, [{"cagr": .2, "bench_cagr": .1}, {"cagr": .2, "bench_cagr": .1}])["A"]
    assert not wf.judge(full, [{"cagr": .2, "bench_cagr": .1}, {"cagr": .05, "bench_cagr": .1}])["pass"]
    b = {"cagr": 0.08, "bench_cagr": 0.10, "max_dd": -0.20, "bench_max_dd": -0.35, "t_monthly_excess": -1}
    assert wf.judge(b, [])["B"]


def test_registered_constants():
    assert wf.PRIMARY == {"L": 3, "metric": "sharpe"} and wf.LOOKBACKS == (1, 3, 5)
    assert wf.INDEX_WF_YEARS[0] == 2002 and wf.INDEX_END == "2026-09-30"
    assert wf.STOCK_WF_YEARS[0] == 2018 and wf.STOCK_WINDOW["perf_start"] == "2012-01-01"
    assert wf.INDEX_HALVES[1][0] == "2014-01-01" and wf.STOCK_HALVES[1][0] == "2022-01-01"


def test_index_pick_on_truncated_data_raises_on_future_rows():
    r = pd.Series(0.01, index=pd.bdate_range("2012-01-02", "2014-01-10"))
    with pytest.raises(AssertionError):
        ri.qt.assert_dev_dates(r.index, "2013-12-31")
