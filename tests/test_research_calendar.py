"""Tests for scripts/research_calendar.py: calendars, leg split, execution timing, costs, metrics, FOMC source."""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

import scripts.research_calendar as rc


def synthetic(sessions: pd.DatetimeIndex, seed: int = 0, div_day: int | None = None) -> rc.Data:
    rng = np.random.default_rng(seed)
    n = len(sessions)
    close = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, n))
    open_ = close * (1 + rng.normal(0, 0.004, n))
    div = np.zeros(n)
    if div_day is not None:
        div[div_day] = 0.5
    # total-return index: (close + div) / prev close
    tr = np.ones(n)
    for k in range(1, n):
        tr[k] = tr[k - 1] * (close[k] + div[k]) / close[k - 1]
    adj = pd.Series(tr, index=sessions)
    op, cl = pd.Series(open_, index=sessions), pd.Series(close, index=sessions)
    r, o, i = rc.split_legs(adj, op, cl)
    oneq = pd.Series(rng.normal(0.0003, 0.01, n), index=sessions)
    oneq.iloc[0] = np.nan
    rf = pd.Series(0.0001, index=sessions)
    return rc.Data(sessions=sessions, r=r, o=o, i=i, open=op, close=cl, oneq_r=oneq, oneq_close=cl.copy(), rf=rf,
                   raw={}, guard={})


# ---------------------------------------------------------------- leg split

def test_legs_multiply_to_total_and_dividend_is_overnight():
    s = pd.bdate_range("2020-01-01", periods=50)
    d = synthetic(s, div_day=10)
    err = ((1 + d.o) * (1 + d.i) - 1 - d.r).abs().max()
    assert err < 1e-12
    # on the ex-dividend day the intraday leg excludes the dividend, the overnight leg includes it
    k = s[10]
    assert d.i[k] == pytest.approx(d.close[k] / d.open[k] - 1)
    # o = (1 + r) / (1 + i) - 1 = open * (close + div) / close / prev_close - 1 (about (open + div) / prev_close - 1)
    assert d.o[k] == pytest.approx(d.open[k] * (d.close[k] + 0.5) / d.close[k] / d.close[s[9]] - 1)
    assert abs(d.o[k] - ((d.open[k] + 0.5) / d.close[s[9]] - 1)) < 1e-4


# ---------------------------------------------------------------- calendars

def test_turn_of_month_window():
    s = pd.bdate_range("2024-01-02", "2024-03-29")
    h = pd.Series(rc.hold_c1(s), index=s)
    held = set(h.index[h].strftime("%Y-%m-%d"))
    assert {"2024-01-31", "2024-02-01", "2024-02-02", "2024-02-05"} <= held
    assert "2024-01-30" not in held and "2024-02-06" not in held
    assert {"2024-02-29", "2024-03-01", "2024-03-04", "2024-03-05", "2024-03-29"} <= held


def test_sell_in_may_months():
    s = pd.bdate_range("2023-04-27", "2023-11-03")
    h = pd.Series(rc.hold_c2(s), index=s)
    assert h["2023-04-28"] and not h["2023-05-01"] and not h["2023-10-31"] and h["2023-11-01"]


def test_holidays_and_pre_holiday():
    s = pd.bdate_range("2024-06-24", "2024-07-12")
    s = s.drop([pd.Timestamp("2024-07-04")])
    hol = rc.scheduled_holidays(s)
    assert list(hol.strftime("%Y-%m-%d")) == ["2024-07-04"]
    h = pd.Series(rc.hold_c3(s, hol), index=s)
    assert h.index[h].strftime("%Y-%m-%d").tolist() == ["2024-07-03"]
    # an unscheduled closure is not a holiday
    s2 = pd.bdate_range("2012-10-22", "2012-11-02").drop(pd.DatetimeIndex(["2012-10-29", "2012-10-30"]))
    assert len(rc.scheduled_holidays(s2)) == 0
    assert not rc.hold_c3(s2, rc.scheduled_holidays(s2)).any()


def test_monday_skip_and_holiday_monday():
    s = pd.bdate_range("2024-05-20", "2024-06-07").drop(pd.Timestamp("2024-05-27"))   # Memorial Day
    h = pd.Series(rc.hold_c6(s), index=s)
    skipped = h.index[~h].strftime("%Y-%m-%d").tolist()
    assert skipped == ["2024-05-28", "2024-06-03"]       # the first data session is never skipped


def test_fomc_days():
    s = pd.bdate_range("2024-01-02", "2024-03-29")
    h = pd.Series(rc.hold_c7(s), index=s)
    assert h.index[h].strftime("%Y-%m-%d").tolist() == ["2024-01-30", "2024-01-31", "2024-03-19", "2024-03-20"]


def test_fomc_list_shape():
    d = pd.DatetimeIndex(rc.FOMC_DATES)
    assert d.is_monotonic_increasing and d.is_unique
    counts = pd.Series(1, index=d).groupby(d.year).sum()
    assert (counts.drop(2020) == 8).all() and counts[2020] == 7
    assert (d.weekday < 5).all()
    for unscheduled in ("2001-01-03", "2001-04-18", "2001-09-17", "2008-01-22", "2008-10-08", "2020-03-03",
                        "2020-03-15"):
        assert pd.Timestamp(unscheduled) not in d


def test_meeting_label_parsing():
    f = rc._meeting_last_day
    assert str(f(2005, "February 1-2")) == "2005-02-02"
    assert str(f(1999, "March 30")) == "1999-03-30"
    assert str(f(2013, "April/May 30-1")) == "2013-05-01"
    assert str(f(2017, "Jan/Feb 31-1")) == "2017-02-01"
    assert str(f(2012, "July 31-August 1")) == "2012-08-01"
    assert str(f(2024, "March 19-20*")) == "2024-03-20"


def test_fomc_constant_matches_fed_pages():
    if not (rc.RAW / "fomccalendars.htm").exists():
        pytest.skip("Federal Reserve pages not downloaded")
    assert rc.parse_fed_pages(rc.RAW) == list(rc.FOMC_DATES)


# ---------------------------------------------------------------- simulation

@pytest.fixture
def data():
    return synthetic(pd.bdate_range("2021-01-04", periods=300), seed=3)


def test_gross_overnight_intraday_and_buyhold(data):
    s, e = 5, 250
    for name, leg in (("C4", data.o), ("C5", data.i), ("QQQ", data.r)):
        seg = rc.segments(name, data.sessions)
        sim = rc.simulate(seg, data, s, e, "C", costs=False)
        assert sim["value"].iloc[-1] / rc.START_EQUITY == pytest.approx(np.prod(1 + leg.iloc[s: e + 1].values))


def test_gross_close_to_close_rule_earns_only_held_days(data):
    s, e = 5, 250
    seg = rc.segments("C1", data.sessions)
    sim = rc.simulate(seg, data, s, e, "C", costs=False)
    h = seg[s: e + 1, 0]
    assert sim["value"].iloc[-1] / rc.START_EQUITY == pytest.approx(np.prod(1 + data.r.iloc[s: e + 1].values[h]))
    simb = rc.simulate(seg, data, s, e, "B", costs=False)
    bill = data.rf.iloc[s: e + 1].values - rc.CASH_ETF_FEE / rc.TRADING_DAYS
    expect = np.prod(np.where(h, 1 + data.r.iloc[s: e + 1].values, 1 + bill))
    assert simb["value"].iloc[-1] / rc.START_EQUITY == pytest.approx(expect)


def test_execution_entry_at_previous_close(data):
    """Holding day t's return requires buying at the close of t-1: the trade sits at the close before."""
    s = 20
    seg = np.zeros((len(data.sessions), 2), dtype=bool)
    seg[s + 3] = True                                  # hold exactly one session
    sim = rc.simulate(seg, data, s, s + 6, "C", costs=False)
    r = sim["ret"]
    assert (r.drop(data.sessions[s + 3]) == 0).all()
    assert r[data.sessions[s + 3]] == pytest.approx(data.r.iloc[s + 3])
    assert sim["switches"] == 2


def test_cost_counts_overnight(data):
    s, e = 5, 104
    seg = rc.segments("C4", data.sessions)
    sim = rc.simulate(seg, data, s, e, "C", costs=True)
    # initial buy + (sell at open, buy at close) each session except no buy at the close of e
    assert sim["orders"] == 1 + 2 * (e - s + 1) - 1
    gross = rc.simulate(seg, data, s, e, "C", costs=False)
    assert sim["value"].iloc[-1] < gross["value"].iloc[-1]
    # each order costs at least the $0.35 minimum plus 1 bp half spread on ~$10k
    per_order = (math.log(gross["value"].iloc[-1]) - math.log(sim["value"].iloc[-1])) / sim["orders"]
    assert per_order > 0.35 / 10_000 + 1e-4 - 1e-6


def test_switch_cost_orders():
    c, n = rc.switch_cost(10_000, "Q", "B", 400.0)
    assert n == 2 and c > 2 * 0.35
    c, n = rc.switch_cost(10_000, "Q", "C", 400.0)
    assert n == 1
    c, n = rc.switch_cost(10_000, "C", "Q", 400.0)
    assert n == 1 and c == pytest.approx(0.35 + 0.0005 * 25 + 1.0)


def test_bill_switch_at_open_is_rejected(data):
    seg = rc.segments("C5", data.sessions)
    with pytest.raises(ValueError):
        rc.simulate(seg, data, 5, 20, "B")


# ---------------------------------------------------------------- metrics

def test_longest_drawdown():
    v = pd.Series([1, 2, 1.5, 1.8, 2.0, 2.5, 2.4, 2.3])
    assert rc.longest_drawdown(v) == 2           # back at the old peak 2.0 ends the first spell


def test_criteria():
    base = {"excess_vs_oneq": 0.01, "t_monthly_excess_vs_oneq": 2.1, "dd_shallower_than_oneq_pp": 0.0}
    assert rc.criteria(base)["A"] and rc.criteria(base)["pass"]
    assert not rc.criteria({**base, "t_monthly_excess_vs_oneq": 1.9})["pass"]
    b = {"excess_vs_oneq": -0.029, "t_monthly_excess_vs_oneq": -1.0, "dd_shallower_than_oneq_pp": 10.0}
    assert rc.criteria(b)["B"]
    assert not rc.criteria({**b, "excess_vs_oneq": -0.031})["pass"]


def test_evaluate_period_runs(data):
    seg = rc.segments("C2", data.sessions)
    out = rc.evaluate_period("C2", seg, data, 5, 250, "B", True)
    m = out["metrics"]
    for k in ("cagr", "excess_vs_qqq", "excess_vs_oneq", "vol", "max_dd", "longest_dd_sessions", "sharpe", "sortino",
              "calmar", "ir_vs_oneq", "beta_vs_oneq", "alpha_vs_oneq", "share_years_beating_oneq", "worst_year",
              "worst_month", "switches_per_year", "time_in_qqq", "cost_drag_per_year"):
        assert k in m and m[k] == m[k]


# ---------------------------------------------------------------- date guard

def test_parse_ohlc_truncates(tmp_path):
    ts = pd.to_datetime(["2026-09-29 13:30", "2026-09-30 13:30", "2026-10-01 13:30"]).tz_localize("UTC")
    j = {"chart": {"result": [{"timestamp": [int(x.timestamp()) for x in ts],
                               "indicators": {"quote": [{"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
                                                         "close": [1, 2, 3], "volume": [1, 1, 1]}],
                                              "adjclose": [{"adjclose": [1, 2, 3]}]}}]}}
    p = tmp_path / "chart.json"
    p.write_text(json.dumps(j))
    df = rc.parse_ohlc(p, "2026-09-30")
    assert df["date"].tolist() == ["2026-09-29", "2026-09-30"] and df["open"].tolist() == [1, 2]
