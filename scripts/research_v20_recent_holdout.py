#!/usr/bin/env python3
"""Evaluate 2025-2026 only after the v20 retraining protocol is frozen."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from scripts import research_v19_source_locked_v10_feasibility as v19
from scripts import research_v20_temporal_retraining as v20


PROTOCOL_PATH = v20.OUTPUT_DIR / "frozen_protocol.json"
OUTPUT_DIR = Path("output/research_only/v20/recent_holdout_20260830")


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _validated_protocol(path: Path) -> tuple[dict, str]:
    path = Path(path)
    actual_sha = _sha256(path)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol["recent_holdout_executed"]:
        raise RuntimeError("v20 protocol already records a holdout execution")
    if protocol["recent_holdout_results_inspected"]:
        raise RuntimeError("v20 protocol already records inspected holdout results")
    if protocol["model_data_isolation"] != "PASS":
        raise RuntimeError("v20 model isolation gate is not PASS")
    if protocol["validation_status"] != "PASS":
        raise RuntimeError("v20 validation did not pass")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v20 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if "sha256" not in binding:
            continue
        actual = _sha256(Path(binding["path"]))
        if actual != binding["sha256"]:
            raise RuntimeError(f"v20 protocol binding changed for {name}: {actual}")
    with TemporaryDirectory() as temporary:
        regenerated = v20.run(Path(temporary) / "freeze")
    if actual_sha != regenerated["protocol"]["sha256"]:
        raise RuntimeError("v20 frozen protocol does not regenerate exactly")
    return protocol, actual_sha


def evaluate_recent_holdout(
    results: dict[int, pd.DataFrame],
) -> dict:
    costs = {}
    all_pass = True
    for cost in v19.COSTS:
        result = results[cost].loc[v20.HOLDOUT_START:v20.HOLDOUT_END]
        annual = v20._annual(result)
        years = tuple(annual.index.astype(int))
        if years != (2025, 2026):
            raise RuntimeError(f"unexpected holdout years at {cost}bps: {years}")
        annual_pass = bool(annual["excess_vs_nasdaq"].gt(0.0).all())
        strategy = float((1.0 + result["strategy"]).prod() - 1.0)
        benchmark = float((1.0 + result["benchmark"]).prod() - 1.0)
        compound_pass = strategy - benchmark > 0.0
        passed = annual_pass and compound_pass
        all_pass = all_pass and passed
        costs[str(cost)] = {
            "annual": [
                {"year": int(year), **values}
                for year, values in annual.to_dict(orient="index").items()
            ],
            "both_annual_excesses_positive": annual_pass,
            "compounded_strategy": strategy,
            "compounded_nasdaq": benchmark,
            "compounded_excess_vs_nasdaq": strategy - benchmark,
            "compounded_gate_passed": compound_pass,
            "all_cost_gates_passed": passed,
        }
    return {
        "all_predeclared_holdout_gates_passed": bool(all_pass),
        "costs": costs,
    }


def _verify_prior_outputs(
    protocol: dict,
    results: dict[int, pd.DataFrame],
) -> None:
    for cost in v19.COSTS:
        development_binding = protocol["outputs"][
            f"selected_development_{cost}bps"
        ]
        validation_binding = protocol["outputs"][
            f"selected_validation_{cost}bps"
        ]
        for binding in (development_binding, validation_binding):
            if _sha256(Path(binding["path"])) != binding["sha256"]:
                raise RuntimeError(f"frozen v20 output changed: {binding['path']}")
        expected_development = pd.read_csv(
            development_binding["path"], index_col="date", parse_dates=True
        )
        expected_validation = pd.read_csv(
            validation_binding["path"], index_col="date", parse_dates=True
        )
        actual_development = results[cost].loc[v20.START:v20.DEVELOPMENT_END]
        actual_validation = results[cost].loc[
            v20.VALIDATION_START:v20.VALIDATION_END
        ]
        pd.testing.assert_frame_equal(
            actual_development,
            expected_development,
            check_exact=False,
            rtol=0.0,
            atol=1e-12,
        )
        pd.testing.assert_frame_equal(
            actual_validation,
            expected_validation,
            check_exact=False,
            rtol=0.0,
            atol=1e-12,
        )


def execute(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(Path(protocol_path))
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v20 holdout output will not be overwritten: {output_dir}")

    price_binding = protocol["input_bindings"]["price_directory"]
    stock_paths, qqq_return = v20._load_replay_inputs(
        end=v20.HOLDOUT_END, price_binding=price_binding
    )
    config = protocol["selected_configuration"]
    results = v20._simulate_variant(
        stock_paths,
        qqq_return,
        lookback=int(config["lookback_sessions"]),
        crowded_stock_weight=float(config["crowded_stock_weight"]),
    )
    _verify_prior_outputs(protocol, results)
    holdout = evaluate_recent_holdout(results)

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for cost in v19.COSTS:
        path = output_dir / f"holdout_daily_{cost}bps.csv"
        results[cost].loc[v20.HOLDOUT_START:v20.HOLDOUT_END].to_csv(
            path, index_label="date"
        )
        outputs[f"holdout_daily_{cost}bps"] = {
            "path": str(path),
            "sha256": _sha256(path),
        }

    passed = holdout["all_predeclared_holdout_gates_passed"]
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "RECENT_HISTORICAL_HOLDOUT_WITH_MODEL_ISOLATION",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "selected_variant": protocol["selected_variant"],
        "selected_configuration": config,
        "model_data_isolation": "PASS",
        "researcher_exposure_status": "REPEATEDLY_HUMAN_EXPOSED",
        "statistically_untouched": False,
        "may_be_called_clean_confirmation": False,
        "recent_holdout_status": "PASS" if passed else "BLOCKED",
        "recent_holdout_result": holdout,
        "short_forward_phase_eligible": bool(passed),
        "short_forward_phase": {
            "minimum_operational_months": 3,
            "target_decision_months": 6,
            "parameters_must_remain_frozen": True,
            "started": False,
        },
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "brokerage_or_trading_authorized": False,
        "outputs": outputs,
        "interpretation_guardrail": (
            "The selected model did not access 2025-2026 until after the v20 "
            "protocol was frozen. Earlier human exposure is still disclosed. "
            "A pass supports a shorter 3-6 month genuine forward phase but "
            "does not itself authorize release, promotion, or trading."
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
    parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = execute(args.protocol, args.output_dir)
    print(json.dumps({
        "selected_variant": report["selected_variant"],
        "recent_holdout_status": report["recent_holdout_status"],
        "short_forward_phase_eligible": report["short_forward_phase_eligible"],
        "release_status": report["release_status"],
        "manifest": report["manifest"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
