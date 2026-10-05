"""Plan step 11 (docs/reversal_2012_2026_data_plan.md, sections 2 and 4.5): terminal values.

Data only. Nothing here computes signals, portfolio returns, rankings by return or spreads.
The only return computed is each ended security's own terminal return (the value received
per share after its last Nasdaq close, divided by that close, minus one), which belongs to
that security's canonical series. Terminal returns are never averaged, ranked or
aggregated; the summary counts rows by type and status and lists security ids to check.

Scope: every security of ``candidate_fetch_list.csv`` or with a dv rank <= 300 in some
universe week (``CACHE/prefilter/security_facts.csv.gz`` ``best_rank``) whose Nasdaq listing
ends by 2026-08-31: a Form 25 delisting on or before that day, or a listing that stops in the
snapshots (``active_nasdaq`` False: a reorganisation, an exchange move, a rename into another
security, a failure).

How the listing ended (``terminal_type``):
- ``cash_merger`` / ``stock_merger`` / ``mixed``: shares converted into cash, into shares of
  another company, or both. A holding-company reorganisation, a reclassification or a rename
  into another security_id is a stock merger at 1 share (``event_subtype``). The terms come
  from the closing 8-K (Items 2.01/3.01/3.03/5.01, 1.03) and its EX-99 press releases, else
  from the latest Item 1.01 agreement 8-Ks before the closing (``terms_stage``). Only clauses
  whose subject is the company's own shares count (``subject_owner``); a CVR is valued at 0;
  a cash-or-stock election is valued at the stock alternative when the acquirer is priced, and
  REVIEWED values the share that traded at the last close by the election results where they
  were read (BRCM: such shares were deemed cash-electing, $51.4829 + 0.0242 share; 21CF: the
  non-electing shares received 0.4517 Disney share). Such a row is held unless REVIEWED approves
  it (see the guard).
  ``acquirer_security_id`` is the acquirer's security in the master (blank when it was not on
  Nasdaq, with the name in ``acquirer_name``);
- ``liquidation``: a redemption or dissolution (Altaba: the liquidating distributions paid
  after the record date, undiscounted);
- ``exchange_move``: the listing moved to another exchange (``destination_exchange``); the
  series continues elsewhere, so there is no terminal return (status ``no_terminal_return``).
  ``last_price_date`` is the last Nasdaq session: the session before the first day on the new
  exchange that the Item 3.01 8-K or its press release states (``destination_start_date``), else
  the transfer Form 25 filing date, never the Form 25 effective date (trading has moved by then);
  it is left blank when no vendor session lies within a week before that day (MSG 2015: the
  series stops 11 weeks early), and the gap is reported in ``for_reconcile_owner.csv``;
- ``bankruptcy_otc``: removed by Nasdaq (Rule 12d2-2(b)), a bankruptcy, a voluntary delisting
  or a bank failure; the value is the first OTC vendor close after the last Nasdaq session
  (before the suspension date the 8-K states), else a sourced existing row, else it is left
  to the owner's D5 rule (status ``awaiting_d5``, no default here). A security that listed on
  Nasdaq again with new shares out of the bankruptcy (the reconcile step's ``RELIST_JUNCTIONS``:
  CHRD, CORZ, WW, OPI, THRY) is valued as its old shares: their rows only (the price book cut
  before the new shares' first traded session, ``relist_junction``), their last Nasdaq session
  (reviewed: the session before the stated suspension), their OTC tail up to the junction when a
  vendor has it (CHRD), else the plan of reorganization: new shares per old share at the new
  shares' first vendor close (REVIEWED ``rule`` ``plan_new_shares``: WW, ``needs_review`` until the
  owner approves the valuation: the builder's checks are in its ``checked`` key; Vroom), or nothing
  (``value`` 0: OPI, Dex Media, whose "nothing" is an inference written as such), which is -100% whatever
  the last close, so it needs no vendor close; one whose old shares have no vendor rows yet is
  ``pending_price``, not D5 (CORZ: its holders got 21% of the new equity); one with neither an OTC close
  nor a reviewed plan valuation is ``needs_review``: the old shares at a junction are never a D5 case.
  A junction security whose master end the snapshots do not date (Vroom: no 2024 Form 25 in the step-3
  table, the new shares still listed) is in scope with ``end_source`` relist_junction, ending on its old
  shares' last Nasdaq session. When the new shares were themselves delisted later (Frontier: FYBR,
  2026-01-30), the row values that later end and the old shares go to the ``relist_old_shares_*``
  columns (REVIEWED_OLD_SHARES: last Nasdaq session, return, status, document, note);
- ``unknown``: no SEC evidence decided it; listed explicitly, never defaulted.

A 1:1 holding-company reorganisation or reincorporation that the reconcile step's ``SUCCESSOR_LINKS``
continues (owner convention of 2026-10-02, as CRSP keeps one PERMNO: Google -> Alphabet, Apache -> APA, ...)
stays a ``stock_merger`` / ``reorganization`` of 1 share with ``acquirer_security_id`` the successor, but books no
terminal return (status ``no_terminal_return``, ``continued_as`` the successor, ``last_price_date`` the reconcile
cut): the series runs on in the successor, whose first return is measured from this last close, so a terminal
return would count that day twice. The links that do not continue (21CF, Pinnacle, Angie's List, AspenTech; round
10: Investors Bancorp's 2.55 conversion, Uniti's 0.6029 and Amerant's Class B folded into Class A) keep their
terminal value; the reconcile step cuts their series at the same last session. Round 10 added the 1:1
reorganisations and renames of REVIEWED that the master gives no successor link (ESRX, MRVL, ASRT, SBGI, VNOM, RTIX,
Z, LBTYB, LMCA/LMCK, LINTA/LINTB, QVCA/QVCB, QRTEA/QRTEB, and Bank of the Ozarks into Bank OZK): they continue too,
so they book no terminal return.

The automatic reading is overridden by ``REVIEWED`` (hand review of the documents named) for
the rows it gets wrong: another company's terms in the same 8-K, ADS ratios, elections,
renames, reorganisations the snapshots date late, spin-off adjustments (Compuware). ``main`` folds the round-10
hand-review verdicts (``CACHE/review/round10/merged/terminal_verdicts.csv``, written by
``scripts/reversal_data_review.py``) into REVIEWED before the build (``load_review_verdicts``; ``--review-verdicts``
reads another file, ``--no-review-verdicts`` none): a verdict's fields replace the code's (a verdict with a
terminal_type restates the whole consideration), ``approve`` / ``correct`` / ``decide_type`` verdicts with an
approval and a url lift the guard, ``price_gap`` verdicts add their checked terms and limits but no approval, ``hold``
verdicts their reason; the output note names the verdict (``hand review <item_id> (<verdict>)``).

Existing sourced rows are reused: ``stocks_list_dir/nasdaq/terminal_returns.csv``, the
holdout supplement and the sue_lt 2020 file, i.e. the three files that
``research_sue_lt_2020_2026.terminal_map`` merges (``complete_terminal``'s 0.0 default is not
used). Their consideration (and source URL) fills a value this step cannot price; the return
is recomputed against this step's vendor last close and the difference reported.

Last price: the last session with volume > 0 on or before the listing end (a merger's Form 25
filing date; an issuer's own Form 25 effective date; the day before a stated suspension; the
session before a halt the closing 8-K states, 'prior to the open of trading on <day>' or 'on the
Closing Date' (dated by the 8-K's filing day); the last closing 8-K for a snapshot-dated end, else
the end plus SNAPSHOT_END_SLACK_DAYS cut before the first run of filler rows or a raw price break
above 50% (SBNY); a reviewed ``limit``), over every vendor raw source (WIKI, the old Tiingo caches
and the step-8 files, Yahoo step 7 and the holdout charts) and the stored files. A successor
reorganisation the master links (``successor_date``, the Form 25 Nasdaq filed for it) with a
snapshot-dated end ends by that date, since the later rows under the same ticker are the
successor's (SSYS, LILA, SOHU); and for every end, the closing 8-K's own statements cap it further:
a halt 'as of (the) close of business on <day>' (ENDP) or a Form 25 filed 'after the close of trading
on <day>' (AVGO 2016), an effective time after the close ('On December 29, 2017 at 5:00 p.m., New York
City time (the "Distribution Date"), Liberty Global plc (the "Company") completed', LILAK; OZRK), the
session before an effective time at midnight ('12:01 a.m., Eastern Time, on August 19, 2025', VNOM),
and for a stock part the session before the day the new shares begin trading ('will begin trading on
July 1, 2016 under the symbol "CATM"', 'As of the open of trading on May 5, 2022, shares of New
DraftKings ... will trade', 'effective as of open of trading on February 24, 2025, will trade', 'on
August 19, 2025, New Viper Class A Common Stock began trading'), each within HALT_WINDOW_DAYS before
the end. A rename the master does not link (QRTEA -> QVCGA) is dated by REVIEWED ``limit``; a
snapshot-dated stock or mixed row whose last trade sits at the snapshot-slack limit while the stored
series ends earlier is held (``slack_overrun``: the later vendor rows may be a successor's). A vendor filler
row (plan rule R5: the previous close repeated on less than 5% of the median volume of the last
20 real sessions, e.g. ATVI 2023-10-13 on 1 share) is not a session: not a last trade, not an
acquirer close, not an OTC close. The close must come from a vendor on that session and, for a
merger, within a week of the Form 25 filing. A stock part is valued at the acquirer's vendor
close on the next XNAS session (or the target's own series under the same ticker for a
one-for-one reorganisation, rename or same-ticker conversion; on a tie the own series wins; or the
series of the acquirer's master predecessor under the same ticker after its successor date, as for
VMED's Liberty Global plc legs); a first close 2 to 5 sessions later gives a value held as
``needs_review``, a later one none. A special dividend paid to holders at the closing (NGHC, DELL,
CHNG, STAY, KRFT: REVIEWED ``special_dividend``) is added to the value, since the last close still
carries it (``special_dividend_cash``); a closing 8-K that names a special dividend REVIEWED does
not settle holds the row for review.
Otherwise:
- ``pending_price``: a Tiingo fetch for the security is planned or running and its file has
  not arrived (the run of October 2026, or month 2); rerun the build when it has;
- ``no_vendor_price``: no vendor reaches the last session (the stored files are never used
  for levels);
- ``needs_acquirer_price``: the stock part needs an acquirer close no vendor series has
  (``--yahoo-acquirers`` fetched 20 charts for the most important ones, ACQUIRER_SYMBOLS);
- ``needs_review``: the guard (an election, a proration or a CVR in the consideration, or a value
  more than GUARD_RETURN = 5% from the last close, except a bankruptcy's OTC close or sourced
  cancellation), an acquirer close 2 to 5 sessions late, an unreviewed special dividend, a
  ``slack_overrun`` or a reviewed ``hold`` (a data check on that one security, e.g. APA 2021: the
  documents state no effective time). The guard yields only to a REVIEWED ``approved`` entry with
  its own ``url``. The value (and ``consideration_per_share``) stays in the local cache; a reviewed
  ``hold_last_session`` (APA, SBGI) also leaves ``last_price_date`` blank. Every row to check is in
  ``CACHE/terminal/manual_review_queue.csv``, with the rows not yet priced on which a reason stands.

Committed columns hold SEC facts, dates and returns only: ``consideration_per_share`` (and
``existing_consideration_per_share``) is blank where the value comes from a vendor close (stock
parts, OTC closes, and existing-file values that price a stock leg), and copied notes are
stripped of closes; the levels are in ``CACHE/terminal/prices_used.csv``. One exception to the
next-session convention: an existing-file value for a stock leg prices the acquirer at that
file's close (often the target's last day); such rows say so (``existing_row_value_stock_leg``).
An existing row whose value is only the last close (no acquirer price there) is not used.

Outputs:
  INPUTS/terminal_returns_2012_2026.csv   one row per scoped security (plan section 1.1 columns first)
  CACHE/terminal/scope.csv                the scope with every fact used
  CACHE/terminal/filings.csv              the SEC filings near each end that were considered
  CACHE/terminal/evidence.csv, leads.csv.gz   terms and flags per security; every lead read
  CACHE/terminal/prices_used.csv          last close and acquirer close used (vendor levels: local only)
  CACHE/terminal/terminal_summary.json    counts by terminal_type and status; unknowns by best rank
  CACHE/terminal/manual_review_queue.csv  security_id, ticker, best_rank, reason, documents: every row to check by hand
  CACHE/terminal/for_reconcile_owner.csv  special dividends the terminal value owns (and any booking of them in the
                                          canonical series: round 9's reconcile drops such a booking, so the action
                                          should read "none"), exchange moves whose last Nasdaq session no vendor dates
  INPUTS/exchange_moves.csv               plan 1.1 (security_id, ticker, date = the first session on the new exchange,
                                          from_exchange, to_exchange, source_url; then last_nasdaq_session, date_basis,
                                          source, terminal_status, note): this step's exchange_move rows, the reconcile
                                          step's series_ends.csv exchange moves this step has no row for, and the
                                          holdout's nasdaq_listing_overrides.csv. Scope (EXCHANGE_MOVES_SCOPE): moves
                                          off Nasdaq 2012-2026 (this step and series_ends); moves onto Nasdaq only from
                                          the holdout table, so to 2019 (none after 2019 is listed: no source of this
                                          step names them); a holdout row's from_exchange is blank unless its text
                                          names the old exchange, its source_url blank when it is a snapshot bracket,
                                          its security_id blank when no Nasdaq interval of the ticker lies within 400
                                          days
  CACHE/reconcile/series_ends.csv         its terminal_2012_2026 column refreshed from this build (reconcile runs first
                                          and can only quote the previous terminal table)
  CACHE/terminal/yahoo_raw/               the acquirer charts
  CACHE/terminal/yahoo_otc/               a record of a fetch made by hand on 2026-10-02, outside this module (3 Yahoo
                                          requests 2 s apart for the old shares' OTC tails at relist junctions:
                                          WGHTQ, OPITS, CORZQ, all 404). The module neither reads nor writes it and
                                          requests no OTC chart; REVIEWED's WW entry cites the 404
  CACHE/terminal/review_docs/             SEC documents read by hand for REVIEWED that no stage fetches (the
                                          successors' 8-K12Bs of QuidelOrtho and APA; BRCM's election results: the
                                          2016-01-26 425s and Avago's closing 8-K EX-99.2; SEC_LIMITER, sec_headers)
  CACHE/raw/sec/docs/{cik}/{accession}/{document}.gz   the SEC documents read (and index.htm.gz), with the ones read
                                          by hand for the relist junctions (the CORZ and OPI emergence 8-Ks and OPI's
                                          EX-99.1, WW's 10-Q for the quarter to 2025-06-30, Thryv's 2020 424B4, Dex
                                          Media's 2016-01-05 8-K)
  ``--out-dir DIR`` writes the INPUTS files under DIR/inputs and the CACHE/terminal outputs (and the refreshed
  series_ends.csv) under DIR/terminal, and nothing else: the repo's earlier SEC envelopes are read but not copied
  into CACHE/raw/sec/docs. ``--reconcile-out ROOT`` reads series_ends.csv and the canonical prices from a reconcile
  ``--out-dir`` build instead of CACHE.

SEC requests (the fetch stages only) go through this process's own limiter, at most 4 a second.

Usage::

    PYTHONPATH=. python scripts/reversal_data_terminal.py --offline   # rebuild from the cache (after Tiingo files arrive)
    PYTHONPATH=. python scripts/reversal_data_terminal.py --offline --out-dir /tmp/terminal_dry  # the same, into scratch
    PYTHONPATH=. python scripts/reversal_data_terminal.py             # fetch missing SEC documents, then build
    PYTHONPATH=. python scripts/reversal_data_terminal.py --no-fetch --yahoo-acquirers   # acquirer charts (capped)
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import html
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common
from scripts import reversal_data_review as review_merge
from scripts.reversal_data_reconcile import RELIST_JUNCTIONS, SUCCESSOR_LINKS

MAIN = common.MAIN_CHECKOUT
INPUTS = common.INPUTS
# CACHE/terminal holds what this step reads back (the acquirer and OTC charts, the provenance index, the review
# documents) and, by default, what it writes; ``--out-dir`` sends the outputs elsewhere (``configure_paths``)
TERMINAL_CACHE = common.CACHE / "terminal"
OUT = TERMINAL_CACHE
SEC_RAW = common.RAW / "sec"
DOCS = SEC_RAW / "docs"
SUB_DIR = SEC_RAW / "submissions"
MASTER = INPUTS / "security_master.csv"
CANDIDATES = INPUTS / "candidate_fetch_list.csv"
FORM25 = INPUTS / "form25_nasdaq_2012_2026.csv"
INTERVALS = INPUTS / "ticker_intervals.csv"
PREFILTER = common.CACHE / "prefilter"
FACTS = PREFILTER / "security_facts.csv.gz"
DAILY = PREFILTER / "daily_series.pkl"
TIINGO_STATUS = common.CACHE / "tiingo" / "fetch_status.csv"
YAHOO_DIR = common.CACHE / "yahoo"
YAHOO_REPORT = YAHOO_DIR / "entity_report.csv"
OUTPUT = INPUTS / "terminal_returns_2012_2026.csv"
EXISTING_FILES = (
    MAIN / "stocks_list_dir" / "nasdaq" / "terminal_returns.csv",
    Path("output/research_only/holdout_2011_2019/inputs/terminal_returns_supplement.csv"),
    Path("output/research_only/sue_lt_2020_2026/inputs/terminal_returns_2020.csv"),
)
WINDOW_END = "2026-08-31"
TOP_RANK = 300
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{document}"
SEC_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}-index.htm"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def read_csv_text(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _cik_int(value) -> int | None:
    text = str(value).strip()
    if not text or text.lower() in ("nan", "<na>", "none"):
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _shift(day: str, days: int) -> str:
    return (pd.Timestamp(day) + pd.Timedelta(days=days)).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ scope


def terminal_candidates(master: pd.DataFrame | None = None, candidates: pd.DataFrame | None = None,
                        facts: pd.DataFrame | None = None, form25: pd.DataFrame | None = None) -> pd.DataFrame:
    """Every candidate-list or rank <= 300 security whose Nasdaq listing ends by WINDOW_END.

    ``end_date`` is the Form 25 effective date when there is one, else the transfer date,
    else the last day the snapshots list the security.
    """
    master = read_csv_text(MASTER) if master is None else master
    candidates = read_csv_text(CANDIDATES) if candidates is None else candidates
    facts = read_csv_text(FACTS) if facts is None else facts
    form25 = read_csv_text(FORM25) if form25 is None else form25
    rank = pd.to_numeric(facts["best_rank"], errors="coerce")
    ranked = set(facts.loc[rank <= TOP_RANK, "security_id"])
    listed = set(candidates["security_id"])
    scope_ids = ranked | listed
    fact = facts.set_index("security_id")
    rows = master[master["security_id"].isin(scope_ids)].copy()
    rows["in_candidates"] = rows["security_id"].isin(listed).map({True: "Y", False: "N"})
    rows["best_rank"] = rows["security_id"].map(pd.to_numeric(fact["best_rank"], errors="coerce"))
    rows["active_nasdaq"] = rows["security_id"].map(fact["active_nasdaq"]).fillna("")
    rows["facts_last_listed"] = rows["security_id"].map(fact["last_listed"]).fillna("")
    rows["last_ticker"] = rows["security_id"].map(fact["last_ticker"]).fillna(rows["first_ticker"])
    rows["tickers"] = rows["security_id"].map(fact["tickers"]).fillna(rows["first_ticker"])
    rows["last_universe_week"] = rows["security_id"].map(fact["last_universe_week"]).fillna("")
    delist = rows["delist_date"].where(rows["delist_date"].ne(""), None)
    ended = (rows["active_nasdaq"].str.lower() != "true") | (delist.notna() & (delist.fillna("9999") <= WINDOW_END))
    # the old shares at a relist junction whose Nasdaq end the master does not date (Vroom: no 2024 Form 25 in the
    # step-3 table, and the new shares still trade): in scope, ending on their last Nasdaq session
    junction_only = rows["security_id"].isin(set(RELIST_JUNCTIONS)) & ~ended
    keep = ended | junction_only
    rows = rows[keep].copy()
    rows["junction_only"] = junction_only[keep].to_numpy(dtype=bool)
    by_accession = form25.drop_duplicates("accession").set_index("accession")
    keep = ["effective_date", "filing_date", "rule_provision", "delisting_basis", "classification",
            "subject_exit", "successor_cik", "successor_tickers", "tickers_new", "tickers_ended", "doc_url"]
    for column in keep:
        rows[f"f25_{column}"] = rows["delist_form25_accession"].map(by_accession[column]).fillna("")
    rows["transfer_basis"] = rows["transfer_form25_accession"].map(by_accession["delisting_basis"]).fillna("")
    rows["transfer_form25_doc_url"] = rows["transfer_form25_accession"].map(by_accession["doc_url"]).fillna("")
    rows["transfer_filing_date"] = rows["transfer_form25_accession"].map(by_accession["filing_date"]).fillna("")
    rows["end_date"] = [
        delist_date or transfer or last
        for delist_date, transfer, last in zip(rows["delist_date"], rows["transfer_date"], rows["last_listed"])]
    rows["end_source"] = ["form25" if d else ("transfer" if t else "snapshots")
                          for d, t in zip(rows["delist_date"], rows["transfer_date"])]
    for k in rows.index[rows["junction_only"]]:
        entry = RELIST_JUNCTIONS[rows.at[k, "security_id"]]
        rows.at[k, "end_date"] = entry.get("old_nasdaq_last_session") or entry["first_new_session"]
        rows.at[k, "end_source"] = "relist_junction"
    rows = rows.drop(columns="junction_only")
    # a security with no listing end at all (no Form 25, no transfer, no listed interval: Ford's lone 2019 snapshot
    # interval, removed by step 4) has nothing to value; it is left out and named in the log
    undated = rows["end_date"].fillna("").eq("")
    if undated.any():
        log(f"scope: left out {int(undated.sum())} securities with no listing end: "
            f"{' '.join(rows.loc[undated, 'security_id'])}")
        rows = rows[~undated].copy()
    intervals = read_csv_text(INTERVALS) if INTERVALS.exists() else pd.DataFrame(columns=["security_id", "end", "source_url"])
    last_interval = intervals.sort_values("end").drop_duplicates("security_id", keep="last").set_index("security_id")
    rows["listing_source_url"] = rows["security_id"].map(last_interval["source_url"]).fillna("")
    rows = rows.sort_values(["best_rank", "security_id"], na_position="last").reset_index(drop=True)
    return rows


# ------------------------------------------------------------------ SEC filings near each end

CLOSING_ITEMS = {"2.01", "3.01", "5.01", "1.03", "3.03"}
CLOSING_BEFORE_DAYS = 60   # the closing 8-K is filed on the closing day, about 10 days before the
CLOSING_AFTER_DAYS = 20    # Form 25 takes effect; a late 8-K/A can follow
NEAR_DAYS = 10             # an 8.01-only 8-K this close to the end may carry the terms
AGREEMENT_BEFORE_DAYS = 550  # merger agreements are signed up to about 18 months before closing
EVENT_FORMS = {"8-K", "8-K/A"}
PROXY_FORMS = {"DEFM14A", "DEFM14C", "S-4", "S-4/A", "F-4", "F-4/A", "SC 14D9", "SC TO-T", "SC 13E3",
               "DEFA14A", "425", "PREM14A", "SC 14D9/A", "SC TO-T/A"}
DEREGISTRATION_FORMS = {"15-12B", "15-12G", "15-15D", "15-12B/A", "15-12G/A"}
_STOP = {"set": False}


class StopFetching(Exception):
    """SEC refused a request (403/429): stop asking and leave the rest for a later run."""


def _items(text: str) -> set[str]:
    return {part.strip() for part in str(text).split(",") if part.strip()}


def company_table(cik: int, offline: bool = False) -> pd.DataFrame:
    """Every filing of ``cik`` from the cached submissions JSON and its older pages (step 4/10 cache)."""
    from scripts import reversal_data_earnings as earnings

    payload = earnings.load_submissions(cik, offline=offline)
    if payload is None:
        return pd.DataFrame(columns=earnings.FILING_FIELDS)
    names = [f["name"] for f in (payload.get("filings") or {}).get("files") or [] if f.get("name")]
    pages = [p for p in (earnings.load_page(name, offline=True) for name in names) if p is not None]
    return earnings.filing_table(payload, pages)


SNAPSHOT_AFTER_DAYS = 60  # a snapshot-dated end can precede the closing 8-K by a capture gap


def end_filings(table: pd.DataFrame, end_date: str, form25_filing: str = "", after_days: int = CLOSING_AFTER_DAYS) -> pd.DataFrame:
    """The filings that can say how a listing ended, each with a ``kind``.

    closing: an 8-K with Item 2.01/3.01/5.01/1.03/3.03 from 60 days before the end (or before the
    Form 25 filing) to 20 days after it; near: an 8.01-only 8-K within 10 days; agreement: an
    8-K with Item 1.01 in the 550 days before; proxy and deregistration forms for context.
    """
    if table.empty:
        return table.assign(kind=[])
    anchor = min(d for d in (end_date, form25_filing) if d)
    lo, hi = _shift(anchor, -CLOSING_BEFORE_DAYS), _shift(end_date, after_days)
    rows = []
    for row in table.itertuples(index=False):
        day, form, items = row.filingDate, row.form, _items(row.items)
        kind = ""
        if form in EVENT_FORMS and lo <= day <= hi and items & CLOSING_ITEMS:
            kind = "closing"
        elif form in EVENT_FORMS and _shift(end_date, -NEAR_DAYS) <= day <= _shift(end_date, NEAR_DAYS) and "8.01" in items:
            kind = "near"
        elif form in EVENT_FORMS and _shift(anchor, -AGREEMENT_BEFORE_DAYS) <= day < lo and "1.01" in items:
            kind = "agreement"
        elif form in PROXY_FORMS and _shift(anchor, -AGREEMENT_BEFORE_DAYS) <= day <= hi:
            kind = "proxy"
        elif form in DEREGISTRATION_FORMS and lo <= day <= _shift(end_date, 400):
            kind = "deregistration"
        elif form in ("25-NSE", "25", "25/A") and lo <= day <= hi:
            kind = "form25"
        if kind:
            rows.append({**row._asdict(), "kind": kind})
    return pd.DataFrame(rows, columns=list(table.columns) + ["kind"])


def doc_path(cik: int, accession: str, document: str) -> Path:
    return DOCS / str(int(cik)) / accession / (document + ".gz")


def doc_url(cik: int, accession: str, document: str) -> str:
    return SEC_ARCHIVES.format(cik=int(cik), folder=accession.replace("-", ""), document=document)


def index_url(cik: int, accession: str) -> str:
    return SEC_INDEX.format(cik=int(cik), folder=accession.replace("-", ""), accession=accession)


_PROVENANCE_CACHE: dict[str, Path] | None = None
PROVENANCE_CACHE_DIR = MAIN / "output" / "data_provenance" / "sec_terminal_filing_cache"


def _provenance_cache() -> dict[str, Path]:
    """source_url -> envelope path for the repo's earlier SEC filing cache (2024-2026 terminal work)."""
    global _PROVENANCE_CACHE
    if _PROVENANCE_CACHE is None:
        index_path = TERMINAL_CACHE / "provenance_cache_index.json"
        if index_path.exists():
            _PROVENANCE_CACHE = {k: Path(v) for k, v in json.loads(index_path.read_text()).items()}
        else:
            found = {}
            for path in sorted(PROVENANCE_CACHE_DIR.glob("*.json.gz")) if PROVENANCE_CACHE_DIR.exists() else []:
                try:
                    with gzip.open(path, "rt", encoding="utf-8") as handle:
                        found[json.load(handle)["source_url"]] = path
                except Exception:  # an unreadable envelope is simply not reused
                    continue
            _PROVENANCE_CACHE = found
            if not READ_ONLY_SHARED:
                TERMINAL_CACHE.mkdir(parents=True, exist_ok=True)
                common.atomic_write(index_path, json.dumps({k: str(v) for k, v in found.items()}, indent=0).encode())
    return _PROVENANCE_CACHE


# ``--out-dir`` (a scratch build): nothing is written outside the out-dir, so the repo's earlier SEC envelopes are read
# but not copied into CACHE/raw/sec/docs, and the provenance index is not written
READ_ONLY_SHARED = False


def _from_provenance(url: str) -> bytes | None:
    path = _provenance_cache().get(url)
    if path is None:
        return None
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = bytes.fromhex(envelope["payload_hex"])
    if common.sha256_bytes(payload) != envelope.get("payload_sha256"):
        return None
    return payload


# This process's own SEC pace: at most 4 requests a second (SEC allows 10 in all, and other steps may run at once)
SEC_LIMITER = common.SlidingWindowLimiter({1: 4})


def fetch_sec(url: str, path: Path, symbol: str = "", offline: bool = False) -> bytes | None:
    """The body for ``url``: from our cache, else the repo's earlier SEC cache, else one request."""
    if path.exists():
        return gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    if path.with_name(path.name + ".404").exists():
        return None
    reused = _from_provenance(url)
    if reused is not None:
        if not READ_ONLY_SHARED:
            common.atomic_write(path, gzip.compress(reused, mtime=0) if path.suffix == ".gz" else reused)
        return reused
    if offline:
        return None
    if _STOP["set"]:
        raise StopFetching("stopped after an earlier refusal")
    try:
        return common.cached_get(url, path, source="sec_terminal_docs", headers=common.sec_headers(),
                                 limiter=SEC_LIMITER, symbol=symbol)
    except FileNotFoundError:
        return None
    except HTTPError as exc:
        if exc.code in (403, 429):
            _STOP["set"] = True
            raise StopFetching(f"HTTP {exc.code}") from None
        raise


def filing_index(cik: int, accession: str, offline: bool = False) -> list[dict]:
    """Documents of a filing from its -index.htm page: [{seq, description, document, type}]."""
    path = DOCS / str(int(cik)) / accession / "index.htm.gz"
    data = fetch_sec(index_url(cik, accession), path, f"CIK{int(cik)}", offline)
    if not data:
        return []
    text = data.decode("utf-8", errors="replace")
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)
        if len(cells) < 4:
            continue
        link = re.search(r'href="([^"]+)"', cells[2], flags=re.I)
        if not link:
            continue
        document = link.group(1).rsplit("/", 1)[-1]
        if document.startswith("ix?doc="):
            document = document.split("/")[-1]
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        out.append({"seq": clean(cells[0]), "description": clean(cells[1]), "document": document,
                    "type": clean(cells[3])})
    return out


# ------------------------------------------------------------------ reading the terms


def doc_text(data: bytes) -> str:
    """Plain text of an HTML or text SEC document, whitespace collapsed, quotes made plain."""
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</td>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ").replace("​", " ")
    text = re.sub("[“”„‟″]", '"', text)
    text = re.sub("[‘’′]", "'", text)
    text = re.sub("[‐-―]", "-", text)
    text = re.sub(r"\s+", " ", text).strip()
    # '(the " Offer Price ")' -> '(the "Offer Price")': some filers pad defined terms with spaces
    return re.sub(r'"\s+([^"]{1,80}?)\s+"', r'"\1"', text)


WORD_NUMBERS = {"one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0, "six": 6.0, "seven": 7.0,
                "eight": 8.0, "nine": 9.0, "ten": 10.0, "one-half": 0.5, "one half": 0.5}
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+"
MONEY = re.compile(r"(?:US)?\$\s?(" + NUMBER + r")")
PAR_VALUE = re.compile(r"par value(?: of)?\s*(?:US)?\$?\s*$|\bpar value\b[^.;]{0,6}$|nominal value[^.;]{0,6}$", re.I)
SHARES = re.compile(
    r"(?<![\w$.,])(" + NUMBER + r"|one|two|three|four|five|one-half)\s*(?:\(\s*" + NUMBER + r"\s*\)\s*)?"
    r"(?:\([^()]{0,60}\)\s*)?"
    r"(?:of\s+(?:an?|one)\s+)?"
    r"(?:validly issued,?\s+)?(?:fully paid and non-?assessable\s+)?"
    r"(?P<inline>(?:[A-Z][\w&.'-]*\s+){0,4}?)"
    r"(?:new\s+)?(?:Class [A-C]\s+|Series [A-C]\s+)?(?:ordinary\s+|common\s+|voting\s+)?"
    r"(?:shares?|Shares?|ADSs?|American [Dd]epositary [Ss]hares?)\b")
CONVERSION = re.compile(
    r"(?:converted (?:automatically |solely )?into(?: and became| and shall represent)?(?: only)? the right to receive|"
    r"converted into the right of the holder (?:thereof )?to receive|"
    r"(?:was|were|has been|have been|will be|shall be|is) (?:automatically )?converted into|"
    r"exchanged for|right to receive|entitled to receive|in exchange for|"
    r"(?:shareholders|stockholders|holders)(?: of [^.]{0,80}?)? (?:received|will receive|became entitled to receive)|"
    r"(?:offer|tender offer)\s*(?:\([^)]{0,40}\)\s*)?to (?:purchase|acquire) (?:any and )?all (?:of )?the (?:issued and )?"
    r"outstanding (?:[^.]|\.\d){0,300}?(?:(?:at|for) (?:a|an offer|a purchase|an) (?:offer )?price (?:per [Ss]hare )?(?:of|equal to)|"
    r"for(?= (?:\((?:i|1|a)\)\s*)?\$)))", re.I)
COMMON_SUBJECT = re.compile(
    r"(?:each|every|all)\s+(?:of the\s+)?(?:[\w'-]+\s+){0,5}(?:shares?|Shares?|ordinary shares?|common shares?|"
    r"American Depositary Shares?|ADSs?|units?)\b|shares? of (?:the Company's |our |its )?(?:class [a-c] |series [a-c] )?"
    r"common stock|Company Common Stock|Company Shares?|Common Shares|ordinary shares|outstanding shares", re.I)
NON_COMMON = re.compile(r"\b(?:options?|restricted stock units?|RSUs?|warrants?|preferred|performance|stock awards?|"
                        r"phantom|deferred stock|notes?|debentures?|awards?|SARs?|appreciation rights?|ESPP|"
                        r"purchase rights?|convertible|CVRs?|contingent value rights?|units? award|PSUs?|"
                        r"Depositary Shares representing|holders? of record)\b", re.I)
CLOSING_TERM = re.compile(r"(?:the|an?)\s+\"([^\"]{3,60})\"\s*\)")  # '..., together with the X, the "Offer Price")'
DEFINED_TERM = re.compile(r"\(\s*(?:the|each, the|each, an?|collectively, the|such amount, the|together, the|"
                          r"such amount being referred to as the|as adjusted, the)?\s*\"([^\"]{3,60})\"\s*\)")
TERM_REFERENCE = re.compile(r"\bthe\s+((?:Per Share |Common Stock |Cash |Stock |Share |Per-Share |Common Share |"
                            r"Mixed |Merger |Offer |Transaction |Scheme |Arrangement )?"
                            r"(?:Merger Consideration|Offer Price|Offer Consideration|Cash Consideration|"
                            r"Stock Consideration|Exchange Ratio|Per Share Price|Per Share Amount|Scheme Consideration|"
                            r"Transaction Consideration|Share Consideration|Cash Amount|Per Share Cash Amount|Closing Amount|"
                            r"Consideration|Price))", re.I)
CONSIDERATION_TERM = re.compile(r"Consideration|Offer Price|Exchange Ratio|Per Share Price|Per Share Amount|"
                                r"Cash Amount|Merger Price|Purchase Price|Share Price", re.I)
CVR = re.compile(r"contingent value rights?|\bCVRs?\b", re.I)
ELECTION = re.compile(r"\b(?:at the election of|at the holder's election|elect(?:ion|ed)? to receive|cash election|"
                      r"stock election|prorat)", re.I)
GENERIC_PARTIES = {"parent", "acquiror", "acquirer", "buyer", "purchaser", "holdco", "the company", "company",
                   "merger sub", "newco", "topco", "new holdco", "pubco", "surviving corporation"}


def _number(text: str) -> float | None:
    text = text.strip().lower()
    if text in WORD_NUMBERS:
        return WORD_NUMBERS[text]
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def money_amounts(text: str, start: int = 0, end: int | None = None) -> list[float]:
    """Dollar amounts in ``text[start:end]`` that read as a cash payment: not a par value (checked
    against the full text around each amount) and not a total ('$1.2 billion')."""
    out = []
    end = len(text) if end is None else min(end, len(text))
    for match in MONEY.finditer(text, start, end):
        before = text[max(0, match.start() - 25):match.start()]
        after = text[match.end():match.end() + 40]
        if PAR_VALUE.search(before) or re.match(r"\s*(?:par value|nominal value)", after, re.I):
            continue
        if re.match(r"\s*(?:million|billion|thousand|mm\b|bn\b)", after, re.I):
            continue
        if re.search(r"(?:\bby|divided by|dividing)\s*$", before, re.I):
            continue  # the divisor of a value-based exchange ratio ('dividing $45.60 by $169.42')
        if CVR.search(text[max(0, match.start() - 150):match.start()]) and not re.search(
                r"\bplus\b[^$]{0,40}$", text[max(0, match.start() - 60):match.start()]):
            continue  # a contingent value right's possible payment, not cash paid at closing
        value = _number(match.group(1))
        if value is not None and 0 < value < 100000:
            out.append(value)
    return out


VALUE_RATIO = re.compile(r"number of (?:[\w-]+ ){0,3}shares[^;]{0,220}?equal to (?:the (?:amount|quotient|number) "
                         r"(?:obtained|determined) by dividing |the quotient of (?:\(x\) )?)?\$\s?(" + NUMBER + r")"
                         r"(?: divided)?(?: by (?:\(y\) )?(?:\$\s?(" + NUMBER + r"))?)?", re.I)


def value_ratio(text: str) -> tuple[float | None, float | None]:
    """A stock part given as a value: '(a number of shares) equal to $45.60 divided by $169.42' ->
    (0.26915, 45.60); with a VWAP divisor only the value is known -> (None, 109.0)."""
    match = VALUE_RATIO.search(text)
    if not match:
        return None, None
    value = _number(match.group(1))
    divisor = _number(match.group(2)) if match.group(2) else None
    return (value / divisor if value and divisor else None), value


def share_amounts(text: str, start: int = 0, end: int | None = None) -> list[tuple[float, str]]:
    """(number, the 200 characters after it) for each count of shares in ``text[start:end]``."""
    out = []
    end = len(text) if end is None else min(end, len(text))
    for match in SHARES.finditer(text, start, end):
        before = text[max(0, match.start() - 12):match.start()]
        if "$" in before[-3:]:
            continue
        value = _number(match.group(1))
        if value is None or value <= 0 or value > 1000:
            continue
        inline = re.sub(r"\b(?:Class|Series|Common|Ordinary|New|Voting|[A-C])\b", "", match.group("inline") or "").strip()
        tail = text[match.end():match.end() + 200]
        out.append((value, (inline + " common stock " + tail) if inline else tail))
    return out


def acquirer_phrase(tail: str) -> str:
    """The issuer named after a share count: 'of Cigna common stock' -> 'Cigna'; 'of Class A Common
    Stock, $0.01 par value per share, of Simmons' -> 'Simmons'."""
    tail = re.sub(r"\([^()]*\)", " ", tail)
    tail = re.sub(r"\s+", " ", tail)
    skip = re.compile(r"(?:Class|Series|Common|Ordinary|New|Voting|Non-?Voting|American|Exchange|Merger|Effective)\b")
    direct = re.match(r"\s*(?:of\s+)?(?:the\s+)?([A-Z][\w&.'-]*(?:\s+(?:[A-Z][\w&.'-]*|of|and|&)){0,5}?)'?s?\s+"
                      r"(?:Series [A-C]\s+|Class [A-C]\s+|voting\s+|non-?voting\s+)?(?:common stock|Common Stock|ordinary shares?|"
                      r"Ordinary Shares?|common shares?|Common Shares?|American Depositary|ADS)", tail)
    if direct and not skip.match(direct.group(1)):
        return clean_party(direct.group(1))
    for of in re.finditer(r"\bof\s+(?:the\s+)?([A-Z][\w&.'-]*(?:\s+(?:[A-Z][\w&.'-]*|&)){0,5})", tail):
        if not skip.match(of.group(1)):
            return clean_party(of.group(1))
    return ""


def resolve_party(text: str, phrase: str) -> str:
    """'Parent' -> the name defined as ("Parent") in the document, else ``phrase``."""
    if phrase.lower() not in GENERIC_PARTIES:
        return phrase
    match = re.search(r"([A-Z][\w&.,'-]*(?:\s+[A-Z&][\w&.,'-]*){0,6}),?\s*"
                      r"(?:an?\s+[A-Za-z ]{0,60}?(?:corporation|company|limited|plc|partnership|entity|N\.V\.|S\.A\.|"
                      r"public limited company|limited liability company)\s*,?\s*)?"
                      r"\(\s*(?:the\s+)?\"" + re.escape(phrase) + r"\"\s*\)", text)
    return clean_party(match.group(1)) if match else phrase


def clean_party(name: str) -> str:
    """'Take-Two's' -> 'Take-Two'; 'Chevron. No' -> 'Chevron'."""
    name = re.sub(r"'s(?=\s|$).*$", "", name.strip(" ,."))
    name = re.split(r"\.\s+[A-Z]", name)[0]
    return name.strip(" ,.")


def define_terms(text: str) -> dict[str, dict]:
    """Defined consideration terms: term -> {'cash': [...], 'shares': [...]}, read from the 250
    characters before the term's '(the "Term")' definition (the last amount there), or, for a
    definition that announces a list ('the following consideration (the "Merger Consideration"):'),
    from the 300 characters after it."""
    terms: dict[str, dict] = {}
    matches = sorted({m.start(1): m for m in list(DEFINED_TERM.finditer(text)) + list(CLOSING_TERM.finditer(text))}.values(),
                     key=lambda m: m.start())
    for match in matches:
        term = match.group(1).strip()
        if not CONSIDERATION_TERM.search(term):
            continue
        lo = max(0, match.start() - 250)
        cash = money_amounts(text, lo, match.start())
        shares = share_amounts(text, lo, match.start())
        if re.search("Exchange Ratio", term, re.I) and not shares:
            number = re.findall(r"(?<![\w$.,])(" + NUMBER + r")\s*$", text[lo:match.start()])
            shares = [(float(number[-1].replace(",", "")), text[match.end():match.end() + 200])] if number else []
        forward = re.match(r"[^.;$]{0,40}?:", text[match.end():match.end() + 60])
        if forward or (not cash and not shares and re.search(r"following|as follows", text[lo:match.start()][-80:])):
            cash = money_amounts(text, match.end(), match.end() + 300)[:1] or cash
            shares = share_amounts(text, match.end(), match.end() + 300)[:1] or shares
            if forward:
                cash, shares = cash[:1], shares[:1]
        window = text[lo:match.start()]
        if CVR.search(window):  # 'the Closing Amount plus one CVR, collectively, the "Offer Price"'
            for known_term, known in list(terms.items()):
                if known["cash"] and re.search(re.escape(known_term), window, re.I) and known_term != term.lower():
                    cash = known["cash"]
                    break
        if cash or shares:
            terms.setdefault(term.lower(), {"cash": cash[-1:], "shares": shares[-1:], "position": match.start()})
    return terms


OWN_GENERIC = re.compile(r"\b(?:the Company|Company Common Stock|Company Shares?|Company common stock|our common stock|"
                         r"Company Ordinary Shares?|Company ADSs?|the Shares|each Share)\b")
OTHER_SUBJECT = re.compile(r"(?:share|shares|ordinary share|ordinary shares) of ([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})"
                           r"(?:'s)?\s+(?:common|ordinary|Class|Series|capital|voting|non-voting)")


LEGAL_SUFFIX = re.compile(r"\b(?:INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|LLC|LP|NV|SA|AG|SE|THE|DE|MD|NEW)\b")


def _alias_key(text: str) -> str:
    text = re.sub(r"/[A-Z]{2}/?$", "", str(text).upper())
    text = re.sub(r"[^A-Z0-9 ]+", " ", text.replace("&", " AND ").replace("'S ", " "))
    return re.sub(r"\s+", " ", LEGAL_SUFFIX.sub(" ", text)).strip()


def own_aliases(text: str, names: list[str]) -> list[str]:
    """Words that name the filing company: the first word and first two words of its names (legal
    suffixes dropped), plus every short name the document defines for it in the parenthesis after
    its name ('Applied Micro Circuits Corporation (the "Company" or "AMCC")')."""
    aliases = []
    for name in names:
        words = _alias_key(name).split()
        if words:
            if len(words[0]) >= 3:
                aliases.append(words[0])
            if len(words) > 1:
                aliases.append(" ".join(words[:2]))
    aliases = list(dict.fromkeys(aliases))
    for name in names:
        lead = re.escape(str(name).split()[0].strip(",.")) if str(name).split() else ""
        if not lead:
            continue
        for match in re.finditer(lead + r"([^()]{0,60}?)\(([^()]{0,120})\)", text[:30000], flags=re.I):
            if re.search(r"\b(?:and|with|by|between|among|of|to|from|or)\b", match.group(1)):
                continue  # the parenthesis belongs to another party named after ours
            for alias in re.findall(r"\"([^\"]{2,40})\"", match.group(2)):
                key = _alias_key(alias)
                if key and key not in ("COMPANY", "MERGER", "MERGER AGREEMENT", "PARENT", "MERGER SUB", "REGISTRANT"):
                    aliases.append(key)
    return list(dict.fromkeys(aliases))


STOCK_WORDS = r"(?:[Cc]ommon [Ss]tock|[Cc]apital [Ss]tock|[Oo]rdinary [Ss]hares?|[Cc]ommon [Ss]hares?|(?:Class|Series) [A-C] [Cc]ommon [Ss]tock)"
SUBJECT_NAME = re.compile(r"(?:share|shares|Share|Shares|ordinary share|ordinary shares) of (?:the\s+)?"
                          r"(?:" + STOCK_WORDS + r"(?:,\s*(?:par value|no par value|nominal value|without par value)[^,()]{0,30},?)?\s+of\s+(?:the\s+)?)?"
                          r"([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})|"
                          r"([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3}) (?:common stock|Common Stock|ordinary shares?|"
                          r"Ordinary Shares?|common shares?|Common Shares?)")
GENERIC_FIRST = {"COMPANY", "CLASS", "SERIES", "COMMON", "EACH", "DELAWARE", "SECTION", "MERGER", "EFFECTIVE", "PARENT",
                 "OUR", "ITS", "SUCH", "THE", "SHARES", "ORDINARY", "CAPITAL", "VOTING", "STOCK"}


def subject_owner(near: str, aliases: list[str]) -> str:
    """'own' when the subject of a conversion is the filing company's shares, 'other' when it names
    another company's shares, '' when it does not say. Only the subject's own words count (the first
    150 characters after 'each'), not the carve-outs that follow."""
    head = near[:150]
    key = " " + _alias_key(head) + " "
    if any(f" {a} " in key for a in aliases):
        return "own"
    match = SUBJECT_NAME.search(head)
    if match:
        raw = re.sub(r"'s\b", "", match.group(1) or match.group(2) or "")
        first = re.sub(r"[^A-Z0-9]", "", raw.split()[0].upper()) if raw.split() else ""
        if not first or first in GENERIC_FIRST:
            return "own"
        if any(f" {a} " in f" {_alias_key(raw)} " for a in aliases):
            return "own"
        return "other"
    if OWN_GENERIC.search(head[:90]):
        return "own"
    return ""


def _lead(cash, shares, refs, body, subject, near, label, position, text, owner="") -> dict:
    phrase = acquirer_phrase(shares[0][1]) if shares else ""
    flat = re.sub(r"\([^()]*\)", " ", body[:450])  # '(or, at the election of the holder, ordinary shares)' is not an alternative
    alternatives = re.search(r"\$\s?[\d.,]+[^$]{0,160}?\bor\b[^$]{0,100}?\d*\.?\d+\s+(?:[A-Z][\w&.-]*\s+){0,3}(?:shares?|of a share|ordinary)", flat) \
        or re.search(r"\d*\.?\d+\s+(?:[A-Z][\w&.-]*\s+){0,3}(?:shares?|of a share)[^$]{0,200}?\bor\b[^$]{0,100}?\$\s?[\d.,]+", flat)
    head_option = re.split(r"\bor\b", flat, maxsplit=1)[0]
    first_is_mixed = bool(MONEY.search(head_option) and SHARES.search(head_option))
    either = bool(cash and shares and alternatives and not first_is_mixed and ELECTION.search(body[:450] + subject[-300:]))
    return {
        "cash": cash[0] if cash else None,
        "cash_all": " ".join(f"{v:g}" for v in cash),
        "shares": shares[0][0] if shares else None,
        "acquirer_phrase": resolve_party(text, phrase) if phrase else "",
        "acquirer_class": (re.search(r"\b(?:Series|Class) [A-C]\b", shares[0][1][:120]) or [""])[0] if shares else "",
        "cvr": bool(CVR.search(body[:420])),
        "election": bool(ELECTION.search(subject[-400:] + body[:420])),
        "either_or": either,
        "stock_value": None,
        "subject_owner": owner,
        "refs": " ".join(refs),
        "snippet": (near[-220:] + " [" + label + "] " + body[:300]).strip(),
        "position": position,
    }


def conversion_leads(text: str, own_names: list[str] | None = None) -> list[dict]:
    """Each place a document says what one common share became (or the tender price), with the
    cash and share amounts read there; defined terms ('the Offer Price') are resolved."""
    terms = define_terms(text)
    aliases = own_aliases(text, own_names or [])
    leads = []
    for match in CONVERSION.finditer(text):
        subject = text[max(0, match.start() - 700):match.start()]
        last_each = max(subject.rfind("each "), subject.rfind("Each "), subject.rfind("every "))
        near = subject[last_each:] if last_each >= 0 else subject[-250:]
        if not (COMMON_SUBJECT.search(subject[-700:]) or COMMON_SUBJECT.search(match.group(0))
                or re.search(r"for each (?:share|Share|ordinary share|outstanding share)", text[match.end():match.end() + 300])):
            continue
        if NON_COMMON.search(near[:100]) or CVR.search(subject[-140:]):
            continue
        clause_end = match.end() + 420
        clause = text[match.end():clause_end]
        stop = re.search(r"\.\s+(?:[A-Z][a-z]+ )|; provided|\(the \"(?:Merger|Offer)\"\)", clause)
        body_len = stop.start() + 1 if stop and stop.start() > 20 else len(clause)
        body = clause[:body_len]
        if re.search(r"principal amount|per \$1,000", body[:220]):
            continue  # a debt instrument's conversion, not a share's
        cash = money_amounts(text, match.end(), match.end() + min(body_len, 260))
        shares = share_amounts(text, match.end(), match.end() + min(body_len, 260))
        ratio, part_value = value_ratio(body[:400])
        stock_value = part_value if ratio is None else None  # with the ratio known the value is not needed
        if part_value is not None:
            cash = [c for c in cash if abs(c - part_value) > 1e-9]
            if ratio is not None and not shares:
                shares = [(round(ratio, 6), body[body.find("shares of"):][:200] if "shares of" in body else "")]
        refs = [m.group(1).lower() for m in TERM_REFERENCE.finditer(body[:200])]
        for ref in refs:
            known = terms.get(ref) or next((v for k, v in terms.items() if k.endswith(ref) or ref.endswith(k)), None)
            if known:
                cash = cash or known["cash"]
                shares = shares or known["shares"]
        if not cash and not shares:
            continue
        lead = _lead(cash, shares, refs, body, subject, near, match.group(0), match.start(), text,
                     subject_owner(near, aliases))
        lead["stock_value"] = stock_value
        leads.append(lead)
    if not leads:  # the terms are only defined: '$74.00 per Share, net to the seller in cash (the "Offer Price")'
        for term, known in terms.items():
            if re.search(r"merger consideration|offer price|per share price|offer consideration|per share amount|"
                         r"cash consideration|scheme consideration|transaction consideration", term):
                window = text[max(0, known["position"] - 250):known["position"]]
                if NON_COMMON.search(window[-120:]):
                    continue
                leads.append(_lead(known["cash"], known["shares"], [term], window, window, window,
                                   f"defined: {term}", known["position"], text, ""))
    return leads


EXCHANGES = (("NYSE American", r"NYSE American|NYSE MKT|NYSE Amex|American Stock Exchange"),
             ("NYSE Arca", r"NYSE Arca"),
             ("Cboe BZX", r"Cboe BZX|BATS|Cboe Global Markets' BZX"),
             ("NYSE", r"New York Stock Exchange|\bNYSE\b"),
             ("OTC", r"OTC Markets|OTC Pink|OTCQX|OTCQB|OTC Bulletin Board|over-the-counter|Pink Sheets|pink sheets|OTC Link"))
TRANSFER = re.compile(r"(?:transfer(?:ring)? (?:of )?(?:the |its )?(?:stock exchange )?listing|list(?:ing)? (?:its |the Company's )?"
                      r"(?:common stock|shares|ordinary shares) on|begin trading on|commence trading on|"
                      r"move (?:its|the) (?:stock )?listing|switch(?:ing)? (?:its )?listing)", re.I)
BANKRUPTCY = re.compile(r"Chapter 11|Chapter 7|voluntary petitions?|Bankruptcy Court|plan of reorganization", re.I)
LIQUIDATION = re.compile(r"plan of (?:complete )?(?:liquidation|dissolution)|liquidating distribution|"
                         r"wind(?:ing)? up|dissolution of the Company|redeem all of (?:its|the) outstanding (?:public )?shares", re.I)
REORGANIZATION = re.compile(r"holding company reorgani[sz]ation|successor issuer|Rule 12g-3|"
                            r"redomicil|re-domicil|reincorporat|Reorganization\"?\)|scheme of arrangement", re.I)
REMOVAL = re.compile(r"suspend(?:ed|ing)? (?:trading|the trading)|delist(?:ing)? determination|Hearings Panel|"
                     r"Listing Qualifications|failure to (?:satisfy|comply)|did not regain compliance|"
                     r"minimum bid price|stockholders' equity requirement", re.I)


def exchanges_named(text: str) -> list[str]:
    return [name for name, pattern in EXCHANGES if re.search(pattern, text)]


def transfer_destination(text: str) -> str:
    """The exchange a listing moves to: the first one named after a sentence announcing a transfer
    ('to the New York Stock Exchange (the "NYSE") and the NYSE MKT, respectively' -> NYSE)."""
    for match in TRANSFER.finditer(text):
        window = text[match.start():match.end() + 260]
        found = []
        for name, pattern in EXCHANGES:
            if name == "OTC":
                continue
            hit = re.search(pattern, window)
            if hit and not (name == "NYSE" and re.match(r"NYSE (?:American|MKT|Amex|Arca)", window[hit.start():])):
                found.append((hit.start(), name))
        if found:
            return min(found)[1]
    return ""


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}
DATE_WORDS = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
                        r"\s+(\d{1,2}),?\s+(\d{4})")
SUSPENSION = re.compile(r"(?:trading (?:in|of) [^.]{0,120}?(?:was|were|will be|would be) suspended|"
                        r"suspend(?:ed|ing)? trading (?:in|of)|suspension of trading)[^.]{0,220}", re.I)


def suspension_date(text: str) -> str:
    """The date Nasdaq suspended trading, from the sentence announcing it ('' when not stated)."""
    for match in SUSPENSION.finditer(text):
        window = text[max(0, match.start() - 160):match.end()]
        dates = DATE_WORDS.findall(window[window.lower().find("suspend"):]) or DATE_WORDS.findall(window)
        if dates:
            month, day, year = dates[0]
            try:
                return f"{int(year):04d}-{MONTHS[month.lower()]:02d}-{int(day):02d}"
            except KeyError:
                continue
    return ""


MONTH_NAMES = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
WEEKDAY = r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday),?\s+)?"
DATE_TEXT = MONTH_NAMES + r"\s+(\d{1,2}),?\s+(\d{4})"
HALT_BEFORE_OPEN = re.compile(r"\b(?:halt|suspend)\w*\b[^.;]{0,220}?\b(?:prior to|before)\s+the\s+open(?:ing)?\b", re.I)
# 'the' is optional: ENDP 'suspended from trading on the NASDAQ as of close of business on February 28, 2014'
HALT_AFTER_CLOSE = re.compile(r"\b(?:halt|suspend)\w*\b[^.;]{0,40}?\b(?:after|following|as of|at|upon)\s+(?:the\s+)?close\s+of\s+"
                              r"(?:trading|the market|business)\b[^.;]{0,40}?\bon\s+" + WEEKDAY + DATE_TEXT, re.I)
# AVGO 2016: 'the NASDAQ filed a Form 25 with the SEC after the close of trading on January 29, 2016'
FORM25_AFTER_CLOSE = re.compile(r"\bForm 25\b[^.;]{0,80}?\b(?:after|following|as of|at|upon)\s+(?:the\s+)?close\s+of\s+"
                                r"(?:trading|the market|business)\s+on\s+" + WEEKDAY + DATE_TEXT, re.I)
# 21CF: 'suspended from trading on Nasdaq prior to the open of trading on the Merger Effective Date'
HALT_DAY = re.compile(r"^[^.;]{0,70}?\bon\s+" + WEEKDAY + r"(?:(?P<closing>the\s+(?:Merger\s+)?(?:Closing|Effective)\s+Date)|"
                      + DATE_TEXT + ")", re.I)
# A conversion that took effect after the close: 'On December 29, 2017 at 5:00 p.m., New York City time (the
# "Distribution Date"), Liberty Global plc (the "Company") completed its previously-announced split-off' (LILAK),
# 'effective as of 4:00 p.m., Central Time, on June 26, 2017' (OZRK). An hour of 4 to 11 p.m.; the sentence must say
# 'effective', or open with 'On <date> at <hour> p.m.' and say 'completed' or 'consummated' after it.
PM = r"(?:p\.\s?m\.|p\.?m\b|PM\b)"
EFFECTIVE_PM_AFTER = re.compile(DATE_TEXT + r",?\s+at\s+(\d{1,2})(?::\d{2})?\s*" + PM + r"(?P<rest>[^.;]{0,140})", re.I)
EFFECTIVE_PM_BEFORE = re.compile(r"\beffective\b[^.;]{0,40}?\b(?:as of|at)\s+(\d{1,2}):\d{2}\s*" + PM + r"[^.;]{0,60}?"
                                 r"\bon\s+" + WEEKDAY + DATE_TEXT, re.I)
COMPLETED = re.compile(r"\b(?:completed|consummated)\b", re.I)
# A conversion that took effect at midnight, before the open: 'the "Viper Pubco Merger Effective Time", which was
# 12:01 a.m., Eastern Time, on August 19, 2025' (VNOM). The shares did not trade on that day: the company's last
# session is the one before. Only a 12 o'clock a.m. time counts (21CF's 'effective at 7:25 a.m. Eastern Time on
# March 19, 2019' is the FOX distribution, and 21CF traded that day); the sentence must say 'effective' (or
# completed / consummated), and a tender offer's expiry ('expire at 12:01 a.m.') does not count.
AM = r"(?:a\.\s?m\.|a\.?m\b|AM\b)"
EFFECTIVE_AM_AFTER = re.compile(DATE_TEXT + r",?\s+at\s+12:\d{2}\s*" + AM + r"(?P<rest>[^.;]{0,140})", re.I)
EFFECTIVE_AM_BEFORE = re.compile(r"\b12:\d{2}\s*" + AM + r"(?P<rest>[^.;]{0,60}?)\bon\s+" + WEEKDAY + DATE_TEXT, re.I)
EXPIRY = re.compile(r"\bexpir|\btender\b", re.I)
# The day the shares that replace the company's begin trading: 'will begin trading on July 1, 2016 under the
# symbol "CATM"' (CATM), 'As of the open of trading on May 5, 2022, shares of New DraftKings ... will trade' (DKNG),
# 'expects trading in the ADSs ... to commence on June 1, 2018' (SOHU), 'effective as of open of trading on February
# 24, 2025, will trade on Nasdaq under the new ticker symbols "QVCGA", "QVCGB"' (QRTEA), and with the date before a
# past-tense verb: 'Accordingly, on August 19, 2025, New Viper Class A Common Stock began trading on Nasdaq' (VNOM).
# The company's last session is the one before.
START_VERB = re.compile(r"\b(?P<verb>beg[ia]n|begins|beginning|commenc(?:e|es|ed|ing)|start(?:s|ed|ing)?)\s+"
                        r"(?:regular[- ]way\s+)?trading\b", re.I)
START_DATE_AFTER = re.compile(r"[^.;]{0,200}?\bon\s+" + WEEKDAY + DATE_TEXT, re.I)
START_DATE_BEFORE = re.compile(r"\bon\s+" + WEEKDAY + DATE_TEXT, re.I)
SUCCESSOR_START = (
    re.compile(r"\btrading\s+(?:in|of)\s+[^.;]{0,160}?\b(?:to|will)\s+(?:begin|commence|start)\s+on\s+" + WEEKDAY + DATE_TEXT, re.I),
    re.compile(r"\b(?:as of|at|from|with)\s+(?:the\s+)?open(?:ing)?\s+of\s+(?:trading|the market|business)\s+on\s+" + WEEKDAY
               + DATE_TEXT + r"[^.;]{0,200}?\btrade", re.I),
)
WHEN_ISSUED = re.compile(r"when[- ]issued", re.I)
# A special dividend the closing 8-K names (NGHC 'plus a special pre-closing dividend of $2.50'): such a row is
# held for review unless REVIEWED says how the dividend is valued (``special_dividend``) or that it was paid
# before the last trade (``special_dividend_before_last_trade``).
SPECIAL_DIVIDEND = re.compile(r"\b(?:special|pre-closing|pre-merger)\s+(?:pre-closing\s+)?(?:cash\s+)?dividend\b", re.I)
NEW_EXCHANGE_START = re.compile(r"\b(?:begin|commence|start)(?:s|ed|ing)?\s+trading\b", re.I)
START_BEFORE = re.compile(r"\b(?:On|Effective)\s+" + WEEKDAY + DATE_TEXT + r",?[^.;]{0,120}$")


def _iso(month: str, day: str, year: str) -> str:
    try:
        return f"{int(year):04d}-{MONTHS[month.lower()]:02d}-{int(day):02d}"
    except (KeyError, ValueError):
        return ""


def halt_dates(text: str) -> dict:
    """What a closing 8-K says about the end of Nasdaq trading: ``before_open`` the day trading was
    halted or suspended before the open ('closing_date' when it says 'on the Closing Date'), and
    ``after_close`` the day it was halted after the close ('' when not stated)."""
    out = {"before_open": "", "after_close": ""}
    for match in HALT_BEFORE_OPEN.finditer(text):
        day = HALT_DAY.match(text[match.end():match.end() + 140])
        if day:
            out["before_open"] = "closing_date" if day.group("closing") else _iso(*day.group(2, 3, 4))
            if out["before_open"]:
                break
    days = [_iso(*m.groups()[-3:]) for p in (HALT_AFTER_CLOSE, FORM25_AFTER_CLOSE) for m in p.finditer(text)]
    days = [d for d in days if d]
    if days:
        out["after_close"] = min(days)
    return out


def _sentence_head(text: str, start: int, width: int = 160) -> str:
    """The part of the sentence before ``start`` (up to ``width`` characters)."""
    head = text[max(0, start - width):start]
    return head[max(head.rfind("."), head.rfind(";")) + 1:]


def effective_after_close(text: str) -> str:
    """The latest day a document says the conversion took effect after the close (an effective time of 4 to
    11 p.m.; a 12 o'clock p.m. is noon, inside the session); '' when not stated. The sentence says 'effective',
    or opens 'On <date> at <hour> p.m.' and says 'completed' or 'consummated' after it (LILAK)."""
    days = []
    for match in EFFECTIVE_PM_AFTER.finditer(text):
        hour = int(match.group(4))
        head = _sentence_head(text, match.start(), 120)
        stated = re.search(r"\beffective\b", head + match.group("rest"), re.I) or (
            re.search(r"\bOn\s+" + WEEKDAY + r"$", head) and COMPLETED.search(match.group("rest")))  # 'On', capital
        if 4 <= hour <= 11 and stated:
            days.append(_iso(*match.groups()[:3]))
    for match in EFFECTIVE_PM_BEFORE.finditer(text):
        if 4 <= int(match.group(1)) <= 11:
            days.append(_iso(*match.groups()[1:4]))
    days = [d for d in days if d]
    return max(days) if days else ""


def effective_before_open(text: str) -> str:
    """The latest day D a document says the conversion took effect at 12:xx a.m. (midnight, before the open):
    the shares did not trade on D. '' when not stated."""
    days = []
    for pattern, date_groups in ((EFFECTIVE_AM_AFTER, slice(0, 3)), (EFFECTIVE_AM_BEFORE, slice(-3, None))):
        for match in pattern.finditer(text):
            sentence = _sentence_head(text, match.start()) + match.group(0)
            if EXPIRY.search(sentence):
                continue
            if re.search(r"\beffective\b", sentence, re.I) or COMPLETED.search(sentence):
                days.append(_iso(*match.groups()[date_groups]))
    days = [d for d in days if d]
    return max(days) if days else ""


def successor_starts(text: str) -> list[str]:
    """The days a document says new shares begin trading (any exchange; when-issued trading does not count):
    the date after the verb ('will begin trading on July 1, 2016'), or, for a past-tense verb, the last date
    before it in the same sentence ('on August 19, 2025, New Viper Class A Common Stock began trading')."""
    out = []
    for match in START_VERB.finditer(text):
        head = _sentence_head(text, match.start(), 220)
        after = START_DATE_AFTER.match(text, match.end())
        if after:
            if not WHEN_ISSUED.search(match.group(0) + after.group(0)):
                out.append(_iso(*after.groups()[-3:]))
            continue
        if match.group("verb").lower() not in ("began", "commenced", "started") or WHEN_ISSUED.search(head):
            continue
        dated = list(START_DATE_BEFORE.finditer(head))
        if dated and not DATE_WORDS.search(head[dated[-1].end():]):  # the nearest date before the verb
            out.append(_iso(*dated[-1].groups()[:3]))
    for pattern in SUCCESSOR_START:
        for match in pattern.finditer(text):
            if WHEN_ISSUED.search(match.group(0)):
                continue
            out.append(_iso(*match.groups()[-3:]))
    return sorted({d for d in out if d})


def new_exchange_starts(text: str) -> list[str]:
    """The days a document says the shares begin trading on another exchange ('expects that its common
    stock will begin trading on the NYSE on July 15, 2013'); a sentence that names only Nasdaq (a new
    ticker there) does not count."""
    out = []
    for match in NEW_EXCHANGE_START.finditer(text):
        tail = re.match(r"[^.;]{0,220}", text[match.end():]).group(0)
        head = text[max(0, match.start() - 160):match.start()]
        head = head[max(head.rfind("."), head.rfind(";")) + 1:]
        named = [name for name, pattern in EXCHANGES[:-1] if re.search(pattern, head + " " + tail)]
        if not named:
            continue
        day = DATE_WORDS.search(tail)
        if day:
            iso = _iso(*day.groups())
        else:
            before = START_BEFORE.search(head)
            iso = _iso(*before.groups()) if before else ""
        if iso:
            out.append(iso)
    return out


def doc_evidence(text: str, own_names: list[str] | None = None) -> dict:
    """Flags and consideration leads read from one document's text."""
    leads = conversion_leads(text, own_names)
    halts = halt_dates(text)
    return {
        "leads": leads,
        "suspension_date": suspension_date(text),
        "halt_before_open": halts["before_open"],
        "halt_after_close": halts["after_close"],
        "effective_after_close": effective_after_close(text),
        "effective_before_open": effective_before_open(text),
        "successor_start": " ".join(successor_starts(text)),
        "special_dividend": bool(SPECIAL_DIVIDEND.search(text)),
        "transfer_start": " ".join(new_exchange_starts(text)),
        "transfer_to": transfer_destination(text),
        "bankruptcy": bool(BANKRUPTCY.search(text)),
        "liquidation": bool(LIQUIDATION.search(text)),
        "reorganization": bool(REORGANIZATION.search(text)),
        "removal": bool(REMOVAL.search(text)),
        "otc": bool(re.search(EXCHANGES[-1][1], text)),
        "cvr": bool(CVR.search(text)),
        "election": bool(ELECTION.search(text)),
        "chars": len(text),
    }


ADS_WORDS = re.compile(r"\bADSs?\b|American Depositary", re.I)


def best_terms(leads: list[dict], ads: bool = False) -> dict:
    """The consideration most of a security's leads agree on: cash, shares, acquirer, flags.
    For an ADS security the leads that speak of ADSs come first (the price per ADS)."""
    if not leads:
        return {}
    own = [l for l in leads if l.get("subject_owner") == "own"]
    neutral = [l for l in leads if not l.get("subject_owner")]
    leads = own or neutral
    if not leads:
        return {}
    if ads:
        per_ads = [l for l in leads if ADS_WORDS.search(l["snippet"])]
        leads = per_ads or leads
    votes: dict[tuple, list[dict]] = {}
    for lead in leads:
        key = (lead["cash"], lead["shares"])
        votes.setdefault(key, []).append(lead)

    def consistent(lead, key):  # a lead that names only one part agrees with a key that has it
        return lead["cash"] in (None, key[0]) and lead["shares"] in (None, key[1])

    def score(item):
        key, group = item
        support = sum(consistent(lead, key) for lead in leads)
        return (support, (key[0] is not None) + (key[1] is not None), len(group), -group[0]["position"])

    (cash, shares), group = max(votes.items(), key=score)
    acquirer = next((g["acquirer_phrase"] for g in group if g["acquirer_phrase"]), "")
    acquirer_class = next((g.get("acquirer_class") for g in group if g.get("acquirer_class")), "")
    return {"cash": cash, "shares": shares, "acquirer_phrase": acquirer, "acquirer_class": acquirer_class,
            "n_leads": len(leads),
            "n_agree": len(group), "cvr": any(g["cvr"] for g in leads), "election": any(g["election"] for g in leads),
            "either_or": any(g.get("either_or") for g in group), "subject_owner": group[0].get("subject_owner", ""),
            "stock_value": next((g["stock_value"] for g in group if g.get("stock_value")), None),
            "alternatives": "; ".join(f"{k[0]}|{k[1]}x{len(v)}" for k, v in votes.items() if k != (cash, shares)),
            "snippet": group[0]["snippet"][:600]}


def scope_filings(scope: pd.DataFrame, offline: bool = True) -> pd.DataFrame:
    """``end_filings`` for every scoped security (one row per security and filing)."""
    frames, tables = [], {}
    for row in scope.itertuples(index=False):
        cik = _cik_int(row.cik)
        if cik is None:
            continue
        if cik not in tables:
            tables[cik] = company_table(cik, offline=offline)
        found = end_filings(tables[cik], row.end_date, row.f25_filing_date,
                            SNAPSHOT_AFTER_DAYS if row.end_source == "snapshots" else CLOSING_AFTER_DAYS)
        if len(found):
            frames.append(found.assign(security_id=row.security_id, cik=cik))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["security_id", "filingDate", "accessionNumber"]).reset_index(drop=True)


EXHIBIT_DOC = re.compile(r"\.(?:htm|html|txt)$", re.I)
AGREEMENT_PER_SECURITY = 3  # the latest Item 1.01 8-Ks before the closing window


def _filing_jobs(filings: pd.DataFrame, sids: set[str] | None, kinds: tuple[str, ...], latest: int = 0) -> list[dict]:
    out = []
    if filings.empty:
        return out
    for sid, group in filings.groupby("security_id", sort=False):
        if sids is not None and sid not in sids:
            continue
        chosen = pd.DataFrame()
        for kind in kinds:  # the first kind present wins (closing, else near)
            chosen = group[group["kind"].eq(kind)]
            if len(chosen):
                break
        if latest:
            chosen = chosen.sort_values("filingDate").tail(latest)
        for row in chosen.itertuples(index=False):
            out.append({"security_id": sid, "cik": int(row.cik), "accession": row.accessionNumber,
                        "document": row.primaryDocument, "kind": row.kind, "role": "primary",
                        "filing_date": row.filingDate, "report_date": getattr(row, "reportDate", "") or "",
                        "form": row.form, "items": row.items})
    return out


def documents_to_fetch(filings: pd.DataFrame, stage: str = "primary", sids: set[str] | None = None,
                       offline_index: bool = True) -> list[dict]:
    """The documents a stage reads.

    primary:   each closing 8-K's main document (each near 8-K when a security has none);
    index:     the -index.htm page of those 8-Ks (lists the exhibits), for ``sids``;
    exhibits:  their EX-99 documents (press releases), from the cached index pages;
    agreement: the latest three Item 1.01 8-Ks before the closing window (main document and
               index page), for ``sids``; agreement_exhibits: their EX-99 documents.
    """
    if stage == "primary":
        return [j for j in _filing_jobs(filings, sids, ("closing", "near")) if j["document"]]
    if stage in ("index", "agreement_index"):
        kinds = ("closing", "near") if stage == "index" else ("agreement",)
        base = _filing_jobs(filings, sids, kinds, AGREEMENT_PER_SECURITY if stage == "agreement_index" else 0)
        return [{**j, "document": "index.htm", "role": "index"} for j in base]
    if stage == "agreement":
        return [j for j in _filing_jobs(filings, sids, ("agreement",), AGREEMENT_PER_SECURITY) if j["document"]]
    if stage in ("exhibits", "agreement_exhibits"):
        kinds = ("closing", "near") if stage == "exhibits" else ("agreement",)
        base = _filing_jobs(filings, sids, kinds, AGREEMENT_PER_SECURITY if stage == "agreement_exhibits" else 0)
        jobs = []
        for job in base:
            for entry in filing_index(job["cik"], job["accession"], offline=offline_index):
                if entry["type"].upper().startswith("EX-99") and EXHIBIT_DOC.search(entry["document"]):
                    jobs.append({**job, "document": entry["document"], "role": entry["type"].upper()})
        return jobs
    raise ValueError(f"unknown stage {stage}")


def security_documents(filings: pd.DataFrame, sid: str) -> list[dict]:
    """Every cached document this step can read for ``sid``: main documents and EX-99 exhibits of
    its closing (or near) and agreement 8-Ks, closing ones first."""
    group = filings[filings["security_id"].eq(sid)]
    docs = []
    for stage in ("primary", "exhibits", "agreement", "agreement_exhibits"):
        for job in documents_to_fetch(group, stage):
            if doc_path(job["cik"], job["accession"], job["document"]).exists():
                docs.append({**job, "stage": stage})
    return docs


FLAG_KEYS = ("bankruptcy", "liquidation", "reorganization", "removal", "otc", "cvr", "election")


def gather_evidence(scope: pd.DataFrame, filings: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per security: the terms read from its cached documents (closing ones before agreement ones)
    and the flags any closing document raises. Also every lead found, for review."""
    by_sid = {sid: group for sid, group in filings.groupby("security_id")} if len(filings) else {}
    rows, all_leads = [], []
    texts: dict[tuple, dict] = {}
    for row in scope.itertuples(index=False):
        group = by_sid.get(row.security_id, pd.DataFrame(columns=list(filings.columns)))
        docs = security_documents(group, row.security_id) if len(group) else []
        out = {"security_id": row.security_id, "n_docs": len(docs), "transfer_to": "", "transfer_start": "",
               "halt_before_open": "", "halt_after_close": "", "effective_after_close": "", "effective_before_open": "",
               "successor_start": "",
               "special_dividend": False,
               **{k: False for k in FLAG_KEYS},
               "closing_items": " ".join(sorted({i for items in group.loc[group["kind"].isin(["closing", "near"]),
                                                                          "items"] for i in _items(items)})),
               "closing_accessions": " ".join(sorted(set(group.loc[group["kind"].eq("closing"), "accessionNumber"]))),
               "closing_dates": " ".join(sorted(set(group.loc[group["kind"].eq("closing") & group["items"].map(
                   lambda items: bool(_items(items) & {"2.01", "3.03", "5.01"})), "filingDate"])))}
        leads_by_stage: dict[str, list[dict]] = {"closing": [], "agreement": []}
        for doc in docs:
            key = (doc["cik"], doc["accession"], doc["document"])
            if key not in texts:
                data = gzip.decompress(doc_path(*key).read_bytes())
                own = [row.name] + [re.sub(r"\s*\(.*$", "", part).strip()
                                    for part in str(row.former_names or "").split("|") if part.strip()]
                texts[key] = doc_evidence(doc_text(data), own)
            evidence = texts[key]
            stage = "closing" if doc["stage"] in ("primary", "exhibits") else "agreement"
            for lead in evidence["leads"]:
                lead = {**lead, "security_id": row.security_id, "cik": doc["cik"], "accession": doc["accession"],
                        "document": doc["document"], "role": doc["role"], "stage": stage,
                        "filing_date": doc["filing_date"], "source_url": doc_url(*key)}
                leads_by_stage[stage].append(lead)
                all_leads.append(lead)
            if stage == "closing":
                for flag in FLAG_KEYS:
                    out[flag] = out[flag] or evidence[flag]
                out["transfer_to"] = out["transfer_to"] or evidence["transfer_to"]
                out["suspension_date"] = out.get("suspension_date") or evidence["suspension_date"]
                # 'on the Closing Date' is dated by the 8-K's filing day (on or after the closing): a cap that
                # never cuts a real session, the filler rule does the rest
                halt = evidence.get("halt_before_open") or ""
                halt = doc["filing_date"] if halt == "closing_date" else halt
                if halt and doc["kind"] == "closing":
                    out["halt_before_open"] = min(d for d in (out.get("halt_before_open"), halt) if d)
                if evidence.get("halt_after_close") and doc["kind"] == "closing":
                    out["halt_after_close"] = min(d for d in (out.get("halt_after_close"), evidence["halt_after_close"]) if d)
                if evidence.get("effective_after_close") and doc["kind"] == "closing":
                    out["effective_after_close"] = max(out["effective_after_close"], evidence["effective_after_close"])
                if evidence.get("effective_before_open") and doc["kind"] == "closing":
                    out["effective_before_open"] = max(out["effective_before_open"], evidence["effective_before_open"])
                if evidence.get("successor_start") and doc["kind"] == "closing":
                    days = set(out["successor_start"].split()) | set(evidence["successor_start"].split())
                    out["successor_start"] = " ".join(sorted(d for d in days if d))
                out["special_dividend"] = out["special_dividend"] or bool(evidence.get("special_dividend"))
                starts = set(str(out.get("transfer_start") or "").split()) | set(str(evidence.get("transfer_start") or "").split())
                out["transfer_start"] = " ".join(sorted(d for d in starts if d))
        stage = "closing" if leads_by_stage["closing"] else "agreement"
        terms = best_terms(leads_by_stage[stage], ads=row.share_class == "ADS")
        if terms:
            best = [l for l in leads_by_stage[stage] if (l["cash"], l["shares"]) == (terms["cash"], terms["shares"])]
            best.sort(key=lambda l: (l["role"] != "primary", l["filing_date"]), reverse=False)
            out.update({f"terms_{k}": v for k, v in terms.items()})
            out.update({"terms_stage": stage, "terms_source_url": best[0]["source_url"],
                        "terms_accession": best[0]["accession"]})
        rows.append(out)
    return pd.DataFrame(rows), pd.DataFrame(all_leads)


def lacking_terms(evidence: pd.DataFrame, scope: pd.DataFrame) -> set[str]:
    """Securities whose end is not a plain removal and whose documents gave no consideration."""
    merged = scope[["security_id", "f25_delisting_basis"]].merge(evidence, on="security_id", how="left")
    no_terms = merged["terms_cash"].isna() & merged["terms_shares"].isna() if "terms_cash" in merged else \
        pd.Series(True, index=merged.index)
    removal = merged["f25_delisting_basis"].eq("exchange_removal")
    return set(merged.loc[no_terms & ~removal, "security_id"])


def fetch_documents(jobs: list[dict], offline: bool = False, workers: int = 6) -> dict:
    """Fetch every job's document (cache first); counts by outcome."""
    seen, unique = set(), []
    for job in jobs:
        key = (job["cik"], job["accession"], job["document"])
        if key not in seen:
            seen.add(key)
            unique.append(job)
    todo = [j for j in unique if not doc_path(j["cik"], j["accession"], j["document"]).exists()]
    log(f"documents: {len(unique)} unique, {len(todo)} not cached")
    _provenance_cache()  # built once here, before the worker threads read it

    def one(job):
        if job["document"] == "index.htm":
            return "ok" if filing_index(job["cik"], job["accession"], offline=offline) else "absent"
        data = fetch_sec(doc_url(job["cik"], job["accession"], job["document"]),
                         doc_path(job["cik"], job["accession"], job["document"]), f"CIK{job['cik']}", offline)
        return "ok" if data else "absent"

    counts: dict[str, int] = {}
    for start in range(0, len(todo), 200):
        chunk = todo[start:start + 200]
        for result in common.parallel_map(one, chunk, workers=workers):
            key = result if isinstance(result, str) else type(result).__name__
            counts[key] = counts.get(key, 0) + 1
        log(f"  fetched {min(start + 200, len(todo))}/{len(todo)}: {counts}")
        if _STOP["set"]:
            log("  SEC refused a request: stopping; rerun later")
            break
    return counts


# ------------------------------------------------------------------ acquirers and existing rows

NAME_SUFFIXES = re.compile(r"\b(?:INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|LLC|LP|L P|NV|N V|SA|S A|AG|SE|"
                           r"HOLDINGS?|GROUP|THE|DE|NEW|CLASS [A-C]|SERIES [A-C]|TRUST|BANCORP|BANCORPORATION|"
                           r"FINANCIAL|ENTERPRISES|INTERNATIONAL|INTL)\b")


def norm_name(name: str) -> str:
    text = re.sub(r"/[A-Z]{2}/?$", "", str(name).upper().strip())
    text = re.sub(r"[^A-Z0-9 ]+", " ", text.replace("&", " AND "))
    text = NAME_SUFFIXES.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


class NameIndex:
    """Master securities by normalised current and former name, with listing dates."""

    def __init__(self, master: pd.DataFrame):
        self.rows: list[tuple[str, dict, str, str]] = []  # (name key, row, valid from, valid to)
        for row in master.to_dict("records"):
            self.rows.append((norm_name(row["name"]), row, "", ""))
            for part in str(row.get("former_names") or "").split("|"):
                match = re.match(r"\s*(.*?)\s*\((\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})\)\s*$", part)
                if match:
                    self.rows.append((norm_name(match.group(1)), row, match.group(2), match.group(3)))
        self.rows = [r for r in self.rows if r[0]]
        self.by_id = {row["security_id"]: row for row in master.to_dict("records")}
        # successor -> [(predecessor, successor_date)] for master successor links under the same ticker: the
        # predecessor's vendor rows after that date are the successor's (1316631.A after 2013-06-07 is LBTYA plc)
        self.predecessors: dict[str, list[tuple[str, str]]] = {}
        for row in self.by_id.values():
            successor, day = str(row.get("successor_security_id") or ""), str(row.get("successor_date") or "")
            if successor and day and successor in self.by_id and row.get("first_ticker") \
                    and row["first_ticker"] == self.by_id[successor].get("first_ticker"):
                self.predecessors.setdefault(successor, []).append((row["security_id"], day))

    def name_of(self, sid: str, day: str = "") -> str:
        """The master's name of ``sid``: the former name it carried on ``day`` when there is one."""
        row = self.by_id.get(sid) or {}
        for part in str(row.get("former_names") or "").split("|"):
            match = re.match(r"\s*(.*?)\s*\((\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})\)\s*$", part)
            if day and match and match.group(2) <= day <= match.group(3):
                return match.group(1)
        return str(row.get("name") or "")

    def ticker_of(self, sid: str) -> str:
        return str((self.by_id.get(sid) or {}).get("first_ticker") or "")

    def find(self, phrase: str, day: str, hint: str = "", exclude_cik: str = "") -> tuple[str, str]:
        """(security_id, how) of the security named ``phrase`` and listed on ``day``; ('', reason) when none or several.
        The target's own CIK is never its acquirer (FLIR's CIK is now named 'Teledyne FLIR')."""
        key = norm_name(phrase)
        if len(key) < 2:
            return "", "no_name"
        def valid(entry):  # a former name counts only while the company carried it (plus a year)
            _, _, start, stop = entry
            return not start or (start <= day <= _shift(stop, 365))

        exact = [e[1] for e in self.rows if e[0] == key and valid(e)]
        prefix = [e[1] for e in self.rows if (e[0].startswith(key + " ") or key.startswith(e[0] + " ")) and valid(e)] \
            if not exact else []
        for found, how in ((exact, "name"), (prefix, "name_prefix")):
            listed = {r["security_id"]: r for r in found  # a CIK the master holds but never saw on Nasdaq has no dates
                      if r["first_listed"] and r["last_listed"] and r["cik"] != exclude_cik
                      and r["first_listed"] <= _shift(day, 31) and r["last_listed"] >= _shift(day, -31)}
            if not listed:
                continue
            ciks = {r["cik"] for r in listed.values()}
            if len(ciks) > 1:
                return "", f"{how}_ambiguous:" + " ".join(sorted(listed))
            if len(listed) > 1:
                cls = re.search(r"\b(?:Series|Class) ([A-C])\b", hint)
                pick = [s for s, r in listed.items() if cls and (r["share_class"] == cls.group(1) or s.endswith("." + cls.group(1)))]
                if len(pick) == 1:
                    return pick[0], how + "+class"
                common = [s for s, r in listed.items() if r["share_class"] in ("COMMON", "A")]
                return (common[0], how + "+first_class") if common else ("", f"{how}_classes:" + " ".join(sorted(listed)))
            return next(iter(listed)), how
        return "", "not_listed_on_nasdaq"


def load_existing(paths=EXISTING_FILES) -> pd.DataFrame:
    """The repo's sourced terminal rows (the files ``research_sue_lt_2020_2026.terminal_map`` merges)."""
    from src.io.terminal_returns import load_observed_terminal_returns

    frames = []
    for path in paths:
        if Path(path).exists():
            # the loader sorts its rows by (ticker, last_price_date) and keeps every column, the note too:
            # the note must come from the same row, never be laid over the sorted frame in file order
            frame = load_observed_terminal_returns(path)
            frame["note"] = frame["note"].fillna("").astype(str) if "note" in frame else ""
            frame["existing_file"] = str(path).replace(str(MAIN) + "/", "")
            frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["ticker", "last_price_date", "terminal_return", "consideration_per_share",
                                     "source_url", "verified_at", "note", "existing_file"])
    out = pd.concat(frames, ignore_index=True)
    # later files win, as in terminal_map (dict.update order)
    return out.drop_duplicates(["ticker", "last_price_date"], keep="last").reset_index(drop=True)


# Price levels in the existing files' notes ('1 BMY (56.41 close 2019-11-20)', '(76.24) ..., closes 2013-06-07'):
# a vendor close is never copied into a committed column, the cash and share terms are.
PRICE_LEVEL = re.compile(r"\s*\([^()]*\d+\.\d+[^()]*\)|,\s*(?:first\s+)?closes?\s+\d{4}-\d{2}-\d{2}")
# an existing row whose value is only the last close (no acquirer price there), not a terminal value
EXISTING_PLACEHOLDER = re.compile(r"history ends at the last close|no \w+ price available", re.I)


def strip_levels(note: str) -> str:
    return re.sub(r"\s{2,}", " ", PRICE_LEVEL.sub("", str(note or ""))).strip()


def _prior_value(prior: dict | None) -> float | None:
    value = (prior or {}).get("consideration_per_share")
    if value in (None, "") or pd.isna(value):
        return None
    return float(value)


def match_existing(existing: pd.DataFrame, scope: pd.DataFrame, intervals: pd.DataFrame) -> dict[str, dict]:
    """security_id -> the existing row whose ticker the security held up to its last price date."""
    ids = set(scope["security_id"])
    spans = intervals[intervals["security_id"].isin(ids)]
    out = {}
    for row in existing.to_dict("records"):
        day = row["last_price_date"].strftime("%Y-%m-%d")
        held = spans[spans["ticker"].eq(row["ticker"]) & (spans["start"] <= day) & (spans["end"] >= _shift(day, -45))]
        sids = sorted(set(held["security_id"]))
        if len(sids) == 1:
            out[sids[0]] = row
        elif len(sids) > 1:  # the interval that ends closest after the date
            held = held.assign(gap=(pd.to_datetime(held["end"]) - pd.Timestamp(day)).dt.days.abs()).sort_values("gap")
            out.setdefault(held["security_id"].iloc[0], row)
    return out


# ------------------------------------------------------------------ last prices

TIINGO_DATA = {"done", "done_review", "partial"}       # step 8 statuses whose rows count as vendor raw
TIINGO_FINAL_EMPTY = {"wrong_entity", "no_data", "no_data_in_window", "refused"}
YAHOO_ACCEPTED = {"ok", "partial", "review"}
VENDOR_ORDER = ("tiingo_step8", "tiingo", "wiki", "yahoo_step7", "yahoo")  # preference on the same session
if common.DATA_VERSION == "v2":
    # data version 2 (plan section 0, 2026-10-05): archived Yahoo rows rank after every v1 source; ``archive_otc`` are
    # the OTC tickers (T+Q, T+F) of the D5 names, read only after the Nasdaq ticker's last row
    VENDOR_ORDER = VENDOR_ORDER + ("archive", "archive_otc")
SNAPSHOT_END_SLACK_DAYS = 45  # a snapshot-dated end can come before the last trade by up to a capture gap
# A vendor filler row (plan rule R5): the close repeats the previous session's close on a volume below
# this share of the median volume of the last FILLER_LOOKBACK real sessions (ATVI 2023-10-13: 1 share
# against 7.3M). Such a row is not a session: not a last trade, not an acquirer close, not an OTC close.
FILLER_VOLUME_SHARE = 0.05
FILLER_LOOKBACK = 20
FILLER_MIN_HISTORY = 5
PRICE_BREAK = 0.5  # after a snapshot-dated end, a raw close this far from the previous one is a new market (OTC)
_XNAS: dict[str, pd.DatetimeIndex] = {}


def xnas_sessions() -> pd.DatetimeIndex:
    """The XNAS sessions 2010-2026 (exchange_calendars, computed locally)."""
    if "sessions" not in _XNAS:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XNAS", start="2010-01-04", end="2026-12-31")
        _XNAS["sessions"] = pd.DatetimeIndex(calendar.sessions).tz_localize(None).normalize()
    return _XNAS["sessions"]


def sessions_after(day, until) -> int:
    """How many XNAS sessions lie in (day, until]: 1 when ``until`` is the next session."""
    sessions = xnas_sessions()
    return int(sessions.searchsorted(pd.Timestamp(until), side="right") - sessions.searchsorted(pd.Timestamp(day), side="right"))


def previous_session(day) -> str:
    """The last XNAS session strictly before ``day``."""
    sessions = xnas_sessions()
    return sessions[sessions.searchsorted(pd.Timestamp(day), side="left") - 1].strftime("%Y-%m-%d")


def filler_flags(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    """True for each row (dates in order) that repeats the previous close on no volume or on a volume
    below FILLER_VOLUME_SHARE of the median of the last FILLER_LOOKBACK real sessions."""
    flags = np.zeros(len(close), dtype=bool)
    recent: list[float] = []
    for i in range(len(close)):
        v = 0.0 if not np.isfinite(volume[i]) else float(volume[i])
        repeat = i > 0 and abs(close[i] - close[i - 1]) <= 1e-6 * max(1.0, abs(close[i - 1]))
        if repeat and (v <= 0 or (len(recent) >= FILLER_MIN_HISTORY
                                  and v < FILLER_VOLUME_SHARE * float(np.median(recent[-FILLER_LOOKBACK:])))):
            flags[i] = True
        elif v > 0:
            recent.append(v)
    return flags


def archive_rows(sid: str, frames: list[pd.DataFrame]) -> list[pd.DataFrame]:
    """Version 2: the archived Yahoo rows of ``sid`` (reversal_data_v2_archive.py ``series/archive``): the Nasdaq
    ticker's rows as ``archive``, and the OTC tickers' rows (``otc``) as ``archive_otc`` only after the last row any
    source has under the Nasdaq ticker (an OTC close follows the last Nasdaq trade; never the other way round)."""
    path = common.V2_FILL / "series" / "archive" / f"{sid}.csv.gz"
    a = pd.read_csv(path) if path.exists() else pd.DataFrame()
    y = common.V2_FILL / "series" / "yahoo_otc" / f"{sid}.csv.gz"  # live Yahoo charts of the OTC tickers (T+Q, T+F)
    if y.exists():
        yo = pd.read_csv(y)
        if len(yo):
            yo = yo.assign(capture="yahoo_live", otc=True)[["date", "capture", "otc", "close_raw", "volume_raw"]]
            a = pd.concat([a, yo], ignore_index=True) if len(a) else yo
    if a.empty:
        return []
    a["date"] = pd.to_datetime(a["date"])
    a = a.sort_values(["date", "capture"], ascending=[True, False]).drop_duplicates("date", keep="first")
    otc = a["otc"].astype(str).str.lower().eq("true")
    nas = a[~otc]
    out = []
    if len(nas):
        out.append(pd.DataFrame({"date": nas["date"], "close": nas["close_raw"], "volume": nas["volume_raw"],
                                 "src": "archive"}))
    last = max([pd.to_datetime(f["date"]).max() for f in frames + out if len(f)], default=None)
    o = a[otc]
    if len(o) and last is not None:
        o = o[o["date"] > last]
        if len(o):
            out.append(pd.DataFrame({"date": o["date"], "close": o["close_raw"], "volume": o["volume_raw"],
                                     "src": "archive_otc"}))
    return out


class PriceBook:
    """Daily raw closes per security from every source this step may use.

    ``rows(sid)`` gives date, close, volume, src for one security: the pre-filter's rows
    (WIKI, the old Tiingo and Yahoo caches, the stored files, one source per day), the step-7
    Yahoo series (accepted verdicts) and the step-8 Tiingo files (statuses with data).
    """

    def __init__(self, ids: set[str] | None = None, daily: pd.DataFrame | None = None, tiingo_status: pd.DataFrame | None = None,
                 yahoo_report: pd.DataFrame | None = None, yahoo_dir: Path = YAHOO_DIR):
        if daily is None:
            daily = pd.read_pickle(DAILY) if DAILY.exists() else pd.DataFrame(
                columns=["security_id", "date", "close", "volume", "src"])
        daily = daily[["security_id", "date", "close", "volume", "src"]]
        if ids is not None:
            daily = daily[daily["security_id"].isin(ids)]
        daily = daily.sort_values(["security_id", "date"], kind="stable").reset_index(drop=True)
        keys, starts = np.unique(daily["security_id"].to_numpy(dtype=str), return_index=True)
        ends = list(starts[1:]) + [len(daily)]
        self._daily = daily
        self._spans = {k: (int(a), int(b)) for k, a, b in zip(keys, starts, ends)}
        if tiingo_status is None:
            tiingo_status = read_csv_text(TIINGO_STATUS) if TIINGO_STATUS.exists() else pd.DataFrame(
                columns=["security_id", "ticker_for_source", "status", "prices_path"])
        self.tiingo_status = tiingo_status
        if yahoo_report is None:
            yahoo_report = read_csv_text(YAHOO_REPORT) if YAHOO_REPORT.exists() else pd.DataFrame(
                columns=["security_id", "verdict"])
        self.yahoo_ok = set(yahoo_report.loc[yahoo_report["verdict"].isin(YAHOO_ACCEPTED), "security_id"])
        self.yahoo_dir = yahoo_dir
        self._cache: dict[str, pd.DataFrame] = {}
        self._sessions: dict[str, pd.DataFrame] = {}

    def tiingo_rows(self, sid: str) -> list[dict]:
        return self.tiingo_status[self.tiingo_status["security_id"].eq(sid)].to_dict("records")

    def rows(self, sid: str) -> pd.DataFrame:
        if sid in self._cache:
            return self._cache[sid]
        frames = []
        if sid in self._spans:
            a, b = self._spans[sid]
            frames.append(self._daily.iloc[a:b].drop(columns="security_id"))
        if sid in self.yahoo_ok and (self.yahoo_dir / f"{sid}.csv.gz").exists():
            data = pd.read_csv(self.yahoo_dir / f"{sid}.csv.gz", usecols=["date", "close_raw", "volume_raw"])
            frames.append(pd.DataFrame({"date": pd.to_datetime(data["date"]), "close": data["close_raw"],
                                        "volume": data["volume_raw"], "src": "yahoo_step7"}))
        for row in self.tiingo_rows(sid):
            path = Path(row.get("prices_path") or "")
            if row.get("status") in TIINGO_DATA and path.exists():
                data = pd.read_csv(path, usecols=["date", "close", "volume"])
                frames.append(pd.DataFrame({"date": pd.to_datetime(data["date"].str[:10]), "close": data["close"],
                                            "volume": data["volume"], "src": "tiingo_step8"}))
        if common.DATA_VERSION == "v2":
            frames += archive_rows(sid, frames)
        if frames:
            out = pd.concat(frames, ignore_index=True)
            out["date"] = pd.to_datetime(out["date"]).dt.normalize()
            out = out[(out["close"] > 0)].sort_values("date").reset_index(drop=True)
        else:
            out = pd.DataFrame(columns=["date", "close", "volume", "src"])
        self._cache[sid] = out
        return out

    def sessions(self, sid: str) -> pd.DataFrame:
        """One row per date of ``sid``: the preferred source's close, the largest volume any source
        reports, and ``filler`` (a vendor filler row, see filler_flags)."""
        if sid in self._sessions:
            return self._sessions[sid]
        rows = self.rows(sid)
        if rows.empty:
            out = pd.DataFrame({"date": pd.Series(dtype="datetime64[ns]"), "close": pd.Series(dtype=float),
                                "volume": pd.Series(dtype=float), "filler": pd.Series(dtype=bool)})
        else:
            order = {s: i for i, s in enumerate(VENDOR_ORDER)}
            ranked = rows.assign(_order=rows["src"].map(order).fillna(len(order)),
                                 volume=pd.to_numeric(rows["volume"], errors="coerce").fillna(0.0))
            ranked = ranked.sort_values(["date", "_order"], kind="stable")
            out = ranked.groupby("date", sort=True).agg(close=("close", "first"), volume=("volume", "max")).reset_index()
            out["filler"] = filler_flags(out["close"].to_numpy(dtype=float), out["volume"].to_numpy(dtype=float))
        self._sessions[sid] = out
        return out

    def filler_dates(self, sid: str) -> set:
        sessions = self.sessions(sid)
        return set(sessions.loc[sessions["filler"], "date"])

    def close_on(self, sid: str, day: pd.Timestamp, after: bool = False) -> dict | None:
        """The vendor raw close of ``sid`` on ``day`` (or on the first session after it when ``after``);
        a vendor filler row is not a session."""
        rows = self.rows(sid)
        vendor = rows[rows["src"].isin(VENDOR_ORDER) & (rows["volume"].fillna(0) > 0)
                      & ~rows["date"].isin(self.filler_dates(sid))]
        if after:
            later = vendor[vendor["date"] > day]
            if later.empty:
                return None
            day = later["date"].min()
        same = vendor[vendor["date"].eq(day)]
        if same.empty:
            return None
        same = same.assign(order=same["src"].map({s: i for i, s in enumerate(VENDOR_ORDER)})).sort_values("order")
        best = same.iloc[0]
        spread = float(same["close"].max() / same["close"].min() - 1) if len(same) > 1 else 0.0
        return {"date": day, "close": float(best["close"]), "src": best["src"], "n_sources": int(len(same)),
                "max_source_diff": spread}

    def before(self, sid: str, day: str) -> "PriceBook":
        """This book with ``sid`` cut to its rows before ``day``: the old shares before a relist junction. The new
        shares' rows under the same security_id belong to the later listing, never to the old shares' last trade
        or OTC close (WW's Tiingo file runs on from the old shares into the new). Other securities are unchanged."""
        import copy

        view = copy.copy(self)
        view._cache, view._sessions = dict(self._cache), dict(self._sessions)
        rows = self.rows(sid)
        view._cache[sid] = rows[rows["date"] < pd.Timestamp(day)].reset_index(drop=True)
        view._sessions.pop(sid, None)
        return view


WIKI_END = "2018-03-27"
# The acquirer's close (or the next leg's) belongs to the XNAS session after the target's last trade.
# A first close up to ACQUIRER_HOLD_SESSIONS sessions later still gives a value, held for review
# (needs_review, the figure local only); a later one is not used.
ACQUIRER_QUOTE_SESSIONS = 1
ACQUIRER_HOLD_SESSIONS = 5
# A value this far from the last close is held for review (needs_review) unless REVIEWED approves it; a bankruptcy
# or removal (an OTC close, a sourced cancellation) is exempt: a large drop is its expected outcome, not a misread term.
GUARD_RETURN = 0.05
ANCHOR_TOLERANCE_DAYS = 7  # the last trade of a merged company lies within a week of its Form 25 filing
TRANSFER_START_DAYS = 30  # a stated first day on the new exchange lies this close to the transfer Form 25
HALT_WINDOW_DAYS = 30  # a halt the closing 8-K states lies this close before the end


def price_window(row, ev: dict | None = None) -> tuple[str, str]:
    """(limit, anchor): the last day a Nasdaq trade can fall on, and the day the last trade should
    be near ('' when the end gives none). A merger's shares stop trading by the Form 25 filing;
    an issuer's own Form 25 (a voluntary delisting) takes effect 10 days later; a snapshot end is
    only known to a capture gap."""
    basis = row["f25_delisting_basis"]
    if row["end_source"] == "form25":
        filed = row["f25_filing_date"] or row["end_date"]
        if basis == "issuer_withdrawal":
            return row["end_date"], ""
        if basis == "exchange_removal":
            return filed, ""
        return filed, filed
    if row["end_source"] == "transfer":
        return row["end_date"], ""
    closing = [d for d in str((ev or {}).get("closing_dates") or "").split() if d]
    if closing:  # a reorganisation or rename: the (last) closing 8-K dates it; later rows are the successor's
        return max(closing), ""
    return _shift(row["end_date"], SNAPSHOT_END_SLACK_DAYS), ""


def transfer_window(row, ev: dict | None = None) -> tuple[str, str, str]:
    """(limit, new_exchange_start, basis) for a listing that moved: the last Nasdaq session is the
    session before the first day on the new exchange that the Item 3.01 8-K or its press release
    states (the first stated day after the transfer Form 25 filing, else the latest one before it),
    else the transfer Form 25 filing date. Not the Form 25 effective date: trading has moved by then
    (ORCL: Form 25 filed 2013-07-12, NYSE from 2013-07-15, effective 2013-07-22)."""
    filed = row.get("transfer_filing_date") or row.get("f25_filing_date") or ""
    reference = filed or row["end_date"]
    stated = [d for d in str((ev or {}).get("transfer_start") or "").split()
              if _shift(reference, -TRANSFER_START_DAYS) <= d <= _shift(reference, TRANSFER_START_DAYS)]
    later = [d for d in stated if d > reference]
    start = min(later) if later else (max(stated) if stated else "")
    if start:
        return previous_session(start), start, "session_before_stated_new_exchange_start"
    if filed:
        return filed, "", "transfer_form25_filing"
    return row["end_date"], "", "end_date"


def snapshot_cut(book: "PriceBook", sid: str, end_date: str, limit: str) -> tuple[str, str]:
    """(limit, reason) for a snapshot-dated end: the window stops before the first run of two or more
    vendor filler rows from SNAPSHOT_END_SLACK_DAYS before the end, or before a raw close more than
    PRICE_BREAK from the previous one after the end (an OTC market), whichever comes first
    (SBNY: Nasdaq trade to 2023-03-10, fillers at 70.00 to 2023-03-27, OTC 0.13 on 2023-03-28)."""
    sessions = book.sessions(sid)
    if sessions.empty:
        return limit, ""
    lo, hi, end = pd.Timestamp(_shift(end_date, -SNAPSHOT_END_SLACK_DAYS)), pd.Timestamp(limit), pd.Timestamp(end_date)
    dates, closes, filler = sessions["date"].to_numpy(), sessions["close"].to_numpy(dtype=float), sessions["filler"].to_numpy()
    for i in range(1, len(sessions)):
        day = pd.Timestamp(dates[i])
        if day <= lo:
            continue
        if day > hi:
            break
        before = pd.Timestamp(dates[i - 1]).strftime("%Y-%m-%d")
        if filler[i] and not filler[i - 1] and i + 1 < len(sessions) and filler[i + 1]:
            return before, "snapshot_end_cut_before_filler_run"
        if day > end and closes[i - 1] > 0 and abs(closes[i] / closes[i - 1] - 1.0) > PRICE_BREAK:
            return before, "snapshot_end_cut_before_price_break"
    return limit, ""


# Acquirers outside the Nasdaq master whose close the stock part needs: one Yahoo chart each
# (``--yahoo-acquirers``, at most YAHOO_MAX requests in all; CACHE/terminal/yahoo_raw). Target
# security_id -> Yahoo symbol of the acquirer as it trades today (BB&T is Truist, TFC).
ACQUIRER_SYMBOLS = {
    "1465112": "T", "1015780": "MS", "889936": "CM", "1378946": "MTB", "921847": "MTB", "77877": "CVX",
    "1620280": "UNIT", "1038205": "OZK", "817473": "ARCC", "354908": "TDY", "1142596": "GMED", "1613859": "ICLR",
    "1644406": "SJM", "1051741": "KEY", "1102112": "BANC", "1499875": "VTR", "1411574": "UNH", "1594012": "CFG",
    "933141": "FHN", "700863": "TFC", "700733": "TFC", "1104188": "SOHU",
    # 21CF: not fetched, the cap below is used up (20 charts cached); the rows wait for a DIS close
    "1308161.A": "DIS", "1308161.B": "DIS",
}
YAHOO_MAX = 20
YAHOO_RAW = TERMINAL_CACHE / "yahoo_raw"


def fetch_acquirer_charts(symbols: list[str] | None = None) -> dict:
    """One v8 chart per acquirer symbol not yet cached (reversal_data_yahoo's fetcher and pacing)."""
    from scripts import reversal_data_yahoo as yahoo

    wanted = sorted(set(symbols or ACQUIRER_SYMBOLS.values()))
    cached = [s for s in wanted if yahoo.cached_raw(s, YAHOO_RAW)[1]]
    room = YAHOO_MAX - len(cached)
    todo = [s for s in wanted if s not in cached][:max(0, room)]
    YAHOO_RAW.mkdir(parents=True, exist_ok=True)
    log(f"acquirer charts: {len(wanted)} symbols, {len(cached)} cached, asking {len(todo)} (cap {YAHOO_MAX})")
    return yahoo.fetch_symbols(todo, period1=yahoo.PERIOD1, raw_dir=YAHOO_RAW) if todo else {}


def acquirer_chart(symbol: str) -> pd.DataFrame:
    """date, close (split-restored raw close), volume from the cached chart of ``symbol`` ('' when none)."""
    from scripts import reversal_data_yahoo as yahoo

    path = yahoo.best_raw(symbol, YAHOO_RAW) if YAHOO_RAW.exists() else None
    if path is None:
        return pd.DataFrame(columns=["date", "close", "volume", "src"])
    _, daily, _ = yahoo.parse_chart(yahoo.read_raw(path))
    return pd.DataFrame({"date": pd.to_datetime(daily["date"]).dt.tz_localize(None).dt.normalize()
                         if getattr(pd.to_datetime(daily["date"]).dt, "tz", None) else pd.to_datetime(daily["date"]),
                         "close": daily["close_raw"], "volume": daily["volume_raw"], "src": "yahoo_acquirer"})


def chart_close_after(chart: pd.DataFrame, day: pd.Timestamp) -> dict | None:
    later = chart[(chart["date"] > day) & (chart["volume"].fillna(0) > 0)]
    if later.empty:
        return None
    first = later.sort_values("date").iloc[0]
    return {"date": first["date"], "close": float(first["close"]), "src": "yahoo_acquirer"}


def last_trade(book: PriceBook, sid: str, limit: str, anchor: str = "") -> dict:
    """The last session with volume > 0 on or before ``limit`` that is not a vendor filler row (a
    repeated close on a sliver of the usual volume, plan rule R5), and the vendor close on it.

    status: ``ok`` (a vendor close on that session, and the session within a week of ``anchor``),
    ``vendor_short`` (no vendor close there: the stored file runs later, or every series stops
    well before the anchor, e.g. at the WIKI end) or ``no_rows``. ``filler_dropped`` counts the
    filler sessions after the last trade and on or before ``limit``.
    """
    rows = book.rows(sid)
    rows = rows[rows["src"] != "archive_otc"]  # v2: an OTC ticker's close is never the last Nasdaq trade
    fillers = book.filler_dates(sid)
    inside = rows[(rows["date"] <= pd.Timestamp(limit)) & (rows["volume"].fillna(0) > 0)]
    traded = inside[~inside["date"].isin(fillers)]
    if traded.empty:
        return {"status": "no_rows", "last_date": "", "vendor_last_date": "", "stored_last_date": "", "filler_dropped": 0}
    vendor = traded[traded["src"].isin(VENDOR_ORDER)]
    stored = traded[~traded["src"].isin(VENDOR_ORDER)]
    last = traded["date"].max()
    out = {"last_date": last.strftime("%Y-%m-%d"),
           "vendor_last_date": vendor["date"].max().strftime("%Y-%m-%d") if len(vendor) else "",
           "stored_last_date": stored["date"].max().strftime("%Y-%m-%d") if len(stored) else "",
           "filler_dropped": int(inside.loc[inside["date"] > last, "date"].nunique())}
    close = book.close_on(sid, last)
    short = close is None or (anchor and last < pd.Timestamp(anchor) - pd.Timedelta(days=ANCHOR_TOLERANCE_DAYS))
    if not anchor and close is not None and close["src"] == "wiki" and out["last_date"] == WIKI_END \
            and limit > _shift(WIKI_END, 14):
        short = True  # the WIKI table ends here, the listing did not
    if short:
        return {**out, "status": "vendor_short"}
    return {**out, "status": "ok", "close": close["close"], "src": close["src"], "n_sources": close["n_sources"],
            "max_source_diff": close["max_source_diff"]}


def acquirer_close(book: PriceBook, names: "NameIndex", acquirer: str, day: pd.Timestamp, target: str = "") -> dict | None:
    """The acquirer's first vendor close after ``day``: from its own series, or from the series of a master
    predecessor under the same ticker, after that predecessor's successor date (those rows are the acquirer's:
    VMED's Liberty Global plc legs close on 2013-06-10 in 1316631.A/C, the plc's own series starts 2013-06-24).
    The target itself is never such a predecessor (its own next close counts only for one-for-one terms)."""
    best = book.close_on(acquirer, day, after=True)
    for predecessor, start in names.predecessors.get(acquirer, []):
        if predecessor == target:
            continue
        quote = book.close_on(predecessor, max(day, pd.Timestamp(start)), after=True)
        if quote is not None and (best is None or quote["date"] < best["date"]):
            best = {**quote, "via": predecessor}
    return best


def otc_close(book: PriceBook, sid: str, after: str, within_days: int = 30) -> dict | None:
    """The first vendor close after ``after`` (an OTC continuation of a removed listing); filler rows
    that repeat the last Nasdaq close are not OTC trades."""
    rows = book.rows(sid)
    later = rows[rows["src"].isin(VENDOR_ORDER) & (rows["date"] > pd.Timestamp(after))
                 & (rows["date"] <= pd.Timestamp(after) + pd.Timedelta(days=within_days)) & (rows["volume"].fillna(0) > 0)
                 & ~rows["date"].isin(book.filler_dates(sid))]
    if later.empty:
        return None
    first = later.sort_values("date").iloc[0]
    return {"date": first["date"].strftime("%Y-%m-%d"), "close": float(first["close"]), "src": first["src"]}


def slack_overrun(end_source: str, kind: str, limit_basis: str, limit: str, trade: dict) -> str:
    """Why a snapshot-dated stock or mixed row's last trade cannot be trusted ('' when it can): it sits at the
    snapshot-slack limit (the end plus SNAPSHOT_END_SLACK_DAYS, within one XNAS session of it) while the stored
    series ends earlier, so the vendor rows up to the limit may be a successor's under the same ticker (a rename
    with no master successor link: QRTEA's rows after 2025-02-21 are QVC Group's QVCGA)."""
    stored, last = str(trade.get("stored_last_date") or ""), str(trade.get("last_date") or "")
    if end_source != "snapshots" or kind not in ("stock_merger", "mixed") or limit_basis != "snapshots_end" \
            or trade.get("status") != "ok" or not stored or not last or stored >= last:
        return ""
    if sessions_after(last, limit) > 1:
        return ""
    return (f"the last vendor trade ({last}) sits at the snapshot-slack limit ({limit}) while the stored series ends on "
            f"{stored}: the later vendor rows may be a successor's under the same ticker")


def price_pending(sid: str, candidates: pd.DataFrame, book: PriceBook) -> str:
    """Why a vendor price may still arrive for ``sid`` ('' when none is coming)."""
    planned = candidates[candidates["security_id"].eq(sid)]
    tiingo = {r.get("status") for r in book.tiingo_rows(sid)}
    for row in planned.itertuples(index=False):
        if row.planned_source == "tiingo":
            if tiingo & TIINGO_DATA:
                continue
            if tiingo & TIINGO_FINAL_EMPTY:
                continue
            if row.status == "conditional_tier_c":
                return "tiingo month 2 (conditional tier C)"
            if row.status == "pending_month2" or tiingo & {"pending_month2"}:
                return "tiingo month 2"
            if row.status == "deferred_quota" or tiingo & {"deferred_quota", "error"}:
                return "tiingo deferred to month 2"
            return "tiingo run in progress (file not yet arrived)"
    return ""


# ------------------------------------------------------------------ classification and values

OUTPUT_COLUMNS = [
    # the existing terminal_returns.csv columns
    "ticker", "last_price_date", "terminal_return", "consideration_per_share", "source_url", "verified_at",
    # plan section 1.1 additions
    "security_id", "delist_date", "terminal_type", "consideration_cash", "consideration_shares", "acquirer_security_id",
    # this step's facts
    "status", "status_note", "event_subtype", "destination_exchange", "destination_start_date", "acquirer_name",
    "acquirer_match",
    "cvr", "election", "consideration_value_basis", "price_source", "price_source_url", "acquirer_price_date",
    "end_date", "end_source", "form25_accession", "form25_basis", "best_rank", "in_candidates", "cik", "name",
    "terms_stage", "existing_file", "existing_terminal_return", "existing_consideration_per_share",
    "existing_source_url", "existing_return_diff",
    # a special dividend paid to holders at the closing, part of the value (an SEC fact; REVIEWED)
    "special_dividend_cash", "special_dividend_record_date",
    # the first traded session of the new shares when this row is the old shares' at a relist junction
    "relist_junction",
    # a 1:1 reorganisation that continues the security (reconcile SUCCESSOR_LINKS): the successor; no terminal return
    "continued_as",
    # the old shares at a relist junction of a security whose row values a later end (REVIEWED_OLD_SHARES: Frontier)
    "relist_old_shares_last_price_date", "relist_old_shares_terminal_return", "relist_old_shares_status",
    "relist_old_shares_source_url", "relist_old_shares_note",
]
TYPES = ("cash_merger", "stock_merger", "mixed", "liquidation", "exchange_move", "bankruptcy_otc", "unknown")
STATUSES = ("computed", "pending_price", "no_vendor_price", "needs_acquirer_price", "needs_review", "no_terminal_return",
            "awaiting_d5", "unknown")
PRICE_SOURCE_URLS = {
    "tiingo_step8": "https://api.tiingo.com/tiingo/daily/{ticker}/prices (raw close; CACHE/tiingo/prices)",
    "tiingo": "https://api.tiingo.com/tiingo/daily/{ticker}/prices (raw close; research_cache Tiingo JSON)",
    "wiki": "https://data.nasdaq.com/api/v3/datatables/WIKI/PRICES (raw close; CACHE/wiki/by_ticker)",
    "yahoo_step7": "https://query1.finance.yahoo.com/v8/finance/chart/{ticker} (split-restored raw close; CACHE/yahoo)",
    "yahoo": "https://query1.finance.yahoo.com/v8/finance/chart/{ticker} (split-restored raw close; holdout yahoo_nominal)",
    # version 2 (2026-10-05): archived Yahoo captures and the OTC tickers' captures / live charts
    "archive": "https://web.archive.org/ (archived Yahoo table.csv / history page of {ticker}; raw close restored; "
               "research_cache/reversal_2012_2026_v2_fill/series/archive)",
    "archive_otc": "https://web.archive.org/ or https://query1.finance.yahoo.com/v8/finance/chart/ (OTC ticker of "
                   "{ticker}: T+Q / T+F; research_cache/reversal_2012_2026_v2_fill/series)",
}
NON_NASDAQ = {"NYSE", "NYSE American", "NYSE Arca", "Cboe BZX", "CBOE"}

# Reviewed by hand from the SEC documents (the closing 8-K or its EX-99.1 unless ``url`` names
# another); each entry overrides the automatic reading. Keys: type, sub, cash, shares, acq
# (acquirer security_id), acq_name, stock_value, rule ('election': cash or stock alternatives,
# valued at the stock alternative when the acquirer is priced, else the cash one), dest, url,
# value (a reviewed total per share), limit (the last trading day), hold (a reason to keep a computed
# value for review), special_dividend (cash per share paid to holders at the closing, added to the value;
# special_dividend_record its record date) or special_dividend_before_last_trade (it went ex before the
# last trade and belongs to the series), rule 'plan_new_shares' (old shares at a relist junction with no OTC close:
# ``shares`` new shares per old share at the new shares' first vendor close), approved (what the reviewer checked: lifts the guard, only with the
# entry's own url, never for an existing-file value), checked (what a builder checked, for the owner's review: it
# lifts nothing), hold_last_session (the hold is about the last session itself: last_price_date stays blank), note.
_SEC = "https://www.sec.gov/Archives/edgar/data/"
REVIEWED: dict[str, dict] = {
    # ---- reorganisations, reclassifications and renames into another security (1 share)
    "885721": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1532063", "limit": "2012-03-30",
               "url": _SEC + "885721/000119312512144955/d328743d8k.htm",
               "note": "Medco merger: the Company's stockholders received Express Scripts Holding shares one-for-one "
                       "(the $28.80 + 0.81 terms in the 8-K are Medco's); the 8-K accepted at 08:07 on 2012-04-02 says "
                       "trading in the Company's stock 'has been halted' and the Parent's 'will trade' as ESRX, so the last "
                       "session is 2012-03-30"},
    "1058057": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1835632", "limit": "2021-04-20",
                "url": _SEC + "1058057/000119312521122807/d156000d8k.htm",
                "note": "redomicile into Marvell Technology, Inc. one-for-one at the Inphi closing (the $66 + 2.323 terms are "
                        "Inphi's); the Bermuda Merger took effect at 4:01 p.m. ET on 2021-04-20"},
    "1261694": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1690666",
                "note": "each Tessera share became one Tessera Holding (later Xperi) share at the DTS acquisition "
                        "(the $42.50 cash is DTS's)"},
    "353569": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1906324", "limit": "2022-05-26",
               "url": _SEC + "1906324/000119312522161806/d323352d8k12b.htm",
               "note": "each Quidel share became one QuidelOrtho share (the cash and 0.1055 terms are Ortho's); QuidelOrtho's "
                       "8-K12B: Nasdaq suspended the Quidel shares prior to the market opening on 2022-05-27 and QuidelOrtho "
                       "shares commenced trading under QDEL that day, so the last Quidel session is 2022-05-26"},
    "864683": {"type": "stock_merger", "sub": "merger", "shares": 1.0, "acq": "1639691",
               "note": "each Cyberonics share became one LivaNova ordinary share (the 0.0472 ratio is Sorin's)"},
    "1316631.B": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1570585.T-LBTYB",
                  "limit": "2013-06-07",
                  "note": "Virgin Media closing (2013-06-07): each Liberty Global Series B share became one Liberty Global plc "
                          "Class B share; the snapshots stop listing LBTYB in 2012, the closing 8-K dates the end"},
    "1166691.T-CMCSK": {"type": "stock_merger", "sub": "reclassification", "shares": 1.0, "acq": "1166691.T-CMCSA",
                        "note": "each Class A Special share reclassified into one Class A share (2015-12-10; EX-99.1)"},
    "1437107.B": {"type": "stock_merger", "sub": "reclassification", "shares": 1.0, "acq": "1437107.A",
                  "note": "Discovery Series B reclassified into one WBD Series A share at the WarnerMedia closing (2022-04-08)"},
    "1437107.C": {"type": "stock_merger", "sub": "reclassification", "shares": 1.0, "acq": "1437107.A",
                  "note": "Discovery Series C reclassified into one WBD Series A share at the WarnerMedia closing (2022-04-08)"},
    "1411488.B": {"type": "stock_merger", "sub": "reclassification", "shares": 1.0, "acq": "1411488.A",
                  "note": "Class B reclassified as Class A one-for-one (2015-05-21; EX-99.1)"},
    "1734342.B": {"type": "stock_merger", "sub": "reclassification", "shares": 1.0, "acq": "1734342.A",
                  "note": "Class B converted into Class A one-for-one in the 2021-11-18 merger; last Class B trade 2021-11-17"},
    "1560385.T-LMCA": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1560385.T-FWONA",
                       "limit": "2017-01-24", "url": _SEC + "1560385/000110465917003788/a17-3007_18a12ba.htm",
                       "note": "Liberty Media Group Series A tracking stock renamed Formula One Group (LMCA -> FWONA) after "
                               "the F1 closing (EX-99.1 of 2017-01-23: the symbols change 'later this week'); the 8-A12B/A "
                               "filed after the close on 2017-01-24 expects the change 'shortly following the filing' and "
                               "WIKI's last LMCA row is 2017-01-24; the April 2016 recapitalisation is a distribution "
                               "inside the series"},
    "1560385.T-LMCK": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1560385.T-FWONK",
                       "limit": "2017-01-24", "url": _SEC + "1560385/000110465917003788/a17-3007_18a12ba.htm",
                       "note": "Liberty Media Group Series C tracking stock renamed Formula One Group (LMCK -> FWONK) with "
                               "Series A (8-A12B/A filed after the close on 2017-01-24)"},
    "1355096.T-LINTA": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QVCA",
                        "limit": "2014-10-06", "url": _SEC + "1355096/000135509614000070/lint-20141006x8k.htm",
                        "note": "LINTA renamed QVC Group Series A (QVCA) at the open of 2014-10-07; the Liberty Ventures "
                                "share distribution of October 2014 is a distribution inside the series"},
    "1355096.T-LINTB": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QVCB",
                        "limit": "2014-10-06", "url": _SEC + "1355096/000135509614000070/lint-20141006x8k.htm",
                        "note": "LINTB renamed QVC Group Series B (QVCB) at the open of 2014-10-07"},
    "1355096.T-QVCA": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QRTEA",
                       "limit": "2018-03-09", "url": _SEC + "1355096/000110465918017857/a18-8242_1ex99d1.htm",
                       "note": "QVC Group Series A renamed Qurate Retail Series A (QRTEA) after the GCI Liberty split-off: "
                               "'Beginning on Monday, March 12, 2018' it trades as QRTEA (EX-99.1)"},
    "1355096.T-QVCB": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QRTEB",
                       "limit": "2018-03-09", "url": _SEC + "1355096/000110465918017857/a18-8242_1ex99d1.htm",
                       "note": "QVC Group Series B renamed Qurate Retail Series B (QRTEB) from 2018-03-12 (EX-99.1)"},
    "1355096.T-QRTEA": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QVCGA",
                        "limit": "2025-02-21", "url": _SEC + "1355096/000110465925016368/tm257272d1_8k.htm",
                        "note": "Qurate Retail renamed QVC Group; the 8-K: the Series A shares 'effective as of open of "
                                "trading on February 24, 2025, will trade on Nasdaq under the new ticker' QVCGA, so the "
                                "last QRTEA session is 2025-02-21 (the master has no successor link for the rename)"},
    "1355096.T-QRTEB": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QVCGB",
                        "limit": "2025-02-21", "url": _SEC + "1355096/000110465925016368/tm257272d1_8k.htm",
                        "note": "Qurate Retail renamed QVC Group; QRTEB trades as QVCGB on Nasdaq from the open of "
                                "2025-02-24 (8-K), so the last QRTEB session is 2025-02-21"},
    "1355096.T-LVNTA": {"type": "stock_merger", "sub": "split_off", "shares": 1.0, "acq": "",
                        "acq_name": "GCI Liberty, Inc. Class A (GLIBA, Nasdaq; not in the security master)",
                        "url": _SEC + "1355096/000110465918017857/a18-8242_18k.htm",
                        "note": "each LVNTA share redeemed for one GLIBA share in the GCI Liberty split-off, 2018-03-09"},
    "1355096.T-LVNTB": {"type": "stock_merger", "sub": "split_off", "shares": 1.0, "acq": "",
                        "acq_name": "GCI Liberty, Inc. Class B (GLIBB; not in the security master)",
                        "url": _SEC + "1355096/000110465918017857/a18-8242_18k.htm",
                        "note": "each LVNTB share redeemed for one GLIBB share in the GCI Liberty split-off, 2018-03-09"},
    "1100441": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1760173", "limit": "2019-03-08",
                "url": _SEC + "1100441/000119312519069904/d719647d8k.htm",
                "note": "each RTI Surgical share became one share of the new holding company (RTI Surgical Holdings, later "
                        "Surgalign) at the Paradigm closing, 2019-03-08; the old shares were suspended prior to the open "
                        "on 2019-03-11"},
    "1006269": {"type": "stock_merger", "sub": "merger", "shares": 1.0, "acq": "1845840",
                "note": "each Loral share became one Telesat Corporation share (or, by election, one Telesat Partnership unit)"},
    "1509470": {"type": "exchange_move", "sub": "rename_same_security", "dest": "Nasdaq",
                "note": "SuRo Capital renamed Neostellar Capital and still listed on Nasdaq (NSLR); the listing did not end, "
                        "the master stops at the ticker change"},
    "733269": {"type": "exchange_move", "sub": "rename_and_transfer", "dest": "NYSE", "limit": "2018-09-28",
               "start": "2018-10-01",
               "note": "Acxiom sold its marketing solutions unit, renamed itself LiveRamp Holdings and moved to the NYSE "
                       "(RAMP) on 2018-10-01, so the last Nasdaq session is 2018-09-28 (its transfer Form 25 was filed "
                       "on 2018-10-01, after the move); the shares were not exchanged"},
    "2007825": {"type": "exchange_move", "sub": "spac_merger_listing_moved", "dest": "NYSE",
                "note": "Churchill Capital Corp X combined with Infleqtion and the same issuer's shares moved to the NYSE (INFQ); "
                        "the $10 is the trust redemption price for redeeming holders"},
    "1068851": {"type": "exchange_move", "sub": "listing_transfer", "dest": "NYSE",
                "note": "transfer of the listing to the NYSE under the symbol PB, December 2011"},
    # ---- terms the automatic reading got wrong or could not read
    "1339947.B": {"type": "stock_merger", "sub": "merger", "shares": 0.59625, "acq": "813828.B",
                  "note": "each Viacom Class B share converted into 0.59625 ViacomCBS Class B share (EX-99.1)"},
    "1339947.A": {"type": "stock_merger", "sub": "merger", "shares": 0.59625, "acq": "813828.A",
                  "note": "each Viacom Class A share converted into 0.59625 ViacomCBS Class A share (EX-99.1)"},
    "813828.B": {"type": "stock_merger", "sub": "election", "shares": 1.0, "acq": "2041610",
                 "note": "each Class B share: one Paramount Skydance Class B share, or by election $15.00 cash (prorated); "
                         "valued at the stock"},
    "1308161.A": {"type": "stock_merger", "sub": "election", "shares": 0.4517, "acq": "",
                  "acq_name": "The Walt Disney Company (NYSE: DIS)", "url": _SEC + "1308161/000095015719000308/form8k.htm",
                  "approved": "closing 8-K: the cash election was oversubscribed; shares with a stock election or no valid "
                              "election (the shares that traded after the 2019-03-14 Election Deadline) were exchanged "
                              "for 0.4517 Disney share, so the last close is valued at 0.4517 x the next DIS close",
                  "note": "Disney merger, 12:02 a.m. on 2019-03-20: $51.572626 cash or 0.4517 Disney share by election, "
                          "prorated; cash-electing shares got $50.667340 + 0.007929 Disney share, non-electing ones 0.4517 "
                          "Disney share; the Fox Corporation shares (1 per 3) were distributed at 7:25 a.m. on 2019-03-19 "
                          "and the 21CF shares traded ex-distribution that day (TFCFA, new CUSIP): a distribution inside "
                          "the series"},
    "1308161.B": {"type": "stock_merger", "sub": "election", "shares": 0.4517, "acq": "",
                  "acq_name": "The Walt Disney Company (NYSE: DIS)", "url": _SEC + "1308161/000095015719000308/form8k.htm",
                  "approved": "closing 8-K: shares with a stock election or no valid election were exchanged for 0.4517 "
                              "Disney share (the cash election was oversubscribed)",
                  "note": "as 21CF Class A: non-electing shares received 0.4517 Disney share; the Fox Corporation Class B "
                          "distribution of 2019-03-19 belongs to the series (TFCF traded ex-distribution that day)"},
    "1054374": {"type": "mixed", "sub": "election", "cash": 51.4829, "shares": 0.0242, "acq": "1649338",
                "url": _SEC + "1441634/000119312516446897/d121614dex992.htm",
                "approved": "final election results (Avago closing 8-K EX-99.2, 2016-02-01): the cash election was "
                            "oversubscribed and each Cash Electing Share received about $51.4829 cash + 0.0242 Broadcom "
                            "Limited share; the 2016-01-26 425 (" + _SEC + "1054374/000119312516437104/d10800d425.htm, also "
                            + _SEC + "1054374/000119312516437142/d129865d425.htm): after the close of the 2016-01-25 "
                            "deadline only shares with no election may be traded, and they are deemed Cash Electing Shares",
                "note": "election: $54.50 cash, 0.4378 Broadcom Limited share or 0.4378 exchangeable unit per share, "
                        "prorated; the share that traded at the last close is a Cash Electing Share: $51.4829 cash + "
                        "0.0242 Broadcom Limited share (the documents are in CACHE/terminal/review_docs)"},
    "936402": {"type": "mixed", "cash": 90.99, "shares": 5.034, "acq": "", "acq_name": "Takeda ADS (NYSE: TAK)",
               "note": "per Shire ADS (3 shares): $90.99 cash + 5.034 Takeda ADS; the 8-K's $30.33 + 1.678 are per ordinary share"},
    "1351288": {"type": "mixed", "cash": 200.0, "shares": 0.12036, "acq": "1232524",
                "note": "per GW ADS (12 ordinary shares): 12 x ($16.66 2/3 cash + 0.010030 Jazz ordinary share)"},
    "1657312": {"type": "cash_merger", "cash": 107.0,
                "note": "per Verona ADS (8 ordinary shares): $107.00 cash; the 8-K's $13.375 is per ordinary share"},
    "1411574": {"type": "mixed", "cash": 11.40, "shares": 45.60 / 169.42, "acq": "", "acq_name": "UnitedHealth Group (NYSE: UNH)",
                "note": "$11.40 cash + the number of UnitedHealth shares equal to $45.60 / $169.42"},
    "1094739": {"type": "mixed", "sub": "election", "cash": 15.60, "shares": 0.2218, "acq": "820318",
                "note": "election among $26.00 cash, 0.5546 II-VI share or $15.60 + 0.2218 II-VI share (prorated); "
                        "valued at the mixed option"},
    "799088": {"type": "mixed", "sub": "election", "cash": 33.06, "shares": 1.0819, "acq": "", "rule": "cash_alternative",
               "acq_name": "AMC Entertainment (NYSE: AMC)",
               "note": "election: $33.06 cash or 1.0819 AMC share (prorated, mostly cash); the two were near equal at the "
                       "close, valued at the cash alternative"},
    "1491778": {"type": "stock_merger", "sub": "election", "shares": 1.0, "acq": "1705110", "limit": "2017-09-29",
                "note": "one ANGI Homeservices Class A share per share, or by election $8.50 cash (capped); valued at the stock; "
                        "the combination closed 2017-09-29 (closing 8-K), the last Angie's List session; the ANGI rows from "
                        "2017-10-02 are ANGI Homeservices under the same ticker"},
    "1470215": {"type": "stock_merger", "sub": "merger", "shares": 1.0, "acq": "1140536",
                "special_dividend_before_last_trade": True,
                "note": "2.6490 Willis shares per Towers Watson share followed by Willis's 2.6490-for-1 consolidation: one "
                        "Willis Towers Watson share; the pre-merger special dividend ($4.87 in the agreement 8-K) went ex "
                        "before the last trade (the vendor books it on 2015-12-30, when the raw close fell) and belongs to "
                        "the canonical series, not to the terminal value"},
    "859014": {"type": "cash_merger", "cash": 10.389188, "url": _SEC + "859014/000114036114045515/form8k.htm",
               "note": "net cash payment of $10.389188 per share (Thoma Bravo, 2014-12-15; the $10.43 agreed less the "
                       "Covisint spin-off tax); the 0.14025466 Covisint shares were a distribution in October 2014"},
    "817473": {"type": "mixed", "cash": 6.48 + 2.45 + 1.20, "shares": 0.483, "acq": "1287750",
               "note": "$6.48 cash from Ares Capital (incl. a $0.07 make-up dividend) + $2.45 from the sale of American "
                       "Capital Mortgage Management + $1.20 from Ares Capital Management, plus 0.483 ARCC share"},
    "891288": {"type": "mixed", "cash": 30.0, "shares": 0.897, "acq": "", "acq_name": "Mallinckrodt plc (NYSE: MNK)",
               "note": "$30.00 cash + 0.897 Mallinckrodt ordinary share"},
    "1110647": {"type": "mixed", "cash": 5.60, "shares": 0.0636, "acq": "1633978",
                "note": "$5.60 cash + 0.0636 Lumentum share"},
    "1644406": {"type": "mixed", "cash": 30.0, "shares": 0.03002, "acq": "", "acq_name": "J.M. Smucker (NYSE: SJM)",
                "note": "$30.00 cash + 0.03002 Smucker share"},
    "1182129": {"type": "mixed", "sub": "election", "cash": 16.625, "shares": 0.2440, "acq": "1593034",
                "note": "election among $16.625 cash + 0.2440 Endo share (standard), $33.25 cash or 0.4880 Endo share "
                        "(prorated); valued at the standard election"},
    "861361": {"type": "mixed", "cash": 34.10, "shares": 0.3101, "acq": "", "acq_name": "Rockwell Collins (NYSE: COL)",
               "note": "$34.10 cash + 0.3101 Rockwell Collins share"},
    "700733": {"type": "mixed", "sub": "election", "cash": 13.0, "shares": 0.3206, "acq": "", "rule": "election",
               "acq_name": "BB&T (NYSE: BBT)",
               "note": "election: $13.00 cash or 0.3206 BB&T share (prorated, 70% stock); stock is the main form"},
    "743316": {"type": "stock_merger", "shares": 0.63, "acq": "6281", "note": "0.6300 Analog Devices share per share"},
    "858339": {"type": "mixed", "sub": "election", "cash": 12.41, "shares": 0.3085, "acq": "1590895", "rule": "election",
               "note": "election: $12.41 cash or 0.3085 new Caesars (Eldorado) share (prorated)"},
    "1175609": {"type": "mixed", "cash": 40.0, "shares": 2.1757, "acq": "1058057",
                "note": "$40.00 cash + 2.1757 Marvell Technology Group common shares"},
    "1611983.A": {"type": "stock_merger", "shares": 0.236, "acq": "1091667",
                  "note": "each Liberty Broadband share converted into 0.236 Charter Class A share"},
    "1611983.C": {"type": "stock_merger", "shares": 0.236, "acq": "1091667",
                  "note": "each Liberty Broadband share converted into 0.236 Charter Class A share"},
    "1424454": {"type": "stock_merger", "sub": "merger", "shares": 1.0, "acq": "1675820",
                "note": "each Rovi share became one share of the new holding company TiVo Corporation"},
    "1088825": {"type": "mixed", "cash": 2.75, "shares": 0.3853, "acq": "1675820",
                "note": "$2.75 cash + 0.3853 TiVo Corporation share per TiVo Inc. share"},
    "354908": {"type": "mixed", "cash": 28.0, "shares": 0.0718, "acq": "", "acq_name": "Teledyne Technologies (NYSE: TDY)",
               "note": "$28.00 cash + 0.0718 Teledyne share"},
    "929940": {"type": "mixed", "cash": 87.69, "shares": 0.42, "acq": "1897982",
               "note": "$87.69 cash + 0.42 New AspenTech share (the Emerson transaction, 2022-05)"},
    "1516973": {"type": "mixed", "sub": "election", "cash": 9.82, "shares": 0.9519, "acq": "", "acq_name": "Annaly (NYSE: NLY)",
                "note": "election among $9.82 cash + 0.9519 Annaly share (mixed), $19.65 cash or 1.9037 Annaly shares "
                        "(prorated); valued at the mixed option"},
    "1499875": {"type": "mixed", "sub": "election", "cash": 11.33, "shares": 0.1688, "acq": "", "rule": "election",
                "acq_name": "Ventas (NYSE: VTR)",
                "note": "election: 0.1688 Ventas share or $11.33 cash (cash capped at 10% of shares); stock is the main form"},
    "831547": {"type": "stock_merger", "shares": 0.1783, "acq": "1808665",
               "note": "0.1783 Assertio share plus one CVR (valued at 0)"},
    "1594012": {"type": "mixed", "cash": 1.46, "shares": 0.297, "acq": "", "acq_name": "Citizens Financial Group (NYSE: CFG)",
                "note": "$1.46 cash + 0.297 Citizens Financial share"},
    "1560385.T-LSXMA": {"type": "stock_merger", "shares": 0.8375, "acq": "908937",
                        "note": "each Liberty SiriusXM share exchanged for 0.8375 New Sirius XM share (2024-09-09)"},
    "1560385.T-LSXMB": {"type": "stock_merger", "shares": 0.8375, "acq": "908937",
                        "note": "each Liberty SiriusXM share exchanged for 0.8375 New Sirius XM share (2024-09-09)"},
    "1560385.T-LSXMK": {"type": "stock_merger", "shares": 0.8375, "acq": "908937",
                        "note": "each Liberty SiriusXM share exchanged for 0.8375 New Sirius XM share (2024-09-09)"},
    "1434729": {"type": "stock_merger", "shares": 1.65, "acq": "1355096.T-QVCA",
                "note": "each HSN share converted into 1.65 QVC Group Series A (QVCA) shares"},
    "1602065": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "2074176",
                "acq_name": "New Viper Energy Class A (Nasdaq: VNOM, new holding company; master 2074176 'Viper Energy, "
                            "Inc.' from 2025-10-01)", "limit": "2025-08-18",
                "url": _SEC + "1602065/000119312525183040/d65540d8k.htm",
                "note": "Sitio combination: each Viper Class A share became one share of the new Viper holding company "
                        "at the Viper Pubco Merger Effective Time, 12:01 a.m. Eastern Time on 2025-08-19, and 'on August "
                        "19, 2025, New Viper Class A Common Stock began trading on Nasdaq' under VNOM (closing 8-K), so "
                        "the last session of the old VNOM is 2025-08-18"},
    "356213": {"type": "stock_merger", "shares": 0.85, "acq": "", "acq_name": "Gaming and Leisure Properties (Nasdaq: GLPI)",
               "extra": [(1.0, "1656239")],
               "note": "0.85 GLPI share per old Pinnacle share, plus one new Pinnacle Entertainment share (1656239) "
                       "distributed at the same closing"},
    "1270400": {"type": "mixed", "cash": 17.50, "shares": 0.2582, "acq": "1570585.T-LBTYA",
                "extra": [(0.1928, "1570585.T-LBTYK")],
                "note": "$17.50 cash + 0.2582 Liberty Global plc Class A + 0.1928 Class C share"},
    "1373707": {"type": "cash_merger", "cash": 2.00, "url": _SEC + "1373707/000119312520201936/d93682d8k.htm",
                "note": "La Jolla closing: $2.00 cash per share plus one CVR (valued at 0); the AcelRx agreement read "
                        "earlier was terminated"},
    "1599901": {"type": "cash_merger", "cash": 72.00, "url": _SEC + "1599901/000119312526079570/d90931d8k.htm",
                "hold": "Atrium Therapeutics (SpinCo, 2093101) shares were distributed on 2026-02-26, one per ten shares "
                        "(record date 2026-02-12), the day before the closing: check whether the last vendor close "
                        "carries the SpinCo entitlement; if it does, add 0.1 x the first Atrium close to the $72.00",
                "note": "$72.00 cash per share (Novartis, closing 8-K Item 2.01); the Atrium SpinCo distribution of "
                        "2026-02-26 (1 per 10, record 2026-02-12) preceded the merger"},
    "935494": {"type": "cash_merger", "cash": 60.00, "note": "$60.00 cash (Emerson); the 1-share line is Merger Sub's stock"},
    "1195933": {"type": "mixed", "sub": "election", "cash": 51.60, "shares": 1.2019, "acq": "", "acq_name": "Kemper (NYSE: KMPR)",
                "note": "election among $51.60 + 1.2019 Kemper share (mixed), $129.00 cash or 2.0031 Kemper shares "
                        "(prorated); valued at the mixed option"},
    "1675820": {"type": "stock_merger", "shares": 0.455, "acq": "1803696", "note": "0.455 Xperi Holding share per TiVo share"},
    "800458": {"type": "stock_merger", "shares": 1.15, "acq": "1158172", "note": "1.15 comScore share per Rentrak share"},
    "1537667": {"type": "stock_merger", "shares": 1.04, "acq": "1456772",
                "note": "merger into Government Properties Income Trust (renamed Office Properties Income Trust): 1.04 GOV "
                        "share per SIR share, 2018-12-31; the 0.502509 ILPT shares were a distribution before the merger"},
    "1501364": {"type": "stock_merger", "shares": 0.875, "acq": "1378946", "note": "0.875 People's United share per share"},
    "1511198": {"type": "stock_merger", "shares": 1.725, "acq": "1378946", "note": "1.725 People's United shares per share"},
    "1600125": {"type": "stock_merger", "shares": 0.275, "acq": "776901", "note": "0.275 Independent Bank Corp. share per share"},
    "1324410": {"type": "stock_merger", "shares": 0.45, "acq": "1564618", "note": "0.45 Independent Bank Group share per share"},
    "846901": {"type": "stock_merger", "shares": 0.8319, "acq": "",
               "acq_name": "Provident Financial Services (NYSE: PFS)", "note": "0.8319 Provident share per share"},
    "1176316": {"type": "stock_merger", "shares": 0.75, "acq": "883948", "note": "0.75 Union Bankshares share per share"},
    "1748907": {"type": "cash_merger", "cash": 16.00,
                "note": "per Orchard ADS (10 ordinary shares): $16.00 cash plus one CVR ($1.00, valued at 0)"},
    "1312928": {"type": "mixed", "sub": "election", "cash": 40.00, "shares": 0.05728, "acq": "1075531", "rule": "election",
                "note": "election: $40.00 cash or 0.05728 priceline.com share (prorated)"},
    "1617977": {"type": "cash_merger", "cash": 14.00, "note": "$14.00 cash (Yum! Brands); the 1-share line is the LLC unit exchange"},
    "1160958": {"type": "mixed", "cash": 66.00, "shares": 2.323, "acq": "1835632",
                "note": "$66.00 cash + 2.323 Marvell Technology, Inc. shares"},
    "1334814": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1617640.A", "limit": "2015-02-17",
                "url": _SEC + "1334814/000119312515050780/d874732d8k.htm",
                "note": "each Zillow Class A share became one Zillow Group Class A share (Trulia closing, 2015-02-17); the "
                        "8-K filed that day: 'After close of market today' trading in Zillow's Class A ceases"},
    "1365101": {"type": "mixed", "sub": "election", "cash": 14.00, "shares": 0.6549, "acq": "", "rule": "election",
                "acq_name": "Cott Corporation (NYSE: COT)", "note": "election: $14.00 cash or 0.6549 Cott share (prorated)"},
    "1575189": {"type": "mixed", "sub": "election", "cash": 3.00, "shares": 1.0, "acq": "891103",
                "note": "one New Match share plus $3.00 cash (or, by election, 0.0337 more New Match share) per share"},
    "1609951": {"type": "stock_merger", "shares": 1.65, "acq": "1102266", "note": "1.65 CenterState shares per share"},
    "1380846": {"type": "mixed", "cash": 6.00, "shares": 0.25, "acq": "", "acq_name": "Raymond James (NYSE: RJF)",
                "note": "$6.00 cash + 0.25 Raymond James share"},
    "1130385": {"type": "mixed", "cash": 16.00, "shares": 0.8998, "acq": "", "acq_name": "Amec Foster Wheeler (LSE / NYSE ADS)",
                "note": "$16.00 cash + 0.8998 Amec Foster Wheeler share or ADS"},
    "1478484": {"type": "mixed", "cash": 9.375, "shares": 0.3098, "acq": "1355096.T-QVCA",
                "note": "$9.375 cash + 0.3098 QVC Group Series A (QVCA) share"},
    "785787": {"type": "mixed", "sub": "election", "cash": 110.0, "shares": 2.5011, "acq": "", "rule": "election",
               "acq_name": "Berry Plastics Group (NYSE: BERY)",
               "note": "election: $110.00 cash or 2.5011 Berry shares, prorated to 50% cash and 50% stock"},
    "1038205": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1569650",
                "acq_name": "Bank OZK (Nasdaq; the bank files with the FDIC; master 1569650, listed there from 2018-08-07)",
                "note": "holding company merged into its bank at 4:00 p.m. Central on 2017-06-26: each share became one "
                        "Bank OZK share, listed on Nasdaq from 2017-06-27 under the same symbol and CUSIP"},
    "912752": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1971213", "limit": "2023-05-31",
               "url": _SEC + "912752/000119312523158935/d530850d8k.htm",
               "note": "holding-company reorganisation: each Sinclair Broadcast Group share became one Sinclair, Inc. share "
                       "in the share exchange 'Effective at 12:00 am Eastern U.S. time on June 1, 2023' (closing 8-K, "
                       "read in round 10: the earlier hold on the last session is resolved), so SBG's last session is "
                       "2023-05-31; the bankruptcy words in the 8-K concern Diamond Sports"},
    "750004": {"type": "exchange_move", "sub": "listing_transfer", "dest": "ASX",
               "url": _SEC + "750004/000075000425000046/lnw-20250731.htm",
               "note": "sole primary listing moved to the Australian Securities Exchange; Nasdaq delisting November 2025"},
    "6769": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1841666", "hold_last_session": True,
             "url": _SEC + "1841666/000119312521063695/d127090d8k12b.htm",
             "hold": "APA's 8-K12B says the reorganisation was completed on 2021-03-01 and APA stock trades on Nasdaq 'on an "
                     "uninterrupted basis', but states no effective time: 2021-03-01 may be APA Corporation's first session "
                     "(then Apache's last session is 2021-02-26)",
             "note": "holding-company reorganisation: each Apache share became one APA Corporation share (2021-03-01)"},
    "1100962": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1593034",
                "note": "each Endo Health Solutions share became one Endo International ordinary share at the Paladin "
                        "closing, 2014-02-28 (the $1.16 cash terms are Paladin's); the shares were suspended as of close of "
                        "business on 2014-02-28"},
    "1326807": {"type": "stock_merger", "sub": "second_step_conversion", "shares": 2.55, "acq": "1594012", "limit": "2014-05-07",
                "url": _SEC + "1594012/000119312514179187/d718726d8k.htm",
                "note": "second-step conversion: each public share became 2.5500 shares of the new Investors Bancorp, "
                        "2014-05-07"},
    "1080034": {"type": "mixed", "sub": "election", "cash": 15.14, "shares": 0.07037, "acq": "",
                "acq_name": "Alliance Data Systems (NYSE: ADS)", "stock_value": 0.07037 * 282.2264,
                "note": "standard election: $15.14 cash + 0.07037 Alliance Data share, $35.00 in all at the $282.2264 "
                        "closing VWAP; the Alliance Data close is not available, so the stock part is valued at that VWAP"},
    "1005201": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1808665", "limit": "2020-05-19",
                "url": _SEC + "1005201/000110465920065440/tm2020220-1_8k.htm",
                "note": "Assertio Therapeutics became a subsidiary of Assertio Holdings on 2020-05-19, each share converting "
                        "into one Assertio Holdings share; the Nasdaq listing passed to Assertio Holdings 'effective as of "
                        "May 20, 2020' (Item 3.01), so the last session of the old shares is 2020-05-19; the 2026 Form 25 "
                        "on this CIK concerns the successor's delisting and the master's 2026 delist date is wrong for "
                        "this security"},
    "1375365": {"sub": "removed_then_relisted",
                "note": "removed for late filings in August 2018 and traded OTC until it was listed on Nasdaq again on "
                        "2020-01-14 (the same security); the value is the first OTC close after the removal"},
    "2007825_placeholder": {},
    # ---- tender offers with a CVR (valued at 0) and plain cash the reader missed
    "1138639": {"type": "mixed", "sub": "election", "cash": 4.66, "shares": 0.5355, "acq": "",
                "acq_name": "Nokia ADS (NYSE: NOK)", "url": _SEC + "1138639/000119312525041368/d847449d8k.htm",
                "hold": "the only value available is the existing file's $6.65, the cash alternative; the mixed option "
                        "needs the Nokia ADS close",
                "note": "election among $6.65 cash, 1.7896 Nokia ADS, or $4.66 cash + 0.5355 Nokia ADS (the mixed option), "
                        "prorated so that at most 30% of the consideration is Nokia ADSs; valued at the mixed option"},
    "1293971": {"type": "cash_merger", "sub": "election", "cash": 5.00,
                "note": "election: (i) $3.00 cash plus one CVR, or (ii) $5.00 cash; valued at the all-cash alternative"},
    "1597553": {"type": "cash_merger", "cash": 8.50, "note": "$8.50 cash plus one CVR (up to $3.50, valued at 0)"},
    "1126234": {"type": "cash_merger", "cash": 4.25, "note": "$4.25 cash plus one CVR (valued at 0)"},
    "1423824": {"type": "cash_merger", "cash": 18.00, "note": "$18.00 cash plus one CVR ($2.00, valued at 0)"},
    "1375151": {"type": "cash_merger", "cash": 26.00, "note": "$26.00 cash plus one CVR ($2.00, valued at 0)"},
    "1505512": {"type": "cash_merger", "cash": 7.00, "note": "$7.00 cash plus one CVR ($7.00, valued at 0)"},
    "1322505": {"type": "cash_merger", "cash": 42.00, "note": "$42.00 cash plus one CVR ($10.00, valued at 0)"},
    "1685071": {"type": "cash_merger", "cash": 27.50, "note": "$27.50 cash plus one CVR ($1.50, valued at 0)"},
    "1012140": {"type": "cash_merger", "cash": 125.00, "note": "$125.00 cash (Amgen tender offer)"},
    "1369868": {"type": "cash_merger", "cash": 120.00, "url": _SEC + "1369868/000110465921052598/tm2113368d1_ex99-1.htm",
                "note": "going private: US$120.00 cash per ordinary share (6-K EX-99.1, 2021-04-20)"},
    "1768012": {"type": "stock_merger", "shares": 0.365, "acq": "1883685",
                "note": "each GNOG Class A share converted into 0.365 New DraftKings Class A share"},
    "1801777": {"type": "stock_merger", "shares": 0.1331, "acq": "922247",
                "note": "each AMTI share converted into 0.1331 Cyclo Therapeutics share"},
    "1669600.A": {"type": "stock_merger", "shares": 0.36, "acq": "1324424",
                  "note": "each Liberty Expedia Series A share converted into 0.36 Expedia common share"},
    "1669600.B": {"type": "stock_merger", "shares": 0.36, "acq": "1324424",
                  "note": "each Liberty Expedia Series B share converted into 0.36 Expedia share (Class B, valued at the common)"},
    # ---- special dividends paid to holders at the closing: part of the terminal value (the last close still
    # carries them; the canonical series must not book them on an earlier date)
    "1578735": {"type": "cash_merger", "cash": 32.00, "special_dividend": 2.50, "special_dividend_record": "",
                "url": _SEC + "1578735/000119312521001004/d71355d8k.htm",
                "note": "$32.00 cash plus a special pre-closing dividend of $2.50 (Allstate, closing 2021-01-04)"},
    "826083": {"type": "cash_merger", "cash": 13.75, "special_dividend": 0.13, "special_dividend_record": "2013-10-28",
               "url": _SEC + "826083/000119312513416110/d619138d8k.htm",
               "note": "$13.75 cash; shares held at the close of business on 2013-10-28 also receive the $0.13 special cash "
                       "dividend, paid promptly after the Effective Time"},
    "1756497": {"type": "cash_merger", "cash": 25.75, "special_dividend": 2.00, "special_dividend_record": "2022-10-03",
                "url": _SEC + "1756497/000119312522256246/d403452d8k.htm",
                "note": "$25.75 cash plus the $2.00 special dividend to holders of record as of immediately prior to the "
                        "Effective Time (2022-10-03), payable 2022-10-04"},
    "1581164": {"type": "cash_merger", "cash": 18.75, "special_dividend": 1.75, "special_dividend_record": "2021-06-15",
                "url": _SEC + "1581164/000119312521192102/d183333d8k.htm",
                "note": "$18.75 cash per Paired Share ($20.50 reduced by the Special Dividend) plus the $1.75 Special Dividend "
                        "to holders of record at the close of business on 2021-06-15 (the last session), paid 2021-06-16 "
                        "before the Effective Time"},
    "1545158": {"type": "stock_merger", "sub": "merger", "shares": 1.0, "acq": "1637459", "special_dividend": 16.50,
                "special_dividend_record": "2015-07-02", "url": _SEC + "1545158/000119312515244355/d36612d8k.htm",
                "note": "one Kraft Heinz share per Kraft share plus the $16.50 special cash dividend to holders of record "
                        "immediately prior to the closing (2015-07-02)"},
    # ---- liquidations and failures
    "1011006": {"type": "liquidation", "sub": "liquidating_distributions", "value": 22.38,
                "url": _SEC + "1011006/000119312520284475/d98991d8k.htm",
                "note": "Altaba: liquidating distributions to holders of record on 2019-10-04, from 8-Ks 2020-11-03 ($8.33), "
                        "2021-07-26 ($7.48), 2021-08-20 ($0.54), 2021-12-21 ($0.67), 2022-03-07 ($0.24), 2022-05-27 ($0.75), "
                        "2022-07-21 ($1.43), 2023-01-05 ($0.68), 2023-02-09 ($0.96), 2024-07-31 ($1.10), 2025-05-06 ($0.20); "
                        "undiscounted, later distributions possible; the $51.50 initial distribution preceded the delisting "
                        "(NAV $23.08 on 2019-09-30)"},
    "1288784": {"type": "bankruptcy_otc", "sub": "bank_failure", "limit": "2023-03-10",
                "url": "https://www.fdic.gov/news/press-releases/2023/pr23018.html",
                "approved": "round-4 re-review: the last Nasdaq trade (2023-03-10) and the first OTC close (2023-03-28) "
                            "were checked against the vendor rows and FDIC PR-18-2023",
                "note": "Signature Bank was closed by the New York State Department of Financial Services and put into FDIC "
                        "receivership on Sunday 2023-03-12 (FDIC PR-18-2023); it filed with the FDIC, not the SEC. Last "
                        "Nasdaq trade 2023-03-10 (the vendor rows 2023-03-13 to 03-27 repeat its close on 0 to 2,605 "
                        "shares); the value is the first OTC close"},
    "1001233": {"type": "bankruptcy_otc", "sub": "removed_by_exchange",
                "note": "Nasdaq delisting for the minimum bid price (8-K Item 8.01, 2026-04-29)"},
    "1785041": {"type": "liquidation", "sub": "spac_trust_redemption",
                "note": "business combination terminated 2022-04-15; public shares redeemed at the trust value per share "
                        "(amount not stated in the documents read)"},
}
REVIEWED.pop("2007825_placeholder", None)

# The old shares at each relist junction (RELIST_JUNCTIONS, reconcile step): bankruptcies after which the security
# listed on Nasdaq again with new shares. Read by hand on 2026-10-02 from the documents named (the emergence 8-Ks,
# the 8-Ks that date the Nasdaq suspension, WW's 10-Q for the share counts, Thryv's 2020 prospectus for Dex Media,
# which filed no 8-K after its 2016 deregistration). Each row ends on the old shares' last Nasdaq session and is
# valued by their OTC tail when a vendor has it (CHRD), else by the plan of reorganization (WW: new shares per old
# share, held for the owner's approval; OPI and Dex Media: nothing). Notes and approvals carry SEC facts and dates
# only (no vendor levels).
REVIEWED.update({
    "1486159": {"type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2020-10-09",
                "url": _SEC + "1486159/000148615920000080/oas-20201002.htm",
                "approved": "the 8-K dates the OTC start: Nasdaq delisted the common stock at the opening of business on "
                            "2020-10-12 and OTC Pink trading as OASAQ began that day; the vendor rows from 2020-10-12 to "
                            "2020-11-19 are the old shares' OTC tail (Yahoo's chart carries the old OAS history up to the "
                            "relist junction of 2020-11-20), so the first OTC close values the old shares after their "
                            "last Nasdaq session, 2020-10-09",
                "note": "Oasis Petroleum: Chapter 11 filed 2020-09-30; delisted from Nasdaq at the opening on 2020-10-12, "
                        "OTC Pink as OASAQ from that day; the plan became effective on 2020-11-19: the old common stock "
                        "was cancelled and its holders received warrants (emergence 8-K filed 2020-11-20); the new "
                        "shares trade on Nasdaq from 2020-11-20"},
    "105319": {"type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2025-05-15", "rule": "plan_new_shares",
               "shares": 0.0111676, "url": _SEC + "105319/000095017025106230/ww-20250630.htm",
               # held for the owner: the builder does not approve its own plan valuation (round-8 review). To
               # commit the value, rename ``checked`` to ``approved`` and drop ``hold``
               "hold": "the plan valuation awaits the owner's approval: the new shares per old share come from the "
                       "10-Q's share counts (issued less treasury), and the new shares' first vendor close is 29 "
                       "sessions after the old shares' last Nasdaq session",
               "checked": "no vendor has the old shares' OTC tail (WGHTQ: Yahoo answers 404, the Tiingo WW file stops "
                           "at 2025-05-15); valued by the plan: the 10-Q for the quarter to 2025-06-30 states that "
                           "900,000 new shares went to the holders of prepetition common stock and that 130,048 "
                           "thousand issued less 49,458 thousand treasury shares were cancelled (80,590 thousand "
                           "outstanding), so 0.0111676 new share per old share, priced at the new shares' first vendor "
                           "close on the relist junction, 2025-06-27",
               "note": "WW International: suspended from Nasdaq on 2025-05-16 (8-K 2025-06-18), Pink market as WGHTQ; "
                       "the plan became effective on 2025-06-24: the old common stock was cancelled and the holders of "
                       "existing equity interests received 900,000 of the 10,000,000 new shares (emergence 8-K "
                       "2025-06-25; 10-Q filed 2025-08-11); the new shares trade from 2025-06-27"},
    "1456772": {"type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2025-10-06", "value": 0.0,
                "url": _SEC + "1456772/000110465926076652/tm2618043d2_8k.htm",
                "approved": "the emergence 8-K (Item 3.03): the Old Common Shares were cancelled on the 2026-06-17 "
                            "Effective Date and their holders did not receive any distribution; the last Nasdaq session "
                            "is 2025-10-06 (suspended on 2025-10-07, 8-K filed 2025-11-06); no vendor has the old shares "
                            "after the WIKI end (unfillable.csv: Tiingo's OPI is the new entity), and none is needed",
                "note": "Office Properties Income Trust: suspended from Nasdaq on 2025-10-07 and quoted on OTC Pink as "
                        "OPITS; Chapter 11 filed 2025-10-30; the plan became effective on 2026-06-17: the old shares "
                        "were cancelled with no distribution; the new shares trade on Nasdaq from 2026-06-22"},
    "1556739": {"type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2016-01-06", "value": 0.0,
                "url": _SEC + "1556739/000114036120022046/nt10007762x19_424b4.htm",
                "approved": "an inference, not a document's statement: Thryv's 2020 prospectus says only that Dex Media's "
                            "former lenders 'obtained ownership of 100% of the common stock of the reorganized Dex "
                            "Media, Inc., subject to dilution from a management incentive plan'; it does not say what the "
                            "old holders received, and Dex Media filed no 8-K after its 2016-02-05 deregistration (none "
                            "is on EDGAR to 2020), so -100% is read from the lenders' 100% (no cash, warrant or other "
                            "distribution to the old holders is named anywhere read); Nasdaq suspended DXM at the "
                            "opening of business on 2016-01-07 (8-K filed 2016-01-05), so its last Nasdaq session is "
                            "2016-01-06",
                "note": "Dex Media (DXM): suspended from Nasdaq on 2016-01-07 (Form 25 filed 2016-01-26), deregistered "
                        "on 2016-02-05, prepackaged Chapter 11 in 2016, emerged on 2016-07-29 (the old equity's "
                        "treatment is inferred: the lenders obtained 100% of the new common stock); the new shares "
                        "(Thryv) listed on Nasdaq on 2020-10-01"},
    # round 9: the relistings the final review of round 8 found (RELIST_JUNCTIONS 1580864 and 1009759; Frontier's old
    # shares are in REVIEWED_OLD_SHARES, since its row values the 2026 delisting of the new shares)
    "1580864": {"type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2024-11-29", "rule": "plan_new_shares",
                "shares": 0.2, "url": _SEC + "1580864/000095017025005647/vrm-20250108.htm",
                # held for the owner, as WW: the builder does not approve its own plan valuation
                "hold": "the plan valuation awaits the owner's approval: 0.2 new share per old share (the 1-for-5 "
                        "Bankruptcy Emergence Issuance Adjustment), the warrants valued at 0, and the new shares' first "
                        "vendor close is about 53 XNAS sessions after the old shares' last Nasdaq session",
                "checked": "no vendor has the old shares' OTC tail (the series stops on 2024-11-29 and resumes with the "
                           "new shares on 2025-02-20); the emergence 8-K: all previously issued equity interests were "
                           "cancelled and extinguished on the 2025-01-14 Effective Date and the 1,822,577 old shares "
                           "were converted at 1-for-5 into new common stock (364,515 shares, the 7.06% the convertible "
                           "noteholders' 92.94% leaves), with 364,516 warrants at $60.95 to the stockholders",
                "note": "Vroom: Nasdaq suspended the common stock at the opening of business on 2024-12-02 (8-K "
                        "2024-11-26, Item 3.01); prepackaged Chapter 11 filed 2024-11-13, effective 2025-01-14; the new "
                        "common stock was registered on the Nasdaq Global Market on 2025-02-19 (8-A12B) and trades from "
                        "2025-02-20"},
    "1009759": {"type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2023-10-04",
                "url": _SEC + "1009759/000155837023016108/cgrn-20230926x8k.htm",
                "note": "Capstone Green Energy: trading on Nasdaq suspended at the opening of business on 2023-10-05 "
                        "(8-K 2023-09-28, Item 3.01; Form 25 filed 2023-10-12); prepackaged Chapter 11 filed "
                        "2023-09-28, effective 2023-12-07: the old common stock was canceled and the successor issued "
                        "18,540,877 new shares pro rata to its holders (8-K12G3); the new shares were quoted OTC as CGEH "
                        "and trade on Nasdaq as CEPL from 2026-07-08. The old shares are valued by their OTC tail "
                        "(Tiingo has them from 2023-10-05), held by the guard for a hand check of that close"},
    "1839341": {"type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2022-12-30",
                "url": _SEC + "1839341/000119312524013078/d661343d8k.htm",
                "note": "Core Scientific: Chapter 11 filed 2022-12-21; the old common stock traded exclusively on OTC "
                        "Pink as CORZQ from 2023-01-03; the plan became effective on 2024-01-23: the old common stock "
                        "was cancelled and Existing Common Interests received 21.0% of the new common stock (the 8-K "
                        "states no per-share ratio); the new shares trade on Nasdaq from 2024-01-24. Not a D5 case: "
                        "the old shares wait for their vendor rows (CORZQ, Tiingo month 2) and their OTC tail"},
})

# The old shares at a relist junction of a security whose row values a later end: the new shares themselves were
# delisted afterwards (Frontier: FYBR acquired by Verizon, Form 25 effective 2026-01-30), so the row is the new
# shares' and the old shares' valuation goes to the ``relist_old_shares_*`` columns. Keys as in REVIEWED.
REVIEWED_OLD_SHARES: dict[str, dict] = {
    "20520": {"type": "bankruptcy_otc", "sub": "bankruptcy", "limit": "2020-04-23", "value": 0.0,
              "url": _SEC + "20520/000114036121015200/brhc10023786_8k12g3.htm",
              "approved": "partly an inference: the emergence 8-K12G3 states that all equity securities of Old Frontier, "
                          "including its common stock, were canceled, released and extinguished on the 2021-04-30 "
                          "Effective Date (Item 1.02) and that the new common stock went to the holders of Allowed "
                          "Senior Notes Claims (Item 3.02); it names no distribution to the old stockholders (the plan "
                          "itself was not read), so -100% is read from those statements; Nasdaq suspended the old "
                          "shares at the opening of business on 2020-04-24 (8-K 2020-04-17, Item 3.01), so their last "
                          "Nasdaq session is 2020-04-23",
              "note": "Frontier Communications (old FTR): suspended from Nasdaq on 2020-04-24, OTC as FTRQ; Chapter 11 "
                      "filed 2020-04-14; the plan became effective on 2021-04-30 with the old common stock cancelled; "
                      "the new shares trade on Nasdaq as FYBR from 2021-05-04"},
}

# The document behind each reviewed entry that sets cash, shares or a value without its own ``url``:
# found by searching the cached documents of the security for the reviewed figures (closing 8-Ks
# first); for a one-for-one reorganisation or rename (no figure to search), the closing 8-K that
# describes it. It replaces the document the automatic reader chose.
REVIEWED_SOURCES: dict[str, str] = {
    "885721": _SEC + "885721/000119312512144955/0001193125-12-144955-index.htm",  # closing
    "1058057": _SEC + "1058057/000119312521122807/d156000d8k.htm",  # closing
    "1261694": _SEC + "1261694/000119312516782591/d298517d8k.htm",  # closing
    "864683": _SEC + "864683/000086468315000054/form8-k.htm",  # closing
    "1316631.B": _SEC + "1316631/000119312513251856/d548311d8k.htm",  # closing
    "1166691.T-CMCSK": _SEC + "1166691/000095010315009516/0000950103-15-009516-index.htm",  # closing
    "1437107.B": _SEC + "1437107/000119312522103051/d328161d8k.htm",  # closing
    "1437107.C": _SEC + "1437107/000119312522103051/d328161d8k.htm",  # closing
    "1411488.B": _SEC + "1411488/000119312515197227/0001193125-15-197227-index.htm",  # closing
    "1734342.B": _SEC + "1734342/000173434221000071/0001734342-21-000071-index.htm",  # closing
    "1355096.T-QVCA": _SEC + "1355096/000110465918017857/a18-8242_1ex99d1.htm",  # closing (EX-99.1: to be renamed Qurate)
    "1355096.T-QVCB": _SEC + "1355096/000110465918017857/a18-8242_1ex99d1.htm",  # closing (EX-99.1: to be renamed Qurate)
    "1100441": _SEC + "1100441/000119312519069904/0001193125-19-069904-index.htm",  # closing
    "1006269": _SEC + "1006269/000110465921141882/0001104659-21-141882-index.htm",  # closing
    "1339947.B": _SEC + "1339947/000119312519306335/d833313d8k.htm",  # the reviewed figures appear here
    "1339947.A": _SEC + "1339947/000119312519306335/d833313d8k.htm",  # the reviewed figures appear here
    "813828.B": _SEC + "813828/000119312525175027/d52142d8k.htm",  # closing
    "936402": _SEC + "936402/000119312519004574/d673508d8k.htm",  # the reviewed figures appear here
    "1351288": _SEC + "1351288/000119312521151938/d362568d8k.htm",  # the reviewed figures appear here
    "1657312": _SEC + "1657312/000110465925097318/tm2528100d1_8k.htm",  # the reviewed figures appear here
    "1411574": _SEC + "1411574/000119312517094780/d360877d8k.htm",  # the reviewed figures appear here
    "1094739": _SEC + "1094739/000119312519253634/d761174d8k.htm",  # the reviewed figures appear here
    "799088": _SEC + "799088/000119312516799826/d303422d8k.htm",  # the reviewed figures appear here
    "1491778": _SEC + "1491778/000149177817000194/angi2017102-8k.htm",  # closing
    "1470215": _SEC + "1470215/000119312516420526/d116348d8k.htm",  # closing
    "817473": _SEC + "817473/000119312517001138/d314485d8k.htm",  # the reviewed figures appear here
    "891288": _SEC + "891288/000119312514310400/d772821d8k.htm",  # the reviewed figures appear here
    "1110647": _SEC + "1110647/000119312518346220/d672294d8k.htm",  # the reviewed figures appear here
    "1644406": _SEC + "1644406/000119312523272309/d927425d8k.htm",  # the reviewed figures appear here
    "1182129": _SEC + "1182129/000119312515025738/d863332d8k.htm",  # the reviewed figures appear here
    "861361": _SEC + "861361/000094787117000307/ss38796_8k.htm",  # the reviewed figures appear here
    "700733": _SEC + "700733/000119312516528653/d173081d8k.htm",  # the reviewed figures appear here
    "743316": _SEC + "743316/000119312521257603/d203144d8k.htm",  # the reviewed figures appear here
    "858339": _SEC + "858339/000119312520196345/d39509d8k.htm",  # the reviewed figures appear here
    "1175609": _SEC + "1175609/000119312518213483/d812219d8k.htm",  # the reviewed figures appear here
    "1611983.A": _SEC + "1611983/000114036126033932/ef20080711_8k.htm",  # the reviewed figures appear here
    "1611983.C": _SEC + "1611983/000114036126033932/ef20080711_8k.htm",  # the reviewed figures appear here
    "1424454": _SEC + "1424454/000119312516704222/d234513d8k.htm",  # closing
    "1088825": _SEC + "1088825/000119312516704243/d242474d8k.htm",  # the reviewed figures appear here
    "354908": _SEC + "354908/000119312521161540/d127190d8k.htm",  # the reviewed figures appear here
    "929940": _SEC + "929940/000114036122019468/ny20004077x9_8k.htm",  # the reviewed figures appear here
    "1516973": _SEC + "1516973/000119312518268756/d614233d8k.htm",  # the reviewed figures appear here
    "1499875": _SEC + "1499875/000110465915003336/a15-2627_28k.htm",  # the reviewed figures appear here
    "831547": _SEC + "831547/000119312523199589/d332583d8k.htm",  # the reviewed figures appear here
    "1594012": _SEC + "1594012/000119312522098215/d346598d8k.htm",  # the reviewed figures appear here
    "1560385.T-LSXMA": _SEC + "1560385/000110465924098251/tm2422514d16_8k.htm",  # the reviewed figures appear here
    "1560385.T-LSXMB": _SEC + "1560385/000110465924098251/tm2422514d16_8k.htm",  # the reviewed figures appear here
    "1560385.T-LSXMK": _SEC + "1560385/000110465924098251/tm2422514d16_8k.htm",  # the reviewed figures appear here
    "1434729": _SEC + "1434729/000095010317012994/dp84687_ex9901.htm",  # the reviewed figures appear here
    "356213": _SEC + "356213/000119312516564276/d188315d8k.htm",  # the reviewed figures appear here
    "1270400": _SEC + "1270400/000119312513256243/d552872d8k.htm",  # the reviewed figures appear here
    "935494": _SEC + "935494/000114036123047693/ef20012268_8k.htm",  # the reviewed figures appear here
    "1195933": _SEC + "1195933/000119312518211388/d711626d8k.htm",  # the reviewed figures appear here
    "1675820": _SEC + "1675820/000119312520156714/d913146d8k.htm",  # the reviewed figures appear here
    "800458": _SEC + "800458/000119312516449086/d102669d8k.htm",  # the reviewed figures appear here
    "1537667": _SEC + "1537667/000110465918075359/a18-42113_48k.htm",  # the reviewed figures appear here
    "1501364": _SEC + "1501364/000119312519281296/d827353d8k.htm",  # the reviewed figures appear here
    "1511198": _SEC + "1511198/000119312518289519/d612451d8k.htm",  # the reviewed figures appear here
    "1600125": _SEC + "1600125/000094337421000499/form8k_111221.htm",  # the reviewed figures appear here
    "1324410": _SEC + "1324410/000132441019000002/gbnk-20190102x8k.htm",  # the reviewed figures appear here
    "846901": _SEC + "846901/000119312524140117/d811981d8k.htm",  # the reviewed figures appear here
    "1176316": _SEC + "1176316/000114420419004222/tv512079_8-k.htm",  # the reviewed figures appear here
    "1748907": _SEC + "1748907/000119312524014239/d74405d8k.htm",  # the reviewed figures appear here
    "1312928": _SEC + "1312928/000119312513228969/d540491d8k.htm",  # the reviewed figures appear here
    "1617977": _SEC + "1617977/000119312520077250/d143057d8k.htm",  # the reviewed figures appear here
    "1160958": _SEC + "1160958/000119312521123317/d147406d8k.htm",  # the reviewed figures appear here
    "1334814": _SEC + "1334814/000119312515050780/d874732d8k.htm",  # closing
    "1365101": _SEC + "1365101/000119312520059088/d873868d8k.htm",  # the reviewed figures appear here
    "1575189": _SEC + "1575189/000110465920080741/tm2023781d3_8k.htm",  # the reviewed figures appear here
    "1609951": _SEC + "1609951/000143774919006083/ncom20190329_8k.htm",  # the reviewed figures appear here
    "1380846": _SEC + "1380846/000119312522165229/d332819d8k.htm",  # the reviewed figures appear here
    "1130385": _SEC + "1130385/000114420414067433/v393901_8k.htm",  # the reviewed figures appear here
    "1478484": _SEC + "1478484/000119312515335489/d78857d8k.htm",  # the reviewed figures appear here
    "785787": _SEC + "785787/000119312517015318/d330949d8k.htm",  # the reviewed figures appear here
    "1038205": _SEC + "1038205/000156459017012994/0001564590-17-012994-index.htm",  # closing
    "912752": _SEC + "912752/000119312523158935/0001193125-23-158935-index.htm",  # closing
    "1100962": _SEC + "1100962/000119312514077915/d683967d8k.htm",  # closing
    "1080034": _SEC + "1080034/000110121514000309/form_8k.htm",  # the reviewed figures appear here
    "1293971": _SEC + "1293971/000119312525132850/d39702d8k.htm",  # the reviewed figures appear here
    "1597553": _SEC + "1597553/000119312525170187/d877271d8k.htm",  # the reviewed figures appear here
    "1126234": _SEC + "1126234/000119312524276512/d909721d8k.htm",  # the reviewed figures appear here
    "1423824": _SEC + "1423824/000119312519271319/d822414d8k.htm",  # the reviewed figures appear here
    "1375151": _SEC + "1375151/000119312522067384/d272891d8k.htm",  # the reviewed figures appear here
    "1505512": _SEC + "1505512/000110465925062408/tm2518614d30_8k.htm",  # the reviewed figures appear here
    "1322505": _SEC + "1322505/000110465923027946/tm238312d2_8k.htm",  # the reviewed figures appear here
    "1685071": _SEC + "1685071/000095015719001299/form8k.htm",  # the reviewed figures appear here
    "1012140": _SEC + "1012140/000119312513387156/d604099d8k.htm",  # the reviewed figures appear here
    "1768012": _SEC + "1768012/000110465922056127/tm2214542d1_8k.htm",  # the reviewed figures appear here
    "1801777": _SEC + "1801777/000143774923035381/amti20231222_8k.htm",  # the reviewed figures appear here
    "1669600.A": _SEC + "1669600/000114036119013508/nc10003451x1_8k.htm",  # the reviewed figures appear here
    "1669600.B": _SEC + "1669600/000114036119013508/nc10003451x1_8k.htm",  # the reviewed figures appear here
}


# The code's own entries, before the round-10 verdicts (CACHE/review/round10/merged/terminal_verdicts.csv) are merged
# into REVIEWED by load_review_verdicts (main does it unless --no-review-verdicts).
CODE_REVIEWED: dict[str, dict] = {sid: dict(entry) for sid, entry in REVIEWED.items()}
REVIEW_VERDICTS = review_merge.MERGED_DIR / "terminal_verdicts.csv"
REVIEW_VERDICTS_APPLIED: dict = {"skipped": True}  # set by main


def load_review_verdicts(path: Path | None = None) -> dict:
    """Merge the hand-review verdicts into REVIEWED (reversal_data_review.terminal_entries: a verdict's fields
    replace the code's, blank ones keep them; price_gap rows add terms and limits but no approval). Returns
    counts for the summary; a missing file changes nothing."""
    path = Path(path or REVIEW_VERDICTS)
    if not path.exists():
        return {"file": str(path), "rows": 0, "entries": 0, "missing": True}
    frame = review_merge.read_csv(path)
    entries = review_merge.terminal_entries(frame, CODE_REVIEWED)
    REVIEWED.clear()
    REVIEWED.update({sid: dict(entry) for sid, entry in CODE_REVIEWED.items()})
    REVIEWED.update(entries)
    return {"file": str(path), "rows": int(len(frame)), "entries": len(entries),
            "new_entries": len([s for s in entries if s not in CODE_REVIEWED]),
            "by_verdict": frame["verdict"].value_counts().to_dict(),
            "approved": len([s for s, e in entries.items() if e.get("approved") and e.get("url")])}


def _fmt(value) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def classify(row: dict, ev: dict) -> dict:
    """How the listing ended and what one share received, from the Form 25, the master links and
    the documents' terms. Returns terminal_type, event_subtype, cash, shares, acquirer phrase,
    destination exchange, source_url and a note."""
    basis = row["f25_delisting_basis"]
    items = _items(str(ev.get("closing_items", "")).replace(" ", ","))
    cash = ev.get("terms_cash")
    shares = ev.get("terms_shares")
    cash = None if cash is None or (isinstance(cash, float) and np.isnan(cash)) else float(cash)
    shares = None if shares is None or (isinstance(shares, float) and np.isnan(shares)) else float(shares)
    terms_url = ev.get("terms_source_url") or ""
    closing_url = ""
    if ev.get("closing_accessions"):
        first = str(ev["closing_accessions"]).split()[0]
        closing_url = index_url(int(float(row["cik"])), first) if row.get("cik") else ""
    form25_url = row.get("f25_doc_url") or ""
    out = {"terminal_type": "unknown", "event_subtype": "", "cash": None, "shares": None, "stock_value": None,
           "acquirer_phrase": ev.get("terms_acquirer_phrase") or "", "acquirer_class": ev.get("terms_acquirer_class") or "",
           "destination_exchange": "",
           "source_url": terms_url or closing_url or form25_url, "note": "",
           "cvr": bool(ev.get("terms_cvr")), "election": bool(ev.get("terms_election")),
           "either_or": str(ev.get("terms_either_or")) == "True"}
    transfer_to = ev.get("transfer_to") or ""
    sec_exchanges = [e for e in str(row.get("exchanges_sec_current") or "").split() if e and e != "Nasdaq"]
    if row["end_source"] == "transfer" or (basis == "issuer_withdrawal" and transfer_to and transfer_to != "OTC") \
            or (row.get("transfer_date") and not row.get("delist_date")):
        destination = transfer_to if transfer_to and transfer_to != "OTC" else (sec_exchanges[0] if sec_exchanges else "")
        url = row.get("transfer_form25_doc_url") or closing_url or form25_url
        return {**out, "terminal_type": "exchange_move", "event_subtype": "listing_transfer",
                "destination_exchange": destination, "source_url": url,
                "note": "" if destination else "destination exchange not stated in the documents read"}
    removal = basis == "exchange_removal"
    stock_value = ev.get("terms_stock_value")
    stock_value = None if stock_value in (None, "") or (isinstance(stock_value, float) and np.isnan(stock_value)) \
        else float(stock_value)
    has_stock = shares is not None or stock_value is not None
    has_terms = cash is not None or has_stock
    if has_terms and not (removal and "2.01" not in items):
        kind = "mixed" if cash is not None and has_stock else ("cash_merger" if cash is not None else "stock_merger")
        out["stock_value"] = stock_value
        subtype = "merger"
        if kind == "stock_merger" and shares == 1.0 and (ev.get("reorganization") or row.get("successor_security_id")):
            subtype = "reorganization"
        if basis in ("redeemed_or_matured", "called_for_redemption") and kind == "cash_merger" and ev.get("liquidation"):
            kind, subtype = "liquidation", "redemption"
        return {**out, "terminal_type": kind, "event_subtype": subtype, "cash": cash, "shares": shares}
    if row.get("successor_security_id"):
        return {**out, "terminal_type": "stock_merger", "event_subtype": "reorganization", "shares": 1.0,
                "source_url": closing_url or form25_url,
                "note": "successor link in security_master (holding-company reorganisation or redomicile); 1 share assumed"}
    if removal or "1.03" in items or ev.get("bankruptcy"):
        subtype = "bankruptcy" if ("1.03" in items or ev.get("bankruptcy")) else "removed_by_exchange"
        return {**out, "terminal_type": "bankruptcy_otc", "event_subtype": subtype,
                "source_url": closing_url or form25_url}
    if basis == "issuer_withdrawal":
        return {**out, "terminal_type": "bankruptcy_otc", "event_subtype": "voluntary_delisting",
                "source_url": closing_url or form25_url,
                "note": "issuer's own Form 25 with no transfer and no merger terms in the documents read"}
    if basis in ("redeemed_or_matured", "called_for_redemption", "rights_extinguished") and ev.get("liquidation"):
        return {**out, "terminal_type": "liquidation", "event_subtype": "redemption_or_dissolution",
                "source_url": closing_url or form25_url, "note": "amount per share not read"}
    note = "no consideration found in the documents read" if basis == "substituted_merger_or_exchange" else \
        "no SEC evidence decided the end"
    return {**out, "note": note}


REVIEW_KEYS = {"type": "terminal_type", "sub": "event_subtype", "cash": "cash", "shares": "shares",
               "acq": "acquirer_security_id", "acq_name": "acquirer_phrase", "stock_value": "stock_value",
               "rule": "value_rule", "dest": "destination_exchange", "url": "source_url", "value": "fixed_value",
               "extra": "extra", "limit": "limit", "hold": "hold", "start": "start",
               "special_dividend": "special_dividend", "special_dividend_record": "special_dividend_record",
               "special_dividend_before_last_trade": "special_dividend_before_last_trade",
               "approved": "approved", "hold_last_session": "hold_last_session", "review": "review_note"}


GUARD_ELECTION = re.compile(r"\bprorat|\belect(?:ion|ions|ed|ing)?\b", re.I)


def _flag(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "y", "yes")


def guard_reasons(decided: dict, out: dict, ev: dict | None = None) -> list[str]:
    """Why the consideration needs a hand check whatever the value: a holder election or a proration (the traded
    share's consideration depends on the election results), or a CVR (valued at 0). Read from the flags the
    terms reader and the closing documents raised (``ev``: a closing 8-K can state an election the lead
    terms reader missed, as for PCYC, SNI and HCBK), the reviewed subtype and rule, and the reviewed note."""
    ev = ev or {}
    note = decided.get("note") or ""
    reasons = []
    if out.get("election") == "Y" or decided.get("event_subtype") == "election" or _flag(ev.get("election")) \
            or decided.get("value_rule") in ("election", "cash_alternative") or GUARD_ELECTION.search(note):
        reasons.append("the consideration involves a holder election or a proration")
    if out.get("cvr") == "Y" or _flag(ev.get("cvr")) or CVR.search(note):
        reasons.append("the consideration includes a CVR (valued at 0)")
    return reasons


def apply_review(row: dict, decided: dict) -> dict:
    """The reading after the hand review in REVIEWED (keys: type, sub, cash, shares, acq, acq_name,
    stock_value, rule, dest, url, value, note); a key that is absent keeps the automatic value."""
    review = REVIEWED.get(row["security_id"])
    out = dict(decided)
    if not review:
        if decided.get("either_or"):  # cash OR stock: the committed terms are alternatives, not a sum
            out["value_rule"], out["event_subtype"] = "election", "election"
        return out
    for key, target in REVIEW_KEYS.items():
        if key in review:
            out[target] = review[key]
    if "url" not in review and row["security_id"] in REVIEWED_SOURCES:
        out["source_url"] = REVIEWED_SOURCES[row["security_id"]]
    if review.get("type") in ("cash_merger", "liquidation") and "shares" not in review:
        out["shares"] = None
    if review.get("type") == "stock_merger" and "cash" not in review:
        out["cash"] = None
    if review.get("type") in ("exchange_move", "bankruptcy_otc", "unknown"):
        out["cash"], out["shares"] = review.get("cash"), review.get("shares")
    out["note"] = ("reviewed: " + review.get("note", "")).strip()
    # the output note: the code's note describes the code's terms, so once a hand-review verdict restates them the
    # output says the verdict's terms instead (the code's note stays in ``note`` for the guards, which read it)
    restated = restated_terms(row["security_id"], review)
    out["output_note"] = (f"reviewed: terms as the hand-review verdict restates them ({restated})" if restated
                          else out["note"])
    out["reviewed"] = True
    return out


RESTATED_KEYS = ("type", "sub", "cash", "shares", "value", "rule", "stock_value")


def restated_terms(sid: str, review: dict) -> str:
    """The terms a merged hand-review verdict changed against the code's own entry (CODE_REVIEWED), as a short line
    ('type stock_merger, cash none, shares 1.0337'), or '' when no verdict was merged or none of the terms changed
    (a subtype the verdict only fills in does not count: the code's note still describes the terms)."""
    if not review.get("review"):
        return ""
    code = CODE_REVIEWED.get(sid) or {}
    same = lambda a, b: (a == b) or (isinstance(a, (int, float)) and isinstance(b, (int, float))
                                     and not isinstance(a, bool) and abs(float(a) - float(b)) < 1e-12)
    changed = [key for key in RESTATED_KEYS if not same(review.get(key), code.get(key))]
    if not [key for key in changed if key != "sub"]:  # a subtype filled in alone leaves the code's note true
        return ""
    return ", ".join(f"{key} {_fmt(review[key]) if review.get(key) is not None else 'none'}"
                     for key in RESTATED_KEYS if key in changed or review.get(key) is not None)


def build_rows(scope: pd.DataFrame, evidence: pd.DataFrame, book: PriceBook, existing: dict[str, dict],
               names: NameIndex, candidates: pd.DataFrame, verified_at: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One output row per scoped security, plus the local-only table of the levels used."""
    clean = lambda v: None if isinstance(v, float) and np.isnan(v) else v
    charts: dict[str, pd.DataFrame] = {}
    ev_by = {r["security_id"]: {k: clean(v) for k, v in r.items()} for r in evidence.to_dict("records")}
    rows, used = [], []
    for row in scope.to_dict("records"):
        sid = row["security_id"]
        ev = ev_by.get(sid, {})
        decided = apply_review(row, classify(row, ev))
        kind = decided["terminal_type"]
        out = {c: "" for c in OUTPUT_COLUMNS}
        out.update({
            "ticker": row["last_ticker"] or row["first_ticker"], "security_id": sid,
            "delist_date": row["delist_date"] or row["transfer_date"], "terminal_type": kind,
            "event_subtype": decided["event_subtype"], "destination_exchange": decided["destination_exchange"],
            "source_url": decided["source_url"], "verified_at": verified_at,
            "consideration_cash": _fmt(decided["cash"]), "consideration_shares": _fmt(decided["shares"]),
            "acquirer_security_id": decided.get("acquirer_security_id") or "",
            "acquirer_name": decided["acquirer_phrase"] if kind in ("stock_merger", "mixed") else "",
            "cvr": "Y" if decided["cvr"] else "N", "election": "Y" if decided["election"] else "N",
            "end_date": row["end_date"], "end_source": row["end_source"],
            "form25_accession": row["delist_form25_accession"] or row["transfer_form25_accession"],
            "form25_basis": row["f25_delisting_basis"] or row["transfer_basis"],
            "best_rank": "" if pd.isna(row["best_rank"]) else str(int(float(row["best_rank"]))),
            "in_candidates": row["in_candidates"], "cik": row["cik"], "name": row["name"],
            "terms_stage": ev.get("terms_stage", "") or "",
        })
        if not out["source_url"]:  # no SEC document read: the listing evidence the end comes from
            out["source_url"] = row.get("listing_source_url") or ""
        output_note = decided.get("output_note", decided["note"])
        notes = [output_note] if output_note else []
        if decided.get("review_note"):  # the hand-review verdict merged into REVIEWED (load_review_verdicts)
            notes.append(decided["review_note"])
        holds = [decided["hold"]] if decided.get("hold") else []  # reasons to hold a computed value for review
        prior = existing.get(sid)
        stock_leg = kind in ("stock_merger", "mixed")
        if prior:
            out.update({"existing_file": prior["existing_file"],
                        "existing_terminal_return": _fmt(float(prior["terminal_return"])),
                        # an existing stock-leg value prices the acquirer at a vendor close: local only
                        "existing_consideration_per_share": "" if stock_leg else _fmt(prior.get("consideration_per_share")),
                        "existing_source_url": prior["source_url"]})
        # ---- a relist junction (RELIST_JUNCTIONS, reconcile step): the security listed again with new shares out
        # of a bankruptcy, so this row is the old shares': their last trade, OTC close and plan value come from
        # their own rows (``view``: the book cut before the new shares' first session), never the new shares'
        junction = RELIST_JUNCTIONS.get(sid) or {}
        first_new = junction.get("first_new_session", "")
        old_level = {}
        if first_new and sid in REVIEWED_OLD_SHARES:
            # the new shares were delisted later (Frontier's FYBR, 2026-01-30): this row values that end with the whole
            # book, and the old shares' valuation goes to the relist_old_shares_* columns (a Form 25 for the old shares
            # that Nasdaq dated after the relisting, as WW's 2025-07-13, is still the old shares' end)
            old_cols, old_level = old_shares_valuation(sid, junction, book)
            out.update(old_cols)
            first_new = ""
        view = book.before(sid, first_new) if first_new else book
        if first_new:
            out["relist_junction"] = first_new
            notes.append(f"old shares at a relist junction: the plan became effective on "
                         f"{junction.get('effective_date') or '?'} and the new shares (first traded session {first_new}) "
                         "are a separate segment of the canonical series under the same security_id")
        # ---- the last Nasdaq trade
        limit, anchor = price_window(row, ev)
        limit_basis = f"{row['end_source']}_end"
        if row["end_source"] == "snapshots" and str(ev.get("closing_dates") or "").strip():
            limit_basis = "last_closing_8k_filing"  # not the snapshot slack: the closing 8-K dates the end
        if decided.get("limit"):  # a reviewed last trading day (the closing the snapshots only bracket)
            limit, anchor, limit_basis = decided["limit"], "", "reviewed"
            if kind == "exchange_move":
                out["destination_start_date"] = decided.get("start") or ""
        elif kind == "exchange_move" and decided["destination_exchange"] != "Nasdaq":
            limit, start, limit_basis = transfer_window(row, ev)
            # the last Nasdaq session lies just before the transfer: a vendor series that stops weeks earlier
            # (MSG: no rows 2015-05-08 to 2015-09-30) does not date it
            anchor, out["destination_start_date"] = limit, start
        else:
            if row["end_source"] == "snapshots" and not str(ev.get("closing_dates") or "").strip():
                cut, reason = snapshot_cut(view, sid, row["end_date"], limit)
                if reason:
                    limit, limit_basis = cut, reason
            # a successor reorganisation the master links (holding company, redomicile, split-off): the Form 25
            # that Nasdaq files for it falls on or after the company's last session, and the later rows under
            # the same ticker are the successor's (SSYS: Form 25 2012-11-30, Stratasys Ltd from 2012-12-03)
            successor_day = str(row.get("successor_date") or "")
            if row["end_source"] == "snapshots" and kind in ("stock_merger", "mixed") and successor_day \
                    and successor_day < limit:
                limit, anchor, limit_basis = successor_day, successor_day, "successor_form25_filing"
            # the closing 8-K's own statement of the halt ('prior to the open of trading on the Closing Date');
            # one far before the window is a misreading and is ignored
            halt = ev.get("halt_before_open") or ""
            if kind not in ("bankruptcy_otc", "unknown") and halt \
                    and _shift(limit, -HALT_WINDOW_DAYS) <= previous_session(halt) < limit:
                limit, limit_basis = previous_session(halt), "session_before_halt_stated_in_closing_8k"
            after_close = ev.get("halt_after_close") or ""
            if kind not in ("bankruptcy_otc", "unknown") and after_close and _shift(limit, -HALT_WINDOW_DAYS) <= after_close < limit:
                limit, limit_basis = after_close, "halt_after_close_stated_in_closing_8k"
            # an effective time after the close ('On December 29, 2017 at 5:00 p.m. ... (the "Distribution
            # Effective Time")'): the shares traded that day and not after it
            effective = ev.get("effective_after_close") or ""
            if kind not in ("bankruptcy_otc", "unknown") and effective and _shift(limit, -HALT_WINDOW_DAYS) <= effective < limit:
                limit, limit_basis = effective, "effective_time_after_close_stated_in_closing_8k"
            # an effective time at midnight ('12:01 a.m., Eastern Time, on August 19, 2025'): no trade on that day
            midnight = ev.get("effective_before_open") or ""
            if kind not in ("bankruptcy_otc", "unknown") and midnight \
                    and _shift(limit, -HALT_WINDOW_DAYS) <= previous_session(midnight) < limit:
                limit, limit_basis = previous_session(midnight), "session_before_midnight_effective_time_in_closing_8k"
            # the first day of the shares that replace the company's ('will begin trading on July 1, 2016 under the
            # symbol "CATM"'): the company's last session is the one before
            if kind in ("stock_merger", "mixed"):
                starts = [previous_session(d) for d in str(ev.get("successor_start") or "").split() if d]
                starts = [d for d in starts if _shift(limit, -HALT_WINDOW_DAYS) <= d < limit]
                if starts:
                    limit, limit_basis = min(starts), "session_before_stated_successor_start"
        suspended = ev.get("suspension_date") or ""
        if kind == "bankruptcy_otc" and suspended and suspended <= limit and limit_basis != "reviewed":
            limit, limit_basis = _shift(suspended, -1), "day_before_stated_suspension"
        if first_new and limit >= first_new:  # the old shares stop before the new ones trade
            limit, anchor, limit_basis = previous_session(first_new), "", "session_before_relist_junction"
        trade = last_trade(view, sid, limit, anchor)
        level = {"security_id": sid, "limit": limit, "limit_basis": limit_basis, "anchor": anchor,
                 **{k: trade.get(k, "") for k in ("status", "last_date", "vendor_last_date", "stored_last_date", "close",
                                                    "src", "n_sources", "max_source_diff", "filler_dropped")}, **old_level}
        if trade["status"] == "ok":
            out["last_price_date"] = trade["last_date"]
            out["price_source"] = trade["src"]
            out["price_source_url"] = PRICE_SOURCE_URLS[trade["src"]].format(ticker=out["ticker"])
        overrun = slack_overrun(row["end_source"], kind, limit_basis, limit, trade)
        if overrun:  # the later vendor rows may be a successor's under the same ticker (QRTEA -> QVCGA)
            holds.append(overrun)
        pending = price_pending(sid, candidates, book) if trade["status"] != "ok" else ""
        price_status = "ok" if trade["status"] == "ok" else ("pending_price" if pending else "no_vendor_price")
        if price_status != "ok":
            notes.append(pending or f"no vendor raw close on the last session ({trade['status']}; vendor to "
                                    f"{trade.get('vendor_last_date') or '-'}, stored to {trade.get('stored_last_date') or '-'})")
        last_day = pd.Timestamp(trade["last_date"]) if trade["status"] == "ok" else None
        # ---- value
        value, basis_note, status = None, "", ""
        if kind == "exchange_move":
            status = "no_terminal_return"
        elif kind == "unknown":
            status = "unknown"
        elif decided.get("fixed_value") is not None:
            value, basis_note = float(decided["fixed_value"]), "sec_terms_reviewed_total"
            out["consideration_per_share"] = _fmt(value)
        elif kind == "mixed" and decided.get("value_rule") == "cash_alternative" and decided["cash"] is not None:
            value, basis_note = float(decided["cash"]), "sec_terms_cash_alternative_of_election"
            out["consideration_per_share"] = _fmt(value)
            notes.append("holders elected cash or stock (prorated); valued at the cash alternative")
        elif kind == "cash_merger" or (kind == "liquidation" and decided["cash"] is not None):
            value, basis_note = float(decided["cash"]), "sec_cash_terms"
            out["consideration_per_share"] = _fmt(value)
        elif kind == "liquidation":
            status = "unknown"
            notes.append("liquidation amount per share not read")
        elif stock_leg and decided.get("stock_value") is not None and (
                decided.get("shares") is None or not decided.get("acquirer_security_id")):
            value = (decided["cash"] or 0.0) + float(decided["stock_value"])
            basis_note = "sec_terms_fixed_value_stock_part"
            out["consideration_per_share"] = _fmt(value)
            notes.append(f"stock part fixed at ${decided['stock_value']:g} of acquirer stock (VWAP ratio not read)")
        elif stock_leg:
            acquirer = decided.get("acquirer_security_id") or ""
            how = "reviewed" if acquirer else ""
            if not acquirer and decided["event_subtype"] == "reorganization" and row.get("successor_security_id"):
                acquirer, how = row["successor_security_id"], "successor_link"
            if not acquirer and decided["acquirer_phrase"]:
                acquirer, how = names.find(decided["acquirer_phrase"], trade.get("last_date") or row["end_date"],
                                           hint=decided.get("acquirer_class") or decided["acquirer_phrase"],
                                           exclude_cik=row["cik"])
            out["acquirer_security_id"], out["acquirer_match"] = acquirer, how
            one_share = decided["shares"] == 1.0 and decided["cash"] is None
            same_series = one_share and decided["event_subtype"] in ("reorganization", "reclassification", "rename")
            same_ticker = one_share and bool(acquirer) and names.ticker_of(acquirer) == out["ticker"]
            symbol = ACQUIRER_SYMBOLS.get(sid, "")
            if last_day is not None and (acquirer or same_series or same_ticker or symbol):
                # every close that can stand for the acquirer on the next session; the fewest sessions after the
                # last trade wins, and on a tie the target's own series (one source, one ticker) before the book
                offers = []
                if same_series or same_ticker:
                    offers.append((0, "own series" + (" (same ticker)" if same_ticker and not same_series else ""),
                                   book.close_on(sid, last_day, after=True)))
                if acquirer:
                    quote = acquirer_close(book, names, acquirer, last_day, target=sid)
                    offers.append((1, f"{quote['via']} series (same ticker)" if quote and quote.get("via") else "", quote))
                if symbol:
                    if symbol not in charts:
                        charts[symbol] = acquirer_chart(symbol)
                    offers.append((2, f"yahoo chart {symbol}", chart_close_after(charts[symbol], last_day)))
                offers = [(sessions_after(last_day, q["date"]), rank, label, q) for rank, label, q in offers if q is not None]
                quote = None
                if offers:
                    gap, _, label, quote = min(offers, key=lambda o: (o[0], o[1]))
                    level["acquirer_gap_sessions"] = gap
                    if gap > ACQUIRER_HOLD_SESSIONS:
                        notes.append(f"acquirer's first vendor close after the last trade is {quote['date']:%Y-%m-%d} "
                                     f"({gap} sessions later), too late")
                        quote = None
                    else:
                        if label:
                            out["acquirer_match"] = (out["acquirer_match"] + f"; {label}").strip("; ")
                        if gap > ACQUIRER_QUOTE_SESSIONS:
                            holds.append(f"the acquirer's first vendor close is {gap} XNAS sessions after the last trade")
                if quote is not None and decided.get("value_rule") == "election":
                    value = float(decided["shares"]) * quote["close"]
                    basis_note = "election: stock alternative at the acquirer_vendor_close (local only)"
                    notes.append(f"holders elected ${decided['cash']:g} cash or {decided['shares']:g} shares (prorated); "
                                 "valued at the stock alternative")
                elif quote is not None:
                    value = (decided["cash"] or 0.0) + float(decided["shares"]) * quote["close"]
                    basis_note = "acquirer_vendor_close (local only)"
                    for extra_shares, extra_sid in decided.get("extra") or []:  # further securities in the package
                        extra = acquirer_close(book, names, extra_sid, last_day, target=sid)
                        extra_gap = sessions_after(last_day, extra["date"]) if extra is not None else None
                        if extra is None or extra_gap > ACQUIRER_HOLD_SESSIONS:
                            notes.append(f"no vendor close for {extra_sid} after the last trade")
                            value = None
                            break
                        if extra_gap > ACQUIRER_QUOTE_SESSIONS:
                            holds.append(f"the {extra_sid} close is {extra_gap} XNAS sessions after the last trade")
                        value += float(extra_shares) * extra["close"]
                        level[f"extra_{extra_sid}_close"] = extra["close"]
                        if extra.get("via"):
                            notes.append(f"{extra_sid} priced from the {extra['via']} series (same ticker)")
                if quote is not None:
                    out["acquirer_price_date"] = quote["date"].strftime("%Y-%m-%d")
                    level.update({"acquirer_close": quote["close"], "acquirer_src": quote["src"],
                                  "acquirer_date": out["acquirer_price_date"], "value": value})
            if how in ("reviewed", "successor_link") and not (REVIEWED.get(sid) or {}).get("acq_name"):
                # the master's name on the conversion day, not the phrase the reader caught ('GNOG RSUs')
                out["acquirer_name"] = names.name_of(acquirer, out["acquirer_price_date"]
                                                     or _shift(trade.get("last_date") or row["end_date"], 1))
            if value is None and decided.get("value_rule") == "election" and decided["cash"] is not None:
                notes.append(f"holders elected ${decided['cash']:g} cash or {decided['shares']:g} shares (prorated); "
                             "the stock alternative needs the acquirer's price (the cash one is not used: they can differ)")
            if value is None and _prior_value(prior) is not None:
                if EXISTING_PLACEHOLDER.search(prior.get("note") or ""):
                    notes.append(f"the {prior['existing_file']} row is a placeholder at the last close, not a value: "
                                 f"{strip_levels(prior.get('note') or '')}")
                else:
                    # the existing file prices the stock leg at the acquirer close of the target's last day (its
                    # convention, not this step's next session); the level stays local, the return is committed
                    value, basis_note = _prior_value(prior), "existing_row_value_stock_leg (local only)"
                    level["existing_value"] = value
                    notes.append(f"value from {prior['existing_file']} (stock leg priced there at a vendor close): "
                                 f"{strip_levels(prior.get('note') or '')}".strip().rstrip(":"))
            if value is None:
                status = "needs_acquirer_price" if price_status == "ok" else price_status
                notes.append(f"acquirer price not available ({how or 'acquirer not identified'})")
        elif kind == "bankruptcy_otc":
            if trade["status"] == "ok":
                # the old shares' OTC tail: up to the relist junction (their rows only), else the usual 30 days
                within = max(30, (pd.Timestamp(first_new) - pd.Timestamp(trade["last_date"])).days) if first_new else 30
                quote = otc_close(view, sid, trade["last_date"], within_days=within)
                if quote is not None:
                    value, basis_note = quote["close"], "otc_vendor_close (local only)"
                    level.update({"otc_close": quote["close"], "otc_date": quote["date"], "otc_src": quote["src"]})
                    notes.append(f"first OTC vendor close on {quote['date']} (the level is local only)")
            if value is None and first_new and decided.get("value_rule") == "plan_new_shares" \
                    and decided.get("shares") is not None and trade["status"] == "ok":
                # no OTC close for the old shares: the plan of reorganization's new shares per old share, at the new
                # shares' first vendor close (the same security_id, from the relist junction on; WW 2025-06-27)
                quote = book.close_on(sid, pd.Timestamp(previous_session(first_new)), after=True)
                if quote is not None:
                    value = (decided["cash"] or 0.0) + float(decided["shares"]) * quote["close"]
                    basis_note = "plan_new_shares_at_first_new_share_close (local only)"
                    out["acquirer_price_date"] = quote["date"].strftime("%Y-%m-%d")
                    out["acquirer_name"] = "the reorganized company's new common stock (same security_id)"
                    gap = sessions_after(trade["last_date"], quote["date"])
                    level.update({"plan_new_share_close": quote["close"], "plan_new_share_date": out["acquirer_price_date"],
                                  "plan_new_share_src": quote["src"], "plan_gap_sessions": gap})
                    notes.append(f"no OTC vendor close for the old shares: valued by the plan of reorganization at "
                                 f"{float(decided['shares']):g} new share per old share, at the new shares' first vendor "
                                 f"close on {out['acquirer_price_date']} ({gap} XNAS sessions after the last Nasdaq trade; "
                                 "the level is local only)")
            if value is None and _prior_value(prior) is not None:
                value, basis_note = _prior_value(prior), "existing_row_consideration"
                out["consideration_per_share"] = _fmt(value)
                out["source_url"] = prior["source_url"]
                notes.append(f"no OTC vendor price; value from {prior['existing_file']} (sourced there: equity cancelled "
                             "or a later cash-out), booked on the session after the last Nasdaq trade")
            if value is None and first_new and price_status == "pending_price":
                # a relist junction whose old shares have no vendor rows yet (CORZ: the CORZQ file is a month-2
                # fetch): the plan gave the old holders value, so the D5 default (-100%) is not the answer
                status = "pending_price"
                notes.append("no vendor rows for the old shares yet; once they arrive the OTC tail values them (the "
                             "owner's D5 rule is not the answer: see the reviewed note on what the plan gave the old "
                             "shares)")
            elif value is None and first_new:
                # the old shares at a relist junction: the plan of reorganization says what they received (an OTC tail,
                # new shares per old share, or nothing), so the owner's D5 rule never applies to them
                status = "needs_review"
                holds.append("relist junction without an OTC vendor close or a reviewed plan valuation: value the old "
                             "shares from the plan of reorganization (REVIEWED); not a D5 case")
                notes.append("no OTC vendor price and no reviewed plan valuation for the old shares")
            elif value is None:
                status = "awaiting_d5"
                notes.append("no OTC vendor price; the owner's D5 rule applies")
        # ---- a special dividend paid to holders at the closing (the last close still carries it)
        special = decided.get("special_dividend")
        if special is not None:
            out["special_dividend_cash"] = _fmt(float(special))
            out["special_dividend_record_date"] = decided.get("special_dividend_record") or ""
            if value is not None and basis_note.startswith("existing_row"):
                holds.append("the special dividend is part of the value, but whether the existing file's value "
                             "includes it is not known")
            elif value is not None:
                value += float(special)
                basis_note += " + special_dividend_at_closing"
                if out["consideration_per_share"]:
                    out["consideration_per_share"] = _fmt(value)
                notes.append(f"includes the ${float(special):g} special dividend paid to holders at the closing; the "
                             "canonical series must not book it on an earlier date")
        elif ev.get("special_dividend") and not decided.get("special_dividend_before_last_trade") \
                and kind in ("cash_merger", "stock_merger", "mixed", "liquidation"):
            holds.append("the closing 8-K names a special dividend; whether holders at the closing receive it on top "
                         "of the terms is not reviewed")
        out["consideration_value_basis"] = basis_note
        if basis_note.startswith("existing_row") and prior:
            out["verified_at"] = pd.Timestamp(prior["verified_at"]).strftime("%Y-%m-%d")
        # ---- the guard: an election, a proration or a CVR in the consideration, or a value more than GUARD_RETURN
        # from the last close, holds the row unless REVIEWED approves it with a source url (a data check on this
        # one security's value, not a statistic)
        review = REVIEWED.get(sid) or {}
        approved = bool(review.get("approved") and review.get("url")) and not basis_note.startswith("existing_row")
        guard = [] if approved or kind in ("exchange_move", "unknown") else guard_reasons(decided, out, ev)
        if approved and value is not None:
            notes.append(f"approved by review: {review['approved']}")
        if value is not None and value == 0.0 and price_status != "ok" and approved and decided.get("limit") \
                and kind == "bankruptcy_otc" and not holds:
            # the old shares were cancelled with nothing (a reviewed plan of reorganization: OPI): the return is
            # -100% whatever the last Nasdaq close was, so no vendor close is needed; the last session is the
            # reviewed one (its stated suspension), not a vendor row
            out["terminal_return"] = _fmt(-1.0)
            out["last_price_date"] = decided["limit"]
            status = "computed"
            notes.append("no vendor close on the last Nasdaq session; the holders received nothing, so the return is "
                         "-100% whatever that close was (last_price_date is the reviewed last Nasdaq session)")
            level.update({"terminal_value": 0.0, "terminal_return_checked": -1.0, "no_close_needed": True})
        elif value is not None:
            if price_status == "ok":
                ratio = value / trade["close"] - 1.0
                level["terminal_value"], level["terminal_return_checked"] = value, ratio
                # bankruptcy_otc rows too: a large drop is expected there, but the OTC close it rests on
                # must be checked by hand before the value is committed
                if not approved and abs(ratio) > GUARD_RETURN:
                    guard.append(f"value / last close - 1 is beyond +/-{GUARD_RETURN:.0%}; terms or prices to be checked")
                holds += guard
                if holds:
                    status = "needs_review"
                    notes.extend(holds)
                    notes.append("the figure is kept in CACHE/terminal/prices_used.csv")
                else:
                    out["terminal_return"] = _fmt(ratio)
                    status = "computed"
            else:
                status = price_status
        elif not status:
            status = price_status if price_status != "ok" else "unknown"
        later = []  # the reasons that stand on a row not yet priced
        if status != "needs_review":
            later = ["to check once priced: " + h for h in dict.fromkeys([decided.get("hold")] + holds + guard) if h]
            notes.extend(later)
        symbol = ACQUIRER_SYMBOLS.get(sid, "")
        if symbol and symbol not in charts:
            charts[symbol] = acquirer_chart(symbol)
        if status == "needs_acquirer_price" and symbol and charts[symbol].empty:
            later.append(f"the {symbol} chart (ACQUIRER_SYMBOLS) is not cached; --yahoo-acquirers asks at most "
                         f"YAHOO_MAX={YAHOO_MAX} charts in all")
            notes.append(later[-1])
        level["review_reasons"] = "; ".join(holds if status == "needs_review" else later)
        if level["review_reasons"]:
            cik = _cik_int(row.get("cik"))
            closing = [index_url(cik, a) for a in str(ev.get("closing_accessions") or "").split() if a and cik is not None]
            level["review_documents"] = " ".join(dict.fromkeys(
                u for u in [out["source_url"], review.get("url"), REVIEWED_SOURCES.get(sid), *closing,
                            out["existing_source_url"]] if u))
        if status == "needs_review":
            # the value of a held row stays local: only the SEC terms (cash, shares) are committed
            level["consideration_per_share_held"] = out["consideration_per_share"]
            out["consideration_per_share"] = ""
        if decided.get("hold_last_session") and out["last_price_date"]:
            # the hold is about the last session itself: it is not dated in the committed file
            level["last_price_date_held"], level["acquirer_price_date_held"] = out["last_price_date"], out["acquirer_price_date"]
            out["last_price_date"], out["acquirer_price_date"] = "", ""
            notes.append("the last session is not dated until the hold is resolved (the candidate is in "
                         "CACHE/terminal/prices_used.csv)")
        link = SUCCESSOR_LINKS.get(sid) or {}
        if link.get("continues") and kind == "stock_merger":
            # owner convention 2026-10-02 (CRSP keeps one PERMNO): a 1:1 holding-company reorganisation continues the
            # security; the canonical series runs on in the successor, whose first return is measured from this last
            # close (reconcile SUCCESSOR_LINKS), so a terminal return here would count that day twice
            status, out["terminal_return"], out["consideration_per_share"] = "no_terminal_return", "", ""
            out["continued_as"] = link["successor"]
            # the reconcile link records the one-for-one conversion into the successor: the row says so (the
            # universe step reads stock_merger / reorganization / 1 share / successor as a continuing link)
            out.update({"event_subtype": "reorganization", "consideration_shares": "1", "consideration_cash": "",
                        "acquirer_security_id": link["successor"]})
            level["terminal_value"], level["review_reasons"] = None, ""
            if out["last_price_date"] != link["last_session"]:
                if out["last_price_date"]:
                    notes.append(f"this step's last session ({out['last_price_date']}) differs from the reconcile cut "
                                 f"({link['last_session']}): the cut is used")
                out["last_price_date"] = link["last_session"]
            notes.append(f"continued as {link['successor']}: a one-for-one reorganisation continues the same security "
                         "(owner convention 2026-10-02, as CRSP keeps one PERMNO); the canonical series runs on in the "
                         f"successor, whose first return is measured from this security's last close on "
                         f"{link['last_session']}; no terminal return is booked (the holds and the guard above do not "
                         "apply)")
        if prior and out["terminal_return"]:
            out["existing_return_diff"] = _fmt(float(out["terminal_return"]) - float(prior["terminal_return"]))
        out["status"] = status
        out["status_note"] = "; ".join(n for n in notes if n)
        rows.append(out)
        used.append(level)
    frame = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    return frame, pd.DataFrame(used)


def old_shares_valuation(sid: str, junction: dict, book: "PriceBook") -> tuple[dict, dict]:
    """The ``relist_old_shares_*`` columns: the old shares at a relist junction of a security whose row values a later
    end (REVIEWED_OLD_SHARES; Frontier). Their rows only (the book cut before the new shares' first session), their
    last Nasdaq session (the reviewed limit), then a reviewed value of nothing (-100%, no close needed), else the first
    OTC vendor close (held for review), else held: the plan decides, never the D5 rule. Dates, returns and SEC facts
    only; the levels go to the second return value (local)."""
    review = REVIEWED_OLD_SHARES.get(sid) or {}
    first_new = junction["first_new_session"]
    view = book.before(sid, first_new)
    limit = review.get("limit") or junction.get("old_nasdaq_last_session") or previous_session(first_new)
    trade = last_trade(view, sid, limit, "")
    approved = bool(review.get("approved") and review.get("url"))
    cols = {"relist_old_shares_last_price_date": "", "relist_old_shares_terminal_return": "",
            "relist_old_shares_status": "", "relist_old_shares_source_url": review.get("url") or junction.get("url", ""),
            "relist_old_shares_note": ""}
    level = {"old_limit": limit, "old_trade_status": trade.get("status", ""), "old_last_date": trade.get("last_date", "")}
    notes = [("reviewed: " + review["note"]) if review.get("note") else ""]
    value = review.get("value")
    if value is not None and float(value) == 0.0 and approved:
        cols["relist_old_shares_terminal_return"] = _fmt(-1.0)
        cols["relist_old_shares_last_price_date"] = limit
        cols["relist_old_shares_status"] = "computed"
        notes.append(f"approved by review: {review['approved']}")
        notes.append("the holders received nothing, so the return is -100% whatever the last Nasdaq close was")
    elif trade.get("status") == "ok":
        cols["relist_old_shares_last_price_date"] = trade["last_date"]
        within = max(30, (pd.Timestamp(first_new) - pd.Timestamp(trade["last_date"])).days)
        quote = otc_close(view, sid, trade["last_date"], within_days=within)
        cols["relist_old_shares_status"] = "needs_review"
        if quote is not None:
            level.update({"old_otc_close": quote["close"], "old_otc_date": quote["date"], "old_last_close": trade["close"]})
            notes.append(f"first OTC vendor close on {quote['date']} (the level is local only); held for a hand check")
        else:
            notes.append("no OTC vendor close: value the old shares from the plan of reorganization (REVIEWED_OLD_SHARES); "
                         "not a D5 case")
    else:
        cols["relist_old_shares_status"] = "needs_review"
        notes.append("no vendor close on the old shares' last Nasdaq session and no reviewed value; not a D5 case")
    cols["relist_old_shares_note"] = "; ".join(n for n in notes if n)
    return cols, level


# ------------------------------------------------------------------ main


def forbid_network() -> None:
    """``--offline``: any socket connection in this process is refused, so the build reads the caches only."""
    import socket

    def refuse(*_args, **_kwargs):
        raise RuntimeError("reversal_data_terminal --offline: a network connection was attempted")

    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.create_connection = refuse


def configure_paths(out_dir: Path | None) -> None:
    """Send the outputs under ``out_dir`` (``inputs/terminal_returns_2012_2026.csv`` and ``terminal/`` for the
    CACHE/terminal files this step writes) instead of INPUTS and CACHE/terminal. Everything read (the documents,
    the charts, the price files, the provenance index) is still read from its usual place."""
    global OUT, OUTPUT, RECONCILE_HANDOFF, MANUAL_REVIEW_QUEUE, EXCHANGE_MOVES, SERIES_ENDS_OUT, READ_ONLY_SHARED
    if out_dir is None:
        return
    root = Path(out_dir).resolve()
    OUT, OUTPUT = root / "terminal", root / "inputs" / "terminal_returns_2012_2026.csv"
    RECONCILE_HANDOFF, MANUAL_REVIEW_QUEUE = OUT / "for_reconcile_owner.csv", OUT / "manual_review_queue.csv"
    EXCHANGE_MOVES, SERIES_ENDS_OUT = root / "inputs" / "exchange_moves.csv", OUT / "series_ends.csv"
    READ_ONLY_SHARED = True  # a scratch build writes nothing outside out_dir


def configure_reconcile(root: Path | None) -> None:
    """Read the reconcile step's outputs (``series_ends.csv``, the canonical price files) from a reconcile
    ``--out-dir`` build at ``root`` (``root/reconcile``, ``root/prices``) instead of CACHE; without --out-dir the
    refreshed series_ends.csv is written back there."""
    global RECONCILE_DIR, CANONICAL_PRICES, SERIES_ENDS_OUT
    if root is None:
        return
    root = Path(root).resolve()
    default_out = SERIES_ENDS_OUT == RECONCILE_DIR / "series_ends.csv"
    RECONCILE_DIR, CANONICAL_PRICES = root / "reconcile", root / "prices"
    if default_out:
        SERIES_ENDS_OUT = RECONCILE_DIR / "series_ends.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--offline", action="store_true",
                        help="no requests (any socket connection is refused): build from the cache only")
    parser.add_argument("--out-dir", default="",
                        help="write terminal_returns_2012_2026.csv under OUT_DIR/inputs and the CACHE/terminal outputs "
                             "under OUT_DIR/terminal (a scratch rebuild); the inputs are read from their usual places")
    parser.add_argument("--scope-only", action="store_true", help="write CACHE/terminal/scope.csv and stop")
    parser.add_argument("--limit", type=int, default=0, help="at most this many documents per fetch stage (a trial)")
    parser.add_argument("--no-fetch", action="store_true", help="skip the SEC fetch stages (same as --offline here)")
    parser.add_argument("--yahoo-acquirers", action="store_true",
                        help=f"fetch the Yahoo charts of ACQUIRER_SYMBOLS not yet cached (at most {YAHOO_MAX} in all)")
    parser.add_argument("--review-verdicts", default="",
                        help=f"the merged hand-review verdicts to fold into REVIEWED (default {REVIEW_VERDICTS})")
    parser.add_argument("--no-review-verdicts", action="store_true", help="use the code's REVIEWED only")
    parser.add_argument("--reconcile-out", default="",
                        help="read series_ends.csv and the canonical price files from this reconcile --out-dir build "
                             "(ROOT/reconcile, ROOT/prices) instead of CACHE")
    args = parser.parse_args(argv)
    if args.offline:
        forbid_network()
    configure_paths(Path(args.out_dir) if args.out_dir else None)
    configure_reconcile(Path(args.reconcile_out) if args.reconcile_out else None)
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"outputs: {OUTPUT} and {OUT}; " + ("offline (network refused)" if args.offline else "SEC fetch stages on"))
    global REVIEW_VERDICTS_APPLIED
    REVIEW_VERDICTS_APPLIED = {"skipped": True} if args.no_review_verdicts else \
        load_review_verdicts(Path(args.review_verdicts) if args.review_verdicts else None)
    log(f"hand-review verdicts: {REVIEW_VERDICTS_APPLIED}")
    scope = terminal_candidates()
    log(f"scope: {len(scope)} securities end by {WINDOW_END}")
    common.atomic_write(OUT / "scope.csv", scope.to_csv(index=False).encode())
    if args.scope_only:
        return 0
    filings = scope_filings(scope, offline=True)
    common.atomic_write(OUT / "filings.csv", filings.to_csv(index=False).encode())
    log(f"filings near the ends: {len(filings)} ({filings['kind'].value_counts().to_dict()})")
    if not args.offline and not args.no_fetch:
        try:
            fetch_all(scope, filings, limit=args.limit)
        except StopFetching as exc:
            log(f"stopped: {exc}")
    if args.yahoo_acquirers and not args.offline:
        log(f"acquirer charts: {fetch_acquirer_charts()}")
    build(scope, filings)
    return 0


if common.DATA_VERSION == "v2":
    # version 2 (2026-10-05): two D5 rows whose SEC filings state a fixed per-share consideration (the SEC review in
    # inputs_v2/v2_d5_sec_evidence.csv); booked as the plan's cash merger rule (4.5). Other "recovery" rows (ranges,
    # CVRs, pro rata residuals) have no fixed value and stay with the D5 rule.
    REVIEWED.update({
        "1354513": {"type": "cash_merger", "sub": "merger", "cash": 2.0503,
                    "url": _SEC + "1354513/000110465916122229/a16-11800_18k.htm",
                    "approved": "v2 SEC review: cash-out merger completed 2016-05-20, 2.0503 USD per share",
                    "note": "ChinaCache (CTCM): merger closing 8-K; each share cancelled for 2.0503 USD cash"},
        "1596946": {"type": "cash_merger", "sub": "merger", "cash": 0.01,
                    "url": _SEC + "1596946/000143774923017339/qtntq20230605_8k.htm",
                    "approved": "v2 SEC review: Jersey merger paid 0.01 USD cash per ordinary share",
                    "note": "Quotient (QTNT): merger closing 8-K 2023-06-14; ordinary shares cancelled for 0.01 USD each"},
    })
    # the code's own table, which load_review_verdicts restores before merging the hand-review verdicts
    CODE_REVIEWED.update({sid: dict(REVIEWED[sid]) for sid in ("1354513", "1596946")})
D5_EVIDENCE = common.INPUTS / "v2_d5_sec_evidence.csv"  # version 2: what the SEC filings say the old equity got


def attach_d5_evidence(frame: pd.DataFrame) -> pd.DataFrame:
    """Version 2 (plan section 0, 2026-10-05): the hand-read SEC evidence on the old common stock of the D5 names
    (``inputs_v2/v2_d5_sec_evidence.csv``) beside each row. It decides nothing by itself: an OTC close found by the
    price rules above still values the row (plan 4.5), and with none the owner's D5 rule (-55%) stays; a plan that
    cancels the old equity without a distribution is the evidence for the -100% stress test."""
    for c in ("sec_equity_decision", "sec_equity_accession", "sec_equity_url"):
        frame[c] = ""
    if not D5_EVIDENCE.exists():
        return frame
    ev = read_csv_text(D5_EVIDENCE).set_index("security_id")
    hit = frame["security_id"].isin(ev.index)
    frame.loc[hit, "sec_equity_decision"] = frame.loc[hit, "security_id"].map(ev["decision"])
    frame.loc[hit, "sec_equity_accession"] = frame.loc[hit, "security_id"].map(ev["accession"])
    frame.loc[hit, "sec_equity_url"] = frame.loc[hit, "security_id"].map(ev["doc_url"])
    return frame


def build(scope: pd.DataFrame, filings: pd.DataFrame) -> pd.DataFrame:
    """Read the cached documents and prices, decide every row, write the outputs."""
    evidence, leads = gather_evidence(scope, filings)
    log(f"evidence: {len(evidence)} securities, {len(leads)} consideration leads")
    common.atomic_write(OUT / "evidence.csv", evidence.to_csv(index=False).encode())
    common.atomic_write(OUT / "leads.csv.gz", gzip.compress(leads.to_csv(index=False).encode(), mtime=0))
    master = read_csv_text(MASTER)
    book = PriceBook()
    log("prices loaded")
    existing = load_existing()
    matched = match_existing(existing, scope, read_csv_text(INTERVALS))
    log(f"existing terminal rows: {len(existing)}, {len(matched)} matched to scoped securities")
    verified_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    frame, used = build_rows(scope, evidence, book, matched, NameIndex(master), read_csv_text(CANDIDATES), verified_at)
    if common.DATA_VERSION == "v2":
        frame = attach_d5_evidence(frame)
    common.atomic_write(OUTPUT, frame.to_csv(index=False).encode())
    common.atomic_write(OUT / "prices_used.csv", used.to_csv(index=False).encode())
    handoff = reconcile_handoff(frame, used)
    common.atomic_write(RECONCILE_HANDOFF, handoff.to_csv(index=False).encode())
    ends = read_csv_text(RECONCILE_DIR / "series_ends.csv") if (RECONCILE_DIR / "series_ends.csv").exists() else None
    moves = exchange_moves(frame, ends, master, read_csv_text(INTERVALS))
    common.atomic_write(EXCHANGE_MOVES, moves.to_csv(index=False).encode())
    log(f"wrote {EXCHANGE_MOVES}: {len(moves)} rows ({moves['source'].value_counts().to_dict() if len(moves) else {}})")
    refreshed = refresh_series_ends(ends, frame)
    if refreshed is not None:
        common.atomic_write(SERIES_ENDS_OUT, refreshed.to_csv(index=False).encode())
        log(f"refreshed the terminal_2012_2026 column of {SERIES_ENDS_OUT}")
    summary = summarize(frame, existing, matched, used)
    summary["for_reconcile_owner"] = {k: sorted(g["security_id"]) for k, g in handoff.groupby("kind")} if len(handoff) else {}
    summary["computed_after_successor_date"] = successor_overruns(frame, scope, used)
    summary["exchange_moves"] = {
        "rows": int(len(moves)), "file": str(EXCHANGE_MOVES), "scope": EXCHANGE_MOVES_SCOPE,
        "by_source": {k: int(v) for k, v in moves["source"].value_counts().items()} if len(moves) else {},
        "onto_nasdaq": {"rows": int(moves["to_exchange"].eq("NASDAQ").sum()) if len(moves) else 0,
                        "last_date": str(moves.loc[moves["to_exchange"].eq("NASDAQ"), "date"].max()) if len(moves) else ""},
        "blank_fields": {c: int(moves[c].eq("").sum()) for c in ("security_id", "from_exchange", "to_exchange",
                                                                 "source_url")} if len(moves) else {}}
    summary["review_verdicts"] = REVIEW_VERDICTS_APPLIED
    summary["continued_by_successor"] = sorted(frame.loc[frame["continued_as"].ne(""), "security_id"])
    summary["relist_old_shares"] = [
        {"security_id": r.security_id, "status": r.relist_old_shares_status,
         "last_price_date": r.relist_old_shares_last_price_date}
        for r in frame[frame["relist_old_shares_status"].ne("")].itertuples(index=False)]
    summary["series_ends_refreshed"] = str(SERIES_ENDS_OUT) if refreshed is not None else ""
    queue = manual_review_queue(frame, used)
    common.atomic_write(MANUAL_REVIEW_QUEUE, queue.to_csv(index=False).encode())
    summary["manual_review_queue"] = {"rows": int(len(queue)),
                                      "needs_review": int(queue["reason"].str.startswith("needs_review").sum()),
                                      "not_yet_priced": int((~queue["reason"].str.startswith("needs_review")).sum())}
    log(f"manual review queue: {summary['manual_review_queue']} -> {MANUAL_REVIEW_QUEUE}")
    common.atomic_write(OUT / "terminal_summary.json", (json.dumps(summary, indent=1, default=str) + "\n").encode())
    log(f"wrote {OUTPUT}: {len(frame)} rows")
    log("by type: " + json.dumps(summary["rows_by_terminal_type"]))
    log("by status: " + json.dumps(summary["rows_by_status"]))
    for item in summary["relist_junction_rows"]:
        log(f"relist junction {item['security_id']} {item['ticker']}: {item['status']} last_price_date "
            f"{item['last_price_date'] or '-'} ({item['value_basis'] or 'no value'})")
    return frame


CANONICAL_PRICES = common.CACHE / "prices"
RECONCILE_DIR = common.CACHE / "reconcile"  # read: series_ends.csv (its terminal_2012_2026 column is refreshed)
SERIES_ENDS_OUT = RECONCILE_DIR / "series_ends.csv"
EXCHANGE_MOVES = INPUTS / "exchange_moves.csv"
HOLDOUT_OVERRIDES = Path("output/research_only/holdout_2011_2019/inputs/nasdaq_listing_overrides.csv")
EXCHANGE_MOVES_SCOPE = (
    "moves off Nasdaq, 2012-2026: this step's exchange_move rows (source terminal) and the reconcile step's "
    "series_ends.csv exchange moves this step has no row for (source series_ends); moves onto Nasdaq: only the "
    "holdout's nasdaq_listing_overrides.csv (source holdout_overrides), whose last move is dated 2019-12-27, so no "
    "move onto Nasdaq after 2019 is listed (no source of this step names them; a security that came from another "
    "exchange later simply starts its Nasdaq interval in ticker_intervals.csv). In the holdout rows from_exchange "
    "is blank unless the row's text names the old exchange (NYSE), source_url is blank when the row rests on a "
    "snapshot bracket only, and security_id is blank when no Nasdaq interval of the ticker starts or ends within "
    "400 days of the move (WTW, OPK, PARA)")
EXCHANGE_MOVE_COLUMNS = ["security_id", "ticker", "date", "from_exchange", "to_exchange", "source_url",
                         "last_nasdaq_session", "date_basis", "source", "terminal_status", "note"]


def _next_session(day: str) -> str:
    sessions = xnas_sessions()
    k = int(sessions.searchsorted(pd.Timestamp(day), side="right"))
    return str(sessions[k].date()) if k < len(sessions) else ""


def _holder(intervals: pd.DataFrame, ticker: str, day: str, max_days: int = 400) -> str:
    """The security whose Nasdaq interval of ``ticker`` starts (or ends) nearest ``day`` within ``max_days``."""
    part = intervals[(intervals["ticker"] == ticker) & intervals["exchange"].str.upper().eq("NASDAQ")]
    best, gap = "", max_days + 1
    for r in part.itertuples(index=False):
        for edge in (r.start, r.end):
            if edge:
                d = abs((pd.Timestamp(edge) - pd.Timestamp(day)).days)
                if d < gap:
                    best, gap = r.security_id, d
    return best


def exchange_moves(frame: pd.DataFrame, ends: pd.DataFrame | None, master: pd.DataFrame,
                   intervals: pd.DataFrame) -> pd.DataFrame:
    """``INPUTS/exchange_moves.csv`` (plan 1.1: security_id, ticker, date, from_exchange, to_exchange, source_url):
    every listing move, ``date`` the first session on the new exchange. From this step's ``exchange_move`` rows
    (``date`` the stated first day on the new exchange, else the session after the last Nasdaq session, else the
    transfer Form 25's end date; ``source`` terminal), the reconcile step's series_ends.csv ``exchange_move`` rows this
    step has no row for (the master's transfer date and transfer Form 25, the destination from SEC's current exchange
    list, which may postdate the move; ``source`` series_ends), and the holdout's nasdaq_listing_overrides.csv (moves
    onto Nasdaq dated by ``nasdaq_from``, moves off it by ``nasdaq_until``; the 2099 placeholders, never listed in the
    snapshots, are not moves; ``source`` holdout_overrides). SEC facts and dates only."""
    rows, seen = [], set()
    for r in frame[frame["terminal_type"].eq("exchange_move")].itertuples(index=False):
        if r.destination_start_date:
            date, basis = r.destination_start_date, "first day on the new exchange stated in the 8-K or press release"
        elif r.last_price_date:
            date, basis = _next_session(r.last_price_date), "the session after the last Nasdaq session"
        else:
            date, basis = r.end_date, "the transfer Form 25's end date (no stated first day, no vendor-dated last session)"
        rows.append({"security_id": r.security_id, "ticker": r.ticker, "date": date, "from_exchange": "NASDAQ",
                     "to_exchange": r.destination_exchange, "source_url": r.source_url,
                     "last_nasdaq_session": r.last_price_date, "date_basis": basis, "source": "terminal",
                     "terminal_status": r.status, "note": ""})
        seen.add(r.security_id)
    info = master.set_index("security_id") if len(master) else pd.DataFrame()
    if ends is not None and len(ends):
        for r in ends[ends["likely_cause"].eq("exchange_move")].itertuples(index=False):
            if r.security_id in seen or r.security_id not in info.index:
                continue
            m = info.loc[r.security_id]
            accession, cik = str(m.get("transfer_form25_accession", "") or ""), str(m.get("cik", "") or "")
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(float(cik))}/{accession.replace('-', '')}/"
                   f"{accession}-index.htm") if accession and cik else ""
            current = [e for e in str(m.get("exchanges_sec_current", "") or "").split() if e and e != "Nasdaq"]
            rows.append({"security_id": r.security_id, "ticker": r.ticker_last, "date": r.transfer_date,
                         "from_exchange": "NASDAQ", "to_exchange": current[0] if current else "", "source_url": url,
                         "last_nasdaq_session": "", "date_basis": "the master's transfer date (the transfer Form 25)",
                         "source": "series_ends", "terminal_status": "",
                         "note": "destination from SEC's current exchange list (may postdate the move); "
                                 f"the series' last row is {r.last_date}"})
            seen.add(r.security_id)
    if HOLDOUT_OVERRIDES.exists():
        overrides = read_csv_text(HOLDOUT_OVERRIDES)
        for r in overrides.itertuples(index=False):
            for day, direction in ((r.nasdaq_from, "onto"), (r.nasdaq_until, "off")):
                if not day or day >= "2099" or not ("2011-06-01" <= day <= WINDOW_END):
                    continue
                sid = _holder(intervals, r.ticker, day)
                if direction == "off" and sid in seen:
                    continue  # this step's own row for the same move
                text = f"{r.basis} {r.evidence}"
                other = "NYSE" if re.search(r"\bNYSE\b", text) else ""
                url = r.evidence if str(r.evidence).startswith("http") else ""
                gaps = [g for g, missing in (("the old exchange is not named in the holdout row", not other),
                                             ("no document: a snapshot bracket", not url),
                                             ("no Nasdaq interval of the ticker within 400 days", not sid)) if missing]
                rows.append({"security_id": sid, "ticker": r.ticker, "date": day,
                             "from_exchange": "NASDAQ" if direction == "off" else other,
                             "to_exchange": other if direction == "off" else "NASDAQ",
                             "source_url": url,
                             "last_nasdaq_session": "", "date_basis": f"nasdaq_{'until' if direction == 'off' else 'from'} "
                                                                       "in the holdout overrides",
                             "source": "holdout_overrides", "terminal_status": "",
                             "note": "; ".join([str(r.basis)] + [f"blank: {g}" for g in gaps])})
    out = pd.DataFrame(rows, columns=EXCHANGE_MOVE_COLUMNS)
    return out.sort_values(["date", "security_id", "ticker"], kind="stable").reset_index(drop=True)


def refresh_series_ends(ends: pd.DataFrame | None, frame: pd.DataFrame) -> pd.DataFrame | None:
    """The reconcile step's series_ends.csv with its ``terminal_2012_2026`` column taken from this build (reconcile
    runs first and can only quote the previous terminal table): the old shares at a relist junction whose row values
    a later end show the relist_old_shares_* status."""
    if ends is None or "terminal_2012_2026" not in ends:
        return None
    by_id = frame.set_index("security_id")
    values = []
    for r in ends.itertuples(index=False):
        if r.security_id not in by_id.index:
            values.append("")
            continue
        t = by_id.loc[r.security_id]
        if r.category == "old_shares_at_relist_junction" and t.get("relist_old_shares_status", ""):
            kind = (REVIEWED_OLD_SHARES.get(r.security_id) or {}).get("type", "bankruptcy_otc")
            values.append(f"{kind}/{t['relist_old_shares_status']} (old shares; the row values the later end) "
                          f"last_price_date={t['relist_old_shares_last_price_date'] or '-'}")
        else:
            values.append(f"{t['terminal_type']}/{t['status']} last_price_date={t['last_price_date'] or '-'} "
                          f"end_date={t['end_date'] or '-'}")
    return ends.assign(terminal_2012_2026=values)


RECONCILE_HANDOFF = OUT / "for_reconcile_owner.csv"
HANDOFF_COLUMNS = ["kind", "security_id", "ticker", "last_price_date", "amount", "record_date", "series_booking_date",
                   "limit", "vendor_last_date", "source_url", "action"]


def series_dividend_dates(sid: str, amount: float, before: str, days: int = 45) -> list[str]:
    """Dates on which the canonical series (CACHE/prices, read only) books a cash dividend of ``amount`` in the
    ``days`` before ``before`` (inclusive)."""
    path = CANONICAL_PRICES / f"{sid}.csv"
    if not path.exists() or not before:
        return []
    data = pd.read_csv(path, usecols=["date", "div_cash"])
    cash = pd.to_numeric(data["div_cash"], errors="coerce").fillna(0.0)
    inside = (data["date"] <= before) & (data["date"] >= _shift(before, -days)) & ((cash - amount).abs() < 0.005)
    return sorted(data.loc[inside, "date"].astype(str))


def reconcile_handoff(frame: pd.DataFrame, used: pd.DataFrame) -> pd.DataFrame:
    """Facts the canonical-series (reconcile) owner must act on; this step never edits the series.

    ``special_dividend_in_terminal_value``: the terminal value owns a special dividend paid to holders at the
    closing; a booking of it in the series on or before the last trade (``series_booking_date``) must go.
    ``exchange_move_vendor_gap``: no vendor session lies within a week before the transfer limit, so the last
    Nasdaq session is not dated here (``last_price_date`` blank)."""
    rows = []
    for r in frame[frame["special_dividend_cash"].ne("")].itertuples(index=False):
        booked = series_dividend_dates(r.security_id, float(r.special_dividend_cash), r.last_price_date)
        rows.append({"kind": "special_dividend_in_terminal_value", "security_id": r.security_id, "ticker": r.ticker,
                     "last_price_date": r.last_price_date, "amount": r.special_dividend_cash,
                     "record_date": r.special_dividend_record_date, "series_booking_date": " ".join(booked),
                     "source_url": r.source_url,
                     "action": ("remove the booking from the series: the terminal value owns the dividend" if booked
                                else "none: the series does not book it (keep it that way)")})
    levels = used.set_index("security_id") if len(used) else pd.DataFrame()
    moves = frame[frame["terminal_type"].eq("exchange_move") & frame["last_price_date"].eq("")]
    for r in moves.itertuples(index=False):
        level = levels.loc[r.security_id] if r.security_id in levels.index else {}
        vendor_last = level.get("vendor_last_date", "")
        if level.get("status", "") != "vendor_short" or not vendor_last or vendor_last == WIKI_END:
            continue  # no vendor rows at all, or the series ends with the WIKI table (a vendor file to come)
        pending = "tiingo" in str(r.status_note).lower()
        rows.append({"kind": "exchange_move_vendor_gap", "security_id": r.security_id, "ticker": r.ticker,
                     "limit": level.get("limit", ""), "vendor_last_date": vendor_last, "source_url": r.source_url,
                     "action": "the vendor series stops weeks before the last Nasdaq session: a gap in the series"
                               + (" (a Tiingo file is still to come: recheck once it has arrived)" if pending else "")})
    return pd.DataFrame(rows, columns=HANDOFF_COLUMNS)


MANUAL_REVIEW_QUEUE = OUT / "manual_review_queue.csv"
QUEUE_COLUMNS = ["security_id", "ticker", "best_rank", "reason", "documents"]


def manual_review_queue(frame: pd.DataFrame, used: pd.DataFrame) -> pd.DataFrame:
    """Every row a person must check before a terminal return can be committed, by best rank: the needs_review
    rows (each hold and guard reason), and the rows not yet priced on which a hold or a guard reason already
    stands (the reason says the status). Facts and document links only, no levels."""
    if not len(used) or "review_reasons" not in used:
        return pd.DataFrame(columns=QUEUE_COLUMNS)
    levels = used.set_index("security_id")
    rows = []
    for r in frame.itertuples(index=False):
        if r.status == "computed" or r.security_id not in levels.index:
            continue
        reasons = levels.at[r.security_id, "review_reasons"]
        reasons = "" if not isinstance(reasons, str) else reasons
        if not reasons:
            continue
        documents = levels.at[r.security_id, "review_documents"] if "review_documents" in levels else ""
        rows.append({"security_id": r.security_id, "ticker": r.ticker, "best_rank": r.best_rank,
                     "reason": f"{r.status}: {reasons}", "documents": documents if isinstance(documents, str) else ""})
    out = pd.DataFrame(rows, columns=QUEUE_COLUMNS)
    order = pd.to_numeric(out["best_rank"], errors="coerce")
    return out.assign(_rank=order).sort_values(["_rank", "security_id"], na_position="last").drop(columns="_rank") \
        .reset_index(drop=True)


def successor_overruns(frame: pd.DataFrame, scope: pd.DataFrame, used: pd.DataFrame | None = None) -> list[str]:
    """Computed stock or mixed rows with a snapshot-dated end whose last trade falls after the master's
    successor date (the successor's sessions under the same ticker), or, with ``used`` (prices_used), whose
    last trade sits at the snapshot-slack limit while the stored series ends earlier (``slack_overrun``: a
    rename the master does not link, QRTEA): should be empty."""
    succ = scope.set_index("security_id")
    levels = used.set_index("security_id") if used is not None and len(used) else pd.DataFrame()
    out = []
    for r in frame[frame["status"].eq("computed") & frame["terminal_type"].isin(["stock_merger", "mixed"])
                   & frame["end_source"].eq("snapshots")].itertuples(index=False):
        day = str(succ["successor_date"].get(r.security_id, "") or "") if "successor_date" in succ else ""
        if day and r.last_price_date > day:
            out.append(r.security_id)
        elif r.security_id in levels.index:
            level = levels.loc[r.security_id]
            trade = {"status": level.get("status", ""), "last_date": level.get("last_date", ""),
                     "stored_last_date": level.get("stored_last_date", "")}
            trade = {k: ("" if not isinstance(v, str) else v) for k, v in trade.items()}
            if slack_overrun(r.end_source, r.terminal_type, str(level.get("limit_basis", "")), str(level.get("limit", "")),
                             trade):
                out.append(r.security_id)
    return sorted(out)


def summarize(frame: pd.DataFrame, existing: pd.DataFrame, matched: dict, used: pd.DataFrame | None = None) -> dict:
    """Counts only (no return is averaged, ranked or otherwise aggregated)."""
    used = pd.DataFrame(columns=["security_id", "limit_basis", "filler_dropped"]) if used is None else used
    filler = pd.to_numeric(used.get("filler_dropped", pd.Series(dtype=float)), errors="coerce").fillna(0)
    rank = pd.to_numeric(frame["best_rank"], errors="coerce")
    unknown = frame[frame["terminal_type"].eq("unknown")].assign(_rank=rank).sort_values("_rank", na_position="last")
    diff = pd.to_numeric(frame["existing_return_diff"], errors="coerce").abs()
    return {
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": int(len(frame)),
        "rows_by_terminal_type": {k: int(v) for k, v in frame["terminal_type"].value_counts().items()},
        "rows_by_status": {k: int(v) for k, v in frame["status"].value_counts().items()},
        "type_by_status": {t: {k: int(v) for k, v in g["status"].value_counts().items()}
                           for t, g in frame.groupby("terminal_type")},
        "ranked_le_300_by_type": {k: int(v) for k, v in frame.loc[rank <= TOP_RANK, "terminal_type"].value_counts().items()},
        "unknown_share": round(float(frame["terminal_type"].eq("unknown").mean()), 4) if len(frame) else 0.0,
        "unknown_by_best_rank": [
            {"security_id": r.security_id, "ticker": r.ticker, "name": r.name, "best_rank": r.best_rank,
             "end_date": r.end_date, "form25_basis": r.form25_basis, "note": r.status_note}
            for r in unknown.itertuples(index=False)],
        "ranked_le_300_by_status": {k: int(v) for k, v in frame.loc[rank <= TOP_RANK, "status"].value_counts().items()},
        "reviewed_by_hand": int(frame["status_note"].str.startswith("reviewed:").sum()),
        "rows_by_value_basis": {k or "none": int(v) for k, v in frame["consideration_value_basis"].value_counts().items()},
        "existing_rows": int(len(existing)), "existing_rows_in_scope": int(len(matched)),
        "existing_return_diff_gt_0.5pct": int((diff > 0.005).sum()),
        "pending_price_rows": int(frame["status"].eq("pending_price").sum()),
        # single-security data checks: ids only, no return values or statistics
        "merger_rows_beyond_5pct_to_check": sorted(
            frame.loc[frame["terminal_type"].isin(["cash_merger", "stock_merger", "mixed"])
                      & (pd.to_numeric(frame["terminal_return"], errors="coerce").abs() > 0.05), "security_id"]),
        "needs_review": sorted(frame.loc[frame["status"].eq("needs_review"), "security_id"]),
        "rows_by_limit_basis": {k: int(v) for k, v in used["limit_basis"].value_counts().items()} if len(used) else {},
        "last_session_stepped_back_over_filler_rows": sorted(used.loc[filler > 0, "security_id"]) if len(used) else [],
        "exchange_moves_with_stated_new_exchange_start": int(
            (frame["terminal_type"].eq("exchange_move") & frame["destination_start_date"].ne("")).sum()),
        # the old shares at each relist junction (RELIST_JUNCTIONS): status and how they were valued, no values
        "relist_junction_rows": [
            {"security_id": r.security_id, "ticker": r.ticker, "first_new_session": r.relist_junction,
             "status": r.status, "last_price_date": r.last_price_date, "value_basis": r.consideration_value_basis}
            for r in frame[frame["relist_junction"].ne("")].itertuples(index=False)] if "relist_junction" in frame else [],
        "returns_aggregated": "none (counts only)",
    }


FETCH_STAGES = (("primary", False), ("index", True), ("exhibits", True), ("agreement_index", True),
                ("agreement", True), ("agreement_exhibits", True))


def fetch_all(scope: pd.DataFrame, filings: pd.DataFrame, limit: int = 0) -> None:
    """Each document stage in turn; after the first, only for securities still lacking terms."""
    sids = None
    for stage, narrow in FETCH_STAGES:
        if narrow:
            evidence, _ = gather_evidence(scope, filings)
            sids = lacking_terms(evidence, scope)
            log(f"stage {stage}: {len(sids)} securities still lack terms")
        jobs = documents_to_fetch(filings, stage, sids if narrow else None)
        if limit:
            jobs = jobs[:limit]
        fetch_documents(jobs)
        if _STOP["set"]:
            raise StopFetching("SEC refused a request")


if __name__ == "__main__":
    sys.exit(main())
