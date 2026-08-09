"""Layer formula-proven filing-local Q4 rows onto a research-only candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.sec_filing_dataset_supplement_candidate import _identity, _normalize_row
from src.io.fundamentals_update import OUTPUT_COLUMNS


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_formula_candidate(
    base_quarterly: str | Path,
    formula_audit: str | Path,
    output_dir: str | Path,
    *,
    fetched_at: str,
) -> dict[str, Any]:
    base_path = Path(base_quarterly).resolve()
    audit_path = Path(formula_audit).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    base = pd.read_csv(base_path)
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if not audit.get("research_only"):
        raise ValueError("formula audit must be explicitly research-only")

    semantic_columns = [
        "ticker",
        "fiscal_end",
        "available_date",
        "metric",
        "value",
        "accession",
    ]
    existing = {_identity(row, semantic_columns) for row in base.to_dict("records")}
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    rejected_unproven = 0

    for proof in audit.get("proofs", []):
        if proof.get("reason") != "formula_proven" or not proof.get("matched"):
            rejected_unproven += 1
            continue
        target = proof["target"]
        selected = proof.get("selected_attempt") or {}
        if not selected.get("matched"):
            raise ValueError("formula_proven proof is missing a matched selected_attempt")
        expected = float(selected["expected_q4_value"])
        actual = float(target["value"])
        if not pd.notna(expected) or abs(expected - actual) > max(1e-6, abs(actual) * 1e-9):
            raise ValueError("formula_proven target does not equal its proof operand result")
        row = {
            **{column: target.get(column) for column in OUTPUT_COLUMNS},
            "concept": target.get("concept"),
            "fetched_at": fetched_at,
        }
        normalized = _normalize_row(row, OUTPUT_COLUMNS)
        key = _identity(normalized, semantic_columns)
        if key in existing:
            skipped.append(normalized)
            continue
        accepted.append(normalized)
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
        "base_quarterly": str(base_path),
        "base_quarterly_sha256": _sha256(base_path),
        "formula_audit": str(audit_path),
        "formula_audit_sha256": _sha256(audit_path),
        "fetched_at": pd.Timestamp(fetched_at).strftime("%Y-%m-%d"),
        "base_row_count": int(len(base)),
        "formula_proven_count": int(audit.get("formula_match_count", 0)),
        "rejected_unproven_count": rejected_unproven,
        "accepted_row_count": len(accepted),
        "skipped_semantically_existing_row_count": len(skipped),
        "accepted_rows": accepted,
        "skipped_semantically_existing_rows": skipped,
        "output_path": str(output_path),
        "output_row_count": int(len(candidate)),
        "output_sha256": _sha256(output_path),
    }
    report_path = output_dir / "layering_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-quarterly", type=Path, required=True)
    parser.add_argument("--formula-audit", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fetched-at", required=True)
    args = parser.parse_args()
    report = build_formula_candidate(
        args.base_quarterly,
        args.formula_audit,
        args.output_dir,
        fetched_at=args.fetched_at,
    )
    print(json.dumps({
        "accepted_rows": report["accepted_row_count"],
        "skipped_existing": report["skipped_semantically_existing_row_count"],
        "output_sha256": report["output_sha256"],
        "research_only": True,
    }, indent=2))


if __name__ == "__main__":
    main()
