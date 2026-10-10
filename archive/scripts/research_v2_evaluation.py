"""Bind and evaluate the predeclared research-v2 walk-forward experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.conf import POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE, PROJECT_PATH
from src.research.data_fingerprint import (
    build_data_manifest,
    can_slim_input_fingerprints,
)


DEFAULT_SUFFIX = "_quarterly_financials_financial_age_150_365_550"
DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/research_v2_evaluation_2026-08-10.json"
)
MINIMUM_OOS_WINS = 4
MINIMUM_30_BPS_WINS = 4
MINIMUM_FORWARD_MONTHS = 12


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def evaluate(
    walk: pd.DataFrame,
    cost: pd.DataFrame,
    selected_audit: dict,
) -> dict:
    wins = int(walk["excess_vs_nasdaq"].gt(0).sum())
    cost_30 = cost.loc[cost["cost_bps"].eq(30.0)]
    wins_30 = int(cost_30["excess_vs_nasdaq"].gt(0).sum())
    checks = {
        "minimum_oos_wins": wins >= MINIMUM_OOS_WINS,
        "positive_median_oos_excess": float(
            walk["excess_vs_nasdaq"].median()
        ) > 0,
        "minimum_30_bps_wins": wins_30 >= MINIMUM_30_BPS_WINS,
        "selected_holding_prices_complete": int(
            selected_audit["positions_with_missing_holding_prices"]
        ) == 0,
        "selected_terminal_returns_complete": int(
            selected_audit["positions_with_unresolved_terminal_return"]
        ) == 0,
    }
    return {
        "oos_years": int(len(walk)),
        "oos_wins_vs_nasdaq": wins,
        "median_oos_excess": float(walk["excess_vs_nasdaq"].median()),
        "minimum_oos_excess": float(walk["excess_vs_nasdaq"].min()),
        "wins_at_30_bps": wins_30,
        "checks": checks,
        "historical_candidate_passed": bool(all(checks.values())),
    }


def run(
    suffix: str,
    output: Path,
    quarterly_path: Path = Path(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    ),
) -> dict:
    base = Path(PROJECT_PATH) / "output"
    paths = {
        "summary": base / f"can_slim_walk_forward_summary{suffix}.json",
        "walk_forward": base / f"can_slim_walk_forward{suffix}.csv",
        "candidates": base / f"can_slim_walk_forward_candidates{suffix}.csv",
        "rankings": base / f"can_slim_walk_forward_rankings{suffix}.csv",
        "cost_stress": base / f"can_slim_walk_forward_cost_stress{suffix}.csv",
        "selected_audit": base / f"can_slim_selected_data_audit{suffix}.json",
        "selected_ledger": base / f"can_slim_selected_data_audit{suffix}.csv",
    }
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    walk = pd.read_csv(paths["walk_forward"])
    cost = pd.read_csv(paths["cost_stress"])
    selected_audit = json.loads(paths["selected_audit"].read_text(encoding="utf-8"))
    evaluation = evaluate(walk, cost, selected_audit)
    quarterly_path = Path(quarterly_path)
    fingerprints = can_slim_input_fingerprints()
    fingerprints["quarterly_fundamentals"] = {
        **fingerprints["quarterly_fundamentals"],
        "path": str(quarterly_path.resolve()),
        "sha256": _sha256(quarterly_path),
        "bytes": quarterly_path.stat().st_size,
    }
    payload = {
        "format_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "research_v2_parameter_recalibration_evaluation",
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_validation_rerun": False,
        "formal_v1_modified": False,
        "experiment": {
            "suffix": suffix,
            "candidate_count": int(summary["candidate_count"]),
            "maximum_financial_age_days_grid": summary.get(
                "maximum_financial_age_days_grid"
            ),
            "selection_method": summary["method"],
            "parameter_update_frequency": summary["parameter_update_frequency"],
        },
        "predeclared_historical_criteria": {
            "minimum_oos_wins": MINIMUM_OOS_WINS,
            "oos_year_count": int(len(walk)),
            "median_oos_excess_must_be_positive": True,
            "minimum_30_bps_wins": MINIMUM_30_BPS_WINS,
            "selected_holding_prices_complete": True,
            "selected_terminal_returns_complete": True,
        },
        "forward_promotion_criteria": {
            "minimum_shadow_months": MINIMUM_FORWARD_MONTHS,
            "satisfied_by_this_historical_run": False,
        },
        "evaluation": evaluation,
        "promotion_eligible": False,
        "promotion_blockers": [
            name for name, passed in evaluation["checks"].items() if not passed
        ] + ["minimum_12_month_forward_shadow_not_observed"],
        "data_manifest": build_data_manifest(fingerprints),
        "strategy_code_fingerprint": fingerprints["strategy_code"],
        "artifact_bindings": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in paths.items()
        },
    }
    _atomic_json(output, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suffix", default=DEFAULT_SUFFIX)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--quarterly-input",
        type=Path,
        default=Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    )
    args = parser.parse_args()
    payload = run(args.suffix, args.output, args.quarterly_input)
    print(json.dumps(payload["evaluation"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
