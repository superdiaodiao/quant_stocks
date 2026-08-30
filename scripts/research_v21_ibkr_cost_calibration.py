#!/usr/bin/env python3
"""Build an offline IBKR commission sensitivity for the frozen v20 target."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v15_benchmark_core_development as v15
from scripts import research_v19_source_locked_v10_feasibility as v19
from scripts import research_v20_temporal_retraining as v20
from src.research.ibkr_cost_calibration import calibrate_reference_orders
from src.research.manual_position_plan import build_manual_position_plan


PROTOCOL = {
    "path": v20.OUTPUT_DIR / "frozen_protocol.json",
    "sha256": (
        "1301bdd763d5ef34923fe067a8484c86cc6bf0676ac7bd6e7ca1e139b8992cc1"
    ),
}
HOLDOUT = {
    "path": Path("output/research_only/v20/recent_holdout_20260830/manifest.json"),
    "sha256": (
        "b2f19203709d3dbc7b088e1a72604a048ee83569251d49cb7f44f947a743fe3e"
    ),
}
ACCOUNT_EQUITIES_USD = (10_000, 25_000, 50_000, 100_000, 250_000)
PRICING_PLANS = ("fixed", "tiered")
OUTPUT_DIR = Path("output/research_only/v21/ibkr_cost_calibration_20260830")


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _verify_binding(name: str, binding: dict) -> dict:
    path = Path(binding["path"])
    actual = _sha256(path)
    if actual != binding["sha256"]:
        raise RuntimeError(f"{name} binding changed: {actual}")
    return {"path": str(path), "sha256": actual}


def _price_at_or_before(ticker: str, cutoff: pd.Timestamp) -> tuple[float, float]:
    if ticker == v15.CORE_TICKER:
        frame = pd.read_csv(v15.QQQ_HISTORY["path"], parse_dates=["date"])
    else:
        frame = pd.read_csv(
            Path("cleaned_stocks_data/price") / f"{ticker}.csv",
            parse_dates=["date"],
        )
    frame = frame.loc[frame["date"].le(cutoff)].sort_values("date")
    if frame.empty:
        raise RuntimeError(f"no price for {ticker} at {cutoff.date()}")
    latest = frame.iloc[-1]
    price = float(latest["close"])
    trailing = frame.tail(50)
    median_dollar_volume = float(
        (trailing["close"].astype(float) * trailing["volume"].astype(float)).median()
    )
    return price, median_dollar_volume


def frozen_reference_portfolio(protocol: dict) -> tuple[pd.DataFrame, dict]:
    cutoff = pd.Timestamp(v20.HOLDOUT_END)
    price_binding = protocol["input_bindings"]["price_directory"]
    stock_paths, qqq_return = v20._load_replay_inputs(
        end=v20.HOLDOUT_END, price_binding=price_binding
    )
    config = protocol["selected_configuration"]
    relative = v19.decision_relative_returns(stock_paths[10], qqq_return)
    _, decisions = v19.simulate_source_locked_contrarian_sleeves(
        stock_paths[10],
        qqq_return,
        relative,
        lookback=int(config["lookback_sessions"]),
        crowded_stock_weight=float(config["crowded_stock_weight"]),
        transfer_cost_bps=10.0,
    )
    latest_decision = decisions.sort_values("date").iloc[-1]
    if pd.Timestamp(latest_decision["date"]) > cutoff:
        raise RuntimeError("v21 decision crossed the frozen cutoff")
    stock_weight = float(latest_decision["stock_weight"])

    targets = pd.read_csv(
        v15.V14_TARGETS["path"], parse_dates=["effective_date"]
    )
    targets = targets.loc[targets["effective_date"].le(cutoff)]
    effective_date = targets["effective_date"].max()
    latest_targets = targets.loc[
        targets["effective_date"].eq(effective_date)
        & targets["ticker"].ne("__CASH__")
    ].copy()
    latest_targets["target_weight"] = (
        latest_targets["target_weight"].astype(float) * stock_weight
    )
    latest_targets = pd.concat([
        latest_targets[["ticker", "target_weight"]],
        pd.DataFrame({
            "ticker": [v15.CORE_TICKER],
            "target_weight": [1.0 - stock_weight],
        }),
    ], ignore_index=True)
    price_rows = {
        ticker: _price_at_or_before(ticker, cutoff)
        for ticker in latest_targets["ticker"]
    }
    latest_targets["current_price"] = latest_targets["ticker"].map(
        lambda ticker: price_rows[ticker][0]
    )
    latest_targets["current_median_dollar_volume_50d"] = (
        latest_targets["ticker"].map(lambda ticker: price_rows[ticker][1])
    )
    if abs(float(latest_targets["target_weight"].sum()) - 1.0) > 1e-12:
        raise RuntimeError("v21 reference weights do not sum to one")
    metadata = {
        "data_cutoff": str(cutoff.date()),
        "target_effective_date": str(pd.Timestamp(effective_date).date()),
        "overlay_decision_date": str(pd.Timestamp(latest_decision["date"]).date()),
        "stock_weight": stock_weight,
        "qqq_weight": 1.0 - stock_weight,
        "crowded": bool(latest_decision["crowded"]),
    }
    return latest_targets, metadata


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v21 output will not be overwritten: {output_dir}")
    protocol_binding = _verify_binding("v20_protocol", PROTOCOL)
    holdout_binding = _verify_binding("v20_holdout", HOLDOUT)
    protocol = json.loads(PROTOCOL["path"].read_text(encoding="utf-8"))
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v20 release boundary changed")
    reference, metadata = frozen_reference_portfolio(protocol)

    output_dir.mkdir(parents=True, exist_ok=True)
    reference_path = output_dir / "historical_reference_portfolio.csv"
    reference.to_csv(reference_path, index=False)
    rows = []
    for equity in ACCOUNT_EQUITIES_USD:
        plan, plan_summary = build_manual_position_plan(
            reference,
            float(equity),
            transaction_cost_bps=0.0,
            fractional_shares=False,
        )
        for pricing_plan in PRICING_PLANS:
            _, commission = calibrate_reference_orders(
                plan,
                pricing_plan=pricing_plan,
                monthly_volume_shares=0.0,
            )
            zero_share_targets = int(
                (
                    plan["target_weight"].gt(0.0)
                    & plan["target_shares"].eq(0.0)
                ).sum()
            )
            achieved_exposure = float(
                plan["target_value"].sum() / float(equity)
            )
            pre_commission_cash = float(
                plan_summary["estimated_residual_cash"]
            )
            post_commission_cash = (
                pre_commission_cash
                - float(commission["ibkr_base_commission_usd"])
            )
            if post_commission_cash < -1e-9:
                raise RuntimeError("reference allocation cannot fund commission")
            rows.append({
                "account_equity_usd": float(equity),
                "pricing_plan": pricing_plan,
                "fractional_shares": False,
                "order_count": commission["order_count"],
                "zero_share_target_count": zero_share_targets,
                "achieved_exposure": achieved_exposure,
                "pre_commission_residual_cash_usd": pre_commission_cash,
                "post_base_commission_cash_usd": max(
                    post_commission_cash, 0.0
                ),
                "target_trade_notional_usd": plan_summary[
                    "estimated_turnover_notional"
                ],
                "ibkr_base_commission_usd": commission[
                    "ibkr_base_commission_usd"
                ],
                "weighted_base_commission_bps": commission[
                    "weighted_base_commission_bps"
                ],
                **{
                    f"remaining_to_{key}bps": value
                    for key, value in commission[
                        "remaining_total_friction_budget_bps"
                    ].items()
                },
            })
    calibration = pd.DataFrame(rows)
    calibration_path = output_dir / "calibration.csv"
    calibration.to_csv(calibration_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "status": "OFFLINE_IBKR_COST_CALIBRATION_ONLY",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "broker_connection_used": False,
        "broker_account_accessed": False,
        "order_created": False,
        "reference_portfolio": metadata,
        "account_equities_usd": list(ACCOUNT_EQUITIES_USD),
        "pricing_plans": list(PRICING_PLANS),
        "calibration": calibration.to_dict(orient="records"),
        "input_bindings": {
            "v20_protocol": protocol_binding,
            "v20_holdout": holdout_binding,
        },
        "outputs": {
            "reference_portfolio": {
                "path": str(reference_path),
                "sha256": _sha256(reference_path),
            },
            "calibration": {
                "path": str(calibration_path),
                "sha256": _sha256(calibration_path),
            },
        },
        "interpretation_guardrail": (
            "Commission-only initial-allocation reference using whole shares. "
            "The remaining bps columns are the combined budget for venue and "
            "regulatory fees, spread, slippage, and impact before reaching each "
            "historical stress level. This is not a live recommendation or an order."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "manifest": {"path": str(manifest_path), "sha256": _sha256(manifest_path)},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = run(args.output_dir)
    print(json.dumps({
        "status": report["status"],
        "reference_portfolio": report["reference_portfolio"],
        "release_status": report["release_status"],
        "manifest": report["manifest"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
