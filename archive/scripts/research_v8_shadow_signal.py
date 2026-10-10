#!/usr/bin/env python3
"""Build one frozen v8 month-end target without broker side effects."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from scripts.research_v5_shadow_signal import _last_nasdaq_session_of_month
from scripts.research_v6_shadow_signal import refresh_base_state, risk_sleeves_as_of
from src.research.can_slim_daily_recommendations import generate_can_slim_shadow_recommendations


MODEL_VERSION = "can-slim-v8-monthly-risk-budget-blend-shadow"
V6_CAPITAL_WEIGHT = 0.25
V7_CAPITAL_WEIGHT = 0.75
V6_STOCK_WEIGHT = 0.25
V6_QQQ_WEIGHT_PER_RISK_ON_SLEEVE = 0.375
V7_STOCK_WEIGHT = 0.40
V7_QQQ_WEIGHT = 0.60
DEFAULT_MANIFEST = Path("output/research_v8_monthly_risk_budget_blend_shadow_summary.json")
DEFAULT_V6_COMPONENT = Path(
    "output/can_slim_walk_forward_summary_quarterly_financials_"
    "financial_age_150_365_550_proven_only_bank_v3_13d77de9.json"
)
DEFAULT_V7_COMPONENT = Path("output/research_v8_v7_frozen_component_summary.json")
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_MARKET_DIR = Path("output/research_only/v6_market/prices")
DEFAULT_MARKET_INDEX = Path("output/research_only/v6_market/nasdaq_index.csv")
DEFAULT_MARKET_UNIVERSE = Path("output/research_only/v6_market/current_universe.csv")
DEFAULT_OUTPUT_DIR = Path("output/daily") / MODEL_VERSION


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, path)


def _positive_weights(frame: pd.DataFrame) -> pd.Series:
    clean = frame.loc[
        frame["ticker"].astype(str).ne("__CASH__")
        & pd.to_numeric(frame["target_weight"], errors="raise").gt(0)
    ].copy()
    if clean["ticker"].astype(str).duplicated().any():
        raise ValueError("component recommendation contains duplicate tickers")
    return clean.set_index(clean["ticker"].astype(str))["target_weight"].astype(float)


def build_v8_recommendations(
    v6_recommendations: pd.DataFrame,
    v7_recommendations: pd.DataFrame,
    risk: dict,
    *,
    decision_date: pd.Timestamp,
    execution_date: str,
) -> pd.DataFrame:
    weights: dict[str, float] = {}
    sleeves: dict[str, set[str]] = {}
    for ticker, weight in _positive_weights(v6_recommendations).items():
        value = float(weight) * V6_STOCK_WEIGHT * V6_CAPITAL_WEIGHT
        weights[ticker] = weights.get(ticker, 0.0) + value
        sleeves.setdefault(ticker, set()).add("v6_stock")
    for ticker, weight in _positive_weights(v7_recommendations).items():
        value = float(weight) * V7_STOCK_WEIGHT * V7_CAPITAL_WEIGHT
        weights[ticker] = weights.get(ticker, 0.0) + value
        sleeves.setdefault(ticker, set()).add("v7_stock")
    qqq_weight = (
        V7_QQQ_WEIGHT * V7_CAPITAL_WEIGHT
        + int(risk["risk_on_sleeves"])
        * V6_QQQ_WEIGHT_PER_RISK_ON_SLEEVE
        * V6_CAPITAL_WEIGHT
    )
    weights["QQQ"] = weights.get("QQQ", 0.0) + qqq_weight
    sleeves.setdefault("QQQ", set()).add("qqq")
    invested = sum(weights.values())
    if invested > 1.0 + 1e-10:
        raise ValueError("v8 target weights exceed 100%")
    rows = [{
        "signal_date": decision_date.strftime("%Y-%m-%d"),
        "execution_date": execution_date,
        "ticker": ticker,
        "target_weight": weight,
        "sleeve": "+".join(sorted(sleeves[ticker])),
    } for ticker, weight in sorted(weights.items()) if weight > 0]
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
    manifest_path: Path = DEFAULT_MANIFEST,
    v6_component_path: Path = DEFAULT_V6_COMPONENT,
    v7_component_path: Path = DEFAULT_V7_COMPONENT,
    qqq_path: Path = DEFAULT_QQQ,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    price_dir: Path = DEFAULT_MARKET_DIR,
    index_path: Path = DEFAULT_MARKET_INDEX,
    universe_path: Path = DEFAULT_MARKET_UNIVERSE,
) -> dict:
    decision = pd.Timestamp(decision_date).normalize()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("model_version") != MODEL_VERSION:
        raise ValueError("unexpected v8 model version")
    if manifest.get("policy_status") != "FROZEN_FORWARD_ONLY":
        raise ValueError("v8 policy is not frozen")
    if manifest.get("release_status") != "BLOCKED":
        raise ValueError("v8 must remain BLOCKED")
    expected = _last_nasdaq_session_of_month(decision)
    if decision != expected:
        return {
            "status": "WAITING_FOR_MONTH_END_SIGNAL", "written": False,
            "decision_date": decision.strftime("%Y-%m-%d"),
            "expected_signal_date": expected.strftime("%Y-%m-%d"),
            "release_status": "BLOCKED",
        }
    if not universe_path.is_file():
        raise FileNotFoundError("v8 research-only current universe is missing")
    universe = pd.read_csv(universe_path, keep_default_na=False)
    symbols = set(universe["Symbol"].dropna().astype(str).str.upper())
    common = {
        "decision_date": decision,
        "refresh_parameters": False,
        "price_dir": price_dir,
        "index_file": index_path,
        "universe_symbols": symbols,
        "universe_file": universe_path,
    }
    v6, v6_meta = generate_can_slim_shadow_recommendations(
        summary_file=v6_component_path,
        history_file=output_dir / "v6_component_history.csv",
        **common,
    )
    v7, v7_meta = generate_can_slim_shadow_recommendations(
        summary_file=v7_component_path,
        history_file=output_dir / "v7_component_history.csv",
        **common,
    )
    if v6_meta["signal_date"] != v7_meta["signal_date"]:
        raise ValueError("v6/v7 component signal dates disagree")
    if pd.Timestamp(v6_meta["signal_date"]) != decision:
        return {
            "status": "WAITING_FOR_MONTH_END_SOURCE_DATA", "written": False,
            "decision_date": decision.strftime("%Y-%m-%d"),
            "release_status": "BLOCKED",
        }
    executions = set(v6["execution_date"].dropna().astype(str)) | set(
        v7["execution_date"].dropna().astype(str)
    )
    executions.discard("")
    if len(executions) != 1:
        raise ValueError("components do not share one execution date")
    execution_date = executions.pop()
    if pd.Timestamp(execution_date) < pd.Timestamp(manifest["forward_evidence_start"]):
        raise ValueError("refusing a pre-forward v8 execution")
    base_state_path = output_dir / "v6_base_forward_state.csv"
    v6_summary = json.loads(v6_component_path.read_text(encoding="utf-8"))
    base_state = refresh_base_state(
        v6_summary, decision, base_state_path,
        price_dir=price_dir, index_path=index_path, universe_symbols=symbols,
    )
    qqq = pd.read_csv(qqq_path, parse_dates=["date"]).set_index("date")
    v6_research = json.loads(
        Path(manifest["bindings"]["v6_component"]["path"]).read_text(encoding="utf-8")
    )
    risk = risk_sleeves_as_of(
        base_state["strategy"], qqq["close"], decision,
        lookbacks=tuple(v6_research["configuration"]["relative_strength_windows"]),
        trend_window=int(v6_research["configuration"]["qqq_trend_window"]),
    )
    recommendations = build_v8_recommendations(
        v6, v7, risk, decision_date=decision, execution_date=execution_date
    )
    recommendations["manifest_sha256"] = _sha256(manifest_path)
    recommendations["v6_component_summary_sha256"] = _sha256(v6_component_path)
    recommendations["v7_component_summary_sha256"] = _sha256(v7_component_path)
    recommendations["v6_data_manifest_sha256"] = v6["portfolio_data_manifest_sha256"].iloc[0]
    recommendations["v7_data_manifest_sha256"] = v7["portfolio_data_manifest_sha256"].iloc[0]
    output = output_dir / f"recommendations_{decision:%Y-%m-%d}.csv"
    if output.is_file():
        existing = pd.read_csv(output)
        pd.testing.assert_frame_equal(existing, recommendations, check_dtype=False)
        return {"status": "ALREADY_RECORDED", "written": False, "output": str(output), "release_status": "BLOCKED"}
    _atomic_csv(output, recommendations)
    history_path = output_dir / "monthly_decisions.csv"
    history = pd.read_csv(history_path) if history_path.is_file() else pd.DataFrame()
    row = pd.DataFrame([{
        "decision_date": decision.strftime("%Y-%m-%d"),
        "execution_date": execution_date,
        "recommendation_sha256": _sha256(output),
        "manifest_sha256": _sha256(manifest_path),
        "broker_action_authorized": False,
    }])
    _atomic_csv(history_path, pd.concat([history, row], ignore_index=True))
    return {
        "status": "RECORDED_LOCAL_V8_SHADOW_SIGNAL", "written": True,
        "decision_date": decision.strftime("%Y-%m-%d"),
        "execution_date": execution_date,
        "target_weight_sum": float(recommendations["target_weight"].sum()),
        "output": str(output), "monthly_history": str(history_path),
        "release_status": "BLOCKED", "broker_action_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision-date", required=True)
    args = parser.parse_args()
    print(json.dumps(record_signal(decision_date=args.decision_date), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
