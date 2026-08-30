#!/usr/bin/env python3
"""Isolated, chronological prospective runner for the frozen v28 stock model.

v43 supersedes v42 before v42 produced any signal.  It reuses v42's tested
signal and valuation calculations while repairing runtime-envelope defects:

* SEC Company Facts is refreshed in a hard-linked isolated cache, never in the
  formal/shared cache.
* monthly signal events must be strictly chronological;
* staged QQQ data is cut off at the declared as-of session;
* a fully written deterministic signal can recover its missing ledger append.

This remains research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pandas as pd

from scripts import research_v42_prospective_v28_observation as v42
from src.conf import (
    FUNDAMENTALS_COVERAGE_FILE,
    FUNDAMENTALS_REFRESH_STATE_FILE,
    NASDAQ_300M_STOCK_LIST_FILE,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_FUNDAMENTALS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    QUARTERLY_FUNDAMENTALS_COVERAGE_FILE,
)
from src.io import fundamentals_update
from src.research.shadow_evaluation import nasdaq_calendar_for_year


MODEL_VERSION = "v43-isolated-v28-stock-only-trailing-stop-25pct"
FIRST_PROSPECTIVE_SIGNAL_DATE = v42.FIRST_PROSPECTIVE_SIGNAL_DATE
OUTPUT_DIR = Path("output/research_only/v43/v28_prospective_20260830")
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
LEDGER_PATH = OUTPUT_DIR / "prospective_ledger.jsonl"
SIGNALS_DIR = OUTPUT_DIR / "signals"
BUNDLES_DIR = OUTPUT_DIR / "bundles"
WORK_DIR = OUTPUT_DIR / "staging_work"
V42_PROTOCOL_PATH = v42.PROTOCOL_PATH
V42_LEDGER_PATH = v42.LEDGER_PATH
V42_SUPERSESSION_PATH = v42.OUTPUT_DIR / "superseded_by_v43.json"
V42_VALIDATE_EVENT_CHAIN = v42._validate_event_chain
V42_VALIDATED_BUNDLE = v42._validated_bundle
V42_REFRESH_CORE_PRICE = v42.refresh_core_price


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(path: str | Path) -> str:
    return v42._sha256(path)


def _file_binding(path: str | Path) -> dict:
    item = Path(path)
    return {"path": str(item), "sha256": _sha256(item)}


def _event_hash(event_without_hash: dict) -> str:
    return hashlib.sha256(_canonical_bytes(event_without_hash)).hexdigest()


def _validate_event_chain(events: list[dict]) -> None:
    """Apply v42 integrity checks plus strict signal/execution chronology."""
    V42_VALIDATE_EVENT_CHAIN(events)
    signal_dates = [
        pd.Timestamp(event["payload"]["signal_date"])
        for event in events
        if event["event_type"] == "SIGNAL_FROZEN"
    ]
    if any(right <= left for left, right in zip(signal_dates, signal_dates[1:])):
        raise RuntimeError("prospective signal dates must be strictly increasing")

    signal_order = {
        event["payload"]["signal_date"]: position
        for position, event in enumerate(
            item for item in events if item["event_type"] == "SIGNAL_FROZEN"
        )
    }
    execution_pairs = [
        (
            signal_order[event["payload"]["signal_date"]],
            pd.Timestamp(event["payload"]["execution_date"]),
        )
        for event in events
        if event["event_type"] == "EXECUTION_DATE_BOUND"
    ]
    execution_pairs.sort()
    execution_dates = [item[1] for item in execution_pairs]
    if any(right <= left for left, right in zip(execution_dates, execution_dates[1:])):
        raise RuntimeError("prospective execution dates must be strictly increasing")


def _read_events(handle) -> list[dict]:
    handle.seek(0)
    events = []
    for line_number, raw in enumerate(handle, start=1):
        if not raw.strip():
            continue
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"invalid prospective ledger JSON on line {line_number}"
            ) from exc
    _validate_event_chain(events)
    return events


def read_ledger(path: str | Path = LEDGER_PATH) -> list[dict]:
    item = Path(path)
    if not item.is_file():
        return []
    with item.open("r", encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle if line.strip()]
    _validate_event_chain(events)
    return events


def append_event(
    *,
    path: str | Path,
    protocol_sha256: str,
    event_type: str,
    payload: dict,
    recorded_at: str | None = None,
) -> dict:
    if event_type not in v42.EVENT_TYPES:
        raise ValueError(f"unsupported prospective event type: {event_type}")
    item = Path(path)
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        events = _read_events(handle)
        if not events and event_type != "PROTOCOL_FROZEN":
            raise RuntimeError("protocol must be frozen before other events")
        if events and event_type == "PROTOCOL_FROZEN":
            raise RuntimeError("prospective protocol event already exists")
        unsigned = {
            "event_index": len(events),
            "event_type": event_type,
            "recorded_at": recorded_at
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "protocol_sha256": protocol_sha256,
            "prev_hash": events[-1]["event_hash"] if events else v42.NULL_HASH,
            "payload": payload,
        }
        event = {**unsigned, "event_hash": _event_hash(unsigned)}
        _validate_event_chain([*events, event])
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return event


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def freeze_protocol(
    path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
) -> dict:
    item = Path(path)
    ledger = Path(ledger_path)
    if item.exists() or ledger.exists():
        raise RuntimeError("v43 protocol/ledger will not be overwritten")
    v42_events = v42.read_ledger(V42_LEDGER_PATH)
    if [event["event_type"] for event in v42_events] != ["PROTOCOL_FROZEN"]:
        raise RuntimeError("v42 can only be superseded before its first signal")
    model = v42._selected_model()
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 2,
        "research_only": True,
        "model_version": MODEL_VERSION,
        "status": "FROZEN_WAITING_FOR_FIRST_SIGNAL",
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_commit": _git_head(),
        "supersedes": {
            "model_version": v42.MODEL_VERSION,
            "protocol": _file_binding(V42_PROTOCOL_PATH),
            "ledger": _file_binding(V42_LEDGER_PATH),
            "v42_signal_count": 0,
            "reason": [
                "v42 refreshed the shared SEC cache while claiming full isolation",
                "v42 did not enforce strictly increasing monthly signal dates",
                "v42 did not cut staged QQQ rows at the declared as-of date",
                "v42 could not recover a deterministic signal after a pre-ledger crash",
            ],
        },
        "model": model,
        "evidence_partition": {
            "2019": {
                "role": "EXCLUDED_PARTIAL_PIT_COVERAGE",
                "counts_as_official_comparison": False,
            },
            "2020_2025": {
                "role": "TRAINING_AND_MODEL_SELECTION_ONLY",
                "years": list(v42.TRAINING_YEARS),
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
            "trailing_stop_fraction": 0.25,
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
            "v42_calculation_core": _file_binding(
                "scripts/research_v42_prospective_v28_observation.py"
            ),
            "v24_signal_helpers": _file_binding(
                "scripts/research_v24_stock_momentum_development.py"
            ),
            "v26_selector": _file_binding(
                "scripts/research_v26_large_liquid_stock_momentum.py"
            ),
            "v26_manifest": _file_binding(v42.V26_MANIFEST),
            "v28_risk_replay": _file_binding(
                "scripts/research_v28_stock_trailing_stop_development.py"
            ),
            "v28_manifest": _file_binding(v42.V28_MANIFEST),
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
            "first_prospective_signal_date": "2026-08-31",
            "official_training_year_wins": 0,
            "superseded_v42_signal_count": 0,
        },
    )
    return {**protocol, "protocol": _file_binding(item), "ledger": str(ledger)}


def write_v42_supersession(
    *,
    path: str | Path = V42_SUPERSESSION_PATH,
    successor_protocol: str | Path = PROTOCOL_PATH,
) -> dict:
    item = Path(path)
    if item.exists():
        raise RuntimeError("v42 supersession record will not be overwritten")
    v42_events = v42.read_ledger(V42_LEDGER_PATH)
    if [event["event_type"] for event in v42_events] != ["PROTOCOL_FROZEN"]:
        raise RuntimeError("v42 is not a zero-signal protocol")
    record = {
        "schema_version": 1,
        "status": "SUPERSEDED_BEFORE_FIRST_SIGNAL",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "v42_protocol": _file_binding(V42_PROTOCOL_PATH),
        "v42_ledger": _file_binding(V42_LEDGER_PATH),
        "v42_signal_count": 0,
        "successor_protocol": _file_binding(successor_protocol),
        "reason": (
            "runtime isolation and chronological-ledger defects found in preflight; "
            "no prospective observation was discarded"
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
        raise RuntimeError("unexpected v43 model version")
    if protocol.get("model") != v42._selected_model():
        raise RuntimeError("v43 frozen model binding changed")
    if protocol.get("release_status") != "BLOCKED" or protocol.get(
        "promotion_eligible"
    ):
        raise RuntimeError("v43 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v43 frozen input changed: {name}")
    if _sha256(protocol["supersedes"]["protocol"]["path"]) != protocol[
        "supersedes"
    ]["protocol"]["sha256"]:
        raise RuntimeError("superseded v42 protocol changed")
    return protocol, protocol_sha


def _formal_financial_bindings() -> dict[str, str]:
    paths = {
        "annual": Path(POINT_IN_TIME_FUNDAMENTALS_FILE),
        "quarterly": Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
        "state": Path(FUNDAMENTALS_REFRESH_STATE_FILE),
        "coverage": Path(FUNDAMENTALS_COVERAGE_FILE),
        "quarterly_coverage": Path(QUARTERLY_FUNDAMENTALS_COVERAGE_FILE),
        "companyfacts_manifest": (
            Path(fundamentals_update.SEC_COMPANYFACTS_CACHE_DIR) / "manifest.json"
        ),
    }
    return {name: _sha256(path) for name, path in paths.items() if path.is_file()}


def _hardlink_clone_cache(source: Path, target: Path) -> dict:
    if target.exists():
        fundamentals_update.verify_companyfacts_cache_manifest(target)
        return {
            "status": "EXISTING_ISOLATED_CACHE_VERIFIED",
            "source": str(source),
            "target": str(target),
        }
    temporary = target.with_name(target.name + ".clone_tmp")
    if temporary.exists():
        raise RuntimeError(f"stale isolated-cache clone exists: {temporary}")
    target.parent.mkdir(parents=True, exist_ok=True)

    def hardlink(source_file, target_file):
        os.link(source_file, target_file)
        return target_file

    with fundamentals_update.companyfacts_cache_lock(source):
        shutil.copytree(source, temporary, copy_function=hardlink)
        fundamentals_update.verify_companyfacts_cache_manifest(temporary)
    os.replace(temporary, target)
    return {
        "status": "HARDLINK_CLONED_AND_VERIFIED",
        "source": str(source),
        "target": str(target),
    }


def _refresh_fundamentals_isolated(
    *,
    as_of: pd.Timestamp,
    universe_path: Path,
    tickers: list[str],
    work: Path,
    workers: int,
) -> dict:
    before = _formal_financial_bindings()
    v42._initialize_fundamental_work(work)
    source_cache = Path(fundamentals_update.SEC_COMPANYFACTS_CACHE_DIR)
    isolated_cache = work / "companyfacts_cache"
    clone = _hardlink_clone_cache(source_cache, isolated_cache)
    replacements = {
        "NASDAQ_300M_STOCK_LIST_FILE": str(universe_path),
        "FUNDAMENTALS_REFRESH_STATE_FILE": str(work / "refresh_state.json"),
        "FUNDAMENTALS_COVERAGE_FILE": str(work / "coverage.json"),
        "QUARTERLY_FUNDAMENTALS_COVERAGE_FILE": str(
            work / "quarterly_coverage.json"
        ),
    }
    original = {name: getattr(fundamentals_update, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(fundamentals_update, name, value)
        audit = fundamentals_update.update_fundamentals(
            as_of=as_of.date(),
            workers=workers,
            refresh_after_days=0,
            output=work / "fundamentals.csv",
            quarterly_output=work / "quarterly.csv",
            force=True,
            tickers=tickers,
            cache_dir=isolated_cache,
        )
    finally:
        for name, value in original.items():
            setattr(fundamentals_update, name, value)
    after = _formal_financial_bindings()
    if after != before:
        raise RuntimeError("formal/shared financial inputs changed during v43 refresh")
    return {
        **audit,
        "isolated_cache": clone,
        "formal_financial_bindings_before": before,
        "formal_financial_bindings_after": after,
        "formal_financial_files_modified": False,
        "shared_companyfacts_cache_modified": False,
    }


def _trim_qqq(path: Path, as_of: pd.Timestamp) -> Path:
    refreshed = V42_REFRESH_CORE_PRICE(path)
    frame = pd.read_csv(refreshed, parse_dates=["date"]).sort_values("date")
    frame = frame.loc[frame["date"] <= as_of].drop_duplicates("date", keep="last")
    if frame.empty or frame["date"].max() != as_of:
        raise RuntimeError("QQQ public history is not ready through bundle as-of")
    temporary = Path(refreshed).with_suffix(Path(refreshed).suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, refreshed)
    return Path(refreshed)


def _is_nasdaq_session(stamp: pd.Timestamp) -> bool:
    calendar = nasdaq_calendar_for_year(stamp.year)
    sessions = pd.DatetimeIndex(
        calendar.sessions_in_range(stamp, stamp)
    ).tz_localize(None).normalize()
    return bool(len(sessions) and sessions[0] == stamp)


def _latest_event_date(events: list[dict], event_type: str, key: str) -> pd.Timestamp | None:
    dates = [
        pd.Timestamp(event["payload"][key])
        for event in events
        if event["event_type"] == event_type
    ]
    return max(dates) if dates else None


def stage_bundle(
    *,
    as_of: str | pd.Timestamp,
    purpose: str,
    bundles_dir: Path = BUNDLES_DIR,
    work_dir: Path = WORK_DIR,
    signals_dir: Path = SIGNALS_DIR,
    ledger_path: Path = LEDGER_PATH,
    workers: int = 16,
    fundamental_workers: int = 4,
) -> dict:
    stamp = pd.Timestamp(as_of).normalize()
    purpose = purpose.upper()
    if purpose not in {"SIGNAL", "MARK"}:
        raise ValueError("bundle purpose must be SIGNAL or MARK")
    if not _is_nasdaq_session(stamp):
        raise ValueError("v43 bundle as-of must be a Nasdaq trading session")
    events = read_ledger(ledger_path)
    if purpose == "SIGNAL":
        latest = _latest_event_date(events, "SIGNAL_FROZEN", "signal_date")
        if latest is not None and stamp <= latest:
            raise RuntimeError("v43 refuses a non-increasing signal bundle date")
        if not v42._is_month_end_signal(stamp):
            raise ValueError("v43 signal bundle requires the final monthly session")
    else:
        latest = _latest_event_date(events, "VALUATION_APPENDED", "as_of")
        if latest is not None and stamp <= latest:
            raise RuntimeError("v43 refuses an already-valued mark bundle date")

    suffix = f"{stamp:%Y-%m-%d}_{purpose.lower()}"
    final_bundle = Path(bundles_dir) / suffix
    if final_bundle.exists():
        manifest, manifest_sha = _validated_bundle(final_bundle, purpose)
        return {
            "status": "ALREADY_STAGED_AND_VERIFIED",
            "purpose": purpose,
            "as_of": manifest["as_of"],
            "bundle": str(final_bundle),
            "manifest_sha256": manifest_sha,
            "release_status": "BLOCKED",
        }

    formal_market_before = {
        "nasdaq_index": _sha256(NASDAQ_INDEX_FILE),
        "nasdaq_universe": _sha256(NASDAQ_300M_STOCK_LIST_FILE),
    }
    base_parent = Path(work_dir) / "bundle_builds"
    base_bundle = base_parent / suffix
    if base_bundle.exists():
        raise RuntimeError(f"stale v43 bundle build exists: {base_bundle}")
    original_fundamental_refresh = v42._refresh_fundamentals_isolated
    original_core_refresh = v42.refresh_core_price
    try:
        v42._refresh_fundamentals_isolated = _refresh_fundamentals_isolated
        v42.refresh_core_price = lambda path: _trim_qqq(Path(path), stamp)
        v42.stage_bundle(
            as_of=stamp,
            purpose=purpose,
            bundles_dir=base_parent,
            work_dir=work_dir,
            signals_dir=signals_dir,
            workers=workers,
            fundamental_workers=fundamental_workers,
        )
    finally:
        v42._refresh_fundamentals_isolated = original_fundamental_refresh
        v42.refresh_core_price = original_core_refresh

    formal_market_after = {
        "nasdaq_index": _sha256(NASDAQ_INDEX_FILE),
        "nasdaq_universe": _sha256(NASDAQ_300M_STOCK_LIST_FILE),
    }
    if formal_market_after != formal_market_before:
        raise RuntimeError("formal market inputs changed during v43 staging")
    qqq_provenance = Path(work_dir) / "market" / "qqq.provenance.json"
    if not qqq_provenance.is_file():
        raise RuntimeError("v43 staged QQQ provenance is missing")
    shutil.copy2(qqq_provenance, base_bundle / "qqq.provenance.json")
    manifest_path = base_bundle / "bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest["runner_version"] = MODEL_VERSION
    manifest["files"]["qqq.provenance.json"] = _sha256(
        base_bundle / "qqq.provenance.json"
    )
    manifest["runtime_isolation"] = {
        "formal_market_bindings_before": formal_market_before,
        "formal_market_bindings_after": formal_market_after,
        "formal_market_files_modified": False,
        "formal_financial_files_modified": False,
        "shared_companyfacts_cache_modified": False,
        "qqq_cutoff": stamp.strftime("%Y-%m-%d"),
    }
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary_manifest, manifest_path)
    final_bundle.parent.mkdir(parents=True, exist_ok=True)
    os.replace(base_bundle, final_bundle)
    return {
        "status": "FROZEN_ISOLATED_INPUT_BUNDLE",
        "purpose": purpose,
        "as_of": stamp.strftime("%Y-%m-%d"),
        "bundle": str(final_bundle),
        "manifest_sha256": _sha256(final_bundle / "bundle_manifest.json"),
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
    }


def _validated_bundle(
    bundle: str | Path, expected_purpose: str | None = None
) -> tuple[dict, str]:
    manifest, manifest_sha = V42_VALIDATED_BUNDLE(bundle, expected_purpose)
    if manifest.get("schema_version") != 2 or manifest.get("runner_version") != MODEL_VERSION:
        raise RuntimeError("bundle was not frozen by the v43 isolated runner")
    isolation = manifest.get("runtime_isolation") or {}
    if isolation.get("formal_market_bindings_before") != isolation.get(
        "formal_market_bindings_after"
    ):
        raise RuntimeError("v43 bundle reports formal market mutation")
    if any(
        isolation.get(name) is not False
        for name in (
            "formal_market_files_modified",
            "formal_financial_files_modified",
            "shared_companyfacts_cache_modified",
        )
    ):
        raise RuntimeError("v43 bundle isolation claim is invalid")
    as_of = pd.Timestamp(manifest["as_of"])
    qqq = pd.read_csv(Path(bundle) / "qqq.csv", parse_dates=["date"])
    if qqq["date"].max() != as_of or qqq["date"].gt(as_of).any():
        raise RuntimeError("v43 bundle contains QQQ rows after its as-of")
    return manifest, manifest_sha


def _build_signal_payload(
    *,
    signal_date: pd.Timestamp,
    bundle: Path,
    protocol: dict,
    protocol_sha: str,
    manifest_sha: str,
) -> dict:
    inputs = v42._load_signal_inputs(bundle, signal_date)
    payload = v42.build_signal_payload(
        signal_date=signal_date,
        inputs=inputs,
        model=protocol["model"],
        protocol_sha256=protocol_sha,
        bundle_manifest_sha256=manifest_sha,
    )
    payload["model_version"] = MODEL_VERSION
    payload["runtime_isolation_verified"] = True
    payload["signal_chronology_verified"] = True
    return payload


def freeze_signal(
    *,
    bundle: str | Path,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
    signals_dir: str | Path = SIGNALS_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    events = read_ledger(ledger_path)
    if not events or events[0]["protocol_sha256"] != protocol_sha:
        raise RuntimeError("v43 ledger does not bind the frozen protocol")
    manifest, manifest_sha = _validated_bundle(bundle, "SIGNAL")
    signal_date = pd.Timestamp(manifest["as_of"]).normalize()
    existing_event = next(
        (
            event
            for event in events
            if event["event_type"] == "SIGNAL_FROZEN"
            and event["payload"]["signal_date"] == f"{signal_date:%Y-%m-%d}"
        ),
        None,
    )
    output = Path(signals_dir) / f"signal_{signal_date:%Y-%m-%d}.json"
    if existing_event is not None:
        if not output.is_file() or _sha256(output) != existing_event["payload"][
            "signal_sha256"
        ]:
            raise RuntimeError("existing v43 signal event has no valid artifact")
        return {
            "status": "ALREADY_FROZEN_AND_VERIFIED",
            "signal_date": f"{signal_date:%Y-%m-%d}",
            "signal": str(output),
            "signal_sha256": _sha256(output),
            "release_status": "BLOCKED",
        }
    latest_signal = _latest_event_date(events, "SIGNAL_FROZEN", "signal_date")
    if latest_signal is not None and signal_date <= latest_signal:
        raise RuntimeError("v43 signal date is not strictly increasing")
    expected = _build_signal_payload(
        signal_date=signal_date,
        bundle=Path(bundle),
        protocol=protocol,
        protocol_sha=protocol_sha,
        manifest_sha=manifest_sha,
    )
    recovered = output.exists()
    if recovered:
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing != expected:
            raise RuntimeError("orphan v43 signal does not match deterministic rebuild")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(expected, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    signal_sha = _sha256(output)
    append_event(
        path=ledger_path,
        protocol_sha256=protocol_sha,
        event_type="SIGNAL_FROZEN",
        payload={
            "signal_date": expected["signal_date"],
            "signal_path": str(output),
            "signal_sha256": signal_sha,
            "bundle_manifest_sha256": manifest_sha,
            "targets": expected["targets"],
            "recovered_after_preledger_crash": recovered,
        },
    )
    return {
        "status": (
            "RECOVERED_AND_FROZEN_PROSPECTIVE_SIGNAL"
            if recovered
            else "FROZEN_PROSPECTIVE_SIGNAL"
        ),
        "signal_date": expected["signal_date"],
        "signal": str(output),
        "signal_sha256": signal_sha,
        "targets": expected["targets"],
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
    }


@contextmanager
def _v42_runtime():
    replacements = {
        "MODEL_VERSION": MODEL_VERSION,
        "_validated_protocol": _validated_protocol,
        "read_ledger": read_ledger,
        "append_event": append_event,
        "_validated_bundle": _validated_bundle,
    }
    original = {name: getattr(v42, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(v42, name, value)
        yield
    finally:
        for name, value in original.items():
            setattr(v42, name, value)


def append_mark(
    *,
    bundle: str | Path,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
    signals_dir: str | Path = SIGNALS_DIR,
) -> dict:
    manifest, _manifest_sha = _validated_bundle(bundle, "MARK")
    as_of = str(manifest["as_of"])
    existing = next(
        (
            event
            for event in read_ledger(ledger_path)
            if event["event_type"] == "VALUATION_APPENDED"
            and event["payload"]["as_of"] == as_of
        ),
        None,
    )
    if existing is not None:
        return {
            "status": "ALREADY_VALUED_AND_VERIFIED",
            "written": False,
            **existing["payload"],
        }
    with _v42_runtime():
        result = v42.append_mark(
            bundle=bundle,
            protocol_path=protocol_path,
            ledger_path=ledger_path,
            signals_dir=signals_dir,
        )
    result["runtime_isolation_verified"] = True
    return result


def status(
    *,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
) -> dict:
    with _v42_runtime():
        result = v42.status(
            protocol_path=protocol_path,
            ledger_path=ledger_path,
        )
    result["supersedes_v42_before_first_signal"] = True
    result["runtime_isolation"] = "VERIFIED_BY_FROZEN_V43_PROTOCOL"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("freeze-protocol")
    subparsers.add_parser("write-v42-supersession")
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
    elif args.command == "write-v42-supersession":
        result = write_v42_supersession()
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
