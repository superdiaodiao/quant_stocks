"""Conservative release gate for the experimental pure-stock strategy."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np

from src.research.shadow_evaluation import (
    BENCHMARK_ID,
    BENCHMARK_RETURN_SERIES,
    PRICE_ADJUSTMENT_POLICY,
)
from src.research.validation_artifacts import (
    verify_validation_artifact_manifest,
)
from src.research.shadow_policy import (
    MIN_COMPLETED_MONTHLY_PERIODS,
    MIN_CONTIGUOUS_SESSIONS,
    MIN_WINNING_PERIODS,
    REQUIRE_EXTERNAL_ANCHOR,
    REQUIRE_POSITIVE_EXCESS,
)


def finite_non_negative_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if np.isfinite(parsed) and parsed >= 0 else None


def sha256_string(value) -> str | None:
    normalized = str(value or "").strip().lower()
    return (
        normalized
        if re.fullmatch(r"[0-9a-f]{64}", normalized)
        else None
    )


def evaluate_release_gate(
    summary: dict,
    shadow_sessions: int = 0,
    shadow_summary: dict | None = None,
) -> dict:
    historical_checks = summary.get("historical_data_checks", {})
    declared_point_in_time_universe = summary.get(
        "candidate_pool_is_point_in_time",
        summary.get(
            "point_in_time_universe",
            bool(
                historical_checks.get("benchmark_calendar_complete", True)
                and historical_checks.get("point_in_time_membership_complete")
                and historical_checks.get("signal_member_prices_complete")
                and historical_checks.get(
                    "signal_technical_candidate_financials_complete", False
                )
            ),
        ),
    )
    listed_price_histories_complete = historical_checks.get(
        "listed_price_histories_complete",
        bool(declared_point_in_time_universe),
    )
    historical_quarterly_conflicts_absent = historical_checks.get(
        "historical_quarterly_value_conflicts_absent",
        bool(declared_point_in_time_universe),
    )
    point_in_time_universe = bool(
        declared_point_in_time_universe
        and listed_price_histories_complete
        and historical_quarterly_conflicts_absent
    )
    selected_terminal_returns_complete = summary.get(
        "selected_position_terminal_returns_complete",
        summary.get(
            "delisting_returns_complete",
            summary.get(
                "observed_delisting_returns_complete",
                historical_checks.get(
                    "observed_delisting_returns_complete", False
                ),
            ),
        ),
    )
    shadow = shadow_summary or {}
    forward_strategy = shadow.get("forward_strategy_return")
    forward_benchmark = shadow.get("forward_benchmark_return")
    forward_periods = int(shadow.get("forward_periods", 0))
    completed_forward_periods = int(
        shadow.get("completed_forward_periods", 0)
    )
    completed_period_win_rate = shadow.get("completed_period_win_rate")
    contiguous_forward_sessions = int(
        shadow.get("contiguous_forward_sessions", 0)
    )
    contiguous_completed_periods = int(
        shadow.get("contiguous_completed_forward_periods", 0)
    )
    contiguous_period_win_rate = shadow.get(
        "contiguous_completed_period_win_rate"
    )
    contiguous_strategy = shadow.get(
        "contiguous_forward_strategy_return"
    )
    contiguous_benchmark = shadow.get(
        "contiguous_forward_benchmark_return"
    )
    accounting_method = shadow.get("accounting_method")
    ledger_provenance = shadow.get("ledger_provenance", {})
    validated_model_version = str(summary.get("model_version") or "")
    shadow_model_version = str(shadow.get("model_version") or "")
    configured_costs = {
        parsed
        for config in summary.get("current_shadow_configs", [])
        if (
            parsed := finite_non_negative_float(
                config.get("transaction_cost_bps")
            )
        ) is not None
    }
    validated_transaction_cost_bps = finite_non_negative_float(
        summary.get("transaction_cost_bps")
    )
    if validated_transaction_cost_bps is None and len(configured_costs) == 1:
        validated_transaction_cost_bps = configured_costs.pop()
    shadow_transaction_cost_bps = finite_non_negative_float(
        shadow.get("transaction_cost_bps")
    )
    validated_strategy_sha256 = sha256_string(
        summary.get("input_fingerprints", {})
        .get("strategy_code", {})
        .get("sha256")
    )
    shadow_strategy_sha256 = sha256_string(
        shadow.get("strategy_sha256")
    )
    checks = {
        "point_in_time_universe": bool(point_in_time_universe),
        "selected_position_terminal_returns_complete": bool(
            selected_terminal_returns_complete
        ),
        "historical_replay_disclosed_as_retrospective": (
            summary.get("historical_evidence_class")
            == "RETROSPECTIVE_IN_SAMPLE"
        ),
        "transaction_cost_stress_passed": bool(summary.get("transaction_cost_stress_passed")),
        "contiguous_forward_sessions_at_least_252": (
            contiguous_forward_sessions >= MIN_CONTIGUOUS_SESSIONS
        ),
        "contiguous_completed_forward_periods_at_least_12": (
            contiguous_completed_periods >= MIN_COMPLETED_MONTHLY_PERIODS
        ),
        "strict_majority_contiguous_periods_beat_nasdaq": (
            contiguous_period_win_rate is not None
            and contiguous_period_win_rate > 0.5
        ),
        "shadow_uses_self_financing_fixed_positions": (
            accounting_method == "self_financing_fixed_positions"
        ),
        "shadow_model_matches_validated_model": (
            bool(validated_model_version)
            and shadow_model_version == validated_model_version
        ),
        "shadow_transaction_cost_matches_validated_model": (
            validated_transaction_cost_bps is not None
            and shadow_transaction_cost_bps is not None
            and bool(np.isclose(
                shadow_transaction_cost_bps,
                validated_transaction_cost_bps,
                rtol=0,
                atol=1e-12,
            ))
        ),
        "shadow_benchmark_matches_validated_policy": (
            shadow.get("benchmark_id") == BENCHMARK_ID
            and shadow.get("benchmark_return_series")
            == BENCHMARK_RETURN_SERIES
        ),
        "shadow_strategy_matches_validated_strategy": (
            validated_strategy_sha256 is not None
            and shadow_strategy_sha256 == validated_strategy_sha256
        ),
        "shadow_price_adjustment_matches_validated_policy": (
            shadow.get("price_adjustment_policy")
            == PRICE_ADJUSTMENT_POLICY
        ),
        "shadow_ledger_integrity_verified": bool(
            ledger_provenance.get("integrity_verified")
        ),
        "shadow_ledger_externally_anchored": bool(
            ledger_provenance.get("externally_anchored")
            if REQUIRE_EXTERNAL_ANCHOR else True
        ),
        "all_contiguous_forward_periods_externally_anchored": bool(
            shadow.get(
                "all_contiguous_forward_periods_externally_anchored"
            ) if REQUIRE_EXTERNAL_ANCHOR else True
        ),
        "contiguous_forward_excess_positive": (
            contiguous_strategy is not None
            and contiguous_benchmark is not None
            and contiguous_strategy > contiguous_benchmark
        ) if REQUIRE_POSITIVE_EXCESS else True,
    }
    failed_checks = [
        name for name, passed in checks.items() if not passed
    ]
    static_check_names = {
        "point_in_time_universe",
        "selected_position_terminal_returns_complete",
        "historical_replay_disclosed_as_retrospective",
        "transaction_cost_stress_passed",
    }
    integrity_check_names = {
        "shadow_uses_self_financing_fixed_positions",
        "shadow_model_matches_validated_model",
        "shadow_transaction_cost_matches_validated_model",
        "shadow_benchmark_matches_validated_policy",
        "shadow_strategy_matches_validated_strategy",
        "shadow_price_adjustment_matches_validated_policy",
        "shadow_ledger_integrity_verified",
        "shadow_ledger_externally_anchored",
        "all_contiguous_forward_periods_externally_anchored",
    }
    forward_check_names = set(checks) - static_check_names - integrity_check_names
    static_failed = [
        name for name in checks
        if name in static_check_names and not checks[name]
    ]
    integrity_failed = [
        name for name in checks
        if name in integrity_check_names and not checks[name]
    ]
    forward_failed = [
        name for name in checks
        if name in forward_check_names and not checks[name]
    ]
    contiguous_wins_raw = shadow.get(
        "contiguous_completed_period_wins_vs_nasdaq"
    )
    contiguous_wins = (
        int(contiguous_wins_raw)
        if contiguous_wins_raw is not None
        else int(round(
            (contiguous_period_win_rate or 0)
            * contiguous_completed_periods
        ))
    )
    return {
        "release_status": "PASS" if all(checks.values()) else "BLOCKED",
        "checks": checks,
        "failed_checks": failed_checks,
        "blocker_classes": {
            "static_research": static_failed,
            "evidence_integrity": integrity_failed,
            "forward_time_and_performance": forward_failed,
        },
        "static_research_context": {
            "historical_data_status": summary.get(
                "historical_data_status"
            ),
            "historical_benchmark_calendar": summary.get(
                "historical_benchmark_calendar"
            ),
            "historical_temporal_security_type_filter": summary.get(
                "historical_temporal_security_type_filter"
            ),
            "historical_missing_price_symbols": summary.get(
                "historical_missing_price_symbols"
            ),
            "historical_minimum_usable_pit_financial_growth_coverage": (
                summary.get(
                    "historical_minimum_usable_pit_financial_growth_coverage"
                )
            ),
            "historical_technical_candidate_financial_coverage": summary.get(
                "historical_technical_candidate_financial_coverage"
            ),
            "historical_missing_usable_pit_financial_growth_symbols": (
                summary.get(
                    "historical_missing_usable_pit_financial_growth_symbols"
                )
            ),
            "historical_missing_no_raw_pit_financial_facts_symbols": (
                summary.get(
                    "historical_missing_no_raw_pit_financial_facts_symbols"
                )
            ),
            "historical_missing_insufficient_financial_history_symbols": (
                summary.get(
                    "historical_missing_insufficient_financial_history_symbols"
                )
            ),
            "historical_missing_stale_financial_growth_symbols": (
                summary.get(
                    "historical_missing_stale_financial_growth_symbols"
                )
            ),
            "historical_financial_gap_observations": summary.get(
                "historical_financial_gap_observations"
            ),
            "historical_missing_price_symbols_without_pit_financial_data": (
                summary.get(
                    "historical_missing_price_symbols_without_pit_financial_data"
                )
            ),
            "historical_missing_price_symbols_never_with_pit_financial_data": (
                summary.get(
                    "historical_missing_price_symbols_never_with_pit_financial_data"
                )
            ),
            "historical_missing_price_symbols_with_mixed_pit_financial_coverage": (
                summary.get(
                    "historical_missing_price_symbols_with_mixed_pit_financial_coverage"
                )
            ),
            "historical_pit_gap_priority_method": summary.get(
                "historical_pit_gap_priority_method"
            ),
            "historical_pit_gap_priority_top20": summary.get(
                "historical_pit_gap_priority_top20"
            ),
            "historical_pit_gap_recovery_priority_method": summary.get(
                "historical_pit_gap_recovery_priority_method"
            ),
            "historical_pit_gap_recovery_top20": summary.get(
                "historical_pit_gap_recovery_top20"
            ),
            "historical_observable_missing_price_detail_rows": summary.get(
                "historical_observable_missing_price_detail_rows"
            ),
            "historical_observable_missing_price_by_ticker": summary.get(
                "historical_observable_missing_price_by_ticker"
            ),
            "historical_maximum_signal_snapshot_age_days": summary.get(
                "historical_maximum_signal_snapshot_age_days"
            ),
            "historical_allowed_signal_snapshot_age_days": summary.get(
                "historical_allowed_signal_snapshot_age_days"
            ),
            "historical_stale_signal_snapshot_dates": summary.get(
                "historical_stale_signal_snapshot_dates"
            ),
            "historical_stale_snapshot_selection_diagnostics": summary.get(
                "historical_stale_snapshot_selection_diagnostics"
            ),
            "historical_unresolved_terminal_returns": summary.get(
                "historical_unresolved_terminal_returns"
            ),
            "unresolved_terminal_returns_affecting_traded_symbols": (
                summary.get(
                    "unresolved_terminal_returns_affecting_traded_symbols"
                )
            ),
            "transaction_cost_stress_definition": summary.get(
                "transaction_cost_stress_definition"
            ),
            "cost_stress_wins": summary.get("cost_stress_wins"),
            "transaction_cost_stress_diagnostics": summary.get(
                "transaction_cost_stress_diagnostics"
            ),
        },
        "waiting_only_is_sufficient": bool(
            failed_checks and not static_failed and not integrity_failed
        ),
        "forward_requirements_remaining": {
            "contiguous_sessions": max(
                MIN_CONTIGUOUS_SESSIONS - contiguous_forward_sessions, 0
            ),
            "completed_monthly_periods": max(
                MIN_COMPLETED_MONTHLY_PERIODS - contiguous_completed_periods, 0
            ),
            "winning_completed_periods": max(MIN_WINNING_PERIODS - contiguous_wins, 0),
            "positive_contiguous_excess_required": not checks[
                "contiguous_forward_excess_positive"
            ],
        },
        "earliest_release_date": None,
        "earliest_release_date_reason": (
            "STATIC_OR_INTEGRITY_BLOCKERS_MUST_BE_RESOLVED_FIRST"
            if static_failed or integrity_failed
            else "REQUIRES_PRECOMMITTED_SHADOW_POLICY"
        ),
        "live_order_submission_supported": False,
        "informational_metrics_not_used_for_release": {
            "historical_years": summary.get("historical_years"),
            "historical_wins_vs_nasdaq": summary.get("wins_vs_nasdaq"),
            "minimum_historical_excess": summary.get(
                "minimum_historical_excess"
            ),
            "bootstrap_ci_95_low": summary.get("bootstrap_ci_95_low"),
            "bootstrap_probability_nonpositive": summary.get(
                "bootstrap_probability_nonpositive"
            ),
            "observed_forward_periods": forward_periods,
            "completed_forward_periods": completed_forward_periods,
            "completed_period_win_rate": completed_period_win_rate,
            "total_forward_sessions": shadow_sessions,
            "contiguous_forward_sessions": contiguous_forward_sessions,
            "contiguous_completed_forward_periods": (
                contiguous_completed_periods
            ),
            "contiguous_completed_period_win_rate": (
                contiguous_period_win_rate
            ),
            "evidence_gap_count": shadow.get("evidence_gap_count", 0),
            "total_forward_strategy_return": forward_strategy,
            "total_forward_benchmark_return": forward_benchmark,
            "contiguous_forward_strategy_return": contiguous_strategy,
            "contiguous_forward_benchmark_return": contiguous_benchmark,
        },
        "reason": (
            "Every release check must pass. Retrospective win counts and "
            "bootstrap statistics are informational only; genuine performance "
            "evidence comes from the externally anchored shadow record. "
            "Changing the model resets the shadow session and completed-period "
            "clocks."
        ),
    }


def write_release_gate(result: dict, output_file: str | Path) -> Path:
    output = Path(output_file)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--summary",
        default="output/can_slim_fixed_top3_summary.json",
    )
    parser.add_argument(
        "--shadow",
        default=(
            "output/daily/can-slim-top3-v1/"
            "shadow_evaluation.json"
        ),
    )
    parser.add_argument(
        "--output",
        default="output/daily/can-slim-top3-v1/release_gate.json",
    )
    args = parser.parse_args()
    artifact_verification = verify_validation_artifact_manifest(
        Path(args.summary).parent
    )
    summary = json.loads(
        Path(args.summary).read_text()
    )
    shadow_path = Path(args.shadow)
    shadow = json.loads(shadow_path.read_text()) if shadow_path.exists() else None
    sessions = int((shadow or {}).get("forward_sessions", 0))
    result = evaluate_release_gate(summary, sessions, shadow)
    result["validation_artifact_manifest"] = artifact_verification
    write_release_gate(result, args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
