"""Plan step 4: a first security master and ticker intervals, from SEC submissions and repo evidence.

Data only. Nothing here computes signals or returns, and price files are read
for their tickers and date ranges only, never for levels.

Which CIKs:
- every subject CIK in ``form25_nasdaq_2012_2026.csv`` (step 3);
- every ticker of a stored price file (``cleaned_stocks_data/price``);
- every common-equity ticker of the repo Nasdaq snapshots
  (``stocks_list_dir/nasdaq/snapshots``: nasdaq_listed_* and nasdaq_300M_*);
- (hook) tickers of the listings builder's Wayback snapshots, read from
  ``CACHE/listings/symdir`` and ``CACHE/listings/companylist`` when they exist.

Tickers become CIK candidates through the repo maps (the cached SEC ticker
maps, ``historical_ticker_ciks.json``, the companyfacts cache envelopes, the
holdout and sue_lt ``sec_cik_map`` files, ``nasdaq_symbol_history*.csv``,
``security_identity.csv``) plus SEC's current ``company_tickers_exchange.json``
and the tickers on the Form 25 search hits. Each candidate's submissions JSON
is fetched once and cached (``CACHE/raw/sec/submissions``).

A snapshot row (date, symbol, name) is assigned to a CIK when the listed name
matches the CIK's SEC name or one of its former names at the time of the listing; in
another word order only for an EDGAR inversion of initials ('PRICE T ROWE GROUP INC'),
for a CIK with ticker evidence ('ARDEN ELIZABETH INC') or for a CIK one of whose names
matches as written ('EVANS BOB FARMS INC'), never through SEC's name list alone ('First
Bank' is not Bank First Corp) (``name+ticker`` when the CIK is also
a ticker candidate, ``name_only`` when the name is unique among all fetched CIKs,
``name+lookup`` when the candidate came from SEC's name list); with no name match
it falls back to a single ticker candidate (``ticker_only``, low confidence). An
EDGAR entity that files no periodic reports (a subsidiary, or a bank filing with
the FDIC) never beats an issuer; alone, for a distinctive name, it is kept as
``name+lookup_nonfiler``. Truncated names (two repo files cut names at the first
hyphen) borrow the nearest full name and otherwise support no name-only match. A
name valid for only part of a listing while a rival could hold the rest, shared by
two CIKs (a holding-company reorganisation), or found through the name list for a CIK
active over a small part of it, is split by date: the CIK that held the ticker on the
dates before keeps it until its latest common Form 25 (only a CIK that held the ticker
before that filing is cut by it). One ticker has one CIK on any date: where two CIKs' rows of a ticker
overlap in time, the weaker evidence is dropped. These resolved rows are written as
they stand (``ticker_rows_raw.csv.gz``) before anything is dropped for a Form 25: step 3
decides from them which filings ended a listing (``subject_exit``), so no exit decision
rests on rows removed because of an exit. Then, after a Form 25 that ended a CIK's
listing, the tail of each covered ticker's run that the lists still show within their
lag (up to 40 days after the filing) goes to the successor that took the ticker, or to
nobody; a run seen 40 days or more after the filing contradicts the exit and keeps its
rows, and rows after the run broke (two full-list snapshots without the symbol) are a
relisting, kept as a separate interval.

Listing evidence is cleaned first: a file that repeats an older file's symbol list
exactly (``stale_duplicate_dates``) is dropped, a full-list file with far fewer common
symbols than its neighbours is presence-only (``partial_snapshot_dates``), and a row of a
company list or symbol catalog that the nearest Nasdaq symbol directories on both sides
do not list is no evidence (``uncorroborated_sightings``: NYSE and OTC names in the
nasdaq.com company lists). Company-list rows with a blank LastSale are no listing
evidence but count as present. Nasdaq's issue type 'Closed End Fund' (business development
companies and closed-end funds) is no security type: such a row is judged by its issuer name like
any other (``CLOSED_END_LABEL``; ARCC, FUND, AINV/MFIC and TICC/OXSQ were typed 'Common Stock' only in
2022), and the universe leaves investment companies out from SEC evidence (owner decision D6).
After a documented move to another exchange (step 3 ``transfer``), a company-list row of the CIK on
the moved ticker that no symbol directory after the move confirms is dropped (``after_transfer``,
``stale_after_transfer``): The Madison Square Garden Company moved MSG to NYSE on 2015-07-27, the
spun-off company took the MSG ticker there on 2015-10-01, and the nasdaq.com lists kept showing MSG
until 2017-10.

Intervals are runs of one (CIK, ticker) across every snapshot family; the share class
is assigned to each run afterwards (and splits a run only for a multi-class CIK whose
class letter changes for good). A run breaks only when the symbol is missing from two
full-list snapshots in a row (any family), or another CIK holds the ticker in between;
a stretch that no source covers (2011-01-25..2011-05-27, 2012-06-22..2012-10-24) does
not break it and is recorded as ``coverage_gap_days``. ``start`` and ``end`` are
observed dates; ``start_prev_absent``/``end_next_absent`` give the full-list snapshots
that bound them from outside.

A delist date is kept only when the security's intervals respect it: nothing seen in a
full-list snapshot more than DELIST_TOLERANCE_DAYS after it except in a later interval
that begins after two full-list snapshots without it (``listed_past_delisting``;
``delist_date_violations`` checks the rule against the raw rows and the build summary
reports ``still_listed_with_delist_date``).

An interval resting on one ticker-only snapshot row of a CIK that SEC lists today only on another
exchange is dropped (``elsewhere_one_row_intervals``: Ford's F on the symbol-only 2019-06-17 file).

Every security an interval names has a master row: a current SEC ticker of a multi-class CIK that no
Nasdaq snapshot shows (a Series B quoted OTC, notes, warrants) is its own security keyed by the ticker,
with no listing dates and a note saying so.

The foreign-filer flag (owner: foreign filers are excluded; for a MIXED CIK only the weeks whose
latest periodic report is foreign) reads the recent block and every older submissions page with
filings since 2011-06 (``regime_facts``; the listing resolution still reads older pages only when
the recent block has no periodic report). A 10-K/10-Q sets the domestic regime, a 20-F/40-F the
foreign one, and a 6-K the foreign one too except beside 10-Qs (``regime_timeline``); the flag says
which regimes are in force over 2011-06-01..2026-08-31 and ``foreign_spans`` when the foreign one is.
``periodic_form_history.csv`` (INPUTS) lists every 10-K/10-Q/20-F/40-F/6-K family filing of each
MIXED CIK with ``regime_in_force`` from its filing date; ``regime_on`` reads it for given days.
``older_pages_not_fetched`` names the pages with filings since 2011-06 that the build could not
read (not cached on an --offline run).

Run order (each step reads the other's output; both are cache-first, and a rerun
from cache takes a few minutes)::

    form25 -> security_master -> form25 -> security_master

repeated until neither table changes (round 4 converged after one alternation; the
raw rows depend on step 3 only through the filing dates of its common Form 25 rows).

SEC requests go through this builder's own limiter (``SEC_LIMITER``, 3 a second).

Usage::

    PYTHONPATH=. python scripts/reversal_data_security_master.py            # fetch + build
    PYTHONPATH=. python scripts/reversal_data_security_master.py --offline  # rebuild from cache only
    # a scratch build: every output under DIR (derived files in DIR/derived), step 3 read from F25
    PYTHONPATH=. python scripts/reversal_data_security_master.py --offline --out-dir DIR --form25 F25
"""
from __future__ import annotations

import argparse
import bisect
import html
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import sys

import pandas as pd

from scripts import reversal_data_common as common

SEC_RAW = common.RAW / "sec"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSION_PAGE_URL = "https://data.sec.gov/submissions/{name}"
TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
CIK_LOOKUP_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
LOOKUP_MAX_CIKS = 8
LOOKUP_COVER_SLACK_DAYS = 1095  # a name-list match active this much less than the listing is split by date
NAME_SLACK_DAYS = 180  # a Nasdaq list may show a new name some weeks before or after EDGAR does
DATED_NAME_SLACK_DAYS = 30  # tighter when two CIKs compete for one date
REPORTING_NOW_DAYS = 150  # a CIK whose last periodic report is this recent is still reporting
PRICE_DIR = Path("cleaned_stocks_data/price")
SNAPSHOT_DIR = Path("stocks_list_dir/nasdaq/snapshots")
IDENTITY_FILE = Path("stocks_list_dir/nasdaq/security_identity.csv")
COMPANYFACTS_DIR = Path("cleaned_stocks_data/financial/sec_companyfacts_cache")
CIK_MAP_FILES = [Path("output/research_only/holdout_2011_2019/inputs/sec_cik_map.csv"),
                 Path("output/research_only/sue_lt_2020_2026/inputs/sec_cik_map_2020.csv")]
SYMBOL_HISTORY_FILES = [Path("output/research_only/holdout_2011_2019/inputs/nasdaq_symbol_history.csv"),
                        Path("output/research_only/sue_lt_2020_2026/inputs/nasdaq_symbol_history_2020.csv")]
LISTINGS_HOOK_DIRS = {"wayback_symdir": common.CACHE / "listings" / "symdir",
                      "wayback_companylist": common.CACHE / "listings" / "companylist"}
FORM25 = common.INPUTS / "form25_nasdaq_2012_2026.csv"
MASTER = common.INPUTS / "security_master.csv"
INTERVALS = common.INPUTS / "ticker_intervals.csv"
WORK = SEC_RAW / "derived"
FILINGS_SINCE = "2011-06-01"
FLAG_UNTIL = "2026-08-31"  # the data window's end: a switch filed later changes none of its weeks
PERIODIC_HISTORY = common.INPUTS / "periodic_form_history.csv"
MULTI_CLASS_MIN_DATES = 3
# This builder's own SEC limit: several builders run at once, each at most 3 requests a second
# (common.SEC_LIMITER allows 7 to one process). SEC answers in about a second, so a few threads share it.
SEC_PER_SECOND = 3
SEC_LIMITER = common.SlidingWindowLimiter({1: SEC_PER_SECOND})
WORKERS = 6
DOMESTIC_FORMS = {"10-K", "10-K405", "10-KSB", "10-KT", "10-Q", "10-QSB", "10-QT"}
FOREIGN_FORMS = {"20-F", "40-F", "6-K", "20FR12B", "40FR12B"}
PERIODIC_FORMS = DOMESTIC_FORMS | {"20-F", "40-F"}
# Forms an operating issuer files itself. A CIK that only reports holdings (Forms 3/4/5, 13D/G)
# or runs an ADR facility (F-6) is not an issuer for identity purposes.
ISSUER_FORMS = DOMESTIC_FORMS | FOREIGN_FORMS | {
    "8-K", "8-K12B", "8-K12G3", "10-12B", "10-12G", "S-1", "F-1", "S-4", "F-4", "S-11", "424B1", "424B4",
    "DEF 14A", "DEFM14A", "PRE 14A", "25-NSE", "15-12B", "15-12G", "NT 10-K", "NT 10-Q", "NT 20-F"}

MASTER_COLUMNS = ["security_id", "cik", "first_ticker", "name", "share_class", "first_listed", "last_listed",
                  "delist_date", "delist_form25_accession", "foreign_filer", "multi_class_group", "price_sources",
                  "identity_notes",
                  # extras
                  "tickers_observed", "tickers_sec_current", "exchanges_sec_current", "former_names", "sic",
                  "sic_description", "state_of_incorporation", "entity_type", "filer_category",
                  "domestic_periodic_first", "domestic_periodic_last", "foreign_forms_first", "foreign_forms_last",
                  "foreign_spans", "filings_coverage_start", "older_pages_not_fetched", "in_form25", "form25_classes",
                  "found_via",
                  "successor_security_id", "successor_date", "successor_form25_accession",
                  "transfer_date", "transfer_form25_accession"]
INTERVAL_COLUMNS = ["security_id", "ticker", "start", "end", "exchange", "source", "source_url",
                    # extras
                    "cik", "share_class", "start_prev_absent", "end_next_absent", "n_snapshots", "match",
                    "name_in_source", "sources", "n_evidence_rows", "coverage_gap_days"]


# ------------------------------------------------------------------ SEC submissions

def parallel_map(function, items: list, workers: int = WORKERS, label: str = "") -> list:
    """``[function(item) for item in items]`` through ``common.parallel_map`` (threads sharing
    SEC_LIMITER); the first failure is raised once every item has finished."""
    items = list(items)
    if label:
        print(f"  {label}: {len(items)} items on {workers} threads", flush=True)
    results = common.parallel_map(function, items, workers)
    failures = [r for r in results if isinstance(r, Exception)]
    if failures:
        raise RuntimeError(f"{label or 'parallel_map'}: {len(failures)} of {len(items)} failed; first: {failures[0]}")
    return results


def submissions_path(cik: int) -> Path:
    return SEC_RAW / "submissions" / f"CIK{int(cik):010d}.json.gz"


def load_submissions(cik: int, *, offline: bool = False) -> dict | None:
    """The submissions JSON for ``cik`` (cache first); None when SEC has no such CIK or offline and uncached."""
    path = submissions_path(cik)
    if offline and not path.exists():
        return None
    try:
        return json.loads(common.cached_get(SUBMISSIONS_URL.format(cik=int(cik)), path, source="sec_submissions",
                                            headers=common.sec_headers(), limiter=SEC_LIMITER,
                                            symbol=f"CIK{int(cik)}"))
    except FileNotFoundError:
        return None


def load_submission_page(name: str, *, offline: bool = False) -> dict | None:
    path = SEC_RAW / "submissions" / f"{name}.gz"
    if offline and not path.exists():
        return None
    try:
        return json.loads(common.cached_get(SUBMISSION_PAGE_URL.format(name=name), path, source="sec_submissions",
                                            headers=common.sec_headers(), limiter=SEC_LIMITER))
    except FileNotFoundError:
        return None


def _filing_columns(block: dict) -> list[tuple[str, str]]:
    forms, dates = block.get("form", []) or [], block.get("filingDate", []) or []
    return list(zip(forms, dates))


def parse_submissions(payload: dict, pages: list[dict] | tuple = ()) -> dict:
    """Identity fields and periodic-filing history from a submissions JSON (plus any older pages)."""
    recent = (payload.get("filings") or {}).get("recent") or {}
    filings = _filing_columns(recent)
    for page in pages:
        filings.extend(_filing_columns(page))
    since = [(f, d) for f, d in filings if d >= FILINGS_SINCE]
    base = lambda form: form[:-2] if form.endswith("/A") else form
    domestic = sorted(d for f, d in since if base(f) in DOMESTIC_FORMS)
    foreign = sorted(d for f, d in since if base(f) in FOREIGN_FORMS)
    older = (payload.get("filings") or {}).get("files") or []
    fetched_pages = {p.get("_page_name") for p in pages}
    return {
        "cik": int(payload.get("cik") or 0),
        "name": payload.get("name") or "",
        "tickers": [str(t).upper() for t in payload.get("tickers") or []],
        "exchanges": [str(x) if x is not None else "" for x in payload.get("exchanges") or []],
        "former_names": [{"name": f.get("name", ""), "from": (f.get("from") or "")[:10], "to": (f.get("to") or "")[:10]}
                         for f in payload.get("formerNames") or []],
        "sic": str(payload.get("sic") or ""), "sic_description": payload.get("sicDescription") or "",
        "state_of_incorporation": payload.get("stateOfIncorporation") or "",
        "entity_type": payload.get("entityType") or "", "category": payload.get("category") or "",
        "periodic_dates": sorted(d for f, d in filings if base(f) in PERIODIC_FORMS),
        "filing_dates": sorted(d for f, d in filings if base(f) in ISSUER_FORMS),
        "domestic_first": domestic[0] if domestic else "", "domestic_last": domestic[-1] if domestic else "",
        "foreign_first": foreign[0] if foreign else "", "foreign_last": foreign[-1] if foreign else "",
        "n_domestic": len(domestic), "n_foreign": len(foreign),
        "coverage_start": min((d for _, d in filings), default=""),
        "older_pages": [{"name": p.get("name"), "from": p.get("filingFrom", ""), "to": p.get("filingTo", "")}
                        for p in older],
        "older_pages_unfetched_since": [p.get("name") for p in older if (p.get("filingTo") or "") >= FILINGS_SINCE
                                        and p.get("name") not in fetched_pages],
    }


def foreign_filer_flag(profile: dict) -> str:
    """The flag from every page (``profile['regime']``, see ``regime_facts``) when the profile has it.
    Otherwise, from the blocks parsed alone: Y only 20-F/40-F/6-K since 2011-06; N only 10-K/10-Q;
    MIXED both; UNKNOWN neither."""
    if profile.get("regime"):
        return profile["regime"]["flag"]
    domestic, foreign = profile["n_domestic"] > 0, profile["n_foreign"] > 0
    return {(True, False): "N", (False, True): "Y", (True, True): "MIXED"}.get((domestic, foreign), "UNKNOWN")


def needs_older_pages(profile: dict) -> bool:
    """Older pages enter the listing-resolution profile only when its recent block holds no
    periodic report (the foreign-filer flag reads every page anyway: ``regime_facts``)."""
    return profile["n_domestic"] == 0 and profile["n_foreign"] == 0 and bool(profile["older_pages_unfetched_since"])


def full_profile(cik: int, *, offline: bool = False) -> dict | None:
    payload = load_submissions(cik, offline=offline)
    if not payload:
        return None
    profile = parse_submissions(payload)
    loaded: dict[str, dict] = {}
    if needs_older_pages(profile):
        pages = []
        for name in profile["older_pages_unfetched_since"]:
            page = load_submission_page(name, offline=offline)
            if page is not None:
                pages.append({**page, "_page_name": name})
                loaded[name] = page
        profile = parse_submissions(payload, pages)
        profile["older_pages_fetched"] = len(pages)
        profile["pages_read"] = [p["_page_name"] for p in pages]
    profile["regime"] = regime_facts(payload, loaded, offline=offline)
    return profile


# ------------------------------------------------------------------ foreign-filer regime, from every page
#
# The owner excludes foreign filers, and for a CIK that switched (MIXED) only the weeks whose latest
# periodic report is foreign. The listing-resolution profile above reads older pages only when the
# recent block has no periodic report, which hid every switch older than the recent block (Atlassian
# filed 20-F/6-K until 2022-08 on its page 001; its recent block starts in 2024-09). So the flag
# reads the recent block plus every older page with filings since FILINGS_SINCE, and
# ``periodic_form_history.csv`` keeps each MIXED CIK's reports with the regime in force after each.

REGIME_DOMESTIC = DOMESTIC_FORMS
REGIME_FOREIGN = {"20-F", "40-F", "20FR12B", "40FR12B"}
REGIME_FORMS = REGIME_DOMESTIC | REGIME_FOREIGN | {"6-K"}
HISTORY_COLUMNS = ["cik", "filing_date", "form", "accession", "form_regime", "sets_regime", "regime_in_force"]


def _base_form(form: str) -> str:
    return form[:-2] if form.endswith("/A") else form


def regime_page_names(payload: dict) -> list[str]:
    """Every older submissions page that holds filings on or after FILINGS_SINCE."""
    files = (payload.get("filings") or {}).get("files") or []
    return [f["name"] for f in files if (f.get("filingTo") or "") >= FILINGS_SINCE and f.get("name")]


def regime_filings(payload: dict, pages: list[dict] | tuple = ()) -> list[tuple[str, str, str]]:
    """(filing date, form, accession) of each 10-K/10-Q/20-F/40-F/6-K family filing, amendments too,
    in the recent block and ``pages``: once per accession, oldest first."""
    seen, out = set(), []
    for block in [(payload.get("filings") or {}).get("recent") or {}, *pages]:
        forms, dates = block.get("form") or [], block.get("filingDate") or []
        accessions = list(block.get("accessionNumber") or []) + [""] * len(forms)
        for form, day, accession in zip(forms, dates, accessions):
            form = str(form or "")
            if _base_form(form) not in REGIME_FORMS or not day:
                continue
            if accession:
                if accession in seen:
                    continue
                seen.add(accession)
            out.append((str(day)[:10], form, str(accession)))
    return sorted(out)


def regime_timeline(filings: list[tuple[str, str, str]]) -> list[dict]:
    """Each filing (oldest first) with the regime it sets and the regime in force after its day.

    D is set by an original 10-K/10-Q family report, F by an original 20-F/40-F (or a 20FR12B/40FR12B
    registration). An original 6-K sets F too, since a foreign period may show only 6-Ks (TEVA's recent
    block: 6-Ks from 2017-09-19, then its first 10-K on 2018-02-12; a new foreign registrant before its
    first 20-F), except where the latest report filed before it and the next report after it are both
    domestic (a 6-K beside 10-Qs: Corvus Gold 2017, Triller 2024). Amendments set nothing (a 20-F/A
    filed after the switch to 10-K amends an old year), unless no original sets anything; then each
    filing sets its base form's regime. ``regime_in_force`` comes from the latest setting filing on
    or before the row's day (on one day a report outranks a 6-K, and F outranks D); rows before the
    first setting filing take that filing's regime.
    """
    rows = [{"filing_date": day, "form": form, "accession": accession,
             "form_regime": "D" if _base_form(form) in REGIME_DOMESTIC else "F"}
            for day, form, accession in sorted(filings)]
    reports = [(r["filing_date"], r["form_regime"]) for r in rows
               if not r["form"].endswith("/A") and _base_form(r["form"]) != "6-K"]
    report_days = [day for day, _ in reports]
    for r in rows:
        if r["form"].endswith("/A"):
            r["sets_regime"] = ""
        elif r["form"] != "6-K":
            r["sets_regime"] = r["form_regime"]
        else:
            before = bisect.bisect_left(report_days, r["filing_date"])
            after = bisect.bisect_right(report_days, r["filing_date"])
            beside_domestic = (before > 0 and reports[before - 1][1] == "D"
                               and after < len(reports) and reports[after][1] == "D")
            r["sets_regime"] = "" if beside_domestic else "F"
    if rows and not any(r["sets_regime"] for r in rows):
        for r in rows:
            r["sets_regime"] = r["form_regime"]
    by_day: dict[str, tuple] = {}
    for r in rows:
        if r["sets_regime"]:
            rank = (_base_form(r["form"]) != "6-K", r["sets_regime"] == "F")
            if r["filing_date"] not in by_day or rank > by_day[r["filing_date"]][0]:
                by_day[r["filing_date"]] = (rank, r["sets_regime"])
    current = by_day[min(by_day)][1] if by_day else ""
    for r in rows:
        current = by_day.get(r["filing_date"], (None, current))[1]
        r["regime_in_force"] = current
    return rows


def regime_flag(timeline: list[dict], since: str = FILINGS_SINCE,
                until: str = FLAG_UNTIL) -> tuple[str, list[tuple[str, str]]]:
    """(flag, foreign spans) over the window [since, until]. Y: foreign throughout; N: domestic
    throughout; MIXED: both; UNKNOWN: no filing of these forms since ``since``. The regime on ``since``
    is the one in force after the latest filing before it (with none, the earliest filing's, so a
    company whose first report comes after ``until`` has that report's regime). Spans are
    [first day, last day] in which the regime in force is foreign, clipped to the window."""
    later = [r for r in timeline if r["filing_date"] >= since]
    if not later:
        return "UNKNOWN", []
    earlier = [r for r in timeline if r["filing_date"] < since]
    inside = [r for r in later if r["filing_date"] <= until]
    regime = (earlier[-1] if earlier else later[0])["regime_in_force"]
    regimes, spans = {regime}, []
    start = since if regime == "F" else None
    for r in inside:
        regime = r["regime_in_force"]
        regimes.add(regime)
        if regime == "F" and start is None:
            start = r["filing_date"]
        elif regime == "D" and start is not None:
            spans.append((start, _shift(r["filing_date"], -1)))
            start = None
    if start is not None:
        spans.append((start, until))
    flag = {frozenset({"D"}): "N", frozenset({"F"}): "Y"}.get(frozenset(regimes), "MIXED")
    return flag, spans


def regime_facts(payload: dict, pages: dict[str, dict] | None = None, *, offline: bool = False) -> dict:
    """The foreign-filer flag and its evidence from the recent block and every older page with
    filings since FILINGS_SINCE (``pages`` already loaded are reused; the rest come from the cache, or
    from SEC unless ``offline``). ``pages_unread`` names those that could not be read (uncached
    offline): the flag rests on fewer filings there."""
    pages = dict(pages or {})
    read, unread = [], []
    for name in regime_page_names(payload):
        page = pages.get(name)
        if page is None:
            page = load_submission_page(name, offline=offline)
        if page is None:
            unread.append(name)
        else:
            pages[name] = page
            read.append(name)
    filings = regime_filings(payload, [pages[name] for name in read])
    timeline = regime_timeline(filings)
    flag, spans = regime_flag(timeline)
    window = [(day, _base_form(form)) for day, form, _ in filings if day >= FILINGS_SINCE]
    domestic = [day for day, base in window if base in DOMESTIC_FORMS]
    foreign = [day for day, base in window if base in FOREIGN_FORMS]
    return {"flag": flag, "spans": spans, "pages_read": read, "pages_unread": unread,
            "domestic_first": min(domestic, default=""), "domestic_last": max(domestic, default=""),
            "foreign_first": min(foreign, default=""), "foreign_last": max(foreign, default=""),
            # the per-filing table is kept only where later steps need it (MIXED: foreign weeks only)
            "timeline": timeline if flag == "MIXED" else []}


def history_rows(profiles: dict[int, dict], ciks: set[int] | None = None) -> pd.DataFrame:
    """``periodic_form_history.csv``: every 10-K/10-Q/20-F/40-F/6-K family filing of each MIXED CIK
    (of ``ciks`` when given), with the regime it sets and the regime in force from its filing date."""
    rows = []
    for cik in sorted(profiles):
        regime = profiles[cik].get("regime") or {}
        if regime.get("flag") != "MIXED" or (ciks is not None and cik not in ciks):
            continue
        rows.extend({"cik": cik, **{k: r[k] for k in HISTORY_COLUMNS[1:]}} for r in regime["timeline"])
    return pd.DataFrame(rows, columns=HISTORY_COLUMNS)


def regime_on(history: pd.DataFrame, cik: int, days: list[str]) -> list[str]:
    """'D' or 'F' in force on each of ``days`` for a CIK of ``periodic_form_history.csv``: the
    regime_in_force of its latest filing on or before the day (its first filing's before that);
    '' for a CIK not in the table (not MIXED: use its security-master flag)."""
    own = history[history["cik"].astype(int) == int(cik)].sort_values("filing_date", kind="stable")
    if own.empty:
        return [""] * len(days)
    dates, regimes = own["filing_date"].astype(str).tolist(), own["regime_in_force"].tolist()
    return [regimes[max(bisect.bisect_right(dates, str(day)) - 1, 0)] for day in days]


# ------------------------------------------------------------------ names and classes

# Legal-form words carry no identity; they are dropped wherever they stand.
LEGAL_WORDS = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "llc", "lp",
               "nv", "sa", "ag", "se", "the", "de", "bv", "spa", "ab", "asa", "oyj", "cos", "companies", "sab", "cv",
               "lc"}
TRAILING_WORDS = {"new", "del", "md", "l", "p", "as"}
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9",
         "hldgs": "holdings", "hldg": "holding", "intl": "international", "grp": "group", "bancorporation": "bancorp"}
SECURITY_TAIL = re.compile(
    r"\s+(?:class [a-z]\b|series [a-z]\b|common stock|common shares|ordinary shares|capital stock|"
    r"american depositary|american depository|depositary shares|sponsored adr|adr\b|ads\b|shares of beneficial|"
    r"new york registry|subordinate voting|common units?).*$", re.I)
# A ' - ' part of a listed name is cut only when it names a security ('PMC - Sierra, Inc. - Common Stock'
# keeps 'PMC - Sierra, Inc.').
SECURITY_PHRASE = re.compile(
    r"\b(?:stock|shares?|units?|warrants?|rights?|notes?|debentures?|bonds?|preferred|deposit[ao]ry|adrs?|adss?|"
    r"receipts?|interests?|ordinary|common|class [a-z]|series [a-z]|issued|certificates?|voting|subordinate|"
    r"tracking|closed[- ]end\s+fund)\b|%", re.I)


def strip_security_phrase(text: str) -> str:
    parts = re.split(r"\s+-\s+", text)
    while len(parts) > 1 and SECURITY_PHRASE.search(parts[-1]):
        parts.pop()
    return " ".join(parts)


def _join_single_letters(tokens: list[str]) -> list[str]:
    """'hunt j b transport' -> 'hunt jb transport' (EDGAR drops the dots of initials, listings keep them)."""
    out, run = [], []
    for token in tokens:
        if len(token) == 1 and token.isalpha():
            run.append(token)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(token)
    if run:
        out.append("".join(run))
    return out


def normalize_issuer_name(text: str) -> str:
    """'Achillion Pharmaceuticals, Inc. - Common Stock' and 'ACHILLION PHARMACEUTICALS INC' -> 'achillion pharmaceuticals'."""
    text = html.unescape(str(text or "")).strip()
    text = strip_security_phrase(text)
    text = SECURITY_TAIL.sub("", text)
    text = re.sub(r"\s*[\\/]\s*[A-Za-z]{2,4}\s*[\\/]?\s*$", "", text)  # COST PLUS INC/CA/, LANDEC CORP \CA\, X INC / CT
    text = re.sub(r"\([^)]*\)", " ", text)  # Bank of Commerce Holdings (CA), Elmira Savings Bank (The)
    text = re.sub(r"\b([A-Za-z])\.(?=[A-Za-z]\b)", r"\1", text)  # S.A. -> SA., N.V. -> NV.
    text = re.sub(r"['’`]", "", text)  # Conn's -> Conns
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = [ROMAN.get(t, t) for t in _join_single_letters(text.split()) if t not in LEGAL_WORDS]
    while tokens and tokens[-1] in TRAILING_WORDS:
        tokens.pop()
    return " ".join(tokens)


FILLER_WORDS = {"of", "and", "the", "for"}
GENERIC_FIRST = {"first", "american", "united", "china", "national", "global", "international", "new", "great",
                 "general", "southern", "northern", "western", "eastern", "pacific", "atlantic", "community", "bank",
                 "citizens", "peoples", "home", "us", "north", "south", "west", "east", "central", "capital"}


def reorder_allowed(tokens: list[str]) -> bool:
    """Whether a name may match another in a different word order: only an EDGAR inversion, which moves
    initials to the end ('PRICE T ROWE GROUP', 'FOSTER L B', 'BARRY R G'), so the name must hold an
    initial (a token of one or two letters that is no filler word). 'First Bank' (FRBA, an FDIC filer)
    is not 'Bank First' (BFC)."""
    return len(tokens) >= 2 and any(len(t) <= 2 and t not in FILLER_WORDS for t in tokens)


def names_match(a: str, b: str, reorder: bool = False) -> bool:
    """Normalized names equal (in another word order for an EDGAR inversion of initials, 'PRICE T ROWE
    GROUP INC' (``reorder_allowed``), or with ``reorder`` for a CIK with ticker evidence, 'ARDEN
    ELIZABETH INC'), or one a token prefix of the other (two tokens, or one distinctive token)."""
    if not a or not b:
        return False
    if a == b or a.replace(" ", "") == b.replace(" ", ""):
        return True
    ta, tb = a.split(), b.split()
    if (reorder_allowed(ta) or (reorder and len(ta) >= 2)) and sorted(ta) == sorted(tb):
        return True
    fa, fb = [t for t in ta if t not in FILLER_WORDS], [t for t in tb if t not in FILLER_WORDS]
    if len(fa) >= 2 and fa == fb:  # 'motorcar parts of america' and EDGAR's 'MOTORCAR PARTS AMERICA INC'
        return True
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if long_[:len(short)] != short:
        return False
    return len(short) >= 2 or (len(short[0]) >= 5 and short[0] not in GENERIC_FIRST)


def distinctive_name(key: str) -> bool:
    """A normalized name that may identify a company by itself: two tokens or more, six letters or more."""
    return len(key.split()) >= 2 and len(key.replace(" ", "")) >= 6


def share_class_from_name(name: str) -> str:
    """'Alphabet Inc. - Class C Capital Stock' -> 'C', 'Rush Enterprises Inc. Common Stock Cl A' -> 'A';
    ADRs -> 'ADS'; otherwise 'COMMON'."""
    text = str(name or "")
    if re.search(r"american depositary|american depository|\bADS\b|\bADR\b|depositary shares", text, re.I):
        return "ADS"
    match = re.search(r"\b(?:class|series|cl\.?)\s+([A-Z])\b", text, re.I)
    return match.group(1).upper() if match else "COMMON"


NON_COMMON = re.compile(
    r"\bPreferred\b|\bPreference Shares?\b|\bWarr+ants?\b|\b(?:Sub)?Units?\b|Notes? due|Debenture|\bRights?\b|"
    r"Tangible Equity|Trust Preferred|Senior Notes|Subordinated Notes|\bETF\b|\bETNs?\b|Exchange Traded|"
    r"\bIndex Fund\b|\bTest Stock\b|\bWhen[- ]Issued\b|\bClosed[- ]End\b|\bL\.?P\.?\b|\bLimited Partnership\b|"
    r"Depositary Shares?,? each representing (?:a |one )?(?:\d+/\d+(?:th)?|fractional|\d[\d,]*th)|"
    r"representing .*interest in .*preferred|\bFund\b|\bPortfolio\b|ProShares|PowerShares|iShares|WisdomTree|"
    r"BLDRS|VelocityShares|NextShares|Index Tracking Stock|\bTrust,? Series 1\b|"
    # Abbreviated preferreds, baby bonds and notes ('Non Cumulative Perp Conv Pfd Ser A', 'Dep Shs Rep 1/40th
    # Int 6.75% Srs A Non-Cum Pfd', 'Baby Bond', 'Senior Unsecured Notes', 'Capital Securities').
    r"\bPfd\b|\bDep\.? Shs\b|\bBaby Bonds?\b|\bNotes\b|\bUnsecured\b|\bCapital Securities\b|\bSAIL Securities\b|"
    r"\bADWs?\b|Depositary Warrants?", re.I)


ADR_NAME = re.compile(r"\bamer\w*\s+deposit[ao]ry|\bADS\b|\bADRs?\b|new york registry", re.I)
ADR_EXCLUDE = re.compile(r"preferred|warr+ant|\brights?\b|\bunits?\b|notes? due|\bADWs?\b|\bPfd\b", re.I)
# On Nasdaq a depositary share or receipt that is not an American depositary share is a fractional
# preferred (SRCLP 'Depository Receipt', IBKCP 'Depositary Shares Representing Series B').
DEPOSITARY_PREFERRED = re.compile(r"deposit[ao]ry (?:shares?|receipts?)\b", re.I)


# Nasdaq's issue type for the listed shares of a closed-end investment company (a business development
# company or a closed-end fund): 'Ares Capital Corporation - Closed End Fund'. The same files call the
# same issues 'Common Stock' at other times (ARCC, FUND, AINV, OXSQ in the 2022-06-24 and 2022-07-23
# files; the 300M screener throughout), so the label is no security type: the issuer's name is judged as
# for any other listing (a name with 'Fund' in it stays out), and whether the issuer is an investment
# company is decided from SEC filings downstream (owner decision D6, step 12).
CLOSED_END_LABEL = re.compile(r"\s+-\s+closed[- ]end\s+fund\s*$", re.I)


def is_common_equity(name: str) -> bool:
    """Common stock, ordinary shares and ADRs; not preferreds, warrants, units, rights, debt, funds, LPs.

    Unlike ``investable_common_equities`` this keeps every ADR (the plan's ADR flag)
    and SPAC shares, so the identity of a SPAC that becomes an operating company is kept. Nasdaq's
    'Closed End Fund' issue type alone does not exclude a listing (``CLOSED_END_LABEL``).
    """
    name = CLOSED_END_LABEL.sub("", str(name or ""))
    if ADR_NAME.search(name) and not ADR_EXCLUDE.search(name):
        return True
    if DEPOSITARY_PREFERRED.search(name) and not re.search(r"ordinary|common share", name, re.I):
        return False
    return not NON_COMMON.search(name)


# ------------------------------------------------------------------ evidence loaders

def _date_from_name(path: Path) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name) or re.search(r"(\d{8})", path.name)
    if not match:
        return ""
    text = match.group(1)
    return text if "-" in text else f"{text[:4]}-{text[4:6]}-{text[6:]}"


# Repo files whose names were cut at the first hyphen ('Coca' for Coca-Cola, 'G' for G-III, '1' for
# 1-800-Flowers): their rows borrow the nearest full name of the same symbol.
TRUNCATED_NAME_FILES = {"nasdaq_listed_2015-01-10.csv", "nasdaq_listed_2018-08-22.csv"}
NO_LAST_SALE = {"", "N/A", "NA", "NAN", "NONE"}


def _read_snapshot(path: Path, source: str) -> pd.DataFrame:
    """One snapshot file as rows (symbol, name, date, flags, source, source_url, name_truncated, no_last_sale).

    Company-list rows without a last sale are flagged ``no_last_sale``: some are companies in
    the IPO pipeline, so they are no listing evidence, but most are listed symbols with a blank
    quote (SunPower in every 2015-2016 list), so they count as present when a run is broken.
    """
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    columns = {c.lower().strip(): c for c in frame.columns}
    symbol = columns.get("symbol") or columns.get("ticker")
    name = columns.get("name") or columns.get("security name") or columns.get("company name")
    if not symbol or not name:
        return pd.DataFrame()
    last_sale = columns.get("lastsale") or columns.get("last sale")
    observed = columns.get("observed at") or columns.get("snapshot_date") or columns.get("date")
    out = pd.DataFrame({"symbol": frame[symbol].str.strip().str.upper(), "name": frame[name].str.strip()})
    out["no_last_sale"] = (frame[last_sale].str.strip().str.lstrip("$").str.upper().isin(NO_LAST_SALE)
                           if last_sale and source == "wayback_companylist" else False)
    out["date"] = frame[observed].str[:10] if observed else _date_from_name(path)
    for flag in ("ETF", "Test Issue", "NextShares"):
        column = columns.get(flag.lower())
        out[flag] = frame[column].str.upper() if column else ""
    source_file = columns.get("source file")
    out["source"] = source
    out["source_url"] = frame[source_file] if source_file else str(path)
    out["name_truncated"] = Path(path).name in TRUNCATED_NAME_FILES
    return out


def load_listing_snapshots(snapshot_dir: Path = SNAPSHOT_DIR, hook_dirs: dict[str, Path] | None = None) -> pd.DataFrame:
    """Every snapshot row (date, symbol, name, flags, source) from the repo and, when present, the Wayback hook."""
    frames = []
    for path in sorted(snapshot_dir.glob("nasdaq_listed_*.csv")):
        frames.append(_read_snapshot(path, "repo_symdir"))
    for path in sorted(snapshot_dir.glob("nasdaq_300M_*.csv")):
        frames.append(_read_snapshot(path, "repo_screener_300M"))
    for source, directory in (LISTINGS_HOOK_DIRS if hook_dirs is None else hook_dirs).items():
        if Path(directory).is_dir():
            for path in sorted(Path(directory).glob("*.csv")) + sorted(Path(directory).glob("*.csv.gz")):
                frames.append(_read_snapshot(path, source))
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["symbol", "name", "date", "ETF", "Test Issue", "NextShares", "source", "source_url",
                                     "name_truncated", "no_last_sale"])
    snapshots = pd.concat(frames, ignore_index=True)
    snapshots["name_truncated"] = snapshots["name_truncated"].fillna(False).astype(bool)
    snapshots["no_last_sale"] = snapshots["no_last_sale"].fillna(False).astype(bool)
    return snapshots[(snapshots["symbol"] != "") & (snapshots["date"] != "")]


STALE_MIN_DAYS = 7  # an identical symbol list this long after an earlier one is a stale copy


def stale_duplicate_dates(snapshots: pd.DataFrame, min_days: int = STALE_MIN_DAYS) -> dict[tuple[str, str], str]:
    """(family, date) -> the earlier date whose symbol list it repeats exactly, for full-list families.

    Some repo symbol files are a later commit of an older file: nasdaq_listed_2024-05-18 lists exactly
    the symbols of 2024-03-28 (same source file and commit), 2023-12-21 those of 2023-10-27. Read at
    their own dates they would list names that left Nasdaq in between (GTH, AMNB, MCAF, Avid, Blue
    Apron) weeks after their Form 25, so they are dropped. Over a few days (a weekend) two lists can
    agree for real; those are kept."""
    out = {}
    for family, group in snapshots.groupby("source"):
        if family in PARTIAL_FAMILIES:
            continue
        seen: dict[frozenset, str] = {}
        for day, symbols in sorted(group.groupby("date")["symbol"]):
            key = frozenset(symbols)
            first = seen.get(key)
            if first is not None and (pd.Timestamp(day) - pd.Timestamp(first)).days > min_days:
                out[(family, day)] = first
            else:
                seen.setdefault(key, day)
    return out


CORROBORATE_DAYS = 70  # the Nasdaq symbol directories that check a sighting of another kind of list
SYMBOL_DIRECTORY = re.compile(r"nasdaqlisted|SymDir", re.I)


def uncorroborated_sightings(snapshots: pd.DataFrame, window_days: int = CORROBORATE_DAYS) -> pd.Series:
    """Rows of full-list files other than Nasdaq's symbol directory (nasdaqlisted.txt) whose symbol
    neither the nearest symbol directory before nor the nearest after lists, both within
    ``window_days`` (nor one of the same day): the nasdaq.com company lists of 2015-2019 carry some
    NYSE and OTC names (MSG, America Movil, 8x8, Condor, Lilis, Pressure BioSciences), and a third-party
    symbol catalog of 2024-01-26 and 2024-02-27 still lists names that left Nasdaq weeks before
    (LiveVox, Salem Media, Virtus). Such a row is no evidence of a Nasdaq listing. Returns a boolean
    mask over ``snapshots``."""
    mask = pd.Series(False, index=snapshots.index)
    full = snapshots[~snapshots["source"].isin(PARTIAL_FAMILIES)]
    directory = (full["source"] == "wayback_symdir") | full["source_url"].astype(str).str.contains(SYMBOL_DIRECTORY)
    listed = full[directory].groupby("date")["symbol"].apply(set).to_dict()
    days = sorted(listed)
    for (_, day), group in full[~directory].groupby(["source", "date"]):
        i = bisect.bisect_left(days, day)
        before = days[i - 1] if i > 0 else None
        j = i + 1 if i < len(days) and days[i] == day else i
        after = days[j] if j < len(days) else None
        if before is None or after is None or (pd.Timestamp(after) - pd.Timestamp(before)).days > 2 * window_days \
                or (pd.Timestamp(day) - pd.Timestamp(before)).days > window_days \
                or (pd.Timestamp(after) - pd.Timestamp(day)).days > window_days:
            continue
        known = listed[before] | listed[after] | listed.get(day, set())
        mask.loc[group.index[~group["symbol"].isin(known)]] = True
    return mask


def directory_rows(frame: pd.DataFrame) -> pd.Series:
    """Rows of Nasdaq's own symbol directory (nasdaqlisted.txt), from the Wayback or a repo file that
    names it (``source``/``family`` and ``source_url``); the test ``uncorroborated_sightings`` uses."""
    family = frame["family"] if "family" in frame else frame["source"]
    return (family == "wayback_symdir") | frame["source_url"].astype(str).str.contains(SYMBOL_DIRECTORY)


def directory_listing(snapshots: pd.DataFrame) -> dict[str, set[str]]:
    """date -> the symbols Nasdaq's symbol directory lists that day (full-list families only)."""
    full = snapshots[~snapshots["source"].isin(PARTIAL_FAMILIES)]
    return full[directory_rows(full)].groupby("date")["symbol"].apply(set).to_dict()


def transfer_filings(form25: pd.DataFrame | None) -> dict[int, list[tuple[str, set[str]]]]:
    """cik -> [(filing date, tickers covered)] of its documented moves to another exchange (step 3's
    ``transfer`` with ``subject_exit`` Y). The tickers are those the CIK listed before the filing, ended
    at it or continued past it; an empty set covers every ticker of the CIK."""
    if form25 is None or form25.empty:
        return {}
    rows = form25[(form25["classification"] == "transfer") & (form25["subject_exit"] == "Y")]
    out: dict[int, list[tuple[str, set[str]]]] = defaultdict(list)
    for row in rows.itertuples():
        covered = set(row.tickers_before.split()) | set(row.tickers_ended.split()) | set(row.tickers_continued.split())
        out[int(row.subject_cik)].append((row.filing_date, covered))
    return dict(out)


def stale_after_transfer(rows: pd.DataFrame, transfers: dict[int, list[tuple[str, set[str]]]],
                         listed: dict[str, set[str]], grace_days: int | None = None,
                         window_days: int = CORROBORATE_DAYS) -> pd.Index:
    """Rows of the nasdaq.com company lists that show a CIK on a ticker it moved to another exchange.

    After a documented transfer (``transfer_filings``: an issuer withdrawal with a Form 8-A12B or a Form
    25 naming the new exchange), a ``wayback_companylist`` row of the CIK on a covered ticker dated more
    than ``grace_days`` after the filing is no evidence of a Nasdaq listing unless a symbol directory
    dated after that cutoff and within ``window_days`` of the row lists the symbol, or the CIK is back
    in a symbol directory on that ticker by then (a return to Nasdaq: TD Ameritrade from 2016-01). The
    company lists of 2015-2019 kept such names for months or years where no symbol directory is near
    enough for ``uncorroborated_sightings`` to check them: The Madison Square Garden Company, on NYSE
    from 2015-07-27 (and its MSG ticker held there by the spun-off company from 2015-10-01), stayed
    in them as MSG until 2017-10; America Movil, Condor, Lilis, R.R. Donnelley and FirstCash likewise.
    Returns the index of those rows. ``rows`` has date, symbol, cik, family, source_url."""
    grace_days = EXIT_GRACE_DAYS if grace_days is None else grace_days
    if not transfers or rows.empty:
        return rows.index[:0]
    days = sorted(listed)
    live = rows[rows["cik"].notna()]
    mine = live[live["cik"].astype(int).isin(set(transfers))]
    if mine.empty:
        return rows.index[:0]
    on_directory = directory_rows(mine)
    stale = []
    for (cik, symbol), group in mine.groupby([mine["cik"].astype(int), "symbol"]):
        own_directory = sorted(group.loc[on_directory[group.index], "date"])
        for filed, covered in transfers[int(cik)]:
            if covered and symbol not in covered:
                continue
            cutoff = _shift(filed, grace_days)
            back = next((d for d in own_directory if d > cutoff), "9999-12-31")
            late = group[(group["date"] > cutoff) & (group["date"] < back) & (group["family"] == "wayback_companylist")]
            for index, day in zip(late.index, late["date"]):
                lo, hi = max(_shift(day, -window_days), cutoff), _shift(day, window_days)
                near = days[bisect.bisect_right(days, lo):bisect.bisect_right(days, hi)]
                if not any(symbol in listed[d] for d in near):
                    stale.append(index)
    return pd.Index(sorted(set(stale)))


PARTIAL_DATE_RATIO = 0.9  # a full-list file with fewer common symbols than this share of its neighbours'


def partial_snapshot_dates(listings: pd.DataFrame, ratio: float = PARTIAL_DATE_RATIO) -> dict[str, list[str]]:
    """family -> dates of a full-list family whose file lists fewer than ``ratio`` times the median
    number of common symbols of the two files on either side (repo_symdir 2022-06-24: 3,037 against
    about 3,770). Such a file is evidence of presence, not of absence."""
    out: dict[str, list[str]] = {}
    for family, group in listings.groupby("source"):
        if family in PARTIAL_FAMILIES:
            continue
        counts = group.groupby("date")["symbol"].nunique().sort_index()
        values, days = counts.tolist(), counts.index.tolist()
        for i, day in enumerate(days):
            around = values[max(0, i - 2):i] + values[i + 1:i + 3]
            if around and values[i] < ratio * sorted(around)[(len(around) - 1) // 2]:  # the lower median
                out.setdefault(family, []).append(day)
    return out


def is_partial_family(family: str) -> bool:
    """Families that list only part of Nasdaq, and the pseudo-family of a full family's partial files."""
    return family in PARTIAL_FAMILIES or family.endswith(PARTIAL_SUFFIX)


TYPED_NAME = re.compile(r" - |common stock|ordinary shares?|common shares|capital stock|depositary|depository|"
                        r"warrants?\b|\bunits?\b|\brights?\b|preferred|notes? due|\bETF\b|beneficial interest", re.I)
# Nasdaq fifth-letter codes that mark a non-common issue: warrants, units, rights, preferreds,
# convertible bonds, when-issued, funds, miscellaneous.
NON_COMMON_FIFTH = set("WURPONMLGHIVXZT")
NEAREST_TYPED_DAYS = 3 * 365


def fill_symbol_only_names(snapshots: pd.DataFrame, days: int = 90) -> pd.DataFrame:
    """Rows whose name is just the symbol (the 2019-06-17 file) borrow the name of the nearest other
    row of the same symbol within ``days``; with none the name is left empty.

    Rows of a file with names cut at the first hyphen (``name_truncated``) borrow the nearest full
    name of the same symbol within ``days`` when the cut name is its start ('Coca' -> 'Coca-Cola
    Consolidated, Inc.'); otherwise they keep the cut name and stay flagged.
    """
    frame = snapshots.copy()
    if "name_truncated" not in frame:
        frame["name_truncated"] = False
    bare = frame["name"].str.strip().str.upper().eq(frame["symbol"])
    cut = frame["name_truncated"].astype(bool) & ~bare
    if not bare.any() and not cut.any():
        return frame
    frame["_day"] = pd.to_datetime(frame["date"], errors="coerce")
    named = frame[~bare & ~cut & frame["_day"].notna()][["symbol", "_day", "name"]].rename(columns={"name": "borrowed"})
    named = named.sort_values("_day")
    if bare.any():
        rows = frame[bare & frame["_day"].notna()].reset_index()
        near = pd.merge_asof(rows.sort_values("_day"), named, on="_day", by="symbol",
                             direction="nearest", tolerance=pd.Timedelta(days=days)).set_index("index")["borrowed"]
        frame.loc[near.index, "name"] = near.fillna("")
        frame.loc[near.index[near.notna()], "name_truncated"] = False
    if cut.any():
        rows = frame[cut & frame["_day"].notna()].reset_index()
        near = pd.merge_asof(rows.sort_values("_day"), named, on="_day", by="symbol",
                             direction="nearest", tolerance=pd.Timedelta(days=days)).set_index("index")
        compact = lambda text: re.sub(r"[^a-z0-9]", "", str(text).lower())
        fits = [isinstance(b, str) and bool(compact(n)) and compact(b).startswith(compact(n))
                for n, b in zip(near["name"], near["borrowed"])]
        fit = near[fits]
        frame.loc[fit.index, "name"] = fit["borrowed"]
        frame.loc[fit.index, "name_truncated"] = False
    return frame.drop(columns="_day")


def fill_classes(rows: pd.DataFrame) -> pd.Series:
    """Share class per row: from the name when it states one, else from the nearest typed row of the
    same CIK and symbol, else COMMON."""
    classes = rows["name"].map(share_class_from_name)
    typed = rows["name"].str.contains(TYPED_NAME)
    frame = rows.assign(_day=pd.to_datetime(rows["date"], errors="coerce"), _class=classes)
    known = frame[typed][["cik", "symbol", "_day", "_class"]].rename(columns={"_class": "near_class"})
    bare = frame[~typed].reset_index()
    if bare.empty or known.empty:
        return classes
    bare["_key"] = bare["cik"].astype(str) + "|" + bare["symbol"]
    known = known.assign(_key=known["cik"].astype(str) + "|" + known["symbol"])
    near = pd.merge_asof(bare.sort_values("_day"), known[["_key", "_day", "near_class"]].sort_values("_day"),
                         on="_day", by="_key", direction="nearest").set_index("index")["near_class"]
    out = classes.copy()
    own = classes.loc[near.index]
    # A bare name that states its class keeps it ('Rush Enterprises Inc Cl B').
    out.loc[near.index] = [o if o not in GENERIC_CLASSES or not isinstance(n, str) else n for o, n in zip(own, near)]
    return out


def common_snapshot_rows(snapshots: pd.DataFrame) -> pd.DataFrame:
    """Rows that are common equity.

    A name that states its security type (symbol files from 2011 on, the 300M
    screener) is judged by the name. A bare company name (the nasdaq.com company
    lists and the truncated 2015 file) is judged by the nearest typed row of the
    same symbol within three years; with none, by the name and the symbol's fifth letter.
    Company-list rows without a last sale (``no_last_sale``) are no listing evidence.
    """
    frame = snapshots.copy()
    if "no_last_sale" in frame:
        frame = frame[~frame["no_last_sale"].fillna(False).astype(bool)]
    flagged = frame["ETF"].eq("Y") | frame["Test Issue"].eq("Y") | frame["NextShares"].eq("Y")
    frame["is_common"] = ~flagged & frame["name"].map(is_common_equity)
    frame["typed"] = frame["name"].str.contains(TYPED_NAME) | flagged
    frame["_day"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame[frame["_day"].notna()]
    typed = frame[frame["typed"]][["symbol", "_day", "is_common"]].rename(columns={"is_common": "typed_common"})
    bare = frame[~frame["typed"]].reset_index()
    if not bare.empty and not typed.empty:
        near = pd.merge_asof(bare.sort_values("_day"), typed.sort_values("_day"), on="_day", by="symbol",
                             direction="nearest", tolerance=pd.Timedelta(days=NEAREST_TYPED_DAYS))
        near = near.set_index("index")["typed_common"]
    else:
        near = pd.Series(dtype=object)
    bare = bare.set_index("index")
    fifth = bare["symbol"].str.len().eq(5) & bare["symbol"].str[-1].isin(NON_COMMON_FIFTH)
    decided = near.reindex(bare.index)
    frame.loc[bare.index, "is_common"] = [bool(d) if pd.notna(d) else (c and not f)
                                          for d, c, f in zip(decided, bare["is_common"], fifth)]
    return frame[frame["is_common"]].drop(columns=["is_common", "typed", "_day"])


def price_file_ranges(price_dir: Path = PRICE_DIR) -> pd.DataFrame:
    """ticker, file, first_date, last_date, rows for every stored price file (dates only)."""
    records = []
    for path in sorted(price_dir.glob("*.csv")):
        dates = pd.read_csv(path, usecols=["date"])["date"].dropna().astype(str)
        records.append({"file": path.name, "ticker": path.stem.upper(), "first_date": dates.min() if len(dates) else "",
                        "last_date": dates.max() if len(dates) else "", "rows": len(dates)})
    return pd.DataFrame(records)


def text_value(value) -> str:
    """``value`` as text, with None and NaN as '' (``str(nan or '')`` is 'nan')."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


class Candidates:
    """ticker -> {cik: set(sources)}."""

    def __init__(self):
        self.map: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))

    def add(self, ticker, cik, source: str) -> None:
        ticker = text_value(ticker).strip().upper()
        if ticker in ("NAN", "NONE", "NULL"):
            return
        try:
            cik = int(cik)
        except (TypeError, ValueError):
            return
        if ticker and cik > 0:
            self.map[ticker][cik].add(source)

    def get(self, ticker: str) -> dict[int, set[str]]:
        return self.map.get(str(ticker).upper(), {})

    def ciks(self) -> set[int]:
        return {cik for by_cik in self.map.values() for cik in by_cik}


def fetch_sec_tickers_exchange(*, offline: bool = False) -> list[dict]:
    """SEC's current ticker list with exchange (one request, cached by date)."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    directory = SEC_RAW / "ticker_maps"
    cached = sorted(directory.glob("company_tickers_exchange_*.json.gz")) if directory.exists() else []
    path = cached[-1] if cached else directory / f"company_tickers_exchange_{stamp}.json.gz"
    if offline and not path.exists():
        return []
    payload = json.loads(common.cached_get(TICKERS_EXCHANGE_URL, path, source="sec_files", headers=common.sec_headers(),
                                           limiter=SEC_LIMITER))
    fields = payload["fields"]
    return [dict(zip(fields, row)) | {"_path": str(path)} for row in payload["data"]]


def _companyfacts_symbols(directory: Path = COMPANYFACTS_DIR) -> list[tuple[str, int]]:
    """(symbol, cik) from the head of each companyfacts cache envelope, without parsing the facts."""
    pairs = []
    for path in sorted(directory.glob("CIK*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            head = handle.read(4096)
        cik = re.search(r'"cik":\s*(\d+)', head)
        symbols = re.search(r'"symbols":\s*\[([^\]]*)\]', head)
        if cik and symbols:
            pairs.extend((s, int(cik.group(1))) for s in re.findall(r'"([^"]+)"', symbols.group(1)))
    return pairs


def collect_candidates(sec_current: list[dict], form25: pd.DataFrame | None) -> Candidates:
    cands = Candidates()
    for row in sec_current:
        cands.add(row.get("ticker"), row.get("cik"), "sec_company_tickers_exchange")
    for path in sorted((COMPANYFACTS_DIR / "ticker_maps").glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for ticker, cik in json.load(handle).items():
                cands.add(ticker, cik, f"repo_ticker_map:{path.name[:22]}")
    hist = COMPANYFACTS_DIR / "historical_ticker_ciks.json"
    if hist.exists():
        for ticker, entry in (json.loads(hist.read_text()).get("entries") or {}).items():
            if isinstance(entry, dict):
                cands.add(ticker, entry.get("cik"), "repo_historical_ticker_ciks")
                for predecessor in entry.get("predecessor_ciks") or []:
                    cands.add(ticker, predecessor, "repo_historical_ticker_ciks_predecessor")
    for ticker, cik in _companyfacts_symbols():
        cands.add(ticker, cik, "repo_companyfacts_cache")
    for path in CIK_MAP_FILES:
        if path.exists():
            for row in pd.read_csv(path).itertuples():
                cands.add(row.ticker, row.source_cik, f"repo_{path.name}")
    if form25 is not None:
        for row in form25.itertuples():
            for ticker in text_value(row.subject_tickers_sec).split():
                cands.add(ticker, row.subject_cik, "form25_efts_display_name")
    # Old symbols take the CIK of the ticker they became (same issuer, renamed).
    for path in SYMBOL_HISTORY_FILES:
        if path.exists():
            for row in pd.read_csv(path).itertuples():
                for cik, sources in list(cands.get(row.ticker).items()):
                    cands.add(row.historical_symbol, cik, f"repo_{path.name}")
    if IDENTITY_FILE.exists():
        for row in pd.read_csv(IDENTITY_FILE).itertuples():
            cik = re.search(r"/edgar/data/(\d+)/", str(row.source_url))
            if cik and row.identity_type in ("issuer_rename", "reverse_merger"):
                cands.add(row.historical_ticker, cik.group(1), "repo_security_identity")
                cands.add(row.provider_ticker, cik.group(1), "repo_security_identity")
    return cands


# ------------------------------------------------------------------ resolution

def name_key(text: str) -> str:
    """Index key: the normalized name without spaces ('U S Concrete' and 'U.S. CONCRETE' agree)."""
    return normalize_issuer_name(text).replace(" ", "")


def index_keys(normalized: str) -> list[str]:
    """Keys of a normalized name in a name index: without spaces, and (for a name with initials,
    ``reorder_allowed``) its sorted tokens, so 'price t rowe group' and 't rowe price group' share a key."""
    if not normalized:
        return []
    keys = [normalized.replace(" ", "")]
    tokens = normalized.split()
    if reorder_allowed(tokens):
        keys.append("~" + " ".join(sorted(tokens)))
    return keys


def names_in(index: dict[str, set[int]] | None, normalized: str) -> set[int]:
    """CIKs filed under ``normalized`` (any key) in a name index."""
    if not index:
        return set()
    found: set[int] = set()
    for key in index_keys(normalized):
        found |= index.get(key, set())
    return found


def build_name_index(profiles: dict[int, dict]) -> dict[str, set[int]]:
    index: dict[str, set[int]] = defaultdict(set)
    for cik, profile in profiles.items():
        for name in [profile["name"], *[f["name"] for f in profile["former_names"]]]:
            for key in index_keys(normalize_issuer_name(name)):
                index[key].add(cik)
    return index


def issuer_like(profile: dict) -> bool:
    """False for an EDGAR entity typed 'other' that never filed a 10-K, 10-Q, 20-F or 40-F (a
    subsidiary, trust or filing agent sharing the issuer's name)."""
    return not (str(profile.get("entity_type") or "").lower() == "other" and not profile.get("periodic_dates"))


def profile_names(profile: dict) -> list[str]:
    return [n for n in {normalize_issuer_name(x) for x in [profile["name"], *[f["name"] for f in profile["former_names"]]]} if n]


def _shift(day: str, days: int) -> str:
    return (pd.Timestamp(day) + pd.Timedelta(days=days)).date().isoformat()


def profile_name_spans(profile: dict) -> list[tuple[str, str, str]]:
    """(normalized name, from, to) for the current and each former EDGAR name.

    EDGAR's 'from' dates are unreliable: the earliest recorded name can be dated from a late filing
    (MICROSTRATEGY INC from 2018-10-25), and a name can start years after the one before it ended
    (eXp World Holdings from 2025-02-19 after eXp Realty International ended 2016-04-27). The names
    are chained in order of their end dates: each starts where the previous one ended, and the
    earliest reaches back to the CIK's first filing (from the submissions' page index, fetched or
    not), never before it, as a holding company formed in 2013 did not bear its predecessor's name
    in 2010.
    """
    spans = profile.get("_name_spans")
    if spans is None:
        former = sorted((f for f in profile.get("former_names") or [] if f.get("name")),
                        key=lambda f: (f.get("to") or "9999-12-31", f.get("from") or ""))
        first_filed = min([q.get("from") or "" for q in profile.get("older_pages") or [] if isinstance(q, dict)]
                          + [profile.get("coverage_start") or ""], default="")
        first_filed = first_filed if first_filed and first_filed > "0000" else "0000-00-00"
        spans, previous_end = [], first_filed
        for f in former:
            begin = min(f.get("from") or previous_end, previous_end) if spans or f.get("from") else previous_end
            begin = min(begin, f.get("from") or begin)
            end_ = f.get("to") or "9999-12-31"
            spans.append((normalize_issuer_name(f["name"]), begin, end_))
            previous_end = max(previous_end, end_) if end_ != "9999-12-31" else previous_end
        current_from = max((f["to"] for f in former if f.get("to")), default="") or first_filed
        spans.insert(0, (normalize_issuer_name(profile["name"]), current_from, "9999-12-31"))
        spans = [span for span in spans if span[0]]
        profile["_name_spans"] = spans
    return spans


def name_valid(profile: dict, key: str, first: str = "", last: str = "", exact: bool = False,
               slack: int = NAME_SLACK_DAYS, reorder: bool = False) -> bool:
    """The CIK carried a name matching ``key`` at some time in first..last (give or take ``slack`` days);
    ``reorder`` (ticker evidence) admits any word order (``names_match``)."""
    lo = _shift(first, -slack) if first else "0000-00-00"
    hi = _shift(last or first, slack) if (last or first) else "9999-12-31"
    if not reorder and not exact:
        # A CIK one of whose names matches as written may match its other names in any order: EDGAR
        # stored Bob Evans Farms as 'EVANS BOB FARMS INC' until 2011.
        reorder = any(names_match(key, n) for n, _, _ in profile_name_spans(profile))
    compact, tokens = key.replace(" ", ""), sorted(key.split())
    same = lambda n: n.replace(" ", "") == compact or ((reorder_allowed(tokens) or (reorder and len(tokens) >= 2))
                                                       and sorted(n.split()) == tokens)
    return any((same(n) if exact else names_match(key, n, reorder)) and a <= hi and b >= lo
               for n, a, b in profile_name_spans(profile))


def name_valid_throughout(profile: dict, key: str, first: str, last: str, slack: int = NAME_SLACK_DAYS,
                          reorder: bool = False) -> bool:
    """The CIK bore a name matching ``key`` both when the listing was first and last seen."""
    return name_valid(profile, key, first, first, slack=slack, reorder=reorder) and \
        name_valid(profile, key, last, last, slack=slack, reorder=reorder)


def active_during(profile: dict, first: str, last: str, slack_days: int = 400) -> bool:
    """The CIK filed something within ``slack_days`` of first..last (or its fetched filings start after that)."""
    dates = profile.get("filing_dates") or []
    if not dates:
        return False
    lo, hi = _shift(first, -slack_days), _shift(last, slack_days)
    if profile.get("coverage_start", "") > hi and profile.get("older_pages"):
        return True  # the recent block starts after the listing; older pages hold that period
    i = bisect.bisect_left(dates, lo)
    return i < len(dates) and dates[i] <= hi


def periodic_during(profile: dict, first: str, last: str, slack_days: int = 400) -> bool:
    dates = profile.get("periodic_dates") or []
    lo, hi = _shift(first, -slack_days), _shift(last, slack_days)
    i = bisect.bisect_left(dates, lo)
    return i < len(dates) and dates[i] <= hi


def reported_during(profile: dict, first: str, last: str, slack_days: int = 400) -> bool:
    """A periodic report within ``slack_days`` of first..last; or, for a CIK that files periodic reports
    and whose fetched filings start after that, presumed (its older pages hold that period)."""
    if periodic_during(profile, first, last, slack_days):
        return True
    return bool(profile.get("periodic_dates")) and bool(profile.get("older_pages")) and \
        profile.get("coverage_start", "") > _shift(last, slack_days)


def covers(profile: dict, first: str, last: str) -> bool:
    lo, hi = activity_window(profile)
    return bool(lo) and lo <= first and hi >= last


def resolve_listing(symbol: str, name: str, cands: Candidates, profiles: dict[int, dict],
                    name_index: dict[str, set[int]], first: str = "", last: str = "",
                    lookup: dict[str, set[int]] | None = None, truncated: bool = False) -> tuple[int | None, str]:
    """(cik, how) for one snapshot (symbol, name) seen first..last.

    Names are compared with the EDGAR name the CIK carried at the time (current and
    former names with their dates), in any word order. how: name+ticker (a ticker
    candidate with that name), name+lookup (that candidate came from SEC's name list: an
    issuer filing during the listing; for a name of one token or under six letters, the
    only one filing periodic reports then), name+lookup_nonfiler (a distinctive name whose
    only EDGAR entity files no periodic reports, such as a bank filing with the FDIC),
    name_only (the only CIK, among every EDGAR entity of that name, filing during the
    listing), name_profiled (the name is too common in EDGAR, but only one profiled
    company bore it and filed then), ticker_only (one mapped ticker candidate filing then,
    names differ), ambiguous (split later by date: two candidates, or a name the CIK bore
    for only part of the listing), unresolved. A non-issuer (entity type 'other' with no
    periodic report) never beats an issuer. ``truncated`` marks a name cut short in its
    source file: it supports no name-derived match.
    """
    key = normalize_issuer_name(name)
    compact = key.replace(" ", "")
    options = cands.get(symbol)
    ticker_ciks = [c for c in options if c in profiles]
    same_name = names_in(name_index, key) | names_in(lookup, key)
    # A candidate from SEC's name list only is accepted when it is an issuer filing periodic reports
    # during the listing (for a one-token or short name, the only such CIK), or, for a distinctive
    # name with no competitor, an entity that files no periodic reports with SEC (a bank filing with
    # the FDIC, such as Signature Bank): 'name+lookup_nonfiler'. Never for a truncated name.
    lookup_only = lambda c: options[c] <= {"sec_cik_lookup"}
    reporting = lambda c: not first or active_during(profiles[c], first, last) or reported_during(profiles[c], first, last)
    distinct = distinctive_name(key)
    short_ok = not truncated and len(compact) >= 2 and compact not in GENERIC_FIRST
    name_ok = not truncated and (distinct or short_ok)

    def lookup_strength(c) -> int:
        profile = profiles[c]
        if truncated:
            return 0
        if issuer_like(profile):
            if distinct and reporting(c):
                return 2
            if short_ok and (not first or reported_during(profile, first, last)):
                return 2
            return 0
        return 1 if distinct else 0

    # A name in another word order counts only for a CIK with ticker evidence (or initials, which any
    # match admits): 'First Bank' found Bank First Corp through SEC's name list alone.
    named = [c for c in ticker_ciks if name_valid(profiles[c], key, first, last, reorder=not lookup_only(c))]
    mapped_named = [c for c in named if not lookup_only(c)]
    if any(issuer_like(profiles[c]) and (not first or active_during(profiles[c], first, last))
           for c in ticker_ciks if not lookup_only(c)):  # a sub-entity listing the ticker never beats the issuer
        mapped_named = [c for c in mapped_named if issuer_like(profiles[c])]
    strong = [c for c in named if lookup_only(c) and lookup_strength(c) == 2]
    weak = [c for c in named if lookup_only(c) and lookup_strength(c) == 1]
    if not mapped_named and not distinct and len(strong) > 1:
        return None, "ambiguous"
    mapped_any = [c for c in ticker_ciks if not lookup_only(c)]
    if not mapped_any and not strong and len(weak) == 1 and not any(
            c in profiles and issuer_like(profiles[c]) for c in same_name - set(weak)):
        return weak[0], "name+lookup_nonfiler"
    by_name = mapped_named + strong
    if len(by_name) > 1:  # a mapped ticker beats a name-list hit
        by_name = mapped_named if len(mapped_named) == 1 else by_name
    if len(by_name) > 1 and first:  # a name valid over the whole listing beats one valid over part of it
        whole = [c for c in by_name if name_valid_throughout(profiles[c], key, first, last, reorder=not lookup_only(c))]
        by_name = whole if len(whole) == 1 else by_name
    if len(by_name) > 1 and first:  # SEC's current holder of the ticker, if it reported during the listing
        holder = [c for c in by_name if symbol in profiles[c]["tickers"] and reported_during(profiles[c], first, last)]
        by_name = holder if len(holder) == 1 else by_name
    if len(by_name) > 1:  # then exact names; then filing at the time
        exact = [c for c in by_name if name_valid(profiles[c], key, first, last, exact=True)] or by_name
        active = [c for c in exact if not first or active_during(profiles[c], first, last)] or exact
        by_name = active
    if len(by_name) == 1 and first:
        # A same-name CIK that filed during only part of the listing too: a holding-company
        # reorganisation (old and new CIK share name and ticker); split by date later. So is a
        # name that was the CIK's EDGAR name for only part of the listing.
        partial = [c for c in same_name - set(by_name) if c in profiles and active_during(profiles[c], first, last)
                   and name_valid(profiles[c], key, first, last) and issuer_like(profiles[c])]
        if partial and not covers(profiles[by_name[0]], first, last):
            return None, "ambiguous"
        lo, hi = activity_window(profiles[by_name[0]])
        if lookup_only(by_name[0]) and lo and (lo > _shift(first, LOOKUP_COVER_SLACK_DAYS)
                                               or hi < _shift(last, -LOOKUP_COVER_SLACK_DAYS)):
            # Found through SEC's name list alone and active over only part of the listing (by more
            # than a year: a listing outlives the last filing by months in a bankruptcy): split by
            # date ('First Bank', FRBA 2014-2024, against FIRSTBANK CORP, which stopped filing in 2014).
            return None, "ambiguous"
        rivals = partial or [c for c in mapped_any if c != by_name[0] and issuer_like(profiles[c])
                             and active_during(profiles[c], first, last)]
        if not name_valid_throughout(profiles[by_name[0]], key, first, last, reorder=not lookup_only(by_name[0])) \
                and (lookup_only(by_name[0]) or rivals):
            return None, "ambiguous"  # split by date; a sole mapped holder with no rival keeps the pair
    if len(by_name) == 1:
        return by_name[0], "name+lookup" if lookup_only(by_name[0]) else "name+ticker"
    if len(by_name) > 1:
        return None, "ambiguous"
    live = [c for c in same_name if c in profiles and (not first or active_during(profiles[c], first, last))
            and name_valid(profiles[c], key, first, last) and issuer_like(profiles[c])]
    if len(live) > 1 and first:  # a parent filing periodic reports beats a same-name subsidiary
        reporting_now = [c for c in live if reported_during(profiles[c], first, last)]
        live = reporting_now if len(reporting_now) == 1 else live
    if not distinct and first:  # a one-token or short name needs an issuer reporting during the listing
        live = [c for c in live if reported_during(profiles[c], first, last)]
    if name_ok and len(live) == 1 and first and not name_valid_throughout(profiles[live[0]], key, first, last):
        return None, "ambiguous"
    if name_ok and 0 < len(same_name) <= LOOKUP_MAX_CIKS and len(live) == 1 and all(c in profiles for c in same_name):
        return live[0], "name_only"
    if name_ok and len(same_name) > LOOKUP_MAX_CIKS and len(live) == 1:
        return live[0], "name_profiled"
    mapped = [c for c in ticker_ciks if not lookup_only(c)]
    if any(issuer_like(profiles[c]) for c in mapped):
        mapped = [c for c in mapped if issuer_like(profiles[c])]
    if len(mapped) == 1 and (not first or active_during(profiles[mapped[0]], first, last)):
        return mapped[0], "ticker_only"
    if len(mapped) > 1 or (name_ok and len(live) > 1):
        return None, "ambiguous"
    return None, "unresolved"


def activity_window(profile: dict, today: str | None = None) -> tuple[str, str]:
    """(from, to) dates during which the CIK plausibly had listed stock.

    From its first filing (or 100 days before its first periodic report, if earlier);
    open at the start when older submission pages were not fetched. To 120 days after
    its last periodic report or 30 days after its last filing, whichever is first;
    open at the end while it still reports (last periodic report within 150 days).
    """
    filed = profile.get("filing_dates") or []
    periodic = profile.get("periodic_dates") or []
    if not filed:
        return ("", "")
    today = today or datetime.now(timezone.utc).date().isoformat()
    shift = lambda day, days: (pd.Timestamp(day) + pd.Timedelta(days=days)).date().isoformat()
    if profile.get("older_pages") and not profile.get("older_pages_fetched"):
        start = "0000-00-00"
    else:
        start = min(filed[0], shift(periodic[0], -100)) if periodic else filed[0]
    if (periodic or filed)[-1] >= shift(today, -REPORTING_NOW_DAYS):
        end = "9999-12-31"
    else:
        end = min(shift(filed[-1], 30), shift(periodic[-1], 120)) if periodic else shift(filed[-1], 30)
    return (start, end)


def resolve_by_date(symbol: str, name: str, dates: list[str], cands: Candidates, profiles: dict[int, dict],
                    name_index: dict[str, set[int]], lookup: dict[str, set[int]] | None = None,
                    exits: dict[int, str] | None = None, truncated: bool = False,
                    exits_checked: bool = False) -> dict[str, int]:
    """For a pair whose name matches several CIKs (a holding-company reorganisation reuses name and
    ticker), or a CIK for only part of the pair's dates, assign each snapshot date to the candidate
    that bore the name on that date and whose activity window holds the date.

    A CIK's window ends at the filing date of the Form 25 that ended its Nasdaq common listing
    (``exits``): the exchange files it once trading has stopped, so later rows of that ticker belong
    to a successor. Where two windows overlap the incumbent (the CIK assigned the dates before, else
    the earlier window start) keeps the date, unless
    both span every date (two unrelated companies), which stays unassigned. A date with one
    candidate accepts a name within NAME_SLACK_DAYS of its EDGAR dates; competing candidates are
    judged within DATED_NAME_SLACK_DAYS.
    """
    if exits and not exits_checked:
        # A CIK's Form 25 ends its window only for a ticker it held before the filing: old IAC
        # (891103) handed IAC to the new IAC on 2020-06-30 and went on as Match Group, taking MTCH
        # from Match Group Inc (1575189), whose own Form 25 of 2020-07-01 ends its MTCH dates. Here
        # judged on this pair's dates; ``exits_checked``: the caller judged it on every row of the
        # symbol (Mylan Inc held MYL under its own name, not under 'Mylan N.V.').
        base = resolve_by_date(symbol, name, dates, cands, profiles, name_index, lookup, None, truncated)
        exits = {c: cut for c, cut in exits.items() if any(c == base.get(day) for day in dates if day <= cut)}
    key = normalize_issuer_name(name)
    options = {c for c in cands.get(symbol) if c in profiles}
    if not truncated and (distinctive_name(key) or len(key.replace(" ", "")) >= 6):
        options |= {c for c in names_in(name_index, key) | names_in(lookup, key) if c in profiles}
    options = {c for c in options if name_valid(profiles[c], key, dates[0], dates[-1]) and issuer_like(profiles[c])}
    windows = {}
    for c in options:
        lo, hi = activity_window(profiles[c])
        cut = (exits or {}).get(c)
        if cut and lo and cut >= lo:
            hi = min(hi, cut)
        windows[c] = (lo, hi)
    span = lambda c: windows[c][0] <= dates[0] and windows[c][1] >= dates[-1]
    out, holder = {}, None
    for day in dates:
        inside = [c for c, (lo, hi) in windows.items() if lo and lo <= day <= hi
                  and name_valid(profiles[c], key, day, day, slack=NAME_SLACK_DAYS)]
        if len(inside) > 1:
            inside = [c for c in inside if name_valid(profiles[c], key, day, day, slack=DATED_NAME_SLACK_DAYS)]
        if len(inside) == 1:
            out[day] = holder = inside[0]
        elif inside and not all(span(c) for c in inside):
            # The CIK that held the ticker on the dates before keeps it until its window ends (its
            # own Form 25): Bridge Bancorp (846617, filing since 1988) took the name Dime Community
            # Bancshares at the 2021-01-29 merger, and old Dime (1005409) listed DCOM until then
            # (Match Group's MTCH, Xenith's XBKS and SharpLink's SBET likewise). With no holder yet,
            # the earlier window start.
            if holder not in inside:
                holder = min(inside, key=lambda c: (windows[c][0], c))
            out[day] = holder
    return out


def load_cik_lookup(*, offline: bool = False) -> dict[str, set[int]]:
    """normalized name -> CIKs from SEC's list of every EDGAR entity name (current and former)."""
    path = SEC_RAW / "cik-lookup-data.txt.gz"
    if offline and not path.exists():
        return {}
    text = common.cached_get(CIK_LOOKUP_URL, path, source="sec_archives", headers=common.sec_headers(),
                             limiter=SEC_LIMITER, timeout=300).decode("latin-1")
    index: dict[str, set[int]] = defaultdict(set)
    for line in text.splitlines():
        name, _, rest = line.rstrip().rstrip(":").rpartition(":")
        if rest.isdigit() and name:
            for key in index_keys(normalize_issuer_name(name)):
                index[key].add(int(rest))
    return index


MATCH_RANK = {"name+ticker": 0, "name+lookup": 1, "name_only": 2, "name_profiled": 2, "name+dated": 3,
              "successor": 3, "name+lookup_nonfiler": 4, "ticker_only": 4}
# Families that list only part of Nasdaq: a symbol missing from them is no evidence of absence.
PARTIAL_FAMILIES = {"repo_screener_300M"}
PARTIAL_SUFFIX = ":partial_files"  # snapshot_dates key of a full family's partial files (partial_snapshot_dates)
FAMILY_ORDER = {"repo_symdir": 0, "wayback_symdir": 1, "wayback_companylist": 2, "repo_screener_300M": 3}
# Rows of a CIK this long after the filing of its terminal Form 25 are not its own (the exchange
# files the 25 once trading has stopped; a few lists lag by days).
EXIT_GRACE_DAYS = 10


def presence_by_date(snapshots: pd.DataFrame) -> dict[str, set[str]]:
    """date -> every symbol in that day's snapshots (any family, common or not, with or without a
    last sale)."""
    return {day: set(symbols) for day, symbols in snapshots.groupby("date")["symbol"]}


# Classes that name no class letter: a row so named carries the letter of the rows around it
# ('1-800-FLOWERS.COM Inc. Common Stock' between 'Class A Common Stock' rows is class A).
GENERIC_CLASSES = {"COMMON", "ADS"}
MIN_CLASS_DATES = 2  # a class letter seen on fewer dates of a run does not split it


def run_class(classes: list[str]) -> str:
    """The class of a run of rows: its most frequent class letter (or ticker key), else ADS or COMMON."""
    specific = Counter(c for c in classes if c not in GENERIC_CLASSES)
    if specific:
        return min(specific.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    generic = Counter(classes)
    return "ADS" if generic["ADS"] > generic["COMMON"] else "COMMON"


def class_segments(days: list[str], class_of_day: dict[str, str], min_dates: int = MIN_CLASS_DATES) -> list[tuple[int, int, str]]:
    """(first index, end index, class) pieces of one run whose class letter changes for good (GOOG:
    class A until 2014-04-02, class C after). Generic days join the piece they fall in; a letter seen
    on fewer than ``min_dates`` dates is absorbed by its neighbour, so a stray name does not split."""
    letters = [(i, class_of_day[d]) for i, d in enumerate(days) if class_of_day[d] not in GENERIC_CLASSES]
    if not letters:
        return [(0, len(days), run_class([class_of_day[d] for d in days]))]
    pieces: list[list] = []  # [letter, first index, n dates]
    for i, letter in letters:
        if pieces and pieces[-1][0] == letter:
            pieces[-1][2] += 1
        else:
            pieces.append([letter, i, 1])
    while len(pieces) > 1 and min(p[2] for p in pieces) < min_dates:
        k = min(range(len(pieces)), key=lambda j: (pieces[j][2], j))
        into = k - 1 if k > 0 else k + 1
        pieces[into][2] += pieces[k][2]
        pieces[into][1] = min(pieces[into][1], pieces[k][1])
        del pieces[k]
        merged = [pieces[0]]
        for piece in pieces[1:]:
            if piece[0] == merged[-1][0]:
                merged[-1][2] += piece[2]
            else:
                merged.append(piece)
        pieces = merged
    bounds = [0] + [p[1] for p in pieces[1:]] + [len(days)]
    return [(bounds[k], bounds[k + 1], p[0]) for k, p in enumerate(pieces)]


def build_intervals(rows: pd.DataFrame, snapshot_dates: dict[str, list[str]],
                    present: dict[str, set[str]] | None = None, split_classes: set[int] | None = None) -> pd.DataFrame:
    """Intervals of each (cik, ticker) from resolved snapshot rows of every family.

    ``rows`` has symbol, date, cik, share_class, family, source_url, how, name.
    ``snapshot_dates`` maps each family to its sorted dates and ``present`` maps a date to the
    symbols in that day's snapshots (default: the symbols in ``rows``; company-list rows without a
    last sale count as present). A run breaks only when, on the union of the full-list families'
    dates, the symbol is missing from two snapshots between two observations, or another CIK holds the
    ticker in between. The class is assigned to each run afterwards (``run_class``: names that state
    no class do not break a run); for the CIKs in ``split_classes`` (multi-class issuers, whose
    security depends on the class) a run is split where its class letter changes for good. A stretch
    with no snapshot at all (no source covers it) never breaks a run; the longest such stretch inside
    the interval is ``coverage_gap_days``.
    """
    full = sorted({d for family, days in snapshot_dates.items() if not is_partial_family(family) for d in days})
    every = sorted({d for days in snapshot_dates.values() for d in days})
    if present is None:
        present = presence_by_date(rows)
    split_classes = split_classes or set()
    out = []
    ordered = rows.sort_values(["symbol", "date"])
    columns = ["date", "cik", "share_class", "family", "source_url", "how", "name"]
    for symbol, group in ordered.groupby("symbol", sort=False):
        holders: dict[str, set] = defaultdict(set)
        by_key: dict[int, list] = defaultdict(list)
        for record in zip(*(group[c].tolist() for c in columns)):
            holders[record[0]].add(int(record[1]))
            by_key[int(record[1])].append(record)
        for cik, records in by_key.items():
            days = sorted({r[0] for r in records})
            runs, begin = [], 0
            for i in range(1, len(days)):
                a, b = days[i - 1], days[i]
                between = full[bisect.bisect_right(full, a):bisect.bisect_left(full, b)]
                absent = sum(1 for u in between if symbol not in present.get(u, ()))
                other = any(holders.get(u, set()) - {cik}
                            for u in every[bisect.bisect_right(every, a):bisect.bisect_left(every, b)])
                if absent >= 2 or other:
                    runs.append(days[begin:i])
                    begin = i
            runs.append(days[begin:])
            classes_of_day: dict[str, list[str]] = defaultdict(list)
            for r in records:
                classes_of_day[r[0]].append(r[2])
            class_of_day = {d: run_class(c) for d, c in classes_of_day.items()}
            for run in runs:
                pieces = (class_segments(run, class_of_day) if cik in split_classes
                          else [(0, len(run), run_class([c for d in run for c in classes_of_day[d]]))])
                for first, stop, share_class in pieces:
                    part = run[first:stop]
                    start, end = part[0], part[-1]
                    inside = sorted((r for r in records if start <= r[0] <= end),
                                    key=lambda r: (r[0], FAMILY_ORDER.get(r[3], 9)))
                    covered = every[bisect.bisect_left(every, start):bisect.bisect_right(every, end)]
                    gap = max(((pd.Timestamp(y) - pd.Timestamp(x)).days for x, y in zip(covered, covered[1:])), default=0)
                    i, j = bisect.bisect_left(full, start), bisect.bisect_right(full, end)
                    out.append({"cik": cik, "share_class": share_class, "ticker": symbol, "start": start, "end": end,
                                "start_prev_absent": full[i - 1] if i > 0 else "",
                                "end_next_absent": full[j] if j < len(full) else "",
                                "n_snapshots": len(part), "exchange": "NASDAQ", "source": inside[0][3],
                                "source_url": inside[0][4],
                                "match": max((r[5] for r in inside), key=lambda m: MATCH_RANK.get(m, 9)),
                                "name_in_source": inside[-1][6],
                                "sources": " ".join(sorted({r[3] for r in inside})), "n_evidence_rows": len(inside),
                                "coverage_gap_days": gap})
    return pd.DataFrame(out)


# A security seen in a full-list snapshot this long after its delist date (the Form 25 filing date plus
# FORM25_EFFECTIVE_LAG_DAYS), in the run that held it at the delisting, keeps no delist date: lists lag
# a few days, and the tail of the run before that is dropped after an exit.
DELIST_TOLERANCE_DAYS = 30
# A delist date this long after the security's last Nasdaq listing is flagged for review: a long
# suspension (Nasdaq's 2025-08 batch for stocks suspended in 2024-04), or a Form 25 naming a
# predecessor CIK no longer listed (Community First Bancshares after its 2021 conversion to Affinity).
LATE_DELIST_DAYS = 365
FORM25_EFFECTIVE_LAG_DAYS = 10  # reversal_data_form25.EFFECTIVE_LAG_DAYS (Rule 12d2-2(d)(1))
# A ticker continues past a Form 25 when its CIK is seen on it CONTINUE_DAYS or more after the filing
# (the delist date plus DELIST_TOLERANCE_DAYS) before the run breaks (two full-list snapshots without
# the symbol, or another CIK on it). Sightings before that are the lag of the lists; stale copies of
# older lists are dropped on loading (stale_duplicate_dates).
CONTINUE_DAYS = FORM25_EFFECTIVE_LAG_DAYS + DELIST_TOLERANCE_DAYS


def continuation_after(obs: list[str], filed: str, days: list[str], full: set[str], present: dict[str, set[str]],
                       symbol: str, other_holder=None, grace_days: int = EXIT_GRACE_DAYS,
                       continue_days: int = CONTINUE_DAYS) -> dict:
    """How one CIK's ticker fares after a Form 25 filed on ``filed``, from its observation dates ``obs``.

    Walks every snapshot date after the last observation on or before the filing plus ``grace_days``:
    an observation extends the run, another CIK holding the ticker (``other_holder(day)``) ends it,
    and so do two full-list snapshots without the symbol. A full-list snapshot listing the symbol but
    not under this CIK (an unresolved or no-last-sale row) is neutral. Returns status 'continued',
    'ended' (the run broke first; 'handover' says whether another CIK took it) or 'open' (the
    snapshots ran out first), with ``after``, the observations after the cutoff that belong to the run
    (the tail a terminal filing drops), and ``unresolved``, the neutral full-list dates seen.
    """
    obs = sorted(obs)
    cutoff = _shift(filed, grace_days)
    horizon = _shift(filed, continue_days)
    seen = set(obs)
    i = bisect.bisect_right(obs, cutoff)
    start = obs[i - 1] if i else cutoff
    absent, after, unresolved = 0, [], 0
    result = lambda status, handover=False: {"status": status, "after": after, "unresolved": unresolved,
                                             "handover": handover, "last_before": obs[i - 1] if i else ""}
    for day in days[bisect.bisect_right(days, start):]:
        if day in seen:
            absent = 0
            if day > cutoff:
                after.append(day)
                if day >= horizon:
                    return result("continued")
            continue
        if other_holder is not None and other_holder(day):
            return result("ended", handover=True)
        if day in full:
            if symbol in present.get(day, ()):
                unresolved += 1
            else:
                absent += 1
                if absent >= 2:
                    return result("ended")
    return result("open")


def ticker_conflicts(rows: pd.DataFrame) -> list[tuple]:
    """(ticker, cik_a, cik_b, overlap_start, overlap_end) where two CIKs' rows of one ticker overlap in
    time: the first-to-last date spans of the two CIKs on the ticker intersect (two families
    disagreeing, or assignments alternating between two CIKs)."""
    found = []
    hulls = rows.groupby(["symbol", "cik"])["date"].agg(["min", "max"]).reset_index()
    for ticker, group in hulls.groupby("symbol"):
        if len(group) < 2:
            continue
        spans = sorted(zip(group["min"], group["max"], group["cik"].astype(int)))
        for i, (s1, e1, c1) in enumerate(spans):
            for s2, e2, c2 in spans[i + 1:]:
                if s2 > e1:
                    break
                found.append((ticker, c1, c2, max(s1, s2), min(e1, e2)))
    return found


def resolve_ticker_conflicts(rows: pd.DataFrame, profiles: dict[int, dict],
                             exits: dict[int, str] | None = None) -> tuple[pd.DataFrame, int]:
    """One CIK per (ticker, date). Where two CIKs' rows of one ticker overlap in time:

    1. over both spans, a row is dropped when its CIK could not hold the ticker on that date (its
       EDGAR name not exactly the listed name then, or the date after its terminal Form 25) but the
       other CIK could;
    2. if the spans still intersect, the CIK with more rows it could hold inside the intersection
       (then more name+ticker rows, then more rows) keeps it, and the other's rows there are dropped.

    Dropped rows get how 'conflict'.
    """
    rows = rows.copy()
    exits = exits or {}
    dropped = 0
    cache: dict[tuple, bool] = {}

    def could_hold(cik, name, day, exact=True) -> bool:
        key = (cik, name, day, exact)
        if key not in cache:
            profile = profiles.get(int(cik))
            cut = _shift(exits[int(cik)], EXIT_GRACE_DAYS) if int(cik) in exits else "9999-12-31"
            cache[key] = bool(profile) and day <= cut and name_valid(
                profile, normalize_issuer_name(name), day, day, exact=exact, slack=DATED_NAME_SLACK_DAYS)
        return cache[key]

    for ticker, a, b, lo, hi in ticker_conflicts(rows):
        pair = rows[(rows["symbol"] == ticker) & rows["cik"].isin([a, b])]
        if pair["cik"].nunique() < 2:
            continue  # settled by an earlier conflict of this ticker
        drop = [i for i, c, n, d in zip(pair.index, pair["cik"], pair["name"], pair["date"])
                if not could_hold(c, n, d) and could_hold(b if int(c) == a else a, n, d)]
        rows.loc[drop, ["cik", "how"]] = [None, "conflict"]
        dropped += len(drop)
        pair = rows[(rows["symbol"] == ticker) & rows["cik"].isin([a, b])]
        hull = pair.groupby("cik")["date"].agg(["min", "max"])
        if len(hull) < 2 or hull["min"].max() > hull["max"].min():
            continue
        lo, hi = hull["min"].max(), hull["max"].min()
        inside = pair[(pair["date"] >= lo) & (pair["date"] <= hi)]

        def score(cik):
            own = inside[inside["cik"] == cik]
            valid = sum(could_hold(cik, n, d, exact=False) for n, d in zip(own["name"], own["date"]))
            return (valid, int((own["how"] == "name+ticker").sum()), len(own), -int(cik))

        loser = min((a, b), key=score)
        hit = inside.index[inside["cik"] == loser]
        rows.loc[hit, ["cik", "how"]] = [None, "conflict"]
        dropped += len(hit)
    return rows, dropped


def drop_rows_after_exit(rows: pd.DataFrame, exits: dict[int, str], successors: dict[tuple[int, str], int],
                         covered: dict[int, set[str]] | None = None, *, days: list[str] | None = None,
                         full: set[str] | None = None, present: dict[str, set[str]] | None = None,
                         grace_days: int = EXIT_GRACE_DAYS) -> tuple[pd.DataFrame, dict]:
    """The tail of a ticker's run after the Form 25 that ended its CIK's Nasdaq listing belongs to
    someone else: rows of the run dated more than ``grace_days`` after the filing go to the
    successor that took the ticker, when there is one ('successor'), else to nobody ('after_exit').

    Only the tickers the filing covered (``covered``: its tickers before and ended, when it names any)
    lose rows, and only rows of the run that held the ticker at the filing (``continuation_after``):
    rows after the run broke (two full-list snapshots without the symbol, or another holder) are a
    relisting and stay. A run that continues past the filing contradicts the exit; its rows stay too.
    ``days``/``full``/``present`` are the snapshot dates, the full-list dates and the symbols per date
    (default: from ``rows``). Returns the rows and counts: dropped, moved, kept_after_exit,
    contradicted (a list of (cik, ticker))."""
    rows = rows.copy()
    days = days if days is not None else sorted(rows["date"].unique())
    full = full if full is not None else set(days)
    present = present if present is not None else presence_by_date(rows)
    live = rows[rows["cik"].notna()]
    holders: dict[tuple[str, str], set[int]] = defaultdict(set)
    for symbol, day, cik in zip(live["symbol"], live["date"], live["cik"]):
        holders[(symbol, day)].add(int(cik))
    counts = {"dropped": 0, "moved": 0, "kept_after_exit": 0, "contradicted": []}
    mine = live[live["cik"].astype(int).isin(set(exits))]
    for (cik, symbol), group in mine.groupby([mine["cik"].astype(int), "symbol"]):
        filed = exits[cik]
        if covered and covered.get(cik) and symbol not in covered[cik]:
            continue
        obs = sorted(group["date"].unique())
        cutoff = _shift(filed, grace_days)
        if obs[0] > cutoff:
            counts["kept_after_exit"] += len(group)
            continue  # first seen after the filing: a new listing
        fate = continuation_after(obs, filed, days, full, present, symbol,
                                  lambda day: bool(holders.get((symbol, day), set()) - {cik}), grace_days)
        late = group[group["date"] > cutoff]
        if fate["status"] == "continued":
            counts["contradicted"].append((cik, symbol))
            counts["kept_after_exit"] += len(late)
            continue
        tail = late.index[late["date"].isin(set(fate["after"]))]
        counts["kept_after_exit"] += len(late) - len(tail)
        successor = successors.get((cik, symbol))
        if successor:
            rows.loc[tail, ["cik", "how"]] = [successor, "successor"]
            counts["moved"] += len(tail)
        else:
            rows.loc[tail, ["cik", "how"]] = [None, "after_exit"]
            counts["dropped"] += len(tail)
    return rows, counts


def form25_cuts(form25: pd.DataFrame | None) -> dict[int, str]:
    """cik -> filing date of its latest Form 25 removing common equity from Nasdaq, whatever the
    classification (an issuer withdrawal before any listing aside). Name-shared tickers are split
    by date at it, so a same-name successor takes the dates after it (Mylan N.V. after Mylan Inc).
    It depends only on the filings and their class, never on the step-3 exit decision, which itself
    reads the rows resolved here."""
    if form25 is None or form25.empty:
        return {}
    kinds = form25["class_kind"] if "class_kind" in form25 else pd.Series("common", index=form25.index)
    rows = form25[kinds.isin(["common", "ads"]) & (form25["classification"] != "unlisted_withdrawal")]
    return rows.groupby("subject_cik")["filing_date"].max().to_dict()


def listed_past_delisting(spans: list[tuple[str, str]], delist_date: str, full: list[str],
                          tolerance_days: int = DELIST_TOLERANCE_DAYS) -> str:
    """'' when a security's snapshot intervals (start, end) respect its delist date; otherwise why not.

    The intervals that began by the delist date must end by it plus ``tolerance_days``, and an
    interval that begins after it counts as a relisting only when at least two full-list snapshots
    (``full``, sorted) without the security lie between the two (the evidence that it left; WW's new
    stock listed 19 days after the Form 25 of its old one, two monthly lists after its last sighting).
    A security with intervals none of which began by the delist date was not listed when the Form 25
    was filed, so the filing gives it no delist date."""
    limit = _shift(delist_date, tolerance_days)
    spans = sorted((s, e) for s, e in spans if s and e)
    held = [e for s, e in spans if s <= delist_date]
    last = max(held) if held else ""
    if spans and not held:
        # A Form 25 filed before the first listing ended nothing (ShiftPixy withdrew its approved Nasdaq
        # registration in 2017-02, four months before its shares first traded there).
        return f"no listing on or before it (first listed {spans[0][0]})"
    if last > limit:
        return f"listed through {last} in the interval holding the delisting"
    later = [s for s, _ in spans if s > delist_date]
    if later:
        first = min(later)
        lo = last or delist_date
        between = bisect.bisect_left(full, first) - bisect.bisect_right(full, lo)
        if between < 2:
            return f"listed again from {first} with {max(between, 0)} full-list snapshot(s) without it since {lo}"
    return ""


def elsewhere_one_row_intervals(intervals: pd.DataFrame, profiles: dict[int, dict]) -> pd.Series:
    """Intervals resting on one snapshot row matched by ticker alone (``n_snapshots`` 1, ``ticker_only``)
    of a CIK that SEC lists today only on other exchanges: Ford's F on the symbol-only 2019-06-17 file
    (SEC: NYSE). One row that matched no SEC name cannot outweigh the SEC's exchange, so the interval is
    dropped (step 12's ``doubtful_intervals`` left it out of the listed set; steps 6 and 9 read the file
    themselves). A CIK SEC lists nowhere today (delisted) keeps its interval."""
    if intervals.empty:
        return pd.Series(False, index=intervals.index)

    def elsewhere(cik) -> bool:
        exchanges = [str(e).upper() for e in (profiles.get(int(cik)) or {}).get("exchanges", []) if e]
        return bool(exchanges) and "NASDAQ" not in exchanges

    one_row = pd.to_numeric(intervals["n_snapshots"], errors="coerce").eq(1) & intervals["match"].eq("ticker_only")
    return one_row & intervals["cik"].map(elsewhere).astype(bool)


def detect_ticker_reuse(intervals: pd.DataFrame) -> dict[str, list[int]]:
    """Tickers held by more than one CIK over the observed intervals."""
    held = intervals.dropna(subset=["cik"]).groupby("ticker")["cik"].apply(lambda s: sorted(set(int(c) for c in s)))
    return {ticker: ciks for ticker, ciks in held.items() if len(ciks) > 1}


def security_id(cik: int, share_class: str, multi_class: bool) -> str:
    return f"{int(cik)}" if not multi_class else f"{int(cik)}.{share_class}"


# ------------------------------------------------------------------ build

def fetch_profiles(ciks: set[int], *, offline: bool = False) -> tuple[dict[int, dict], list[int]]:
    ordered = sorted(ciks)
    found = parallel_map(lambda cik: full_profile(cik, offline=offline), ordered, label="submissions")
    profiles = {cik: profile for cik, profile in zip(ordered, found) if profile is not None}
    return profiles, [cik for cik, profile in zip(ordered, found) if profile is None]


def build(offline: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, dict, pd.DataFrame, pd.DataFrame]:
    form25 = read_form25(FORM25)
    exits = terminal_exits(form25)
    cuts = form25_cuts(form25)
    sec_current = fetch_sec_tickers_exchange(offline=offline)
    cands = collect_candidates(sec_current, form25)
    snapshots = load_listing_snapshots()
    stale = stale_duplicate_dates(snapshots)
    if stale:  # stale copies of an older file: neither presence nor absence evidence
        drop = pd.Series(list(zip(snapshots["source"], snapshots["date"])), index=snapshots.index).isin(set(stale))
        snapshots = snapshots[~drop]
    uncorroborated = uncorroborated_sightings(snapshots)  # NYSE/OTC names in company lists, stale catalogs
    dropped_sightings = snapshots[uncorroborated]
    snapshots = snapshots[~uncorroborated]
    snapshots = fill_symbol_only_names(snapshots)
    listings = common_snapshot_rows(snapshots)
    partial_files = partial_snapshot_dates(listings)
    prices = price_file_ranges()
    print(f"snapshots: {snapshots['date'].nunique()} dates, {len(listings)} common rows, "
          f"{listings['symbol'].nunique()} symbols; price files: {len(prices)}", flush=True)

    price_tickers = {t.split("_")[0] for t in prices["ticker"]}
    wanted = set()
    form25_ciks = set(form25["subject_cik"].astype(int)) if form25 is not None else set()
    wanted |= form25_ciks
    for ticker in price_tickers | set(listings["symbol"]):
        wanted |= set(cands.get(ticker))
    print(f"CIKs to profile: {len(wanted)} (form25 {len(form25_ciks)})", flush=True)
    profiles, missing = fetch_profiles(wanted, offline=offline)
    print(f"profiles: {len(profiles)}, not found at SEC: {len(missing)}", flush=True)
    # Current SEC tickers of every profiled CIK are candidates too.
    for cik, profile in profiles.items():
        for ticker in profile["tickers"]:
            cands.add(ticker, cik, "sec_submissions_tickers")
    name_index = build_name_index(profiles)

    # Resolve each distinct (symbol, name) once, over the dates it was seen.
    lookup = load_cik_lookup(offline=offline)
    pairs = listings.groupby(["symbol", "name"]).agg(min=("date", "min"), max=("date", "max"),
                                                    truncated=("name_truncated", "all")).reset_index()
    truncated_pairs = {(p.symbol, p.name) for p in pairs.itertuples() if p.truncated}
    resolve_all = lambda: {(p.symbol, p.name): resolve_listing(p.symbol, p.name, cands, profiles, name_index,
                                                               p.min, p.max, lookup, bool(p.truncated))
                           for p in pairs.itertuples()}
    resolved = resolve_all()
    first_pass = dict(Counter(how for _, how in resolved.values()))
    # Second pass: every EDGAR entity carrying a weakly matched name (SEC's full name list) is
    # profiled, so a name is accepted only if it is unique among those filing at the time.
    span = {(p.symbol, p.name): (p.min, p.max) for p in pairs.itertuples()}
    weak = [(s, n) for (s, n), (c, how) in resolved.items()
            if how in ("ticker_only", "ambiguous", "unresolved", "name_only")
            or (how == "name+ticker" and not covers(profiles[c], *span[(s, n)]))]
    extra = set()
    for symbol, name in weak:
        if (symbol, name) in truncated_pairs:
            continue
        found = names_in(lookup, normalize_issuer_name(name))
        if 0 < len(found) <= LOOKUP_MAX_CIKS:
            for cik in found:
                cands.add(symbol, cik, "sec_cik_lookup")
            extra |= found - set(profiles)
    print(f"name lookup: {len(weak)} weak pairs, {len(extra)} new CIKs to profile", flush=True)
    more, more_missing = fetch_profiles(extra, offline=offline)
    profiles.update(more)
    missing += more_missing
    for cik, profile in more.items():
        for ticker in profile["tickers"]:
            cands.add(ticker, cik, "sec_submissions_tickers")
    name_index = build_name_index(profiles)
    resolved = resolve_all()
    listings = listings.merge(pd.DataFrame([{"symbol": s, "name": n, "cik": c, "how": h}
                                            for (s, n), (c, h) in resolved.items()]), on=["symbol", "name"], how="left")
    dated = 0
    ambiguous = [(key, group, sorted(group["date"].unique()))
                 for key, group in listings[listings["how"] == "ambiguous"].groupby(["symbol", "name"])]
    uncut = {key: resolve_by_date(key[0], key[1], days, cands, profiles, name_index, lookup, None, key in truncated_pairs)
             for key, _, days in ambiguous}
    # The CIKs that held each symbol on or before the date of their latest common Form 25, on any row
    # (resolved pairs and the uncut dated split): only their Form 25 cuts the symbol's dates.
    seen = pd.concat([listings.loc[listings["cik"].notna(), ["symbol", "date", "cik"]],
                      pd.DataFrame([(key[0], day, cik) for key, by_day in uncut.items() for day, cik in by_day.items()],
                                   columns=["symbol", "date", "cik"])], ignore_index=True)
    seen["cik"] = seen["cik"].astype(int)
    seen = seen[seen["cik"].isin(set(cuts))]
    seen = seen[seen["date"] <= seen["cik"].map(cuts)]
    held = seen.groupby("symbol")["cik"].apply(set).to_dict()
    for (symbol, name), group, days in ambiguous:
        truncated = (symbol, name) in truncated_pairs
        # A date after a CIK's latest common Form 25 goes to a rival bearing the name then, when one
        # is active; otherwise it stays with the CIK (whether the filing ended the listing is step 3's
        # call, made from these rows).
        cut_here = {c: cuts[c] for c in held.get(symbol, ())}
        by_day = {**uncut[(symbol, name)],
                  **resolve_by_date(symbol, name, days, cands, profiles, name_index, lookup, cut_here, truncated,
                                    exits_checked=True)}
        hit = group.index[group["date"].isin(by_day)]
        listings.loc[hit, "cik"] = group.loc[hit, "date"].map(by_day)
        listings.loc[hit, "how"] = "name+dated"
        dated += len(hit)
    listings["share_class"] = listings["name"].map(share_class_from_name)
    listings["family"] = listings["source"]
    snapshot_dates = {family: sorted(group["date"].unique()) for family, group in snapshots.groupby("source")}
    for family, days in partial_files.items():  # a partial file shows presence, not absence
        snapshot_dates[family] = [d for d in snapshot_dates[family] if d not in set(days)]
        snapshot_dates[family + PARTIAL_SUFFIX] = sorted(days)
    every_date = sorted({d for days in snapshot_dates.values() for d in days})
    full_dates = sorted({d for f, days in snapshot_dates.items() if not is_partial_family(f) for d in days})
    present = presence_by_date(snapshots)
    matched = listings[listings["cik"].notna()].copy()
    matched["cik"] = matched["cik"].astype(int)
    # One CIK per (ticker, date): the weaker evidence is dropped where two CIKs overlap.
    conflicts_before = len(ticker_conflicts(matched))
    conflict_rows = 0

    def settle_conflicts(frame):
        nonlocal conflict_rows
        for _ in range(3):
            if not ticker_conflicts(frame[frame["cik"].notna()]):
                break
            frame, dropped = resolve_ticker_conflicts(frame, profiles, cuts)
            conflict_rows += dropped
        return frame

    matched = settle_conflicts(matched)
    # The raw evidence step 3 reads (every resolved row, before any row is dropped for a Form 25),
    # so its exit decisions never rest on rows this step removed because of them.
    write_raw_rows(matched, snapshots)
    # Company-list rows of a CIK on a ticker it moved to another exchange, with no symbol directory to
    # confirm them, are no listing (MSG on NYSE from 2015-07-27, kept in the nasdaq.com lists to 2017).
    after_transfer = stale_after_transfer(matched, transfer_filings(form25), directory_listing(snapshots))
    after_transfer_pairs = sorted({f"{int(c)}:{t}" for c, t in zip(matched.loc[after_transfer, "cik"],
                                                                    matched.loc[after_transfer, "symbol"])})
    matched.loc[after_transfer, ["cik", "how"]] = [None, "after_transfer"]
    # The tail of a run after the Form 25 that ended the CIK's listing goes to the successor that
    # took the ticker, or to nobody; rows after the run broke are a relisting and stay.
    pre_drop = matched["cik"].copy()
    matched, exit_counts = drop_rows_after_exit(matched, exits, successor_tickers(form25), exit_coverage(form25),
                                                days=every_date, full=set(full_dates), present=present)
    # Sightings for the delist-date rule: every row as resolved, a row handed to a successor as the successor's.
    sightings = matched[["date", "symbol"]].assign(cik=matched["cik"].where(matched["how"] != "after_exit", pre_drop))
    matched = settle_conflicts(matched)
    for how in ("after_exit", "after_transfer", "conflict", "successor"):
        hit = matched.index[matched["how"] == how]
        listings.loc[hit, "how"] = how
        listings.loc[hit, "cik"] = matched.loc[hit, "cik"]
    matched = matched[matched["cik"].notna()].copy()
    matched["cik"] = matched["cik"].astype(int)
    # A row whose name carries no class (company lists, the truncated 2015 file) takes the class
    # of the nearest typed row of the same CIK and ticker (GOOG was class A until 2014, then C).
    matched["share_class"] = fill_classes(matched)

    # Multi-class: a CIK with two or more common symbols listed on the same snapshot date. Where
    # two of them share a class letter (tracking stocks, truncated names) the ticker keys the class.
    # A ticker change seen on one or two overlapping snapshots is not a second class: it takes
    # MULTI_CLASS_MIN_DATES dates with two symbols.
    same_day = matched.groupby(["cik", "date"]).agg(symbols=("symbol", "nunique"), classes=("share_class", "nunique"))
    count_dates = lambda mask: same_day[mask].reset_index().groupby("cik")["date"].nunique()
    two = count_dates(same_day["symbols"] > 1)
    multi = set(two[two >= MULTI_CLASS_MIN_DATES].index)
    clash = count_dates(same_day["symbols"] > same_day["classes"])
    collide = set(clash[clash >= MULTI_CLASS_MIN_DATES].index) & multi
    keyed = matched["cik"].isin(collide)
    matched.loc[keyed, "share_class"] = "T-" + matched.loc[keyed, "symbol"]
    intervals = build_intervals(matched, snapshot_dates, present, split_classes=multi)
    if not intervals.empty:
        intervals["security_id"] = [security_id(c, s, c in multi) for c, s in zip(intervals["cik"], intervals["share_class"])]
    doubtful = elsewhere_one_row_intervals(intervals, profiles)
    doubtful_dropped = intervals[doubtful][["security_id", "ticker", "start", "end", "source"]].to_dict("records")
    intervals = intervals[~doubtful].reset_index(drop=True)

    # SEC's current tickers and exchanges as open-ended evidence.
    current = []
    for row in sec_current:
        cik = int(row["cik"])
        if cik in profiles:
            current.append({"cik": cik, "ticker": str(row["ticker"]).upper(), "start": "", "end": row["_path"][-18:-8],
                            "exchange": str(row.get("exchange") or "").upper(), "source": "sec_company_tickers_exchange",
                            "source_url": TICKERS_EXCHANGE_URL, "share_class": "", "match": "sec",
                            "name_in_source": row.get("name", ""), "n_snapshots": 0,
                            "start_prev_absent": "", "end_next_absent": "", "sources": "sec_company_tickers_exchange",
                            "n_evidence_rows": 1, "coverage_gap_days": None})
    current = pd.DataFrame(current)
    if not current.empty:
        class_by_ticker = {}
        if not intervals.empty:
            class_by_ticker = intervals.sort_values("end").groupby(["cik", "ticker"])["share_class"].last().to_dict()
        current["share_class"] = [class_by_ticker.get((c, t), f"T-{t}" if c in multi else "COMMON")
                                  for c, t in zip(current["cik"], current["ticker"])]
        current["security_id"] = [security_id(c, s, c in multi) for c, s in zip(current["cik"], current["share_class"])]
    all_intervals = pd.concat([intervals, current], ignore_index=True)

    # Price files -> CIK: the snapshot interval covering the file's last date, else a single map candidate.
    price_map = []
    by_ticker = {t: g for t, g in intervals.groupby("ticker")} if not intervals.empty else {}
    identity = pd.read_csv(IDENTITY_FILE) if IDENTITY_FILE.exists() else pd.DataFrame(columns=["historical_ticker"])
    for row in prices.itertuples():
        base = row.ticker.split("_")[0]
        cik, how, spans = None, "unresolved", ""
        if "_" in row.ticker:
            hit = identity[identity["historical_ticker"].astype(str).str.upper() == row.ticker]
            match = re.search(r"/edgar/data/(\d+)/", str(hit["source_url"].iloc[0])) if len(hit) else None
            if match:
                cik, how = int(match.group(1)), "security_identity"
        group = by_ticker.get(base)
        if group is not None:
            # An interval reaches to the next snapshot without the ticker, or 120 days past its end.
            reach = [n if n else _shift(e, 120) for n, e in zip(group["end_next_absent"].fillna(""), group["end"])]
            group = group.assign(reach=reach)
            inside = group[(group["start"] <= row.last_date) & (group["reach"] >= row.last_date)]
            overlap = group[(group["start"] <= row.last_date) & (group["reach"] >= row.first_date)]
            spans = " ".join(str(c) for c in sorted(set(overlap["cik"].astype(int))))
            if cik is None:
                pick = inside if not inside.empty else group[group["end"] <= row.last_date].sort_values("end").tail(1)
                if pick["cik"].nunique() == 1:
                    cik, how = int(pick["cik"].iloc[0]), "snapshot_interval"
                elif pick["cik"].nunique() > 1:
                    how = "ambiguous"
        if cik is None:
            options = [c for c in cands.get(base) if c in profiles]
            current_holders = [c for c in options if base in profiles[c]["tickers"]]
            if len(options) == 1:
                cik, how = options[0], "ticker_map"
            elif len(current_holders) == 1:
                cik, how = current_holders[0], "sec_current_ticker"
            elif options:
                how = "ambiguous"
        price_map.append({"ticker": row.ticker, "file": row.file, "first_date": row.first_date,
                          "last_date": row.last_date, "rows": row.rows, "cik": cik, "how": how,
                          "ciks_on_ticker_in_file_range": spans})
    price_map = pd.DataFrame(price_map)
    price_map["cik"] = price_map["cik"].astype("Int64")

    master = build_master(profiles, all_intervals, intervals, form25, price_map, multi, missing, full_dates)
    delist_cleared = int(master["identity_notes"].str.contains("gives no delist date", regex=False).sum())
    violations = delist_date_violations(master, intervals, sightings, full_dates)
    # Per-family runs (the same rule on one family's dates) are kept as audit evidence.
    evidence = []
    for family, days in snapshot_dates.items():
        own = matched[matched["family"] == family]
        if not own.empty and not family.endswith(PARTIAL_SUFFIX):
            dates_of = {family: days, family + PARTIAL_SUFFIX: snapshot_dates.get(family + PARTIAL_SUFFIX, [])}
            evidence.append(build_intervals(own, dates_of, presence_by_date(snapshots[snapshots["source"] == family]),
                                            split_classes=multi))
    evidence = pd.concat(evidence, ignore_index=True) if evidence else pd.DataFrame(columns=INTERVAL_COLUMNS)
    if not evidence.empty:
        evidence["security_id"] = [security_id(c, k, c in multi) for c, k in zip(evidence["cik"], evidence["share_class"])]
    reuse = detect_ticker_reuse(intervals) if not intervals.empty else {}
    unresolved_pairs = listings[listings["cik"].isna()].groupby(["symbol", "name", "how"]).agg(
        first=("date", "min"), last=("date", "max"), n=("date", "size")).reset_index()
    stats = {
        "snapshot_dates": {k: len(v) for k, v in snapshot_dates.items()},
        "listing_rows_common": len(listings), "listing_symbols_common": int(listings["symbol"].nunique()),
        "listing_symbol_name_pairs": len(pairs),
        "pair_resolution_first_pass": first_pass,
        "pair_resolution": dict(Counter(how for _, how in resolved.values())),
        "lookup_weak_pairs": len(weak), "lookup_new_ciks": len(extra), "rows_resolved_by_date": dated,
        "row_resolution": dict(Counter(listings["how"])),
        "listing_rows_resolved": int(listings["cik"].notna().sum()),
        "listing_symbols_unresolved": int(unresolved_pairs["symbol"].nunique()),
        "ciks_wanted": len(wanted), "ciks_profiled": len(profiles), "ciks_not_found_at_sec": len(missing),
        "form25_ciks": len(form25_ciks), "form25_ciks_not_found": len(form25_ciks & set(missing)),
        "price_files": len(price_map), "price_file_resolution": dict(Counter(price_map["how"])),
        "price_files_unresolved": price_map.loc[price_map["cik"].isna(), "ticker"].tolist(),
        "foreign_filer": dict(Counter(master.drop_duplicates("cik")["foreign_filer"])),
        "multi_class_ciks": len(multi), "ticker_reuse_tickers": len(reuse),
        "master_rows": len(master), "master_ciks": int(master["cik"].nunique()),
        "interval_rows": len(all_intervals), "interval_rows_by_source": dict(Counter(all_intervals["source"])),
        "older_pages_fetched_for": sum(1 for p in profiles.values() if p.get("older_pages_fetched")),
        "foreign_filer_regime": regime_stats(profiles, master),
        "wayback_hook_rows": int(snapshots["source"].str.startswith("wayback").sum()),
        "truncated_name_pairs": len(truncated_pairs),
        "terminal_form25_exits_used": len(exits),
        "ticker_conflicts_before": conflicts_before, "ticker_conflict_rows_dropped": conflict_rows,
        "ticker_conflicts_after": len(ticker_conflicts(matched)),
        "rows_after_exit_dropped": exit_counts["dropped"], "rows_after_exit_to_successor": exit_counts["moved"],
        "rows_after_exit_kept_relisting": exit_counts["kept_after_exit"],
        "rows_after_transfer_dropped": len(after_transfer), "after_transfer_cik_tickers": after_transfer_pairs,
        "intervals_dropped_one_ticker_only_row_listed_elsewhere": doubtful_dropped,
        "exits_contradicted_by_continuation": [f"{c}:{t}" for c, t in exit_counts["contradicted"]],
        "intervals_bridging_coverage_gap_over_120d": int((intervals["coverage_gap_days"] > 120).sum()) if not intervals.empty else 0,
        "master_delist_dates": int(master["delist_date"].fillna("").ne("").sum()),
        "delist_dates_cleared_listed_after": delist_cleared,
        "delist_dates_over_365d_after_last_listing": int(master["identity_notes"].str.contains(
            "days after the last Nasdaq listing", regex=False).sum()),
        "master_relisted_after_delist_date": int(relisted_after_delisting(master)),
        "ticker_breaks_same_security": break_stats(intervals, full_dates, present),
        "still_listed_with_delist_date": int(violations["security_id"].nunique()),
        "still_listed_with_delist_date_rows": int(violations["n_rows_after"].sum()),
        "still_listed_with_delist_date_examples": violations.head(20).to_dict("records"),
        "partial_snapshot_files": {f: d for f, d in partial_files.items()},
        "stale_duplicate_snapshots_dropped": {f"{f} {d}": first for (f, d), first in sorted(stale.items())},
        "uncorroborated_sightings_dropped": {"rows": len(dropped_sightings),
                                             "by_family": dict(Counter(dropped_sightings["source"])),
                                             "top_symbols": dict(Counter(dropped_sightings["symbol"]).most_common(25))},
        "master_successor_links": int(master["successor_security_id"].fillna("").ne("").sum()),
        "master_transfers": int(master["transfer_date"].fillna("").ne("").sum()),
    }
    WORK.mkdir(parents=True, exist_ok=True)
    common.atomic_write(WORK / "listing_unresolved.csv", unresolved_pairs.to_csv(index=False).encode())
    common.atomic_write(WORK / "delist_date_violations.csv", violations.to_csv(index=False).encode())
    common.atomic_write(WORK / "price_file_cik_map.csv", price_map.to_csv(index=False).encode())
    common.atomic_write(WORK / "ticker_reuse.json", (json.dumps(reuse, indent=1) + "\n").encode())
    common.atomic_write(WORK / "ciks_not_found.json", (json.dumps(missing) + "\n").encode())
    common.atomic_write(WORK / "snapshot_dates.json", (json.dumps(
        {"families": snapshot_dates, "partial_families": sorted(f for f in snapshot_dates if is_partial_family(f)),
         "stale_duplicates_dropped": {f"{f} {d}": first for (f, d), first in sorted(stale.items())}},
        indent=0) + "\n").encode())
    common.atomic_write(WORK / "security_master_build_summary.json",
                        (json.dumps({k: v for k, v in stats.items()}, indent=2, default=str) + "\n").encode())
    write_files_read(profiles, form25)
    history = history_rows(profiles, {int(c) for c in master["cik"]})
    stats["periodic_form_history"] = {"rows": len(history), "ciks": int(history["cik"].nunique())}
    return master, all_intervals, stats, evidence, history


FORM25_TEXT_COLUMNS = ["subject_tickers_sec", "class_of_security", "classification", "classification_evidence",
                       "class_kind", "tickers_before", "tickers_ended", "tickers_new", "successor_tickers",
                       "subject_exit", "form", "effective_date", "filing_date", "accession", "subject_name"]


def read_form25(path: Path = FORM25) -> pd.DataFrame | None:
    """The step-3 table with text columns as '' (never NaN) and CIKs as integers."""
    if not Path(path).exists():
        return None
    frame = pd.read_csv(path, dtype={c: str for c in FORM25_TEXT_COLUMNS}, keep_default_na=False, low_memory=False)
    for column in FORM25_TEXT_COLUMNS:
        if column not in frame:
            frame[column] = ""
    frame["subject_cik"] = frame["subject_cik"].astype(int)
    if "successor_cik" not in frame:
        frame["successor_cik"] = ""
    frame["successor_cik"] = pd.to_numeric(frame["successor_cik"], errors="coerce").astype("Int64")
    return frame


def terminal_exits(form25: pd.DataFrame | None) -> dict[int, str]:
    """cik -> filing date of the latest Form 25 that ended the CIK's Nasdaq common listing."""
    if form25 is None or form25.empty:
        return {}
    if form25["subject_exit"].ne("").any():
        rows = form25[form25["subject_exit"] == "Y"]
    else:  # a table from before subject_exit existed
        rows = form25[(form25["classification"] == "common_delisting")
                      & ~form25["classification_evidence"].str.contains("stayed on Nasdaq|relisted", na=False)]
    return rows.groupby("subject_cik")["filing_date"].max().to_dict()


def exit_coverage(form25: pd.DataFrame | None) -> dict[int, set[str]]:
    """cik -> the tickers its terminal Form 25 covered: its Nasdaq tickers before the filing and those
    that ended at it (empty when the filing names none; then every ticker of the CIK is covered)."""
    if form25 is None or form25.empty or not form25["subject_exit"].eq("Y").any():
        return {}
    exits = terminal_exits(form25)
    rows = form25[form25["subject_exit"].eq("Y") & form25["filing_date"].eq(form25["subject_cik"].map(exits))]
    out: dict[int, set[str]] = defaultdict(set)
    for row in rows.itertuples():
        out[int(row.subject_cik)] |= set(row.tickers_before.split()) | set(row.tickers_ended.split())
    return dict(out)


RAW_ROWS = WORK / "ticker_rows_raw.csv.gz"


def write_raw_rows(matched: pd.DataFrame, snapshots: pd.DataFrame, path: Path | None = None) -> int:
    """Every resolved (date, symbol, cik) before any row is dropped for a Form 25, plus every other
    (date, symbol) of the snapshots with an empty cik (unresolved, non-common or without a last sale),
    so a reader knows where a symbol was listed under no CIK. Step 3 reads it."""
    path = path or RAW_ROWS
    resolved = matched.loc[matched["cik"].notna(), ["date", "symbol", "cik"]].drop_duplicates()
    resolved["cik"] = resolved["cik"].astype(int)
    seen = snapshots[["date", "symbol"]].drop_duplicates()
    bare = seen.merge(resolved[["date", "symbol"]].drop_duplicates(), how="left", indicator=True)
    bare = bare.loc[bare["_merge"] == "left_only", ["date", "symbol"]]
    frame = pd.concat([resolved, bare.assign(cik=pd.NA)], ignore_index=True)
    frame["cik"] = frame["cik"].astype("Int64")
    frame = frame.sort_values(["symbol", "date", "cik"], na_position="last")
    common.atomic_write(path, gzip.compress(frame.to_csv(index=False).encode("utf-8"), mtime=0))
    return len(frame)


def read_raw_rows(path: Path | None = None) -> pd.DataFrame | None:
    """The rows ``write_raw_rows`` wrote (date, symbol, cik as Int64), or None when absent."""
    path = Path(path or RAW_ROWS)
    if not path.exists():
        return None
    frame = pd.read_csv(path, dtype={"date": str, "symbol": str}, keep_default_na=False, na_values={"cik": [""]})
    frame["cik"] = frame["cik"].astype("Int64")
    return frame


def relisted_after_delisting(master: pd.DataFrame, days: int = DELIST_TOLERANCE_DAYS) -> int:
    """Master rows whose last listing is more than ``days`` after their delist date (a relisting)."""
    has = master["delist_date"].fillna("").ne("") & master["last_listed"].fillna("").ne("")
    later = [str(l) > _shift(d, days) for d, l in zip(master.loc[has, "delist_date"], master.loc[has, "last_listed"])]
    return int(sum(later))


def delist_date_violations(master: pd.DataFrame, intervals: pd.DataFrame, rows: pd.DataFrame, full_dates: list[str],
                           tolerance_days: int = DELIST_TOLERANCE_DAYS) -> pd.DataFrame:
    """Securities seen in a full-list snapshot more than ``tolerance_days`` after their delist date
    outside a recorded relisting: the rule every delist date must pass.

    ``rows`` (date, symbol, cik) are the snapshot rows the CIK was resolved to before any row was
    dropped for a Form 25 (rows handed to a successor count for the successor). A sighting after the
    delist date plus the tolerance is allowed only inside an interval of the same security that
    began after the delist date and that ``listed_past_delisting`` accepts as a relisting (two
    full-list snapshots without the security before it). A sighting after the security's documented
    move to another exchange (``transfer_date``) that none of its intervals holds is a company-list row
    dropped as no listing (``stale_after_transfer``: Lilis Energy, delisted 2016-08-11 and on NYSE
    American from 2017-05, still in the nasdaq.com lists to 2017-09), not a contradiction. One row per
    (security, ticker) at fault."""
    columns = ["security_id", "cik", "ticker", "delist_date", "first_seen_after", "last_seen_after", "n_rows_after",
               "reason"]
    if master.empty or intervals.empty or rows.empty:
        return pd.DataFrame(columns=columns)
    full = sorted(full_dates)
    full_set = set(full)
    snap = intervals[intervals["source"] != "sec_company_tickers_exchange"]
    by_sid = {sid: g for sid, g in snap.groupby("security_id")}
    live = rows[rows["cik"].notna() & rows["date"].isin(full_set)]
    seen = {key: sorted(g) for key, g in live.groupby([live["cik"].astype(int), "symbol"])["date"]}
    out = []
    for row in master[master["delist_date"].fillna("").ne("")].itertuples():
        own = by_sid.get(row.security_id)
        if own is None:
            continue
        spans = sorted(zip(own["start"], own["end"]))
        verdict = listed_past_delisting(spans, row.delist_date, full, tolerance_days)
        relisted = [] if verdict else [(s, e) for s, e in spans if s > row.delist_date]
        limit = _shift(row.delist_date, tolerance_days)
        moved = str(getattr(row, "transfer_date", "") or "")
        stale = lambda d: bool(moved) and d > moved and not any(s <= d <= e for s, e in spans)
        for ticker in sorted(set(own["ticker"])):
            days = seen.get((int(row.cik), ticker), [])
            late = [d for d in days[bisect.bisect_right(days, limit):]
                    if not any(s <= d <= e for s, e in relisted) and not stale(d)]
            if late:
                out.append({"security_id": row.security_id, "cik": int(row.cik), "ticker": ticker,
                            "delist_date": row.delist_date, "first_seen_after": late[0], "last_seen_after": late[-1],
                            "n_rows_after": len(late),
                            "reason": verdict or "rows outside every interval of the security (dropped after the exit)"})
    return pd.DataFrame(out, columns=columns)


def break_stats(intervals: pd.DataFrame, full_dates: list[str], present: dict[str, set[str]]) -> dict:
    """Breaks between consecutive snapshot intervals of one security on one ticker: how many, how many
    change share class, and how many have neither two full-list snapshots without the symbol nor another
    CIK's interval of the ticker in between (unexplained), with the security-days they leave uncovered."""
    out = {"breaks": 0, "class_change": 0, "unexplained": 0, "unexplained_securities": 0, "uncovered_days": 0}
    if intervals.empty:
        return out
    snap = intervals[intervals["source"] != "sec_company_tickers_exchange"]
    by_ticker = {t: g for t, g in snap.groupby("ticker")}
    hit = set()
    for (sid, ticker), group in snap.groupby(["security_id", "ticker"]):
        spans = sorted(zip(group["start"], group["end"], group["share_class"], group["cik"]))
        for (s1, e1, k1, c1), (s2, e2, k2, _) in zip(spans, spans[1:]):
            out["breaks"] += 1
            out["class_change"] += int(k1 != k2)
            between = full_dates[bisect.bisect_right(full_dates, e1):bisect.bisect_left(full_dates, s2)]
            absent = sum(1 for u in between if ticker not in present.get(u, ()))
            others = by_ticker[ticker]
            other = ((others["cik"] != c1) & (others["start"] < s2) & (others["end"] > e1)).any()
            if absent < 2 and not other:
                out["unexplained"] += 1
                out["uncovered_days"] += max((pd.Timestamp(s2) - pd.Timestamp(e1)).days - 1, 0)
                hit.add(sid)
    out["unexplained_securities"] = len(hit)
    return out


def successor_tickers(form25: pd.DataFrame | None) -> dict[tuple[int, str], int]:
    """(subject CIK, ticker) -> successor CIK, from Form 25 reorganisations with a ticker handover."""
    if form25 is None or form25.empty:
        return {}
    out = {}
    for row in form25[form25["successor_cik"].notna()].itertuples():
        for ticker in row.successor_tickers.split():
            out[(int(row.subject_cik), ticker)] = int(row.successor_cik)
    return out


GENERIC_COMMON = re.compile(r"common|ordinary|capital stock|depositary", re.I)


def exit_matches_class(description: str, tickers_ended: str, share_class: str, observed: set[str],
                       cik_classes: set[str]) -> bool:
    """Whether a common Form 25 of a multi-class CIK removed the class ``share_class``.

    By the class or series letters it names ('Class A Common Stock', 'Series C Liberty SiriusXM');
    by the tickers that ended at the filing (from the snapshot intervals); and a filing naming
    generic 'Common Stock' applies to the classes keyed COMMON or by ticker (T-...), or to every
    class when the CIK has none of those. A class keyed by a ticker ending in A, B or C is not
    removed by a filing naming only other class letters, whatever the ticker evidence (QVC Group's
    'Series B Common Stock' Form 25 of 2025-05-27 did not remove QRTEA); a K suffix (Liberty's Series
    C, Comcast's Class A Special) is not compared.
    """
    letters = {x.upper() for x in re.findall(r"\b(?:class|series)\s+([a-z])\b", str(description), re.I)}
    if share_class in letters:
        return True
    suffix = share_class[-1] if share_class.startswith("T-") else ""
    if letters and suffix in ("A", "B", "C") and suffix not in letters:
        return False
    if set(str(tickers_ended).split()) & observed:
        return True
    unkeyed = {k for k in cik_classes if k == "COMMON" or k.startswith("T-")}
    if letters and letters & cik_classes:
        return False
    if not GENERIC_COMMON.search(str(description)) and not letters:
        return False
    if str(tickers_ended).split():
        return False  # the ticker evidence names other classes
    return share_class in unkeyed if unkeyed else True


def class_letter(share_class: str) -> str:
    """The class letter of a share class: 'A' for A, the ticker's A/B/C suffix for a ticker-keyed class
    (a K suffix is Liberty's and Discovery's Series C), '' otherwise."""
    share_class = str(share_class or "")
    if len(share_class) == 1 and share_class.isalpha():
        return share_class
    if share_class.startswith("T-") and len(share_class) > 3:
        suffix = share_class[-1]
        return "C" if suffix == "K" else suffix if suffix in "ABC" else ""
    return ""


def regime_stats(profiles: dict[int, dict], master: pd.DataFrame) -> dict:
    """Flag counts over the master's CIKs, against the flag the recent block alone gave (the rule
    before every page was read), and the pages the flag could not read."""
    ciks = {int(c) for c in master["cik"]}
    have = [c for c in sorted(ciks) if c in profiles and profiles[c].get("regime")]
    recent_only = {c: {(True, False): "N", (False, True): "Y", (True, True): "MIXED"}.get(
        (profiles[c]["n_domestic"] > 0, profiles[c]["n_foreign"] > 0), "UNKNOWN") for c in have}
    flag = {c: profiles[c]["regime"]["flag"] for c in have}
    unread = {c: profiles[c]["regime"]["pages_unread"] for c in have if profiles[c]["regime"]["pages_unread"]}
    return {"ciks": len(have), "flags": dict(Counter(flag.values())),
            "flags_recent_block_rule": dict(Counter(recent_only.values())),
            "changes_from_recent_block_rule": dict(Counter(f"{recent_only[c]}->{flag[c]}" for c in have
                                                           if flag[c] != recent_only[c])),
            "changed_ciks": {str(c): f"{recent_only[c]}->{flag[c]}" for c in have if flag[c] != recent_only[c]},
            "pages_read": sum(len(profiles[c]["regime"]["pages_read"]) for c in have),
            "pages_unread": sum(len(v) for v in unread.values()),
            "ciks_with_pages_unread_by_flag": dict(Counter(flag[c] for c in unread))}


def write_files_read(profiles: dict[int, dict], form25: pd.DataFrame | None) -> dict:
    """The raw/sec files this builder read (for the manifest, which should hash only these), and how
    many cached submissions files no builder reads any more (left over from earlier iterations)."""
    read = set()
    for cik, profile in profiles.items():
        read.add(str(submissions_path(cik)))
        read |= {str(SEC_RAW / "submissions" / f"{name}.gz") for name in profile.get("pages_read", [])}
        read |= {str(SEC_RAW / "submissions" / f"{name}.gz") for name in (profile.get("regime") or {}).get("pages_read", [])}
    tickers = sorted((SEC_RAW / "ticker_maps").glob("company_tickers_exchange_*.json.gz"))
    read |= {str(tickers[-1])} if tickers else set()
    read.add(str(SEC_RAW / "cik-lookup-data.txt.gz"))
    form25_list = WORK / "files_read_form25.json"
    form25_read = set(json.loads(form25_list.read_text())["files"]) if form25_list.exists() else set()
    on_disk = {str(p) for p in (SEC_RAW / "submissions").glob("*.gz")}
    summary = {"files": sorted(read), "submissions_on_disk": len(on_disk),
               "submissions_read_by_builders": len(on_disk & (read | form25_read)),
               "submissions_orphans": sorted(on_disk - read - form25_read),
               "manual_one_off_requests": {
                   "raw/sec/companyfacts/CIK0001704760.json.gz, CIK0001748252.json.gz":
                       "two companyfacts requests made by hand during round 1 (logged as sec_companyfacts); "
                       "no builder reads them"}}
    common.atomic_write(WORK / "files_read_security_master.json", (json.dumps(summary, indent=1) + "\n").encode())
    return summary


def build_master(profiles, all_intervals, intervals, form25, price_map, multi, missing,
                 full_dates: list[str] | None = None) -> pd.DataFrame:
    """One row per security. Its delist date is that of its latest common Form 25 delisting that its
    snapshot intervals respect (``listed_past_delisting`` on the sorted full-list dates
    ``full_dates``): a security still listed after a Form 25 in the run that held it at the filing
    keeps no delist date from it, unless a later relisting is a separate interval after two full-list
    snapshots without it."""
    full_dates = sorted(full_dates or [])
    form25 = form25 if form25 is not None else pd.DataFrame(columns=["subject_cik", *FORM25_TEXT_COLUMNS, "successor_cik"])
    f25_by_cik = {int(c): g for c, g in form25.groupby("subject_cik")}
    prices_by_cik = {int(c): g for c, g in price_map.dropna(subset=["cik"]).groupby("cik")}
    reuse = detect_ticker_reuse(intervals) if not intervals.empty else {}
    keys = {}  # security_id -> (cik, share_class); snapshot intervals define securities
    if not intervals.empty:
        for sid, group in intervals.groupby("security_id"):
            classes = [c for c in group["share_class"] if c != "COMMON"]
            keys[sid] = (int(group["cik"].iloc[0]), Counter(classes).most_common(1)[0][0] if classes else "COMMON")
    ciks_with_rows = {c for c, _ in keys.values()}
    for cik in set(profiles) | set(f25_by_cik) | set(prices_by_cik):
        if cik not in ciks_with_rows:
            keys[security_id(cik, "COMMON", False)] = (cik, "COMMON")
    # Every security an interval names is in the master: a current SEC ticker of a multi-class CIK that
    # no Nasdaq snapshot shows (a Series B quoted OTC, notes, warrants: GOOGM, BATRB, UHAL-B) is a
    # security of its own, keyed by its ticker, with no listing dates.
    current_only: dict[str, pd.DataFrame] = {}
    if not all_intervals.empty:
        for sid, group in all_intervals[~all_intervals["security_id"].isin(set(keys))].groupby("security_id"):
            keys[sid] = (int(group["cik"].iloc[0]), str(group["share_class"].iloc[0]))
            current_only[sid] = group
    by_sid = {sid: g for sid, g in all_intervals.groupby("security_id")} if not all_intervals.empty else {}
    classes_of = defaultdict(set)
    for _, (c, k) in keys.items():
        classes_of[c].add(k)
    successor_of, predecessor_of = defaultdict(set), defaultdict(set)
    for row in form25[form25["successor_cik"].notna()].itertuples():
        successor_of[int(row.subject_cik)].add(int(row.successor_cik))
        predecessor_of[int(row.successor_cik)].add(int(row.subject_cik))
    snapshot_iv = intervals if not intervals.empty else pd.DataFrame(
        columns=["cik", "ticker", "start", "end", "share_class", "security_id"])

    def successor_security(row, listed: pd.DataFrame, share_class: str) -> str:
        """The successor's security that took this security's stock, from the filing on: the one holding
        this security's last ticker (LLYVK went to Liberty Live Holdings' class C, FOXA to Fox Corp's
        class A); else the one of the same class letter; else the one holding a handed-over ticker."""
        succ = snapshot_iv[(snapshot_iv["cik"] == int(row.successor_cik))
                           & (snapshot_iv["start"] >= _shift(row.filing_date, -60))].sort_values("start")
        own = listed.sort_values("end", ascending=False)["ticker"].drop_duplicates() if not listed.empty else []
        for ticker in own:
            hit = succ[succ["ticker"] == ticker]
            if not hit.empty:
                return hit.iloc[0]["security_id"]
        letter = class_letter(share_class)
        same = succ[[class_letter(k) == letter for k in succ["share_class"]]] if letter else succ.iloc[:0]
        if same["security_id"].nunique() == 1:
            return same.iloc[0]["security_id"]
        after = succ[succ["ticker"].isin(set(row.successor_tickers.split()))]
        return after.iloc[0]["security_id"] if not after.empty else str(int(row.successor_cik))

    rows = []
    for sid, (cik, share_class) in sorted(keys.items(), key=lambda kv: (kv[1][0], kv[0])):
        profile = profiles.get(cik)
        regime = (profile.get("regime") or {"domestic_first": profile["domestic_first"],
                                            "domestic_last": profile["domestic_last"],
                                            "foreign_first": profile["foreign_first"],
                                            "foreign_last": profile["foreign_last"],
                                            "pages_unread": profile["older_pages_unfetched_since"]}) if profile else {}
        own = by_sid.get(sid, pd.DataFrame(columns=all_intervals.columns))
        listed = own[own["source"] != "sec_company_tickers_exchange"] if not own.empty else own
        f25 = f25_by_cik.get(cik)
        observed = sorted(set(own["ticker"])) if not own.empty else []

        def mine(kinds: tuple) -> pd.DataFrame | None:
            """This security's Form 25 rows of the given classifications (class-matched when multi-class)."""
            if f25 is None:
                return None
            chosen = f25[f25["classification"].isin(kinds)]
            if cik in multi and not chosen.empty:
                keep = [exit_matches_class(r.class_of_security, r.tickers_ended or r.successor_tickers, share_class,
                                           set(observed), classes_of[cik]) for r in chosen.itertuples()]
                chosen = chosen[keep]
            return chosen.sort_values("filing_date") if not chosen.empty else None

        exits = mine(("common_delisting",))  # a reorganisation or transfer is not a delisting
        last_exit, refused = None, []
        spans = list(zip(listed["start"], listed["end"])) if not listed.empty else []
        for _, candidate in (exits.iloc[::-1].iterrows() if exits is not None else []):
            reason = listed_past_delisting(spans, candidate["effective_date"], full_dates)
            if not reason:
                last_exit = candidate
                break
            refused.append(f"Form 25 {candidate['accession']} ({candidate['effective_date']}) gives no delist date: {reason}")
        reorgs = mine(("reorg", "reorg_review"))
        handover = None
        if reorgs is not None:
            linked = reorgs[reorgs["successor_cik"].notna()]
            if cik in multi and not linked.empty:
                linked = linked[[bool(set(t.split()) & set(observed)) or not t for t in linked["successor_tickers"]]]
            handover = linked.iloc[-1] if not linked.empty else None
        if handover is not None and last_exit is not None and last_exit["filing_date"] > handover["filing_date"]:
            handover = None  # delisted after the reorganisation (relisted in between)
        transfers = mine(("transfer",))
        last_transfer = transfers.iloc[-1] if transfers is not None else None
        notes = []
        for ticker in observed:
            if ticker in reuse:
                notes.append(f"ticker {ticker} also held by CIK " + " ".join(str(c) for c in reuse[ticker] if c != cik))
        if not listed.empty and (listed["match"] == "ticker_only").any():
            notes.append("some snapshot names did not match SEC names (ticker_only)")
        if not listed.empty and (listed["match"] == "name+lookup_nonfiler").any():
            notes.append("CIK of a same-name EDGAR entity that files no periodic reports (name+lookup_nonfiler)")
        if not listed.empty and (listed["match"] == "successor").any():
            notes.append("some snapshot rows reassigned from a predecessor after its Form 25 (successor)")
        if exits is not None and len(exits) > 1:
            notes.append(f"{len(exits)} common Form 25 delistings")
        notes.extend(refused)
        if last_exit is not None and not listed.empty \
                and last_exit["effective_date"] > _shift(listed["end"].max(), LATE_DELIST_DAYS):
            lag = (pd.Timestamp(last_exit["effective_date"]) - pd.Timestamp(listed["end"].max())).days
            notes.append(f"review: delist date {lag} days after the last Nasdaq listing {listed['end'].max()} "
                         "(a long suspension, or a Form 25 naming a predecessor CIK no longer listed)")
        if last_exit is not None and not listed.empty and listed["end"].max() > _shift(last_exit["effective_date"], 30):
            notes.append(f"listed on Nasdaq again after the {last_exit['effective_date']} delisting")
        if last_transfer is not None and not listed.empty \
                and listed["end"].max() > _shift(last_transfer["effective_date"], DELIST_TOLERANCE_DAYS):
            notes.append(f"in Nasdaq lists until {listed['end'].max()}, after the {last_transfer['effective_date']} "
                         "transfer to another exchange")
        if handover is not None and handover["classification"] == "reorg_review":
            notes.append("successor link from a ticker handover with differing names (reorg_review)")
        if cik in missing:
            notes.append("SEC submissions not found")
        if sid in current_only:
            seen = current_only[sid]
            notes.append("only in SEC's current ticker list (" + ", ".join(
                f"{t} on {e or 'no exchange'}" for t, e in zip(seen["ticker"], seen["exchange"].fillna("")))
                + "); no Nasdaq snapshot row")
        if successor_of.get(cik):
            notes.append("Form 25 reorg: successor CIK " + " ".join(map(str, sorted(successor_of[cik]))))
        if predecessor_of.get(cik):
            notes.append("Form 25 reorg: predecessor CIK " + " ".join(map(str, sorted(predecessor_of[cik]))))
        price_rows = prices_by_cik.get(cik)
        if price_rows is not None and cik in multi:
            price_rows = price_rows[price_rows["ticker"].str.split("_").str[0].isin(observed)]
            price_rows = price_rows if not price_rows.empty else None
        first_ticker = ""
        if not listed.empty:
            first_ticker = listed.sort_values("start").iloc[0]["ticker"]
        elif sid in current_only:
            first_ticker = current_only[sid]["ticker"].iloc[0]
        elif profile and profile["tickers"]:
            first_ticker = profile["tickers"][0]
        elif f25 is not None and text_value(f25.iloc[0].get("subject_tickers_sec")).strip():
            first_ticker = text_value(f25.iloc[0]["subject_tickers_sec"]).split()[0]
        found_via = []
        if f25 is not None:
            found_via.append("form25")
        if price_rows is not None:
            found_via.append("price_file")
        if not listed.empty:
            found_via.append("snapshot")
        if not own.empty and (own["source"] == "sec_company_tickers_exchange").any():
            found_via.append("sec_current")
        rows.append({
            "security_id": sid, "cik": cik, "first_ticker": first_ticker,
            "name": profile["name"] if profile else (f25.iloc[0]["subject_name"] if f25 is not None else ""),
            "share_class": share_class,
            "first_listed": listed["start"].min() if not listed.empty else "",
            "last_listed": listed["end"].max() if not listed.empty else "",
            "delist_date": last_exit["effective_date"] if last_exit is not None else "",
            "delist_form25_accession": last_exit["accession"] if last_exit is not None else "",
            "foreign_filer": foreign_filer_flag(profile) if profile else "UNKNOWN",
            "multi_class_group": str(cik) if cik in multi else "",
            "price_sources": " ".join(
                f"stored:{r.file}[{r.first_date}..{r.last_date}]"
                + (f"{{ticker also CIK {' '.join(c for c in str(r.ciks_on_ticker_in_file_range).split() if c != str(cik))}}}"
                   if len(str(r.ciks_on_ticker_in_file_range).split()) > 1 else "")
                for r in price_rows.itertuples()) if price_rows is not None else "",
            "identity_notes": "; ".join(notes),
            "tickers_observed": " ".join(observed),
            "tickers_sec_current": " ".join(profile["tickers"]) if profile else "",
            "exchanges_sec_current": " ".join(profile["exchanges"]) if profile else "",
            "former_names": " | ".join(f"{f['name']} ({f['from']}..{f['to']})" for f in profile["former_names"]) if profile else "",
            "sic": profile["sic"] if profile else "", "sic_description": profile["sic_description"] if profile else "",
            "state_of_incorporation": profile["state_of_incorporation"] if profile else "",
            "entity_type": profile["entity_type"] if profile else "", "filer_category": profile["category"] if profile else "",
            "domestic_periodic_first": regime.get("domestic_first", ""),
            "domestic_periodic_last": regime.get("domestic_last", ""),
            "foreign_forms_first": regime.get("foreign_first", ""),
            "foreign_forms_last": regime.get("foreign_last", ""),
            "foreign_spans": " ".join(f"{a}..{b}" for a, b in regime.get("spans", [])),
            "filings_coverage_start": profile["coverage_start"] if profile else "",
            "older_pages_not_fetched": " ".join(regime.get("pages_unread", [])),
            "in_form25": "Y" if f25 is not None else "N",
            "form25_classes": " | ".join(sorted(set(f"{c}:{k}" for c, k in zip(f25["classification"], f25["class_kind"]))))
            if f25 is not None else "",
            "found_via": " ".join(found_via),
            "successor_security_id": successor_security(handover, listed, share_class) if handover is not None else "",
            "successor_date": handover["filing_date"] if handover is not None else "",
            "successor_form25_accession": handover["accession"] if handover is not None else "",
            "transfer_date": last_transfer["effective_date"] if last_transfer is not None else "",
            "transfer_form25_accession": last_transfer["accession"] if last_transfer is not None else "",
        })
    return pd.DataFrame(rows, columns=MASTER_COLUMNS)


def redirect_outputs(out_dir: Path | None = None, form25: Path | None = None) -> None:
    """Write every output under ``out_dir`` (the three INPUTS tables at its top, the derived files in
    ``out_dir/derived``) instead of INPUTS and CACHE/raw/sec/derived, for a scratch build; read the
    step-3 table from ``form25`` when given. The SEC cache itself is shared."""
    global MASTER, INTERVALS, PERIODIC_HISTORY, WORK, RAW_ROWS, FORM25
    if out_dir is not None:
        out_dir = Path(out_dir)
        MASTER, INTERVALS = out_dir / "security_master.csv", out_dir / "ticker_intervals.csv"
        PERIODIC_HISTORY = out_dir / "periodic_form_history.csv"
        WORK = out_dir / "derived"
        RAW_ROWS = WORK / "ticker_rows_raw.csv.gz"
    if form25 is not None:
        FORM25 = Path(form25)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="use cached responses only")
    parser.add_argument("--out-dir", type=Path, help="scratch build: write every output under this directory")
    parser.add_argument("--form25", type=Path, help="read the step-3 table from this file (default INPUTS)")
    args = parser.parse_args(argv)
    redirect_outputs(args.out_dir, args.form25)
    master, intervals, stats, evidence, history = build(offline=args.offline)
    common.atomic_write(MASTER, master.to_csv(index=False).encode("utf-8"))
    common.atomic_write(PERIODIC_HISTORY, history.to_csv(index=False).encode("utf-8"))
    evidence = evidence.reindex(columns=INTERVAL_COLUMNS).sort_values(["ticker", "start", "source"], na_position="first")
    common.atomic_write(WORK / "ticker_intervals_evidence.csv.gz",
                        gzip.compress(evidence.to_csv(index=False).encode("utf-8"), mtime=0))
    merged = intervals.reindex(columns=INTERVAL_COLUMNS).sort_values(["ticker", "start", "source"], na_position="first")
    merged["cik"] = merged["cik"].astype("Int64")
    merged["coverage_gap_days"] = merged["coverage_gap_days"].astype("Int64")
    common.atomic_write(INTERVALS, merged.to_csv(index=False).encode("utf-8"))
    stats["interval_rows_merged"] = len(merged)
    stats["interval_rows_merged_snapshot"] = int((merged["source"] != "sec_company_tickers_exchange").sum())
    stats["interval_rows_family_evidence"] = len(evidence)
    common.atomic_write(WORK / "security_master_build_summary.json",
                        (json.dumps(stats, indent=2, default=str) + "\n").encode())
    print(json.dumps({k: v for k, v in stats.items() if k != "price_files_unresolved"}, indent=2, default=str))
    print(f"price files unresolved: {len(stats['price_files_unresolved'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
