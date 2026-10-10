#!/usr/bin/env python3
"""Scale the v7 sleeve to cash monthly when QQQ is below trend."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd


V6_PATH = Path("output/research_v6_walkforward_defensive_ensemble_50bps_daily.csv")
V7_STOCK_PATH = Path("output/research_v7_qqq_targeted_core_satellite_stock_sleeve_10bps_daily.csv")
QQQ_PATH = Path("output/research_only/qqq_nasdaq_history.csv")
PREFIX = Path("output/research_v11_monthly_trend_scaled_v7")
V7_STOCK_WEIGHT = 0.40
V7_CAPITAL_WEIGHT = 0.75
TREND_WINDOW = 100
RISK_OFF_EXPOSURE = 0.75
COST_BPS = 50.0


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


V7 = _load("research_v7_v11", "scripts/research_v7_qqq_targeted_core_satellite.py")
V8 = _load("research_v8_v11", "scripts/research_v8_monthly_risk_budget_blend.py")
ROBUST = _load("research_v8_robust_v11", "scripts/research_v8_short_horizon_robustness.py")


def scale_sleeve_monthly(
    sleeve: pd.DataFrame,
    qqq_close: pd.Series,
    *,
    trend_window: int = TREND_WINDOW,
    risk_off_exposure: float = RISK_OFF_EXPOSURE,
    transaction_cost_bps: float = COST_BPS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not 0 <= risk_off_exposure <= 1:
        raise ValueError("risk_off_exposure must be in [0, 1]")
    frame = sleeve.sort_index()
    close = qqq_close.reindex(frame.index).ffill()
    periods = frame.index.to_period("M")
    nav = 1.0
    sleeve_value = cash = 0.0
    previous_period = None
    rows = []
    decisions = []
    for position, stamp in enumerate(frame.index):
        if position:
            sleeve_value *= 1.0 + float(frame["return"].iloc[position])
            nav = sleeve_value + cash
        turnover = cost = 0.0
        if periods[position] != previous_period:
            history = close.iloc[:position]
            trend_ready = len(history) >= trend_window
            trend_on = bool(
                trend_ready and history.iloc[-1] > history.tail(trend_window).mean()
            )
            exposure = 1.0 if (not trend_ready or trend_on) else risk_off_exposure
            target = nav * exposure
            turnover = abs(target - sleeve_value)
            cost = turnover * transaction_cost_bps / 10_000.0
            nav -= cost
            sleeve_value = nav * exposure
            cash = nav - sleeve_value
            decisions.append({
                "date": stamp, "trend_ready": trend_ready,
                "trend_on": trend_on, "exposure": exposure,
                "prior_close": float(history.iloc[-1]) if len(history) else None,
                "prior_trend_mean": float(history.tail(trend_window).mean()) if trend_ready else None,
            })
        rows.append({"date": stamp, "nav": sleeve_value + cash, "turnover": turnover, "transaction_cost": cost})
        previous_period = periods[position]
    result = pd.DataFrame(rows).set_index("date")
    result["return"] = result["nav"].pct_change().fillna(result["nav"].iloc[0] - 1)
    result["benchmark_return"] = frame["benchmark_return"]
    result["qqq_return"] = frame["qqq_return"]
    result["drawdown"] = result["nav"].div(result["nav"].cummax()).sub(1.0)
    return result, pd.DataFrame(decisions)


def run() -> dict:
    read = lambda path: pd.read_csv(path, parse_dates=["date"]).set_index("date")
    v6, stock, qqq = read(V6_PATH), read(V7_STOCK_PATH), read(QQQ_PATH)
    dividends = qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index))
    qqq_return = qqq["close"].add(dividends).div(qqq["close"].shift(1)).sub(1.0).reindex(stock.index).fillna(0.0)
    base_v7 = V7.simulate_monthly_core_satellite(
        stock, qqq_return, stock_weight=V7_STOCK_WEIGHT,
        qqq_weight=1.0 - V7_STOCK_WEIGHT, transaction_cost_bps=COST_BPS,
    )
    variants = {}
    primary = primary_decisions = None
    for window in (100, 150, 200):
        for exposure in (0.50, 0.75):
            scaled, decisions = scale_sleeve_monthly(
                base_v7, qqq["close"], trend_window=window,
                risk_off_exposure=exposure,
            )
            combined = V8.combine_monthly_sleeves(
                v6, scaled, v7_weight=V7_CAPITAL_WEIGHT,
                transfer_cost_bps=COST_BPS,
            )
            relative = ROBUST.relative_returns(combined["return"], combined["qqq_return"])
            summary = V7.summarize(combined)
            summary["13_week_positive_fraction"] = ROBUST.rolling_summary(relative, 65)["positive_fraction"]
            summary["26_week_positive_fraction"] = ROBUST.rolling_summary(relative, 130)["positive_fraction"]
            variants[f"window_{window}_risk_off_{exposure:.2f}"] = summary
            if window == TREND_WINDOW and exposure == RISK_OFF_EXPOSURE:
                primary, primary_decisions = combined, decisions
    assert primary is not None and primary_decisions is not None
    primary.to_csv(PREFIX.with_name(PREFIX.name + "_50bps_daily.csv"), index_label="date")
    primary_decisions.to_csv(PREFIX.with_name(PREFIX.name + "_decisions.csv"), index=False)
    payload = {
        "model_version": "can-slim-v11-monthly-trend-scaled-v7-research",
        "research_only": True,
        "historical_selection_contaminated": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "configuration": {
            "trend_window": TREND_WINDOW,
            "risk_off_exposure": RISK_OFF_EXPOSURE,
            "decision_frequency": "monthly",
            "feature_lag": "trend ends on session before rebalance",
            "v7_stock_weight": V7_STOCK_WEIGHT,
            "v7_capital_weight": V7_CAPITAL_WEIGHT,
            "transaction_cost_bps": COST_BPS,
        },
        "primary": variants["window_100_risk_off_0.75"],
        "neighbor_sensitivity": variants,
        "selection_warning": "Trend scaling was designed after inspecting v8 regime diagnostics.",
    }
    PREFIX.with_name(PREFIX.name + "_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
