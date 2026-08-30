#!/usr/bin/env python3
"""Develop a monthly rank buffer around the frozen v43 stock model.

The selected names, point-in-time profitability/liquidity rules, monthly
calendar, equal weights, 25 percent individual trailing stop, and cash-only
risk-off asset remain unchanged.  The only candidate dimension is how far a
currently held Top-5 name may fall in the signal-date ranking before it is
replaced at the next monthly rebalance.

This architecture was proposed after the researcher had seen 2026 diagnostics,
so 2020-2025 results are training evidence only.  No 2026 return is read or
used for parameter selection.  v43 remains the prospective baseline unless a
candidate beats Nasdaq in every training year at 50 bps, does not worsen the
baseline drawdown, and passes every expanding next-year diagnostic.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v26_large_liquid_stock_momentum as v26
from scripts import research_v28_stock_trailing_stop_development as v28
from scripts import research_v30_2019_selection_path_adjudication as v30
from scripts import research_v33_portfolio_stop_development as v33
from scripts import research_v43_isolated_prospective_v28_observation as v43
from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.research.universe_history import snapshot_directory


DEVELOPMENT_START = "2020-01-01"
DEVELOPMENT_END = "2025-12-31"
DEVELOPMENT_YEARS = tuple(range(2020, 2026))
FINAL_COMPARISON_YEARS: tuple[int, ...] = ()
TRUE_PROSPECTIVE_START = "2026-08-31"
COSTS = v23.COSTS
RANK_BUFFER_MULTIPLES = (1, 2, 3)
TOP_N = 5
TRAILING_STOP_FRACTION = 0.25
BASELINE_CANDIDATE = "rank_buffer_1x_individual_stop_25pct"

OUTPUT_DIR = Path(
    "output/research_only/v44/monthly_rank_buffer_stop_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
V28_MANIFEST = v28.DEVELOPMENT_OUTPUT_DIR / "manifest.json"
V30_MANIFEST = v30.RESULT_OUTPUT_DIR / "manifest.json"
V30_TARGETS = v30.RESULT_OUTPUT_DIR / "selected_targets.csv"
V43_PROTOCOL = v43.PROTOCOL_PATH
V43_LEDGER = v43.LEDGER_PATH


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: str | Path) -> dict:
    item = Path(path)
    return {"path": str(item), "sha256": _sha256(item)}


def _directory_binding(path: str | Path, pattern: str) -> dict:
    return v26._directory_binding(Path(path), pattern)


def candidate_specs() -> list[dict]:
    return [
        {
            "key": (
                f"rank_buffer_{multiple}x_individual_stop_"
                f"{int(TRAILING_STOP_FRACTION * 100)}pct"
            ),
            "rank_buffer_multiple": multiple,
            "top_n": TOP_N,
            "individual_trailing_stop_fraction": TRAILING_STOP_FRACTION,
            "signal_frequency": "monthly",
            "weighting": "equal_weight",
            "stopped_position_reentry": "next_monthly_rebalance_only",
        }
        for multiple in RANK_BUFFER_MULTIPLES
    ]


def buffered_selection(
    ranked: list[str],
    previous: list[str],
    *,
    top_n: int,
    rank_buffer_multiple: int,
) -> list[str]:
    """Retain prior names inside the buffer, then fill from current rank."""
    if top_n <= 0 or rank_buffer_multiple < 1:
        raise ValueError("top_n and rank_buffer_multiple must be positive")
    if len(ranked) != len(set(ranked)) or len(previous) != len(set(previous)):
        raise ValueError("ranked and previous names must be unique")
    buffer_names = set(ranked[: top_n * rank_buffer_multiple])
    selected = [ticker for ticker in previous if ticker in buffer_names][:top_n]
    selected.extend(
        ticker
        for ticker in ranked
        if ticker not in selected and len(selected) < top_n
    )
    return selected[:top_n]


def _selected_base_specification() -> dict:
    manifest = json.loads(V30_MANIFEST.read_text(encoding="utf-8"))
    if manifest["selected_candidate"] != v30.SELECTED_CANDIDATE:
        raise RuntimeError("v30 selected candidate changed")
    audit = manifest["audit"]
    if not audit["strategy_selection_path_complete"]:
        raise RuntimeError("v30 2019 selection path is no longer complete")
    spec = dict(v30.selected_specification())
    expected = {
        "lookback_sessions": 63,
        "skip_recent_sessions": 0,
        "liquid_pool_size": 25,
        "top_n": TOP_N,
        "quality_mode": "profitable",
        "signal_frequency": "monthly",
    }
    for key, value in expected.items():
        if spec.get(key) != value:
            raise RuntimeError(f"v30 base specification changed for {key}")
    return spec


def _validate_sources() -> dict:
    v28_manifest = json.loads(V28_MANIFEST.read_text(encoding="utf-8"))
    if v28_manifest["development_status"] != "PASS":
        raise RuntimeError("v28 development status changed")
    if v28_manifest["selected_candidate"] != "individual_trailing_stop_25pct":
        raise RuntimeError("v28 selected stop changed")
    protocol = json.loads(V43_PROTOCOL.read_text(encoding="utf-8"))
    if protocol["status"] != "FROZEN_WAITING_FOR_FIRST_SIGNAL":
        raise RuntimeError("v43 protocol is no longer waiting for its first signal")
    events = v43.read_ledger(V43_LEDGER)
    if [event["event_type"] for event in events] != ["PROTOCOL_FROZEN"]:
        raise RuntimeError("v43 already has prospective evidence and cannot be replaced")
    return {
        "base_selector": v30.SELECTED_CANDIDATE,
        "base_risk_policy": v28_manifest["selected_candidate"],
        "v30_2019_selection_path_complete": True,
        "v43_signal_count_at_freeze": 0,
        "2026_used_to_motivate_architecture": True,
        "2026_used_for_buffer_selection": False,
    }


def freeze_protocol(path: str | Path = PROTOCOL_PATH) -> dict:
    item = Path(path)
    if item.exists():
        raise RuntimeError(f"v44 protocol will not be overwritten: {item}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V44_MONTHLY_RANK_BUFFER_STOP_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "objective": (
            "Reduce monthly replacement churn without changing the frozen "
            "stock selector, equal weights, or 25 percent individual stop."
        ),
        "source_diagnosis": _validate_sources(),
        "evaluation_boundary": {
            "development_start": DEVELOPMENT_START,
            "development_end": DEVELOPMENT_END,
            "training_years": list(DEVELOPMENT_YEARS),
            "training_years_excluded_from_final_comparison": True,
            "final_comparison_years": list(FINAL_COMPARISON_YEARS),
            "2019_role": (
                "base selector development and source-locked selection-path "
                "adjudication; not rank-buffer parameter selection"
            ),
            "2026_used_for_parameter_selection": False,
            "architecture_isolated_from_2026": False,
            "parameter_isolated_from_2026": True,
            "true_prospective_start": TRUE_PROSPECTIVE_START,
        },
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "controlled_dimension": {
            "rank_buffer_multiple": list(RANK_BUFFER_MULTIPLES),
            "retention_rule": (
                "retain an existing target while it remains inside Top-N "
                "times rank_buffer_multiple at the completed monthly signal"
            ),
        },
        "fixed_model": {
            "selector": _selected_base_specification(),
            "top_n": TOP_N,
            "weighting": "equal 20 percent slots; missing slots remain cash",
            "individual_trailing_stop_fraction": TRAILING_STOP_FRACTION,
            "stop_execution": "next trading close after completed daily close signal",
            "stopped_weight": "CASH",
            "reentry": "next frozen monthly target only",
            "risk_off_asset": "CASH",
            "forbidden_index_etfs": sorted(v23.FORBIDDEN_ETFS),
        },
        "cost_bps": list(COSTS),
        "benchmark": "NASDAQ_COMPOSITE",
        "training_eligibility_gates": {
            "positive_each_training_year_at_50bps": True,
            "positive_compounded_excess_at_30_and_50bps": True,
            "strategy_drawdown_at_50bps_no_worse_than_1x_baseline": True,
            "all_expanding_next_year_diagnostics_pass": True,
            "label": "TRAINING_DIAGNOSTIC_NOT_FINAL_EVIDENCE",
        },
        "selection_order": [
            "eligible first",
            "worst annual training excess at 50bps descending",
            "absolute strategy drawdown at 50bps ascending",
            "turnover at 50bps ascending",
            "compounded training excess at 50bps descending",
            "candidate key ascending",
        ],
        "walk_forward_folds": [
            {"selection_years": list(range(2020, year)), "test_year": year}
            for year in range(2022, 2026)
        ],
        "v43_replacement_rule": {
            "v43_remains_frozen_baseline_unless_v44_development_status_passes": True,
            "v43_must_still_have_zero_signal_events_at_replacement": True,
            "development_pass_does_not_authorize_broker_or_orders": True,
        },
        "input_bindings": {
            "runner": _file_binding(runner),
            "v26_selector": _file_binding(
                "scripts/research_v26_large_liquid_stock_momentum.py"
            ),
            "v28_stop_replay": _file_binding(
                "scripts/research_v28_stock_trailing_stop_development.py"
            ),
            "v30_manifest": _file_binding(V30_MANIFEST),
            "v30_targets": _file_binding(V30_TARGETS),
            "v33_evaluation_helpers": _file_binding(
                "scripts/research_v33_portfolio_stop_development.py"
            ),
            "v43_protocol": _file_binding(V43_PROTOCOL),
            "v43_ledger": _file_binding(V43_LEDGER),
            "price_directory": _directory_binding(
                CLEANED_PRICE_DATA_DIR, "*.csv"
            ),
            "universe_snapshots": _directory_binding(
                snapshot_directory(), "nasdaq_listed_*.csv"
            ),
            "nasdaq_index": _file_binding(NASDAQ_INDEX_FILE),
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
    protocol_sha = _sha256(item)
    if protocol["status"] != "FROZEN_NOT_DEVELOPED":
        raise RuntimeError("v44 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v44 candidate grid changed")
    if protocol["source_diagnosis"] != _validate_sources():
        raise RuntimeError("v44 source diagnosis changed")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v44 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(binding["path"], binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v44 directory binding changed for {name}")
        elif _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v44 file binding changed for {name}")
    return protocol, protocol_sha


def _load_inputs() -> tuple[dict, pd.DataFrame, pd.Series, pd.DataFrame]:
    ranking_inputs = v26._load_inputs()
    raw_close, nasdaq, _qqq, _v26_targets = v28._load_inputs()
    base_targets = pd.read_csv(V30_TARGETS, parse_dates=["effective_date"])
    base_targets = base_targets.loc[
        base_targets["effective_date"].between(DEVELOPMENT_START, DEVELOPMENT_END)
    ].copy()
    if base_targets.empty:
        raise RuntimeError("v30 base target schedule is empty")
    return ranking_inputs, raw_close, nasdaq, base_targets


def generate_buffered_target_schedule(
    spec: dict,
    ranking_inputs: dict,
    base_targets: pd.DataFrame,
) -> pd.DataFrame:
    """Apply rank hysteresis to the frozen v30 monthly decision calendar."""
    top_n = int(spec["top_n"])
    multiple = int(spec["rank_buffer_multiple"])
    base_spec = _selected_base_specification()
    close = ranking_inputs["close"]
    previous: list[str] = []
    rows: list[dict] = []
    for effective_date, group in base_targets.groupby("effective_date", sort=True):
        effective_date = pd.Timestamp(effective_date).normalize()
        active = group.loc[group["ticker"].ne("__CASH__")]
        frozen_top = active["ticker"].astype(str).tolist()
        if not frozen_top:
            selected: list[str] = []
        elif not previous and effective_date == base_targets["effective_date"].min():
            # The first 2020 target was adjudicated through v30's recovered
            # 2019 selection path.  Reuse it instead of reconstructing a gap.
            selected = frozen_top
        else:
            earlier = close.index[close.index < effective_date]
            if not len(earlier):
                raise RuntimeError(f"no signal session before {effective_date.date()}")
            signal_date = pd.Timestamp(earlier[-1])
            ranking = v26._large_liquid_ranking(
                signal_date, base_spec, ranking_inputs
            )
            ranked = ranking.index.astype(str).tolist()
            if frozen_top != ranked[: len(frozen_top)]:
                raise RuntimeError(
                    f"v30 frozen Top-5 changed on {effective_date.date()}"
                )
            selected = buffered_selection(
                ranked,
                previous,
                top_n=top_n,
                rank_buffer_multiple=multiple,
            )
        previous = selected
        if not selected:
            rows.append({
                "effective_date": effective_date,
                "ticker": "__CASH__",
                "target_weight": 0.0,
                "base_transaction_cost_bps": 10.0,
            })
            continue
        for ticker in selected:
            rows.append({
                "effective_date": effective_date,
                "ticker": ticker,
                "target_weight": 1.0 / top_n,
                "base_transaction_cost_bps": 10.0,
            })
    targets = pd.DataFrame(rows)
    forbidden = (
        set(targets["ticker"].astype(str)) - {"__CASH__"}
    ) & v23.FORBIDDEN_ETFS
    if forbidden:
        raise RuntimeError(f"v44 selected forbidden ETFs: {sorted(forbidden)}")
    return targets


def _generate_candidate(
    spec: dict,
    ranking_inputs: dict,
    raw_close: pd.DataFrame,
    nasdaq: pd.Series,
    base_targets: pd.DataFrame,
) -> tuple[dict[int, pd.DataFrame], pd.DataFrame]:
    targets = generate_buffered_target_schedule(spec, ranking_inputs, base_targets)
    results = {}
    for cost in COSTS:
        daily = v28.replay_with_individual_trailing_stop(
            raw_close,
            nasdaq,
            targets,
            DEVELOPMENT_START,
            DEVELOPMENT_END,
            trailing_stop_fraction=TRAILING_STOP_FRACTION,
            transaction_cost_bps=float(cost),
        )
        results[cost] = v33._canonicalize_result(
            daily, nasdaq, DEVELOPMENT_START, DEVELOPMENT_END
        )
    return results, targets


def _selection_row(
    key: str,
    results: dict[int, pd.DataFrame],
    baseline_results: dict[int, pd.DataFrame],
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
    baseline_drawdown = abs(
        v33._period_metrics(baseline_results[50], years)[
            "strategy_maximum_drawdown"
        ]
    )
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
        "baseline_drawdown_50bps": baseline_drawdown,
        "drawdown_not_worse_than_baseline": (
            selected_costs[50]["drawdown"] <= baseline_drawdown + 1e-12
        ),
        "turnover_50bps": selected_costs[50]["turnover"],
        "final_evidence": False,
    }


def select_candidate(
    results_by_candidate: dict[str, dict[int, pd.DataFrame]],
    years: tuple[int, ...] = DEVELOPMENT_YEARS,
) -> tuple[str | None, list[dict]]:
    if BASELINE_CANDIDATE not in results_by_candidate:
        raise ValueError("v44 baseline candidate is missing")
    baseline = results_by_candidate[BASELINE_CANDIDATE]
    ranking = [
        _selection_row(key, results, baseline, years)
        for key, results in results_by_candidate.items()
    ]
    ranking.sort(key=lambda row: (
        not row["training_eligible"],
        -row["worst_annual_training_excess_50bps"],
        row["strategy_drawdown_50bps"],
        row["turnover_50bps"],
        -row["compounded_training_excess_50bps"],
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
        raise RuntimeError(f"v44 output will not be overwritten: {output_dir}")
    ranking_inputs, raw_close, nasdaq, base_targets = _load_inputs()
    results_by_candidate = {}
    targets_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        results, targets = _generate_candidate(
            spec, ranking_inputs, raw_close, nasdaq, base_targets
        )
        results_by_candidate[spec["key"]] = results
        targets_by_candidate[spec["key"]] = targets
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
        targets_path = output_dir / "selected_targets.csv"
        targets_by_candidate[selected].to_csv(targets_path, index=False)
        outputs["selected_targets"] = _file_binding(targets_path)
        for cost in COSTS:
            path = output_dir / f"selected_daily_{cost}bps.csv"
            results_by_candidate[selected][cost].to_csv(path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(path)

    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V44_MONTHLY_RANK_BUFFER_STOP_DEVELOPMENT_RESULT",
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
        "final_comparison_years": list(FINAL_COMPARISON_YEARS),
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
