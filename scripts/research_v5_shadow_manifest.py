#!/usr/bin/env python3
"""Freeze research-v5 as a future-only challenger without promoting it."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile


RESEARCH_MODEL_VERSION = "can-slim-v5-qqq-relative-trend-core-research"
SHADOW_MODEL_VERSION = "can-slim-v5-qqq-relative-trend-core-shadow"
FORWARD_START = "2026-08-10"
DEFAULT_RESEARCH = Path("output/research_v5_qqq_relative_trend_core_summary.json")
DEFAULT_EXECUTION = Path("output/research_v5_execution_sensitivity.json")
DEFAULT_OUTPUT = Path("output/research_v5_qqq_relative_trend_core_shadow_summary.json")


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def create_manifest(research_path: Path, execution_path: Path) -> dict:
    research = json.loads(research_path.read_text(encoding="utf-8"))
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    if research.get("model_version") != RESEARCH_MODEL_VERSION:
        raise ValueError("unexpected v5 research model version")
    if research.get("historical_selection_contaminated") is not True:
        raise ValueError("v5 historical selection contamination must be disclosed")
    if research.get("release_status") != "BLOCKED":
        raise ValueError("v5 research must remain BLOCKED")
    if research.get("promotion_eligible") is not False:
        raise ValueError("v5 research cannot already be promotion eligible")
    cost_stress = research["cost_stress"]
    if {int(cost_stress[str(cost)]["wins_vs_nasdaq"]) for cost in (10, 30, 50)} != {4}:
        raise ValueError("v5 must retain four wins at every declared cost")
    comparison = research["comparison_vs_v4_at_30bps"]
    if comparison["minimum_excess_delta"] <= 0:
        raise ValueError("v5 did not improve the worst historical annual excess")
    if comparison["maximum_drawdown_delta"] < 0:
        raise ValueError("v5 historical maximum drawdown is worse than v4")
    if execution["selected_path_integrity"]["positions_with_missing_holding_prices"] != 0:
        raise ValueError("v5 selected path has missing holding prices")
    if execution["selected_path_integrity"]["positions_with_unresolved_terminal_return"] != 0:
        raise ValueError("v5 selected path has unresolved terminal returns")
    baseline = execution["continuous_whole_share_30bps"]
    stressed = execution["execution_stress"]["results"]
    if {int(result["wins_vs_nasdaq"]) for result in baseline.values()} != {4}:
        raise ValueError("v5 whole-share account sizes did not retain four wins")
    if {int(result["wins_vs_nasdaq"]) for result in stressed.values()} != {4}:
        raise ValueError("v5 execution stress did not retain four wins")
    qqq = research["inputs"]["qqq_price"]
    if qqq["return_series"] != "close_plus_cash_dividend_on_ex_date":
        raise ValueError("v5 QQQ return series must include cash dividends")
    if qqq["missing_v4_sessions"] != len(qqq["carried_zero_return_market_holidays"]):
        raise ValueError("v5 QQQ missing sessions are not fully explained")
    return {
        "format_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_version": SHADOW_MODEL_VERSION,
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "historical_selection_contaminated": True,
        "release_reason": (
            "The relative-strength/trend overlay was formed after historical inspection. "
            "Historical and execution-stress results are diagnostics only; promotion "
            "requires a new immutable forward record."
        ),
        "forward_evidence_start": FORWARD_START,
        "observed_forward_months": 0,
        "observed_monthly_signals": 0,
        "configuration": research["configuration"],
        "promotion_policy": {
            "minimum_forward_months": 12,
            "minimum_monthly_signal_observations": 12,
            "relative_strength_warmup_sessions": 63,
            "net_excess_at_30_bps_must_be_positive": True,
            "maximum_drawdown_must_not_exceed_40pct": True,
            "parameters_must_remain_unchanged": True,
            "data_manifest_must_remain_verifiable": True,
            "selected_price_and_terminal_data_must_remain_complete": True,
            "prefreeze_signals_must_not_count_as_forward_evidence": True,
        },
        "historical_diagnostic": {
            "cost_stress": cost_stress,
            "comparison_vs_v4_at_30bps": comparison,
        },
        "execution_diagnostic": {
            "whole_share_30bps": baseline,
            "execution_stress_policy": {
                key: execution["execution_stress"][key]
                for key in (
                    "transaction_cost_bps",
                    "additional_slippage_bps",
                    "deterministic_fill_fraction",
                    "rounding_rule",
                )
            },
            "execution_stress_results": stressed,
        },
        "frozen_bindings": {
            "research_summary": {
                "path": str(research_path.resolve()),
                "sha256": _sha256(research_path),
            },
            "execution_sensitivity": {
                "path": str(execution_path.resolve()),
                "sha256": _sha256(execution_path),
            },
            "v4_daily": research["inputs"]["v4_daily"],
            "v4_frozen_summary": research["inputs"]["v4_frozen_summary"],
            "qqq_price": qqq,
            "daily_artifact": research["daily_artifact"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--execution", type=Path, default=DEFAULT_EXECUTION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = create_manifest(args.research, args.execution)
    _atomic_json(args.output, payload)
    print(json.dumps({
        "model_version": payload["model_version"],
        "policy_status": payload["policy_status"],
        "release_status": payload["release_status"],
        "output": str(args.output),
    }, indent=2))


if __name__ == "__main__":
    main()
