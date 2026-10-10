#!/usr/bin/env python3
"""Whole-share execution audit for the v8 monthly risk-budget blend."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR
from src.io.security_identity import issuer_rename_transitions


V6_BASE_TARGETS = Path(
    "output/can_slim_walk_forward_targets_quarterly_financials_"
    "financial_age_150_365_550_proven_only_bank_v3_13d77de9.csv"
)
V6_DAILY = Path("output/research_v6_walkforward_defensive_ensemble_50bps_daily.csv")
V7_TARGETS = Path("output/research_v7_qqq_targeted_core_satellite_stock_targets.csv")
V8_DAILY = Path("output/research_v8_monthly_risk_budget_blend_50bps_daily.csv")
QQQ_PATH = Path("output/research_only/qqq_nasdaq_history.csv")
OUTPUT_PATH = Path("output/research_v8_execution_sensitivity.json")
ACCOUNT_SIZES = (10_000.0, 25_000.0, 100_000.0)
V6_CAPITAL_WEIGHT = 0.25
V7_CAPITAL_WEIGHT = 0.75
V6_STOCK_WEIGHT = 0.25
V6_QQQ_WEIGHT_PER_RISK_ON_SLEEVE = 0.375
V7_STOCK_WEIGHT = 0.40
V7_QQQ_WEIGHT = 0.60


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


V5_EXEC = _load("v5_execution", "scripts/research_v5_execution_sensitivity.py")
V7_EXEC = _load("v7_execution", "scripts/research_v7_execution_sensitivity.py")


def _schedule(frame: pd.DataFrame) -> dict[pd.Timestamp, pd.Series]:
    clean = frame.loc[frame["ticker"].ne("__CASH__")].copy()
    return {
        pd.Timestamp(stamp): group.set_index("ticker")["target_weight"]
        for stamp, group in clean.groupby("effective_date")
    }


def build_v8_targets(
    v6_targets: pd.DataFrame,
    v7_targets: pd.DataFrame,
    v6_daily: pd.DataFrame,
    *,
    v7_stock_weight: float = V7_STOCK_WEIGHT,
    v7_capital_weight: float = V7_CAPITAL_WEIGHT,
) -> pd.DataFrame:
    if not 0 <= v7_stock_weight <= 1 or not 0 <= v7_capital_weight <= 1:
        raise ValueError("v7 weights must be in [0, 1]")
    v6_capital_weight = 1.0 - v7_capital_weight
    left = v6_targets.copy()
    right = v7_targets.copy()
    for frame in (left, right):
        frame["effective_date"] = pd.to_datetime(frame["effective_date"])
    v6_schedule = _schedule(left)
    v7_schedule = _schedule(right)
    dates = sorted(set(left["effective_date"]) | set(right["effective_date"]))
    rows = []
    for stamp in dates:
        weights: dict[str, float] = {}
        for ticker, weight in v6_schedule.get(stamp, pd.Series(dtype=float)).items():
            weights[str(ticker)] = weights.get(str(ticker), 0.0) + (
                float(weight) * V6_STOCK_WEIGHT * v6_capital_weight
            )
        for ticker, weight in v7_schedule.get(stamp, pd.Series(dtype=float)).items():
            weights[str(ticker)] = weights.get(str(ticker), 0.0) + (
                float(weight) * v7_stock_weight * v7_capital_weight
            )
        risk_on = int(v6_daily.reindex([stamp])["risk_on_sleeves"].iloc[0])
        qqq_weight = (
            (1.0 - v7_stock_weight) * v7_capital_weight
            + risk_on * V6_QQQ_WEIGHT_PER_RISK_ON_SLEEVE * v6_capital_weight
        )
        weights["QQQ"] = weights.get("QQQ", 0.0) + qqq_weight
        total = sum(weights.values())
        if total > 1.0 + 1e-10:
            raise ValueError(f"v8 target weight exceeds one on {stamp.date()}: {total}")
        for ticker, weight in sorted(weights.items()):
            if weight > 0:
                rows.append({
                    "effective_date": stamp,
                    "ticker": ticker,
                    "target_weight": weight,
                    "sleeve": "core" if ticker == "QQQ" else "stock",
                })
    return pd.DataFrame(rows)


def attach_execution_prices(
    targets: pd.DataFrame,
    qqq_close: pd.Series,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
) -> pd.DataFrame:
    frame = targets.copy()
    prices = []
    for row in frame.itertuples(index=False):
        if row.ticker == "QQQ":
            value = qqq_close.reindex([row.effective_date]).iloc[0]
        else:
            path = Path(price_dir) / f"{str(row.ticker).lower()}.csv"
            history = pd.read_csv(
                path, usecols=["date", "close"], parse_dates=["date"]
            ).set_index("date")["close"]
            value = history.reindex([row.effective_date]).iloc[0]
        if pd.isna(value) or value <= 0:
            raise ValueError(
                f"missing positive execution price for {row.ticker} on "
                f"{row.effective_date.date()}"
            )
        prices.append(float(value))
    frame["execution_close"] = prices
    return frame


def run() -> dict:
    v6_targets = pd.read_csv(V6_BASE_TARGETS)
    v7_targets = pd.read_csv(V7_TARGETS)
    v6_daily = pd.read_csv(V6_DAILY, parse_dates=["date"]).set_index("date")
    v8_daily = pd.read_csv(V8_DAILY, parse_dates=["date"]).set_index("date")
    qqq = pd.read_csv(QQQ_PATH, parse_dates=["date"]).set_index("date")
    qqq_dividend = qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index))
    targets = attach_execution_prices(
        build_v8_targets(v6_targets, v7_targets, v6_daily), qqq["close"]
    )
    tickers = sorted(targets.loc[targets["ticker"].ne("QQQ"), "ticker"].unique())
    transitions = issuer_rename_transitions()
    transition_rows = transitions.loc[transitions["historical_ticker"].isin(tickers)]
    actions = pd.read_csv(
        V5_EXEC.CORPORATE_ACTIONS_PATH,
        parse_dates=["last_price_date", "effective_date"],
    )
    relevant = actions.loc[actions["predecessor"].isin(tickers)]
    all_tickers = sorted(
        set(tickers)
        | set(transition_rows["provider_ticker"])
        | set(relevant["successor"].dropna().astype(str))
    )
    stock_close = V5_EXEC.load_stock_close_panel(
        all_tickers, v8_daily.index, CLEANED_PRICE_DATA_DIR
    )
    baseline = {}
    stressed = {}
    for size in ACCOUNT_SIZES:
        path = V5_EXEC.simulate_continuous_whole_share(
            targets, stock_close, qqq["close"], qqq_dividend,
            v8_daily["benchmark_return"], account_size=size,
            transaction_cost_bps=50.0, identity_transitions=transitions,
            corporate_actions=actions,
        )
        baseline[str(int(size))] = V7_EXEC.summarize_path(
            path, v8_daily["return"], v8_daily["qqq_return"]
        )
        stress_path = V5_EXEC.simulate_continuous_whole_share(
            targets, stock_close, qqq["close"], qqq_dividend,
            v8_daily["benchmark_return"], account_size=size,
            transaction_cost_bps=50.0, execution_slippage_bps=10.0,
            fill_fraction=0.75, identity_transitions=transitions,
            corporate_actions=actions,
        )
        stressed[str(int(size))] = V7_EXEC.summarize_path(
            stress_path, v8_daily["return"], v8_daily["qqq_return"]
        )
    neighbor_stress = {}
    for stock_weight in (0.375, 0.40, 0.425, 0.45):
        for capital_weight in (0.60, 0.70, 0.725, 0.75):
            neighbor_targets = attach_execution_prices(
                build_v8_targets(
                    v6_targets,
                    v7_targets,
                    v6_daily,
                    v7_stock_weight=stock_weight,
                    v7_capital_weight=capital_weight,
                ),
                qqq["close"],
            )
            account_results = {}
            for size in ACCOUNT_SIZES:
                path = V5_EXEC.simulate_continuous_whole_share(
                    neighbor_targets, stock_close, qqq["close"], qqq_dividend,
                    v8_daily["benchmark_return"], account_size=size,
                    transaction_cost_bps=50.0, execution_slippage_bps=10.0,
                    fill_fraction=0.75, identity_transitions=transitions,
                    corporate_actions=actions,
                )
                summary = V7_EXEC.summarize_path(
                    path, v8_daily["return"], v8_daily["qqq_return"]
                )
                account_results[str(int(size))] = {
                    key: summary[key]
                    for key in (
                        "wins_vs_nasdaq",
                        "wins_vs_qqq",
                        "minimum_excess_vs_qqq",
                        "median_excess_vs_qqq",
                        "maximum_drawdown",
                    )
                }
            neighbor_stress[
                f"stock_{stock_weight:.3f}_capital_{capital_weight:.3f}"
            ] = account_results
    payload = {
        "schema_version": 1,
        "research_only": True,
        "model_version": "can-slim-v8-monthly-risk-budget-blend-research",
        "whole_share_50bps": baseline,
        "execution_stress": {
            "additional_slippage_bps": 10.0,
            "fill_fraction": 0.75,
            "results": stressed,
        },
        "execution_stress_neighbors": neighbor_stress,
        "robustness_gate": {
            "supported_account_sizes": [25_000, 100_000],
            "unsupported_account_sizes": {
                "10000": (
                    "Fails the deterministic 75% fill plus 10 bps slippage "
                    "stress; whole-share granularity is too material."
                ),
            },
            "minimum_wins_vs_nasdaq": 5,
            "minimum_wins_vs_qqq": 4,
            "baseline_passed": all(
                result["wins_vs_nasdaq"] >= 5 and result["wins_vs_qqq"] >= 4
                for size, result in baseline.items() if int(size) >= 25_000
            ),
            "execution_stress_passed": all(
                result["wins_vs_nasdaq"] >= 5 and result["wins_vs_qqq"] >= 4
                for size, result in stressed.items() if int(size) >= 25_000
            ),
        },
        "target_panel": {
            "rows": int(len(targets)),
            "periods": int(targets["effective_date"].nunique()),
            "stock_tickers": int(len(tickers)),
            "maximum_total_weight": float(
                targets.groupby("effective_date")["target_weight"].sum().max()
            ),
            "minimum_total_weight": float(
                targets.groupby("effective_date")["target_weight"].sum().min()
            ),
        },
        "interpretation": (
            "The v6 and v7 targets are combined before whole-share rounding. "
            "Residual cash, QQQ dividends, corporate actions, costs, slippage, "
            "and deterministic partial fills are included."
        ),
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
