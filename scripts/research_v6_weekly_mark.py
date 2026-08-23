#!/usr/bin/env python3
"""Append one reconciled weekly v6 paper-account mark."""

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
from src.io.security_identity import issuer_rename_transitions
from src.research.shadow_evaluation import nasdaq_calendar_for_year


MODEL_VERSION = "can-slim-v6-walkforward-defensive-ensemble-shadow"
PAPER_ACCOUNT_SIZE = 25_000.0
DEFAULT_SUMMARY = Path("output/research_v6_walkforward_defensive_ensemble_shadow_summary.json")
DEFAULT_MODEL_DIR = Path("output/daily") / MODEL_VERSION
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_MARKET_DIR = Path("output/research_only/v6_market/prices")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, path)


def last_nasdaq_session_of_week(stamp: pd.Timestamp) -> pd.Timestamp:
    stamp = stamp.normalize()
    monday = stamp - pd.Timedelta(days=stamp.weekday())
    friday = monday + pd.Timedelta(days=4)
    sessions = nasdaq_calendar_for_year(stamp.year).sessions_in_range(monday, friday)
    if not len(sessions):
        raise ValueError("week contains no Nasdaq session")
    return pd.Timestamp(sessions[-1]).tz_localize(None).normalize()


def _load_targets(model_dir: Path, as_of: pd.Timestamp) -> tuple[pd.DataFrame, list[Path]]:
    paths = sorted(model_dir.glob("recommendations_*.csv"))
    frames = []
    used = []
    for path in paths:
        frame = pd.read_csv(path)
        effective = pd.to_datetime(frame["execution_date"], errors="raise")
        if effective.nunique() != 1 or effective.iloc[0] > as_of:
            continue
        frames.append(frame.assign(effective_date=effective.iloc[0]))
        used.append(path)
    if not frames:
        return pd.DataFrame(), []
    targets = pd.concat(frames, ignore_index=True)
    if targets.duplicated(["effective_date", "ticker"]).any():
        raise ValueError("duplicate target for one execution date and ticker")
    return targets, used


def record_weekly_mark(
    *,
    as_of: str | pd.Timestamp,
    summary_path: Path = DEFAULT_SUMMARY,
    model_dir: Path = DEFAULT_MODEL_DIR,
    qqq_path: Path = DEFAULT_QQQ,
    price_dir: Path = DEFAULT_MARKET_DIR,
) -> dict:
    observation = pd.Timestamp(as_of).normalize()
    expected = last_nasdaq_session_of_week(observation)
    if observation != expected:
        return {
            "status": "WAITING_FOR_WEEK_END",
            "written": False,
            "as_of": observation.strftime("%Y-%m-%d"),
            "expected_week_end": expected.strftime("%Y-%m-%d"),
            "release_status": "BLOCKED",
        }
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("model_version") != MODEL_VERSION:
        raise ValueError("unexpected v6 model version")
    if summary.get("release_status") != "BLOCKED":
        raise ValueError("v6 must remain BLOCKED")
    if observation < pd.Timestamp(summary["forward_evidence_start"]):
        return {
            "status": "WAITING_FOR_FORWARD_START",
            "written": False,
            "as_of": observation.strftime("%Y-%m-%d"),
            "forward_evidence_start": summary["forward_evidence_start"],
            "release_status": "BLOCKED",
        }
    targets, target_paths = _load_targets(model_dir, observation)
    if targets.empty:
        return {
            "status": "WAITING_FOR_FIRST_EXECUTION",
            "written": False,
            "as_of": observation.strftime("%Y-%m-%d"),
            "release_status": "BLOCKED",
        }
    expected_summary_sha = _sha256(summary_path)
    if set(targets["v6_summary_sha256"].astype(str)) != {expected_summary_sha}:
        raise ValueError("recommendations do not bind the current frozen summary")
    first_execution = pd.Timestamp(targets["effective_date"].min())
    qqq = pd.read_csv(qqq_path, parse_dates=["date"]).set_index("date").sort_index()
    index = qqq.loc[first_execution:observation].index
    if index.empty or index[-1] != observation:
        return {
            "status": "WAITING_FOR_WEEK_END_PRICES",
            "written": False,
            "as_of": observation.strftime("%Y-%m-%d"),
            "latest_qqq_date": qqq.index.max().strftime("%Y-%m-%d"),
            "release_status": "BLOCKED",
        }
    target_panel = targets[["effective_date", "ticker", "target_weight"]].copy()
    stock_tickers = sorted(
        target_panel.loc[
            ~target_panel["ticker"].isin(["QQQ", "__CASH__"]), "ticker"
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
    stock_close = load_stock_close_panel(stock_tickers, index, price_dir=price_dir)
    qqq_dividend = qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index))
    qqq_return = qqq["close"].add(qqq_dividend).div(
        qqq["close"].shift(1)
    ).sub(1.0).reindex(index).fillna(0.0)
    # The first session is the execution-close anchor. Its prior close return
    # occurred before the paper account existed and must not count forward.
    qqq_return.iloc[0] = 0.0
    path = simulate_continuous_whole_share(
        target_panel,
        stock_close,
        qqq["close"],
        qqq_dividend,
        qqq_return,
        account_size=PAPER_ACCOUNT_SIZE,
        transaction_cost_bps=50.0,
        execution_slippage_bps=10.0,
        fill_fraction=0.75,
        identity_transitions=transitions,
        corporate_actions=corporate_actions,
    )
    strategy_nav = float(path.loc[observation, "nav"] / PAPER_ACCOUNT_SIZE)
    qqq_nav = float((1.0 + qqq_return).cumprod().loc[observation])
    marks_path = model_dir / "weekly_marks.csv"
    marks = pd.read_csv(marks_path, parse_dates=["week_end"]) if marks_path.is_file() else pd.DataFrame()
    if not marks.empty and observation in set(marks["week_end"]):
        existing = marks.loc[marks["week_end"].eq(observation)].iloc[0]
        if abs(float(existing["strategy_nav_after_costs"]) - strategy_nav) > 1e-12:
            raise RuntimeError("weekly mark date already binds a different NAV")
        return {
            "status": "ALREADY_RECORDED",
            "written": False,
            "as_of": observation.strftime("%Y-%m-%d"),
            "release_status": "BLOCKED",
        }
    if not marks.empty and observation <= marks["week_end"].max():
        raise ValueError("weekly marks are append-only")
    previous_strategy_nav = (
        float(marks.sort_values("week_end").iloc[-1]["strategy_nav_after_costs"])
        if not marks.empty else 1.0
    )
    previous_qqq_nav = (
        float(marks.sort_values("week_end").iloc[-1]["qqq_nav"])
        if not marks.empty else 1.0
    )
    row = pd.DataFrame([{
        "week_end": observation,
        "strategy_nav_after_costs": strategy_nav,
        "qqq_nav": qqq_nav,
        "strategy_return_after_costs": strategy_nav / previous_strategy_nav - 1.0,
        "qqq_return": qqq_nav / previous_qqq_nav - 1.0,
        "paper_account_size": PAPER_ACCOUNT_SIZE,
        "transaction_cost_bps": 50.0,
        "additional_slippage_bps": 10.0,
        "fill_fraction": 0.75,
        "bindings_verified": True,
        "execution_reconciled": True,
        "authorized_canary_execution_reconciled": False,
        "summary_sha256": expected_summary_sha,
        "qqq_input_sha256": _sha256(qqq_path),
        "target_files_sha256_json": json.dumps(
            {str(path): _sha256(path) for path in target_paths}, sort_keys=True
        ),
    }])
    _atomic_csv(marks_path, pd.concat([marks, row], ignore_index=True))
    return {
        "status": "RECORDED_RECONCILED_WEEKLY_MARK",
        "written": True,
        "as_of": observation.strftime("%Y-%m-%d"),
        "strategy_nav_after_costs": strategy_nav,
        "qqq_nav": qqq_nav,
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
        "output": str(marks_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--qqq", type=Path, default=DEFAULT_QQQ)
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_MARKET_DIR)
    args = parser.parse_args()
    print(json.dumps(record_weekly_mark(
        as_of=args.as_of,
        summary_path=args.summary,
        model_dir=args.model_dir,
        qqq_path=args.qqq,
        price_dir=args.price_dir,
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
