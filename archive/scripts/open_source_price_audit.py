"""Audit public GitHub price-data candidates without importing their data.

This is deliberately a research-only audit.  A public repository, an open
source scraper, and an OSI code licence do not grant permission to copy the
underlying market data into the formal price cache.  The output records the
observable evidence needed to decide whether a candidate deserves a separate
manual/licensing review; it never writes a formal price CSV.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOSITORIES = (
    "ARKMD/stooq",
    "Acelogic/WayBackMachineStockScraper",
    "ishchat/US-Stocks-ETF-Stooq-Data",
)
DEFAULT_GAP_FILE = PROJECT_ROOT / "output/historical_pit_gap_priorities.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "output/data_provenance/open_source_price_audit.json"
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "quant-stocks-research-audit"}


def _get_json(url: str) -> tuple[object | None, str | None]:
    try:
        with urlopen(Request(url, headers=HEADERS), timeout=30) as response:
            return json.load(response), None
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        return None, f"{type(error).__name__}: {error}"


def _get_text(url: str) -> tuple[str | None, str | None]:
    try:
        with urlopen(Request(url, headers={**HEADERS, "Accept": "text/plain"}), timeout=30) as response:
            return response.read().decode("utf-8", errors="replace"), None
    except (HTTPError, URLError, TimeoutError) as error:
        return None, f"{type(error).__name__}: {error}"


def _gap_tickers(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            str(row.get("ticker") or "").strip().upper()
            for row in csv.DictReader(handle)
            if str(row.get("ticker") or "").strip()
        }


def _extract_ticker_list(source: str | None) -> list[str]:
    if not source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "NDX_DELISTED_TICKERS" for target in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            return []
        if isinstance(value, list):
            return sorted({str(item).upper() for item in value})
    return []


def _wayback_probe(tickers: list[str]) -> list[dict]:
    """Check archived Yahoo historical-page evidence, without downloading prices."""
    observations = []
    for ticker in tickers[:20]:
        query_url = (
            "https://web.archive.org/cdx/search/cdx?"
            f"url={quote(f'finance.yahoo.com/q/hp?s={ticker}', safe='')}&"
            "output=json&filter=statuscode:200&"
            "fl=timestamp,original,statuscode,mimetype,length&collapse=digest"
        )
        payload, error = _get_json(query_url)
        rows = payload[1:] if isinstance(payload, list) and payload else []
        timestamps = [str(row[0]) for row in rows if isinstance(row, list) and row]
        observations.append({
            "ticker": ticker,
            "endpoint": "finance.yahoo.com/q/hp",
            "snapshot_count": len(rows),
            "earliest_snapshot": min(timestamps) if timestamps else None,
            "latest_snapshot": max(timestamps) if timestamps else None,
            "error": error,
            "price_rows_downloaded": False,
        })
    return observations


def audit_repository(repository: str, gap_tickers: set[str]) -> dict:
    metadata, metadata_error = _get_json(f"https://api.github.com/repos/{repository}")
    if not isinstance(metadata, dict):
        return {"repository": repository, "metadata_error": metadata_error, "formal_import_eligible": False}
    default_branch = str(metadata.get("default_branch") or "main")
    raw_root = f"https://raw.githubusercontent.com/{repository}/{default_branch}"
    readme, readme_error = _get_text(f"{raw_root}/README.md")
    scraper, scraper_error = _get_text(f"{raw_root}/scraper.py")
    declared_license = metadata.get("license") or {}
    license_name = declared_license.get("spdx_id") or declared_license.get("name")
    if not license_name and readme and re.search(r"(?im)^#+\s*license|\blicense\s*\n", readme):
        license_name = "README_CLAIM_ONLY"
    tickers = _extract_ticker_list(scraper)
    keywords = (readme or "").lower()
    return {
        "repository": repository,
        "url": metadata.get("html_url"),
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "default_branch": default_branch,
        "latest_commit": metadata.get("pushed_at"),
        "repository_license": license_name,
        "license_api_value": metadata.get("license"),
        "readme_available": readme is not None,
        "readme_error": readme_error,
        "scraper_available": scraper is not None,
        "scraper_error": scraper_error,
        "price_data_claims": [
            phrase
            for phrase in (
                "delisted", "wayback", "historical", "stooq", "yahoo finance", "ohlcv", "corporate action"
            )
            if phrase in keywords
        ],
        "delisted_ticker_list": tickers,
        "gap_ticker_overlap": sorted(set(tickers) & gap_tickers),
        "has_pit_membership_or_availability_fields": False,
        "has_delisting_return_or_terminal_value_fields": False,
        "formal_import_eligible": False,
        "decision": (
            "research_only_license_or_market-data_rights_unverified"
            if not license_name or license_name == "README_CLAIM_ONLY"
            else "research_only_requires_market_data_rights_review"
        ),
    }


def audit(repositories: list[str], gap_file: Path) -> dict:
    gaps = _gap_tickers(gap_file)
    repository_results = [audit_repository(repository, gaps) for repository in repositories]
    wayback_tickers = sorted({
        ticker
        for item in repository_results
        if item["repository"] == "Acelogic/WayBackMachineStockScraper"
        for ticker in item.get("gap_ticker_overlap", [])
    })
    return {
        "schema_version": 1,
        "research_only": True,
        "formal_cache_untouched": True,
        "gap_file": str(gap_file.relative_to(PROJECT_ROOT)) if gap_file.is_relative_to(PROJECT_ROOT) else str(gap_file),
        "gap_ticker_count": len(gaps),
        "repositories": repository_results,
        "wayback_cdx_probe": _wayback_probe(wayback_tickers),
        "conclusion": (
            "No audited public repository supplies verified PIT membership, delisting returns, "
            "terminal values, and reusable market-data rights; candidates may only be used for "
            "targeted research comparison until separately licensed or otherwise cleared."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", action="append", dest="repositories", default=None)
    parser.add_argument("--gap-file", type=Path, default=DEFAULT_GAP_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = audit(args.repositories or list(DEFAULT_REPOSITORIES), args.gap_file)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "gap_ticker_count": result["gap_ticker_count"],
        "repositories": [
            {"repository": item["repository"], "license": item.get("repository_license"), "overlap": len(item.get("gap_ticker_overlap", []))}
            for item in result["repositories"]
        ],
        "formal_import_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
