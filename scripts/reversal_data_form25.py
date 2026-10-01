"""Plan step 3: every Nasdaq Form 25-NSE filed 2012-01-01 .. 2026-09-30, with public float.

Data only. Nothing here computes signals or returns.

Sources (all SEC, all through ``SEC_LIMITER`` and ``sec_headers()``):
- EDGAR full-text search (``efts.sec.gov/LATEST/search-index``), form 25-NSE,
  restricted to filings that list Nasdaq (CIK 1354457) as an entity, in
  half-year slices of 100 hits per page.
- The 25-NSE primary XML document of every Nasdaq hit. The search hit carries
  no class of security, so the document is the only place that says whether
  the removed class is the common stock, a warrant, a unit, a preferred or a
  note. Documents of other exchanges' 25-NSE filings are never fetched.
- XBRL frames ``dei/EntityPublicFloat`` (USD) and
  ``dei/EntityCommonStockSharesOutstanding`` (shares), CY2011Q1I..CY2026Q2I.

Classification of a row (plan column ``classification``):
- ``other_class``: the document names a class that is not common equity
  (warrants, units, rights, preferred, debt, LP units, fund shares).
- ``common_delisting``: the document names common stock, ordinary shares or
  American depositary shares.
- ``reorg``: common, and SEC evidence shows the same equity continuing on
  Nasdaq: either the subject CIK still lists a Nasdaq ticker and keeps filing
  periodic reports, or a different CIK with a matching name holds the same
  ticker on Nasdaq within 45 days (needs ``ticker_intervals.csv`` from the
  security master; the script is run again after it).
- ``transfer``: common, the subject keeps filing periodic reports more than a
  year later and SEC lists it today on another national exchange. The 25-NSE
  document never states a transfer, so this is evidence-based and flagged.

``effective_date`` is the filing date plus 10 days (Rule 12d2-2(d)(1)); the
document carries no effective date, and trading has usually stopped before.

Usage::

    PYTHONPATH=. python scripts/reversal_data_form25.py            # fetch + build
    PYTHONPATH=. python scripts/reversal_data_form25.py --offline  # rebuild from cache only
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

import pandas as pd

from scripts import reversal_data_common as common

NASDAQ_CIK = 1354457
FORM = "25-NSE"
START, END = "2012-01-01", "2026-09-30"
PAGE_SIZE = 100
SEC_RAW = common.RAW / "sec"
EFTS_URL = ("https://efts.sec.gov/LATEST/search-index?forms={form}&ciks={cik:010d}"
            "&dateRange=custom&startdt={start}&enddt={end}&from={from_}")
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{document}"
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/dei/{concept}/{unit}/{period}.json"
FLOAT_CONCEPT, SHARES_CONCEPT = ("EntityPublicFloat", "USD"), ("EntityCommonStockSharesOutstanding", "shares")
FRAME_FIRST, FRAME_LAST = (2011, 1), (2026, 2)
OUTPUT = common.INPUTS / "form25_nasdaq_2012_2026.csv"
OLD_LIST = Path("output/research_only/sue_lt_2020_2026/inputs/sec_form25_nasdaq_2020_2026.csv")
TICKER_INTERVALS = common.INPUTS / "ticker_intervals.csv"
COMPARISON = SEC_RAW / "derived" / "form25_vs_sue_lt_2020_2026.json"
EFFECTIVE_LAG_DAYS = 10
FLOAT_LOOKBACK_YEARS = 3
# Implied float per share (float / shares outstanding) outside this band means a unit error
# in one of the two facts; a real Nasdaq price times a float fraction stays inside it.
IMPLIED_PER_SHARE_BAND = (0.01, 10_000.0)
MAX_PLAUSIBLE_FLOAT = 5e12
RATIO_BAND = (0.05, 1.5)  # the plan's check once a raw close is available

PLAN_COLUMNS = ["filing_date", "effective_date", "accession", "subject_cik", "subject_name", "filer_cik",
                "class_of_security", "classification", "public_float_usd", "float_check_flag"]
EXTRA_COLUMNS = ["form", "document", "rule_provision", "delisting_basis", "class_kind", "subject_tickers_sec",
                 "n_subject_ciks", "float_end_date", "float_accession", "float_obs_3y", "shares_outstanding",
                 "shares_end_date", "implied_float_per_share", "classification_evidence", "doc_url"]


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
    ("ads", r"american deposit[ao]ry|depository receipts?|depositary receipts?|\badss?\b|\badrs?\b|new york registry|"
            r"deposit[ao]ry shares.*ordinary"),
    ("preferred", r"deposit[ao]ry shares"),  # a bare depositary share on Nasdaq is a fractional preferred
    ("common", r"common|ordinary|capital stock|beneficial interest|\bstock\b|\bshares?\b"),
]


def classify_security_class(description: str) -> str:
    text = str(description or "").lower()
    if not text.strip():
        return "unknown"
    for kind, pattern in CLASS_RULES:
        if re.search(pattern, text, re.I):
            return kind
    return "other"


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
    return "ok"


def float_for_filing(floats: pd.DataFrame, shares: pd.DataFrame, cik: int, filing_date: str,
                     years: int = FLOAT_LOOKBACK_YEARS) -> dict:
    """Maximum public float with an end date in the ``years`` before ``filing_date``, and its unit check."""
    empty = {"public_float_usd": None, "float_end_date": "", "float_accession": "", "float_obs_3y": 0,
             "shares_outstanding": None, "shares_end_date": "", "implied_float_per_share": None}
    filed = pd.Timestamp(filing_date)
    own = floats[(floats["cik"] == cik) & (floats["end"] <= filed) & (floats["end"] > filed - pd.DateOffset(years=years))]
    if own.empty:
        return {**empty, "float_check_flag": "no_float"}
    best = own.sort_values(["val", "end"], ascending=[False, False]).iloc[0]
    result = {**empty, "public_float_usd": float(best["val"]), "float_end_date": best["end"].date().isoformat(),
              "float_accession": best["accn"], "float_obs_3y": int(len(own))}
    own_shares = shares[shares["cik"] == cik]
    same = own_shares[own_shares["accn"] == best["accn"]]
    if same.empty:  # the same 10-K's cover-page count, else the nearest count within a year
        gap = (own_shares["end"] - best["end"]).abs()
        same = own_shares[gap <= pd.Timedelta(days=366)].assign(gap=gap).sort_values("gap")
    if not same.empty:
        row = same.iloc[0]
        result.update(shares_outstanding=float(row["val"]), shares_end_date=row["end"].date().isoformat(),
                      implied_float_per_share=float(best["val"]) / float(row["val"]) if row["val"] > 0 else None)
    result["float_check_flag"] = check_float_units(result["public_float_usd"], result["shares_outstanding"])
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
    if evidence.get("successor_same_ticker"):
        return "reorg", f"same ticker on Nasdaq under CIK {evidence['successor_same_ticker']} with matching name"
    later = evidence.get("current_tickers", "")
    before, ended = evidence.get("tickers_before", ""), evidence.get("tickers_ended", "")
    if evidence.get("still_on_nasdaq_same_cik"):
        if basis == "exchange_removal":
            return "common_delisting", f"{basis}; relisted on Nasdaq later ({later})"
        began = evidence.get("tickers_new", "")
        if ended and began:
            return "reorg", f"{basis}; ticker(s) {ended} ended and {began} began under the same CIK ({later})"
        if ended:
            return "common_delisting", f"{basis}; ticker(s) {ended} ended while the CIK stayed on Nasdaq ({later})"
        note = f"Nasdaq tickers before the filing: {before}" if before else "no snapshot ticker evidence"
        return "reorg", f"subject CIK still on Nasdaq ({later}) and files periodic reports >400d later; {note}"
    if evidence.get("listed_elsewhere_now"):
        kept = sorted(set(before.split()) & set(evidence.get("tickers_elsewhere", "").split()))
        if basis != "exchange_removal" and kept:
            return "transfer", f"ticker {' '.join(kept)} listed today on {evidence['listed_elsewhere_now']}; files periodic reports >400d later"
        return "common_delisting", f"{basis}; listed on {evidence['listed_elsewhere_now']} today ({later})"
    return "common_delisting", basis


def nasdaq_tickers_around(intervals: pd.DataFrame | None, cik: int, filing_date: str,
                          already_ended: set[str] = frozenset()) -> tuple[list[str], list[str], list[str]]:
    """From the security master's snapshot intervals: the tickers the CIK had listed on Nasdaq up to
    60 days before the filing (less ``already_ended``, attributed to an earlier filing), those of them
    not seen 30 days or more after it, and tickers of the CIK first seen from 30 days before to 45
    days after the filing (a ticker change or new classes in exchange)."""
    if intervals is None or intervals.empty:
        return [], [], []
    own = intervals[intervals["cik"] == cik]
    if own.empty:
        return [], [], []
    filed = pd.Timestamp(filing_date)
    before = own[(own["start"] <= filed) & (own["end"] >= filed - pd.Timedelta(days=60))]
    tickers = sorted(set(before["ticker"]) - set(already_ended))
    after = own[own["end"] >= filed + pd.Timedelta(days=30)]
    first_seen = own.groupby("ticker")["start"].min()
    new = first_seen[(first_seen > filed - pd.Timedelta(days=30)) & (first_seen <= filed + pd.Timedelta(days=45))]
    return tickers, sorted(set(tickers) - set(after["ticker"])), sorted(set(new.index) - set(tickers))


def subject_evidence(subject_cik: int, filing_date: str, submissions: dict | None) -> dict:
    """Continuation evidence from the subject's own submissions JSON (cached by the security master)."""
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
    if later:
        evidence["current_tickers"] = " ".join(f"{t}:{x}" for t, x in zip(profile["tickers"], profile["exchanges"]))
    if later and "Nasdaq" in exchanges:
        evidence["still_on_nasdaq_same_cik"] = True
    elif later and exchanges - {"Nasdaq", "OTC", ""}:
        evidence["listed_elsewhere_now"] = "/".join(sorted(exchanges - {"Nasdaq", "OTC", ""}))
    return evidence


def successor_evidence(rows: pd.DataFrame, intervals: pd.DataFrame | None, names: dict[int, list[str]]) -> dict[str, int]:
    """accession -> successor CIK holding the subject's last Nasdaq ticker within 45 days, names matching."""
    if intervals is None or intervals.empty:
        return {}
    iv = intervals.copy()
    iv["start"], iv["end"] = pd.to_datetime(iv["start"], errors="coerce"), pd.to_datetime(iv["end"], errors="coerce")
    iv["cik"] = iv["cik"].astype(int)
    iv = iv[iv["exchange"].astype(str).str.upper().str.startswith("NASDAQ")]
    by_cik = {cik: group for cik, group in iv.groupby("cik")}
    by_ticker = {ticker: group for ticker, group in iv.groupby("ticker")}
    found = {}
    for row in rows.itertuples():
        own = by_cik.get(row.subject_cik)
        if own is None:
            continue
        filed = pd.Timestamp(row.filing_date)
        before = own[(own["start"] <= filed + pd.Timedelta(days=15))]
        if before.empty:
            continue
        ticker = before.sort_values("end").iloc[-1]["ticker"]
        others = by_ticker.get(ticker)
        if others is None:
            continue
        others = others[(others["cik"] != row.subject_cik)
                        & ((others["start"] - filed).abs() <= pd.Timedelta(days=45))]
        own_names = {_norm_name(n) for n in names.get(row.subject_cik, [])} - {""}
        for other in others.itertuples():
            other_names = {_norm_name(n) for n in names.get(other.cik, [])} - {""}
            if own_names & other_names or any(a.split()[:2] == b.split()[:2] and len(a.split()) >= 2
                                              for a in own_names for b in other_names):
                found[row.accession] = int(other.cik)
                break
    return found


# ------------------------------------------------------------------ build

def build(offline: bool = False) -> pd.DataFrame:
    hits, log = all_hits(offline=offline)
    rows = pd.DataFrame(filter_nasdaq_filer(hits))
    print(f"EFTS: {len(hits)} hits in {len(log)} slices, {len(rows)} Nasdaq filings", flush=True)
    from scripts.reversal_data_security_master import parallel_map

    def document(row):
        data = fetch_form25_doc(row.subject_cik, row.accession, row.document, offline=offline)
        return parse_form25_xml(data) if data else {"parse": "missing"}

    parsed = parallel_map(document, list(rows.itertuples()), label="documents")
    docs = pd.DataFrame(parsed).add_prefix("doc_")
    rows = pd.concat([rows.reset_index(drop=True), docs], axis=1)
    rows["subject_cik"] = [int(i) if pd.notna(i) and i else int(s) for i, s in zip(rows["doc_issuer_cik"], rows["subject_cik"])]
    rows["subject_name"] = [i if isinstance(i, str) and i else e for i, e in zip(rows["doc_issuer_name"], rows["subject_name_efts"])]
    rows["class_of_security"] = rows["doc_class_of_security"].fillna("")
    rows["rule_provision"] = rows["doc_rule_provision"].fillna("")
    rows["class_kind"] = rows["class_of_security"].map(classify_security_class)
    rows["delisting_basis"] = rows["rule_provision"].map(delisting_basis)
    rows["effective_date"] = [(date.fromisoformat(d) + timedelta(days=EFFECTIVE_LAG_DAYS)).isoformat() for d in rows["filing_date"]]
    rows["doc_url"] = [doc_url(c, a, d) for c, a, d in zip(rows["subject_cik"], rows["accession"], rows["document"])]

    floats = fetch_float_frames(*FLOAT_CONCEPT, frame_periods(), offline=offline)
    shares = fetch_float_frames(*SHARES_CONCEPT, frame_periods(), offline=offline)
    print(f"frames: {len(floats)} float facts, {len(shares)} shares facts", flush=True)
    float_by_cik = {c: g for c, g in floats.groupby("cik")}
    shares_by_cik = {c: g for c, g in shares.groupby("cik")}
    empty = floats.iloc[:0]
    float_rows = pd.DataFrame([float_for_filing(float_by_cik.get(c, empty), shares_by_cik.get(c, empty), c, d)
                               for c, d in zip(rows["subject_cik"], rows["filing_date"])])
    rows = pd.concat([rows, float_rows], axis=1)

    from scripts.reversal_data_security_master import load_submissions, parse_submissions
    intervals = pd.read_csv(TICKER_INTERVALS, low_memory=False) if TICKER_INTERVALS.exists() else None
    if intervals is not None:  # Nasdaq snapshot intervals only; SEC's current-ticker rows carry no dates
        intervals = intervals[intervals["source"] != "sec_company_tickers_exchange"].copy()
        intervals["start"] = pd.to_datetime(intervals["start"], errors="coerce")
        intervals["end"] = pd.to_datetime(intervals["end"], errors="coerce")
        intervals["cik"] = intervals["cik"].astype(int)
    names, evidence, ended_by_cik = {}, [], {}
    common_rows = rows["class_kind"].isin(["common", "ads"])
    rows = rows.sort_values(["filing_date", "accession"]).reset_index(drop=True)
    common_rows = rows["class_kind"].isin(["common", "ads"])
    for row in rows.itertuples():
        payload = load_submissions(row.subject_cik, offline=True) if common_rows[row.Index] else None
        if payload:
            profile = parse_submissions(payload)
            names[row.subject_cik] = [profile["name"], *[f["name"] for f in profile["former_names"]]]
        ev = subject_evidence(row.subject_cik, row.filing_date, payload)
        if common_rows[row.Index]:
            done = {t for t, day in ended_by_cik.get(row.subject_cik, {}).items()
                    if pd.Timestamp(row.filing_date) - pd.Timestamp(day) <= pd.Timedelta(days=60)}
            before, ended, began = nasdaq_tickers_around(intervals, row.subject_cik, row.filing_date, done)
            ev.update(tickers_before=" ".join(before), tickers_ended=" ".join(ended), tickers_new=" ".join(began))
            ended_by_cik.setdefault(row.subject_cik, {}).update({t: row.filing_date for t in ended})
        evidence.append(ev)
    if intervals is not None:
        for cik in intervals["cik"].unique():
            if cik not in names:
                payload = load_submissions(int(cik), offline=True)
                if payload:
                    profile = parse_submissions(payload)
                    names[int(cik)] = [profile["name"], *[f["name"] for f in profile["former_names"]]]
    successors = successor_evidence(rows[common_rows], intervals, names)
    out = []
    for row, ev in zip(rows.itertuples(), evidence):
        if row.accession in successors:
            ev = {**ev, "successor_same_ticker": successors[row.accession]}
        out.append(classify_row(row.class_kind, row.delisting_basis, ev))
    rows["classification"] = [c for c, _ in out]
    rows["classification_evidence"] = [e for _, e in out]
    rows = rows.sort_values(["filing_date", "accession"]).reset_index(drop=True)
    table = rows[PLAN_COLUMNS + EXTRA_COLUMNS]
    common.atomic_write(OUTPUT, table.to_csv(index=False).encode("utf-8"))
    summary = {
        "slices": log, "hits": len(hits), "nasdaq_filings": len(rows),
        "documents_missing": int((rows["doc_parse"] == "missing").sum()),
        "documents_regex_parsed": int((rows["doc_parse"] == "regex").sum()),
        "exchange_cik_mismatch": int(((rows["doc_exchange_cik"].notna()) & (rows["doc_exchange_cik"] != NASDAQ_CIK)).sum()),
        "accession_prefix_not_nasdaq": int((~rows["accession_prefix_is_filer"]).sum()),
        "multi_subject_filings": int((rows["n_subject_ciks"] > 1).sum()),
        "forms": dict(Counter(rows["form"])), "class_kind": dict(Counter(rows["class_kind"])),
        "classification": dict(Counter(rows["classification"])),
        "delisting_basis": dict(Counter(rows["delisting_basis"])),
        "float_check_flag": dict(Counter(rows["float_check_flag"])),
        "unique_subject_ciks": int(rows["subject_cik"].nunique()),
        "by_year": dict(Counter(rows["filing_date"].str[:4])),
    }
    common.atomic_write(SEC_RAW / "derived" / "form25_build_summary.json", (json.dumps(summary, indent=2) + "\n").encode())
    print(json.dumps({k: v for k, v in summary.items() if k != "slices"}, indent=2), flush=True)
    return table


# ------------------------------------------------------------------ comparison with sue_lt_2020_2026

def compare_with_sue_lt(new: pd.DataFrame, old_path: Path = OLD_LIST, offline: bool = True) -> dict:
    """Row-level comparison of 2020-01-01 .. old max date, keyed on (filing date, subject CIK)."""
    old = pd.read_csv(old_path)
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
