#!/usr/bin/env python3
"""Recover one SHA-locked Nasdaq Trader snapshot for the v14 research universe."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

from src.io.nasdaq_update import import_nasdaq_trader_files


DEFAULT_SNAPSHOT_DIR = Path("output/research_only/v14/universe_snapshots")
SOURCE_URL = (
    "https://raw.githubusercontent.com/Tonychen0227/StocksPython/"
    "46844218084c6d03fad878f1197778587ff167c0/nasdaqtraded.txt"
)
SOURCE_REPOSITORY = "Tonychen0227/StocksPython"
SOURCE_COMMIT = "46844218084c6d03fad878f1197778587ff167c0"
SOURCE_PATH = "nasdaqtraded.txt"
SOURCE_BLOB_SHA = "2b113f07b34152c3d3db7a4b7b625568d83ae486"
PAYLOAD_SHA256 = "4d303e124f355ecd005f7179df8b8ef1b5ae498dc76cf3bc355810bfb487b7e3"
OBSERVED_AT = "2019-07-15"
EXPECTED_HEADER = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
    "Round Lot Size|ETF|NextShares"
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download(source_url: str) -> bytes:
    request = Request(source_url, headers={"User-Agent": "quant-stocks-research/1.0"})
    with urlopen(request, timeout=60) as response:
        return response.read()


def verify_payload(
    payload: bytes,
    *,
    expected_sha256: str,
    observed_at: str,
    minimum_rows: int,
) -> dict:
    actual_sha256 = _sha256_bytes(payload)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Pinned Nasdaq payload SHA mismatch: {actual_sha256} != {expected_sha256}"
        )
    text = payload.decode("utf-8-sig", errors="strict")
    lines = text.splitlines()
    if not lines or lines[0].rstrip("\r") != EXPECTED_HEADER:
        raise ValueError("Pinned Nasdaq payload header mismatch")
    footer = re.search(r"File Creation Time:\s*(\d{8})", text)
    expected_footer_date = observed_at[5:7] + observed_at[8:10] + observed_at[:4]
    if footer is None or footer.group(1) != expected_footer_date:
        raise ValueError("Pinned Nasdaq payload observation date mismatch")
    data_rows = sum(
        1
        for line in lines[1:]
        if line.strip() and not line.startswith("File Creation Time:")
    )
    if data_rows < minimum_rows:
        raise ValueError(f"Pinned Nasdaq payload has only {data_rows} rows")
    return {
        "payload_sha256": actual_sha256,
        "observed_at": observed_at,
        "data_rows": data_rows,
        "line_count": len(lines),
        "header": lines[0].rstrip("\r"),
        "footer": next(
            line.rstrip("\r") for line in lines if line.startswith("File Creation Time:")
        ),
    }


def recover(
    *,
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    source_url: str = SOURCE_URL,
    expected_sha256: str = PAYLOAD_SHA256,
    observed_at: str = OBSERVED_AT,
    minimum_rows: int = 1000,
) -> dict:
    payload = _download(source_url)
    verification = verify_payload(
        payload,
        expected_sha256=expected_sha256,
        observed_at=observed_at,
        minimum_rows=minimum_rows,
    )
    imported = import_nasdaq_trader_files(
        [source_url], minimum_rows=minimum_rows, snapshot_dir=snapshot_dir
    )
    matching = [
        row for row in imported["imported"] if row.get("observed_at") == observed_at
    ]
    if len(matching) != 1:
        raise RuntimeError(
            f"Expected exactly one imported {observed_at} snapshot; got {matching!r}; "
            f"skipped={imported['skipped']!r}"
        )
    snapshot = Path(matching[0]["snapshot"])
    result = {
        "schema_version": 1,
        "research_only": True,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_universe_modified": False,
        "source": {
            "url": source_url,
            "repository": SOURCE_REPOSITORY,
            "commit": SOURCE_COMMIT,
            "path": SOURCE_PATH,
            "git_blob_sha": SOURCE_BLOB_SHA,
            **verification,
        },
        "snapshot": {
            **matching[0],
            "sha256": _sha256_path(snapshot),
        },
        "minimum_rows": minimum_rows,
    }
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest = snapshot_dir / "github_pinned_gap_recovery_manifest.json"
    manifest.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    result["manifest"] = str(manifest)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--minimum-rows", type=int, default=1000)
    args = parser.parse_args()
    result = recover(snapshot_dir=args.snapshot_dir, minimum_rows=args.minimum_rows)
    print(json.dumps({
        "manifest": result["manifest"],
        "observed_at": result["source"]["observed_at"],
        "rows": result["snapshot"]["rows"],
        "snapshot": result["snapshot"]["snapshot"],
        "snapshot_sha256": result["snapshot"]["sha256"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
