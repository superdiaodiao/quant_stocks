"""Plan step 3: every Nasdaq Form 25 filed 2012-01-01 .. 2026-09-30, with public float.

Data only. Nothing here computes signals or returns.

Sources (all SEC, all through ``SEC_LIMITER`` and ``sec_headers()``):
- EDGAR full-text search (``efts.sec.gov/LATEST/search-index``), form 25-NSE,
  restricted to filings that list Nasdaq (CIK 1354457) as an entity, in
  half-year slices of 100 hits per page.
- The 25-NSE primary XML document of every Nasdaq hit. The search hit carries
  no class of security, so the document is the only place that says whether
  the removed class is the common stock, a warrant, a unit, a preferred or a
  note. Documents of other exchanges' 25-NSE filings are never fetched.
- Issuer-filed Form 25 (voluntary withdrawals under Rule 12d2-2(c), such as
  moves to another exchange). Their search hits list only the issuer, so they
  are found by full-text search for "Nasdaq" in forms 25, and kept when the
  cover names Nasdaq as the exchange the class is removed from.
- XBRL frames ``dei/EntityPublicFloat`` (USD) and
  ``dei/EntityCommonStockSharesOutstanding`` (shares), CY2011Q1I..CY2026Q2I.

A description that lists several classes is split into its items
(``class_kinds``); a filing that removes the common stock (or ADS) together with
warrants, preferreds or notes is a common delisting.

Classification of a row (plan column ``classification``):
- ``other_class``: the document names no common equity, only other classes
  (warrants, units, rights, preferred, debt, LP units, fund shares).
- ``common_delisting``: common stock, ordinary shares or American depositary
  shares leave Nasdaq. This includes an acquirer already listed under another
  ticker taking over the subject's ticker (Tornier and WMGI).
- ``reorg``: common, and the same equity continues on Nasdaq: the subject CIK
  still lists a Nasdaq ticker and keeps filing periodic reports, or its ticker
  passes at the next snapshot (whatever the gap) to a new registrant with a
  matching name (a holding company). ``reorg_review``: such a handover under
  Rule 12d2-2(a)(3) to a new registrant with a different name (Google to
  Alphabet, Apache to APA), or under (a)(2) while the subject keeps another
  Nasdaq ticker (a Liberty split-off); an (a)(2) handover that leaves the
  subject with no ticker is a cash-out, a common delisting with no successor
  (Avidity's RNA to Atrium). Both read the security master's raw snapshot rows
  (``ticker_rows_raw.csv.gz``: every resolved row before any was dropped for a
  Form 25) and ``snapshot_dates.json``; the script is run again after it.
- ``transfer``: common, and the class moves to another exchange: an issuer
  withdrawal whose Form 25 names another exchange or that comes with a Form
  8-A12B, or a subject that keeps filing periodic reports and that SEC lists
  today on another exchange under the same ticker.

``subject_exit`` is Y when the filing ended the subject CIK's Nasdaq common
listing: no common ticker the CIK listed at the filing is seen 40 days or more after
it before its run breaks (two full-list snapshots without it); a filing with no
snapshot rows of the CIK either way falls back on SEC's submissions. A documented
transfer to another exchange ends the listing unless a continuing ticker is one SEC
still lists on Nasdaq. ``unlisted_withdrawal``: an issuer's own Form 25 filed before the
CIK's first Nasdaq row (ShiftPixy), which ended nothing. The security master drops the
tail of the run after an exit only for the tickers it covered.

``effective_date`` is the filing date plus 10 days (Rule 12d2-2(d)(1)); the
document carries no effective date, and trading has usually stopped before.

Usage::

    PYTHONPATH=. python scripts/reversal_data_form25.py            # fetch + build
    PYTHONPATH=. python scripts/reversal_data_form25.py --offline  # rebuild from cache only
"""
from __future__ import annotations

import argparse
import bisect
import html
from collections import Counter
from datetime import date, timedelta
import json
from pathlib import Path
import re
import statistics
import sys
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

import pandas as pd

from scripts import reversal_data_common as common

NASDAQ_CIK = 1354457
FORM = "25-NSE"
START, END = "2012-01-01", "2026-09-30"
PAGE_SIZE = 100
SEC_RAW = common.RAW / "sec"
EFTS_BASE = "https://efts.sec.gov/LATEST/search-index"
EFTS_URL = (EFTS_BASE + "?forms={form}&ciks={cik:010d}"
            "&dateRange=custom&startdt={start}&enddt={end}&from={from_}")
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{document}"
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/dei/{concept}/{unit}/{period}.json"
FLOAT_CONCEPT, SHARES_CONCEPT = ("EntityPublicFloat", "USD"), ("EntityCommonStockSharesOutstanding", "shares")
FRAME_FIRST, FRAME_LAST = (2011, 1), (2026, 2)
OUTPUT = common.INPUTS / "form25_nasdaq_2012_2026.csv"
OLD_LIST = Path("output/research_only/sue_lt_2020_2026/inputs/sec_form25_nasdaq_2020_2026.csv")
SNAPSHOT_DATES = SEC_RAW / "derived" / "snapshot_dates.json"
FILES_READ: set[str] = set()  # raw/sec files read in this run (written to derived/files_read_form25.json)
COMPARISON = SEC_RAW / "derived" / "form25_vs_sue_lt_2020_2026.json"
EFFECTIVE_LAG_DAYS = 10
FLOAT_LOOKBACK_YEARS = 3
# Implied float per share (float / shares outstanding) outside this band means a unit error
# in one of the two facts; a real Nasdaq price times a float fraction stays inside it. Above
# REVIEW_PER_SHARE it is flagged 'review' (a few real Nasdaq prices are that high, but most such
# facts are x1000 errors) until the raw-close ratio test can be run.
IMPLIED_PER_SHARE_BAND = (0.01, 10_000.0)
REVIEW_PER_SHARE = 1_000.0
FLOAT_MEDIAN_MULTIPLE = 20
MAX_PLAUSIBLE_FLOAT = 5e12
RATIO_BAND = (0.05, 1.5)  # the plan's check once a raw close is available

PLAN_COLUMNS = ["filing_date", "effective_date", "accession", "subject_cik", "subject_name", "filer_cik",
                "class_of_security", "classification", "public_float_usd", "float_check_flag"]
EXTRA_COLUMNS = ["form", "document", "rule_provision", "rule_parse", "delisting_basis", "class_kind", "class_kinds",
                 "subject_tickers_sec", "n_subject_ciks", "float_end_date", "float_accession", "float_obs_3y",
                 "float_facts_dropped", "shares_outstanding", "shares_end_date", "implied_float_per_share",
                 "tickers_before", "tickers_ended", "tickers_new", "tickers_continued", "successor_cik", "successor_tickers",
                 "subject_exit", "classification_evidence", "doc_url"]


# ------------------------------------------------------------------ search

def half_year_slices(start: str = START, end: str = END) -> list[tuple[str, str]]:
    """[(start, end)] covering start..end in calendar half-years, clipped at both ends."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    slices, year, half = [], first.year, 0 if first.month <= 6 else 1
    while True:
        s = date(year, 1 + 6 * half, 1)
        e = date(year, 6, 30) if half == 0 else date(year, 12, 31)
        if s > last:
            break
        slices.append((max(s, first).isoformat(), min(e, last).isoformat()))
        year, half = (year, 1) if half == 0 else (year + 1, 0)
    return slices


def efts_search(form: str, start: str, end: str, from_: int, cik: int = NASDAQ_CIK, *, offline: bool = False) -> dict:
    url = EFTS_URL.format(form=form, cik=cik, start=start, end=end, from_=from_)
    path = SEC_RAW / "efts" / f"{form}_cik{cik}_{start}_{end}_{from_}.json"
    if offline and not path.exists():
        raise FileNotFoundError(f"offline and not cached: {path}")
    return json.loads(common.cached_get(url, path, source="sec_efts", headers=common.sec_headers(),
                                        limiter=common.SEC_LIMITER))


def efts_query(params: dict, cache_name: str, *, offline: bool = False) -> dict:
    """Any EDGAR full-text search query (cached as raw/sec/efts/{cache_name}.json)."""
    url = EFTS_BASE + "?" + urlencode(params)
    path = SEC_RAW / "efts" / f"{cache_name}.json"
    if offline and not path.exists():
        raise FileNotFoundError(f"offline and not cached: {path}")
    return json.loads(common.cached_get(url, path, source="sec_efts", headers=common.sec_headers(),
                                        limiter=common.SEC_LIMITER))


def all_hits(form: str = FORM, start: str = START, end: str = END, *, offline: bool = False,
             search=efts_search) -> tuple[list[dict], list[dict]]:
    """Every hit in every slice, paging 100 at a time; also a per-slice log with the totals."""
    hits, log = [], []
    for s, e in half_year_slices(start, end):
        first = search(form, s, e, 0, offline=offline)
        total = first["hits"]["total"]
        if total.get("relation") != "eq" or total["value"] >= 10_000:
            raise RuntimeError(f"{s}..{e}: {total} hits; use smaller slices")
        pages, got = [first], len(first["hits"]["hits"])
        while got < total["value"]:
            page = search(form, s, e, got, offline=offline)
            if not page["hits"]["hits"]:
                raise RuntimeError(f"{s}..{e}: empty page at from={got} of {total['value']}")
            pages.append(page)
            got += len(page["hits"]["hits"])
        slice_hits = [hit for page in pages for hit in page["hits"]["hits"]]
        ids = {hit["_id"] for hit in slice_hits}
        if len(ids) != total["value"]:
            raise RuntimeError(f"{s}..{e}: {len(ids)} unique hits, total says {total['value']}")
        hits.extend(slice_hits)
        log.append({"start": s, "end": e, "total": total["value"], "pages": len(pages)})
    return hits, log


DISPLAY = re.compile(r"^(?P<name>.*?)\s+(?:\((?P<tickers>[A-Z0-9.\-]+(?:, [A-Z0-9.\-]+)*)\)\s+)?\(CIK (?P<cik>\d{10})\)\s*$")


def parse_display_name(text: str) -> dict:
    """'Carlyle Group Inc.  (CG, CGABL)  (CIK 0001527166)' -> name, tickers, cik."""
    match = DISPLAY.match(str(text).strip())
    if not match:
        return {"name": str(text).strip(), "tickers": [], "cik": None}
    tickers = match.group("tickers")
    return {"name": match.group("name").strip(), "tickers": tickers.split(", ") if tickers else [],
            "cik": int(match.group("cik"))}


def filter_nasdaq_filer(hits: list[dict], cik: int = NASDAQ_CIK) -> list[dict]:
    """Hits whose entities include the exchange ``cik``, one record per filing (primary document)."""
    rows, seen = [], set()
    exchange = f"{cik:010d}"
    for hit in hits:
        source = hit["_source"]
        if exchange not in source.get("ciks", []):
            continue
        accession, _, document = hit["_id"].partition(":")
        if accession in seen:
            continue
        seen.add(accession)
        names = [parse_display_name(n) for n in source.get("display_names", [])]
        subjects = [n for n in names if n["cik"] is not None and n["cik"] != cik]
        if not subjects:  # fall back on the ciks list when display names do not parse
            subjects = [{"name": "", "tickers": [], "cik": int(c)} for c in source["ciks"] if int(c) != cik]
        rows.append({
            "accession": accession, "document": document, "form": source.get("form", ""),
            "filing_date": source.get("file_date", ""), "filer_cik": cik,
            "accession_prefix_is_filer": accession.startswith(exchange),
            "subject_cik": subjects[0]["cik"] if subjects else None,
            "subject_name_efts": subjects[0]["name"] if subjects else "",
            "subject_tickers_sec": " ".join(subjects[0]["tickers"]) if subjects else "",
            "n_subject_ciks": len(subjects),
            "other_subject_ciks": " ".join(str(s["cik"]) for s in subjects[1:]),
        })
    return rows


# ------------------------------------------------------------------ issuer-filed Form 25

ISSUER_FORM = "25"
OTHER_EXCHANGES = re.compile(r"New York Stock Exchange|\bNYSE\b|NYSE MKT|NYSE American|NYSE Arca|American Stock Exchange|"
                             r"\bAMEX\b|Cboe|\bBATS\b|\bIEX\b|Investors Exchange|Long-Term Stock Exchange", re.I)
CHECKED_MARKS = {"x", "☒", "ý", "þ", "■", "✓", "√", "✔", "⌧", "☑", "[x]", "(x)", "×", "x]", "n", "ý", "þ"}
PROVISION = re.compile(r"(?P<mark>\S{1,3})\s*(?:Pursuant\s+to\s+)?17\s*CFR\s*240\.?\s*12d2-?2\s*\((?P<a>[a-d])\)"
                       r"(?:\s*\((?P<b>\d)\))?", re.I)


def issuer_search(form: str, start: str, end: str, from_: int, *, offline: bool = False) -> dict:
    """Full-text search for issuer-filed Form 25 naming Nasdaq: an issuer's own Form 25 (a voluntary
    withdrawal under Rule 12d2-2(c), such as an exchange transfer) lists only the issuer's CIK."""
    params = {"q": '"Nasdaq"', "forms": form, "dateRange": "custom", "startdt": start, "enddt": end}
    if from_:
        params["from"] = from_
    return efts_query(params, f"{form}_qNasdaq_{start}_{end}_{from_}", offline=offline)


def issuer_filings(hits: list[dict]) -> list[dict]:
    """One record per issuer-filed Form 25 (its primary document), in the shape of filter_nasdaq_filer."""
    rows, seen = [], set()
    for hit in hits:
        source = hit["_source"]
        accession, _, document = hit["_id"].partition(":")
        if accession in seen or str(source.get("file_type") or source.get("form")) not in ("25", "25/A"):
            continue
        seen.add(accession)
        names = [parse_display_name(n) for n in source.get("display_names", [])]
        subjects = [n for n in names if n["cik"] is not None] or [
            {"name": "", "tickers": [], "cik": int(c)} for c in source.get("ciks", [])]
        if not subjects:
            continue
        rows.append({
            "accession": accession, "document": document, "form": source.get("form", ""),
            "filing_date": source.get("file_date", ""), "filer_cik": subjects[0]["cik"],
            "accession_prefix_is_filer": accession.startswith(f"{subjects[0]['cik']:010d}"),
            "subject_cik": subjects[0]["cik"], "subject_name_efts": subjects[0]["name"],
            "subject_tickers_sec": " ".join(subjects[0]["tickers"]), "n_subject_ciks": len(subjects),
            "other_subject_ciks": " ".join(str(x["cik"]) for x in subjects[1:]),
        })
    return rows


def html_text(data: bytes | str) -> str:
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).replace("\xa0", " ").split())


def checked_provision(text: str) -> str:
    """The Rule 12d2-2 provision whose box is marked on a Form 25 cover, or '' when not exactly one is."""
    marked = {f"17 CFR 240.12d2-2({m.group('a')})" + (f"({m.group('b')})" if m.group("b") else "")
              for m in PROVISION.finditer(text) if m.group("mark").strip().lower() in CHECKED_MARKS}
    return marked.pop() if len(marked) == 1 else ""


def parse_issuer_form25(data: bytes | str) -> dict:
    """Exchange, class and provision from an issuer's Form 25 (HTML text, or the XML of the form)."""
    raw = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    if re.search(r"<(?:\w+:)?notificationOfRemoval\b|<descriptionClassSecurity>", raw[:5000] + raw[-5000:]):
        doc = parse_form25_xml(raw)
        return {**doc, "on_nasdaq": "nasdaq" in doc["exchange_name"].lower(), "other_exchange": "",
                "rule_parse": "xml"}
    text = html_text(raw)
    cover = re.search(r"\(\s*Exact name of (?:the )?Issuer", text, re.I)
    head = text[:cover.start()] if cover else text[:1500]
    number = re.search(r"Commission File Number[:\s]*[\w-]*", head, re.I)
    head = head[number.end():] if number else head[-400:]
    described = re.search(r"\(\s*(?:Description|Title) of (?:the |each )?(?:class(?:es)? of )?securities[^)]*\)", text, re.I)
    offices = re.search(r"executive offices\s*\)", text, re.I)
    class_text = ""
    if described:
        start = offices.end() if offices and offices.end() < described.start() else max(0, described.start() - 300)
        class_text = text[start:described.start()].strip(" _")
        if not class_text:  # the label comes first: '(Description of class of securities) Rights to ...'
            after = text[described.end():described.end() + 500]
            class_text = re.split(r"Please place|\(", after, maxsplit=1)[0].strip(" _")
    rule = checked_provision(text)
    rest = text[cover.end():] if cover else text
    other = OTHER_EXCHANGES.search(rest)
    return {"exchange_cik": None, "exchange_name": head.strip()[:300], "issuer_cik": None, "issuer_name": "",
            "class_of_security": class_text[:500], "rule_provision": rule or "17 CFR 240.12d2-2(c)",
            "signature_date": "", "parse": "html", "on_nasdaq": bool(re.search(r"nasdaq", head, re.I)),
            "other_exchange": other.group(0) if other else "",
            "rule_parse": "checked_box" if rule else "assumed_12d2-2(c)"}


def filings_near(cik: int, day: str, forms: set[str], days_before: int = 45, days_after: int = 15,
                 *, offline: bool = False) -> list[tuple[str, str]]:
    """(form, date) of the subject's filings of ``forms`` near ``day``, fetching the older submissions
    page that covers ``day`` when the recent block starts after it."""
    from scripts.reversal_data_security_master import load_submission_page, load_submissions
    payload = load_submissions(cik, offline=True)
    if not payload:
        return []
    lo = (pd.Timestamp(day) - pd.Timedelta(days=days_before)).date().isoformat()
    hi = (pd.Timestamp(day) + pd.Timedelta(days=days_after)).date().isoformat()
    blocks = [(payload.get("filings") or {}).get("recent") or {}]
    for page in (payload.get("filings") or {}).get("files") or []:
        if (page.get("filingFrom") or "") <= hi and (page.get("filingTo") or "") >= lo:
            older = load_submission_page(page["name"], offline=offline)
            if older:
                blocks.append(older)
                FILES_READ.add(str(SEC_RAW / "submissions" / f"{page['name']}.gz"))
    return sorted((f, d) for block in blocks for f, d in zip(block.get("form", []) or [], block.get("filingDate", []) or [])
                  if f in forms and lo <= d <= hi)


# ------------------------------------------------------------------ documents

def doc_url(cik: int, accession: str, document: str) -> str:
    return DOC_URL.format(cik=int(cik), acc=accession.replace("-", ""), document=document)


def fetch_form25_doc(cik: int, accession: str, document: str, *, offline: bool = False) -> bytes | None:
    path = SEC_RAW / "docs" / str(int(cik)) / accession / document
    if offline and not path.exists():
        return None
    try:
        return common.cached_get(doc_url(cik, accession, document), path, source="sec_archives",
                                 headers=common.sec_headers(), limiter=common.SEC_LIMITER)
    except FileNotFoundError:
        return None


def _text(node, tag: str) -> str:
    found = node.find(f".//{{*}}{tag}") if node is not None else None
    if found is None:
        found = node.find(f".//{tag}") if node is not None else None
    return (found.text or "").strip() if found is not None else ""


def parse_form25_xml(data: bytes | str) -> dict:
    """Exchange, issuer, class of security and rule provision from a 25-NSE primary document."""
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError:
        grab = lambda tag: (re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.S | re.I) or [None, ""])[1]
        return {"exchange_cik": None, "exchange_name": "", "issuer_cik": None, "issuer_name": "",
                "class_of_security": grab("descriptionClassSecurity").strip(),
                "rule_provision": grab("ruleProvision").strip(), "signature_date": grab("signatureDate").strip(),
                "parse": "regex"}
    exchange = root.find("{*}exchange") if root.find("{*}exchange") is not None else root.find("exchange")
    issuer = root.find("{*}issuer") if root.find("{*}issuer") is not None else root.find("issuer")
    to_int = lambda value: int(value) if value.isdigit() else None
    return {
        "exchange_cik": to_int(_text(exchange, "cik")), "exchange_name": _text(exchange, "entityName"),
        "issuer_cik": to_int(_text(issuer, "cik")), "issuer_name": _text(issuer, "entityName"),
        "class_of_security": " ".join(_text(root, "descriptionClassSecurity").split()),
        "rule_provision": " ".join(_text(root, "ruleProvision").split()),
        "signature_date": _text(root, "signatureDate"), "parse": "xml",
    }


CLASS_RULES = [  # first match wins; order matters (a unit names its warrant, a warrant names the stock)
    ("unit", r"\bunits?\b(?!.*limited partner)|\bsubunits?\b"),
    ("warrant", r"\bwarr?ants?\b"),
    ("right", r"\brights?\b"),
    ("preferred", r"\bpreferred\b|\bpreference\b|\bpfd\b"),
    ("fund", r"\betfs?\b|\betns?\b|index fund|exchange[- ]traded|\bfund\b|\bportfolio\b|nextshares|proshares|"
             r"velocityshares|\bultra(?:pro)?\b"),
    ("debt", r"\bnotes?\b|\bdebentures?\b|\bbonds?\b|\bsenior\b|\bsubordinated\b|\bdue \d{4}\b|\d+(?:\.\d+)?\s*%"),
    ("lp_units", r"limited partner|\bL\.?P\.?\b|partnership units|common units"),
    ("ads", r"\bamer\w* deposit[ao]ry|american depository receipts?|\badss?\b|\badrs?\b|new york registry|"
            r"deposit[ao]ry shares.*ordinary"),
    # A bare 'Depositary Receipt' is an ADR for a foreign issuer, a fractional preferred for a domestic
    # one (Stericycle's SRCLP); resolve_receipt decides from the subject's filings.
    ("receipt", r"deposit[ao]ry receipts?"),
    ("preferred", r"deposit[ao]ry shares"),  # a bare depositary share on Nasdaq is a fractional preferred
    ("common", r"common|ordinary|capital stock|beneficial interest|\bstock\b|\bshares?\b"),
]
# Splits a description into the classes it lists ('Common Stock, 3.600% Notes due 2025, ...').
ITEM_SPLIT = re.compile(r"\s*(?:[,;&]|\):|\band\b|\bplus\b|\bwith (?=associated\b))\s*", re.I)
# Words that start a description of the item before them rather than a new item: the common stock
# inside 'Units, each consisting of one share of common stock' or 'Warrants to purchase Common Stock'.
QUALIFIER = re.compile(r"\b(?:each|consisting|consists?|representing|represents?|represented|exercisable|"
                       r"to purchase|convertible|entitling|underlying|interest in)\b", re.I)
# Whole-description kinds that splitting must not undo ('iShares ... Index Fund, Shares of Beneficial
# Interest' is a fund; 'Common Units, Representing Limited Partner Interests' are LP units).
WHOLE_KINDS = {"fund", "lp_units"}


def _first_kind(text: str) -> str:
    for kind, pattern in CLASS_RULES:
        if re.search(pattern, text, re.I):
            return kind
    return "other"


_CURRENCY = r"(?:US\$|NT\$|HK\$|C\$|A\$|R\$|\$|€|£|DKK|NIS|CHF|EUR|GBP|ILS|SEK|NOK|RMB)"
_AMOUNT = rf"(?:{_CURRENCY}\s*)?[\d.,]*\d(?:\s*(?:cents?|dollars?|euros?|pence|(?:Danish\s+)?kroner|{_CURRENCY}))?"
_PER_SHARE = r"(?:\s+(?:per|each)\s+(?:ordinary\s+|common\s+)?share)?"
# 'par value $0.001 per share', '$0.01 par value', 'no par value', 'nominal value €1.00 per share': these
# describe the item before them, and their 'share' is no class of its own (Masimo's 'Preferred Stock
# Purchase Rights, par value $0.001 per share' removed the rights only).
PAR_VALUE = re.compile(rf"\b(?:with(?:out)?\s+)?(?:a\s+)?(?:no\s+)?(?:par|nominal)\s+value(?:\s+of)?(?:\s*{_AMOUNT})?{_PER_SHARE}"
                       rf"|{_AMOUNT}\s+(?:par|nominal)\s+value{_PER_SHARE}", re.I)


def class_kinds(description: str) -> list[str]:
    """The kind of every class a Form 25 description lists, in order (sub-descriptions skipped).

    'Common stock and preferred stock' -> [common, preferred]; 'Units, each consisting of one share
    of Class A common stock and one-half of one warrant' -> [unit]. Par-value phrases are cut first,
    and leave an item break behind ('Common shares without par value Warrants' lists two classes); a
    fragment naming no security of its own ('Series A', an address line) belongs to the item before it.
    """
    text = re.sub(r"\([^)]*\)", " ", str(description or ""))  # parentheticals describe, they do not list
    text = PAR_VALUE.sub(", ", text)
    kinds = []
    for segment in text.split(";"):
        described = False  # inside the sub-description of the previous item
        for part in ITEM_SPLIT.split(segment):
            part = part.strip(" .:-—")
            if not part or described:
                continue
            qualifier = QUALIFIER.search(part)
            kind = _first_kind(part)
            if qualifier:
                described = True
                head = part[:qualifier.start()].strip()
                if not head:
                    continue
                if kind in ("common", "ads"):
                    kind = _first_kind(head)
            if kind == "other" and kinds:
                continue  # no security noun: part of the item before it
            kinds.append(kind)
    return kinds


def classify_security_class(description: str) -> str:
    """The class a Form 25 removes; a filing that lists the common stock (or ADS) among other classes
    removes the common stock ('Common Stock, 3.600% Notes due 2025' is a common delisting)."""
    text = str(description or "").lower()
    if not text.strip():
        return "unknown"
    whole = _first_kind(text)
    if whole in WHOLE_KINDS:
        return whole
    for kind in class_kinds(text):
        if kind in ("common", "ads"):
            return kind
    return whole


def resolve_receipt(kind: str, foreign_flag: str, tickers: str = "") -> str:
    """'receipt' -> 'ads' for a foreign filer, 'preferred' for a domestic one or a fifth-letter-P ticker."""
    if kind != "receipt":
        return kind
    if any(len(t) == 5 and t[-1] in "PONM" for t in str(tickers).split()):
        return "preferred"
    return "ads" if foreign_flag in ("Y", "MIXED") else "preferred"


def delisting_basis(rule: str) -> str:
    """Rule 12d2-2 paragraph cited by the exchange, in words."""
    text = re.sub(r"\s+", "", str(rule or "")).lower()
    for key, label in (("12d2-2(a)(1)", "called_for_redemption"), ("12d2-2(a)(2)", "redeemed_or_matured"),
                       ("12d2-2(a)(3)", "substituted_merger_or_exchange"), ("12d2-2(a)(4)", "rights_extinguished"),
                       ("12d2-2(b)", "exchange_removal"), ("12d2-2(c)", "issuer_withdrawal")):
        if key in text:
            return label
    return "unknown" if not text else "other"


# ------------------------------------------------------------------ frames and float

def frame_periods(first: tuple[int, int] = FRAME_FIRST, last: tuple[int, int] = FRAME_LAST) -> list[str]:
    periods, (year, quarter) = [], first
    while (year, quarter) <= last:
        periods.append(f"CY{year}Q{quarter}I")
        year, quarter = (year, quarter + 1) if quarter < 4 else (year + 1, 1)
    return periods


def fetch_frame(concept: str, unit: str, period: str, *, offline: bool = False) -> dict | None:
    path = SEC_RAW / "frames" / f"{concept}_{period}.json.gz"
    if offline and not path.exists():
        return None
    try:
        return json.loads(common.cached_get(FRAME_URL.format(concept=concept, unit=unit, period=period), path,
                                            source="sec_frames", headers=common.sec_headers(),
                                            limiter=common.SEC_LIMITER))
    except FileNotFoundError:
        return None


def fetch_float_frames(concept: str, unit: str, periods: list[str], *, offline: bool = False) -> pd.DataFrame:
    """Long table cik, end, val, accn, period, entity_name over the given instant frames."""
    from scripts.reversal_data_security_master import parallel_map
    payloads = parallel_map(lambda period: fetch_frame(concept, unit, period, offline=offline), periods, workers=4)
    records = []
    for period, payload in zip(periods, payloads):
        for fact in (payload or {}).get("data", []):
            records.append({"cik": int(fact["cik"]), "end": fact.get("end"), "val": float(fact["val"]),
                            "accn": fact.get("accn", ""), "period": period, "entity_name": fact.get("entityName", "")})
    frame = pd.DataFrame(records, columns=["cik", "end", "val", "accn", "period", "entity_name"])
    frame["end"] = pd.to_datetime(frame["end"], errors="coerce")
    return frame


def check_float_units(public_float: float | None, shares: float | None, raw_close: float | None = None) -> str:
    """'ok', or why the float looks like a unit error, or why it cannot be checked.

    With ``raw_close`` this is the plan's test, float / (shares x close) in [0.05, 1.5].
    Without it (no raw prices in this round) the float per share must lie in a band
    that any real Nasdaq price times a float fraction stays inside.
    """
    if public_float is None or pd.isna(public_float):
        return "no_float"
    if public_float <= 0:
        return "nonpositive_float"
    if public_float > MAX_PLAUSIBLE_FLOAT:
        return "float_above_5T"
    if shares is None or pd.isna(shares) or shares <= 0:
        return "no_shares"
    if raw_close is not None and not pd.isna(raw_close) and raw_close > 0:
        ratio = public_float / (shares * raw_close)
        return "ok" if RATIO_BAND[0] <= ratio <= RATIO_BAND[1] else "ratio_out_of_band"
    per_share = public_float / shares
    if per_share < IMPLIED_PER_SHARE_BAND[0]:
        return "implied_per_share_low"
    if per_share > IMPLIED_PER_SHARE_BAND[1]:
        return "implied_per_share_high"
    if per_share > REVIEW_PER_SHARE:
        return "review"
    return "ok"


def _matching_shares(own_shares: pd.DataFrame, accession: str, end: pd.Timestamp) -> pd.Series | None:
    """The shares-outstanding fact of the same 10-K cover page, else the nearest within a year."""
    same = own_shares[own_shares["accn"] == accession]
    if same.empty:
        gap = (own_shares["end"] - end).abs()
        same = own_shares[gap <= pd.Timedelta(days=366)].assign(gap=gap).sort_values("gap")
    return same.iloc[0] if not same.empty else None


# A float FLOAT_JUMP_RATIO times the CIK's lowest other float nearby, at X1000_MIN_PER_SHARE or more a
# share, is taken for a x1000 error (a SPAC's float can grow 25-fold or more at its merger, but at about
# $10 to $40 a share). A float below 1/FLOAT_JUMP_RATIO of the CIK's other floats is no reference (a
# /1000 fact of a large company would make its real floats look like jumps).
FLOAT_JUMP_RATIO = 50
X1000_MIN_PER_SHARE = 100.0
# A float above MARKET_CAP_MULTIPLE times the largest Nasdaq market cap listed for the CIK within two
# years of the float date (company lists 2011-2019, screeners 2025-2026) is a unit error when it also
# implies X1000_MIN_PER_SHARE or more a share, or exceeds MARKET_CAP_GROSS times that cap (Crosstex's
# shares fact is x1000 too). Fast growers stay below it (Trillium 2020 at 14 times its 2019 cap,
# Nikola at 54 times VectoIQ's but $34 a share).
MARKET_CAP_MULTIPLE = 20.0
MARKET_CAP_GROSS = 200.0
MARKET_CAP_WINDOW_DAYS = 730
COMPANY_LIST_DIR = common.CACHE / "listings" / "companylist"
SCREENER_GLOB = "stocks_list_dir/nasdaq/snapshots/nasdaq_300M_*.csv"
# The screeners list every Nasdaq stock with a market cap of $300M or more (their smallest is $300.05M):
# a CIK seen in the Nasdaq lists within SCREENER_NEAR_DAYS on both sides of a screener date (the 2025
# symbol files are monthly) but absent from it was worth less, which bounds its cap where no company
# list does (Lyra's $13.8B float of 2024-06, x1000).
SCREENER_FLOOR = 3e8
SCREENER_NEAR_DAYS = 45
# A shares fact this many times (up to SHARES_X1000_BAND[1]) another shares fact of the CIK within
# SHARES_X1000_DAYS is a x1000 error; a float of SHARES_X1000_MIN_FLOAT or more that looks right per
# share against it is x1000 too (Crosstex 2011: 47.4B shares and $364B against 47.6M shares in 2012).
# Below that the other fact is as likely the one in error (reported in thousands: Power One 2012,
# Interactive Intelligence, Approach Resources and Yellow keep their sub-$1B floats).
SHARES_X1000_BAND = (500.0, 2000.0)
SHARES_X1000_DAYS = 400
SHARES_X1000_MIN_FLOAT = 2e10


def load_market_caps(raw_rows: pd.DataFrame | None, company_list_dir: Path = COMPANY_LIST_DIR,
                     screener_glob: str = SCREENER_GLOB) -> dict[int, pd.Series]:
    """cik -> market caps (USD) by date, from the Nasdaq company lists and screeners whose (date, symbol)
    the security master resolved to the CIK (``ticker_rows_raw.csv.gz``). Data only: the listed
    market cap bounds the float for the unit check; no price enters any output."""
    if raw_rows is None or raw_rows.empty:
        return {}
    frames = []
    paths = sorted(Path(company_list_dir).glob("*.csv")) if Path(company_list_dir).is_dir() else []
    for path in paths + sorted(Path().glob(screener_glob)):
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        cap = "MarketCap" if "MarketCap" in frame else "Market Cap" if "Market Cap" in frame else None
        if cap is None or "Symbol" not in frame:
            continue
        day = frame["Observed At"].str[:10] if "Observed At" in frame else pd.Series(
            re.search(r"(\d{4}-\d{2}-\d{2})", path.name).group(1), index=frame.index)
        frames.append(pd.DataFrame({"date": day, "symbol": frame["Symbol"].str.strip().str.upper(),
                                    "cap": pd.to_numeric(frame[cap].str.lstrip("$"), errors="coerce")}))
    if not frames:
        return {}
    caps = pd.concat(frames, ignore_index=True).dropna(subset=["cap"])
    caps = caps[caps["cap"] > 0]
    resolved = raw_rows[raw_rows["cik"].notna()][["date", "symbol", "cik"]]
    caps = caps.merge(resolved, on=["date", "symbol"])
    caps = pd.concat([caps, screener_bounds(resolved, sorted(Path().glob(screener_glob)))], ignore_index=True)
    caps["date"] = pd.to_datetime(caps["date"])
    return {int(c): g.groupby("date")["cap"].sum().sort_index() for c, g in caps.groupby("cik")}


def screener_bounds(resolved: pd.DataFrame, screener_paths: list[Path], floor: float = SCREENER_FLOOR,
                    near_days: int = SCREENER_NEAR_DAYS) -> pd.DataFrame:
    """(date, symbol, cik, cap=floor) for each CIK seen in the Nasdaq lists within ``near_days`` both
    before and after a screener date (so listed on it), none of whose symbols that screener lists: its
    market cap was below the floor then, an upper bound for the unit check. ``resolved`` holds the
    resolved (date, symbol, cik) rows."""
    out = []
    for path in screener_paths:
        found = re.search(r"(\d{4}-\d{2}-\d{2})", Path(path).name)
        if not found:
            continue
        day = found.group(1)
        listed = set(pd.read_csv(path, dtype=str, keep_default_na=False)["Symbol"].str.strip().str.upper())
        lo = (pd.Timestamp(day) - pd.Timedelta(days=near_days)).date().isoformat()
        hi = (pd.Timestamp(day) + pd.Timedelta(days=near_days)).date().isoformat()
        near = resolved[(resolved["date"] >= lo) & (resolved["date"] <= hi)]
        both_sides = set(near.loc[near["date"] <= day, "cik"]) & set(near.loc[near["date"] >= day, "cik"])
        inside = near.groupby("cik")["symbol"].apply(lambda symbols: bool(set(symbols) & listed))
        for cik in inside.index[~inside.values & inside.index.isin(both_sides)]:
            out.append({"date": day, "symbol": "", "cik": cik, "cap": floor})
    return pd.DataFrame(out, columns=["date", "symbol", "cik", "cap"])


def market_cap_near(caps: pd.Series | None, end: pd.Timestamp, days: int = MARKET_CAP_WINDOW_DAYS) -> float | None:
    """The largest listed market cap of the CIK within ``days`` of ``end`` (None when none is listed)."""
    if caps is None or caps.empty:
        return None
    near = caps[(caps.index >= end - pd.Timedelta(days=days)) & (caps.index <= end + pd.Timedelta(days=days))]
    return float(near.max()) if not near.empty else None


def float_unit_flag(value: float, end: pd.Timestamp, shares: float | None, lowest_other: float | None,
                    caps: pd.Series | None = None) -> str:
    """``check_float_units`` plus two cross-checks for a float that passes it: above MARKET_CAP_MULTIPLE
    times the CIK's largest listed Nasdaq market cap within two years ('above_listed_market_cap'); or,
    with no listed cap near, FLOAT_JUMP_RATIO times the CIK's lowest other float nearby at
    X1000_MIN_PER_SHARE or more a share ('x1000_vs_other_floats': SeaChange's $23.1B in 2022 after
    $46M in 2021, at $457 a share). A float whose shares fact is x1000 too passes both when the CIK has
    no listed cap near it (Crosstex 2011: $364B at $7.68 a share; its Nasdaq symbol is unresolved), and
    a float jump alone cannot tell it from a real one (Cassava rose 200-fold in 2020-2021)."""
    flag = check_float_units(value, shares)
    if flag not in ("ok", "review", "no_shares"):
        return flag
    per_share = value / shares if shares and shares > 0 else None
    high = per_share is None or per_share >= X1000_MIN_PER_SHARE
    cap = market_cap_near(caps, end)
    if cap is not None:  # a listed market cap is the better reference: the jump test is not needed
        if value > MARKET_CAP_GROSS * cap or (value > MARKET_CAP_MULTIPLE * cap and high):
            return "above_listed_market_cap"
        return flag
    if lowest_other and per_share is not None and high and value >= FLOAT_JUMP_RATIO * lowest_other:
        return "x1000_vs_other_floats"
    return flag


def shares_x1000(own_shares: pd.DataFrame, count: pd.Series | None, band: tuple[float, float] = SHARES_X1000_BAND,
                 days: int = SHARES_X1000_DAYS) -> bool:
    """Whether the shares fact ``count`` is SHARES_X1000_BAND times another shares fact of the CIK within
    ``days`` (a x1000 error: a reverse split of 1-for-500 or more is far rarer)."""
    if count is None or not count["val"] > 0 or own_shares.empty:
        return False
    near = own_shares[((own_shares["end"] - count["end"]).abs() <= pd.Timedelta(days=days)) & (own_shares["val"] > 0)]
    ratios = float(count["val"]) / near["val"].astype(float)
    return bool(((ratios >= band[0]) & (ratios <= band[1])).any())


def float_for_filing(floats: pd.DataFrame, shares: pd.DataFrame, cik: int, filing_date: str,
                     years: int = FLOAT_LOOKBACK_YEARS, caps: pd.Series | None = None) -> dict:
    """Maximum checked public float with an end date in the ``years`` before ``filing_date``.

    Every fact in the window is unit-checked against its own shares count first (``float_unit_flag``,
    with the CIK's listed market caps ``caps``, including the $300M bound of an absence from the
    screeners, and its other float facts over twice the window; a float of $20B or more whose shares
    count is x1000 another shares fact nearby is 'shares_x1000_vs_other_shares'). A
    fact that fails the check is dropped; so is a fact the check cannot clear ('review': above $1,000
    a share, or 'no_shares') that exceeds FLOAT_MEDIAN_MULTIPLE times the lower median of the CIK's
    facts over twice the window (of its cleanly checked facts when it has any; an issuer can file
    several x1000 facts in a row). A fact that passes cleanly is kept whatever its size (a SPAC's float
    can grow 25-fold at its merger). The maximum is taken over the kept facts; when every fact is
    dropped the largest is reported with its failure flag. ``float_facts_dropped`` counts the dropped facts.
    """
    empty = {"public_float_usd": None, "float_end_date": "", "float_accession": "", "float_obs_3y": 0,
             "float_facts_dropped": 0, "shares_outstanding": None, "shares_end_date": "", "implied_float_per_share": None}
    filed = pd.Timestamp(filing_date)
    mine = floats[(floats["cik"] == cik) & (floats["end"] <= filed)]
    own = mine[mine["end"] > filed - pd.DateOffset(years=years)]
    if own.empty:
        return {**empty, "float_check_flag": "no_float"}
    own_shares = shares[shares["cik"] == cik]
    wide_facts = mine[(mine["end"] > filed - pd.DateOffset(years=2 * years)) & (mine["val"] > 0)]

    def lowest_other(fact) -> float | None:
        others = wide_facts.loc[(wide_facts["accn"] != fact.accn) | (wide_facts["end"] != fact.end), "val"]
        if others.empty:
            return None
        typical = statistics.median_low(sorted(others))
        usable = others[others >= typical / FLOAT_JUMP_RATIO]
        return float(usable.min()) if not usable.empty else None

    def flag_of(fact, count) -> str:
        flag = float_unit_flag(float(fact.val), fact.end, float(count["val"]) if count is not None else None,
                               lowest_other(fact), caps)
        if flag in ("ok", "review") and fact.val >= SHARES_X1000_MIN_FLOAT and shares_x1000(own_shares, count):
            return "shares_x1000_vs_other_shares"  # the float per share looks right only against x1000 shares
        return flag

    checked = []  # (value, flag) of every fact over twice the window, for the reference median
    for fact in mine[mine["end"] > filed - pd.DateOffset(years=2 * years)].itertuples():
        checked.append((float(fact.val), flag_of(fact, _matching_shares(own_shares, fact.accn, fact.end))))
    clean = sorted(v for v, flag in checked if flag == "ok")
    wide = clean or sorted(v for v, _ in checked if v > 0)
    median = statistics.median_low(wide) if wide else None
    facts = []
    for fact in own.itertuples():
        count = _matching_shares(own_shares, fact.accn, fact.end)
        flag = flag_of(fact, count)
        if flag in ("review", "no_shares") and median is not None and fact.val > FLOAT_MEDIAN_MULTIPLE * median:
            flag = f"{flag}_above_{FLOAT_MEDIAN_MULTIPLE}x_median"
        facts.append({"val": float(fact.val), "end": fact.end, "accn": fact.accn, "flag": flag,
                      "kept": flag in ("ok", "review", "no_shares"), "count": count})
    kept = [f for f in facts if f["kept"]]
    best = max(kept or facts, key=lambda f: (f["val"], f["end"]))
    result = {**empty, "public_float_usd": best["val"], "float_end_date": best["end"].date().isoformat(),
              "float_accession": best["accn"], "float_obs_3y": len(facts), "float_facts_dropped": len(facts) - len(kept)}
    if best["count"] is not None:
        n = float(best["count"]["val"])
        result.update(shares_outstanding=n, shares_end_date=best["count"]["end"].date().isoformat(),
                      implied_float_per_share=best["val"] / n if n > 0 else None)
    result["float_check_flag"] = best["flag"]
    return result


# ------------------------------------------------------------------ classification

def _norm_name(text: str) -> str:
    from scripts.reversal_data_security_master import normalize_issuer_name
    return normalize_issuer_name(text)


def classify_row(class_kind: str, basis: str, evidence: dict) -> tuple[str, str]:
    """(classification, evidence note) for one filing; see the module docstring.

    A removal for non-compliance (Rule 12d2-2(b)) stays a delisting even when the
    company is listed somewhere today: it left Nasdaq, and any relisting is later.
    When the subject CIK continues on Nasdaq but a ticker it had listed before the
    filing is gone afterwards (a tracking stock or a second class exchanged away),
    that class did end, so it is a delisting too.
    """
    if class_kind not in ("common", "ads"):
        return "other_class", f"class_kind={class_kind}"
    if evidence.get("unlisted"):
        return "unlisted_withdrawal", (f"{basis}; issuer Form 25 with no Nasdaq snapshot row of the CIK before it "
                                       f"(first seen {evidence.get('first_seen', '')})")
    handover = evidence.get("successor")
    unlinked = ""  # a handover the classification does not accept, named in the evidence note
    if handover and basis not in ("issuer_withdrawal", "exchange_removal"):  # a move or a removal, not a reorganisation
        tickers = " ".join(handover["tickers"])
        # A redemption (Rule 12d2-2(a)(2)) handing a ticker to a new registrant of another name is a
        # split-off only while the subject keeps another Nasdaq ticker (Liberty Media's Braves and Live
        # tracking stocks); otherwise the stock was cashed out and a spun-off company took the ticker
        # (Avidity's RNA, bought for cash by Novartis, went to Atrium Therapeutics in 2026-03).
        kept = set(evidence.get("tickers_continued", "").split()) - set(handover["tickers"])
        split_off = basis == "redeemed_or_matured" and bool(kept)
        if handover["new"] and (handover["names_match"] or basis == "substituted_merger_or_exchange" or split_off):
            label = "reorg" if handover["names_match"] else "reorg_review"
            names = "matching" if handover["names_match"] else "different"
            return label, f"{basis}; ticker {tickers} passed at the next snapshot to new Nasdaq registrant CIK {handover['cik']} ({names} names)"
        if not handover["new"]:
            return "common_delisting", (f"{basis}; ticker {tickers} taken over by CIK {handover['cik']}, "
                                        "already listed under another ticker (acquirer)")
        if basis == "redeemed_or_matured":
            unlinked = (f"; ticker {tickers} passed to new registrant CIK {handover['cik']} of a different name "
                        "and the subject kept no other Nasdaq ticker: no successor")
    if basis == "issuer_withdrawal" and evidence.get("transfer_hint"):
        return "transfer", f"{basis}; {evidence['transfer_hint']}"
    later = evidence.get("current_tickers", "")
    before, ended = evidence.get("tickers_before", ""), evidence.get("tickers_ended", "")
    if evidence.get("still_on_nasdaq_same_cik"):
        continued = set(evidence.get("tickers_continued", "").split())
        began = " ".join(t for t in evidence.get("tickers_new", "").split() if t in continued or not continued)
        renamed = [t for t in ended.split() if t not in evidence.get("tickers_handed_over", "").split()]
        kept = " ".join(sorted(continued)) or later
        if basis == "exchange_removal":
            what = f"ticker(s) {ended} removed while {kept} continued" if ended else f"ticker(s) {kept} continued in the snapshots"
            return "common_delisting", f"{basis}; {what}"
        if renamed and began:
            return "reorg", f"{basis}; ticker(s) {ended} ended and {began} began under the same CIK ({later})"
        if ended:
            return "common_delisting", f"{basis}; ticker(s) {ended} ended while the CIK stayed on Nasdaq ({kept})"
        if evidence.get("snapshot_continues"):
            return "reorg", f"{basis}; the CIK's Nasdaq ticker(s) {kept} continued past the filing ({later})"
        note = f"Nasdaq tickers before the filing: {before}" if before else "no snapshot ticker evidence"
        return "reorg", f"subject CIK still on Nasdaq ({later}) and files periodic reports >400d later; {note}"
    if evidence.get("listed_elsewhere_now"):
        kept = sorted(set(before.split()) & set(evidence.get("tickers_elsewhere", "").split()))
        if basis != "exchange_removal" and kept:
            return "transfer", f"ticker {' '.join(kept)} listed today on {evidence['listed_elsewhere_now']}; files periodic reports >400d later"
        return "common_delisting", f"{basis}; listed on {evidence['listed_elsewhere_now']} today ({later})"
    if evidence.get("relisted_later"):
        gone = f"ticker(s) {ended} left the Nasdaq lists at the filing" if ended else "not in the Nasdaq lists after the filing"
        return "common_delisting", f"{basis}; {gone}; relisted on Nasdaq later ({later})"
    return "common_delisting", basis + unlinked


def subject_exit(classification: str, evidence: dict) -> str:
    """'Y' when the filing ended the subject CIK's Nasdaq common listing: a delisting, transfer or
    handover to a new registrant that left no Nasdaq ticker of the CIK behind.

    It is 'Y' only when no ticker the CIK listed before the filing continued after it and no new
    ticker of the CIK took over (``still_on_nasdaq_same_cik``, from the step-4 raw snapshot rows
    through ``snapshot_evidence``); a filing with no snapshot evidence either way ends the listing.
    """
    if classification not in ("common_delisting", "transfer", "reorg", "reorg_review"):
        return "N"
    if classification.startswith("reorg") and not evidence.get("successor"):
        return "N"  # the same CIK continues
    if classification == "transfer" and evidence.get("transfer_hint"):
        # A documented move to another exchange (a Form 8-A12B, or the Form 25 names it) ends the listing
        # unless a ticker that continued in the lists is one SEC still lists on Nasdaq: the company lists
        # of 2016-2017 kept America Movil, Condor and Lilis for months after their moves to NYSE markets,
        # where no symbol directory is near enough to check them.
        credible = set(evidence.get("tickers_continued", "").split()) & set(evidence.get("nasdaq_now", "").split())
        return "N" if credible else "Y"
    if evidence.get("still_on_nasdaq_same_cik"):
        return "N"  # a ticker of the CIK continued past the filing (one class ended, or a ticker change)
    if evidence.get("tickers_continued"):
        return "N"
    before, ended = set(evidence.get("tickers_before", "").split()), set(evidence.get("tickers_ended", "").split())
    if ended and not before <= ended and not evidence.get("successor"):
        return "N"  # one class ended and another class of the CIK continued
    return "Y"


BEFORE_WINDOW_DAYS = 180  # a ticker seen this close before the filing was listed when it was filed
# (a suspended stock leaves the lists weeks or months before the exchange files: Windtree, WW)
NEW_TICKER_WINDOW = (30, 45)  # a ticker of the CIK first seen this many days before/after is new


class SnapshotEvidence:
    """The security master's raw snapshot rows (``ticker_rows_raw.csv.gz``: every resolved row before
    any row was dropped for a Form 25, and every other listed symbol with no CIK) with the snapshot
    dates, for the continuation tests of ``snapshot_evidence``."""

    def __init__(self, rows: pd.DataFrame, snapshot_dates: dict[str, list[str]], partial: set[str] = frozenset()):
        from scripts.reversal_data_security_master import build_intervals, presence_by_date
        self.days = sorted({d for days in snapshot_dates.values() for d in days})
        self.full = {d for family, days in snapshot_dates.items() if family not in partial for d in days}
        self.full_days = sorted(self.full)
        self.present = presence_by_date(rows)
        resolved = rows[rows["cik"].notna()].copy()
        resolved["cik"] = resolved["cik"].astype(int)
        self.obs = {key: sorted(set(g)) for key, g in resolved.groupby(["cik", "symbol"])["date"]}
        self.holders: dict[tuple[str, str], set[int]] = {}
        for (symbol, day), g in resolved.groupby(["symbol", "date"])["cik"]:
            self.holders[(symbol, day)] = set(g)
        self.tickers_of: dict[int, list[str]] = {}
        for cik, symbol in self.obs:
            self.tickers_of.setdefault(cik, []).append(symbol)
        runs_in = resolved.assign(share_class="COMMON", family="raw", source_url="", how="raw", name="")
        families = {family: days for family, days in snapshot_dates.items()}
        self.runs = build_intervals(runs_in, families, self.present)
        if not self.runs.empty:
            self.runs["start"] = pd.to_datetime(self.runs["start"])
            self.runs["end"] = pd.to_datetime(self.runs["end"])

    @classmethod
    def load(cls, rows_path: Path, dates_path: Path) -> "SnapshotEvidence | None":
        from scripts.reversal_data_security_master import read_raw_rows
        rows = read_raw_rows(rows_path)
        if rows is None or not Path(dates_path).exists():
            return None
        dates = json.loads(Path(dates_path).read_text())
        return cls(rows, dates["families"], set(dates.get("partial_families", [])))

    def status(self, cik: int, symbol: str, filing_date: str) -> dict:
        from scripts.reversal_data_security_master import continuation_after
        other = lambda day: bool(self.holders.get((symbol, day), set()) - {cik})
        return continuation_after(self.obs.get((cik, symbol), []), filing_date, self.days, self.full, self.present,
                                  symbol, other)

    def last_full_before(self, day: str) -> str:
        """The last full-list snapshot date on or before ``day`` ('' when none)."""
        i = bisect.bisect_right(self.full_days, day)
        return self.full_days[i - 1] if i else ""

    def seen_on(self, cik: int, symbol: str, day: str) -> bool:
        obs = self.obs.get((cik, symbol), [])
        i = bisect.bisect_left(obs, day)
        return i < len(obs) and obs[i] == day

    def ended_before(self, cik: int, symbol: str, day: str) -> bool:
        """Whether the CIK's run on ``symbol`` broke before ``day``: two full-list snapshots after its
        last sighting on or before ``day``, and up to it, do not list the symbol at all."""
        obs = self.obs.get((cik, symbol), [])
        i = bisect.bisect_right(obs, day)
        if not i:
            return False
        lo, hi = bisect.bisect_right(self.full_days, obs[i - 1]), bisect.bisect_right(self.full_days, day)
        absent = sum(1 for d in self.full_days[lo:hi] if symbol not in self.present.get(d, ()))
        return absent >= 2


def snapshot_evidence(snap: SnapshotEvidence | None, cik: int, filing_date: str, already_ended: set[str] = frozenset(),
                      sec_nasdaq_tickers: set[str] = frozenset(), sec_nasdaq: bool = False, recent: bool = False,
                      periodic_later: bool = False) -> dict:
    """Which of the CIK's Nasdaq tickers continued past the filing, from the raw snapshot rows.

    ``tickers_new``: tickers of the CIK first seen from 30 days before to 45 days after it (a ticker
    change, or new classes in exchange); ``tickers_before``: the other tickers the CIK was seen on
    within BEFORE_WINDOW_DAYS before the filing (less ``already_ended``, attributed to an earlier
    filing of the CIK, and less ``tickers_ended_earlier``: those absent from the last full list before
    the filing, where the CIK was listed under another ticker, and whose run had broken by then).
    Each is 'continued' when ``continuation_after`` says so; ``tickers_ended`` are
    those listed at the filing that did not continue. A run that the snapshots stop following
    before that ('open': the lists end, or list the symbol under no CIK) continues when SEC lists the
    CIK on Nasdaq today (for a filing at least 400 days old, only while the CIK keeps filing periodic
    reports); an open run with no observation after the filing ends with it. ``rows_before``: whether
    the CIK has any snapshot row on or before the filing date.
    """
    from scripts.reversal_data_security_master import EXIT_GRACE_DAYS
    out = {"tickers_before": "", "tickers_ended": "", "tickers_new": "", "tickers_continued": "",
           "tickers_handed_over": "", "tickers_ended_earlier": "", "rows_before": False, "rows_any": False,
           "first_seen": "", "snapshot_continues": None}
    if snap is None:
        return out
    tickers = snap.tickers_of.get(cik, [])
    if not tickers:
        return out
    shift = lambda days: (pd.Timestamp(filing_date) + pd.Timedelta(days=days)).date().isoformat()
    lo, new_lo, new_hi = shift(-BEFORE_WINDOW_DAYS), shift(-NEW_TICKER_WINDOW[0]), shift(NEW_TICKER_WINDOW[1])
    cutoff = shift(EXIT_GRACE_DAYS)
    first_seen = min(snap.obs[(cik, s)][0] for s in tickers)
    out.update(rows_any=True, rows_before=first_seen <= filing_date, first_seen=first_seen)
    before, new = [], []
    for symbol in tickers:
        if symbol in already_ended:
            continue
        obs = snap.obs[(cik, symbol)]
        i = bisect.bisect_right(obs, cutoff)
        if new_lo < obs[0] <= new_hi:  # first seen around the filing: a new ticker (PCSC -> FRNM)
            new.append(symbol)
        elif i and obs[i - 1] >= lo:
            before.append(symbol)
    # A ticker whose run broke before the filing while the CIK was still listed under another ticker on
    # the last full list before it was renamed or exchanged earlier, not listed at the filing (QRTEA
    # became QVCGA in 2025-02/03, months before QVC Group's Series B Form 25 of 2025-05-27). A CIK with
    # no ticker on that list keeps the window (a suspended stock leaves the lists before the filing).
    last_full = snap.last_full_before(filing_date)
    on_last = {s for s in tickers if last_full and snap.seen_on(cik, s, last_full)}
    earlier = sorted(s for s in before if on_last and s not in on_last and snap.ended_before(cik, s, filing_date))
    before = [s for s in before if s not in earlier]
    out["tickers_ended_earlier"] = " ".join(earlier)
    out["snapshot_continues"] = False
    if not before and not new:
        return out
    continued, ended, handed = [], [], []
    for symbol in sorted(before) + sorted(new):
        fate = snap.status(cik, symbol, filing_date)
        keeps = fate["status"] == "continued"
        if fate["status"] == "open" and (fate["after"] or fate["unresolved"] >= 2):
            keeps = sec_nasdaq and (symbol in sec_nasdaq_tickers or bool(fate["after"])) and (recent or periodic_later)
        if keeps:
            continued.append(symbol)
        elif symbol in before or snap.obs[(cik, symbol)][0] <= cutoff:  # listed at the filing, and gone
            ended.append(symbol)
            if fate["handover"]:
                handed.append(symbol)
    out.update(tickers_before=" ".join(sorted(before)), tickers_ended=" ".join(sorted(ended)),
               tickers_new=" ".join(sorted(new)), tickers_continued=" ".join(sorted(continued)),
               tickers_handed_over=" ".join(sorted(handed)), snapshot_continues=bool(continued))
    return out


def issuer_form25_before_listing(form: str, seen: dict) -> bool:
    """An issuer's own Form 25 filed before the CIK's first Nasdaq snapshot row: nothing listed was
    removed (ShiftPixy filed one on 2017-02-14 and first listed in 2017-07), so it ends no listing."""
    return str(form) in ("25", "25/A") and bool(seen.get("rows_any")) and not seen.get("rows_before")


def subject_evidence(subject_cik: int, filing_date: str, submissions: dict | None) -> dict:
    """SEC evidence from the subject's own submissions JSON (cached by the security master): periodic
    reports more than 400 days after the filing, and the exchanges SEC lists the CIK on today.
    ``still_on_nasdaq_same_cik`` is set here only as a fallback; ``snapshot_evidence`` overrides it
    whenever the snapshots follow the CIK's tickers past the filing."""
    if not submissions:
        return {}
    from scripts.reversal_data_security_master import parse_submissions
    profile = parse_submissions(submissions)
    filed = pd.Timestamp(filing_date)
    later = [pd.Timestamp(d) for d in profile["periodic_dates"] if pd.Timestamp(d) > filed + pd.Timedelta(days=400)]
    exchanges = {str(x or "") for x in profile["exchanges"]}
    evidence = {"periodic_after_400d": len(later)}
    evidence["tickers_elsewhere"] = " ".join(t for t, x in zip(profile["tickers"], profile["exchanges"])
                                             if x and x not in ("Nasdaq", "OTC"))
    evidence["sec_nasdaq_tickers"] = {t for t, x in zip(profile["tickers"], profile["exchanges"]) if x == "Nasdaq"}
    if later or "Nasdaq" in exchanges:
        evidence["current_tickers"] = " ".join(f"{t}:{x}" for t, x in zip(profile["tickers"], profile["exchanges"]))
    if later and "Nasdaq" in exchanges:
        evidence["still_on_nasdaq_same_cik"] = True
    elif later and exchanges - {"Nasdaq", "OTC", ""}:
        evidence["listed_elsewhere_now"] = "/".join(sorted(exchanges - {"Nasdaq", "OTC", ""}))
    return evidence


HANDOVER_WINDOW_DAYS = 60  # the subject held the ticker this close to the filing, and the successor took it after
MIN_SUCCESSOR_DAYS = 30  # a successor holds the ticker at least this long (or it is its first ticker)
MIN_SUCCESSOR_SNAPSHOTS = 5  # and is seen this often in the year after the takeover


def successor_holds(successor: pd.DataFrame, ticker: str, takeover: pd.Timestamp) -> bool:
    """Whether a CIK first seen on ``ticker`` at ``takeover`` really took it over, rather than being a
    stray row or two of a lagging list (Broadcom Ltd on BRCM the day after Broadcom Corp's Form 25, or
    Barrick on Randgold's GOLD in two company lists): it is seen at least MIN_SUCCESSOR_SNAPSHOTS
    times in the year after, and holds the ticker MIN_SUCCESSOR_DAYS or has it as its first Nasdaq
    ticker (Ribbon kept SONS a month before RBBN; Tessera Holding moved from TSRA to XPER)."""
    year = successor[(successor["end"] >= takeover) & (successor["start"] <= takeover + pd.Timedelta(days=365))]
    if year["n_snapshots"].sum() < MIN_SUCCESSOR_SNAPSHOTS:
        return False
    on_ticker = successor[successor["ticker"] == ticker]
    if on_ticker["end"].max() - takeover >= pd.Timedelta(days=MIN_SUCCESSOR_DAYS):
        return True
    return not ((successor["ticker"] != ticker) & (successor["start"] <= takeover)).any()


def successor_evidence(rows: pd.DataFrame, intervals: pd.DataFrame | None, names: dict[int, list[str]],
                       snapshot_dates: list[str] | None = None) -> dict[str, dict]:
    """accession -> ticker handover found in the snapshot intervals.

    A handover: a ticker the subject held up to HANDOVER_WINDOW_DAYS before the filing is held by
    another CIK at the next snapshot after the subject's last one (whatever the gap between
    snapshots), or overlaps it, starting no earlier than that window. The successor is ``new`` when
    it had no Nasdaq listing under any ticker before the handover (a holding company formed for the
    reorganisation, as Alphabet for Google) rather than an acquirer already listed under another
    ticker that adopted the subject's (Tornier taking WMGI). ``names_match`` compares the EDGAR
    names (current and former) of the two CIKs.
    """
    from scripts.reversal_data_security_master import names_match
    if intervals is None or intervals.empty:
        return {}
    iv = intervals.copy()
    iv["start"], iv["end"] = pd.to_datetime(iv["start"], errors="coerce"), pd.to_datetime(iv["end"], errors="coerce")
    iv["cik"] = iv["cik"].astype(int)
    iv["n_snapshots"] = pd.to_numeric(iv["n_snapshots"], errors="coerce").fillna(1) if "n_snapshots" in iv else 1
    iv = iv[iv["exchange"].astype(str).str.upper().str.startswith("NASDAQ")]
    by_cik = {cik: group for cik, group in iv.groupby("cik")}
    by_ticker = {ticker: group for ticker, group in iv.groupby("ticker")}
    first_listed = iv.groupby("cik")["start"].min().to_dict()
    days = sorted(pd.Timestamp(d) for d in (snapshot_dates or []))
    window = pd.Timedelta(days=HANDOVER_WINDOW_DAYS)

    def next_snapshot(day: pd.Timestamp) -> pd.Timestamp:
        i = bisect.bisect_right(days, day)
        return days[i] if i < len(days) else day + pd.Timedelta(days=7)

    found = {}
    for row in rows.itertuples():
        own = by_cik.get(row.subject_cik)
        if own is None:
            continue
        filed = pd.Timestamp(row.filing_date)
        held = own[(own["start"] <= filed + pd.Timedelta(days=15)) & (own["end"] >= filed - window)]
        handed: dict[int, list[str]] = {}
        for ticker, mine in held.groupby("ticker"):
            last = mine["end"].max()
            if last > filed + window:
                continue  # the subject kept this ticker well past the filing
            reach = max(next_snapshot(last), last + pd.Timedelta(days=7))
            others = by_ticker.get(ticker)
            if others is None:
                continue
            others = others[(others["cik"] != row.subject_cik) & (others["start"] >= filed - window)
                            & (others["start"] <= reach) & (others["start"] > mine["start"].min())]
            for other in others.sort_values("start").itertuples():
                if successor_holds(by_cik[int(other.cik)], ticker, other.start):
                    handed.setdefault(int(other.cik), []).append(ticker)
                    break
        if not handed:
            continue
        successor, tickers = max(handed.items(), key=lambda kv: (len(kv[1]), -kv[0]))
        took = by_ticker[tickers[0]]
        took = took[took["cik"] == successor]["start"].min()
        own_names = {_norm_name(n) for n in names.get(row.subject_cik, [])} - {""}
        other_names = {_norm_name(n) for n in names.get(successor, [])} - {""}
        found[row.accession] = {
            "cik": successor, "tickers": sorted(tickers),
            "new": first_listed.get(successor, took) >= took - pd.Timedelta(days=30),
            "names_match": any(names_match(a, b) for a in own_names for b in other_names)}
    return found


# ------------------------------------------------------------------ build

def _parallel(function, items, label):
    from scripts.reversal_data_security_master import parallel_map
    return parallel_map(function, items, label=label)


def nasdaq_filed_rows(offline: bool) -> tuple[pd.DataFrame, list[dict], int]:
    """25-NSE filings with Nasdaq as filer, with their parsed documents."""
    hits, log = all_hits(offline=offline)
    rows = pd.DataFrame(filter_nasdaq_filer(hits))

    def document(row):
        data = fetch_form25_doc(row.subject_cik, row.accession, row.document, offline=offline)
        FILES_READ.add(str(SEC_RAW / "docs" / str(int(row.subject_cik)) / row.accession / row.document))
        parsed = parse_form25_xml(data) if data else {"parse": "missing"}
        return {**parsed, "on_nasdaq": True, "other_exchange": "", "rule_parse": "xml" if data else "missing"}

    docs = pd.DataFrame(_parallel(document, list(rows.itertuples()), "25-NSE documents")).add_prefix("doc_")
    return pd.concat([rows.reset_index(drop=True), docs], axis=1), log, len(hits)


def issuer_filed_rows(offline: bool) -> tuple[pd.DataFrame, list[dict], int]:
    """Issuer-filed Form 25 whose cover names Nasdaq as the exchange the class is removed from."""
    hits, log = all_hits(ISSUER_FORM, offline=offline, search=issuer_search)
    rows = pd.DataFrame(issuer_filings(hits))
    if rows.empty:
        return rows, log, len(hits)

    def document(row):
        data = fetch_form25_doc(row.subject_cik, row.accession, row.document, offline=offline)
        FILES_READ.add(str(SEC_RAW / "docs" / str(int(row.subject_cik)) / row.accession / row.document))
        return parse_issuer_form25(data) if data else {"parse": "missing", "on_nasdaq": False}

    docs = pd.DataFrame(_parallel(document, list(rows.itertuples()), "issuer Form 25 documents")).add_prefix("doc_")
    rows = pd.concat([rows.reset_index(drop=True), docs], axis=1)
    return rows[rows["doc_on_nasdaq"].fillna(False).astype(bool)].reset_index(drop=True), log, len(hits)


def build(offline: bool = False) -> pd.DataFrame:
    nse, log, n_hits = nasdaq_filed_rows(offline)
    print(f"EFTS 25-NSE: {n_hits} hits in {len(log)} slices, {len(nse)} Nasdaq filings", flush=True)
    issuer, issuer_log, n_issuer_hits = issuer_filed_rows(offline)
    print(f"EFTS 25 naming Nasdaq: {n_issuer_hits} hits, {len(issuer)} with Nasdaq as the exchange", flush=True)
    rows = pd.concat([nse, issuer], ignore_index=True)
    rows["subject_cik"] = [int(i) if pd.notna(i) and i else int(s) for i, s in zip(rows["doc_issuer_cik"], rows["subject_cik"])]
    rows["subject_name"] = [i if isinstance(i, str) and i else e for i, e in zip(rows["doc_issuer_name"], rows["subject_name_efts"])]
    rows["subject_tickers_sec"] = rows["subject_tickers_sec"].fillna("")
    rows["class_of_security"] = rows["doc_class_of_security"].fillna("")
    rows["rule_provision"] = rows["doc_rule_provision"].fillna("")
    rows["rule_parse"] = rows["doc_rule_parse"].fillna("")
    rows["class_kinds"] = ["|".join(class_kinds(c)) for c in rows["class_of_security"]]
    rows["class_kind"] = rows["class_of_security"].map(classify_security_class)
    rows["delisting_basis"] = rows["rule_provision"].map(delisting_basis)
    rows["effective_date"] = [(date.fromisoformat(d) + timedelta(days=EFFECTIVE_LAG_DAYS)).isoformat() for d in rows["filing_date"]]
    rows["doc_url"] = [doc_url(c, a, d) for c, a, d in zip(rows["subject_cik"], rows["accession"], rows["document"])]

    from scripts.reversal_data_security_master import (RAW_ROWS, foreign_filer_flag, load_submissions,
                                                       parse_submissions, read_raw_rows)
    # The security master's raw snapshot rows, from before it dropped any row for a Form 25: the
    # exit decisions below never rest on rows removed because of them.
    raw_rows = read_raw_rows(RAW_ROWS)
    caps = load_market_caps(raw_rows)
    floats = fetch_float_frames(*FLOAT_CONCEPT, frame_periods(), offline=offline)
    shares = fetch_float_frames(*SHARES_CONCEPT, frame_periods(), offline=offline)
    print(f"frames: {len(floats)} float facts, {len(shares)} shares facts; listed market caps for {len(caps)} CIKs",
          flush=True)
    float_by_cik = {c: g for c, g in floats.groupby("cik")}
    shares_by_cik = {c: g for c, g in shares.groupby("cik")}
    empty = floats.iloc[:0]
    float_rows = pd.DataFrame([float_for_filing(float_by_cik.get(c, empty), shares_by_cik.get(c, empty), c, d,
                                                caps=caps.get(int(c)))
                               for c, d in zip(rows["subject_cik"], rows["filing_date"])])
    rows = pd.concat([rows, float_rows], axis=1)

    snap = None
    if raw_rows is not None and SNAPSHOT_DATES.exists():
        dates = json.loads(SNAPSHOT_DATES.read_text())
        snap = SnapshotEvidence(raw_rows, dates["families"], set(dates.get("partial_families", [])))
    runs = snap.runs if snap is not None else None
    snapshot_dates = []
    if SNAPSHOT_DATES.exists():
        dates = json.loads(SNAPSHOT_DATES.read_text())
        snapshot_dates = sorted({d for family, days in dates["families"].items()
                                 if family not in dates.get("partial_families", []) for d in days})
    rows = rows.sort_values(["filing_date", "accession"]).reset_index(drop=True)
    names, evidence, ended_by_cik, payloads = {}, [], {}, {}
    recent_from = (pd.Timestamp(END) - pd.Timedelta(days=400)).date().isoformat()

    def payload_of(cik: int) -> dict | None:
        if cik not in payloads:
            payloads[cik] = load_submissions(cik, offline=True)
            if payloads[cik]:
                FILES_READ.add(str(SEC_RAW / "submissions" / f"CIK{int(cik):010d}.json.gz"))
        return payloads[cik]

    for i, row in rows.iterrows():
        if row["class_kind"] == "receipt":
            payload = payload_of(row["subject_cik"])
            flag = foreign_filer_flag(parse_submissions(payload)) if payload else "UNKNOWN"
            rows.at[i, "class_kind"] = resolve_receipt("receipt", flag, row["subject_tickers_sec"])
    common_rows = rows["class_kind"].isin(["common", "ads"])
    for row in rows.itertuples():
        payload = payload_of(row.subject_cik) if common_rows[row.Index] else None
        if payload:
            profile = parse_submissions(payload)
            names[row.subject_cik] = [profile["name"], *[f["name"] for f in profile["former_names"]]]
        ev = subject_evidence(row.subject_cik, row.filing_date, payload)
        if common_rows[row.Index]:
            done = {t for t, day in ended_by_cik.get(row.subject_cik, {}).items()
                    if pd.Timestamp(row.filing_date) - pd.Timestamp(day) <= pd.Timedelta(days=BEFORE_WINDOW_DAYS)}
            sec_tickers = ev.pop("sec_nasdaq_tickers", set())
            ev["nasdaq_now"] = " ".join(sorted(sec_tickers))
            seen = snapshot_evidence(snap, row.subject_cik, row.filing_date, done, sec_tickers, bool(sec_tickers),
                                     row.filing_date >= recent_from, ev.get("periodic_after_400d", 0) > 0)
            ev.update({k: v for k, v in seen.items() if k != "snapshot_continues"})
            if seen["snapshot_continues"] is not None:  # the snapshots follow the CIK: they decide
                sec_says = bool(ev.get("still_on_nasdaq_same_cik"))
                ev["still_on_nasdaq_same_cik"] = seen["snapshot_continues"]
                ev["snapshot_continues"] = seen["snapshot_continues"]
                if sec_says and not seen["snapshot_continues"]:
                    ev["relisted_later"] = True
                    ev.pop("listed_elsewhere_now", None)
                if issuer_form25_before_listing(row.form, seen):
                    ev["unlisted"] = True
            ended_by_cik.setdefault(row.subject_cik, {}).update({t: row.filing_date for t in seen["tickers_ended"].split()})
            if row.delisting_basis == "issuer_withdrawal":
                hints = []
                if row.doc_other_exchange:
                    hints.append(f"the Form 25 names {row.doc_other_exchange}")
                near = filings_near(row.subject_cik, row.filing_date, {"8-A12B", "8-A12B/A"}, offline=offline)
                if near:
                    hints.append(f"Form {near[0][0]} filed {near[0][1]} (registration on another exchange)")
                if hints:
                    ev["transfer_hint"] = "; ".join(hints)
        else:
            ev.pop("sec_nasdaq_tickers", None)
        evidence.append(ev)
    if runs is not None and not runs.empty:  # names of the CIKs that held a ticker some subject held (possible successors)
        subjects = set(rows.loc[common_rows, "subject_cik"].astype(int))
        shared = set(runs.loc[runs["cik"].isin(subjects), "ticker"])
        for cik in runs.loc[runs["ticker"].isin(shared), "cik"].unique():
            if cik not in names:
                payload = payload_of(int(cik))
                if payload:
                    profile = parse_submissions(payload)
                    names[int(cik)] = [profile["name"], *[f["name"] for f in profile["former_names"]]]
    successors = successor_evidence(rows[common_rows], runs, names, snapshot_dates)
    out = []
    for row, ev in zip(rows.itertuples(), evidence):
        if row.accession in successors:
            ev["successor"] = successors[row.accession]
        label, note = classify_row(row.class_kind, row.delisting_basis, ev)
        if not (label.startswith("reorg") and "new Nasdaq registrant" in note):
            ev.pop("successor", None)  # a handover the classification did not rest on
        handover = ev.get("successor")
        out.append((label, note, subject_exit(label, ev) if row.class_kind in ("common", "ads") else "N",
                    handover["cik"] if handover else None, " ".join(handover["tickers"]) if handover else ""))
    rows["classification"] = [o[0] for o in out]
    rows["classification_evidence"] = [o[1] for o in out]
    rows["subject_exit"] = [o[2] for o in out]
    rows["successor_cik"] = pd.array([o[3] for o in out], dtype="Int64")
    rows["successor_tickers"] = [o[4] for o in out]
    for column in ("tickers_before", "tickers_ended", "tickers_new", "tickers_continued"):
        rows[column] = [ev.get(column, "") for ev in evidence]
    rows = rows.sort_values(["filing_date", "accession"]).reset_index(drop=True)
    table = rows[PLAN_COLUMNS + EXTRA_COLUMNS]
    common.atomic_write(OUTPUT, table.to_csv(index=False).encode("utf-8"))
    FILES_READ.update(str(SEC_RAW / "frames" / f"{c}_{p}.json.gz") for c in (FLOAT_CONCEPT[0], SHARES_CONCEPT[0])
                      for p in frame_periods())
    FILES_READ.update(str(SEC_RAW / "efts" / f"{FORM}_cik{NASDAQ_CIK}_{s}_{e}_{f}.json") for s, e, f in _pages(log))
    FILES_READ.update(str(SEC_RAW / "efts" / f"{ISSUER_FORM}_qNasdaq_{s}_{e}_{f}.json") for s, e, f in _pages(issuer_log))
    write_files_read()
    common_mask = rows["class_kind"].isin(["common", "ads"])
    summary = {
        "slices": log, "issuer_slices": issuer_log, "hits": n_hits, "issuer_hits": n_issuer_hits,
        "nasdaq_filings": len(nse), "issuer_filings_naming_nasdaq": len(issuer), "rows": len(rows),
        "documents_missing": int((rows["doc_parse"] == "missing").sum()),
        "documents_regex_parsed": int((rows["doc_parse"] == "regex").sum()),
        "issuer_rule_parse": dict(Counter(rows.loc[rows["form"].isin(["25", "25/A"]), "rule_parse"])),
        "exchange_cik_mismatch": int(((rows["doc_exchange_cik"].notna()) & (rows["doc_exchange_cik"] != NASDAQ_CIK)
                                      & rows["form"].str.startswith("25-NSE")).sum()),
        "accession_prefix_not_nasdaq": int((~rows["accession_prefix_is_filer"].astype(bool) & rows["form"].str.startswith("25-NSE")).sum()),
        "multi_subject_filings": int((rows["n_subject_ciks"] > 1).sum()),
        "forms": dict(Counter(rows["form"])), "class_kind": dict(Counter(rows["class_kind"])),
        "classification": dict(Counter(rows["classification"])),
        "classification_by_form": {f: dict(Counter(g["classification"])) for f, g in rows.groupby("form")},
        "delisting_basis": dict(Counter(rows["delisting_basis"])),
        "float_check_flag": dict(Counter(rows["float_check_flag"])),
        "float_facts_dropped_rows": int((rows["float_facts_dropped"] > 0).sum()),
        "common_rows_listing_other_classes": int((common_mask & rows["class_kinds"].str.contains("|", regex=False)).sum()),
        "subject_exit": dict(Counter(rows.loc[common_mask, "subject_exit"])),
        "successor_links": int(rows["successor_cik"].notna().sum()),
        "unique_subject_ciks": int(rows["subject_cik"].nunique()),
        "by_year": dict(Counter(rows["filing_date"].str[:4])),
    }
    common.atomic_write(SEC_RAW / "derived" / "form25_build_summary.json", (json.dumps(summary, indent=2) + "\n").encode())
    print(json.dumps({k: v for k, v in summary.items() if k not in ("slices", "issuer_slices")}, indent=2), flush=True)
    return table


def _pages(log: list[dict]) -> list[tuple[str, str, int]]:
    """(start, end, from) of every search page the slices log says was read."""
    return [(entry["start"], entry["end"], i * PAGE_SIZE) for entry in log for i in range(entry["pages"])]


def write_files_read() -> None:
    """The raw/sec files this builder read, so the manifest can hash only these; plus the probe
    requests made by hand to test the issuer-filed search (not read by any builder)."""
    probes = sorted(str(p) for p in (SEC_RAW / "efts").glob("probe_*.json"))
    summary = {"files": sorted(FILES_READ), "probe_requests_not_read": probes,
               "probe_note": "four requests on 2026-10-01 testing whether EFTS finds issuer-filed Form 25 for "
                             "Nasdaq: ciks=Oracle 2013 (found; lists only the issuer CIK), q=Nasdaq forms=25 "
                             "2013-H2 (15 hits), and the Oracle and VimpelCom documents"}
    common.atomic_write(SEC_RAW / "derived" / "files_read_form25.json", (json.dumps(summary, indent=1) + "\n").encode())


# ------------------------------------------------------------------ comparison with sue_lt_2020_2026

def compare_with_sue_lt(new: pd.DataFrame, old_path: Path = OLD_LIST, offline: bool = True) -> dict:
    """Row-level comparison of 2020-01-01 .. old max date, keyed on (filing date, subject CIK).
    The old list holds 25-NSE filings only, so issuer-filed Form 25 rows are left out."""
    old = pd.read_csv(old_path)
    new = new[new["form"].astype(str).str.startswith("25-NSE")]
    old_end = old["date"].max()
    recent = new[(new["filing_date"] >= "2020-01-01") & (new["filing_date"] <= old_end)].copy()
    key = lambda frame, d, c: Counter(zip(frame[d], frame[c].astype(int)))
    old_keys, new_keys = key(old, "date", "cik"), key(recent, "filing_date", "subject_cik")
    only_old, only_new = old_keys - new_keys, new_keys - old_keys
    both = old_keys & new_keys
    floats = fetch_float_frames(*FLOAT_CONCEPT, frame_periods(), offline=offline)
    window = floats[(floats["end"] >= "2019-01-01") & (floats["end"] <= "2024-12-31")]
    max_19_24 = window.groupby("cik")["val"].max()
    merged = old.merge(recent[["filing_date", "subject_cik", "public_float_usd", "class_kind", "classification"]]
                       .drop_duplicates(["filing_date", "subject_cik"]),
                       left_on=["date", "cik"], right_on=["filing_date", "subject_cik"], how="left")
    merged["recomputed_max_2019_2024"] = merged["cik"].map(max_19_24)
    has_old = merged["max_float_2019_2024"].notna()
    agree = (merged["recomputed_max_2019_2024"] - merged["max_float_2019_2024"]).abs() <= 1.0
    return {
        "old_rows": len(old), "old_date_range": [old["date"].min(), old_end],
        "new_rows_same_window": len(recent), "matched_rows": sum(both.values()),
        "only_in_old": sum(only_old.values()), "only_in_new": sum(only_new.values()),
        "only_in_old_examples": [f"{d} {c}" for d, c in list(only_old)[:20]],
        "only_in_new_examples": [f"{d} {c}" for d, c in list(only_new)[:20]],
        "new_rows_after_old_end": int((new["filing_date"] > old_end).sum()),
        "old_rows_with_float": int(has_old.sum()),
        "old_float_reproduced_by_max_2019_2024_frames": int((has_old & agree).sum()),
        "old_float_not_reproduced": int((has_old & ~agree).sum()),
        "old_float_not_reproduced_examples": merged.loc[has_old & ~agree, ["date", "cik", "max_float_2019_2024",
                                                                           "recomputed_max_2019_2024"]].head(10).to_dict("records"),
        "new_classification_of_old_rows": dict(Counter(merged["classification"].fillna("unmatched"))),
        "old_float_vs_new_3y_float_differs": int((has_old & merged["public_float_usd"].notna()
                                                  & ((merged["public_float_usd"] - merged["max_float_2019_2024"]).abs() > 1.0)).sum()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="use cached responses only")
    args = parser.parse_args(argv)
    table = build(offline=args.offline)
    comparison = compare_with_sue_lt(table)
    common.atomic_write(COMPARISON, (json.dumps(comparison, indent=2, default=str) + "\n").encode())
    print(json.dumps(comparison, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
