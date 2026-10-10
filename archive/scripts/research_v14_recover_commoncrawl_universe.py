#!/usr/bin/env python3
"""Recover Common Crawl Nasdaq listings into an isolated v14 snapshot set."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from src.conf import NASDAQ_300M_STOCK_LIST_FILE
from src.io.nasdaq_update import import_nasdaq_trader_files


DEFAULT_CATALOG = Path(
    "output/research_only/v14/commoncrawl_nasdaq_listings_catalog.json"
)
DEFAULT_OUTPUT = Path("output/research_only/v14/universe_snapshots")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recover(
    *,
    catalog_path: Path = DEFAULT_CATALOG,
    output_dir: Path = DEFAULT_OUTPUT,
    baseline_dir: Path | None = None,
    minimum_rows: int = 1000,
) -> dict:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog.get("errors"):
        raise ValueError("catalog still contains unresolved Common Crawl errors")
    if not catalog.get("research_only"):
        raise ValueError("catalog is not marked research_only")
    baseline_dir = baseline_dir or (
        Path(NASDAQ_300M_STOCK_LIST_FILE).parent / "snapshots"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    copied, conflicts = [], []
    for source in sorted(baseline_dir.glob("nasdaq_listed_*.csv")):
        target = output_dir / source.name
        source_sha = _sha256(source)
        if target.exists() and _sha256(target) != source_sha:
            conflicts.append({"source": str(source), "target": str(target)})
            continue
        if not target.exists():
            shutil.copy2(source, target)
        copied.append({
            "source": str(source), "target": str(target), "sha256": source_sha
        })
    if conflicts:
        raise ValueError(f"conflicting isolated snapshots: {conflicts}")
    sources = [row["archive_source"] for row in catalog.get("captures", [])]
    imported = import_nasdaq_trader_files(
        sources, minimum_rows=minimum_rows, snapshot_dir=output_dir
    )
    manifest = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_universe_modified": False,
        "catalog": {"path": str(catalog_path), "sha256": _sha256(catalog_path)},
        "baseline": {"path": str(baseline_dir), "copied": copied},
        "commoncrawl_capture_count": len(sources),
        "imported": imported["imported"],
        "skipped": imported["skipped"],
        "snapshot_dir": str(output_dir),
    }
    manifest_path = output_dir / "commoncrawl_recovery_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest["manifest"] = str(manifest_path)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--baseline-dir", type=Path)
    parser.add_argument("--minimum-rows", type=int, default=1000)
    args = parser.parse_args()
    result = recover(
        catalog_path=args.catalog,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
        minimum_rows=args.minimum_rows,
    )
    print(json.dumps({
        "manifest": result["manifest"],
        "baseline_snapshot_count": len(result["baseline"]["copied"]),
        "capture_count": result["commoncrawl_capture_count"],
        "imported_count": len(result["imported"]),
        "skipped_count": len(result["skipped"]),
        "formal_universe_modified": result["formal_universe_modified"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
