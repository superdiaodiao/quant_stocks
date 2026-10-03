"""Offline tests for scripts/research_qqq_timing.py (no cache files are read)."""
import json

import numpy as np
import pandas as pd
import pytest

from scripts import research_qqq_timing as qt


# ---------------------------------------------------------------- date guard

def test_dev_end_is_2014():
    assert qt.DEV_END == "2014-12-31"


def test_truncate_drops_rows_after_dev_end_and_guard_raises():
    frame = pd.DataFrame({"date": ["2014-12-30", "2014-12-31", "2015-01-02", "2020-03-16"], "x": [1, 2, 3, 4]})
    out = qt.truncate_dev(frame, "date")
    assert out["date"].tolist() == ["2014-12-30", "2014-12-31"]
    qt.assert_dev_dates(out["date"])
    with pytest.raises(qt.FutureDataError):
        qt.assert_dev_dates(["2014-12-31", "2015-01-02"])
    with pytest.raises(qt.FutureDataError):
        qt.assert_dev_dates(pd.DatetimeIndex(["2015-01-02"]))


def test_parse_chart_truncates_immediately(tmp_path):
    days = ["2014-12-30", "2014-12-31", "2015-01-02"]
    ts = [int(pd.Timestamp(d + " 09:30", tz="America/New_York").timestamp()) for d in days]
    doc = {"chart": {"result": [{"timestamp": ts,
                                 "indicators": {"quote": [{"close": [10.0, 11.0, 12.0]}],
                                                "adjclose": [{"adjclose": [9.0, 10.0, 11.0]}]},
                                 "events": {"dividends": {str(ts[1]): {"date": ts[1], "amount": 0.5}}}}]}}
    path = tmp_path / "chart.json"
    path.write_text(json.dumps(doc))
    df = qt.parse_chart(path)
    assert df["date"].tolist() == ["2014-12-30", "2014-12-31"]
    assert df["dividend"].tolist() == [0.0, 0.5]


# ---------------------------------------------------------------- signals use data up to t only

def _series(n=600, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2000-01-03", periods=n)
    r = pd.Series(rng.normal(0.0004, 0.015, n), index=idx)
    return (1 + r).cumprod(), r


@pytest.mark.parametrize("rule", [qt.Rule("T01", 100, 0.02), qt.Rule("T02", 150, 0.0), qt.Rule("T12", 75, 0.05),
                                  qt.Rule("VT", target=0.2, window=20), qt.Rule("VTT", 150, 0.02, 0.2, 60),
                                  qt.Rule("M01", 150, 0.0)])
def test_no_look_ahead(rule):
    p, r = _series()
    base = qt.target_exposure(rule, p, r)
    k = 400
    r2 = r.copy()
    r2.iloc[k + 1:] = -r2.iloc[k + 1:] * 3      # rewrite the future
    p2 = (1 + r2).cumprod()
    pert = qt.target_exposure(rule, p2, r2)
    pd.testing.assert_series_equal(base.iloc[:k + 1], pert.iloc[:k + 1])


def test_trend_band_keeps_state_inside_band():
    p = pd.Series([100.0] * 5 + [101.0, 103.0, 101.0, 99.5, 97.0])
    st = qt.trend_state(p, 5, 0.02)
    # MA of the first window is 100; 101 is inside the band (keeps initial state); 103 > MA*1.02 turns on;
    # back to 101 / 99.5 stays on (inside the band of the moving MA); 97 < MA*0.98 turns off
    assert np.isnan(st.iloc[3])
    assert st.iloc[4] == 0.0          # initial state: P == MA, not above
    assert st.iloc[6] == 1.0
    assert st.iloc[7] == 1.0 and st.iloc[8] == 1.0
    assert st.iloc[9] == 0.0


def test_month_end_only_holds_value_through_month():
    idx = pd.bdate_range("2001-01-29", "2001-03-05")
    sig = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    out = qt.month_end_only(sig)
    jan_end = sig.loc["2001-01-31"]
    assert (out.loc["2001-02-01":"2001-02-27"] == jan_end).all()
    assert out.loc["2001-02-28"] == sig.loc["2001-02-28"]
    assert np.isnan(out.loc["2001-01-29"])


def test_rule_grid_matches_preregistration():
    rules = qt.rule_grid()
    assert len(rules) == 142
    assert len({r.name for r in rules}) == 142


# ---------------------------------------------------------------- synthetic 2x

def test_synthetic_2x_math():
    idx = pd.bdate_range("2001-01-01", periods=3)
    r = pd.Series([0.01, 0.0, -0.02], index=idx)
    rf = pd.Series([0.0001, 0.0001, 0.0001], index=idx)
    out = qt.synthetic_2x(r, rf, spread=0.0063, er=0.0095)
    drag = 0.0001 + 0.0063 / 252 + 0.0095 / 252
    np.testing.assert_allclose(out.values, [0.02 - drag, -drag, -0.04 - drag])


def test_weights_for_exposure():
    np.testing.assert_allclose(qt.weights_for(0.0), [0, 0, 1])
    np.testing.assert_allclose(qt.weights_for(0.6), [0.6, 0, 0.4])
    np.testing.assert_allclose(qt.weights_for(1.5), [0.5, 0.5, 0])
    np.testing.assert_allclose(qt.weights_for(2.0), [0, 1, 0])
    w = qt.weights_for(1.3)
    assert w[0] + 2 * w[1] == pytest.approx(1.3)


def test_order_cost_minimum_and_cap():
    # $1,000 at $50 = 20 shares: commission is the $0.35 minimum
    c = qt.order_cost(1000.0, 50.0, sell=False)
    assert c == pytest.approx(0.35 + 20 * qt.FEES_PER_SHARE + 1000 * qt.HALF_SPREAD)
    # sells add the regulatory fee
    assert qt.order_cost(1000.0, 50.0, sell=True) - c == pytest.approx(1000 * qt.SELL_REG_FRAC)
    # tiny order: commission capped at 1% of value
    assert qt.order_cost(10.0, 50.0, False) == pytest.approx(0.1 + 0.2 * qt.FEES_PER_SHARE + 10 * qt.HALF_SPREAD)


# ---------------------------------------------------------------- execution timing

def _sim_inputs():
    idx = pd.bdate_range("2003-01-01", periods=8)
    r1 = pd.Series(0.01, index=idx)
    r2 = pd.Series(0.02, index=idx)
    rc = pd.Series(0.0, index=idx)
    price = pd.Series(50.0, index=idx)
    target = pd.Series([0, 0, 0, 1, 1, 1, 1, 1], index=idx, dtype=float)   # turns on at the close of idx[3]
    return idx, r1, r2, rc, price, target


def test_next_close_execution():
    idx, r1, r2, rc, price, target = _sim_inputs()
    sim = qt.simulate(target, r1, r2, rc, price, str(idx[0].date()), str(idx[-1].date()), lag=1)
    e = sim["exposure_held"]
    # decided at close idx[3], traded at close idx[4], first invested return is idx[5]
    assert e.loc[idx[4]] == 0.0
    assert e.loc[idx[5]] == 1.0
    # idx[4] earns cash (0) and pays the two orders (sell the T-bill ETF, buy QQQ) at its close
    assert -0.001 < sim["ret"].loc[idx[4]] < 0.0
    assert sim["ret"].loc[idx[3]] == pytest.approx(0.0)
    assert sim["ret"].loc[idx[5]] == pytest.approx(0.01)
    assert sim["rebalances"] == 1


def test_same_close_execution_variant():
    idx, r1, r2, rc, price, target = _sim_inputs()
    sim = qt.simulate(target, r1, r2, rc, price, str(idx[0].date()), str(idx[-1].date()), lag=0)
    e = sim["exposure_held"]
    assert e.loc[idx[3]] == 0.0
    assert e.loc[idx[4]] == 1.0
    assert -0.001 < sim["ret"].loc[idx[3]] < 0.0   # traded at the close of idx[3]: cost lands that day
    assert sim["ret"].loc[idx[4]] == pytest.approx(0.01)
    assert sim["ret"].loc[idx[5]] == pytest.approx(0.01)


def test_two_x_state_earns_two_x_return():
    idx, r1, r2, rc, price, target = _sim_inputs()
    sim = qt.simulate(target * 2, r1, r2, rc, price, str(idx[0].date()), str(idx[-1].date()), lag=1)
    assert sim["ret"].loc[idx[6]] == pytest.approx(0.02)


def test_vol_target_band_suppresses_small_trades():
    idx = pd.bdate_range("2003-01-01", periods=6)
    r1 = pd.Series(0.0, index=idx)
    target = pd.Series([1.0, 1.1, 1.2, 1.3, 1.0, 0.0], index=idx)
    sim = qt.simulate(target, r1, r1 * 2, r1, pd.Series(50.0, index=idx), str(idx[0].date()),
                      str(idx[-1].date()), lag=0, continuous=True)
    # 1.0 -> 1.1 and 1.2 stay (below 0.25), 1.3 trades, 1.0 trades (0.3 away), 0.0 exits
    assert sim["rebalances"] == 3


# ---------------------------------------------------------------- one-shot test mode

def test_oneshot_frozen_rule_and_dates():
    assert qt.ONESHOT_RULE.name == "T01_L200_b2"
    assert qt.ONESHOT_REPORT_ONLY.name == "T02_L200_b2"
    assert str(qt.latest_complete_month_end(pd.Timestamp("2026-10-03")).date()) == "2026-09-30"
    assert str(qt.latest_complete_month_end(pd.Timestamp("2026-03-31")).date()) == "2026-02-28"


def test_evaluate_criteria():
    q = {"max_dd": -0.35, "calmar": 0.50, "cagr": 0.175}
    ok = qt.evaluate_criteria({"max_dd": -0.24, "calmar": 0.60, "cagr": 0.15}, q)
    assert ok["pass"]
    assert not qt.evaluate_criteria({"max_dd": -0.26, "calmar": 0.60, "cagr": 0.15}, q)["c1_maxdd_at_least_10pp_shallower"]
    assert not qt.evaluate_criteria({"max_dd": -0.24, "calmar": 0.50, "cagr": 0.15}, q)["c2_calmar_higher"]
    bad3 = qt.evaluate_criteria({"max_dd": -0.10, "calmar": 1.4, "cagr": 0.14}, q)
    assert not bad3["c3_cagr_shortfall_at_most_3pp"] and not bad3["pass"]


def test_fill_rf_uses_dtb3_only_after_kf_ends():
    sessions = pd.bdate_range("2026-08-27", "2026-09-03")
    rf = pd.Series(0.0002, index=pd.bdate_range("2026-08-27", "2026-08-31"))
    dtb3 = pd.Series(0.0378, index=sessions)
    out = qt.fill_rf(rf, dtb3, sessions)
    assert (out.loc[:"2026-08-31"] == 0.0002).all()
    np.testing.assert_allclose(out.loc["2026-09-01":].values, 0.0378 / 252)
