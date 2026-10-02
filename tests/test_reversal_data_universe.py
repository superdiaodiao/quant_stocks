"""Tests for plan step 12, the weekly universe and its completeness checks (scripts/reversal_data_universe.py)."""
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


def test_proxy_above_uses_market_cap_first_and_float_without_one():
    frame = pd.DataFrame({"week_index": [0, 0, 0, 0], "mcap": [5e9, 1e9, np.nan, np.nan],
                          "float_usd": [np.nan, 9e9, 3e9, np.nan]})
    cut = pd.DataFrame({"cut_mcap": [2e9], "cut_float": [2e9]}, index=pd.Index([0], name="week_index"))
    assert un.proxy_above(frame, cut).tolist() == [True, False, True, False]


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
        # b: first row 2020-01-02, a close but no 25-row median yet: a new listing, not missing.
        {"security_id": "b", "week_index": 1, "first_row": "2020-01-02", "last_row": "2020-12-31", "close": 15.0,
         "price_ge_10": "Y"},
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


def test_the_script_never_reads_the_return_column():
    source = Path(un.__file__).read_text()
    assert '"tr"' not in source.replace('"tr", "mcap"', "")  # only the banned-column list names it
    assert "usecols=[\"security_id\", \"date\", \"close_raw\", \"volume_raw\", \"src_primary\"]" in source


# ------------------------------------------------------------------ built outputs

BUILT_TOP = un.TOP300_FILE
BUILT_SUMMARY = un.SUMMARY_FILE


@pytest.mark.skipif(not BUILT_TOP.exists(), reason="universe not built")
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
