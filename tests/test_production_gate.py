from src.research.production_gate import evaluate_release_gate


def test_release_gate_blocks_optimistic_but_incomplete_evidence():
    result = evaluate_release_gate({
        "candidate_pool_is_point_in_time": False,
        "delisting_returns_complete": False,
        "out_of_sample_win_rate": 0.75,
        "minimum_out_of_sample_excess": -0.01,
        "bootstrap_ci_95_low": -0.02,
        "transaction_cost_stress_passed": False,
    }, shadow_sessions=251)
    assert result["release_status"] == "BLOCKED"
    assert not result["live_order_submission_supported"]


def test_release_gate_requires_every_check():
    result = evaluate_release_gate({
        "candidate_pool_is_point_in_time": True,
        "delisting_returns_complete": True,
        "out_of_sample_win_rate": 0.75,
        "minimum_out_of_sample_excess": 0.01,
        "bootstrap_ci_95_low": 0.001,
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=252, shadow_summary={
        "forward_strategy_return": 0.20,
        "forward_benchmark_return": 0.10,
    })
    assert result["release_status"] == "PASS"


def test_release_gate_accepts_factor_validation_key_names():
    result = evaluate_release_gate({
        "point_in_time_universe": True,
        "observed_delisting_returns_complete": True,
        "oos_win_rate": 0.75,
        "minimum_oos_excess": 0.01,
        "bootstrap_ci_95_low": 0.001,
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=252, shadow_summary={
        "forward_strategy_return": 0.20,
        "forward_benchmark_return": 0.10,
    })
    assert result["release_status"] == "PASS"


def test_release_gate_blocks_forward_underperformance_after_252_sessions():
    result = evaluate_release_gate({
        "candidate_pool_is_point_in_time": True,
        "delisting_returns_complete": True,
        "out_of_sample_win_rate": 0.75,
        "minimum_out_of_sample_excess": 0.01,
        "bootstrap_ci_95_low": 0.001,
        "transaction_cost_stress_passed": True,
    }, shadow_sessions=252, shadow_summary={
        "forward_strategy_return": 0.05,
        "forward_benchmark_return": 0.10,
    })
    assert result["release_status"] == "BLOCKED"
