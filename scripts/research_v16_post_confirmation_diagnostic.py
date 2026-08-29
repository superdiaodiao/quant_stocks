#!/usr/bin/env python3
"""Decompose the frozen v16 2025 near miss without replaying the strategy.

The diagnostic reads only already-emitted daily and target CSVs.  It exactly
reconstructs each pre-cost daily return from net return, turnover, and the
frozen cost rate.  It must not execute a target replay, alter a gate, or be
interpreted as a new confirmation result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


YEAR = 2025
OUTPUT_DIR = Path(
    "output/research_only/v16/post_confirmation_diagnostic_20260829"
)
CORE_TICKER = "__QQQ_CORE__"
CASH_TICKER = "__CASH__"

INPUTS = {
    "v14_daily": {
        "path": Path(
            "output/can_slim_walk_forward_daily_"
            "research_v14_frozen_20260829_one_shot.csv"
        ),
        "sha256": (
            "38817e63084bd9c462ea0a1bafc2a7489261c4c8aecf4e264668f79f58c3b555"
        ),
    },
    "v14_targets": {
        "path": Path(
            "output/can_slim_walk_forward_targets_"
            "research_v14_frozen_20260829_one_shot.csv"
        ),
        "sha256": (
            "5a38bb620eadfb162be48e3f26cfcc378bd19c1b1cff674efbb1c8dce536f47f"
        ),
    },
    "v16_manifest": {
        "path": Path(
            "output/research_only/v16/frozen_confirmation_20260829/manifest.json"
        ),
        "sha256": (
            "ec50967c881de5f0ecb81e78f0f33fcaf538341aa3b8031e1a40f609b03788ec"
        ),
    },
    "v16_daily_10bps": {
        "path": Path(
            "output/research_only/v16/frozen_confirmation_20260829/"
            "daily_10bps.csv"
        ),
        "sha256": (
            "ee132586cadba229a1c1584f8032267a9f33998dff898fb5dc1e5f23e254922c"
        ),
    },
    "v16_daily_30bps": {
        "path": Path(
            "output/research_only/v16/frozen_confirmation_20260829/"
            "daily_30bps.csv"
        ),
        "sha256": (
            "3d8bbe2c081cac8ea84b4ea2d62196bab735881cf53a14225ad9dc4c6ed2d4d3"
        ),
    },
    "v16_daily_50bps": {
        "path": Path(
            "output/research_only/v16/frozen_confirmation_20260829/"
            "daily_50bps.csv"
        ),
        "sha256": (
            "de9973e8276de83c0211e26a0bd7bfa74d8e59db2d2e59754a68c017abc94ed0"
        ),
    },
    "v16_targets": {
        "path": Path(
            "output/research_only/v16/frozen_confirmation_20260829/"
            "frozen_targets.csv"
        ),
        "sha256": (
            "a3e2780f9cbe425cbffc7ec3bdf974515c23a8c5f9361d2f429880ac9b375467"
        ),
    },
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_inputs() -> dict[str, dict]:
    verified = {}
    for name, binding in INPUTS.items():
        actual = _sha256(binding["path"])
        if actual != binding["sha256"]:
            raise RuntimeError(f"{name} binding changed: {actual}")
        verified[name] = {
            "path": str(binding["path"]),
            "sha256": actual,
        }
    manifest = json.loads(
        INPUTS["v16_manifest"]["path"].read_text(encoding="utf-8")
    )
    if manifest["historical_gate_status"] != "BLOCKED":
        raise RuntimeError("v16 frozen result status changed")
    if manifest["gates"]["all_predeclared_gates_passed"]:
        raise RuntimeError("v16 frozen gate result changed")
    return verified


def reconstruct_pre_cost_returns(
    daily: pd.DataFrame,
    *,
    cost_bps: float,
) -> pd.DataFrame:
    """Invert the replay's exact nav-after-cost equation without rerunning."""
    required = {"strategy", "turnover"}
    missing = required - set(daily.columns)
    if missing:
        raise ValueError(f"daily columns missing: {sorted(missing)}")
    result = daily.copy()
    rate = float(cost_bps) / 10_000.0
    cost_factor = 1.0 - result["turnover"].astype(float) * rate
    if cost_factor.le(0.0).any():
        raise ValueError("transaction-cost factor must remain positive")
    result["pre_cost_strategy"] = (
        (1.0 + result["strategy"].astype(float)) / cost_factor - 1.0
    )
    result["daily_cost_return_drag"] = (
        result["pre_cost_strategy"] - result["strategy"].astype(float)
    )
    return result


def reconcile_pre_cost_paths(
    daily_by_cost: dict[int, pd.DataFrame],
) -> dict:
    """Prove that all frozen cost scenarios invert to one gross path."""
    expected_costs = [10, 30, 50]
    if sorted(daily_by_cost) != expected_costs:
        raise ValueError(
            f"cost scenarios changed: {sorted(daily_by_cost)}"
        )
    reconstructed = {
        cost: reconstruct_pre_cost_returns(daily, cost_bps=float(cost))
        for cost, daily in daily_by_cost.items()
    }
    baseline = reconstructed[10]["pre_cost_strategy"]
    maximum_absolute_error = 0.0
    for cost in (30, 50):
        candidate = reconstructed[cost]["pre_cost_strategy"]
        if not baseline.index.equals(candidate.index):
            raise RuntimeError(f"{cost}bps daily index changed")
        error = float((baseline - candidate).abs().max())
        maximum_absolute_error = max(maximum_absolute_error, error)
        if not np.allclose(
            baseline.to_numpy(),
            candidate.to_numpy(),
            rtol=0.0,
            atol=1e-12,
        ):
            raise RuntimeError(
                f"{cost}bps does not reconcile to the frozen gross path: "
                f"{error}"
            )
    return {
        "cost_bps": expected_costs,
        "all_paths_reconciled": True,
        "absolute_tolerance": 1e-12,
        "maximum_absolute_error": maximum_absolute_error,
    }


def target_event_types(targets: pd.DataFrame) -> pd.Series:
    """Classify each emitted target transition as stocks, core, or cash."""
    frame = targets.copy()
    frame["effective_date"] = pd.to_datetime(
        frame["effective_date"], errors="raise"
    ).dt.normalize()
    states = {}
    for date, group in frame.groupby("effective_date", sort=True):
        tickers = set(group["ticker"].astype(str))
        if tickers == {CASH_TICKER}:
            state = "cash"
        elif tickers == {CORE_TICKER}:
            state = "core"
        elif CASH_TICKER in tickers or CORE_TICKER in tickers:
            raise ValueError(f"mixed synthetic target state on {date.date()}")
        else:
            state = "stocks"
        states[pd.Timestamp(date)] = state
    previous = "initial"
    events = {}
    for date, state in states.items():
        events[date] = f"{previous}_to_{state}"
        previous = state
    return pd.Series(events, name="target_event_type", dtype="object")


def active_target_states(
    targets: pd.DataFrame,
    index: pd.DatetimeIndex,
) -> pd.Series:
    events = target_event_types(targets)
    states = events.str.rsplit("_to_", n=1).str[-1]
    active = states.reindex(index).ffill()
    if active.isna().any():
        raise ValueError("daily index begins before first target event")
    return active.rename("active_target_state")


def _compound(returns: pd.Series) -> float:
    return float((1.0 + returns.astype(float)).prod() - 1.0)


def summarize_costs(
    daily: pd.DataFrame,
    targets: pd.DataFrame,
    *,
    year: int = YEAR,
    cost_bps: float = 10.0,
) -> tuple[dict, pd.DataFrame]:
    reconstructed = reconstruct_pre_cost_returns(daily, cost_bps=cost_bps)
    period = reconstructed.loc[reconstructed.index.year == year].copy()
    if period.empty:
        raise ValueError(f"daily result has no rows for {year}")
    events = target_event_types(targets).reindex(period.index).fillna("none")
    period["target_event_type"] = events
    net_return = _compound(period["strategy"])
    gross_return = _compound(period["pre_cost_strategy"])
    event_rows = []
    for event_type in sorted(set(events) - {"none"}):
        mask = events.eq(event_type)
        counterfactual = period["strategy"].copy()
        counterfactual.loc[mask] = period.loc[mask, "pre_cost_strategy"]
        event_rows.append({
            "target_event_type": event_type,
            "event_count": int(mask.sum()),
            "turnover_sum": float(period.loc[mask, "turnover"].sum()),
            "net_to_event_cost_free_return_improvement": (
                _compound(counterfactual) - net_return
            ),
        })
    event_frame = pd.DataFrame(event_rows).sort_values("target_event_type")
    return {
        "year": int(year),
        "transaction_cost_bps": float(cost_bps),
        "net_strategy_return": net_return,
        "pre_cost_strategy_return": gross_return,
        "total_cost_return_drag": gross_return - net_return,
        "turnover_sum": float(period["turnover"].sum()),
        "target_event_count": int(events.ne("none").sum()),
    }, event_frame


def summarize_v14_cash_intervals(
    v14_daily: pd.DataFrame,
    v14_targets: pd.DataFrame,
    v16_daily: pd.DataFrame,
    v16_targets: pd.DataFrame,
    *,
    year: int = YEAR,
) -> dict:
    index = v16_daily.index.intersection(v14_daily.index)
    index = index[index.year == year]
    if index.empty:
        raise ValueError(f"no aligned daily rows for {year}")
    v14_state = active_target_states(v14_targets, index)
    v16_state = active_target_states(v16_targets, index)
    output = {}
    for scope, mask in {
        "v14_cash_sessions": v14_state.eq("cash"),
        "v14_stock_sessions": v14_state.eq("stocks"),
    }.items():
        output[scope] = {
            "session_count": int(mask.sum()),
            "v14_strategy_return": _compound(
                v14_daily.reindex(index).loc[mask, "strategy"]
            ),
            "v16_strategy_return": _compound(
                v16_daily.reindex(index).loc[mask, "strategy"]
            ),
            "nasdaq_return": _compound(
                v16_daily.reindex(index).loc[mask, "benchmark"]
            ),
            "v16_core_session_count": int(
                (v16_state.eq("core") & mask).sum()
            ),
            "v16_cash_session_count": int(
                (v16_state.eq("cash") & mask).sum()
            ),
        }
    return output


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    inputs = _verify_inputs()
    v14_daily = pd.read_csv(
        INPUTS["v14_daily"]["path"], index_col="date", parse_dates=True
    )
    v16_daily_by_cost = {
        cost: pd.read_csv(
            INPUTS[f"v16_daily_{cost}bps"]["path"],
            index_col="date",
            parse_dates=True,
        )
        for cost in (10, 30, 50)
    }
    gross_path_reconciliation = reconcile_pre_cost_paths(v16_daily_by_cost)
    v16_daily = v16_daily_by_cost[10]
    v14_targets = pd.read_csv(
        INPUTS["v14_targets"]["path"], parse_dates=["effective_date"]
    )
    v16_targets = pd.read_csv(
        INPUTS["v16_targets"]["path"], parse_dates=["effective_date"]
    )
    v14_costs, v14_events = summarize_costs(v14_daily, v14_targets)
    v16_costs, v16_events = summarize_costs(v16_daily, v16_targets)
    benchmark_return = _compound(
        v16_daily.loc[v16_daily.index.year == YEAR, "benchmark"]
    )
    intervals = summarize_v14_cash_intervals(
        v14_daily,
        v14_targets,
        v16_daily,
        v16_targets,
    )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    v14_event_path = output_dir / "v14_2025_cost_events.csv"
    v16_event_path = output_dir / "v16_2025_cost_events.csv"
    v14_events.to_csv(v14_event_path, index=False)
    v16_events.to_csv(v16_event_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "diagnostic_status": "POST_CONFIRMATION_READ_ONLY",
        "historical_selection_contaminated": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "strategy_replayed": False,
        "frozen_result_modified": False,
        "year": YEAR,
        "nasdaq_return": benchmark_return,
        "v14_cost_decomposition": v14_costs,
        "v16_cost_decomposition": v16_costs,
        "v16_gross_path_reconciliation": gross_path_reconciliation,
        "v16_minus_v14": {
            "net_strategy_return_improvement": (
                v16_costs["net_strategy_return"]
                - v14_costs["net_strategy_return"]
            ),
            "pre_cost_strategy_return_improvement": (
                v16_costs["pre_cost_strategy_return"]
                - v14_costs["pre_cost_strategy_return"]
            ),
            "incremental_cost_return_drag": (
                v16_costs["total_cost_return_drag"]
                - v14_costs["total_cost_return_drag"]
            ),
            "incremental_turnover": (
                v16_costs["turnover_sum"] - v14_costs["turnover_sum"]
            ),
        },
        "session_decomposition": intervals,
        "input_bindings": inputs,
        "outputs": {
            "v14_cost_events": {
                "path": str(v14_event_path),
                "sha256": _sha256(v14_event_path),
            },
            "v16_cost_events": {
                "path": str(v16_event_path),
                "sha256": _sha256(v16_event_path),
            },
        },
        "interpretation_guardrail": (
            "This post-confirmation decomposition cannot change the frozen "
            "v16 gate result or support parameter selection. It may only "
            "identify the already-observed near-miss mechanism."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "manifest": {
            "path": str(manifest_path),
            "sha256": _sha256(manifest_path),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = run(args.output_dir)
    print(json.dumps({
        "diagnostic_status": report["diagnostic_status"],
        "strategy_replayed": report["strategy_replayed"],
        "release_status": report["release_status"],
        "v16_minus_v14": report["v16_minus_v14"],
        "manifest": report["manifest"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
