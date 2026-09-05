#!/usr/bin/env python3
"""Supersede the zero-signal v50r1 protocol after the missed 2026-08-31 window.

v50r2 is a runtime repair, not a model change.  It reuses v50r1's immutable
development replay, selector, and 20%/25% risk thresholds byte-for-byte and
leaves ``scripts/research_v50_corrected_v47.py`` untouched so that the r1
protocol stays independently verifiable.

Two facts motivate the revision:

* the inherited v42 bundle-manifest writer stores ``Series.all()`` readiness
  gates, which are NumPy booleans that the standard JSON encoder rejects, so
  every SIGNAL staging would have crashed after its downloads and discarded
  the staged data; and
* the original 2026-08-31 window was missed by an external scheduler.  It is
  recorded as missed and is never backfilled; the first legal SIGNAL date is
  2026-09-30.

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import research_v42_prospective_v28_observation as v42
from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v48_isolated_prospective_v47_observation as v48
from scripts import research_v50_corrected_v47 as r1
from src.research.corrected_stock_policy import VALIDATION_PATH


MODEL_VERSION = "v50r2-corrected-v47-sourced-actions"
SUPERSEDED_MODEL_VERSION = r1.MODEL_VERSION
# The original window is recorded as missed; it is never backfilled.
MISSED_SIGNAL_DATE = pd.Timestamp(r1.FIRST_PROSPECTIVE_SIGNAL_DATE)
FIRST_PROSPECTIVE_SIGNAL_DATE = pd.Timestamp("2026-09-30")
REPO_ROOT = r1.REPO_ROOT
# r1's development replay is immutable and reused verbatim.
DEVELOPMENT_DIR = r1.OUTPUT_DIR
DEVELOPMENT_PROTOCOL_PATH = r1.DEVELOPMENT_PROTOCOL_PATH
DEVELOPMENT_OUTPUT_DIR = r1.DEVELOPMENT_OUTPUT_DIR
V50R1_PROTOCOL_PATH = r1.PROTOCOL_PATH
V50R1_LEDGER_PATH = r1.LEDGER_PATH
V50R1_SUPERSESSION_PATH = r1.OUTPUT_DIR / "superseded_by_v50r2.json"
OUTPUT_DIR = Path("output/research_only/v50/corrected_v47_20260905_r2")
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
LEDGER_PATH = OUTPUT_DIR / "prospective_ledger.jsonl"
SIGNALS_DIR = OUTPUT_DIR / "signals"
BUNDLES_DIR = OUTPUT_DIR / "bundles"
WORK_DIR = OUTPUT_DIR / "staging_work"

_sha256 = r1._sha256
_portable_path = r1._portable_path
_file_binding = r1._file_binding
_git_head = r1._git_head
_signal_staging_is_timely = r1._signal_staging_is_timely
_hybrid_replay_adapter = r1._hybrid_replay_adapter


def runtime_repair_specification() -> dict:
    """Describe the r2 runtime repair; the model and thresholds are unchanged."""
    return {
        "bundle_manifest_scalars": "numpy_scalars_normalized_to_python",
        "development_replay": "reused_from_v50r1_unchanged",
        "r1_runner_modified": False,
        "missed_signal_dates": [MISSED_SIGNAL_DATE.strftime("%Y-%m-%d")],
        "missed_signal_backfill_allowed": False,
        "first_prospective_signal_date": (
            FIRST_PROSPECTIVE_SIGNAL_DATE.strftime("%Y-%m-%d")
        ),
        "new_threshold_search": False,
    }


def _zero_signal_v50r1_events() -> list[dict]:
    events = v43.read_ledger(V50R1_LEDGER_PATH)
    if [event["event_type"] for event in events] != ["PROTOCOL_FROZEN"]:
        raise RuntimeError("v50r1 can only be superseded before its first signal")
    return events


def _selected_model() -> dict:
    return r1._selected_model()


class _JsonScalarProxy:
    """Delegate ``json`` while normalizing NumPy scalars in ``dumps``.

    Serializable payloads are emitted byte-for-byte unchanged, so ledger
    event hashes are unaffected; only otherwise-fatal NumPy scalars change.
    """

    def __init__(self, delegate):
        self._delegate = delegate

    def dumps(self, value, *args, **kwargs):
        original_default = kwargs.get("default")

        def normalize(item):
            if isinstance(item, np.generic):
                return item.item()
            if original_default is not None:
                return original_default(item)
            raise TypeError(
                f"Object of type {item.__class__.__name__} is not JSON serializable"
            )

        kwargs["default"] = normalize
        return self._delegate.dumps(value, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._delegate, name)


def _validated_protocol(
    path: str | Path = PROTOCOL_PATH,
) -> tuple[dict, str]:
    item = Path(path)
    protocol = json.loads(item.read_text(encoding="utf-8"))
    protocol_sha = _sha256(item)
    if protocol.get("model_version") != MODEL_VERSION:
        raise RuntimeError("unexpected v50r2 model version")
    if protocol.get("model") != _selected_model():
        raise RuntimeError("v50r2 frozen model binding changed")
    if protocol.get("runtime_repair") != runtime_repair_specification():
        raise RuntimeError("v50r2 runtime repair specification changed")
    if protocol.get("release_status") != "BLOCKED" or protocol.get(
        "promotion_eligible"
    ):
        raise RuntimeError("v50r2 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v50r2 frozen input changed: {name}")
    return protocol, protocol_sha


def _validated_bundle(
    bundle: str | Path, expected_purpose: str | None = None
) -> tuple[dict, str]:
    manifest, manifest_sha = r1.V43_VALIDATED_BUNDLE(bundle, expected_purpose)
    if manifest.get("runner_version") != MODEL_VERSION:
        raise RuntimeError("bundle was not frozen by the v50r2 runner")
    if manifest.get("purpose") == "SIGNAL":
        created_at = pd.Timestamp(manifest["created_at"])
        if created_at.tzinfo is None:
            raise RuntimeError("v50r2 SIGNAL bundle created_at lacks a timezone")
        as_of = pd.Timestamp(manifest["as_of"])
        if as_of < FIRST_PROSPECTIVE_SIGNAL_DATE:
            raise RuntimeError(
                "v50r2 refuses a SIGNAL bundle for a missed or pre-r2 date"
            )
        if created_at.tz_convert("UTC").date() != as_of.date():
            raise RuntimeError(
                "v50r2 SIGNAL bundle was staged after its declared UTC date"
            )
    return manifest, manifest_sha


def _build_signal_payload(**kwargs) -> dict:
    payload = r1._build_signal_payload(**kwargs)
    payload["model_version"] = MODEL_VERSION
    payload["runtime_repair"] = runtime_repair_specification()
    return payload


@contextmanager
def _runtime():
    """Bind the isolated v43 runtime to r2 and make manifest writes robust."""
    replacements = {
        "MODEL_VERSION": MODEL_VERSION,
        "_validated_protocol": _validated_protocol,
        "_validated_bundle": _validated_bundle,
        "_build_signal_payload": _build_signal_payload,
    }
    original = {name: getattr(v43, name) for name in replacements}
    original_v42_json = v43.v42.json
    original_v43_json = v43.json
    try:
        for name, value in replacements.items():
            setattr(v43, name, value)
        v43.v42.json = _JsonScalarProxy(original_v42_json)
        v43.json = _JsonScalarProxy(original_v43_json)
        yield
    finally:
        v43.json = original_v43_json
        v43.v42.json = original_v42_json
        for name, value in original.items():
            setattr(v43, name, value)


def freeze_protocol(
    path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
) -> dict:
    item = Path(path)
    ledger = Path(ledger_path)
    if item.exists() or ledger.exists():
        raise RuntimeError("v50r2 protocol/ledger will not be overwritten")
    r1._zero_signal_v48_events()
    _zero_signal_v50r1_events()
    manifest = r1._development_manifest()
    protocol = {
        "schema_version": 3,
        "research_only": True,
        "model_version": MODEL_VERSION,
        "status": "FROZEN_WAITING_FOR_FIRST_SIGNAL",
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_commit": _git_head(),
        "supersedes": {
            "model_version": SUPERSEDED_MODEL_VERSION,
            "protocol": _file_binding(V50R1_PROTOCOL_PATH),
            "ledger": _file_binding(V50R1_LEDGER_PATH),
            "v50r1_signal_count": 0,
            "reason": [
                "the inherited v42 bundle-manifest writer could not serialize "
                "the NumPy boolean fundamentals readiness gate, so every "
                "SIGNAL staging would have failed after its downloads",
                "the 2026-08-31 window was missed by the external scheduler "
                "and is recorded as missed, never backfilled",
            ],
        },
        "runtime_repair": runtime_repair_specification(),
        "model": _selected_model(),
        "evidence_partition": {
            "2020_2025": {
                "role": "CORRECTED_TRAINING_DIAGNOSTIC_ONLY",
                "years": list(r1.DEVELOPMENT_YEARS),
                "counts_as_official_comparison": False,
                "official_year_wins": 0,
            },
            "2026_01_07": {
                "role": "RESEARCHER_EXPOSED_REUSED_DIAGNOSTIC",
                "counts_as_official_comparison": False,
                "official_year_wins": 0,
            },
            "2026_08": {
                "role": "MISSED_WINDOW_NOT_BACKFILLED",
                "counts_as_official_comparison": False,
                "official_year_wins": 0,
            },
            "prospective": {
                "first_signal_date": FIRST_PROSPECTIVE_SIGNAL_DATE.strftime(
                    "%Y-%m-%d"
                ),
                "counts_as_official_comparison": True,
            },
        },
        "signal_policy": {
            "frequency": "completed calendar-month final Nasdaq session",
            "execution": "next common trading-session close",
            "universe_staging": "same UTC date as signal",
            "late_signal_bundle_allowed": False,
            "missed_signal_dates": [MISSED_SIGNAL_DATE.strftime("%Y-%m-%d")],
            "missed_signal_backfill_allowed": False,
        },
        "risk_policy": {
            "entry_loss_fraction": r1.ENTRY_LOSS_FRACTION,
            "entry_reference": "adjusted close at latest monthly rebalance",
            "portfolio_trailing_stop_fraction": (
                r1.PORTFOLIO_TRAILING_STOP_FRACTION
            ),
            "portfolio_peak_reset": "latest monthly rebalance",
            "stop_checked_before_rebalance": True,
            "coincident_stop_precedence": "stop_vetoes_same_close_reentry",
            "missing_entry_close": "fail_closed",
        },
        "price_policy": {
            "automatic_heuristic_adjustment_allowed": False,
            "confirmed_actions_only": True,
            "reviewed_market_moves_preserved": True,
            "unresolved_rank_or_target_event": "fail_closed",
        },
        "evaluation": {
            "primary_benchmark": "NASDAQ_COMPOSITE_PRICE_RETURN",
            "secondary_benchmark": "QQQ_TOTAL_RETURN_REFERENCE_ONLY",
            "transaction_cost_bps": list(r1.COSTS),
            "training_years_are_never_counted_as_wins": True,
            "official_score_requires_complete_prospective_periods": True,
        },
        "immutability": {
            "protocol_overwrite_allowed": False,
            "signal_overwrite_allowed": False,
            "ledger_mode": "append_only_sha256_chain",
            "input_binding_location": "repository_relative",
        },
        "input_bindings": {
            "runner": _file_binding(__file__),
            "r1_runner": _file_binding(r1.__file__),
            "corrected_policy": _file_binding(
                "src/research/corrected_stock_policy.py"
            ),
            "development_protocol": _file_binding(DEVELOPMENT_PROTOCOL_PATH),
            "development_manifest": _file_binding(
                DEVELOPMENT_OUTPUT_DIR / "manifest.json"
            ),
            "v43_runtime_core": _file_binding(v43.__file__),
            "v42_calculation_core": _file_binding(v42.__file__),
            "v48_protocol": _file_binding(v48.PROTOCOL_PATH),
            "v48_ledger": _file_binding(v48.LEDGER_PATH),
            "v50r1_protocol": _file_binding(V50R1_PROTOCOL_PATH),
            "v50r1_ledger": _file_binding(V50R1_LEDGER_PATH),
            "corporate_action_validation": _file_binding(VALIDATION_PATH),
            "reviewed_market_moves": _file_binding(
                "stocks_list_dir/nasdaq/reviewed_market_moves.csv"
            ),
        },
        "corrected_training_positive_years_50bps": manifest[
            "positive_training_years_50bps"
        ],
        "parameters_frozen_before_prospective_data": True,
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
    v43.append_event(
        path=ledger,
        protocol_sha256=protocol_sha,
        event_type="PROTOCOL_FROZEN",
        payload={
            "protocol_path": _portable_path(item),
            "protocol_sha256": protocol_sha,
            "first_prospective_signal_date": (
                FIRST_PROSPECTIVE_SIGNAL_DATE.strftime("%Y-%m-%d")
            ),
            "missed_signal_dates": [MISSED_SIGNAL_DATE.strftime("%Y-%m-%d")],
            "official_training_year_wins": 0,
            "superseded_v48_signal_count": 0,
            "superseded_v50r1_signal_count": 0,
        },
    )
    return {**protocol, "protocol": _file_binding(item), "ledger": str(ledger)}


def write_v50r1_supersession(
    *,
    path: str | Path = V50R1_SUPERSESSION_PATH,
    successor_protocol: str | Path = PROTOCOL_PATH,
) -> dict:
    item = Path(path)
    if item.exists():
        raise RuntimeError("v50r1 supersession record will not be overwritten")
    _zero_signal_v50r1_events()
    record = {
        "schema_version": 1,
        "status": "SUPERSEDED_BEFORE_FIRST_SIGNAL",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "v50r1_protocol": _file_binding(V50R1_PROTOCOL_PATH),
        "v50r1_ledger": _file_binding(V50R1_LEDGER_PATH),
        "v50r1_signal_count": 0,
        "missed_signal_dates": [MISSED_SIGNAL_DATE.strftime("%Y-%m-%d")],
        "successor_protocol": _file_binding(successor_protocol),
        "reason": (
            "the v42 bundle-manifest writer could not serialize the NumPy "
            "boolean readiness gate; the r1 development replay is reused "
            "unchanged and the missed 2026-08-31 window is never backfilled"
        ),
    }
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return record


def stage_bundle(*, observed_at: datetime | None = None, **kwargs) -> dict:
    purpose = str(kwargs.get("purpose", "")).upper()
    stamp = pd.Timestamp(kwargs.get("as_of")).normalize()
    kwargs.setdefault("bundles_dir", BUNDLES_DIR)
    kwargs.setdefault("work_dir", WORK_DIR)
    kwargs.setdefault("signals_dir", SIGNALS_DIR)
    kwargs.setdefault("ledger_path", LEDGER_PATH)
    if purpose == "SIGNAL" and stamp < FIRST_PROSPECTIVE_SIGNAL_DATE:
        raise RuntimeError(
            "v50r2 refuses a SIGNAL before "
            f"{FIRST_PROSPECTIVE_SIGNAL_DATE:%Y-%m-%d}; the "
            f"{MISSED_SIGNAL_DATE:%Y-%m-%d} window was missed and is never "
            "backfilled"
        )
    existing = Path(kwargs["bundles_dir"]) / f"{stamp:%Y-%m-%d}_{purpose.lower()}"
    if purpose == "SIGNAL" and not existing.exists():
        if not _signal_staging_is_timely(stamp, observed_at):
            raise RuntimeError(
                "v50r2 refuses late SIGNAL staging with a current universe"
            )
    with _runtime():
        return v43.stage_bundle(**kwargs)


def freeze_signal(
    *,
    bundle: str | Path,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
    signals_dir: str | Path = SIGNALS_DIR,
) -> dict:
    with _runtime():
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
        with _runtime():
            result = v43.append_mark(
                bundle=bundle,
                protocol_path=protocol_path,
                ledger_path=ledger_path,
                signals_dir=signals_dir,
            )
    finally:
        v42.v28.replay_with_individual_trailing_stop = original_replay
    result["corrected_hybrid_risk_replay_verified"] = True
    return result


def status(
    *,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
) -> dict:
    with _runtime():
        result = v43.status(
            protocol_path=protocol_path,
            ledger_path=ledger_path,
        )
    result["supersedes_v48_before_first_signal"] = True
    result["supersedes_v50r1_before_first_signal"] = True
    result["corrected_price_policy"] = "SOURCED_ACTIONS_ONLY"
    result["late_signal_bundle_allowed"] = False
    result["first_prospective_signal_date"] = (
        FIRST_PROSPECTIVE_SIGNAL_DATE.strftime("%Y-%m-%d")
    )
    result["missed_signal_dates"] = [MISSED_SIGNAL_DATE.strftime("%Y-%m-%d")]
    result["bundle_manifest_scalars"] = "numpy_scalars_normalized_to_python"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("freeze-protocol")
    subparsers.add_parser("write-v50r1-supersession")
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
    elif args.command == "write-v50r1-supersession":
        result = write_v50r1_supersession()
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
