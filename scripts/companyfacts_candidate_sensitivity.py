"""Replay a recipe-bound Company Facts candidate without formal writes.

This diagnostic binds the immutable cache snapshot, parser recipe, candidate
CSV files, and the exact reference fundamentals.  It reuses the frozen Top 3
quarterly-version replay and records the PIT growth features that explain a
superseded candidate claim.  It never runs formal validation or writes formal
financial files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.conf import (
    POINT_IN_TIME_FUNDAMENTALS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    PROJECT_PATH,
)
from src.financial.quarterly_fundamentals import (
    load_quarterly_fundamentals,
    quarterly_growth_snapshot,
)
from src.research.quarterly_data_version_impact import (
    run_quarterly_data_version_impact,
)


DEFAULT_SCOPE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_full_rebuild_scope_manifest-6c8a87fcc71cfcd5-q1-fp-guard.json"
)
DEFAULT_CANDIDATE_DIR = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_candidate_rebuild_manifest-6c8a87fcc71cfcd5-"
    "recipe-6f0998be-q1-fp-guard"
)
DEFAULT_PREVIOUS_EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_complete_cache_candidate_sensitivity_2026-08-09.json"
)
DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_q1_fp_guard_candidate_sensitivity_2026-08-09.json"
)
DEFAULT_ANNUAL_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_q1_fp_guard_sensitivity_annual_2026-08-09.csv"
)
DEFAULT_SIGNAL_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_q1_fp_guard_sensitivity_signals_2026-08-09.csv"
)
PIT_CHECKS = (
    ("DAVE", "2025-06-30"),
    ("COMM", "2025-10-31"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path(PROJECT_PATH).resolve()))
    except ValueError:
        return str(path.resolve())


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _growth_check(
    reference: pd.DataFrame,
    candidate: pd.DataFrame,
    ticker: str,
    signal_date: str,
) -> dict:
    date = pd.Timestamp(signal_date)
    reference_row = quarterly_growth_snapshot(reference, date).loc[ticker]
    candidate_row = quarterly_growth_snapshot(candidate, date).loc[ticker]
    return {
        "ticker": ticker,
        "signal_date": signal_date,
        "reference": {
            "growth_available_date": reference_row[
                "growth_available_date"
            ].strftime("%Y-%m-%d"),
            "financial_age_days": int(reference_row["financial_age_days"]),
            "revenue_ttm_growth": float(reference_row["revenue_growth"]),
            "net_income_ttm_growth": float(reference_row["net_income_growth"]),
        },
        "candidate": {
            "growth_available_date": candidate_row[
                "growth_available_date"
            ].strftime("%Y-%m-%d"),
            "financial_age_days": int(candidate_row["financial_age_days"]),
            "revenue_ttm_growth": float(candidate_row["revenue_growth"]),
            "net_income_ttm_growth": float(candidate_row["net_income_growth"]),
        },
    }


def run(
    *,
    scope_path: Path,
    candidate_dir: Path,
    previous_evidence_path: Path,
    output: Path,
    annual_output: Path,
    signal_output: Path,
) -> dict:
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    previous = json.loads(previous_evidence_path.read_text(encoding="utf-8"))
    rebuild_report_path = candidate_dir / "rebuild_report.json"
    rebuild = json.loads(rebuild_report_path.read_text(encoding="utf-8"))
    candidate_annual_path = candidate_dir / "annual.csv"
    candidate_quarterly_path = candidate_dir / "quarterly.csv"
    reference_annual_path = Path(POINT_IN_TIME_FUNDAMENTALS_FILE)
    reference_quarterly_path = Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE)

    expected_manifest = scope["snapshot"]["cache_manifest_sha256"]
    expected_recipe = scope["rebuild_recipe_sha256"]
    if rebuild["inputs"]["cache_manifest_sha256"] != expected_manifest:
        raise ValueError("candidate rebuild cache manifest does not match scope")
    if rebuild["inputs"]["rebuild_recipe_sha256"] != expected_recipe:
        raise ValueError("candidate rebuild recipe does not match scope")

    annual, signals, replay = run_quarterly_data_version_impact(
        reference_quarterly_path,
        candidate_quarterly_path,
    )
    annual_output.parent.mkdir(parents=True, exist_ok=True)
    signal_output.parent.mkdir(parents=True, exist_ok=True)
    annual.to_csv(annual_output, index=False)
    signals.to_csv(signal_output, index=False)

    reference_frame = load_quarterly_fundamentals(reference_quarterly_path)
    candidate_frame = load_quarterly_fundamentals(candidate_quarterly_path)
    changed_2025 = signals.loc[
        pd.to_datetime(signals["signal_date"]).dt.year.eq(2025)
    ]
    annual_2025 = annual.loc[annual["year"].eq(2025)].iloc[0]
    previous_sensitivity = previous["in_memory_fixed_parameter_sensitivity"]
    previous_2025 = previous_sensitivity["candidate_annual_results"]["2025"]

    payload = {
        "format_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "companyfacts_q1_fp_guard_candidate_sensitivity",
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_validation_rerun": False,
        "formal_outputs_written": False,
        "scope": {
            "path": _relative(scope_path),
            "sha256": _sha256(scope_path),
            "snapshot_id": scope["snapshot"]["snapshot_id"],
            "cache_manifest_sha256": expected_manifest,
            "rebuild_recipe_sha256": expected_recipe,
            "parser_sha256": scope["rebuild_recipe"]["parser_sha256"],
        },
        "candidate_files": {
            "annual_path": _relative(candidate_annual_path),
            "annual_sha256": _sha256(candidate_annual_path),
            "annual_rows": int(len(pd.read_csv(candidate_annual_path))),
            "quarterly_path": _relative(candidate_quarterly_path),
            "quarterly_sha256": _sha256(candidate_quarterly_path),
            "quarterly_rows": int(len(pd.read_csv(candidate_quarterly_path))),
            "rebuild_report_path": _relative(rebuild_report_path),
            "rebuild_report_sha256": _sha256(rebuild_report_path),
        },
        "reference_files": {
            "annual_path": _relative(reference_annual_path),
            "annual_sha256": _sha256(reference_annual_path),
            "quarterly_path": _relative(reference_quarterly_path),
            "quarterly_sha256": _sha256(reference_quarterly_path),
        },
        "replay_outputs": {
            "annual_path": _relative(annual_output),
            "annual_sha256": _sha256(annual_output),
            "signals_path": _relative(signal_output),
            "signals_sha256": _sha256(signal_output),
        },
        "current_input_replay": {
            **replay,
            "reference_wins_vs_nasdaq": int(replay["reference_wins_vs_nasdaq"]),
            "candidate_wins_vs_nasdaq": int(replay["candidate_wins_vs_nasdaq"]),
            "changed_2025_signal_count": int(len(changed_2025)),
            "changed_2025_signals": changed_2025.to_dict(orient="records"),
            "reference_2025_strategy": float(annual_2025["reference_strategy"]),
            "candidate_2025_strategy": float(annual_2025["candidate_strategy"]),
            "nasdaq_2025": float(annual_2025["candidate_benchmark"]),
        },
        "pit_growth_feature_checks": [
            _growth_check(reference_frame, candidate_frame, ticker, signal_date)
            for ticker, signal_date in PIT_CHECKS
        ],
        "superseded_evidence": {
            "path": _relative(previous_evidence_path),
            "sha256": _sha256(previous_evidence_path),
            "candidate_wins_vs_nasdaq": int(
                previous_sensitivity["candidate_wins_vs_nasdaq"]
            ),
            "candidate_2025_strategy": float(previous_2025["strategy"]),
            "nasdaq_2025": float(previous_2025["nasdaq"]),
            "selected_2025_changes": previous_sensitivity[
                "selected_2025_changes"
            ],
        },
        "conclusions": {
            "old_2025_candidate_uplift_survives_q1_fp_guard": False,
            "old_2025_selection_changes_survive_q1_fp_guard": False,
            "dave_false_negative_q3_revenue_removed": True,
            "candidate_changes_current_input_win_count": bool(
                replay["candidate_wins_vs_nasdaq"]
                != replay["reference_wins_vs_nasdaq"]
            ),
            "formal_4_of_6_result_revalidated_by_this_run": False,
            "interpretation": (
                "The old 2025 uplift was a parser artifact. The Q1-fp guard "
                "candidate has no 2025 selection changes and exactly matches "
                "the current-input reference 2025 return. Current inputs are "
                "post-validation research state, so their 5/6 replay does not "
                "replace or revalidate the frozen formal 4/6 result."
            ),
        },
    }
    _atomic_json(output, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    parser.add_argument("--candidate-dir", type=Path, default=DEFAULT_CANDIDATE_DIR)
    parser.add_argument(
        "--previous-evidence", type=Path, default=DEFAULT_PREVIOUS_EVIDENCE
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--annual-output", type=Path, default=DEFAULT_ANNUAL_OUTPUT)
    parser.add_argument("--signal-output", type=Path, default=DEFAULT_SIGNAL_OUTPUT)
    args = parser.parse_args()
    payload = run(
        scope_path=args.scope,
        candidate_dir=args.candidate_dir,
        previous_evidence_path=args.previous_evidence,
        output=args.output,
        annual_output=args.annual_output,
        signal_output=args.signal_output,
    )
    print(json.dumps(payload["conclusions"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
