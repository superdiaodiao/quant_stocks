import json

from src.research.production_gate import (
    evaluate_release_gate,
    write_release_gate,
)


VERIFIED_LEDGER = {
    "integrity_verified": True,
    "externally_anchored": True,
}
STRATEGY_SHA256 = "a" * 64


def test_release_gate_report_is_written_atomically(tmp_path):
    output = tmp_path / "release_gate.json"
    write_release_gate({"release_status": "BLOCKED"}, output)

    assert json.loads(output.read_text()) == {
        "release_status": "BLOCKED"
    }
    assert not output.with_suffix(".json.tmp").exists()


def test_release_gate_blocks_optimistic_but_incomplete_evidence():
    result = evaluate_release_gate({
        "model_version": "can-slim-top3-v1",
        "candidate_pool_is_point_in_time": False,
        "delisting_returns_complete": False,
        "out_of_sample_win_rate": 0.75,
        "minimum_out_of_sample_excess": -0.01,
        "bootstrap_ci_95_low": -0.02,
        "transaction_cost_stress_passed": False,
    }, shadow_sessions=251)
    assert result["release_status"] == "BLOCKED"
    assert not result["live_order_submission_supported"]
    assert result["blocker_classes"]["static_research"] == [
        "point_in_time_universe",
        "selected_position_terminal_returns_complete",
        "historical_replay_disclosed_as_retrospective",
        "transaction_cost_stress_passed",
    ]
    assert result["waiting_only_is_sufficient"] is False
    assert result["forward_requirements_remaining"] == {
        "contiguous_sessions": 252,
        "completed_monthly_periods": 12,
        "winning_completed_periods": 7,
        "positive_contiguous_excess_required": True,
    }
    assert (
        result["earliest_release_date_reason"]
        == "STATIC_OR_INTEGRITY_BLOCKERS_MUST_BE_RESOLVED_FIRST"
    )


def test_release_gate_rejects_listed_symbol_with_ended_price_history():
    result = evaluate_release_gate({
        "model_version": "can-slim-top3-v1",
        "candidate_pool_is_point_in_time": True,
        "historical_data_checks": {
            "listed_price_histories_complete": False,
        },
    })

    assert result["checks"]["point_in_time_universe"] is False


def test_release_gate_rejects_historical_quarterly_value_conflicts():
    result = evaluate_release_gate({
        "model_version": "can-slim-top3-v1",
        "candidate_pool_is_point_in_time": True,
        "historical_data_checks": {
            "historical_quarterly_value_conflicts_absent": False,
        },
    })

    assert result["checks"]["point_in_time_universe"] is False


def test_release_gate_rejects_shadow_from_another_model():
    result = evaluate_release_gate(
        {"model_version": "can-slim-top3-v1"},
        shadow_summary={"model_version": "other-model"},
    )

    assert result["checks"][
        "shadow_model_matches_validated_model"
    ] is False
    assert "shadow_model_matches_validated_model" in (
        result["blocker_classes"]["evidence_integrity"]
    )


def test_release_gate_rejects_wrong_shadow_transaction_cost():
    result = evaluate_release_gate(
        {
            "model_version": "can-slim-top3-v1",
            "current_shadow_configs": [{
                "transaction_cost_bps": 10.0,
            }],
        },
        shadow_summary={
            "model_version": "can-slim-top3-v1",
            "transaction_cost_bps": 0.0,
        },
    )

    assert result["checks"][
        "shadow_transaction_cost_matches_validated_model"
    ] is False
    assert "shadow_transaction_cost_matches_validated_model" in (
        result["blocker_classes"]["evidence_integrity"]
    )


def test_release_gate_rejects_wrong_shadow_benchmark():
    result = evaluate_release_gate(
        {"model_version": "can-slim-top3-v1"},
        shadow_summary={
            "model_version": "can-slim-top3-v1",
            "benchmark_id": "qqq",
            "benchmark_return_series": "adjusted-close",
        },
    )

    assert result["checks"][
        "shadow_benchmark_matches_validated_policy"
    ] is False
    assert "shadow_benchmark_matches_validated_policy" in (
        result["blocker_classes"]["evidence_integrity"]
    )


def test_release_gate_rejects_wrong_strategy_fingerprint():
    result = evaluate_release_gate(
        {
            "model_version": "can-slim-top3-v1",
            "input_fingerprints": {
                "strategy_code": {"sha256": STRATEGY_SHA256},
            },
        },
        shadow_summary={
            "model_version": "can-slim-top3-v1",
            "strategy_sha256": "b" * 64,
        },
    )

    assert result["checks"][
        "shadow_strategy_matches_validated_strategy"
    ] is False
    assert "shadow_strategy_matches_validated_strategy" in (
        result["blocker_classes"]["evidence_integrity"]
    )


def test_release_gate_requires_every_check():
    result = evaluate_release_gate({
        "model_version": "can-slim-top3-v1",
        "transaction_cost_bps": 10.0,
        "input_fingerprints": {
            "strategy_code": {"sha256": STRATEGY_SHA256},
        },
        "candidate_pool_is_point_in_time": True,
        "delisting_returns_complete": True,
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "out_of_sample_win_rate": 0.0,
        "minimum_out_of_sample_excess": -1.0,
        "bootstrap_ci_95_low": -99.0,
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=252, shadow_summary={
        "model_version": "can-slim-top3-v1",
        "transaction_cost_bps": 10.0,
        "benchmark_id": "nasdaq-composite",
        "benchmark_return_series": "close-price-index",
        "price_adjustment_policy": (
            "confirmed-actions-plus-common-split-heuristic"
        ),
        "strategy_sha256": STRATEGY_SHA256,
        "forward_strategy_return": 0.20,
        "forward_benchmark_return": 0.10,
        "contiguous_forward_strategy_return": 0.20,
        "contiguous_forward_benchmark_return": 0.10,
        "forward_periods": 12,
        "completed_forward_periods": 12,
        "completed_period_win_rate": 7 / 12,
        "contiguous_forward_sessions": 252,
        "contiguous_completed_forward_periods": 12,
        "contiguous_completed_period_win_rate": 7 / 12,
        "accounting_method": "self_financing_fixed_positions",
        "ledger_provenance": VERIFIED_LEDGER,
        "all_forward_periods_externally_anchored": True,
        "all_contiguous_forward_periods_externally_anchored": True,
    })
    assert result["release_status"] == "PASS"
    assert (
        result["informational_metrics_not_used_for_release"][
            "bootstrap_ci_95_low"
        ]
        == -99.0
    )


def test_release_gate_accepts_factor_validation_key_names():
    result = evaluate_release_gate({
        "model_version": "can-slim-top3-v1",
        "transaction_cost_bps": 10.0,
        "input_fingerprints": {
            "strategy_code": {"sha256": STRATEGY_SHA256},
        },
        "point_in_time_universe": True,
        "observed_delisting_returns_complete": True,
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "oos_win_rate": 0.75,
        "minimum_oos_excess": 0.01,
        "bootstrap_ci_95_low": 0.001,
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=252, shadow_summary={
        "model_version": "can-slim-top3-v1",
        "transaction_cost_bps": 10.0,
        "benchmark_id": "nasdaq-composite",
        "benchmark_return_series": "close-price-index",
        "price_adjustment_policy": (
            "confirmed-actions-plus-common-split-heuristic"
        ),
        "strategy_sha256": STRATEGY_SHA256,
        "forward_strategy_return": 0.20,
        "forward_benchmark_return": 0.10,
        "contiguous_forward_strategy_return": 0.20,
        "contiguous_forward_benchmark_return": 0.10,
        "forward_periods": 12,
        "completed_forward_periods": 12,
        "completed_period_win_rate": 7 / 12,
        "contiguous_forward_sessions": 252,
        "contiguous_completed_forward_periods": 12,
        "contiguous_completed_period_win_rate": 7 / 12,
        "accounting_method": "self_financing_fixed_positions",
        "ledger_provenance": VERIFIED_LEDGER,
        "all_forward_periods_externally_anchored": True,
        "all_contiguous_forward_periods_externally_anchored": True,
    })
    assert result["release_status"] == "PASS"


def test_release_gate_reads_nested_historical_data_checks():
    result = evaluate_release_gate({
        "historical_data_checks": {
            "point_in_time_membership_complete": True,
            "signal_member_prices_complete": True,
            "signal_member_financials_complete": True,
            "signal_technical_candidate_financials_complete": True,
            "observed_delisting_returns_complete": True,
        },
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "transaction_cost_stress_passed": False,
    })

    assert result["checks"]["point_in_time_universe"] is True
    assert result["checks"][
        "selected_position_terminal_returns_complete"
    ] is True
    assert result["blocker_classes"]["static_research"] == [
        "transaction_cost_stress_passed"
    ]


def test_release_gate_rejects_missing_or_incomplete_candidate_financials():
    base = {
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "transaction_cost_stress_passed": True,
    }
    for financial_check in (None, False):
        checks = {
            "benchmark_calendar_complete": True,
            "point_in_time_membership_complete": True,
            "signal_member_prices_complete": True,
            "observed_delisting_returns_complete": True,
        }
        if financial_check is not None:
            checks[
                "signal_technical_candidate_financials_complete"
            ] = financial_check
        result = evaluate_release_gate({
            **base,
            "historical_data_checks": checks,
        })
        assert result["checks"]["point_in_time_universe"] is False


def test_release_gate_rejects_incomplete_historical_benchmark_calendar():
    result = evaluate_release_gate({
        "historical_data_checks": {
            "benchmark_calendar_complete": False,
            "point_in_time_membership_complete": True,
            "signal_member_prices_complete": True,
            "signal_member_financials_complete": True,
            "signal_technical_candidate_financials_complete": True,
            "observed_delisting_returns_complete": True,
        },
        "historical_benchmark_calendar": {
            "complete": False,
            "missing_sessions": ["2025-06-17"],
        },
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "transaction_cost_stress_passed": True,
    })

    assert result["checks"]["point_in_time_universe"] is False
    assert result["blocker_classes"]["static_research"] == [
        "point_in_time_universe"
    ]
    assert result["static_research_context"][
        "historical_benchmark_calendar"
    ]["missing_sessions"] == ["2025-06-17"]


def test_release_gate_separates_market_wide_from_traded_terminal_gaps():
    result = evaluate_release_gate({
        "historical_data_checks": {
            "point_in_time_membership_complete": False,
            "signal_member_prices_complete": False,
            "observed_delisting_returns_complete": False,
        },
        "selected_position_terminal_returns_complete": True,
        "historical_unresolved_terminal_returns": 95,
        "unresolved_terminal_returns_affecting_traded_symbols": [],
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "transaction_cost_stress_passed": False,
    })

    assert result["checks"][
        "selected_position_terminal_returns_complete"
    ] is True
    assert (
        "selected_position_terminal_returns_complete"
        not in result["blocker_classes"]["static_research"]
    )
    assert (
        result["static_research_context"][
            "historical_unresolved_terminal_returns"
        ]
        == 95
    )


def test_release_gate_rejects_undisclosed_historical_evidence_class():
    result = evaluate_release_gate({
        "candidate_pool_is_point_in_time": True,
        "delisting_returns_complete": True,
        "out_of_sample_win_rate": 1.0,
        "minimum_out_of_sample_excess": 1.0,
        "bootstrap_ci_95_low": 1.0,
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=252, shadow_summary={
        "forward_strategy_return": 0.20,
        "forward_benchmark_return": 0.10,
        "contiguous_forward_strategy_return": 0.20,
        "contiguous_forward_benchmark_return": 0.10,
        "forward_periods": 12,
        "completed_forward_periods": 12,
        "completed_period_win_rate": 7 / 12,
        "contiguous_forward_sessions": 252,
        "contiguous_completed_forward_periods": 12,
        "contiguous_completed_period_win_rate": 7 / 12,
        "accounting_method": "self_financing_fixed_positions",
        "ledger_provenance": VERIFIED_LEDGER,
        "all_forward_periods_externally_anchored": True,
        "all_contiguous_forward_periods_externally_anchored": True,
    })

    assert result["release_status"] == "BLOCKED"
    assert (
        "historical_replay_disclosed_as_retrospective"
        in result["failed_checks"]
    )


def test_release_gate_blocks_forward_underperformance_after_252_sessions():
    result = evaluate_release_gate({
        "candidate_pool_is_point_in_time": True,
        "delisting_returns_complete": True,
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "out_of_sample_win_rate": 0.75,
        "minimum_out_of_sample_excess": 0.01,
        "bootstrap_ci_95_low": 0.001,
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=252, shadow_summary={
        "forward_strategy_return": 0.05,
        "forward_benchmark_return": 0.10,
        "contiguous_forward_strategy_return": 0.05,
        "contiguous_forward_benchmark_return": 0.10,
        "forward_periods": 12,
        "completed_forward_periods": 12,
        "completed_period_win_rate": 7 / 12,
        "contiguous_forward_sessions": 252,
        "contiguous_completed_forward_periods": 12,
        "contiguous_completed_period_win_rate": 7 / 12,
        "accounting_method": "self_financing_fixed_positions",
        "ledger_provenance": VERIFIED_LEDGER,
        "all_forward_periods_externally_anchored": True,
        "all_contiguous_forward_periods_externally_anchored": True,
    })
    assert result["release_status"] == "BLOCKED"


def test_release_gate_rejects_one_stale_portfolio_held_for_a_year():
    result = evaluate_release_gate({
        "candidate_pool_is_point_in_time": True,
        "delisting_returns_complete": True,
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "out_of_sample_win_rate": 0.75,
        "minimum_out_of_sample_excess": 0.01,
        "bootstrap_ci_95_low": 0.001,
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=252, shadow_summary={
        "forward_strategy_return": 0.20,
        "forward_benchmark_return": 0.10,
        "contiguous_forward_strategy_return": 0.20,
        "contiguous_forward_benchmark_return": 0.10,
        "forward_periods": 1,
        "completed_forward_periods": 1,
        "completed_period_win_rate": 1.0,
        "contiguous_forward_sessions": 252,
        "contiguous_completed_forward_periods": 1,
        "contiguous_completed_period_win_rate": 1.0,
        "accounting_method": "self_financing_fixed_positions",
        "ledger_provenance": VERIFIED_LEDGER,
        "all_forward_periods_externally_anchored": True,
        "all_contiguous_forward_periods_externally_anchored": True,
    })

    assert result["release_status"] == "BLOCKED"
    assert (
        "contiguous_completed_forward_periods_at_least_12"
        in result["failed_checks"]
    )
    assert result["checks"]["contiguous_forward_excess_positive"] is True


def test_release_gate_rejects_unanchored_forward_ledger():
    result = evaluate_release_gate({
        "candidate_pool_is_point_in_time": True,
        "delisting_returns_complete": True,
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "out_of_sample_win_rate": 0.75,
        "minimum_out_of_sample_excess": 0.01,
        "bootstrap_ci_95_low": 0.001,
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=252, shadow_summary={
        "forward_strategy_return": 0.20,
        "forward_benchmark_return": 0.10,
        "contiguous_forward_strategy_return": 0.20,
        "contiguous_forward_benchmark_return": 0.10,
        "forward_periods": 12,
        "completed_forward_periods": 12,
        "completed_period_win_rate": 7 / 12,
        "contiguous_forward_sessions": 252,
        "contiguous_completed_forward_periods": 12,
        "contiguous_completed_period_win_rate": 7 / 12,
        "accounting_method": "self_financing_fixed_positions",
        "ledger_provenance": {
            "integrity_verified": True,
            "externally_anchored": False,
        },
        "all_forward_periods_externally_anchored": True,
        "all_contiguous_forward_periods_externally_anchored": True,
    })

    assert result["release_status"] == "BLOCKED"
    assert (
        "shadow_ledger_externally_anchored"
        in result["failed_checks"]
    )


def test_release_gate_rejects_mixed_anchored_and_legacy_periods():
    result = evaluate_release_gate({
        "candidate_pool_is_point_in_time": True,
        "delisting_returns_complete": True,
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "out_of_sample_win_rate": 0.75,
        "minimum_out_of_sample_excess": 0.01,
        "bootstrap_ci_95_low": 0.001,
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=252, shadow_summary={
        "forward_strategy_return": 0.20,
        "forward_benchmark_return": 0.10,
        "contiguous_forward_strategy_return": 0.20,
        "contiguous_forward_benchmark_return": 0.10,
        "forward_periods": 12,
        "completed_forward_periods": 12,
        "completed_period_win_rate": 7 / 12,
        "contiguous_forward_sessions": 252,
        "contiguous_completed_forward_periods": 12,
        "contiguous_completed_period_win_rate": 7 / 12,
        "accounting_method": "self_financing_fixed_positions",
        "ledger_provenance": VERIFIED_LEDGER,
        "all_forward_periods_externally_anchored": False,
        "all_contiguous_forward_periods_externally_anchored": False,
    })

    assert result["release_status"] == "BLOCKED"
    assert (
        "all_contiguous_forward_periods_externally_anchored"
        in result["failed_checks"]
    )


def test_release_gate_rejects_one_big_month_masking_most_monthly_losses():
    result = evaluate_release_gate({
        "candidate_pool_is_point_in_time": True,
        "delisting_returns_complete": True,
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=252, shadow_summary={
        "forward_strategy_return": 0.20,
        "forward_benchmark_return": 0.10,
        "contiguous_forward_strategy_return": 0.20,
        "contiguous_forward_benchmark_return": 0.10,
        "forward_periods": 13,
        "completed_forward_periods": 12,
        "completed_period_win_rate": 1 / 12,
        "contiguous_forward_sessions": 252,
        "contiguous_completed_forward_periods": 12,
        "contiguous_completed_period_win_rate": 1 / 12,
        "accounting_method": "self_financing_fixed_positions",
        "ledger_provenance": VERIFIED_LEDGER,
        "all_forward_periods_externally_anchored": True,
        "all_contiguous_forward_periods_externally_anchored": True,
    })

    assert result["release_status"] == "BLOCKED"
    assert (
        "strict_majority_contiguous_periods_beat_nasdaq"
        in result["failed_checks"]
    )


def test_release_gate_resets_evidence_clock_after_monthly_gap():
    result = evaluate_release_gate({
        "candidate_pool_is_point_in_time": True,
        "delisting_returns_complete": True,
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=400, shadow_summary={
        "forward_strategy_return": 0.20,
        "forward_benchmark_return": 0.10,
        "contiguous_forward_strategy_return": -0.05,
        "contiguous_forward_benchmark_return": 0.02,
        "forward_periods": 15,
        "completed_forward_periods": 14,
        "completed_period_win_rate": 0.75,
        "contiguous_forward_sessions": 20,
        "contiguous_completed_forward_periods": 1,
        "contiguous_completed_period_win_rate": 1.0,
        "accounting_method": "self_financing_fixed_positions",
        "ledger_provenance": VERIFIED_LEDGER,
        "all_contiguous_forward_periods_externally_anchored": True,
        "evidence_gap_count": 1,
    })

    assert result["release_status"] == "BLOCKED"
    assert "contiguous_forward_sessions_at_least_252" in result[
        "failed_checks"
    ]
    assert (
        "contiguous_completed_forward_periods_at_least_12"
        in result["failed_checks"]
    )
