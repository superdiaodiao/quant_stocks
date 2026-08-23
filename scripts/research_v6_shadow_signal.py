#!/usr/bin/env python3
"""Record one frozen v6 month-end target without broker side effects."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.research_v5_shadow_signal import _last_nasdaq_session_of_month
from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE, POINT_IN_TIME_EPS_FILE
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim import CanSlimConfig, calculate_can_slim_scheduled_returns
from src.research.can_slim_daily_recommendations import (
    quarterly_input_from_summary,
    generate_can_slim_shadow_recommendations,
)
from src.research.can_slim_walk_forward import configs_from_snapshots
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of


MODEL_VERSION = "can-slim-v6-walkforward-defensive-ensemble-shadow"
DEFAULT_SUMMARY = Path("output/research_v6_walkforward_defensive_ensemble_shadow_summary.json")
DEFAULT_BASE_STATE = Path(
    "output/daily/can-slim-v6-walkforward-defensive-ensemble-shadow/"
    "base_forward_state.csv"
)
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_MARKET_DIR = Path("output/research_only/v6_market/prices")
DEFAULT_MARKET_INDEX = Path("output/research_only/v6_market/nasdaq_index.csv")
DEFAULT_MARKET_UNIVERSE = Path("output/research_only/v6_market/current_universe.csv")
DEFAULT_OUTPUT_DIR = Path("output/daily") / MODEL_VERSION


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


def refresh_base_state(
    summary: dict,
    decision_date: pd.Timestamp,
    output_path: Path,
    price_dir: Path = DEFAULT_MARKET_DIR,
    index_path: Path = DEFAULT_MARKET_INDEX,
    universe_symbols: set[str] | None = None,
) -> pd.DataFrame:
    """Replay only frozen base snapshots through the requested decision date."""
    state_start = decision_date - pd.Timedelta(days=240)
    load_start = state_start - pd.Timedelta(days=450)
    close, dollar_volume = load_panel(
        price_dir,
        load_start.strftime("%Y-%m-%d"),
        decision_date.strftime("%Y-%m-%d"),
    )
    nasdaq = pd.read_csv(
        index_path, index_col="date", parse_dates=True
    )["close"]
    quarterly_path, _ = quarterly_input_from_summary(summary)
    quarterly = load_quarterly_fundamentals(quarterly_path)
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    universe_snapshots = load_universe_snapshots()
    snapshots = [
        {
            **snapshot,
            "effective_start": pd.Timestamp(snapshot["effective_start"]),
            "configs": [CanSlimConfig(**values) for values in snapshot["configs"]],
        }
        for snapshot in summary["model_snapshots"]
    ]
    result = calculate_can_slim_scheduled_returns(
        close,
        dollar_volume,
        nasdaq,
        eps,
        state_start.strftime("%Y-%m-%d"),
        decision_date.strftime("%Y-%m-%d"),
        lambda date: configs_from_snapshots(snapshots, date),
        lambda date: (
            universe_symbols
            if universe_symbols is not None
            and pd.Timestamp(date) >= decision_date.to_period("M").start_time
            else universe_as_of(universe_snapshots, date)
        ),
        "monthly",
        quarterly,
    )
    if result.empty or result.index.max() != decision_date:
        latest = result.index.max().strftime("%Y-%m-%d") if not result.empty else None
        raise ValueError(
            f"base price state does not reach decision date; latest={latest}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    result.to_csv(temporary, index_label="date")
    os.replace(temporary, output_path)
    return result


def risk_sleeves_as_of(
    base_return: pd.Series,
    qqq_close: pd.Series,
    decision_date: pd.Timestamp,
    *,
    lookbacks: tuple[int, ...] = (42, 45),
    trend_window: int = 100,
) -> dict:
    base = base_return.sort_index().loc[:decision_date]
    qqq = qqq_close.sort_index().reindex(base.index).ffill()
    if base.empty or base.index[-1] != decision_date:
        raise ValueError("base state does not extend through the decision date")
    if qqq.isna().any() or len(qqq) < max(max(lookbacks) + 1, trend_window):
        raise ValueError("insufficient contiguous QQQ/base warmup")
    trend_on = bool(qqq.iloc[-1] > qqq.tail(trend_window).mean())
    sleeves = []
    for lookback in lookbacks:
        stock_return = float((1.0 + base.tail(lookback)).prod() - 1.0)
        qqq_return = float(qqq.iloc[-1] / qqq.iloc[-lookback - 1] - 1.0)
        leads = stock_return >= qqq_return
        sleeves.append({
            "lookback": int(lookback),
            "stock_return": stock_return,
            "qqq_return": qqq_return,
            "stock_leads": bool(leads),
            "qqq_trend_on": trend_on,
            "risk_on": bool(leads or trend_on),
        })
    return {
        "sleeves": sleeves,
        "risk_on_sleeves": int(sum(item["risk_on"] for item in sleeves)),
        "qqq_trend_on": trend_on,
    }


def build_v6_recommendations(
    base_recommendations: pd.DataFrame,
    risk: dict,
    *,
    decision_date: pd.Timestamp,
    execution_date: str,
) -> pd.DataFrame:
    positive = base_recommendations.loc[
        base_recommendations["target_weight"].astype(float).gt(0.0)
        & base_recommendations["ticker"].astype(str).ne("__CASH__")
    ].copy()
    rows = []
    for position in positive.itertuples(index=False):
        rows.append({
            "signal_date": decision_date.strftime("%Y-%m-%d"),
            "execution_date": execution_date,
            "ticker": str(position.ticker),
            "target_weight": float(position.target_weight) * 0.25,
            "sleeve": "walk_forward_stock",
        })
    qqq_weight = 0.375 * int(risk["risk_on_sleeves"])
    if qqq_weight:
        rows.append({
            "signal_date": decision_date.strftime("%Y-%m-%d"),
            "execution_date": execution_date,
            "ticker": "QQQ",
            "target_weight": qqq_weight,
            "sleeve": "qqq",
        })
    invested = sum(float(row["target_weight"]) for row in rows)
    if invested > 1.0 + 1e-12:
        raise ValueError("v6 target weights exceed 100%")
    rows.append({
        "signal_date": decision_date.strftime("%Y-%m-%d"),
        "execution_date": execution_date,
        "ticker": "__CASH__",
        "target_weight": max(0.0, 1.0 - invested),
        "sleeve": "cash",
    })
    result = pd.DataFrame(rows)
    result["model_version"] = MODEL_VERSION
    result["release_status"] = "BLOCKED"
    result["broker_action_authorized"] = False
    result["risk_on_sleeves"] = int(risk["risk_on_sleeves"])
    result["risk_evidence_json"] = json.dumps(risk, sort_keys=True)
    return result


def record_signal(
    *,
    decision_date: str | pd.Timestamp,
    summary_path: Path = DEFAULT_SUMMARY,
    base_state_path: Path = DEFAULT_BASE_STATE,
    qqq_path: Path = DEFAULT_QQQ,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    refresh_state: bool = True,
    price_dir: Path = DEFAULT_MARKET_DIR,
    index_path: Path = DEFAULT_MARKET_INDEX,
    universe_path: Path = DEFAULT_MARKET_UNIVERSE,
) -> dict:
    decision = pd.Timestamp(decision_date).normalize()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("model_version") != MODEL_VERSION:
        raise ValueError("unexpected v6 model version")
    if summary.get("policy_status") != "FROZEN_FORWARD_ONLY":
        raise ValueError("v6 policy is not frozen")
    if summary.get("release_status") != "BLOCKED":
        raise ValueError("v6 must remain BLOCKED")
    expected = _last_nasdaq_session_of_month(decision)
    if decision != expected:
        return {
            "status": "WAITING_FOR_MONTH_END_SIGNAL",
            "written": False,
            "decision_date": decision.strftime("%Y-%m-%d"),
            "expected_signal_date": expected.strftime("%Y-%m-%d"),
            "release_status": "BLOCKED",
        }

    history_path = output_dir / "base_recommendation_history.csv"
    if not universe_path.is_file():
        raise FileNotFoundError("v6 research-only current universe is missing")
    universe_frame = pd.read_csv(universe_path)
    universe_symbols = set(
        universe_frame["Symbol"].dropna().astype(str).str.upper()
    )
    base_recommendations, metadata = generate_can_slim_shadow_recommendations(
        decision_date=decision,
        summary_file=summary_path,
        history_file=history_path,
        refresh_parameters=False,
        price_dir=price_dir,
        index_file=index_path,
        universe_symbols=universe_symbols,
        universe_file=universe_path,
    )
    if pd.Timestamp(metadata["signal_date"]) != decision:
        return {
            "status": "WAITING_FOR_MONTH_END_SOURCE_DATA",
            "written": False,
            "decision_date": decision.strftime("%Y-%m-%d"),
            "latest_available_as_of": metadata["as_of"],
            "latest_available_signal_date": metadata["signal_date"],
            "release_status": "BLOCKED",
        }
    execution_values = base_recommendations["execution_date"].dropna().astype(str)
    execution_values = execution_values.loc[execution_values.ne("")].unique()
    if len(execution_values) != 1:
        raise ValueError("base recommendation lacks one execution date")
    execution_date = str(execution_values[0])
    if pd.Timestamp(execution_date) < pd.Timestamp(summary["forward_evidence_start"]):
        raise ValueError("refusing a pre-forward v6 execution")
    base_state = (
        refresh_base_state(
            summary, decision, base_state_path,
            price_dir=price_dir, index_path=index_path,
            universe_symbols=universe_symbols,
        )
        if refresh_state
        else pd.read_csv(base_state_path, parse_dates=["date"]).set_index("date")
    )
    qqq = pd.read_csv(qqq_path, parse_dates=["date"]).set_index("date")
    risk = risk_sleeves_as_of(
        base_state["strategy"], qqq["close"], decision,
        lookbacks=tuple(summary["frozen_configuration"]["relative_strength_windows"]),
        trend_window=int(summary["frozen_configuration"]["qqq_trend_window"]),
    )
    recommendations = build_v6_recommendations(
        base_recommendations, risk,
        decision_date=decision, execution_date=execution_date,
    )
    recommendations["v6_summary_sha256"] = _sha256(summary_path)
    recommendations["base_state_sha256"] = _sha256(base_state_path)
    recommendations["qqq_input_sha256"] = _sha256(qqq_path)
    recommendations["universe_input_sha256"] = _sha256(universe_path)
    recommendations["base_data_manifest_sha256"] = base_recommendations[
        "portfolio_data_manifest_sha256"
    ].iloc[0]
    recommendations["base_strategy_sha256"] = base_recommendations[
        "portfolio_strategy_sha256"
    ].iloc[0]

    output = output_dir / f"recommendations_{decision:%Y-%m-%d}.csv"
    if output.is_file():
        existing = pd.read_csv(output)
        pd.testing.assert_frame_equal(existing, recommendations, check_dtype=False)
        return {
            "status": "ALREADY_RECORDED",
            "written": False,
            "decision_date": decision.strftime("%Y-%m-%d"),
            "output": str(output),
            "release_status": "BLOCKED",
        }
    _atomic_csv(output, recommendations)
    monthly_history = output_dir / "monthly_decisions.csv"
    old = pd.read_csv(monthly_history) if monthly_history.is_file() else pd.DataFrame()
    decision_row = pd.DataFrame([{
        "decision_date": decision.strftime("%Y-%m-%d"),
        "execution_date": execution_date,
        "risk_on_sleeves": int(risk["risk_on_sleeves"]),
        "recommendation_sha256": _sha256(output),
        "summary_sha256": _sha256(summary_path),
        "base_state_sha256": _sha256(base_state_path),
        "qqq_input_sha256": _sha256(qqq_path),
        "universe_input_sha256": _sha256(universe_path),
        "broker_action_authorized": False,
    }])
    _atomic_csv(monthly_history, pd.concat([old, decision_row], ignore_index=True))
    return {
        "status": "RECORDED_LOCAL_V6_SHADOW_SIGNAL",
        "written": True,
        "decision_date": decision.strftime("%Y-%m-%d"),
        "execution_date": execution_date,
        "risk_on_sleeves": int(risk["risk_on_sleeves"]),
        "target_weight_sum": float(recommendations["target_weight"].sum()),
        "output": str(output),
        "monthly_history": str(monthly_history),
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
        "base_metadata": metadata,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision-date", required=True)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--base-state", type=Path, default=DEFAULT_BASE_STATE)
    parser.add_argument("--qqq", type=Path, default=DEFAULT_QQQ)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_MARKET_DIR)
    parser.add_argument("--index", type=Path, default=DEFAULT_MARKET_INDEX)
    parser.add_argument("--universe", type=Path, default=DEFAULT_MARKET_UNIVERSE)
    parser.add_argument("--no-refresh-base-state", action="store_true")
    args = parser.parse_args()
    print(json.dumps(record_signal(
        decision_date=args.decision_date,
        summary_path=args.summary,
        base_state_path=args.base_state,
        qqq_path=args.qqq,
        output_dir=args.output_dir,
        refresh_state=not args.no_refresh_base_state,
        price_dir=args.price_dir,
        index_path=args.index,
        universe_path=args.universe,
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
