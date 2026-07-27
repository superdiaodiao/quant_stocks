"""Conservative release gate for the experimental pure-stock strategy."""

from __future__ import annotations

import json
from pathlib import Path


def evaluate_release_gate(
    summary: dict,
    shadow_sessions: int = 0,
    shadow_summary: dict | None = None,
) -> dict:
    point_in_time_universe = summary.get(
        "candidate_pool_is_point_in_time", summary.get("point_in_time_universe", False)
    )
    delisting_returns_complete = summary.get(
        "delisting_returns_complete",
        summary.get("observed_delisting_returns_complete", False),
    )
    oos_win_rate = summary.get(
        "out_of_sample_win_rate", summary.get("oos_win_rate", 0)
    )
    minimum_oos_excess = summary.get(
        "minimum_out_of_sample_excess", summary.get("minimum_oos_excess", -1)
    )
    shadow = shadow_summary or {}
    forward_strategy = shadow.get("forward_strategy_return")
    forward_benchmark = shadow.get("forward_benchmark_return")
    checks = {
        "point_in_time_universe": bool(point_in_time_universe),
        "observed_delisting_returns_complete": bool(delisting_returns_complete),
        "oos_win_rate_at_least_75pct": oos_win_rate >= 0.75,
        "positive_minimum_oos_year": minimum_oos_excess > 0,
        "bootstrap_95pct_low_positive": summary.get("bootstrap_ci_95_low", -1) > 0,
        "transaction_cost_stress_passed": bool(summary.get("transaction_cost_stress_passed")),
        "forward_shadow_sessions_at_least_252": shadow_sessions >= 252,
        "forward_shadow_excess_positive": (
            forward_strategy is not None
            and forward_benchmark is not None
            and forward_strategy > forward_benchmark
        ),
    }
    return {
        "release_status": "PASS" if all(checks.values()) else "BLOCKED",
        "checks": checks,
        "live_order_submission_supported": False,
        "reason": "Every check must pass; changing the model resets the shadow-session clock.",
    }


def main() -> None:
    summary = json.loads(
        Path("output/can_slim_fixed_top3_summary.json").read_text()
    )
    shadow_path = Path(
        "output/daily/can-slim-top3-v1/shadow_evaluation.json"
    )
    shadow = json.loads(shadow_path.read_text()) if shadow_path.exists() else None
    sessions = int((shadow or {}).get("forward_sessions", 0))
    print(json.dumps(evaluate_release_gate(summary, sessions, shadow), indent=2))


if __name__ == "__main__":
    main()
