"""Research-only probe for historical ticker transitions behind price gaps.

For each high-priority missing-price ticker, query SEC full-text filing display
names to recover a CIK, then read that issuer's submissions metadata for later
tickers. This produces candidates only; it never changes identity or price
files. A candidate requires filing-level evidence before import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import PROJECT_PATH


HEADERS = {"User-Agent": "quant-stocks-research contact@example.com", "Accept": "application/json"}
CIK_PATTERN = re.compile(r"\(CIK\s*(?P<cik>\d{10})\)")
SNAPSHOT_DATE_PATTERN = re.compile(r"(20\d{2}-\d{2}-\d{2})")
SECURITY_NAME_SUFFIX = re.compile(
    r"\s+(?:-\s*)?(?:class\s+[a-z]+\s+)?(?:common stock|ordinary shares?|"
    r"american depositary shares?|ads(?:s)?|depositary shares?)"
    r"(?:\s*[,\-].*|\s+ex-.*)?$",
    re.I,
)


def _display_ticker_cik(display: str, ticker: str) -> str | None:
    """Return the CIK when an SEC display-name ticker list contains ticker."""
    normalized_display = str(display).upper()
    normalized_ticker = ticker.upper()
    ticker_in_parentheses = re.search(
        rf"\([^)]*(?<![A-Z0-9.\-]){re.escape(normalized_ticker)}(?![A-Z0-9.\-])[^)]*\)",
        normalized_display,
    )
    cik = CIK_PATTERN.search(normalized_display)
    return cik.group("cik") if ticker_in_parentheses and cik else None


def _normalized_company_name(value: str) -> str:
    value = re.sub(r"\([^)]*\)", " ", str(value))
    tokens = re.findall(r"[a-z0-9]+", value.lower())
    aliases = {
        "co": "company", "corp": "corporation", "inc": "incorporated",
        "ltd": "limited",
    }
    return " ".join(aliases.get(token, token) for token in tokens)


def _display_exact_name_cik(display: str, company_name: str) -> str | None:
    """Accept an exact normalized issuer name when a sourced name was queried."""
    cik = CIK_PATTERN.search(str(display).upper())
    if cik is None:
        return None
    display_name = str(display).split("(", 1)[0]
    return (
        cik.group("cik")
        if _normalized_company_name(display_name)
        == _normalized_company_name(company_name)
        else None
    )


def _get_json(url: str) -> tuple[dict | list, str]:
    request = Request(url, headers=HEADERS)
    with urlopen(request, timeout=10) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _missing_cik_tickers(triage_path: str | Path) -> list[str]:
    payload = json.loads(Path(triage_path).read_text(encoding="utf-8"))
    return sorted({
        str(row.get("ticker", "")).upper().strip()
        for row in payload.get("records", [])
        if row.get("status") == "MISSING_CIK_MAPPING"
        and str(row.get("ticker", "")).strip()
    })


def _snapshot_name_queries(
    snapshot_dir: str | Path, tickers: list[str]
) -> tuple[dict[str, str], dict[str, dict]]:
    wanted = set(tickers)
    latest: dict[str, tuple[str, str, str]] = {}
    for path in sorted(Path(snapshot_dir).glob("*.csv")):
        match = SNAPSHOT_DATE_PATTERN.search(path.name)
        observed = match.group(1) if match else ""
        try:
            frame = pd.read_csv(path, usecols=["Symbol", "Name"], dtype=str)
        except (ValueError, pd.errors.EmptyDataError):
            continue
        for row in frame.itertuples(index=False):
            ticker = str(row.Symbol).upper().strip()
            name = str(row.Name).strip()
            if ticker not in wanted or not name or name.lower() == "nan":
                continue
            candidate = (observed, path.as_posix(), name)
            if ticker not in latest or candidate[:2] > latest[ticker][:2]:
                latest[ticker] = candidate
    queries = {}
    sources = {}
    for ticker, (observed, path, raw_name) in latest.items():
        query = SECURITY_NAME_SUFFIX.sub("", raw_name).strip(" ,-.")
        if not query:
            continue
        queries[ticker] = query
        sources[ticker] = {
            "raw_name": raw_name,
            "snapshot_date": observed or None,
            "snapshot_path": path,
        }
    return queries, sources


def _search_ticker(ticker: str, search_query: str | None = None) -> dict:
    normalized = ticker.upper().strip()
    query_text = (search_query or normalized).strip()
    query = urlencode({"q": query_text, "from": 0, "size": 100})
    url = f"https://efts.sec.gov/LATEST/search-index?{query}"
    try:
        payload, search_payload_sha256 = _get_json(url)
        matches = []
        for hit in ((payload.get("hits") or {}).get("hits") or []):
            source = hit.get("_source") or {}
            for display in source.get("display_names") or []:
                cik = _display_ticker_cik(display, normalized)
                match_method = "ticker_in_sec_display"
                if cik is None and search_query:
                    cik = _display_exact_name_cik(display, query_text)
                    match_method = "exact_normalized_company_name"
                if cik:
                    matches.append({
                        "cik": cik, "display_name": display,
                        "match_method": match_method,
                        "filing_date": source.get("file_date"),
                        "form": source.get("form"), "accession": source.get("adsh"),
                    })
        fallback_searches = []
        if not matches and search_query:
            for form in ("10-K", "S-1", "F-1", "8-K"):
                fallback_url = "https://efts.sec.gov/LATEST/search-index?" + urlencode({
                    "q": f'"{query_text}"', "forms": form, "from": 0, "size": 100,
                })
                fallback_payload, fallback_sha = _get_json(fallback_url)
                fallback_searches.append({
                    "form": form,
                    "search_url": fallback_url,
                    "search_payload_sha256": fallback_sha,
                })
                for hit in ((fallback_payload.get("hits") or {}).get("hits") or []):
                    source = hit.get("_source") or {}
                    for display in source.get("display_names") or []:
                        cik = _display_ticker_cik(display, normalized)
                        match_method = "ticker_in_form_filtered_sec_display"
                        if cik is None:
                            cik = _display_exact_name_cik(display, query_text)
                            match_method = "exact_name_in_form_filtered_sec_display"
                        if cik:
                            matches.append({
                                "cik": cik,
                                "display_name": display,
                                "match_method": match_method,
                                "filing_date": source.get("file_date"),
                                "form": source.get("form"),
                                "accession": source.get("adsh"),
                            })
                if matches:
                    break
        dedup = {(row["cik"], row["display_name"]): row for row in matches}
        ciks = sorted(dedup)
        submissions = []
        for cik, display in ciks[:5]:
            submission_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            try:
                metadata, submission_payload_sha256 = _get_json(submission_url)
                submissions.append({
                    "cik": cik, "issuer_name": metadata.get("name"),
                    "current_tickers": metadata.get("tickers") or [],
                    "current_exchanges": metadata.get("exchanges") or [],
                    "former_names": metadata.get("former_names") or [],
                    "display_name": display,
                    "submission_url": submission_url,
                    "submission_payload_sha256": submission_payload_sha256,
                })
            except Exception as exc:
                submissions.append({
                    "cik": cik, "display_name": display,
                    "submission_url": submission_url, "metadata_error": repr(exc),
                })
            time.sleep(0.1)
        return {
            "ticker": normalized, "status": "ok", "search_url": url,
            "search_query": query_text,
            "search_payload_sha256": search_payload_sha256,
            "fallback_searches": fallback_searches,
            "matches": list(dedup.values()), "issuers": submissions,
        }
    except Exception as exc:
        return {
            "ticker": normalized, "status": "failed", "search_url": url,
            "search_query": query_text,
            "error": repr(exc),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gap-file", default="output/historical_pit_gap_priorities.csv")
    parser.add_argument("--tickers", help="Comma-separated override")
    parser.add_argument(
        "--triage-missing-cik",
        help="Use MISSING_CIK_MAPPING tickers from a SEC submissions triage JSON",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--search-queries-json",
        help="Optional JSON object mapping ticker to a company-name SEC search query",
    )
    parser.add_argument(
        "--snapshot-name-queries",
        help="Derive company-name queries from the latest matching Nasdaq snapshot",
    )
    parser.add_argument(
        "--restart", action="store_true",
        help="Discard a compatible partial checkpoint and query every ticker again",
    )
    parser.add_argument("--output", default=str(Path(PROJECT_PATH) / "output/data_provenance/sec_ticker_transition_probe.json"))
    args = parser.parse_args()
    if args.tickers:
        tickers = sorted({item.upper().strip() for item in args.tickers.split(",") if item.strip()})
    elif args.triage_missing_cik:
        tickers = _missing_cik_tickers(args.triage_missing_cik)
        tickers = tickers[args.offset : args.offset + args.limit]
    else:
        gap = pd.read_csv(args.gap_file)
        tickers = gap.loc[gap["absent_price_file_signal_count"].gt(0)].sort_values(
            ["recovery_priority_rank", "priority_rank"]
        )["ticker"].astype(str).str.upper().head(args.limit).tolist()
    search_queries = {}
    search_query_sources = {}
    search_queries_sha256 = None
    if args.snapshot_name_queries:
        search_queries, search_query_sources = _snapshot_name_queries(
            args.snapshot_name_queries, tickers
        )
    if args.search_queries_json:
        query_path = Path(args.search_queries_json)
        raw_queries = query_path.read_bytes()
        search_queries_sha256 = hashlib.sha256(raw_queries).hexdigest()
        search_queries.update({
            str(key).upper(): str(value)
            for key, value in json.loads(raw_queries).items()
        })
    if search_queries:
        search_queries_sha256 = hashlib.sha256(
            json.dumps(search_queries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    target = Path(args.output)
    prior = None
    if target.exists() and not args.restart:
        candidate = json.loads(target.read_text(encoding="utf-8"))
        if candidate.get("requested_tickers") == tickers:
            prior = candidate
    successful_prior = {
        row["ticker"]: row
        for row in ((prior or {}).get("results") or [])
        if row.get("status") == "ok" and row.get("ticker") in tickers
    }
    report = {
        "schema_version": 2,
        "research_only": True,
        "status": "IN_PROGRESS",
        "started_at_utc": (
            prior.get("started_at_utc")
            if prior and prior.get("started_at_utc")
            else datetime.now(timezone.utc).isoformat()
        ),
        "resumed_at_utc": (
            datetime.now(timezone.utc).isoformat() if prior else None
        ),
        "requested_tickers": tickers,
        "search_queries": {ticker: search_queries[ticker] for ticker in tickers if ticker in search_queries},
        "search_query_sources": {
            ticker: search_query_sources[ticker]
            for ticker in tickers if ticker in search_query_sources
        },
        "search_queries_sha256": search_queries_sha256,
        "completed_ticker_count": len(successful_prior),
        "results": sorted(successful_prior.values(), key=lambda item: item["ticker"]),
    }
    _atomic_write_json(target, report)
    results_by_ticker: dict[str, dict] = dict(successful_prior)
    pending_tickers = [ticker for ticker in tickers if ticker not in successful_prior]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_search_ticker, ticker, search_queries.get(ticker)): ticker
            for ticker in pending_tickers
        }
        for future in as_completed(futures):
            row = future.result()
            results_by_ticker[row["ticker"]] = row
            report["results"] = sorted(
                results_by_ticker.values(), key=lambda item: item["ticker"]
            )
            report["completed_ticker_count"] = len(results_by_ticker)
            report["last_checkpoint_ticker"] = row["ticker"]
            report["last_checkpoint_at_utc"] = datetime.now(timezone.utc).isoformat()
            _atomic_write_json(target, report)
    results = report["results"]
    report["status"] = "COMPLETE"
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(target, report)
    counts = pd.Series([row["status"] for row in results]).value_counts().to_dict()
    print(json.dumps({"counts": counts, "requested": len(tickers)}, indent=2))


if __name__ == "__main__":
    main()
