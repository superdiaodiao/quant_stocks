"""Offline tests for the round-9 security-master fixes (plan step 4): Nasdaq's 'Closed End Fund' label,
company-list rows after a move to another exchange (MSG), securities known only from SEC's current ticker
list, a Form 25 filed before the first listing, and the scratch-build redirect."""
import pandas as pd

from scripts import reversal_data_security_master as sm


# ------------------------------------------------------------------ 'Closed End Fund' is Nasdaq's issue type

def test_a_closed_end_fund_label_is_judged_by_the_issuer_name():
    # Business development companies and trusts named without 'Fund' are listed equity; D6 decides later.
    for name in ("Ares Capital Corporation - Closed End Fund", "TICC Capital Corp. - Closed End Fund",
                 "Sprott Focus Trust, Inc. - Closed End Fund", "Apollo Investment Corporation - Closed End Fund"):
        assert sm.is_common_equity(name), name
    # An issuer named a fund stays out, as before (CUBA's file even spells it 'FUnd').
    for name in ("The Herzfeld Caribbean Basin Fund, Inc. - Closed End FUnd",
                 "Calamos Global Total Return Fund - Closed End Fund", "Eaton Vance Closed-End Fund Inc",
                 "Ares Capital Corporation - 6.875% Notes due 2047"):
        assert not sm.is_common_equity(name), name


def test_the_closed_end_fund_label_is_cut_from_the_name_key():
    assert sm.normalize_issuer_name("Ares Capital Corporation - Closed End Fund") == "ares capital"
    assert sm.normalize_issuer_name("Oxford Square Capital Corp. - Closed End Fund") == "oxford square capital"
    assert sm.normalize_issuer_name("PMC - Sierra, Inc. - Common Stock") == "pmc sierra"


def test_bare_company_list_rows_follow_a_closed_end_fund_typed_row():
    snaps = pd.DataFrame([
        {"symbol": "ARCC", "name": "Ares Capital Corporation - Closed End Fund", "date": "2010-12-31",
         "source": "wayback_symdir"},
        {"symbol": "ARCC", "name": "Ares Capital Corporation", "date": "2011-01-25", "source": "wayback_companylist"},
        {"symbol": "CUBA", "name": "The Herzfeld Caribbean Basin Fund, Inc. - Closed End FUnd", "date": "2010-12-31",
         "source": "wayback_symdir"},
        {"symbol": "CUBA", "name": "The Herzfeld Caribbean Basin Fund, Inc.", "date": "2011-01-25",
         "source": "wayback_companylist"},
    ]).assign(**{"ETF": "", "Test Issue": "", "NextShares": "", "source_url": "", "name_truncated": False,
                 "no_last_sale": False})
    rows = sm.common_snapshot_rows(snaps)
    assert sorted(zip(rows["symbol"], rows["date"])) == [("ARCC", "2010-12-31"), ("ARCC", "2011-01-25")]


# ------------------------------------------------------------------ company-list rows after a transfer

SYMDIR_URL = "https://web.archive.org/web/20150628013536id_/http://www.nasdaqtrader.com:80/dynamic/SymDir/nasdaqlisted.txt"
LIST_URL = "https://web.archive.org/web/20151009224448id_/http://www.nasdaq.com:80/screening/companies-by-name.aspx"


def _row(day, symbol, cik, family):
    return {"date": day, "symbol": symbol, "cik": cik, "family": family,
            "source_url": SYMDIR_URL if family == "wayback_symdir" else LIST_URL}


def test_msg_company_list_rows_after_its_move_to_nyse_are_no_listing():
    # MSG moved to NYSE on 2015-07-27 (Form 25 filed 2015-07-24, Form 8-A12B 2015-07-14); the nasdaq.com
    # lists kept it to 2017. The symbol directory before the move does not confirm the rows after it.
    rows = pd.DataFrame([_row("2015-06-26", "MSG", 1469372, "wayback_symdir"),
                         _row("2015-08-10", "MSG", 1469372, "wayback_companylist"),
                         _row("2015-10-09", "MSG", 1469372, "wayback_companylist"),
                         _row("2016-11-13", "MSG", 1469372, "wayback_companylist"),
                         _row("2016-11-13", "OTHER", 7, "wayback_companylist")])
    transfers = {1469372: [("2015-07-24", {"MSG"})]}
    listed = {"2015-06-26": {"MSG", "OTHER"}, "2016-12-30": {"OTHER"}}
    stale = sm.stale_after_transfer(rows, transfers, listed)
    assert list(rows.loc[stale, "date"]) == ["2015-08-10", "2015-10-09", "2016-11-13"]
    assert set(rows.loc[stale, "symbol"]) == {"MSG"}


def test_a_directory_after_the_move_or_a_return_to_nasdaq_keeps_the_rows():
    rows = pd.DataFrame([_row("2012-03-01", "AMTD", 1, "wayback_symdir"),
                         _row("2013-05-01", "AMTD", 1, "wayback_companylist"),   # on NYSE then: stale
                         _row("2016-01-11", "AMTD", 1, "wayback_symdir"),        # back on Nasdaq
                         _row("2017-03-01", "AMTD", 1, "wayback_companylist"),   # after the return: kept
                         _row("2013-05-01", "XYZ", 2, "wayback_companylist"),    # confirmed by a later directory
                         _row("2013-05-01", "XYZW", 2, "wayback_companylist")])  # ticker the filing did not cover
    transfers = {1: [("2012-04-24", {"AMTD"})], 2: [("2013-01-10", {"XYZ"})]}
    listed = {"2012-03-01": {"AMTD"}, "2013-06-15": {"XYZ"}, "2016-01-11": {"AMTD"}}
    stale = sm.stale_after_transfer(rows, transfers, listed)
    assert list(zip(rows.loc[stale, "symbol"], rows.loc[stale, "date"])) == [("AMTD", "2013-05-01")]


def test_transfer_filings_take_only_documented_exits():
    f25 = pd.DataFrame([
        {"subject_cik": 1469372, "filing_date": "2015-07-24", "classification": "transfer", "subject_exit": "Y",
         "tickers_before": "MSG", "tickers_ended": "", "tickers_continued": "MSG"},
        {"subject_cik": 9, "filing_date": "2020-01-02", "classification": "transfer", "subject_exit": "N",
         "tickers_before": "ABC", "tickers_ended": "", "tickers_continued": "ABC"},
        {"subject_cik": 8, "filing_date": "2020-01-02", "classification": "common_delisting", "subject_exit": "Y",
         "tickers_before": "DEF", "tickers_ended": "DEF", "tickers_continued": ""},
    ])
    assert sm.transfer_filings(f25) == {1469372: [("2015-07-24", {"MSG"})]}


# ------------------------------------------------------------------ the master names every interval's security

def _issuer(name, tickers=()):
    return {**sm.parse_submissions({}), "name": name, "former_names": [], "tickers": list(tickers),
            "entity_type": "operating", "older_pages": [], "periodic_dates": ["2012-03-01", "2026-03-01"],
            "filing_dates": ["2012-03-01", "2026-03-01"]}


def test_a_current_only_ticker_of_a_multi_class_issuer_gets_a_master_row():
    profiles = {88948: _issuer("Seneca Foods Corp", ["SENEA", "SENEB", "SENEL"])}
    snap = pd.DataFrame([
        {"security_id": "88948.A", "cik": 88948, "ticker": "SENEA", "start": "2010-12-31", "end": "2026-08-01",
         "share_class": "A", "source": "repo_symdir", "match": "name+ticker", "exchange": "NASDAQ"},
        {"security_id": "88948.B", "cik": 88948, "ticker": "SENEB", "start": "2010-12-31", "end": "2026-08-01",
         "share_class": "B", "source": "repo_symdir", "match": "name+ticker", "exchange": "NASDAQ"}])
    current = pd.DataFrame([
        {"security_id": "88948.A", "cik": 88948, "ticker": "SENEA", "start": "", "end": "2026-10-01",
         "share_class": "A", "source": "sec_company_tickers_exchange", "match": "sec", "exchange": "NASDAQ"},
        {"security_id": "88948.T-SENEL", "cik": 88948, "ticker": "SENEL", "start": "", "end": "2026-10-01",
         "share_class": "T-SENEL", "source": "sec_company_tickers_exchange", "match": "sec", "exchange": "OTC"}])
    every = pd.concat([snap, current], ignore_index=True)
    prices = pd.DataFrame(columns=["ticker", "file", "first_date", "last_date", "rows", "cik", "how",
                                   "ciks_on_ticker_in_file_range"])
    f25 = pd.DataFrame(columns=["subject_cik", *sm.FORM25_TEXT_COLUMNS, "successor_cik"])
    master = sm.build_master(profiles, every, snap, f25, prices, {88948}, []).set_index("security_id")
    assert set(every["security_id"]) <= set(master.index)
    senel = master.loc["88948.T-SENEL"]
    assert senel["first_ticker"] == "SENEL" and senel["first_listed"] == "" and senel["share_class"] == "T-SENEL"
    assert "only in SEC's current ticker list (SENEL on OTC)" in senel["identity_notes"]
    assert master.loc["88948.A", "first_listed"] == "2010-12-31"
    assert "current ticker list" not in master.loc["88948.A", "identity_notes"]


# ------------------------------------------------------------------ a Form 25 before the first listing

def test_a_form25_before_the_first_listing_gives_no_delist_date():
    full = ["2017-01-30", "2017-03-16", "2017-05-11", "2017-07-01", "2017-08-13"]
    # ShiftPixy: Form 25 effective 2017-02-24, first Nasdaq row 2017-07-01.
    assert sm.listed_past_delisting([("2017-07-01", "2026-07-01")], "2017-02-24", full).startswith(
        "no listing on or before it")
    # A security with no snapshot interval keeps the date; one listed before it is judged as before.
    assert sm.listed_past_delisting([], "2017-02-24", full) == ""
    assert sm.listed_past_delisting([("2012-01-01", "2017-02-20")], "2017-02-24", full) == ""


# ------------------------------------------------------------------ scratch builds

def test_redirect_outputs_moves_every_output(tmp_path, monkeypatch):
    for name in ("MASTER", "INTERVALS", "PERIODIC_HISTORY", "WORK", "RAW_ROWS", "FORM25"):
        monkeypatch.setattr(sm, name, getattr(sm, name))
    form25 = tmp_path / "f25.csv"
    sm.redirect_outputs(tmp_path, form25)
    assert sm.MASTER == tmp_path / "security_master.csv" and sm.INTERVALS == tmp_path / "ticker_intervals.csv"
    assert sm.PERIODIC_HISTORY == tmp_path / "periodic_form_history.csv"
    assert sm.WORK == tmp_path / "derived" and sm.RAW_ROWS == tmp_path / "derived" / "ticker_rows_raw.csv.gz"
    assert sm.FORM25 == form25


def test_the_builder_has_its_own_three_a_second_limiter():
    assert sm.SEC_PER_SECOND == 3 and sm.SEC_LIMITER.windows == {1: 3}
    assert sm.SEC_LIMITER is not sm.common.SEC_LIMITER


# ------------------------------------------------------------------ one ticker-only row against SEC's exchange

def test_one_ticker_only_row_of_an_issuer_listed_elsewhere_is_dropped():
    intervals = pd.DataFrame([
        {"cik": 37996, "ticker": "F", "n_snapshots": 1, "match": "ticker_only"},        # Ford, NYSE: dropped
        {"cik": 37996, "ticker": "F", "n_snapshots": 4, "match": "ticker_only"},        # several rows: kept
        {"cik": 1597033, "ticker": "SABR", "n_snapshots": 1, "match": "ticker_only"},   # SEC says Nasdaq: kept
        {"cik": 5, "ticker": "GONE", "n_snapshots": 1, "match": "ticker_only"},         # listed nowhere now: kept
        {"cik": 6, "ticker": "NAME", "n_snapshots": 1, "match": "name+ticker"}])        # a name match: kept
    profiles = {37996: {"exchanges": ["NYSE", "NYSE"]}, 1597033: {"exchanges": ["Nasdaq"]}, 5: {"exchanges": []},
                6: {"exchanges": ["NYSE"]}}
    assert sm.elsewhere_one_row_intervals(intervals, profiles).tolist() == [True, False, False, False, False]


def test_rows_after_a_transfer_outside_every_interval_do_not_contradict_a_delist_date():
    # Lilis Energy: Nasdaq delisting 2016-08-11, company-list relisting 2017-03..05, NYSE American from
    # 2017-05-18; the nasdaq.com lists kept LLEX to 2017-09 (rows dropped as after_transfer).
    master = pd.DataFrame([{"security_id": "1437557", "cik": "1437557", "delist_date": "2016-08-11",
                            "transfer_date": "2017-05-18"}])
    intervals = pd.DataFrame([
        {"security_id": "1437557", "ticker": "LLEX", "start": "2014-01-22", "end": "2016-05-20", "source": "x"},
        {"security_id": "1437557", "ticker": "LLEX", "start": "2017-03-16", "end": "2017-05-11", "source": "x"}])
    full = ["2016-05-20", "2016-06-04", "2016-09-10", "2016-11-13", "2017-01-30", "2017-03-16", "2017-05-11",
            "2017-06-12", "2017-09-07"]
    rows = pd.DataFrame({"date": ["2016-05-20", "2017-03-16", "2017-05-11", "2017-06-12", "2017-09-07"],
                         "symbol": "LLEX", "cik": 1437557})
    assert sm.delist_date_violations(master, intervals, rows, full).empty
    # Without the move the same rows contradict the delist date.
    assert len(sm.delist_date_violations(master.assign(transfer_date=""), intervals, rows, full)) == 1
