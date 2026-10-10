#!/usr/bin/env python3
"""Checkpointed Common Crawl catalog audit for historical Nasdaq listings."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CATALOG_URL = "https://index.commoncrawl.org/collinfo.json"
TARGET_URL = "www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
DEFAULT_OUTPUT = Path(
    "output/research_only/v14/commoncrawl_nasdaq_listings_catalog.json"
)
HEADERS = {"User-Agent": "quant-stocks-historical-universe-audit/1.0"}


def archive_source_url(record: dict) -> str:
    return (
        "https://data.commoncrawl.org/" + record["filename"]
        + f"#offset={record['offset']}&length={record['length']}"
        + f"&timestamp={record['timestamp']}"
    )


def _read_json(url: str):
    with urlopen(Request(url, headers=HEADERS), timeout=45) as response:
        return json.load(response)


def _read_lines(url: str) -> list[str]:
    try:
        with urlopen(Request(url, headers=HEADERS), timeout=45) as response:
            return response.read().decode("utf-8").splitlines()
    except HTTPError as exc:
        if exc.code == 404:
            return []
        raise


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def audit(
    *,
    start_year: int = 2015,
    end_year: int = 2020,
    output: Path = DEFAULT_OUTPUT,
    pause_seconds: float = 0.5,
) -> dict:
    if start_year > end_year:
        raise ValueError("start_year must not exceed end_year")
    previous = {}
    if output.is_file():
        previous = json.loads(output.read_text(encoding="utf-8"))
    checked = set(previous.get("checked_indexes", []))
    captures = {
        (row["timestamp"], row["digest"]): row
        for row in previous.get("captures", [])
    }
    errors = {
        row["index"]: row for row in previous.get("errors", [])
    }
    collections = _read_json(CATALOG_URL)
    selected = sorted(
        [
            item for item in collections
            if any(
                item["id"].startswith(f"CC-MAIN-{year}-")
                for year in range(start_year, end_year + 1)
            )
        ],
        key=lambda item: item["id"],
    )
    for item in selected:
        index = item["id"]
        if index in checked:
            continue
        query = item["cdx-api"] + "?" + urlencode({
            "url": TARGET_URL,
            "output": "json",
            "filter": "status:200",
        })
        try:
            lines = _read_lines(query)
            for line in lines:
                if not line.strip():
                    continue
                record = json.loads(line)
                record["index"] = index
                record["observed_at"] = (
                    record["timestamp"][:4] + "-" + record["timestamp"][4:6]
                    + "-" + record["timestamp"][6:8]
                )
                record["archive_source"] = archive_source_url(record)
                captures[(record["timestamp"], record["digest"])] = record
            checked.add(index)
            errors.pop(index, None)
        except Exception as exc:
            errors[index] = {"index": index, "error": repr(exc)}
        payload = {
            "schema_version": 1,
            "research_only": True,
            "target_url": TARGET_URL,
            "requested_years": [start_year, end_year],
            "collection_count": len(selected),
            "checked_indexes": sorted(checked),
            "captures": sorted(
                captures.values(), key=lambda row: row["timestamp"]
            ),
            "errors": sorted(errors.values(), key=lambda row: row["index"]),
            "imported": False,
            "formal_universe_modified": False,
            "release_status": "BLOCKED",
        }
        _atomic_json(output, payload)
        if pause_seconds:
            time.sleep(pause_seconds)
    return json.loads(output.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2015)
    parser.add_argument("--end-year", type=int, default=2020)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--pause-seconds", type=float, default=0.5)
    args = parser.parse_args()
    result = audit(
        start_year=args.start_year, end_year=args.end_year,
        output=args.output, pause_seconds=args.pause_seconds,
    )
    print(json.dumps({
        "output": str(args.output),
        "collection_count": result["collection_count"],
        "checked_index_count": len(result["checked_indexes"]),
        "capture_count": len(result["captures"]),
        "error_count": len(result["errors"]),
        "captures_by_year": {
            str(year): sum(
                row["observed_at"].startswith(str(year))
                for row in result["captures"]
            )
            for year in range(args.start_year, args.end_year + 1)
        },
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
