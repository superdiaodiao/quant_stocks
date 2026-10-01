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
matches the CIK's SEC name or one of its former names, in any word order ('PRICE T
ROWE GROUP INC'), at the time of the listing (``name+ticker`` when the CIK is also
a ticker candidate, ``name_only`` when the name is unique among all fetched CIKs,
``name+lookup`` when the candidate came from SEC's name list); with no name match
it falls back to a single ticker candidate (``ticker_only``, low confidence). An
EDGAR entity that files no periodic reports (a subsidiary, or a bank filing with
the FDIC) never beats an issuer; alone, for a distinctive name, it is kept as
``name+lookup_nonfiler``. Truncated names (two repo files cut names at the first
hyphen) borrow the nearest full name and otherwise support no name-only match. A
name valid for only part of a listing while a rival could hold the rest, or shared
by two CIKs (a holding-company reorganisation), is split by date; a CIK's dates end
at the Form 25 that ended its Nasdaq common listing, and its rows more than 10 days
after that filing go to the successor that took the ticker, or to nobody. One
ticker has one CIK on any date: where two CIKs' rows of a ticker overlap in time,
the weaker evidence is dropped.

Intervals are runs of one (CIK, share class, ticker) across every snapshot family.
A run breaks only when the symbol is missing from two full-list snapshots in a row
(any family), or another security holds the ticker in between; a stretch that no
source covers (2011-01-25..2011-05-27, 2012-06-22..2012-10-24) does not break it
and is recorded as ``coverage_gap_days``. ``start`` and ``end`` are observed
dates; ``start_prev_absent``/``end_next_absent`` give the full-list snapshots that
bound them from outside.

Run order (each step reads the other's output; both are cache-first, and a rerun
from cache takes a few minutes)::

    form25 -> security_master -> form25 -> security_master

repeated until neither table changes (round 2 converged after two alternations).

Usage::

    PYTHONPATH=. python scripts/reversal_data_security_master.py            # fetch + build
    PYTHONPATH=. python scripts/reversal_data_security_master.py --offline  # rebuild from cache only
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
MULTI_CLASS_MIN_DATES = 3
# SEC answers in about a second, so a few threads share SEC_LIMITER to reach its 7 requests a second.
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
                  "filings_coverage_start", "older_pages_not_fetched", "in_form25", "form25_classes", "found_via",
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
                                            headers=common.sec_headers(), limiter=common.SEC_LIMITER,
                                            symbol=f"CIK{int(cik)}"))
    except FileNotFoundError:
        return None


def load_submission_page(name: str, *, offline: bool = False) -> dict | None:
    path = SEC_RAW / "submissions" / f"{name}.gz"
    if offline and not path.exists():
        return None
    try:
        return json.loads(common.cached_get(SUBMISSION_PAGE_URL.format(name=name), path, source="sec_submissions",
                                            headers=common.sec_headers(), limiter=common.SEC_LIMITER))
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
    """Y: only 20-F/40-F/6-K since 2011-06; N: only 10-K/10-Q; MIXED: both (a switch); UNKNOWN: neither."""
    domestic, foreign = profile["n_domestic"] > 0, profile["n_foreign"] > 0
    return {(True, False): "N", (False, True): "Y", (True, True): "MIXED"}.get((domestic, foreign), "UNKNOWN")


def needs_older_pages(profile: dict) -> bool:
    """Older pages are fetched only when the recent block cannot set the foreign-filer flag."""
    return profile["n_domestic"] == 0 and profile["n_foreign"] == 0 and bool(profile["older_pages_unfetched_since"])


def full_profile(cik: int, *, offline: bool = False) -> dict | None:
    payload = load_submissions(cik, offline=offline)
    if not payload:
        return None
    profile = parse_submissions(payload)
    if needs_older_pages(profile):
        pages = []
        for name in profile["older_pages_unfetched_since"]:
            page = load_submission_page(name, offline=offline)
            if page is not None:
                pages.append({**page, "_page_name": name})
        profile = parse_submissions(payload, pages)
        profile["older_pages_fetched"] = len(pages)
        profile["pages_read"] = [p["_page_name"] for p in pages]
    return profile


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
    r"tracking)\b|%", re.I)


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


def names_match(a: str, b: str) -> bool:
    """Normalized names equal (in any word order: EDGAR stores 'PRICE T ROWE GROUP INC'), or one a token
    prefix of the other (two tokens, or one distinctive token)."""
    if not a or not b:
        return False
    if a == b or a.replace(" ", "") == b.replace(" ", ""):
        return True
    ta, tb = a.split(), b.split()
    if len(ta) >= 2 and sorted(ta) == sorted(tb):
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
    """'Alphabet Inc. - Class C Capital Stock' -> 'C'; ADRs -> 'ADS'; otherwise 'COMMON'."""
    text = str(name or "")
    if re.search(r"american depositary|american depository|\bADS\b|\bADR\b|depositary shares", text, re.I):
        return "ADS"
    match = re.search(r"\b(?:class|series)\s+([A-Z])\b", text, re.I)
    return match.group(1).upper() if match else "COMMON"


NON_COMMON = re.compile(
    r"\bPreferred\b|\bPreference Shares?\b|\bWarrants?\b|\b(?:Sub)?Units?\b|Notes? due|Debenture|\bRights?\b|"
    r"Tangible Equity|Trust Preferred|Senior Notes|Subordinated Notes|\bETF\b|\bETNs?\b|Exchange Traded|"
    r"\bIndex Fund\b|\bTest Stock\b|\bWhen[- ]Issued\b|\bClosed[- ]End\b|\bL\.?P\.?\b|\bLimited Partnership\b|"
    r"Depositary Shares?,? each representing (?:a |one )?(?:\d+/\d+(?:th)?|fractional|\d[\d,]*th)|"
    r"representing .*interest in .*preferred|\bFund\b|\bPortfolio\b|ProShares|PowerShares|iShares|WisdomTree|"
    r"BLDRS|VelocityShares|NextShares|Index Tracking Stock|\bTrust,? Series 1\b", re.I)


ADR_NAME = re.compile(r"\bamer\w*\s+deposit[ao]ry|\bADS\b|\bADRs?\b|new york registry", re.I)
ADR_EXCLUDE = re.compile(r"preferred|warrant|\brights?\b|\bunits?\b|notes? due", re.I)
# On Nasdaq a depositary share or receipt that is not an American depositary share is a fractional
# preferred (SRCLP 'Depository Receipt', IBKCP 'Depositary Shares Representing Series B').
DEPOSITARY_PREFERRED = re.compile(r"deposit[ao]ry (?:shares?|receipts?)\b", re.I)


def is_common_equity(name: str) -> bool:
    """Common stock, ordinary shares and ADRs; not preferreds, warrants, units, rights, debt, funds, LPs.

    Unlike ``investable_common_equities`` this keeps every ADR (the plan's ADR flag)
    and SPAC shares, so the identity of a SPAC that becomes an operating company is kept.
    """
    name = str(name or "")
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
    """One snapshot file as rows (symbol, name, date, flags, source, source_url, name_truncated).

    Company-list rows without a last sale are dropped: they are companies in the IPO
    pipeline, not listed securities.
    """
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    columns = {c.lower().strip(): c for c in frame.columns}
    symbol = columns.get("symbol") or columns.get("ticker")
    name = columns.get("name") or columns.get("security name") or columns.get("company name")
    if not symbol or not name:
        return pd.DataFrame()
    last_sale = columns.get("lastsale") or columns.get("last sale")
    if last_sale and source == "wayback_companylist":
        frame = frame[~frame[last_sale].str.strip().str.lstrip("$").str.upper().isin(NO_LAST_SALE)]
    observed = columns.get("observed at") or columns.get("snapshot_date") or columns.get("date")
    out = pd.DataFrame({"symbol": frame[symbol].str.strip().str.upper(), "name": frame[name].str.strip()})
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
                                     "name_truncated"])
    snapshots = pd.concat(frames, ignore_index=True)
    snapshots["name_truncated"] = snapshots["name_truncated"].fillna(False).astype(bool)
    return snapshots[(snapshots["symbol"] != "") & (snapshots["date"] != "")]


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
    out.loc[near.index] = near.fillna("COMMON")
    return out


def common_snapshot_rows(snapshots: pd.DataFrame) -> pd.DataFrame:
    """Rows that are common equity.

    A name that states its security type (symbol files from 2011 on, the 300M
    screener) is judged by the name. A bare company name (the nasdaq.com company
    lists and the truncated 2015 file) is judged by the nearest typed row of the
    same symbol within three years; with none, by the name and the symbol's fifth letter.
    """
    frame = snapshots.copy()
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
                                           limiter=common.SEC_LIMITER))
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
    """Keys of a normalized name in a name index: without spaces, and (two tokens or more) its sorted
    tokens, so 'price t rowe group' and 't rowe price group' share a key."""
    if not normalized:
        return []
    keys = [normalized.replace(" ", "")]
    tokens = normalized.split()
    if len(tokens) >= 2:
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
               slack: int = NAME_SLACK_DAYS) -> bool:
    """The CIK carried a name matching ``key`` at some time in first..last (give or take ``slack`` days)."""
    lo = _shift(first, -slack) if first else "0000-00-00"
    hi = _shift(last or first, slack) if (last or first) else "9999-12-31"
    compact, tokens = key.replace(" ", ""), sorted(key.split())
    same = lambda n: n.replace(" ", "") == compact or (len(tokens) >= 2 and sorted(n.split()) == tokens)
    return any((same(n) if exact else names_match(key, n)) and a <= hi and b >= lo
               for n, a, b in profile_name_spans(profile))


def name_valid_throughout(profile: dict, key: str, first: str, last: str, slack: int = NAME_SLACK_DAYS) -> bool:
    """The CIK bore a name matching ``key`` both when the listing was first and last seen."""
    return name_valid(profile, key, first, first, slack=slack) and name_valid(profile, key, last, last, slack=slack)


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

    named = [c for c in ticker_ciks if name_valid(profiles[c], key, first, last)]
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
        whole = [c for c in by_name if name_valid_throughout(profiles[c], key, first, last)]
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
        rivals = partial or [c for c in mapped_any if c != by_name[0] and issuer_like(profiles[c])
                             and active_during(profiles[c], first, last)]
        if not name_valid_throughout(profiles[by_name[0]], key, first, last) and (lookup_only(by_name[0]) or rivals):
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
                    exits: dict[int, str] | None = None, truncated: bool = False) -> dict[str, int]:
    """For a pair whose name matches several CIKs (a holding-company reorganisation reuses name and
    ticker), or a CIK for only part of the pair's dates, assign each snapshot date to the candidate
    that bore the name on that date and whose activity window holds the date.

    A CIK's window ends at the filing date of the Form 25 that ended its Nasdaq common listing
    (``exits``): the exchange files it once trading has stopped, so later rows of that ticker belong
    to a successor. Where two windows overlap the incumbent (earlier start) keeps the date, unless
    both span every date (two unrelated companies), which stays unassigned. A date with one
    candidate accepts a name within NAME_SLACK_DAYS of its EDGAR dates; competing candidates are
    judged within DATED_NAME_SLACK_DAYS.
    """
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
    out = {}
    for day in dates:
        inside = [c for c, (lo, hi) in windows.items() if lo and lo <= day <= hi
                  and name_valid(profiles[c], key, day, day, slack=NAME_SLACK_DAYS)]
        if len(inside) > 1:
            inside = [c for c in inside if name_valid(profiles[c], key, day, day, slack=DATED_NAME_SLACK_DAYS)]
        if len(inside) == 1:
            out[day] = inside[0]
        elif inside and not all(span(c) for c in inside):
            out[day] = min(inside, key=lambda c: (windows[c][0], c))
    return out


def load_cik_lookup(*, offline: bool = False) -> dict[str, set[int]]:
    """normalized name -> CIKs from SEC's list of every EDGAR entity name (current and former)."""
    path = SEC_RAW / "cik-lookup-data.txt.gz"
    if offline and not path.exists():
        return {}
    text = common.cached_get(CIK_LOOKUP_URL, path, source="sec_archives", headers=common.sec_headers(),
                             limiter=common.SEC_LIMITER, timeout=300).decode("latin-1")
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
FAMILY_ORDER = {"repo_symdir": 0, "wayback_symdir": 1, "wayback_companylist": 2, "repo_screener_300M": 3}
# Rows of a CIK this long after the filing of its terminal Form 25 are not its own (the exchange
# files the 25 once trading has stopped; a few lists lag by days).
EXIT_GRACE_DAYS = 10


def presence_by_date(snapshots: pd.DataFrame) -> dict[str, set[str]]:
    """date -> every symbol in that day's snapshots (any family, common or not)."""
    return {day: set(symbols) for day, symbols in snapshots.groupby("date")["symbol"]}


def build_intervals(rows: pd.DataFrame, snapshot_dates: dict[str, list[str]],
                    present: dict[str, set[str]] | None = None) -> pd.DataFrame:
    """Intervals of each (cik, share_class, ticker) from resolved snapshot rows of every family.

    ``rows`` has symbol, date, cik, share_class, family, source_url, how, name.
    ``snapshot_dates`` maps each family to its sorted dates and ``present`` maps a date to the
    symbols in that day's snapshots (default: the symbols in ``rows``). A run breaks only when, on the
    union of the full-list families' dates, the symbol is missing from two snapshots in a row, or
    another security holds the ticker in between. A stretch with no snapshot at all (no source covers
    it) never breaks a run; the longest such stretch inside the interval is ``coverage_gap_days``.
    """
    full = sorted({d for family, days in snapshot_dates.items() if family not in PARTIAL_FAMILIES for d in days})
    every = sorted({d for days in snapshot_dates.values() for d in days})
    if present is None:
        present = presence_by_date(rows)
    out = []
    ordered = rows.sort_values(["symbol", "date"])
    columns = ["date", "cik", "share_class", "family", "source_url", "how", "name"]
    for symbol, group in ordered.groupby("symbol", sort=False):
        holders: dict[str, set] = defaultdict(set)
        by_key: dict[tuple, list] = defaultdict(list)
        for record in zip(*(group[c].tolist() for c in columns)):
            key = (int(record[1]), record[2])
            holders[record[0]].add(key)
            by_key[key].append(record)
        for key, records in by_key.items():
            days = sorted({r[0] for r in records})
            runs, begin = [], 0
            for i in range(1, len(days)):
                a, b = days[i - 1], days[i]
                between = full[bisect.bisect_right(full, a):bisect.bisect_left(full, b)]
                absent = sum(1 for u in between if symbol not in present.get(u, ()))
                other = any(holders.get(u, set()) - {key}
                            for u in every[bisect.bisect_right(every, a):bisect.bisect_left(every, b)])
                if absent >= 2 or other:
                    runs.append(days[begin:i])
                    begin = i
            runs.append(days[begin:])
            for run in runs:
                start, end = run[0], run[-1]
                inside = sorted((r for r in records if start <= r[0] <= end),
                                key=lambda r: (r[0], FAMILY_ORDER.get(r[3], 9)))
                covered = every[bisect.bisect_left(every, start):bisect.bisect_right(every, end)]
                gap = max(((pd.Timestamp(y) - pd.Timestamp(x)).days for x, y in zip(covered, covered[1:])), default=0)
                i, j = bisect.bisect_left(full, start), bisect.bisect_right(full, end)
                out.append({"cik": key[0], "share_class": key[1], "ticker": symbol, "start": start, "end": end,
                            "start_prev_absent": full[i - 1] if i > 0 else "",
                            "end_next_absent": full[j] if j < len(full) else "",
                            "n_snapshots": len(run), "exchange": "NASDAQ", "source": inside[0][3],
                            "source_url": inside[0][4],
                            "match": max((r[5] for r in inside), key=lambda m: MATCH_RANK.get(m, 9)),
                            "name_in_source": inside[-1][6],
                            "sources": " ".join(sorted({r[3] for r in inside})), "n_evidence_rows": len(inside),
                            "coverage_gap_days": gap})
    return pd.DataFrame(out)


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
                         grace_days: int = EXIT_GRACE_DAYS) -> tuple[pd.DataFrame, int, int]:
    """Rows of a CIK dated more than ``grace_days`` after the Form 25 that ended its Nasdaq listing
    belong to someone else: to the successor that took the ticker, when there is one ('successor'),
    else to nobody ('after_exit')."""
    rows = rows.copy()
    cut = rows["cik"].map(lambda c: _shift(exits[int(c)], grace_days) if pd.notna(c) and int(c) in exits else "9999-12-31")
    late = rows.index[rows["date"] > cut]
    moved = 0
    for i in late:
        successor = successors.get((int(rows.at[i, "cik"]), rows.at[i, "symbol"]))
        if successor:
            rows.at[i, "cik"], rows.at[i, "how"] = successor, "successor"
            moved += 1
        else:
            rows.at[i, "cik"], rows.at[i, "how"] = None, "after_exit"
    return rows, len(late) - moved, moved


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


def build(offline: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    form25 = read_form25(FORM25)
    exits = terminal_exits(form25)
    sec_current = fetch_sec_tickers_exchange(offline=offline)
    cands = collect_candidates(sec_current, form25)
    snapshots = fill_symbol_only_names(load_listing_snapshots())
    listings = common_snapshot_rows(snapshots)
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
    for (symbol, name), group in listings[listings["how"] == "ambiguous"].groupby(["symbol", "name"]):
        by_day = resolve_by_date(symbol, name, sorted(group["date"].unique()), cands, profiles, name_index, lookup,
                                 exits, (symbol, name) in truncated_pairs)
        hit = group.index[group["date"].isin(by_day)]
        listings.loc[hit, "cik"] = group.loc[hit, "date"].map(by_day)
        listings.loc[hit, "how"] = "name+dated"
        dated += len(hit)
    listings["share_class"] = listings["name"].map(share_class_from_name)
    listings["family"] = listings["source"]
    snapshot_dates = {family: sorted(group["date"].unique()) for family, group in snapshots.groupby("source")}
    matched = listings[listings["cik"].notna()].copy()
    matched["cik"] = matched["cik"].astype(int)
    # No rows after a CIK's terminal Form 25 (they go to the successor that took the ticker, if any),
    # and one CIK per (ticker, date): the weaker evidence is dropped where two CIKs overlap.
    matched, after_exit_dropped, after_exit_moved = drop_rows_after_exit(matched, exits, successor_tickers(form25))
    conflicts_before = len(ticker_conflicts(matched[matched["cik"].notna()]))
    conflict_rows = 0
    for _ in range(3):
        live = matched[matched["cik"].notna()]
        if not ticker_conflicts(live):
            break
        matched, dropped = resolve_ticker_conflicts(matched, profiles, exits)
        conflict_rows += dropped
    for how in ("after_exit", "conflict", "successor"):
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
    present = presence_by_date(snapshots)
    intervals = build_intervals(matched, snapshot_dates, present)
    if not intervals.empty:
        intervals["security_id"] = [security_id(c, s, c in multi) for c, s in zip(intervals["cik"], intervals["share_class"])]

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

    master = build_master(profiles, all_intervals, intervals, form25, price_map, multi, missing)
    # Per-family runs (the same rule on one family's dates) are kept as audit evidence.
    evidence = []
    for family, days in snapshot_dates.items():
        own = matched[matched["family"] == family]
        if not own.empty:
            evidence.append(build_intervals(own, {family: days}, presence_by_date(snapshots[snapshots["source"] == family])))
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
        "wayback_hook_rows": int(snapshots["source"].str.startswith("wayback").sum()),
        "truncated_name_pairs": len(truncated_pairs),
        "terminal_form25_exits_used": len(exits),
        "ticker_conflicts_before": conflicts_before, "ticker_conflict_rows_dropped": conflict_rows,
        "ticker_conflicts_after": len(ticker_conflicts(matched)),
        "rows_after_exit_dropped": after_exit_dropped, "rows_after_exit_to_successor": after_exit_moved,
        "intervals_bridging_coverage_gap_over_120d": int((intervals["coverage_gap_days"] > 120).sum()) if not intervals.empty else 0,
        "master_delist_dates": int(master["delist_date"].fillna("").ne("").sum()),
        "master_successor_links": int(master["successor_security_id"].fillna("").ne("").sum()),
        "master_transfers": int(master["transfer_date"].fillna("").ne("").sum()),
    }
    WORK.mkdir(parents=True, exist_ok=True)
    common.atomic_write(WORK / "listing_unresolved.csv", unresolved_pairs.to_csv(index=False).encode())
    common.atomic_write(WORK / "price_file_cik_map.csv", price_map.to_csv(index=False).encode())
    common.atomic_write(WORK / "ticker_reuse.json", (json.dumps(reuse, indent=1) + "\n").encode())
    common.atomic_write(WORK / "ciks_not_found.json", (json.dumps(missing) + "\n").encode())
    common.atomic_write(WORK / "snapshot_dates.json", (json.dumps(
        {"families": snapshot_dates, "partial_families": sorted(PARTIAL_FAMILIES)}, indent=0) + "\n").encode())
    common.atomic_write(WORK / "security_master_build_summary.json",
                        (json.dumps({k: v for k, v in stats.items()}, indent=2, default=str) + "\n").encode())
    write_files_read(profiles, form25)
    return master, all_intervals, stats, evidence


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
    class when the CIK has none of those.
    """
    letters = {x.upper() for x in re.findall(r"\b(?:class|series)\s+([a-z])\b", str(description), re.I)}
    if share_class in letters:
        return True
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


def write_files_read(profiles: dict[int, dict], form25: pd.DataFrame | None) -> dict:
    """The raw/sec files this builder read (for the manifest, which should hash only these), and how
    many cached submissions files no builder reads any more (left over from earlier iterations)."""
    read = set()
    for cik, profile in profiles.items():
        read.add(str(submissions_path(cik)))
        read |= {str(SEC_RAW / "submissions" / f"{name}.gz") for name in profile.get("pages_read", [])}
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


def build_master(profiles, all_intervals, intervals, form25, price_map, multi, missing) -> pd.DataFrame:
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
    by_sid = {sid: g for sid, g in all_intervals.groupby("security_id")} if not all_intervals.empty else {}
    classes_of = defaultdict(set)
    for _, (c, k) in keys.items():
        classes_of[c].add(k)
    successor_of, predecessor_of = defaultdict(set), defaultdict(set)
    for row in form25[form25["successor_cik"].notna()].itertuples():
        successor_of[int(row.subject_cik)].add(int(row.successor_cik))
        predecessor_of[int(row.successor_cik)].add(int(row.subject_cik))
    snapshot_iv = intervals if not intervals.empty else pd.DataFrame(columns=["cik", "ticker", "start", "security_id"])

    def successor_security(row) -> str:
        """The successor's security holding a handed-over ticker from the filing on."""
        tickers = set(row.successor_tickers.split())
        after = snapshot_iv[(snapshot_iv["cik"] == int(row.successor_cik)) & snapshot_iv["ticker"].isin(tickers)
                            & (snapshot_iv["start"] >= _shift(row.filing_date, -60))]
        return after.sort_values("start").iloc[0]["security_id"] if not after.empty else str(int(row.successor_cik))

    rows = []
    for sid, (cik, share_class) in sorted(keys.items(), key=lambda kv: (kv[1][0], kv[0])):
        profile = profiles.get(cik)
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
        last_exit = exits.iloc[-1] if exits is not None else None
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
        if last_exit is not None and not listed.empty and listed["end"].max() > _shift(last_exit["effective_date"], 30):
            notes.append(f"listed on Nasdaq again after the {last_exit['effective_date']} delisting")
        if handover is not None and handover["classification"] == "reorg_review":
            notes.append("successor link from a ticker handover with differing names (reorg_review)")
        if cik in missing:
            notes.append("SEC submissions not found")
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
            "domestic_periodic_first": profile["domestic_first"] if profile else "",
            "domestic_periodic_last": profile["domestic_last"] if profile else "",
            "foreign_forms_first": profile["foreign_first"] if profile else "",
            "foreign_forms_last": profile["foreign_last"] if profile else "",
            "filings_coverage_start": profile["coverage_start"] if profile else "",
            "older_pages_not_fetched": " ".join(profile["older_pages_unfetched_since"]) if profile else "",
            "in_form25": "Y" if f25 is not None else "N",
            "form25_classes": " | ".join(sorted(set(f"{c}:{k}" for c, k in zip(f25["classification"], f25["class_kind"]))))
            if f25 is not None else "",
            "found_via": " ".join(found_via),
            "successor_security_id": successor_security(handover) if handover is not None else "",
            "successor_date": handover["filing_date"] if handover is not None else "",
            "successor_form25_accession": handover["accession"] if handover is not None else "",
            "transfer_date": last_transfer["effective_date"] if last_transfer is not None else "",
            "transfer_form25_accession": last_transfer["accession"] if last_transfer is not None else "",
        })
    return pd.DataFrame(rows, columns=MASTER_COLUMNS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="use cached responses only")
    args = parser.parse_args(argv)
    master, intervals, stats, evidence = build(offline=args.offline)
    common.atomic_write(MASTER, master.to_csv(index=False).encode("utf-8"))
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
