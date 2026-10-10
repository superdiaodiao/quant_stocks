"""Measure parsed annual/quarterly impact of research-only SEC supplements."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.io.fundamentals_update import (
    OUTPUT_COLUMNS,
    parse_companyfacts_annual,
    parse_companyfacts_quarterly,
)


COMPARE_COLUMNS = [
    "ticker",
    "fiscal_end",
    "available_date",
    "metric",
    "value",
    "taxonomy",
    "concept",
    "form",
    "accession",
]
VALUE_COLUMNS = ["ticker", "fiscal_end", "available_date", "metric", "value"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_gz(path: Path) -> dict[str, Any]:
    return json.loads(gzip.decompress(path.read_bytes()))


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    result = []
    for row in frame[COMPARE_COLUMNS].to_dict("records"):
        for field in ("fiscal_end", "available_date"):
            row[field] = pd.Timestamp(row[field]).strftime("%Y-%m-%d")
        row["value"] = float(row["value"])
        result.append(row)
    return sorted(
        result,
        key=lambda row: tuple(str(row.get(column)) for column in COMPARE_COLUMNS),
    )


def _identity(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def _parse_quarterly_with_targeted_sec_facts(
    symbol: str, payload: dict[str, Any], fetched_at: str
) -> pd.DataFrame:
    """Add only target-bound direct filing-dataset quarters to normal parsing."""
    base = parse_companyfacts_quarterly(symbol, payload, fetched_at=fetched_at)
    direct = []
    for taxonomy, concepts in payload.get("facts", {}).items():
        for concept_name, concept in concepts.items():
            for rows in concept.get("units", {}).values():
                for row in rows:
                    metric = row.get("_sec_filing_dataset_target_metric")
                    if not metric:
                        continue
                    direct.append({
                        "ticker": symbol.upper(),
                        "fiscal_end": pd.to_datetime(row["end"]),
                        "available_date": pd.to_datetime(row["filed"]),
                        "metric": metric,
                        "value": float(row["val"]),
                        "taxonomy": taxonomy,
                        "concept": concept_name,
                        "form": row.get("form"),
                        "accession": row.get("accn"),
                        "fetched_at": pd.Timestamp(fetched_at).tz_localize(None).normalize(),
                    })
    if not direct:
        return base
    return (
        pd.concat([base, pd.DataFrame(direct)[OUTPUT_COLUMNS]], ignore_index=True)
        .drop_duplicates(COMPARE_COLUMNS, keep="first")
        .sort_values(["ticker", "available_date", "fiscal_end", "metric"])
    )


def audit_supplement_impact(
    supplemented_dir: str | Path,
    output: str | Path,
) -> dict[str, Any]:
    supplemented_dir = Path(supplemented_dir).resolve()
    output = Path(output).resolve()
    provenance_path = supplemented_dir / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    entries = []
    for source in provenance["entries"]:
        envelope = _read_json_gz(Path(source["output_path"]))
        symbol = (source.get("symbols") or [""])[0]
        base_envelope_path = Path(source["source_snapshot"])
        raw = base_envelope_path.read_bytes()
        if base_envelope_path.suffix == ".gz":
            raw = gzip.decompress(raw)
        base_payload = json.loads(raw)["payload"]
        merged_payload = envelope["payload"]
        datasets = {}
        for name, parser in (
            ("annual", parse_companyfacts_annual),
            ("quarterly", _parse_quarterly_with_targeted_sec_facts),
        ):
            base_rows = _records(parser(symbol, base_payload, fetched_at="2000-01-01"))
            merged_rows = _records(parser(symbol, merged_payload, fetched_at="2000-01-01"))
            base_by_key = {_identity(row): row for row in base_rows}
            merged_by_key = {_identity(row): row for row in merged_rows}
            added = [merged_by_key[key] for key in sorted(merged_by_key.keys() - base_by_key.keys())]
            removed = [base_by_key[key] for key in sorted(base_by_key.keys() - merged_by_key.keys())]
            base_values = {
                _identity({column: row[column] for column in VALUE_COLUMNS})
                for row in base_rows
            }
            merged_values = {
                _identity({column: row[column] for column in VALUE_COLUMNS})
                for row in merged_rows
            }
            datasets[name] = {
                "base_row_count": len(base_rows),
                "merged_row_count": len(merged_rows),
                "added_row_count": len(added),
                "removed_row_count": len(removed),
                "added_rows": added,
                "removed_rows": removed,
                "added_coordinate_value_count": len(merged_values - base_values),
                "removed_coordinate_value_count": len(base_values - merged_values),
            }
        entries.append({
            "cik": source["cik"],
            "symbol": symbol,
            "supplemented_payload_sha256": source["merged_payload_sha256"],
            "datasets": datasets,
        })
    report = {
        "format_version": 1,
        "research_only": True,
        "formal_financial_files_modified": False,
        "supplemented_dir": str(supplemented_dir),
        "supplement_provenance_sha256": _sha256(provenance_path),
        "entries": entries,
        "totals": {
            dataset: {
                "added_rows": sum(
                    entry["datasets"][dataset]["added_row_count"] for entry in entries
                ),
                "removed_rows": sum(
                    entry["datasets"][dataset]["removed_row_count"] for entry in entries
                ),
                "added_coordinate_values": sum(
                    entry["datasets"][dataset]["added_coordinate_value_count"]
                    for entry in entries
                ),
                "removed_coordinate_values": sum(
                    entry["datasets"][dataset]["removed_coordinate_value_count"]
                    for entry in entries
                ),
            }
            for dataset in ("annual", "quarterly")
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supplemented-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_supplement_impact(args.supplemented_dir, args.output)
    print(json.dumps(report["totals"], indent=2))


if __name__ == "__main__":
    main()
