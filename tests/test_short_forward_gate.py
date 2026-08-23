import numpy as np

from src.research.short_forward_gate import evaluate_short_forward_gate


INTEGRITY = {
    "parameters_frozen": True,
    "manifest_valid": True,
    "selected_prices_complete": True,
    "delisting_values_complete": True,
}


def test_13_weeks_can_only_unlock_limited_canary_review():
    result = evaluate_short_forward_gate(
        [0.01] * 13, monthly_decisions=3, **INTEGRITY
    )
    assert result["status"] == "CANARY_REVIEW_ONLY"
    assert result["limited_canary_eligible"] is True
    assert result["promotion_eligible"] is False
    assert result["next_review_week"] == 26


def test_26_weeks_can_promote_only_with_positive_lower_bound():
    result = evaluate_short_forward_gate(
        [0.01] * 26, monthly_decisions=6, **INTEGRITY
    )
    assert result["status"] == "PROMOTION_REVIEW_ELIGIBLE"
    assert result["bootstrap_90pct_lower_bound"] > 0
    assert result["promotion_eligible"] is True


def test_39_weeks_rejects_inconclusive_edge_instead_of_waiting_a_year():
    alternating = np.tile([0.01, -0.01], 20)[:39]
    result = evaluate_short_forward_gate(
        alternating, monthly_decisions=9, **INTEGRITY
    )
    assert result["status"] == "REJECTED_INSUFFICIENT_FORWARD_EDGE"
    assert result["promotion_eligible"] is False
    assert result["next_review_week"] is None


def test_integrity_failure_blocks_immediately():
    integrity = dict(INTEGRITY)
    integrity["manifest_valid"] = False
    result = evaluate_short_forward_gate(
        [0.01] * 26, monthly_decisions=6, **integrity
    )
    assert result["status"] == "BLOCKED_INTEGRITY"
    assert result["failed_integrity_checks"] == ["manifest_valid"]
