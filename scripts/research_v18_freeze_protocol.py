#!/usr/bin/env python3
"""Freeze the v18 contaminated robustness protocol without executing it."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts import research_v14_freeze_protocol as v14_freeze
from scripts import research_v15_benchmark_core_development as v15


OUTPUT_PATH = Path("output/research_only/v18/frozen_protocol_20260829.json")
FROZEN_AT = "2026-08-29"

BINDINGS = {
    "source_v7_script": {
        "path": Path("scripts/research_v7_qqq_targeted_core_satellite.py"),
        "sha256": (
            "53aa5f44812c54876a66049e7831ad643efae686fdd9b636a8e8cdb2c5f7f23e"
        ),
    },
    "source_v7_summary": {
        "path": Path(
            "output/research_v7_qqq_targeted_core_satellite_summary.json"
        ),
        "sha256": (
            "a628d44020338f96b69e4ec2a13e4ea56a37bb162ed6e248ba6dd19bb1d4793e"
        ),
    },
    "v16_result": {
        "path": Path(
            "output/research_only/v16/frozen_confirmation_20260829/manifest.json"
        ),
        "sha256": (
            "ec50967c881de5f0ecb81e78f0f33fcaf538341aa3b8031e1a40f609b03788ec"
        ),
    },
    "v16_post_confirmation_diagnostic": {
        "path": Path(
            "output/research_only/v16/post_confirmation_diagnostic_20260829/"
            "manifest.json"
        ),
        "sha256": (
            "30d2c62153ef6a463f0d3bf257077636c3f65a1662629d6c3a2f759fa3b9ccd0"
        ),
    },
    "v17_negative_development": {
        "path": Path(
            "output/research_only/v17/stock_turnover_cap_development/manifest.json"
        ),
        "sha256": (
            "9413031594ac17170102781dd85180856d65cf83a444a9163b8fbdd33631a818"
        ),
    },
    "v18_development": {
        "path": Path(
            "output/research_only/v18/source_locked_v7_core_development/"
            "manifest.json"
        ),
        "sha256": (
            "6a324a5923c7c3eaf20f7e0267e9ecabde476726ee4aedfef2e636ee7ba9a51b"
        ),
    },
}
V18_DEVELOPMENT_OUTPUTS = {
    "targets": {
        "path": Path(
            "output/research_only/v18/source_locked_v7_core_development/"
            "core_satellite_targets.csv"
        ),
        "sha256": (
            "56d60680b6e1e7b28aa6c5f3091745d7262627855d5299ce4a857c4a2600ac78"
        ),
    },
    "daily_10bps": {
        "path": Path(
            "output/research_only/v18/source_locked_v7_core_development/"
            "daily_10bps.csv"
        ),
        "sha256": (
            "c73029079b04efb3c31d22ebb94b98a0d65d61ef16865ae98a58d5dfb484da0d"
        ),
    },
    "daily_30bps": {
        "path": Path(
            "output/research_only/v18/source_locked_v7_core_development/"
            "daily_30bps.csv"
        ),
        "sha256": (
            "54bcfd966cf7ad25dd0171e1a42a849bd16625214380d3e584fd23cff38dd154"
        ),
    },
    "daily_50bps": {
        "path": Path(
            "output/research_only/v18/source_locked_v7_core_development/"
            "daily_50bps.csv"
        ),
        "sha256": (
            "52c6e1e2950608dde4b009327279c01bf6f8965b18b723895479ef1190e7bcc1"
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
    "qqq_total_return_helper": {
        "path": Path("scripts/research_v15_benchmark_core_development.py"),
        "sha256": (
            "3ca5deed7aae3e3c9a1c2aae8098794d12cc0ba13808ec6e165f48936bd36c7c"
        ),
    },
    "v18_development": {
        "path": Path("scripts/research_v18_source_locked_v7_core_development.py"),
        "sha256": (
            "e9d4cea91a2e54da36d451830224d53eb0df06c4bc3fd949ab06ef9ecc13dafb"
        ),
    },
    "v18_frozen_robustness": {
        "path": Path("scripts/research_v18_frozen_robustness.py"),
        "sha256": (
            "35e46cb1b23d7ede90f0be649ca469411f88280df8a67978823c2af78e9c9982"
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


def _validate_research_chain() -> dict:
    verified = _verify_bindings(BINDINGS)
    source = json.loads(
        BINDINGS["source_v7_summary"]["path"].read_text(encoding="utf-8")
    )
    if source["configuration"]["stock_weight"] != 0.20:
        raise RuntimeError("source v7 stock weight changed")
    if source["configuration"]["qqq_weight"] != 0.80:
        raise RuntimeError("source v7 QQQ weight changed")
    if source["release_status"] != "BLOCKED" or source["promotion_eligible"]:
        raise RuntimeError("source v7 policy boundary changed")
    v16_result = json.loads(
        BINDINGS["v16_result"]["path"].read_text(encoding="utf-8")
    )
    if v16_result["historical_gate_status"] != "BLOCKED":
        raise RuntimeError("v16 frozen result changed")
    diagnostic = json.loads(
        BINDINGS["v16_post_confirmation_diagnostic"]["path"].read_text(
            encoding="utf-8"
        )
    )
    if diagnostic["strategy_replayed"]:
        raise RuntimeError("v16 diagnostic unexpectedly replayed strategy")
    v17 = json.loads(
        BINDINGS["v17_negative_development"]["path"].read_text(
            encoding="utf-8"
        )
    )
    if v17["all_development_gates_passed"]:
        raise RuntimeError("v17 negative development result changed")
    if v17["post_development_period_computed"]:
        raise RuntimeError("v17 crossed its development boundary")
    v18 = json.loads(
        BINDINGS["v18_development"]["path"].read_text(encoding="utf-8")
    )
    if not v18["all_development_gates_passed"]:
        raise RuntimeError("v18 development no longer passes")
    if not v18["development_result"]["qqq_relative_gates"][
        "all_costs_passed"
    ]:
        raise RuntimeError("v18 QQQ-relative development gates changed")
    if v18["post_development_period_computed"]:
        raise RuntimeError("v18 crossed its development boundary")
    if v18["source_locked_architecture"] != {
        "stock_weight": 0.20,
        "qqq_weight": 0.80,
        "rebalance_frequency": "frozen v14 monthly target events",
        "stock_target_policy": (
            "scale every frozen v14 stock target by 20%; leave an empty "
            "stock sleeve in cash when v14 is cash"
        ),
    }:
        raise RuntimeError("v18 source-locked architecture changed")
    return {
        **verified,
        "v18_development_outputs": _verify_bindings(V18_DEVELOPMENT_OUTPUTS),
    }


def build(output_path: Path = OUTPUT_PATH) -> dict:
    chain = _validate_research_chain()
    base_inputs = _verify_bindings({
        "v14_protocol": v15.V14_PROTOCOL,
        "v14_targets": v15.V14_TARGETS,
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
        raise RuntimeError("v18 price-directory binding changed")
    inputs = {
        **base_inputs,
        **chain,
        "formal": v14_freeze._verify_bindings(v14_freeze.FORMAL_BINDINGS),
        "code": _verify_bindings(CODE_BINDINGS),
        "price_directory": price_directory,
    }
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "frozen_at": FROZEN_AT,
        "protocol_status": "FROZEN_V18_CONTAMINATED_ROBUSTNESS_PROTOCOL",
        "historical_selection_contaminated": True,
        "statistically_untouched": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": True,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "historical_robustness_replay_executed": False,
        "post_development_results_inspected": False,
        "input_bindings": inputs,
        "hypothesis": "V18_SOURCE_LOCKED_V7_CORE_ON_V14_TARGETS",
        "frozen_architecture": {
            "qqq_weight": 0.80,
            "stock_weight": 0.20,
            "weight_source": "pre-existing v7 research configuration",
            "new_weight_grid_searched": False,
            "target_source": "frozen v14 stock target schedule",
            "rebalance_frequency": "frozen v14 monthly target events",
            "v14_cash_behavior": "hold 80% QQQ core and 20% cash",
        },
        "execution": {
            "transaction_cost_bps": [10, 30, 50],
            "run_count": 1,
            "retune_after_result": False,
            "failure_action": (
                "remain BLOCKED; do not change weights, targets, or gates"
            ),
        },
        "data_split": {
            "development_validation": {
                "start": "2022-01-01",
                "end": "2024-12-31",
                "exposure_status": "HUMAN_EXPOSURE_CONTAMINATED",
            },
            "historical_robustness": {
                "start": "2025-01-01",
                "end": "2026-07-17",
                "years": [2025, 2026],
                "exposure_status": "REPEATEDLY_HUMAN_EXPOSED",
                "statistically_untouched": False,
                "may_be_called_confirmation": False,
            },
            "genuine_untouched_phase": {
                "kind": "future forward observation",
                "started": False,
                "authorized": False,
            },
        },
        "predeclared_gates": {
            "post_development_nasdaq_annual_win_count": {
                f"{cost}_bps": {"required": 2, "total_years": 2}
                for cost in (10, 30, 50)
            },
            "full_history_nasdaq_annual_win_count": {
                f"{cost}_bps": {"required": 5, "total_years": 5}
                for cost in (10, 30, 50)
            },
            "full_history_qqq_annual_win_count": {
                f"{cost}_bps": {"required": 3, "total_years": 5}
                for cost in (10, 30, 50)
            },
            "post_development_compounded_excess": {
                "benchmarks": ["Nasdaq", "QQQ total return"],
                "cost_bps": [10, 30, 50],
                "operator": ">",
                "threshold": 0.0,
            },
            "full_history_compounded_excess": {
                "benchmarks": ["Nasdaq", "QQQ total return"],
                "cost_bps": [10, 30, 50],
                "operator": ">",
                "threshold": 0.0,
            },
            "drawdown": {
                "cost_bps": 10,
                "maximum_loss_fraction": 0.40,
                "maximum_underperformance_vs_nasdaq_percentage_points": 5.0,
            },
            "leave_one_satellite_out": {
                "remove": "largest non-QQQ satellite contribution",
                "selection_metric": (
                    "largest non-QQQ net arithmetic daily return attribution"
                ),
                "removed_weight_behavior": "leave as cash; do not renormalize",
                "benchmarks": ["Nasdaq", "QQQ total return"],
                "compounded_excess_operator": ">",
                "compounded_excess_threshold": 0.0,
            },
            "gate_failure": "release_status remains BLOCKED",
        },
        "interpretation_guardrail": (
            "This protocol authorizes one contaminated historical robustness "
            "replay. Even a pass cannot be called confirmation and cannot "
            "authorize release, promotion, or trading."
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
        "frozen_architecture": report["frozen_architecture"],
        "historical_robustness_replay_executed": report[
            "historical_robustness_replay_executed"
        ],
        "release_status": report["release_status"],
        "output": report["output"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
