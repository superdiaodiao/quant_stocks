#!/usr/bin/env python3
"""Recover an auditable August model target after the v50 signal window was missed.

The recovery is intentionally split from the prospective v50 ledger.  It can
reconstruct what the already-frozen v50r1 model selects from data through
2026-08-31, but it cannot backdate that reconstruction or count it as an
original prospective signal.  The next official signal remains the next
completed month-end session.

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

import pandas as pd

from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50_corrected_v47 as v50
from src.research.corrected_stock_policy import VALIDATION_PATH


MODEL_VERSION = "v51r5-v50r1-august-late-recovery"
SOURCE_SIGNAL_DATE = pd.Timestamp("2026-08-31")
ORIGINAL_SIGNAL_DEADLINE = pd.Timestamp("2026-09-01T00:00:00Z")
EARLIEST_RECOVERY_SHADOW_EXECUTION_DATE = pd.Timestamp("2026-09-04")
NEXT_OFFICIAL_SIGNAL_DATE = pd.Timestamp("2026-09-30")
REPO_ROOT = Path(__file__).resolve().parents[1]
FAILED_OUTPUT_DIR = Path("output/research_only/v51/august_recovery_20260904")
FAILED_PROTOCOL_PATH = FAILED_OUTPUT_DIR / "frozen_recovery_protocol.json"
SUPERSEDED_R1_OUTPUT_DIR = Path(
    "output/research_only/v51/august_recovery_20260904_r1"
)
SUPERSEDED_R1_PROTOCOL_PATH = (
    SUPERSEDED_R1_OUTPUT_DIR / "frozen_recovery_protocol.json"
)
RECOVERED_R2_OUTPUT_DIR = Path(
    "output/research_only/v51/august_recovery_20260904_r2"
)
RECOVERED_R2_PROTOCOL_PATH = RECOVERED_R2_OUTPUT_DIR / "frozen_recovery_protocol.json"
RECOVERED_R2_WORK_DIR = RECOVERED_R2_OUTPUT_DIR / "staging_work"
RECOVERED_R2_FUNDAMENTAL_DIR = RECOVERED_R2_WORK_DIR / "fundamentals"
SUPERSEDED_R3_OUTPUT_DIR = Path(
    "output/research_only/v51/august_recovery_20260904_r3"
)
SUPERSEDED_R3_PROTOCOL_PATH = (
    SUPERSEDED_R3_OUTPUT_DIR / "frozen_recovery_protocol.json"
)
SUPERSEDED_R4_OUTPUT_DIR = Path(
    "output/research_only/v51/august_recovery_20260904_r4"
)
SUPERSEDED_R4_PROTOCOL_PATH = (
    SUPERSEDED_R4_OUTPUT_DIR / "frozen_recovery_protocol.json"
)
OUTPUT_DIR = Path("output/research_only/v51/august_recovery_20260904_r5")
PROTOCOL_PATH = OUTPUT_DIR / "frozen_recovery_protocol.json"
REPORT_PATH = OUTPUT_DIR / "august_2026_late_diagnostic.json"
BUNDLES_DIR = OUTPUT_DIR / "diagnostic_bundles"
WORK_DIR = OUTPUT_DIR / "staging_work"
UNUSED_SIGNALS_DIR = OUTPUT_DIR / "unused_signals"
UNIVERSE_SNAPSHOT = Path(
    "stocks_list_dir/nasdaq/snapshots/nasdaq_listed_2026-07-01.csv"
)
SYMBOL_REPAIR_EVIDENCE = Path(
    "stocks_list_dir/nasdaq/snapshots/nasdaq_listed_2026-02-21.csv"
)
RECOVERED_FUNDAMENTAL_FILES = (
    "fundamentals.csv",
    "quarterly.csv",
    "coverage.json",
    "quarterly_coverage.json",
    "refresh_state.json",
)


def _resolve_path(path: str | Path) -> Path:
    item = Path(path)
    return item if item.is_absolute() else REPO_ROOT / item


def _sha256(path: str | Path) -> str:
    return v50._sha256(path)


def _portable_path(path: str | Path) -> str:
    return v50._portable_path(path)


def _file_binding(path: str | Path) -> dict:
    return {"path": _portable_path(path), "sha256": _sha256(path)}


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_exclusive_json(path: str | Path, payload: dict) -> None:
    item = _resolve_path(path)
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def recovery_specification() -> dict:
    return {
        "source_signal_date": SOURCE_SIGNAL_DATE.strftime("%Y-%m-%d"),
        "original_signal_deadline": ORIGINAL_SIGNAL_DEADLINE.isoformat(),
        "classification": "LATE_RECONSTRUCTION_NOT_ORIGINAL_PROSPECTIVE_SIGNAL",
        "selection_model": v50.MODEL_VERSION,
        "model_parameters_changed": False,
        "universe_policy": "latest source-locked snapshot on or before source date",
        "universe_snapshot": _portable_path(UNIVERSE_SNAPSHOT),
        "universe_symbol_repairs": {
            "Nano Labs Ltd - Class A Ordinary Shares": "NA",
        },
        "price_and_fundamental_cutoff": SOURCE_SIGNAL_DATE.strftime("%Y-%m-%d"),
        "missed_sessions_counted_as_strategy_return": False,
        "eligible_for_original_august_prospective_score": False,
        "eligible_for_broker_execution": False,
        "earliest_recovery_shadow_execution_date": (
            EARLIEST_RECOVERY_SHADOW_EXECUTION_DATE.strftime("%Y-%m-%d")
        ),
        "next_official_signal_date": NEXT_OFFICIAL_SIGNAL_DATE.strftime(
            "%Y-%m-%d"
        ),
    }


def _input_bindings() -> dict:
    bindings = {
        "runner": _file_binding(__file__),
        "failed_v51_protocol": _file_binding(FAILED_PROTOCOL_PATH),
        "superseded_v51r1_protocol": _file_binding(
            SUPERSEDED_R1_PROTOCOL_PATH
        ),
        "recovered_v51r2_protocol": _file_binding(RECOVERED_R2_PROTOCOL_PATH),
        "superseded_v51r3_protocol": _file_binding(
            SUPERSEDED_R3_PROTOCOL_PATH
        ),
        "superseded_v51r4_protocol": _file_binding(
            SUPERSEDED_R4_PROTOCOL_PATH
        ),
        "v50_runner": _file_binding(v50.__file__),
        "v50_protocol": _file_binding(v50.PROTOCOL_PATH),
        "v50_ledger": _file_binding(v50.LEDGER_PATH),
        "v43_isolated_staging": _file_binding(v43.__file__),
        "source_locked_universe": _file_binding(UNIVERSE_SNAPSHOT),
        "symbol_repair_evidence": _file_binding(SYMBOL_REPAIR_EVIDENCE),
        "corporate_action_validation": _file_binding(VALIDATION_PATH),
    }
    for filename in RECOVERED_FUNDAMENTAL_FILES:
        bindings[f"recovered_v51r2_{filename}"] = _file_binding(
            RECOVERED_R2_FUNDAMENTAL_DIR / filename
        )
    return bindings


def _recovered_unmapped_tickers() -> list[str]:
    audit = json.loads(
        _resolve_path(RECOVERED_R2_FUNDAMENTAL_DIR / "coverage.json").read_text(
            encoding="utf-8"
        )
    )
    if audit.get("as_of") != SOURCE_SIGNAL_DATE.strftime("%Y-%m-%d"):
        raise RuntimeError("recovered v51r2 fundamentals have the wrong cutoff")
    return sorted(set(audit.get("unmapped_universe_tickers", [])))


def _recovered_future_date_summary() -> dict:
    summary = {}
    for filename in ("fundamentals.csv", "quarterly.csv"):
        frame = pd.read_csv(
            _resolve_path(RECOVERED_R2_FUNDAMENTAL_DIR / filename),
            usecols=["ticker", "available_date"],
        )
        available = pd.to_datetime(frame["available_date"], errors="raise")
        future = frame.loc[available > SOURCE_SIGNAL_DATE]
        summary[filename] = {
            "source_rows": int(len(frame)),
            "future_rows_to_remove": int(len(future)),
            "future_ticker_count": int(future["ticker"].nunique()),
            "maximum_source_available_date": available.max().strftime(
                "%Y-%m-%d"
            ),
        }
    return summary


def freeze_recovery_protocol(
    path: str | Path = PROTOCOL_PATH,
    *,
    observed_at: datetime | None = None,
) -> dict:
    item = _resolve_path(path)
    if item.exists():
        raise RuntimeError("v51 recovery protocol will not be overwritten")
    observed = observed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ValueError("recovery freeze timestamp must be timezone-aware")
    if pd.Timestamp(observed) <= ORIGINAL_SIGNAL_DEADLINE:
        raise RuntimeError("v51 recovery is only valid after the original window")
    current = v50.status()
    if current["frozen_signal_count"] != 0:
        raise RuntimeError("v51 recovery requires the v50 signal count to remain zero")
    if current["bound_execution_count"] != 0 or current["valuation_count"] != 0:
        raise RuntimeError("v51 recovery refuses a non-empty v50 observation history")
    if not v43._is_nasdaq_session(SOURCE_SIGNAL_DATE):
        raise RuntimeError("v51 source date is not a Nasdaq session")
    if not v43.v42._is_month_end_signal(SOURCE_SIGNAL_DATE):
        raise RuntimeError("v51 source date is not the final monthly session")
    if not v43._is_nasdaq_session(EARLIEST_RECOVERY_SHADOW_EXECUTION_DATE):
        raise RuntimeError("v51 recovery execution date is not a Nasdaq session")
    if not v43.v42._is_month_end_signal(NEXT_OFFICIAL_SIGNAL_DATE):
        raise RuntimeError("v51 next official signal is not a month-end session")

    protocol = {
        "schema_version": 1,
        "research_only": True,
        "model_version": MODEL_VERSION,
        "status": "FROZEN_LATE_DIAGNOSTIC_ONLY",
        "frozen_at": observed.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "code_commit": _git_head(),
        "superseded_attempts": [
            {
                "protocol": _file_binding(FAILED_PROTOCOL_PATH),
                "target_was_generated": False,
                "reason": (
                    "the first recovery attempt refreshed fundamentals against "
                    "the September current universe before replacing it with "
                    "the source-locked pre-signal snapshot"
                ),
            },
            {
                "protocol": _file_binding(SUPERSEDED_R1_PROTOCOL_PATH),
                "target_was_generated": False,
                "reason": (
                    "the source-locked universe still contained SEC-unmapped "
                    "bank tickers; the build was not started after that condition "
                    "was detected"
                ),
            },
            {
                "protocol": _file_binding(RECOVERED_R2_PROTOCOL_PATH),
                "target_was_generated": False,
                "reason": (
                    "the complete source-locked SEC refresh found more "
                    "unmapped tickers than the over-narrow five-ticker guard; "
                    "the refreshed inputs were retained before any target was "
                    "generated"
                ),
            },
            {
                "protocol": _file_binding(SUPERSEDED_R3_PROTOCOL_PATH),
                "target_was_generated": False,
                "reason": (
                    "the recovered parsed files contained records first "
                    "available after 2026-08-31; the future-date gate stopped "
                    "the build before any target was generated"
                ),
            },
            {
                "protocol": _file_binding(SUPERSEDED_R4_PROTOCOL_PATH),
                "target_was_generated": False,
                "reason": (
                    "the source snapshot serialized ticker NA as an empty "
                    "symbol; the missing-price gate stopped the build before "
                    "any target was generated"
                ),
            },
        ],
        "sec_unmapped_policy": {
            "expected_tickers": _recovered_unmapped_tickers(),
            "keep_in_source_locked_universe": True,
            "invent_or_guess_cik": False,
            "refresh_mapped_tickers_only": True,
            "selection_missing_value_policy_changed": False,
        },
        "as_of_filter_policy": {
            "field": "available_date",
            "maximum_allowed": SOURCE_SIGNAL_DATE.strftime("%Y-%m-%d"),
            "operation": "deterministically remove later rows before staging",
            "source_summary": _recovered_future_date_summary(),
            "selection_parameters_changed": False,
        },
        "recovery_specification": recovery_specification(),
        "input_bindings": _input_bindings(),
        "v50_ledger_must_remain_unchanged": True,
        "target_not_inspected_before_this_freeze": True,
        "parameters_changed_after_observation": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    _write_exclusive_json(item, protocol)
    return {**protocol, "protocol": _file_binding(item)}


def _validated_recovery_protocol(
    path: str | Path = PROTOCOL_PATH,
) -> tuple[dict, str]:
    item = _resolve_path(path)
    protocol = json.loads(item.read_text(encoding="utf-8"))
    if protocol.get("model_version") != MODEL_VERSION:
        raise RuntimeError("unexpected v51 recovery model version")
    if protocol.get("recovery_specification") != recovery_specification():
        raise RuntimeError("v51 recovery specification changed")
    if protocol.get("release_status") != "BLOCKED" or protocol.get(
        "promotion_eligible"
    ):
        raise RuntimeError("v51 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v51 frozen input changed: {name}")
    return protocol, _sha256(item)


@contextmanager
def _late_diagnostic_runtime():
    """Use v50's runner identity without enabling v50's timeliness claim."""
    original_model_version = v43.MODEL_VERSION
    original_refresh_universe = v43.v42.refresh_universe
    original_refresh_fundamentals = v43._refresh_fundamentals_isolated
    try:
        v43.MODEL_VERSION = v50.MODEL_VERSION
        v43.v42.refresh_universe = _source_locked_universe_refresh
        v43._refresh_fundamentals_isolated = (
            _source_locked_fundamentals_refresh
        )
        yield
    finally:
        v43._refresh_fundamentals_isolated = original_refresh_fundamentals
        v43.v42.refresh_universe = original_refresh_universe
        v43.MODEL_VERSION = original_model_version


def _source_locked_universe_refresh(
    as_of,
    *,
    min_market_cap,
    target_path,
    common_equities_only,
) -> dict:
    """Inject the pre-signal universe before any prices or fundamentals refresh."""
    stamp = pd.Timestamp(as_of).normalize()
    if stamp != SOURCE_SIGNAL_DATE:
        raise RuntimeError("v51r5 universe refresh only supports the August cutoff")
    source = _resolve_path(UNIVERSE_SNAPSHOT)
    frame, repairs = _normalized_source_locked_universe()
    required = {"Symbol", "ETF", "Test Issue", "Observed At"}
    if not required.issubset(frame.columns):
        raise RuntimeError("source-locked universe snapshot schema changed")
    observed_dates = pd.to_datetime(frame["Observed At"], errors="raise")
    if observed_dates.max().normalize() > SOURCE_SIGNAL_DATE:
        raise RuntimeError("source-locked universe snapshot is future-dated")
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False)
    return {
        "status": "SOURCE_LOCKED_PRE_SIGNAL_UNIVERSE",
        "as_of": stamp.strftime("%Y-%m-%d"),
        "source": _file_binding(source),
        "output": _portable_path(target),
        "rows": int(len(frame)),
        "symbol_repairs": repairs,
        "minimum_market_cap_ignored": min_market_cap == 0,
        "common_equities_filtered_by_stager": bool(common_equities_only),
    }


def _normalized_source_locked_universe() -> tuple[pd.DataFrame, list[dict]]:
    """Repair the literal ticker NA that older pandas ingestion blanked."""
    source = _resolve_path(UNIVERSE_SNAPSHOT)
    frame = pd.read_csv(source, keep_default_na=False)
    blank = frame["Symbol"].astype(str).str.strip().eq("")
    if not blank.any():
        return frame, []
    blank_rows = frame.loc[blank]
    expected_name = "Nano Labs Ltd - Class A Ordinary Shares"
    if len(blank_rows) != 1 or blank_rows.iloc[0]["Name"] != expected_name:
        raise RuntimeError("unexpected blank symbol in source-locked universe")
    evidence = pd.read_csv(
        _resolve_path(SYMBOL_REPAIR_EVIDENCE), keep_default_na=False
    )
    supported = evidence.loc[
        evidence["Symbol"].eq("NA")
        & evidence["Name"].astype(str).str.contains("Nano Labs", regex=False)
    ]
    if len(supported) != 1:
        raise RuntimeError("source-locked evidence does not support Nano Labs=NA")
    frame = frame.copy()
    frame.loc[blank, "Symbol"] = "NA"
    return frame, [
        {
            "name": expected_name,
            "from": "",
            "to": "NA",
            "reason": "literal NA ticker was serialized as a missing value",
            "evidence": _file_binding(SYMBOL_REPAIR_EVIDENCE),
        }
    ]


def _source_locked_fundamentals_refresh(
    *,
    as_of: pd.Timestamp,
    universe_path: Path,
    tickers: list[str],
    work: Path,
    workers: int,
) -> dict:
    """Reuse the complete r2 refresh while retaining every unmapped ticker."""
    del workers
    stamp = pd.Timestamp(as_of).normalize()
    if stamp != SOURCE_SIGNAL_DATE:
        raise RuntimeError("v51r5 fundamentals only support the August cutoff")
    received = pd.read_csv(universe_path, keep_default_na=False)
    expected, _repairs = _normalized_source_locked_universe()
    if not received.equals(expected):
        raise RuntimeError("v51r5 fundamentals received the wrong universe")
    audit_path = Path(work) / "coverage.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("as_of") != SOURCE_SIGNAL_DATE.strftime("%Y-%m-%d"):
        raise RuntimeError("v51r5 recovered fundamentals have the wrong cutoff")
    if not audit.get("parsed_outputs_written"):
        raise RuntimeError("v51r5 recovered fundamentals were not written")
    if int(audit.get("deferred_by_limit_ticker_count", 0)) != 0:
        raise RuntimeError("v51r5 recovered fundamentals contain deferred work")
    quarterly = pd.read_csv(Path(work) / "quarterly.csv")
    available = pd.to_datetime(quarterly["available_date"], errors="coerce")
    if not available.dropna().le(SOURCE_SIGNAL_DATE).all():
        raise RuntimeError("v51r5 recovered fundamentals contain future data")
    unmapped = sorted(set(audit.get("unmapped_universe_tickers", [])))
    if unmapped != _recovered_unmapped_tickers():
        raise RuntimeError("v51r5 SEC-unmapped audit changed")
    if not set(unmapped).issubset(set(tickers)):
        raise RuntimeError("v51r5 unmapped tickers left the source universe")
    audit["late_recovery_unmapped_policy"] = {
        "classification": "SEC_CIK_UNAVAILABLE_NOT_DROPPED_OR_GUESSED",
        "explicit_stage_ticker_count": len(tickers),
        "unmapped_tickers": unmapped,
        "kept_in_source_locked_universe": True,
        "invented_cik_count": 0,
        "mapped_tickers_refreshed": True,
        "selection_missing_value_policy_changed": False,
        "reused_completed_v51r2_refresh": True,
    }
    return audit


def _materialize_recovered_work(target: Path) -> dict:
    """Copy only completed parsed/market inputs, excluding the 2 GB raw cache."""
    target = _resolve_path(target)
    source = _resolve_path(RECOVERED_R2_WORK_DIR)
    if target.exists():
        raise RuntimeError(f"v51r5 recovered work already exists: {target}")
    temporary = target.with_name(target.name + ".copy_tmp")
    if temporary.exists():
        raise RuntimeError(f"stale v51r5 recovered-work copy exists: {temporary}")
    (temporary / "fundamentals").mkdir(parents=True)
    for filename in RECOVERED_FUNDAMENTAL_FILES:
        shutil.copy2(
            source / "fundamentals" / filename,
            temporary / "fundamentals" / filename,
        )
    filters = {}
    parsed_frames = {}
    for filename in ("fundamentals.csv", "quarterly.csv"):
        path = temporary / "fundamentals" / filename
        frame = pd.read_csv(path, low_memory=False)
        available = pd.to_datetime(frame["available_date"], errors="raise")
        keep = available <= SOURCE_SIGNAL_DATE
        filtered = frame.loc[keep].copy()
        output = path.with_suffix(path.suffix + ".tmp")
        filtered.to_csv(output, index=False)
        os.replace(output, path)
        filters[filename] = {
            "source_rows": int(len(frame)),
            "retained_rows": int(len(filtered)),
            "removed_future_rows": int((~keep).sum()),
            "maximum_retained_available_date": pd.to_datetime(
                filtered["available_date"], errors="raise"
            ).max().strftime("%Y-%m-%d"),
        }
        parsed_frames[filename] = filtered.assign(
            available_date=pd.to_datetime(
                filtered["available_date"], errors="raise"
            )
        )
    normalized_universe, _repairs = _normalized_source_locked_universe()
    current = v43.v42.investable_common_equities(normalized_universe)
    universe = current["Symbol"].dropna().astype(str).str.upper().tolist()
    coverage_path = temporary / "fundamentals" / "coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage.update(
        v43.fundamentals_update.audit_fundamentals_coverage(
            parsed_frames["fundamentals.csv"],
            universe,
            SOURCE_SIGNAL_DATE.date(),
        )
    )
    coverage["late_recovery_as_of_filter"] = filters
    coverage_path.write_text(
        json.dumps(coverage, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    quarterly_coverage_path = (
        temporary / "fundamentals" / "quarterly_coverage.json"
    )
    quarterly_coverage = json.loads(
        quarterly_coverage_path.read_text(encoding="utf-8")
    )
    quarterly_coverage.update(
        v43.fundamentals_update.audit_quarterly_coverage(
            parsed_frames["quarterly.csv"],
            universe,
            SOURCE_SIGNAL_DATE.date(),
        )
    )
    quarterly_coverage["late_recovery_as_of_filter"] = filters
    quarterly_coverage_path.write_text(
        json.dumps(quarterly_coverage, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.copytree(source / "market", temporary / "market")
    os.replace(temporary, target)
    return {
        "status": "COPIED_COMPLETED_R2_INPUTS",
        "source": _portable_path(source),
        "target": _portable_path(target),
        "raw_companyfacts_cache_copied": False,
        "available_date_filters": filters,
        "fundamental_bindings": {
            filename: _file_binding(target / "fundamentals" / filename)
            for filename in RECOVERED_FUNDAMENTAL_FILES
        },
    }


def _stage_late_bundle(
    *,
    bundles_dir: Path = BUNDLES_DIR,
    work_dir: Path = WORK_DIR,
    workers: int = 16,
    fundamental_workers: int = 4,
) -> Path:
    with _late_diagnostic_runtime():
        result = v43.stage_bundle(
            as_of=SOURCE_SIGNAL_DATE,
            purpose="SIGNAL",
            bundles_dir=_resolve_path(bundles_dir),
            work_dir=_resolve_path(work_dir),
            signals_dir=_resolve_path(UNUSED_SIGNALS_DIR),
            ledger_path=_resolve_path(v50.LEDGER_PATH),
            workers=workers,
            fundamental_workers=fundamental_workers,
        )
    return Path(result["bundle"])


def _replace_bundle_universe(bundle: Path) -> dict:
    snapshot, repairs = _normalized_source_locked_universe()
    required = {"Symbol", "ETF", "Test Issue", "Observed At"}
    if not required.issubset(snapshot.columns):
        raise RuntimeError("source-locked universe snapshot schema changed")
    observed_dates = pd.to_datetime(snapshot["Observed At"], errors="raise")
    if observed_dates.max().normalize() > SOURCE_SIGNAL_DATE:
        raise RuntimeError("source-locked universe snapshot is future-dated")
    universe_path = bundle / "current_universe.csv"
    snapshot.to_csv(universe_path, index=False)

    manifest_path = bundle / "bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["current_universe.csv"] = _sha256(universe_path)
    manifest["late_diagnostic_recovery"] = {
        "classification": "SOURCE_LOCKED_UNIVERSE_REPLACED_AFTER_LATE_STAGING",
        "snapshot": _file_binding(UNIVERSE_SNAPSHOT),
        "symbol_repairs": repairs,
        "future_current_universe_used_for_selection": False,
        "eligible_for_original_prospective_score": False,
    }
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest_path)
    return manifest


def _validated_late_bundle(bundle: Path) -> tuple[dict, str]:
    with _late_diagnostic_runtime():
        manifest, manifest_sha = v43._validated_bundle(bundle, "SIGNAL")
    recovery = manifest.get("late_diagnostic_recovery", {})
    if recovery.get("future_current_universe_used_for_selection") is not False:
        raise RuntimeError("v51 late bundle lacks its source-locked universe claim")
    if pd.Timestamp(manifest["as_of"]).normalize() != SOURCE_SIGNAL_DATE:
        raise RuntimeError("v51 late bundle has the wrong source cutoff")
    return manifest, manifest_sha


def build_late_diagnostic(
    *,
    protocol_path: str | Path = PROTOCOL_PATH,
    report_path: str | Path = REPORT_PATH,
    bundles_dir: Path = BUNDLES_DIR,
    work_dir: Path = WORK_DIR,
    workers: int = 16,
    fundamental_workers: int = 4,
    observed_at: datetime | None = None,
) -> dict:
    protocol, protocol_sha = _validated_recovery_protocol(protocol_path)
    output = _resolve_path(report_path)
    if output.exists():
        report = json.loads(output.read_text(encoding="utf-8"))
        return {**report, "report": _file_binding(output), "written": False}
    observed = observed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ValueError("diagnostic timestamp must be timezone-aware")
    ledger_before = _file_binding(v50.LEDGER_PATH)
    recovered_work = _materialize_recovered_work(work_dir)
    bundle = _stage_late_bundle(
        bundles_dir=bundles_dir,
        work_dir=work_dir,
        workers=workers,
        fundamental_workers=fundamental_workers,
    )
    _replace_bundle_universe(bundle)
    manifest, manifest_sha = _validated_late_bundle(bundle)
    frozen_v50, frozen_v50_sha = v50._validated_protocol()
    payload = v50._build_signal_payload(
        signal_date=SOURCE_SIGNAL_DATE,
        bundle=bundle,
        protocol=frozen_v50,
        protocol_sha=frozen_v50_sha,
        manifest_sha=manifest_sha,
    )
    ledger_after = _file_binding(v50.LEDGER_PATH)
    if ledger_after != ledger_before:
        raise RuntimeError("v51 recovery modified the v50 prospective ledger")

    payload["signal_staging_timeliness_verified"] = False
    payload["prospective_signal"] = False
    payload["execution_date"] = None
    payload["execution_policy"] = "original next-session execution was missed"
    report = {
        "schema_version": 1,
        "research_only": True,
        "model_version": MODEL_VERSION,
        "status": "COMPLETED_LATE_AUGUST_DIAGNOSTIC",
        "classification": "LATE_RECONSTRUCTION_NOT_ORIGINAL_PROSPECTIVE_SIGNAL",
        "generated_at": observed.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "source_signal_date": SOURCE_SIGNAL_DATE.strftime("%Y-%m-%d"),
        "original_signal_deadline": ORIGINAL_SIGNAL_DEADLINE.isoformat(),
        "protocol_sha256": protocol_sha,
        "bundle": {
            "path": _portable_path(bundle),
            "manifest_sha256": manifest_sha,
            "created_at": manifest["created_at"],
        },
        "source_locked_universe": _file_binding(UNIVERSE_SNAPSHOT),
        "recovered_work": recovered_work,
        "v50_ledger_before": ledger_before,
        "v50_ledger_after": ledger_after,
        "v50_ledger_unchanged": True,
        "missed_sessions_excluded_from_performance": [
            "2026-09-01",
            "2026-09-02",
            "2026-09-03",
        ],
        "earliest_recovery_shadow_execution_date": (
            EARLIEST_RECOVERY_SHADOW_EXECUTION_DATE.strftime("%Y-%m-%d")
        ),
        "next_official_signal_date": NEXT_OFFICIAL_SIGNAL_DATE.strftime(
            "%Y-%m-%d"
        ),
        "model_output": payload,
        "eligible_for_original_august_prospective_score": False,
        "broker_action_authorized": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    _write_exclusive_json(output, report)
    return {**report, "report": _file_binding(output), "written": True}


def status(
    *,
    protocol_path: str | Path = PROTOCOL_PATH,
    report_path: str | Path = REPORT_PATH,
) -> dict:
    protocol_item = _resolve_path(protocol_path)
    report_item = _resolve_path(report_path)
    result = {
        "model_version": MODEL_VERSION,
        "recovery_protocol_frozen": protocol_item.is_file(),
        "late_august_diagnostic_available": report_item.is_file(),
        "original_august_prospective_signal_exists": False,
        "next_official_signal_date": NEXT_OFFICIAL_SIGNAL_DATE.strftime(
            "%Y-%m-%d"
        ),
        "broker_action_authorized": False,
        "release_status": "BLOCKED",
    }
    if protocol_item.is_file():
        _protocol, protocol_sha = _validated_recovery_protocol(protocol_item)
        result["protocol_sha256"] = protocol_sha
    if report_item.is_file():
        report = json.loads(report_item.read_text(encoding="utf-8"))
        result["diagnostic_report"] = _file_binding(report_item)
        result["targets"] = report["model_output"]["targets"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("freeze-protocol")
    build_parser = subparsers.add_parser("build-diagnostic")
    build_parser.add_argument("--workers", type=int, default=16)
    build_parser.add_argument("--fundamental-workers", type=int, default=4)
    subparsers.add_parser("status")
    args = parser.parse_args()
    if args.command == "freeze-protocol":
        result = freeze_recovery_protocol()
    elif args.command == "build-diagnostic":
        result = build_late_diagnostic(
            workers=args.workers,
            fundamental_workers=args.fundamental_workers,
        )
    else:
        result = status()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
