"""Prove missing Q4 rows from filing annual facts and PIT Q1-Q3 operands."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from scripts.sec_inline_xbrl_target_probe import parse_inline_xbrl
from src.io.fundamentals_update import _coalesce_equivalent_quarter_ends


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _quarter_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    result = []
    for row in frame.to_dict("records"):
        result.append({
            "ticker": row["ticker"],
            "fiscal_end": _date(row["fiscal_end"]),
            "available_date": _date(row["available_date"]),
            "metric": row["metric"],
            "value": float(row["value"]),
            "taxonomy": row.get("taxonomy"),
            "concept": row.get("concept"),
            "form": row.get("form"),
            "accession": row.get("accession"),
        })
    return result


def _annual_operands(
    facts: list[dict[str, Any]], target: dict[str, Any]
) -> list[dict[str, Any]]:
    concept = str(target["concept"]).split(":")[-1]
    target_end = pd.Timestamp(target["fiscal_end"])
    candidates = {}
    for fact in facts:
        if str(fact.get("name") or "").split(":")[-1] != concept:
            continue
        if fact.get("segmented") or not fact.get("start") or not fact.get("end"):
            continue
        start, end = pd.Timestamp(fact["start"]), pd.Timestamp(fact["end"])
        duration = (end - start).days
        if not 250 <= duration <= 450 or abs((end - target_end).days) > 7:
            continue
        key = (start, end, float(fact["value"]), str(fact.get("unit")))
        candidates[key] = {
            **fact,
            "start": _date(start),
            "end": _date(end),
            "value": float(fact["value"]),
            "duration_days": duration,
        }
    return list(candidates.values())


def _select_quarters(
    quarterly: pd.DataFrame, target: dict[str, Any], annual_end: pd.Timestamp
) -> tuple[pd.DataFrame, list[int]]:
    target_available = pd.Timestamp(target["available_date"])
    candidates = quarterly.loc[
        quarterly["ticker"].eq(target["ticker"])
        & quarterly["metric"].eq(target["metric"])
        & quarterly["fiscal_end"].lt(annual_end)
        & quarterly["fiscal_end"].ge(annual_end - pd.Timedelta(days=330))
        & quarterly["available_date"].le(target_available)
        & ~quarterly["concept"].astype(str).str.startswith(
            ("derived_q4", "foreign_derived_q4", "foreign_")
        )
    ].copy()
    if candidates.empty:
        return candidates, []
    candidates = candidates.sort_values(
        ["available_date", "fiscal_end", "accession"], kind="stable"
    ).drop_duplicates("fiscal_end", keep="last")
    candidates = _coalesce_equivalent_quarter_ends(candidates)
    quarters = candidates.nlargest(3, "fiscal_end").sort_values("fiscal_end")
    ends = quarters["fiscal_end"].tolist() + [annual_end]
    gaps = [
        int((later - earlier).days)
        for earlier, later in zip(ends, ends[1:])
    ]
    return quarters, gaps


def audit_q4_formulas(
    target_manifest: str | Path,
    inline_batch: str | Path,
    quarterly_path: str | Path,
    output: str | Path,
) -> dict[str, Any]:
    target_manifest = Path(target_manifest).resolve()
    inline_batch = Path(inline_batch).resolve()
    quarterly_path = Path(quarterly_path).resolve()
    output = Path(output).resolve()
    targets = json.loads(target_manifest.read_text(encoding="utf-8"))[
        "unmatched_target_rows"
    ]
    batch = json.loads(inline_batch.read_text(encoding="utf-8"))
    filing_by_key = {
        (row["ticker"], row["accession"]): row
        for row in batch["records"]
        if row["status"] != "ERROR"
    }
    facts_by_key = {}
    for key, filing in filing_by_key.items():
        _, facts_by_key[key] = parse_inline_xbrl(filing["xbrl_path"])
    quarterly = pd.read_csv(
        quarterly_path,
        parse_dates=["fiscal_end", "available_date"],
    )
    proofs = []
    for target in targets:
        key = (target["ticker"], target["accession"])
        facts = facts_by_key.get(key)
        proof: dict[str, Any] = {"target": target, "matched": False}
        if facts is None:
            proof["reason"] = "filing_xbrl_unavailable"
            proofs.append(proof)
            continue
        annuals = _annual_operands(facts, target)
        proof["annual_candidates"] = annuals
        if not annuals:
            proof["reason"] = "annual_operand_absent"
            proofs.append(proof)
            continue
        attempts = []
        for annual in annuals:
            annual_end = pd.Timestamp(annual["end"])
            quarters, gaps = _select_quarters(quarterly, target, annual_end)
            expected = (
                float(annual["value"] - quarters["value"].sum())
                if len(quarters) == 3 and all(60 <= gap <= 135 for gap in gaps)
                else None
            )
            matched = bool(
                expected is not None
                and np.isclose(
                    expected, float(target["value"]), rtol=1e-9, atol=1e-6
                )
            )
            attempts.append({
                "annual_operand": annual,
                "quarter_operands": _quarter_records(quarters),
                "quarter_gap_days": gaps,
                "expected_q4_value": expected,
                "actual_target_value": float(target["value"]),
                "matched": matched,
            })
        proof["attempts"] = attempts
        matches = [attempt for attempt in attempts if attempt["matched"]]
        if matches:
            proof.update({
                "matched": True,
                "reason": "formula_proven",
                "selected_attempt": matches[0],
            })
        elif not any(len(attempt["quarter_operands"]) == 3 for attempt in attempts):
            proof["reason"] = "quarter_operands_incomplete"
        elif not any(
            len(attempt["quarter_gap_days"]) == 3
            and all(60 <= gap <= 135 for gap in attempt["quarter_gap_days"])
            for attempt in attempts
        ):
            proof["reason"] = "quarter_gaps_invalid"
        else:
            proof["reason"] = "formula_value_mismatch"
        proofs.append(proof)
    report = {
        "format_version": 1,
        "research_only": True,
        "formal_financial_files_modified": False,
        "target_manifest": str(target_manifest),
        "target_manifest_sha256": _sha256(target_manifest),
        "inline_batch": str(inline_batch),
        "inline_batch_sha256": _sha256(inline_batch),
        "quarterly_path": str(quarterly_path),
        "quarterly_sha256": _sha256(quarterly_path),
        "target_count": len(targets),
        "formula_match_count": sum(proof["matched"] for proof in proofs),
        "reason_counts": {
            reason: sum(proof["reason"] == reason for proof in proofs)
            for reason in sorted({proof["reason"] for proof in proofs})
        },
        "proofs": proofs,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-manifest", type=Path, required=True)
    parser.add_argument("--inline-batch", type=Path, required=True)
    parser.add_argument("--quarterly", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_q4_formulas(
        args.target_manifest, args.inline_batch, args.quarterly, args.output
    )
    print(json.dumps({
        "targets": report["target_count"],
        "formula_matches": report["formula_match_count"],
        "reason_counts": report["reason_counts"],
        "research_only": True,
    }, indent=2))


if __name__ == "__main__":
    main()
