#!/usr/bin/env python3
"""Adjudicate the v22 observation's two economically neutral holiday rows.

The original frozen protocol and one-shot result remain immutable.  This
separate artifact may classify the calendar-only blocker after proving that
all extra rows are zero-activity rows and that removing them changes none of
the precommitted performance metrics or gates.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v22_2026_observation as observation


PROTOCOL_BINDING = {
    "path": str(observation.PROTOCOL_PATH),
    "sha256": "ad1551c77e4ab9a48ea99b9a3f6ea81cdf5d2563a5c101871148af3d20f952d1",
}
MANIFEST_BINDING = {
    "path": str(observation.RESULTS_DIR / "manifest.json"),
    "sha256": "3a6e3cc04faa7d14596c9b3823adad3be1fb3d9598196d7fe4b46dc7db8d29e5",
}
OUTPUT_PATH = observation.RESULTS_DIR / "calendar_adjudication.json"
NEUTRAL_COLUMNS = (
    "strategy",
    "benchmark",
    "qqq",
    "turnover",
    "transaction_cost",
)
METRIC_COLUMNS = (
    "strategy",
    "nasdaq",
    "qqq",
    "excess_vs_nasdaq",
    "excess_vs_qqq",
    "strategy_maximum_drawdown",
    "nasdaq_maximum_drawdown",
    "drawdown_lag_vs_nasdaq",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _verified_json(binding: dict) -> dict:
    path = Path(binding["path"])
    actual = _sha256(path)
    if actual != binding["sha256"]:
        raise RuntimeError(f"bound artifact changed: {path} {actual}")
    return json.loads(path.read_text(encoding="utf-8"))


def _calendar_check(
    frame: pd.DataFrame,
    expected_dates: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, dict]:
    missing = expected_dates.difference(frame.index)
    extra = frame.index.difference(expected_dates)
    extra_rows = frame.loc[extra]
    neutral = bool(
        len(extra_rows)
        and extra_rows[list(NEUTRAL_COLUMNS)].fillna(0.0).abs().le(1e-15).all().all()
    )
    normalized = frame.reindex(expected_dates)
    return normalized, {
        "missing_expected_dates": [stamp.strftime("%Y-%m-%d") for stamp in missing],
        "extra_non_session_dates": [stamp.strftime("%Y-%m-%d") for stamp in extra],
        "extra_rows_are_economically_neutral": neutral,
        "raw_session_count": len(frame),
        "normalized_session_count": len(normalized),
    }


def adjudicate(output_path: Path = OUTPUT_PATH) -> dict:
    output_path = Path(output_path)
    if output_path.exists():
        raise RuntimeError(f"calendar adjudication will not be overwritten: {output_path}")

    protocol = _verified_json(PROTOCOL_BINDING)
    manifest = _verified_json(MANIFEST_BINDING)
    if manifest["observation_status"] != "BLOCKED":
        raise RuntimeError("original observation status changed")
    original = manifest["observation_result"]
    if original["data_gates"]["exact_session_calendar"]:
        raise RuntimeError("original observation did not have a calendar blocker")
    if not original["data_gates"]["decision_month_gate"]:
        raise RuntimeError("original observation also failed its decision-month gate")

    nasdaq_binding = protocol["input_bindings"]["nasdaq_index"]
    nasdaq_dates = observation._date_index(Path(nasdaq_binding["path"]), "date")
    expected_dates = nasdaq_dates[
        (nasdaq_dates >= pd.Timestamp(protocol["observation_window"]["start"]))
        & (nasdaq_dates <= pd.Timestamp(protocol["observation_window"]["end"]))
    ]
    if observation._date_sequence_sha256(expected_dates) != protocol[
        "observation_window"
    ]["expected_session_dates_sha256"]:
        raise RuntimeError("frozen expected session calendar changed")

    cost_checks = {}
    all_neutral = True
    all_metrics_unchanged = True
    for cost in observation.v19.COSTS:
        binding = manifest["outputs"][f"observation_daily_{cost}bps"]
        if _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"daily observation changed at {cost}bps")
        frame = pd.read_csv(
            binding["path"], index_col="date", parse_dates=True
        )
        normalized, calendar = _calendar_check(frame, expected_dates)
        recalculated = observation._period_metrics(normalized)
        original_metrics = original["costs"][str(cost)]
        differences = {
            name: float(recalculated[name] - original_metrics[name])
            for name in METRIC_COLUMNS
        }
        metrics_unchanged = all(abs(value) <= 1e-12 for value in differences.values())
        no_missing = not calendar["missing_expected_dates"]
        neutral = calendar["extra_rows_are_economically_neutral"] and no_missing
        all_neutral = all_neutral and neutral
        all_metrics_unchanged = all_metrics_unchanged and metrics_unchanged
        cost_checks[str(cost)] = {
            **calendar,
            "metric_differences_after_normalization": differences,
            "all_performance_metrics_unchanged": metrics_unchanged,
            "original_acceptance_cost_gate_passed": original_metrics[
                "all_cost_gates_passed"
            ],
        }

    performance_gates_passed = all(
        original["costs"][str(cost)]["all_cost_gates_passed"]
        for cost in observation.GATE_COSTS
    )
    adjudicated_pass = bool(
        all_neutral and all_metrics_unchanged and performance_gates_passed
    )
    if not adjudicated_pass:
        decision = "KEEP_BLOCKED"
        additional_months = None
        operational_cycles = None
    else:
        decision = "PERFORMANCE_GATES_SATISFIED_AFTER_TECHNICAL_CALENDAR_ADJUDICATION"
        pass_policy = protocol["precommitted_decision_policy"]["if_all_gates_pass"]
        additional_months = pass_policy[
            "additional_performance_observation_months_required"
        ]
        operational_cycles = pass_policy[
            "minimum_future_operational_dry_run_cycles_required"
        ]

    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V22_2026_POST_RESULT_TECHNICAL_CALENDAR_ADJUDICATION",
        "adjudicated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "original_protocol": PROTOCOL_BINDING,
        "original_result_manifest": MANIFEST_BINDING,
        "original_observation_status": manifest["observation_status"],
        "original_blocker": "TWO_EXTRA_ZERO_ACTIVITY_US_MARKET_HOLIDAY_ROWS",
        "post_result_technical_adjudication": True,
        "strategy_parameters_changed": False,
        "performance_acceptance_gates_changed": False,
        "performance_metrics_changed": False,
        "cost_checks": cost_checks,
        "adjudicated_performance_status": "PASS" if adjudicated_pass else "BLOCKED",
        "adjudicated_decision": decision,
        "additional_performance_observation_months_required": additional_months,
        "minimum_future_operational_dry_run_cycles_required": operational_cycles,
        "actual_account_whole_share_validation_status": "PENDING",
        "operational_shadow_status": "NOT_STARTED",
        "statistically_untouched": False,
        "may_be_called_clean_confirmation": False,
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "implementation": {
            "path": str(Path(__file__).relative_to(Path.cwd())),
            "sha256": _sha256(Path(__file__)),
        },
        "interpretation_guardrail": (
            "This post-result adjudication cannot turn the observation into a "
            "clean blind holdout. It only proves that two US-market holiday rows "
            "were economically neutral and that the already-frozen performance "
            "metrics and gates are unchanged. Trading remains unauthorized."
        ),
    }
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "adjudication": {"path": str(output_path), "sha256": _sha256(output_path)},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = adjudicate(args.output)
    print(json.dumps({
        "original_observation_status": report["original_observation_status"],
        "adjudicated_performance_status": report["adjudicated_performance_status"],
        "additional_performance_observation_months_required": report[
            "additional_performance_observation_months_required"
        ],
        "minimum_future_operational_dry_run_cycles_required": report[
            "minimum_future_operational_dry_run_cycles_required"
        ],
        "release_status": report["release_status"],
        "adjudication": report["adjudication"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
