#!/usr/bin/env python3
"""Freeze negative archive evidence for five stale 2019 universe signals."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess


OUTPUT_PATH = Path(
    "output/research_only/v14/universe_2019_stale_signal_negative_evidence.json"
)
FINAL_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_sohu_restated_quarters_recovered.json"
)
FINAL_AUDIT_SHA256 = (
    "70ae7458cdcf785d69942acc46782b99bca3fd70c4b67b0348c5f68e178295a1"
)
COMMONCRAWL_CATALOG_PATH = Path(
    "output/research_only/v14/commoncrawl_nasdaq_listings_catalog.json"
)
COMMONCRAWL_CATALOG_SHA256 = (
    "6dca025e6e3f7555016c8554706357d3865a04980117fbdea7fc51d460e85ee7"
)
GITHUB_CATALOG_PATH = Path(
    "output/research_only/v14/github_nasdaq_listings_catalog.json"
)
GITHUB_CATALOG_SHA256 = (
    "8ad5f951c347907874f4f64c20843b28a663e1962a36efcd6bdf092f200d8628"
)
PINNED_GAP_MANIFEST_PATH = Path(
    "output/research_only/v14/universe_snapshots/"
    "github_pinned_gap_recovery_manifest.json"
)
PINNED_GAP_MANIFEST_SHA256 = (
    "891672888ce97dcbc3276493679d70c53a7d254134e68663183c5ec8a65540a4"
)

STALE_SIGNAL_DATES = (
    "2019-03-29",
    "2019-04-30",
    "2019-05-31",
    "2019-08-30",
    "2019-09-30",
)
TARGET_FILE_NAMES = ("nasdaqlisted.txt", "nasdaqtraded.txt")
EXPECTED_2019_GITHUB_CATALOG_DATES = (
    "2019-01-09",
    "2019-02-22",
    "2019-10-02",
    "2019-11-06",
    "2019-12-27",
    "2019-12-31",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exact_search_queries() -> list[dict]:
    rows = []
    for signal_date in STALE_SIGNAL_DATES:
        stamp = datetime.strptime(signal_date, "%Y-%m-%d").strftime("%m%d%Y")
        for file_name in TARGET_FILE_NAMES:
            rows.append({
                "signal_date": signal_date,
                "file_name": file_name,
                "stamp": stamp,
                "query": f'"File Creation Time: {stamp}" filename:{file_name}',
            })
    return rows


def _github_search(query: str) -> dict:
    result = subprocess.run(
        [
            "gh", "api", "-X", "GET", "search/code",
            "-f", f"q={query}", "-f", "per_page=100",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    return {
        "total_count": int(payload["total_count"]),
        "items": [
            {
                "repository": item["repository"]["full_name"],
                "path": item["path"],
                "sha": item["sha"],
                "html_url": item["html_url"],
            }
            for item in payload.get("items", [])
        ],
    }


def validate_local_archive_evidence(
    *,
    final_audit_path: Path = FINAL_AUDIT_PATH,
    final_audit_sha256: str = FINAL_AUDIT_SHA256,
    commoncrawl_path: Path = COMMONCRAWL_CATALOG_PATH,
    commoncrawl_sha256: str = COMMONCRAWL_CATALOG_SHA256,
    github_path: Path = GITHUB_CATALOG_PATH,
    github_sha256: str = GITHUB_CATALOG_SHA256,
    pinned_path: Path = PINNED_GAP_MANIFEST_PATH,
    pinned_sha256: str = PINNED_GAP_MANIFEST_SHA256,
) -> dict:
    bindings = {
        "final_candidate_path_audit": (final_audit_path, final_audit_sha256),
        "commoncrawl_catalog": (commoncrawl_path, commoncrawl_sha256),
        "github_catalog": (github_path, github_sha256),
        "github_pinned_gap_manifest": (pinned_path, pinned_sha256),
    }
    for name, (path, expected_sha) in bindings.items():
        actual_sha = _sha256(path)
        if actual_sha != expected_sha:
            raise RuntimeError(f"{name} binding changed: {actual_sha}")

    audit = json.loads(final_audit_path.read_text(encoding="utf-8"))
    price_audit = audit["price_audit"]
    if tuple(price_audit["stale_signal_snapshot_dates"]) != STALE_SIGNAL_DATES:
        raise RuntimeError("stale signal-date set changed")
    if int(price_audit["maximum_signal_snapshot_age_days"]) != 30:
        raise RuntimeError("maximum universe snapshot age policy changed")
    if int(price_audit["maximum_observed_signal_snapshot_age_days"]) != 98:
        raise RuntimeError("observed stale universe age changed")

    commoncrawl = json.loads(commoncrawl_path.read_text(encoding="utf-8"))
    captures = commoncrawl["captures"]
    if commoncrawl["requested_years"] != [2015, 2020]:
        raise RuntimeError("Common Crawl requested-year envelope changed")
    if int(commoncrawl["collection_count"]) != 64:
        raise RuntimeError("Common Crawl catalog coverage changed")
    if len(commoncrawl["checked_indexes"]) != 64 or commoncrawl["errors"]:
        raise RuntimeError("Common Crawl catalog is incomplete")
    captures_2019 = [
        row for row in captures if str(row["observed_at"]).startswith("2019-")
    ]
    if len(captures) != 36 or captures_2019:
        raise RuntimeError("Common Crawl 2019 negative result changed")

    github = json.loads(github_path.read_text(encoding="utf-8"))
    records = github["records"]
    if github["query"] != '"File Creation Time:" filename:nasdaqlisted.txt':
        raise RuntimeError("GitHub broad catalog query changed")
    if int(github["search_result_count"]) != 153 or len(records) != 122:
        raise RuntimeError("GitHub broad catalog coverage changed")
    if len(github["errors"]) != 1 or "missing Nasdaq header" not in github[
        "errors"
    ][0]["error"]:
        raise RuntimeError("GitHub catalog error classification changed")
    github_2019_dates = tuple(
        row["observed_at"]
        for row in records
        if str(row["observed_at"]).startswith("2019-")
    )
    if github_2019_dates != EXPECTED_2019_GITHUB_CATALOG_DATES:
        raise RuntimeError("GitHub 2019 broad catalog dates changed")

    pinned = json.loads(pinned_path.read_text(encoding="utf-8"))
    snapshot = pinned["snapshot"]
    if snapshot["observed_at"] != "2019-07-15" or int(snapshot["rows"]) != 3462:
        raise RuntimeError("pinned 2019-07-15 recovery changed")
    return {
        name: {"path": str(path), "sha256": expected_sha}
        for name, (path, expected_sha) in bindings.items()
    } | {
        "commoncrawl": {
            "checked_index_count": 64,
            "capture_count": 36,
            "capture_2019_count": 0,
            "error_count": 0,
        },
        "github_broad_catalog": {
            "search_result_count": 153,
            "valid_record_count": 122,
            "invalid_non_snapshot_count": 1,
            "record_2019_dates": list(github_2019_dates),
        },
        "nearest_additional_pinned_snapshot": {
            "observed_at": snapshot["observed_at"],
            "rows": int(snapshot["rows"]),
            "sha256": snapshot["sha256"],
        },
        "snapshot_policy": {
            "maximum_age_days": 30,
            "maximum_observed_age_days": 98,
            "stale_signal_dates": list(STALE_SIGNAL_DATES),
        },
    }


def classify_exact_searches(search_rows: list[dict]) -> dict:
    expected = {(row["signal_date"], row["file_name"]) for row in exact_search_queries()}
    observed = {(row["signal_date"], row["file_name"]) for row in search_rows}
    if observed != expected or len(search_rows) != len(expected):
        raise RuntimeError("exact GitHub search envelope is incomplete")
    hits = [row for row in search_rows if int(row["total_count"]) != 0]
    if hits:
        raise RuntimeError(f"exact GitHub archive candidates require review: {hits}")
    return {
        "query_count": len(search_rows),
        "zero_result_count": len(search_rows),
        "hit_count": 0,
        "status": "NO_EXACT_DATE_CAPTURE_FOUND",
    }


def build(
    output_path: Path = OUTPUT_PATH,
    *,
    search=_github_search,
    local_evidence_kwargs: dict | None = None,
    checked_at: str | None = None,
) -> dict:
    local = validate_local_archive_evidence(**(local_evidence_kwargs or {}))
    search_rows = []
    for request in exact_search_queries():
        result = search(request["query"])
        search_rows.append({**request, **result})
    exact = classify_exact_searches(search_rows)
    report = {
        "schema_version": 1,
        "research_only": True,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_universe_modified": False,
        "checked_at": checked_at or datetime.now(timezone.utc).isoformat(),
        "stale_signal_dates": list(STALE_SIGNAL_DATES),
        "local_archive_evidence": local,
        "exact_github_searches": search_rows,
        "exact_search_summary": exact,
        "classification": "SOURCE_EXHAUSTED_EXCLUDE_SIGNAL_DATES",
        "execution_policy": {
            "excluded_signal_dates": list(STALE_SIGNAL_DATES),
            "carry_prior_holdings_forward": True,
            "backdate_later_snapshot": False,
            "raise_maximum_snapshot_age": False,
            "fabricate_membership": False,
        },
        "interpretation": (
            "The exact Nasdaq membership for these five signals is not proven. "
            "All 64 Common Crawl indexes covering 2015-2020, the broad pinned "
            "GitHub catalog, the additional 2019-07-15 pinned snapshot, and ten "
            "exact date/file queries found no admissible capture. The five "
            "signals must be skipped while prior holdings continue; later "
            "snapshots may not be projected backward."
        ),
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["output"] = {
        "path": str(output_path), "sha256": _sha256(output_path)
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = build(args.output)
    print(json.dumps({
        "classification": report["classification"],
        "excluded_signal_dates": report["execution_policy"][
            "excluded_signal_dates"
        ],
        "output": report["output"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
