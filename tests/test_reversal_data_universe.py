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
    assert reasons == {"a": "tiingo_pending", "b": "series_gap", "c": "unfillable", "d": "unfillable",
                       "e": "no_vendor_source", "f": "candidate_other", "g": "not_candidate"}


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
    missing = pd.DataFrame({"week_end": pd.to_datetime(["2015-01-02"] * 4),
                            "dv_ratio": [1.1, np.nan, np.nan, 2.0], "proxy_ratio": [0.1, 3.0, np.nan, 3.0],
                            "pf_price_low": [False, False, False, True]})
    assert un.expected_top250(missing, rates).tolist() == [0.5, 0.2, 0.01, 0.0]


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
