#!/usr/bin/env python3
"""Development-only test of filling v14 cash targets with a QQQ core.

This is a separately named exploratory hypothesis.  It preserves every v14
stock target and replaces only uninvested target weight with QQQ.  The script
is intentionally bounded to 2022-2024 and must not compute confirmation-period
returns for 2025-2026.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.conf import NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel


START = "2022-01-01"
DEVELOPMENT_END = "2024-12-31"
CORE_TICKER = "__QQQ_CORE__"
OUTPUT_DIR = Path("output/research_only/v15/benchmark_core_development")

V14_PROTOCOL = {
    "path": Path("output/research_only/v14/frozen_protocol_20260829.json"),
    "sha256": (
        "bf0e7440bce078e754443132fa02f5552a70f93c2bcf2db8dce660a66eda57b4"
    ),
}
V14_RESULT = {
    "path": Path(
        "output/research_only/v14/frozen_replay_20260829/manifest.json"
    ),
    "sha256": (
        "19db71cfc9d2c7c79257561de901b742be193a1fd15ae593a544f420d42602c8"
    ),
}
V14_TARGETS = {
    "path": Path(
        "output/can_slim_walk_forward_targets_"
        "research_v14_frozen_20260829_one_shot.csv"
    ),
    "sha256": (
        "5a38bb620eadfb162be48e3f26cfcc378bd19c1b1cff674efbb1c8dce536f47f"
    ),
}
V14_DAILY = {
    "path": Path(
        "output/can_slim_walk_forward_daily_"
        "research_v14_frozen_20260829_one_shot.csv"
    ),
    "sha256": (
        "38817e63084bd9c462ea0a1bafc2a7489261c4c8aecf4e264668f79f58c3b555"
    ),
}
QQQ_HISTORY = {
    "path": Path("output/research_only/qqq_nasdaq_history.csv"),
    "sha256": (
        "ebfb0caf80fcf539e1e3254daf368acce2012eadffa1b7e0f8e82fee1a3fd0d0"
    ),
}
QQQ_PROVENANCE = {
    "path": Path("output/research_only/qqq_nasdaq_history.provenance.json"),
    "sha256": (
        "e3eae7b8b93d6d4d56ec0d30113c620c2d252e389244f7a46da621e999e2c1b2"
    ),
}

DEVELOPMENT_GATES = {
    "annual_win_count": {
        "10": {"required": 2, "years": 3},
        "30": {"required": 2, "years": 3},
        "50": {"required": 2, "years": 3},
    },
    "compounded_excess_threshold": 0.0,
    "maximum_drawdown_loss_fraction": 0.40,
    "maximum_drawdown_underperformance_vs_nasdaq_pp": 5.0,
    "motivating_2023_excess_improvement_pp": 5.0,
    "minimum_2022_excess": -0.05,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_binding(name: str, binding: dict) -> dict:
    path = Path(binding["path"])
    actual = _sha256(path)
    if actual != binding["sha256"]:
        raise RuntimeError(f"{name} binding changed: {actual}")
    return {"path": str(path), "sha256": actual}


def qqq_total_return_index(
    qqq: pd.DataFrame,
    index: pd.DatetimeIndex,
    *,
    allowed_market_closed: pd.Series,
) -> pd.Series:
    """Build a split/dividend-aware QQQ wealth index on stock sessions."""
    frame = qqq.copy()
    frame.index = pd.to_datetime(frame.index, errors="raise")
    close = frame["close"].astype(float).reindex(index)
    allowed = allowed_market_closed.reindex(index).fillna(False).astype(bool)
    first_valid = close.first_valid_index()
    if first_valid is None:
        raise ValueError("QQQ has no price in the requested window")
    internal_missing = close.loc[first_valid:].isna()
    forbidden = internal_missing & ~allowed.loc[internal_missing.index]
    if forbidden.any():
        dates = forbidden.index[forbidden].strftime("%Y-%m-%d").tolist()
        raise ValueError(f"QQQ has non-market-closure gaps: {dates[:5]}")
    close = close.ffill()
    dividends = frame.get(
        "cash_dividend", pd.Series(0.0, index=frame.index)
    ).astype(float).reindex(index).fillna(0.0)
    returns = close.add(dividends).div(close.shift(1)).sub(1.0)
    returns.loc[first_valid] = 0.0
    if returns.loc[first_valid:].isna().any():
        raise ValueError("QQQ total-return series remains incomplete")
    wealth = (1.0 + returns.fillna(0.0)).cumprod() * 100.0
    wealth.name = CORE_TICKER
    return wealth


def fill_uninvested_target_weight(
    targets: pd.DataFrame,
    *,
    end: str | pd.Timestamp = DEVELOPMENT_END,
) -> pd.DataFrame:
    """Keep every stock target and assign only its residual weight to QQQ."""
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
    output = []
    for effective_date, group in frame.groupby("effective_date", sort=True):
        costs = group["base_transaction_cost_bps"].astype(float).unique()
        if len(costs) != 1:
            raise ValueError(f"v14 target costs changed on {effective_date.date()}")
        stocks = group.loc[~group["ticker"].eq("__CASH__")].copy()
        if stocks["ticker"].eq(CORE_TICKER).any():
            raise ValueError("v14 stock targets already contain the core ticker")
        stock_weight = float(stocks["target_weight"].astype(float).sum())
        if stock_weight < -1e-12 or stock_weight > 1.0 + 1e-9:
            raise ValueError(
                f"v14 stock target weight invalid on {effective_date.date()}"
            )
        output.extend(stocks.to_dict(orient="records"))
        residual = max(0.0, 1.0 - stock_weight)
        if residual > 1e-12:
            output.append({
                "effective_date": effective_date,
                "ticker": CORE_TICKER,
                "target_weight": residual,
                "base_transaction_cost_bps": float(costs[0]),
            })
    result = pd.DataFrame(output, columns=[
        "effective_date",
        "ticker",
        "target_weight",
        "base_transaction_cost_bps",
    ])
    totals = result.groupby("effective_date")["target_weight"].sum()
    if not totals.sub(1.0).abs().le(1e-9).all():
        raise RuntimeError("QQQ core-filled target weights do not sum to one")
    return result


def _compound(returns: pd.Series) -> float:
    return float((1.0 + returns.astype(float)).prod() - 1.0)


def _max_drawdown(returns: pd.Series) -> float:
    wealth = (1.0 + returns.astype(float)).cumprod()
    return float((wealth / wealth.cummax() - 1.0).min())


def summarize_development(
    results: dict[int, pd.DataFrame],
    v14_daily: pd.DataFrame,
    gates: dict = DEVELOPMENT_GATES,
) -> dict:
    """Apply gates declared before any v15 development result is computed."""
    expected_years = [2022, 2023, 2024]
    cost_rows = {}
    all_pass = True
    for cost in (10, 30, 50):
        result = results[cost]
        annual = (1.0 + result[["strategy", "benchmark"]]).groupby(
            result.index.year
        ).prod() - 1.0
        annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
        if annual.index.astype(int).tolist() != expected_years:
            raise RuntimeError(f"v15 development year envelope changed: {cost}")
        wins = int(annual["excess_vs_nasdaq"].gt(0).sum())
        required = int(gates["annual_win_count"][str(cost)]["required"])
        compounded_strategy = _compound(result["strategy"])
        compounded_nasdaq = _compound(result["benchmark"])
        compounded_excess = compounded_strategy - compounded_nasdaq
        passed_wins = wins >= required
        passed_compounded = compounded_excess > float(
            gates["compounded_excess_threshold"]
        )
        all_pass = all_pass and passed_wins and passed_compounded
        cost_rows[str(cost)] = {
            "annual": [
                {"year": int(year), **row}
                for year, row in annual.to_dict(orient="index").items()
            ],
            "annual_win_count": wins,
            "required_annual_win_count": required,
            "annual_win_gate_passed": passed_wins,
            "compounded_strategy": compounded_strategy,
            "compounded_nasdaq": compounded_nasdaq,
            "compounded_excess": compounded_excess,
            "compounded_gate_passed": passed_compounded,
        }

    primary = results[10]
    strategy_drawdown = _max_drawdown(primary["strategy"])
    nasdaq_drawdown = _max_drawdown(primary["benchmark"])
    drawdown_underperformance_pp = max(
        0.0, (abs(strategy_drawdown) - abs(nasdaq_drawdown)) * 100.0
    )
    passed_drawdown = (
        abs(strategy_drawdown) <= gates["maximum_drawdown_loss_fraction"]
        and drawdown_underperformance_pp
        <= gates["maximum_drawdown_underperformance_vs_nasdaq_pp"]
    )
    all_pass = all_pass and passed_drawdown

    v14_dev = v14_daily.loc[:DEVELOPMENT_END]
    v14_annual = (1.0 + v14_dev[["strategy", "benchmark"]]).groupby(
        v14_dev.index.year
    ).prod() - 1.0
    v14_2023_excess = float(
        v14_annual.loc[2023, "strategy"] - v14_annual.loc[2023, "benchmark"]
    )
    v15_2023_excess = float(
        cost_rows["10"]["annual"][1]["excess_vs_nasdaq"]
    )
    improvement_pp = (v15_2023_excess - v14_2023_excess) * 100.0
    v15_2022_excess = float(
        cost_rows["10"]["annual"][0]["excess_vs_nasdaq"]
    )
    passed_mechanism = (
        improvement_pp >= gates["motivating_2023_excess_improvement_pp"]
        and v15_2022_excess >= gates["minimum_2022_excess"]
    )
    all_pass = all_pass and passed_mechanism
    return {
        "all_development_gates_passed": bool(all_pass),
        "costs": cost_rows,
        "drawdown": {
            "strategy": strategy_drawdown,
            "nasdaq": nasdaq_drawdown,
            "underperformance_vs_nasdaq_pp": drawdown_underperformance_pp,
            "gate_passed": passed_drawdown,
        },
        "mechanism": {
            "v14_2023_excess": v14_2023_excess,
            "v15_2023_excess": v15_2023_excess,
            "improvement_pp": improvement_pp,
            "required_improvement_pp": gates[
                "motivating_2023_excess_improvement_pp"
            ],
            "v15_2022_excess": v15_2022_excess,
            "minimum_2022_excess": gates["minimum_2022_excess"],
            "gate_passed": passed_mechanism,
        },
    }


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    bindings = {
        "v14_protocol": _verify_binding("v14_protocol", V14_PROTOCOL),
        "v14_result": _verify_binding("v14_result", V14_RESULT),
        "v14_targets": _verify_binding("v14_targets", V14_TARGETS),
        "v14_daily": _verify_binding("v14_daily", V14_DAILY),
        "qqq_history": _verify_binding("qqq_history", QQQ_HISTORY),
        "qqq_provenance": _verify_binding("qqq_provenance", QQQ_PROVENANCE),
    }
    protocol = json.loads(V14_PROTOCOL["path"].read_text(encoding="utf-8"))
    if protocol["data_split"]["development_validation"]["end"] != (
        DEVELOPMENT_END
    ):
        raise RuntimeError("v14 development boundary changed")
    price_binding = protocol["input_bindings"]["price_directory"]

    targets = pd.read_csv(V14_TARGETS["path"], parse_dates=["effective_date"])
    core_targets = fill_uninvested_target_weight(targets)
    if core_targets["effective_date"].max() > pd.Timestamp(DEVELOPMENT_END):
        raise RuntimeError("v15 development target crossed confirmation boundary")
    close, _ = load_panel(price_binding["path"], "2017-11-28", DEVELOPMENT_END)
    prices = back_adjust_common_splits(close)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    qqq = pd.read_csv(QQQ_HISTORY["path"], index_col="date", parse_dates=True)
    prices[CORE_TICKER] = qqq_total_return_index(
        qqq,
        prices.index,
        allowed_market_closed=nasdaq.reindex(prices.index).isna(),
    )
    results = {}
    for cost in (10, 30, 50):
        stressed_targets = core_targets.copy()
        stressed_targets["base_transaction_cost_bps"] = float(cost)
        result, _ = replay_can_slim_target_schedule(
            prices,
            nasdaq,
            stressed_targets,
            START,
            DEVELOPMENT_END,
            adjust_splits=False,
        )
        results[cost] = result
    v14_daily = pd.read_csv(V14_DAILY["path"], index_col="date", parse_dates=True)
    summary = summarize_development(results, v14_daily)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target_path = output_dir / "core_filled_targets.csv"
    core_targets.to_csv(target_path, index=False)
    output_paths = {"core_filled_targets": target_path}
    for cost, result in results.items():
        path = output_dir / f"daily_{cost}bps.csv"
        result.to_csv(path, index_label="date")
        output_paths[f"daily_{cost}bps"] = path
    report = {
        "schema_version": 1,
        "research_only": True,
        "hypothesis": "V15_BENCHMARK_RELATIVE_QQQ_CASH_FILL",
        "stage": "DEVELOPMENT_ONLY",
        "development_period": {"start": START, "end": DEVELOPMENT_END},
        "confirmation_period_computed": False,
        "confirmation_results_inspected": False,
        "historical_selection_contaminated": True,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "stock_target_policy": "preserve every frozen v14 stock target",
        "core_policy": "fill only residual target weight with QQQ",
        "core_weight_renormalized": False,
        "development_gates": DEVELOPMENT_GATES,
        "development_result": summary,
        "input_bindings": {
            **bindings,
            "price_directory": price_binding,
        },
        "outputs": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in output_paths.items()
        },
        "interpretation_guardrail": (
            "This development run may justify freezing a separate v15 protocol. "
            "It does not repair or rerun v14 and does not inspect v15 returns "
            "after 2024-12-31."
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
        "all_development_gates_passed": report["development_result"][
            "all_development_gates_passed"
        ],
        "confirmation_period_computed": report["confirmation_period_computed"],
        "release_status": report["release_status"],
        "manifest": report["manifest"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
