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
matches the CIK's SEC name or one of its former names (``name+ticker`` when the
CIK is also a ticker candidate, ``name_only`` when the name is unique among all
fetched CIKs); with no name match it falls back to a single ticker candidate
(``ticker_only``, low confidence). Consecutive snapshot appearances with the
same CIK and share class form one interval; an interval breaks when the symbol
is missing from two snapshots in a row or the gap exceeds 120 days. ``start``
and ``end`` are observed dates; ``start_prev_absent``/``end_next_absent`` give
the snapshots that bound them from outside.

Usage::

    PYTHONPATH=. python scripts/reversal_data_security_master.py            # fetch + build
    PYTHONPATH=. python scripts/reversal_data_security_master.py --offline  # rebuild from cache only
"""
from __future__ import annotations

import argparse
import bisect
import html
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
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
RUN_BREAK_DAYS = 120
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
                  "filings_coverage_start", "older_pages_not_fetched", "in_form25", "form25_classes", "found_via"]
INTERVAL_COLUMNS = ["security_id", "ticker", "start", "end", "exchange", "source", "source_url",
                    # extras
                    "cik", "share_class", "start_prev_absent", "end_next_absent", "n_snapshots", "match",
                    "name_in_source"]


# ------------------------------------------------------------------ SEC submissions

def parallel_map(function, items: list, workers: int = WORKERS, label: str = "", every: int = 500) -> list:
    """``[function(item) for item in items]`` on a few threads (the shared SEC_LIMITER sets the pace)."""
    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(function, item): i for i, item in enumerate(items)}
        for done, future in enumerate(futures, 1):
            results[futures[future]] = future.result()
            if label and done % every == 0:
                print(f"  {label} {done}/{len(items)}", flush=True)
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
    r"\s+-\s+.*$|\s+(?:class [a-z]\b|series [a-z]\b|common stock|common shares|ordinary shares|capital stock|"
    r"american depositary|american depository|depositary shares|sponsored adr|adr\b|ads\b|shares of beneficial|"
    r"new york registry|subordinate voting|common units?).*$", re.I)


def normalize_issuer_name(text: str) -> str:
    """'Achillion Pharmaceuticals, Inc. - Common Stock' and 'ACHILLION PHARMACEUTICALS INC' -> 'achillion pharmaceuticals'."""
    text = html.unescape(str(text or "")).strip()
    text = SECURITY_TAIL.sub("", text)
    text = re.sub(r"\s*[\\/]\s*[A-Za-z]{2,4}\s*[\\/]?\s*$", "", text)  # COST PLUS INC/CA/, LANDEC CORP \CA\, X INC / CT
    text = re.sub(r"\([^)]*\)", " ", text)  # Bank of Commerce Holdings (CA), Elmira Savings Bank (The)
    text = re.sub(r"\b([A-Za-z])\.(?=[A-Za-z]\b)", r"\1", text)  # S.A. -> SA., N.V. -> NV.
    text = re.sub(r"['’`]", "", text)  # Conn's -> Conns
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = [ROMAN.get(t, t) for t in text.split() if t not in LEGAL_WORDS]
    while tokens and tokens[-1] in TRAILING_WORDS:
        tokens.pop()
    return " ".join(tokens)


GENERIC_FIRST = {"first", "american", "united", "china", "national", "global", "international", "new", "great",
                 "general", "southern", "northern", "western", "eastern", "pacific", "atlantic", "community", "bank",
                 "citizens", "peoples", "home", "us", "north", "south", "west", "east", "central", "capital"}


def names_match(a: str, b: str) -> bool:
    """Normalized names equal, or one a token prefix of the other (two tokens, or one distinctive token)."""
    if not a or not b:
        return False
    if a == b or a.replace(" ", "") == b.replace(" ", ""):
        return True
    ta, tb = a.split(), b.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if long_[:len(short)] != short:
        return False
    return len(short) >= 2 or (len(short[0]) >= 5 and short[0] not in GENERIC_FIRST)


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


ADR_NAME = re.compile(r"american depositary|american depository|\bADS\b|\bADRs?\b|new york registry", re.I)
ADR_EXCLUDE = re.compile(r"preferred|warrant|\brights?\b|\bunits?\b|notes? due", re.I)


def is_common_equity(name: str) -> bool:
    """Common stock, ordinary shares and ADRs; not preferreds, warrants, units, rights, debt, funds, LPs.

    Unlike ``investable_common_equities`` this keeps every ADR (the plan's ADR flag)
    and SPAC shares, so the identity of a SPAC that becomes an operating company is kept.
    """
    name = str(name or "")
    if ADR_NAME.search(name) and not ADR_EXCLUDE.search(name):
        return True
    return not NON_COMMON.search(name)


# ------------------------------------------------------------------ evidence loaders

def _date_from_name(path: Path) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name) or re.search(r"(\d{8})", path.name)
    if not match:
        return ""
    text = match.group(1)
    return text if "-" in text else f"{text[:4]}-{text[4:6]}-{text[6:]}"


def _read_snapshot(path: Path, source: str) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    columns = {c.lower().strip(): c for c in frame.columns}
    symbol = columns.get("symbol") or columns.get("ticker")
    name = columns.get("name") or columns.get("security name") or columns.get("company name")
    if not symbol or not name:
        return pd.DataFrame()
    observed = columns.get("observed at") or columns.get("snapshot_date") or columns.get("date")
    out = pd.DataFrame({"symbol": frame[symbol].str.strip().str.upper(), "name": frame[name].str.strip()})
    out["date"] = frame[observed].str[:10] if observed else _date_from_name(path)
    for flag in ("ETF", "Test Issue", "NextShares"):
        column = columns.get(flag.lower())
        out[flag] = frame[column].str.upper() if column else ""
    source_file = columns.get("source file")
    out["source"] = source
    out["source_url"] = frame[source_file] if source_file else str(path)
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
        return pd.DataFrame(columns=["symbol", "name", "date", "ETF", "Test Issue", "NextShares", "source", "source_url"])
    snapshots = pd.concat(frames, ignore_index=True)
    return snapshots[(snapshots["symbol"] != "") & (snapshots["date"] != "")]


TYPED_NAME = re.compile(r" - |common stock|ordinary shares?|common shares|capital stock|depositary|depository|"
                        r"warrants?\b|\bunits?\b|\brights?\b|preferred|notes? due|\bETF\b|beneficial interest", re.I)
# Nasdaq fifth-letter codes that mark a non-common issue: warrants, units, rights, preferreds,
# convertible bonds, when-issued, funds, miscellaneous.
NON_COMMON_FIFTH = set("WURPONMLGHIVXZT")
NEAREST_TYPED_DAYS = 3 * 365


def fill_symbol_only_names(snapshots: pd.DataFrame, days: int = 90) -> pd.DataFrame:
    """Rows whose name is just the symbol (the 2019-06-17 file) borrow the name of the nearest other
    row of the same symbol within ``days``; with none the name is left empty."""
    frame = snapshots.copy()
    bare = frame["name"].str.strip().str.upper().eq(frame["symbol"])
    if not bare.any():
        return frame
    frame["_day"] = pd.to_datetime(frame["date"], errors="coerce")
    named = frame[~bare & frame["_day"].notna()][["symbol", "_day", "name"]].rename(columns={"name": "borrowed"})
    rows = frame[bare & frame["_day"].notna()].reset_index()
    near = pd.merge_asof(rows.sort_values("_day"), named.sort_values("_day"), on="_day", by="symbol",
                         direction="nearest", tolerance=pd.Timedelta(days=days)).set_index("index")["borrowed"]
    frame.loc[near.index, "name"] = near.fillna("")
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


class Candidates:
    """ticker -> {cik: set(sources)}."""

    def __init__(self):
        self.map: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))

    def add(self, ticker, cik, source: str) -> None:
        ticker = str(ticker or "").strip().upper()
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
            for ticker in str(row.subject_tickers_sec or "").split():
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


def build_name_index(profiles: dict[int, dict]) -> dict[str, set[int]]:
    index: dict[str, set[int]] = defaultdict(set)
    for cik, profile in profiles.items():
        for name in [profile["name"], *[f["name"] for f in profile["former_names"]]]:
            key = name_key(name)
            if key:
                index[key].add(cik)
    return index


def profile_names(profile: dict) -> list[str]:
    return [n for n in {normalize_issuer_name(x) for x in [profile["name"], *[f["name"] for f in profile["former_names"]]]} if n]


def _shift(day: str, days: int) -> str:
    return (pd.Timestamp(day) + pd.Timedelta(days=days)).date().isoformat()


def profile_name_spans(profile: dict) -> list[tuple[str, str, str]]:
    """(normalized name, from, to) for the current and each former EDGAR name."""
    spans = profile.get("_name_spans")
    if spans is None:
        former = profile.get("former_names") or []
        current_from = max((f["to"] for f in former if f.get("to")), default="") or "0000-00-00"
        spans = [(normalize_issuer_name(profile["name"]), current_from, "9999-12-31")]
        spans += [(normalize_issuer_name(f["name"]), f.get("from") or "0000-00-00", f.get("to") or "9999-12-31")
                  for f in former]
        spans = [span for span in spans if span[0]]
        profile["_name_spans"] = spans
    return spans


def name_valid(profile: dict, key: str, first: str = "", last: str = "", exact: bool = False,
               slack: int = NAME_SLACK_DAYS) -> bool:
    """The CIK carried a name matching ``key`` at some time in first..last (give or take ``slack`` days)."""
    lo = _shift(first, -slack) if first else "0000-00-00"
    hi = _shift(last or first, slack) if (last or first) else "9999-12-31"
    compact = key.replace(" ", "")
    return any((n.replace(" ", "") == compact if exact else names_match(key, n)) and a <= hi and b >= lo
               for n, a, b in profile_name_spans(profile))


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


def covers(profile: dict, first: str, last: str) -> bool:
    lo, hi = activity_window(profile)
    return bool(lo) and lo <= first and hi >= last


def resolve_listing(symbol: str, name: str, cands: Candidates, profiles: dict[int, dict],
                    name_index: dict[str, set[int]], first: str = "", last: str = "",
                    lookup: dict[str, set[int]] | None = None) -> tuple[int | None, str]:
    """(cik, how) for one snapshot (symbol, name) seen first..last.

    Names are compared with the EDGAR name the CIK carried at the time (current and
    former names with their dates). how: name+ticker (a ticker candidate with that
    name), name+lookup (that candidate came from SEC's name list), name_only (the only
    CIK, among every EDGAR entity of that name, filing during the listing),
    name_profiled (the name is too common in EDGAR, but only one profiled company bore
    it and filed then), ticker_only (one mapped ticker candidate filing then, names
    differ), ambiguous (split later by date), unresolved.
    """
    key = normalize_issuer_name(name)
    compact = key.replace(" ", "")
    options = cands.get(symbol)
    ticker_ciks = [c for c in options if c in profiles]
    same_name = set(name_index.get(compact, set())) | (lookup.get(compact, set()) if lookup else set())
    by_name = [c for c in ticker_ciks if name_valid(profiles[c], key, first, last)]
    if len(by_name) > 1:  # a mapped ticker beats a name-list hit; then exact names; then filing at the time
        mapped_by_name = [c for c in by_name if options[c] - {"sec_cik_lookup"}]
        by_name = mapped_by_name if len(mapped_by_name) == 1 else by_name
    if len(by_name) > 1:
        exact = [c for c in by_name if name_valid(profiles[c], key, first, last, exact=True)] or by_name
        active = [c for c in exact if not first or active_during(profiles[c], first, last)] or exact
        by_name = active
    if len(by_name) == 1 and first and not covers(profiles[by_name[0]], first, last):
        # A same-name CIK that filed during only part of the listing too: a holding-company
        # reorganisation (old and new CIK share name and ticker); split by date later.
        partial = [c for c in same_name - set(by_name) if c in profiles and active_during(profiles[c], first, last)
                   and name_valid(profiles[c], key, first, last)]
        if partial:
            return None, "ambiguous"
    if len(by_name) == 1:
        lookup_only = options[by_name[0]] <= {"sec_cik_lookup"}
        return by_name[0], "name+lookup" if lookup_only else "name+ticker"
    if len(by_name) > 1:
        return None, "ambiguous"
    live = [c for c in same_name if c in profiles and (not first or active_during(profiles[c], first, last))
            and name_valid(profiles[c], key, first, last)]
    if len(live) > 1 and first:  # a parent filing periodic reports beats a same-name subsidiary
        reporting = [c for c in live if periodic_during(profiles[c], first, last)]
        live = reporting if len(reporting) == 1 else live
    if 0 < len(same_name) <= LOOKUP_MAX_CIKS and len(live) == 1 and all(c in profiles for c in same_name):
        return live[0], "name_only"
    if len(same_name) > LOOKUP_MAX_CIKS and len(live) == 1 and len(compact) >= 6:
        return live[0], "name_profiled"
    mapped = [c for c in ticker_ciks if options[c] - {"sec_cik_lookup"}]
    if len(mapped) == 1 and (not first or active_during(profiles[mapped[0]], first, last)):
        return mapped[0], "ticker_only"
    if len(mapped) > 1 or len(live) > 1:
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
                    name_index: dict[str, set[int]], lookup: dict[str, set[int]] | None = None) -> dict[str, int]:
    """For a pair whose name matches several CIKs (a holding-company reorganisation reuses name and
    ticker), assign each snapshot date to the candidate that bore the name and whose activity window
    holds the date; where two windows overlap the incumbent (earlier start) keeps it, unless both
    span every date (two unrelated companies), which stays unassigned."""
    key = normalize_issuer_name(name)
    compact = key.replace(" ", "")
    options = {c for c in cands.get(symbol) if c in profiles}
    options |= {c for c in set(name_index.get(compact, set())) | (lookup.get(compact, set()) if lookup else set())
                if c in profiles}
    options = {c for c in options if name_valid(profiles[c], key, dates[0], dates[-1])}
    windows = {c: activity_window(profiles[c]) for c in options}
    span = lambda c: windows[c][0] <= dates[0] and windows[c][1] >= dates[-1]
    out = {}
    for day in dates:
        inside = [c for c, (lo, hi) in windows.items() if lo and lo <= day <= hi
                  and name_valid(profiles[c], key, day, day, slack=DATED_NAME_SLACK_DAYS)]
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
            key = name_key(name)
            if key:
                index[key].add(int(rest))
    return index


def build_intervals(rows: pd.DataFrame, snapshot_dates: dict[str, list[str]]) -> pd.DataFrame:
    """Intervals from resolved snapshot rows (symbol, date, cik, share_class, source, ...).

    ``snapshot_dates`` maps each source family to its sorted snapshot dates, so a
    run breaks when the symbol is missing from two snapshots in a row, or when
    consecutive appearances are more than RUN_BREAK_DAYS apart.
    """
    out = []
    for (family, symbol), group in rows.sort_values("date").groupby(["family", "symbol"], sort=False):
        dates = snapshot_dates[family]
        position = {d: i for i, d in enumerate(dates)}
        run: list = []

        def flush():
            if not run:
                return
            first, last = run[0], run[-1]
            i, j = position[first.date], position[last.date]
            out.append({"cik": first.cik, "share_class": first.share_class, "ticker": symbol,
                        "start": first.date, "end": last.date,
                        "start_prev_absent": dates[i - 1] if i > 0 else "",
                        "end_next_absent": dates[j + 1] if j + 1 < len(dates) else "",
                        "n_snapshots": len(run), "exchange": "NASDAQ", "source": family,
                        "source_url": first.source_url, "match": first.how, "name_in_source": last.name})

        for row in group.itertuples():
            if run:
                prev = run[-1]
                skipped = position[row.date] - position[prev.date] - 1
                gap = (pd.Timestamp(row.date) - pd.Timestamp(prev.date)).days
                if (row.cik, row.share_class) != (prev.cik, prev.share_class) or skipped >= 2 or gap > RUN_BREAK_DAYS:
                    flush()
                    run = []
            run.append(row)
        flush()
    return pd.DataFrame(out)


MERGE_COLUMNS = INTERVAL_COLUMNS + ["sources", "n_evidence_rows"]
MATCH_RANK = {"name+ticker": 0, "name+lookup": 1, "name_only": 2, "name_profiled": 2, "name+dated": 3, "ticker_only": 4}


def merge_intervals(evidence: pd.DataFrame, gap_days: int = RUN_BREAK_DAYS) -> pd.DataFrame:
    """One row per run of a (security, ticker) across every snapshot family: evidence intervals that
    overlap or lie within ``gap_days`` of each other are joined. ``source`` and ``source_url`` are the
    earliest evidence's; ``sources`` lists every family; ``match`` is the weakest match joined."""
    out = []
    for (sid, ticker), group in evidence.sort_values("start").groupby(["security_id", "ticker"], sort=False):
        run: list = []

        def flush():
            if not run:
                return
            first = run[0]
            last = max(run, key=lambda r: r.end)
            weakest = max((r.match for r in run), key=lambda m: MATCH_RANK.get(m, 9))
            out.append({"security_id": sid, "ticker": ticker, "start": first.start, "end": last.end,
                        "exchange": first.exchange, "source": first.source, "source_url": first.source_url,
                        "cik": first.cik, "share_class": first.share_class,
                        "start_prev_absent": first.start_prev_absent, "end_next_absent": last.end_next_absent,
                        "n_snapshots": sum(int(r.n_snapshots) for r in run), "match": weakest,
                        "name_in_source": last.name_in_source,
                        "sources": " ".join(sorted({r.source for r in run})), "n_evidence_rows": len(run)})

        reach = ""
        for row in group.itertuples():
            if run and row.start > _shift(reach, gap_days):
                flush()
                run = []
            run.append(row)
            reach = max(reach, row.end)
        flush()
    return pd.DataFrame(out, columns=MERGE_COLUMNS)


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
    form25 = pd.read_csv(FORM25, dtype={"subject_tickers_sec": str}) if FORM25.exists() else None
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
    pairs = listings.groupby(["symbol", "name"])["date"].agg(["min", "max"]).reset_index()
    resolve_all = lambda: {(p.symbol, p.name): resolve_listing(p.symbol, p.name, cands, profiles, name_index,
                                                               p.min, p.max, lookup) for p in pairs.itertuples()}
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
        found = lookup.get(name_key(name), set())
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
        by_day = resolve_by_date(symbol, name, sorted(group["date"].unique()), cands, profiles, name_index, lookup)
        hit = group.index[group["date"].isin(by_day)]
        listings.loc[hit, "cik"] = group.loc[hit, "date"].map(by_day)
        listings.loc[hit, "how"] = "name+dated"
        dated += len(hit)
    listings["share_class"] = listings["name"].map(share_class_from_name)
    listings["family"] = listings["source"]
    snapshot_dates = {family: sorted(group["date"].unique()) for family, group in snapshots.groupby("source")}
    matched = listings[listings["cik"].notna()].copy()
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
    intervals = build_intervals(matched, snapshot_dates)
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
                            "start_prev_absent": "", "end_next_absent": ""})
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

    master = build_master(profiles, all_intervals, intervals, form25, price_map, multi, missing)
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
    }
    WORK.mkdir(parents=True, exist_ok=True)
    common.atomic_write(WORK / "listing_unresolved.csv", unresolved_pairs.to_csv(index=False).encode())
    common.atomic_write(WORK / "price_file_cik_map.csv", price_map.to_csv(index=False).encode())
    common.atomic_write(WORK / "ticker_reuse.json", (json.dumps(reuse, indent=1) + "\n").encode())
    common.atomic_write(WORK / "ciks_not_found.json", (json.dumps(missing) + "\n").encode())
    common.atomic_write(WORK / "security_master_build_summary.json",
                        (json.dumps({k: v for k, v in stats.items()}, indent=2, default=str) + "\n").encode())
    return master, all_intervals, stats


def build_master(profiles, all_intervals, intervals, form25, price_map, multi, missing) -> pd.DataFrame:
    form25 = form25 if form25 is not None else pd.DataFrame(columns=["subject_cik"])
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
    successor_of, predecessor_of = defaultdict(set), defaultdict(set)
    if "classification_evidence" in form25:
        for row in form25[form25["classification"] == "reorg"].itertuples():
            match = re.search(r"under CIK (\d+)", str(row.classification_evidence))
            if match:
                successor_of[int(row.subject_cik)].add(int(match.group(1)))
                predecessor_of[int(match.group(1))].add(int(row.subject_cik))
    rows = []
    for sid, (cik, share_class) in sorted(keys.items(), key=lambda kv: (kv[1][0], kv[0])):
        profile = profiles.get(cik)
        own = by_sid.get(sid, pd.DataFrame(columns=all_intervals.columns))
        listed = own[own["source"] != "sec_company_tickers_exchange"] if not own.empty else own
        f25 = f25_by_cik.get(cik)
        exits = f25[f25["classification"].isin(["common_delisting", "reorg"])] if f25 is not None else None
        if exits is not None and cik in multi and not exits.empty:
            exits = exits[exits["class_of_security"].str.contains(rf"\bclass {share_class}\b", case=False, na=False)]
        last_exit = exits.sort_values("filing_date").iloc[-1] if exits is not None and not exits.empty else None
        notes = []
        observed = sorted(set(own["ticker"])) if not own.empty else []
        for ticker in observed:
            if ticker in reuse:
                notes.append(f"ticker {ticker} also held by CIK " + " ".join(str(c) for c in reuse[ticker] if c != cik))
        if not listed.empty and (listed["match"] == "ticker_only").any():
            notes.append("some snapshot names did not match SEC names (ticker_only)")
        if f25 is not None and exits is not None and len(exits) > 1:
            notes.append(f"{len(exits)} common 25-NSE filings")
        if cik in missing:
            notes.append("SEC submissions not found")
        if successor_of.get(cik):
            notes.append("25-NSE reorg: successor CIK " + " ".join(map(str, sorted(successor_of[cik]))))
        if predecessor_of.get(cik):
            notes.append("25-NSE reorg: predecessor CIK " + " ".join(map(str, sorted(predecessor_of[cik]))))
        price_rows = prices_by_cik.get(cik)
        if price_rows is not None and cik in multi:
            price_rows = price_rows[price_rows["ticker"].str.split("_").str[0].isin(observed)]
            price_rows = price_rows if not price_rows.empty else None
        first_ticker = ""
        if not listed.empty:
            first_ticker = listed.sort_values("start").iloc[0]["ticker"]
        elif profile and profile["tickers"]:
            first_ticker = profile["tickers"][0]
        elif f25 is not None and str(f25.iloc[0].get("subject_tickers_sec") or "").strip():
            first_ticker = str(f25.iloc[0]["subject_tickers_sec"]).split()[0]
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
        })
    return pd.DataFrame(rows, columns=MASTER_COLUMNS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="use cached responses only")
    args = parser.parse_args(argv)
    master, intervals, stats = build(offline=args.offline)
    common.atomic_write(MASTER, master.to_csv(index=False).encode("utf-8"))
    evidence = intervals.reindex(columns=INTERVAL_COLUMNS).sort_values(["ticker", "start", "source"], na_position="first")
    common.atomic_write(WORK / "ticker_intervals_evidence.csv.gz",
                        gzip.compress(evidence.to_csv(index=False).encode("utf-8"), mtime=0))
    snapshot = evidence[evidence["source"] != "sec_company_tickers_exchange"]
    current = evidence[evidence["source"] == "sec_company_tickers_exchange"].assign(sources="sec_company_tickers_exchange",
                                                                                   n_evidence_rows=1)
    merged = pd.concat([merge_intervals(snapshot), current.reindex(columns=MERGE_COLUMNS)], ignore_index=True)
    merged = merged.sort_values(["ticker", "start", "source"], na_position="first")
    common.atomic_write(INTERVALS, merged.to_csv(index=False).encode("utf-8"))
    stats["interval_rows_merged"] = len(merged)
    stats["interval_rows_merged_snapshot"] = int((merged["source"] != "sec_company_tickers_exchange").sum())
    common.atomic_write(WORK / "security_master_build_summary.json",
                        (json.dumps(stats, indent=2, default=str) + "\n").encode())
    print(json.dumps({k: v for k, v in stats.items() if k != "price_files_unresolved"}, indent=2, default=str))
    print(f"price files unresolved: {len(stats['price_files_unresolved'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
