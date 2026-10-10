#!/usr/bin/env python3
"""Audit whether 13/26-week v8 evidence can support shortened reviews.

All results reuse inspected history and are therefore diagnostic only. The
moving-block bootstrap preserves short-range dependence better than IID daily
sampling, but it does not create independent forward evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PATH = Path("output/research_v8_monthly_risk_budget_blend_50bps_daily.csv")
OUTPUT_PATH = Path("output/research_v8_short_horizon_robustness.json")
WINDOWS = {"13_weeks": 65, "26_weeks": 130}
BLOCK_SESSIONS = 21
BOOTSTRAP_SAMPLES = 10_000
RANDOM_SEED = 20260810


def relative_returns(strategy: pd.Series, benchmark: pd.Series) -> pd.Series:
    strategy = pd.to_numeric(strategy, errors="coerce")
    benchmark = pd.to_numeric(benchmark, errors="coerce")
    if strategy.isna().any() or benchmark.isna().any():
        raise ValueError("strategy and benchmark returns must be complete")
    if (strategy <= -1).any() or (benchmark <= -1).any():
        raise ValueError("returns must be greater than -100%")
    return (1.0 + strategy).div(1.0 + benchmark).sub(1.0)


def compounded_return(returns: np.ndarray | pd.Series) -> float:
    values = np.asarray(returns, dtype=float)
    return float(np.prod(1.0 + values) - 1.0)


def rolling_summary(relative: pd.Series, sessions: int) -> dict:
    if sessions <= 0 or sessions > len(relative):
        raise ValueError("sessions must fit inside the return history")
    growth = (1.0 + relative).rolling(sessions).apply(np.prod, raw=True).sub(1.0)
    growth = growth.dropna()
    return {
        "sessions": sessions,
        "overlapping_windows": int(len(growth)),
        "positive_fraction": float((growth > 0).mean()),
        "median_relative_return": float(growth.median()),
        "minimum_relative_return": float(growth.min()),
        "maximum_relative_return": float(growth.max()),
        "quantile_10": float(growth.quantile(0.10)),
        "quantile_25": float(growth.quantile(0.25)),
    }


def start_offset_summary(relative: pd.Series, sessions: int) -> dict:
    offsets = {}
    for offset in range(min(BLOCK_SESSIONS, sessions)):
        outcomes = []
        for start in range(offset, len(relative) - sessions + 1, sessions):
            outcomes.append(compounded_return(relative.iloc[start : start + sessions]))
        if outcomes:
            offsets[str(offset)] = {
                "windows": len(outcomes),
                "positive_fraction": float(np.mean(np.asarray(outcomes) > 0)),
                "minimum_relative_return": float(np.min(outcomes)),
            }
    fractions = [item["positive_fraction"] for item in offsets.values()]
    return {
        "offsets": offsets,
        "minimum_positive_fraction_across_offsets": float(min(fractions)),
        "median_positive_fraction_across_offsets": float(np.median(fractions)),
    }


def moving_block_bootstrap(
    relative: pd.Series,
    sessions: int,
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    block_sessions: int = BLOCK_SESSIONS,
    seed: int = RANDOM_SEED,
) -> dict:
    values = relative.to_numpy(dtype=float)
    if block_sessions <= 0 or block_sessions > len(values):
        raise ValueError("block_sessions must fit inside the return history")
    rng = np.random.default_rng(seed)
    block_starts = np.arange(len(values) - block_sessions + 1)
    blocks_needed = int(np.ceil(sessions / block_sessions))
    outcomes = np.empty(samples)
    for sample in range(samples):
        starts = rng.choice(block_starts, size=blocks_needed, replace=True)
        path = np.concatenate(
            [values[start : start + block_sessions] for start in starts]
        )[:sessions]
        outcomes[sample] = compounded_return(path)
    return {
        "samples": samples,
        "block_sessions": block_sessions,
        "seed": seed,
        "positive_probability": float(np.mean(outcomes > 0)),
        "median_relative_return": float(np.median(outcomes)),
        "quantile_05": float(np.quantile(outcomes, 0.05)),
        "quantile_10": float(np.quantile(outcomes, 0.10)),
        "quantile_25": float(np.quantile(outcomes, 0.25)),
    }


def event_neutralization(relative: pd.Series, sessions: int) -> dict:
    results = {}
    for count in (1, 3, 5):
        adjusted = relative.copy()
        largest = adjusted.nlargest(count).index
        adjusted.loc[largest] = 0.0
        summary = rolling_summary(adjusted, sessions)
        results[f"top_{count}_positive_days_zeroed"] = {
            "dates": [pd.Timestamp(date).strftime("%Y-%m-%d") for date in largest],
            "positive_fraction": summary["positive_fraction"],
            "minimum_relative_return": summary["minimum_relative_return"],
            "quantile_10": summary["quantile_10"],
        }
    return results


def build_report(frame: pd.DataFrame) -> dict:
    relative = relative_returns(frame["return"], frame["qqq_return"])
    relative.index = pd.to_datetime(frame["date"])
    windows = {}
    for label, sessions in WINDOWS.items():
        windows[label] = {
            "rolling": rolling_summary(relative, sessions),
            "start_offset_nonoverlapping": start_offset_summary(relative, sessions),
            "moving_block_bootstrap": moving_block_bootstrap(relative, sessions),
            "event_neutralization": event_neutralization(relative, sessions),
        }
    return {
        "schema_version": 1,
        "research_only": True,
        "historical_selection_contaminated": True,
        "independent_forward_evidence": False,
        "model_version": "can-slim-v8-monthly-risk-budget-blend-research",
        "benchmark": "QQQ total return",
        "method": (
            "Daily self-financing relative returns; overlapping rolling windows, "
            "21-session start offsets, and 21-session moving-block bootstrap."
        ),
        "windows": windows,
        "interpretation_guardrail": (
            "These diagnostics may reject an unstable short review policy but "
            "cannot promote the strategy without real frozen forward marks."
        ),
    }


def run(input_path: Path = INPUT_PATH, output_path: Path = OUTPUT_PATH) -> dict:
    frame = pd.read_csv(input_path)
    payload = build_report(frame)
    payload["input"] = {
        "path": str(input_path),
        "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
