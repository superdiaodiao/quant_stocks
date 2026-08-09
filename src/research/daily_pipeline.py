"""One-command daily data refresh and shadow recommendation pipeline."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping

import pandas as pd

from src.io.financial_update import update_financials, update_sec_fallback
from src.io.fundamentals_update import update_fundamentals
from src.io.nasdaq_update import update_all
from src.research.data_audit import (
    audit_selected_price_calendars,
    require_project_data,
)
from src.research.can_slim_daily_recommendations import (
    generate_can_slim_shadow_recommendations,
    save_can_slim_shadow_recommendations,
)
from src.research.shadow_evaluation import evaluate_history
from src.research.shadow_ledger import (
    current_run_source,
    github_actions_source_is_valid,
    verify_shadow_ledger,
    write_shadow_ledger_manifest,
)
from src.research.production_gate import (
    evaluate_release_gate,
    write_release_gate,
)
from src.research.validation_artifacts import (
    verify_validation_artifact_manifest,
)


MODEL_VERSION = "can-slim-top3-v1"
RECOMMENDATION_LOG_COLUMNS = (
    "as_of",
    "ticker",
    "rank",
    "action",
    "action_reason",
    "mode",
    "model_version",
    "signal_date",
    "execution_date",
    "current_price",
    "target_weight",
    "portfolio_source_kind",
    "generated_at",
)


def compact_recommendations_for_log(
    recommendations: pd.DataFrame,
) -> pd.DataFrame:
    return recommendations.loc[:, [
        column
        for column in RECOMMENDATION_LOG_COLUMNS
        if column in recommendations.columns
    ]]


def compact_metadata_for_log(metadata: dict) -> dict:
    fingerprints = metadata.get("input_fingerprints", {})
    return {
        key: metadata.get(key)
        for key in (
            "as_of",
            "model_version",
            "mode",
            "release_status",
            "signal_frequency",
            "recommendations",
            "action_reason",
            "parameters_refreshed",
        )
    } | {
        "strategy_sha256": (
            fingerprints.get("strategy_code", {}).get("sha256")
        ),
        "data_manifest_sha256": (
            fingerprints.get("data_manifest", {}).get("sha256")
        ),
        "pipeline_data_status": metadata.get("pipeline_data_status"),
    }


def require_reusable_shadow_ledger(
    history_file: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict:
    history_path = Path(history_file)
    if not history_path.exists():
        return {
            "status": "NO_LEDGER_BOOTSTRAP",
            "integrity_verified": True,
            "externally_anchored": False,
        }
    verification = verify_shadow_ledger(history_path)
    if not verification.get("integrity_verified"):
        raise RuntimeError(
            "Existing shadow ledger is not safe to reuse: "
            f"{verification.get('status')}"
        )
    if verification.get("externally_anchored"):
        source = current_run_source(environment)
        if not github_actions_source_is_valid(source):
            raise RuntimeError(
                "Externally anchored shadow ledger can only be extended "
                "by a verified GitHub Actions run"
            )
        if not str(source.get("previous_artifact_id") or "").isdigit():
            raise RuntimeError(
                "Externally anchored shadow ledger requires a restored "
                "previous artifact"
            )
        previous_source = verification.get("source") or {}
        for field in ("repository", "workflow", "default_branch"):
            if source.get(field) != previous_source.get(field):
                raise RuntimeError(
                    "Externally anchored shadow ledger can only be extended "
                    "by its canonical repository workflow"
                )
    return verification


def write_pipeline_status(
    output_dir: str | Path,
    payload: dict,
    model_version: str = MODEL_VERSION,
) -> Path:
    """Atomically replace status so a failed run cannot leave stale PASS."""
    path = Path(output_dir, model_version, "pipeline_status.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def compact_update_report(report: dict | None) -> dict | None:
    if report is None:
        return None
    failures = report.get("failures", [])
    return {
        key: report.get(key)
        for key in ("end", "requested", "counts", "fresh_coverage")
        if key in report
    } | {
        "failure_count": len(failures),
        "failure_tickers": [
            failure.get("ticker") for failure in failures[:20]
        ],
    }


def selected_positive_weight_tickers(recommendations) -> list[str]:
    """Return actual selected stocks, excluding zero weights and cash."""
    if recommendations.empty:
        return []
    weights = recommendations["target_weight"].astype(float)
    return sorted(set(
        recommendations.loc[weights.gt(0), "ticker"]
        .astype(str).str.upper()
    ) - {"__CASH__"})


def selected_quarterly_conflict_tickers(
    selected_tickers: list[str],
    audit: dict,
    signal_date,
    history_years: int = 4,
) -> list[str]:
    """Return selected tickers with ambiguous facts usable at the signal."""
    selected = {str(ticker).upper() for ticker in selected_tickers}
    signal = pd.Timestamp(signal_date)
    fiscal_cutoff = signal - pd.DateOffset(years=history_years)
    conflicts = set()
    for row in audit.get("quarterly_value_conflicts", []):
        ticker = str(row["ticker"]).upper()
        if ticker not in selected:
            continue
        if (
            pd.Timestamp(row["available_date"]) <= signal
            and pd.Timestamp(row["fiscal_end"]) >= fiscal_cutoff
        ):
            conflicts.add(ticker)
    return sorted(conflicts)


def run_pipeline(args: argparse.Namespace, as_of: date) -> None:
    market_update = None
    financial_updates = {}
    if not args.skip_update and not args.skip_market_update:
        market_update = update_all(end=as_of, workers=args.workers)
    if not args.skip_update and not args.skip_financial_update:
        financial_updates["nasdaq_eps"] = update_financials(
            as_of=as_of, workers=min(args.workers, 8)
        )
        financial_updates["sec_eps_fallback"] = update_sec_fallback(
            as_of=as_of, workers=min(args.workers, 4)
        )
        financial_updates["sec_fundamentals"] = update_fundamentals(
            as_of=as_of, workers=min(args.workers, 4)
        )
    audit = require_project_data(as_of)
    summary_path = Path("output/can_slim_fixed_top3_summary.json")
    artifact_verification = verify_validation_artifact_manifest(
        summary_path.parent
    )
    summary = json.loads(summary_path.read_text())
    expected_strategy_sha256 = summary["input_fingerprints"][
        "strategy_code"
    ]["sha256"]
    model_version = summary.get("model_version", MODEL_VERSION)
    history_file = Path(
        args.output_dir,
        model_version,
        "recommendation_history.csv",
    )
    require_reusable_shadow_ledger(history_file)
    recommendations, metadata = generate_can_slim_shadow_recommendations(
        summary_file=summary_path,
        history_file=history_file,
    )
    selected_tickers = selected_positive_weight_tickers(recommendations)
    selected_calendar_audit = audit_selected_price_calendars(
        selected_tickers,
        metadata["signal_date"],
    )
    if not selected_calendar_audit["complete"]:
        gap_tickers = [
            row["ticker"] for row in selected_calendar_audit["gaps"]
        ]
        raise RuntimeError(
            "Selected portfolio price-calendar audit failed"
            + (
                ": " + ", ".join(gap_tickers)
                if gap_tickers else ""
            )
        )
    selected_financial_conflicts = selected_quarterly_conflict_tickers(
        selected_tickers,
        audit,
        metadata["signal_date"],
    )
    if selected_financial_conflicts:
        raise RuntimeError(
            "Selected portfolio contains ambiguous quarterly facts: "
            + ", ".join(selected_financial_conflicts)
        )
    metadata["pipeline_data_status"] = {
        "requested_as_of": as_of.isoformat(),
        "benchmark_latest_date": audit["benchmark_latest_date"],
        "price_coverage": audit["price_coverage"],
        "financial_coverage": audit["financial_coverage"],
        "quarterly_fundamentals_coverage": audit[
            "quarterly_fundamentals_coverage"
        ],
        "quarterly_fundamentals_coverage_basis": audit.get(
            "quarterly_fundamentals_coverage_basis"
        ),
        "quarterly_fundamentals_fresh_universe_coverage": audit.get(
            "quarterly_fundamentals_fresh_universe_coverage"
        ),
        "quarterly_fundamentals_fresh_addressable_coverage": audit.get(
            "quarterly_fundamentals_fresh_addressable_coverage"
        ),
        "material_missing_strategy_prices": len(
            audit["material_missing_strategy_prices"]
        ),
        "material_internal_price_gaps": len(
            audit.get("material_internal_price_gaps", [])
        ),
        "selected_price_calendar": selected_calendar_audit,
        "selected_quarterly_conflicts": selected_financial_conflicts,
        "market_update": compact_update_report(market_update),
        "financial_updates": {
            name: compact_update_report(report)
            for name, report in financial_updates.items()
        },
    }
    output = save_can_slim_shadow_recommendations(
        recommendations, metadata, args.output_dir
    )
    print(compact_recommendations_for_log(recommendations).to_string(
        index=False, float_format=lambda value: f"{value:.4f}"
    ))
    print(json.dumps(
        compact_metadata_for_log(metadata),
        sort_keys=True,
    ))
    print(output)
    model_dir = f"{args.output_dir}/{metadata['model_version']}"
    write_shadow_ledger_manifest(
        f"{model_dir}/recommendation_history.csv"
    )
    evaluation = evaluate_history(
        f"{model_dir}/recommendation_history.csv",
        f"{model_dir}/shadow_evaluation.json",
        expected_strategy_sha256=expected_strategy_sha256,
    )
    release_gate = evaluate_release_gate(
        summary,
        int(evaluation["forward_sessions"]),
        evaluation,
    )
    release_gate["validation_artifact_manifest"] = (
        artifact_verification
    )
    write_release_gate(
        release_gate,
        Path(model_dir, "release_gate.json"),
    )
    write_pipeline_status(
        args.output_dir,
        {
            "status": "PASS",
            "model_version": metadata["model_version"],
            "recommendation_as_of": metadata["as_of"],
            "pipeline_data_status": metadata["pipeline_data_status"],
            "shadow_status": evaluation["status"],
            "release_status": release_gate["release_status"],
            "release_blocker_classes": release_gate["blocker_classes"],
            "waiting_only_is_sufficient": release_gate[
                "waiting_only_is_sufficient"
            ],
            "forward_periods": evaluation["forward_periods"],
            "completed_forward_periods": evaluation[
                "completed_forward_periods"
            ],
            "contiguous_completed_forward_periods": evaluation[
                "contiguous_completed_forward_periods"
            ],
            "contiguous_forward_sessions": evaluation[
                "contiguous_forward_sessions"
            ],
            "contiguous_forward_strategy_return": evaluation[
                "contiguous_forward_strategy_return"
            ],
            "contiguous_forward_benchmark_return": evaluation[
                "contiguous_forward_benchmark_return"
            ],
            "forward_sessions": evaluation["forward_sessions"],
        },
        metadata["model_version"],
    )
    print(evaluation)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-update", action="store_true", help="Use all existing local data")
    parser.add_argument("--skip-market-update", action="store_true")
    parser.add_argument("--skip-financial-update", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output-dir", default="output/daily")
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        help=(
            "Explicit data-audit date for reproducible local runs; "
            "defaults to yesterday."
        ),
    )
    args = parser.parse_args()
    as_of = args.as_of or (date.today() - timedelta(days=1))
    try:
        run_pipeline(args, as_of)
    except Exception as error:
        write_pipeline_status(
            args.output_dir,
            {
                "status": "FAIL",
                "model_version": MODEL_VERSION,
                "requested_as_of": as_of.isoformat(),
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
        raise


if __name__ == "__main__":
    main()
