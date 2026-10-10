#!/usr/bin/env python3
"""Search v6/v7 risk budgets for short-horizon relative consistency.

This is an explicitly contaminated diagnostic scan. It is used to decide
whether a new walk-forward policy is worth implementing, not as promotion
evidence.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd


V6_DAILY = Path("output/research_v6_walkforward_defensive_ensemble_50bps_daily.csv")
V7_STOCK_DAILY = Path(
    "output/research_v7_qqq_targeted_core_satellite_stock_sleeve_10bps_daily.csv"
)
QQQ_PATH = Path("output/research_only/qqq_nasdaq_history.csv")
OUTPUT_PATH = Path("output/research_v9_short_horizon_stability_scan.json")


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


V7 = _load("research_v7_scan", "scripts/research_v7_qqq_targeted_core_satellite.py")
V8 = _load("research_v8_scan", "scripts/research_v8_monthly_risk_budget_blend.py")
ROBUST = _load(
    "research_v8_robustness_scan",
    "scripts/research_v8_short_horizon_robustness.py",
)


def candidate_metrics(result: pd.DataFrame) -> dict:
    summary = V7.summarize(result)
    relative = ROBUST.relative_returns(result["return"], result["qqq_return"])
    short = ROBUST.rolling_summary(relative, 65)
    medium = ROBUST.rolling_summary(relative, 130)
    return {
        "wins_vs_nasdaq": summary["wins_vs_nasdaq"],
        "wins_vs_qqq": summary["wins_vs_qqq"],
        "cagr": summary["cagr"],
        "maximum_drawdown": summary["maximum_drawdown"],
        "minimum_annual_excess_vs_qqq": summary["minimum_excess_vs_qqq"],
        "13_week_positive_fraction": short["positive_fraction"],
        "13_week_quantile_10": short["quantile_10"],
        "26_week_positive_fraction": medium["positive_fraction"],
        "26_week_quantile_10": medium["quantile_10"],
    }


def admissible(metrics: dict) -> bool:
    return (
        metrics["wins_vs_nasdaq"] >= 5
        and metrics["wins_vs_qqq"] >= 4
        and metrics["cagr"] >= 0.18
        and metrics["maximum_drawdown"] >= -0.30
    )


def scan(v6: pd.DataFrame, stock: pd.DataFrame, qqq_return: pd.Series) -> dict:
    candidates = []
    for stock_weight in (0.20, 0.25, 0.30, 0.35, 0.40, 0.45):
        v7 = V7.simulate_monthly_core_satellite(
            stock,
            qqq_return,
            stock_weight=stock_weight,
            qqq_weight=1.0 - stock_weight,
            transaction_cost_bps=50.0,
        )
        for capital_weight in (0.40, 0.50, 0.60, 0.70, 0.75, 0.80, 0.90):
            result = V8.combine_monthly_sleeves(
                v6, v7, v7_weight=capital_weight, transfer_cost_bps=50.0
            )
            metrics = candidate_metrics(result)
            metrics.update({
                "v7_stock_weight": stock_weight,
                "v7_capital_weight": capital_weight,
                "admissible": admissible(metrics),
            })
            candidates.append(metrics)
    candidates.sort(
        key=lambda row: (
            row["admissible"],
            min(row["13_week_positive_fraction"], row["26_week_positive_fraction"]),
            row["26_week_positive_fraction"],
            row["13_week_quantile_10"],
            row["cagr"],
        ),
        reverse=True,
    )
    return {
        "schema_version": 1,
        "research_only": True,
        "historical_selection_contaminated": True,
        "objective": (
            "Maximize the weaker 13/26-week positive-window fraction after "
            "requiring Nasdaq 5/5, QQQ 4/5, CAGR >=18%, and drawdown <=30%."
        ),
        "candidate_count": len(candidates),
        "admissible_count": sum(row["admissible"] for row in candidates),
        "ranked_candidates": candidates,
    }


def run() -> dict:
    v6 = pd.read_csv(V6_DAILY, parse_dates=["date"]).set_index("date")
    stock = pd.read_csv(V7_STOCK_DAILY, parse_dates=["date"]).set_index("date")
    qqq = pd.read_csv(QQQ_PATH, parse_dates=["date"]).set_index("date")
    dividends = qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index))
    qqq_return = qqq["close"].add(dividends).div(qqq["close"].shift(1)).sub(1.0)
    qqq_return = qqq_return.reindex(stock.index).fillna(0.0)
    payload = scan(v6, stock, qqq_return)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
