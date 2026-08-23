#!/usr/bin/env python3
"""Whole-share and partial-fill audit for the v7 core/satellite challenger."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR
from src.io.security_identity import issuer_rename_transitions


TARGETS_PATH = Path("output/research_v7_qqq_targeted_core_satellite_stock_targets.csv")
DAILY_PATH = Path("output/research_v7_qqq_targeted_core_satellite_50bps_daily.csv")
QQQ_PATH = Path("output/research_only/qqq_nasdaq_history.csv")
OUTPUT_PATH = Path("output/research_v7_execution_sensitivity.json")
PRICE_DIR = Path(CLEANED_PRICE_DATA_DIR)
ACCOUNT_SIZES = (10_000.0, 25_000.0, 100_000.0)
STOCK_WEIGHT = 0.20
QQQ_WEIGHT = 0.80


def _load_v5_execution_module():
    path = Path("scripts/research_v5_execution_sensitivity.py")
    spec = importlib.util.spec_from_file_location("research_v5_execution", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


V5 = _load_v5_execution_module()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_v7_target_panel(
    stock_targets: pd.DataFrame,
    qqq_close: pd.Series,
    price_dir: str | Path,
) -> pd.DataFrame:
    frame = stock_targets.copy()
    frame["effective_date"] = pd.to_datetime(frame["effective_date"])
    rows = []
    for effective_date, group in frame.groupby("effective_date", sort=True):
        for position in group.itertuples(index=False):
            ticker = str(position.ticker)
            if ticker == "__CASH__" or float(position.target_weight) <= 0:
                continue
            price_path = Path(price_dir) / f"{ticker.lower()}.csv"
            prices = pd.read_csv(
                price_path, usecols=["date", "close"], parse_dates=["date"]
            )
            match = prices.loc[prices["date"].eq(effective_date), "close"]
            if match.empty or not np.isfinite(match.iloc[-1]) or match.iloc[-1] <= 0:
                raise ValueError(
                    f"missing positive execution close for {ticker} on "
                    f"{effective_date.date()}"
                )
            rows.append({
                "effective_date": effective_date,
                "ticker": ticker,
                "sleeve": "stock",
                "target_weight": float(position.target_weight) * STOCK_WEIGHT,
                "execution_close": float(match.iloc[-1]),
            })
        qqq_price = qqq_close.reindex([effective_date]).iloc[0]
        if not np.isfinite(qqq_price) or qqq_price <= 0:
            raise ValueError(f"QQQ close missing on {effective_date.date()}")
        rows.append({
            "effective_date": effective_date,
            "ticker": "QQQ",
            "sleeve": "core",
            "target_weight": QQQ_WEIGHT,
            "execution_close": float(qqq_price),
        })
    result = pd.DataFrame(rows)
    if result.empty:
        raise ValueError("no v7 execution targets")
    totals = result.groupby("effective_date")["target_weight"].sum()
    if totals.gt(1.0 + 1e-12).any():
        raise ValueError("v7 target weights exceed one")
    return result.sort_values(["effective_date", "sleeve", "ticker"]).reset_index(drop=True)


def summarize_path(
    result: pd.DataFrame,
    fractional_return: pd.Series,
    qqq_return: pd.Series,
) -> dict:
    joined = pd.DataFrame({
        "whole_share": result["return"],
        "fractional": fractional_return.reindex(result.index),
        "nasdaq": result["benchmark_return"],
        "qqq": qqq_return.reindex(result.index),
    }).dropna()
    annual = (1.0 + joined).groupby(joined.index.year).prod() - 1.0
    excess_n = annual["whole_share"] - annual["nasdaq"]
    excess_q = annual["whole_share"] - annual["qqq"]
    drawdown = result["nav"].div(result["nav"].cummax()).sub(1.0)
    return {
        "wins_vs_nasdaq": int(excess_n.gt(0).sum()),
        "wins_vs_qqq": int(excess_q.gt(0).sum()),
        "minimum_excess_vs_nasdaq": float(excess_n.min()),
        "minimum_excess_vs_qqq": float(excess_q.min()),
        "median_excess_vs_qqq": float(excess_q.median()),
        "maximum_drawdown": float(drawdown.min()),
        "final_nav": float(result["nav"].iloc[-1]),
        "cumulative_transaction_cost": float(result["transaction_cost"].sum()),
        "cumulative_slippage_cost": float(result["slippage_cost"].sum()),
        "requested_shares": int(result["requested_share_delta"].sum()),
        "filled_shares": int(result["filled_share_delta"].sum()),
        "annual": annual.reset_index(names="year").to_dict(orient="records"),
    }


def run(
    *,
    targets_path: str | Path = TARGETS_PATH,
    daily_path: str | Path = DAILY_PATH,
    qqq_path: str | Path = QQQ_PATH,
    output_path: str | Path = OUTPUT_PATH,
    price_dir: str | Path = PRICE_DIR,
) -> dict:
    targets_path = Path(targets_path)
    daily_path = Path(daily_path)
    qqq_path = Path(qqq_path)
    output_path = Path(output_path)
    stock_targets = pd.read_csv(targets_path)
    daily = pd.read_csv(daily_path, parse_dates=["date"]).set_index("date")
    qqq = pd.read_csv(qqq_path, parse_dates=["date"]).set_index("date")
    qqq_close = qqq["close"]
    qqq_dividend = qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index))
    target_panel = build_v7_target_panel(stock_targets, qqq_close, price_dir)
    tickers = sorted(
        target_panel.loc[target_panel["sleeve"].eq("stock"), "ticker"].unique()
    )
    transitions = issuer_rename_transitions()
    transition_rows = transitions.loc[transitions["historical_ticker"].isin(tickers)]
    corporate_actions = pd.read_csv(
        V5.CORPORATE_ACTIONS_PATH,
        parse_dates=["last_price_date", "effective_date"],
    )
    relevant_actions = corporate_actions.loc[
        corporate_actions["predecessor"].isin(tickers)
    ]
    all_tickers = sorted(
        set(tickers)
        | set(transition_rows["provider_ticker"])
        | set(relevant_actions["successor"].dropna().astype(str))
    )
    stock_close = V5.load_stock_close_panel(all_tickers, daily.index, price_dir)
    baseline = {}
    stressed = {}
    for size in ACCOUNT_SIZES:
        path = V5.simulate_continuous_whole_share(
            target_panel, stock_close, qqq_close, qqq_dividend,
            daily["benchmark_return"], account_size=size,
            transaction_cost_bps=50.0,
            identity_transitions=transitions,
            corporate_actions=corporate_actions,
        )
        baseline[str(int(size))] = summarize_path(
            path, daily["return"], daily["qqq_return"]
        )
        stress_path = V5.simulate_continuous_whole_share(
            target_panel, stock_close, qqq_close, qqq_dividend,
            daily["benchmark_return"], account_size=size,
            transaction_cost_bps=50.0, execution_slippage_bps=10.0,
            fill_fraction=0.75, identity_transitions=transitions,
            corporate_actions=corporate_actions,
        )
        stressed[str(int(size))] = summarize_path(
            stress_path, daily["return"], daily["qqq_return"]
        )
    payload = {
        "schema_version": 1,
        "research_only": True,
        "model_version": "can-slim-v7-qqq-targeted-core-satellite-research",
        "fractional_configuration": {
            "stock_weight": STOCK_WEIGHT,
            "qqq_weight": QQQ_WEIGHT,
            "transaction_cost_bps": 50.0,
        },
        "whole_share_50bps": baseline,
        "execution_stress": {
            "transaction_cost_bps": 50.0,
            "additional_slippage_bps": 10.0,
            "deterministic_fill_fraction": 0.75,
            "results": stressed,
        },
        "target_panel": {
            "rows": int(len(target_panel)),
            "periods": int(target_panel["effective_date"].nunique()),
            "stock_tickers": int(len(tickers)),
            "maximum_target_weight": float(
                target_panel.groupby("effective_date")["target_weight"].sum().max()
            ),
        },
        "inputs": {
            "stock_targets": {"path": str(targets_path), "sha256": _sha256(targets_path)},
            "fractional_daily": {"path": str(daily_path), "sha256": _sha256(daily_path)},
            "qqq": {"path": str(qqq_path), "sha256": _sha256(qqq_path)},
        },
        "interpretation": (
            "Integer shares are held between monthly rebalances; residual cash, "
            "QQQ dividends, corporate actions, terminal returns, costs, slippage, "
            "and deterministic partial fills are included. This is not empirical "
            "broker-fill evidence or authorization to trade."
        ),
    }
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
