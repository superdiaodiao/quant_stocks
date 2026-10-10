"""The quant core gives the same numbers as the study code it was extracted from.

Each test feeds the same (synthetic, seeded) inputs to a quant function and to the original function (frozen
copies of the pre-phase-2 study code in tests/quant/originals/), and requires exact equality (``==`` on floats, ``assert_*_equal`` with
``check_exact``). Tests that need the local research cache skip when it is absent.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from quant.backtest import costs, execution
from quant.data import calendar, guards, ohlc, panel, rates, version
from quant.data.sources import yahoo
from quant.evaluation import criteria, metrics
from scripts import study_data_version as dv
from originals import calendar as cal
from originals import canslim_dev as cs
from originals import intraday_t as it
from originals import qqq_timing as qt
from originals import reversal_dev as rev

RNG = np.random.default_rng(20261010)


def daily_returns(n=800, start="2015-01-02", vol=0.012, drift=0.0004, seed=None):
    rng = np.random.default_rng(seed) if seed is not None else RNG
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(rng.normal(drift, vol, n), index=idx)


# ======================================================================== costs

@pytest.mark.parametrize("sell", [False, True])
def test_etf_order_cost_matches_qqq_timing_and_calendar(sell):
    for value, price in [(0.0, 50.0), (-5.0, 50.0), (10.0, 50.0), (1234.5, 97.3), (10_000.0, 350.0), (2e6, 12.0)]:
        want = qt.order_cost(value, price, sell)                        # HALF_SPREAD = 1 bp there
        assert costs.etf_order_cost(value, price, sell, qt.HALF_SPREAD) == want
        assert costs.etf_order_cost(value, price, sell, 2e-4) == cal.order_cost(value, price, sell, 2e-4)


@pytest.mark.parametrize("sell", [False, True])
def test_ibkr_order_cost_matches_reversal_dev(sell):
    for shares, price in [(0, 10.0), (1, 10.0), (37, 251.3), (-120, 33.33), (5000, 4.2), (90_000, 25.0)]:
        hs = rev.half_spread(float(RNG.integers(1, 400)), price)
        assert costs.ibkr_order_cost(shares, price, sell, hs) == rev.order_cost(shares, price, sell, hs)


def test_value_cost_and_half_spreads_match_originals():
    for rank in [np.nan, 1, 50, 51, 120, 300, 301]:
        for price in [0.8, 3.0, 25.0, 400.0]:
            for mult in (1.0, 2.0):
                assert costs.rank_half_spread(rank, price, mult) == rev.half_spread(rank, price, mult)
    for value, price in [(0.0, 10.0), (500.0, 10.0), (9_999.0, 123.4)]:
        hs = rev.half_spread(80, price)
        assert costs.ibkr_value_cost(value, price, True, hs) == cs.order_cost(value, price, True, hs)
    assert costs.ibkr_value_cost(100.0, float("nan"), False, 1e-4) == 0.0
    for sym in ("QQQ", "QLD", "ONEQ"):
        for price in (0.5, 20.0, 400.0):
            assert costs.tick_half_spread(it.HALF_SPREAD_BPS[sym], price) == it.hs_of(sym, price)
    assert costs.REBALANCE_BAND == rev.REBALANCE_BAND and costs.START_EQUITY == qt.START_EQUITY


# ======================================================================== execution

def test_cent_rounding_and_limit_fills_match_intraday_t():
    for x in RNG.uniform(1, 500, 200).tolist() + [100.0, 99.995, 0.015]:
        assert execution.ceil_cent(x) == it.ceil_cent(x)
        assert execution.floor_cent(x) == it.floor_cent(x)
    for _ in range(500):
        lim = float(RNG.uniform(20, 200))
        o, h, lo = lim * (1 + RNG.normal(0, 0.01, 3))
        for through in (True, False):
            assert execution.fill_sell_limit(o, h, lim, through) == it.fill_t1(o, h, lim, through)
            assert execution.fill_buy_limit(o, lo, lim, through) == it.fill_t2(o, lo, lim, through)


def synthetic_ohlc(n=300, seed=1, splits=None):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-02", periods=n)
    c = 100 * np.cumprod(1 + rng.normal(0.0005, 0.015, n))
    o = c * (1 + rng.normal(0, 0.004, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n)))
    div = np.where(rng.uniform(size=n) < 0.02, 0.3, 0.0)
    adj = c * np.cumprod(1 + np.r_[0, div[1:] / c[:-1]])
    df = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "open": o, "high": h, "low": lo, "close": c,
                       "adjclose": adj, "dividend": div})
    return df, (splits or {})


def assert_series_equal_fields(a, b):
    for f in ("open", "high", "low", "close", "prev_close", "split", "div", "atr_pct"):
        np.testing.assert_array_equal(getattr(a, f), getattr(b, f))
    pd.testing.assert_series_equal(a.tr, b.tr, check_exact=True)
    assert a.sessions.equals(b.sessions) and a.checks == b.checks and a.sym == b.sym


def test_real_ohlc_build_matches_intraday_t():
    df, _ = synthetic_ohlc()
    splits = {df["date"].iloc[120]: 2.0, df["date"].iloc[250]: 1.5}
    assert_series_equal_fields(ohlc.build_real_ohlc("XYZ", df, splits), it.build("XYZ", df, splits))


def test_buy_hold_returns_matches_intraday_buy_hold_oneq():
    df, _ = synthetic_ohlc(seed=3)
    s = it.build("ONEQ", df, {})
    sessions = s.sessions
    for a, b in [(5, 200), (1, 299), (100, 101)]:
        for on in (True, False):
            want = it.buy_hold_oneq(s, sessions, sessions[a], sessions[b], on)
            got = execution.buy_hold_returns(s, sessions, sessions[a], sessions[b], it.HALF_SPREAD_BPS["ONEQ"], on)
            pd.testing.assert_series_equal(got, want, check_exact=True)


def test_next_session_map():
    sessions = pd.bdate_range("2020-01-01", periods=10)
    got = execution.next_session([sessions[2], sessions[0], sessions[9]], sessions)
    assert got == {sessions[1]: sessions[0], sessions[3]: sessions[2]}


# ======================================================================== metrics

def test_return_metrics_match_qqq_timing_and_calendar():
    r = daily_returns()
    rf = pd.Series(0.0001, index=r.index)
    b = daily_returns(seed=7)
    v = (1 + r).cumprod()
    assert metrics.cagr_of(r) == qt.cagr_of(r)
    assert math.isnan(metrics.cagr_of(pd.Series([], dtype=float)))
    assert metrics.max_drawdown(v) == qt.max_drawdown(v)
    pd.testing.assert_series_equal(metrics.monthly(r), qt.monthly(r), check_exact=True)
    pd.testing.assert_series_equal(metrics.yearly(r), qt.yearly(r), check_exact=True)
    assert metrics.cagr_months(metrics.monthly(r)) == qt.cagr_months(qt.monthly(r))
    assert metrics.longest_drawdown(v) == cal.longest_drawdown(v)
    mx = metrics.monthly(r) - metrics.monthly(b)
    assert metrics.t_and_ir(mx) == cal.t_and_ir(mx)
    assert metrics.t_and_ir(pd.Series([0.01] * 5)) == cal.t_and_ir(pd.Series([0.01] * 5)) == (0.0, 0.0)
    assert metrics.core_metrics(r, rf) == cal.core_metrics(r, rf)
    assert metrics.relative_metrics(r, b, rf, "oneq") == cal.relative_metrics(r, b, rf, "oneq")
    assert metrics.max_drawdown_of_returns(r) == rev.max_drawdown(r)


def test_longest_drawdown_days():
    idx = pd.to_datetime(["2020-01-01", "2020-01-10", "2020-02-01", "2020-03-01", "2020-03-05"])
    assert metrics.longest_drawdown_days(pd.Series([1.0, 0.9, 0.95, 1.01, 0.99], index=idx)) == 60
    assert metrics.longest_drawdown_days(pd.Series([1.0, 1.1, 1.0, 0.9, 0.8], index=idx)) == 55


def test_multiple_testing_matches_originals():
    for n in (1, 6, 18, 231):
        assert criteria.bonferroni_t(n) == qt.bonferroni_t(n)
    trials = RNG.normal(0.05, 0.1, 40)
    want_m = qt.deflated_sharpe(0.3, 120, -0.2, 4.0, trials)
    assert criteria.deflated_sharpe(0.3, 120, -0.2, 4.0, trials) == want_m
    want_w = rev.deflated_sharpe(0.3, 120, -0.2, 4.0, trials)
    assert criteria.deflated_sharpe(0.3, 120, -0.2, 4.0, trials, period="weekly") == want_w
    p = [0.001, 0.2, 0.04, 0.03, 0.5, 0.011]
    assert criteria.bh_reject(p).tolist() == [True, False, False, False, False, True]   # step-up: k = 2
    assert not criteria.bh_reject([0.5, 0.9]).any()


def test_ab_criteria_reproduces_the_three_original_formulas():
    """regime ``evaluate`` (full + halves), megacap ``criteria`` (halves), selective_t ``judge`` (excess vs 0)."""
    for _ in range(2000):
        parts = [{"cagr": float(RNG.normal(0.12, 0.05)), "oneq_cagr": float(RNG.normal(0.12, 0.05)),
                  "dd_shallower_than_oneq_pp": float(RNG.normal(5, 8)), "t_monthly_excess_vs_oneq": float(RNG.normal(1, 1.5))}
                 for _ in range(3)]
        for p in parts:
            p["excess_vs_oneq"] = p["cagr"] - p["oneq_cagr"]
        full, h1, h2 = parts
        a = all(p["cagr"] > p["oneq_cagr"] for p in parts) and full["t_monthly_excess_vs_oneq"] >= 2.0
        b = all(p["dd_shallower_than_oneq_pp"] >= 10.0 and p["cagr"] >= p["oneq_cagr"] - 0.03 for p in parts)
        assert criteria.ab_criteria(parts, full) == (a, b)
        a = all(x["cagr"] > x["oneq_cagr"] for x in (h1, h2)) and full["t_monthly_excess_vs_oneq"] >= 2.0
        b = all(x["dd_shallower_than_oneq_pp"] >= 10.0 and x["cagr"] >= x["oneq_cagr"] - 0.03 for x in (h1, h2))
        assert criteria.ab_criteria([h1, h2], full) == (a, b)
        a = h1["excess_vs_oneq"] > 0 and h2["excess_vs_oneq"] > 0 and full["t_monthly_excess_vs_oneq"] >= 2.0
        b = all(x["dd_shallower_than_oneq_pp"] >= 10.0 and x["excess_vs_oneq"] >= -0.03 for x in (h1, h2))
        assert criteria.ab_criteria((h1, h2), full, cagr="excess_vs_oneq", bench=None) == (a, b)


# ======================================================================== data

def test_guards_match_originals():
    frame = pd.DataFrame({"date": ["2014-12-30", "2014-12-31", "2015-01-02"], "x": [1, 2, 3]})
    pd.testing.assert_frame_equal(guards.truncate_dev(frame, "date", "2014-12-31"),
                                  qt.truncate_dev(frame, "date", "2014-12-31"))
    pd.testing.assert_frame_equal(guards.truncate_window(frame, "date", "2014-12-31", "2015-01-02"),
                                  cs.truncate(frame, "date", "2014-12-31", "2015-01-02"))
    with pytest.raises(guards.FutureDataError):
        guards.assert_dev_dates(frame["date"], "2014-12-31")
    with pytest.raises(guards.DateGuardError):
        guards.assert_window(frame["date"], "2014-12-31", "2015-12-31")
    guards.assert_window([], None, "2000-01-01")


def chart_json(path, n=60, seed=5, split_at=None):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2014-11-03", periods=n, tz="America/New_York") + pd.Timedelta(hours=9, minutes=30)
    close = (100 * np.cumprod(1 + rng.normal(0, 0.01, n))).round(4).tolist()
    close[7] = None                                                    # a row without a close is dropped
    ev = {"dividends": {"1": {"date": int(days[20].timestamp()), "amount": 0.25}}}
    if split_at is not None:
        ev["splits"] = {"2": {"date": int(days[split_at].timestamp()), "numerator": 3, "denominator": 1}}
    res = {"timestamp": [int(d.timestamp()) for d in days], "events": ev, "meta": {"dataGranularity": "1d"},
           "indicators": {"quote": [{"open": close, "high": close, "low": close, "close": close}],
                          "adjclose": [{"adjclose": [None if c is None else c * 0.99 for c in close]}]}}
    path.write_text(json.dumps({"chart": {"result": [res]}}))
    return json.loads(path.read_text())


def test_yahoo_parsers_match_originals(tmp_path):
    p = tmp_path / "chart_X.json"
    payload = chart_json(p, split_at=30)
    end = "2014-12-31"
    pd.testing.assert_frame_equal(yahoo.parse_chart(p, end), qt.parse_chart(p, end))
    pd.testing.assert_frame_equal(yahoo.parse_ohlc(p, end), cal.parse_ohlc(p, end))
    assert yahoo.split_events(p, end) == it.split_events(p, end)
    assert yahoo.split_events(p, "2014-11-05") == it.split_events(p, "2014-11-05") == {}
    df, splits = yahoo.parse_ohlc_payload(payload, end)
    assert df["date"].max() <= end and len(splits) == 1 and df["close"].notna().all()


def test_fill_rf_matches_qqq_timing():
    sessions = pd.bdate_range("2020-01-01", periods=40)
    rf = pd.Series(0.0001, index=sessions[:25])
    dtb3 = pd.Series(0.05, index=sessions[::3])
    pd.testing.assert_series_equal(rates.fill_rf(rf, dtb3, sessions), qt.fill_rf(rf, dtb3, sessions), check_exact=True)


def test_rates_files_match_qqq_timing():
    if not rates.KF_ZIP.exists() or not rates.DTB3_CSV.exists():
        pytest.skip("research cache not available")
    assert rates.KF_ZIP == qt.KF_ZIP and rates.DTB3_CSV == qt.CACHE / "DTB3.csv"
    pd.testing.assert_series_equal(rates.load_kf_rf("2026-09-30"), qt.load_kf_rf("2026-09-30"), check_exact=True)
    pd.testing.assert_series_equal(rates.load_dtb3("2026-09-30"), qt.load_dtb3("2026-09-30"), check_exact=True)


def test_version_paths_and_index_match_originals():
    assert (version.DATA_VERSION, version.INPUTS, version.CACHE) == (dv.DATA_VERSION, dv.INPUTS, dv.CACHE)
    assert version.versioned("a/b/c.csv.gz") == dv.versioned("a/b/c.csv.gz")
    tr = pd.DataFrame({"A": [np.nan, 0.01, -0.02, np.nan], "B": [0.03, np.nan, 0.01, 0.0]})
    close = pd.DataFrame({"A": [np.nan, 10.0, 9.8, np.nan], "B": [5.0, 5.1, np.nan, 5.2]})
    pd.testing.assert_frame_equal(panel.make_index(tr, close), rev.make_index(tr, close))
    pd.testing.assert_frame_equal(panel.make_index(tr), rev.make_index(tr))
    assert panel.TERMINAL_D5 == rev.TERMINAL_D5


def test_calendar_helpers():
    idx = pd.to_datetime(["2020-01-30", "2020-01-31", "2020-02-03", "2020-02-28", "2020-03-02", "2020-03-03"])
    assert calendar.month_end_mask(pd.DatetimeIndex(idx)).tolist() == [False, True, False, True, False, False]
    assert calendar.last_session_of_each_month(pd.DatetimeIndex(idx)) == [idx[1], idx[3]]


def test_oneq_and_panel_loaders_match_originals():
    from quant.data.benchmarks import ONEQ_CHART, oneq_on_sessions
    if not ONEQ_CHART.exists() or not (version.CACHE / "prices/daily_panel.csv.gz").exists():
        pytest.skip("research cache not available")
    from originals import indicators as ind
    from originals import livermore as lv
    want = lv.load_window("test2")
    got = panel.load_window("test2", lv.WINDOWS["test2"])
    for f in ("sig_idx", "perf_idx", "close", "close_adj", "vol_adj", "universe", "terminal_events"):
        pd.testing.assert_frame_equal(getattr(got, f), getattr(want, f), check_exact=True)
    for f in ("last_row", "qqq_close", "qqq_perf_idx"):
        pd.testing.assert_series_equal(getattr(got, f), getattr(want, f), check_exact=True)
    assert got.sessions.equals(want.sessions) and got.spec == want.spec and got.guard == want.guard
    a = oneq_on_sessions(want.sessions, "2011-06-01", "2016-12-31")
    b = ind.oneq_on_sessions(want.sessions, "2011-06-01", "2016-12-31")
    for x, y in zip(a, b):
        pd.testing.assert_series_equal(x, y, check_exact=True)
