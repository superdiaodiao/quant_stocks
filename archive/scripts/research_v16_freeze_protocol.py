#!/usr/bin/env python3
"""Freeze v16 inputs, parameter, and gates without running confirmation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts import research_v14_freeze_protocol as v14_freeze
from scripts import research_v15_benchmark_core_development as v15


OUTPUT_PATH = Path("output/research_only/v16/frozen_protocol_20260829.json")
FROZEN_AT = "2026-08-29"

V15_DEVELOPMENT = {
    "path": Path(
        "output/research_only/v15/benchmark_core_development/manifest.json"
    ),
    "sha256": (
        "a9236a0e897e5e487eddbc8d2e022dd49c844015bb24f38f2261c11947cf6118"
    ),
}
V16_DEVELOPMENT = {
    "path": Path(
        "output/research_only/v16/trend_confirmed_qqq_development/manifest.json"
    ),
    "sha256": (
        "42e59bc3070003e9efddb6fdab4a0a45e10d09e52f3ff21efc05ee49f212245c"
    ),
}
V16_SELECTED_OUTPUTS = {
    "targets_sma_50": {
        "path": Path(
            "output/research_only/v16/trend_confirmed_qqq_development/"
            "targets_sma_50.csv"
        ),
        "sha256": (
            "da271e14b128c89b2358b135df6f1b2559f0c7d5872d929ce8f083a35b2cab91"
        ),
    },
    "daily_sma_50_10bps": {
        "path": Path(
            "output/research_only/v16/trend_confirmed_qqq_development/"
            "daily_sma_50_10bps.csv"
        ),
        "sha256": (
            "5d7ecf8062dcd2dca1a27fcd81aed444c295095bd74cda90cffc2e69c8572ca1"
        ),
    },
    "daily_sma_50_30bps": {
        "path": Path(
            "output/research_only/v16/trend_confirmed_qqq_development/"
            "daily_sma_50_30bps.csv"
        ),
        "sha256": (
            "98800d98bc8abfcca6f14fc88c76656bcf48d2d730d258e02dd198719dfaefd1"
        ),
    },
    "daily_sma_50_50bps": {
        "path": Path(
            "output/research_only/v16/trend_confirmed_qqq_development/"
            "daily_sma_50_50bps.csv"
        ),
        "sha256": (
            "c2f311c77f28b7eef190f11adcedc0aff6134b918eaa86379fb519fbf344e8b7"
        ),
    },
}
CODE_BINDINGS = {
    "can_slim": {
        "path": Path("src/research/can_slim.py"),
        "sha256": (
            "f08797f5b1b07b3d6f41251cd259cab503a6e8e4e2ba43a936a18b7c217c42e5"
        ),
    },
    "v15_development": {
        "path": Path("scripts/research_v15_benchmark_core_development.py"),
        "sha256": (
            "3ca5deed7aae3e3c9a1c2aae8098794d12cc0ba13808ec6e165f48936bd36c7c"
        ),
    },
    "v16_development": {
        "path": Path(
            "scripts/research_v16_trend_confirmed_qqq_development.py"
        ),
        "sha256": (
            "89beeb34a6c63bb9d443a1a7b2f3b635dac4ab8f60f03d139ec4be9ec05a7f99"
        ),
    },
    "v16_frozen_confirmation": {
        "path": Path("scripts/research_v16_frozen_confirmation.py"),
        "sha256": (
            "6c49d6e60d761a7be1265ce73003476cbff0f6bf4d23635b03d067abc182773e"
        ),
    },
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_bindings(bindings: dict[str, dict]) -> dict[str, dict]:
    verified = {}
    for name, binding in bindings.items():
        path = Path(binding["path"])
        actual = _sha256(path)
        if actual != binding["sha256"]:
            raise RuntimeError(f"{name} binding changed: {actual}")
        verified[name] = {"path": str(path), "sha256": actual}
    return verified


def _validate_development() -> dict:
    v15_binding = _verify_bindings({"v15_development": V15_DEVELOPMENT})[
        "v15_development"
    ]
    v16_binding = _verify_bindings({"v16_development": V16_DEVELOPMENT})[
        "v16_development"
    ]
    selected_outputs = _verify_bindings(V16_SELECTED_OUTPUTS)
    v15_report = json.loads(
        V15_DEVELOPMENT["path"].read_text(encoding="utf-8")
    )
    if v15_report["development_result"]["all_development_gates_passed"]:
        raise RuntimeError("negative v15 development result changed to pass")
    if v15_report["confirmation_period_computed"]:
        raise RuntimeError("v15 development crossed confirmation boundary")

    v16_report = json.loads(
        V16_DEVELOPMENT["path"].read_text(encoding="utf-8")
    )
    if v16_report["lookback_candidates"] != [20, 50, 100, 200]:
        raise RuntimeError("v16 candidate grid changed")
    if v16_report["selected_lookback"] != 50:
        raise RuntimeError("v16 selected lookback changed")
    if not v16_report["all_development_gates_passed"]:
        raise RuntimeError("v16 development no longer passes")
    if not v16_report["candidate_results"]["50"]["summary"][
        "all_development_gates_passed"
    ]:
        raise RuntimeError("selected v16 candidate no longer passes")
    if v16_report["confirmation_period_computed"]:
        raise RuntimeError("v16 development crossed confirmation boundary")
    if v16_report["confirmation_results_inspected"]:
        raise RuntimeError("v16 development inspected confirmation results")
    return {
        "v15_negative_development": v15_binding,
        "v16_development": v16_binding,
        "v16_development_selected_outputs": selected_outputs,
    }


def build(output_path: Path = OUTPUT_PATH) -> dict:
    development = _validate_development()
    v14_inputs = _verify_bindings({
        "v14_protocol": v15.V14_PROTOCOL,
        "v14_result": v15.V14_RESULT,
        "v14_targets": v15.V14_TARGETS,
        "v14_daily": v15.V14_DAILY,
        "qqq_history": v15.QQQ_HISTORY,
        "qqq_provenance": v15.QQQ_PROVENANCE,
    })
    v14_protocol = json.loads(
        v15.V14_PROTOCOL["path"].read_text(encoding="utf-8")
    )
    price_binding = v14_protocol["input_bindings"]["price_directory"]
    price_directory = v14_freeze._directory_binding(
        Path(price_binding["path"]),
        "*.csv",
        v14_freeze.PRICE_FILE_COUNT,
        v14_freeze.PRICE_CONTENT_MANIFEST_SHA256,
    )
    if price_directory != price_binding:
        raise RuntimeError("v16 price-directory binding changed")
    inputs = {
        **v14_inputs,
        **development,
        "formal": v14_freeze._verify_bindings(v14_freeze.FORMAL_BINDINGS),
        "code": _verify_bindings(CODE_BINDINGS),
        "price_directory": price_directory,
    }
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "frozen_at": FROZEN_AT,
        "protocol_status": "FROZEN_V16_HISTORICAL_CONFIRMATION_PROTOCOL",
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": True,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "historical_confirmation_executed": False,
        "confirmation_results_inspected": False,
        "input_bindings": inputs,
        "hypothesis": "V16_TREND_CONFIRMED_QQQ_CASH_FILL",
        "stock_target_policy": "preserve every frozen v14 stock target",
        "frozen_parameter": {
            "core_ticker": "QQQ",
            "synthetic_replay_ticker": "__QQQ_CORE__",
            "qqq_sma_sessions": 50,
            "signal_observation": (
                "prior QQQ session close above its 50-session simple moving "
                "average"
            ),
            "activation_scope": "only while the frozen v14 target is cash",
            "stock_weights_renormalized": False,
            "unallocated_weight_behavior": "cash",
        },
        "execution": {
            "transaction_cost_bps": [10, 30, 50],
            "run_count": 1,
            "retune_after_result": False,
            "failure_action": (
                "remain BLOCKED; do not change the 50-session parameter or "
                "the gates"
            ),
        },
        "data_split": {
            "development_validation": {
                "start": "2022-01-01",
                "end": "2024-12-31",
                "selection_status": "HUMAN_EXPOSURE_CONTAMINATED",
            },
            "historical_confirmation": {
                "start": "2025-01-01",
                "end": "2026-07-17",
                "years": [2025, 2026],
                "exposure_status": "HUMAN_EXPOSURE_CONTAMINATED",
                "statistically_untouched": False,
                "interpretation": (
                    "One-shot frozen historical confirmation only; previous "
                    "human diagnostics exposed this interval."
                ),
            },
            "genuine_untouched_phase": {
                "kind": "future forward observation",
                "started": False,
                "authorized": False,
            },
        },
        "predeclared_gates": {
            "confirmation_annual_excess_win_count": {
                "10_bps": {"required": 2, "total_years": 2},
                "30_bps": {"required": 1, "total_years": 2},
                "50_bps": {"required": 1, "total_years": 2},
            },
            "confirmation_compounded_excess": {
                "cost_bps": [10, 30, 50],
                "operator": ">",
                "threshold": 0.0,
            },
            "full_history_annual_excess_win_count": {
                "10_bps": {"required": 4, "total_years": 5},
                "30_bps": {"required": 3, "total_years": 5},
                "50_bps": {"required": 3, "total_years": 5},
            },
            "full_history_compounded_excess": {
                "cost_bps": [10, 30, 50],
                "operator": ">",
                "threshold": 0.0,
            },
            "drawdown": {
                "cost_bps": 10,
                "maximum_loss_fraction": 0.40,
                "maximum_underperformance_vs_nasdaq_percentage_points": 5.0,
            },
            "leave_one_out": {
                "remove": "largest single-name contribution",
                "selection_metric": (
                    "largest net arithmetic daily return attribution"
                ),
                "removed_weight_behavior": "leave as cash; do not renormalize",
                "compounded_excess_operator": ">",
                "compounded_excess_threshold": 0.0,
            },
            "gate_failure": "release_status remains BLOCKED",
        },
        "interpretation_guardrail": (
            "This freeze authorizes exactly one research-only historical "
            "confirmation. It cannot authorize promotion, trading, or claims "
            "of an untouched holdout."
        ),
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(protocol, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        **protocol,
        "output": {"path": str(output_path), "sha256": _sha256(output_path)},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = build(args.output)
    print(json.dumps({
        "protocol_status": report["protocol_status"],
        "frozen_parameter": report["frozen_parameter"],
        "historical_confirmation_executed": report[
            "historical_confirmation_executed"
        ],
        "release_status": report["release_status"],
        "output": report["output"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
