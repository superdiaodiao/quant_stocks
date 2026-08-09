"""Research-only SEC submissions triage for historical price-data gaps.

This tool deliberately does not alter price files, terminal returns, security
identity mappings, or validation artifacts.  It caches the exact SEC
submissions payload used for each review so the resulting evidence leads can
be regenerated offline before a human decides whether formal data should
change.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE, PROJECT_PATH
from src.io.financial_update import SEC_HEADERS


SEC_SUBMISSIONS_API = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EVENT_FORMS = {
    "6-K",
    "6-K/A",
    "8-K",
    "8-K/A",
    "DEFM14A",
    "PREM14A",
    "S-4",
    "S-4/A",
    "15-12B",
    "15-12G",
    "25-NSE",
}
TERMINATION_FORMS = {"15-12B", "15-12G"}

DEFAULT_SUMMARY = (
    Path(PROJECT_PATH) / "output/can_slim_fixed_top3_summary.json"
)
DEFAULT_TICKER_CIKS = (
    Path(PROJECT_PATH)
    / "cleaned_stocks_data/financial/sec_companyfacts_cache"
    / "historical_ticker_ciks.json"
)
DEFAULT_COMPANYFACTS_CACHE_DIR = (
    Path(PROJECT_PATH) / "cleaned_stocks_data/financial/sec_companyfacts_cache"
)
DEFAULT_CACHE_DIR = (
    Path(PROJECT_PATH) / "output/data_provenance/sec_submission_triage_cache"
)
DEFAULT_OUTPUT = (
    Path(PROJECT_PATH) / "output/data_provenance/sec_submission_triage.json"
)
DEFAULT_HISTORICAL_AUDIT = Path(PROJECT_PATH) / "output/historical_data_audit.json"


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _payload_sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _cache_path(cache_dir: str | Path, cik: int) -> Path:
    return Path(cache_dir) / f"CIK{int(cik):010d}.json.gz"


def _portable_project_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(Path(PROJECT_PATH).resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _submission_url(cik: int) -> str:
    return SEC_SUBMISSIONS_API.format(cik=int(cik))


def fetch_sec_submissions(cik: int, timeout: int = 30) -> dict:
    """Fetch one SEC submissions payload using the project fair-access header."""
    source_url = _submission_url(cik)
    request = Request(source_url, headers=SEC_HEADERS)
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError(f"CIK {cik}: SEC submissions payload is not an object")
    return payload


def _write_cached_submission(
    cache_dir: str | Path,
    cik: int,
    payload: dict,
) -> dict:
    path = _cache_path(cache_dir, cik)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "format_version": 1,
        "cik": int(cik),
        "source_url": _submission_url(cik),
        "fetched_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "payload_sha256": _payload_sha256(payload),
        "payload": payload,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False, sort_keys=True)
    os.replace(temporary, path)
    return envelope


def _read_cached_submission(cache_dir: str | Path, cik: int) -> dict:
    path = _cache_path(cache_dir, cik)
    if not path.exists():
        raise FileNotFoundError(f"missing cached SEC submissions payload: {path}")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    required = {
        "format_version",
        "cik",
        "source_url",
        "fetched_at",
        "payload_sha256",
        "payload",
    }
    missing = required - set(envelope)
    if missing:
        raise ValueError(
            f"CIK {cik}: cached submissions envelope missing {sorted(missing)}"
        )
    if int(envelope["cik"]) != int(cik):
        raise ValueError(f"CIK {cik}: cached submissions CIK does not match path")
    actual_sha = _payload_sha256(envelope["payload"])
    if actual_sha != envelope["payload_sha256"]:
        raise ValueError(f"CIK {cik}: cached submissions payload hash mismatch")
    return envelope


def _load_submission(
    cik: int,
    cache_dir: str | Path,
    *,
    refresh: bool,
    fetcher: Callable[[int], dict] = fetch_sec_submissions,
) -> dict:
    if refresh:
        return _write_cached_submission(cache_dir, cik, fetcher(cik))
    return _read_cached_submission(cache_dir, cik)


def load_historical_ticker_ciks(path: str | Path = DEFAULT_TICKER_CIKS) -> dict[str, dict]:
    """Load the sourced historical ticker→CIK registry used by Company Facts."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = payload.get("entries", payload)
    if not isinstance(entries, dict):
        raise ValueError("historical ticker CIK registry must contain an entries map")
    return {
        str(ticker).upper().strip(): value
        for ticker, value in entries.items()
        if isinstance(value, dict) and str(ticker).strip()
    }


def load_cached_sec_ticker_maps(
    cache_dir: str | Path = DEFAULT_COMPANYFACTS_CACHE_DIR,
) -> dict[str, dict]:
    """Load cached SEC ticker maps, excluding time-varying ticker conflicts.

    SEC publishes a current ticker map, so snapshots from different dates can
    legitimately disagree after a ticker is reused.  Such a ticker is not a
    safe historical identity claim and is omitted instead of making every
    unrelated ticker unusable.
    """
    ticker_map_dir = Path(cache_dir) / "ticker_maps"
    result: dict[str, dict] = {}
    ambiguous: set[str] = set()
    for path in sorted(ticker_map_dir.glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            mapping = json.load(handle)
        if not isinstance(mapping, dict):
            raise ValueError(f"cached SEC ticker map is not an object: {path}")
        for raw_ticker, raw_cik in mapping.items():
            ticker = str(raw_ticker).upper().strip()
            if not ticker or ticker in ambiguous:
                continue
            cik = int(raw_cik)
            existing = result.get(ticker)
            if existing is not None and int(existing["cik"]) != cik:
                result.pop(ticker, None)
                ambiguous.add(ticker)
                continue
            result[ticker] = {
                "cik": cik,
                "source_type": "cached_sec_ticker_map",
                "source_path": str(path),
            }
    return result


def load_probe_ticker_ciks(patterns: list[str]) -> dict[str, dict]:
    """Load only unique-CIK mappings from reproducible SEC probe reports."""
    candidates: dict[str, dict[int, set[str]]] = {}
    for pattern in patterns:
        paths = sorted(glob.glob(str(pattern)))
        if not paths:
            raise FileNotFoundError(f"SEC transition probe glob matched no files: {pattern}")
        for raw_path in paths:
            path = Path(raw_path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            for row in payload.get("results", []):
                ticker = str(row.get("ticker", "")).upper().strip()
                if not ticker or row.get("status") != "ok":
                    continue
                ciks = {
                    int(issuer["cik"])
                    for issuer in (row.get("issuers") or [])
                    if issuer.get("cik") is not None
                }
                if len(ciks) != 1:
                    continue
                cik = ciks.pop()
                candidates.setdefault(ticker, {}).setdefault(cik, set()).add(
                    _portable_project_path(path)
                )
    return {
        ticker: {
            "cik": next(iter(by_cik)),
            "source_type": "sec_transition_probe_unique_cik",
            "source_paths": sorted(next(iter(by_cik.values()))),
        }
        for ticker, by_cik in candidates.items()
        if len(by_cik) == 1
    }


def load_ticker_cik_registry(
    historical_path: str | Path = DEFAULT_TICKER_CIKS,
    companyfacts_cache_dir: str | Path = DEFAULT_COMPANYFACTS_CACHE_DIR,
    probe_globs: list[str] | None = None,
) -> dict[str, dict]:
    """Combine sourced historical aliases with exact cached current SEC maps."""
    registry = load_cached_sec_ticker_maps(companyfacts_cache_dir)
    if probe_globs:
        registry.update(load_probe_ticker_ciks(probe_globs))
    registry.update(load_historical_ticker_ciks(historical_path))
    return registry


def load_priority_tickers(summary_path: str | Path = DEFAULT_SUMMARY) -> list[str]:
    """Return the unresolved observable competitors, preserving report order."""
    summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    values = summary.get("historical_unresolved_observable_potential_competitors", [])
    return list(dict.fromkeys(
        str(ticker).upper().strip() for ticker in values if str(ticker).strip()
    ))


def load_unresolved_terminal_tickers(
    audit_path: str | Path = DEFAULT_HISTORICAL_AUDIT,
) -> list[str]:
    """Return the mature unresolved terminal-history queue from the audit."""
    payload = json.loads(Path(audit_path).read_text(encoding="utf-8"))
    rows = payload.get("unresolved_terminal_return_histories", [])
    return list(dict.fromkeys(
        str(row.get("ticker", "")).upper().strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("ticker", "")).strip()
    ))


def _last_local_price_date(ticker: str, price_dir: str | Path) -> pd.Timestamp | None:
    path = Path(price_dir) / f"{ticker.lower()}.csv"
    if not path.exists():
        return None
    dates = pd.to_datetime(
        pd.read_csv(path, usecols=["date"])["date"], errors="coerce"
    ).dropna()
    if dates.empty:
        return None
    return pd.Timestamp(dates.max()).normalize()


def _missing_nasdaq_sessions(
    last_local_price_date: pd.Timestamp | None,
    analysis_end: pd.Timestamp | None,
    benchmark_dates: pd.Series | pd.DatetimeIndex | None,
) -> int | None:
    if last_local_price_date is None or analysis_end is None:
        return None
    if benchmark_dates is None:
        benchmark_dates = pd.read_csv(
            NASDAQ_INDEX_FILE, usecols=["date"], parse_dates=["date"]
        )["date"]
    dates = pd.DatetimeIndex(pd.to_datetime(benchmark_dates).dropna()).normalize()
    return int(((dates > last_local_price_date) & (dates <= analysis_end)).sum())


def _filing_url(cik: int, accession: str, document: str) -> str:
    accession_number = str(accession).replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession_number}/{document}"
    )


def _event_filings(
    payload: dict,
    *,
    cik: int,
    after: pd.Timestamp | None,
) -> list[dict]:
    recent = (payload.get("filings") or {}).get("recent") or {}
    dates = recent.get("filingDate") or []
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    documents = recent.get("primaryDocument") or []
    results = []
    for filing_date, form, accession, document in zip(
        dates, forms, accessions, documents
    ):
        if form not in EVENT_FORMS:
            continue
        parsed_date = pd.Timestamp(filing_date).normalize()
        if after is not None and parsed_date <= after:
            continue
        results.append({
            "filing_date": parsed_date.strftime("%Y-%m-%d"),
            "form": form,
            "accession": accession,
            "primary_document": document,
            "source_url": _filing_url(cik, accession, document),
        })
    return sorted(results, key=lambda item: item["filing_date"])


def _resolution_review(
    ticker: str,
    current_tickers: list[str],
    events: list[dict],
    analysis_end: pd.Timestamp | None,
    last_local_price_date: pd.Timestamp | None,
) -> str:
    if any(event["form"] in TERMINATION_FORMS for event in events):
        if analysis_end is not None and any(
            pd.Timestamp(event["filing_date"]) > analysis_end
            and event["form"] in TERMINATION_FORMS
            for event in events
        ):
            return "PRICE_SOURCE_REVIEW_BEFORE_POST_END_TERMINATION"
        if last_local_price_date is not None:
            return "PRICE_SOURCE_AND_TERMINAL_RETURN_REVIEW"
        return "TERMINAL_RETURN_REVIEW"
    if current_tickers and ticker not in current_tickers:
        return "IDENTITY_TRANSITION_REVIEW"
    if not current_tickers and any(
        event["form"] in {"6-K", "6-K/A"} for event in events
    ):
        return "PRICE_SOURCE_AND_TERMINAL_RETURN_REVIEW"
    return "PRICE_SOURCE_REVIEW"


def _next_required_evidence(review: str) -> str:
    if review == "PRICE_SOURCE_AND_TERMINAL_RETURN_REVIEW":
        return (
            "Recover PIT prices through the final tradable session, then "
            "review the cited SEC completion filing and reconcile per-share "
            "consideration before any terminal-return change."
        )
    if review == "PRICE_SOURCE_REVIEW_BEFORE_POST_END_TERMINATION":
        return (
            "Recover PIT prices through the analysis end; the cited later "
            "termination cannot fill an earlier price-observability gap."
        )
    if review == "IDENTITY_TRANSITION_REVIEW":
        return (
            "Verify a sourced effective ticker-change date and PIT price "
            "continuity before any security-identity change."
        )
    if review == "TERMINAL_RETURN_REVIEW":
        return (
            "Review the cited SEC completion filing and reconcile per-share "
            "consideration before any terminal-return change."
        )
    return "Recover PIT prices from a reproducible source before formal data changes."


def triage_sec_submissions(
    tickers: list[str],
    *,
    ticker_ciks: dict[str, dict],
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    refresh_missing_only: bool = False,
    analysis_end: str | pd.Timestamp | None = None,
    benchmark_dates: pd.Series | pd.DatetimeIndex | None = None,
    fetcher: Callable[[int], dict] = fetch_sec_submissions,
) -> dict:
    """Create review leads from cached SEC submissions without changing formal data."""
    normalized_tickers = list(dict.fromkeys(
        str(ticker).upper().strip() for ticker in tickers if str(ticker).strip()
    ))
    parsed_end = (
        pd.Timestamp(analysis_end).normalize()
        if analysis_end is not None
        else None
    )
    rows = []
    for ticker in normalized_tickers:
        mapping = ticker_ciks.get(ticker)
        if not mapping or mapping.get("cik") is None:
            rows.append({
                "ticker": ticker,
                "status": "MISSING_CIK_MAPPING",
                "resolution_review": "CIK_MAPPING_REVIEW",
            })
            continue
        cik = int(mapping["cik"])
        last_price_date = _last_local_price_date(ticker, price_dir)
        missing_sessions = _missing_nasdaq_sessions(
            last_price_date, parsed_end, benchmark_dates
        )
        try:
            should_refresh = refresh or (
                refresh_missing_only and not _cache_path(cache_dir, cik).exists()
            )
            envelope = _load_submission(
                cik, cache_dir, refresh=should_refresh, fetcher=fetcher
            )
        except FileNotFoundError as exc:
            rows.append({
                "ticker": ticker,
                "cik": cik,
                "status": "CACHE_MISSING",
                "resolution_review": "SEC_SUBMISSIONS_CACHE_REVIEW",
                "error": str(exc),
            })
            continue
        except Exception as exc:
            rows.append({
                "ticker": ticker,
                "cik": cik,
                "status": "FETCH_OR_CACHE_ERROR",
                "resolution_review": "SEC_SUBMISSIONS_CACHE_REVIEW",
                "error": str(exc),
            })
            continue

        payload = envelope["payload"]
        current_tickers = sorted({
            str(value).upper().strip()
            for value in (payload.get("tickers") or [])
            if str(value).strip()
        })
        events = _event_filings(
            payload, cik=cik, after=last_price_date
        )
        review = _resolution_review(
            ticker,
            current_tickers,
            events,
            parsed_end,
            last_price_date,
        )
        rows.append({
            "ticker": ticker,
            "cik": cik,
            "company_name": payload.get("name"),
            "last_local_price_date": (
                last_price_date.strftime("%Y-%m-%d")
                if last_price_date is not None else None
            ),
            "missing_nasdaq_sessions_through_analysis_end": missing_sessions,
            "current_sec_tickers": current_tickers,
            "status": "RESEARCH_LEAD_ONLY",
            "resolution_review": review,
            "next_required_evidence": _next_required_evidence(review),
            "event_filings_after_local_price": events,
            "cache_path": _portable_project_path(_cache_path(cache_dir, cik)),
            "cache_payload_sha256": envelope["payload_sha256"],
            "cache_source_url": envelope["source_url"],
            "cache_fetched_at": envelope["fetched_at"],
        })
    counts = Counter(row["status"] for row in rows)
    return {
        "format_version": 1,
        "mode": (
            "refresh" if refresh else
            "refresh_missing_only" if refresh_missing_only else
            "offline_cache"
        ),
        "research_only": True,
        "analysis_end": (
            parsed_end.strftime("%Y-%m-%d") if parsed_end is not None else None
        ),
        "warning": (
            "This is a SEC filing lead queue, not terminal-return or ticker-"
            "identity evidence. It never updates formal prices, terminal "
            "returns, security identities, or validation artifacts."
        ),
        "requested_ticker_count": len(normalized_tickers),
        "counts": dict(sorted(counts.items())),
        "records": rows,
    }


def _write_report(path: str | Path, report: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tickers",
        help="Comma-separated tickers; defaults to the validation summary queue.",
    )
    parser.add_argument(
        "--audit-unresolved-terminal",
        nargs="?",
        const=str(DEFAULT_HISTORICAL_AUDIT),
        help=(
            "Use unresolved_terminal_return_histories from this audit JSON "
            f"(default path when flag has no value: {DEFAULT_HISTORICAL_AUDIT})."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch and atomically cache current SEC submissions payloads.",
    )
    parser.add_argument(
        "--refresh-missing-only",
        action="store_true",
        help="Fetch only CIK submissions not already present in the SHA cache.",
    )
    parser.add_argument("--summary", default=str(DEFAULT_SUMMARY))
    parser.add_argument("--ticker-ciks", default=str(DEFAULT_TICKER_CIKS))
    parser.add_argument(
        "--companyfacts-cache-dir", default=str(DEFAULT_COMPANYFACTS_CACHE_DIR)
    )
    parser.add_argument(
        "--probe-glob",
        action="append",
        default=[],
        help=(
            "Glob for SEC ticker-transition probe reports; repeatable. Only "
            "tickers resolving to exactly one CIK across all matched reports "
            "are admitted."
        ),
    )
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--analysis-end", default="2026-07-17")
    args = parser.parse_args()
    if args.tickers:
        tickers = args.tickers.split(",")
    elif args.audit_unresolved_terminal:
        tickers = load_unresolved_terminal_tickers(args.audit_unresolved_terminal)
    else:
        tickers = load_priority_tickers(args.summary)
    report = triage_sec_submissions(
        tickers,
        ticker_ciks=load_ticker_cik_registry(
            args.ticker_ciks, args.companyfacts_cache_dir, args.probe_glob
        ),
        price_dir=args.price_dir,
        cache_dir=args.cache_dir,
        refresh=args.refresh,
        refresh_missing_only=args.refresh_missing_only,
        analysis_end=args.analysis_end,
    )
    _write_report(args.output, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
