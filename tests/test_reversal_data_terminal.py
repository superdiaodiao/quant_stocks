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
               "former_names", "exchanges_sec_current"]
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
        assert set(review) <= set(tr.REVIEW_KEYS) | {"note", "return_checked"}, sid
        assert review.get("note"), sid
        if "type" in review:
            assert review["type"] in tr.TYPES, sid
        if review.get("type") in ("stock_merger", "mixed"):
            assert review.get("shares") is not None or review.get("stock_value") is not None, sid


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
