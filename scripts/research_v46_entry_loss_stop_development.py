#!/usr/bin/env python3
"""Develop entry-loss stops around the frozen monthly stock selector.

Unlike v28's trailing stop, this risk rule does not follow a position's peak.
It exits only when adjusted close falls a fixed fraction below the price at the
latest monthly rebalance.  This avoids selling a profitable stock merely for
giving back part of an intra-month gain while still limiting a newly entered
position that immediately collapses.

The architecture was motivated after 2026 diagnostics were visible, but the
10/15/20/25 percent threshold is selected only on 2020-2025.  Training years
remain non-final evidence and v43 is untouched unless every frozen gate passes.
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
from src.research.data_quality import (
    back_adjust_common_splits,
    stock_returns_with_delisting_penalty,
)


DEVELOPMENT_START = "2020-01-01"
DEVELOPMENT_END = "2025-12-31"
DEVELOPMENT_YEARS = tuple(range(2020, 2026))
FINAL_COMPARISON_YEARS: tuple[int, ...] = ()
TRUE_PROSPECTIVE_START = "2026-08-31"
COSTS = v23.COSTS
ENTRY_LOSS_THRESHOLDS = (0.10, 0.15, 0.20, 0.25)
FORBIDDEN_ETFS = v23.FORBIDDEN_ETFS

OUTPUT_DIR = Path(
    "output/research_only/v46/entry_loss_stop_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
V45_MANIFEST = v45.DEVELOPMENT_OUTPUT_DIR / "manifest.json"
V45_SUMMARIES = v45.DEVELOPMENT_OUTPUT_DIR / "candidate_summaries.json"
V30_TARGETS = v30.RESULT_OUTPUT_DIR / "selected_targets.csv"


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: str | Path) -> dict:
    item = Path(path)
    return {"path": str(item), "sha256": _sha256(item)}


def candidate_specs() -> list[dict]:
    return [
        {
            "key": f"monthly_entry_loss_stop_{int(threshold * 100)}pct",
            "entry_loss_fraction": threshold,
            "reference_price": "adjusted close at latest monthly rebalance",
            "stop_signal_frequency": "daily_close",
            "execution": "next_trading_close",
            "sale_scope": "triggered_stock_only",
            "reentry": "next_frozen_monthly_target_only",
        }
        for threshold in ENTRY_LOSS_THRESHOLDS
    ]


def _baseline_drawdown_50bps() -> float:
    manifest = json.loads(V45_MANIFEST.read_text(encoding="utf-8"))
    if manifest["development_status"] != "BLOCKED":
        raise RuntimeError("v45 development status changed")
    if manifest["full_training_candidate"] is not None:
        raise RuntimeError("v45 unexpectedly found a full training candidate")
    row = next(
        item
        for item in manifest["training_ranking"]
        if item["candidate"] == v45.v44.BASELINE_CANDIDATE
    )
    return float(row["strategy_drawdown_50bps"])


def _validate_sources() -> dict:
    v45_manifest = json.loads(V45_MANIFEST.read_text(encoding="utf-8"))
    if v45_manifest["development_status"] != "BLOCKED":
        raise RuntimeError("v45 no longer records a blocked result")
    failed_folds = [
        int(row["test_year"])
        for row in v45_manifest["walk_forward_training_diagnostics"]
        if row["test_status"] != "PASS"
    ]
    if failed_folds != [2023]:
        raise RuntimeError(f"v45 failure diagnosis changed: {failed_folds}")
    events = v43.read_ledger(v43.LEDGER_PATH)
    if [event["event_type"] for event in events] != ["PROTOCOL_FROZEN"]:
        raise RuntimeError("v43 already has prospective evidence")
    return {
        "base_selector": v30.SELECTED_CANDIDATE,
        "v45_failed_walk_forward_years": failed_folds,
        "v45_rank_buffer_rejected": True,
        "trailing_stop_baseline_drawdown_50bps": _baseline_drawdown_50bps(),
        "2026_used_to_motivate_architecture": True,
        "2026_used_for_threshold_selection": False,
        "v43_signal_count_at_freeze": 0,
    }


def freeze_protocol(path: str | Path = PROTOCOL_PATH) -> dict:
    item = Path(path)
    if item.exists():
        raise RuntimeError(f"v46 protocol will not be overwritten: {item}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V46_ENTRY_LOSS_STOP_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "objective": (
            "Reduce concentrated downside without trailing profitable peaks "
            "or changing the frozen monthly stock selection path."
        ),
        "source_diagnosis": _validate_sources(),
        "evaluation_boundary": {
            "development_start": DEVELOPMENT_START,
            "development_end": DEVELOPMENT_END,
            "training_years": list(DEVELOPMENT_YEARS),
            "training_years_excluded_from_final_comparison": True,
            "final_comparison_years": [],
            "2019_role": (
                "base selector development and source-locked selection-path "
                "adjudication; not entry-loss threshold selection"
            ),
            "2026_used_for_parameter_selection": False,
            "architecture_isolated_from_2026": False,
            "parameter_isolated_from_2026": True,
            "true_prospective_start": TRUE_PROSPECTIVE_START,
        },
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "fixed_model": {
            "selector": v30.selected_specification(),
            "monthly_targets": _file_binding(V30_TARGETS),
            "weighting": "equal 20 percent slots; missing slots remain cash",
            "risk_off_asset": "CASH",
            "forbidden_index_etfs": sorted(FORBIDDEN_ETFS),
        },
        "risk_policy": {
            "reference_reset": "every frozen monthly target execution",
            "signal": (
                "adjusted close at or below monthly reference times one minus "
                "entry_loss_fraction"
            ),
            "execution": "next trading close after completed daily close signal",
            "monthly_target_priority_on_same_execution_date": True,
            "stopped_weight": "CASH",
            "reentry": "next frozen monthly target only",
            "not_a_trailing_stop": True,
        },
        "cost_bps": list(COSTS),
        "benchmark": "NASDAQ_COMPOSITE",
        "training_eligibility_gates": {
            "positive_each_training_year_at_50bps": True,
            "positive_compounded_excess_at_30_and_50bps": True,
            "absolute_drawdown_at_50bps_no_worse_than_v45_1x_baseline": True,
            "all_expanding_next_year_diagnostics_pass": True,
            "label": "TRAINING_DIAGNOSTIC_NOT_FINAL_EVIDENCE",
        },
        "selection_order": [
            "eligible first",
            "worst annual training excess at 50bps descending",
            "absolute strategy drawdown at 50bps ascending",
            "compounded training excess at 50bps descending",
            "turnover at 50bps ascending",
            "candidate key ascending",
        ],
        "walk_forward_folds": [
            {"selection_years": list(range(2020, year)), "test_year": year}
            for year in range(2022, 2026)
        ],
        "v43_replacement_rule": {
            "v43_remains_baseline_unless_v46_development_status_passes": True,
            "v43_must_still_have_zero_signal_events_at_replacement": True,
            "development_pass_does_not_authorize_broker_or_orders": True,
        },
        "input_bindings": {
            "runner": _file_binding(runner),
            "v28_target_and_accounting_helpers": _file_binding(Path(v28.__file__)),
            "v30_protocol": _file_binding(v30.PROTOCOL_PATH),
            "v30_manifest": _file_binding(v30.RESULT_OUTPUT_DIR / "manifest.json"),
            "v30_targets": _file_binding(V30_TARGETS),
            "v33_evaluation_helpers": _file_binding(Path(v33.__file__)),
            "v43_protocol": _file_binding(v43.PROTOCOL_PATH),
            "v43_ledger": _file_binding(v43.LEDGER_PATH),
            "v45_protocol": _file_binding(v45.PROTOCOL_PATH),
            "v45_runner": _file_binding(Path(v45.__file__)),
            "v45_manifest": _file_binding(V45_MANIFEST),
            "v45_candidate_summaries": _file_binding(V45_SUMMARIES),
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
        raise RuntimeError("v46 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v46 candidate grid changed")
    if protocol["source_diagnosis"] != _validate_sources():
        raise RuntimeError("v46 source diagnosis changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v46 file binding changed for {name}")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v46 release boundary changed")
    return protocol, _sha256(item)


def replay_with_entry_loss_stop(
    raw_close: pd.DataFrame,
    index_close: pd.Series,
    target_schedule: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    entry_loss_fraction: float,
    transaction_cost_bps: float,
) -> pd.DataFrame:
    if not 0.0 < entry_loss_fraction < 1.0:
        raise ValueError("entry loss fraction must be between zero and one")
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
    pending_exits: set[str] = set()
    cash = 1.0
    nav = 1.0
    rows = []
    cost_rate = float(transaction_cost_bps) / 10_000.0
    trading_dates = prices.index
    for position, (current_date, daily_returns) in enumerate(returns.iterrows()):
        previous_nav = nav
        position_values = position_values.mul(1.0 + daily_returns)
        pre_trade_nav = float(cash + position_values.sum())
        turnover = 0.0
        transaction_cost = 0.0
        stop_exit_count = 0
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
            entry_prices = {}
            for ticker in position_values.index[position_values.gt(1e-12)]:
                price = prices.at[current_date, ticker]
                if pd.notna(price):
                    entry_prices[str(ticker)] = float(price)
            pending_exits.clear()
        elif pending_exits:
            active_exits = [
                ticker for ticker in sorted(pending_exits)
                if float(position_values.get(ticker, 0.0)) > 1e-12
            ]
            sold = (
                float(position_values.loc[active_exits].sum())
                if active_exits
                else 0.0
            )
            transaction_cost = sold * cost_rate
            turnover = sold / pre_trade_nav if pre_trade_nav else 0.0
            cash += sold - transaction_cost
            position_values.loc[active_exits] = 0.0
            stop_exit_count = len(active_exits)
            for ticker in active_exits:
                entry_prices.pop(ticker, None)
            pending_exits.clear()
        nav = float(cash + position_values.sum())

        next_date = (
            trading_dates[position + 1]
            if position + 1 < len(trading_dates)
            else None
        )
        if next_date is not None and pd.Timestamp(next_date) not in targets:
            for ticker in position_values.index[position_values.gt(1e-12)]:
                price = prices.at[current_date, ticker]
                reference = entry_prices.get(str(ticker))
                if pd.isna(price) or reference is None:
                    continue
                if float(price) <= reference * (1.0 - entry_loss_fraction):
                    pending_exits.add(str(ticker))
        rows.append({
            "strategy": nav / previous_nav - 1.0 if previous_nav else 0.0,
            "benchmark": float(benchmark.loc[current_date]),
            "invested": float(position_values.sum() / nav) if nav else 0.0,
            "turnover": turnover,
            "transaction_cost": transaction_cost,
            "holdings": int(position_values.gt(1e-12).sum()),
            "stop_exits": stop_exit_count,
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
    forbidden = (
        set(targets["ticker"].astype(str)) - {"__CASH__"}
    ) & FORBIDDEN_ETFS
    if forbidden:
        raise RuntimeError(f"base targets contain forbidden ETFs: {sorted(forbidden)}")
    return inputs["raw_close"], inputs["nasdaq"], targets


def _selection_row(
    key: str,
    results: dict[int, pd.DataFrame],
    years: tuple[int, ...],
) -> dict:
    selected_costs = {}
    for cost in COSTS:
        result = results[cost].loc[results[cost].index.year.isin(years)]
        annual = (
            (1.0 + result[["strategy", "benchmark"]])
            .groupby(result.index.year)
            .prod()
            - 1.0
        )
        annual["excess"] = annual["strategy"] - annual["benchmark"]
        period = v33._period_metrics(results[cost], years)
        selected_costs[cost] = {
            "wins": int(annual["excess"].gt(0.0).sum()),
            "worst": float(annual["excess"].min()),
            "compounded_excess": period["compounded_excess_vs_nasdaq"],
            "drawdown": abs(period["strategy_maximum_drawdown"]),
            "turnover": period["turnover"],
        }
    baseline_drawdown = _baseline_drawdown_50bps()
    eligible = bool(
        selected_costs[30]["compounded_excess"] > 0.0
        and selected_costs[50]["compounded_excess"] > 0.0
        and selected_costs[50]["wins"] == len(years)
        and selected_costs[50]["drawdown"] <= baseline_drawdown + 1e-12
    )
    return {
        "candidate": key,
        "training_eligible": eligible,
        "selection_years": list(years),
        "required_positive_training_years": len(years),
        "positive_training_years_50bps": selected_costs[50]["wins"],
        "worst_annual_training_excess_50bps": selected_costs[50]["worst"],
        "compounded_training_excess_30bps": selected_costs[30][
            "compounded_excess"
        ],
        "compounded_training_excess_50bps": selected_costs[50][
            "compounded_excess"
        ],
        "strategy_drawdown_50bps": selected_costs[50]["drawdown"],
        "maximum_allowed_drawdown_50bps": baseline_drawdown,
        "drawdown_gate_passed": (
            selected_costs[50]["drawdown"] <= baseline_drawdown + 1e-12
        ),
        "turnover_50bps": selected_costs[50]["turnover"],
        "final_evidence": False,
    }


def select_candidate(
    results_by_candidate: dict[str, dict[int, pd.DataFrame]],
    years: tuple[int, ...] = DEVELOPMENT_YEARS,
) -> tuple[str | None, list[dict]]:
    ranking = [
        _selection_row(key, results, years)
        for key, results in results_by_candidate.items()
    ]
    ranking.sort(key=lambda row: (
        not row["training_eligible"],
        -row["worst_annual_training_excess_50bps"],
        row["strategy_drawdown_50bps"],
        -row["compounded_training_excess_50bps"],
        row["turnover_50bps"],
        row["candidate"],
    ))
    for index, row in enumerate(ranking, start=1):
        row["rank"] = index
    selected = next(
        (row["candidate"] for row in ranking if row["training_eligible"]),
        None,
    )
    return selected, ranking


def develop(
    protocol_path: str | Path = PROTOCOL_PATH,
    output_dir: str | Path = DEVELOPMENT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v46 output will not be overwritten: {output_dir}")
    raw_close, nasdaq, targets = _load_inputs()
    results_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        results = {}
        for cost in COSTS:
            daily = replay_with_entry_loss_stop(
                raw_close,
                nasdaq,
                targets,
                DEVELOPMENT_START,
                DEVELOPMENT_END,
                entry_loss_fraction=float(spec["entry_loss_fraction"]),
                transaction_cost_bps=float(cost),
            )
            results[cost] = v33._canonicalize_result(
                daily, nasdaq, DEVELOPMENT_START, DEVELOPMENT_END
            )
        results_by_candidate[spec["key"]] = results
        summaries[spec["key"]] = v33._summary(results)

    full_training_candidate, ranking = select_candidate(results_by_candidate)
    folds = []
    for fold in protocol["walk_forward_folds"]:
        years = tuple(int(year) for year in fold["selection_years"])
        fold_selected, fold_ranking = select_candidate(results_by_candidate, years)
        ranked_candidate = fold_selected or fold_ranking[0]["candidate"]
        test_year = int(fold["test_year"])
        test = v33._period_metrics(
            results_by_candidate[ranked_candidate][50], (test_year,)
        )
        folds.append({
            **fold,
            "selected_candidate": ranked_candidate,
            "training_gates_passed": fold_selected is not None,
            "test_excess_vs_nasdaq_50bps": test[
                "compounded_excess_vs_nasdaq"
            ],
            "test_status": (
                "PASS"
                if test["compounded_excess_vs_nasdaq"] > 0.0
                else "BLOCKED"
            ),
            "final_evidence": False,
        })
    all_folds_passed = bool(
        folds and all(fold["test_status"] == "PASS" for fold in folds)
    )
    selected = full_training_candidate if all_folds_passed else None

    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_path = output_dir / "candidate_ranking.csv"
    pd.DataFrame(ranking).to_csv(ranking_path, index=False)
    summaries_path = output_dir / "candidate_summaries.json"
    summaries_path.write_text(
        json.dumps(summaries, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    folds_path = output_dir / "walk_forward_training_diagnostics.csv"
    pd.DataFrame(folds).to_csv(folds_path, index=False)
    outputs = {
        "candidate_ranking": _file_binding(ranking_path),
        "candidate_summaries": _file_binding(summaries_path),
        "walk_forward_training_diagnostics": _file_binding(folds_path),
    }
    selected_spec = None
    selected_summary = None
    if selected is not None:
        selected_spec = next(
            spec for spec in protocol["candidate_grid"] if spec["key"] == selected
        )
        selected_summary = summaries[selected]
        for cost in COSTS:
            path = output_dir / f"selected_daily_{cost}bps.csv"
            results_by_candidate[selected][cost].to_csv(path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(path)

    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V46_ENTRY_LOSS_STOP_DEVELOPMENT_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "development_status": "PASS" if selected is not None else "BLOCKED",
        "full_training_candidate": full_training_candidate,
        "selected_candidate": selected,
        "selected_specification": selected_spec,
        "selected_summary": selected_summary,
        "evaluation_boundary": protocol["evaluation_boundary"],
        "training_ranking": ranking,
        "walk_forward_training_diagnostics": folds,
        "walk_forward_pass_count": sum(
            fold["test_status"] == "PASS" for fold in folds
        ),
        "all_walk_forward_folds_passed": all_folds_passed,
        "research_forward_observation_ready": selected is not None,
        "v43_supersession_eligible": selected is not None,
        "training_years_counted_as_final_wins": False,
        "final_comparison_years": [],
        "contains_index_etf_holdings": False,
        "risk_off_asset": "CASH",
        "2019_used_for_base_selector_and_path_adjudication": True,
        "2026_used_for_parameter_selection": False,
        "2026_architecture_exposure_disclosed": True,
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
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
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
