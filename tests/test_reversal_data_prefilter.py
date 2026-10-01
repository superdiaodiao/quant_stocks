"""Tests for plan step 6, the liquidity pre-filter (scripts/reversal_data_prefilter.py)."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts import reversal_data_prefilter as pf


def _spans(rows):
    return pd.DataFrame(rows, columns=["security_id", "ticker", "list_start", "list_end"])


# ------------------------------------------------------------------ calendar and listing

def test_week_end_is_the_last_session_of_the_calendar_week():
    sessions = pf.xnas_sessions("2012-03-26", "2012-04-20")
    weeks = pf.week_ends(sessions, "2012-03-26", "2012-04-20")
    # Good Friday 2012-04-06 was a holiday, so that week ends on Thursday.
    assert [d.strftime("%Y-%m-%d") for d in weeks] == ["2012-03-30", "2012-04-05", "2012-04-13", "2012-04-20"]


def test_full_window_has_759_week_ends():
    weeks = pf.week_ends(pf.xnas_sessions())
    assert len(weeks) == 759
    assert weeks[0] == pd.Timestamp(pf.FIRST_WEEK) and weeks[-1] == pd.Timestamp(pf.LAST_WEEK)


def test_listing_span_ends_before_the_next_absent_snapshot_or_at_the_delisting():
    intervals = pd.DataFrame([
        {"security_id": "1", "ticker": "AAA", "start": "2012-01-25", "end": "2014-03-04", "end_next_absent": "2014-06-05",
         "start_prev_absent": "2011-11-26", "name_in_source": "A Inc - Common Stock", "share_class": "COMMON"},
        {"security_id": "2", "ticker": "BBB", "start": "2012-01-25", "end": "2026-07-01", "end_next_absent": "",
         "start_prev_absent": "", "name_in_source": "B Inc - Common Stock", "share_class": "COMMON"},
        {"security_id": "3", "ticker": "CCC", "start": "2012-01-25", "end": "2013-05-26", "end_next_absent": "2013-08-03",
         "start_prev_absent": "", "name_in_source": "C Inc", "share_class": "COMMON"},
    ])
    master = pd.DataFrame({"security_id": ["1", "2", "3"], "delist_date": ["", "", "2013-06-10"]})
    spans = pf.listing_spans(intervals, master).set_index("security_id")
    assert spans.loc["1", "list_end"] == "2014-06-04"
    assert spans.loc["2", "list_end"] == pf.WINDOW_END
    assert spans.loc["3", "list_end"] == "2013-06-10"


def test_spac_names_are_not_common_but_adrs_are_left_to_the_foreign_flag():
    assert pf.non_common_interval("Silver Run Acquisition Corporation II - Class A Common Stock", "A")
    assert not pf.non_common_interval("Shire plc - American Depositary Shares, each representing three Ordinary Shares",
                                      "ADS")
    assert not pf.non_common_interval("Apple Inc. - Common Stock", "COMMON")


def test_mixed_filer_is_foreign_only_while_its_latest_report_is_foreign():
    timeline = [("2012-04-03", "F"), ("2019-05-01", "F"), ("2020-04-07", "D"), ("2021-01-05", "D")]
    spans = pf.foreign_spans_from_timeline(timeline)
    assert spans == [("1900-01-01", "2020-04-06")]
    assert pf.is_foreign_on(spans, "2015-01-02") and not pf.is_foreign_on(spans, "2020-06-01")
    back = pf.foreign_spans_from_timeline([("2012-01-01", "D"), ("2018-03-01", "F")])
    assert back == [("2018-03-01", "2100-01-01")]


def test_periodic_timeline_ignores_6k_beside_quarterly_reports():
    payload = {"filings": {"recent": {"form": ["10-Q", "6-K", "10-K", "20-F/A"],
                                      "filingDate": ["2020-05-01", "2020-06-01", "2021-02-01", "2015-03-01"]}}}
    assert pf.periodic_timeline(payload) == [("2015-03-01", "F"), ("2020-05-01", "D"), ("2021-02-01", "D")]


# ------------------------------------------------------------------ ticker map

def test_ticker_map_assigns_direct_continuity_and_drops_other_companies():
    spans = _spans([("S1", "OLD", "2011-01-01", "2014-12-31"), ("S1", "NEW", "2015-01-01", "2026-08-31"),
                    ("S2", "XYZ", "2016-01-01", "2026-08-31")])
    tmap = pf.TickerMap(spans)
    owner, direct = tmap.assign("NEW", ["2013-06-03", "2016-06-01", "2010-06-01"])
    # 2013: the vendor keeps the renamed company's history under NEW (continuity, not direct).
    assert list(owner) == ["S1", "S1", ""] and list(direct) == [False, True, False]
    owner, direct = tmap.assign("XYZ", ["2013-06-03", "2017-01-03"])
    # Before S2 listed, XYZ belonged to somebody else (another exchange): dropped.
    assert list(owner) == ["", "S2"]


def test_best_rows_prefer_vendor_and_direct_rows():
    rows = pd.DataFrame({
        "security_id": ["S", "S", "S", "S"], "date": pd.to_datetime(["2015-01-02"] * 3 + ["2015-01-05"]),
        "close": [10.0, 11.0, 12.0, 13.0], "volume": [100.0] * 4, "src": ["stored", "wiki", "wiki", "stored"],
        "direct": [True, False, True, True], "ticker": ["A"] * 4, "file": ["s", "w1", "w2", "s"]})
    best = pf.best_rows(rows).set_index("date")
    assert best.loc["2015-01-02", "file"] == "w2" and best.loc["2015-01-02", "n_src"] == 2
    assert best.loc["2015-01-05", "src"] == "stored" and not best.loc["2015-01-05", "vendor"]
    assert best.loc["2015-01-02", "dv"] == 1200.0


def test_yahoo_split_restore_multiplies_by_later_splits():
    stamps = pd.to_datetime(["2020-08-28", "2020-08-31", "2020-09-01"])
    splits = {"x": {"date": int(pd.Timestamp("2020-08-31").timestamp()), "numerator": 4, "denominator": 1}}
    assert list(pf.yahoo_split_factor(stamps, splits)) == [4.0, 1.0, 1.0]


# ------------------------------------------------------------------ flags and ranks

def test_price_flag_marks_stored_closes_under_10_unknown():
    close = np.array([12.0, 8.0, 12.0, 8.0, np.nan])
    src = np.array([0, 0, 3, 3, -1])
    assert list(pf.price_flag(close, src)) == ["Y", "N", "Y", "U", ""]


def test_dv_ranks_are_within_week_and_skip_ineligible_rows():
    frame = pd.DataFrame({"week_end": ["w1", "w1", "w1", "w2"], "dv50": [5.0, 9.0, 7.0, 1.0]})
    ranks = pf.rank_within_weeks(frame, "dv50", pd.Series([True, True, False, True]))
    assert ranks.tolist()[:2] == [2.0, 1.0] and np.isnan(ranks.iloc[2]) and ranks.iloc[3] == 1.0


def test_float_tiers_need_a_clean_float():
    assert pf.float_tier(1.2e9, "ok") == "B_A"
    assert pf.float_tier(7e8, "no_shares") == "B_B"
    assert pf.float_tier(4e8, "review") == "B_C"
    assert pf.float_tier(2e8, "ok") == ""
    assert pf.float_tier(2e12, "float_above_5T") == ""
    assert pf.float_tier(np.nan, "ok") == ""


def test_seeded_sample_is_fixed_and_sorted():
    ids = [str(k) for k in range(100)]
    first = pf.seeded_sample(ids, 20)
    assert first == pf.seeded_sample(list(reversed(ids)), 20)
    assert len(first) == 20 and first == sorted(first)
    assert pf.seeded_sample(ids[:5], 20) == sorted(ids[:5])


def test_ordinary_and_odd_split_ratios():
    assert pf._ordinary_ratio(7.0) and pf._ordinary_ratio(1.5) and pf._ordinary_ratio(0.1)
    assert not pf._ordinary_ratio(1.061) and not pf._ordinary_ratio(0.9535) and not pf._ordinary_ratio(1.155)


# ------------------------------------------------------------------ Tiingo range match

INDEX = {
    "OLDCO": [{"exchange": "NASDAQ", "start": "2004-01-02", "end": "2019-05-10"}],
    "LATE": [{"exchange": "NASDAQ", "start": "2016-01-04", "end": "2017-06-30"}],
    "REUSE": [{"exchange": "NYSE", "start": "2021-03-01", "end": "2026-09-30"}],
    "BOTH": [{"exchange": "NASDAQ", "start": "2005-01-03", "end": "2014-02-10"},
             {"exchange": "NASDAQ", "start": "2023-01-03", "end": "2026-09-30"}],
}


def test_tiingo_range_match_full_partial_wrong_entity_and_missing():
    assert pf.tiingo_range_match(["OLDCO"], "2018-03-28", "2019-05-20", INDEX)["match"] == "Y"
    partial = pf.tiingo_range_match(["LATE"], "2011-06-01", "2017-07-10", INDEX)
    assert partial["match"] == "partial" and 0 < partial["coverage"] < 1
    assert pf.tiingo_range_match(["REUSE"], "2012-01-01", "2015-01-01", INDEX)["match"] == "wrong_entity"
    assert pf.tiingo_range_match(["NOPE"], "2012-01-01", "2015-01-01", INDEX)["match"] == "no_row"


def test_tiingo_range_match_tries_renamed_tickers_and_flags_reuse():
    match = pf.tiingo_range_match(["REUSE", "OLDCO"], "2012-01-01", "2019-05-01", INDEX)
    assert match["match"] == "Y" and match["ticker"] == "OLDCO"
    reused = pf.tiingo_range_match(["BOTH"], "2011-06-01", "2014-02-20", INDEX)
    assert reused["match"] == "Y" and reused["reused"]


# ------------------------------------------------------------------ windows and budget

def test_needed_window_runs_to_the_listing_end_when_the_gap_does():
    row = pd.Series({"first_listed": "2010-12-31", "first_uncovered": pd.Timestamp("2018-03-30"),
                     "last_uncovered": pd.Timestamp("2019-05-03"), "last_universe_week": pd.Timestamp("2019-05-03"),
                     "last_listed": "2019-05-14", "transfer_date": ""})
    assert pf.needed_window(row) == ("2018-01-14", "2019-05-14")
    row["transfer_date"] = "2019-05-14"
    assert pf.needed_window(row)[1] == "2019-06-11"
    row["last_universe_week"] = pd.Timestamp("2020-01-03")
    assert pf.needed_window(row)[1] == "2019-05-03"


def test_late_start_window():
    row = pd.Series({"first_price": pd.Timestamp("2016-09-01"), "first_listed": "2010-12-31"})
    assert pf.late_start(row) == ("2011-06-01", "2016-08-31")
    row["first_price"] = pd.Timestamp("2011-07-01")
    assert pf.late_start(row) is None


def test_budget_defers_lowest_priority_symbols():
    frame = pd.DataFrame({"planned_source": ["tiingo"] * 4 + ["yahoo"], "fetch_month": [pf.MONTH_1] * 5,
                          "priority": [1, 2, 3, 3, 9], "security_id": ["a", "b", "c", "d", "e"],
                          "ticker_for_source": ["A", "B", "C", "A", "E"], "status": ["pending"] * 5})
    out, budget = pf.apply_budget(frame, used=0, reserve=0, monthly=2)
    assert out["status"].tolist() == ["pending", "pending", "deferred_quota", "pending", "pending"]
    assert budget["month1_symbols"] == 2 and budget["deferred_quota_symbols"] == 1


# ------------------------------------------------------------------ built outputs

BUILT = [pf.CANDIDATES, pf.UNFILLABLE, pf.OUT / "prefilter_summary.json", pf.OUT / "weekly_coverage.csv"]


@pytest.mark.skipif(not all(Path(p).exists() for p in BUILT), reason="step 6 outputs not built")
def test_built_candidate_list_follows_the_plan():
    candidates = pd.read_csv(pf.CANDIDATES, dtype=str, keep_default_na=False)
    master = pd.read_csv(pf.MASTER, dtype=str, keep_default_na=False).set_index("security_id")
    assert list(candidates.columns) == pf.CANDIDATE_COLUMNS
    assert set(candidates["planned_source"]) <= {"wiki", "yahoo", "tiingo", "unfillable"}
    assert not candidates.duplicated(["security_id", "reason"]).any()
    # Foreign filers are never ranked or fetched (MIXED ones only outside their foreign years).
    assert not candidates["security_id"].map(master["foreign_filer"]).eq("Y").any()
    # Tiingo is planned only on a supported_tickers range match; active names go to Yahoo.
    tiingo = candidates[candidates["planned_source"] == "tiingo"]
    assert tiingo["tiingo_range_match"].isin(["Y", "partial"]).all()
    fetch = candidates[candidates["reason"] != "V_verify_sample"]
    assert (fetch.loc[fetch["active"] == "Y", "planned_source"] == "yahoo").all()
    assert (fetch.loc[fetch["planned_source"] == "unfillable", "status"].isin(["no_data", "wrong_entity"])).all()
    # The seeded tier-C sample has 20 names and V has 50.
    assert (candidates["reason"] == "B_C_sample_300M_500M").sum() == pf.TIER_C_SAMPLE
    assert (candidates["reason"] == "V_verify_sample").sum() == pf.V_SAMPLE
    month1 = tiingo[tiingo["fetch_month"] == pf.MONTH_1]["ticker_for_source"].nunique()
    assert month1 <= pf.TIINGO_MONTHLY_SYMBOLS
    # Committed files carry ranks and SEC floats only, never vendor price levels.
    for column in ("close", "dv50", "dv20", "market_cap", "last_sale"):
        assert column not in candidates.columns
    assert all(c in pf.UNFILLABLE_COLUMNS for c in pd.read_csv(pf.UNFILLABLE, nrows=0).columns)


@pytest.mark.skipif(not all(Path(p).exists() for p in BUILT), reason="step 6 outputs not built")
def test_built_coverage_has_every_week_and_ranks_300_names():
    coverage = pd.read_csv(pf.OUT / "weekly_coverage.csv")
    assert len(coverage) == 759
    assert (coverage["top300_vendor_raw"] <= 300).all()
    assert (coverage["top300_vendor_raw"] + coverage["top300_stored_only"] <= 300).all()
    assert (coverage["top300_vendor_after_plan"] >= coverage["top300_vendor_raw"]).all()
