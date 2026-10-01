"""Plan step 10 (docs/reversal_2012_2026_data_plan.md, sections 5.1-5.2): earnings dates and point-in-time SIC.

Data only. Nothing here computes returns, signals, return rankings or spreads. It
records when each company's earnings 8-K reached EDGAR, the XNAS session that
first traded on it (D0), and the SIC code printed in each filing's header.

Scope: every CIK that is in ``candidate_fetch_list.csv`` (step 6) or that holds a
dv20 or dv50 rank <= 300 in any week of ``CACHE/prefilter/weekly_metrics.pkl``,
minus foreign filers (security_master ``foreign_filer`` Y; the owner excluded
them). MIXED and UNKNOWN filers stay in scope and are counted.

Steps:
1. Submissions JSON (cached in step 4) plus every older page whose ``filingTo`` is on
   or after 2011-10-01 (``CACHE/raw/sec/submissions/CIK##########-submissions-NNN.json.gz``).
2. Events: 8-K and 8-K/A filed from 2011-10-01 whose ``items`` list holds 2.02.
3. Each event is assigned a fiscal quarter: the latest period end before its filing
   date, from the company's own 10-Q/10-K ``reportDate`` values (extended by 91-day
   steps past the last known period). ``first_in_fiscal_quarter`` marks the earliest
   event of each company-quarter.
4. Fallback: each fiscal quarter that has a 10-Q/10-K (filed from 2011-10-01) but no
   Item 2.02 event gets that report's acceptance (the first original filing for
   the period) in ``earnings_fallback_periodic.csv``.
5. ``-index-headers.html`` for every event and every fallback filing
   (``CACHE/raw/sec/headers/{cik}/{accession}-index-headers.html.gz``), through
   SEC_LIMITER and sec_headers(). ``ACCEPTANCE-DATETIME`` there is Eastern wall-clock
   time and is the value used; the JSON ``acceptanceDateTime`` is kept and compared
   (it is labelled Z but is sometimes Eastern time).
6. D0 = the first XNAS session whose close (16:00 ET, 13:00 on early-close days) is
   strictly after the acceptance time: after the close, or on a non-session day, it is
   the next session.
7. ``sic_history.csv``: one row per header read, the SIC of the block whose CENTRAL
   INDEX KEY is the company (a multi-filer 8-K lists several).

Outputs:
  INPUTS/earnings_events.csv, INPUTS/earnings_fallback_periodic.csv, INPUTS/sic_history.csv
  CACHE/earnings/earnings_summary.json   counts, header/JSON agreement, coverage
  CACHE/earnings/scope.csv, company_year_coverage.csv, no_event_companies.csv

Usage::

    PYTHONPATH=. python scripts/reversal_data_earnings.py                 # fetch what is missing, then build
    PYTHONPATH=. python scripts/reversal_data_earnings.py --offline       # build from the cache only
    PYTHONPATH=. python scripts/reversal_data_earnings.py --limit-ciks 5  # a small trial
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import gzip
import html
import json
from pathlib import Path
import re
import sys
import threading
import time
from urllib.error import HTTPError

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common

INPUTS = common.INPUTS
SEC_RAW = common.RAW / "sec"
SUB_DIR = SEC_RAW / "submissions"
HEADER_DIR = SEC_RAW / "headers"
OUT = common.CACHE / "earnings"
PREFILTER = common.CACHE / "prefilter"
WEEKLY = PREFILTER / "weekly_metrics.pkl"
MASTER = INPUTS / "security_master.csv"
CANDIDATES = INPUTS / "candidate_fetch_list.csv"
EVENTS_OUT = INPUTS / "earnings_events.csv"
FALLBACK_OUT = INPUTS / "earnings_fallback_periodic.csv"
SIC_OUT = INPUTS / "sic_history.csv"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
PAGE_URL = "https://data.sec.gov/submissions/{name}"
HEADER_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}-index-headers.html"
HDR_SGML_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}.hdr.sgml"
INDEX_HEADERS_FROM = "2014-06-01"

EVENTS_FROM = "2011-10-01"
TOP_RANK = 300
EVENT_FORMS = {"8-K", "8-K/A"}
PERIODIC_FORMS = {"10-Q", "10-K", "10-QT", "10-KT", "10-K405", "10-QSB", "10-KSB"}
QUARTER_STEP_DAYS = 91
QUARTER_MAX_LAG_DAYS = 200  # an event this long after the last known period end is not assigned by extension
WORKERS = 8
CHUNK = 400


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def read_csv_text(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


# ------------------------------------------------------------------ scope

def _cik_int(value) -> int | None:
    text = str(value).strip()
    if not text or text.lower() in ("nan", "<na>", "none"):
        return None
    return int(float(text))


def build_scope(weekly: pd.DataFrame, candidates: pd.DataFrame, master: pd.DataFrame) -> pd.DataFrame:
    """One row per in-scope CIK: why it is in scope, its security_ids, its listed and top-300 spans.

    A CIK is in scope when a candidate row names it or one of its weeks has a dv20 or dv50
    rank <= 300. A CIK whose every security is flagged foreign (Y) is left out.
    """
    weekly = weekly[weekly["cik"].notna()]
    top = weekly[(weekly["dv50_rank"] <= TOP_RANK) | (weekly["dv20_rank"] <= TOP_RANK)]
    listed = weekly[weekly["universe"]]
    rows: dict[int, dict] = {}

    def entry(cik: int) -> dict:
        return rows.setdefault(cik, {"cik": cik, "in_top300": "N", "in_candidates": "N", "security_ids": set()})

    for cik, sid in zip(top["cik"].astype(int), top["security_id"].astype(str)):
        item = entry(cik)
        item["in_top300"] = "Y"
        item["security_ids"].add(sid)
    for cik, sid in zip(candidates["cik"], candidates["security_id"].astype(str)):
        value = _cik_int(cik)
        if value is None:
            continue
        item = entry(value)
        item["in_candidates"] = "Y"
        item["security_ids"].add(sid)
    master = master.assign(_cik=master["cik"].map(_cik_int))
    flags = master.groupby("_cik")["foreign_filer"].agg(lambda s: " ".join(sorted(set(s))))
    names = master.groupby("_cik")["name"].first()
    top_span = top.groupby(top["cik"].astype(int))["week_end"].agg(["min", "max", "size"])
    listed_span = listed.groupby(listed["cik"].astype(int))["week_end"].agg(["min", "max"])
    out = []
    for cik, item in sorted(rows.items()):
        flag = flags.get(cik, "")
        out.append({
            "cik": cik, "name": names.get(cik, ""), "security_ids": " ".join(sorted(item["security_ids"])),
            "in_top300": item["in_top300"], "in_candidates": item["in_candidates"], "foreign_filer": flag,
            "excluded_foreign": "Y" if flag == "Y" else "N",
            "listed_first_week": _day(listed_span["min"].get(cik)), "listed_last_week": _day(listed_span["max"].get(cik)),
            "top300_first_week": _day(top_span["min"].get(cik)), "top300_last_week": _day(top_span["max"].get(cik)),
            "top300_weeks": int(top_span["size"].get(cik, 0)),
        })
    return pd.DataFrame(out)


def _day(value) -> str:
    return "" if value is None or pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d")


def load_scope() -> pd.DataFrame:
    weekly = pd.read_pickle(WEEKLY)[["security_id", "cik", "week_end", "universe", "dv50_rank", "dv20_rank"]]
    return build_scope(weekly, read_csv_text(CANDIDATES), read_csv_text(MASTER))


# ------------------------------------------------------------------ SEC fetches (cache first)

class StopFetching(Exception):
    """SEC refused a request (403/429): stop asking and leave the rest for a later run."""


_STOP = threading.Event()


def _sec_get(url: str, path: Path, source: str, symbol: str, offline: bool) -> bytes | None:
    """The body for ``url`` from cache, else one request through SEC_LIMITER; None when absent (404/offline)."""
    if path.exists():
        return gzip.decompress(path.read_bytes())
    if offline or path.with_name(path.name + ".404").exists():
        return None
    if _STOP.is_set():
        raise StopFetching("stopped after an earlier refusal")
    try:
        return common.cached_get(url, path, source=source, headers=common.sec_headers(),
                                 limiter=common.SEC_LIMITER, symbol=symbol)
    except FileNotFoundError:
        return None
    except HTTPError as exc:
        if exc.code in (403, 429):
            _STOP.set()
            raise StopFetching(f"HTTP {exc.code}") from None
        raise


def submissions_path(cik: int) -> Path:
    return SUB_DIR / f"CIK{int(cik):010d}.json.gz"


def load_submissions(cik: int, offline: bool = False) -> dict | None:
    data = _sec_get(SUBMISSIONS_URL.format(cik=int(cik)), submissions_path(cik), "sec_submissions",
                    f"CIK{int(cik)}", offline)
    return json.loads(data) if data else None


def pages_needed(payload: dict, since: str = EVENTS_FROM) -> list[str]:
    """Names of the older submissions pages that hold filings on or after ``since``."""
    files = (payload.get("filings") or {}).get("files") or []
    return [f["name"] for f in files if (f.get("filingTo") or "") >= since and f.get("name")]


def load_page(name: str, offline: bool = False) -> dict | None:
    data = _sec_get(PAGE_URL.format(name=name), SUB_DIR / f"{name}.gz", "sec_submissions",
                    name.split("-")[0], offline)
    return json.loads(data) if data else None


FILING_FIELDS = ["accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form", "items",
                 "primaryDocument", "primaryDocDescription"]


def filing_table(payload: dict, pages: list[dict]) -> pd.DataFrame:
    """Every filing in the recent block and the given older pages, one row per accession."""
    blocks = [(payload.get("filings") or {}).get("recent") or {}] + list(pages)
    frames = []
    for block in blocks:
        n = len(block.get("accessionNumber") or [])
        if n:
            frames.append(pd.DataFrame({f: (block.get(f) or [""] * n) for f in FILING_FIELDS}))
    if not frames:
        return pd.DataFrame(columns=FILING_FIELDS)
    table = pd.concat(frames, ignore_index=True).fillna("")
    for column in FILING_FIELDS:
        table[column] = table[column].astype(str)
    return table.drop_duplicates("accessionNumber").reset_index(drop=True)


def company_filings(cik: int, offline: bool = False) -> dict:
    """Submissions JSON plus the needed older pages for ``cik``: the filing table and fetch facts."""
    payload = load_submissions(cik, offline)
    if payload is None:
        return {"cik": cik, "table": pd.DataFrame(columns=FILING_FIELDS), "pages_needed": 0, "pages_read": 0,
                "missing": "submissions", "fiscal_year_end": ""}
    names = pages_needed(payload)
    pages = [p for p in (load_page(name, offline) for name in names) if p is not None]
    return {"cik": cik, "table": filing_table(payload, pages), "pages_needed": len(names), "pages_read": len(pages),
            "missing": "" if len(pages) == len(names) else "pages", "fiscal_year_end": payload.get("fiscalYearEnd") or ""}


# ------------------------------------------------------------------ events, fiscal quarters, fallback

def has_item(items: str, item: str = "2.02") -> bool:
    return item in [part.strip() for part in str(items).split(",")]


def item202_filings(table: pd.DataFrame, since: str = EVENTS_FROM) -> pd.DataFrame:
    """8-K and 8-K/A filings from ``since`` whose items include 2.02."""
    mask = table["form"].isin(EVENT_FORMS) & (table["filingDate"] >= since) & table["items"].map(has_item)
    return table[mask].sort_values(["filingDate", "acceptanceDateTime", "accessionNumber"]).reset_index(drop=True)


def periodic_filings(table: pd.DataFrame) -> pd.DataFrame:
    """Original (not amended) 10-Q/10-K filings that carry a period of report."""
    mask = table["form"].isin(PERIODIC_FORMS) & (table["reportDate"] != "")
    return table[mask].sort_values(["filingDate", "acceptanceDateTime", "accessionNumber"]).reset_index(drop=True)


def period_ends(table: pd.DataFrame) -> list[str]:
    """Known fiscal period ends: the reportDate of every 10-Q/10-K, amendments included."""
    bases = table["form"].str.replace(r"/A$", "", regex=True)
    ends = table.loc[bases.isin(PERIODIC_FORMS) & (table["reportDate"] != ""), "reportDate"]
    return sorted(set(ends))


def assign_quarter(filing_date: str, ends: list[str]) -> tuple[str, str]:
    """(fiscal quarter end, how) for an event filed on ``filing_date``.

    The quarter is the latest known period end strictly before the filing date. When that
    end is more than a quarter back (a company that stopped filing 10-Qs, or skipped some)
    the grid is extended from it by 91-day steps; an event more than QUARTER_MAX_LAG_DAYS
    after its latest known end, or before the first one, is left unassigned.
    """
    before = [e for e in ends if e < filing_date]
    if not before:
        return "", "none"
    last = before[-1]
    lag = (pd.Timestamp(filing_date) - pd.Timestamp(last)).days
    if lag <= QUARTER_STEP_DAYS + 15:
        return last, "report_date"
    if lag > QUARTER_MAX_LAG_DAYS:
        return "", "none"
    steps = (lag - 1) // QUARTER_STEP_DAYS
    return (pd.Timestamp(last) + pd.Timedelta(days=QUARTER_STEP_DAYS * steps)).strftime("%Y-%m-%d"), "extended"


def mark_first_in_quarter(events: pd.DataFrame) -> pd.Series:
    """Y for the earliest event (by acceptance, then accession) of each (cik, fiscal quarter)."""
    if events.empty:
        return pd.Series([], dtype=str)
    order = events.sort_values(["cik", "fiscal_quarter_end", "acceptance_sort", "accession"])
    first = ~order.duplicated(["cik", "fiscal_quarter_end"]) & (order["fiscal_quarter_end"] != "")
    return first.reindex(events.index).map({True: "Y", False: "N"})


def fallback_filings(table: pd.DataFrame, event_quarters: set[str], since: str = EVENTS_FROM) -> pd.DataFrame:
    """The first original 10-Q/10-K for each period that has no Item 2.02 event, filed from ``since``.

    ``other_8k_between`` lists ('date:items', ';'-joined) the company's 8-Ks with Item 7.01 or 8.01
    filed after the period end and up to the periodic filing: some companies (Urban Outfitters
    since 2017) put the earnings release under 8.01, so the release may predate the fallback.
    It is a flag for review, never used as the event.
    """
    periodic = periodic_filings(table)
    periodic = periodic[periodic["filingDate"] >= since]
    first = periodic.drop_duplicates("reportDate", keep="first")
    out = first[~first["reportDate"].isin(event_quarters)].reset_index(drop=True)
    others = table[table["form"].isin(EVENT_FORMS)
                   & table["items"].map(lambda items: has_item(items, "7.01") or has_item(items, "8.01"))]
    others = others.sort_values("filingDate")
    out["other_8k_between"] = [
        ";".join(f"{d}:{i}" for d, i in zip(o["filingDate"], o["items"]))
        for o in (others[(others["filingDate"] > period) & (others["filingDate"] <= filed)]
                  for period, filed in zip(out["reportDate"], out["filingDate"]))]
    return out


# ------------------------------------------------------------------ index headers

def header_path(cik: int, accession: str, kind: str = "index_headers") -> Path:
    name = f"{accession}-index-headers.html.gz" if kind == "index_headers" else f"{accession}.hdr.sgml.gz"
    return HEADER_DIR / str(int(cik)) / name


def header_url(cik: int, accession: str, kind: str = "index_headers") -> str:
    url = HEADER_URL if kind == "index_headers" else HDR_SGML_URL
    return url.format(cik=int(cik), folder=accession.replace("-", ""), accession=accession)


def header_kinds(filing_date: str) -> tuple[str, str]:
    """The header files to try, in order. EDGAR has no -index-headers.html for filings before
    about 2014-06 (all 61 such requests in the five-CIK trial were 404); the same SGML header
    is served as {accession}.hdr.sgml, so older filings ask for that first."""
    if filing_date and filing_date < INDEX_HEADERS_FROM:
        return ("hdr_sgml", "index_headers")
    return ("index_headers", "hdr_sgml")


def _absent(path: Path) -> bool:
    return path.with_name(path.name + ".404").exists()


def cached_header_kind(cik: int, accession: str) -> str:
    """Which header file is cached for the filing ('' when none)."""
    for kind in ("index_headers", "hdr_sgml"):
        if header_path(cik, accession, kind).exists():
            return kind
    return ""


def header_settled(cik: int, accession: str) -> bool:
    """A header is cached, or both header files are known to be absent."""
    return bool(cached_header_kind(cik, accession)) or all(
        _absent(header_path(cik, accession, kind)) for kind in ("index_headers", "hdr_sgml"))


def load_header(cik: int, accession: str, offline: bool = False, filing_date: str = "") -> tuple[str | None, str]:
    """(header text, kind) from the cache, else fetched: the preferred file, then the other on a 404."""
    cached = cached_header_kind(cik, accession)
    if cached:
        return gzip.decompress(header_path(cik, accession, cached).read_bytes()).decode("utf-8", "replace"), cached
    for kind in header_kinds(filing_date):
        data = _sec_get(header_url(cik, accession, kind), header_path(cik, accession, kind), "sec_headers",
                        f"CIK{int(cik)}", offline)
        if data:
            return data.decode("utf-8", errors="replace"), kind
    return None, ""


_TAG = re.compile(r"^<([A-Z0-9-]+)>(.*)$")
_SIC_TEXT = re.compile(r"STANDARD INDUSTRIAL CLASSIFICATION:\s*(.*?)\s*\[(\d{4})\]")


def _sgml_lines(text: str) -> list[str]:
    """The SGML header lines (the HTML comment block, or the escaped <PRE> copy)."""
    start = text.find("<SEC-HEADER>")
    if start < 0:
        text = html.unescape(text)
        start = text.find("<SEC-HEADER>")
    if start < 0:
        return []
    end = text.find("</SEC-HEADER>", start)
    return [line.strip() for line in text[start:end if end > 0 else None].splitlines()]


def parse_header(text: str, cik: int) -> dict:
    """Acceptance time (Eastern wall clock), form, period, items and the company's SIC from an index header.

    ``sic_match`` is ``cik`` when a filer block carries the company's CIK, ``first_filer``
    when none does (the first block is used) and ``none`` when the header has no filer block.
    """
    out = {"acceptance_header_et": "", "header_form": "", "header_period": "", "header_filing_date": "",
           "header_items": "", "header_sic": "", "header_sic_description": "", "header_name": "",
           "sic_match": "none", "n_filers": 0}
    blocks, current, section, items = [], None, "", []
    for line in _sgml_lines(text):
        match = _TAG.match(line)
        if not match:
            if line.startswith("</"):
                tag = line[2:].rstrip(">")
                if tag in ("FILER", "SUBJECT-COMPANY", "FILED-BY") and current is not None:
                    blocks.append(current)
                    current = None
                elif tag == "COMPANY-DATA":
                    section = ""
            continue
        tag, value = match.group(1), match.group(2).strip()
        if tag == "ACCEPTANCE-DATETIME" and len(value) >= 14:
            out["acceptance_header_et"] = f"{value[:4]}-{value[4:6]}-{value[6:8]} {value[8:10]}:{value[10:12]}:{value[12:14]}"
        elif tag == "TYPE" and not out["header_form"]:
            out["header_form"] = value
        elif tag == "PERIOD":
            out["header_period"] = f"{value[:4]}-{value[4:6]}-{value[6:8]}" if len(value) == 8 else value
        elif tag == "FILING-DATE":
            out["header_filing_date"] = f"{value[:4]}-{value[4:6]}-{value[6:8]}" if len(value) == 8 else value
        elif tag == "ITEMS":
            items.append(value)
        elif tag in ("FILER", "SUBJECT-COMPANY", "FILED-BY"):
            current = {"role": tag, "cik": None, "sic": "", "name": ""}
        elif tag == "COMPANY-DATA":
            section = "company"
        elif current is not None and section == "company":
            if tag == "CIK" and value.isdigit():
                current["cik"] = int(value)
            elif tag == "ASSIGNED-SIC":
                current["sic"] = value
            elif tag == "CONFORMED-NAME":
                current["name"] = value
    out["header_items"] = ",".join(items)
    filers = [b for b in blocks if b["role"] == "FILER"] or blocks
    out["n_filers"] = len(filers)
    chosen = next((b for b in filers if b["cik"] == int(cik)), None)
    if chosen is not None:
        out["sic_match"] = "cik"
    elif filers:
        chosen, out["sic_match"] = filers[0], "first_filer"
    if chosen is not None:
        out["header_sic"] = chosen["sic"].zfill(4) if chosen["sic"].isdigit() else chosen["sic"]
        out["header_name"] = chosen["name"]
        descriptions = {code: desc for desc, code in _SIC_TEXT.findall(html.unescape(text))}
        out["header_sic_description"] = descriptions.get(out["header_sic"], "")
    return out


# ------------------------------------------------------------------ times and D0

class XnasCloses:
    """XNAS sessions with their closes in Eastern wall-clock time (16:00, or 13:00 on early-close days)."""

    def __init__(self, start: str = "2011-06-01", end: str = "2027-09-30"):
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XNAS", start="2010-01-04", end="2027-10-01")
        sessions = calendar.sessions_in_range(start, end)
        closes = calendar.closes.loc[sessions]
        self.sessions = pd.DatetimeIndex(sessions).tz_localize(None) if sessions.tz is not None else pd.DatetimeIndex(sessions)
        self.closes = pd.DatetimeIndex(closes.dt.tz_convert("America/New_York").dt.tz_localize(None))
        self.opens = pd.DatetimeIndex(calendar.opens.loc[sessions].dt.tz_convert("America/New_York").dt.tz_localize(None))

    def d0(self, acceptance_et: str) -> tuple[str, str]:
        """(D0 session, timing) for an Eastern acceptance time 'YYYY-MM-DD HH:MM:SS'.

        D0 is the first session whose close is strictly after the acceptance; timing is
        pre_open / intraday / after_close (same calendar day as a session) or non_session.
        """
        if not acceptance_et:
            return "", ""
        stamp = pd.Timestamp(acceptance_et)
        index = int(self.closes.searchsorted(stamp, side="right"))
        if index >= len(self.closes):
            return "", ""
        session = self.sessions[index]
        day = stamp.normalize()
        position = self.sessions.searchsorted(day)
        if position < len(self.sessions) and self.sessions[position] == day:
            if stamp < self.opens[position]:
                timing = "pre_open"
            elif stamp < self.closes[position]:
                timing = "intraday"
            else:
                timing = "after_close"
        else:
            timing = "non_session"
        return session.strftime("%Y-%m-%d"), timing


def json_naive(value: str) -> str:
    """'2016-04-26T20:31:09.000Z' -> '2016-04-26 20:31:09' (the label is not trusted)."""
    text = str(value).strip()
    if len(text) < 19:
        return ""
    return text[:10] + " " + text[11:19]


def utc_to_et(naive_utc: str) -> str:
    if not naive_utc:
        return ""
    stamp = pd.Timestamp(naive_utc).tz_localize("UTC").tz_convert("America/New_York").tz_localize(None)
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def json_label(json_raw: str, header_et: str) -> str:
    """How the JSON acceptanceDateTime relates to the header: utc, et_labelled_z, other or no_header."""
    naive = json_naive(json_raw)
    if not header_et:
        return "no_header"
    if not naive:
        return "no_json"
    if utc_to_et(naive) == header_et:
        return "utc"
    if naive == header_et:
        return "et_labelled_z"
    return "other"


def resolve_acceptance(json_raw: str, header_et: str, month_rule: str = "utc") -> tuple[str, str]:
    """(Eastern acceptance time, tz_resolution). The header wins; without one the JSON is read
    by ``month_rule`` (the majority label of headers in the same filing month)."""
    if header_et:
        return header_et, "header"
    naive = json_naive(json_raw)
    if not naive:
        return "", "none"
    if month_rule == "et_labelled_z":
        return naive, "json_et_rule"
    return utc_to_et(naive), "json_utc"


# ------------------------------------------------------------------ per-company plan

def plan_company(cik: int, offline: bool = False) -> dict:
    """Item 2.02 events (with fiscal quarters) and fallback periodic filings for one CIK, from its submissions."""
    facts = company_filings(cik, offline)
    table = facts.pop("table")
    ends = period_ends(table)
    events = item202_filings(table).copy()
    quarters = [assign_quarter(day, ends) for day in events["filingDate"]]
    events["fiscal_quarter_end"] = [q for q, _ in quarters]
    events["fiscal_quarter_how"] = [h for _, h in quarters]
    fallback = fallback_filings(table, set(events["fiscal_quarter_end"]) - {""})
    periodic = periodic_filings(table)
    return {**facts, "events": events, "fallback": fallback, "filer_forms": filer_forms(table),
            "n_periodic_since": int((periodic["filingDate"] >= EVENTS_FROM).sum()),
            "first_filing": table["filingDate"].min() if len(table) else "",
            "last_filing": table["filingDate"].max() if len(table) else ""}


FLAG_FROM = "2011-06-01"  # the window step 4 used for the foreign-filer flag
FOREIGN_PERIODIC = {"20-F", "40-F"}
DOMESTIC_PERIODIC = PERIODIC_FORMS


def filer_forms(table: pd.DataFrame, since: str = FLAG_FROM) -> pd.DataFrame:
    """(filingDate, base form) of every 10-K/10-Q/20-F/40-F/6-K filed from ``since`` (amendments as the base form)."""
    base = table["form"].str.replace(r"/A$", "", regex=True)
    keep = base.isin(DOMESTIC_PERIODIC | FOREIGN_PERIODIC | {"6-K"}) & (table["filingDate"] >= since)
    return pd.DataFrame({"filingDate": table.loc[keep, "filingDate"], "form": base[keep]}).sort_values("filingDate")


def foreign_flag_check(scope: pd.DataFrame, plans: dict[int, dict], weekly: pd.DataFrame) -> pd.DataFrame:
    """The foreign-filer flag recomputed from every submissions page read here (step 4 read only the
    recent block unless it held no periodic report), with the top-300 weeks whose latest periodic
    report (filed on or before the week) is a 20-F/40-F. Rows only where the flags differ."""
    top = weekly[weekly["cik"].notna() & ((weekly["dv50_rank"] <= TOP_RANK) | (weekly["dv20_rank"] <= TOP_RANK))]
    top = top.assign(cik=top["cik"].astype(int))[["cik", "week_end"]].drop_duplicates()
    rows = []
    for record in scope.to_dict("records"):
        cik = int(record["cik"])
        forms = plans.get(cik, {}).get("filer_forms")
        if forms is None or forms.empty:
            continue
        domestic = forms[forms["form"].isin(DOMESTIC_PERIODIC)]
        foreign = forms[forms["form"].isin(FOREIGN_PERIODIC | {"6-K"})]
        flag = {(True, False): "N", (False, True): "Y", (True, True): "MIXED"}.get((len(domestic) > 0, len(foreign) > 0), "UNKNOWN")
        if flag == record["foreign_filer"]:
            continue
        periodic = forms[forms["form"].isin(DOMESTIC_PERIODIC | FOREIGN_PERIODIC)]
        weeks = top.loc[top["cik"] == cik, "week_end"].sort_values()
        foreign_weeks = 0
        if len(periodic) and len(weeks):
            dates = pd.to_datetime(periodic["filingDate"]).to_numpy()
            is_foreign = periodic["form"].isin(FOREIGN_PERIODIC).to_numpy()
            position = np.searchsorted(dates, weeks.to_numpy(), side="right") - 1
            latest = np.where(position >= 0, is_foreign[np.clip(position, 0, None)], is_foreign[0])
            foreign_weeks = int(latest.sum())
        rows.append({"cik": cik, "name": record["name"], "flag_security_master": record["foreign_filer"],
                     "flag_all_pages": flag, "domestic_first": domestic["filingDate"].min() if len(domestic) else "",
                     "domestic_last": domestic["filingDate"].max() if len(domestic) else "",
                     "foreign_first": foreign["filingDate"].min() if len(foreign) else "",
                     "foreign_last": foreign["filingDate"].max() if len(foreign) else "",
                     "n_20f_40f": int(foreign["form"].isin(FOREIGN_PERIODIC).sum()), "n_6k": int((foreign["form"] == "6-K").sum()),
                     "top300_weeks": int(len(weeks)), "top300_weeks_latest_periodic_foreign": foreign_weeks})
    return pd.DataFrame(rows)


def plan_all(ciks: list[int], offline: bool = False) -> tuple[dict[int, dict], dict[int, str]]:
    """(plans, failures): ``plan_company`` for every CIK (older pages fetched as needed), with progress lines."""
    plans: dict[int, dict] = {}
    failures: dict[int, str] = {}
    started = time.time()
    for start in range(0, len(ciks), CHUNK):
        chunk = ciks[start:start + CHUNK]
        results = common.parallel_map(lambda cik: plan_company(cik, offline), chunk, WORKERS)
        for cik, result in zip(chunk, results):
            if isinstance(result, Exception):
                log(f"  CIK {cik}: {type(result).__name__}: {result}")
                failures[cik] = f"{type(result).__name__}: {result}"
                continue
            plans[cik] = result
        log(f"  submissions: {min(start + CHUNK, len(ciks))}/{len(ciks)} CIKs planned "
            f"({time.time() - started:.0f}s; pages read {sum(p['pages_read'] for p in plans.values())})")
        if _STOP.is_set():
            log("  SEC refused a request: stopping the submissions stage")
            break
    return plans, failures


def header_jobs(plans: dict[int, dict]) -> list[tuple[int, str, str, str]]:
    """(cik, accession, filing date, kind) for every event and fallback filing."""
    jobs = []
    for cik, plan in plans.items():
        for frame, kind in ((plan["events"], "item202"), (plan["fallback"], "periodic_fallback")):
            jobs += [(cik, acc, day, kind) for acc, day in zip(frame["accessionNumber"], frame["filingDate"])]
    return jobs


def fetch_headers(jobs: list[tuple[int, str, str, str]], offline: bool = False) -> Counter:
    """Fetch every header not yet settled, in chunks, printing progress; stops on an SEC refusal."""
    todo = [(cik, acc, day) for cik, acc, day, _ in jobs if not header_settled(cik, acc)]
    counts = Counter(cached=len(jobs) - len(todo))
    log(f"headers: {len(jobs)} needed, {counts['cached']} cached, {len(todo)} to fetch")
    if offline or not todo:
        counts["not_fetched_offline"] = len(todo) if offline else 0
        return counts
    started = time.time()

    def one(item):
        cik, acc, day = item
        text, kind = load_header(cik, acc, filing_date=day)
        return kind if text is not None else "missing_both"

    for start in range(0, len(todo), CHUNK):
        chunk = todo[start:start + CHUNK]
        for result in common.parallel_map(one, chunk, WORKERS):
            counts[result if isinstance(result, str) else f"error_{type(result).__name__}"] += 1
        done = min(start + CHUNK, len(todo))
        elapsed = time.time() - started
        eta = elapsed / done * (len(todo) - done) if done else 0
        log(f"  headers: {done}/{len(todo)} ({elapsed / 60:.1f} min, eta {eta / 60:.1f} min) "
            + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if _STOP.is_set():
            log("  SEC refused a request: stopping; re-run later to fetch the rest")
            break
    return counts


# ------------------------------------------------------------------ build the tables

EVENT_COLUMNS = ["cik", "security_id", "accession", "form", "items", "acceptance_json_raw", "acceptance_header_et",
                 "tz_resolution", "filing_date", "d0_session", "first_in_fiscal_quarter", "source",
                 # extras
                 "acceptance_et", "acceptance_timing", "json_label", "report_date", "fiscal_quarter_end",
                 "fiscal_quarter_how", "header_file"]
# The header's SIC goes to sic_history.csv; its form type matched the JSON form on every filing read.
SIC_COLUMNS = ["cik", "observed_date", "sic", "source_accession",
               # extras
               "form", "sic_description", "sic_match", "blank_check_6770", "sic_changed", "operating_sic_after_6770"]


def _rows(cik: int, frame: pd.DataFrame, source: str, security_ids: str) -> list[dict]:
    """Table rows for ``frame``'s filings, with each cached header parsed (blank fields when none)."""
    rows = []
    for record in frame.to_dict("records"):
        acc = record["accessionNumber"]
        text, kind = load_header(cik, acc, offline=True)
        parsed = parse_header(text or "", cik)
        rows.append({
            "cik": cik, "security_id": security_ids, "accession": acc, "form": record["form"],
            "items": record["items"], "acceptance_json_raw": record["acceptanceDateTime"],
            "acceptance_header_et": parsed["acceptance_header_et"], "filing_date": record["filingDate"],
            "source": source, "report_date": record["reportDate"],
            "fiscal_quarter_end": record.get("fiscal_quarter_end", record["reportDate"]),
            "fiscal_quarter_how": record.get("fiscal_quarter_how", "report_date"),
            "header_form": parsed["header_form"], "header_sic": parsed["header_sic"],
            "header_sic_description": parsed["header_sic_description"], "sic_match": parsed["sic_match"],
            "primary_document": record["primaryDocument"], "header_file": kind,
            "other_8k_between": record.get("other_8k_between", ""),
            "json_label": json_label(record["acceptanceDateTime"], parsed["acceptance_header_et"]),
        })
    return rows


def label_keys(row: dict) -> tuple[str, str]:
    """Keys for the JSON-label rule: (filer agent and year, filing month). The label follows the
    filing agent (the accession prefix): in the trial, agent-filed 8-Ks carried Eastern time
    labelled Z and self-filed ones true UTC, in the same months."""
    return f"{row['accession'][:10]}|{row['filing_date'][:4]}", row["filing_date"][:7]


def label_rules(rows: list[dict]) -> dict[str, str]:
    """Majority JSON label (utc / et_labelled_z) among headers for each agent-year and each month."""
    votes: dict[str, Counter] = {}
    for row in rows:
        if row["json_label"] in ("utc", "et_labelled_z"):
            for key in label_keys(row):
                votes.setdefault(key, Counter())[row["json_label"]] += 1
    return {key: counter.most_common(1)[0][0] for key, counter in votes.items()}


def finish_rows(rows: list[dict], calendar: XnasCloses, rules: dict[str, str]) -> pd.DataFrame:
    for row in rows:
        agent, month = label_keys(row)
        rule = rules.get(agent) or rules.get(month) or "utc"
        row["acceptance_et"], row["tz_resolution"] = resolve_acceptance(
            row["acceptance_json_raw"], row["acceptance_header_et"], rule)
        row["d0_session"], row["acceptance_timing"] = calendar.d0(row["acceptance_et"])
        row["acceptance_sort"] = row["acceptance_et"] or row["filing_date"]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS + ["header_sic_description", "acceptance_sort"])
    return frame


def build_tables(plans: dict[int, dict], scope: pd.DataFrame, calendar: XnasCloses) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(events, fallback) from the plans and the cached headers."""
    ids = dict(zip(scope["cik"].astype(int), scope["security_ids"]))
    event_rows, fallback_rows = [], []
    for n, (cik, plan) in enumerate(sorted(plans.items()), 1):
        event_rows += _rows(cik, plan["events"], "item202", ids.get(cik, ""))
        fallback_rows += _rows(cik, plan["fallback"], "periodic_fallback", ids.get(cik, ""))
        if n % 400 == 0:
            log(f"  parsed headers for {n}/{len(plans)} CIKs")
    rules = label_rules(event_rows + fallback_rows)
    events = finish_rows(event_rows, calendar, rules)
    fallback = finish_rows(fallback_rows, calendar, rules)
    if len(events):
        events["first_in_fiscal_quarter"] = mark_first_in_quarter(events)
    if len(fallback):
        fallback["first_in_fiscal_quarter"] = "Y"
    order = ["cik", "acceptance_sort", "accession"]
    events = events.sort_values(order).reset_index(drop=True) if len(events) else events
    fallback = fallback.sort_values(order).reset_index(drop=True) if len(fallback) else fallback
    return events, fallback


def sic_history(events: pd.DataFrame, fallback: pd.DataFrame) -> pd.DataFrame:
    """One row per header with a SIC: the code observed on that acceptance date, change and 6770 flags."""
    frames = [f for f in (events, fallback) if len(f)]
    if not frames:
        return pd.DataFrame(columns=SIC_COLUMNS)
    both = pd.concat(frames, ignore_index=True)
    both = both[(both["header_sic"] != "") & (both["acceptance_header_et"] != "")].copy()
    both["observed_date"] = both["acceptance_header_et"].str[:10]
    both = both.sort_values(["cik", "acceptance_header_et", "accession"]).drop_duplicates(["cik", "accession"])
    out = pd.DataFrame({
        "cik": both["cik"].astype(int), "observed_date": both["observed_date"], "sic": both["header_sic"],
        "source_accession": both["accession"], "form": both["form"],
        "sic_description": both["header_sic_description"], "sic_match": both["sic_match"],
    }).reset_index(drop=True)
    out["blank_check_6770"] = np.where(out["sic"] == "6770", "Y", "N")
    previous = out.groupby("cik")["sic"].shift()
    out["sic_changed"] = np.where(previous.notna() & (previous != out["sic"]), "Y", "N")
    after = []
    for _, group in out.groupby("cik", sort=False):
        codes = group["sic"].tolist()
        later = [""] * len(codes)
        nxt = ""
        for i in range(len(codes) - 1, -1, -1):
            later[i] = nxt if codes[i] == "6770" else ""
            if codes[i] not in ("6770", "0000", ""):
                nxt = codes[i]
        after += later
    out["operating_sic_after_6770"] = after
    return out[SIC_COLUMNS]


# ------------------------------------------------------------------ coverage (counts only)

def presence(weekly: pd.DataFrame, ciks: set[int]) -> pd.DataFrame:
    """Per (cik, calendar quarter): weeks listed, weeks in the top 300, and the quarter's week count."""
    weekly = weekly[weekly["cik"].notna() & weekly["universe"]].copy()
    weekly["cik"] = weekly["cik"].astype(int)
    weekly = weekly[weekly["cik"].isin(ciks)]
    weekly["top"] = (weekly["dv50_rank"] <= TOP_RANK) | (weekly["dv20_rank"] <= TOP_RANK)
    weekly["quarter"] = weekly["week_end"].dt.to_period("Q").astype(str)
    # one row per cik-week (a multi-class company counts once)
    per_week = weekly.groupby(["cik", "quarter", "week_end"])["top"].any().reset_index()
    out = per_week.groupby(["cik", "quarter"]).agg(listed_weeks=("week_end", "size"), top_weeks=("top", "sum")).reset_index()
    total = weekly.drop_duplicates("week_end").groupby("quarter").size().rename("quarter_weeks")
    return out.merge(total, left_on="quarter", right_index=True)


def quarter_coverage(present: pd.DataFrame, events: pd.DataFrame, fallback: pd.DataFrame) -> pd.DataFrame:
    """``present`` plus event counts by D0 calendar quarter: all Item 2.02, first-in-quarter, fallback."""
    def counts(frame: pd.DataFrame, name: str, mask=None) -> pd.Series:
        if frame.empty:
            return pd.Series(dtype=int, name=name)
        frame = frame[frame["d0_session"] != ""]
        if mask is not None:
            frame = frame[mask(frame)]
        quarter = pd.PeriodIndex(pd.to_datetime(frame["d0_session"]), freq="Q").astype(str)
        result = frame.groupby([frame["cik"].astype(int).values, np.asarray(quarter)]).size().rename(name)
        result.index.names = ["cik", "quarter"]
        return result

    out = present.set_index(["cik", "quarter"])
    for series in (counts(events, "n_item202"),
                   counts(events, "n_item202_first", lambda f: f["first_in_fiscal_quarter"] == "Y"),
                   counts(fallback, "n_fallback")):
        out = out.join(series, how="left")
    out = out.fillna({"n_item202": 0, "n_item202_first": 0, "n_fallback": 0})
    for column in ("n_item202", "n_item202_first", "n_fallback"):
        out[column] = out[column].astype(int)
    out["full_quarter"] = out["listed_weeks"] >= out["quarter_weeks"]
    return out.reset_index()


def coverage_summary(quarters: pd.DataFrame) -> dict:
    """Share of company-quarters (top-300 at least one week, listed the whole quarter) with an event, by year."""
    q = quarters[(quarters["top_weeks"] > 0) & quarters["full_quarter"]].copy()
    q["year"] = q["quarter"].str[:4]
    q["any_item202"] = q["n_item202"] > 0
    q["any_event"] = (q["n_item202"] > 0) | (q["n_fallback"] > 0)
    by_year = q.groupby("year").agg(company_quarters=("cik", "size"), with_item202=("any_item202", "sum"),
                                    with_item202_or_fallback=("any_event", "sum"))
    by_year["share_item202"] = (by_year["with_item202"] / by_year["company_quarters"]).round(4)
    by_year["share_any"] = (by_year["with_item202_or_fallback"] / by_year["company_quarters"]).round(4)
    return {"definition": "calendar quarters in which the CIK holds a dv20/dv50 rank <= 300 in at least one week "
                          "and is listed in every week of the quarter; an event counts when its D0 falls in the quarter",
            "company_quarters": int(len(q)), "with_item202": int(q["any_item202"].sum()),
            "with_item202_or_fallback": int(q["any_event"].sum()),
            "share_any": round(float(q["any_event"].mean()), 4) if len(q) else None,
            "by_year": {year: {"company_quarters": int(row.company_quarters), "with_item202": int(row.with_item202),
                               "with_item202_or_fallback": int(row.with_item202_or_fallback),
                               "share_item202": float(row.share_item202), "share_any": float(row.share_any)}
                        for year, row in by_year.iterrows()}}


def company_year_table(quarters: pd.DataFrame) -> pd.DataFrame:
    """Per (cik, calendar year): listed and top-300 weeks, Item 2.02 / first-in-quarter / fallback counts."""
    q = quarters.assign(year=quarters["quarter"].str[:4])
    out = q.groupby(["cik", "year"]).agg(listed_weeks=("listed_weeks", "sum"), top_weeks=("top_weeks", "sum"),
                                         n_item202=("n_item202", "sum"), n_item202_first=("n_item202_first", "sum"),
                                         n_fallback=("n_fallback", "sum")).reset_index()
    year_weeks = (quarters.drop_duplicates("quarter").assign(year=lambda f: f["quarter"].str[:4])
                  .groupby("year")["quarter_weeks"].sum())
    out["year_weeks"] = out["year"].map(year_weeks).astype(int)
    out["full_year"] = np.where(out["listed_weeks"] >= out["year_weeks"], "Y", "N")
    out["n_quarterly_events"] = out["n_item202_first"] + out["n_fallback"]
    return out


def company_year_summary(years: pd.DataFrame) -> dict:
    """Distribution of quarterly events (first-in-quarter Item 2.02 + fallback) per top-300 company-year."""
    y = years[years["top_weeks"] > 0]
    bucket = y["n_quarterly_events"].clip(upper=5).map(lambda n: "5+" if n >= 5 else str(int(n)))
    full = y["full_year"] == "Y"
    table = pd.crosstab(y.loc[full, "year"], bucket[full])
    return {"definition": "calendar years with a dv20/dv50 rank <= 300 in at least one week; quarterly events = "
                          "first-in-fiscal-quarter Item 2.02 events plus fallback filings with D0 in the year",
            "company_years_top300": int(len(y)), "full_year_listed": int(full.sum()),
            "events_per_company_year": {str(k): int(v) for k, v in bucket.value_counts().sort_index().items()},
            "events_per_full_company_year": {str(k): int(v) for k, v in bucket[full].value_counts().sort_index().items()},
            "zero_item202_company_years": int((y["n_item202"] == 0).sum()),
            "zero_any_company_years": int((y["n_quarterly_events"] == 0).sum()),
            "full_years_by_year": {year: {str(k): int(v) for k, v in row.items()} for year, row in table.iterrows()}}


def gap_summary(events: pd.DataFrame, fallback: pd.DataFrame, scope: pd.DataFrame) -> dict:
    """Days between consecutive quarterly events (first-in-quarter Item 2.02 or fallback) inside the listed span."""
    frames = []
    if len(events):
        frames.append(events.loc[events["first_in_fiscal_quarter"] == "Y", ["cik", "d0_session"]])
    if len(fallback):
        frames.append(fallback[["cik", "d0_session"]])
    if not frames:
        return {}
    both = pd.concat(frames, ignore_index=True)
    both = both[both["d0_session"] != ""].copy()
    span = scope.set_index("cik")[["listed_first_week", "listed_last_week"]]
    both = both.join(span, on="cik")
    both = both[(both["d0_session"] >= both["listed_first_week"]) & (both["d0_session"] <= both["listed_last_week"])]
    both = both.sort_values(["cik", "d0_session"])
    days = pd.to_datetime(both["d0_session"]).groupby(both["cik"]).diff().dt.days.dropna()
    return {"gaps": int(len(days)), "share_60_120": round(float(((days >= 60) & (days <= 120)).mean()), 4),
            "under_60": int((days < 60).sum()), "over_120": int((days > 120).sum()),
            "median_days": float(days.median()) if len(days) else None}


FF_MAPS = INPUTS / "ff_industry_maps.csv"


def ff49_lookup(path: Path = FF_MAPS) -> dict[str, str]:
    """SIC code (4 digits) -> FF49 short name, from Siccodes49 ranges."""
    maps = read_csv_text(path)
    maps = maps[maps["scheme"] == "FF49"]
    out = {}
    for lo, hi, name in zip(maps["sic_lo"].astype(int), maps["sic_hi"].astype(int), maps["short_name"]):
        for code in range(lo, hi + 1):
            out.setdefault(f"{code:04d}", name)
    return out


def sic_week_coverage(weekly: pd.DataFrame, sic: pd.DataFrame, ff49: dict[str, str], ciks: set[int]) -> dict:
    """For top-300 name-weeks of ``ciks``: is there a header SIC on or before the week, only a later one,
    or none; and does that SIC fall in an FF49 range (blank checks 6770 counted apart)."""
    top = weekly[weekly["cik"].notna() & ((weekly["dv50_rank"] <= TOP_RANK) | (weekly["dv20_rank"] <= TOP_RANK))]
    top = top.assign(cik=top["cik"].astype(int))[["cik", "week_end"]].drop_duplicates().sort_values("week_end")
    top = top[top["cik"].isin(ciks)]
    if sic.empty:
        return {"name_weeks": int(len(top)), "no_sic": int(len(top))}
    top = top.assign(week_end=top["week_end"].astype("datetime64[ns]"))
    obs = sic.assign(observed=pd.to_datetime(sic["observed_date"]).astype("datetime64[ns]"))
    obs = obs[["cik", "observed", "sic"]].sort_values("observed")
    merged = pd.merge_asof(top, obs, left_on="week_end", right_on="observed", by="cik", direction="backward")
    later = pd.merge_asof(top, obs, left_on="week_end", right_on="observed", by="cik", direction="forward")
    status = np.where(merged["sic"].notna(), "on_or_before", np.where(later["sic"].notna(), "earliest_after", "none"))
    code = merged["sic"].fillna(later["sic"]).fillna("")
    in_ff49 = code.map(lambda c: c in ff49)
    return {"name_weeks": int(len(top)), "sic_status": dict(Counter(status)),
            "with_sic_share": round(float((status != "none").mean()), 4),
            "ff49_range_match_share": round(float(in_ff49.mean()), 4),
            "blank_check_6770_name_weeks": int((code == "6770").sum()),
            "no_ff49_range_codes": dict(Counter(code[~in_ff49 & (code != "")]).most_common(15))}


def compact_sic_history(sic: pd.DataFrame) -> pd.DataFrame:
    """The first header of each company-year plus every header whose SIC differs from the one before.

    A point-in-time lookup (the latest row on or before t) gives the same SIC as the full list."""
    if sic.empty:
        return sic
    first_of_year = ~sic.assign(year=sic["observed_date"].str[:4]).duplicated(["cik", "year"])
    return sic[first_of_year | (sic["sic_changed"] == "Y")].reset_index(drop=True)


def no_event_companies(scope: pd.DataFrame, plans: dict[int, dict], events: pd.DataFrame,
                       fallback: pd.DataFrame) -> pd.DataFrame:
    """In-scope CIKs with no Item 2.02 event (and whether a fallback filing exists)."""
    n202 = events.groupby("cik").size() if len(events) else pd.Series(dtype=int)
    nfb = fallback.groupby("cik").size() if len(fallback) else pd.Series(dtype=int)
    rows = []
    for record in scope.to_dict("records"):
        cik = int(record["cik"])
        if n202.get(cik, 0) > 0:
            continue
        plan = plans.get(cik, {})
        rows.append({**{k: record[k] for k in ("cik", "name", "security_ids", "foreign_filer", "in_top300",
                                               "in_candidates", "listed_first_week", "listed_last_week",
                                               "top300_weeks")},
                     "n_fallback": int(nfb.get(cik, 0)), "n_periodic_since_2011_10": plan.get("n_periodic_since", 0),
                     "first_filing": plan.get("first_filing", ""), "last_filing": plan.get("last_filing", ""),
                     "submissions_missing": plan.get("missing", "no_plan")})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ main

def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    common.atomic_write(path, frame.to_csv(index=False).encode("utf-8"))


def label_summary(events: pd.DataFrame, fallback: pd.DataFrame) -> dict:
    both = pd.concat([f for f in (events, fallback) if len(f)] or [pd.DataFrame(columns=["json_label", "filing_date"])],
                     ignore_index=True)
    if both.empty:
        return {}
    table = pd.crosstab(both["filing_date"].str[:4], both["json_label"])
    return {"overall": dict(Counter(both["json_label"])),
            "by_year": {year: {k: int(v) for k, v in row.items()} for year, row in table.iterrows()}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="never request; build from the cache only")
    parser.add_argument("--limit-ciks", type=int, default=0, help="a trial on the first N CIKs (writes CACHE/earnings/trial)")
    parser.add_argument("--plan-only", action="store_true", help="stop after counting the headers needed")
    args = parser.parse_args(argv)
    started = time.time()
    log("scope: weekly top-300 and candidate CIKs")
    weekly = pd.read_pickle(WEEKLY)[["security_id", "cik", "week_end", "universe", "dv50_rank", "dv20_rank"]]
    scope = build_scope(weekly, read_csv_text(CANDIDATES), read_csv_text(MASTER))
    scope = scope[scope["excluded_foreign"] == "N"].reset_index(drop=True)
    if args.limit_ciks:
        scope = scope.head(args.limit_ciks)
    out_dir = OUT / "trial" if args.limit_ciks else OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "scope.csv", scope)
    ciks = scope["cik"].astype(int).tolist()
    log(f"scope: {len(ciks)} CIKs (top300 {int((scope['in_top300'] == 'Y').sum())}, "
        f"candidates {int((scope['in_candidates'] == 'Y').sum())}, flags {dict(Counter(scope['foreign_filer']))})")
    plans, failures = plan_all(ciks, args.offline)
    jobs = header_jobs(plans)
    kinds = Counter(kind for *_, kind in jobs)
    pages = (sum(p["pages_needed"] for p in plans.values()), sum(p["pages_read"] for p in plans.values()))
    log(f"plan: {len(plans)} CIKs, older pages needed {pages[0]} read {pages[1]}; headers by kind {dict(kinds)}")
    if args.plan_only:
        return 0
    fetch_counts = fetch_headers(jobs, args.offline)
    if _STOP.is_set():
        log("stopped on an SEC refusal; tables are built from what is cached")
    log("build: parse headers, D0 sessions")
    calendar = XnasCloses()
    events, fallback = build_tables(plans, scope, calendar)
    sic_all = sic_history(events, fallback)
    sic = compact_sic_history(sic_all)
    event_out = events.reindex(columns=EVENT_COLUMNS)
    fallback_out = fallback.reindex(columns=EVENT_COLUMNS + ["other_8k_between"])
    if args.limit_ciks:
        targets = (out_dir / "earnings_events.csv", out_dir / "earnings_fallback_periodic.csv", out_dir / "sic_history.csv")
    else:
        targets = (EVENTS_OUT, FALLBACK_OUT, SIC_OUT)
    for path, frame in zip(targets, (event_out, fallback_out, sic)):
        _write_csv(path, frame)
        log(f"wrote {path} ({len(frame)} rows)")
    log("coverage")
    present = presence(weekly, set(ciks))
    quarters = quarter_coverage(present, events, fallback)
    years = company_year_table(quarters)
    _write_csv(out_dir / "company_year_coverage.csv", years)
    missing = no_event_companies(scope, plans, events, fallback)
    _write_csv(out_dir / "no_event_companies.csv", missing)
    flags = foreign_flag_check(scope, plans, weekly)
    _write_csv(out_dir / "foreign_flag_check.csv", flags)
    both = pd.concat([f for f in (events, fallback) if len(f)] or [pd.DataFrame(columns=["filing_date", "acceptance_et"])],
                     ignore_index=True)
    both = both[both["acceptance_et"] != ""]
    lag = (pd.to_datetime(both["filing_date"]) - pd.to_datetime(both["acceptance_et"].str[:10])).dt.days
    headers_missing = int(((events.get("acceptance_header_et", pd.Series(dtype=str)) == "").sum() if len(events) else 0)
                          + ((fallback.get("acceptance_header_et", pd.Series(dtype=str)) == "").sum() if len(fallback) else 0))
    summary = {
        "generated": datetime.now().isoformat(timespec="seconds"), "runtime_s": round(time.time() - started, 1),
        "scope": {"ciks": len(ciks), "by_foreign_flag": dict(Counter(scope["foreign_filer"])),
                  "in_top300": int((scope["in_top300"] == "Y").sum()),
                  "in_candidates": int((scope["in_candidates"] == "Y").sum())},
        "submissions": {"planned_ciks": len(plans), "older_pages_needed": pages[0], "older_pages_read": pages[1],
                        "ciks_missing": dict(Counter(p["missing"] for p in plans.values() if p["missing"])),
                        "ciks_failed": {str(k): v for k, v in failures.items()}},
        "headers": {"needed": len(jobs), "by_kind": dict(kinds), "fetch": dict(fetch_counts),
                    "rows_without_header": headers_missing, "stopped_on_refusal": _STOP.is_set()},
        "events": {"rows": int(len(events)), "ciks": int(events["cik"].nunique()) if len(events) else 0,
                   "by_form": dict(Counter(events["form"])) if len(events) else {},
                   "first_in_fiscal_quarter": int((events["first_in_fiscal_quarter"] == "Y").sum()) if len(events) else 0,
                   "fiscal_quarter_how": dict(Counter(events["fiscal_quarter_how"])) if len(events) else {},
                   "acceptance_timing": dict(Counter(events["acceptance_timing"])) if len(events) else {},
                   "tz_resolution": dict(Counter(events["tz_resolution"])) if len(events) else {},
                   "sic_match": dict(Counter(events["sic_match"])) if len(events) else {},
                   "by_filing_year": dict(sorted(Counter(events["filing_date"].str[:4]).items())) if len(events) else {}},
        "fallback": {"rows": int(len(fallback)), "ciks": int(fallback["cik"].nunique()) if len(fallback) else 0,
                     "with_other_8k_between": int((fallback["other_8k_between"] != "").sum()) if len(fallback) else 0,
                     "by_form": dict(Counter(fallback["form"])) if len(fallback) else {},
                     "by_filing_year": dict(sorted(Counter(fallback["filing_date"].str[:4]).items())) if len(fallback) else {}},
        "json_vs_header": label_summary(events, fallback),
        "sic_history": {"headers_with_sic": int(len(sic_all)), "rows": int(len(sic)),
                        "ciks": int(sic["cik"].nunique()) if len(sic) else 0,
                        "header_form_differs_from_json_form": int(sum(
                            (f["header_form"] != f["form"]).sum() for f in (events, fallback) if len(f))),
                        "changes": int((sic["sic_changed"] == "Y").sum()) if len(sic) else 0,
                        "blank_check_6770_rows": int((sic["blank_check_6770"] == "Y").sum()) if len(sic) else 0,
                        "ciks_ever_6770": int(sic.loc[sic["blank_check_6770"] == "Y", "cik"].nunique()) if len(sic) else 0,
                        "top300_name_weeks": sic_week_coverage(weekly, sic, ff49_lookup(), set(ciks))},
        "coverage_company_quarters": coverage_summary(quarters),
        "coverage_company_years": company_year_summary(years),
        "gaps_between_quarterly_events": gap_summary(events, fallback, scope),
        "filing_date_minus_acceptance_date_days": {str(k): int(v) for k, v in lag.value_counts().sort_index().items()},
        "foreign_flag_check": {"ciks_flag_differs": int(len(flags)),
                               "by_change": dict(Counter(flags["flag_security_master"] + "->" + flags["flag_all_pages"]))
                               if len(flags) else {},
                               "top300_weeks_latest_periodic_foreign": int(flags["top300_weeks_latest_periodic_foreign"].sum())
                               if len(flags) else 0},
        "no_event_companies": {"no_item202": int(len(missing)),
                               "no_item202_no_fallback": int((missing["n_fallback"] == 0).sum()) if len(missing) else 0},
    }
    common.atomic_write(out_dir / "earnings_summary.json", (json.dumps(summary, indent=1, default=str) + "\n").encode())
    log(f"events {len(events)}, fallback {len(fallback)}, sic rows {len(sic)}; "
        f"company-quarters with an event {summary['coverage_company_quarters'].get('share_any')}; "
        f"no Item 2.02: {len(missing)} CIKs; done in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
