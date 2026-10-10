#!/usr/bin/env python3
"""Import unambiguous pinned GitHub listings into the isolated v14 universe."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from src.io.nasdaq_update import import_nasdaq_trader_files


DEFAULT_CATALOG = Path(
    "output/research_only/v14/github_nasdaq_listings_catalog.json"
)
DEFAULT_SNAPSHOT_DIR = Path("output/research_only/v14/universe_snapshots")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_missing(
    *,
    catalog_path: Path = DEFAULT_CATALOG,
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    start_year: int = 2015,
    end_year: int = 2020,
    minimum_rows: int = 1000,
) -> dict:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not catalog.get("research_only"):
        raise ValueError("catalog is not marked research_only")
    manifest_path = snapshot_dir / "github_recovery_manifest.json"
    previous = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists() else {}
    )
    grouped: dict[str, list[dict]] = {}
    for row in catalog.get("records", []):
        year = int(row["observed_at"][:4])
        if start_year <= year <= end_year:
            grouped.setdefault(row["observed_at"], []).append(row)
    selected, existing, conflicts = [], [], []
    for observed_at, rows in sorted(grouped.items()):
        target = snapshot_dir / f"nasdaq_listed_{observed_at}.csv"
        if target.exists():
            existing.append(observed_at)
            continue
        payloads = {row["payload_sha256"] for row in rows}
        if len(payloads) != 1:
            conflicts.append({
                "observed_at": observed_at,
                "payload_sha256": sorted(payloads),
                "sources": sorted(row["source"] for row in rows),
            })
            continue
        selected.append(rows[0])
    if selected:
        imported = import_nasdaq_trader_files(
            [row["source"] for row in selected],
            minimum_rows=minimum_rows,
            snapshot_dir=snapshot_dir,
        )
    else:
        cumulative = snapshot_dir / "nasdaq_trader_file_import_manifest.json"
        imported = (
            json.loads(cumulative.read_text(encoding="utf-8"))
            if cumulative.exists() else {"imported": [], "skipped": []}
        )
    evidence_selected = selected or previous.get("selected", [])
    selected_sources = {row["source"] for row in evidence_selected}
    newly_imported = [
        row for row in imported["imported"]
        if row.get("source_file") in selected_sources
    ]
    result = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_universe_modified": False,
        "catalog": {"path": str(catalog_path), "sha256": _sha256(catalog_path)},
        "snapshot_dir": str(snapshot_dir),
        "selected": evidence_selected,
        "already_present_dates": existing,
        "conflicts": conflicts,
        "imported": newly_imported,
        "skipped": imported["skipped"],
    }
    manifest_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    result["manifest"] = str(manifest_path)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--start-year", type=int, default=2015)
    parser.add_argument("--end-year", type=int, default=2020)
    parser.add_argument("--minimum-rows", type=int, default=1000)
    args = parser.parse_args()
    result = import_missing(
        catalog_path=args.catalog, snapshot_dir=args.snapshot_dir,
        start_year=args.start_year, end_year=args.end_year,
        minimum_rows=args.minimum_rows,
    )
    print(json.dumps({
        "manifest": result["manifest"],
        "selected_count": len(result["selected"]),
        "imported_count": len(result["imported"]),
        "skipped_count": len(result["skipped"]),
        "conflict_count": len(result["conflicts"]),
        "formal_universe_modified": result["formal_universe_modified"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
