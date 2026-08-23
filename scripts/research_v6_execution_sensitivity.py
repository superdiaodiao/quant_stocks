#!/usr/bin/env python3
"""Whole-share and partial-fill stress for the v6 defensive ensemble."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v5_execution_sensitivity import (
    ACCOUNT_SIZES,
    CORPORATE_ACTIONS_PATH,
    load_stock_close_panel,
    simulate_continuous_whole_share,
    summarize_continuous_path,
    summarize_whole_share_rounding,
)
from src.io.security_identity import issuer_rename_transitions


DEFAULT_BASE_TARGETS = Path(
    "output/can_slim_walk_forward_targets_quarterly_financials_"
    "financial_age_150_365_550_proven_only_bank_v3_13d77de9.csv"
)
DEFAULT_V6_DAILY = Path("output/research_v6_walkforward_defensive_ensemble_50bps_daily.csv")
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_OUTPUT = Path("output/research_v6_execution_sensitivity.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_v6_targets(
    base_targets: pd.DataFrame, allocation: pd.DataFrame
) -> pd.DataFrame:
    targets = base_targets.copy()
    targets["effective_date"] = pd.to_datetime(targets["effective_date"])
    targets = targets.loc[targets["ticker"].ne("__CASH__")].copy()
    allocation = allocation.sort_index()
    effective_dates = (
        allocation.groupby([allocation.index.year, allocation.index.month])
        .head(1).index
    )
    rows = []
    for effective_date in effective_dates:
        base = targets.loc[targets["effective_date"].eq(effective_date)]
        for position in base.itertuples(index=False):
            rows.append({
                "effective_date": effective_date,
                "ticker": str(position.ticker),
                "target_weight": float(position.target_weight) * 0.25,
                "sleeve": "v4",
            })
        risk_on_sleeves = int(allocation.loc[effective_date, "risk_on_sleeves"])
        qqq_weight = 0.375 * risk_on_sleeves
        if qqq_weight > 0.0:
            rows.append({
                "effective_date": effective_date,
                "ticker": "QQQ",
                "target_weight": qqq_weight,
                "sleeve": "qqq",
            })
        if base.empty and qqq_weight == 0.0:
            rows.append({
                "effective_date": effective_date,
                "ticker": "__CASH__",
                "target_weight": 0.0,
                "sleeve": "cash",
            })
    result = pd.DataFrame(rows)
    totals = result.groupby("effective_date")["target_weight"].sum()
    if totals.gt(1.0 + 1e-12).any():
        raise ValueError("v6 target weights exceed 100%")
    return result


def run(
    base_targets_path: Path = DEFAULT_BASE_TARGETS,
    v6_daily_path: Path = DEFAULT_V6_DAILY,
    qqq_path: Path = DEFAULT_QQQ,
    output_path: Path = DEFAULT_OUTPUT,
) -> dict:
    base_targets = pd.read_csv(base_targets_path)
    allocation = pd.read_csv(v6_daily_path, parse_dates=["date"]).set_index("date")
    qqq = pd.read_csv(qqq_path, parse_dates=["date"]).set_index("date")
    targets = build_v6_targets(base_targets, allocation)
    stock_tickers = sorted(
        targets.loc[
            targets["sleeve"].eq("v4"), "ticker"
        ].unique()
    )
    transitions = issuer_rename_transitions()
    relevant_transitions = transitions.loc[
        transitions["historical_ticker"].isin(stock_tickers)
    ]
    corporate_actions = pd.read_csv(
        CORPORATE_ACTIONS_PATH,
        parse_dates=["last_price_date", "effective_date"],
    )
    relevant_actions = corporate_actions.loc[
        corporate_actions["predecessor"].isin(stock_tickers)
    ]
    stock_tickers = sorted(
        set(stock_tickers)
        | set(relevant_transitions["provider_ticker"])
        | set(relevant_actions["successor"].dropna().astype(str))
    )
    stock_close = load_stock_close_panel(stock_tickers, allocation.index)
    targets["execution_close"] = targets.apply(
        lambda row: (
            1.0
            if row["ticker"] == "__CASH__"
            else float(
                qqq.loc[row["effective_date"], "close"]
                if row["ticker"] == "QQQ"
                else stock_close.loc[row["effective_date"], row["ticker"]]
            )
        ),
        axis=1,
    )
    qqq_dividend = qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index))
    normal = {}
    stressed = {}
    for account_size in ACCOUNT_SIZES:
        path = simulate_continuous_whole_share(
            targets, stock_close, qqq["close"], qqq_dividend,
            allocation["benchmark_return"],
            account_size=account_size,
            transaction_cost_bps=50.0,
            identity_transitions=transitions,
            corporate_actions=corporate_actions,
        )
        normal[str(int(account_size))] = summarize_continuous_path(
            path, allocation["return"]
        )
        stressed_path = simulate_continuous_whole_share(
            targets, stock_close, qqq["close"], qqq_dividend,
            allocation["benchmark_return"],
            account_size=account_size,
            transaction_cost_bps=50.0,
            execution_slippage_bps=10.0,
            fill_fraction=0.75,
            identity_transitions=transitions,
            corporate_actions=corporate_actions,
        )
        stressed[str(int(account_size))] = summarize_continuous_path(
            stressed_path, allocation["return"]
        )
    payload = {
        "schema_version": 1,
        "research_only": True,
        "model_version": "can-slim-v6-walkforward-defensive-ensemble-research",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "transaction_cost_bps": 50.0,
        "whole_share_rounding": {
            str(int(size)): summarize_whole_share_rounding(targets, size)
            for size in ACCOUNT_SIZES
        },
        "continuous_whole_share": normal,
        "execution_stress": {
            "additional_slippage_bps": 10.0,
            "fill_fraction": 0.75,
            "results": stressed,
        },
        "target_panel": {
            "rows": int(len(targets)),
            "periods": int(targets["effective_date"].nunique()),
            "stock_symbols": len(stock_tickers),
        },
        "inputs": {
            "base_targets": {"path": str(base_targets_path), "sha256": _sha256(base_targets_path)},
            "v6_daily": {"path": str(v6_daily_path), "sha256": _sha256(v6_daily_path)},
            "qqq": {"path": str(qqq_path), "sha256": _sha256(qqq_path)},
        },
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-targets", type=Path, default=DEFAULT_BASE_TARGETS)
    parser.add_argument("--v6-daily", type=Path, default=DEFAULT_V6_DAILY)
    parser.add_argument("--qqq", type=Path, default=DEFAULT_QQQ)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(run(args.base_targets, args.v6_daily, args.qqq, args.output), indent=2))


if __name__ == "__main__":
    main()
