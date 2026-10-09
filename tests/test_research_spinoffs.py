"""Offline tests for the spin-off study (synthetic data only; no cache files or network)."""
import numpy as np
import pandas as pd
import pytest

from scripts import research_spinoffs as sp
from scripts import research_spinoffs_events as ev


# ------------------------------------------------------------------ SEC text reading

INFO = ("This information statement is being furnished in connection with the distribution by Sears Holdings "
        "Corporation (“Sears Holdings”) to its stockholders of all of the outstanding shares of Lands’ End, Inc. "
        "on a pro rata basis. We expect the shares of Lands’ End common stock to be distributed by Sears Holdings "
        "to you on April 4, 2014 (the “distribution date”). Each holder of record as of 5:30 p.m. Eastern time on "
        "March 24, 2014 (the “record date”) will receive shares. Lands’ End has applied to list its common stock "
        "on The NASDAQ Stock Market under the symbol “LE.”")


def test_reads_distribution_record_parent_symbol_exchange():
    d, _ = ev.find_distribution_date(INFO)
    r, _ = ev.find_record_date(INFO)
    assert d == pd.Timestamp("2014-04-04") and r == pd.Timestamp("2014-03-24")
    p, _ = ev.find_parent(INFO, ["Lands' End, Inc."])
    assert p.startswith("Sears Holdings")
    assert ev.find_symbol(INFO) == "LE"
    assert ev.find_exchange(INFO) == "Nasdaq"
    assert ev.is_spinoff_text(INFO)


def test_not_a_spinoff_without_information_statement():
    assert not ev.is_spinoff_text("Form 10 registration of our common stock, which trades over the counter.")


def test_completion_date_pattern():
    t = ev.no_abbrev_dots("On July 17, 2015, eBay Inc. completed the previously announced separation of PayPal Holdings, Inc.")
    found = ev._dates_near(t, ev.COMPLETE_PATTERNS)
    assert found and found[0][0] == pd.Timestamp("2015-07-17")


def test_first_regular_way_day_is_the_session_after_distribution():
    s = pd.DatetimeIndex(pd.bdate_range("2015-07-13", "2015-07-24"))
    assert ev.next_session(s, pd.Timestamp("2015-07-17")) == pd.Timestamp("2015-07-20")   # Friday -> Monday


# ------------------------------------------------------------------ series choice and costs

def _series(start, n=300, src="yahoo:X"):
    d = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"date": d, "close_raw": 10.0, "volume_raw": 1e6, "tr": 0.0, "source": src})


def test_choose_series_rejects_a_reused_ticker_and_keeps_when_issued_rows():
    start = pd.Timestamp("2016-05-02")
    old = _series("2011-01-03", 3000, "yahoo:OLD")   # another company under the same ticker years before
    wi = _series("2016-04-20", 300, "wiki:NEW")      # when-issued rows 12 days before: allowed
    best, why = sp.choose_series([old, wi], start)
    assert best["source"].iloc[0] == "wiki:NEW" and "yahoo:OLD" in why[0]


def test_half_spread_tiers():
    assert sp.half_spread(1e8, 50) == pytest.approx(5e-4)
    assert sp.half_spread(2e7, 50) == pytest.approx(10e-4)
    assert sp.half_spread(5e6, 50) == pytest.approx(25e-4)
    assert sp.half_spread(1e6, 50) == pytest.approx(50e-4)
    assert sp.half_spread(float("nan"), 50) == pytest.approx(50e-4)
    assert sp.half_spread(1e8, 2.0) == pytest.approx(0.005 / 2.0)   # at least half a tick


def test_regular_way_index_counts_sessions_from_the_first_regular_way_day():
    s = pd.DatetimeIndex(pd.bdate_range("2015-07-20", periods=10))
    assert s[sp.regular_way_index(s, "2015-07-20", 5)] == pd.Timestamp("2015-07-24")
    assert s[sp.regular_way_index(s, "2015-07-20", 1)] == pd.Timestamp("2015-07-20")


# ------------------------------------------------------------------ engine

def _asset(key, sessions, ret=0.0, px=20.0, last=None, terminal=None):
    n = len(sessions)
    p = np.full(n, px, float)
    r = np.full(n, ret, float)
    last_i = n - 1 if last is None else last
    p[last_i + 1:] = np.nan
    r[last_i + 1:] = 0.0
    return sp.Asset(key, p, r, last_i, terminal, np.full(n, 1e8))


def _run(plan, assets, sessions, months=12, slots=10):
    n = len(sessions)
    return sp.simulate(plan, assets, sessions, np.zeros(n), np.full(n, 50.0), months, slots=slots)


def test_flat_prices_lose_only_costs_and_exit_after_twelve_months():
    s = pd.DatetimeIndex(pd.bdate_range("2015-01-01", "2016-12-30"))
    assets = {"spinco_1": _asset("spinco_1", s)}
    plan = [{"key": "spinco_1", "entry_i": 10, "event_id": "e1", "order": (1,)}]
    res = _run(plan, assets, s)
    t = res["trades"]
    assert len(t) == 1 and t.iloc[0]["exit_kind"] == "planned"
    exit_d = pd.Timestamp(t.iloc[0]["exit"])
    assert exit_d == s[s.searchsorted(s[10] + pd.DateOffset(months=12))]
    assert 9_950 < res["nav"].iloc[-1] < ACCOUNT_
    assert res["exposure"].iloc[20] == pytest.approx(0.1, abs=0.01)


ACCOUNT_ = sp.ACCOUNT


def test_slots_are_first_come_first_served():
    s = pd.DatetimeIndex(pd.bdate_range("2015-01-01", "2015-12-31"))
    assets = {f"spinco_{k}": _asset(f"spinco_{k}", s) for k in range(3)}
    plan = [{"key": f"spinco_{k}", "entry_i": 5 + k, "event_id": f"e{k}", "order": (k,)} for k in range(3)]
    res = _run(plan, assets, s, slots=2)
    assert res["skipped_full"] == 1
    assert set(res["trades"]["event_id"]) == {"e0", "e1"}


def test_delisting_books_the_terminal_return_on_the_next_session():
    s = pd.DatetimeIndex(pd.bdate_range("2015-01-01", "2015-12-31"))
    assets = {"spinco_1": _asset("spinco_1", s, last=50, terminal=-0.55)}
    plan = [{"key": "spinco_1", "entry_i": 10, "event_id": "e1", "order": (1,)}]
    res = _run(plan, assets, s)
    t = res["trades"].iloc[0]
    assert t["exit_kind"] == "delisted" and t["exit"] == s[51].date().isoformat()
    assert t["ret"] == pytest.approx(-0.55, abs=0.01)
    # about 10% of the account lost 55%
    assert res["nav"].iloc[-1] == pytest.approx(ACCOUNT_ * (1 - 0.1 * 0.55), rel=0.01)


def test_monthly_rebalance_trims_a_winner_back_to_one_slot():
    s = pd.DatetimeIndex(pd.bdate_range("2015-01-01", "2015-06-30"))
    a = _asset("spinco_1", s, ret=0.0)
    a.ret[15] = 1.0          # doubles on one day
    a.px[15:] = 40.0
    plan = [{"key": "spinco_1", "entry_i": 2, "event_id": "e1", "order": (1,)}]
    res = _run(plan, {"spinco_1": a}, s)
    assert res["exposure"].iloc[-1] == pytest.approx(0.1, abs=0.015)


def test_events_before_a_segment_are_not_moved_into_it():
    s = pd.DatetimeIndex(pd.bdate_range("2019-01-02", periods=30))
    assert sp.regular_way_index(s, "2018-12-20", 5) is None
    assert sp.regular_way_index(s, "2019-03-01", 5) is None
