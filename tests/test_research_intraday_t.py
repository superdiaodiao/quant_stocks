"""Synthetic tests for scripts/research_intraday_t.py (no vendor data needed)."""
import math

import numpy as np
import pandas as pd
import pytest

from scripts import research_intraday_t as m


def make(o, h, lo, c, div=None, split=None, atr=0.02):
    n = len(c)
    dates = pd.bdate_range("2020-01-01", periods=n)
    c = np.asarray(c, float)
    split = np.ones(n) if split is None else np.asarray(split, float)
    tr = pd.Series(c, index=dates).pct_change()
    return m.Series_("QQQ", dates, np.asarray(o, float), np.asarray(h, float), np.asarray(lo, float), c,
                     np.r_[np.nan, c[:-1]] / split, split, np.zeros(n) if div is None else np.asarray(div, float),
                     np.full(n, atr), tr, {})


def q_reserve(p=100.0, cap=None):
    b = math.floor(0.75 * (m.START_EQUITY - 5) / p)
    cash = m.START_EQUITY - b * p
    return min(b // 3, math.floor((cash - m.CASH_BUFFER) / (cap if cap else m.CAP_MULT * p)))


def test_cent_rounding():
    assert m.ceil_cent(101.001) == pytest.approx(101.01)
    assert m.ceil_cent(101.00) == pytest.approx(101.00)
    assert m.floor_cent(98.999) == pytest.approx(98.99)
    assert m.floor_cent(99.00) == pytest.approx(99.00)


def test_fill_t1_rules():
    assert m.fill_t1(102.0, 103.0, 101.0, True) == 102.0            # gap above the limit: opening auction
    assert m.fill_t1(100.0, 101.06, 101.0, True) == 101.0           # traded through by >= 0.05%
    assert m.fill_t1(100.0, 101.03, 101.0, True) is None            # touched, not through
    assert m.fill_t1(100.0, 101.0, 101.0, False) == 101.0           # touch fill (report-only variant)
    assert m.fill_t1(10.0, 10.105, 10.10, True) is None             # one tick needed on a cheap price
    assert m.fill_t1(10.0, 10.11, 10.10, True) == 10.10


def test_fill_t2_rules():
    assert m.fill_t2(98.0, 97.0, 99.0, True) == 98.0
    assert m.fill_t2(100.0, 98.94, 99.0, True) == 99.0
    assert m.fill_t2(100.0, 98.97, 99.0, True) is None


def test_hold_has_no_trips_and_tracks_price():
    d = make([100] * 5, [101] * 5, [99] * 5, [100, 102, 104, 103, 105])
    sim = m.simulate(d, 1, 4, m.Spec(None, costs=False))
    assert sim["trips"].empty
    b = math.floor(0.75 * (m.START_EQUITY - 5) / 100)
    cash = m.START_EQUITY - b * 100
    assert sim["value"].iloc[-1] == pytest.approx(b * 105 + cash)


def test_t1_fill_pnl_and_no_fill():
    # day 1: prev close 100, limit 101.00, high 102 -> fill at 101, close 100.5 -> gross q * 0.5
    # day 2: prev close 100.5, limit 101.51, high 101.52 -> not through (need 101.56) -> no trade
    d = make([100, 100.2, 100.6], [100, 102.0, 101.52], [100, 99.8, 100.1], [100, 100.5, 101.0])
    sim = m.simulate(d, 1, 2, m.Spec("T1", "k1", costs=False))
    q = q_reserve()
    assert len(sim["trips"]) == 1
    assert sim["trips"]["gross"].iloc[0] == pytest.approx(q * 0.5)
    h = m.simulate(d, 1, 2, m.Spec(None, costs=False))
    assert sim["value"].iloc[-1] - h["value"].iloc[-1] == pytest.approx(q * 0.5)


def test_t1_gap_open_fills_at_open():
    d = make([100, 103.0], [100, 104.0], [100, 102.0], [100, 103.5])
    sim = m.simulate(d, 1, 1, m.Spec("T1", "k1", costs=False))
    assert sim["trips"]["gross"].iloc[0] == pytest.approx(q_reserve() * (103.0 - 103.5))


def test_t2_fill_pnl():
    d = make([100, 99.5], [100, 100.0], [100, 98.0], [100, 99.6])
    sim = m.simulate(d, 1, 1, m.Spec("T2", "k1", costs=False))
    q = q_reserve(cap=99.0)
    assert sim["trips"]["gross"].iloc[0] == pytest.approx(q * (99.6 - 99.0))


def test_t3_is_minus_a_third_of_the_intraday_leg():
    d = make([100, 101, 99], [100, 103, 101], [100, 100, 97], [100, 102, 98])
    sim = m.simulate(d, 1, 2, m.Spec("T3", costs=False))
    h = m.simulate(d, 1, 2, m.Spec(None, costs=False))
    q1 = q_reserve()
    cash1 = m.START_EQUITY - math.floor(0.75 * (m.START_EQUITY - 5) / 100) * 100 + q1 * (101 - 102)
    q2 = min(math.floor(0.75 * (m.START_EQUITY - 5) / 100) // 3, math.floor((cash1 - m.CASH_BUFFER) / (1.10 * 102)))
    assert sim["value"].iloc[-1] - h["value"].iloc[-1] == pytest.approx(q1 * (101 - 102) + q2 * (99 - 98))


def test_costs_reduce_value_and_count_orders():
    d = make([100, 101, 99], [100, 103, 101], [100, 100, 97], [100, 102, 98])
    net = m.simulate(d, 1, 2, m.Spec("T3"))
    gross = m.simulate(d, 1, 2, m.Spec("T3", costs=False))
    assert net["value"].iloc[-1] < gross["value"].iloc[-1]
    assert net["orders"] == 1 + 4
    assert net["value"].iloc[0] == m.START_EQUITY


def test_reserve_limits_quantity_and_flags_gfv():
    # close jumps 15% above the previous close: buy-back exceeds the 1.10 * P reserve sizing
    d = make([100, 101.0], [100, 116.0], [100, 100.5], [100, 115.0])
    sim = m.simulate(d, 1, 1, m.Spec("T1", "k1", costs=False))
    assert sim["gfv_days"] == 1
    q = sim["trips"]["q"].iloc[0]
    b = math.floor(0.75 * (m.START_EQUITY - 5) / 100)
    cash = m.START_EQUITY - b * 100
    assert q == min(b // 3, math.floor((cash - m.CASH_BUFFER) / 110.0))


def test_split_and_dividend():
    # 2:1 split on day 2: real prices halve, shares double; dividend 1.0 per (post-split) share on day 3
    d = make([100, 100, 50, 50], [100, 100, 50, 50], [100, 100, 50, 50], [100, 100, 50, 50],
             div=[0, 0, 0, 1.0], split=[1, 1, 2, 1])
    sim = m.simulate(d, 1, 3, m.Spec(None, costs=False))
    b = math.floor(0.75 * (m.START_EQUITY - 5) / 100)
    assert sim["final_base"] == 2 * b
    assert sim["value"].iloc[-1] == pytest.approx(m.START_EQUITY + 2 * b * 1.0)


def test_real_factor_and_atr_shift():
    dates = pd.bdate_range("2020-01-01", periods=4)
    f = m.real_factor(dates, {str(dates[2].date()): 2.0})
    assert list(f) == [2.0, 2.0, 1.0, 1.0]
    n = 30
    df = pd.DataFrame({"date": pd.bdate_range("2020-01-01", periods=n).strftime("%Y-%m-%d"),
                       "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "adjclose": 100.0, "dividend": 0.0})
    d = m.build("QQQ", df, {})
    assert np.isnan(d.atr_pct[20]) and d.atr_pct[21] == pytest.approx(0.02)   # 20 true ranges up to t-1


def test_rebalance_band():
    c = [100] * 3 + [200] * 3
    d = make(c, c, c, c)
    sim = m.simulate(d, 1, 5, m.Spec(None, costs=False))
    assert sim["final_base"] == math.floor(0.75 * (m.START_EQUITY - 5) / 100)   # same month: no rebalance
    dates = pd.DatetimeIndex(["2020-01-29", "2020-01-30", "2020-01-31", "2020-02-03", "2020-02-04"])
    d2 = make([100, 100, 200, 200, 200], [100, 100, 200, 200, 200], [100, 100, 200, 200, 200],
              [100, 100, 200, 200, 200])
    d2.sessions = dates
    sim2 = m.simulate(d2, 1, 4, m.Spec(None, costs=False))
    b0 = math.floor(0.75 * (m.START_EQUITY - 5) / 100)
    v = b0 * 200 + m.START_EQUITY - b0 * 100
    assert sim2["final_base"] == math.floor(0.75 * (v - 5) / 200)


def test_judge():
    base = {"excess_vs_h75": 0.01, "excess_vs_oneq": 0.01, "t_monthly_excess_vs_h75": 2.5,
            "t_monthly_excess_vs_oneq": 2.1, "dd_shallower_than_oneq_pp": 0.0}
    j = m.judge({"half1": base, "half2": base, "full": base})
    assert j["adds_value_vs_h75"] and j["A"] and not j["B"]
    bad = dict(base, excess_vs_h75=-0.001)
    assert not m.judge({"half1": base, "half2": bad, "full": base})["adds_value_vs_h75"]
