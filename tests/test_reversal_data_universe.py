"""Tests for plan step 12, the weekly universe and its completeness checks (scripts/reversal_data_universe.py)."""
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts import reversal_data_universe as un


def _interval(**kwargs):
    row = {"security_id": "1", "ticker": "AAA", "start": "2012-02-01", "end": "2013-01-02", "end_next_absent": "",
           "start_prev_absent": "", "name_in_source": "A Inc - Common Stock", "share_class": "COMMON"}
    row.update(kwargs)
    return row


def _master(rows):
    base = {"security_id": "", "delist_date": "", "transfer_date": ""}
    return pd.DataFrame([{**base, **r} for r in rows])


# ------------------------------------------------------------------ calendar and listed set

def test_calendar_has_759_weeks_and_each_previous_week_end():
    sessions, weeks, week_pos, prev_pos = un.calendar()
    assert len(weeks) == 759 and weeks[0] == pd.Timestamp("2012-01-06") and weeks[-1] == pd.Timestamp("2026-07-17")
    assert sessions[prev_pos[0]] == pd.Timestamp("2011-12-30")
    assert (sessions[prev_pos[1:]] == weeks[:-1]).all()
    assert (sessions[week_pos] == weeks).all()


def test_listing_span_runs_to_the_day_before_the_next_absent_snapshot_and_stops_before_form25():
    intervals = pd.DataFrame([
        _interval(security_id="1", end="2014-03-04", end_next_absent="2014-06-05"),
        _interval(security_id="2", end="2026-07-01"),
        _interval(security_id="3", end="2013-05-26", end_next_absent="2013-08-03"),
        _interval(security_id="4", end="2026-06-01", end_next_absent="2026-07-01"),
    ])
    master = _master([{"security_id": "1"}, {"security_id": "2"}, {"security_id": "3", "delist_date": "2013-06-10"},
                      {"security_id": "4", "transfer_date": "2026-06-21"}])
    spans = un.listing_spans(intervals, master).set_index("security_id")
    assert spans.loc["1", "list_end"] == "2014-06-04"
    assert spans.loc["2", "list_end"] == un.WINDOW_END
    # Plan 3.1: listed while t is before the Form 25 effective date (or the transfer away).
    assert spans.loc["3", "list_end"] == "2013-06-09" and spans.loc["3", "cut"] == "form25"
    assert spans.loc["4", "list_end"] == "2026-06-20" and spans.loc["4", "cut"] == "transfer"


def test_an_interval_that_starts_after_the_delisting_is_a_later_listing():
    intervals = pd.DataFrame([_interval(start="2012-02-01", end="2013-01-02", end_next_absent="2013-02-01"),
                              _interval(start="2015-03-01", end="2016-01-02", end_next_absent="2016-02-01")])
    master = _master([{"security_id": "1", "delist_date": "2013-01-20"}])
    spans = un.listing_spans(intervals, master)
    assert spans["list_end"].tolist() == ["2013-01-19", "2016-01-31"]
    assert spans["after_cut"].tolist() == [False, True]


def test_ipo_rule_starts_at_the_first_price_between_two_snapshots():
    intervals = pd.DataFrame([_interval(start="2012-03-15", start_prev_absent="2012-02-01"),
                              _interval(security_id="2", ticker="BBB", start="2012-03-15", start_prev_absent="2012-02-01")])
    master = _master([{"security_id": "1"}, {"security_id": "2"}])
    # 1 first trades between the snapshots (an IPO); 2 traded before the earlier snapshot (a transfer).
    spans = un.listing_spans(intervals, master, {"1": "2012-02-24", "2": "2011-06-01"}).set_index("security_id")
    assert spans.loc["1", "list_start"] == "2012-02-24" and spans.loc["1", "ipo_start"]
    assert spans.loc["2", "list_start"] == "2012-03-15" and not spans.loc["2", "ipo_start"]


def test_ipo_rule_is_not_applied_when_the_series_starts_where_step_9_cut_it():
    sessions = pd.bdate_range("2012-01-02", "2012-04-30")
    intervals = pd.DataFrame([_interval(start="2012-03-15", start_prev_absent="2012-02-01")])
    master = _master([{"security_id": "1"}])
    # The first session after the earlier snapshot is the first day step 9 keeps: a cut, not an IPO.
    spans = un.listing_spans(intervals, master, {"1": "2012-02-02"}, sessions=sessions)
    assert spans["list_start"].iloc[0] == "2012-03-15"
    assert spans["ipo_boundary"].iloc[0] and not spans["ipo_start"].iloc[0]
    spans = un.listing_spans(intervals, master, {"1": "2012-02-03"}, sessions=sessions)
    assert spans["list_start"].iloc[0] == "2012-02-03" and spans["ipo_start"].iloc[0]


def test_weekly_listed_keeps_the_newer_ticker_and_marks_foreign_weeks_and_non_common_names():
    weeks = pd.DatetimeIndex(["2012-01-06", "2012-01-13", "2012-01-20", "2012-01-27"])
    spans = pd.DataFrame([
        {"security_id": "1", "ticker": "OLD", "list_start": "2012-01-01", "list_end": "2012-01-20", "non_common": False},
        {"security_id": "1", "ticker": "NEW", "list_start": "2012-01-13", "list_end": "2012-12-31", "non_common": False},
        {"security_id": "2", "ticker": "FGN", "list_start": "2012-01-01", "list_end": "2012-12-31", "non_common": False},
        {"security_id": "3", "ticker": "UNT", "list_start": "2012-01-01", "list_end": "2012-01-10", "non_common": True},
    ])
    listed = un.weekly_listed(spans, weeks, {"2": [("2012-01-12", "2012-01-21")]}, shells={"9"})
    one = listed[listed["security_id"] == "1"]
    assert one["ticker"].tolist() == ["OLD", "NEW", "NEW", "NEW"]
    two = listed[listed["security_id"] == "2"].set_index("week_end")
    assert two["foreign"].tolist() == [False, True, True, False]
    assert two["eligible"].tolist() == [True, False, False, True]
    three = listed[listed["security_id"] == "3"]
    assert len(three) == 1 and not three["eligible"].iloc[0]
    assert not listed.duplicated(["security_id", "week_index"]).any()


# ------------------------------------------------------------------ canonical metrics and ranks

def _grid(n_sessions=60):
    sessions = pd.bdate_range("2012-01-02", periods=n_sessions)
    every = pd.Series(sessions, index=sessions).groupby(sessions.to_period("W-SUN")).max()
    weeks = pd.DatetimeIndex(every.values)
    week_pos = sessions.get_indexer(weeks)
    prev_pos = np.r_[-1, week_pos[:-1]]
    return sessions, weeks, week_pos, prev_pos


def test_canonical_medians_need_enough_rows_and_the_week_close_may_be_five_sessions_old():
    sessions, weeks, week_pos, prev_pos = _grid()
    rows = []
    for k, day in enumerate(sessions):
        rows.append(("A", day, 20.0, 1000.0 * (k + 1), "wiki"))  # every session
        if k < 26:
            rows.append(("B", day, 5.0, 100.0, "yahoo"))  # stops after session 26
    panel = pd.DataFrame(rows, columns=["security_id", "date", "close_raw", "volume_raw", "src_primary"])
    m = un.canonical_metrics(panel, sessions, week_pos, prev_pos)
    a, b = list(m["ids"]).index("A"), list(m["ids"]).index("B")
    last = len(weeks) - 1
    # A's dv50 at the last week is the median of the last 50 sessions' dollar volume.
    p = week_pos[last]
    expected = np.median([20.0 * 1000.0 * (k + 1) for k in range(p - 49, p + 1)])
    assert m["dv50"][last, a] == pytest.approx(expected)
    # B has 26 rows: dv20 once 10 rows exist, dv50 once 25 do.
    first_week_b = 0
    assert np.isnan(m["dv20"][first_week_b, b])
    week_with_26 = int(np.searchsorted(week_pos, 25))
    assert not np.isnan(m["dv50"][week_with_26, b])
    # B's last row is session 25; the week close is kept while it is at most 5 sessions old.
    for w, pos in enumerate(week_pos):
        lag = pos - 25
        if 0 <= lag <= 5:
            assert m["close"][w, b] == 5.0 and m["lag"][w, b] == lag
        elif lag > 5:
            assert np.isnan(m["close"][w, b])
    assert m["in_week"][:, a].all() and m["on_week_end"][:, a].all()
    assert m["first_row"]["B"] == sessions[0].strftime("%Y-%m-%d")
    assert m["last_row"]["B"] == sessions[25].strftime("%Y-%m-%d")


def test_a_close_from_the_previous_week_is_not_a_close_in_the_week():
    sessions, weeks, week_pos, prev_pos = _grid(15)
    days = [sessions[k] for k in range(15) if k not in (5, 6, 7, 8, 9)]  # nothing in the second week
    panel = pd.DataFrame({"security_id": "A", "date": days, "close_raw": 12.0, "volume_raw": 10.0, "src_primary": "tiingo"})
    m = un.canonical_metrics(panel, sessions, week_pos, prev_pos)
    assert m["in_week"][:, 0].tolist() == [True, False, True]
    assert m["lag"][1, 0] == 5 and m["close"][1, 0] == 12.0  # still the week's (stale) close


def test_ranks_apply_the_ten_dollar_test_and_break_ties_by_security_id():
    listed = pd.DataFrame({
        "week_index": [0, 0, 0, 0, 0],
        "security_id": ["4", "2", "3", "1", "5"],
        "eligible": [True, True, True, True, False],
        "close": [9.99, 10.0, 50.0, 30.0, 100.0],
        "dv20": [9e9, 5e6, 5e6, 1e6, 8e9],
        "dv50": [9e9, 5e6, 5e6, 1e6, 8e9],
    })
    ranked = un.rank_weeks(listed).set_index("security_id")
    assert ranked.loc["4", "price_ge_10"] == "N" and np.isnan(ranked.loc["4", "dv50_rank"])
    assert ranked.loc["4", "dv50_rank_any_price"] == 1
    assert ranked.loc["2", "dv50_rank"] == 1 and ranked.loc["3", "dv50_rank"] == 2  # tie: lower id first
    assert ranked.loc["1", "dv50_rank"] == 3
    assert np.isnan(ranked.loc["5", "dv50_rank"]) and np.isnan(ranked.loc["5", "dv50_rank_any_price"])


# ------------------------------------------------------------------ missing names and estimates

def test_security_reasons_follow_the_precedence():
    master = pd.DataFrame({"security_id": ["a", "b", "c", "d", "e", "f", "g"]})
    candidates = pd.DataFrame({"security_id": ["a", "b", "c", "f"], "planned_source": ["tiingo", "tiingo", "tiingo", "yahoo"]})
    unfillable = pd.DataFrame({"security_id": ["d"]})
    no_series = pd.DataFrame({"security_id": ["e"], "reason": ["no_vendor_source"]})
    status = pd.DataFrame({"security_id": ["b", "c"], "status": ["done", "wrong_entity"],
                           "updated_utc": ["2026-10-02T01", "2026-10-02T02"]})
    reasons = un.security_reasons(master, candidates, unfillable, no_series, status, series_ids={"b"})
    # f is planned for Yahoo and the Yahoo run has not answered it yet.
    assert reasons == {"a": "tiingo_pending", "b": "series_gap", "c": "unfillable", "d": "unfillable",
                       "e": "no_vendor_source", "f": "yahoo_pending", "g": "not_candidate"}
    answered = pd.DataFrame({"security_id": ["f"], "verdict": ["ok"]})
    reasons = un.security_reasons(master, candidates, unfillable, no_series, status, series_ids={"b"},
                                  yahoo_report=answered)
    assert reasons["f"] == "candidate_other"


def test_yahoo_answers_newer_than_the_panel_are_pending_reconcile():
    report = pd.DataFrame({"security_id": ["y1", "y2", "y3"], "symbol": ["Y1", "Y2", "Y3"],
                           "verdict": ["ok", "no_rows", "ok"], "first_row": ["2011-06-01", "2025-09-29", "2011-06-01"],
                           "last_row": ["2026-08-31", "2026-08-31", "2026-08-31"]})
    status = pd.DataFrame({"symbol": ["Y1", "Y2", "Y3"], "fetched_utc": ["2026-10-03T01:00:00+00:00",
                                                                        "2026-10-03T01:00:00+00:00",
                                                                        "2026-10-01T01:00:00+00:00"]})
    answers = un.yahoo_answers(report, status)
    assert answers["status"].tolist() == ["done", "no_data", "done"]
    assert un.recent_fetches(answers, "2026-10-02T05:04:50+00:00") == {"y1"}


def test_binned_rates_use_the_year_when_it_has_rows_and_pool_otherwise():
    rng = np.random.default_rng(0)
    years = np.r_[np.full(200, 2012), np.full(5, 2013)]
    ratio = np.r_[np.full(100, 1.2), np.full(100, 0.1), np.full(5, 1.2)]
    actual = np.r_[rng.random(100) < 0.8, np.zeros(100, dtype=bool), np.zeros(5, dtype=bool)]
    rates = un.binned_rates(un.ratio_bins(ratio), actual, years)
    i2012, i2013 = un.YEARS.index(2012), un.YEARS.index(2013)
    b_hi, b_lo = un.ratio_bins(np.array([1.2]))[0], un.ratio_bins(np.array([0.1]))[0]
    assert rates["table"][i2012, b_hi] == pytest.approx(actual[:100].mean())
    assert rates["table"][i2012, b_lo] == 0.0
    # 2013 has 5 rows in that bin (< 30): the pooled rate over both years is used.
    assert rates["table"][i2013, b_hi] == pytest.approx(actual[np.r_[:100, 200:205]].mean())
    assert un.ratio_bins(np.array([np.nan]))[0] == -1


def test_expected_top250_takes_dv_evidence_then_proxy_and_zero_under_ten_dollars():
    n_bins = len(un.RATIO_EDGES) - 1
    table = lambda value: {"table": np.full((len(un.YEARS), n_bins), value), "counts": None}
    rates = {"dv_rule": table(0.5), "proxy_rule": table(0.2), "no_evidence": {"n": 1, "p": 0.01}}
    missing = pd.DataFrame({"week_end": pd.to_datetime(["2015-01-02"] * 5),
                            "dv_ratio": [1.1, np.nan, np.nan, 2.0, np.nan], "proxy_ratio": [0.1, 3.0, np.nan, 3.0, 0.1],
                            "pf_price_low": [False, False, False, True, False]})
    p = un.expected_top250(missing, rates)
    # No evidence at all: unknown, no rate (never the small no-evidence rate).
    assert p[[0, 1, 3, 4]].tolist() == [0.5, 0.2, 0.0, 0.2] and np.isnan(p[2])
    # The sensitivity puts 0 on the proxy's lowest bin only (row 4: proxy at 10% of the band median).
    low = un.expected_top250(missing, rates, proxy_bin0=0.0)
    assert low[[0, 1, 3, 4]].tolist() == [0.5, 0.2, 0.0, 0.0] and np.isnan(low[2])


def test_a_class_whose_own_dollar_volume_is_far_under_the_cut_is_not_given_the_company_proxy_rate():
    # QRTEB-shaped: weeks 0-8 missing with only Qurate's company float; from week 9 step 6 holds its own
    # dv50 at 1e4 against a 3e7 cut (rank ~1,092). QRTEA, the liquid class, is not touched.
    weeks = np.arange(12)
    frame = pd.DataFrame({
        "security_id": ["QRTEB"] * 12 + ["QRTEA"] * 12 + ["SOLO"] * 12,
        "week_index": np.r_[weeks, weeks, weeks],
        "multi_class": [True] * 24 + [False] * 12,
        "dv50": [np.nan] * 12 + [6e7] * 12 + [np.nan] * 12,
        "pf_dv50": [np.nan] * 9 + [1e4] * 3 + [np.nan] * 12 + [np.nan] * 9 + [1e4] * 3})
    cut = pd.DataFrame({"cut250": 3e7}, index=pd.Index(weeks, name="week_index"))
    ratio, away = un.class_dv_ratios(frame, cut)
    q = frame["security_id"].eq("QRTEB").to_numpy()
    assert np.allclose(ratio[q], 1e4 / 3e7)
    assert away[q].tolist() == [9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 0, 0]
    assert np.allclose(ratio[frame["security_id"].eq("QRTEA").to_numpy()], 2.0)
    assert np.isnan(ratio[frame["security_id"].eq("SOLO").to_numpy()]).all()     # a single class: its own float
    # Beyond CLASS_DV_WEEKS nothing is borrowed.
    far = frame[frame["security_id"] == "QRTEB"].assign(week_index=lambda f: np.where(f["pf_dv50"].notna(), 40, 0))
    far_ratio, _ = un.class_dv_ratios(far, pd.DataFrame({"cut250": 3e7}, index=pd.Index([0, 40], name="week_index")))
    assert np.isnan(far_ratio[:9]).all() and un.CLASS_DV_WEEKS < 40

    n_bins = len(un.RATIO_EDGES) - 1
    table = lambda value: {"table": np.full((len(un.YEARS), n_bins), value), "counts": None}
    rates = {"dv_rule": table(0.001), "proxy_rule": table(0.2), "proxy_rule_multi_class": table(0.65)}
    missing = pd.DataFrame({"week_end": pd.to_datetime(["2018-03-23"] * 3), "dv_ratio": [np.nan] * 3,
                            "proxy_ratio": [4.0, 4.0, 4.0], "pf_price_low": [False] * 3,
                            "multi_class": [True, True, False], "proxy_class_capped": [True, False, False]})
    assert un.expected_top250(missing, rates).tolist() == [0.001, 0.65, 0.2]
    assert un.expected_top250(missing.drop(columns="proxy_class_capped"), rates).tolist() == [0.65, 0.65, 0.2]


def test_proxy_above_takes_either_market_cap_or_float_and_keeps_the_market_cap_first_reading_apart():
    frame = pd.DataFrame({"week_index": [0, 0, 0, 0, 0], "mcap": [5e9, 1e9, np.nan, np.nan, 1e9],
                          "float_usd": [np.nan, 9e9, 3e9, np.nan, 1e9]})
    cut = pd.DataFrame({"cut_mcap": [2e9], "cut_float": [2e9]}, index=pd.Index([0], name="week_index"))
    # INO 2020: a carried market cap below the band median does not outweigh a float that reaches it.
    assert un.proxy_above(frame, cut).tolist() == [True, True, True, False, False]
    assert un.proxy_above_mcap_first(frame, cut).tolist() == [True, False, True, False, False]
    # The calibrated ratio is the either-one reading (round 8): a float-only week is binned by its float, so
    # ratio >= 1 exactly where proxy_above; the plan's market-cap-first ratio is kept for the check-6 view.
    ratio = un.proxy_ratio_of(frame, cut)
    assert ratio[[0, 1, 2, 4]].tolist() == [2.5, 4.5, 1.5, 0.5] and np.isnan(ratio[3])
    assert ((ratio >= 1.0) == un.proxy_above(frame, cut)).all()
    assert un.proxy_ratio_mcap_first(frame, cut)[[0, 1, 2]].tolist() == [2.5, 0.5, 1.5]


def _missing_frame():
    return pd.DataFrame({
        "security_id": ["a", "a", "b", "c", "d"], "cik": ["1", "1", "1", "2", "3"],
        "week_index": [0, 1, 0, 0, 0], "week_end": pd.to_datetime(["2020-01-03", "2020-01-10", "2020-01-03",
                                                                     "2020-01-03", "2020-01-03"]),
        "multi_class": [True, True, True, False, False], "missing": [True, True, False, True, True],
        "dv50_rank_any_price": [np.nan, np.nan, 12.0, np.nan, np.nan]})


def test_a_missing_class_is_sibling_priced_only_when_another_class_ranks_that_week():
    frame = _missing_frame()
    # Week 0: class a is missing and class b of the same company ranks; week 1: b is not listed.
    assert un.sibling_priced(frame).tolist() == [True, False, False, False, False]


def test_missing_weeks_inside_a_newer_tiingo_answer_are_pending_reconcile():
    frame = _missing_frame()
    status = pd.DataFrame({"security_id": ["a", "c", "d"], "status": ["done", "done", "wrong_entity"],
                           "first_date": ["2011-06-01", "2011-06-01", "2011-06-01"],
                           "last_date": ["2020-01-06", "2020-12-31", "2020-12-31"],
                           "fetched_utc": ["2026-10-02T03:00:00+00:00", "2026-10-01T01:00:00+00:00",
                                           "2026-10-02T03:00:00+00:00"]})
    pending = un.pending_reconcile(frame, status, "2026-10-01T21:04:50+00:00")
    # a: the answer reaches 2020-01-06 (+5 days covers the 2020-01-10 week end); c: fetched before the
    # panel was built, so step 9 already read it; d: the fetch found another company.
    assert pending.tolist() == [True, True, False, False, False]
    assert un.recent_fetches(status, "2026-10-01T21:04:50+00:00") == {"a"}


def test_unfillable_windows_apply_per_week():
    frame = _missing_frame()
    windows = un.windows_of(pd.DataFrame({"security_id": ["a"], "needed_start": ["2020-01-01"],
                                          "needed_end": ["2020-01-05"]}))
    assert un.in_windows(frame, windows).tolist() == [True, False, False, False, False]


def test_coverage_weights_split_a_company_value_among_its_listed_classes():
    rows = pd.DataFrame({"security_id": ["a", "b", "c"], "cik": ["1", "1", "2"], "week_index": [0, 0, 0],
                         "multi_class": [True, True, False], "proxy": [10.0, 10.0, 4.0]})
    assert un.coverage_weights(rows)["weight"].tolist() == [5.0, 5.0, 4.0]


def test_known_status_uses_canonical_ranks_then_step6_dollar_volume():
    listed = pd.DataFrame({
        "eligible": [True] * 5, "outside_trading": [False] * 5, "young": [False] * 5,
        "dv50_rank_any_price": [3.0, 400.0, np.nan, np.nan, np.nan], "dv50_rank": [3.0, 400.0, np.nan, np.nan, np.nan],
        "price_ge_10": ["Y", "Y", "", "", ""], "missing": [False, False, True, True, True],
        "pf_dv_ok": [True, True, True, False, False], "pf_ge_cut250": [True, False, True, False, False],
        "pf_price_low": [False, False, False, True, False]})
    known, status = un.known_status(listed)
    # The last row has neither a canonical rank nor step-6 evidence: its status is what the proxy estimates.
    assert len(known) == 4 and status.tolist() == [True, False, True, False]
    assert known["status_from_step6"].tolist() == [False, False, True, True]


def test_capture_summary_separates_unfillable_pending_and_sibling_gaps():
    capture = pd.DataFrame({"snapshot_date": ["2015-01-02"] * 5, "security_id": list("abcde"),
                            "row_within_5": [True, False, False, False, False],
                            "unfillable": [False, True, False, False, False],
                            "sibling_priced": [False, False, False, True, False],
                            "reason": ["", "unfillable", "tiingo_pending", "no_vendor_source", "not_candidate"]})
    out = un.capture_summary(capture)
    assert out["min_share"] == 0.2 and out["min_share_plan"] == 0.4
    assert out["min_share_after_pending_siblings"] == 0.8 and out["other_gap_securities"] == ["e"]


# ------------------------------------------------------------------ names step 6 never saw, unknown evidence

def test_a_listed_name_with_no_step6_rows_gets_its_stored_file_dollar_volume(tmp_path):
    # SMCI-type: listed again after a Form 25 cut, no canonical rank and no step-6 row; the master names a
    # stored file under its ticker, plus one under another ticker that it never listed with.
    sessions, weeks, week_pos, _ = _grid(80)
    days = sessions.strftime("%Y-%m-%d")
    pd.DataFrame({"date": days, "close": 2.0, "volume": 1e6}).to_csv(tmp_path / "smci.csv", index=False)
    pd.DataFrame({"date": days, "close": 50.0, "volume": 9e9}).to_csv(tmp_path / "smcip.csv", index=False)
    master = pd.DataFrame({"security_id": ["S", "T"], "price_sources": [
        f"stored:smci.csv[{days[0]}..{days[-1]}] stored:smcip.csv[{days[0]}..{days[-1]}]", ""]})
    # Listed from session 10 on: earlier stored rows are not the listing's and stay out of the medians.
    spans = pd.DataFrame({"security_id": ["S", "T"], "ticker": ["SMCI", "TTT"],
                          "list_start": [days[10], days[0]], "list_end": [days[-1], days[-1]]})
    listed = pd.DataFrame({"security_id": ["S"] * len(weeks) + ["T"], "week_index": list(range(len(weeks))) + [0],
                           "eligible": True, "in_pf": False, "dv50_rank_any_price": np.nan})
    out = un.stored_direct_dv(listed, spans, master, sessions, week_pos, directory=tmp_path)
    s_rows = out.iloc[:len(weeks)]
    rows_listed = np.maximum(week_pos - 10 + 1, 0)
    # dv20 needs 10 listed rows, dv50 25; every listed day is 2 x 1e6 (the SMCIP file is not used).
    assert np.isnan(s_rows["sd_dv20"].values[rows_listed < 10]).all()
    assert (s_rows["sd_dv20"].values[rows_listed >= 10] == 2e6).all()
    assert np.isnan(s_rows["sd_dv50"].values[rows_listed < 25]).all()
    assert (s_rows["sd_dv50"].values[rows_listed >= 25] == 2e6).all()
    assert out.iloc[-1].isna().all()  # T: the master names no stored file
    applied = un.apply_stored_direct(listed.assign(pf_dv50=np.nan, pf_dv20=np.nan, pf_price="", pf_src=""), out)
    used = applied["dv_stored_direct"]
    assert (applied.loc[used, "pf_src"] == "stored_direct").all() and (applied.loc[used, "pf_price"] == "U").all()


def test_mapping_boundary_rows_of_another_company_are_trimmed_but_a_transfer_keeps_its_history():
    sessions = pd.bdate_range("2014-03-03", "2014-07-31")
    spans = pd.DataFrame({"security_id": ["TRUE", "AMD", "FLOOR"], "ticker": ["TRUE", "AMD", "FLR"],
                          "ipo_boundary": True, "start_prev_absent": ["2014-03-25", "2014-03-25", "2014-03-25"],
                          "snapshot_start": ["2014-06-05", "2014-06-05", "2014-06-05"]})
    master = pd.DataFrame({"security_id": ["TRUE", "AMD", "FLOOR"], "price_sources": [
        "stored:true.csv[2014-05-16..2026-07-17]", "stored:amd.csv[2004-01-02..2026-07-31]",
        "stored:flr.csv[2014-05-01..2026-07-17]"]})
    first = {"TRUE": "2014-03-26", "AMD": "2014-03-26", "FLOOR": "2014-03-26"}
    trims = un.boundary_trims(spans, master, first, sessions, floor_dates={"2014-05-01"})
    # TRUE: TrueCar's own file starts between the two snapshots, 36 sessions after the canonical rows;
    # AMD's file starts years before (a transfer); FLR's start is the repo's coverage start.
    assert trims == {"TRUE": "2014-05-16"}
    panel = pd.DataFrame({"security_id": ["TRUE", "TRUE", "AMD"],
                          "date": pd.to_datetime(["2014-05-15", "2014-05-16", "2014-03-26"])})
    assert un.trim_panel(panel, trims)["date"].dt.strftime("%Y-%m-%d").tolist() == ["2014-05-16", "2014-03-26"]
    # With the trimmed first row the IPO rule starts the listing at TrueCar's first day.
    intervals = pd.DataFrame([_interval(security_id="TRUE", ticker="TRUE", start="2014-06-05", end="2015-01-02",
                                        start_prev_absent="2014-03-25")])
    span = un.listing_spans(intervals, _master([{"security_id": "TRUE"}]), {"TRUE": "2014-05-16"}, sessions=sessions)
    assert span["list_start"].iloc[0] == "2014-05-16" and span["ipo_start"].iloc[0]


def test_dv_window_before_listing_flags_boundary_weeks_whose_window_reaches_before_the_start():
    sessions = pd.bdate_range("2015-01-01", periods=120)
    week_pos = np.array([30, 60, 90, 110])
    rows = pd.DataFrame({"week_index": [0, 1, 2, 3], "ipo_boundary": [True, True, False, True],
                         "interval_start": [sessions[20].strftime("%Y-%m-%d")] * 4,
                         "first_row": [sessions[0].strftime("%Y-%m-%d")] * 4})
    # Windows start at sessions -19, 11, 41, 61: only the first two reach before session 20.
    assert un.window_before_listing(rows, sessions, week_pos).tolist() == [True, True, False, False]


def test_evidence_class_never_calls_a_name_without_series_or_proxy_small():
    frame = pd.DataFrame({"pf_price_low": [True, False, False, False], "pf_dv_ok": [False, True, False, False],
                          "proxy_ratio": [np.nan, np.nan, 0.01, np.nan]})
    assert un.evidence_class(frame).tolist() == ["price_lt_10", "dv", "proxy", "unknown"]


def test_proxy_ratio_falls_back_to_float_when_the_band_has_no_market_caps():
    frame = pd.DataFrame({"week_index": [0, 1], "mcap": [3e9, 3e9], "float_usd": [1e9, 1e9]})
    cut = pd.DataFrame({"cut_mcap": [2e9, np.nan], "cut_float": [4e9, 4e9]}, index=pd.Index([0, 1], name="week_index"))
    assert un.proxy_ratio_of(frame, cut).tolist() == [1.5, 0.25]
    assert un.proxy_above(frame, cut).tolist() == [True, False]


def test_a_name_every_planned_source_answered_empty_is_unfillable_even_with_a_partial_series():
    master = pd.DataFrame({"security_id": ["cree", "ok"]})
    candidates = pd.DataFrame({"security_id": ["cree", "ok"], "planned_source": ["yahoo", "yahoo"]})
    report = pd.DataFrame({"security_id": ["cree", "ok", "ok"], "verdict": ["no_rows", "no_rows", "ok"]})
    empty = pd.DataFrame(columns=["security_id"])
    reasons = un.security_reasons(master, candidates, empty, pd.DataFrame(columns=["security_id", "reason"]),
                                  pd.DataFrame(), series_ids={"cree", "ok"}, yahoo_report=report)
    assert reasons == {"cree": "unfillable", "ok": "series_gap"}


def _summary_inputs(cases):
    """One week per case: 250 ranked names with a close in the week plus the case's missing names."""
    rows = []
    for k, missing in enumerate(cases):
        for r in range(1, 251):
            rows.append({"week_index": k, "security_id": f"r{r}", "dv50_rank": float(r), "missing": False})
        for j, m in enumerate(missing):
            rows.append({"week_index": k, "security_id": f"m{j}", "dv50_rank": np.nan, "missing": True, **m})
    base = {"eligible": True, "foreign": False, "non_common": False, "spac_shell": False, "foreign_filer": "N",
            "in_week": True, "on_week_end": True, "zero_volume": False, "outside_trading": False, "young": False,
            "pf_dv_ok": False, "pf_ge_cut250": False, "pf_ge_cut300": False, "proxy_above": False,
            "multi_class": False, "missing_reason": "", "pf_price_low": False, "evidence": "", "p_top250": 0.0,
            "sibling_priced": False, "dv_stored_direct": False, "proxy": np.nan, "cik": "", "pf_dv50_rank": np.nan,
            "ends_within_4w": False, "terminal_unresolved": False}
    listed = pd.DataFrame([{**base, **row} for row in rows])
    listed["price_ge_10"] = np.where(listed["missing"], "", "Y")
    listed["dv20_rank"] = listed["dv50_rank"]
    for column in ("in_week", "on_week_end"):
        listed[column] = listed[column] & ~listed["missing"]
    n = len(cases)
    weeks = pd.DatetimeIndex(pd.bdate_range("2020-01-03", periods=n * 5, freq="B")[4::5])
    cut = pd.DataFrame({"cut250": 1e7, "cut300": 8e6, "pf_cut250": 1e7, "pf_cut300": 8e6},
                       index=pd.Index(range(n), name="week_index"))
    ages = pd.DataFrame({"week_end": weeks, "snapshot_age_days": 5, "snapshot_source": "repo_symdir"})
    top = pd.DataFrame({"week_index": listed.loc[~listed["missing"], "week_index"].values, "dv50_rank": 1.0,
                        "ff49": "13", "earnings_event_within_3_sessions": "N"})
    return listed, cut, weeks, ages, top


def test_complete_flags_block_on_unknown_proxy_only_and_dv_names_and_strict_on_the_expected_count():
    unknown = {"evidence": "unknown", "p_top250": np.nan}
    cases = [
        [{**unknown, "missing_reason": "not_candidate"}],  # 0: an unknown name blocks every flag
        [{**unknown, "missing_reason": "tiingo_pending"}],  # 1: pending: blocks complete, not after_pending
        [{"evidence": "proxy", "proxy_above": True, "missing_reason": "unfillable", "p_top250": 0.6}],  # 2
        [{"evidence": "dv", "pf_dv_ok": True, "pf_ge_cut250": True, "missing_reason": "series_gap", "p_top250": 0.97}],
        [{"evidence": "dv", "pf_dv_ok": True, "missing_reason": "not_candidate", "p_top250": 0.3}],  # 4: complete
        [{"evidence": "proxy", "missing_reason": "not_candidate", "p_top250": 0.6},
         {"evidence": "proxy", "missing_reason": "not_candidate", "p_top250": 0.6}],  # 5: complete, not strict
    ]
    s = un.weekly_summary(*_summary_inputs(cases))
    assert s["complete_250"].tolist() == ["N", "N", "N", "N", "Y", "Y"]
    assert s["complete_250_after_pending"].tolist() == ["N", "Y", "N", "N", "Y", "Y"]
    assert s["complete_250_strict"].tolist() == ["N", "N", "N", "N", "Y", "N"]
    assert s["n_missing_unknown"].tolist() == [1, 1, 0, 0, 0, 0]
    assert s["n_missing_unknown_not_pending"].tolist() == [1, 0, 0, 0, 0, 0]
    # The upper residual counts the unknown name as a top-250 name; the estimate leaves it out.
    assert s["est_missing_top250_residual"].tolist() == [0.0, 0.0, 0.6, 0.97, 0.3, 1.2]
    assert s["est_missing_top250_residual_upper"].tolist() == [1.0, 0.0, 0.6, 0.97, 0.3, 1.2]


def test_a_minor_class_whose_sibling_ranks_blocks_after_pending_but_not_its_ex_sibling_view():
    cases = [[{"evidence": "proxy", "proxy_above": True, "missing_reason": "unfillable", "sibling_priced": True}],
             [{"evidence": "proxy", "proxy_above": True, "missing_reason": "unfillable", "sibling_priced": False}]]
    listed, cut, weeks, ages, top = _summary_inputs(cases)
    s = un.weekly_summary(listed, cut, weeks, ages, top)
    assert s["complete_250_after_pending"].tolist() == ["N", "N"]
    assert un.after_pending_ex_siblings(s, listed).tolist() == [True, False]


def test_answers_older_than_the_panel_that_it_lacks_are_not_pending():
    frame = _missing_frame()
    status = pd.DataFrame({"security_id": ["a", "c"], "status": ["done", "partial"],
                           "first_date": ["2011-06-01", "2011-06-01"], "last_date": ["2026-08-31", "2026-08-31"],
                           "fetched_utc": ["2026-10-01T19:36:08+00:00", "2026-10-03T01:00:00+00:00"]})
    built = "2026-10-02T05:04:50+00:00"
    # a (SMCI-type): answered before the panel was built, yet its weeks are missing: step 9 dropped them.
    assert un.pending_reconcile(frame, status, built, newer=False).tolist() == [True, True, False, False, False]
    assert un.pending_reconcile(frame, status, built).tolist() == [False, False, False, True, False]


def test_check_6_counts_every_non_pending_reason_and_unknown_names_as_top250():
    settled = pd.DataFrame({"missing_reason": ["unfillable", "series_gap", "not_candidate", "not_candidate"],
                            "evidence": ["dv", "dv", "proxy", "unknown"], "p_top250": [1.0, 3.0, 1.0, np.nan],
                            "p_top250_low": [1.0, 3.0, 0.0, np.nan]})
    six = un.check_6(settled, settled, slots=250)
    assert six["unfillable_only_share"] == 0.004 and six["pass_unfillable_only"]
    assert six["residual_share"] == 0.02 and six["pass_known_evidence_only"]
    assert six["unknown_name_weeks"] == 1 and six["upper_share"] == 0.024 and not six["pass"]
    assert six["range_share"] == [0.016, 0.02, 0.024]


def test_month2_leads_take_active_unknown_names_but_not_delisted_ones():
    missing = pd.DataFrame({"security_id": ["smci", "old", "dv"], "missing_reason": ["series_gap", "not_candidate", "unfillable"],
                            "weeks_pf_dv_ge_cut300": [0, 0, 3], "weeks_no_pf_dv_proxy_ge_half": [0, 0, 0],
                            "weeks_no_pf_dv": [10, 10, 0], "weeks_proxy_above": [0, 0, 0], "weeks_unknown": [10, 10, 0],
                            "active_now": [True, False, False], "est_top250_weeks": [0.0, 0.0, 2.9]})
    leads = un.month2_leads(missing, pd.DataFrame({"security_id": ["dv"], "planned_source": ["tiingo"], "reason": ["A1"]}),
                            pd.DataFrame())
    assert leads["security_id"].tolist() == ["smci", "dv"]
    assert leads["lead_rule"].tolist() == ["unknown_active", "dv_ge_cut300"]
    assert leads["suggested_source"].tolist() == ["yahoo", "tiingo"]


# ------------------------------------------------------------------ trading bounds and the plan 3.3 checks

def test_mark_missing_skips_weeks_outside_trading_new_listings_and_closes_under_ten_dollars():
    weeks = pd.to_datetime(["2020-01-03", "2020-01-10", "2020-01-17", "2020-01-24"])
    base = {"eligible": True, "dv50_rank_any_price": np.nan, "close": np.nan, "dv50": np.nan, "price_ge_10": "",
            "has_series": True, "pf_outside_trading": False, "pf_dv50": np.nan, "pf_price": "", "mcap": np.nan,
            "float_usd": np.nan, "multi_class": False, "cik": "1"}
    rows = [
        # a: its series ends 2020-01-08 and its listing 10 days later: the weeks after are outside trading.
        {"security_id": "a", "week_index": 2, "first_row": "2015-01-02", "last_row": "2020-01-08"},
        # b: first row 2020-01-02, a close but no 25-row median yet: a new listing whose series starts at its
        # earliest start, not missing.
        {"security_id": "b", "week_index": 1, "first_row": "2020-01-02", "last_row": "2020-12-31", "close": 15.0,
         "price_ge_10": "Y", "series_starts_listing": True},
        # c: a close under $10 and no median: the $10 test already excludes it.
        {"security_id": "c", "week_index": 1, "first_row": "2015-01-02", "last_row": "2020-12-31", "close": 4.0,
         "price_ge_10": "N"},
        # d: listed for years, its series stops in 2018: missing, with no evidence at all.
        {"security_id": "d", "week_index": 3, "first_row": "2015-01-02", "last_row": "2018-06-01"},
    ]
    listed = pd.DataFrame([{**base, **r} for r in rows])
    listed["week_end"] = weeks[listed["week_index"].values]
    spans = pd.DataFrame({"security_id": ["a", "b", "c", "d"], "list_start": ["2010-01-01", "2019-12-30", "2010-01-01",
                                                                             "2010-01-01"],
                          "list_end": ["2020-01-18", "2026-08-31", "2026-08-31", "2026-08-31"]})
    cut = pd.DataFrame({"cut250": 1e7, "cut300": 8e6, "cut_mcap": 2e9, "cut_float": 2e9},
                       index=pd.Index(range(4), name="week_index"))
    out = un.mark_missing(listed, spans, {"d": "series_gap"}, cut).set_index("security_id")
    assert out.loc["a", "outside_trading"] and not out.loc["a", "missing"]
    assert out.loc["b", "young"] and not out.loc["b", "missing"]
    assert not out.loc["c", "missing"]
    assert out.loc["d", "missing"] and out.loc["d", "missing_reason"] == "series_gap"
    assert out.loc["d", "evidence"] == "unknown"
    # A fetch answered d before the panel was built and covers the week: the panel lacks rows in hand.
    status = pd.DataFrame({"security_id": ["d"], "status": ["done"], "first_date": ["2011-06-01"],
                           "last_date": ["2026-08-31"], "fetched_utc": ["2026-10-01T19:36:08+00:00"]})
    out = un.mark_missing(listed, spans, {"d": "series_gap"}, cut, status, "2026-10-02T05:04:50+00:00")
    assert out.set_index("security_id").loc["d", "missing_reason"] == "answer_not_in_panel"
    # A planned Yahoo request does not make it pending: step 9 would drop the same history again.
    out = un.mark_missing(listed, spans, {"d": "yahoo_pending"}, cut, status, "2026-10-02T05:04:50+00:00")
    assert out.set_index("security_id").loc["d", "missing_reason"] == "answer_not_in_panel"
    out = un.mark_missing(listed, spans, {"d": "unfillable"}, cut, status, "2026-10-02T05:04:50+00:00")
    assert out.set_index("security_id").loc["d", "missing_reason"] == "unfillable"


def test_form25_check_finds_series_ends_near_the_delisting_and_says_why_others_do_not():
    sessions = pd.bdate_range("2015-01-01", "2016-12-31")
    form25 = pd.DataFrame({"accession": ["x1", "x2", "x3", "x4"], "classification": "common_delisting",
                           "public_float_usd": ["2e9", "3e9", "5e9", "1e8"], "float_check_flag": "ok",
                           "effective_date": ["2015-06-10", "2015-09-10", "2016-03-10", "2016-03-10"],
                           "filing_date": ["2015-05-29", "2015-08-31", "2016-02-29", "2016-02-29"],
                           "subject_name": ["Near", "Late", "None", "Small"]})
    master = pd.DataFrame({"security_id": ["near", "late", "none", "small"], "delist_form25_accession": ["x1", "x2", "x3", "x4"]})
    spans = pd.DataFrame({"security_id": ["near", "late", "none", "small"], "non_common": False, "after_cut": False})
    last_row = {"near": "2015-06-05", "late": "2015-12-31"}
    out = un.form25_check(form25, master, last_row, sessions, {"none": "tiingo_pending"}, {}, set(), spans,
                          step6_best={"none": 120.0}).set_index("security_id")
    assert out.loc["near", "status"] == "series_ends_near"
    assert out.loc["late", "status"] == "series_ends_late" and out.loc["late", "reason"] == "rows_after_filing"
    assert out.loc["none", "status"] == "no_series" and out.loc["none", "reason"] == "tiingo_pending"
    assert "small" not in out.index  # float under $1B


def test_nasdaq100_check_marks_covered_partial_and_foreign_members():
    sessions = pd.bdate_range("2012-01-02", "2012-12-31")
    spans = pd.DataFrame({"security_id": ["a", "b", "f"], "ticker": ["AAA", "BBB", "FFF"],
                          "list_start": "2011-01-01", "list_end": "2026-08-31"})
    master = pd.DataFrame({"security_id": ["a", "b", "f"], "foreign_filer": ["N", "N", "Y"],
                           "tickers_observed": ["AAA", "BBB", "FFF"]})
    full = pd.DataFrame({"security_id": "a", "date": sessions})
    half = pd.DataFrame({"security_id": "b", "date": sessions[:100]})
    panel = pd.concat([full, half], ignore_index=True)
    out = un.nasdaq100_check({"2012": ["AAA", "BBB", "FFF", "ZZZ"]}, spans, master, {"f": [("1900-01-01", "2100-01-01")]},
                             panel, sessions, {"b": "series_gap"}).set_index("ticker")
    assert out.loc["AAA", "status"] == "covered"
    assert out.loc["BBB", "status"] == "partial" and out.loc["BBB", "reason"] == "series_gap"
    assert out.loc["FFF", "status"] == "foreign_excluded"
    assert out.loc["ZZZ", "status"] == "not_mapped"


def test_capture_coverage_needs_a_row_within_five_sessions_and_marks_sibling_classes():
    sessions = pd.bdate_range("2015-01-01", "2015-03-31")
    lists = pd.DataFrame({"snapshot_date": "2015-02-02", "as_of_session": "2015-02-02",
                          "security_id": ["a", "b", "c1", "c2"], "ticker": ["A", "B", "CA", "CB"],
                          "market_cap": [5e9, 4e9, 3e9, 3e9]})
    spans = pd.DataFrame({"security_id": ["a", "b", "c1", "c2"], "list_start": "2010-01-01", "list_end": "2026-08-31",
                          "non_common": False})
    master = pd.DataFrame({"security_id": ["a", "b", "c1", "c2"], "cik": ["1", "2", "3", "3"]})
    panel = pd.DataFrame({"security_id": ["a", "b", "c2"],
                          "date": pd.to_datetime(["2015-01-27", "2015-01-20", "2015-02-02"])})
    out = un.capture_coverage(lists, spans, {}, set(), panel, sessions, set(), master, {"b": "not_candidate"}).set_index("security_id")
    # a: 4 sessions before the capture; b: 9 sessions before; c1: none, but its sibling class c2 has one.
    assert out.loc["a", "row_within_5"] and not out.loc["a", "row_on_as_of"]
    assert not out.loc["b", "row_within_5"] and out.loc["b", "reason"] == "not_candidate"
    assert not out.loc["c1", "row_within_5"] and out.loc["c1", "sibling_priced"]


# ------------------------------------------------------------------ industry, earnings, buckets

def test_sic_on_dates_latest_header_earliest_master_and_6770():
    history = pd.DataFrame({
        "cik": ["10", "10", "20"], "observed_date": ["2013-01-01", "2015-01-01", "2014-01-01"],
        "sic": ["6770", "2834", "6770"], "operating_sic_after_6770": ["2834", "", ""]})
    cik = np.array(["10", "10", "10", "20", "30"], dtype=object)
    days = pd.Series(pd.to_datetime(["2012-06-01", "2014-06-01", "2016-06-01", "2014-06-01", "2014-06-01"]))
    sic, basis = un.sic_on_dates(cik, days, history, {"30": "7372"})
    assert sic.tolist() == ["2834", "2834", "2834", "6770", "7372"]
    assert basis.tolist() == ["earliest_header+6770_operating", "header+6770_operating", "header", "header", "master"]


def test_ff49_maps_ranges_unmatched_to_other_and_blank_to_blank():
    maps = pd.DataFrame({"scheme": ["FF49", "FF49", "FF49", "FF12"], "industry_id": ["13", "49", "36", "1"],
                         "short_name": ["Drugs", "Other", "Softw", "x"], "sic_lo": ["2830", "4950", "7370", "0100"],
                         "sic_hi": ["2836", "4959", "7372", "9999"]})
    ids, names, unmatched = un.ff49_of(np.array(["2834", "9995", "", "7372"], dtype=object), maps)
    assert ids.tolist() == ["13", "49", "", "36"] and names.tolist() == ["Drugs", "Other", "", "Softw"]
    assert unmatched.tolist() == [False, True, False, False]


def test_earnings_offsets_are_signed_session_distances_to_the_nearest_d0():
    sessions = pd.bdate_range("2012-01-02", periods=40)
    events = pd.DataFrame({"cik": ["1", "1", "2"], "d0_session": [sessions[10].strftime("%Y-%m-%d"),
                                                                    sessions[30].strftime("%Y-%m-%d"), ""],
                           "event_kind": ["results_release", "preannouncement", "results_release"]})
    cik = np.array(["1", "1", "1", "2", "1"], dtype=object)
    pos = np.array([12, 28, 20, 12, 22])
    offset, release = un.earnings_offsets(cik, pos, events, sessions)
    # A tie takes the earlier event; a CIK without a dated event has none.
    assert offset[0] == -2 and offset[1] == 2 and offset[2] == -10 and np.isnan(offset[3]) and offset[4] == 8
    # Results releases only: 28 and 22 are 18 and 12 sessions after the release, beyond the 10-session reach.
    assert release[0] == -2 and np.isnan(release[1]) and np.isnan(release[4])


def test_dv_bucket_labels_and_blank_for_missing():
    assert un.dv_bucket(1.3e7) == "10M-20M"
    assert un.dv_bucket(2e7) == "20M-50M"
    assert un.dv_bucket(7.5e8) == "500M-1B"
    assert un.dv_bucket(np.nan) == "" and un.dv_bucket(0) == ""


def test_committed_tables_refuse_vendor_values():
    with pytest.raises(AssertionError):
        un.assert_no_vendor_values(pd.DataFrame(columns=["week_end", "dv50"]), pd.DataFrame(columns=["week_end"]))
    un.assert_no_vendor_values(pd.DataFrame(columns=un.TOP300_COLUMNS), pd.DataFrame(columns=un.SUMMARY_COLUMNS))


def test_snapshot_age_is_days_since_the_latest_snapshot_on_or_before_the_week():
    index = pd.DataFrame({"snapshot_date": ["2012-01-01", "2012-03-01"], "source": ["wayback_symdir", "repo_symdir"]})
    ages = un.snapshot_ages(pd.DatetimeIndex(["2012-01-06", "2012-03-02"]), index)
    assert ages["snapshot_age_days"].tolist() == [5, 1]
    assert ages["snapshot_source"].tolist() == ["wayback_symdir", "repo_symdir"]


def test_the_script_never_reads_the_return_column(tmp_path):
    source = Path(un.__file__).read_text()
    assert '"tr"' not in source.replace('"tr", "mcap"', "")  # only the banned-column list names it
    assert "tr" not in un.PANEL_COLUMNS and "usecols=lambda c: c in PANEL_COLUMNS" in source
    path = tmp_path / "panel.csv"
    path.write_text("security_id,date,close_raw,volume_raw,split_factor,div_cash,tr,src_primary,n_sources,max_src_diff,flags\n"
                    "a,2020-11-19,1.0,10,1,0,0.5,yahoo,1,,\n"
                    "a,2020-11-20,20.0,10,1,0,,yahoo,1,,gap_before:3;relist_junction\n")
    panel = un.load_panel(path)
    assert "tr" not in panel.columns and "flags" not in panel.columns
    assert panel["relist_junction"].tolist() == [False, True]


# ------------------------------------------------------------------ round 6: young names, pending weeks, investment companies

def _young_frame(**columns):
    base = {"week_end": pd.to_datetime(["2026-04-10"]), "close": [np.nan], "dv50": [np.nan], "first_row": [""],
            "pf_dv50": [np.nan], "pf_n50": [np.nan], "pf_first_data": pd.to_datetime([None]), "listing_start": ["2010-12-31"],
            "listing_sessions": [3000], "new_listing": [True], "listing_continues": [False]}
    base.update(columns)
    return pd.DataFrame(base)


def test_a_new_listing_with_no_series_is_young_in_its_first_25_sessions_only():
    assert un.young_weeks(_young_frame(listing_sessions=[24], listing_start=["2026-03-09"])).tolist() == [True]
    assert un.young_weeks(_young_frame(listing_sessions=[25], listing_start=["2026-03-06"])).tolist() == [False]
    # A later run after a gap in the snapshots is no new listing.
    assert un.young_weeks(_young_frame(listing_sessions=[5], listing_start=["2026-04-03"],
                                       first_listing_run=[False])).tolist() == [False]
    # IRHO: listed 2026-03-01 (units), its shares in step 6 from 2026-03-20 with 15 rows in the window.
    irho = _young_frame(listing_sessions=[29], listing_start=["2026-03-01"], pf_n50=[15.0],
                        pf_first_data=pd.to_datetime(["2026-03-20"]))
    assert un.young_weeks(irho).tolist() == [True]
    # A dv50 from step 6 settles it; so does a series of 25 rows.
    assert un.young_weeks(irho.assign(pf_dv50=[1e6])).tolist() == [False]
    assert un.young_weeks(irho.assign(pf_n50=[25.0])).tolist() == [False]


def test_a_name_step6_holds_no_row_for_or_whose_data_starts_late_is_not_young():
    # Altaba after 2017-06: n50 = 0, an old listing.
    assert un.young_weeks(_young_frame(pf_n50=[0.0], pf_first_data=pd.to_datetime(["2011-06-03"]))).tolist() == [False]
    # Atlantic American: listed since 2010, step 6's data starts in 2026-03: not a new listing.
    late = _young_frame(pf_n50=[10.0], pf_first_data=pd.to_datetime(["2026-03-20"]))
    assert un.young_weeks(late).tolist() == [False]


def test_mark_missing_does_not_count_a_young_name_without_series_as_missing():
    weeks = pd.to_datetime(["2026-04-10"])
    listed = pd.DataFrame({"security_id": ["irho"], "week_index": [0], "week_end": weeks, "eligible": [True],
                           "dv50_rank_any_price": [np.nan], "close": [np.nan], "dv50": [np.nan], "price_ge_10": [""],
                           "has_series": [False], "pf_outside_trading": [False], "pf_dv50": [np.nan], "pf_price": [""],
                           "mcap": [np.nan], "float_usd": [np.nan], "multi_class": [False], "cik": ["1"],
                           "first_row": [""], "last_row": [""], "pf_n50": [15.0],
                           "pf_first_data": pd.to_datetime(["2026-03-20"]), "listing_start": ["2026-03-01"],
                           "listing_sessions": [29]})
    spans = pd.DataFrame({"security_id": ["irho"], "list_start": ["2026-03-01"], "list_end": ["2026-08-31"]})
    cut = pd.DataFrame({"cut250": 1e7, "cut300": 8e6, "cut_mcap": 2e9, "cut_float": 2e9}, index=pd.Index([0], name="week_index"))
    out = un.mark_missing(listed, spans, {"irho": "not_candidate"}, cut)
    assert out["young"].tolist() == [True] and out["missing"].tolist() == [False]
    out = un.mark_missing(listed.assign(pf_n50=[0.0]), spans, {"irho": "not_candidate"}, cut)
    assert out["missing"].tolist() == [True] and out["evidence"].tolist() == ["unknown"]


def test_run_starts_join_a_rename_but_not_a_relisting():
    spans = pd.DataFrame({"security_id": ["a", "a", "a"], "list_start": ["2012-01-01", "2014-05-01", "2020-01-01"],
                          "list_end": ["2014-04-30", "2018-12-31", "2026-08-31"]})
    assert un.run_starts(spans).tolist() == ["2012-01-01", "2012-01-01", "2020-01-01"]


def test_pending_reasons_hold_only_in_the_weeks_a_candidate_row_of_that_source_needs():
    master = pd.DataFrame({"security_id": ["corz", "smci"]})
    # CORZ: a Tier-C Tiingo row for CORZQ (2021-11-28..2023-01-12) and a Yahoo row from 2023-12-15.
    candidates = pd.DataFrame({"security_id": ["corz", "corz", "smci"], "planned_source": ["tiingo", "yahoo", "tiingo"],
                               "needed_start": ["2021-11-28", "2023-12-15", "2018-01-21"],
                               "needed_end": ["2023-01-12", "2026-08-31", "2020-02-25"]})
    reasons = un.MissingReasons(master, candidates, pd.DataFrame(columns=["security_id"]),
                                pd.DataFrame(columns=["security_id", "reason"]), pd.DataFrame(), series_ids={"corz", "smci"})
    assert reasons.get("corz") == "tiingo_pending" and reasons.chains["corz"][:2] == ["tiingo_pending", "yahoo_pending"]
    days = ["2022-06-03", "2023-06-02", "2024-04-05", "2026-07-17"]
    assert reasons.weekly(["corz"] * 4, days).tolist() == ["tiingo_pending", "series_gap", "yahoo_pending", "yahoo_pending"]
    # SMCI: its only Tiingo row needs 2018-2020; its 2024 weeks are a series gap, not pending.
    assert reasons.weekly(["smci", "smci"], ["2019-06-07", "2024-06-07"]).tolist() == ["tiingo_pending", "series_gap"]
    assert reasons.on("smci", "2024-06-07") == "series_gap"
    # A window reaching into the week counts (needed_end on the Wednesday before the week end).
    assert reasons.weekly(["corz"], ["2023-01-13"]).tolist() == ["tiingo_pending"]


def test_pending_definitions_name_every_pending_reason():
    texts = un.pending_definitions()
    for reason in un.PENDING_REASONS:
        assert reason in texts["complete_250_after_pending"] and reason in texts["residual_survivorship"]
    assert texts["pending_reasons"] == sorted(un.PENDING_REASONS)


def _evidence(*rows):
    return [(day, form, f"acc-{k}") for k, (day, form) in enumerate(rows)]


def test_a_bdc_election_counts_until_its_withdrawal():
    spans = un.investment_company_spans(_evidence(("2004-04-21", "N-54A"), ("2010-01-01", "40-17G"),
                                                  ("2018-04-02", "N-54C")))
    assert spans == [("2004-04-21", "2018-04-01", "bdc_election")]
    # Red Cat: elected in 1998, withdrew in 2014, an operating company when it listed in 2021.
    spans = un.investment_company_spans(_evidence(("1998-09-17", "N-54A"), ("2014-07-23", "N-54C")))
    assert spans == [("1998-09-17", "2014-07-22", "bdc_election")]
    # A withdrawal with no cached election (American Capital): a BDC from the first evidence on.
    spans = un.investment_company_spans(_evidence(("2009-06-19", "40-17G"), ("2009-08-19", "N-2"),
                                                  ("2017-01-03", "N-54C")))
    assert spans == [("2009-06-19", "2017-01-02", "bdc_until_withdrawal+filings")]


def test_a_run_of_fund_filings_counts_from_first_to_last_and_a_stray_filing_does_not():
    # Altaba: N-8A and N-2 on 2017-06-16, then N-CSR and N-PX; Global Self Storage stops filing in 2016.
    spans = un.investment_company_spans(_evidence(("2017-06-16", "N-8A"), ("2017-06-16", "N-2"),
                                                  ("2017-08-25", "N-PX"), ("2017-08-29", "N-CSR")))
    assert spans == [("2017-06-16", "2017-08-29", "filings")]
    assert un.investment_company_spans(_evidence(("2001-06-27", "N-30D"))) == []   # Medical Action's lone N-30D
    gap = un.investment_company_spans(_evidence(("2010-01-04", "40-17G"), ("2010-12-01", "40-17G"),
                                                ("2013-01-02", "40-17G"), ("2013-11-01", "40-17G")))
    assert [s[:2] for s in gap] == [("2010-01-04", "2010-12-01"), ("2013-01-02", "2013-11-01")]


def test_the_last_run_is_carried_forward_while_the_issuer_still_files_unless_it_deregistered():
    run = _evidence(*[(f"{y}-07-30", "40-17G") for y in range(2010, 2026)])
    assert un.investment_company_spans(run, latest_filing="2026-09-11")[-1][1:] == (un.IC_OPEN_END, "filings_current")
    assert un.investment_company_spans(run, latest_filing="2028-09-11")[-1][1:] == ("2025-07-30", "filings")
    closed = run + [("2025-10-01", "N-8F", "x")]
    assert un.investment_company_spans(closed, latest_filing="2026-09-11")[-1][1:] == ("2025-07-30", "filings")


def test_ic_evidence_keeps_n_px_only_before_mid_2024_and_amendments_as_their_form():
    filings = [("N-PX", "2025-09-02", "a"), ("N-PX", "2023-08-30", "b"), ("N-2/A", "2013-07-23", "c"),
               ("10-K", "2014-03-01", "d"), ("N-8F ORDR", "2015-08-27", "e"), ("497", "2014-01-01", "f")]
    assert un.ic_evidence(filings) == [("2013-07-23", "N-2", "c"), ("2015-08-27", "N-8F", "e"), ("2023-08-30", "N-PX", "b")]
    # Bank OZK files N-PX from 2025 only: no evidence, it stays in the base.
    assert un.ic_evidence([("N-PX", "2025-09-02", "a"), ("N-PX", "2026-08-30", "b")]) == []
    assert un.ic_evidence([], [("2014-02-01", "h")]) == [("2014-02-01", "SIC 6726", "h")]


def test_investment_companies_read_the_cached_submissions_and_apply_to_every_class(tmp_path):
    import gzip as gz
    def write(name, payload):
        (tmp_path / name).write_bytes(gz.compress(json.dumps(payload).encode()))
    write("CIK0000000010.json.gz", {"cik": "10", "sic": "", "filings": {
        "recent": {"form": ["10-K", "40-17G", "N-2"], "filingDate": ["2014-03-01", "2013-05-01", "2012-06-01"],
                   "accessionNumber": ["a", "b", "c"]},
        "files": [{"name": "CIK0000000010-submissions-001.json", "filingFrom": "2004-01-01", "filingTo": "2011-12-31"}]}})
    write("CIK0000000010-submissions-001.json.gz", {"form": ["N-54A"], "filingDate": ["2004-04-21"], "accessionNumber": ["e"]})
    write("CIK0000000020.json.gz", {"cik": "20", "sic": "6022", "filings": {
        "recent": {"form": ["N-PX", "10-K"], "filingDate": ["2025-09-02", "2026-02-01"], "accessionNumber": ["x", "y"]}}})
    master = pd.DataFrame({"security_id": ["10.A", "10.B", "20", "30"], "cik": ["10", "10", "20", ""],
                           "first_ticker": ["BDCA", "BDCB", "OZK", "NOCIK"]})
    spans, issuers, facts = un.investment_companies(master, {"10.A", "10.B", "20", "30"}, directory=tmp_path)
    assert spans == {"10.A": [("2004-04-21", un.IC_OPEN_END)], "10.B": [("2004-04-21", un.IC_OPEN_END)]}
    assert issuers["cik"].tolist() == ["10"] and issuers["election"].iloc[0] == "2004-04-21:e"
    assert facts["ciks"] == 2 and facts["pages_missing"] == 0 and facts["main_missing"] == []


def test_weekly_listed_leaves_investment_company_weeks_out_of_the_base():
    spans = pd.DataFrame({"security_id": ["altaba", "op"], "ticker": ["AABA", "OP"], "list_start": "2017-01-01",
                          "list_end": "2018-12-31", "non_common": False})
    weeks = pd.DatetimeIndex(["2017-06-09", "2017-06-16", "2017-06-23"])
    listed = un.weekly_listed(spans, weeks, {}, set(), {"altaba": [("2017-06-16", un.IC_OPEN_END)]}).set_index(
        ["security_id", "week_end"])
    assert listed.loc[("altaba", "2017-06-09"), "eligible"] and not listed.loc[("altaba", "2017-06-16"), "eligible"]
    assert listed.loc[("altaba", "2017-06-23"), "investment_company"] and listed.loc[("op", "2017-06-23"), "eligible"]


def test_ranks_including_investment_companies_report_their_former_top250_weeks():
    listed = pd.DataFrame({"security_id": ["bdc", "op"], "week_index": [0, 0], "eligible": [False, True],
                           "investment_company": [True, False], "non_common": False, "spac_shell": False, "foreign": False,
                           "close": [20.0, 20.0], "dv50": [5e8, 1e8], "dv20": [5e8, 1e8]})
    ranked = un.rank_weeks(listed).set_index("security_id")
    assert np.isnan(ranked.loc["bdc", "dv50_rank"]) and ranked.loc["op", "dv50_rank"] == 1
    assert ranked.loc["bdc", "dv50_rank_incl_investment"] == 1 and ranked.loc["op", "dv50_rank_incl_investment"] == 2


def test_listed_now_shells_follow_step6_listed_now_set():
    spans = pd.DataFrame({"security_id": ["dync", "ccix", "op", "old"], "list_start": "2025-01-01",
                          "list_end": [un.WINDOW_END, "2026-08-05", un.WINDOW_END, "2026-06-30"], "non_common": False})
    looks = {"dync", "ccix"}
    added, ending = un.listed_now_shells(spans, None, None, {"old": "sic_6770_spac_name"},
                                         spac_like=lambda ids: set(ids) & looks)
    assert added == ["dync"] and ending == ["ccix"]


def test_form25_and_nasdaq100_checks_set_investment_companies_aside():
    sessions = pd.bdate_range("2016-06-01", "2017-03-31")
    form25 = pd.DataFrame({"accession": ["x1"], "classification": "common_delisting", "public_float_usd": ["5e9"],
                           "float_check_flag": "ok", "effective_date": ["2017-01-13"], "filing_date": ["2017-01-03"],
                           "subject_name": ["American Capital"]})
    master = pd.DataFrame({"security_id": ["acas"], "delist_form25_accession": ["x1"]})
    spans = pd.DataFrame({"security_id": ["acas"], "non_common": False, "after_cut": False})
    out = un.form25_check(form25, master, {"acas": "2016-11-30"}, sessions, {}, {}, set(), spans,
                          investment={"acas": [("1997-08-27", "2017-01-12")]})
    assert out["status"].tolist() == ["excluded_investment_company"]
    s12 = pd.bdate_range("2012-01-02", "2012-12-31")
    spans = pd.DataFrame({"security_id": ["bdc"], "ticker": ["BDC"], "list_start": "2011-01-01", "list_end": "2026-08-31"})
    master = pd.DataFrame({"security_id": ["bdc"], "foreign_filer": ["N"], "tickers_observed": ["BDC"]})
    n100 = un.nasdaq100_check({"2012": ["BDC"]}, spans, master, {}, pd.DataFrame({"security_id": [], "date": []}), s12,
                              investment={"bdc": [("2004-01-01", un.IC_OPEN_END)]})
    assert n100["status"].tolist() == ["investment_company_excluded"]


# ------------------------------------------------------------------ round 7 review: young rule 1, merger tails, inputs, junctions

def _spans(rows):
    base = {"ipo_start": False, "start_prev_absent": "", "list_end": "2026-08-31"}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_new_listing_evidence_needs_an_issuer_not_public_before_and_dates_a_snapshot_start_from_the_gap():
    sessions = pd.bdate_range("2013-01-01", "2021-12-31")
    master = pd.DataFrame({
        "security_id": ["nbl", "linta", "qvca", "vwr", "ipo", "old"],
        "cik": ["72207", "1355096", "1355096", "1412232", "9", "8"],
        "domestic_periodic_first": ["2011-07-28", "2011-08-09", "2011-08-09", "2014-11-06", "", ""]})
    spans = _spans([
        # NBL: a transfer from NYSE (its 10-Ks go back years): not a new listing.
        {"security_id": "nbl", "list_start": "2020-02-25", "start_prev_absent": "2020-02-24"},
        # QVCA: the reclassified LINTA tracking stock under a new security_id.
        {"security_id": "linta", "list_start": "2010-12-31", "list_end": "2014-11-20"},
        {"security_id": "qvca", "list_start": "2014-11-21", "start_prev_absent": "2014-09-21"},
        # VWR: IPO 2014-10-02, first in a snapshot of 2014-11-21, absent from the one of 2014-09-21.
        {"security_id": "vwr", "list_start": "2014-11-21", "start_prev_absent": "2014-09-21"},
        {"security_id": "ipo", "list_start": "2016-05-10", "ipo_start": True, "start_prev_absent": "2016-04-01"},
        {"security_id": "old", "list_start": "2010-12-31"},
    ])
    out = un.new_listing_evidence(spans, master, sessions).set_index("security_id")
    assert out.loc["nbl", "new_listing_basis"] == "older_issuer" and not out.loc["nbl", "new_listing"]
    assert out.loc["linta", "new_listing_basis"] == "no_earlier_snapshot"   # listed since the first snapshot
    assert out.loc["qvca", "new_listing_basis"] == "older_issuer" and out.loc["qvca", "listing_continues"]
    assert out.loc["vwr", "new_listing_basis"] == "snapshot_gap" and out.loc["vwr", "earliest_start"] == "2014-09-22"
    assert out.loc["ipo", "new_listing_basis"] == "ipo_rule" and out.loc["ipo", "earliest_start"] == "2016-05-10"
    assert out.loc["old", "new_listing_basis"] == "no_earlier_snapshot" and out.loc["old", "earliest_start"] == ""
    # Without the issuer's 10-K history, the other listed class still shows a reorganisation.
    bare = master.assign(domestic_periodic_first="")
    out = un.new_listing_evidence(spans, bare, sessions).set_index("security_id")
    assert out.loc["qvca", "new_listing_basis"] == "issuer_listed_before" and out.loc["qvca", "listing_continues"]


def test_young_rule_1_counts_from_the_earliest_start_and_skips_names_public_before():
    sessions = pd.bdate_range("2014-06-02", "2015-03-31")
    weeks = pd.DatetimeIndex(pd.Series(sessions, index=sessions).groupby(sessions.to_period("W-SUN")).max().values)
    week_pos = sessions.get_indexer(weeks)
    spans = _spans([{"security_id": "vwr", "list_start": "2014-11-21", "start_prev_absent": "2014-09-21"},
                    {"security_id": "new", "list_start": "2014-11-21", "start_prev_absent": "2014-11-14"},
                    {"security_id": "nbl", "list_start": "2014-11-21", "start_prev_absent": "2014-11-14"}])
    master = pd.DataFrame({"security_id": ["vwr", "new", "nbl"], "cik": ["1", "2", "3"],
                           "domestic_periodic_first": ["2014-11-06", "", "2011-07-28"]})
    newness = un.new_listing_evidence(spans, master, sessions)
    k = int(np.searchsorted(weeks, pd.Timestamp("2014-11-21")))
    listed = pd.DataFrame({"security_id": ["vwr", "new", "nbl"], "week_index": [k] * 3, "week_end": [weeks[k]] * 3,
                           "listing_start": "2014-11-21", "close": np.nan, "dv50": np.nan, "first_row": "",
                           "pf_dv50": np.nan, "pf_n50": np.nan, "pf_first_data": pd.to_datetime([None] * 3)})
    listed = un.attach_new_listing(listed, spans, newness, sessions, week_pos)
    # VWR: 45 sessions since 2014-09-22 (not young: its weeks stay missing); a name absent from the
    # snapshot a week before is at most in its fifth session; NBL (a transfer) has no earliest start.
    assert listed["listing_sessions"].tolist()[:2] == [45, 5] and np.isnan(listed["listing_sessions"].iloc[2])
    assert un.young_rules(listed).tolist() == ["", "new_listing", ""]
    # A short step-6 series of an issuer that was public before is no new listing either (QVCA).
    short = listed.assign(pf_n50=[np.nan, np.nan, 10.0], pf_first_data=pd.to_datetime([None, None, "2014-11-21"]))
    assert un.young_rules(short).tolist()[2] == ""
    assert un.young_rules(short.assign(listing_continues=[False, False, False])).tolist()[2] == "short_series"


def test_a_final_ipo_prospectus_in_the_snapshot_gap_dates_the_start():
    sessions = pd.bdate_range("2014-06-02", "2015-03-31")
    spans = _spans([{"security_id": "vwr", "list_start": "2014-11-21", "start_prev_absent": "2014-09-21"},
                    {"security_id": "ipo", "list_start": "2014-11-21", "start_prev_absent": "2014-09-21"},
                    {"security_id": "old", "list_start": "2014-11-21", "start_prev_absent": "2014-09-21"}])
    master = pd.DataFrame({"security_id": ["vwr", "ipo", "old"], "cik": ["1", "2", "3"], "domestic_periodic_first": ""})
    # VWR's 424B4 of 2014-10-02 dates its start two sessions earlier (still 39 sessions before 2014-11-21:
    # not young); a later IPO in the same gap files on 2014-11-14; a 424B4 long before the gap is not this listing.
    prospectus = {"1": ["2014-10-02"], "2": ["2014-11-14"], "3": ["2013-05-01"]}
    out = un.new_listing_evidence(spans, master, sessions, prospectus).set_index("security_id")
    assert out.loc["vwr", "new_listing_basis"] == "prospectus" and out.loc["vwr", "earliest_start"] == "2014-09-30"
    assert out.loc["ipo", "new_listing_basis"] == "prospectus" and out.loc["ipo", "earliest_start"] == "2014-11-12"
    assert out.loc["old", "new_listing_basis"] == "snapshot_gap" and out.loc["old", "earliest_start"] == "2014-09-22"


def test_investment_companies_collect_each_ciks_final_prospectus_dates(tmp_path):
    import gzip as gz
    (tmp_path / "CIK0000000030.json.gz").write_bytes(gz.compress(json.dumps({"cik": "30", "filings": {"recent": {
        "form": ["424B4", "S-1", "424B5", "424B1"], "filingDate": ["2014-10-02", "2014-08-01", "2016-03-01", "2018-05-01"],
        "accessionNumber": ["a", "b", "c", "d"]}}}).encode()))
    master = pd.DataFrame({"security_id": ["30"], "cik": ["30"], "first_ticker": ["VWR"]})
    _, issuers, facts = un.investment_companies(master, {"30"}, directory=tmp_path)
    assert facts["prospectus"] == {"30": ["2014-10-02", "2018-05-01"]} and issuers.empty


def test_young_summary_counts_rule_1_weeks_at_the_band_median_and_the_first_run_weeks_left_missing():
    sessions = pd.bdate_range("2020-01-01", periods=40)
    weeks = pd.DatetimeIndex(pd.Series(sessions, index=sessions).groupby(sessions.to_period("W-SUN")).max().values)
    week_pos = sessions.get_indexer(weeks)
    listed = pd.DataFrame({"security_id": ["new", "nbl"], "ticker": ["NEW", "NBL"], "week_index": [1, 1],
                           "week_end": [weeks[1]] * 2, "eligible": True, "young": [True, False],
                           "young_rule": ["new_listing", ""], "close": np.nan, "dv50": np.nan, "pf_dv50": np.nan,
                           "proxy_above": [True, True], "listing_start": sessions[0].strftime("%Y-%m-%d"),
                           "first_listing_run": True, "new_listing_basis": ["snapshot_gap", "older_issuer"],
                           "missing": [False, True]})
    out = un.young_summary(listed, sessions, week_pos)
    assert out["by_rule"] == {"new_listing": 1} and out["new_listing_rule"]["proxy_above_name_weeks"] == 1
    assert out["new_listing_rule"]["proxy_above_securities"] == {"new:NEW": 1}
    left = out["first_run_weeks_not_young"]
    assert left["by_basis"] == {"older_issuer": 1} and left["missing_name_weeks"] == 1
    assert left["proxy_above_securities"] == {"nbl:NBL": 1}


def test_a_bdc_that_withdraws_on_its_merger_date_stays_out_through_the_merger_tail():
    # American Capital: N-54C on 2017-01-03 (the merger's closing), listed to 2017-01-12 (Form 25).
    evidence = _evidence(("2009-06-19", "40-17G"), ("2009-08-19", "N-2"), ("2017-01-03", "N-54C"))
    ends = [("2001-01-02", "2017-01-12", ["2017-01-13", "2017-01-03"])]
    spans = un.investment_company_spans(evidence, latest_filing="2017-01-20", listing_ends=ends)
    assert spans == [("2009-06-19", "2017-01-12", "bdc_until_withdrawal+filings+merger_tail")]
    assert un.investment_company_spans(evidence, latest_filing="2017-01-20")[0][1] == "2017-01-02"
    # Medallion withdrew in 2018 and kept trading as a bank holding company: no tail.
    medallion = _evidence(("2004-04-21", "N-54A"), ("2018-04-02", "N-54C"))
    kept = un.investment_company_spans(medallion, listing_ends=[("2004-01-02", "2026-08-30", [])])
    assert kept == [("2004-04-21", "2018-04-01", "bdc_election")]
    # A fund whose regular filings end months before the merger: its N-8F at the delisting carries it there.
    fund = _evidence(("2014-02-28", "N-CSR"), ("2014-08-29", "N-CSRS"), ("2015-02-27", "N-CSR"), ("2015-11-20", "N-8F"))
    tail = un.investment_company_spans(fund, latest_filing="2015-12-01",
                                       listing_ends=[("2010-01-04", "2015-11-30", ["2015-12-01"])])
    assert tail == [("2014-02-28", "2015-11-30", "filings+merger_tail")]
    # A span that closes inside a listing whose end is far away is left alone.
    far = un.investment_company_spans(fund, latest_filing="2015-12-01", listing_ends=[("2010-01-04", "2019-06-28", [])])
    assert far == [("2014-02-28", "2015-02-27", "filings")]


def test_listing_ends_join_renames_skip_open_listings_and_carry_the_delisting_dates():
    spans = pd.DataFrame({"security_id": ["a", "a", "a", "b"], "list_start": ["2010-01-04", "2014-05-01", "2018-01-02", "2015-01-02"],
                          "list_end": ["2014-04-30", "2016-12-30", "2026-08-31", "2021-03-18"]})
    master = pd.DataFrame({"security_id": ["a", "b"], "delist_date": ["", "2021-03-29"]})
    terminal = pd.DataFrame({"security_id": ["b"], "last_price_date": ["2021-03-18"]})
    ends = un.listing_ends(spans, master, terminal)
    assert ends == {"a": [("2010-01-04", "2016-12-30", [])], "b": [("2015-01-02", "2021-03-18", ["2021-03-29", "2021-03-18"])]}


def test_investment_companies_record_every_submissions_file_and_a_digest_that_moves_with_the_cache(tmp_path):
    import gzip as gz
    def write(name, payload):
        (tmp_path / name).write_bytes(gz.compress(json.dumps(payload).encode()))
    write("CIK0000000010.json.gz", {"cik": "10", "sic": "", "filings": {
        "recent": {"form": ["N-2", "40-17G"], "filingDate": ["2012-06-01", "2013-05-01"], "accessionNumber": ["c", "b"]},
        "files": [{"name": "CIK0000000010-submissions-001.json", "filingFrom": "2004-01-01", "filingTo": "2011-12-31"}]}})
    master = pd.DataFrame({"security_id": ["10", "20"], "cik": ["10", "20"], "first_ticker": ["BDC", "GONE"]})
    _, _, facts = un.investment_companies(master, {"10", "20"}, directory=tmp_path)
    assert facts["files"]["CIK0000000010-submissions-001.json.gz"] == un.SUBMISSIONS_MISSING
    assert facts["files"]["CIK0000000020.json.gz"] == un.SUBMISSIONS_MISSING
    assert facts["files_read"] == 1 and facts["files_missing"] == 2
    assert facts["digest"] == un.submissions_digest(facts["files"])
    write("CIK0000000010-submissions-001.json.gz", {"form": ["N-54A"], "filingDate": ["2004-04-21"], "accessionNumber": ["e"]})
    _, _, later = un.investment_companies(master, {"10", "20"}, directory=tmp_path)
    assert later["digest"] != facts["digest"] and later["files_read"] == 2


def test_dollar_volume_windows_restart_at_a_relist_junction():
    sessions, weeks, week_pos, prev_pos = _grid(80)
    rows = [("CHRD", day, 2.0 if k < 40 else 100.0, 1000.0, "yahoo", k == 40) for k, day in enumerate(sessions)]
    rows += [("SAME", day, 2.0 if k < 40 else 100.0, 1000.0, "yahoo", False) for k, day in enumerate(sessions)]
    panel = pd.DataFrame(rows, columns=["security_id", "date", "close_raw", "volume_raw", "src_primary", "relist_junction"])
    m = un.canonical_metrics(panel, sessions, week_pos, prev_pos)
    chrd, same = list(m["ids"]).index("CHRD"), list(m["ids"]).index("SAME")
    for w, pos in enumerate(week_pos):
        new_rows = pos - 40 + 1
        if new_rows <= 0:
            assert m["dv50"][w, chrd] == m["dv50"][w, same] or np.isnan(m["dv50"][w, chrd])
        elif new_rows < 10:
            assert np.isnan(m["dv20"][w, chrd]) and np.isnan(m["dv50"][w, chrd])  # the new shares' own rows only
        elif new_rows < 25:
            assert m["dv20"][w, chrd] == pytest.approx(100_000.0) and np.isnan(m["dv50"][w, chrd])
        else:
            assert m["dv50"][w, chrd] == pytest.approx(100_000.0)
    assert m["relist_junctions"] == {"CHRD": [sessions[40].strftime("%Y-%m-%d")]}
    # Without the flag the old shares' rows stay in the window (the same series).
    assert not np.isnan(m["dv50"][int(np.searchsorted(week_pos, 42)), same])


def test_the_canonical_young_rule_restarts_at_a_relist_junction():
    # CHRD-shaped: the old shares' rows start ~5 months before the junction, so counting from the security's
    # first row the new shares' first weeks (a close, no dv50 yet) would be missing; counted from the
    # junction they are young until the new shares hold 25 rows.
    sessions, weeks, week_pos, prev_pos = _grid(140)
    junction = 100
    rows = [("CHRD", day, 2.0 if k < junction else 100.0, 1000.0, "yahoo", k == junction)
            for k, day in enumerate(sessions)]
    panel = pd.DataFrame(rows, columns=["security_id", "date", "close_raw", "volume_raw", "src_primary", "relist_junction"])
    m = un.canonical_metrics(panel, sessions, week_pos, prev_pos)
    listed = pd.DataFrame({"security_id": "CHRD", "week_index": np.arange(len(weeks)), "week_end": weeks})
    out = un.attach_metrics(listed, m)
    day = sessions[junction].strftime("%Y-%m-%d")
    assert (out["first_row"] == sessions[0].strftime("%Y-%m-%d")).all()
    before = out["week_end"] < sessions[junction]
    assert (out.loc[before, "segment_first_row"] == out.loc[before, "first_row"]).all()
    assert (out.loc[out["week_end"] >= sessions[junction], "segment_first_row"] == day).all()
    rules = un.young_rules(out)
    new_rows = week_pos - junction + 1
    after = new_rows >= 1
    assert after.any() and (new_rows[after] < 25).any() and (new_rows[after] >= 25).any()
    for w in np.flatnonzero(after):
        if new_rows[w] < 25:
            assert np.isnan(out.loc[w, "dv50"]) and rules[w] == "canonical", (w, new_rows[w])
        else:
            assert not np.isnan(out.loc[w, "dv50"]) and rules[w] == "", (w, new_rows[w])
    # Without the segment column (first_row only) the same weeks are not young: the defect this fixes.
    assert (un.young_rules(out.drop(columns="segment_first_row"))[after & (new_rows < 25)] == "").all()
    # A security without a junction keeps its first row.
    plain = un.segment_first_rows(pd.DataFrame({"security_id": ["X"], "week_end": weeks[:1], "first_row": ["2012-01-02"]}),
                                  {"CHRD": [day]})
    assert plain.tolist() == ["2012-01-02"]


def test_one_ticker_only_snapshot_row_against_a_non_nasdaq_sec_exchange_is_left_out_and_reported():
    base = {"start_prev_absent": "", "end_next_absent": "", "source": "repo_symdir", "source_url": "x",
            "name_in_source": "", "share_class": "COMMON", "exchange": "NASDAQ"}
    intervals = pd.DataFrame([
        {**base, "security_id": "37996", "ticker": "F", "start": "2019-06-17", "end": "2019-06-17",
         "start_prev_absent": "2019-06-14", "end_next_absent": "2019-08-09", "n_snapshots": "1", "match": "ticker_only"},
        # The same shape for a delisted security (no SEC current exchange) or one on Nasdaq now: kept.
        {**base, "security_id": "1597033", "ticker": "SABR", "start": "2011-05-27", "end": "2011-05-27",
         "n_snapshots": "1", "match": "ticker_only"},
        {**base, "security_id": "9", "ticker": "OLD", "start": "2013-01-02", "end": "2013-01-02",
         "n_snapshots": "1", "match": "ticker_only"},
        # Two snapshots, or a name match: kept even with an SEC exchange elsewhere.
        {**base, "security_id": "8", "ticker": "TWO", "start": "2015-01-02", "end": "2015-02-02",
         "n_snapshots": "2", "match": "ticker_only"},
        {**base, "security_id": "7", "ticker": "NAME", "start": "2015-01-02", "end": "2015-01-02",
         "n_snapshots": "1", "match": "name+ticker"}])
    master = pd.DataFrame({"security_id": ["37996", "1597033", "9", "8", "7"],
                           "exchanges_sec_current": ["NYSE NYSE NYSE NYSE", "NASDAQ", "", "NYSE", "NYSE"]})
    assert un.doubtful_intervals(intervals, master).tolist() == [True, False, False, False, False]
    kept, report = un.drop_doubtful_intervals(intervals, master)
    assert kept["security_id"].tolist() == ["1597033", "9", "8", "7"]
    assert [(r["security_id"], r["ticker"], r["start"], r["sec_current_exchanges"]) for r in report] == [
        ("37996", "F", "2019-06-17", "NYSE NYSE NYSE NYSE")]
    # A file without the snapshot columns keeps every interval.
    assert not un.doubtful_intervals(intervals.drop(columns=["n_snapshots"]), master).any()


def test_the_summary_definitions_follow_the_constants_the_code_uses():
    young = un.young_definition()
    for part in (f"{un.YOUNG_DAYS} days", f"{un.YOUNG_ISSUER_DAYS} days", "1 to 24", "24 or fewer", "n50 = 0",
                 "older_issuer", "issuer_listed_before", "snapshot_gap", "ipo_rule", "successor_link",
                 f"within {un.SERIES_START_SESSIONS} sessions of its earliest start"):
        assert part in young, part
    link = un.successor_link_definition()
    for part in (f"{un.SUCCESSOR_LINK_DAYS} days", "stock_merger / reorganization of 1 successor share", "no day"):
        assert part in link, part
    ic = un.investment_company_definition()
    for part in (un.IC_ELECTION, un.IC_WITHDRAWAL, un.IC_DEREGISTRATION, un.IC_OPEN_END, f"{un.IC_RUN_GAP_DAYS} days",
                 f">= {un.IC_MIN_RUN}", "filings_current", "merger_tail", f"{un.IC_TAIL_DAYS} days", un.IC_NPX_BEFORE):
        assert part in ic, part


# ------------------------------------------------------------------ round 8 review: young only for new listings, successor links

def _link_master(rows):
    base = {"cik": "", "domestic_periodic_first": "", "successor_security_id": "", "successor_date": "", "delist_date": "",
            "transfer_date": ""}
    return pd.DataFrame([{**base, **r} for r in rows])


def _reorganization(predecessor, successor, kind="stock_merger", subtype="reorganization", shares="1"):
    return {"security_id": predecessor, "terminal_type": kind, "event_subtype": subtype, "consideration_shares": shares,
            "acquirer_security_id": successor}


def test_googl_shape_a_1to1_successor_link_runs_the_windows_across_and_is_no_new_listing():
    # Google -> Alphabet (2015-10-02): the old class A rows run to 10-08 (and three more rows after the
    # handover here, which must not count), Alphabet's from 10-09, its first listed day; a second link has the
    # LBTYA shape, the successor's own rows duplicated before its listing starts.
    sessions, weeks, week_pos, prev_pos = _grid(80)
    day = lambda k: sessions[k].strftime("%Y-%m-%d")
    hand = 45
    rows = [("goog", sessions[k], 1000.0 if k >= hand else 10.0, 1000.0, "wiki") for k in range(hand + 3)]
    rows += [("googl", sessions[k], 20.0, 1000.0, "wiki") for k in range(hand, 80)]
    rows += [("lgi", sessions[k], 10.0, 1000.0, "wiki") for k in range(hand)]
    rows += [("lgi_plc", sessions[k], 30.0 if k < hand else 20.0, 1000.0, "wiki") for k in range(hand - 5, 80)]
    panel = pd.DataFrame(rows, columns=["security_id", "date", "close_raw", "volume_raw", "src_primary"])
    master = _link_master([
        {"security_id": "goog", "cik": "1288776", "domestic_periodic_first": "2004-11-12",
         "successor_security_id": "googl", "successor_date": day(40)},
        {"security_id": "googl", "cik": "1652044"},
        {"security_id": "lgi", "cik": "1316631", "domestic_periodic_first": "2005-08-15",
         "successor_security_id": "lgi_plc", "successor_date": day(40)},
        {"security_id": "lgi_plc", "cik": "1570585"}])
    spans = _spans([{"security_id": "goog", "list_start": "2010-12-31", "list_end": day(hand - 1)},
                    {"security_id": "googl", "list_start": day(hand), "start_prev_absent": day(hand - 2), "ipo_start": True},
                    {"security_id": "lgi", "list_start": "2010-12-31", "list_end": day(hand - 1)},
                    {"security_id": "lgi_plc", "list_start": day(hand), "start_prev_absent": day(hand - 2)}])
    terminal = pd.DataFrame([_reorganization("goog", "googl"), _reorganization("lgi", "lgi_plc")])
    plain = un.canonical_metrics(panel, sessions, week_pos, prev_pos)
    links = un.successor_links(master, spans, plain["first_row"], terminal).set_index("successor_id")
    assert links.loc["googl", "continuing"] and links.loc["googl", "continuing_basis"] == "successor_date"
    assert links.loc["googl", "one_to_one"] and links.loc["googl", "windows_cross"]
    assert links.loc["googl", "handover"] == day(hand)
    m = un.canonical_metrics(panel, sessions, week_pos, prev_pos, links=links.reset_index())
    ids = list(m["ids"])
    g, old, plc = ids.index("googl"), ids.index("goog"), ids.index("lgi_plc")
    first = int(np.searchsorted(week_pos, hand))          # Alphabet's first week: 5 rows of its own
    assert np.isnan(plain["dv50"][first, g]) and np.isnan(plain["dv20"][first, g])
    for w in range(first, len(weeks)):
        pos = week_pos[w]
        # one row per session: the predecessor's before the handover, Alphabet's own from it (never the
        # predecessor's three rows after the handover, at 100 times the dollar volume)
        combined = [1e4 if k < hand else 2e4 for k in range(pos - 49, pos + 1)]
        assert m["dv50"][w, g] == pytest.approx(np.median(combined)), w
        assert m["dv20"][w, g] == pytest.approx(np.median(combined[-20:])), w
    assert m["successor_windows"]["googl"] == {"predecessor": "goog", "handover": day(hand),
                                               "predecessor_rows_in_first_window": 45, "own_rows_replaced": 0,
                                               "predecessor_rows_on_or_after_handover_unused": 3}
    # The successor's own rows before its listing start give way to the predecessor's (no day twice).
    assert m["successor_windows"]["lgi_plc"]["own_rows_replaced"] == 5
    assert m["dv50"][first, plc] == pytest.approx(np.median([1e4 if k < hand else 2e4 for k in
                                                             range(week_pos[first] - 49, week_pos[first] + 1)]))
    # The predecessor's own windows, the closes and the first rows do not change.
    assert np.array_equal(m["dv50"][:, old], plain["dv50"][:, old], equal_nan=True)
    assert np.array_equal(m["close"], plain["close"], equal_nan=True)
    assert m["first_row"] == plain["first_row"] and m["first_row"]["googl"] == day(hand)
    # Alphabet is no new listing: even without a dv50 (the windows not run across) it is not young.
    newness = un.new_listing_evidence(spans, master, sessions, links=links.reset_index(), first_row=m["first_row"])
    ev = newness.set_index("security_id")
    assert ev.loc["googl", "new_listing_basis"] == "successor_link" and ev.loc["googl", "predecessor_security_id"] == "goog"
    assert ev.loc["googl", "listing_continues"] and not ev.loc["googl", "new_listing"]
    assert not ev.loc["googl", "series_starts_listing"]
    listed = pd.DataFrame({"security_id": "googl", "week_index": np.arange(first, len(weeks)),
                           "week_end": weeks[first:], "listing_start": day(hand), "pf_dv50": np.nan,
                           "pf_n50": 5.0, "pf_first_data": pd.to_datetime(day(hand))})
    listed = un.attach_new_listing(listed, spans, newness, sessions, week_pos)
    without = un.attach_metrics(listed, plain)
    assert np.isnan(without["dv50"].iloc[0]) and not np.isnan(without["close"].iloc[0])
    assert (un.young_rules(without) == "").all()           # before the fix: "canonical" for 5 weeks
    across = un.attach_metrics(listed, m)
    assert across["dv50"].notna().all()                    # ranked from its first week


def test_kdp_and_hst_shape_a_transfer_from_nyse_without_rows_before_it_stays_missing_and_blocks():
    # KDP (2020-09-21) and HST (2020-11-02) moved from NYSE; their rows start at the Nasdaq start, so the
    # first weeks have a close and no dv50. An issuer filing 10-Ks for years is no new listing: missing, and
    # the proxy (or step 6's dollar volume) blocks the week. A real IPO of the same shape stays young.
    sessions = pd.bdate_range("2020-06-01", "2021-03-31")
    weeks = pd.DatetimeIndex(pd.Series(sessions, index=sessions).groupby(sessions.to_period("W-SUN")).max().values)
    week_pos = sessions.get_indexer(weeks)
    master = _link_master([{"security_id": "kdp", "cik": "1418135", "domestic_periodic_first": "2008-05-08"},
                           {"security_id": "hst", "cik": "1070750", "domestic_periodic_first": "1999-03-26"},
                           {"security_id": "ipo", "cik": "9"},
                           {"security_id": "gpro", "cik": "8"}])
    spans = _spans([{"security_id": "kdp", "list_start": "2020-09-21", "start_prev_absent": "2020-09-18"},
                    {"security_id": "hst", "list_start": "2020-11-02", "start_prev_absent": "2020-10-30"},
                    {"security_id": "ipo", "list_start": "2020-09-21", "start_prev_absent": "2020-09-11", "ipo_start": True},
                    # GoPro-shaped: an IPO prospectus dates the start; the series begins 8 sessions later.
                    {"security_id": "gpro", "list_start": "2020-09-25", "start_prev_absent": "2020-09-04"}])
    first_row = {"kdp": "2020-09-21", "hst": "2020-11-02", "ipo": "2020-09-21", "gpro": "2020-09-25"}
    newness = un.new_listing_evidence(spans, master, sessions, {"8": ["2020-09-16"]}, first_row=first_row)
    ev = newness.set_index("security_id")
    assert ev.loc["kdp", "new_listing_basis"] == "older_issuer" and ev.loc["hst", "new_listing_basis"] == "older_issuer"
    assert ev.loc["ipo", "series_starts_listing"] and ev.loc["ipo", "series_start_gap"] == 0
    assert ev.loc["gpro", "new_listing_basis"] == "prospectus" and ev.loc["gpro", "series_start_gap"] == 9
    assert not ev.loc["gpro", "series_starts_listing"]
    k = int(np.searchsorted(weeks, pd.Timestamp("2020-10-09")))
    h = int(np.searchsorted(weeks, pd.Timestamp("2020-11-13")))
    ids = ["kdp", "hst", "ipo", "gpro"]
    listed = pd.DataFrame({"security_id": ids, "week_index": [k, h, k, k], "week_end": weeks[[k, h, k, k]],
                           "listing_start": ["2020-09-21", "2020-11-02", "2020-09-21", "2020-09-25"],
                           "first_row": [first_row[s] for s in ids], "close": 30.0, "dv50": np.nan,
                           "price_ge_10": "Y", "eligible": True, "dv50_rank_any_price": np.nan, "has_series": True,
                           "last_row": "2026-08-31", "pf_outside_trading": False, "pf_dv50": np.nan, "pf_price": "",
                           "pf_n50": np.nan, "pf_first_data": pd.to_datetime([None] * 4),
                           "mcap": [4e10, 1.2e10, 4e10, 4e9], "float_usd": np.nan, "multi_class": False, "cik": "1"})
    listed = un.attach_new_listing(listed, spans, newness, sessions, week_pos)
    # Before the fix the canonical rule called all four young; now only the IPO whose series starts at its
    # start; GoPro is young by rule 1 (sessions from its earliest start), the transfers by none.
    assert un.young_rules(listed).tolist() == ["", "", "canonical", "new_listing"]
    cut = pd.DataFrame({"cut250": 1e8, "cut300": 8e7, "cut_mcap": 1e10, "cut_float": 1e10},
                       index=pd.Index(sorted({k, h}), name="week_index"))
    out = un.mark_missing(listed, spans.assign(list_end="2026-08-31"), {s: "not_candidate" for s in ids}, cut)
    out = out.set_index("security_id")
    assert out.loc[["kdp", "hst"], "missing"].all() and not out.loc[["ipo", "gpro"], "missing"].any()
    assert (out.loc[["kdp", "hst"], "evidence"] == "proxy").all() and out.loc[["kdp", "hst"], "proxy_above"].all()
    assert un.blocks_week(out.loc[["kdp", "hst"]]).all()
    # With step 6's dollar volume at or above the cut (its own Yahoo history), the dv evidence blocks instead.
    dv = un.mark_missing(listed.assign(pf_dv50=2e8, pf_price="Y"), spans.assign(list_end="2026-08-31"),
                         {s: "not_candidate" for s in ids}, cut).set_index("security_id")
    assert dv.loc["kdp", "missing"] and dv.loc["kdp", "pf_ge_cut250"] and un.blocks_week(dv.loc[["kdp"]]).all()
    # A relist junction still restarts the rule (CHRD): a segment that starts there is young.
    junction = listed.assign(segment_first_row=listed["first_row"], segment_junction=[True, False, False, False])
    assert un.young_rules(junction).tolist()[0] == "canonical"


def test_azpn_shape_a_successor_with_a_predecessor_is_a_continuing_listing_for_every_young_rule():
    # New AspenTech (2022-05-16, Emerson's $87.69 + 0.42 share): its listing starts 2022-06-07, the day after
    # the old AspenTech's last listed day, 22 days after the successor date; its first canonical row is 05-12.
    # A QDEL-shaped link (QuidelOrtho listed from 06-07, the Form 25 dated 05-27) counts by the handover alone.
    sessions = pd.bdate_range("2022-01-03", "2022-12-30")
    weeks = pd.DatetimeIndex(pd.Series(sessions, index=sessions).groupby(sessions.to_period("W-SUN")).max().values)
    week_pos = sessions.get_indexer(weeks)
    master = _link_master([
        {"security_id": "929940", "cik": "929940", "domestic_periodic_first": "1996-02-14",
         "successor_security_id": "1897982", "successor_date": "2022-05-16"},
        {"security_id": "1897982", "cik": "1897982", "domestic_periodic_first": "2022-08-09"},
        {"security_id": "353569", "cik": "353569", "domestic_periodic_first": "1994-01-01",
         "successor_security_id": "1906324", "successor_date": "2022-05-27"},
        {"security_id": "1906324", "cik": "1906324"},
        # a link far from the successor's listing (60 days after the date, 40 after the predecessor's end)
        {"security_id": "old", "cik": "7", "domestic_periodic_first": "2001-01-01",
         "successor_security_id": "far", "successor_date": "2022-03-01"},
        {"security_id": "far", "cik": "6"}])
    spans = _spans([
        {"security_id": "929940", "list_start": "2010-12-31", "list_end": "2022-06-06"},
        {"security_id": "1897982", "list_start": "2022-06-07", "start_prev_absent": "2022-06-03"},
        {"security_id": "353569", "list_start": "2010-12-31", "list_end": "2022-06-06"},
        {"security_id": "1906324", "list_start": "2022-06-07", "start_prev_absent": "2022-06-03"},
        {"security_id": "old", "list_start": "2010-12-31", "list_end": "2022-03-20"},
        {"security_id": "far", "list_start": "2022-04-29", "start_prev_absent": "2022-04-27", "ipo_start": True}])
    first_row = {"929940": "2011-06-01", "1897982": "2022-05-12", "353569": "2011-06-01", "1906324": "2022-06-07",
                 "old": "2011-06-01", "far": "2022-04-29"}
    terminal = pd.DataFrame([_reorganization("929940", "1897982", "mixed", "merger", "0.42"),
                             _reorganization("353569", "1906324")])
    links = un.successor_links(master, spans, first_row, terminal).set_index("successor_id")
    assert links.loc["1897982", "continuing"] and links.loc["1897982", "continuing_basis"] == "first_row"
    assert not links.loc["1897982", "one_to_one"] and not links.loc["1897982", "windows_cross"]
    assert "mixed / merger / 0.42" in links.loc["1897982", "one_to_one_basis"]
    assert links.loc["1906324", "continuing_basis"] == "handover" and links.loc["1906324", "windows_cross"]
    assert not links.loc["far", "continuing"] and links.loc["far", "continuing_basis"] == ""
    newness = un.new_listing_evidence(spans, master, sessions, links=links.reset_index(), first_row=first_row)
    ev = newness.set_index("security_id")
    assert ev.loc["1897982", "new_listing_basis"] == "successor_link" and ev.loc["1897982", "listing_continues"]
    assert ev.loc["1897982", "earliest_start"] == "" and ev.loc["1906324", "new_listing_basis"] == "successor_link"
    assert ev.loc["far", "new_listing_basis"] == "ipo_rule" and ev.loc["far", "series_starts_listing"]
    # AZPN 2022-06-10: rule 1 (before: new_listing), short_series (a 10-row step-6 series) and the canonical
    # rule (a close without a dv50) all leave it missing.
    k = int(np.searchsorted(weeks, pd.Timestamp("2022-06-10")))
    base = {"security_id": "1897982", "week_index": k, "week_end": weeks[k], "listing_start": "2022-06-07",
            "first_row": "2022-05-12", "dv50": np.nan, "pf_dv50": np.nan, "pf_n50": 10.0,
            "pf_first_data": pd.Timestamp("2022-06-07")}
    listed = pd.DataFrame([{**base, "close": np.nan}, {**base, "close": 150.0}])
    listed = un.attach_new_listing(listed, spans, newness, sessions, week_pos)
    assert np.isnan(listed["listing_sessions"]).all()
    assert un.young_rules(listed).tolist() == ["", ""]
    # The same rows read as a new listing (no link) were young by rule 1 and short_series.
    bare = un.new_listing_evidence(spans, master.assign(successor_security_id=""), sessions, first_row=first_row)
    assert bare.set_index("security_id").loc["1897982", "new_listing_basis"] == "snapshot_gap"
    unlinked = un.attach_new_listing(listed.drop(columns=["new_listing", "new_listing_basis", "listing_continues",
                                                          "earliest_start", "predecessor_security_id",
                                                          "series_starts_listing", "listing_sessions",
                                                          "first_listing_run"]),
                                     spans, bare, sessions, week_pos)
    assert un.young_rules(unlinked).tolist()[0] == "new_listing"


def test_esrx_shape_a_1to1_reorganisation_step_11_books_but_the_master_does_not_link_continues_the_security():
    # Express Scripts -> Express Scripts Holding (2012-04): the master has no successor link, step 11 books the
    # old shares' end as stock_merger / reorganization / 1 share into the new holding company (its Form 25 is
    # dated 17 days after the handover). The old rows run to the day before the new listing starts. Before
    # the fix the holding company was young for five weeks (canonical / short_series) while ranked 12th-13th.
    sessions, weeks, week_pos, prev_pos = _grid(80)
    day = lambda k: sessions[k].strftime("%Y-%m-%d")
    hand = 45
    rows = [("esrx_old", sessions[k], 10.0, 1000.0, "wiki") for k in range(hand)]
    rows += [("esrx_new", sessions[k], 20.0, 1000.0, "wiki") for k in range(hand, 80)]
    panel = pd.DataFrame(rows, columns=["security_id", "date", "close_raw", "volume_raw", "src_primary"])
    master = _link_master([
        {"security_id": "esrx_old", "cik": "885721", "domestic_periodic_first": "1996-03-29"},
        {"security_id": "esrx_new", "cik": "1532063"},
        # a master link elsewhere for the same kind of predecessor: step 11 adds nothing to it
        {"security_id": "linked", "cik": "5", "domestic_periodic_first": "2001-01-01",
         "successor_security_id": "linked_new", "successor_date": day(hand)},
        {"security_id": "linked_new", "cik": "4"},
        {"security_id": "solo", "cik": "3", "domestic_periodic_first": "2001-01-01"},
        {"security_id": "blank", "cik": "2", "domestic_periodic_first": "2001-01-01"},
        {"security_id": "cashy", "cik": "1", "domestic_periodic_first": "2001-01-01"},
        {"security_id": "cashy_new", "cik": "11"}])
    spans = _spans([
        {"security_id": "esrx_old", "list_start": "2010-12-31", "list_end": day(hand - 1)},
        {"security_id": "esrx_new", "list_start": day(hand), "start_prev_absent": day(hand - 2), "ipo_start": True},
        {"security_id": "linked", "list_start": "2010-12-31", "list_end": day(hand - 1)},
        {"security_id": "linked_new", "list_start": day(hand), "start_prev_absent": day(hand - 2)},
        {"security_id": "solo", "list_start": "2010-12-31", "list_end": day(hand - 1)},
        {"security_id": "blank", "list_start": "2010-12-31", "list_end": day(hand - 1)},
        {"security_id": "cashy", "list_start": "2010-12-31", "list_end": day(hand - 1)},
        {"security_id": "cashy_new", "list_start": day(hand), "start_prev_absent": day(hand - 2)}])
    end = {"end_date": day(hand + 12), "delist_date": day(hand + 12)}
    terminal = pd.DataFrame([{**_reorganization("esrx_old", "esrx_new"), **end},
                             {**_reorganization("linked", "esrx_new"), **end},     # the master link wins
                             {**_reorganization("solo", "not_listed"), **end},     # acquirer never listed
                             {**_reorganization("blank", ""), **end},              # no acquirer named
                             {**_reorganization("cashy", "cashy_new", "mixed", "merger", "0.42"), **end}])
    plain = un.canonical_metrics(panel, sessions, week_pos, prev_pos)
    links = un.successor_links(master, spans, plain["first_row"], terminal)
    assert sorted(zip(links["predecessor_id"], links["successor_id"], links["link_source"])) == [
        ("esrx_old", "esrx_new", "step11"), ("linked", "linked_new", "security_master")]
    esrx = links.set_index("successor_id").loc["esrx_new"]
    assert esrx["successor_date"] == day(hand + 12) and esrx["predecessor_end"] == day(hand - 1)
    assert esrx["continuing"] and esrx["continuing_basis"] == "handover"   # not 10 days from the Form 25
    assert esrx["one_to_one"] and esrx["windows_cross"] and esrx["handover"] == day(hand)
    # The windows run across: ranked from the first week, one row per session.
    m = un.canonical_metrics(panel, sessions, week_pos, prev_pos, links=links)
    k = list(m["ids"]).index("esrx_new")
    first = int(np.searchsorted(week_pos, hand))
    assert np.isnan(plain["dv50"][first, k])
    for w in range(first, len(weeks)):
        pos = week_pos[w]
        assert m["dv50"][w, k] == pytest.approx(np.median([1e4 if j < hand else 2e4 for j in range(pos - 49, pos + 1)]))
    assert m["successor_windows"]["esrx_new"]["predecessor_rows_in_first_window"] == 45
    # No new listing: not young by any rule, even without the windows run across.
    newness = un.new_listing_evidence(spans, master, sessions, links=links, first_row=m["first_row"]).set_index("security_id")
    assert newness.loc["esrx_new", "new_listing_basis"] == "successor_link"
    assert newness.loc["esrx_new", "predecessor_security_id"] == "esrx_old"
    assert not newness.loc["esrx_new", "series_starts_listing"]
    listed = pd.DataFrame({"security_id": "esrx_new", "week_index": np.arange(first, first + 5),
                           "week_end": weeks[first:first + 5], "listing_start": day(hand), "pf_dv50": np.nan,
                           "pf_n50": 5.0, "pf_first_data": pd.to_datetime(day(hand))})
    listed = un.attach_new_listing(listed, spans, newness.reset_index(), sessions, week_pos)
    without = un.attach_metrics(listed, plain)
    assert np.isnan(without["dv50"].iloc[0]) and (un.young_rules(without) == "").all()
    assert un.attach_metrics(listed, m)["dv50"].notna().all()
    # The round-9 failure: the same rows with no step-11 row read as a new listing, young by the canonical rule.
    bare = un.successor_links(master, spans, plain["first_row"], terminal.iloc[1:])
    assert "esrx_new" not in set(bare["successor_id"])
    old = un.new_listing_evidence(spans, master, sessions, links=bare, first_row=plain["first_row"])
    unlinked = un.attach_metrics(un.attach_new_listing(listed[["security_id", "week_index", "week_end",
                                                               "listing_start", "pf_dv50", "pf_n50", "pf_first_data"]],
                                                       spans, old, sessions, week_pos), plain)
    assert un.young_rules(unlinked).tolist()[0] == "canonical"


def test_iac_shape_a_predecessor_listed_on_after_the_successor_starts_does_not_continue_it():
    # New IAC (2020-07-01) is a spin-off: old IAC went on under its own security as MTCH to 2026 (a rename,
    # joined into one listing run), so the master link does not continue it and new IAC is a new listing.
    # RCM 2022 (the old shares listed 28 days past the new start) still continues; a predecessor that lists
    # again years later is measured by its run before the successor's start.
    sessions = pd.bdate_range("2014-01-01", "2023-12-29")
    master = _link_master([
        {"security_id": "iac_old", "cik": "891103", "domestic_periodic_first": "1996-01-01",
         "successor_security_id": "iac_new", "successor_date": "2020-06-30"},
        {"security_id": "iac_new", "cik": "1800227"},
        {"security_id": "rcm_old", "cik": "1472595", "domestic_periodic_first": "2010-01-01",
         "successor_security_id": "rcm_new", "successor_date": "2022-06-22"},
        {"security_id": "rcm_new", "cik": "1910851"},
        {"security_id": "back_old", "cik": "70", "domestic_periodic_first": "2001-01-01",
         "successor_security_id": "back_new", "successor_date": "2016-03-01"},
        {"security_id": "back_new", "cik": "71"}])
    spans = _spans([
        {"security_id": "iac_old", "list_start": "2010-12-31", "list_end": "2016-01-28"},
        {"security_id": "iac_old", "list_start": "2016-01-29", "list_end": "2020-07-28"},
        {"security_id": "iac_old", "list_start": "2020-07-29", "list_end": "2026-08-31"},   # as MTCH
        {"security_id": "iac_new", "list_start": "2020-07-01", "start_prev_absent": "2020-06-29", "ipo_start": True},
        {"security_id": "rcm_old", "list_start": "2017-03-16", "list_end": "2022-07-22"},
        {"security_id": "rcm_new", "list_start": "2022-06-24", "start_prev_absent": "2022-06-07"},
        {"security_id": "back_old", "list_start": "2010-12-31", "list_end": "2016-03-03"},
        {"security_id": "back_old", "list_start": "2019-05-01", "list_end": "2026-08-31"},
        {"security_id": "back_new", "list_start": "2016-03-04", "start_prev_absent": "2016-03-01"}])
    first_row = {"iac_old": "2011-06-01", "iac_new": "2020-07-01", "rcm_old": "2017-03-16", "rcm_new": "2022-03-25",
                 "back_old": "2011-06-01", "back_new": "2016-03-04"}
    links = un.successor_links(master, spans, first_row).set_index("successor_id")
    assert links.loc["iac_new", "predecessor_end"] == "2026-08-31"
    assert links.loc["iac_new", "continuing_basis"] == "predecessor_listed_on" and not links.loc["iac_new", "continuing"]
    assert links.loc["rcm_new", "continuing"] and links.loc["rcm_new", "continuing_basis"] == "successor_date"
    assert links.loc["back_new", "predecessor_end"] == "2016-03-03" and links.loc["back_new", "continuing"]
    # With a tighter overlap the RCM shape would not continue either.
    assert not un.successor_links(master, spans, first_row, overlap_days=20).set_index("successor_id").loc[
        "rcm_new", "continuing"]
    newness = un.new_listing_evidence(spans, master, sessions, links=links.reset_index(),
                                      first_row=first_row).set_index("security_id")
    assert newness.loc["iac_new", "new_listing_basis"] == "ipo_rule" and newness.loc["iac_new", "series_starts_listing"]
    assert newness.loc["iac_new", "predecessor_security_id"] == ""
    assert newness.loc["rcm_new", "new_listing_basis"] == "successor_link"


def test_young_summary_counts_canonical_weeks_by_basis_link_and_proxy_above_and_the_weeks_it_leaves_missing():
    sessions = pd.bdate_range("2020-01-01", periods=80)
    weeks = pd.DatetimeIndex(pd.Series(sessions, index=sessions).groupby(sessions.to_period("W-SUN")).max().values)
    week_pos = sessions.get_indexer(weeks)
    w = weeks[2]
    rows = [
        # a real IPO, canonical young, its proxy at the band median
        {"security_id": "ipo", "ticker": "IPO", "young": True, "young_rule": "canonical", "new_listing_basis": "ipo_rule",
         "proxy_above": True, "missing": False},
        # KDP-shaped: a close, no dv50, 10 days after its first row: no longer young, missing and blocking
        {"security_id": "kdp", "ticker": "KDP", "young": False, "young_rule": "", "new_listing_basis": "older_issuer",
         "proxy_above": True, "missing": True, "evidence": "proxy"},
        # Alphabet-shaped without the windows run across: continuing, missing
        {"security_id": "googl", "ticker": "GOOGL", "young": False, "young_rule": "", "predecessor_security_id": "goog",
         "new_listing_basis": "successor_link", "proxy_above": True, "missing": True, "evidence": "proxy"},
        # an old name with a dv50: not counted anywhere
        {"security_id": "old", "ticker": "OLD", "young": False, "young_rule": "", "new_listing_basis": "no_earlier_snapshot",
         "proxy_above": False, "missing": False, "dv50": 5e7, "first_row": "2011-06-01"}]
    base = {"week_index": 2, "week_end": w, "eligible": True, "close": 20.0, "dv50": np.nan, "pf_dv50": np.nan,
            "first_row": (w - pd.Timedelta(days=10)).strftime("%Y-%m-%d"), "predecessor_security_id": "",
            "listing_start": sessions[0].strftime("%Y-%m-%d"), "first_listing_run": True, "pf_ge_cut250": False,
            "pf_dv_ok": False, "pf_price_low": False, "evidence": ""}
    listed = pd.DataFrame([{**base, **r} for r in rows])
    listed["segment_first_row"] = listed["first_row"]
    out = un.young_summary(listed, sessions, week_pos)
    assert out["canonical_rule"]["by_basis"] == {"ipo_rule": 1} and out["canonical_rule"]["proxy_above_name_weeks"] == 1
    assert out["canonical_rule"]["with_predecessor_link"] == 0 and out["with_predecessor_link"]["name_weeks"] == 0
    left = out["canonical_rule_not_applied"]
    assert left["by_basis"] == {"older_issuer": 1, "successor_link": 1} and left["with_predecessor_link"] == 1
    assert left["missing_name_weeks"] == 2 and left["missing_blocking_name_weeks"] == 2
    assert left["proxy_above_by_basis"] == {"older_issuer": 1, "successor_link": 1}
    assert left["blocking_securities"] == {"googl:GOOGL": 1, "kdp:KDP": 1}


def test_successor_link_summary_reports_each_link_and_its_first_weeks():
    links = pd.DataFrame([{"predecessor_id": "goog", "successor_id": "googl", "successor_date": "2015-10-02",
                           "successor_start": "2015-10-09", "successor_first_row": "2015-10-09",
                           "predecessor_end": "2015-10-08", "continuing": True, "continuing_basis": "successor_date",
                           "one_to_one": True, "one_to_one_basis": "step 11: stock_merger / reorganization / 1 share",
                           "windows_cross": True, "handover": "2015-10-09"}], columns=un.LINK_COLUMNS)
    weeks = pd.to_datetime(["2015-10-09", "2015-10-16"])
    listed = pd.DataFrame({"security_id": "googl", "ticker": "GOOGL", "week_index": [0, 1], "week_end": weeks,
                           "eligible": True, "dv50_rank_any_price": [6.0, 7.0], "dv50_rank": [6.0, 7.0],
                           "missing": False, "young": False})
    windows = {"googl": {"predecessor": "goog", "handover": "2015-10-09", "predecessor_rows_in_first_window": 45}}
    out = un.successor_link_summary(listed, links, windows)
    assert out["links"] == 1 and out["windows_cross_with_predecessor_rows"] == 1
    entry = out["by_link"][0]
    assert entry["first_5_weeks"] == {"base": 2, "ranked_dv50": 2, "best_dv50_rank": 6, "missing": 0,
                                      "missing_blocking": 0, "young": 0}
    assert entry["predecessor_rows_in_first_window"] == 45 and entry["ticker"] == "GOOGL"
    assert un.successor_links(pd.DataFrame({"security_id": ["a"]}), pd.DataFrame(columns=["security_id"]), {}).empty


def test_check_6_reports_the_market_cap_first_binning_beside_the_either_one_count():
    # INO-shaped float-only week: binned by its float (p 0.6) rather than by a stale market cap (p 0.01).
    settled = pd.DataFrame({"missing_reason": ["unfillable", "not_candidate", "not_candidate"],
                            "evidence": ["proxy", "proxy", "unknown"], "p_top250": [0.6, 0.4, np.nan],
                            "p_top250_mcap_first": [0.01, 0.4, np.nan], "proxy_above_float_only": [True, False, False]})
    six = un.check_6(settled, settled, slots=50)
    alt = six["market_cap_first_binning"]
    assert six["residual_share"] == 0.02 and alt["residual_share"] == 0.0082
    assert alt["residual_name_weeks_difference"] == 0.59 and alt["float_only_name_weeks"] == 1
    assert alt["float_only_est_top250"] == {"either_one_binning": 0.6, "market_cap_first_binning": 0.01}
    assert alt["unfillable_only_share"] == 0.0002 and alt["upper_share"] == 0.0282
    table = un.completeness_table({2012: {"weeks": 1, "complete_250_share": 1.0, "complete_250_strict_share": 1.0,
                                          "complete_250_after_pending_share": 1.0,
                                          "complete_250_after_pending_ex_sibling_classes_share": 1.0,
                                          "top250_close_in_week_share": 1.0, "check_6": six}})
    assert table["residual_mcap_first"].tolist() == [0.0082]


# ------------------------------------------------------------------ built outputs

# REVERSAL_UNIVERSE_BUILD=DIR checks a build written with --out-dir DIR instead of the published files; a
# build by another code version is skipped (its columns may differ from this code's).
_BUILD_DIR = os.environ.get("REVERSAL_UNIVERSE_BUILD", "")
BUILT_TOP = Path(_BUILD_DIR) / un.TOP300_FILE.name if _BUILD_DIR else un.TOP300_FILE
BUILT_SUMMARY = Path(_BUILD_DIR) / un.SUMMARY_FILE.name if _BUILD_DIR else un.SUMMARY_FILE
BUILT_JSON = (Path(_BUILD_DIR) if _BUILD_DIR else un.OUT) / "universe_summary.json"


def _built_version() -> str:
    try:
        return json.loads(BUILT_JSON.read_text()).get("code_version", "")
    except (OSError, ValueError):
        return ""


_STALE = _built_version() != un.CODE_VERSION
_STALE_REASON = f"universe built by code version {_built_version() or '?'}, not {un.CODE_VERSION}"


@pytest.mark.skipif(not BUILT_TOP.exists(), reason="universe not built")
@pytest.mark.skipif(_STALE, reason=_STALE_REASON)
def test_built_top300_has_ranks_only_and_full_ranks_each_week():
    top = pd.read_csv(BUILT_TOP, dtype={"security_id": str, "cik": str, "ff49": str})
    assert list(top.columns) == un.TOP300_COLUMNS
    assert top["week_end"].nunique() == 759
    # A row is kept when any of its four ranks is <= 300, so dv50 ranks 1-300 are all there each week.
    for column in ("dv50_rank", "dv20_rank"):
        ranked = top[top[column] <= un.PRICE_RANK]
        per_week = ranked.groupby("week_end")[column]
        assert (per_week.nunique() == un.PRICE_RANK).all() and (per_week.max() == un.PRICE_RANK).all()
    assert set(top.loc[top["dv50_rank"].notna(), "price_ge_10"]) == {"Y"}
    assert (top[["dv50_rank", "dv20_rank", "dv50_rank_any_price", "dv20_rank_any_price"]] <= un.PRICE_RANK).any(axis=1).all()
    assert not top.duplicated(["week_end", "security_id"]).any()
    assert set(top["earnings_event_within_3_sessions"]) <= {"Y", "N"}


@pytest.mark.skipif(not BUILT_SUMMARY.exists(), reason="universe not built")
@pytest.mark.skipif(_STALE, reason=_STALE_REASON)
def test_built_summary_has_every_week_and_no_vendor_values():
    summary = pd.read_csv(BUILT_SUMMARY)
    assert list(summary.columns) == un.SUMMARY_COLUMNS and len(summary) == 759
    assert (summary["top250_close_in_week"] <= 250).all()
    assert (summary["n_ranked_dv50"] >= 250).all()
    assert set(summary["complete_250"]) <= {"Y", "N"}
    # No week is complete while a missing name is unknown or has only a proxy at the band median.
    complete = summary["complete_250"] == "Y"
    assert (summary.loc[complete, ["n_missing_unknown", "n_missing_proxy_above_no_pf_dv",
                                   "n_missing_pf_dv_ge_cut250"]] == 0).all().all()
    after = summary["complete_250_after_pending"] == "Y"
    assert (summary.loc[after, ["n_missing_unknown_not_pending", "n_missing_proxy_above_no_pf_dv_not_pending",
                                "n_missing_pf_dv_ge_cut250_not_pending"]] == 0).all().all()
    assert ((summary["complete_250_strict"] == "Y") <= complete).all()
    assert (summary["est_missing_top250_residual_upper"] >= summary["est_missing_top250_residual"]).all()
