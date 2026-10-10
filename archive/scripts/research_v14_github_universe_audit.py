#!/usr/bin/env python3
"""Checkpoint pinned GitHub Nasdaq listing files for v14 gap recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_OUTPUT = Path(
    "output/research_only/v14/github_nasdaq_listings_catalog.json"
)
QUERY = '"File Creation Time:" filename:nasdaqlisted.txt'
HEADERS = {"User-Agent": "quant-stocks-historical-universe-audit/1.0"}


def raw_url(item: dict) -> str:
    match = re.fullmatch(
        r"https://github\.com/([^/]+/[^/]+)/blob/([^/]+)/(.*)",
        item["html_url"],
    )
    if match is None:
        raise ValueError(f"unrecognized GitHub blob URL: {item['html_url']}")
    return (
        f"https://raw.githubusercontent.com/{match.group(1)}/"
        f"{match.group(2)}/{quote(match.group(3), safe='/')}"
    )


def _gh_search(page: int) -> dict:
    result = subprocess.run(
        [
            "gh", "api", "-X", "GET", "search/code",
            "-f", f"q={QUERY}", "-f", "per_page=100", "-f", f"page={page}",
        ],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def _read(url: str) -> bytes:
    with urlopen(Request(url, headers=HEADERS), timeout=45) as response:
        return response.read()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def audit(
    *, output: Path = DEFAULT_OUTPUT, pause_seconds: float = 0.2
) -> dict:
    previous = (
        json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    )
    records = {row["blob_sha"]: row for row in previous.get("records", [])}
    errors = {row["blob_sha"]: row for row in previous.get("errors", [])}
    first = _gh_search(1)
    pages = (int(first["total_count"]) + 99) // 100
    items = list(first["items"])
    for page in range(2, pages + 1):
        items.extend(_gh_search(page)["items"])
    for item in items:
        blob_sha = item["sha"]
        if blob_sha in records:
            continue
        source = raw_url(item)
        try:
            payload = _read(source)
            text = payload.decode("utf-8-sig", errors="replace")
            footer = re.search(r"File Creation Time:\s*(\d{8})", text)
            header = text.find("Symbol|Security Name|")
            if footer is None or header < 0:
                raise ValueError("missing Nasdaq header or file creation time")
            observed_at = (
                footer.group(1)[4:8] + "-" + footer.group(1)[:2]
                + "-" + footer.group(1)[2:4]
            )
            records[blob_sha] = {
                "blob_sha": blob_sha,
                "repository": item["repository"]["full_name"],
                "path": item["path"],
                "observed_at": observed_at,
                "source": source,
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
            }
            errors.pop(blob_sha, None)
        except Exception as exc:
            errors[blob_sha] = {
                "blob_sha": blob_sha, "source": source, "error": repr(exc)
            }
        _atomic_json(output, {
            "schema_version": 1,
            "research_only": True,
            "query": QUERY,
            "search_result_count": len(items),
            "records": sorted(records.values(), key=lambda row: row["observed_at"]),
            "errors": sorted(errors.values(), key=lambda row: row["blob_sha"]),
            "formal_universe_modified": False,
            "release_status": "BLOCKED",
        })
        if pause_seconds:
            time.sleep(pause_seconds)
    return json.loads(output.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--pause-seconds", type=float, default=0.2)
    args = parser.parse_args()
    result = audit(output=args.output, pause_seconds=args.pause_seconds)
    years = {}
    for row in result["records"]:
        year = row["observed_at"][:4]
        years[year] = years.get(year, 0) + 1
    print(json.dumps({
        "output": str(args.output),
        "search_result_count": result["search_result_count"],
        "record_count": len(result["records"]),
        "error_count": len(result["errors"]),
        "records_by_year": years,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
