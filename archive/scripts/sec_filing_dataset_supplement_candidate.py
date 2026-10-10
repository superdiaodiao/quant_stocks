"""Layer proven SEC filing-dataset rows onto a research-only quarterly CSV."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.sec_filing_dataset_supplement_impact import (
    COMPARE_COLUMNS,
    _parse_quarterly_with_targeted_sec_facts,
)
from src.io.fundamentals_update import OUTPUT_COLUMNS


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_row(row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    normalized = {column: row.get(column) for column in columns}
    for field in ("fiscal_end", "available_date"):
        normalized[field] = pd.Timestamp(normalized[field]).strftime("%Y-%m-%d")
    if "fetched_at" in normalized and normalized["fetched_at"] is not None:
        normalized["fetched_at"] = pd.Timestamp(
            normalized["fetched_at"]
        ).strftime("%Y-%m-%d")
    normalized["value"] = float(normalized["value"])
    return normalized


def _identity(row: dict[str, Any], columns: list[str]) -> str:
    return json.dumps(
        _normalize_row(row, columns), sort_keys=True, separators=(",", ":")
    )


def build_quarterly_candidate(
    base_quarterly: str | Path,
    supplemented_dir: str | Path,
    impact_report: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    base_quarterly = Path(base_quarterly).resolve()
    supplemented_dir = Path(supplemented_dir).resolve()
    impact_report = Path(impact_report).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    base = pd.read_csv(base_quarterly)
    impact = json.loads(impact_report.read_text(encoding="utf-8"))
    supplement_provenance_path = supplemented_dir / "provenance.json"
    supplement_provenance = json.loads(
        supplement_provenance_path.read_text(encoding="utf-8")
    )
    supplement_by_symbol = {
        (entry.get("symbols") or [""])[0]: entry
        for entry in supplement_provenance["entries"]
    }

    parsed_additions = []
    for entry in impact["entries"]:
        wanted = {
            _identity(row, COMPARE_COLUMNS)
            for row in entry["datasets"]["quarterly"]["added_rows"]
        }
        if not wanted:
            continue
        source = supplement_by_symbol[entry["symbol"]]
        envelope = json.loads(gzip.decompress(Path(source["output_path"]).read_bytes()))
        snapshot_path = Path(source["source_snapshot"])
        raw = snapshot_path.read_bytes()
        if snapshot_path.suffix == ".gz":
            raw = gzip.decompress(raw)
        fetched_at = json.loads(raw).get("fetched_at") or "2000-01-01"
        parsed = _parse_quarterly_with_targeted_sec_facts(
            entry["symbol"], envelope["payload"], fetched_at=fetched_at
        )
        for row in parsed.to_dict("records"):
            if _identity(row, COMPARE_COLUMNS) in wanted:
                parsed_additions.append(row)

    semantic_columns = [
        "ticker", "fiscal_end", "available_date", "metric", "value", "accession"
    ]
    existing = {
        _identity(row, semantic_columns) for row in base.to_dict("records")
    }
    accepted = []
    skipped_existing = []
    for row in parsed_additions:
        key = _identity(row, semantic_columns)
        if key in existing:
            skipped_existing.append(_normalize_row(row, OUTPUT_COLUMNS))
            continue
        accepted.append(_normalize_row(row, OUTPUT_COLUMNS))
        existing.add(key)
    candidate = pd.concat([base, pd.DataFrame(accepted)], ignore_index=True)
    candidate["fiscal_end"] = pd.to_datetime(candidate["fiscal_end"])
    candidate["available_date"] = pd.to_datetime(candidate["available_date"])
    candidate = candidate.sort_values(
        ["ticker", "available_date", "fiscal_end", "metric", "accession"],
        kind="stable",
    )
    output_path = output_dir / "quarterly.csv"
    candidate.to_csv(output_path, index=False)
    report = {
        "format_version": 1,
        "research_only": True,
        "formal_financial_files_modified": False,
        "base_quarterly": str(base_quarterly),
        "base_quarterly_sha256": _sha256(base_quarterly),
        "supplemented_dir": str(supplemented_dir),
        "supplement_provenance_sha256": _sha256(supplement_provenance_path),
        "impact_report": str(impact_report),
        "impact_report_sha256": _sha256(impact_report),
        "base_row_count": len(base),
        "parsed_added_row_count": len(parsed_additions),
        "accepted_row_count": len(accepted),
        "skipped_semantically_existing_row_count": len(skipped_existing),
        "accepted_rows": accepted,
        "skipped_semantically_existing_rows": skipped_existing,
        "output_path": str(output_path),
        "output_row_count": len(candidate),
        "output_sha256": _sha256(output_path),
    }
    (output_dir / "layering_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-quarterly", type=Path, required=True)
    parser.add_argument("--supplemented-dir", type=Path, required=True)
    parser.add_argument("--impact-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = build_quarterly_candidate(
        args.base_quarterly,
        args.supplemented_dir,
        args.impact_report,
        args.output_dir,
    )
    print(json.dumps({
        "accepted_rows": report["accepted_row_count"],
        "skipped_existing": report["skipped_semantically_existing_row_count"],
        "output_sha256": report["output_sha256"],
        "research_only": True,
    }, indent=2))


if __name__ == "__main__":
    main()
