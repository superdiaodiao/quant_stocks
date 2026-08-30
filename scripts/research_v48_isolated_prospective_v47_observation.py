#!/usr/bin/env python3
"""Isolated prospective runner for the frozen v47 hybrid stock model.

v48 supersedes v43 before either protocol produced a signal.  It preserves the
tested v43 isolation, chronology, and crash-recovery envelope while replacing
only the frozen risk replay: a 20 percent loss from the latest monthly entry
price plus a 25 percent monthly-reset portfolio trailing stop.

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pandas as pd

from scripts import research_v42_prospective_v28_observation as v42
from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v47_hybrid_entry_portfolio_stop as v47


MODEL_VERSION = "v48-isolated-v47-entry20-portfolio25"
FIRST_PROSPECTIVE_SIGNAL_DATE = v43.FIRST_PROSPECTIVE_SIGNAL_DATE
OUTPUT_DIR = Path("output/research_only/v48/v47_prospective_20260830")
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
LEDGER_PATH = OUTPUT_DIR / "prospective_ledger.jsonl"
SIGNALS_DIR = OUTPUT_DIR / "signals"
BUNDLES_DIR = OUTPUT_DIR / "bundles"
WORK_DIR = OUTPUT_DIR / "staging_work"
V43_SUPERSESSION_PATH = v43.OUTPUT_DIR / "superseded_by_v48.json"
V47_MANIFEST = v47.DEVELOPMENT_OUTPUT_DIR / "manifest.json"

_sha256 = v43._sha256
_file_binding = v43._file_binding
_validate_event_chain = v43._validate_event_chain
read_ledger = v43.read_ledger
append_event = v43.append_event


def _selected_model() -> dict:
    """Bind the v26 selector to the one fixed v47 hybrid risk rule."""
    manifest = json.loads(V47_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("development_status") != "PASS":
        raise RuntimeError("v47 development result is no longer PASS")
    if not manifest.get("research_forward_observation_ready"):
        raise RuntimeError("v47 is no longer forward-observation ready")
    if not manifest.get("v43_supersession_eligible"):
        raise RuntimeError("v47 is no longer eligible to supersede v43")
    if manifest.get("candidate_specification") != v47.candidate_spec():
        raise RuntimeError("v47 selected hybrid specification changed")
    if manifest.get("release_status") != "BLOCKED" or manifest.get(
        "promotion_eligible"
    ):
        raise RuntimeError("v47 release boundary changed")
    model = v42._selected_model()
    model["risk_specification"] = {
        **manifest["candidate_specification"],
        # Retained for the v42 calculation-core interface.  The adapter below
        # verifies that it equals the frozen portfolio-stop threshold.
        "trailing_stop_fraction": v47.PORTFOLIO_TRAILING_STOP_FRACTION,
    }
    return model


def _v43_zero_signal_events() -> list[dict]:
    events = v43.read_ledger(v43.LEDGER_PATH)
    if [event["event_type"] for event in events] != ["PROTOCOL_FROZEN"]:
        raise RuntimeError("v43 can only be superseded before its first signal")
    return events


def freeze_protocol(
    path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
) -> dict:
    """Write the immutable v48 protocol and its genesis ledger event."""
    item = Path(path)
    ledger = Path(ledger_path)
    if item.exists() or ledger.exists():
        raise RuntimeError("v48 protocol/ledger will not be overwritten")
    _v43_zero_signal_events()
    model = _selected_model()
    v47_manifest = json.loads(V47_MANIFEST.read_text(encoding="utf-8"))
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "model_version": MODEL_VERSION,
        "status": "FROZEN_WAITING_FOR_FIRST_SIGNAL",
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_commit": v43._git_head(),
        "supersedes": {
            "model_version": v43.MODEL_VERSION,
            "protocol": _file_binding(v43.PROTOCOL_PATH),
            "ledger": _file_binding(v43.LEDGER_PATH),
            "v43_signal_count": 0,
            "reason": (
                "v47 passed every frozen training and drawdown gate before the "
                "first v43 prospective signal"
            ),
        },
        "model": model,
        "development_evidence": {
            "v47_manifest": _file_binding(V47_MANIFEST),
            "development_status": "PASS",
            "training_years": list(v47.DEVELOPMENT_YEARS),
            "positive_training_years_50bps": v47_manifest[
                "positive_training_years_50bps"
            ],
            "strategy_drawdown_50bps": v47_manifest[
                "strategy_drawdown_50bps"
            ],
            "maximum_allowed_drawdown_50bps": v47_manifest[
                "maximum_allowed_drawdown_50bps"
            ],
            "training_years_counted_as_official_wins": 0,
        },
        "evidence_partition": {
            "2019": {
                "role": "BASE_SELECTOR_AND_ADJUDICATED_SELECTION_PATH",
                "counts_as_official_comparison": False,
            },
            "2020_2025": {
                "role": "TRAINING_AND_MODEL_SELECTION_ONLY",
                "years": list(v47.DEVELOPMENT_YEARS),
                "counts_as_official_comparison": False,
                "official_year_wins": 0,
            },
            "2026_01_07": {
                "role": "RESEARCHER_EXPOSED_REUSED_DIAGNOSTIC",
                "start": v42.REUSED_DIAGNOSTIC_START,
                "end": v42.REUSED_DIAGNOSTIC_END,
                "counts_as_official_comparison": False,
                "official_year_wins": 0,
            },
            "prospective": {
                "first_signal_date": FIRST_PROSPECTIVE_SIGNAL_DATE.strftime(
                    "%Y-%m-%d"
                ),
                "performance_start": (
                    "first common trading-session close after frozen signal"
                ),
                "counts_as_official_comparison": True,
            },
        },
        "signal_policy": {
            "frequency": "completed calendar-month final Nasdaq session",
            "execution": "next common trading-session close",
            "holdings": "individual common equities or cash only",
            "forbidden_etfs": sorted(v42.FORBIDDEN_ETFS),
            "chronology": "strictly_increasing_signal_and_execution_dates",
        },
        "risk_policy": {
            "entry_loss_fraction": v47.ENTRY_LOSS_FRACTION,
            "entry_reference": "adjusted close at latest monthly rebalance",
            "portfolio_trailing_stop_fraction": (
                v47.PORTFOLIO_TRAILING_STOP_FRACTION
            ),
            "portfolio_peak_reset": "latest monthly rebalance",
            "signal_frequency": "daily completed close",
            "execution": "next common trading-session close",
            "reentry": "next frozen monthly target only",
        },
        "evaluation": {
            "primary_benchmark": "NASDAQ_COMPOSITE_PRICE_RETURN",
            "secondary_benchmark": "QQQ_TOTAL_RETURN_REFERENCE_ONLY",
            "transaction_cost_bps": list(v42.COSTS),
            "training_years_are_never_counted_as_wins": True,
            "researcher_exposed_2026_diagnostic_is_never_counted_as_a_win": True,
            "official_score_requires_complete_prospective_periods": True,
        },
        "runtime_isolation": {
            "formal_market_files_writable": False,
            "formal_financial_files_writable": False,
            "shared_companyfacts_cache_writable": False,
            "companyfacts_cache_mode": "hardlink_clone_then_atomic_replace",
            "qqq_rows_after_bundle_as_of_allowed": False,
        },
        "immutability": {
            "protocol_overwrite_allowed": False,
            "signal_overwrite_allowed": False,
            "ledger_mode": "append_only_sha256_chain",
            "deterministic_signal_recovery_allowed": True,
        },
        "input_bindings": {
            "runner": _file_binding(runner),
            "v43_runtime_core": _file_binding(Path(v43.__file__)),
            "v43_protocol": _file_binding(v43.PROTOCOL_PATH),
            "v43_ledger": _file_binding(v43.LEDGER_PATH),
            "v42_calculation_core": _file_binding(Path(v42.__file__)),
            "v47_protocol": _file_binding(v47.PROTOCOL_PATH),
            "v47_runner": _file_binding(Path(v47.__file__)),
            "v47_manifest": _file_binding(V47_MANIFEST),
        },
        "parameters_frozen_before_prospective_data": True,
        "contains_index_etf_holdings": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(protocol, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    protocol_sha = _sha256(item)
    append_event(
        path=ledger,
        protocol_sha256=protocol_sha,
        event_type="PROTOCOL_FROZEN",
        payload={
            "protocol_path": str(item),
            "protocol_sha256": protocol_sha,
            "first_prospective_signal_date": FIRST_PROSPECTIVE_SIGNAL_DATE.strftime(
                "%Y-%m-%d"
            ),
            "official_training_year_wins": 0,
            "superseded_v43_signal_count": 0,
        },
    )
    return {**protocol, "protocol": _file_binding(item), "ledger": str(ledger)}


def write_v43_supersession(
    *,
    path: str | Path = V43_SUPERSESSION_PATH,
    successor_protocol: str | Path = PROTOCOL_PATH,
) -> dict:
    """Record that no v43 prospective observation was discarded."""
    item = Path(path)
    if item.exists():
        raise RuntimeError("v43 supersession record will not be overwritten")
    _v43_zero_signal_events()
    record = {
        "schema_version": 1,
        "status": "SUPERSEDED_BEFORE_FIRST_SIGNAL",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "v43_protocol": _file_binding(v43.PROTOCOL_PATH),
        "v43_ledger": _file_binding(v43.LEDGER_PATH),
        "v43_signal_count": 0,
        "successor_protocol": _file_binding(successor_protocol),
        "reason": (
            "v47 passed every predeclared development gate; no v43 prospective "
            "signal, execution, or valuation was discarded"
        ),
    }
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {**record, "record": _file_binding(item)}


def _validated_protocol(path: str | Path = PROTOCOL_PATH) -> tuple[dict, str]:
    item = Path(path)
    protocol = json.loads(item.read_text(encoding="utf-8"))
    protocol_sha = _sha256(item)
    if protocol.get("model_version") != MODEL_VERSION:
        raise RuntimeError("unexpected v48 model version")
    if protocol.get("model") != _selected_model():
        raise RuntimeError("v48 frozen model binding changed")
    if protocol.get("release_status") != "BLOCKED" or protocol.get(
        "promotion_eligible"
    ):
        raise RuntimeError("v48 release boundary changed")
    if protocol["evidence_partition"]["2020_2025"]["official_year_wins"] != 0:
        raise RuntimeError("v48 training evidence was counted as official")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v48 frozen input changed: {name}")
    return protocol, protocol_sha


@contextmanager
def _v43_runtime():
    replacements = {
        "MODEL_VERSION": MODEL_VERSION,
        "_validated_protocol": _validated_protocol,
    }
    original = {name: getattr(v43, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(v43, name, value)
        yield
    finally:
        for name, value in original.items():
            setattr(v43, name, value)


def _hybrid_replay_adapter(
    raw_close: pd.DataFrame,
    index_close: pd.Series,
    target_schedule: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    trailing_stop_fraction: float,
    transaction_cost_bps: float,
) -> pd.DataFrame:
    if trailing_stop_fraction != v47.PORTFOLIO_TRAILING_STOP_FRACTION:
        raise RuntimeError("v48 portfolio-stop interface binding changed")
    return v47.replay_with_hybrid_stop(
        raw_close,
        index_close,
        target_schedule,
        start,
        end,
        entry_loss_fraction=v47.ENTRY_LOSS_FRACTION,
        portfolio_stop_fraction=v47.PORTFOLIO_TRAILING_STOP_FRACTION,
        transaction_cost_bps=transaction_cost_bps,
    )


def stage_bundle(**kwargs) -> dict:
    kwargs.setdefault("bundles_dir", BUNDLES_DIR)
    kwargs.setdefault("work_dir", WORK_DIR)
    kwargs.setdefault("signals_dir", SIGNALS_DIR)
    kwargs.setdefault("ledger_path", LEDGER_PATH)
    with _v43_runtime():
        return v43.stage_bundle(**kwargs)


def _validated_bundle(
    bundle: str | Path, expected_purpose: str | None = None
) -> tuple[dict, str]:
    with _v43_runtime():
        return v43._validated_bundle(bundle, expected_purpose)


def freeze_signal(
    *,
    bundle: str | Path,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
    signals_dir: str | Path = SIGNALS_DIR,
) -> dict:
    with _v43_runtime():
        return v43.freeze_signal(
            bundle=bundle,
            protocol_path=protocol_path,
            ledger_path=ledger_path,
            signals_dir=signals_dir,
        )


def append_mark(
    *,
    bundle: str | Path,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
    signals_dir: str | Path = SIGNALS_DIR,
) -> dict:
    original_replay = v42.v28.replay_with_individual_trailing_stop
    try:
        v42.v28.replay_with_individual_trailing_stop = _hybrid_replay_adapter
        with _v43_runtime():
            result = v43.append_mark(
                bundle=bundle,
                protocol_path=protocol_path,
                ledger_path=ledger_path,
                signals_dir=signals_dir,
            )
    finally:
        v42.v28.replay_with_individual_trailing_stop = original_replay
    result["hybrid_risk_replay_verified"] = True
    return result


def status(
    *,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
) -> dict:
    with _v43_runtime():
        result = v43.status(
            protocol_path=protocol_path,
            ledger_path=ledger_path,
        )
    result["supersedes_v43_before_first_signal"] = True
    result.pop("supersedes_v42_before_first_signal", None)
    result["frozen_risk_model"] = (
        "monthly_entry_loss_20pct_plus_portfolio_trailing_stop_25pct"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("freeze-protocol")
    subparsers.add_parser("write-v43-supersession")
    stage_parser = subparsers.add_parser("stage-bundle")
    stage_parser.add_argument("--as-of", required=True)
    stage_parser.add_argument("--purpose", choices=["SIGNAL", "MARK"], required=True)
    stage_parser.add_argument("--workers", type=int, default=16)
    stage_parser.add_argument("--fundamental-workers", type=int, default=4)
    signal_parser = subparsers.add_parser("freeze-signal")
    signal_parser.add_argument("--bundle", type=Path, required=True)
    mark_parser = subparsers.add_parser("append-mark")
    mark_parser.add_argument("--bundle", type=Path, required=True)
    subparsers.add_parser("status")
    args = parser.parse_args()
    if args.command == "freeze-protocol":
        result = freeze_protocol()
    elif args.command == "write-v43-supersession":
        result = write_v43_supersession()
    elif args.command == "stage-bundle":
        result = stage_bundle(
            as_of=args.as_of,
            purpose=args.purpose,
            workers=args.workers,
            fundamental_workers=args.fundamental_workers,
        )
    elif args.command == "freeze-signal":
        result = freeze_signal(bundle=args.bundle)
    elif args.command == "append-mark":
        result = append_mark(bundle=args.bundle)
    else:
        result = status()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
