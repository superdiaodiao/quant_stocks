#!/usr/bin/env python3
"""Run the unchanged v44 hypothesis through the adjudicated v30 universe.

v44 stopped before producing any candidate return because its ranking helper
used the pre-adjudication universe path.  v45 changes only that runtime input:
it reuses v29's recovered snapshots, v30's FB-to-META normalization, and v30's
four gap-date universe adjudications.  Candidate buffers, gates, costs, years,
selection order, trailing stop, and v43 replacement rule are copied verbatim
from the already-frozen v44 protocol.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v26_large_liquid_stock_momentum as v26
from scripts import research_v28_stock_trailing_stop_development as v28
from scripts import research_v29_recovered_2019_stock_momentum as v29
from scripts import research_v30_2019_selection_path_adjudication as v30
from scripts import research_v33_portfolio_stop_development as v33
from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v44_monthly_rank_buffer_stop_development as v44


OUTPUT_DIR = Path(
    "output/research_only/v45/monthly_rank_buffer_stop_runtime_repair_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: str | Path) -> dict:
    item = Path(path)
    return {"path": str(item), "sha256": _sha256(item)}


def _v44_protocol() -> dict:
    protocol = json.loads(v44.PROTOCOL_PATH.read_text(encoding="utf-8"))
    if protocol["status"] != "FROZEN_NOT_DEVELOPED":
        raise RuntimeError("v44 protocol status changed")
    if protocol["candidate_grid"] != v44.candidate_specs():
        raise RuntimeError("v44 candidate grid changed")
    output_dir = v44.DEVELOPMENT_OUTPUT_DIR
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError("v44 unexpectedly produced development results")
    return protocol


def _validate_sources() -> dict:
    v29._validated_protocol(v29.PROTOCOL_PATH)
    v30._validated_protocol(v30.PROTOCOL_PATH)
    source = v44._validate_sources()
    if [event["event_type"] for event in v43.read_ledger(v43.LEDGER_PATH)] != [
        "PROTOCOL_FROZEN"
    ]:
        raise RuntimeError("v43 already has prospective evidence")
    return {
        **source,
        "v44_runtime_status": "FAILED_BEFORE_ANY_CANDIDATE_RETURN",
        "v44_failure": (
            "pre-adjudication v26 ranking disagreed with frozen v30 Top-5 "
            "on 2020-11-02"
        ),
        "runtime_repair": (
            "reuse v29 recovered snapshots plus v30 identity normalization "
            "and gap-date universe adjudication"
        ),
        "candidate_grid_changed": False,
        "training_gates_changed": False,
    }


def freeze_protocol(path: str | Path = PROTOCOL_PATH) -> dict:
    item = Path(path)
    if item.exists():
        raise RuntimeError(f"v45 protocol will not be overwritten: {item}")
    source = _v44_protocol()
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    copied_fields = (
        "objective",
        "evaluation_boundary",
        "candidate_grid",
        "candidate_count",
        "controlled_dimension",
        "fixed_model",
        "cost_bps",
        "benchmark",
        "training_eligibility_gates",
        "selection_order",
        "walk_forward_folds",
        "v43_replacement_rule",
    )
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V45_V44_RUNTIME_REPAIR_PRECOMMITMENT",
        "status": "FROZEN_RUNTIME_REPAIR_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        **{field: source[field] for field in copied_fields},
        "source_diagnosis": _validate_sources(),
        "runtime_input_policy": {
            "ranking_inputs": "v29 recovered point-in-time inputs",
            "identity_normalization": "v30 source-locked FB-to-META mapping",
            "gap_signal_universe": "v30 bounding-snapshot adjudication",
            "monthly_decision_calendar": "v30 frozen target effective dates",
            "performance_results_read_from_failed_v44": False,
        },
        "input_bindings": {
            "runner": _file_binding(runner),
            "v44_frozen_protocol": _file_binding(v44.PROTOCOL_PATH),
            "v44_failed_runner": _file_binding(Path(v44.__file__)),
            "v29_protocol": _file_binding(v29.PROTOCOL_PATH),
            "v29_runner": _file_binding(Path(v29.__file__)),
            "v30_protocol": _file_binding(v30.PROTOCOL_PATH),
            "v30_runner": _file_binding(Path(v30.__file__)),
            "v30_manifest": _file_binding(v30.RESULT_OUTPUT_DIR / "manifest.json"),
            "v30_targets": _file_binding(v30.RESULT_OUTPUT_DIR / "selected_targets.csv"),
            "v28_stop_replay": _file_binding(Path(v28.__file__)),
            "v33_evaluation_helpers": _file_binding(Path(v33.__file__)),
            "v43_protocol": _file_binding(v43.PROTOCOL_PATH),
            "v43_ledger": _file_binding(v43.LEDGER_PATH),
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
    source = _v44_protocol()
    if protocol["status"] != "FROZEN_RUNTIME_REPAIR_NOT_DEVELOPED":
        raise RuntimeError("v45 protocol status changed")
    copied_fields = (
        "objective",
        "evaluation_boundary",
        "candidate_grid",
        "candidate_count",
        "controlled_dimension",
        "fixed_model",
        "cost_bps",
        "benchmark",
        "training_eligibility_gates",
        "selection_order",
        "walk_forward_folds",
        "v43_replacement_rule",
    )
    for field in copied_fields:
        if protocol[field] != source[field]:
            raise RuntimeError(f"v45 changed frozen v44 field: {field}")
    if protocol["source_diagnosis"] != _validate_sources():
        raise RuntimeError("v45 source diagnosis changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v45 file binding changed for {name}")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v45 release boundary changed")
    return protocol, _sha256(item)


def _adjudicated_inputs() -> dict:
    inputs = v29._load_inputs()
    snapshots = v30.normalize_meta_identity(v29.load_repaired_universe_snapshots())
    audit, adjudicated = v30.audit_gap_selection_path(
        inputs, snapshots, v30.selected_specification()
    )
    if not audit["strategy_selection_path_complete"]:
        raise RuntimeError("v30 selection path is no longer complete")
    universe_cache = {}

    def universe(signal_date):
        stamp = pd.Timestamp(signal_date).normalize()
        if stamp in adjudicated:
            return adjudicated[stamp] - v29.FORBIDDEN_ETFS
        if stamp not in universe_cache:
            symbols = v30.universe_as_of(
                snapshots,
                stamp,
                maximum_age_days=v29.MAXIMUM_SNAPSHOT_AGE_DAYS,
            )
            universe_cache[stamp] = (
                None if symbols is None else set(symbols) - v29.FORBIDDEN_ETFS
            )
        return universe_cache[stamp]

    inputs["universe"] = universe
    inputs["technical_cache"] = {}
    inputs["quality_cache"] = {}
    inputs["large_liquid_cache"] = {}
    return inputs


def _load_inputs() -> tuple[dict, pd.DataFrame]:
    inputs = _adjudicated_inputs()
    base_targets = pd.read_csv(
        v30.RESULT_OUTPUT_DIR / "selected_targets.csv",
        parse_dates=["effective_date"],
    )
    base_targets = base_targets.loc[
        base_targets["effective_date"].between(
            v44.DEVELOPMENT_START, v44.DEVELOPMENT_END
        )
    ].copy()
    if base_targets.empty:
        raise RuntimeError("v30 base target schedule is empty")
    return inputs, base_targets


def generate_buffered_target_schedule(
    spec: dict,
    inputs: dict,
    base_targets: pd.DataFrame,
) -> pd.DataFrame:
    top_n = int(spec["top_n"])
    multiple = int(spec["rank_buffer_multiple"])
    base_spec = v30.selected_specification()
    close = inputs["close"]
    previous: list[str] = []
    rows = []
    first_effective = pd.Timestamp(base_targets["effective_date"].min()).normalize()
    for effective_date, group in base_targets.groupby("effective_date", sort=True):
        effective_date = pd.Timestamp(effective_date).normalize()
        frozen_top = group.loc[
            group["ticker"].ne("__CASH__"), "ticker"
        ].astype(str).tolist()
        if not frozen_top:
            selected = []
        elif not previous and effective_date == first_effective:
            selected = frozen_top
        else:
            earlier = close.index[close.index < effective_date]
            if not len(earlier):
                raise RuntimeError(f"no signal session before {effective_date.date()}")
            signal_date = pd.Timestamp(earlier[-1])
            ranked = v26._large_liquid_ranking(
                signal_date, base_spec, inputs
            ).index.astype(str).tolist()
            if frozen_top != ranked[: len(frozen_top)]:
                raise RuntimeError(
                    f"adjudicated v30 Top-5 changed on {effective_date.date()}"
                )
            selected = v44.buffered_selection(
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
        else:
            rows.extend({
                "effective_date": effective_date,
                "ticker": ticker,
                "target_weight": 1.0 / top_n,
                "base_transaction_cost_bps": 10.0,
            } for ticker in selected)
    targets = pd.DataFrame(rows)
    forbidden = (
        set(targets["ticker"].astype(str)) - {"__CASH__"}
    ) & v29.FORBIDDEN_ETFS
    if forbidden:
        raise RuntimeError(f"v45 selected forbidden ETFs: {sorted(forbidden)}")
    return targets


def _generate_candidate(
    spec: dict,
    inputs: dict,
    base_targets: pd.DataFrame,
) -> tuple[dict[int, pd.DataFrame], pd.DataFrame]:
    targets = generate_buffered_target_schedule(spec, inputs, base_targets)
    results = {}
    for cost in v44.COSTS:
        daily = v28.replay_with_individual_trailing_stop(
            inputs["raw_close"],
            inputs["nasdaq"],
            targets,
            v44.DEVELOPMENT_START,
            v44.DEVELOPMENT_END,
            trailing_stop_fraction=v44.TRAILING_STOP_FRACTION,
            transaction_cost_bps=float(cost),
        )
        results[cost] = v33._canonicalize_result(
            daily,
            inputs["nasdaq"],
            v44.DEVELOPMENT_START,
            v44.DEVELOPMENT_END,
        )
    return results, targets


def develop(
    protocol_path: str | Path = PROTOCOL_PATH,
    output_dir: str | Path = DEVELOPMENT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v45 output will not be overwritten: {output_dir}")
    inputs, base_targets = _load_inputs()
    results_by_candidate = {}
    targets_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        results, targets = _generate_candidate(spec, inputs, base_targets)
        results_by_candidate[spec["key"]] = results
        targets_by_candidate[spec["key"]] = targets
        summaries[spec["key"]] = v33._summary(results)

    full_training_candidate, ranking = v44.select_candidate(results_by_candidate)
    folds = []
    for fold in protocol["walk_forward_folds"]:
        years = tuple(int(year) for year in fold["selection_years"])
        fold_selected, fold_ranking = v44.select_candidate(
            results_by_candidate, years
        )
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
        for cost in v44.COSTS:
            path = output_dir / f"selected_daily_{cost}bps.csv"
            results_by_candidate[selected][cost].to_csv(path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(path)

    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V45_V44_RUNTIME_REPAIR_DEVELOPMENT_RESULT",
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
        "v44_hypothesis_changed_by_runtime_repair": False,
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
