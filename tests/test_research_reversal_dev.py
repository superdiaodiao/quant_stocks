"""Offline tests for scripts/research_reversal_dev.py (no cache files are read)."""
import numpy as np
import pandas as pd
import pytest

from scripts import research_reversal_dev as dev


# ---------------------------------------------------------------- date guard

def test_truncate_drops_rows_after_dev_end_and_guard_raises():
    frame = pd.DataFrame({"date": ["2016-12-29", "2016-12-30", "2017-01-03", "2023-05-01"], "x": [1, 2, 3, 4]})
    out = dev.truncate_dev(frame, "date")
    assert out["date"].tolist() == ["2016-12-29", "2016-12-30"]
    dev.assert_dev_dates(out["date"])
    with pytest.raises(dev.FutureDataError):
        dev.assert_dev_dates(["2016-12-30", "2017-01-03"])
    with pytest.raises(dev.FutureDataError):
        dev.assert_dev_dates(pd.DatetimeIndex(["2017-01-03"]))


def test_dev_end_is_2016():
    assert dev.DEV_END == "2016-12-31"


def _toy_data(sessions):
    idx = pd.DatetimeIndex(sessions)
    empty = pd.DataFrame(index=idx)
    return dev.DevData(sessions=idx, universe=pd.DataFrame(), tr=empty, close=empty, split=empty,
                       last_row=pd.Series(dtype="datetime64[ns]"), terminal_events=pd.DataFrame(), ind=empty,
                       qqq=pd.Series(0.0, index=idx))


def test_schedule_drops_week_whose_monday_exit_is_outside_window():
    # last week end 2016-12-30: the Monday after it is not in the truncated calendar, so that week is dropped
    sessions = pd.bdate_range("2016-12-05", "2016-12-30")
    data = _toy_data(sessions)
    week_ends = ["2016-12-09", "2016-12-16", "2016-12-23", "2016-12-30"]
    sched = dev.schedule(data, week_ends, "monday", 1)
    assert [str(x.date()) for _, _, x in sched] == ["2016-12-19", "2016-12-26"]
    fri = dev.schedule(data, week_ends, "friday", 1)
    assert str(fri[-1][2].date()) == "2016-12-30"


# ---------------------------------------------------------------- industry adjustment

def test_industry_adjusted_signal_tiny_example():
    sessions = pd.DatetimeIndex(["2015-01-02", "2015-01-05", "2015-01-06", "2015-01-07", "2015-01-08", "2015-01-09"])
    tr = pd.DataFrame({"A": [0.5, 0.10, -0.05, 0.0, 0.0, 0.02],
                       "B": [0.0, -0.02, -0.02, np.nan, 0.0, 0.0]}, index=sessions)
    ind = pd.DataFrame({7: [0.9, 0.01, 0.01, 0.0, 0.0, 0.0], 12: [0.0, 0.0, 0.0, 0.0, 0.0, -0.01]}, index=sessions)
    windows = dev.week_windows(sessions, ["2015-01-02", "2015-01-09"])
    window = windows[pd.Timestamp("2015-01-09")]
    assert list(window) == list(sessions[1:])          # the formation week excludes the previous week end
    sig = dev.industry_adjusted_signal(tr, ind, window, pd.Series({"A": 7, "B": 12}))
    a = 1.10 * 0.95 * 1.02 - 1 - (1.01 * 1.01 - 1)
    b = 0.98 * 0.98 - 1 - (0.99 - 1)                    # the missing day counts as 0
    assert sig["A"] == pytest.approx(a)
    assert sig["B"] == pytest.approx(b)
    # two names in one industry: same industry return is subtracted, so the ranking is the raw-return ranking
    sig2 = dev.industry_adjusted_signal(tr, ind, window, pd.Series({"A": 7, "B": 7}))
    assert sig2["A"] - sig2["B"] == pytest.approx((1.10 * 0.95 * 1.02) - (0.98 * 0.98))


# ---------------------------------------------------------------- costs

def test_commission_minimum_and_cap():
    small = dev.order_cost(10, 50.0, sell=False, hs=0.0)
    assert small["commission"] == pytest.approx(0.35)           # 10 x 0.0035 = 0.035 -> min 0.35
    big = dev.order_cost(1000, 50.0, sell=False, hs=0.0)
    assert big["commission"] == pytest.approx(3.5)
    tiny = dev.order_cost(1, 20.0, sell=False, hs=0.0)
    assert tiny["commission"] == pytest.approx(0.20)            # 1% of $20 caps the $0.35 minimum
    frac = dev.order_cost(0.4, 150.0, sell=False, hs=0.0)
    assert frac["commission"] == pytest.approx(0.35)
    assert dev.order_cost(0, 50.0, sell=True, hs=0.001)["total"] == 0.0


def test_sell_pays_regulatory_fees_and_spread():
    buy = dev.order_cost(100, 50.0, sell=False, hs=0.0005)
    sell = dev.order_cost(100, 50.0, sell=True, hs=0.0005)
    assert sell["fees"] - buy["fees"] == pytest.approx(5000 * dev.SEC_FEE_PER_DOLLAR_SOLD + 100 * dev.FINRA_TAF_PER_SHARE_SOLD)
    assert buy["spread"] == pytest.approx(2.5)


def test_half_spread_buckets_and_tick_floor():
    assert dev.half_spread(10, 100.0) == pytest.approx(2e-4)
    assert dev.half_spread(240, 100.0) == pytest.approx(6e-4)
    assert dev.half_spread(np.nan, 100.0) == pytest.approx(10e-4)
    assert dev.half_spread(10, 10.0) == pytest.approx(5e-4)      # half a cent on $10 beats 2 bps
    assert dev.half_spread(10, 100.0, mult=2.0) == pytest.approx(4e-4)


# ---------------------------------------------------------------- weekly order count

def test_weekly_order_count():
    current = {"A": 10.0, "B": -5.0, "C": 3.0, "E": 8.0}
    target = {"A": 10.5, "B": 5.0, "D": -4.0, "E": 20.0}
    new, orders = dev.plan_orders(current, target, {})
    # A within the 25% band: no order; B flips side: 2 orders; C exits: 1; D enters: 1; E partial top-up: 1
    assert len(orders) == 5
    assert [o for o in orders if o[0] == "B"] == [("B", 5.0), ("B", 5.0)]
    assert ("C", -3.0) in orders and ("D", -4.0) in orders and ("E", 12.0) in orders
    assert new == {"A": 10.0, "B": 5.0, "D": -4.0, "E": 20.0}


def test_full_turnover_week_counts_two_orders_per_name():
    current = {f"L{i}": 1.0 for i in range(5)} | {f"S{i}": -1.0 for i in range(5)}
    target = {f"L{i + 5}": 1.0 for i in range(5)} | {f"S{i + 5}": -1.0 for i in range(5)}
    _, orders = dev.plan_orders(current, target, {})
    assert len(orders) == 20


def test_make_index_flat_after_terminal():
    s = pd.DatetimeIndex(pd.bdate_range("2015-01-01", periods=5))
    tr = pd.DataFrame({"X": [np.nan, 0.1, -0.5, np.nan, np.nan]}, index=s)
    close = pd.DataFrame({"X": [10.0, 11.0, 5.5, np.nan, np.nan]}, index=s)
    idx = dev.make_index(tr, close)
    assert idx["X"].iloc[0] == pytest.approx(1.0)                 # the first row (no return yet) is a valid entry
    assert np.isnan(dev.make_index(tr)["X"].iloc[0])
    assert idx["X"].iloc[2] / idx["X"].iloc[1] == pytest.approx(0.5)
    assert idx["X"].iloc[4] == idx["X"].iloc[2]
