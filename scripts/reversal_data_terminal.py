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
  a cash-or-stock election is valued at the stock alternative when the acquirer is priced.
  ``acquirer_security_id`` is the acquirer's security in the master (blank when it was not on
  Nasdaq, with the name in ``acquirer_name``);
- ``liquidation``: a redemption or dissolution (Altaba: the liquidating distributions paid
  after the record date, undiscounted);
- ``exchange_move``: the listing moved to another exchange (``destination_exchange``); the
  series continues elsewhere, so there is no terminal return (status ``no_terminal_return``);
- ``bankruptcy_otc``: removed by Nasdaq (Rule 12d2-2(b)), a bankruptcy, a voluntary delisting
  or a bank failure; the value is the first OTC vendor close after the last Nasdaq session
  (before the suspension date the 8-K states), else a sourced existing row, else it is left
  to the owner's D5 rule (status ``awaiting_d5``, no default here);
- ``unknown``: no SEC evidence decided it; listed explicitly, never defaulted.

The automatic reading is overridden by ``REVIEWED`` (hand review of the documents named) for
the rows it gets wrong: another company's terms in the same 8-K, ADS ratios, elections,
renames, reorganisations the snapshots date late, spin-off adjustments (Compuware).

Existing sourced rows are reused: ``stocks_list_dir/nasdaq/terminal_returns.csv``, the
holdout supplement and the sue_lt 2020 file, i.e. the three files that
``research_sue_lt_2020_2026.terminal_map`` merges (``complete_terminal``'s 0.0 default is not
used). Their consideration (and source URL) fills a value this step cannot price; the return
is recomputed against this step's vendor last close and the difference reported.

Last price: the last session with volume > 0 on or before the listing end (a merger's Form 25
filing date; an issuer's own Form 25 effective date; the day before a stated suspension; the
last closing 8-K for a snapshot-dated end), over every vendor raw source (WIKI, the old
Tiingo caches and the step-8 files, Yahoo step 7 and the holdout charts) and the stored
files. The close must come from a vendor on that session and, for a merger, within a week of
the Form 25 filing. Otherwise:
- ``pending_price``: a Tiingo fetch for the security is planned or running and its file has
  not arrived (the run of October 2026, or month 2); rerun the build when it has;
- ``no_vendor_price``: no vendor reaches the last session (the stored files are never used
  for levels);
- ``needs_acquirer_price``: the stock part needs an acquirer close no vendor series has
  (``--yahoo-acquirers`` fetched 20 charts for the most important ones, ACQUIRER_SYMBOLS);
- ``needs_review``: a merger value more than 25% from the last close (a data check on that
  one security); the figure stays in the local cache.

Committed columns hold SEC facts and ratios only: ``consideration_per_share`` is blank where
the value comes from a vendor close (stock parts, OTC closes); the levels are in
``CACHE/terminal/prices_used.csv``.

Outputs:
  INPUTS/terminal_returns_2012_2026.csv   one row per scoped security (plan section 1.1 columns first)
  CACHE/terminal/scope.csv                the scope with every fact used
  CACHE/terminal/filings.csv              the SEC filings near each end that were considered
  CACHE/terminal/evidence.csv, leads.csv.gz   terms and flags per security; every lead read
  CACHE/terminal/prices_used.csv          last close and acquirer close used (vendor levels: local only)
  CACHE/terminal/terminal_summary.json    counts by terminal_type and status; unknowns by best rank
  CACHE/terminal/yahoo_raw/               the acquirer charts
  CACHE/raw/sec/docs/{cik}/{accession}/{document}.gz   the SEC documents read (and index.htm.gz)

Usage::

    PYTHONPATH=. python scripts/reversal_data_terminal.py --offline   # rebuild from the cache (after Tiingo files arrive)
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

MAIN = common.MAIN_CHECKOUT
INPUTS = common.INPUTS
OUT = common.CACHE / "terminal"
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
    rows = rows[ended].copy()
    by_accession = form25.drop_duplicates("accession").set_index("accession")
    keep = ["effective_date", "filing_date", "rule_provision", "delisting_basis", "classification",
            "subject_exit", "successor_cik", "successor_tickers", "tickers_new", "tickers_ended", "doc_url"]
    for column in keep:
        rows[f"f25_{column}"] = rows["delist_form25_accession"].map(by_accession[column]).fillna("")
    rows["transfer_basis"] = rows["transfer_form25_accession"].map(by_accession["delisting_basis"]).fillna("")
    rows["transfer_form25_doc_url"] = rows["transfer_form25_accession"].map(by_accession["doc_url"]).fillna("")
    rows["end_date"] = [
        delist_date or transfer or last
        for delist_date, transfer, last in zip(rows["delist_date"], rows["transfer_date"], rows["last_listed"])]
    rows["end_source"] = ["form25" if d else ("transfer" if t else "snapshots")
                          for d, t in zip(rows["delist_date"], rows["transfer_date"])]
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
        index_path = OUT / "provenance_cache_index.json"
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
            OUT.mkdir(parents=True, exist_ok=True)
            common.atomic_write(index_path, json.dumps({k: str(v) for k, v in found.items()}, indent=0).encode())
    return _PROVENANCE_CACHE


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


def fetch_sec(url: str, path: Path, symbol: str = "", offline: bool = False) -> bytes | None:
    """The body for ``url``: from our cache, else the repo's earlier SEC cache, else one request."""
    if path.exists():
        return gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    if path.with_name(path.name + ".404").exists():
        return None
    reused = _from_provenance(url)
    if reused is not None:
        common.atomic_write(path, gzip.compress(reused, mtime=0) if path.suffix == ".gz" else reused)
        return reused
    if offline:
        return None
    if _STOP["set"]:
        raise StopFetching("stopped after an earlier refusal")
    try:
        return common.cached_get(url, path, source="sec_terminal_docs", headers=common.sec_headers(),
                                 limiter=common.SEC_LIMITER, symbol=symbol)
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


def doc_evidence(text: str, own_names: list[str] | None = None) -> dict:
    """Flags and consideration leads read from one document's text."""
    leads = conversion_leads(text, own_names)
    return {
        "leads": leads,
        "suspension_date": suspension_date(text),
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
                        "filing_date": row.filingDate, "form": row.form, "items": row.items})
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
        out = {"security_id": row.security_id, "n_docs": len(docs), "transfer_to": "",
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
            frame = load_observed_terminal_returns(path)
            raw = pd.read_csv(path, dtype=str, keep_default_na=False)
            frame["note"] = raw["note"].values if "note" in raw else ""
            frame["existing_file"] = str(path).replace(str(MAIN) + "/", "")
            frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["ticker", "last_price_date", "terminal_return", "consideration_per_share",
                                     "source_url", "verified_at", "note", "existing_file"])
    out = pd.concat(frames, ignore_index=True)
    # later files win, as in terminal_map (dict.update order)
    return out.drop_duplicates(["ticker", "last_price_date"], keep="last").reset_index(drop=True)


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
SNAPSHOT_END_SLACK_DAYS = 45  # a snapshot-dated end can come before the last trade by up to a capture gap


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
        if frames:
            out = pd.concat(frames, ignore_index=True)
            out["date"] = pd.to_datetime(out["date"]).dt.normalize()
            out = out[(out["close"] > 0)].sort_values("date").reset_index(drop=True)
        else:
            out = pd.DataFrame(columns=["date", "close", "volume", "src"])
        self._cache[sid] = out
        return out

    def close_on(self, sid: str, day: pd.Timestamp, after: bool = False) -> dict | None:
        """The vendor raw close of ``sid`` on ``day`` (or on the first session after it when ``after``)."""
        rows = self.rows(sid)
        vendor = rows[rows["src"].isin(VENDOR_ORDER) & (rows["volume"].fillna(0) > 0)]
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


WIKI_END = "2018-03-27"
ACQUIRER_QUOTE_DAYS = 7  # the acquirer's close must come within a week after the target's last trade
REVIEW_RETURN = 0.25  # a merger value this far from the last close is held back for review
ANCHOR_TOLERANCE_DAYS = 7  # the last trade of a merged company lies within a week of its Form 25 filing


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


# Acquirers outside the Nasdaq master whose close the stock part needs: one Yahoo chart each
# (``--yahoo-acquirers``, at most YAHOO_MAX requests in all; CACHE/terminal/yahoo_raw). Target
# security_id -> Yahoo symbol of the acquirer as it trades today (BB&T is Truist, TFC).
ACQUIRER_SYMBOLS = {
    "1465112": "T", "1015780": "MS", "889936": "CM", "1378946": "MTB", "921847": "MTB", "77877": "CVX",
    "1620280": "UNIT", "1038205": "OZK", "817473": "ARCC", "354908": "TDY", "1142596": "GMED", "1613859": "ICLR",
    "1644406": "SJM", "1051741": "KEY", "1102112": "BANC", "1499875": "VTR", "1411574": "UNH", "1594012": "CFG",
    "933141": "FHN", "700863": "TFC", "700733": "TFC", "1104188": "SOHU",
}
YAHOO_MAX = 20
YAHOO_RAW = OUT / "yahoo_raw"


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
    """The last session with volume > 0 on or before ``limit``, and the vendor close on it.

    status: ``ok`` (a vendor close on that session, and the session within a week of ``anchor``),
    ``vendor_short`` (no vendor close there: the stored file runs later, or every series stops
    well before the anchor, e.g. at the WIKI end) or ``no_rows``.
    """
    rows = book.rows(sid)
    traded = rows[(rows["date"] <= pd.Timestamp(limit)) & (rows["volume"].fillna(0) > 0)]
    if traded.empty:
        return {"status": "no_rows", "last_date": "", "vendor_last_date": "", "stored_last_date": ""}
    vendor = traded[traded["src"].isin(VENDOR_ORDER)]
    stored = traded[~traded["src"].isin(VENDOR_ORDER)]
    last = traded["date"].max()
    out = {"last_date": last.strftime("%Y-%m-%d"),
           "vendor_last_date": vendor["date"].max().strftime("%Y-%m-%d") if len(vendor) else "",
           "stored_last_date": stored["date"].max().strftime("%Y-%m-%d") if len(stored) else ""}
    close = book.close_on(sid, last)
    short = close is None or (anchor and last < pd.Timestamp(anchor) - pd.Timedelta(days=ANCHOR_TOLERANCE_DAYS))
    if not anchor and close is not None and close["src"] == "wiki" and out["last_date"] == WIKI_END \
            and limit > _shift(WIKI_END, 14):
        short = True  # the WIKI table ends here, the listing did not
    if short:
        return {**out, "status": "vendor_short"}
    return {**out, "status": "ok", "close": close["close"], "src": close["src"], "n_sources": close["n_sources"],
            "max_source_diff": close["max_source_diff"]}


def otc_close(book: PriceBook, sid: str, after: str, within_days: int = 30) -> dict | None:
    """The first vendor close after ``after`` (an OTC continuation of a removed listing)."""
    rows = book.rows(sid)
    later = rows[rows["src"].isin(VENDOR_ORDER) & (rows["date"] > pd.Timestamp(after))
                 & (rows["date"] <= pd.Timestamp(after) + pd.Timedelta(days=within_days)) & (rows["volume"].fillna(0) > 0)]
    if later.empty:
        return None
    first = later.sort_values("date").iloc[0]
    return {"date": first["date"].strftime("%Y-%m-%d"), "close": float(first["close"]), "src": first["src"]}


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
    "status", "status_note", "event_subtype", "destination_exchange", "acquirer_name", "acquirer_match",
    "cvr", "election", "consideration_value_basis", "price_source", "price_source_url", "acquirer_price_date",
    "end_date", "end_source", "form25_accession", "form25_basis", "best_rank", "in_candidates", "cik", "name",
    "terms_stage", "existing_file", "existing_terminal_return", "existing_consideration_per_share",
    "existing_source_url", "existing_return_diff",
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
}
NON_NASDAQ = {"NYSE", "NYSE American", "NYSE Arca", "Cboe BZX", "CBOE"}

# Reviewed by hand from the SEC documents (the closing 8-K or its EX-99.1 unless ``url`` names
# another); each entry overrides the automatic reading. Keys: type, sub, cash, shares, acq
# (acquirer security_id), acq_name, stock_value, rule ('election': cash or stock alternatives,
# valued at the stock alternative when the acquirer is priced, else the cash one), dest, url,
# value (a reviewed total per share), note.
_SEC = "https://www.sec.gov/Archives/edgar/data/"
REVIEWED: dict[str, dict] = {
    # ---- reorganisations, reclassifications and renames into another security (1 share)
    "885721": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1532063",
               "note": "Medco merger: the Company's stockholders received Express Scripts Holding shares one-for-one "
                       "(the $28.80 + 0.81 terms in the 8-K are Medco's)"},
    "1058057": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1835632",
                "note": "redomicile into Marvell Technology, Inc. one-for-one at the Inphi closing (the $66 + 2.323 terms are Inphi's)"},
    "1261694": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1690666",
                "note": "each Tessera share became one Tessera Holding (later Xperi) share at the DTS acquisition "
                        "(the $42.50 cash is DTS's)"},
    "353569": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1906324",
               "note": "each Quidel share became one QuidelOrtho share (the cash and 0.1055 terms are Ortho's)"},
    "864683": {"type": "stock_merger", "sub": "merger", "shares": 1.0, "acq": "1639691",
               "note": "each Cyberonics share became one LivaNova ordinary share (the 0.0472 ratio is Sorin's)"},
    "1316631.B": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1570585.T-LBTYB",
                  "note": "Virgin Media closing: each Liberty Global Series B share became one Liberty Global plc Class B share"},
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
                       "url": _SEC + "1560385/000156038517000004/lmca-20170123ex991483c15.htm",
                       "note": "Liberty Media Group Series A tracking stock renamed Formula One Group (LMCA -> FWONA) after "
                               "the F1 closing, January 2017; the April 2016 recapitalisation is a distribution inside the series"},
    "1560385.T-LMCK": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1560385.T-FWONK",
                       "url": _SEC + "1560385/000156038517000004/lmca-20170123ex991483c15.htm",
                       "note": "Liberty Media Group Series C tracking stock renamed Formula One Group (LMCK -> FWONK), January 2017"},
    "1355096.T-LINTA": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QVCA",
                        "url": _SEC + "1355096/000135509614000070/lint-20141006x8k.htm",
                        "note": "LINTA renamed QVC Group Series A (QVCA) at the open of 2014-10-07; the Liberty Ventures "
                                "share distribution of October 2014 is a distribution inside the series"},
    "1355096.T-LINTB": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QVCB",
                        "url": _SEC + "1355096/000135509614000070/lint-20141006x8k.htm",
                        "note": "LINTB renamed QVC Group Series B (QVCB) at the open of 2014-10-07"},
    "1355096.T-QVCA": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QRTEA",
                       "note": "QVC Group Series A renamed Qurate Retail Series A (QRTEA) in 2018 after the GCI Liberty split-off"},
    "1355096.T-QVCB": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QRTEB",
                       "note": "QVC Group Series B renamed Qurate Retail Series B (QRTEB) in 2018"},
    "1355096.T-QRTEA": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QVCGA",
                        "url": _SEC + "1355096/000110465925016368/tm257272d1_8k.htm",
                        "note": "Qurate Retail renamed QVC Group; QRTEA trades as QVCGA on Nasdaq from 2025-02-24"},
    "1355096.T-QRTEB": {"type": "stock_merger", "sub": "rename", "shares": 1.0, "acq": "1355096.T-QVCGB",
                        "url": _SEC + "1355096/000110465925016368/tm257272d1_8k.htm",
                        "note": "Qurate Retail renamed QVC Group; QRTEB trades as QVCGB on Nasdaq from 2025-02-24"},
    "1355096.T-LVNTA": {"type": "stock_merger", "sub": "split_off", "shares": 1.0, "acq": "",
                        "acq_name": "GCI Liberty, Inc. Class A (GLIBA, Nasdaq; not in the security master)",
                        "url": _SEC + "1355096/000110465918017857/a18-8242_18k.htm",
                        "note": "each LVNTA share redeemed for one GLIBA share in the GCI Liberty split-off, 2018-03-09"},
    "1355096.T-LVNTB": {"type": "stock_merger", "sub": "split_off", "shares": 1.0, "acq": "",
                        "acq_name": "GCI Liberty, Inc. Class B (GLIBB; not in the security master)",
                        "url": _SEC + "1355096/000110465918017857/a18-8242_18k.htm",
                        "note": "each LVNTB share redeemed for one GLIBB share in the GCI Liberty split-off, 2018-03-09"},
    "1100441": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1760173",
                "note": "each RTI Surgical share became one share of the new holding company (RTI Surgical Holdings, later "
                        "Surgalign) at the Paradigm closing, 2019-03-08"},
    "1006269": {"type": "stock_merger", "sub": "merger", "shares": 1.0, "acq": "1845840",
                "note": "each Loral share became one Telesat Corporation share (or, by election, one Telesat Partnership unit)"},
    "1509470": {"type": "exchange_move", "sub": "rename_same_security", "dest": "Nasdaq",
                "note": "SuRo Capital renamed Neostellar Capital and still listed on Nasdaq (NSLR); the listing did not end, "
                        "the master stops at the ticker change"},
    "733269": {"type": "exchange_move", "sub": "rename_and_transfer", "dest": "NYSE",
               "note": "Acxiom sold its marketing solutions unit, renamed itself LiveRamp Holdings and moved to the NYSE "
                       "(RAMP) on 2018-10-01; the shares were not exchanged"},
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
    "1308161.A": {"type": "mixed", "sub": "election", "cash": 51.572626, "shares": 0.4517, "acq": "",
                  "acq_name": "The Walt Disney Company (NYSE: DIS)", "rule": "cash_alternative",
                  "note": "Disney merger 2019-03-20: $51.572626 cash or 0.4517 Disney share by election (prorated), "
                          "near-equal at the close, valued at the cash alternative; the Fox Corporation shares "
                          "(1 per 3) were distributed at 7:25 a.m. on 2019-03-19 and belong to the series as a "
                          "distribution (check that the last vendor close is ex-distribution)"},
    "1308161.B": {"type": "mixed", "sub": "election", "cash": 51.572626, "shares": 0.4517, "acq": "",
                  "acq_name": "The Walt Disney Company (NYSE: DIS)", "rule": "cash_alternative",
                  "note": "as 21CF Class A: $51.572626 cash or 0.4517 Disney share by election, valued at the cash "
                          "alternative; the Fox Corporation Class B distribution of 2019-03-19 belongs to the series"},
    "1054374": {"type": "mixed", "sub": "election", "cash": 54.5, "shares": 0.4378, "acq": "1649338", "rule": "election",
                "note": "election: $54.50 cash or 0.4378 Broadcom Limited share per share (prorated)"},
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
    "1491778": {"type": "stock_merger", "sub": "election", "shares": 1.0, "acq": "1705110",
                "note": "one ANGI Homeservices Class A share per share, or by election $8.50 cash (capped); valued at the stock"},
    "1470215": {"type": "stock_merger", "sub": "merger", "shares": 1.0, "acq": "1140536",
                "note": "2.6490 Willis shares per Towers Watson share followed by Willis's 2.6490-for-1 consolidation: one "
                        "Willis Towers Watson share; the $4.87 special dividend was paid before the close"},
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
    "1602065": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "",
                "acq_name": "New Viper Energy Class A (Nasdaq: VNOM, new holding company)",
                "note": "Sitio combination: each Viper Class A share became one share of the new Viper holding company"},
    "356213": {"type": "stock_merger", "shares": 0.85, "acq": "", "acq_name": "Gaming and Leisure Properties (Nasdaq: GLPI)",
               "extra": [(1.0, "1656239")],
               "note": "0.85 GLPI share per old Pinnacle share, plus one new Pinnacle Entertainment share (1656239) "
                       "distributed at the same closing"},
    "1270400": {"type": "mixed", "cash": 17.50, "shares": 0.2582, "acq": "1570585.T-LBTYA",
                "extra": [(0.1928, "1570585.T-LBTYK")],
                "note": "$17.50 cash + 0.2582 Liberty Global plc Class A + 0.1928 Class C share"},
    "1373707": {"type": "cash_merger", "cash": 2.00,
                "note": "La Jolla closing: $2.00 cash per share plus one CVR (valued at 0); the AcelRx agreement read "
                        "earlier was terminated"},
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
    "1334814": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1617640.A",
                "note": "each Zillow Class A share became one Zillow Group Class A share (Trulia closing, 2015-02-17)"},
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
    "1038205": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "",
                "acq_name": "Bank OZK (Nasdaq; the bank files with the FDIC, not the SEC)",
                "note": "holding company merged into its bank: each share became one Bank OZK share, listed on Nasdaq "
                        "from 2017-06-27 under the same symbol"},
    "912752": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1971213",
               "note": "holding-company reorganisation: each Sinclair Broadcast Group share became one Sinclair, Inc. share "
                       "(2023-06-01); the bankruptcy words in the 8-K concern Diamond Sports"},
    "750004": {"type": "exchange_move", "sub": "listing_transfer", "dest": "ASX",
               "url": _SEC + "750004/000075000425000046/lnw-20250731.htm",
               "note": "sole primary listing moved to the Australian Securities Exchange; Nasdaq delisting November 2025"},
    "1100962": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1593034",
                "note": "each Endo Health Solutions share became one Endo International ordinary share at the Paladin "
                        "closing, 2014-02-28 (the $1.16 cash terms are Paladin's)"},
    "1326807": {"type": "stock_merger", "sub": "second_step_conversion", "shares": 2.55, "acq": "1594012", "limit": "2014-05-07",
                "url": _SEC + "1594012/000119312514179187/d718726d8k.htm",
                "note": "second-step conversion: each public share became 2.5500 shares of the new Investors Bancorp, "
                        "2014-05-07"},
    "1080034": {"type": "mixed", "sub": "election", "cash": 15.14, "shares": 0.07037, "acq": "",
                "acq_name": "Alliance Data Systems (NYSE: ADS)", "stock_value": 0.07037 * 282.2264,
                "note": "standard election: $15.14 cash + 0.07037 Alliance Data share, $35.00 in all at the $282.2264 "
                        "closing VWAP; the Alliance Data close is not available, so the stock part is valued at that VWAP"},
    "1005201": {"type": "stock_merger", "sub": "reorganization", "shares": 1.0, "acq": "1808665", "limit": "2020-05-20",
                "url": _SEC + "1005201/000110465920065440/tm2020220-1_8k.htm",
                "note": "Assertio Therapeutics became a subsidiary of Assertio Holdings, each share converting into one "
                        "Assertio Holdings share (2020-05-20); the 2026 Form 25 on this CIK concerns the successor's "
                        "delisting and the master's 2026 delist date is wrong for this security"},
    "1375365": {"sub": "removed_then_relisted",
                "note": "removed for late filings in August 2018 and traded OTC until it was listed on Nasdaq again on "
                        "2020-01-14 (the same security); the value is the first OTC close after the removal"},
    "2007825_placeholder": {},
    # ---- tender offers with a CVR (valued at 0) and plain cash the reader missed
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
    # ---- liquidations and failures
    "1011006": {"type": "liquidation", "sub": "liquidating_distributions", "value": 22.38,
                "url": _SEC + "1011006/000119312520284475/d98991d8k.htm",
                "note": "Altaba: liquidating distributions to holders of record on 2019-10-04, from 8-Ks 2020-11-03 ($8.33), "
                        "2021-07-26 ($7.48), 2021-08-20 ($0.54), 2021-12-21 ($0.67), 2022-03-07 ($0.24), 2022-05-27 ($0.75), "
                        "2022-07-21 ($1.43), 2023-01-05 ($0.68), 2023-02-09 ($0.96), 2024-07-31 ($1.10), 2025-05-06 ($0.20); "
                        "undiscounted, later distributions possible; the $51.50 initial distribution preceded the delisting "
                        "(NAV $23.08 on 2019-09-30)"},
    "1288784": {"type": "bankruptcy_otc", "sub": "bank_failure",
                "note": "Signature Bank was closed by its regulator and put into FDIC receivership on 2023-03-12; it filed "
                        "with the FDIC, not the SEC, so no SEC document; equity left to the D5 rule"},
    "1001233": {"type": "bankruptcy_otc", "sub": "removed_by_exchange",
                "note": "Nasdaq delisting for the minimum bid price (8-K Item 8.01, 2026-04-29)"},
    "1785041": {"type": "liquidation", "sub": "spac_trust_redemption",
                "note": "business combination terminated 2022-04-15; public shares redeemed at the trust value per share "
                        "(amount not stated in the documents read)"},
}
REVIEWED.pop("2007825_placeholder", None)


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
               "extra": "extra", "limit": "limit"}


def apply_review(row: dict, decided: dict) -> dict:
    """The reading after the hand review in REVIEWED (keys: type, sub, cash, shares, acq, acq_name,
    stock_value, rule, dest, url, value, note); a key that is absent keeps the automatic value."""
    review = REVIEWED.get(row["security_id"])
    out = dict(decided)
    if not review:
        if decided.get("either_or"):
            out["value_rule"] = "election"
        return out
    for key, target in REVIEW_KEYS.items():
        if key in review:
            out[target] = review[key]
    if review.get("type") in ("cash_merger", "liquidation") and "shares" not in review:
        out["shares"] = None
    if review.get("type") == "stock_merger" and "cash" not in review:
        out["cash"] = None
    if review.get("type") in ("exchange_move", "bankruptcy_otc", "unknown"):
        out["cash"], out["shares"] = review.get("cash"), review.get("shares")
    out["reviewed_return"] = bool(review.get("return_checked"))
    out["note"] = ("reviewed: " + review.get("note", "")).strip()
    out["reviewed"] = True
    return out


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
        notes = [decided["note"]] if decided["note"] else []
        prior = existing.get(sid)
        if prior:
            out.update({"existing_file": prior["existing_file"],
                        "existing_terminal_return": _fmt(float(prior["terminal_return"])),
                        "existing_consideration_per_share": _fmt(prior.get("consideration_per_share")),
                        "existing_source_url": prior["source_url"]})
        # ---- the last Nasdaq trade
        limit, anchor = price_window(row, ev)
        if decided.get("limit"):  # a reviewed last trading day (the closing the snapshots only bracket)
            limit, anchor = decided["limit"], ""
        suspended = ev.get("suspension_date") or ""
        if kind == "bankruptcy_otc" and suspended and suspended <= limit:
            limit = _shift(suspended, -1)
        trade = last_trade(book, sid, limit, anchor)
        level = {"security_id": sid, "limit": limit, "anchor": anchor, **{k: trade.get(k, "") for k in (
            "status", "last_date", "vendor_last_date", "stored_last_date", "close", "src", "n_sources", "max_source_diff")}}
        if trade["status"] == "ok":
            out["last_price_date"] = trade["last_date"]
            out["price_source"] = trade["src"]
            out["price_source_url"] = PRICE_SOURCE_URLS[trade["src"]].format(ticker=out["ticker"])
        pending = price_pending(sid, candidates, book) if trade["status"] != "ok" else ""
        price_status = "ok" if trade["status"] == "ok" else ("pending_price" if pending else "no_vendor_price")
        if price_status != "ok":
            notes.append(pending or f"no vendor raw close on the last session ({trade['status']}; vendor to "
                                    f"{trade.get('vendor_last_date') or '-'}, stored to {trade.get('stored_last_date') or '-'})")
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
        elif kind in ("stock_merger", "mixed") and decided.get("stock_value") is not None and (
                decided.get("shares") is None or not decided.get("acquirer_security_id")):
            value = (decided["cash"] or 0.0) + float(decided["stock_value"])
            basis_note = "sec_terms_fixed_value_stock_part"
            out["consideration_per_share"] = _fmt(value)
            notes.append(f"stock part fixed at ${decided['stock_value']:g} of acquirer stock (VWAP ratio not read)")
        elif kind in ("stock_merger", "mixed"):
            acquirer = decided.get("acquirer_security_id") or ""
            how = "reviewed" if acquirer else ""
            if not acquirer and decided["event_subtype"] == "reorganization" and row.get("successor_security_id"):
                acquirer, how = row["successor_security_id"], "successor_link"
            if not acquirer and decided["acquirer_phrase"]:
                acquirer, how = names.find(decided["acquirer_phrase"], trade.get("last_date") or row["end_date"],
                                           hint=decided.get("acquirer_class") or decided["acquirer_phrase"],
                                           exclude_cik=row["cik"])
            out["acquirer_security_id"], out["acquirer_match"] = acquirer, how
            same_series_only = not acquirer and decided["event_subtype"] in ("reorganization", "reclassification", "rename") \
                and decided["shares"] == 1.0
            symbol = ACQUIRER_SYMBOLS.get(sid, "")
            if (acquirer or same_series_only or symbol) and trade["status"] == "ok":
                quote = book.close_on(acquirer, pd.Timestamp(trade["last_date"]), after=True) if acquirer else None
                if quote is None and symbol:
                    if symbol not in charts:
                        charts[symbol] = acquirer_chart(symbol)
                    quote = chart_close_after(charts[symbol], pd.Timestamp(trade["last_date"]))
                    if quote is not None:
                        out["acquirer_match"] = (out["acquirer_match"] + f"; yahoo chart {symbol}").strip("; ")
                same_series = decided["event_subtype"] in ("reorganization", "reclassification", "rename") \
                    and decided["shares"] == 1.0
                if same_series:  # one vendor series under one ticker: the next session may sit on either side
                    own = book.close_on(sid, pd.Timestamp(trade["last_date"]), after=True)
                    if own is not None and (quote is None or own["date"] < quote["date"]):
                        quote = own
                if quote is not None and (quote["date"] - pd.Timestamp(trade["last_date"])).days > ACQUIRER_QUOTE_DAYS:
                    notes.append(f"acquirer's first vendor close after the last trade is {quote['date']:%Y-%m-%d}, too late")
                    quote = None
                if quote is not None and decided.get("value_rule") == "election":
                    value = float(decided["shares"]) * quote["close"]
                    basis_note = "election: stock alternative at the acquirer_vendor_close (local only)"
                    notes.append(f"holders elected ${decided['cash']:g} cash or {decided['shares']:g} shares (prorated); "
                                 "valued at the stock alternative")
                elif quote is not None:
                    value = (decided["cash"] or 0.0) + float(decided["shares"]) * quote["close"]
                    basis_note = "acquirer_vendor_close (local only)"
                    for extra_shares, extra_sid in decided.get("extra") or []:  # further securities in the package
                        extra = book.close_on(extra_sid, pd.Timestamp(trade["last_date"]), after=True)
                        if extra is None or (extra["date"] - pd.Timestamp(trade["last_date"])).days > ACQUIRER_QUOTE_DAYS:
                            notes.append(f"no vendor close for {extra_sid} after the last trade")
                            value = None
                            break
                        value += float(extra_shares) * extra["close"]
                        level[f"extra_{extra_sid}_close"] = extra["close"]
                if quote is not None:
                    out["acquirer_price_date"] = quote["date"].strftime("%Y-%m-%d")
                    level.update({"acquirer_close": quote["close"], "acquirer_src": quote["src"],
                                  "acquirer_date": out["acquirer_price_date"], "value": value})
            if value is None and decided.get("value_rule") == "election" and decided["cash"] is not None:
                notes.append(f"holders elected ${decided['cash']:g} cash or {decided['shares']:g} shares (prorated); "
                             "the stock alternative needs the acquirer's price (the cash one is not used: they can differ)")
            if value is None and prior and prior.get("consideration_per_share") not in (None, "") \
                    and not pd.isna(prior.get("consideration_per_share")):
                value, basis_note = float(prior["consideration_per_share"]), "existing_row_consideration"
                out["consideration_per_share"] = _fmt(value)
                notes.append(f"value from {prior['existing_file']}: {prior.get('note') or ''}".strip())
            if value is None:
                status = "needs_acquirer_price" if price_status == "ok" else price_status
                notes.append(f"acquirer price not available ({how or 'acquirer not identified'})")
        elif kind == "bankruptcy_otc":
            if trade["status"] == "ok":
                quote = otc_close(book, sid, trade["last_date"])
                if quote is not None:
                    value, basis_note = quote["close"], "otc_vendor_close (local only)"
                    level.update({"otc_close": quote["close"], "otc_date": quote["date"], "otc_src": quote["src"]})
            if value is None and prior and prior.get("consideration_per_share") not in (None, "") \
                    and not pd.isna(prior.get("consideration_per_share")):
                value, basis_note = float(prior["consideration_per_share"]), "existing_row_consideration"
                out["consideration_per_share"] = _fmt(value)
                out["source_url"] = prior["source_url"]
                notes.append(f"no OTC vendor price; value from {prior['existing_file']} (sourced there: equity cancelled "
                             "or a later cash-out), booked on the session after the last Nasdaq trade")
            if value is None:
                status = "awaiting_d5"
                notes.append("no OTC vendor price; the owner's D5 rule applies")
        out["consideration_value_basis"] = basis_note
        if basis_note == "existing_row_consideration" and prior:
            out["verified_at"] = pd.Timestamp(prior["verified_at"]).strftime("%Y-%m-%d")
        if value is not None:
            if price_status == "ok":
                ratio = value / trade["close"] - 1.0
                level["terminal_value"], level["terminal_return_checked"] = value, ratio
                if kind != "bankruptcy_otc" and not decided.get("reviewed_return") and abs(ratio) > REVIEW_RETURN:
                    status = "needs_review"  # a data check on this one security's value, not a statistic
                    notes.append(f"value / last close - 1 is beyond +/-{REVIEW_RETURN:.0%}; terms or prices to be checked "
                                 "(the figure is kept in CACHE/terminal/prices_used.csv)")
                else:
                    out["terminal_return"] = _fmt(ratio)
                    status = "computed"
            else:
                status = price_status
        elif not status:
            status = price_status if price_status != "ok" else "unknown"
        if prior and out["terminal_return"]:
            out["existing_return_diff"] = _fmt(float(out["terminal_return"]) - float(prior["terminal_return"]))
        out["status"] = status
        out["status_note"] = "; ".join(n for n in notes if n)
        rows.append(out)
        used.append(level)
    frame = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    return frame, pd.DataFrame(used)


# ------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--offline", action="store_true", help="no requests: build from the cache only")
    parser.add_argument("--scope-only", action="store_true", help="write CACHE/terminal/scope.csv and stop")
    parser.add_argument("--limit", type=int, default=0, help="at most this many documents per fetch stage (a trial)")
    parser.add_argument("--no-fetch", action="store_true", help="skip the SEC fetch stages (same as --offline here)")
    parser.add_argument("--yahoo-acquirers", action="store_true",
                        help=f"fetch the Yahoo charts of ACQUIRER_SYMBOLS not yet cached (at most {YAHOO_MAX} in all)")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
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
    common.atomic_write(OUTPUT, frame.to_csv(index=False).encode())
    common.atomic_write(OUT / "prices_used.csv", used.to_csv(index=False).encode())
    summary = summarize(frame, existing, matched)
    common.atomic_write(OUT / "terminal_summary.json", (json.dumps(summary, indent=1, default=str) + "\n").encode())
    log(f"wrote {OUTPUT}: {len(frame)} rows")
    log("by type: " + json.dumps(summary["rows_by_terminal_type"]))
    log("by status: " + json.dumps(summary["rows_by_status"]))
    return frame


def summarize(frame: pd.DataFrame, existing: pd.DataFrame, matched: dict) -> dict:
    """Counts only (no return is averaged, ranked or otherwise aggregated)."""
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
