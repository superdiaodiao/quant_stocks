"""Tests for plan step 11, terminal values (scripts/reversal_data_terminal.py).

Synthetic inputs only: no request is sent. The last group checks the built output file when it
exists (the committed input), and is skipped otherwise.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts import reversal_data_terminal as tr


# ------------------------------------------------------------------ reading the terms

CASH_8K = (
    "<p>Item 2.01 Completion of Acquisition. At the effective time of the Merger, each share of common stock, "
    "par value $0.001 per share (&ldquo;Company Common Stock&rdquo;), issued and outstanding immediately prior to the "
    "Effective Time (other than shares owned by Parent) was converted into the right to receive $43.50 in cash, "
    "without interest (the &ldquo; Merger Consideration &rdquo;). Each option to purchase shares was cancelled and "
    "converted into the right to receive $12.00 in cash.</p>")

TENDER_8K = (
    "Purchaser commenced a tender offer (the “Offer”) to acquire all of the outstanding shares of common stock, "
    "par value $0.001 per share, of the Company (“Shares”), at a price of $74.00 per Share, net to the seller in "
    "cash, without interest (the “Offer Price”). Each Share not tendered was converted into the right to receive "
    "an amount in cash equal to the Offer Price.")

MIXED_8K = (
    "each outstanding share of Company common stock, par value $0.001 per share, other than shares subject to "
    "restricted stock awards, was automatically converted into the right to receive the following consideration "
    "(collectively, the \"Merger Consideration\"), without interest: $46.00 in cash (the \"Cash Consideration\"); and "
    "0.2321 of a share of common stock of Analog Devices, par value $0.16 2/3 per share.")

CVR_8K = (
    "Merger Sub commenced a tender offer to acquire all of the outstanding shares of common stock of the Company "
    "for (i) $8.50 per Share, net to the seller in cash (the \"Closing Amount\"), plus (ii) one contingent value right "
    "per Share (a \"CVR\"), which represents the right to receive up to $3.50 per Share upon milestones.")

OTHER_COMPANY_8K = (
    "Pursuant to the Mergers, each share of common stock of Medco (other than shares held by Medco) was converted into "
    "(i) the right to receive $28.80 in cash, without interest, and (ii) 0.81 shares of common stock of the Parent.")


def test_doc_text_drops_tags_and_pads_out_defined_terms():
    text = tr.doc_text(CASH_8K.encode())
    assert "<p>" not in text and '(the "Merger Consideration")' in text
    assert "“" not in text and "  " not in text


def test_money_amounts_skip_par_values_totals_divisors_and_cvr_payments():
    assert tr.money_amounts("common stock, par value $0.001 per share, for $43.50 in cash") == [43.5]
    assert tr.money_amounts("an equity value of $4.4 billion or $21.26 per share") == [21.26]
    assert tr.money_amounts("equal to $45.60 divided by $169.42") == [45.6]
    text = tr.doc_text(CVR_8K.encode())
    assert tr.money_amounts(text) == [8.5]


def test_money_amounts_use_the_context_outside_the_window():
    text = "each share of Company common stock, par value $0.001 per share, was converted"
    start = text.index("$")
    assert tr.money_amounts(text, start, len(text)) == []


@pytest.mark.parametrize("phrase, ratio, acquirer", [
    ('plus (2) 0.897 of a Mallinckrodt ordinary share (the "Stock")', 0.897, "Mallinckrodt"),
    ("0.3101 of a validly issued, fully paid and non-assessable share of common stock, par value $0.01 per share, "
     "of Rockwell Collins", 0.3101, "Rockwell Collins"),
    ('receive 1.0837 (the "Exchange Ratio") shares of voting common stock, par value $0.01 per share, of Schwab',
     1.0837, "Schwab"),
    ("0.2434 of a share of Cigna common stock", 0.2434, "Cigna"),
    ("5.034 Takeda ADSs", 5.034, "Takeda"),
    ("one share of Class A common stock of ANGI", 1.0, "ANGI"),
    ("2.1243 American depositary shares of AstraZeneca", 2.1243, "AstraZeneca"),
])
def test_share_counts_and_the_issuer_named(phrase, ratio, acquirer):
    found = tr.share_amounts(phrase)
    assert found and found[0][0] == pytest.approx(ratio)
    assert tr.acquirer_phrase(found[0][1]) == acquirer


def test_value_ratio_reads_a_stock_part_given_as_a_value():
    ratio, value = tr.value_ratio("a number of shares of UnitedHealth Group common stock, par value $0.01 per share, "
                                  "equal to the amount obtained by dividing $45.60 by $169.42")
    assert ratio == pytest.approx(45.60 / 169.42) and value == 45.6
    assert tr.value_ratio("a number of shares of AbbVie common stock equal to $109.00 divided by the volume weighted "
                          "average price") == (None, 109.0)


def test_cash_merger_lead_ignores_option_clauses():
    leads = tr.conversion_leads(tr.doc_text(CASH_8K.encode()), ["ACME CORP"])
    assert leads and {l["cash"] for l in leads} == {43.5}
    assert tr.best_terms(leads)["cash"] == 43.5


def test_tender_offer_price_is_resolved_from_its_definition():
    terms = tr.best_terms(tr.conversion_leads(tr.doc_text(TENDER_8K.encode()), ["INTERMUNE INC"]))
    assert terms["cash"] == 74.0 and terms["shares"] is None


def test_forward_definition_and_mixed_terms():
    terms = tr.best_terms(tr.conversion_leads(MIXED_8K, ["LINEAR TECHNOLOGY CORP"]))
    assert (terms["cash"], terms["shares"]) == (46.0, pytest.approx(0.2321))
    assert terms["acquirer_phrase"] == "Analog Devices"


def test_cvr_payment_is_not_read_as_cash():
    terms = tr.best_terms(tr.conversion_leads(tr.doc_text(CVR_8K.encode()), ["SAGE THERAPEUTICS"]))
    assert terms["cash"] == 8.5


def test_another_companys_conversion_gives_no_terms():
    leads = tr.conversion_leads(OTHER_COMPANY_8K, ["EXPRESS SCRIPTS INC"])
    assert leads and all(l["subject_owner"] == "other" for l in leads)
    assert tr.best_terms(leads) == {}


def test_best_terms_prefers_the_complete_reading_that_partial_leads_agree_with():
    base = {"acquirer_phrase": "", "cvr": False, "election": False, "snippet": "", "subject_owner": "own"}
    leads = [{**base, "cash": 78.75, "shares": 0.1287, "position": 1}] + \
            [{**base, "cash": 78.75, "shares": None, "position": k} for k in range(2, 7)]
    terms = tr.best_terms(leads)
    assert (terms["cash"], terms["shares"]) == (78.75, 0.1287)


def test_election_between_cash_and_stock_is_marked():
    text = ("each share of Company Common Stock was converted into the right to receive, at the election of the "
            "holder, either (i) $33.06 in cash or (ii) 1.0819 shares of AMC common stock, subject to proration.")
    leads = tr.conversion_leads(text, ["CARMIKE CINEMAS"])
    assert any(l["either_or"] for l in leads)
    mixed_first = ("each share of Company Common Stock was converted into the right to receive, at the election of the "
                   "holder, either (i) 1.2019 shares of Kemper common stock and $51.60 in cash, (ii) $129.00 in cash, "
                   "or (iii) 2.0031 shares of Kemper common stock.")
    assert not any(l["either_or"] for l in tr.conversion_leads(mixed_first, ["INFINITY PROPERTY"]))


def test_transfer_destination_takes_the_first_exchange_named():
    text = ("the Board authorized the transfer of the listing of the Company's Common Stock and its Warrants from "
            "NASDAQ to the New York Stock Exchange (the \"NYSE\") and the NYSE MKT, respectively.")
    assert tr.transfer_destination(text) == "NYSE"
    assert tr.transfer_destination("will transfer its listing to NYSE American LLC") == "NYSE American"


def test_suspension_date():
    text = "Nasdaq notified the Company that trading of its common stock will be suspended at the opening of business on March 3, 2020."
    assert tr.suspension_date(text) == "2020-03-03"


# ------------------------------------------------------------------ scope and filings

def _master(rows):
    columns = ["security_id", "cik", "first_ticker", "name", "share_class", "first_listed", "last_listed", "delist_date",
               "delist_form25_accession", "transfer_date", "transfer_form25_accession", "successor_security_id",
               "successor_date", "former_names", "exchanges_sec_current"]
    return pd.DataFrame([{c: r.get(c, "") for c in columns} for r in rows], dtype=str)


def test_scope_keeps_ranked_or_candidate_securities_whose_nasdaq_listing_ended():
    master = _master([
        {"security_id": "1", "cik": "1", "first_ticker": "AAA", "delist_date": "2019-05-01", "delist_form25_accession": "A1"},
        {"security_id": "2", "cik": "2", "first_ticker": "BBB"},                    # active on Nasdaq
        {"security_id": "3", "cik": "3", "first_ticker": "CCC", "delist_date": "2026-09-20"},  # after the window
        {"security_id": "4", "cik": "4", "first_ticker": "DDD", "last_listed": "2015-01-01"},  # snapshots stop
        {"security_id": "5", "cik": "5", "first_ticker": "EEE", "delist_date": "2018-01-01"},  # neither ranked nor listed
    ])
    facts = pd.DataFrame({"security_id": ["1", "2", "3", "4", "5"], "best_rank": ["10", "5", "20", "400", "900"],
                          "active_nasdaq": ["False", "True", "True", "False", "False"],
                          "last_listed": ["2019-05-01", "2026-08-31", "2026-08-31", "2015-01-01", "2018-01-01"],
                          "last_ticker": ["AAA", "BBB", "CCC", "DDD", "EEE"], "tickers": ["AAA", "BBB", "CCC", "DDD", "EEE"],
                          "last_universe_week": [""] * 5}, dtype=str)
    candidates = pd.DataFrame({"security_id": ["4"]}, dtype=str)
    form25 = pd.DataFrame({"accession": ["A1"], "effective_date": ["2019-05-01"], "filing_date": ["2019-04-21"],
                           "rule_provision": [""], "delisting_basis": ["substituted_merger_or_exchange"],
                           "classification": [""], "subject_exit": [""], "successor_cik": [""], "successor_tickers": [""],
                           "tickers_new": [""], "tickers_ended": [""], "doc_url": ["u"]}, dtype=str)
    scope = tr.terminal_candidates(master, candidates, facts, form25)
    assert list(scope["security_id"]) == ["1", "4"]
    assert scope.set_index("security_id").loc["1", "end_source"] == "form25"
    assert scope.set_index("security_id").loc["4", "end_source"] == "snapshots"


def test_end_filings_kinds_and_windows():
    table = pd.DataFrame({
        "accessionNumber": ["a", "b", "c", "d", "e"],
        "filingDate": ["2019-04-21", "2018-09-01", "2019-04-30", "2019-03-01", "2019-01-01"],
        "reportDate": [""] * 5, "acceptanceDateTime": [""] * 5,
        "form": ["8-K", "8-K", "15-12G", "DEFM14A", "8-K"],
        "items": ["2.01,3.01,5.01", "1.01", "", "", "2.02"],
        "primaryDocument": ["a.htm", "b.htm", "c.htm", "d.htm", "e.htm"], "primaryDocDescription": [""] * 5})
    found = tr.end_filings(table, "2019-05-01", "2019-04-21")
    assert dict(zip(found["accessionNumber"], found["kind"])) == {"a": "closing", "b": "agreement", "c": "deregistration",
                                                                  "d": "proxy"}


# ------------------------------------------------------------------ prices

def _book(daily_rows, tiingo_rows=None):
    daily = pd.DataFrame(daily_rows, columns=["security_id", "date", "close", "volume", "src"])
    daily["date"] = pd.to_datetime(daily["date"])
    status = pd.DataFrame(tiingo_rows or [], columns=["security_id", "ticker_for_source", "status", "prices_path"])
    report = pd.DataFrame(columns=["security_id", "verdict"])
    return tr.PriceBook(daily=daily, tiingo_status=status, yahoo_report=report, yahoo_dir=Path("/nonexistent"))


def test_last_trade_needs_a_vendor_close_near_the_form25_filing():
    book = _book([("1", "2018-03-26", 40.0, 100, "wiki"), ("1", "2018-03-27", 41.0, 100, "wiki"),
                  ("2", "2019-04-18", 49.8, 100, "wiki"), ("2", "2019-04-19", 49.9, 100, "wiki"),
                  ("3", "2019-04-18", 30.0, 100, "wiki"), ("3", "2019-04-19", 30.1, 100, "stored")])
    assert tr.last_trade(book, "1", "2019-04-21", "2019-04-21")["status"] == "vendor_short"  # WIKI ended a year early
    ok = tr.last_trade(book, "2", "2019-04-21", "2019-04-21")
    assert (ok["status"], ok["last_date"], ok["close"]) == ("ok", "2019-04-19", 49.9)
    assert tr.last_trade(book, "3", "2019-04-21", "2019-04-21")["status"] == "vendor_short"  # stored runs later
    assert tr.last_trade(book, "1", "2018-08-01")["status"] == "vendor_short"  # the WIKI end, not a listing end


def test_price_pending_while_the_tiingo_fetch_is_planned():
    book = _book([], [{"security_id": "9", "ticker_for_source": "X", "status": "no_data", "prices_path": ""}])
    candidates = pd.DataFrame({"security_id": ["8", "9", "7"], "planned_source": ["tiingo", "tiingo", "tiingo"],
                               "status": ["pending", "pending", "conditional_tier_c"]})
    assert "in progress" in tr.price_pending("8", candidates, book)
    assert tr.price_pending("9", candidates, book) == ""  # Tiingo answered: nothing more is coming
    assert "month 2" in tr.price_pending("7", candidates, book)


# ------------------------------------------------------------------ classification and values

def _row(**kw):
    base = {"security_id": "1", "cik": "1", "first_ticker": "AAA", "last_ticker": "AAA", "name": "ACME CORP",
            "share_class": "COMMON", "delist_date": "2019-05-01", "transfer_date": "", "last_listed": "2019-04-20",
            "end_date": "2019-05-01", "end_source": "form25", "f25_filing_date": "2019-04-21",
            "f25_delisting_basis": "substituted_merger_or_exchange", "f25_doc_url": "https://www.sec.gov/f25.xml",
            "delist_form25_accession": "A1", "transfer_form25_accession": "", "transfer_basis": "",
            "transfer_form25_doc_url": "", "successor_security_id": "", "exchanges_sec_current": "", "best_rank": 50.0,
            "in_candidates": "N", "former_names": "", "listing_source_url": "https://example.org/listing"}
    base.update(kw)
    return base


def _evidence(sid="1", **kw):
    base = {"security_id": sid, "closing_items": "2.01 3.01", "closing_accessions": "0000000001-19-000001",
            "closing_dates": "2019-04-21", "transfer_to": "", "suspension_date": "", "terms_cash": None,
            "terms_shares": None, "terms_source_url": "https://www.sec.gov/8k.htm", "terms_stage": "closing"}
    base.update(kw)
    return base


def _build(rows, evidence, book, candidates=None):
    scope = pd.DataFrame(rows)
    names = tr.NameIndex(_master([{"security_id": "50", "cik": "50", "name": "TARGETCO HOLDINGS INC",
                                   "first_listed": "2010-01-01", "last_listed": "2026-08-01"}]))
    candidates = candidates if candidates is not None else pd.DataFrame(columns=["security_id", "planned_source", "status"])
    frame, used = tr.build_rows(scope, pd.DataFrame(evidence), book, {}, names, candidates, "2026-10-02")
    return frame.set_index("security_id"), used


def test_cash_merger_value_and_return():
    book = _book([("1", "2019-04-18", 43.0, 100, "tiingo"), ("1", "2019-04-19", 43.2, 100, "tiingo")])
    frame, _ = _build([_row()], [_evidence(terms_cash=43.5)], book)
    row = frame.loc["1"]
    assert (row.terminal_type, row.status, row.consideration_per_share) == ("cash_merger", "computed", "43.5")
    assert float(row.terminal_return) == pytest.approx(43.5 / 43.2 - 1)
    assert row.last_price_date == "2019-04-19" and row.source_url == "https://www.sec.gov/8k.htm"


def test_pending_price_leaves_the_return_blank():
    book = _book([])
    candidates = pd.DataFrame({"security_id": ["1"], "planned_source": ["tiingo"], "status": ["pending"]})
    frame, _ = _build([_row()], [_evidence(terms_cash=43.5)], book, candidates)
    row = frame.loc["1"]
    assert (row.status, row.terminal_return, row.consideration_per_share) == ("pending_price", "", "43.5")


def test_stock_merger_uses_the_acquirers_next_close_and_keeps_levels_local():
    book = _book([("1", "2019-04-19", 20.0, 100, "wiki"), ("50", "2019-04-19", 39.0, 100, "wiki"),
                  ("50", "2019-04-22", 40.0, 100, "wiki")])
    frame, used = _build([_row()], [_evidence(terms_shares=0.5, terms_acquirer_phrase="TargetCo Holdings")], book)
    row = frame.loc["1"]
    assert (row.terminal_type, row.acquirer_security_id, row.status) == ("stock_merger", "50", "computed")
    assert float(row.terminal_return) == pytest.approx(0.5 * 40.0 / 20.0 - 1)
    assert row.consideration_per_share == ""  # a vendor-derived value is not committed
    assert used.set_index("security_id").loc["1", "acquirer_close"] == 40.0


def test_implausible_merger_value_is_held_for_review():
    book = _book([("1", "2019-04-19", 10.0, 100, "wiki")])
    frame, _ = _build([_row()], [_evidence(terms_cash=43.5)], book)
    assert (frame.loc["1", "status"], frame.loc["1", "terminal_return"]) == ("needs_review", "")


def test_exchange_move_has_no_terminal_return():
    book = _book([("1", "2019-04-19", 10.0, 100, "wiki")])
    row = _row(end_source="transfer", delist_date="", transfer_date="2019-05-01", f25_delisting_basis="",
               transfer_form25_doc_url="https://www.sec.gov/t25.xml")
    frame, _ = _build([row], [_evidence(transfer_to="NYSE")], book)
    out = frame.loc["1"]
    assert (out.terminal_type, out.destination_exchange, out.status, out.terminal_return) == \
        ("exchange_move", "NYSE", "no_terminal_return", "")


def test_removal_without_an_otc_price_is_left_to_the_d5_rule():
    book = _book([("1", "2019-02-01", 1.0, 100, "tiingo")])
    row = _row(f25_delisting_basis="exchange_removal")
    frame, _ = _build([row], [_evidence(closing_items="3.01")], book)
    out = frame.loc["1"]
    assert (out.terminal_type, out.status, out.terminal_return) == ("bankruptcy_otc", "awaiting_d5", "")


def test_unknown_end_is_explicit_and_never_defaulted():
    book = _book([("1", "2019-04-19", 10.0, 100, "wiki")])
    frame, _ = _build([_row()], [_evidence(closing_items="", closing_accessions="")], book)
    out = frame.loc["1"]
    assert (out.terminal_type, out.status, out.terminal_return, out.consideration_per_share) == ("unknown", "unknown", "", "")
    assert out.source_url  # the Form 25 that shows the end


def test_successor_link_is_a_one_share_reorganisation_continuing_the_series():
    book = _book([("1", "2019-04-19", 10.0, 100, "wiki"), ("1", "2019-04-22", 10.1, 100, "wiki")])
    row = _row(successor_security_id="60", end_source="snapshots", delist_date="", f25_delisting_basis="",
               f25_filing_date="", end_date="2019-04-20")
    frame, _ = _build([row], [_evidence(closing_items="", closing_accessions="", closing_dates="2019-04-19")], book)
    out = frame.loc["1"]
    assert (out.terminal_type, out.event_subtype, out.consideration_shares) == ("stock_merger", "reorganization", "1")
    assert out.status == "computed" and float(out.terminal_return) == pytest.approx(10.1 / 10.0 - 1)


def test_summary_counts_only():
    frame = pd.DataFrame({c: [""] * 2 for c in tr.OUTPUT_COLUMNS})
    frame["terminal_type"], frame["status"], frame["best_rank"] = ["unknown", "cash_merger"], ["unknown", "computed"], ["3", "1"]
    summary = tr.summarize(frame, pd.DataFrame(), {})
    assert summary["rows_by_terminal_type"] == {"unknown": 1, "cash_merger": 1}
    assert summary["unknown_by_best_rank"][0]["best_rank"] == "3"
    assert summary["returns_aggregated"].startswith("none")
    assert not any("mean" in k or "median" in k for k in summary)


# ------------------------------------------------------------------ reviewed table

def test_reviewed_entries_are_well_formed():
    for sid, review in tr.REVIEWED.items():
        assert set(review) <= set(tr.REVIEW_KEYS) | {"note", "checked"}, sid  # checked: a builder's note for the owner
        assert review.get("note"), sid
        if review.get("approved"):  # an approval that lifts the guard cites its own source
            # a failed bank files with the FDIC, not the SEC (Signature Bank)
            allowed = ("https://www.sec.gov/Archives/edgar/data/",) + (
                ("https://www.fdic.gov/",) if review.get("sub") == "bank_failure" else ())
            assert review.get("url", "").startswith(allowed), sid
        if "type" in review:
            assert review["type"] in tr.TYPES, sid
        if review.get("type") in ("stock_merger", "mixed"):
            assert review.get("shares") is not None or review.get("stock_value") is not None, sid
        if any(k in review for k in ("cash", "shares", "value")):  # changed terms cite the document they come from
            url = review.get("url") or tr.REVIEWED_SOURCES.get(sid, "")
            assert url.startswith("https://www.sec.gov/Archives/edgar/data/"), sid
        if "limit" in review:
            assert pd.Timestamp(review["limit"]).dayofweek < 5, sid
    assert set(tr.REVIEWED_SOURCES) <= set(tr.REVIEWED)


def test_reviewed_acquirers_exist_in_the_master():
    if not tr.MASTER.exists():
        pytest.skip("security master not built")
    ids = set(tr.read_csv_text(tr.MASTER)["security_id"])
    missing = {sid: r["acq"] for sid, r in tr.REVIEWED.items() if r.get("acq") and r["acq"] not in ids}
    assert not missing


# ------------------------------------------------------------------ the built file

@pytest.fixture(scope="module")
def built():
    if not tr.OUTPUT.exists():
        pytest.skip("terminal_returns_2012_2026.csv not built")
    return tr.read_csv_text(tr.OUTPUT)


def test_built_file_columns_and_values(built):
    assert list(built.columns[:12]) == ["ticker", "last_price_date", "terminal_return", "consideration_per_share",
                                        "source_url", "verified_at", "security_id", "delist_date", "terminal_type",
                                        "consideration_cash", "consideration_shares", "acquirer_security_id"]
    assert built["security_id"].is_unique
    assert set(built["terminal_type"]) <= set(tr.TYPES)
    assert set(built["status"]) <= set(tr.STATUSES)
    assert built["source_url"].str.len().gt(0).all()
    has_return = built["terminal_return"].ne("")
    assert (built.loc[has_return, "status"] == "computed").all()
    assert (built.loc[built["status"].eq("computed"), "terminal_return"] != "").all()
    assert built.loc[built["terminal_type"].eq("exchange_move"), "terminal_return"].eq("").all()
    values = pd.to_numeric(built.loc[has_return, "terminal_return"])
    assert (values >= -1.0).all()
    assert not {"close", "close_raw", "last_close", "acquirer_close"} & set(built.columns)  # no vendor levels committed


def test_built_file_unknown_share_is_small(built):
    assert built["terminal_type"].eq("unknown").mean() <= 0.05


# ------------------------------------------------------------------ round 4 regressions (named cases)

def _days(start, n):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]


def _history(sid, start, n, close=50.0, volume=5_000_000, src="tiingo"):
    return [(sid, d, close + 0.01 * i, volume, src) for i, d in enumerate(_days(start, n))]


def test_filler_flags_mark_a_repeated_close_on_a_sliver_of_volume():
    close = np.array([10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.5, 10.5, 10.5])
    volume = np.array([1e6] * 6 + [1.0, 0.0, 1e6])
    flags = tr.filler_flags(close, volume)
    assert list(flags) == [False] * 6 + [True, True, False]  # a real session at the same close is kept


def test_atvi_filler_row_is_not_the_last_session_and_the_acquirer_close_follows_the_real_one():
    # ATVI 2023-10-12 close 94.42 on 7.3M shares; 2023-10-13 close 94.42 on 1 share (Nasdaq halted it before the open)
    rows = _history("1", "2023-09-01", 29, close=94.0, volume=7_000_000)
    rows += [("1", "2023-10-12", 94.42, 7_323_451, "tiingo"), ("1", "2023-10-13", 94.42, 1, "tiingo")]
    rows += [("50", "2023-10-12", 46.9, 1e6, "tiingo"), ("50", "2023-10-13", 47.3, 1e6, "tiingo"),
             ("50", "2023-10-16", 47.6, 1e6, "tiingo")]
    book = _book(rows)
    trade = tr.last_trade(book, "1", "2023-10-16", "2023-10-16")
    assert (trade["status"], trade["last_date"], trade["close"], trade["filler_dropped"]) == ("ok", "2023-10-12", 94.42, 1)
    row = _row(delist_date="2023-10-23", end_date="2023-10-23", f25_filing_date="2023-10-16")
    frame, used = _build([row], [_evidence(terms_shares=2.0, terms_acquirer_phrase="TargetCo Holdings",
                                           closing_dates="2023-10-13")], book)
    out = frame.loc["1"]
    assert (out.last_price_date, out.acquirer_price_date) == ("2023-10-12", "2023-10-13")
    assert float(out.terminal_return) == pytest.approx(2.0 * 47.3 / 94.42 - 1)


def test_halt_stated_in_the_closing_8k_caps_the_last_session():
    text = ("the Company requested that Nasdaq halt trading of the Shares on Nasdaq prior to the open of trading on the "
            "Closing Date and file a Form 25.")
    assert tr.halt_dates(text)["before_open"] == "closing_date"
    text = ("requested that trading of the Common Stock on Nasdaq be suspended prior to the opening of trading on NASDAQ "
            "on September 1, 2021, and that Nasdaq file a Form 25")
    assert tr.halt_dates(text)["before_open"] == "2021-09-01"
    text = "trading of the Common Stock was halted after the close of trading on June 12, 2023 and will be suspended"
    assert tr.halt_dates(text)["after_close"] == "2023-06-12"
    # the halt date caps the window even when the vendor row after it carries real-looking volume
    rows = _history("1", "2021-07-01", 44) + [("1", "2021-09-01", 60.0, 2_000_000, "tiingo")]  # to 2021-08-31
    book = _book(rows)
    row = _row(delist_date="2021-09-13", end_date="2021-09-13", f25_filing_date="2021-09-02")
    frame, used = _build([row], [_evidence(terms_cash=51.0, halt_before_open="2021-09-01")], book)
    assert frame.loc["1", "last_price_date"] == "2021-08-31"
    assert used.set_index("security_id").loc["1", "limit_basis"] == "session_before_halt_stated_in_closing_8k"


def test_sbny_snapshot_end_cuts_before_the_filler_run_and_books_the_first_otc_close():
    # Signature Bank: Nasdaq trade to 2023-03-10 at 70.00; fillers at 70.00 on 0 to 2,605 shares; OTC 0.13 on 03-28
    rows = _history("7", "2023-01-02", 40, close=110.0, volume=1_500_000)
    rows += [("7", "2023-03-10", 70.0, 21_708_250, "tiingo"), ("7", "2023-03-13", 70.0, 2_605, "tiingo")]
    rows += [("7", d, 70.0, 0, "tiingo") for d in _days("2023-03-14", 8)]  # to 2023-03-23
    rows += [("7", "2023-03-24", 70.0, 1, "tiingo"), ("7", "2023-03-27", 70.0, 0, "tiingo"),
             ("7", "2023-03-28", 0.13, 83_639_747, "tiingo")]
    book = _book(rows)
    assert tr.snapshot_cut(book, "7", "2023-03-22", "2023-05-06") == ("2023-03-10", "snapshot_end_cut_before_filler_run")
    row = _row(security_id="7", end_source="snapshots", delist_date="", f25_delisting_basis="", f25_filing_date="",
               end_date="2023-03-22")
    frame, used = _build([row], [_evidence("7", closing_items="", closing_accessions="", closing_dates="", bankruptcy=True)],
                         book)
    out = frame.loc["7"]
    # the OTC close is beyond +/-5% of the last Nasdaq close, so the row waits for a hand check
    assert (out.terminal_type, out.status, out.last_price_date) == ("bankruptcy_otc", "needs_review", "2023-03-10")
    held = used.set_index("security_id").loc["7"]
    assert float(held["terminal_return_checked"]) == pytest.approx(0.13 / 70.0 - 1)
    assert out.consideration_per_share == ""  # the OTC level stays local
    assert used.set_index("security_id").loc["7", "otc_date"] == "2023-03-28"


def test_sbny_reviewed_entry_dates_the_last_trade_and_cites_the_fdic():
    review = tr.REVIEWED["1288784"]
    assert review["limit"] == "2023-03-10" and review["type"] == "bankruptcy_otc"
    assert review["url"].startswith("https://www.fdic.gov/")


def _angi_names():
    return tr.NameIndex(_master([{"security_id": "1705110", "cik": "1705110", "first_ticker": "ANGI",
                                  "name": "ANGI HOMESERVICES INC", "first_listed": "2017-10-08", "last_listed": "2026-07-01"}]))


def test_angi_uses_its_last_day_and_the_same_ticker_next_close_not_a_late_acquirer_quote():
    # Angie's List 2017-09-29 close 12.46; ANGI rows from 2017-10-02 are ANGI Homeservices under the same ticker;
    # the acquirer's own first book row is 2017-10-09 (6 sessions later)
    rows = _history("1491778", "2017-08-01", 30, close=12.0, volume=1_000_000, src="wiki")
    rows += [("1491778", "2017-09-29", 12.46, 2_367_959, "wiki"), ("1491778", "2017-10-02", 12.76, 850_652, "wiki"),
             ("1491778", "2017-10-03", 12.57, 763_699, "wiki"), ("1705110", "2017-10-09", 11.55, 181_927, "wiki")]
    book = _book(rows)
    row = _row(security_id="1491778", last_ticker="ANGI", first_ticker="ANGI", end_source="snapshots", delist_date="",
               f25_delisting_basis="", f25_filing_date="", end_date="2017-09-30")
    ev = _evidence("1491778", closing_dates="2017-10-02")
    scope = pd.DataFrame([row])
    frame, used = tr.build_rows(scope, pd.DataFrame([ev]), book, {}, _angi_names(),
                                pd.DataFrame(columns=["security_id", "planned_source", "status"]), "2026-10-02")
    out = frame.set_index("security_id").loc["1491778"]
    # the reviewed terms carry a cash election ($8.50, capped): the guard holds the value until a review approves it
    assert (out.last_price_date, out.acquirer_price_date, out.status) == ("2017-09-29", "2017-10-02", "needs_review")
    level = used.set_index("security_id").loc["1491778"]
    assert float(level.terminal_return_checked) == pytest.approx(12.76 / 12.46 - 1)  # about +2.4%, not -9.5%
    assert out.terminal_return == "" and "election" in level.review_reasons
    assert "same ticker" in out.acquirer_match
    assert out.acquirer_name == "ANGI HOMESERVICES INC"


def test_acquirer_close_two_sessions_late_is_held_for_review():
    # BRCM-like: last trade on a Friday, the acquirer's first close on Tuesday
    book = _book([("1", "2016-01-29", 50.0, 100, "wiki"), ("50", "2016-02-02", 40.0, 100, "wiki")])
    frame, used = _build([_row(f25_filing_date="2016-02-01")],
                         [_evidence(terms_shares=1.2, terms_acquirer_phrase="TargetCo Holdings")], book)
    out = frame.loc["1"]
    assert (out.status, out.terminal_return) == ("needs_review", "")
    assert "2 XNAS sessions" in out.status_note
    assert used.set_index("security_id").loc["1", "acquirer_gap_sessions"] == 2


def test_acas_book_quote_years_late_falls_back_to_the_yahoo_chart(monkeypatch):
    # ARCC's book rows start in 2022; the ARCC chart has 2017-01-04 close 16.97
    book = _book([("817473", "2017-01-03", 17.99, 37_662_542, "wiki"), ("1287750", "2022-06-08", 20.0, 1e6, "tiingo")])
    chart = pd.DataFrame({"date": pd.to_datetime(["2017-01-03", "2017-01-04"]), "close": [16.5, 16.97],
                          "volume": [5e6, 6.8e6], "src": "yahoo_acquirer"})
    monkeypatch.setattr(tr, "acquirer_chart", lambda symbol: chart if symbol == "ARCC" else chart.iloc[:0])
    row = _row(security_id="817473", f25_filing_date="2017-01-04")
    frame, used = _build([row], [_evidence("817473")], book)
    out = frame.loc["817473"]
    assert (out.status, out.acquirer_price_date) == ("computed", "2017-01-04")
    assert float(out.terminal_return) == pytest.approx((10.13 + 0.483 * 16.97) / 17.99 - 1)
    assert "yahoo chart ARCC" in out.acquirer_match


def test_same_series_rename_prefers_its_own_next_close_on_a_tie():
    # LMCA -> FWONA: WIKI LMCA 2017-01-24 vs the other vendor's FWONA close on the same day
    book = _book([("1", "2017-01-23", 29.66, 480_352, "wiki"), ("1", "2017-01-24", 30.10, 359_080, "wiki"),
                  ("50", "2017-01-24", 29.337, 369_668, "yahoo_step7")])
    row = _row(end_source="snapshots", delist_date="", f25_delisting_basis="", f25_filing_date="", end_date="2017-01-20")
    ev = _evidence(closing_dates="2017-01-23", terms_shares=1.0, terms_acquirer_phrase="TargetCo Holdings",
                   reorganization=True)
    frame, _ = _build([row], [ev], book)
    out = frame.loc["1"]
    assert out.event_subtype == "reorganization"
    assert float(out.terminal_return) == pytest.approx(30.10 / 29.66 - 1)
    # round 10: the 8-A12B/A filed after the close on 2017-01-24 dates the symbol change (WIKI's last LMCA row)
    assert tr.REVIEWED["1560385.T-LMCA"]["limit"] == tr.REVIEWED["1560385.T-LMCK"]["limit"] == "2017-01-24"
    assert tr.REVIEWED["1560385.T-LMCA"]["url"].endswith("1560385/000110465917003788/a17-3007_18a12ba.htm")


def test_orcl_exchange_move_ends_on_the_last_nasdaq_session():
    text = ("Subject to the approval by the NYSE of Oracle's listing application, Oracle expects that its common stock "
            "will begin trading on the NYSE on July 15, 2013.")
    assert tr.new_exchange_starts(text) == ["2013-07-15"]
    assert tr.new_exchange_starts("On February 17, 2026, the Common Stock will begin trading on The New York Stock "
                                  "Exchange under the new trading symbol") == ["2026-02-17"]
    assert tr.new_exchange_starts("Class A common stock is expected to begin trading on the Nasdaq under the ticker "
                                  "symbol \"PR\" on September 2, 2022") == []  # a new ticker on Nasdaq, not a move
    rows = [("1", d, 30.0 + 0.1 * i, 1e7, "wiki") for i, d in enumerate(_days("2013-07-08", 11))]  # to 2013-07-22
    book = _book(rows)
    row = _row(end_source="transfer", delist_date="", transfer_date="2013-07-22", end_date="2013-07-22",
               f25_delisting_basis="", f25_filing_date="", transfer_filing_date="2013-07-12", transfer_basis="issuer_withdrawal",
               transfer_form25_doc_url="https://www.sec.gov/t25.htm")
    frame, used = _build([row], [_evidence(transfer_to="NYSE", transfer_start="2013-07-15")], book)
    out = frame.loc["1"]
    assert (out.terminal_type, out.status, out.last_price_date, out.destination_start_date) == \
        ("exchange_move", "no_terminal_return", "2013-07-12", "2013-07-15")
    # without a stated start: the transfer Form 25 filing date
    frame, _ = _build([row], [_evidence(transfer_to="NYSE")], book)
    assert frame.loc["1", "last_price_date"] == "2013-07-12" and frame.loc["1", "destination_start_date"] == ""


def test_load_existing_keeps_each_note_on_its_own_row(tmp_path):
    path = tmp_path / "terminal_returns.csv"
    pd.DataFrame({"ticker": ["XLNX", "ESRX", "KRFT"],
                  "last_price_date": ["2022-02-11", "2018-12-20", "2015-07-02"],
                  "terminal_return": ["0.01", "0.02", "0.03"], "consideration_per_share": ["196.9", "92.5", "89.46"],
                  "source_url": ["u1", "u2", "u3"], "verified_at": ["2026-01-01"] * 3,
                  "note": ["1.7234 AMD (114.27 close 2022-02-14)", "$48.75 cash + 0.2434 CI (179.80 close 2018-12-20)",
                           "1 KHC share (72.96, first close 2015-07-06) + $16.50 special dividend"]}).to_csv(path, index=False)
    loaded = tr.load_existing([path]).set_index("ticker")
    assert loaded.loc["ESRX", "note"].startswith("$48.75 cash + 0.2434 CI")
    assert loaded.loc["XLNX", "note"].startswith("1.7234 AMD")
    assert loaded.loc["KRFT", "note"].startswith("1 KHC share")
    assert tr.strip_levels(loaded.loc["ESRX", "note"]) == "$48.75 cash + 0.2434 CI"
    assert tr.strip_levels(loaded.loc["KRFT", "note"]) == "1 KHC share + $16.50 special dividend"
    assert tr.strip_levels("$17.50 cash + 0.2582 LBTYA (76.24) + 0.1928 LBTYK (71.51), closes 2013-06-07") == \
        "$17.50 cash + 0.2582 LBTYA + 0.1928 LBTYK"


def test_existing_stock_leg_value_commits_the_return_but_no_level():
    book = _book([("1", "2019-11-20", 103.0, 1e6, "wiki")])
    prior = {"existing_file": "holdout supplement", "terminal_return": 0.033, "consideration_per_share": 106.41,
             "source_url": "https://www.sec.gov/celg.htm", "verified_at": "2026-01-01",
             "note": "$50 cash + 1 BMY (56.41 close 2019-11-20) + 1 CVR valued at 0"}
    names = tr.NameIndex(_master([]))
    frame, used = tr.build_rows(pd.DataFrame([_row(f25_filing_date="2019-11-21")]),
                                pd.DataFrame([_evidence(terms_cash=50.0, terms_shares=1.0, terms_acquirer_phrase="Bristol")]),
                                book, {"1": prior}, names, pd.DataFrame(columns=["security_id", "planned_source", "status"]),
                                "2026-10-02")
    out = frame.set_index("security_id").loc["1"]
    assert out.status == "computed" and float(out.terminal_return) == pytest.approx(106.41 / 103.0 - 1)
    assert out.consideration_per_share == "" and out.existing_consideration_per_share == ""
    assert "56.41" not in out.status_note and "$50 cash + 1 BMY" in out.status_note
    assert used.set_index("security_id").loc["1", "existing_value"] == 106.41


def test_existing_placeholder_at_the_last_close_is_not_a_value():
    book = _book([("1", "2013-09-30", 22.93, 1e6, "wiki")])
    prior = {"existing_file": "repo", "terminal_return": 0.0, "consideration_per_share": 22.93, "source_url": "u",
             "verified_at": "2026-01-01", "note": "0.160 Actavis plc share; no Actavis price available, history ends at the last close"}
    frame, _ = tr.build_rows(pd.DataFrame([_row(f25_filing_date="2013-10-01")]),
                             pd.DataFrame([_evidence(terms_shares=0.16, terms_acquirer_phrase="Actavis")]),
                             book, {"1": prior}, tr.NameIndex(_master([])),
                             pd.DataFrame(columns=["security_id", "planned_source", "status"]), "2026-10-02")
    out = frame.set_index("security_id").loc["1"]
    assert (out.status, out.terminal_return, out.consideration_per_share) == ("needs_acquirer_price", "", "")


def test_unreviewed_cash_or_stock_election_is_marked_as_an_election():
    book = _book([("1", "2019-04-19", 20.0, 100, "wiki")])
    frame, _ = _build([_row()], [_evidence(terms_cash=22.0, terms_shares=1.3284, terms_either_or="True",
                                           terms_acquirer_phrase="Brookline")], book)
    assert frame.loc["1", "event_subtype"] == "election"


def test_reviewed_hold_keeps_a_computed_value_for_review():
    review = tr.REVIEWED["1599901"]
    assert review["cash"] == 72.0 and "Atrium" in review["hold"]
    book = _book([("1599901", "2026-02-26", 71.9, 1e6, "tiingo_step8")])
    frame, _ = _build([_row(security_id="1599901", f25_filing_date="2026-02-27")], [_evidence("1599901")], book)
    out = frame.loc["1599901"]
    assert (out.status, out.terminal_return) == ("needs_review", "")
    assert "Atrium" in out.status_note


def test_ttph_cites_the_la_jolla_closing_8k():
    assert tr.REVIEWED["1373707"]["url"].endswith("1373707/000119312520201936/d93682d8k.htm")


def test_built_file_named_cases(built):
    rows = built.set_index("security_id")
    expect = {"1288784": ("bankruptcy_otc", "2023-03-10"), "718877": ("cash_merger", "2023-10-12"),
              "1341439": ("exchange_move", "2013-07-12"), "1491778": ("stock_merger", "2017-09-29")}
    for sid, (kind, last) in expect.items():
        if sid in rows.index:
            assert (rows.loc[sid, "terminal_type"], rows.loc[sid, "last_price_date"]) == (kind, last), sid
    if "1288784" in rows.index:
        assert rows.loc["1288784", "status"] == "computed"
    if "817473" in rows.index:
        assert rows.loc["817473", "status"] == "computed" and rows.loc["817473", "acquirer_price_date"] == "2017-01-04"
    # the copied notes are the row's own deal (ESRX's is the Cigna deal), with no vendor levels
    if "1532063" in rows.index and rows.loc["1532063", "existing_file"]:
        assert "$42.00" not in rows.loc["1532063", "status_note"]
    noted = built[built["consideration_value_basis"].str.startswith("existing_row_value_stock_leg")]
    assert noted["consideration_per_share"].eq("").all()
    assert not noted["status_note"].str.contains(r"\d+\.\d+ close \d{4}-\d{2}-\d{2}").any()


# ------------------------------------------------------------------ round 5 regressions (named cases)

def test_halt_after_close_reads_close_of_business_without_the_and_a_form25_filed_after_the_close():
    endp = "Endo's common shares were suspended from trading on the NASDAQ as of close of business on February 28, 2014."
    assert tr.halt_dates(endp)["after_close"] == "2014-02-28"
    avgo = ("the NASDAQ filed a Form 25 with the SEC after the close of trading on January 29, 2016 to withdraw Avago "
            "Ordinary Shares from listing")
    assert tr.halt_dates(avgo)["after_close"] == "2016-01-29"
    # 21CF: 'prior to the open of trading on the Merger Effective Date' is dated by the 8-K's filing day
    fox = ("The shares of 21CF Common Stock were suspended from trading on Nasdaq prior to the open of trading on the "
           "Merger Effective Date.")
    assert tr.halt_dates(fox)["before_open"] == "closing_date"


def test_effective_time_after_the_close():
    # the LILAK closing 8-K's own words (0001570585-18-000013): no 'effective', but 'On <date> at 5:00 p.m.' and 'completed'
    lilak = ('Item 2.01. Completion of Acquisition or Disposition of Assets On December 29, 2017 at 5:00 p.m., New York '
             'City time (the "Distribution Date"), Liberty Global plc (the "Company") completed its previously-announced '
             'split-off (the "Split-Off") of its former wholly-owned subsidiary Liberty Latin America Ltd. ("Splitco").')
    assert tr.effective_after_close(lilak) == "2017-12-29"
    assert tr.effective_after_close('On June 2, 2016 at 6:00 p.m., Eastern Time, the merger was consummated') == "2016-06-02"
    assert tr.effective_after_close("On March 1, 2016 at 5:00 p.m., the board of directors met") == ""  # no completion
    assert tr.effective_after_close("the meeting on March 1, 2016 at 5:00 p.m., when the deal was completed") == ""
    sohu = 'On May 31, 2018 at 4:30 PM Eastern Daylight Time (such date and time, the "Effective Time"), Sohu Delaware'
    assert tr.effective_after_close(sohu) == "2018-05-31"
    ozrk = ('consummated by the filing of articles of merger, effective as of 4:00 p.m., Central Time, on June 26, 2017 '
            '(the "Effective Time")')
    assert tr.effective_after_close(ozrk) == "2017-06-26"
    split = ("on March 8, 2018, at 4:21 p.m., New York City time, pursuant to the terms of the effective charter. On "
             'March 9, 2018, at 4:01 p.m., New York City time (the "Split-Off Effective Time"), Liberty completed')
    assert tr.effective_after_close(split) == "2018-03-09"  # the latest stated effective time
    assert tr.effective_after_close("the earnings call, scheduled for November 4, 2015, at 3:30 p.m. Central Time") == ""
    assert tr.effective_after_close("effective as of 12:01 p.m. on June 26, 2017 (the Effective Time)") == ""  # noon
    assert tr.effective_after_close("The Offer expired at 5:00 p.m., New York City time, on March 1, 2016.") == ""


def test_successor_starts():
    catm = ('The Ordinary Shares were approved for listing on NASDAQ and will begin trading on July 1, 2016 under the '
            'symbol "CATM," the same symbol')
    assert tr.successor_starts(catm) == ["2016-07-01"]
    dkng = ("As of the open of trading on May 5, 2022, shares of New DraftKings Class A Common Stock will trade on The "
            "Nasdaq Global Select Market under the ticker symbol \"DKNG.\"")
    assert tr.successor_starts(dkng) == ["2022-05-05"]
    sohu = ("Sohu Delaware expects trading in the ADSs representing Sohu Cayman ordinary shares on the NASDAQ Global "
            "Select Market to commence on June 1, 2018.")
    assert tr.successor_starts(sohu) == ["2018-06-01"]
    assert tr.successor_starts("the new shares will begin trading on a when-issued basis on March 1, 2019.") == []


def _successor_names(pred="1", succ="50", ticker="AAA", day="2012-11-30"):
    return tr.NameIndex(_master([
        {"security_id": pred, "cik": pred, "first_ticker": ticker, "name": "ACME CORP", "first_listed": "2010-01-01",
         "last_listed": day, "successor_security_id": succ, "successor_date": day},
        {"security_id": succ, "cik": succ, "first_ticker": ticker, "name": "ACME HOLDINGS LTD",
         "first_listed": "2012-12-10", "last_listed": "2026-08-01"}]))


def test_successor_reorganisation_ends_on_the_master_successor_date_not_on_the_successors_sessions():
    # SSYS: Form 25 and successor_date 2012-11-30 (74.95); Stratasys Ltd trades as SSYS from 2012-12-03 (70.05);
    # the snapshots end 2012-11-13 and no closing 8-K is in the window, so the window ran to 2012-12-28
    rows = _history("1", "2012-10-01", 44, close=78.0, volume=600_000, src="wiki")  # to 2012-11-29
    rows += [("1", "2012-11-30", 74.95, 605_500, "wiki"), ("1", "2012-12-03", 73.05, 3_621_700, "wiki")]
    rows += [("1", d, 72.0, 500_000, "wiki") for d in _days("2012-12-04", 19)]  # to 2012-12-28
    book = _book(rows)
    row = _row(end_source="snapshots", delist_date="", f25_delisting_basis="", f25_filing_date="", end_date="2012-11-13",
               successor_security_id="50", successor_date="2012-11-30")
    ev = _evidence(closing_items="", closing_accessions="", closing_dates="")
    frame, used = tr.build_rows(pd.DataFrame([row]), pd.DataFrame([ev]), book, {}, _successor_names(),
                                pd.DataFrame(columns=["security_id", "planned_source", "status"]), "2026-10-02")
    out = frame.set_index("security_id").loc["1"]
    assert (out.terminal_type, out.event_subtype, out.status) == ("stock_merger", "reorganization", "computed")
    assert (out.last_price_date, out.acquirer_price_date) == ("2012-11-30", "2012-12-03")
    assert float(out.terminal_return) == pytest.approx(73.05 / 74.95 - 1)
    assert used.set_index("security_id").loc["1", "limit_basis"] == "successor_form25_filing"
    assert tr.successor_overruns(frame, pd.DataFrame([row])) == []
    # the old reading (no successor date) books the successor's December move: the check flags such a row
    stale = frame.copy()
    stale["last_price_date"] = "2012-12-28"
    assert tr.successor_overruns(stale, pd.DataFrame([row])) == ["1"]


def test_lilak_effective_time_and_catm_successor_start_cap_the_closing_8k_date():
    rows = _history("1", "2016-05-16", 33, close=38.0, volume=300_000, src="wiki")  # to 2016-06-29
    rows += [("1", "2016-06-30", 39.81, 288_123, "wiki"), ("1", "2016-07-01", 40.11, 167_786, "wiki"),
             ("1", "2016-07-05", 40.80, 397_817, "wiki")]
    book = _book(rows)
    row = _row(end_source="snapshots", delist_date="", f25_delisting_basis="", f25_filing_date="", end_date="2016-07-07",
               successor_security_id="50", successor_date="2016-07-13")
    ev = _evidence(closing_dates="2016-07-01", successor_start="2016-07-01")
    frame, used = tr.build_rows(pd.DataFrame([row]), pd.DataFrame([ev]), book, {}, _successor_names(day="2016-07-13"),
                                pd.DataFrame(columns=["security_id", "planned_source", "status"]), "2026-10-02")
    out = frame.set_index("security_id").loc["1"]
    assert (out.last_price_date, out.acquirer_price_date) == ("2016-06-30", "2016-07-01")
    assert float(out.terminal_return) == pytest.approx(40.11 / 39.81 - 1)
    assert used.set_index("security_id").loc["1", "limit_basis"] == "session_before_stated_successor_start"
    # an effective time after the close of 2016-06-30 caps the window the same way
    ev = _evidence(closing_dates="2016-07-05", effective_after_close="2016-06-30")
    frame, used = tr.build_rows(pd.DataFrame([row]), pd.DataFrame([ev]), book, {}, _successor_names(day="2016-07-13"),
                                pd.DataFrame(columns=["security_id", "planned_source", "status"]), "2026-10-02")
    assert frame.set_index("security_id").loc["1", "last_price_date"] == "2016-06-30"
    assert used.set_index("security_id").loc["1", "limit_basis"] == "effective_time_after_close_stated_in_closing_8k"


def test_qdel_and_apa_reviewed():
    qdel = tr.REVIEWED["353569"]
    assert qdel["limit"] == "2022-05-26" and qdel["url"].endswith("1906324/000119312522161806/d323352d8k12b.htm")
    assert "353569" not in tr.REVIEWED_SOURCES  # the reviewed url names the successor's 8-K12B
    assert "effective time" in tr.REVIEWED["6769"]["hold"]
    # round 10: Sinclair's closing 8-K does state the time (12:00 am ET on 2023-06-01): no hold, last session 05-31
    sbgi = tr.REVIEWED["912752"]
    assert "hold" not in sbgi and not sbgi.get("hold_last_session") and sbgi["limit"] == "2023-05-31"
    assert "12:00 am" in sbgi["note"] and sbgi["url"].endswith("912752/000119312523158935/d530850d8k.htm")


def test_special_dividend_paid_at_the_closing_is_part_of_the_value():
    # NGHC: $32.00 cash plus a special pre-closing dividend of $2.50; last close 34.18 still carries it
    book = _book([("1578735", "2020-12-30", 34.15, 3_485_901, "tiingo_step8"),
                  ("1578735", "2020-12-31", 34.18, 4_683_520, "tiingo_step8")])
    row = _row(security_id="1578735", delist_date="2021-01-14", end_date="2021-01-14", f25_filing_date="2021-01-04")
    frame, used = _build([row], [_evidence("1578735", terms_cash=32.0, special_dividend=True)], book)
    out = frame.loc["1578735"]
    assert (out.status, out.consideration_cash, out.consideration_per_share) == ("computed", "32", "34.5")
    assert (out.special_dividend_cash, out.consideration_value_basis) == ("2.5", "sec_cash_terms + special_dividend_at_closing")
    assert float(out.terminal_return) == pytest.approx(34.50 / 34.18 - 1)
    for sid, total in {"826083": 13.88, "1756497": 27.75, "1581164": 20.50}.items():
        review = tr.REVIEWED[sid]
        assert review["cash"] + review["special_dividend"] == pytest.approx(total), sid


def test_unreviewed_special_dividend_in_the_closing_8k_holds_the_row():
    book = _book([("1", "2019-04-19", 43.2, 100, "tiingo")])
    frame, _ = _build([_row()], [_evidence(terms_cash=43.5, special_dividend=True)], book)
    out = frame.loc["1"]
    assert (out.status, out.terminal_return) == ("needs_review", "")
    assert "special dividend" in out.status_note
    assert tr.SPECIAL_DIVIDEND.search("plus a special pre-closing dividend of $2.50")
    assert tr.SPECIAL_DIVIDEND.search("declared a one-time special dividend of $2.00 in cash")
    assert not tr.SPECIAL_DIVIDEND.search("the regular quarterly dividend of $0.05")
    assert tr.REVIEWED["1470215"]["special_dividend_before_last_trade"]  # TW: went ex before the last trade


def test_vmed_legs_use_the_same_ticker_predecessor_series_after_its_successor_date():
    # VMED last trade 2013-06-07; Liberty Global plc (60, 61) has no own rows until 2013-06-24, but the old Liberty
    # Global Inc series (40, 41) carries the plc's closes under LBTYA/LBTYK from 2013-06-10
    rows = [("1", "2013-06-06", 50.5, 1e6, "tiingo"), ("1", "2013-06-07", 51.0, 1e6, "tiingo"),
            ("40", "2013-06-07", 76.24, 1e6, "wiki"), ("40", "2013-06-10", 74.18, 1e6, "wiki"),
            ("41", "2013-06-07", 71.51, 1e6, "tiingo"), ("41", "2013-06-10", 69.44, 1e6, "tiingo"),
            ("60", "2013-06-24", 70.0, 1e6, "wiki"), ("61", "2013-06-24", 66.0, 1e6, "wiki")]
    book = _book(rows)
    names = tr.NameIndex(_master([
        {"security_id": "40", "first_ticker": "LBTYA", "successor_security_id": "60", "successor_date": "2013-06-07"},
        {"security_id": "41", "first_ticker": "LBTYK", "successor_security_id": "61", "successor_date": "2013-06-07"},
        {"security_id": "60", "first_ticker": "LBTYA"}, {"security_id": "61", "first_ticker": "LBTYK"}]))
    quote = tr.acquirer_close(book, names, "60", pd.Timestamp("2013-06-07"))
    assert (quote["date"], quote["close"], quote["via"]) == (pd.Timestamp("2013-06-10"), 74.18, "40")
    assert tr.acquirer_close(book, names, "60", pd.Timestamp("2013-06-07"), target="40")["close"] == 70.0  # never itself
    scope = pd.DataFrame([_row(f25_filing_date="2013-06-07", delist_date="2013-06-17", end_date="2013-06-17")])
    ev = pd.DataFrame([_evidence(terms_cash=17.5, terms_shares=0.2582)])
    original = tr.REVIEWED.get("1")
    tr.REVIEWED["1"] = {"type": "mixed", "cash": 17.50, "shares": 0.2582, "acq": "60", "extra": [(0.1928, "61")],
                        "note": "synthetic VMED"}
    try:
        frame, _ = tr.build_rows(scope, ev, book, {}, names, pd.DataFrame(columns=["security_id", "planned_source", "status"]),
                                 "2026-10-02")
    finally:
        tr.REVIEWED.pop("1")
        if original is not None:
            tr.REVIEWED["1"] = original
    out = frame.set_index("security_id").loc["1"]
    assert (out.status, out.acquirer_price_date) == ("computed", "2013-06-10")
    assert float(out.terminal_return) == pytest.approx((17.50 + 0.2582 * 74.18 + 0.1928 * 69.44) / 51.0 - 1)
    assert "40 series (same ticker)" in out.acquirer_match


def test_msg_exchange_move_without_a_vendor_session_near_the_transfer_leaves_the_last_session_blank():
    rows = [("1", d, 80.0 + 0.1 * i, 1e6, "wiki") for i, d in enumerate(_days("2015-04-01", 27))]  # to 2015-05-07
    book = _book(rows)
    row = _row(end_source="transfer", delist_date="", transfer_date="2015-08-03", end_date="2015-08-03",
               f25_delisting_basis="", f25_filing_date="", transfer_filing_date="2015-07-24", transfer_basis="issuer_withdrawal",
               transfer_form25_doc_url="https://www.sec.gov/t25.htm")
    frame, used = _build([row], [_evidence(transfer_to="NYSE", transfer_start="2015-07-27")], book)
    out = frame.loc["1"]
    assert (out.terminal_type, out.status, out.last_price_date, out.destination_start_date) == \
        ("exchange_move", "no_terminal_return", "", "2015-07-27")
    handoff = tr.reconcile_handoff(frame.reset_index(), used)
    assert handoff.set_index("security_id").loc["1", "kind"] == "exchange_move_vendor_gap"


def test_reconcile_handoff_reports_a_special_dividend_the_series_books_early(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "CANONICAL_PRICES", tmp_path)
    pd.DataFrame({"date": ["2022-09-27", "2022-09-28", "2022-09-30"], "div_cash": [0, 2.0, 0]}).to_csv(
        tmp_path / "1756497.csv", index=False)
    frame = pd.DataFrame([{c: "" for c in tr.OUTPUT_COLUMNS} | {
        "security_id": "1756497", "ticker": "CHNG", "terminal_type": "cash_merger", "last_price_date": "2022-09-30",
        "special_dividend_cash": "2", "special_dividend_record_date": "2022-10-03", "source_url": "u"}])
    out = tr.reconcile_handoff(frame, pd.DataFrame(columns=["security_id"])).iloc[0]
    assert (out.kind, out.series_booking_date) == ("special_dividend_in_terminal_value", "2022-09-28")
    assert out.action.startswith("remove the booking")


def test_built_file_successor_reorganisations_end_on_the_predecessors_last_session(built):
    rows = built.set_index("security_id")
    expect = {"1100962": "2014-02-28", "1104188": "2018-05-31", "353569": "2022-05-26", "915735": "2012-11-30",
              "1570585.T-LILAK": "2017-12-29", "1570585.T-LILA": "2017-12-29", "1441634": "2016-01-29",
              "1277856": "2016-06-30", "1772757": "2022-05-04", "1038205": "2017-06-26"}
    for sid, last in expect.items():
        if sid in rows.index and rows.loc[sid, "last_price_date"]:
            assert rows.loc[sid, "last_price_date"] == last, sid
    if "6769" in rows.index:
        assert rows.loc["6769", "status"] != "computed"  # APA: no effective time stated
    scope_path = tr.OUT / "scope.csv"
    if scope_path.exists():
        assert tr.successor_overruns(built, tr.read_csv_text(scope_path)) == []


def test_built_file_special_dividends_are_in_the_value(built):
    rows = built.set_index("security_id")
    expect = {"1578735": "34.5", "826083": "13.88", "1756497": "27.75", "1581164": "20.5"}
    for sid, value in expect.items():
        if sid in rows.index:
            assert rows.loc[sid, "consideration_per_share"] == value, sid
            assert rows.loc[sid, "special_dividend_cash"] != "", sid


# ------------------------------------------------------------------ round 6 regressions (named cases)

def test_rename_and_successor_limits_without_a_master_successor_link_are_reviewed():
    # QRTEA/QRTEB -> QVCGA/QVCGB from the open of 2025-02-24 (8-K 0001104659-25-016368); VNOM: 12:01 a.m. on
    # 2025-08-19, new shares began trading that day (closing 8-K 0001193125-25-183040)
    for sid in ("1355096.T-QRTEA", "1355096.T-QRTEB"):
        review = tr.REVIEWED[sid]
        assert review["limit"] == "2025-02-21" and review["url"].endswith("000110465925016368/tm257272d1_8k.htm"), sid
    vnom = tr.REVIEWED["1602065"]
    assert vnom["limit"] == "2025-08-18" and vnom["url"].endswith("1602065/000119312525183040/d65540d8k.htm")
    assert "1602065" not in tr.REVIEWED_SOURCES


def test_successor_starts_with_the_date_before_a_past_tense_verb_and_without_the():
    vnom = ("As a result of the Mergers, all shares of Former Viper Common Stock were cancelled, and New Viper became the "
            "successor to Former Viper. Accordingly, on August 19, 2025, New Viper Class A Common Stock began trading on "
            "Nasdaq in place of Former Viper Class A Common Stock under the ticker symbol \"VNOM\".")
    assert tr.successor_starts(vnom) == ["2025-08-19"]
    qrte = ("The Company's Series A common stock ... previously traded on the Nasdaq Stock Market LLC (\"Nasdaq\") under "
            "the ticker symbols \"QRTEA\", \"QRTEB\" and \"QRTEP\", respectively and, effective as of open of trading on "
            "February 24, 2025, will trade on Nasdaq under the new ticker symbols \"QVCGA\", \"QVCGB\" and \"QVCGP\"")
    assert tr.successor_starts(qrte) == ["2025-02-24"]
    assert tr.successor_starts("The new shares commenced trading on Nasdaq on June 3, 2019.") == ["2019-06-03"]
    # a future verb with the date before it is not read; nor a date that only precedes another date
    assert tr.successor_starts("On May 1, 2019, the Company said the new shares will begin trading.") == []
    assert tr.successor_starts("On January 4, 2016, and December 1, 2015 the shares began trading") == []
    assert tr.successor_starts("On March 1, 2019, the when-issued shares began trading.") == []


def test_midnight_effective_time_caps_the_last_session_at_the_session_before():
    vnom = ('at the effective time of the Viper Pubco Merger (the "Viper Pubco Merger Effective Time", which was 12:01 '
            'a.m., Eastern Time, on August 19, 2025), (A) each share of Former Viper\'s Class A common stock')
    assert tr.effective_before_open(vnom) == "2025-08-19"
    assert tr.effective_before_open('On June 3, 2019 at 12:01 a.m., Eastern Time (the "Effective Time"), the merger was '
                                    'completed') == "2019-06-03"
    assert tr.effective_before_open("The Offer expired at 12:01 a.m., New York City time, on March 2, 2016, and the "
                                    "merger became effective") == ""  # a tender offer's expiry
    fox = ("Following the Separation, effective at 7:25 a.m. Eastern Time on March 19, 2019 (the \"Closing Date\"), 21CF "
           "distributed all of the issued and outstanding common stock of FOX")
    assert tr.effective_before_open(fox) == ""  # only 12 o'clock a.m.; 21CF traded on 2019-03-19
    rows = _history("1", "2025-07-01", 35, close=30.0, volume=1e6) + [("1", "2025-08-19", 30.5, 1e6, "tiingo")]
    book = _book(rows)  # to 2025-08-18, then the successor's first day under the same ticker
    row = _row(end_source="snapshots", delist_date="", f25_delisting_basis="", f25_filing_date="", end_date="2025-09-01")
    ev = _evidence(closing_dates="2025-08-19", terms_shares=1.0, terms_acquirer_phrase="TargetCo Holdings",
                   effective_before_open="2025-08-19")
    frame, used = _build([row], [ev], book)
    assert frame.loc["1", "last_price_date"] == "2025-08-18"
    assert used.set_index("security_id").loc["1", "limit_basis"] == "session_before_midnight_effective_time_in_closing_8k"


def test_last_trade_at_the_snapshot_slack_limit_with_an_earlier_stored_end_is_held():
    # QRTEA before the reviewed limit: snapshots end 2025-02-01, slack limit 2025-03-18; the vendor rows run to the
    # limit (QVCGA's under the same ticker) while the stored series ends on 2025-02-21
    rows = _history("1", "2025-01-02", 51, close=30.0, volume=5e6)  # to 2025-03-13
    rows += [("1", d, 30.6 + 0.1 * i, 5e6, "tiingo")
             for i, d in enumerate(("2025-03-14", "2025-03-17", "2025-03-18", "2025-03-19"))]
    rows += [("1", d, 30.0, 5e6, "stored") for d in _days("2025-01-02", 36)]  # stored to 2025-02-20
    rows += [("1", "2025-02-21", 30.0, 5e6, "stored")]
    book = _book(rows)
    row = _row(end_source="snapshots", delist_date="", f25_delisting_basis="", f25_filing_date="", end_date="2025-02-01")
    ev = _evidence(closing_items="", closing_accessions="", closing_dates="", terms_shares=1.0,
                   terms_acquirer_phrase="TargetCo Holdings", reorganization=True)
    frame, used = _build([row], [ev], book)
    level = used.set_index("security_id").loc["1"]
    assert (level.limit, level.limit_basis, level.last_date) == ("2025-03-18", "snapshots_end", "2025-03-18")
    out = frame.loc["1"]
    assert (out.status, out.terminal_return) == ("needs_review", "")
    assert "snapshot-slack limit" in out.status_note
    # the check over the built rows flags such a row if it were ever computed
    stale = frame.reset_index().assign(status="computed")
    assert tr.successor_overruns(stale, pd.DataFrame([row]), used) == ["1"]
    assert tr.successor_overruns(stale, pd.DataFrame([row])) == []  # without the levels only the successor date counts


def test_brcm_traded_share_is_a_cash_electing_share_from_the_read_election_results():
    review = tr.REVIEWED["1054374"]
    assert (review["cash"], review["shares"], review["acq"]) == (51.4829, 0.0242, "1649338")
    assert review["url"].endswith("1441634/000119312516446897/d121614dex992.htm") and "d10800d425.htm" in review["approved"]
    for path in ("1441634/0001193125-16-446897/d121614dex992.htm.gz", "1054374/0001193125-16-437104/d10800d425.htm.gz"):
        if (tr.OUT / "review_docs").exists():
            assert (tr.OUT / "review_docs" / path).exists(), path
    book = _book([("1054374", "2016-01-29", 54.67, 1e6, "wiki"), ("1649338", "2016-02-01", 137.68, 1e6, "wiki")])
    names = tr.NameIndex(_master([{"security_id": "1649338", "cik": "1649338", "name": "BROADCOM LTD",
                                   "first_listed": "2016-02-01", "last_listed": "2018-04-04"}]))
    row = _row(security_id="1054374", f25_filing_date="2016-02-01")
    frame, _ = tr.build_rows(pd.DataFrame([row]), pd.DataFrame([_evidence("1054374", terms_election=True)]), book, {},
                             names, pd.DataFrame(columns=["security_id", "planned_source", "status"]), "2026-10-02")
    out = frame.set_index("security_id").loc["1054374"]
    assert out.status == "computed" and out.election == "Y"  # an election, approved by the reviewed results
    assert float(out.terminal_return) == pytest.approx((51.4829 + 0.0242 * 137.68) / 54.67 - 1)  # not 0.4378 x 137.68


def test_21cf_non_electing_shares_wait_for_a_dis_close(monkeypatch):
    for sid in ("1308161.A", "1308161.B"):
        review = tr.REVIEWED[sid]
        assert (review["type"], review["shares"], review.get("cash"), review.get("rule")) == ("stock_merger", 0.4517, None, None)
        assert tr.ACQUIRER_SYMBOLS[sid] == "DIS" and review["url"].endswith("1308161/000095015719000308/form8k.htm")
    assert len(set(tr.ACQUIRER_SYMBOLS.values())) > tr.YAHOO_MAX  # the cap is used up: DIS is not fetched
    book = _book([("1308161.A", "2019-03-18", 49.0, 1e7, "tiingo"), ("1308161.A", "2019-03-19", 49.6, 1e7, "tiingo")])
    row = _row(security_id="1308161.A", end_source="snapshots", delist_date="", f25_delisting_basis="",
               f25_filing_date="", end_date="2019-03-13")
    ev = _evidence("1308161.A", closing_dates="2019-03-20", halt_before_open="2019-03-20", terms_election=True)
    monkeypatch.setattr(tr, "acquirer_chart", lambda symbol: pd.DataFrame(columns=["date", "close", "volume", "src"]))
    frame, used = _build([row], [ev], book)
    out = frame.loc["1308161.A"]
    assert (out.status, out.terminal_return, out.last_price_date) == ("needs_acquirer_price", "", "2019-03-19")
    queue = tr.manual_review_queue(frame.reset_index(), used)
    assert "DIS chart" in queue.set_index("security_id").loc["1308161.A", "reason"]
    chart = pd.DataFrame({"date": pd.to_datetime(["2019-03-19", "2019-03-20"]), "close": [111.0, 110.0],
                          "volume": [1e7, 2e7], "src": "yahoo_acquirer"})
    monkeypatch.setattr(tr, "acquirer_chart", lambda symbol: chart)
    frame, _ = _build([row], [ev], book)
    out = frame.loc["1308161.A"]
    assert (out.status, out.acquirer_price_date) == ("computed", "2019-03-20")
    assert float(out.terminal_return) == pytest.approx(0.4517 * 110.0 / 49.6 - 1)


def test_reviewed_hold_on_the_last_session_blanks_last_price_date(monkeypatch):
    assert tr.REVIEWED["6769"]["hold_last_session"]
    # APA continues as APA Corporation (SUCCESSOR_LINKS): taken out here to test the hold itself
    monkeypatch.delitem(tr.SUCCESSOR_LINKS, "6769")
    book = _book([("6769", "2021-02-26", 21.8, 1e7, "tiingo"), ("6769", "2021-03-01", 21.9, 1e7, "tiingo"),
                  ("6769", "2021-03-02", 22.0, 1e7, "tiingo")])
    row = _row(security_id="6769", end_source="snapshots", delist_date="", f25_delisting_basis="", f25_filing_date="",
               end_date="2021-03-01")
    frame, used = _build([row], [_evidence("6769", closing_dates="2021-03-01")], book)
    out = frame.loc["6769"]
    assert (out.status, out.last_price_date, out.acquirer_price_date, out.terminal_return) == ("needs_review", "", "", "")
    level = used.set_index("security_id").loc["6769"]
    assert (level.last_price_date_held, level.acquirer_price_date_held) == ("2021-03-01", "2021-03-02")


def test_a_one_for_one_reorganisation_that_continues_books_no_terminal_return():
    """Owner convention 2026-10-02: Apache -> APA continues the security (reconcile SUCCESSOR_LINKS), so the row has no
    terminal return (the successor's first return runs from Apache's last close) and the hold on the last session
    is moot: the reconcile cut dates it."""
    assert tr.SUCCESSOR_LINKS["6769"]["continues"]
    book = _book([("6769", "2021-02-26", 21.8, 1e7, "tiingo"), ("6769", "2021-03-01", 21.9, 1e7, "tiingo"),
                  ("6769", "2021-03-02", 22.0, 1e7, "tiingo")])
    row = _row(security_id="6769", end_source="snapshots", delist_date="", f25_delisting_basis="", f25_filing_date="",
               end_date="2021-03-01")
    frame, used = _build([row], [_evidence("6769", closing_dates="2021-03-01")], book)
    out = frame.loc["6769"]
    assert (out.terminal_type, out.event_subtype, out.consideration_shares) == ("stock_merger", "reorganization", "1")
    assert (out.status, out.terminal_return, out.continued_as) == ("no_terminal_return", "", "1841666")
    assert out.last_price_date == tr.SUCCESSOR_LINKS["6769"]["last_session"] == "2021-03-01"
    assert "continued as 1841666" in out.status_note
    assert not used.set_index("security_id").loc["6769", "review_reasons"]
    # a cut-only link (21CF -> Fox) keeps its terminal value
    assert not tr.SUCCESSOR_LINKS["1308161.A"]["continues"]


def test_guard_holds_elections_cvrs_and_values_beyond_5pct_unless_approved_with_a_url():
    book = _book([("1", "2019-04-18", 43.0, 100, "tiingo"), ("1", "2019-04-19", 43.2, 100, "tiingo")])
    for ev, word in ((_evidence(terms_cash=43.5, terms_cvr=True), "CVR"),
                     (_evidence(terms_cash=43.5, terms_election=True), "election"),
                     (_evidence(terms_cash=46.0), "5%")):
        frame, used = _build([_row()], [ev], book)
        out = frame.loc["1"]
        assert (out.status, out.terminal_return, out.consideration_per_share) == ("needs_review", "", ""), word
        assert out.consideration_cash != "" and word in used.set_index("security_id").loc["1", "review_reasons"], word
    original = tr.REVIEWED.get("1")
    try:
        tr.REVIEWED["1"] = {"type": "cash_merger", "cash": 43.5, "approved": "CVR checked", "note": "synthetic"}
        frame, _ = _build([_row()], [_evidence(terms_cash=43.5, terms_cvr=True)], book)
        assert frame.loc["1", "status"] == "needs_review"  # an approval without its own url does not count
        tr.REVIEWED["1"]["url"] = "https://www.sec.gov/Archives/edgar/data/1/0001/x.htm"
        frame, _ = _build([_row()], [_evidence(terms_cash=43.5, terms_cvr=True)], book)
        assert frame.loc["1", "status"] == "computed" and frame.loc["1", "consideration_per_share"] == "43.5"
    finally:
        tr.REVIEWED.pop("1")
        if original is not None:
            tr.REVIEWED["1"] = original
    # a bankruptcy's OTC close is not exempt from the 5% rule: its OTC close is checked by hand first
    rows = _history("7", "2023-01-02", 40, close=110.0, volume=1_500_000) + [("7", "2023-03-01", 1.0, 5e6, "tiingo")]
    frame, _ = _build([_row(security_id="7", f25_delisting_basis="exchange_removal", f25_filing_date="2023-02-28")],
                      [_evidence("7", closing_items="3.01")], _book(rows))
    assert frame.loc["7", "status"] == "needs_review"


def test_manual_review_queue_lists_held_and_flagged_rows_by_best_rank():
    book = _book([("1", "2019-04-19", 43.2, 100, "tiingo"), ("2", "2019-04-19", 43.2, 100, "tiingo")])
    candidates = pd.DataFrame({"security_id": ["3"], "planned_source": ["tiingo"], "status": ["pending"]})
    rows = [_row(best_rank=120.0), _row(security_id="2", best_rank=7.0), _row(security_id="3", best_rank=50.0)]
    evidence = [_evidence(terms_cash=43.5, terms_cvr=True), _evidence("2", terms_cash=43.5),
                _evidence("3", terms_cash=10.0, terms_election=True)]
    frame, used = _build(rows, evidence, book, candidates)
    queue = tr.manual_review_queue(frame.reset_index(), used)
    assert list(queue.columns) == ["security_id", "ticker", "best_rank", "reason", "documents"]
    assert list(queue["security_id"]) == ["3", "1"]  # by best rank; the computed row 2 is not queued
    assert queue.loc[0, "reason"].startswith("pending_price: to check once priced: the consideration involves")
    assert queue.loc[1, "reason"].startswith("needs_review: the consideration includes a CVR")
    assert "https://www.sec.gov/8k.htm" in queue.loc[1, "documents"]


def test_built_file_round_6_cases(built):
    rows = built.set_index("security_id")
    if "1054374" in rows.index and rows.loc["1054374", "terminal_return"]:
        assert abs(float(rows.loc["1054374", "terminal_return"])) < 0.02  # BRCM: not the all-stock +10.25%
    for sid in ("1308161.A", "1308161.B"):
        if sid in rows.index:
            assert rows.loc[sid, "status"] != "computed" or rows.loc[sid, "acquirer_price_date"] == "2019-03-20", sid
    for sid in ("6769", "912752"):
        if sid in rows.index and rows.loc[sid, "status"] == "needs_review":
            assert rows.loc[sid, "last_price_date"] == "", sid
    for sid in ("1355096.T-QRTEA", "1355096.T-QRTEB"):
        if sid in rows.index and rows.loc[sid, "last_price_date"]:
            assert rows.loc[sid, "last_price_date"] <= "2025-02-21", sid
    if "1602065" in rows.index and rows.loc["1602065", "last_price_date"]:
        assert rows.loc["1602065", "last_price_date"] <= "2025-08-18"
    held = rows[rows["status"].eq("needs_review")]
    assert held["terminal_return"].eq("").all() and held["consideration_per_share"].eq("").all()
    computed = rows[rows["status"].eq("computed") & ~rows["terminal_type"].eq("bankruptcy_otc")]
    beyond = computed[pd.to_numeric(computed["terminal_return"]).abs() > tr.GUARD_RETURN]
    flagged = computed[computed["cvr"].eq("Y") | computed["election"].eq("Y")]
    approved = {sid for sid, r in tr.REVIEWED.items() if r.get("approved") and r.get("url")}
    assert set(beyond.index) <= approved and set(flagged.index) <= approved
    queue_path = tr.MANUAL_REVIEW_QUEUE
    if queue_path.exists():
        queue = tr.read_csv_text(queue_path)
        assert list(queue.columns) == ["security_id", "ticker", "best_rank", "reason", "documents"]
        assert set(held.index) <= set(queue["security_id"])
        ranks = pd.to_numeric(queue["best_rank"], errors="coerce")
        assert ranks.dropna().is_monotonic_increasing


def test_an_election_stated_only_in_the_closing_documents_holds_the_row():
    from scripts import reversal_data_terminal as t
    decided, out = {"note": ""}, {"election": "N", "cvr": "N"}
    assert t.guard_reasons(decided, out, {"election": "True", "cvr": "False"}) == [
        "the consideration involves a holder election or a proration"]
    assert t.guard_reasons(decided, out, {"election": "False", "cvr": "True"}) == [
        "the consideration includes a CVR (valued at 0)"]
    assert t.guard_reasons(decided, out, {}) == []


def test_built_file_has_no_unapproved_computed_value_far_from_its_last_close():
    import pandas as pd
    from scripts import reversal_data_terminal as t
    rows = pd.read_csv(t.OUTPUT, dtype=str, keep_default_na=False)
    computed = rows[rows["status"] == "computed"]
    returns = pd.to_numeric(computed["terminal_return"], errors="coerce")
    far = computed[returns.abs() > t.GUARD_RETURN]
    approved = {sid for sid, r in t.REVIEWED.items() if r.get("approved") and r.get("url")}
    assert set(far["security_id"]) <= approved


# ------------------------------------------------------------------ round 7: the old shares at a relist junction

def _junction(monkeypatch, sid="1", first_new="2025-06-27", effective="2025-06-24", review=None):
    monkeypatch.setitem(tr.RELIST_JUNCTIONS, sid, {"first_new_session": first_new, "effective_date": effective,
                                                   "kind": "bankruptcy_share_exchange", "read": True, "url": "u",
                                                   "note": ""})
    if review is not None:
        monkeypatch.setitem(tr.REVIEWED, sid, review)


def _ww_book(stored_tail=True):
    rows = [("1", "2025-05-14", 0.28, 9e6, "tiingo"), ("1", "2025-05-15", 0.25, 1e7, "tiingo")]
    if stored_tail:  # the stored file's OTC tail (never a level)
        rows += [("1", d, 26.0, 5e4, "stored") for d in _days("2025-05-16", 28)]
    rows += [("1", "2025-06-27", 27.0, 2e5, "tiingo"), ("1", "2025-06-30", 30.2, 4e5, "tiingo")]
    return _book(rows)


def test_price_book_before_cuts_only_the_old_shares_rows():
    book = _book([("1", "2025-05-15", 0.25, 100, "tiingo"), ("1", "2025-06-27", 27.0, 100, "tiingo"),
                  ("2", "2025-06-27", 5.0, 100, "tiingo")])
    view = book.before("1", "2025-06-27")
    assert view.rows("1")["date"].max() == pd.Timestamp("2025-05-15")
    assert len(view.rows("2")) == 1 and len(book.rows("1")) == 2  # the book itself is unchanged


def test_old_shares_last_trade_never_comes_from_the_new_shares(monkeypatch):
    """WW: the Tiingo file runs on from the old shares (to 2025-05-15) into the new ones (from 2025-06-27); the
    Form 25 is filed after the junction. The old shares' last trade is 2025-05-15, not a new-share row."""
    _junction(monkeypatch)
    monkeypatch.delitem(tr.REVIEWED, "1", raising=False)
    row = _row(f25_delisting_basis="substituted_merger_or_exchange", f25_filing_date="2025-07-03",
               end_date="2025-07-13", delist_date="2025-07-13")
    evidence = [_evidence(closing_items="1.03 3.01", bankruptcy=True)]
    frame, used = _build([row], evidence, _ww_book(stored_tail=False))
    out, level = frame.loc["1"], used.set_index("security_id").loc["1"]
    assert level["last_date"] == "2025-05-15" and level["limit_basis"] == "session_before_relist_junction"
    # no OTC close and no reviewed plan valuation: held for the plan, never the D5 rule (round 9)
    assert (out.terminal_type, out.status, out.relist_junction) == ("bankruptcy_otc", "needs_review", "2025-06-27")
    assert "old shares at a relist junction" in out.status_note
    # without the junction entry the same book gives a new-share row as the old shares' last trade
    monkeypatch.delitem(tr.RELIST_JUNCTIONS, "1")
    plain = _build([row], evidence, _ww_book(stored_tail=False))[1].set_index("security_id").loc["1"]
    assert plain["last_date"] == "2025-06-30"


def test_old_shares_without_an_otc_close_are_valued_by_the_plan_at_the_new_shares_first_close(monkeypatch):
    _junction(monkeypatch, review={
        "type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2025-05-15", "rule": "plan_new_shares",
        "shares": 0.0111676, "url": "https://www.sec.gov/Archives/edgar/data/1/x.htm",
        "approved": "the plan's new shares per old share", "note": "WW-like"})
    row = _row(f25_delisting_basis="substituted_merger_or_exchange", f25_filing_date="2025-07-03",
               end_date="2025-07-13", delist_date="2025-07-13")
    frame, used = _build([row], [_evidence(closing_items="1.03 3.01", bankruptcy=True)], _ww_book())
    out, level = frame.loc["1"], used.set_index("security_id").loc["1"]
    assert (out.status, out.last_price_date, out.acquirer_price_date) == ("computed", "2025-05-15", "2025-06-27")
    assert float(out.terminal_return) == pytest.approx(0.0111676 * 27.0 / 0.25 - 1)
    assert out.consideration_shares == "0.0111676" and out.consideration_per_share == ""  # the level stays local
    assert out.consideration_value_basis.startswith("plan_new_shares_at_first_new_share_close")
    assert level["plan_new_share_close"] == 27.0 and "27" not in out.status_note.replace("2025-06-27", "")


def test_old_shares_otc_tail_values_them_and_stops_at_the_junction(monkeypatch):
    """CHRD: the old shares' Nasdaq last session 2020-10-09, OTC rows from 2020-10-12, new shares 2020-11-20."""
    review = {"type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2020-10-09",
              "url": "https://www.sec.gov/Archives/edgar/data/1/y.htm", "approved": "OTC start dated", "note": "CHRD-like"}
    _junction(monkeypatch, first_new="2020-11-20", effective="2020-11-19", review=review)
    rows = [("1", "2020-10-08", 0.17, 6e7, "yahoo_step7"), ("1", "2020-10-09", 0.16, 7e7, "yahoo_step7"),
            ("1", "2020-10-12", 0.10, 5e7, "yahoo_step7"), ("1", "2020-11-19", 0.12, 1e6, "yahoo_step7"),
            ("1", "2020-11-20", 31.0, 8e5, "yahoo_step7")]
    row = _row(f25_delisting_basis="exchange_removal", f25_filing_date="2020-10-27", end_date="2020-11-06",
               delist_date="2020-11-06")
    frame, used = _build([row], [_evidence(closing_items="1.03 3.01", bankruptcy=True)], _book(rows))
    out = frame.loc["1"]
    assert (out.status, out.last_price_date) == ("computed", "2020-10-09")
    assert float(out.terminal_return) == pytest.approx(0.10 / 0.16 - 1)
    assert used.set_index("security_id").loc["1", "otc_date"] == "2020-10-12"
    # with no OTC row before the junction, the new shares' first row is not taken for an OTC close, and the row waits
    # for a plan valuation: the old shares at a junction are never a D5 case (round 9)
    gap_rows = [r for r in rows if r[1] not in ("2020-10-12", "2020-11-19")]
    frame, used = _build([row], [_evidence(closing_items="1.03 3.01", bankruptcy=True)], _book(gap_rows))
    assert frame.loc["1", "status"] == "needs_review" and frame.loc["1", "terminal_return"] == ""
    assert "not a D5 case" in used.set_index("security_id").loc["1", "review_reasons"]


def test_old_shares_cancelled_with_nothing_need_no_close(monkeypatch):
    """OPI: no vendor row near the last Nasdaq session (the WIKI table ends in 2018), and the holders received
    nothing: -100% whatever the close, dated on the reviewed last Nasdaq session."""
    review = {"type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2025-10-06", "value": 0.0,
              "url": "https://www.sec.gov/Archives/edgar/data/1/z.htm", "approved": "no distribution", "note": "OPI-like"}
    _junction(monkeypatch, first_new="2026-06-22", effective="2026-06-17", review=review)
    rows = [("1", "2018-03-27", 13.29, 8e5, "wiki"), ("1", "2025-10-06", 0.3, 1e6, "stored"),
            ("1", "2026-06-22", 17.0, 5e4, "yahoo_step7")]
    row = _row(f25_delisting_basis="exchange_removal", f25_filing_date="2025-11-14", end_date="2025-11-24",
               delist_date="2025-11-24")
    frame, used = _build([row], [_evidence(closing_items="1.03 3.01", bankruptcy=True)], _book(rows))
    out = frame.loc["1"]
    assert (out.status, out.terminal_return, out.last_price_date) == ("computed", "-1", "2025-10-06")
    assert out.consideration_per_share == "0" and out.price_source == ""
    assert used.set_index("security_id").loc["1", "status"] == "vendor_short"


def test_old_shares_waiting_for_their_vendor_rows_are_pending_not_d5(monkeypatch):
    review = {"type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2022-12-30",
              "url": "https://www.sec.gov/Archives/edgar/data/1/c.htm", "note": "CORZ-like"}
    _junction(monkeypatch, first_new="2024-01-24", effective="2024-01-23", review=review)
    candidates = pd.DataFrame({"security_id": ["1"], "planned_source": ["tiingo"], "status": ["conditional_tier_c"]})
    row = _row(f25_delisting_basis="exchange_removal", f25_filing_date="2023-04-12", end_date="2023-04-22",
               delist_date="2023-04-22")
    frame, _ = _build([row], [_evidence(closing_items="1.03 3.01", bankruptcy=True)],
                      _book([("1", "2024-01-24", 3.44, 1e6, "yahoo_step7")]), candidates)
    out = frame.loc["1"]
    assert (out.status, out.event_subtype, out.terminal_return) == ("pending_price", "bankruptcy", "")
    assert "D5 rule is not the answer" in out.status_note


def test_every_relist_junction_has_a_reviewed_row_dated_on_the_old_shares_last_nasdaq_session():
    for sid, junction in tr.RELIST_JUNCTIONS.items():
        review = tr.REVIEWED_OLD_SHARES.get(sid) or tr.REVIEWED.get(sid)  # Frontier: the old shares' own entry
        assert review and review["type"] == "bankruptcy_otc" and review["sub"] == "bankruptcy", sid
        assert review["limit"] == junction["old_nasdaq_last_session"] < junction["first_new_session"], sid
        assert review["url"].startswith("https://www.sec.gov/Archives/edgar/data/"), sid
    assert tr.REVIEWED["1456772"]["value"] == 0.0 and tr.REVIEWED["1556739"]["value"] == 0.0
    assert tr.REVIEWED["105319"]["rule"] == "plan_new_shares"
    assert "approved" not in tr.REVIEWED["1839341"]  # CORZ waits for its old-share rows
    # WW: the builder's plan valuation is held for the owner (round-8 review), its checks kept beside it
    ww = tr.REVIEWED["105319"]
    assert "approved" not in ww and ww["hold"] and "0.0111676" in ww["checked"]
    assert "checked" not in tr.REVIEW_KEYS  # a builder's check lifts nothing


def test_a_held_plan_valuation_waits_for_review_with_the_value_kept_local(monkeypatch):
    """WW as committed: no approval, a hold for the owner. The row is needs_review, its return blank, and the
    valuation is kept in prices_used for the reviewer."""
    _junction(monkeypatch, review={
        "type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2025-05-15", "rule": "plan_new_shares",
        "shares": 0.0111676, "url": "https://www.sec.gov/Archives/edgar/data/1/x.htm",
        "hold": "the plan valuation awaits the owner's approval", "checked": "share counts from the 10-Q",
        "note": "WW-like"})
    row = _row(f25_delisting_basis="substituted_merger_or_exchange", f25_filing_date="2025-07-03",
               end_date="2025-07-13", delist_date="2025-07-13")
    frame, used = _build([row], [_evidence(closing_items="1.03 3.01", bankruptcy=True)], _ww_book())
    out, level = frame.loc["1"], used.set_index("security_id").loc["1"]
    assert (out.status, out.terminal_return, out.last_price_date) == ("needs_review", "", "2025-05-15")
    assert "awaits the owner's approval" in out.status_note and "approved by review" not in out.status_note
    assert level["terminal_return_checked"] == pytest.approx(0.0111676 * 27.0 / 0.25 - 1)
    assert out.consideration_per_share == ""


def test_configure_paths_sends_the_outputs_under_the_out_dir(tmp_path, monkeypatch):
    for name in ("OUT", "OUTPUT", "RECONCILE_HANDOFF", "MANUAL_REVIEW_QUEUE", "EXCHANGE_MOVES", "SERIES_ENDS_OUT",
                 "READ_ONLY_SHARED", "RECONCILE_DIR", "CANONICAL_PRICES"):
        monkeypatch.setattr(tr, name, getattr(tr, name))  # restored after the test
    tr.configure_paths(tmp_path)
    root = tmp_path.resolve()
    assert tr.OUTPUT == root / "inputs" / "terminal_returns_2012_2026.csv" and tr.OUT == root / "terminal"
    assert tr.MANUAL_REVIEW_QUEUE.parent == tr.RECONCILE_HANDOFF.parent == root / "terminal"
    assert tr.EXCHANGE_MOVES == root / "inputs" / "exchange_moves.csv" and tr.SERIES_ENDS_OUT == root / "terminal" / "series_ends.csv"
    assert tr.READ_ONLY_SHARED  # a scratch build writes nothing outside its out-dir
    tr.configure_reconcile(tmp_path / "rc")
    assert tr.RECONCILE_DIR == (tmp_path / "rc").resolve() / "reconcile" and tr.CANONICAL_PRICES.name == "prices"
    assert tr.SERIES_ENDS_OUT == root / "terminal" / "series_ends.csv"  # still under the out-dir
    assert tr.YAHOO_RAW == tr.TERMINAL_CACHE / "yahoo_raw"  # the charts are still read from CACHE/terminal



# ------------------------------------------------------------------ round 9: the old shares of a junction whose new shares
# were delisted later, a junction-only scope row, exchange_moves.csv, the refreshed series_ends column, scratch builds

def test_old_shares_of_a_security_delisted_again_later_go_to_their_own_columns(monkeypatch):
    """Frontier: the row values FYBR's 2026 cash merger with the whole book; the old FTR shares (cancelled with nothing,
    an inference written as such) are -100% in the relist_old_shares_* columns."""
    _junction(monkeypatch, first_new="2021-05-04", effective="2021-04-30")
    monkeypatch.setitem(tr.REVIEWED_OLD_SHARES, "1", {
        "type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2020-04-23", "value": 0.0,
        "url": "https://www.sec.gov/Archives/edgar/data/1/o.htm", "approved": "partly an inference", "note": "FTR-like"})
    monkeypatch.delitem(tr.REVIEWED, "1", raising=False)
    book = _book([("1", "2018-03-07", 8.05, 1e6, "wiki"), ("1", "2026-01-15", 38.4, 1e6, "tiingo"),
                  ("1", "2026-01-16", 38.45, 1e6, "tiingo")])
    row = _row(delist_date="2026-01-30", end_date="2026-01-30", f25_filing_date="2026-01-20")
    frame, used = _build([row], [_evidence(terms_cash=38.5, closing_dates="2026-01-20")], book)
    out = frame.loc["1"]
    assert (out.terminal_type, out.status, out.last_price_date, out.relist_junction) == ("cash_merger", "computed",
                                                                                         "2026-01-16", "")
    assert float(out.terminal_return) == pytest.approx(38.5 / 38.45 - 1)
    assert (out.relist_old_shares_status, out.relist_old_shares_terminal_return,
            out.relist_old_shares_last_price_date) == ("computed", "-1", "2020-04-23")
    assert "partly an inference" in out.relist_old_shares_note
    assert used.set_index("security_id").loc["1", "old_limit"] == "2020-04-23"


def test_a_junction_whose_master_has_no_end_is_in_scope_on_its_old_shares_last_nasdaq_session(monkeypatch):
    """Vroom: no 2024 Form 25 in the step-3 table and the new shares still listed, so the master dates no end."""
    monkeypatch.setitem(tr.RELIST_JUNCTIONS, "9", {"first_new_session": "2025-02-20", "effective_date": "2025-01-14",
                                                   "old_nasdaq_last_session": "2024-11-29", "kind": "x", "url": "u"})
    master = _master([{"security_id": "9", "cik": "9", "first_ticker": "VRM", "last_listed": "2026-07-01"},
                      {"security_id": "8", "cik": "8", "first_ticker": "ACT", "last_listed": "2026-07-01"}])
    facts = pd.DataFrame({"security_id": ["9", "8"], "best_rank": ["180", "50"], "active_nasdaq": ["True", "True"],
                          "last_listed": ["2026-07-01"] * 2, "last_ticker": ["VRM", "ACT"], "tickers": ["VRM", "ACT"],
                          "last_universe_week": [""] * 2}, dtype=str)
    form25 = pd.DataFrame(columns=["accession", "effective_date", "filing_date", "rule_provision", "delisting_basis",
                                   "classification", "subject_exit", "successor_cik", "successor_tickers", "tickers_new",
                                   "tickers_ended", "doc_url"])
    scope = tr.terminal_candidates(master, pd.DataFrame({"security_id": []}, dtype=str), facts, form25)
    assert list(scope["security_id"]) == ["9"]  # an active security without a junction stays out
    assert scope.iloc[0]["end_date"] == "2024-11-29" and scope.iloc[0]["end_source"] == "relist_junction"


def test_every_round_9_junction_review_is_held_or_written_as_an_inference():
    vroom, cepl, thry = tr.REVIEWED["1580864"], tr.REVIEWED["1009759"], tr.REVIEWED["1556739"]
    assert "approved" not in vroom and vroom["hold"] and vroom["rule"] == "plan_new_shares" and vroom["shares"] == 0.2
    assert "approved" not in cepl and cepl["limit"] == "2023-10-04"  # the guard holds the OTC close for a check
    assert thry["approved"].startswith("an inference") and tr.REVIEWED_OLD_SHARES["20520"]["approved"].startswith(
        "partly an inference")
    assert "20520" not in tr.REVIEWED  # Frontier's row values FYBR's 2026 delisting


def test_exchange_moves_join_the_terminal_rows_series_ends_and_the_holdout_overrides(tmp_path, monkeypatch):
    overrides = tmp_path / "overrides.csv"
    pd.DataFrame([{"ticker": "PEP", "nasdaq_from": "2017-12-20", "nasdaq_until": "", "basis": "8-K", "evidence": "https://e/1"},
                  {"ticker": "HON", "nasdaq_from": "2099-01-01", "nasdaq_until": "", "basis": "absent", "evidence": "snapshots"},
                  {"ticker": "ORCL", "nasdaq_from": "", "nasdaq_until": "2013-07-15", "basis": "moved to NYSE",
                   "evidence": "https://e/2"}]).to_csv(overrides, index=False)
    monkeypatch.setattr(tr, "HOLDOUT_OVERRIDES", overrides)
    frame = pd.DataFrame([{"security_id": "1", "ticker": "ORCL", "terminal_type": "exchange_move", "status": "no_terminal_return",
                           "destination_start_date": "2013-07-15", "last_price_date": "2013-07-12", "end_date": "2013-07-22",
                           "destination_exchange": "NYSE", "source_url": "https://sec/orcl"},
                          {"security_id": "2", "ticker": "X", "terminal_type": "cash_merger", "status": "computed",
                           "destination_start_date": "", "last_price_date": "2015-01-02", "end_date": "2015-01-05",
                           "destination_exchange": "", "source_url": ""}])
    ends = pd.DataFrame([{"security_id": "3", "ticker_last": "UNFI", "likely_cause": "exchange_move", "transfer_date": "2019-01-07",
                          "last_date": "2019-01-04"}])
    master = _master([{"security_id": "3", "cik": "1020859", "transfer_form25_accession": "0001020859-18-000166",
                       "exchanges_sec_current": "NYSE"}])
    intervals = pd.DataFrame([{"security_id": "1", "ticker": "ORCL", "start": "2010-12-31", "end": "2013-07-12", "exchange": "NASDAQ"},
                              {"security_id": "77476", "ticker": "PEP", "start": "2017-12-20", "end": "2026-08-01",
                               "exchange": "NASDAQ"}])
    moves = tr.exchange_moves(frame, ends, master, intervals).set_index("ticker")
    assert list(moves.columns[:5]) == ["security_id", "date", "from_exchange", "to_exchange", "source_url"]
    assert sorted(moves.index) == ["ORCL", "PEP", "UNFI"]  # ORCL once (the terminal row), no 2099 placeholder
    assert (moves.loc["ORCL", "date"], moves.loc["ORCL", "source"]) == ("2013-07-15", "terminal")
    assert (moves.loc["PEP", "security_id"], moves.loc["PEP", "to_exchange"]) == ("77476", "NASDAQ")
    assert moves.loc["UNFI", "to_exchange"] == "NYSE" and moves.loc["UNFI", "source_url"].endswith("-index.htm")
    # a holdout row that names no old exchange says why the field is blank (round 10)
    assert moves.loc["PEP", "from_exchange"] == "" and "blank: the old exchange is not named" in moves.loc["PEP", "note"]
    assert "2019" in tr.EXCHANGE_MOVES_SCOPE and "onto Nasdaq" in tr.EXCHANGE_MOVES_SCOPE


def test_refresh_series_ends_quotes_this_build_and_labels_the_old_shares():
    ends = pd.DataFrame([{"security_id": "J", "category": "old_shares_at_relist_junction", "terminal_2012_2026": "stale"},
                         {"security_id": "K", "category": "ends_before_delist", "terminal_2012_2026": "stale"},
                         {"security_id": "Z", "category": "ends_before_delist", "terminal_2012_2026": "stale"}])
    frame = pd.DataFrame([{"security_id": "J", "terminal_type": "cash_merger", "status": "computed", "last_price_date": "2026-01-16",
                           "end_date": "2026-01-30", "relist_old_shares_status": "computed",
                           "relist_old_shares_last_price_date": "2020-04-23"},
                          {"security_id": "K", "terminal_type": "bankruptcy_otc", "status": "needs_review",
                           "last_price_date": "2024-11-29", "end_date": "2024-11-29", "relist_old_shares_status": "",
                           "relist_old_shares_last_price_date": ""}])
    out = tr.refresh_series_ends(ends, frame).set_index("security_id")["terminal_2012_2026"]
    assert out["J"].startswith("bankruptcy_otc/computed (old shares") and "2020-04-23" in out["J"]
    assert out["K"] == "bankruptcy_otc/needs_review last_price_date=2024-11-29 end_date=2024-11-29" and out["Z"] == ""
    assert tr.refresh_series_ends(None, frame) is None


def test_a_scratch_build_reads_the_repos_sec_envelopes_without_copying_them(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_from_provenance", lambda url: b"body")
    path = tmp_path / "docs" / "x.htm.gz"
    monkeypatch.setattr(tr, "READ_ONLY_SHARED", True)
    assert tr.fetch_sec("https://www.sec.gov/x.htm", path, offline=True) == b"body" and not path.exists()
    monkeypatch.setattr(tr, "READ_ONLY_SHARED", False)
    assert tr.fetch_sec("https://www.sec.gov/x.htm", path, offline=True) == b"body" and path.exists()


def test_a_month_2_tiingo_row_is_pending_for_month_2():
    candidates = pd.DataFrame({"security_id": ["1"], "planned_source": ["tiingo"], "status": ["pending_month2"]})
    assert tr.price_pending("1", candidates, _book([])) == "tiingo month 2"
