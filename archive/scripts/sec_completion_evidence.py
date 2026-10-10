"""Cache research-only SEC completion-filing evidence for PIT price leads.

This tool is intentionally narrow.  It caches the exact bytes of reviewed
SEC 8-K filings and emits a review package that links those filings to the
existing research-only price-triage report.  It does *not* update prices,
terminal returns, security identities, annual/quarterly fundamentals,
coverage reports, or validation artifacts.

Use ``--refresh`` to retrieve and atomically cache the SEC HTML once.  Without
it, the tool is fully offline and verifies the raw-byte SHA-256 before replay.
The output remains a lead for human review; it is not permission to make a
formal-data change.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html
import json
import os
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from src.io.financial_update import SEC_HEADERS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = Path("output/data_provenance/sec_completion_filing_cache")
DEFAULT_PRICE_TRIAGE = Path("output/data_provenance/stockanalysis_price_triage.json")
DEFAULT_OUTPUT = Path("output/data_provenance/sec_completion_evidence.json")

# These are deliberately a small, reviewed queue, rather than an inferred
# corporate-action feed.  The facts below identify a source document; the
# extracted markers merely show that its raw cached content contains the terms
# requiring a later formal review.
FILING_SPECS: dict[str, dict[str, Any]] = {
    "AVDX": {
        "ticker": "AVDX",
        "cik": 1858257,
        "form": "8-K",
        "filing_date": "2025-10-15",
        "accession": "0001193125-25-239635",
        "primary_document": "d49921d8k.htm",
        "source_url": (
            "https://www.sec.gov/Archives/edgar/data/1858257/"
            "000119312525239635/d49921d8k.htm"
        ),
        "event_scope": "cash-consideration completion candidate",
        "expected_markers": ("completed", "$10.00"),
        "required_before_formal_use": (
            "Independently review the completion/effective date and the final "
            "tradable session against a licensed PIT price source.",
            "Reconcile the per-share cash consideration and all eligibility "
            "conditions before any terminal-return change.",
            "Obtain explicit authorization and run the formal-data impact audit "
            "before modifying any released input or validation artifact.",
        ),
    },
    "PPBI": {
        "ticker": "PPBI",
        "cik": 1028918,
        "form": "8-K",
        "filing_date": "2025-09-02",
        "accession": "0001193125-25-193403",
        "primary_document": "d43060d8k.htm",
        "source_url": (
            "https://www.sec.gov/Archives/edgar/data/1028918/"
            "000119312525193403/d43060d8k.htm"
        ),
        "event_scope": "stock-consideration / identity-continuity candidate",
        "expected_markers": ("completed", "0.9150", "suspend"),
        "required_before_formal_use": (
            "Independently review the completion/effective date and the final "
            "tradable session against a licensed PIT price source.",
            "Establish a sourced security-identity and exchange-ratio treatment; "
            "a stock conversion must not be represented as a cash terminal return.",
            "Obtain explicit authorization and run the formal-data impact audit "
            "before modifying any released input or validation artifact.",
        ),
    },
    "APLS": {
        "ticker": "APLS",
        "cik": 1492422,
        "form": "8-K",
        "filing_date": "2026-05-14",
        "accession": "0001193125-26-222923",
        "primary_document": "d23709d8k.htm",
        "source_url": (
            "https://www.sec.gov/Archives/edgar/data/1492422/"
            "000119312526222923/d23709d8k.htm"
        ),
        "event_scope": (
            "cash-consideration-plus-contingent-value-right completion "
            "candidate"
        ),
        "expected_markers": ("May 14, 2026", "$41.00", "contingent value right"),
        "required_before_formal_use": (
            "Independently review the completion/effective date and the final "
            "tradable session against a licensed PIT price source.",
            "Treat the $41.00 cash amount and non-transferable CVR separately; "
            "do not assume a CVR value or include it in a terminal return "
            "without sourced valuation evidence.",
            "Obtain explicit authorization and run the formal-data impact audit "
            "before modifying any released input or validation artifact.",
        ),
    },
    "CPRX": {
        "ticker": "CPRX",
        "cik": 1369568,
        "form": "8-K",
        "filing_date": "2026-07-16",
        "accession": "0001193125-26-304984",
        "primary_document": "d159184d8k.htm",
        "source_url": (
            "https://www.sec.gov/Archives/edgar/data/1369568/"
            "000119312526304984/d159184d8k.htm"
        ),
        "event_scope": "cash-consideration completion candidate",
        "expected_markers": ("July 15, 2026", "$31.50", "suspended"),
        "required_before_formal_use": (
            "Independently review the completion/effective date and the final "
            "tradable session against a licensed PIT price source.",
            "Reconcile the $31.50 per-share cash consideration and all "
            "eligibility conditions before any terminal-return change.",
            "Obtain explicit authorization and run the formal-data impact audit "
            "before modifying any released input or validation artifact.",
        ),
    },
    "COMM": {
        "ticker": "COMM",
        "cik": 1517228,
        "form": "8-K",
        "filing_date": "2026-01-15",
        "accession": "0001193125-26-014078",
        "primary_document": "d749492d8k.htm",
        "source_url": (
            "https://www.sec.gov/Archives/edgar/data/1517228/"
            "000119312526014078/d749492d8k.htm"
        ),
        "event_scope": "same-issuer ticker-transition candidate (COMM to VISN)",
        "expected_markers": ("January 14, 2026", "VISN", "continue to trade"),
        "required_before_formal_use": (
            "Independently review the January 14, 2026 ticker transition and "
            "continuous COMM/VISN price coverage against a licensed PIT source.",
            "Establish a sourced same-issuer identity transition; a ticker "
            "migration must not be represented as a cash terminal return.",
            "Obtain explicit authorization and run the formal-data impact audit "
            "before modifying any released input or validation artifact.",
        ),
    },
}


def _normalized_tickers(tickers: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            str(ticker).upper().strip() for ticker in tickers if str(ticker).strip()
        )
    )


def _cache_path(cache_dir: str | Path, spec: dict[str, Any]) -> Path:
    return Path(cache_dir) / (
        f"{spec['ticker']}-{str(spec['accession']).replace('-', '')}.json.gz"
    )


def _payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _portable_project_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(resolved)


def fetch_sec_filing(spec: dict[str, Any], timeout: int = 30) -> bytes:
    """Fetch one SEC filing without parsing or transforming the raw response."""
    request = Request(str(spec["source_url"]), headers=SEC_HEADERS)
    with urlopen(request, timeout=timeout) as response:
        payload = response.read()
    if not payload:
        raise ValueError(f"{spec['ticker']}: SEC filing response is empty")
    return payload


def _write_cached_filing(
    cache_dir: str | Path,
    spec: dict[str, Any],
    payload: bytes,
) -> dict[str, Any]:
    if not isinstance(payload, bytes):
        raise TypeError(f"{spec['ticker']}: SEC filing payload must be bytes")
    path = _cache_path(cache_dir, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "format_version": 1,
        "ticker": spec["ticker"],
        "cik": int(spec["cik"]),
        "form": spec["form"],
        "filing_date": spec["filing_date"],
        "accession": spec["accession"],
        "primary_document": spec["primary_document"],
        "source_url": spec["source_url"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "payload_sha256": _payload_sha256(payload),
        "payload_base64": base64.b64encode(payload).decode("ascii"),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False, sort_keys=True)
    os.replace(temporary, path)
    return envelope


def _read_cached_filing(
    cache_dir: str | Path,
    spec: dict[str, Any],
) -> tuple[dict[str, Any], bytes]:
    path = _cache_path(cache_dir, spec)
    if not path.exists():
        raise FileNotFoundError(f"missing cached SEC filing evidence: {path}")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    required = {
        "format_version",
        "ticker",
        "cik",
        "form",
        "filing_date",
        "accession",
        "primary_document",
        "source_url",
        "fetched_at",
        "payload_sha256",
        "payload_base64",
    }
    missing = required - set(envelope)
    if missing:
        raise ValueError(
            f"{spec['ticker']}: cached SEC filing envelope missing {sorted(missing)}"
        )
    for key in (
        "ticker",
        "cik",
        "form",
        "filing_date",
        "accession",
        "primary_document",
        "source_url",
    ):
        expected = int(spec[key]) if key == "cik" else spec[key]
        actual = int(envelope[key]) if key == "cik" else envelope[key]
        if actual != expected:
            raise ValueError(
                f"{spec['ticker']}: cached SEC filing {key} does not match source spec"
            )
    try:
        payload = base64.b64decode(envelope["payload_base64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{spec['ticker']}: cached SEC filing payload is not valid base64"
        ) from exc
    actual_sha = _payload_sha256(payload)
    if actual_sha != envelope["payload_sha256"]:
        raise ValueError(f"{spec['ticker']}: cached SEC filing payload hash mismatch")
    return envelope, payload


def _load_filing(
    cache_dir: str | Path,
    spec: dict[str, Any],
    *,
    refresh: bool,
    fetcher: Callable[[dict[str, Any]], bytes] = fetch_sec_filing,
) -> tuple[dict[str, Any], bytes]:
    if refresh:
        _write_cached_filing(cache_dir, spec, fetcher(spec))
    return _read_cached_filing(cache_dir, spec)


def _html_text(payload: bytes) -> str:
    decoded = payload.decode("utf-8", errors="replace")
    without_nontext = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>",
        " ",
        decoded,
        flags=re.IGNORECASE | re.DOTALL,
    )
    without_tags = re.sub(r"<[^>]+>", " ", without_nontext)
    normalized = unicodedata.normalize("NFKC", html.unescape(without_tags))
    return re.sub(r"\s+", " ", normalized).strip()


def _marker_evidence(text: str, markers: tuple[str, ...]) -> list[dict[str, Any]]:
    evidence = []
    for marker in markers:
        match = re.search(re.escape(marker), text, flags=re.IGNORECASE)
        evidence.append(
            {
                "marker": marker,
                "found": match is not None,
                "snippet": (
                    text[max(0, match.start() - 160) : match.end() + 240]
                    if match is not None
                    else None
                ),
            }
        )
    return evidence


def _price_triage_link(ticker: str, price_triage_path: str | Path) -> dict[str, Any]:
    path = Path(price_triage_path)
    if not path.exists():
        return {
            "status": "PRICE_TRIAGE_REPORT_NOT_FOUND",
            "report_path": _portable_project_path(path),
        }
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "PRICE_TRIAGE_REPORT_UNREADABLE",
            "report_path": _portable_project_path(path),
            "error": str(exc),
        }
    record = next(
        (
            value
            for value in report.get("records", [])
            if str(value.get("ticker", "")).upper() == ticker
        ),
        None,
    )
    if record is None:
        return {
            "status": "PRICE_TRIAGE_RECORD_NOT_FOUND",
            "report_path": _portable_project_path(path),
        }
    return {
        "status": "LINKED_RESEARCH_LEAD_ONLY",
        "report_path": _portable_project_path(path),
        "assessment": record.get("assessment"),
        "cache_path": record.get("cache_path"),
        "cache_payload_sha256": record.get("cache_payload_sha256"),
    }


def build_completion_evidence(
    tickers: list[str],
    *,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    price_triage_path: str | Path = DEFAULT_PRICE_TRIAGE,
    refresh: bool = False,
    fetcher: Callable[[dict[str, Any]], bytes] = fetch_sec_filing,
) -> dict[str, Any]:
    """Build a raw-source-backed review package without formal-data mutation."""
    normalized_tickers = _normalized_tickers(tickers)
    unknown = sorted(set(normalized_tickers) - set(FILING_SPECS))
    if unknown:
        raise ValueError(f"no reviewed SEC completion filing spec for {unknown}")

    records = []
    for ticker in normalized_tickers:
        spec = FILING_SPECS[ticker]
        envelope, payload = _load_filing(
            cache_dir, spec, refresh=refresh, fetcher=fetcher
        )
        marker_evidence = _marker_evidence(
            _html_text(payload), tuple(spec["expected_markers"])
        )
        expected_markers_found = all(item["found"] for item in marker_evidence)
        records.append(
            {
                "ticker": ticker,
                "cik": spec["cik"],
                "form": spec["form"],
                "filing_date": spec["filing_date"],
                "accession": spec["accession"],
                "primary_document": spec["primary_document"],
                "source_url": spec["source_url"],
                "event_scope": spec["event_scope"],
                "research_only": True,
                "evidence_status": (
                    "RESEARCH_EVIDENCE_CACHED_REQUIRES_FORMAL_REVIEW"
                    if expected_markers_found
                    else "REVIEW_EXPECTED_FILING_TERMS_NOT_FOUND"
                ),
                "marker_evidence": marker_evidence,
                "raw_cache": {
                    "path": _portable_project_path(_cache_path(cache_dir, spec)),
                    "payload_sha256": envelope["payload_sha256"],
                    "fetched_at": envelope["fetched_at"],
                },
                "price_triage_link": _price_triage_link(
                    ticker, price_triage_path
                ),
                "required_before_formal_use": list(
                    spec["required_before_formal_use"]
                ),
                "formal_data_actions_performed": [],
            }
        )
    return {
        "format_version": 1,
        "mode": "refresh" if refresh else "offline_cache",
        "research_only": True,
        "formal_data_written": False,
        "purpose": (
            "Reproducibly cache exact SEC filing bytes and package completion "
            "leads for review; this is not terminal-return or identity evidence."
        ),
        "limitations": [
            "SEC filing text alone does not establish the final tradable session.",
            "A stock conversion is not a cash terminal return.",
            "No formal data may change without source-license review, explicit "
            "authorization, and an impact audit.",
        ],
        "requested_ticker_count": len(normalized_tickers),
        "records": records,
    }


def _write_report(path: str | Path, report: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tickers",
        default=",".join(FILING_SPECS),
        help="Comma-separated reviewed filing queue.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch and atomically cache exact SEC filing bytes.",
    )
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--price-triage", default=str(DEFAULT_PRICE_TRIAGE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    report = build_completion_evidence(
        args.tickers.split(","),
        cache_dir=args.cache_dir,
        price_triage_path=args.price_triage,
        refresh=args.refresh,
    )
    _write_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
