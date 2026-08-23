#!/usr/bin/env python3
"""Blend v6 defense and v7 QQQ alpha as monthly self-financing sleeves.

The family and primary weights were selected after inspecting the same history.
This is retrospective research, not independent forward evidence or trading
authorization.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pandas as pd


V6_DAILY = Path("output/research_v6_walkforward_defensive_ensemble_50bps_daily.csv")
V7_STOCK_DAILY = Path(
    "output/research_v7_qqq_targeted_core_satellite_stock_sleeve_10bps_daily.csv"
)
QQQ_PATH = Path("output/research_only/qqq_nasdaq_history.csv")
PREFIX = Path("output/research_v8_monthly_risk_budget_blend")
V7_STOCK_WEIGHT = 0.40
V7_QQQ_WEIGHT = 0.60
V7_CAPITAL_WEIGHT = 0.75
V6_CAPITAL_WEIGHT = 0.25
UNDERLYING_COST_BPS = 50.0
SLEEVE_TRANSFER_COST_BPS = 50.0


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


V6 = _load_module("research_v6", "scripts/research_v6_walkforward_defensive_ensemble.py")
V7 = _load_module("research_v7", "scripts/research_v7_qqq_targeted_core_satellite.py")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def combine_monthly_sleeves(
    v6: pd.DataFrame,
    v7: pd.DataFrame,
    *,
    v7_weight: float = V7_CAPITAL_WEIGHT,
    transfer_cost_bps: float = SLEEVE_TRANSFER_COST_BPS,
) -> pd.DataFrame:
    if not 0 < v7_weight < 1:
        raise ValueError("v7_weight must be in (0, 1)")
    if transfer_cost_bps < 0:
        raise ValueError("transfer_cost_bps must be non-negative")
    index = v6.index.intersection(v7.index)
    if index.empty:
        raise ValueError("v6 and v7 have no overlapping sessions")
    left = v6.reindex(index)
    right = v7.reindex(index)
    if left["return"].isna().any() or right["return"].isna().any():
        raise ValueError("sleeve return series is incomplete")
    periods = index.to_period("M")
    nav = 1.0
    v6_value = v7_value = 0.0
    previous_period = None
    rows = []
    for position, stamp in enumerate(index):
        if position:
            v6_value *= 1.0 + float(left.loc[stamp, "return"])
            v7_value *= 1.0 + float(right.loc[stamp, "return"])
            nav = v6_value + v7_value
        turnover = cost = 0.0
        if periods[position] != previous_period:
            target_v7 = nav * v7_weight
            target_v6 = nav - target_v7
            turnover = abs(target_v6 - v6_value) + abs(target_v7 - v7_value)
            cost = turnover * transfer_cost_bps / 10_000.0
            nav -= cost
            v7_value = nav * v7_weight
            v6_value = nav - v7_value
        nav = v6_value + v7_value
        rows.append({
            "date": stamp,
            "nav": nav,
            "v6_value": v6_value,
            "v7_value": v7_value,
            "sleeve_turnover": turnover,
            "sleeve_transfer_cost": cost,
        })
        previous_period = periods[position]
    result = pd.DataFrame(rows).set_index("date")
    result["return"] = result["nav"].pct_change().fillna(result["nav"].iloc[0] - 1)
    result["benchmark_return"] = left["benchmark_return"]
    result["qqq_return"] = left["qqq_return"]
    result["drawdown"] = result["nav"].div(result["nav"].cummax()).sub(1.0)
    return result


def run(
    v6_path: Path = V6_DAILY,
    v7_stock_path: Path = V7_STOCK_DAILY,
    qqq_path: Path = QQQ_PATH,
    prefix: Path = PREFIX,
) -> dict:
    v6 = pd.read_csv(v6_path, parse_dates=["date"]).set_index("date")
    stock = pd.read_csv(v7_stock_path, parse_dates=["date"]).set_index("date")
    qqq = pd.read_csv(qqq_path, parse_dates=["date"]).set_index("date")
    qqq_return = qqq["close"].add(
        qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index))
    ).div(qqq["close"].shift(1)).sub(1.0).reindex(stock.index).fillna(0.0)

    sensitivity = {}
    primary = None
    for stock_weight in (0.375, 0.40, 0.425):
        v7_weighted = V7.simulate_monthly_core_satellite(
            stock,
            qqq_return,
            stock_weight=stock_weight,
            qqq_weight=1.0 - stock_weight,
            transaction_cost_bps=UNDERLYING_COST_BPS,
        )
        for capital_weight in (0.70, 0.725, 0.75):
            for transfer_cost in (25.0, 50.0, 100.0):
                result = combine_monthly_sleeves(
                    v6,
                    v7_weighted,
                    v7_weight=capital_weight,
                    transfer_cost_bps=transfer_cost,
                )
                key = (
                    f"stock_{stock_weight:.3f}_capital_{capital_weight:.3f}_"
                    f"transfer_{int(transfer_cost)}"
                )
                sensitivity[key] = V7.summarize(result)
                if (
                    stock_weight == V7_STOCK_WEIGHT
                    and capital_weight == V7_CAPITAL_WEIGHT
                    and transfer_cost == SLEEVE_TRANSFER_COST_BPS
                ):
                    primary = result
    assert primary is not None
    prefix.parent.mkdir(parents=True, exist_ok=True)
    primary.to_csv(prefix.with_name(prefix.name + "_50bps_daily.csv"), index_label="date")
    payload = {
        "model_version": "can-slim-v8-monthly-risk-budget-blend-research",
        "research_only": True,
        "historical_selection_contaminated": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "forward_review_policy": {
            "limited_canary_review": {
                "minimum_calendar_weeks": 13,
                "minimum_weekly_marks": 13,
                "minimum_monthly_decisions": 3,
                "scope": "limited_capital_canary_review_only",
                "positive_cumulative_excess_after_30bps_required": True,
                "maximum_drawdown_limit": 0.40,
            },
            "full_promotion_review": {
                "minimum_calendar_weeks": 26,
                "minimum_weekly_marks": 26,
                "minimum_monthly_decisions": 6,
                "scope": "full_promotion_eligibility_review",
                "positive_cumulative_excess_after_30bps_required": True,
                "bootstrap_90pct_lower_bound_must_be_positive": True,
                "maximum_drawdown_limit": 0.40,
            },
            "final_decision": {
                "calendar_weeks": 39,
                "minimum_weekly_marks": 39,
                "minimum_monthly_decisions": 9,
                "if_promotion_evidence_is_still_inconclusive": (
                    "reject_candidate_and_return_to_research"
                ),
            },
            "parameters_must_remain_frozen": True,
            "calendar_time_without_auditable_marks_is_evidence": False,
            "immediate_blockers": [
                "parameter_drift",
                "manifest_integrity_failure",
                "selected_price_gap",
                "unresolved_delisting_terminal_value",
            ],
        },
        "configuration": {
            "v7_stock_weight": V7_STOCK_WEIGHT,
            "v7_qqq_weight": V7_QQQ_WEIGHT,
            "v7_capital_weight": V7_CAPITAL_WEIGHT,
            "v6_capital_weight": V6_CAPITAL_WEIGHT,
            "underlying_cost_bps": UNDERLYING_COST_BPS,
            "sleeve_transfer_cost_bps": SLEEVE_TRANSFER_COST_BPS,
            "rebalance_frequency": "monthly",
        },
        "primary": V7.summarize(primary),
        "sensitivity": sensitivity,
        "inputs": {
            "v6_daily": {"path": str(v6_path), "sha256": _sha256(v6_path)},
            "v7_stock_daily": {
                "path": str(v7_stock_path), "sha256": _sha256(v7_stock_path)
            },
            "qqq": {"path": str(qqq_path), "sha256": _sha256(qqq_path)},
        },
        "selection_warning": (
            "The v7 inner weight and v6/v7 capital split were selected after "
            "inspecting 2022-2026. Results are diagnostics, not forward evidence."
        ),
    }
    prefix.with_name(prefix.name + "_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
