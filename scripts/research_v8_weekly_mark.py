#!/usr/bin/env python3
"""Append one reconciled v8 weekly mark without broker side effects."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from scripts.research_v5_execution_sensitivity import (
    CORPORATE_ACTIONS_PATH,
    load_stock_close_panel,
    simulate_continuous_whole_share,
)
from scripts.research_v6_weekly_mark import last_nasdaq_session_of_week, _load_targets
from src.io.security_identity import issuer_rename_transitions


MODEL_VERSION = "can-slim-v8-monthly-risk-budget-blend-shadow"
PAPER_ACCOUNT_SIZE = 25_000.0
DEFAULT_MANIFEST = Path("output/research_v8_monthly_risk_budget_blend_shadow_summary.json")
DEFAULT_MODEL_DIR = Path("output/daily") / MODEL_VERSION
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_MARKET_DIR = Path("output/research_only/v6_market/prices")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, path)


def record_weekly_mark(
    *,
    as_of: str | pd.Timestamp,
    manifest_path: Path = DEFAULT_MANIFEST,
    model_dir: Path = DEFAULT_MODEL_DIR,
    qqq_path: Path = DEFAULT_QQQ,
    price_dir: Path = DEFAULT_MARKET_DIR,
) -> dict:
    observation = pd.Timestamp(as_of).normalize()
    expected = last_nasdaq_session_of_week(observation)
    if observation != expected:
        return {
            "status": "WAITING_FOR_WEEK_END", "written": False,
            "as_of": observation.strftime("%Y-%m-%d"),
            "expected_week_end": expected.strftime("%Y-%m-%d"),
            "release_status": "BLOCKED",
        }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("model_version") != MODEL_VERSION:
        raise ValueError("unexpected v8 model version")
    if manifest.get("release_status") != "BLOCKED":
        raise ValueError("v8 must remain BLOCKED")
    if observation < pd.Timestamp(manifest["forward_evidence_start"]):
        return {
            "status": "WAITING_FOR_FORWARD_START", "written": False,
            "as_of": observation.strftime("%Y-%m-%d"),
            "forward_evidence_start": manifest["forward_evidence_start"],
            "release_status": "BLOCKED",
        }
    targets, target_paths = _load_targets(model_dir, observation)
    if targets.empty:
        return {
            "status": "WAITING_FOR_FIRST_EXECUTION", "written": False,
            "as_of": observation.strftime("%Y-%m-%d"),
            "release_status": "BLOCKED",
        }
    expected_manifest_sha = _sha256(manifest_path)
    if set(targets["manifest_sha256"].astype(str)) != {expected_manifest_sha}:
        raise ValueError("recommendations do not bind the frozen v8 manifest")
    first_execution = pd.Timestamp(targets["effective_date"].min())
    qqq = pd.read_csv(qqq_path, parse_dates=["date"]).set_index("date").sort_index()
    index = qqq.loc[first_execution:observation].index
    if index.empty or index[-1] != observation:
        return {
            "status": "WAITING_FOR_WEEK_END_PRICES", "written": False,
            "as_of": observation.strftime("%Y-%m-%d"),
            "latest_qqq_date": qqq.index.max().strftime("%Y-%m-%d"),
            "release_status": "BLOCKED",
        }
    panel = targets[["effective_date", "ticker", "target_weight"]].copy()
    stock_tickers = sorted(panel.loc[~panel["ticker"].isin(["QQQ", "__CASH__"]), "ticker"].unique())
    transitions = issuer_rename_transitions()
    transition_rows = transitions.loc[transitions["historical_ticker"].isin(stock_tickers)]
    actions = pd.read_csv(CORPORATE_ACTIONS_PATH, parse_dates=["last_price_date", "effective_date"])
    action_rows = actions.loc[actions["predecessor"].isin(stock_tickers)]
    all_tickers = sorted(
        set(stock_tickers)
        | set(transition_rows["provider_ticker"])
        | set(action_rows["successor"].dropna().astype(str))
    )
    stock_close = load_stock_close_panel(all_tickers, index, price_dir=price_dir)
    dividend = qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index))
    qqq_return = qqq["close"].add(dividend).div(qqq["close"].shift(1)).sub(1.0).reindex(index).fillna(0.0)
    qqq_return.iloc[0] = 0.0
    path = simulate_continuous_whole_share(
        panel, stock_close, qqq["close"], dividend, qqq_return,
        account_size=PAPER_ACCOUNT_SIZE, transaction_cost_bps=50.0,
        execution_slippage_bps=10.0, fill_fraction=0.75,
        identity_transitions=transitions, corporate_actions=actions,
    )
    strategy_nav = float(path.loc[observation, "nav"] / PAPER_ACCOUNT_SIZE)
    qqq_nav = float((1.0 + qqq_return).cumprod().loc[observation])
    marks_path = model_dir / "weekly_marks.csv"
    marks = pd.read_csv(marks_path, parse_dates=["week_end"]) if marks_path.is_file() else pd.DataFrame()
    if not marks.empty and observation in set(marks["week_end"]):
        existing = marks.loc[marks["week_end"].eq(observation)].iloc[0]
        if abs(float(existing["strategy_nav_after_costs"]) - strategy_nav) > 1e-12:
            raise RuntimeError("weekly mark date already binds a different NAV")
        return {"status": "ALREADY_RECORDED", "written": False, "release_status": "BLOCKED"}
    if not marks.empty and observation <= marks["week_end"].max():
        raise ValueError("weekly marks are append-only")
    previous_strategy = float(marks.iloc[-1]["strategy_nav_after_costs"]) if not marks.empty else 1.0
    previous_qqq = float(marks.iloc[-1]["qqq_nav"]) if not marks.empty else 1.0
    row = pd.DataFrame([{
        "week_end": observation,
        "strategy_nav_after_costs": strategy_nav,
        "qqq_nav": qqq_nav,
        "strategy_return_after_costs": strategy_nav / previous_strategy - 1.0,
        "qqq_return": qqq_nav / previous_qqq - 1.0,
        "paper_account_size": PAPER_ACCOUNT_SIZE,
        "transaction_cost_bps": 50.0,
        "additional_slippage_bps": 10.0,
        "fill_fraction": 0.75,
        "parameters_frozen": True,
        "bindings_verified": True,
        "selected_prices_complete": True,
        "delisting_values_complete": True,
        "manifest_sha256": expected_manifest_sha,
        "qqq_input_sha256": _sha256(qqq_path),
        "target_files_sha256_json": json.dumps({str(path): _sha256(path) for path in target_paths}, sort_keys=True),
    }])
    _atomic_csv(marks_path, pd.concat([marks, row], ignore_index=True))
    return {
        "status": "RECORDED_RECONCILED_V8_WEEKLY_MARK", "written": True,
        "as_of": observation.strftime("%Y-%m-%d"),
        "strategy_nav_after_costs": strategy_nav, "qqq_nav": qqq_nav,
        "release_status": "BLOCKED", "broker_action_authorized": False,
        "output": str(marks_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()
    print(json.dumps(record_weekly_mark(as_of=args.as_of), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
