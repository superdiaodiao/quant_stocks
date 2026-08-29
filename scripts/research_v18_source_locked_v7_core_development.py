#!/usr/bin/env python3
"""Development-only replay of the source-locked v7 80/20 architecture.

The 80% QQQ core and 20% stock satellite weights come from the pre-existing v7
research family, not from the v16/v17 result.  This script applies that one
architecture to the current frozen v14 stock targets and current SHA-bound QQQ
history only through 2024-12-31.  It does not search a new weight grid.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v15_benchmark_core_development as v15
from src.conf import NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel


START = v15.START
DEVELOPMENT_END = v15.DEVELOPMENT_END
CORE_TICKER = v15.CORE_TICKER
STOCK_WEIGHT = 0.20
QQQ_WEIGHT = 0.80
OUTPUT_DIR = Path(
    "output/research_only/v18/source_locked_v7_core_development"
)

V7_SCRIPT = {
    "path": Path("scripts/research_v7_qqq_targeted_core_satellite.py"),
    "sha256": (
        "53aa5f44812c54876a66049e7831ad643efae686fdd9b636a8e8cdb2c5f7f23e"
    ),
}
V7_SUMMARY = {
    "path": Path("output/research_v7_qqq_targeted_core_satellite_summary.json"),
    "sha256": (
        "a628d44020338f96b69e4ec2a13e4ea56a37bb162ed6e248ba6dd19bb1d4793e"
    ),
}
DEVELOPMENT_GATES = {
    **v15.DEVELOPMENT_GATES,
    "minimum_2023_excess": 0.0,
    "minimum_annual_win_count_vs_qqq": 2,
    "compounded_excess_vs_qqq_threshold": 0.0,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_binding(name: str, binding: dict) -> dict:
    path = Path(binding["path"])
    actual = _sha256(path)
    if actual != binding["sha256"]:
        raise RuntimeError(f"{name} binding changed: {actual}")
    return {"path": str(path), "sha256": actual}


def source_locked_core_satellite_targets(
    targets: pd.DataFrame,
    *,
    end: str | pd.Timestamp = DEVELOPMENT_END,
    stock_weight: float = STOCK_WEIGHT,
    qqq_weight: float = QQQ_WEIGHT,
) -> pd.DataFrame:
    """Scale frozen stock targets into a fixed QQQ/stock sleeve structure."""
    if stock_weight < 0.0 or qqq_weight < 0.0:
        raise ValueError("sleeve weights must be non-negative")
    if stock_weight + qqq_weight > 1.0 + 1e-12:
        raise ValueError("sleeve weights must sum to at most one")
    required = {
        "effective_date",
        "ticker",
        "target_weight",
        "base_transaction_cost_bps",
    }
    missing = required - set(targets.columns)
    if missing:
        raise ValueError(f"v14 target columns missing: {sorted(missing)}")
    frame = targets.copy()
    frame["effective_date"] = pd.to_datetime(
        frame["effective_date"], errors="raise"
    ).dt.normalize()
    frame = frame.loc[frame["effective_date"].le(pd.Timestamp(end))]
    rows = []
    for date, group in frame.groupby("effective_date", sort=True):
        costs = group["base_transaction_cost_bps"].astype(float).unique()
        if len(costs) != 1:
            raise ValueError(f"v14 target costs changed on {date.date()}")
        stocks = group.loc[~group["ticker"].eq("__CASH__")].copy()
        original_stock_weight = float(
            stocks["target_weight"].astype(float).sum()
        )
        if original_stock_weight < -1e-12 or original_stock_weight > 1.0 + 1e-9:
            raise ValueError(f"v14 stock weight invalid on {date.date()}")
        for record in stocks.to_dict(orient="records"):
            record["target_weight"] = (
                float(record["target_weight"]) * stock_weight
            )
            rows.append(record)
        rows.append({
            "effective_date": pd.Timestamp(date),
            "ticker": CORE_TICKER,
            "target_weight": qqq_weight,
            "base_transaction_cost_bps": float(costs[0]),
        })
    result = pd.DataFrame(rows, columns=[
        "effective_date",
        "ticker",
        "target_weight",
        "base_transaction_cost_bps",
    ])
    totals = result.groupby("effective_date")["target_weight"].sum()
    if totals.gt(stock_weight + qqq_weight + 1e-9).any():
        raise RuntimeError("core-satellite target exceeds its capital budget")
    if result["effective_date"].max() > pd.Timestamp(end):
        raise RuntimeError("core-satellite target crossed development boundary")
    return result


def summarize_development(
    results: dict[int, pd.DataFrame],
    v14_daily: pd.DataFrame,
    qqq_return: pd.Series,
    gates: dict = DEVELOPMENT_GATES,
) -> dict:
    performance = v15.summarize_development(results, v14_daily, gates)
    annual_10 = performance["costs"]["10"]["annual"]
    row_2023 = next(row for row in annual_10 if int(row["year"]) == 2023)
    excess_2023 = float(row_2023["excess_vs_nasdaq"])
    passed_2023 = excess_2023 > float(gates["minimum_2023_excess"])
    qqq_rows = {}
    passed_qqq = True
    for cost, result in results.items():
        joined = pd.DataFrame({
            "strategy": result["strategy"],
            "qqq": qqq_return.reindex(result.index),
        })
        if joined["qqq"].isna().any():
            raise RuntimeError(f"QQQ return is incomplete at {cost}bps")
        annual = (1.0 + joined).groupby(joined.index.year).prod() - 1.0
        annual["excess_vs_qqq"] = annual["strategy"] - annual["qqq"]
        wins = int(annual["excess_vs_qqq"].gt(0.0).sum())
        required = int(gates["minimum_annual_win_count_vs_qqq"])
        compounded_strategy = float(
            (1.0 + joined["strategy"]).prod() - 1.0
        )
        compounded_qqq = float((1.0 + joined["qqq"]).prod() - 1.0)
        compounded_excess = compounded_strategy - compounded_qqq
        passed_wins = wins >= required
        passed_compounded = compounded_excess > float(
            gates["compounded_excess_vs_qqq_threshold"]
        )
        passed_qqq = passed_qqq and passed_wins and passed_compounded
        qqq_rows[str(cost)] = {
            "annual": [
                {"year": int(year), **row}
                for year, row in annual.to_dict(orient="index").items()
            ],
            "annual_win_count": wins,
            "required_annual_win_count": required,
            "annual_win_gate_passed": passed_wins,
            "compounded_strategy": compounded_strategy,
            "compounded_qqq": compounded_qqq,
            "compounded_excess_vs_qqq": compounded_excess,
            "compounded_gate_passed": passed_compounded,
        }
    return {
        **performance,
        "source_locked_architecture_gate": {
            "2023_excess_vs_nasdaq": excess_2023,
            "operator": ">",
            "threshold": float(gates["minimum_2023_excess"]),
            "gate_passed": passed_2023,
        },
        "qqq_relative_gates": {
            "costs": qqq_rows,
            "all_costs_passed": bool(passed_qqq),
        },
        "all_development_gates_passed": bool(
            performance["all_development_gates_passed"]
            and passed_2023
            and passed_qqq
        ),
    }


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    bindings = {
        "v14_protocol": v15._verify_binding("v14_protocol", v15.V14_PROTOCOL),
        "v14_targets": v15._verify_binding("v14_targets", v15.V14_TARGETS),
        "v14_daily": v15._verify_binding("v14_daily", v15.V14_DAILY),
        "qqq_history": v15._verify_binding("qqq_history", v15.QQQ_HISTORY),
        "qqq_provenance": v15._verify_binding(
            "qqq_provenance", v15.QQQ_PROVENANCE
        ),
        "source_v7_script": _verify_binding("source_v7_script", V7_SCRIPT),
        "source_v7_summary": _verify_binding("source_v7_summary", V7_SUMMARY),
    }
    source_summary = json.loads(
        V7_SUMMARY["path"].read_text(encoding="utf-8")
    )
    source_config = source_summary["configuration"]
    if source_config["stock_weight"] != STOCK_WEIGHT:
        raise RuntimeError("source v7 stock weight changed")
    if source_config["qqq_weight"] != QQQ_WEIGHT:
        raise RuntimeError("source v7 QQQ weight changed")
    if source_summary["release_status"] != "BLOCKED":
        raise RuntimeError("source v7 release boundary changed")
    if source_summary["promotion_eligible"]:
        raise RuntimeError("source v7 promotion boundary changed")

    protocol = json.loads(
        v15.V14_PROTOCOL["path"].read_text(encoding="utf-8")
    )
    price_binding = protocol["input_bindings"]["price_directory"]
    targets = pd.read_csv(
        v15.V14_TARGETS["path"], parse_dates=["effective_date"]
    )
    blended_targets = source_locked_core_satellite_targets(targets)
    close, _ = load_panel(price_binding["path"], "2017-01-01", DEVELOPMENT_END)
    prices = back_adjust_common_splits(close)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    qqq = pd.read_csv(
        v15.QQQ_HISTORY["path"], index_col="date", parse_dates=True
    )
    prices[CORE_TICKER] = v15.qqq_total_return_index(
        qqq,
        prices.index,
        allowed_market_closed=nasdaq.reindex(prices.index).isna(),
    )
    qqq_return = prices[CORE_TICKER].pct_change(fill_method=None).fillna(0.0)
    results = {}
    for cost in (10, 30, 50):
        stressed = blended_targets.copy()
        stressed["base_transaction_cost_bps"] = float(cost)
        result, _ = replay_can_slim_target_schedule(
            prices,
            nasdaq,
            stressed,
            START,
            DEVELOPMENT_END,
            adjust_splits=False,
        )
        results[cost] = result
    v14_daily = pd.read_csv(
        v15.V14_DAILY["path"], index_col="date", parse_dates=True
    )
    summary = summarize_development(results, v14_daily, qqq_return)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target_path = output_dir / "core_satellite_targets.csv"
    blended_targets.to_csv(target_path, index=False)
    output_paths = {"targets": target_path}
    for cost, result in results.items():
        path = output_dir / f"daily_{cost}bps.csv"
        result.to_csv(path, index_label="date")
        output_paths[f"daily_{cost}bps"] = path
    report = {
        "schema_version": 1,
        "research_only": True,
        "hypothesis": "V18_SOURCE_LOCKED_V7_CORE_ON_V14_TARGETS",
        "stage": "DEVELOPMENT_ONLY",
        "historical_selection_contaminated": True,
        "source_architecture_precedes_v16_v17": True,
        "new_weight_grid_searched": False,
        "development_period": {"start": START, "end": DEVELOPMENT_END},
        "post_development_period_computed": False,
        "post_development_results_inspected": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "source_locked_architecture": {
            "stock_weight": STOCK_WEIGHT,
            "qqq_weight": QQQ_WEIGHT,
            "rebalance_frequency": "frozen v14 monthly target events",
            "stock_target_policy": (
                "scale every frozen v14 stock target by 20%; leave an empty "
                "stock sleeve in cash when v14 is cash"
            ),
        },
        "development_gates": DEVELOPMENT_GATES,
        "development_result": summary,
        "all_development_gates_passed": summary[
            "all_development_gates_passed"
        ],
        "input_bindings": {**bindings, "price_directory": price_binding},
        "outputs": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in output_paths.items()
        },
        "interpretation_guardrail": (
            "The architecture is source-locked but historically exposed. "
            "Development success could authorize only a separately frozen, "
            "contaminated robustness replay, never release or promotion."
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
        "hypothesis": report["hypothesis"],
        "stage": report["stage"],
        "all_development_gates_passed": report[
            "all_development_gates_passed"
        ],
        "post_development_period_computed": report[
            "post_development_period_computed"
        ],
        "release_status": report["release_status"],
        "manifest": report["manifest"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
