#!/usr/bin/env python3
"""Evaluate one predeclared hybrid risk rule on the frozen stock targets.

v46's 20 percent monthly-entry loss stop beat Nasdaq in every 2020-2025
training year at 50 bps, but missed the predeclared drawdown ceiling by 0.4751
percentage points.  v47 adds the already-tested 25 percent monthly portfolio
trailing stop as a catastrophe backstop.  No threshold grid is searched.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v28_stock_trailing_stop_development as v28
from scripts import research_v30_2019_selection_path_adjudication as v30
from scripts import research_v33_portfolio_stop_development as v33
from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v45_rank_buffer_runtime_repair as v45
from scripts import research_v46_entry_loss_stop_development as v46
from src.research.data_quality import (
    back_adjust_common_splits,
    stock_returns_with_delisting_penalty,
)


DEVELOPMENT_START = v46.DEVELOPMENT_START
DEVELOPMENT_END = v46.DEVELOPMENT_END
DEVELOPMENT_YEARS = v46.DEVELOPMENT_YEARS
FINAL_COMPARISON_YEARS: tuple[int, ...] = ()
COSTS = v23.COSTS
ENTRY_LOSS_FRACTION = 0.20
PORTFOLIO_TRAILING_STOP_FRACTION = 0.25
CANDIDATE = "entry_loss_20pct_plus_portfolio_trailing_stop_25pct"

OUTPUT_DIR = Path(
    "output/research_only/v47/hybrid_entry_portfolio_stop_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
V46_MANIFEST = v46.DEVELOPMENT_OUTPUT_DIR / "manifest.json"
V46_SUMMARIES = v46.DEVELOPMENT_OUTPUT_DIR / "candidate_summaries.json"
V30_TARGETS = v30.RESULT_OUTPUT_DIR / "selected_targets.csv"


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: str | Path) -> dict:
    item = Path(path)
    return {"path": str(item), "sha256": _sha256(item)}


def candidate_spec() -> dict:
    return {
        "key": CANDIDATE,
        "entry_loss_fraction": ENTRY_LOSS_FRACTION,
        "entry_reference": "adjusted close at latest monthly rebalance",
        "portfolio_trailing_stop_fraction": PORTFOLIO_TRAILING_STOP_FRACTION,
        "portfolio_peak_reset": "latest monthly rebalance",
        "signal_frequency": "daily_close",
        "execution": "next_trading_close",
        "risk_off_asset": "CASH",
        "reentry": "next_frozen_monthly_target_only",
    }


def _v46_row() -> dict:
    manifest = json.loads(V46_MANIFEST.read_text(encoding="utf-8"))
    if manifest["development_status"] != "BLOCKED":
        raise RuntimeError("v46 development status changed")
    return next(
        row
        for row in manifest["training_ranking"]
        if row["candidate"] == "monthly_entry_loss_stop_20pct"
    )


def _validate_sources() -> dict:
    row = _v46_row()
    if row["positive_training_years_50bps"] != 6:
        raise RuntimeError("v46 20 percent annual-win result changed")
    if row["drawdown_gate_passed"]:
        raise RuntimeError("v46 20 percent drawdown unexpectedly passed")
    events = v43.read_ledger(v43.LEDGER_PATH)
    if [event["event_type"] for event in events] != ["PROTOCOL_FROZEN"]:
        raise RuntimeError("v43 already has prospective evidence")
    return {
        "v46_candidate": row["candidate"],
        "v46_positive_training_years_50bps": 6,
        "v46_worst_annual_training_excess_50bps": row[
            "worst_annual_training_excess_50bps"
        ],
        "v46_strategy_drawdown_50bps": row["strategy_drawdown_50bps"],
        "maximum_allowed_drawdown_50bps": row[
            "maximum_allowed_drawdown_50bps"
        ],
        "drawdown_gap_percentage_points": 100.0 * (
            row["strategy_drawdown_50bps"]
            - row["maximum_allowed_drawdown_50bps"]
        ),
        "entry_loss_threshold_source": "frozen v46 grid",
        "portfolio_stop_threshold_source": "frozen v33 grid",
        "new_threshold_search": False,
        "2026_used_for_parameter_selection": False,
        "v43_signal_count_at_freeze": 0,
    }


def freeze_protocol(path: str | Path = PROTOCOL_PATH) -> dict:
    item = Path(path)
    if item.exists():
        raise RuntimeError(f"v47 protocol will not be overwritten: {item}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V47_HYBRID_ENTRY_PORTFOLIO_STOP_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "objective": (
            "Close v46's fixed drawdown gap without searching a new threshold "
            "or changing the frozen monthly stock targets."
        ),
        "source_diagnosis": _validate_sources(),
        "evaluation_boundary": {
            "development_start": DEVELOPMENT_START,
            "development_end": DEVELOPMENT_END,
            "training_years": list(DEVELOPMENT_YEARS),
            "training_years_excluded_from_final_comparison": True,
            "final_comparison_years": [],
            "2019_role": "base selector and adjudicated selection path",
            "2026_used_for_parameter_selection": False,
            "architecture_isolated_from_2026": False,
            "parameter_isolated_from_2026": True,
            "true_prospective_start": v46.TRUE_PROSPECTIVE_START,
        },
        "candidate": candidate_spec(),
        "candidate_count": 1,
        "cost_bps": list(COSTS),
        "benchmark": "NASDAQ_COMPOSITE",
        "training_gates": {
            "positive_each_training_year_at_50bps": True,
            "positive_compounded_excess_at_30_and_50bps": True,
            "absolute_drawdown_at_50bps_no_worse_than_v45_1x_baseline": True,
            "fixed_candidate_positive_in_each_expanding_test_year": True,
            "label": "TRAINING_DIAGNOSTIC_NOT_FINAL_EVIDENCE",
        },
        "walk_forward_tests": list(range(2022, 2026)),
        "input_bindings": {
            "runner": _file_binding(runner),
            "v28_accounting_helpers": _file_binding(Path(v28.__file__)),
            "v30_targets": _file_binding(V30_TARGETS),
            "v33_portfolio_stop_source": _file_binding(Path(v33.__file__)),
            "v43_protocol": _file_binding(v43.PROTOCOL_PATH),
            "v43_ledger": _file_binding(v43.LEDGER_PATH),
            "v45_input_runtime": _file_binding(Path(v45.__file__)),
            "v46_protocol": _file_binding(v46.PROTOCOL_PATH),
            "v46_runner": _file_binding(Path(v46.__file__)),
            "v46_manifest": _file_binding(V46_MANIFEST),
            "v46_summaries": _file_binding(V46_SUMMARIES),
            "data_quality": _file_binding("src/research/data_quality.py"),
        },
        "parameters_frozen_before_development": True,
        "contains_index_etf_holdings": False,
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    item.parent.mkdir(parents=True, exist_ok=True)
    item.write_text(
        json.dumps(protocol, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**protocol, "protocol": _file_binding(item)}


def _validated_protocol(path: str | Path) -> tuple[dict, str]:
    item = Path(path)
    protocol = json.loads(item.read_text(encoding="utf-8"))
    if protocol["status"] != "FROZEN_NOT_DEVELOPED":
        raise RuntimeError("v47 protocol status changed")
    if protocol["candidate"] != candidate_spec():
        raise RuntimeError("v47 fixed candidate changed")
    if protocol["source_diagnosis"] != _validate_sources():
        raise RuntimeError("v47 source diagnosis changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v47 file binding changed for {name}")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v47 release boundary changed")
    return protocol, _sha256(item)


def replay_with_hybrid_stop(
    raw_close: pd.DataFrame,
    index_close: pd.Series,
    target_schedule: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    entry_loss_fraction: float,
    portfolio_stop_fraction: float,
    transaction_cost_bps: float,
) -> pd.DataFrame:
    if not 0.0 < entry_loss_fraction < 1.0:
        raise ValueError("entry loss fraction must be between zero and one")
    if not 0.0 < portfolio_stop_fraction < 1.0:
        raise ValueError("portfolio stop fraction must be between zero and one")
    if transaction_cost_bps < 0.0:
        raise ValueError("transaction cost must be non-negative")
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    prices = back_adjust_common_splits(raw_close).sort_index()
    returns = stock_returns_with_delisting_penalty(prices).fillna(0.0)
    benchmark = index_close.reindex(prices.index).ffill().pct_change(
        fill_method=None
    ).fillna(0.0)
    targets = v28._target_dict(prices, target_schedule, transaction_cost_bps)
    position_values = pd.Series(0.0, index=prices.columns)
    entry_prices: dict[str, float] = {}
    pending_stock_exits: set[str] = set()
    pending_portfolio_exit = False
    cash = 1.0
    nav = 1.0
    portfolio_peak = 1.0
    cost_rate = float(transaction_cost_bps) / 10_000.0
    rows = []
    dates = prices.index
    for position, (current_date, daily_returns) in enumerate(returns.iterrows()):
        previous_nav = nav
        position_values = position_values.mul(1.0 + daily_returns)
        pre_trade_nav = float(cash + position_values.sum())
        turnover = 0.0
        transaction_cost = 0.0
        stock_stop_exits = 0
        portfolio_stop_exits = 0
        target = targets.get(pd.Timestamp(current_date))
        if target is not None:
            post_trade_nav = pre_trade_nav
            for _ in range(20):
                desired = target * post_trade_nav
                traded = float((desired - position_values).abs().sum())
                updated = pre_trade_nav - traded * cost_rate
                if abs(updated - post_trade_nav) < 1e-12:
                    post_trade_nav = updated
                    break
                post_trade_nav = updated
            desired = target * post_trade_nav
            traded = float((desired - position_values).abs().sum())
            transaction_cost = traded * cost_rate
            turnover = traded / pre_trade_nav if pre_trade_nav else 0.0
            cash = float(pre_trade_nav - desired.sum() - transaction_cost)
            position_values = desired
            entry_prices = {
                str(ticker): float(prices.at[current_date, ticker])
                for ticker in position_values.index[position_values.gt(1e-12)]
                if pd.notna(prices.at[current_date, ticker])
            }
            pending_stock_exits.clear()
            pending_portfolio_exit = False
            nav = float(cash + position_values.sum())
            portfolio_peak = nav
        elif pending_portfolio_exit:
            active = position_values.index[position_values.gt(1e-12)]
            sold = float(position_values.loc[active].sum()) if len(active) else 0.0
            transaction_cost = sold * cost_rate
            turnover = sold / pre_trade_nav if pre_trade_nav else 0.0
            cash += sold - transaction_cost
            position_values.loc[active] = 0.0
            portfolio_stop_exits = int(bool(len(active)))
            entry_prices.clear()
            pending_stock_exits.clear()
            pending_portfolio_exit = False
            nav = float(cash)
        elif pending_stock_exits:
            active = [
                ticker for ticker in sorted(pending_stock_exits)
                if float(position_values.get(ticker, 0.0)) > 1e-12
            ]
            sold = float(position_values.loc[active].sum()) if active else 0.0
            transaction_cost = sold * cost_rate
            turnover = sold / pre_trade_nav if pre_trade_nav else 0.0
            cash += sold - transaction_cost
            position_values.loc[active] = 0.0
            stock_stop_exits = len(active)
            for ticker in active:
                entry_prices.pop(ticker, None)
            pending_stock_exits.clear()
            nav = float(cash + position_values.sum())
        else:
            nav = pre_trade_nav

        next_date = dates[position + 1] if position + 1 < len(dates) else None
        if next_date is not None and pd.Timestamp(next_date) not in targets:
            portfolio_peak = max(portfolio_peak, nav)
            if nav <= portfolio_peak * (1.0 - portfolio_stop_fraction):
                pending_portfolio_exit = True
                pending_stock_exits.clear()
            else:
                for ticker in position_values.index[position_values.gt(1e-12)]:
                    price = prices.at[current_date, ticker]
                    reference = entry_prices.get(str(ticker))
                    if (
                        pd.notna(price)
                        and reference is not None
                        and float(price) <= reference * (1.0 - entry_loss_fraction)
                    ):
                        pending_stock_exits.add(str(ticker))
        rows.append({
            "strategy": nav / previous_nav - 1.0 if previous_nav else 0.0,
            "benchmark": float(benchmark.loc[current_date]),
            "invested": float(position_values.sum() / nav) if nav else 0.0,
            "turnover": turnover,
            "transaction_cost": transaction_cost,
            "holdings": int(position_values.gt(1e-12).sum()),
            "stock_stop_exits": stock_stop_exits,
            "portfolio_stop_exits": portfolio_stop_exits,
            "stop_exits": stock_stop_exits + portfolio_stop_exits,
            "portfolio_value": nav,
            "cash": cash,
        })
    return pd.DataFrame(rows, index=prices.index).loc[start:end]


def _load_inputs() -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    inputs = v45._adjudicated_inputs()
    targets = pd.read_csv(V30_TARGETS, parse_dates=["effective_date"])
    targets = targets.loc[
        targets["effective_date"].between(DEVELOPMENT_START, DEVELOPMENT_END)
    ].copy()
    return inputs["raw_close"], inputs["nasdaq"], targets


def develop(
    protocol_path: str | Path = PROTOCOL_PATH,
    output_dir: str | Path = DEVELOPMENT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v47 output will not be overwritten: {output_dir}")
    raw_close, nasdaq, targets = _load_inputs()
    results = {}
    for cost in COSTS:
        daily = replay_with_hybrid_stop(
            raw_close,
            nasdaq,
            targets,
            DEVELOPMENT_START,
            DEVELOPMENT_END,
            entry_loss_fraction=ENTRY_LOSS_FRACTION,
            portfolio_stop_fraction=PORTFOLIO_TRAILING_STOP_FRACTION,
            transaction_cost_bps=float(cost),
        )
        results[cost] = v33._canonicalize_result(
            daily, nasdaq, DEVELOPMENT_START, DEVELOPMENT_END
        )
    summary = v33._summary(results)
    annual_50 = summary["costs"]["50"]["annual_training_diagnostics"]
    positive_years = sum(row["excess_vs_nasdaq"] > 0.0 for row in annual_50)
    metrics_50 = summary["costs"]["50"]
    baseline_drawdown = v46._baseline_drawdown_50bps()
    folds = []
    for year in protocol["walk_forward_tests"]:
        metrics = v33._period_metrics(results[50], (int(year),))
        folds.append({
            "candidate": CANDIDATE,
            "test_year": int(year),
            "test_excess_vs_nasdaq_50bps": metrics[
                "compounded_excess_vs_nasdaq"
            ],
            "test_status": (
                "PASS" if metrics["compounded_excess_vs_nasdaq"] > 0.0 else "BLOCKED"
            ),
            "final_evidence": False,
        })
    gates = {
        "positive_each_training_year_at_50bps": positive_years == len(DEVELOPMENT_YEARS),
        "positive_compounded_excess_at_30bps": (
            summary["costs"]["30"]["compounded_excess_vs_nasdaq"] > 0.0
        ),
        "positive_compounded_excess_at_50bps": (
            metrics_50["compounded_excess_vs_nasdaq"] > 0.0
        ),
        "absolute_drawdown_at_50bps_no_worse_than_baseline": (
            abs(metrics_50["strategy_maximum_drawdown"])
            <= baseline_drawdown + 1e-12
        ),
        "all_expanding_test_years_positive": all(
            fold["test_status"] == "PASS" for fold in folds
        ),
    }
    passed = all(gates.values())

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "candidate_summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    folds_path = output_dir / "walk_forward_training_diagnostics.csv"
    pd.DataFrame(folds).to_csv(folds_path, index=False)
    outputs = {
        "candidate_summary": _file_binding(summary_path),
        "walk_forward_training_diagnostics": _file_binding(folds_path),
    }
    if passed:
        for cost in COSTS:
            path = output_dir / f"selected_daily_{cost}bps.csv"
            results[cost].to_csv(path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V47_HYBRID_ENTRY_PORTFOLIO_STOP_DEVELOPMENT_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "development_status": "PASS" if passed else "BLOCKED",
        "candidate": CANDIDATE,
        "selected_candidate": CANDIDATE if passed else None,
        "candidate_specification": candidate_spec(),
        "candidate_summary": summary,
        "gates": gates,
        "positive_training_years_50bps": positive_years,
        "strategy_drawdown_50bps": abs(metrics_50["strategy_maximum_drawdown"]),
        "maximum_allowed_drawdown_50bps": baseline_drawdown,
        "walk_forward_training_diagnostics": folds,
        "research_forward_observation_ready": passed,
        "v43_supersession_eligible": passed,
        "training_years_counted_as_final_wins": False,
        "2026_used_for_parameter_selection": False,
        "outputs": outputs,
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = _file_binding(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--develop", action="store_true")
    parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEVELOPMENT_OUTPUT_DIR)
    args = parser.parse_args()
    if not args.freeze and not args.develop:
        parser.error("at least one of --freeze or --develop is required")
    result = None
    if args.freeze:
        result = freeze_protocol(args.protocol)
    if args.develop:
        result = develop(args.protocol, args.output_dir)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
