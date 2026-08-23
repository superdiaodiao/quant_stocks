"""Deterministic 13/26/39-week forward review gate."""

from __future__ import annotations

import numpy as np


CANARY_WEEKS = 13
EARLY_PROMOTION_WEEKS = 26
FINAL_DECISION_WEEKS = 39
MAXIMUM_DRAWDOWN_LIMIT = 0.40
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_BLOCK_WEEKS = 4
CONFIDENCE_QUANTILE = 0.10


def _compound(values: np.ndarray) -> float:
    return float(np.prod(1.0 + values) - 1.0)


def _maximum_drawdown(values: np.ndarray) -> float:
    wealth = np.cumprod(1.0 + values)
    if not len(wealth):
        return 0.0
    peaks = np.maximum.accumulate(np.concatenate(([1.0], wealth)))[:-1]
    return float(np.min(wealth / peaks - 1.0))


def bootstrap_lower_bound(
    weekly_excess_returns: list[float] | np.ndarray,
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = 20260810,
) -> float:
    values = np.asarray(weekly_excess_returns, dtype=float)
    if len(values) < BOOTSTRAP_BLOCK_WEEKS:
        raise ValueError("at least four weekly marks are required")
    if not np.isfinite(values).all() or (values <= -1).any():
        raise ValueError("weekly excess returns must be finite and above -100%")
    starts = np.arange(len(values) - BOOTSTRAP_BLOCK_WEEKS + 1)
    blocks = int(np.ceil(len(values) / BOOTSTRAP_BLOCK_WEEKS))
    rng = np.random.default_rng(seed)
    outcomes = np.empty(samples)
    for sample in range(samples):
        chosen = rng.choice(starts, size=blocks, replace=True)
        path = np.concatenate([
            values[start : start + BOOTSTRAP_BLOCK_WEEKS] for start in chosen
        ])[: len(values)]
        outcomes[sample] = _compound(path)
    return float(np.quantile(outcomes, CONFIDENCE_QUANTILE))


def evaluate_short_forward_gate(
    weekly_excess_returns: list[float] | np.ndarray,
    *,
    monthly_decisions: int,
    parameters_frozen: bool,
    manifest_valid: bool,
    selected_prices_complete: bool,
    delisting_values_complete: bool,
) -> dict:
    values = np.asarray(weekly_excess_returns, dtype=float)
    integrity = {
        "parameters_frozen": parameters_frozen,
        "manifest_valid": manifest_valid,
        "selected_prices_complete": selected_prices_complete,
        "delisting_values_complete": delisting_values_complete,
    }
    failed_integrity = [key for key, passed in integrity.items() if not passed]
    weeks = int(len(values))
    cumulative = _compound(values) if weeks else 0.0
    maximum_drawdown = _maximum_drawdown(values)
    result = {
        "observed_weeks": weeks,
        "monthly_decisions": int(monthly_decisions),
        "cumulative_excess_return": cumulative,
        "maximum_drawdown": maximum_drawdown,
        "bootstrap_90pct_lower_bound": None,
        "failed_integrity_checks": failed_integrity,
        "promotion_eligible": False,
        "limited_canary_eligible": False,
    }
    if failed_integrity:
        result.update({"status": "BLOCKED_INTEGRITY", "next_review_week": None})
        return result
    if maximum_drawdown < -MAXIMUM_DRAWDOWN_LIMIT:
        result.update({"status": "REJECTED_DRAWDOWN", "next_review_week": None})
        return result
    if weeks < CANARY_WEEKS or monthly_decisions < 3:
        result.update({"status": "ACCUMULATING_CANARY", "next_review_week": 13})
        return result
    result["limited_canary_eligible"] = cumulative > 0
    if weeks < EARLY_PROMOTION_WEEKS or monthly_decisions < 6:
        result.update({"status": "CANARY_REVIEW_ONLY", "next_review_week": 26})
        return result
    lower_bound = bootstrap_lower_bound(values)
    result["bootstrap_90pct_lower_bound"] = lower_bound
    if cumulative > 0 and lower_bound > 0:
        result.update({
            "status": "PROMOTION_REVIEW_ELIGIBLE",
            "next_review_week": None,
            "promotion_eligible": True,
        })
        return result
    if weeks < FINAL_DECISION_WEEKS or monthly_decisions < 9:
        result.update({"status": "INCONCLUSIVE_UNTIL_FINAL_REVIEW", "next_review_week": 39})
        return result
    result.update({"status": "REJECTED_INSUFFICIENT_FORWARD_EDGE", "next_review_week": None})
    return result
