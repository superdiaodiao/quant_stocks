import json

import pandas as pd

from scripts.sec_inline_xbrl_q4_formula_candidate import build_formula_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


def _row(value=10.0):
    return {
        "ticker": "TEST",
        "fiscal_end": "2020-12-31",
        "available_date": "2021-03-01",
        "metric": "revenue",
        "value": value,
        "taxonomy": "us-gaap",
        "concept": "derived_q4:Revenues",
        "form": "10-K",
        "accession": "0000000000-21-000001",
        "fetched_at": "2021-03-02",
    }


def test_build_formula_candidate_only_layers_proven_missing_rows(tmp_path):
    base_path = tmp_path / "base.csv"
    pd.DataFrame([_row()])[OUTPUT_COLUMNS].to_csv(base_path, index=False)
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({
        "research_only": True,
        "formula_match_count": 2,
        "proofs": [
            {
                "reason": "formula_proven",
                "matched": True,
                "target": _row(),
                "selected_attempt": {"matched": True, "expected_q4_value": 10.0},
            },
            {
                "reason": "formula_proven",
                "matched": True,
                "target": {
                    **_row(12.0),
                    "available_date": "2022-03-01",
                    "accession": "0000000000-22-000002",
                },
                "selected_attempt": {"matched": True, "expected_q4_value": 12.0},
            },
            {"reason": "quarter_gaps_invalid", "matched": False, "target": _row(99.0)},
        ],
    }), encoding="utf-8")

    report = build_formula_candidate(
        base_path, audit_path, tmp_path / "candidate", fetched_at="2026-08-09"
    )

    assert report["accepted_row_count"] == 1
    assert report["skipped_semantically_existing_row_count"] == 1
    assert report["rejected_unproven_count"] == 1
    candidate = pd.read_csv(report["output_path"])
    assert candidate["value"].tolist() == [10.0, 12.0]
    assert candidate.iloc[-1]["fetched_at"] == "2026-08-09"
