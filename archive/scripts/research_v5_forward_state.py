#!/usr/bin/env python3
"""Append one auditable daily state point used by the v5 allocation signal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd


V4_MODEL_VERSION = "can-slim-v4-cost-robust-top10-shadow"
V5_MODEL_VERSION = "can-slim-v5-qqq-relative-trend-core-shadow"
DEFAULT_SUMMARY = Path("output/research_v5_qqq_relative_trend_core_shadow_summary.json")
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_OUTPUT = Path("output/daily") / V5_MODEL_VERSION / "relative_strength_history.csv"


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def append_state(
    *,
    v4_observation_path: str | Path,
    summary_path: str | Path = DEFAULT_SUMMARY,
    qqq_path: str | Path = DEFAULT_QQQ,
    output_path: str | Path = DEFAULT_OUTPUT,
) -> dict:
    v4_observation_path = Path(v4_observation_path)
    summary_path = Path(summary_path)
    qqq_path = Path(qqq_path)
    output_path = Path(output_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    observation = json.loads(v4_observation_path.read_text(encoding="utf-8"))
    if summary.get("model_version") != V5_MODEL_VERSION:
        raise ValueError("unexpected v5 shadow model version")
    if summary.get("policy_status") != "FROZEN_FORWARD_ONLY":
        raise ValueError("v5 shadow policy is not frozen")
    if summary.get("release_status") != "BLOCKED":
        raise ValueError("v5 shadow must remain BLOCKED")
    if observation.get("model_version") != V4_MODEL_VERSION:
        raise ValueError("state input is not a v4 shadow observation")
    if observation.get("status") not in {
        "EXECUTION_ANCHOR_ONLY",
        "UNANCHORED_FORWARD_OBSERVATION",
        "EXTERNALLY_ANCHORED_FORWARD_OBSERVATION",
    }:
        raise ValueError("v4 observation has an unsupported status")
    observation_date = pd.Timestamp(observation["observation_date"]).normalize()
    if observation_date < pd.Timestamp(summary["forward_evidence_start"]):
        raise ValueError("refusing to append pre-freeze v5 state")
    qqq = pd.read_csv(qqq_path, parse_dates=["date"]).set_index("date").sort_index()
    if observation_date not in qqq.index:
        raise ValueError("QQQ close missing on v4 observation date")
    qqq_close = float(qqq.loc[observation_date, "close"])
    qqq_dividend = float(
        qqq.loc[observation_date, "cash_dividend"]
        if "cash_dividend" in qqq.columns else 0.0
    )
    history = pd.read_csv(output_path, parse_dates=["date"]) if output_path.is_file() else pd.DataFrame()
    if not history.empty and observation_date in set(history["date"]):
        existing = history.loc[history["date"].eq(observation_date)].iloc[0]
        if existing["v4_observation_sha256"] != _sha256(v4_observation_path):
            raise RuntimeError("v5 state date already binds a different v4 observation")
        return {
            "status": "ALREADY_RECORDED",
            "written": False,
            "date": observation_date.strftime("%Y-%m-%d"),
            "output": str(output_path),
        }
    if not history.empty and observation_date <= history["date"].max():
        raise ValueError("v5 state history is append-only")
    signal_date = str(observation["signal_date"])
    if history.empty:
        period_start_nav = 1.0
        prior_qqq_nav = 1.0
        prior_qqq_close = qqq_close
    else:
        prior = history.sort_values("date").iloc[-1]
        prior_qqq_nav = float(prior["qqq_total_return_nav"])
        prior_qqq_close = float(prior["qqq_close"])
        same_period = str(prior["v4_signal_date"]) == signal_date
        period_start_nav = (
            float(prior["v4_period_start_nav"])
            if same_period else float(prior["v4_nav"])
        )
    strategy_return = observation.get("strategy_return")
    v4_nav = (
        period_start_nav
        if strategy_return is None
        else period_start_nav * (1.0 + float(strategy_return))
    )
    qqq_total_return_nav = prior_qqq_nav * (
        (qqq_close + qqq_dividend) / prior_qqq_close
    )
    row = pd.DataFrame([{
        "date": observation_date,
        "v4_nav": v4_nav,
        "v4_period_start_nav": period_start_nav,
        "v4_signal_date": signal_date,
        "v4_execution_date": observation["execution_date"],
        "qqq_close": qqq_close,
        "qqq_cash_dividend": qqq_dividend,
        "qqq_total_return_nav": qqq_total_return_nav,
        "v4_observation_status": observation["status"],
        "v4_nav_accounting_method": (
            "chained_monthly_standalone_fixed_positions_with_full_entry_cost"
        ),
        "v4_observation_sha256": _sha256(v4_observation_path),
        "qqq_input_sha256": _sha256(qqq_path),
        "v5_frozen_summary_sha256": _sha256(summary_path),
        "externally_anchored": observation["status"]
        == "EXTERNALLY_ANCHORED_FORWARD_OBSERVATION",
        "counts_as_promotion_evidence": False,
    }])
    combined = pd.concat([history, row], ignore_index=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    combined.to_csv(temporary, index=False)
    os.replace(temporary, output_path)
    return {
        "status": "RECORDED_V5_SIGNAL_STATE",
        "written": True,
        "date": observation_date.strftime("%Y-%m-%d"),
        "v4_nav": v4_nav,
        "qqq_total_return_nav": qqq_total_return_nav,
        "externally_anchored": bool(row["externally_anchored"].iloc[0]),
        "counts_as_promotion_evidence": False,
        "output": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v4-observation", type=Path, required=True)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--qqq", type=Path, default=DEFAULT_QQQ)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(append_state(
        v4_observation_path=args.v4_observation,
        summary_path=args.summary,
        qqq_path=args.qqq,
        output_path=args.output,
    ), indent=2))


if __name__ == "__main__":
    main()
