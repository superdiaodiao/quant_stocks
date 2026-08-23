#!/usr/bin/env python3
"""Report v4 forward-only progress without relaxing its frozen promotion gate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd


MODEL_VERSION = "can-slim-v4-cost-robust-top10-shadow"
DEFAULT_SUMMARY = Path("output/research_v4_cost_robust_top10_shadow_summary.json")
DEFAULT_MODEL_DIR = Path("output/daily") / MODEL_VERSION


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_evaluation(path: Path) -> dict:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("model_version") not in {None, MODEL_VERSION}:
        raise ValueError("v4 evaluation has an unexpected model version")
    if payload.get("transaction_cost_bps") not in {None, 30, 30.0}:
        raise ValueError("v4 promotion evaluation must use 30 bps")
    return payload


def build_status(
    *,
    summary_path: str | Path = DEFAULT_SUMMARY,
    model_dir: str | Path = DEFAULT_MODEL_DIR,
) -> dict:
    summary_path = Path(summary_path)
    model_dir = Path(model_dir)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("model_version") != MODEL_VERSION:
        raise ValueError("unexpected v4 shadow model version")
    if summary.get("policy_status") != "FROZEN_FORWARD_ONLY":
        raise ValueError("v4 shadow policy is not frozen")
    if summary.get("release_status") != "BLOCKED":
        raise ValueError("v4 shadow release status must remain BLOCKED")
    if summary.get("promotion_eligible") is not False:
        raise ValueError("v4 frozen summary cannot already be promotion eligible")

    policy = summary["promotion_policy"]
    history_path = model_dir / "recommendation_history.csv"
    signal_dates: list[str] = []
    manifest_sha_values: list[str] = []
    strategy_sha_values: list[str] = []
    if history_path.is_file() and history_path.stat().st_size:
        history = pd.read_csv(history_path)
        if not history.empty:
            versions = set(history["model_version"].dropna().astype(str))
            if versions != {MODEL_VERSION}:
                raise ValueError("v4 shadow history contains another model version")
            signal_dates = sorted(
                pd.to_datetime(history["signal_date"], errors="raise")
                .dt.strftime("%Y-%m-%d")
                .unique()
                .tolist()
            )
            manifest_sha_values = sorted(
                history["portfolio_data_manifest_sha256"]
                .dropna().astype(str).unique().tolist()
            )
            strategy_sha_values = sorted(
                history["portfolio_strategy_sha256"]
                .dropna().astype(str).unique().tolist()
            )

    evaluation_path = model_dir / "shadow_evaluation_30bps.json"
    evaluation = _load_evaluation(evaluation_path)
    completed_months = int(
        evaluation.get("contiguous_completed_forward_periods", 0)
    )
    signal_observations = len(signal_dates)
    strategy_return = evaluation.get("contiguous_forward_strategy_return")
    benchmark_return = evaluation.get("contiguous_forward_benchmark_return")
    net_excess = (
        float(strategy_return - benchmark_return)
        if strategy_return is not None and benchmark_return is not None
        else None
    )
    maximum_drawdown = evaluation.get("contiguous_forward_maximum_drawdown")

    expected_manifest = summary["source_evidence"]["data_manifest_sha256"]
    expected_strategy = summary["source_evidence"]["strategy_code_sha256"]
    gates = {
        "minimum_forward_months": completed_months
        >= int(policy["minimum_forward_months"]),
        "minimum_monthly_signal_observations": signal_observations
        >= int(policy["minimum_monthly_signal_observations"]),
        "net_excess_at_30_bps_positive": net_excess is not None
        and net_excess > 0,
        "maximum_drawdown_not_over_40pct": maximum_drawdown is not None
        and maximum_drawdown >= -0.40,
        "parameters_unchanged": True,
        "data_manifest_verifiable": bool(manifest_sha_values)
        and manifest_sha_values == [expected_manifest],
        "strategy_code_verifiable": bool(strategy_sha_values)
        and strategy_sha_values == [expected_strategy],
        "selected_price_and_terminal_data_complete": False,
    }
    # This reporter deliberately cannot promote the model.  Price/terminal
    # completeness and the final release decision require a separate audited
    # review after the full forward window exists.
    return {
        "schema_version": 1,
        "research_only": True,
        "model_version": MODEL_VERSION,
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "forward_evidence_start": summary["forward_evidence_start"],
        "observed_forward_months": completed_months,
        "monthly_signal_observations": signal_observations,
        "latest_signal_date": signal_dates[-1] if signal_dates else None,
        "net_excess_at_30_bps": net_excess,
        "forward_maximum_drawdown": maximum_drawdown,
        "gates": gates,
        "unsatisfied_gates": [name for name, passed in gates.items() if not passed],
        "frozen_bindings": {
            "summary_path": str(summary_path.resolve()),
            "summary_sha256": _sha256(summary_path),
            "data_manifest_sha256": expected_manifest,
            "strategy_code_sha256": expected_strategy,
            "quarterly_input_sha256": summary["quarterly_input"]["sha256"],
        },
        "history_path": str(history_path),
        "evaluation_path": str(evaluation_path),
        "release_block_reason": (
            "Promotion requires 12 forward months, 12 monthly signals, positive "
            "30 bps net excess, drawdown no worse than 40%, unchanged frozen "
            "inputs, and a separate selected-price/terminal-data completeness audit."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = build_status(summary_path=args.summary, model_dir=args.model_dir)
    output = args.output or args.model_dir / "forward_status.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({**result, "output": str(output)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
