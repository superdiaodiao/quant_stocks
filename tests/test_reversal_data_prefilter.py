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


def test_a_later_listing_after_a_form25_cut_is_kept_whole():
    # SMCI: Form 25 effective 2019-03-22, back on Nasdaq from 2020-02-25 (round 5: cut to one day).
    intervals = pd.DataFrame([
        {"security_id": "1375365", "ticker": "SMCI", "start": "2010-12-31", "end": "2018-08-22",
         "end_next_absent": "2018-09-07", "start_prev_absent": "", "name_in_source": "Super Micro - Common Stock",
         "share_class": "COMMON"},
        {"security_id": "1375365", "ticker": "SMCI", "start": "2020-02-25", "end": "2026-08-01", "end_next_absent": "",
         "start_prev_absent": "2019-12-27", "name_in_source": "Super Micro Common Stock", "share_class": "COMMON"},
        {"security_id": "9", "ticker": "NINE", "start": "2012-01-25", "end": "2026-07-01", "end_next_absent": "",
         "start_prev_absent": "", "name_in_source": "Nine Inc - Common Stock", "share_class": "COMMON"},
    ])
    master = pd.DataFrame({"security_id": ["1375365", "9"], "delist_date": ["2019-03-22", "2015-05-01"]})
    spans = pf.listing_spans(intervals, master)
    smci = spans[spans["security_id"] == "1375365"].sort_values("list_start")
    assert smci["list_end"].tolist() == ["2018-09-06", pf.WINDOW_END]
    assert smci["after_cut"].tolist() == [False, True]
    # a delisting inside an interval still cuts it
    nine = spans[spans["security_id"] == "9"].iloc[0]
    assert nine["list_end"] == "2015-05-01" and not nine["after_cut"]


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


def test_mixed_filer_spans_come_from_the_periodic_form_history(tmp_path):
    # TEVA: 20-Fs, then 6-Ks only, then its first 10-K on 2018-02-12 (the recent block alone missed
    # the foreign years); a second CIK switches back and forth.
    history = pd.DataFrame([
        ("818686", "2011-02-15", "20-F", "F"), ("818686", "2017-09-19", "6-K", "F"),
        ("818686", "2018-02-12", "10-K", "D"), ("818686", "2018-05-01", "10-Q", "D"),
        ("2", "2012-03-01", "10-K", "D"), ("2", "2015-04-01", "20-F", "F"), ("2", "2015-04-01", "6-K", "F"),
        ("2", "2019-03-01", "10-K", "D")], columns=["cik", "filing_date", "form", "regime_in_force"])
    history.to_csv(tmp_path / "h.csv", index=False)
    master = pd.DataFrame({"security_id": ["818686", "2", "3", "4", "5"], "cik": ["818686", "2", "3", "4", "5"],
                           "foreign_filer": ["MIXED", "MIXED", "Y", "N", "MIXED"],
                           "foreign_spans": ["2011-06-01..2018-02-11", "", "", "", "2014-01-01..2015-12-31"]})
    spans, facts = pf.foreign_spans(master, tmp_path / "h.csv")
    assert spans["818686"] == [("1900-01-01", "2018-02-11")]
    assert pf.is_foreign_on(spans["818686"], "2012-01-06") and not pf.is_foreign_on(spans["818686"], "2018-02-16")
    assert spans["2"] == [("2015-04-01", "2019-02-28")]
    assert spans["3"] == pf.ALWAYS and "4" not in spans
    assert spans["5"] == [("2014-01-01", "2015-12-31")]  # not in the history: security_master's spans
    assert facts["mixed_from_history"] == 2 and facts["mixed_from_master_spans"] == 1
    assert facts["history_vs_master_spans_differ"] == ["2"]


def test_needed_window_stops_four_weeks_after_the_last_domestic_week():
    row = pd.Series({"first_listed": "2010-12-31", "first_uncovered": pd.Timestamp("2018-03-30"),
                     "last_uncovered": pd.Timestamp("2018-02-23"), "last_universe_week": pd.Timestamp("2018-02-23"),
                     "last_listed": "2021-04-26", "transfer_date": "", "ends_foreign": True})
    assert pf.needed_window(row)[1] == "2018-03-23"
    row["ends_foreign"] = False
    assert pf.needed_window(row)[1] == "2021-04-26"


# ------------------------------------------------------------------ SPAC shells

def test_unmerged_spac_shells_are_found_from_sic_names_and_tickers(tmp_path):
    master = pd.DataFrame([
        # an unmerged shell with an operating-sounding name: SIC 6770, one ticker, delisted
        ("1", "Sentinel Energy Services Inc.", "6770", "", "2019-11-17", "2019-11-06"),
        # a merged SPAC: renamed while listed and a new ticker (JetPay)
        ("2", "JetPay Corp", "6770", "Universal Business Payment Solutions Acquisition Corp (2010-12-22..2013-08-02)",
         "2018-12-16", "2018-11-21"),
        # a SPAC renaming itself keeps its ticker: still a shell
        ("3", "SVF Investment Corp.", "6770", "Gazelle Opportunities I (Cayman) Corp (2020-11-16..2020-11-16)",
         "2023-02-05", "2023-01-23"),
        # an active SPAC by name; an active de-SPAC with a stale SIC and a plain name stays common
        ("4", "Cantor Equity Partners II, Inc.", "6770", "", "", "2026-08-31"),
        ("5", "Innventure, Inc.", "6770", "Learn SPAC HoldCo, Inc. (2024-01-26..2024-10-02)", "", "2026-08-31"),
        # a merger sub's name with an operating SIC is no SPAC
        ("6", "GRIZZLY MERGER SUB 1, LLC", "4841", "", "2020-12-20", "2020-12-20"),
        # SIC 6770 but an operating SIC appeared later in sic_history, two tickers
        ("7", "Some Operating Co", "6770", "", "2022-01-01", "2021-12-01"),
    ], columns=["security_id", "name", "sic", "former_names", "delist_date", "last_listed"])
    master["cik"] = master["security_id"]
    intervals = pd.DataFrame([("1", "STNL", "Sentinel Energy Services Inc. - Class A Ordinary Shares"),
                              ("2", "UBPS", "Universal Business Payment Solutions"), ("2", "JTPY", "JetPay Corp"),
                              ("3", "SVFA", "SVF Investment Corp - Class A"), ("4", "CEPT", "Cantor Equity Partners II"),
                              ("5", "INV", "Innventure, Inc. - Common Stock"), ("6", "GLIBA", "GCI Liberty"),
                              ("7", "OLDX", "Old"), ("7", "NEWX", "New")], columns=["security_id", "ticker", "name_in_source"])
    sic = pd.DataFrame({"cik": ["7"], "operating_sic_after_6770": ["3711"]})
    sic.to_csv(tmp_path / "sic.csv", index=False)
    shells = pf.spac_shells(master, intervals, tmp_path / "sic.csv")
    assert set(shells) == {"1", "3", "4"}
    assert shells["1"] == "sic_6770_never_operating_one_ticker" and shells["4"] == "sic_6770_spac_name"
    spans = pd.DataFrame({"security_id": ["1", "5"], "ticker": ["STNL", "INV"], "list_start": ["2018-01-02"] * 2,
                          "list_end": ["2018-12-31"] * 2, "name": ["Sentinel Energy Services Inc.", "Innventure"],
                          "share_class": ["A", "COMMON"]})
    weekly = pf.listed_weeks(spans, master, {}, pf.week_ends(pf.xnas_sessions("2018-01-01", "2018-12-31"),
                                                              "2018-01-01", "2018-12-31"), shells)
    assert weekly.groupby("security_id")["non_common"].all().to_dict() == {"1": True, "5": False}


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


def test_tiingo_range_match_tries_renamed_tickers_and_a_hidden_row_is_not_a_match():
    match = pf.tiingo_range_match(["REUSE", "OLDCO"], "2012-01-01", "2019-05-01", INDEX)
    assert match["match"] == "Y" and match["ticker"] == "OLDCO"
    # The API answers BOTH with its later row, so the row that covers the need is hidden.
    hidden = pf.tiingo_range_match(["BOTH"], "2011-06-01", "2014-02-20", INDEX)
    assert hidden["match"] == "hidden" and hidden["hidden_by"]["start"] == "2023-01-03"
    assert not pf.useful_partial(hidden, "2011-06-01")


def supported(rows):
    frame = pd.DataFrame(rows, columns=["ticker", "exchange", "assetType", "priceCurrency", "startDate", "endDate"])
    return pf.tiingo_index(frame)


def security(first="2010-12-31", last="2020-05-31", active=False, starts=None, transfer=""):
    return {"first_listed": first, "last_listed": last, "active": active, "transfer_date": transfer,
            "ticker_starts": starts or {}}


def test_the_served_row_is_the_one_that_ends_latest():
    index = supported([("CA", "NASDAQ", "ETF", "USD", "2023-12-14", "2026-09-30"),
                       ("CA", "NASDAQ", "Stock", "USD", "1984-09-07", "2018-11-06"),
                       ("CZR", "NASDAQ", "Stock", "USD", "2012-02-08", "2020-07-20"),
                       ("CZR", "NASDAQ", "Stock", "USD", "2014-09-22", "2026-09-30")])
    assert pf.served_row(index["CA"])["asset_type"] == "ETF"
    assert pf.served_row(index["CZR"])["start"] == "2014-09-22"
    assert [r["served"] for r in index["CZR"]] == [False, True]


def test_q_suffix_alias_and_sec_tickers_are_tried_for_a_bankrupt_or_renamed_company():
    index = supported([("CLVS", "NASDAQ", "Stock", "USD", "2023-01-03", "2023-01-03"),
                       ("CLVSQ", "PINK", "Stock", "USD", "2011-11-16", "2023-07-25"),
                       ("SDCCQ", "PINK", "Stock", "USD", "2019-09-12", "2026-09-30"),
                       ("TFCFA", "NASDAQ", "Stock", "USD", "1996-03-11", "2019-03-20")])
    row = pd.Series({"tickers": "CLVS", "ticker_last_held": "CLVS:2023-01-08", "tickers_sec_current": ""})
    candidates = pf.tiingo_tickers(row, "2018-01-21", "x", pf.q_suffix_map(index))
    assert candidates == [("CLVS", "own"), ("CLVSQ", "q_suffix")]
    match = pf.tiingo_range_match(candidates, "2018-01-21", "2023-01-08", index, security(first="2011-11-16"))
    assert match["match"] == "Y" and match["ticker"] == "CLVSQ" and match["kind"] == "q_suffix"
    # own ticker + one letter + Q, only when the row starts with the security (SmileDirectClub)
    row = pd.Series({"tickers": "SDC", "ticker_last_held": "SDC:2023-10-26", "tickers_sec_current": ""})
    candidates = pf.tiingo_tickers(row, "2019-09-13", "x", pf.q_suffix_map(index))
    assert ("SDCCQ", "q_prefix") in candidates
    assert pf.tiingo_range_match(candidates, "2019-09-13", "2023-10-26", index, security(first="2019-09-13"))["ticker"] == "SDCCQ"
    assert pf.tiingo_range_match(candidates, "2019-09-13", "2023-10-26", index,
                                 security(first="2016-01-04"))["match"] != "Y"
    # the reviewed alias: 21st Century Fox's class A history is under TFCFA
    row = pd.Series({"tickers": "FOXA", "ticker_last_held": "FOXA:2019-05-05", "tickers_sec_current": ""})
    candidates = pf.tiingo_tickers(row, "2018-01-21", "1308161.A")
    assert ("TFCFA", "reviewed_alias") in candidates
    match = pf.tiingo_range_match(candidates, "2018-01-21", "2019-03-25", index, security())
    assert match["match"] == "Y" and match["ticker"] == "TFCFA"


def test_a_late_row_inside_the_need_or_a_hidden_own_row_marks_another_company():
    index = supported([("ACET", "NASDAQ", "Stock", "USD", "2018-01-26", "2026-09-30"),
                       ("ACETQ", "NASDAQ", "Stock", "USD", "1990-03-26", "2019-10-01"),
                       # VIVO: Meridian's own row and a later company's served row
                       ("VIVO", "NASDAQ", "Stock", "USD", "1992-02-26", "2026-03-27"),
                       ("VIVO", "NASDAQ", "Stock", "USD", "2016-12-29", "2026-09-30"),
                       # COHR: old Coherent's row ends at its close, the served row runs on
                       ("COHR", "NYSE", "Stock", "USD", "1990-03-26", "2026-09-30"),
                       ("COHR", "NASDAQ", "Stock", "USD", "1990-03-26", "2022-07-01"),
                       # a history Tiingo starts on 2016-01-04 is the same company
                       ("CUT", "NASDAQ", "Stock", "USD", "2016-01-04", "2018-06-01")])
    aceto = security(starts={"ACET": "2010-12-31"})
    assert pf.tiingo_range_match([("ACET", "own")], "2018-01-21", "2019-05-05", index, aceto)["match"] == "newer_company"
    match = pf.tiingo_range_match([("ACET", "own"), ("ACETQ", "q_suffix")], "2018-01-21", "2019-05-05", index, aceto)
    assert match["ticker"] == "ACETQ" and match["match"] == "Y"
    vivo = pf.tiingo_range_match(["VIVO"], "2018-01-21", "2023-02-07", index,
                                 security(last="2023-02-07", starts={"VIVO": "2010-12-31"}))
    assert vivo["match"] == "hidden" and vivo["row_start"] == "1992-02-26"
    cohr = pf.tiingo_range_match(["COHR"], "2018-01-21", "2022-07-11", index, security(last="2022-07-11"))
    assert cohr["match"] == "hidden" and cohr["row_end"] == "2022-07-01"
    cut = pf.tiingo_range_match(["CUT"], "2016-06-01", "2018-06-01", index, security(last="2018-06-01"))
    assert cut["match"] == "Y" and not cut["starts_late"]


def test_non_stock_rows_count_only_when_they_start_and_end_with_the_security():
    index = supported([("PAND", "NASDAQ", "ETF", "USD", "2020-07-17", "2021-04-01"),
                       ("NVLS", "NASDAQ", "ETF", "USD", "2006-12-28", "2017-07-24")])
    pandion = pf.tiingo_range_match(["PAND"], "2020-07-29", "2021-04-04", index,
                                    security(first="2020-07-29", last="2021-04-04"))
    assert pandion["match"] == "Y" and pandion["asset_type"] == "ETF"
    novellus = pf.tiingo_range_match(["NVLS"], "2011-10-23", "2012-06-14", index, security(last="2012-06-14"))
    assert novellus["match"] == "no_row"


def test_flags_mark_ambiguous_matches_and_shared_tickers():
    index = supported([("RDUS", "NASDAQ", "Stock", "USD", "2014-06-06", "2026-09-30"),
                       ("RDUS", "NASDAQ", "Stock", "USD", "1993-11-16", "2025-07-10")])
    match = pf.tiingo_range_match(["RDUS"], "2014-07-07", "2022-08-25", index,
                                  security(first="2014-06-06", last="2022-08-25"))
    flags = pf.match_flags(match, index, "1428522", "2014-07-07", {"RDUS": {"912603"}})
    assert match["match"] == "Y" and "multi_row" in flags and "sec_holder:912603" in flags
    assert "outlives_listing" in flags
    frame = pd.DataFrame({"security_id": ["a", "b", "c"], "planned_source": ["tiingo", "tiingo", "tiingo"],
                          "ticker_for_source": ["TIVO", "TIVO", "XYZ"], "tiingo_flags": ["", "", ""], "note": ["", "", ""],
                          "tiingo_reused_ticker": ["", "", ""]})
    marked = pf.mark_shared(frame).set_index("security_id")
    assert marked.loc["a", "tiingo_flags"] == "shared:b" and marked.loc["a", "tiingo_reused_ticker"] == "Y"
    assert marked.loc["c", "tiingo_reused_ticker"] == "" and "confirms the entity" in marked.loc["b", "note"]


# ------------------------------------------------------------------ rule Y_active_all

def _fact_rows(rows: dict) -> pd.DataFrame:
    base = {"uncovered_weeks": 0, "delist_date": "", "best_rank_a1_effective": np.nan, "best_rank_a1": np.nan,
            "public_float_usd": np.nan, "float_check_flag": "", "implied_float_per_share": "", "first_price": pd.NaT,
            "first_listed": "2015-01-02", "last_listed": pf.WINDOW_END, "cik": "1", "active": True, "via_successor": "",
            "best_rank_uncovered": np.nan, "uncovered_unpriced_weeks": 0, "listed_now": True,
            "spac_like_now": False}
    return pd.DataFrame([{**base, **r} for r in rows.values()], index=list(rows))


def test_y_active_all_takes_every_uncovered_name_listed_now_whatever_its_rank():
    facts = _fact_rows({
        "quiet": {"uncovered_weeks": 40, "uncovered_unpriced_weeks": 40},       # no dollar volume, no rank
        "liquid": {"uncovered_weeks": 300, "best_rank_uncovered": 12},           # SMCI-like
        "covered": {"uncovered_weeks": 0},                                       # vendor series covers it
        "gone": {"uncovered_weeks": 30, "listed_now": False, "active": False, "last_listed": "2020-05-01"},
        "shell_now": {"uncovered_weeks": 5, "listed_now": False},                # open interval, not common now
        "blank_check": {"uncovered_weeks": 9, "spac_like_now": True},            # Churchill Capital Corp XI
    })
    ranks = pd.DataFrame(columns=["security_id", "snapshot_date", "mcap_rank"])
    floats = pd.DataFrame(columns=["cik", "end", "val"])
    hits = pf.rule_hits(facts, ranks, floats)
    assert hits["quiet"] == {"Y_active_all": ("uncovered_universe_weeks", 40)}
    assert set(hits["liquid"]) == {"Y_active_rank300", "Y_active_all"}
    assert "covered" not in hits and "shell_now" not in hits and "blank_check" not in hits
    assert "Y_active_all" not in hits.get("gone", {})
    # the rule comes last, so a name another rule fetches keeps that rule as its reason
    assert pf.REASON_PRIORITY[-1] == "Y_active_all"
    assert sorted(hits["liquid"], key=pf.REASON_PRIORITY.index)[0] == "Y_active_rank300"


def test_a_relisted_name_keeps_the_delisted_route_before_its_form25_and_yahoo_after():
    # Core Scientific: Form 25 2023-04-22 in its bankruptcy, new equity listed again from 2024-03-28.
    row = pd.Series({"first_listed": "2021-04-05", "first_uncovered": pd.Timestamp("2021-04-09"),
                     "last_uncovered": pd.Timestamp("2026-07-17"), "last_universe_week": pd.Timestamp("2026-07-17"),
                     "last_listed": pf.WINDOW_END, "transfer_date": "", "active": True, "active_nasdaq": True,
                     "via_successor": "", "delist_date": "2023-04-22", "relisted_from": "2024-03-28",
                     "listed_before_cut": "2023-04-22"})
    hits = {"B_C_tier": ("form25_max_float_3y_usd", 4e8), "Y_active_all": ("uncovered_universe_weeks", 200)}
    parts = pf.relisted_parts(row, ["B_C_rest_300M_500M", "Y_active_all"], hits)
    (early, pre, s1, e1, _), (late, post, s2, e2, _) = parts
    assert early == ["B_C_rest_300M_500M"] and not pre.active and pre.last_listed == "2023-04-22"
    assert (s1, e1) == ("2021-04-05", "2023-04-22")
    assert late == ["Y_active_all"] and post.active and (s2, e2) == ("2024-01-13", pf.WINDOW_END)
    # without a rule other than Y, or with a need that starts after the relisting, one row as before
    assert len(pf.relisted_parts(row, ["Y_active_all"], hits)) == 1
    row["first_uncovered"] = pd.Timestamp("2024-06-07")
    assert len(pf.relisted_parts(row, ["B_C_rest_300M_500M", "Y_active_all"], hits)) == 1


def test_the_samples_on_disk_are_kept_and_only_freed_places_are_redrawn():
    ids = [f"s{k:02d}" for k in range(30)]
    order = pf.seeded_order(ids)
    frame = pd.DataFrame({"security_id": ids, "reason": ["B_C_rest_300M_500M"] * 30, "priority": [12] * 30,
                          "reasons_all": ["B_C_rest_300M_500M"] * 30, "planned_source": ["tiingo"] * 30,
                          "status": ["conditional_tier_c"] * 30, "fetch_month": [pf.MONTH_2] * 30})
    keep = set(order[10:15])            # an earlier draw (another pool), one of its names no longer fetchable
    frame.loc[frame["security_id"] == order[10], "planned_source"] = "unfillable"
    out, facts = pf.refill_tier_c_sample(frame, n=5, keep=keep)
    sample = set(out.loc[out["reason"] == "B_C_sample_300M_500M", "security_id"])
    assert sample == set(order[11:15]) | {order[0]} and facts["kept_from_previous"] == 4


def test_yahoo_ticker_of_a_name_listed_now_is_the_open_interval_ticker():
    # Agenus: AGEND (the reverse-split ticker of 2011-10-07) starts after AGEN's open interval does.
    row = pd.Series({"active_nasdaq": True, "open_ticker": "AGEN", "last_ticker": "AGEND", "yahoo_tickers": "AGEN"})
    assert pf.yahoo_ticker(row) == "AGEN"
    moved = pd.Series({"active_nasdaq": False, "open_ticker": "", "last_ticker": "CREE", "yahoo_tickers": "WOLF"})
    assert pf.yahoo_ticker(moved) == "WOLF"


def test_blank_check_shells_listed_now_are_found_without_sic_6770(tmp_path):
    master = pd.DataFrame({"security_id": ["a", "b", "c", "d"], "cik": ["1", "2", "3", "4"],
                           "name": ["Churchill Capital Corp XI", "Dynamix Corp", "Rocket Lab Corp", "Gores Holdings IX"],
                           "sic": ["3569", "6770", "3760", "6770"]})
    intervals = pd.DataFrame({"security_id": ["a", "b", "c", "c", "d"],
                              "name_in_source": ["Churchill Capital Corp XI - Class A", "Dynamix Corporation - Class A",
                                                 "Vector Acquisition Corporation - Class A", "Rocket Lab Corp Common Stock",
                                                 "Gores Holdings IX - Class A"]})
    sic = tmp_path / "sic.csv"
    pd.DataFrame({"cik": ["4"], "operating_sic_after_6770": ["3714"]}).to_csv(sic, index=False)
    # c merged (an operating listed name); d has an operating SIC after 6770, but its names are SPAC-like
    assert pf.spac_like_now(master, intervals, ["a", "b", "c", "d"], sic) == {"a", "b", "d"}
    assert pf.spac_like_now(master, intervals, ["c"], sic) == set()


def test_listed_now_needs_an_open_common_interval_in_the_last_week(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "PRICE_FILE_OWNERS", tmp_path / "none.csv")
    weeks = pd.to_datetime(["2026-07-10", "2026-07-17"])
    rows = []
    for sid, non_common, foreign in (("open", False, False), ("shell", True, False), ("gone", False, False),
                                     ("foreign", False, True)):
        for k, week in enumerate(weeks):
            if sid == "gone" and k == 1:
                continue
            rows.append({"security_id": sid, "week_end": week, "non_common": non_common, "foreign": foreign,
                         "universe": not (non_common or foreign), "vendor_ok": False, "outside_trading": False,
                         "dv50_rank": np.nan, "dv20_rank": np.nan, "dv50": np.nan, "src": "stored"})
    weekly = pd.DataFrame(rows)
    spans = pd.DataFrame({"security_id": ["open", "shell", "gone", "foreign"], "ticker": ["OPN", "SHL", "GON", "FOR"],
                          "list_start": ["2026-01-02"] * 4,
                          "list_end": [pf.WINDOW_END, pf.WINDOW_END, "2026-07-13", pf.WINDOW_END]})
    master = pd.DataFrame({"security_id": ["open", "shell", "gone", "foreign"], "cik": ["1", "2", "3", "4"],
                           "name": ["O", "S", "G", "F"], "share_class": ["COMMON"] * 4, "delist_date": ["", "", "2026-07-13", ""],
                           "delist_form25_accession": [""] * 4, "foreign_filer": ["N", "N", "N", "Y"],
                           "transfer_date": [""] * 4, "tickers_sec_current": ["OPN", "SHL", "", "FOR"],
                           "exchanges_sec_current": ["Nasdaq", "Nasdaq", "", "Nasdaq"], "successor_security_id": [""] * 4})
    best = pd.DataFrame({"security_id": ["open"], "date": pd.to_datetime(["2026-07-10"]), "src": ["stored"], "file": ["opn.csv"]})
    form25 = pd.DataFrame(columns=["accession", "public_float_usd", "float_check_flag", "implied_float_per_share",
                                   "classification"])
    facts = pf.security_facts(weekly, spans, master, best, form25)
    assert facts["listed_now"].to_dict() == {"foreign": True, "gone": False, "open": True, "shell": False}
    assert facts.loc["foreign", "foreign_now"] and not facts.loc["open", "foreign_now"]
    # the foreign one has no universe week, so the rule never holds for it
    assert facts.loc["foreign", "uncovered_weeks"] == 0 and facts.loc["open", "uncovered_weeks"] == 2


def test_unknown_size_delisted_lists_weeks_with_neither_a_series_nor_a_proxy():
    weeks = pd.to_datetime(["2020-01-03", "2020-01-10", "2020-01-17", "2020-01-24"])
    rows = []
    for sid in ("gone", "gone_fetched", "gone_proxy", "listed"):
        for week in weeks:
            rows.append({"security_id": sid, "week_end": week, "universe": True, "vendor_ok": False,
                         "outside_trading": False, "dv50": np.nan, "price_ge_10": "",
                         "mcap": 5e9 if sid == "gone_proxy" else np.nan, "float_usd": np.nan})
    weekly = pd.DataFrame(rows)
    weekly.loc[(weekly["security_id"] == "gone") & (weekly["week_end"] == weeks[0]), ["dv50", "price_ge_10"]] = [1e6, "U"]
    weekly.loc[(weekly["security_id"] == "gone") & (weekly["week_end"] == weeks[3]), "outside_trading"] = True
    facts = pd.DataFrame({"active_nasdaq": [False, False, False, True], "tickers": ["GON", "GF", "GP", "LST"],
                          "name": ["G", "GF", "GP", "L"], "cik": ["1", "2", "3", "4"],
                          "delist_date": ["2020-02-01"] * 3 + [""], "last_listed": ["2020-02-01"] * 3 + [pf.WINDOW_END]},
                         index=["gone", "gone_fetched", "gone_proxy", "listed"])
    candidates = pd.DataFrame({"security_id": ["gone_fetched"], "reason": ["B_B_float_500M_1B"],
                               "planned_source": ["tiingo"], "status": ["pending"], "fetch_month": [pf.MONTH_1]})
    # a fetched series with a row in the weeks of 2020-01-10 and 2020-01-17 only
    dates = {"gone_fetched": np.array(["2020-01-08", "2020-01-16"], dtype="datetime64[D]")}
    out = pf.unknown_size_delisted(weekly, facts, candidates, fetched={}, dates=dates).set_index("security_id")
    assert list(out.index) == ["gone", "gone_fetched"]
    assert out.loc["gone", "unknown_weeks"] == 2            # one week has stored dv, one is after the last trade
    assert (out.loc["gone", "first_unknown_week"], out.loc["gone", "last_unknown_week"]) == ("2020-01-10", "2020-01-17")
    assert out.loc["gone", "next_step"].startswith("not_a_candidate")
    assert out.loc["gone_fetched", "unknown_weeks"] == 2    # 2020-01-03 and 2020-01-24
    assert out.loc["gone_fetched", "next_step"] == "tiingo_month1_pending"
    assert list(out.columns) == pf.UNKNOWN_SIZE_COLUMNS[1:]


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


def test_budget_follows_fetch_order_from_the_owners_already_used_count():
    frame = pd.DataFrame({"planned_source": ["tiingo"] * 4 + ["yahoo"], "fetch_month": [pf.MONTH_1] * 5,
                          "priority": [1, 2, 3, 3, 9], "security_id": ["a", "b", "c", "d", "e"],
                          "ticker_for_source": ["A", "B", "C", "A", "E"], "status": ["pending"] * 5,
                          "fetch_order": ["1", "2", "3", "1", ""]})
    out, budget = pf.apply_budget(frame, used=1, already_used=1, stop=4)
    assert out["status"].tolist() == ["pending", "pending", "deferred_quota", "pending", "pending"]
    assert budget["month1_symbols"] == 2 and budget["deferred_quota_symbols"] == 1
    assert budget["already_used_outside_ledger"] == 1 and budget["month1_room"] == 2
    _, unknown = pf.apply_budget(frame, used=0)
    assert "not given" in unknown["already_used_outside_ledger"]


def test_fetch_order_puts_month_one_by_priority_then_rank_and_shares_a_ticker():
    frame = pd.DataFrame({"planned_source": ["tiingo"] * 5, "security_id": ["v", "s", "a", "b", "z"],
                          "fetch_month": [pf.MONTH_1] * 4 + [pf.MONTH_2],
                          "priority": [10, 7, 1, 1, 12], "best_rank": ["5", "8", "200", "50", "1"],
                          "ticker_for_source": ["VV", "SS", "AA", "BB", "SS"], "v_category": ["odd_split", "", "", "", ""]})
    out = pf.assign_fetch_order(frame).set_index("security_id")["fetch_order"].to_dict()
    assert out == {"b": "1", "a": "2", "s": "3", "v": "4", "z": "3"}


def test_tier_c_sample_is_refilled_with_fetchable_names():
    ids = [f"s{k:02d}" for k in range(30)]
    order = pf.seeded_order(ids)
    frame = pd.DataFrame({"security_id": ids, "reason": ["B_C_rest_300M_500M"] * 30, "priority": [12] * 30,
                          "reasons_all": ["B_C_rest_300M_500M"] * 30, "planned_source": ["tiingo"] * 30,
                          "status": ["conditional_tier_c"] * 30, "fetch_month": [pf.MONTH_2] * 30})
    frame.loc[frame["security_id"] == order[0], "planned_source"] = "unfillable"
    out, facts = pf.refill_tier_c_sample(frame, n=5)
    sample = out[out["reason"] == "B_C_sample_300M_500M"]
    assert list(sample["security_id"]) == sorted(order[1:6])
    assert set(sample["status"]) == {"pending"} and set(sample["fetch_month"]) == {pf.MONTH_1}
    assert facts["sample"] == 5 and facts["pool_unfetchable"] == 1


def test_float_price_check_corrects_or_drops_unit_errors_of_10b_or_more():
    # Mister Car Wash-like $604B on 300M shares at $15 (a stored close): x1000 gives $2.01 a share, kept as
    # $604M (round 6); $15B on 1B shares at $20 fits the price and a fact without shares is kept as it is;
    # one whose x1000 value is still no price ($50T on 10M shares: $5,000 a share) is dropped.
    floats = pd.DataFrame({"cik": [1, 2, 3, 4], "end": pd.to_datetime(["2021-06-30"] * 4),
                           "val": [6.04e11, 1.5e10, 1.2e10, 5.0e13], "shares": [3.0e8, 1.0e9, float("nan"), 1.0e7]})
    weekly = pd.DataFrame({"security_id": ["a", "b", "c", "d"], "week_end": pd.to_datetime(["2021-07-02"] * 4),
                           "close": [15.0, 20.0, 1.0, 2.0]})
    master = pd.DataFrame({"security_id": ["a", "b", "c", "d"], "cik": ["1", "2", "3", "4"]})
    fixed = []
    kept, dropped = pf.float_price_check(floats, weekly, master, fixed)
    assert list(kept["cik"]) == [1, 2, 3]
    assert kept.loc[kept["cik"] == 1, "val"].iloc[0] == pytest.approx(6.04e8)
    assert kept.loc[kept["cik"] == 1, "unit_fix"].iloc[0] == "per_share"
    assert kept.loc[kept["cik"] == 1, "val_reported"].iloc[0] == pytest.approx(6.04e11)
    # the second value is still the dropped facts only (step 12 counts them)
    assert [r["cik"] for r in dropped] == [4] and [r["cik"] for r in fixed] == [1]


def test_x1000_float_errors_are_corrected_only_when_the_corrected_value_checks_out():
    # Codiak: $412.7B on 22.3M shares and no other fact (x1000: $18.48 a share); Vintage Wine Estates:
    # $429.6B next to a $364.7M fact (x1000 agrees), and $35.0B, whose x1000 ($35.0M) is a tenth of it.
    floats = pd.DataFrame({"cik": [1659352, 1834045, 1834045, 1834045],
                           "end": ["2021-06-30", "2020-12-31", "2021-12-31", "2022-12-31"],
                           "val": [4.127182e11, 3.6468e8, 4.295629e11, 3.5e10]})
    shares = pd.DataFrame({"cik": [1659352, 1834045, 1834045, 1834045],
                           "end": ["2021-08-02", "2020-12-31", "2022-02-01", "2023-04-30"],
                           "shares": [22331222.0, 36e6, 61691054.0, 59339163.0]})
    kept, decisions = pf.screen_floats(floats, shares)
    kept = kept.set_index(["cik", kept["end"].dt.strftime("%Y-%m-%d")])
    assert kept.loc[(1659352, "2021-06-30"), "val"] == pytest.approx(4.127182e8)
    assert kept.loc[(1659352, "2021-06-30"), "unit_fix"] == "per_share"
    assert kept.loc[(1834045, "2021-12-31"), "unit_fix"] == "per_share+other_facts"
    assert (1834045, "2022-12-31") not in kept.index
    dropped = [d for d in decisions if d["action"] == "dropped"]
    assert len(dropped) == 1 and "0.10x the median" in dropped[0]["check_or_reason"]
    assert pf.unit_fix(1e10, float("nan"))[0] is False
    assert pf.unit_fix(1e10, 1e9, close=None) == (False, "x1000 gives $0.01 a share outstanding: not an ordinary price")


# ------------------------------------------------------------------ built outputs

BUILT = [pf.CANDIDATES, pf.UNFILLABLE, pf.OUT / "prefilter_summary.json", pf.OUT / "weekly_coverage.csv"]


@pytest.mark.skipif(not all(Path(p).exists() for p in BUILT), reason="step 6 outputs not built")
def test_built_candidate_list_follows_the_plan():
    candidates = pd.read_csv(pf.CANDIDATES, dtype=str, keep_default_na=False)
    master = pd.read_csv(pf.MASTER, dtype=str, keep_default_na=False).set_index("security_id")
    assert list(candidates.columns) == pf.CANDIDATE_COLUMNS
    assert set(candidates["planned_source"]) <= {"wiki", "yahoo", "tiingo", "unfillable"}
    assert not candidates.duplicated(["security_id", "reason", "planned_source", "needed_start"]).any()
    # (a Yahoo row and its Tiingo or unfillable fallback share the reason; nothing else does)
    assert not candidates[candidates["fallback_from"] == ""].duplicated(["security_id", "reason"]).any()
    # Foreign filers are never ranked or fetched (MIXED ones only outside their foreign years).
    assert not candidates["security_id"].map(master["foreign_filer"]).eq("Y").any()
    # Tiingo is planned only on a supported_tickers range match; active names go to Yahoo.
    tiingo = candidates[candidates["planned_source"] == "tiingo"]
    shadowed = tiingo["tiingo_flags"].str.split().apply(lambda f: "ask_shadowed" in f)
    assert tiingo.loc[~shadowed, "tiingo_range_match"].isin(["Y", "partial"]).all()
    assert (tiingo.loc[shadowed, "tiingo_range_match"] == "hidden").all()
    assert (tiingo.loc[shadowed, "status"] == "deferred_quota").all() and (tiingo.loc[shadowed, "fallback_from"] != "").all()
    fetch = candidates[candidates["reason"] != "V_verify_sample"]
    own = fetch[fetch["fallback_from"] == ""]
    assert (own.loc[own["active"] == "Y", "planned_source"] == "yahoo").all()
    # a Tiingo fallback is month 2, after a Yahoo answer that failed or covers under half of the need
    fallback = fetch[(fetch["fallback_from"] != "") & (fetch["planned_source"] == "tiingo")]
    assert (fallback["status"] == "deferred_quota").all() and (fallback["fetch_month"] == pf.MONTH_2).all()
    assert (fetch.loc[fetch["planned_source"] == "unfillable", "status"].isin(["no_data", "wrong_entity"])).all()
    # The seeded tier-C sample has 20 names and V has 50.
    assert (candidates["reason"] == "B_C_sample_300M_500M").sum() == pf.TIER_C_SAMPLE
    assert (candidates["reason"] == "V_verify_sample").sum() == pf.V_SAMPLE
    month1 = tiingo[tiingo["fetch_month"] == pf.MONTH_1]["ticker_for_source"].nunique()
    assert month1 <= pf.TIINGO_MONTH_STOP
    # One fetch_order per Tiingo ticker; S names are month 1, ahead of the samples.
    order = tiingo.assign(n=tiingo["fetch_order"].astype(int))
    assert order.groupby("ticker_for_source")["n"].nunique().eq(1).all()
    s_rows = order[order["reason"].str.startswith("S_") & (order["status"] == "pending")]
    samples = order["reason"].isin(["V_verify_sample", "B_C_sample_300M_500M"])
    # (a sample ticker that a higher rule also needs, ZG for old Zillow, keeps that rule's place)
    later = order[samples & ~order["ticker_for_source"].isin(order.loc[~samples, "ticker_for_source"])]
    assert len(s_rows) and s_rows["n"].max() < later["n"].min()
    # Every planned Tiingo row is the row the API serves for its ticker.
    index = pf.tiingo_index(pf.load_supported_tickers(offline=True))
    for row in tiingo[~shadowed].itertuples(index=False):
        served = pf.served_row(index[row.ticker_for_source])
        assert (served["start"], served["end"]) == (row.tiingo_row_start, row.tiingo_row_end), row.ticker_for_source
    # TEVA (foreign to 2018-02) and Atlassian (to 2022-11) are not ranked in their foreign years;
    # unmerged SPAC shells are no candidates.
    assert not ((candidates["security_id"] == "818686") & candidates["reason"].str.startswith("A")).any()
    assert not ((candidates["security_id"] == "1650372") & (candidates["reason"] == "A1_wiki_dv_rank300")).any()
    shells = pf.spac_shells(master.reset_index(), pf.load_intervals())
    assert not candidates["security_id"].isin(list(shells)).any()
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


@pytest.mark.skipif(not all(Path(p).exists() for p in BUILT + [pf.OUT / "security_facts.csv.gz", pf.UNKNOWN_SIZE]),
                    reason="step 6 outputs not built")
def test_built_list_sends_every_uncovered_name_listed_now_to_yahoo():
    candidates = pd.read_csv(pf.CANDIDATES, dtype=str, keep_default_na=False)
    facts = pd.read_csv(pf.OUT / "security_facts.csv.gz", dtype=str, keep_default_na=False).set_index("security_id")
    listed = facts[(facts["listed_now"] == "True") & (facts["uncovered_weeks"].astype(int) > 0)]
    shells = facts.index[facts["spac_like_now"] == "True"]
    yahoo = candidates[candidates["planned_source"] == "yahoo"]
    assert set(listed.index) - set(shells) <= set(yahoo["security_id"])
    # round 6: and every name still trading off Nasdaq (moved, or out of the latest snapshots), under its
    # SEC current ticker, unless Yahoo cannot be asked for it (when-issued, another class's tickers)
    off = facts[(facts["active"] == "True") & (facts["last_listed"].str[:10] != pf.WINDOW_END)
                & (facts["uncovered_weeks"].astype(int) > 0) & (facts["yahoo_excluded"] == "")]
    assert len(off) and set(off.index) <= set(yahoo["security_id"])
    for sid in ("857855", "816956", "1578987"):  # UCBI -> UCB, CNMD, BANX
        assert sid in set(yahoo["security_id"]), sid
    assert yahoo.loc[yahoo["security_id"] == "857855", "ticker_for_source"].iloc[0] == "UCB"
    assert not candidates["security_id"].isin(["1570585.T-LBYAV", "1570585.T-LBYKV", "1560385.T-LLYVA",
                                               "1560385.T-LLYVK"]).any()
    assert not candidates.loc[candidates["reason"] == "Y_active_all", "security_id"].isin(shells).any()
    # round 5: listed, never ranked by step 6 before later listings after a Form 25 were kept
    for sid, ticker in pf.Y_NAMED.items():
        rows = yahoo[yahoo["security_id"] == sid]
        assert (rows["ticker_for_source"] == ticker).any() and rows["reason"].str.startswith("Y_active").any(), ticker
    # foreign filers (Y) and SPAC shells never get the rule
    y_rows = candidates[candidates["reason"] == "Y_active_all"]
    assert not y_rows["security_id"].map(facts["foreign_filer"]).eq("Y").any()
    unknown = pd.read_csv(pf.UNKNOWN_SIZE, dtype=str, keep_default_na=False)
    assert list(unknown.columns) == pf.UNKNOWN_SIZE_COLUMNS
    assert not unknown["security_id"].isin(listed.index).any()
    assert not unknown["security_id"].isin(facts.index[facts["active"] == "True"]).any()
    for column in ("close", "dv50", "market_cap", "float_usd"):
        assert column not in unknown.columns


# ------------------------------------------------------------------ round 6

def _wolf_facts(extra: dict | None = None) -> pd.DataFrame:
    base = {"tickers": "CREE", "ticker_last_held": "CREE:2021-10-03", "ticker_first_held": "CREE:2010-12-31",
            "tickers_sec_current": "WOLF", "first_listed": "2010-12-31", "last_listed": "2021-10-03", "active": True,
            "transfer_date": "", "cik": "895419", "name": "WOLFSPEED, INC.", "delist_date": "", "via_successor": ""}
    rows = {"895419": base, **(extra or {})}
    return pd.DataFrame(list(rows.values()), index=list(rows))


def _yahoo_row(sid, ticker, start, end, reason="A1_wiki_dv_rank300"):
    return {"security_id": sid, "ticker_for_source": ticker, "needed_start": start, "needed_end": end, "reason": reason,
            "reasons_all": reason, "priority": pf.REASON_PRIORITY.index(reason) + 1, "planned_source": "yahoo",
            "status": "pending", "fetch_month": pf.MONTH_1, "active": "Y", "cik": sid, "name": "x", "note": "",
            "tiingo_flags": "", "tiingo_reused_ticker": ""}


def test_a_yahoo_answer_without_the_need_falls_back_to_tiingo_wolf():
    # Wolfspeed (CREE until 2021-10): Yahoo's WOLF holds only the post-bankruptcy equity from 2025-09-29, so
    # the A1 need 2018-01-21..2021-10-31 has no row (no_rows). Tiingo's WOLF row 1993-02-09..2025-09-26
    # covers it; the API serves the NYSE row of the new equity, so the row is asked only on purpose.
    index = supported([("WOLF", "NASDAQ", "Stock", "USD", "1993-02-09", "2025-09-26"),
                       ("WOLF", "NASDAQ", "Stock", "USD", "2025-06-30", "2025-09-19"),
                       ("WOLF", "NYSE", "Stock", "USD", "2025-09-29", "2026-09-30")])
    candidates = pd.DataFrame([_yahoo_row("895419", "WOLF", "2018-01-21", "2021-10-31")]).reindex(
        columns=pf.CANDIDATE_COLUMNS)
    answers = {("895419", "WOLF"): [{"symbol": "WOLF", "verdict": "no_rows", "needed_start": "2018-01-21",
                                     "needed_end": "2021-10-31", "first_row": "2025-09-29", "last_row": "2026-08-31",
                                     "need_coverage": 0.0, "reasons": "no rows in the need"}]}
    out, counts = pf.yahoo_fallback(candidates, _wolf_facts(), answers, index, sessions=pf.xnas_sessions())
    yahoo = out[out["planned_source"] == "yahoo"].iloc[0]
    assert yahoo["status"] == "yahoo_failed" and yahoo["yahoo_row_start"] == "2025-09-29"
    tiingo = out[out["planned_source"] == "tiingo"].iloc[0]
    assert tiingo["ticker_for_source"] == "WOLF" and tiingo["fallback_from"] == "yahoo_no_rows"
    assert (tiingo["needed_start"], tiingo["needed_end"]) == ("2018-01-21", "2021-10-31")
    assert (tiingo["status"], tiingo["fetch_month"]) == ("deferred_quota", pf.MONTH_2)
    assert (tiingo["tiingo_row_start"], tiingo["tiingo_row_end"]) == ("1993-02-09", "2025-09-26")
    assert "ask_shadowed" in tiingo["tiingo_flags"].split() and counts["fallback_tiingo_ask_shadowed"] == 1
    # With a single WOLF row the API serves, it is a plain Tiingo row for month 2.
    plain = supported([("WOLF", "NASDAQ", "Stock", "USD", "1993-02-09", "2025-09-26")])
    out, counts = pf.yahoo_fallback(candidates, _wolf_facts(), answers, plain, sessions=pf.xnas_sessions())
    tiingo = out[out["planned_source"] == "tiingo"].iloc[0]
    assert tiingo["tiingo_range_match"] == "Y" and "ask_shadowed" not in tiingo["tiingo_flags"]
    assert counts["fallback_tiingo"] == 1
    # With no Tiingo row it is unfillable, and unfillable.csv names the Yahoo answer.
    out, counts = pf.yahoo_fallback(candidates, _wolf_facts(), answers, {}, sessions=pf.xnas_sessions())
    gone = out[out["planned_source"] == "unfillable"].iloc[0]
    assert gone["fallback_from"] == "yahoo_no_rows" and gone["status"] == "no_data"
    weekly = pd.DataFrame({"security_id": ["895419"], "week_end": pd.to_datetime(["2019-01-04"]), "universe": [True],
                           "mcap": [np.nan], "float_usd": [np.nan]})
    ranks = pd.DataFrame(columns=["security_id", "snapshot_date", "mcap_rank"])
    table = pf.unfillable_rows(out, _wolf_facts(), weekly, pd.Series([False], index=weekly.index), ranks)
    assert len(table) == 1 and "yahoo(no_rows 2025-09-29..2026-08-31)" in table.loc[0, "sources_tried"]


def test_yahoo_answers_set_the_status_and_only_poor_partials_fall_back():
    index = supported([("GOOD", "NASDAQ", "Stock", "USD", "2010-01-04", "2026-09-30"),
                       ("SHORT", "NASDAQ", "Stock", "USD", "2010-01-04", "2026-09-30")])
    facts = _wolf_facts({
        "1": {**_wolf_facts().iloc[0].to_dict(), "tickers": "GOOD", "ticker_last_held": f"GOOD:{pf.WINDOW_END}",
              "ticker_first_held": "GOOD:2010-12-31", "tickers_sec_current": "GOOD", "last_listed": pf.WINDOW_END},
        "2": {**_wolf_facts().iloc[0].to_dict(), "tickers": "SHORT", "ticker_last_held": f"SHORT:{pf.WINDOW_END}",
              "ticker_first_held": "SHORT:2010-12-31", "tickers_sec_current": "SHORT", "last_listed": pf.WINDOW_END}})
    candidates = pd.DataFrame([_yahoo_row("1", "GOOD", "2018-01-21", "2026-08-31", "Y_active_all"),
                               _yahoo_row("2", "SHORT", "2018-07-18", "2026-08-31", "Y_active_rank300"),
                               _yahoo_row("895419", "WOLF", "2018-01-21", "2021-10-31")]).reindex(
        columns=pf.CANDIDATE_COLUMNS)
    answers = {("1", "GOOD"): [{"symbol": "GOOD", "verdict": "partial", "needed_start": "2018-01-21",
                                "needed_end": "2026-08-31", "first_row": "2019-06-03", "last_row": "2026-08-31",
                                "need_coverage": 0.83, "reasons": ""}],
               ("2", "SHORT"): [{"symbol": "SHORT", "verdict": "partial", "needed_start": "2018-07-18",
                                 "needed_end": "2026-08-31", "first_row": "2026-07-17", "last_row": "2026-08-31",
                                 "need_coverage": 0.0034, "reasons": ""}]}
    out, counts = pf.yahoo_fallback(candidates, facts, answers, index, sessions=pf.xnas_sessions())
    status = out[out["planned_source"] == "yahoo"].set_index("security_id")["status"].to_dict()
    assert status == {"1": "partial", "2": "partial", "895419": "pending"}
    fallback = out[out["fallback_from"] != ""]
    assert list(fallback["security_id"]) == ["2"]  # CRNX-like: 0.3% of the need
    assert (fallback.iloc[0]["needed_start"], fallback.iloc[0]["needed_end"]) == ("2018-07-18", "2026-07-16")
    # the gap a good partial leaves (17 months before 2019-06-03) is listed in unfillable.csv, no symbol spent
    weekly = pd.DataFrame({"security_id": ["1"], "week_end": pd.to_datetime(["2018-06-01"]), "universe": [True],
                           "mcap": [np.nan], "float_usd": [np.nan]})
    ranks = pd.DataFrame(columns=["security_id", "snapshot_date", "mcap_rank"])
    table = pf.unfillable_rows(out, facts, weekly, pd.Series([False], index=weekly.index), ranks)
    row = table[table["security_id"] == "1"].iloc[0]
    assert (row["needed_start"], row["needed_end"]) == ("2018-01-21", "2019-06-02") and "tiingo not asked" in row["sources_tried"]


def test_y_active_all_also_takes_names_still_trading_off_nasdaq():
    # UCBI moved to NYSE as UCB (2024-08); BANX fell out of the latest snapshots while SEC still lists it;
    # LLYVA's CIK tickers now name other tracking stocks, so Yahoo cannot be asked for it.
    facts = _fact_rows({
        "ucbi": {"uncovered_weeks": 332, "listed_now": False, "last_listed": "2024-08-12"},
        "banx": {"uncovered_weeks": 643, "listed_now": False, "last_listed": "2026-02-28"},
        "llyva": {"uncovered_weeks": 125, "listed_now": False, "last_listed": "2025-12-31",
                  "yahoo_excluded": "sec_tickers_of_another_class"},
        "gone": {"uncovered_weeks": 30, "listed_now": False, "active": False, "last_listed": "2020-05-01"},
    })
    hits = pf.rule_hits(facts, pd.DataFrame(columns=["security_id", "snapshot_date", "mcap_rank"]),
                        pd.DataFrame(columns=["cik", "end", "val"]))
    assert "Y_active_all" in hits["ucbi"] and "Y_active_all" in hits["banx"]
    assert "llyva" not in hits and "gone" not in hits
    assert pf.yahoo_ticker(pd.Series({"active_nasdaq": False, "last_ticker": "UCBI", "yahoo_tickers": "UCB"})) == "UCB"
    assert pf.yahoo_ticker(pd.Series({"active_nasdaq": False, "last_ticker": "UHALB",
                                      "yahoo_tickers": "UHAL UHAL-B"})) == "UHAL-B"
    assert pf.WHEN_ISSUED.search("Liberty Global plc - Class A Ordinary Shares When Distributed")


def test_unknown_size_names_are_only_names_no_longer_trading():
    weeks = pd.to_datetime(["2020-01-03", "2020-01-10"])
    weekly = pd.DataFrame([{"security_id": s, "week_end": w, "universe": True, "vendor_ok": False,
                            "outside_trading": False, "dv50": np.nan, "price_ge_10": "", "mcap": np.nan,
                            "float_usd": np.nan} for s in ("gone", "moved") for w in weeks])
    facts = pd.DataFrame({"active_nasdaq": [False, False], "active": [False, True], "tickers": ["GON", "MOV"],
                          "name": ["G", "M"], "cik": ["1", "2"], "delist_date": ["2020-02-01", ""],
                          "last_listed": ["2020-02-01", "2020-02-01"]}, index=["gone", "moved"])
    out = pf.unknown_size_delisted(weekly, facts, pd.DataFrame(columns=["security_id", "reason", "planned_source",
                                                                        "status", "fetch_month"]), fetched={}, dates={})
    assert list(out["security_id"]) == ["gone"]


def test_a_later_listing_after_a_form25_needs_a_symbol_file_or_a_price_row():
    # LLEX 2017-03-16..09-11 and PBIO 2017-08-13..09-11 were seen in Wayback company lists only, with no
    # price at all: cut to one day as before round 5. CHRD's later listing has symbol files and prices.
    spans = pd.DataFrame([
        {"security_id": "llex", "ticker": "LLEX", "list_start": "2017-03-16", "list_end": "2017-09-11",
         "after_cut": True, "sources": "wayback_companylist"},
        {"security_id": "chrd", "ticker": "OAS", "list_start": "2020-11-20", "list_end": "2022-07-22",
         "after_cut": True, "sources": "repo_symdir"},
        {"security_id": "cepl", "ticker": "CEPL", "list_start": "2026-07-02", "list_end": "2026-08-31",
         "after_cut": True, "sources": "repo_screener_300M"},
        {"security_id": "llex", "ticker": "LLEX", "list_start": "2014-01-22", "list_end": "2016-05-20",
         "after_cut": False, "sources": "wayback_companylist wayback_symdir"}])
    best = pd.DataFrame({"security_id": ["cepl"], "date": pd.to_datetime(["2026-07-08"])})
    out, cut = pf.confirm_after_cut(spans, best)
    assert [c["security_id"] for c in cut] == ["llex"]
    llex = out.iloc[0]
    assert llex["list_end"] == "2017-03-16" and not llex["after_cut"] and llex["after_cut_unconfirmed"]
    assert out.iloc[1]["after_cut"] and out.iloc[2]["after_cut"] and out.iloc[2]["list_end"] == pf.WINDOW_END


def test_the_ipo_rule_does_not_start_a_listing_on_another_companys_rows_truecar():
    # TRUE: snapshot absent 2014-03-25, first seen 2014-06-05; WIKI's TRUE rows from 2014-03-26 (the first
    # session the mapping span allows) are another company's; TrueCar's IPO and its stored file 2014-05-16.
    sessions = pf.xnas_sessions("2014-01-02", "2014-12-31")
    days = sessions[(sessions >= "2014-03-26") & (sessions <= "2014-06-30")]
    best = pd.DataFrame({"security_id": "1327318", "date": days})
    spans = pd.DataFrame([{"security_id": "1327318", "ticker": "TRUE", "list_start": "2014-06-05",
                           "list_end": pf.WINDOW_END, "start_prev_absent": "2014-03-25", "snapshot_start": "2014-06-05"},
                          # AMD-like transfer: rows start on the boundary session, no later stored file
                          {"security_id": "amd", "ticker": "AMD", "list_start": "2014-06-05", "list_end": pf.WINDOW_END,
                           "start_prev_absent": "2014-03-25", "snapshot_start": "2014-06-05"},
                          # an IPO between two snapshots
                          {"security_id": "ipo", "ticker": "IPO", "list_start": "2014-06-05", "list_end": pf.WINDOW_END,
                           "start_prev_absent": "2014-03-25", "snapshot_start": "2014-06-05"}])
    best = pd.concat([best, pd.DataFrame({"security_id": "amd", "date": days}),
                      pd.DataFrame({"security_id": "ipo", "date": days[days >= "2014-05-01"]})], ignore_index=True)
    stored = {("1327318", "TRUE"): ["2014-05-16"]}
    trims = pf.boundary_trims(spans, best, stored, sessions)
    assert trims == {("1327318", "TRUE", "2014-06-05"): ("2014-03-26", "2014-05-16")}
    trimmed, dropped = pf.trim_rows(best, trims)
    assert dropped == int(((days >= "2014-03-26") & (days < "2014-05-16")).sum())
    out = pf.extend_starts(spans, trimmed, sessions).set_index("security_id")
    assert out.loc["1327318", "list_start"] == "2014-05-16" and out.loc["1327318", "ipo_start"]
    assert out.loc["amd", "list_start"] == "2014-06-05" and out.loc["amd", "ipo_boundary"]
    assert out.loc["ipo", "list_start"] == "2014-05-01" and out.loc["ipo", "ipo_start"]
    # with step 12's published list, only the intervals it names keep their snapshot start
    listed = pf.extend_starts(spans, trimmed, sessions, boundary={("amd", "AMD", "2014-06-05")}).set_index("security_id")
    assert listed.loc["amd", "ipo_boundary"] and listed.loc["amd", "list_start"] == "2014-06-05"
    assert listed.loc["1327318", "list_start"] == "2014-05-16" and listed.loc["ipo", "list_start"] == "2014-05-01"


def test_a_relisting_after_a_bankruptcy_has_no_warm_up_in_the_old_shares():
    # Oasis/Chord: Form 25 2020-11-06 in its bankruptcy, the new equity listed from 2020-11-20.
    row = pd.Series({"first_listed": "2019-12-27", "first_uncovered": pd.Timestamp("2019-12-27"),
                     "last_uncovered": pd.Timestamp("2026-07-17"), "last_universe_week": pd.Timestamp("2026-07-17"),
                     "last_listed": pf.WINDOW_END, "transfer_date": "", "active": True, "active_nasdaq": True,
                     "via_successor": "", "delist_date": "2020-11-06", "relisted_from": "2020-11-20",
                     "listed_before_cut": "2020-10-11", "relist_new_equity": "bankruptcy"})
    hits = {"B_A_float_ge_1B": ("form25_max_float_3y_usd", 2e9), "Y_active_rank300": ("dv", 100)}
    (_, _, s1, e1, _), (_, _, s2, e2, note) = pf.relisted_parts(row, ["B_A_float_ge_1B", "Y_active_rank300"], hits)
    assert e1 == "2020-10-11" and s2 == "2020-11-20" and "no warm-up" in note
    row["relist_new_equity"] = ""
    assert pf.relisted_parts(row, ["B_A_float_ge_1B", "Y_active_rank300"], hits)[1][2] == "2020-09-06"
    facts = pd.DataFrame({"relisted_from": ["2020-11-20", "2020-01-15", ""],
                          "delist_form25_accession": ["a1", "a2", "a3"]}, index=["1486159", "1375365", "x"])
    terminal = pd.DataFrame({"security_id": ["1486159", "1375365"], "event_subtype": ["bankruptcy", "removed_by_exchange"]})
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "terminal.csv"
        terminal.to_csv(path, index=False)
        kinds = pf.relisting_kinds(facts, pd.DataFrame({"accession": ["a2"], "delisting_basis": ["exchange_removal"]}), path)
    assert kinds.to_dict() == {"1486159": "bankruptcy", "1375365": "", "x": ""}


def test_the_month2_plan_ranks_tiingo_only_names_by_expected_top250_weeks():
    candidates = pd.DataFrame([
        {**_yahoo_row("a", "AAA", "2018-01-21", "2021-10-31"), "planned_source": "tiingo", "status": "deferred_quota",
         "fetch_month": pf.MONTH_2, "fallback_from": "yahoo_no_rows"},
        {**_yahoo_row("b", "BBB", "2019-01-01", "2020-12-31", "V_verify_sample"), "planned_source": "tiingo",
         "status": "deferred_quota", "fetch_month": pf.MONTH_2, "fallback_from": ""},
        {**_yahoo_row("c", "CCC", "2019-01-01", "2020-12-31", "B_C_rest_300M_500M"), "planned_source": "tiingo",
         "status": "conditional_tier_c", "fetch_month": pf.MONTH_2, "fallback_from": ""},
        {**_yahoo_row("d", "DDD", "2019-01-01", "2020-12-31", "B_A_float_ge_1B"), "planned_source": "tiingo",
         "status": "pending", "fetch_month": pf.MONTH_1, "fallback_from": ""},  # month 1, being fetched
        {**_yahoo_row("e", "EEE", "2019-01-01", "2020-12-31", "B_A_float_ge_1B"), "planned_source": "tiingo",
         "status": "pending", "fetch_month": pf.MONTH_1, "fallback_from": ""},  # answered already
    ]).reindex(columns=pf.CANDIDATE_COLUMNS).fillna("")
    status = pd.DataFrame([{"security_id": "e", "ticker_for_source": "EEE", "needed_start": "2019-01-01",
                            "needed_end": "2020-12-31", "status": "done"}])
    evidence = pd.DataFrame({"week_end": pd.to_datetime(["2019-01-04", "2019-01-11", "2019-01-04", "2022-01-07"]),
                             "security_id": ["a", "a", "c", "a"], "p_top250": [0.9, 0.8, 0.5, 5.0],
                             "unknown": [False, False, False, False]})
    weekly = pd.DataFrame(columns=["security_id", "week_end", "universe", "vendor_ok", "dv50_rank"])
    plan, facts = pf.tiingo_month2_plan(candidates, pd.DataFrame(), pd.DataFrame(), {}, weekly, evidence, status,
                                        cached=lambda t: t == "CCC")
    assert list(plan["security_id"]) == ["a", "c", "b"]
    assert plan["expected_top250_weeks"].tolist() == [1.7, 0.5, 0.0]  # a's 2022 week is outside its need
    assert plan["group"].tolist() == ["yahoo_fallback", "tier_c", "deferred_quota"]
    assert plan["new_symbol"].tolist() == ["Y", "", "Y"] and plan["cum_new_symbols"].tolist() == [1, 1, 2]
    assert set(plan[f"within_{pf.PLAN_CUT}"]) == {"Y"} and facts["rows"] == 3


def test_list_changes_name_the_rows_a_build_dropped():
    before = pd.DataFrame({"security_id": ["1", "2"], "reason": ["A1_wiki_dv_rank300", "B_A_float_ge_1B"],
                           "planned_source": ["tiingo", "tiingo"], "status": ["pending", "pending"]})
    after = pd.DataFrame({"security_id": ["1", "2"], "reason": ["A1_wiki_dv_rank300", "B_A_float_ge_1B"],
                          "planned_source": ["tiingo", "unfillable"], "status": ["done", "wrong_entity"]})
    changes = pf.list_changes(before, after)
    assert changes["dropped_keys"] == ["2|B_A_float_ge_1B|tiingo"] and changes["added"] == 1
    assert changes["status_changes"] == {"pending->done": 1}


@pytest.mark.skipif(not all(Path(p).exists() for p in BUILT + [pf.YAHOO_REPORT, pf.MONTH2_PLAN]),
                    reason="step 6 / step 7 outputs not built")
def test_built_list_feeds_yahoo_answers_back_and_plans_the_fallbacks():
    candidates = pd.read_csv(pf.CANDIDATES, dtype=str, keep_default_na=False)
    yahoo = candidates[candidates["planned_source"] == "yahoo"]
    assert set(yahoo["status"]) <= {"pending", "done", "done_review", "partial", "yahoo_failed"}
    report = pd.read_csv(pf.YAHOO_REPORT, dtype=str, keep_default_na=False)
    answered = set(zip(report["security_id"], report["candidate_symbol"]))
    keys = list(zip(yahoo["security_id"], yahoo["ticker_for_source"]))
    assert all((k in answered) == (st != "pending") for k, st in zip(keys, yahoo["status"]))
    # every failed or poor Yahoo answer has a fallback row (Tiingo, or unfillable)
    poor = yahoo[(yahoo["status"] == "yahoo_failed") | ((yahoo["status"] == "partial")
                                                       & (pd.to_numeric(yahoo["yahoo_coverage"]) < pf.MIN_PARTIAL_COVERAGE))]
    fallback = candidates[candidates["fallback_from"] != ""]
    assert set(zip(poor["security_id"], poor["reason"])) == set(zip(fallback["security_id"], fallback["reason"]))
    # Wolfspeed (CREE): its A1 need 2018-2021 ends with a Tiingo row
    wolf = fallback[fallback["security_id"] == "895419"]
    assert (wolf["planned_source"] == "tiingo").any() and (wolf["ticker_for_source"] == "WOLF").any()
    # INPUTS/unfillable.csv is this build's (every unfillable candidate row is in it)
    unfillable = pd.read_csv(pf.UNFILLABLE, dtype=str, keep_default_na=False)
    gone = candidates[(candidates["planned_source"] == "unfillable") & (candidates["reason"] != "V_verify_sample")]
    assert set(zip(gone["security_id"], gone["needed_start"])) <= set(zip(unfillable["security_id"], unfillable["needed_start"]))
    plan = pd.read_csv(pf.MONTH2_PLAN, dtype=str, keep_default_na=False)
    assert list(plan.columns) == pf.PLAN_COLUMNS
    assert pd.to_numeric(plan["expected_top250_weeks"]).is_monotonic_decreasing
    assert set(fallback.loc[fallback["planned_source"] == "tiingo", "security_id"]) <= set(plan["security_id"])
