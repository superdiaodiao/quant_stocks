#!/usr/bin/env python3
"""Run an append-only prospective observation of the frozen v28 stock model.

This runner deliberately separates model development from prospective evidence:

* 2020-2025 selected the v26 stock selector and v28 trailing-stop threshold.
* January-July 2026 is researcher-exposed diagnostic evidence only.
* The first prospective signal is the completed 2026-08-31 close.
* A signal is frozen before its next-close paper execution and can never be
  overwritten.  Later marks are appended to a SHA-256 chained ledger.

The runner creates research-only paper observations.  It does not connect to a
broker, create orders, allocate capital, or hold QQQ/another index ETF.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable

import pandas as pd

from scripts import research_v24_stock_momentum_development as v24
from scripts import research_v26_large_liquid_stock_momentum as v26
from scripts import research_v28_stock_trailing_stop_development as v28
from scripts.research_v5_trend_core_satellite import refresh_core_price
from scripts.research_v6_market_refresh import (
    reconcile_research_index,
    seed_cache,
)
from src.conf import (
    FUNDAMENTALS_COVERAGE_FILE,
    FUNDAMENTALS_REFRESH_STATE_FILE,
    POINT_IN_TIME_FUNDAMENTALS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    QUARTERLY_FUNDAMENTALS_COVERAGE_FILE,
)
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.io import fundamentals_update
from src.io.nasdaq_update import refresh_universe, update_all
from src.io.security_universe import investable_common_equities
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel
from src.research.shadow_evaluation import nasdaq_calendar_for_year
from src.strategy.common import market_regime_is_on


MODEL_VERSION = "v28-stock-only-individual-trailing-stop-25pct"
FIRST_PROSPECTIVE_SIGNAL_DATE = pd.Timestamp("2026-08-31")
TRAINING_YEARS = tuple(range(2020, 2026))
REUSED_DIAGNOSTIC_START = "2026-01-01"
REUSED_DIAGNOSTIC_END = "2026-07-31"
COSTS = (10, 30, 50)
FORBIDDEN_ETFS = v28.FORBIDDEN_ETFS

OUTPUT_DIR = Path("output/research_only/v42/v28_prospective_20260830")
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
LEDGER_PATH = OUTPUT_DIR / "prospective_ledger.jsonl"
SIGNALS_DIR = OUTPUT_DIR / "signals"
BUNDLES_DIR = OUTPUT_DIR / "bundles"
WORK_DIR = OUTPUT_DIR / "staging_work"

V26_MANIFEST = v26.DEVELOPMENT_OUTPUT_DIR / "manifest.json"
V28_MANIFEST = v28.DEVELOPMENT_OUTPUT_DIR / "manifest.json"

NULL_HASH = "0" * 64
EVENT_TYPES = {
    "PROTOCOL_FROZEN",
    "SIGNAL_FROZEN",
    "EXECUTION_DATE_BOUND",
    "VALUATION_APPENDED",
}


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_binding(path: str | Path) -> dict:
    item = Path(path)
    return {"path": str(item), "sha256": _sha256(item)}


def _event_hash(event_without_hash: dict) -> str:
    return hashlib.sha256(_canonical_bytes(event_without_hash)).hexdigest()


def _read_ledger_handle(handle) -> list[dict]:
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


def _validate_event_chain(events: list[dict]) -> None:
    previous = NULL_HASH
    protocol_sha = None
    signals: dict[str, str] = {}
    executions: dict[str, str] = {}
    latest_mark = None
    for expected_index, event in enumerate(events):
        if event.get("event_index") != expected_index:
            raise RuntimeError("prospective ledger event indexes are not contiguous")
        if event.get("event_type") not in EVENT_TYPES:
            raise RuntimeError("prospective ledger contains an unknown event type")
        if event.get("prev_hash") != previous:
            raise RuntimeError("prospective ledger previous-hash link is broken")
        claimed = event.get("event_hash")
        unsigned = {key: value for key, value in event.items() if key != "event_hash"}
        actual = _event_hash(unsigned)
        if claimed != actual:
            raise RuntimeError("prospective ledger event hash is invalid")
        if expected_index == 0:
            if event["event_type"] != "PROTOCOL_FROZEN":
                raise RuntimeError("prospective ledger must start with protocol freeze")
            protocol_sha = event.get("protocol_sha256")
        elif event.get("protocol_sha256") != protocol_sha:
            raise RuntimeError("prospective ledger mixes protocol bindings")

        payload = event.get("payload") or {}
        event_type = event["event_type"]
        if event_type == "SIGNAL_FROZEN":
            signal_date = str(payload.get("signal_date"))
            if signal_date in signals:
                raise RuntimeError("prospective ledger contains a duplicate signal")
            if pd.Timestamp(signal_date) < FIRST_PROSPECTIVE_SIGNAL_DATE:
                raise RuntimeError("prospective ledger contains a pre-start signal")
            signals[signal_date] = str(payload.get("signal_sha256"))
        elif event_type == "EXECUTION_DATE_BOUND":
            signal_date = str(payload.get("signal_date"))
            execution_date = str(payload.get("execution_date"))
            if signal_date not in signals or signal_date in executions:
                raise RuntimeError("execution event does not bind one frozen signal")
            if pd.Timestamp(execution_date) <= pd.Timestamp(signal_date):
                raise RuntimeError("execution date must be after its signal date")
            executions[signal_date] = execution_date
        elif event_type == "VALUATION_APPENDED":
            as_of = pd.Timestamp(payload.get("as_of"))
            if not executions:
                raise RuntimeError("valuation cannot precede the first execution")
            if latest_mark is not None and as_of <= latest_mark:
                raise RuntimeError("prospective valuation dates are not append-only")
            latest_mark = as_of
        previous = claimed


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
    """Append one fsync'd event while holding an exclusive process lock."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported prospective event type: {event_type}")
    item = Path(path)
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        events = _read_ledger_handle(handle)
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
            "prev_hash": events[-1]["event_hash"] if events else NULL_HASH,
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


def _selected_model() -> dict:
    v26_manifest = json.loads(V26_MANIFEST.read_text(encoding="utf-8"))
    v28_manifest = json.loads(V28_MANIFEST.read_text(encoding="utf-8"))
    if v26_manifest.get("development_status") != "PASS":
        raise RuntimeError("v26 selected model is no longer PASS on training")
    if v26_manifest.get("selected_candidate") != v27_candidate_name():
        raise RuntimeError("v26 selected stock specification changed")
    if v28_manifest.get("development_status") != "PASS":
        raise RuntimeError("v28 trailing-stop selection is no longer PASS")
    expected_stop = {
        "key": "individual_trailing_stop_25pct",
        "reentry_policy": "next_monthly_rebalance_only",
        "stop_signal_frequency": "daily",
        "trailing_stop_fraction": 0.25,
    }
    if v28_manifest.get("selected_specification") != expected_stop:
        raise RuntimeError("v28 selected trailing-stop specification changed")
    if v28_manifest.get("contains_index_etf_holdings"):
        raise RuntimeError("v28 manifest unexpectedly contains ETF holdings")
    if v28_manifest.get("release_status") != "BLOCKED":
        raise RuntimeError("v28 release boundary changed")
    return {
        "selector_candidate": v26_manifest["selected_candidate"],
        "selector_specification": v26_manifest["selected_specification"],
        "risk_specification": expected_stop,
    }


def v27_candidate_name() -> str:
    return "mom63_skip0_liquid25_top5_profitable_monthly"


def freeze_protocol(
    path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
) -> dict:
    """Write the immutable prospective protocol and its genesis ledger event."""
    item = Path(path)
    ledger = Path(ledger_path)
    if item.exists() or ledger.exists():
        raise RuntimeError("v42 protocol/ledger will not be overwritten")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    model = _selected_model()
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "model_version": MODEL_VERSION,
        "status": "FROZEN_WAITING_FOR_FIRST_SIGNAL",
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_commit": _git_head(),
        "model": model,
        "evidence_partition": {
            "2019": {
                "role": "EXCLUDED_PARTIAL_PIT_COVERAGE",
                "signals_available": 4,
                "signals_expected": 12,
                "counts_as_official_comparison": False,
            },
            "2020_2025": {
                "role": "TRAINING_AND_MODEL_SELECTION_ONLY",
                "years": list(TRAINING_YEARS),
                "counts_as_official_comparison": False,
                "official_year_wins": 0,
            },
            "2026_01_07": {
                "role": "RESEARCHER_EXPOSED_REUSED_DIAGNOSTIC",
                "start": REUSED_DIAGNOSTIC_START,
                "end": REUSED_DIAGNOSTIC_END,
                "counts_as_official_comparison": False,
                "official_year_wins": 0,
            },
            "prospective": {
                "first_signal_date": FIRST_PROSPECTIVE_SIGNAL_DATE.strftime(
                    "%Y-%m-%d"
                ),
                "performance_start": (
                    "first common trading-session close after the frozen signal"
                ),
                "counts_as_official_comparison": True,
            },
        },
        "signal_policy": {
            "frequency": "completed calendar-month final Nasdaq session",
            "execution": "next common trading-session close",
            "holdings": "individual common equities or cash only",
            "residual_asset": "CASH",
            "forbidden_etfs": sorted(FORBIDDEN_ETFS),
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
            "transaction_cost_bps": list(COSTS),
            "official_score_requires_complete_prospective_periods": True,
            "training_years_are_never_counted_as_wins": True,
            "researcher_exposed_2026_diagnostic_is_never_counted_as_a_win": True,
            "objective": (
                "win every complete prospective comparison period versus Nasdaq "
                "at 50bps without changing the frozen model on observed results"
            ),
        },
        "immutability": {
            "protocol_overwrite_allowed": False,
            "signal_overwrite_allowed": False,
            "ledger_mode": "append_only_sha256_chain",
            "historical_valuation_overwrite_allowed": False,
        },
        "input_bindings": {
            "runner": _file_binding(runner),
            "v24_signal_helpers": _file_binding(
                "scripts/research_v24_stock_momentum_development.py"
            ),
            "v26_selector": _file_binding(
                "scripts/research_v26_large_liquid_stock_momentum.py"
            ),
            "v26_manifest": _file_binding(V26_MANIFEST),
            "v28_risk_replay": _file_binding(
                "scripts/research_v28_stock_trailing_stop_development.py"
            ),
            "v28_manifest": _file_binding(V28_MANIFEST),
            "data_quality": _file_binding("src/research/data_quality.py"),
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
        },
    )
    return {**protocol, "protocol": _file_binding(item), "ledger": str(ledger)}


def _validated_protocol(path: str | Path = PROTOCOL_PATH) -> tuple[dict, str]:
    item = Path(path)
    protocol = json.loads(item.read_text(encoding="utf-8"))
    protocol_sha = _sha256(item)
    if protocol.get("model_version") != MODEL_VERSION:
        raise RuntimeError("unexpected v42 model version")
    if protocol.get("model") != _selected_model():
        raise RuntimeError("v42 frozen model binding changed")
    if protocol.get("release_status") != "BLOCKED":
        raise RuntimeError("v42 release boundary changed")
    if protocol.get("promotion_eligible"):
        raise RuntimeError("v42 cannot be promotion eligible")
    if not protocol.get("evaluation", {}).get("training_years_are_never_counted_as_wins"):
        raise RuntimeError("v42 training-evidence partition changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v42 frozen input changed: {name}")
    return protocol, protocol_sha


def _latest_date(path: Path) -> pd.Timestamp | None:
    if not path.is_file():
        return None
    dates = pd.read_csv(path, usecols=["date"])["date"]
    if dates.empty:
        return None
    return pd.to_datetime(dates, errors="raise").max().normalize()


def _hardlink_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _signal_tickers(signals_dir: Path = SIGNALS_DIR) -> set[str]:
    tickers: set[str] = set()
    for path in sorted(signals_dir.glob("signal_*.json")):
        signal = json.loads(path.read_text(encoding="utf-8"))
        tickers.update(
            row["ticker"]
            for row in signal["targets"]
            if row["ticker"] != "__CASH__"
        )
    return tickers


def _initialize_fundamental_work(work: Path) -> None:
    mappings = {
        Path(POINT_IN_TIME_FUNDAMENTALS_FILE): work / "fundamentals.csv",
        Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE): work / "quarterly.csv",
        Path(FUNDAMENTALS_REFRESH_STATE_FILE): work / "refresh_state.json",
        Path(FUNDAMENTALS_COVERAGE_FILE): work / "coverage.json",
        Path(QUARTERLY_FUNDAMENTALS_COVERAGE_FILE): work / "quarterly_coverage.json",
    }
    work.mkdir(parents=True, exist_ok=True)
    for source, target in mappings.items():
        if not target.exists() and source.exists():
            shutil.copy2(source, target)


def _refresh_fundamentals_isolated(
    *,
    as_of: pd.Timestamp,
    universe_path: Path,
    tickers: list[str],
    work: Path,
    workers: int,
) -> dict:
    """Refresh parsed outputs in work while leaving formal datasets untouched."""
    _initialize_fundamental_work(work)
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
            cache_dir=fundamentals_update.SEC_COMPANYFACTS_CACHE_DIR,
        )
    finally:
        for name, value in original.items():
            setattr(fundamentals_update, name, value)
    return audit


def _price_manifest(price_dir: Path, tickers: list[str]) -> dict[str, dict]:
    result = {}
    for ticker in sorted(tickers):
        path = price_dir / f"{ticker.lower()}.csv"
        if not path.is_file():
            raise RuntimeError(f"staged price file is missing: {ticker}")
        latest = _latest_date(path)
        result[ticker] = {
            "file": f"prices/{path.name}",
            "sha256": _sha256(path),
            "latest_date": latest.strftime("%Y-%m-%d") if latest is not None else None,
        }
    return result


def stage_bundle(
    *,
    as_of: str | pd.Timestamp,
    purpose: str,
    bundles_dir: Path = BUNDLES_DIR,
    work_dir: Path = WORK_DIR,
    signals_dir: Path = SIGNALS_DIR,
    workers: int = 16,
    fundamental_workers: int = 4,
) -> dict:
    """Refresh mutable work inputs, then freeze one immutable dated bundle."""
    stamp = pd.Timestamp(as_of).normalize()
    purpose = purpose.upper()
    if purpose not in {"SIGNAL", "MARK"}:
        raise ValueError("bundle purpose must be SIGNAL or MARK")
    suffix = stamp.strftime("%Y-%m-%d") + "_" + purpose.lower()
    bundle = Path(bundles_dir) / suffix
    if bundle.exists():
        raise RuntimeError(f"v42 bundle will not be overwritten: {bundle}")

    market_work = Path(work_dir) / "market"
    market_work.mkdir(parents=True, exist_ok=True)
    universe_path = market_work / "current_universe.csv"
    universe_refresh = None
    if purpose == "SIGNAL":
        universe_refresh = refresh_universe(
            stamp.date(),
            min_market_cap=0,
            target_path=universe_path,
            common_equities_only=True,
        )
        current = investable_common_equities(
            pd.read_csv(universe_path, keep_default_na=False)
        )
        tickers = sorted(
            set(current["Symbol"].dropna().astype(str).str.upper())
            - FORBIDDEN_ETFS
        )
    else:
        tickers = sorted(_signal_tickers(Path(signals_dir)))
        if not tickers:
            raise RuntimeError("no frozen stock target exists for a mark bundle")

    price_dir = market_work / "prices"
    index_path = market_work / "nasdaq_index.csv"
    seed = seed_cache(tickers, price_dir=price_dir, index_path=index_path)
    market_update = update_all(
        end=stamp.date(),
        workers=workers,
        tickers=tickers,
        price_dir=price_dir,
        index_path=index_path,
    )
    index_refresh = reconcile_research_index(
        stamp,
        index_path=index_path,
        provenance_path=market_work / "index_close_provenance.json",
    )
    qqq_path = market_work / "qqq.csv"
    refresh_core_price(qqq_path)

    fundamentals_audit = None
    fundamental_work = Path(work_dir) / "fundamentals"
    if purpose == "SIGNAL":
        fundamentals_audit = _refresh_fundamentals_isolated(
            as_of=stamp,
            universe_path=universe_path,
            tickers=tickers,
            work=fundamental_work,
            workers=fundamental_workers,
        )

    temporary = bundle.with_name("." + bundle.name + ".tmp")
    if temporary.exists():
        raise RuntimeError(f"stale v42 temporary bundle exists: {temporary}")
    temporary.mkdir(parents=True)
    try:
        for ticker in tickers:
            source = price_dir / f"{ticker.lower()}.csv"
            if source.is_file():
                _hardlink_or_copy(source, temporary / "prices" / source.name)
        shutil.copy2(index_path, temporary / "nasdaq_index.csv")
        shutil.copy2(qqq_path, temporary / "qqq.csv")
        shutil.copy2(
            market_work / "index_close_provenance.json",
            temporary / "index_close_provenance.json",
        )
        if purpose == "SIGNAL":
            shutil.copy2(universe_path, temporary / "current_universe.csv")
            shutil.copy2(fundamental_work / "quarterly.csv", temporary / "quarterly.csv")
            shutil.copy2(fundamental_work / "coverage.json", temporary / "fundamentals_coverage.json")
            shutil.copy2(
                fundamental_work / "quarterly_coverage.json",
                temporary / "quarterly_coverage.json",
            )

        price_bindings = _price_manifest(temporary / "prices", tickers)
        exact = sum(
            binding["latest_date"] == stamp.strftime("%Y-%m-%d")
            for binding in price_bindings.values()
        )
        latest_index = _latest_date(temporary / "nasdaq_index.csv")
        latest_qqq = _latest_date(temporary / "qqq.csv")
        gates = {
            "nasdaq_through_as_of": latest_index is not None and latest_index >= stamp,
            "qqq_through_as_of": latest_qqq is not None and latest_qqq >= stamp,
            "all_required_price_files_present": len(price_bindings) == len(tickers),
            "required_prices_exact_at_least_98pct": (
                exact / len(tickers) >= 0.98 if tickers else False
            ),
        }
        if purpose == "SIGNAL":
            quarterly = pd.read_csv(temporary / "quarterly.csv")
            available = pd.to_datetime(quarterly["available_date"], errors="coerce")
            gates.update({
                "fundamentals_refresh_as_of_signal": (
                    str(fundamentals_audit.get("as_of"))
                    == stamp.strftime("%Y-%m-%d")
                ),
                "fundamentals_no_future_available_date": (
                    available.dropna().le(stamp).all()
                ),
                "fundamentals_no_deferred_limit": (
                    int(fundamentals_audit.get("deferred_by_limit_ticker_count", 0))
                    == 0
                ),
                "fundamentals_parsed_outputs_written": bool(
                    fundamentals_audit.get("parsed_outputs_written")
                ),
            })
        if not all(gates.values()):
            failed = sorted(name for name, passed in gates.items() if not passed)
            raise RuntimeError(f"v42 staged bundle is not ready: {failed}")

        manifest = {
            "schema_version": 1,
            "research_only": True,
            "purpose": purpose,
            "as_of": stamp.strftime("%Y-%m-%d"),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "price_files": price_bindings,
            "files": {
                "nasdaq_index.csv": _sha256(temporary / "nasdaq_index.csv"),
                "qqq.csv": _sha256(temporary / "qqq.csv"),
                "index_close_provenance.json": _sha256(
                    temporary / "index_close_provenance.json"
                ),
            },
            "readiness_gates": gates,
            "market_refresh": {
                "seed": seed,
                "universe_refresh": universe_refresh,
                "update": market_update,
                "index_refresh": index_refresh,
            },
            "formal_market_files_modified": False,
            "formal_financial_files_modified": False,
            "release_status": "BLOCKED",
            "broker_action_authorized": False,
        }
        if purpose == "SIGNAL":
            manifest["files"].update({
                "current_universe.csv": _sha256(temporary / "current_universe.csv"),
                "quarterly.csv": _sha256(temporary / "quarterly.csv"),
                "fundamentals_coverage.json": _sha256(
                    temporary / "fundamentals_coverage.json"
                ),
                "quarterly_coverage.json": _sha256(
                    temporary / "quarterly_coverage.json"
                ),
            })
            manifest["fundamentals_refresh"] = fundamentals_audit
        with (temporary / "bundle_manifest.json").open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, bundle)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return {
        "status": "FROZEN_INPUT_BUNDLE",
        "purpose": purpose,
        "as_of": stamp.strftime("%Y-%m-%d"),
        "bundle": str(bundle),
        "manifest_sha256": _sha256(bundle / "bundle_manifest.json"),
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
    }


def _validated_bundle(bundle: str | Path, expected_purpose: str | None = None) -> tuple[dict, str]:
    root = Path(bundle)
    manifest_path = root / "bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if expected_purpose and manifest.get("purpose") != expected_purpose.upper():
        raise RuntimeError("v42 bundle purpose does not match this operation")
    if not all(manifest.get("readiness_gates", {}).values()):
        raise RuntimeError("v42 bundle readiness is not fully satisfied")
    for name, expected_sha in manifest.get("files", {}).items():
        if _sha256(root / name) != expected_sha:
            raise RuntimeError(f"v42 bundle file binding changed: {name}")
    for ticker, binding in manifest.get("price_files", {}).items():
        if _sha256(root / binding["file"]) != binding["sha256"]:
            raise RuntimeError(f"v42 bundle price binding changed: {ticker}")
    return manifest, _sha256(manifest_path)


def _is_month_end_signal(signal_date: pd.Timestamp) -> bool:
    calendar = nasdaq_calendar_for_year(signal_date.year)
    sessions = pd.DatetimeIndex(
        calendar.sessions_in_range(
            signal_date.replace(day=1), signal_date + pd.offsets.MonthEnd(0)
        )
    ).tz_localize(None).normalize()
    return bool(len(sessions) and sessions[-1] == signal_date)


def _load_signal_inputs(bundle: Path, signal_date: pd.Timestamp) -> dict:
    load_start = (signal_date - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    raw_close, dollar_volume = load_panel(
        bundle / "prices", load_start, signal_date.strftime("%Y-%m-%d")
    )
    close = back_adjust_common_splits(raw_close).sort_index()
    dollar_volume = dollar_volume.reindex_like(close)
    nasdaq = pd.read_csv(
        bundle / "nasdaq_index.csv", index_col="date", parse_dates=True
    )["close"].sort_index().loc[:signal_date]
    quarterly = load_quarterly_fundamentals(bundle / "quarterly.csv")
    current = investable_common_equities(
        pd.read_csv(bundle / "current_universe.csv", keep_default_na=False)
    )
    current_symbols = (
        set(current["Symbol"].dropna().astype(str).str.upper()) - FORBIDDEN_ETFS
    )

    def universe(as_of):
        return current_symbols if pd.Timestamp(as_of).normalize() == signal_date else None

    return {
        "raw_close": raw_close,
        "close": close,
        "dollar_volume": dollar_volume,
        "nasdaq": nasdaq,
        "quarterly": quarterly,
        "universe": universe,
        "technical_cache": {},
        "quality_cache": {},
        "large_liquid_cache": {},
    }


def build_signal_payload(
    *,
    signal_date: str | pd.Timestamp,
    inputs: dict,
    model: dict,
    protocol_sha256: str,
    bundle_manifest_sha256: str,
    ranking_function: Callable | None = None,
) -> dict:
    stamp = pd.Timestamp(signal_date).normalize()
    if stamp < FIRST_PROSPECTIVE_SIGNAL_DATE:
        raise ValueError("v42 refuses pre-prospective signals")
    if not _is_month_end_signal(stamp):
        raise ValueError("v42 signals require the final Nasdaq session of a month")
    close = inputs["close"].sort_index()
    if stamp not in close.index:
        raise RuntimeError("signal bundle lacks the completed signal close")
    if close.index.max() > stamp:
        raise RuntimeError("signal generation refuses future price rows")
    nasdaq = inputs["nasdaq"].sort_index()
    if nasdaq.index.max() > stamp or stamp not in nasdaq.index:
        raise RuntimeError("signal generation has an invalid Nasdaq cutoff")
    quarterly = inputs["quarterly"]
    if quarterly["available_date"].dropna().gt(stamp).any():
        raise RuntimeError("signal generation refuses future fundamentals")

    index_close = nasdaq.reindex(close.index).ffill()
    regime_on = bool(market_regime_is_on(stamp, index_close, v24.MARKET_MA_DAYS))
    spec = model["selector_specification"]
    ranking_function = ranking_function or v26._large_liquid_ranking
    ranking = (
        ranking_function(stamp, spec, inputs)
        if regime_on
        else pd.DataFrame()
    )
    top_n = int(spec["top_n"])
    selected = ranking.head(top_n).index.astype(str).tolist()
    forbidden = sorted(set(selected) & FORBIDDEN_ETFS)
    if forbidden:
        raise RuntimeError(f"v42 selected forbidden ETFs: {forbidden}")
    if selected:
        targets = [
            {"ticker": ticker, "target_weight": 1.0 / top_n}
            for ticker in selected
        ]
    else:
        targets = [{"ticker": "__CASH__", "target_weight": 0.0}]
    return {
        "schema_version": 1,
        "research_only": True,
        "model_version": MODEL_VERSION,
        "signal_date": stamp.strftime("%Y-%m-%d"),
        "execution_date": None,
        "execution_policy": "first common trading-session close after signal",
        "market_regime_on": regime_on,
        "targets": targets,
        "selected_count": len(selected),
        "contains_index_etf_holdings": False,
        "protocol_sha256": protocol_sha256,
        "bundle_manifest_sha256": bundle_manifest_sha256,
        "parameters_changed_after_observation": False,
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
    }


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
        raise RuntimeError("v42 ledger does not bind the frozen protocol")
    manifest, manifest_sha = _validated_bundle(bundle, "SIGNAL")
    signal_date = pd.Timestamp(manifest["as_of"]).normalize()
    if any(
        event["event_type"] == "SIGNAL_FROZEN"
        and event["payload"]["signal_date"] == signal_date.strftime("%Y-%m-%d")
        for event in events
    ):
        raise RuntimeError("v42 signal date is already frozen")
    output = Path(signals_dir) / f"signal_{signal_date:%Y-%m-%d}.json"
    if output.exists():
        raise RuntimeError(f"v42 signal will not be overwritten: {output}")
    inputs = _load_signal_inputs(Path(bundle), signal_date)
    payload = build_signal_payload(
        signal_date=signal_date,
        inputs=inputs,
        model=protocol["model"],
        protocol_sha256=protocol_sha,
        bundle_manifest_sha256=manifest_sha,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    signal_sha = _sha256(output)
    append_event(
        path=ledger_path,
        protocol_sha256=protocol_sha,
        event_type="SIGNAL_FROZEN",
        payload={
            "signal_date": payload["signal_date"],
            "signal_path": str(output),
            "signal_sha256": signal_sha,
            "bundle_manifest_sha256": manifest_sha,
            "targets": payload["targets"],
        },
    )
    return {
        "status": "FROZEN_PROSPECTIVE_SIGNAL",
        "signal_date": payload["signal_date"],
        "signal": str(output),
        "signal_sha256": signal_sha,
        "targets": payload["targets"],
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
    }


def _signal_artifacts(signals_dir: Path, as_of: pd.Timestamp) -> list[tuple[Path, dict]]:
    result = []
    for path in sorted(signals_dir.glob("signal_*.json")):
        signal = json.loads(path.read_text(encoding="utf-8"))
        if pd.Timestamp(signal["signal_date"]) <= as_of:
            result.append((path, signal))
    return result


def _execution_map(events: list[dict]) -> dict[str, str]:
    return {
        event["payload"]["signal_date"]: event["payload"]["execution_date"]
        for event in events
        if event["event_type"] == "EXECUTION_DATE_BOUND"
    }


def _load_mark_market(bundle: Path, as_of: pd.Timestamp) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    raw_close, _ = load_panel(
        bundle / "prices",
        (FIRST_PROSPECTIVE_SIGNAL_DATE - pd.Timedelta(days=400)).strftime("%Y-%m-%d"),
        as_of.strftime("%Y-%m-%d"),
    )
    nasdaq = pd.read_csv(
        bundle / "nasdaq_index.csv", index_col="date", parse_dates=True
    )["close"].sort_index().loc[:as_of]
    qqq = pd.read_csv(
        bundle / "qqq.csv", index_col="date", parse_dates=True
    ).sort_index().loc[:as_of]
    return raw_close, nasdaq, qqq


def _first_execution_date(
    signal: dict,
    raw_close: pd.DataFrame,
    nasdaq: pd.Series,
) -> pd.Timestamp | None:
    signal_date = pd.Timestamp(signal["signal_date"])
    sessions = nasdaq.loc[nasdaq.index > signal_date].dropna().index
    if not len(sessions):
        return None
    execution = pd.Timestamp(sessions[0]).normalize()
    if execution not in raw_close.index:
        raise RuntimeError("first execution session is absent from staged prices")
    selected = [
        row["ticker"] for row in signal["targets"] if row["ticker"] != "__CASH__"
    ]
    missing = [
        ticker
        for ticker in selected
        if ticker not in raw_close.columns or pd.isna(raw_close.at[execution, ticker])
    ]
    if missing:
        raise RuntimeError(
            f"first execution close is missing selected prices: {missing}"
        )
    return execution


def _qqq_returns(qqq: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.Series:
    total = qqq["close"].add(
        qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index)).fillna(0.0)
    )
    return total.div(qqq["close"].shift(1)).sub(1.0).reindex(dates).fillna(0.0)


def _maximum_drawdown(nav: pd.Series) -> float:
    return float(nav.div(nav.cummax()).sub(1.0).min())


def _market_prefix_sha256(
    bundle: Path,
    *,
    tickers: list[str],
    as_of: pd.Timestamp,
) -> str:
    """Hash only strategy-relevant input rows through one frozen mark date."""
    digest = hashlib.sha256()
    inputs = [
        ("NASDAQ", bundle / "nasdaq_index.csv", ["date", "close"]),
        ("QQQ", bundle / "qqq.csv", ["date", "close", "cash_dividend"]),
    ]
    inputs.extend(
        (
            ticker,
            bundle / "prices" / f"{ticker.lower()}.csv",
            ["date", "close", "volume"],
        )
        for ticker in sorted(tickers)
    )
    for name, path, columns in inputs:
        frame = pd.read_csv(path)
        missing = set(columns) - set(frame.columns)
        if missing == {"cash_dividend"}:
            frame["cash_dividend"] = 0.0
            missing = set()
        if missing:
            raise RuntimeError(
                f"v42 prefix input {name} lacks columns: {sorted(missing)}"
            )
        frame = frame[columns].copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
        frame = frame.loc[frame["date"] <= as_of].sort_values("date")
        if frame.empty or frame["date"].max() < as_of:
            raise RuntimeError(f"v42 prefix input {name} is stale at {as_of.date()}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(
            frame.to_csv(index=False, date_format="%Y-%m-%d").encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _period_evaluation(
    result: pd.DataFrame,
    *,
    first_execution: pd.Timestamp,
) -> dict:
    frame = result.loc[first_execution:].copy()
    monthly = (1.0 + frame[["strategy", "benchmark"]]).groupby(
        frame.index.to_period("M")
    ).prod().sub(1.0)
    complete_months = []
    for period, row in monthly.iterrows():
        calendar = nasdaq_calendar_for_year(period.year)
        sessions = pd.DatetimeIndex(
            calendar.sessions_in_range(
                period.start_time.normalize(), period.end_time.normalize()
            )
        ).tz_localize(None).normalize()
        observed = frame.index[frame.index.to_period("M") == period]
        if len(sessions) and len(observed) and observed[0] == sessions[0] and observed[-1] == sessions[-1]:
            complete_months.append({
                "period": str(period),
                "strategy_return": float(row["strategy"]),
                "nasdaq_return": float(row["benchmark"]),
                "win": bool(row["strategy"] > row["benchmark"]),
            })
    full_years = []
    for year, group in frame.groupby(frame.index.year):
        calendar = nasdaq_calendar_for_year(int(year))
        sessions = pd.DatetimeIndex(
            calendar.sessions_in_range(pd.Timestamp(year, 1, 1), pd.Timestamp(year, 12, 31))
        ).tz_localize(None).normalize()
        if len(sessions) and group.index[0] == sessions[0] and group.index[-1] == sessions[-1]:
            strategy = float((1.0 + group["strategy"]).prod() - 1.0)
            benchmark = float((1.0 + group["benchmark"]).prod() - 1.0)
            full_years.append({
                "year": int(year),
                "strategy_return": strategy,
                "nasdaq_return": benchmark,
                "win": strategy > benchmark,
            })
    return {
        "complete_prospective_months": complete_months,
        "complete_month_wins": sum(row["win"] for row in complete_months),
        "complete_month_count": len(complete_months),
        "all_complete_prospective_months_won": bool(complete_months)
        and all(row["win"] for row in complete_months),
        "complete_prospective_years": full_years,
        "official_year_wins": sum(row["win"] for row in full_years),
        "official_year_count": len(full_years),
        "training_year_wins_counted": 0,
        "reused_2026_diagnostic_wins_counted": 0,
    }


def append_mark(
    *,
    bundle: str | Path,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
    signals_dir: str | Path = SIGNALS_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    manifest, manifest_sha = _validated_bundle(bundle, "MARK")
    as_of = pd.Timestamp(manifest["as_of"]).normalize()
    events = read_ledger(ledger_path)
    if not events or events[0]["protocol_sha256"] != protocol_sha:
        raise RuntimeError("v42 ledger does not bind the frozen protocol")
    prior_marks = [
        event for event in events if event["event_type"] == "VALUATION_APPENDED"
    ]
    if prior_marks and as_of <= pd.Timestamp(prior_marks[-1]["payload"]["as_of"]):
        raise RuntimeError("v42 marks are append-only by completed session")

    signal_items = _signal_artifacts(Path(signals_dir), as_of)
    if not signal_items:
        return {
            "status": "WAITING_FOR_FIRST_FROZEN_SIGNAL",
            "as_of": as_of.strftime("%Y-%m-%d"),
            "written": False,
            "release_status": "BLOCKED",
        }
    for path, signal in signal_items:
        if _sha256(path) != next(
            event["payload"]["signal_sha256"]
            for event in events
            if event["event_type"] == "SIGNAL_FROZEN"
            and event["payload"]["signal_date"] == signal["signal_date"]
        ):
            raise RuntimeError("a frozen v42 signal artifact changed")

    raw_close, nasdaq, qqq = _load_mark_market(Path(bundle), as_of)
    executions = _execution_map(events)
    for _path, signal in signal_items:
        signal_date = signal["signal_date"]
        if signal_date in executions:
            continue
        execution = _first_execution_date(signal, raw_close, nasdaq)
        if execution is None:
            continue
        append_event(
            path=ledger_path,
            protocol_sha256=protocol_sha,
            event_type="EXECUTION_DATE_BOUND",
            payload={
                "signal_date": signal_date,
                "execution_date": execution.strftime("%Y-%m-%d"),
                "signal_sha256": _sha256(_path),
                "bundle_manifest_sha256": manifest_sha,
                "paper_execution_only": True,
            },
        )
        executions[signal_date] = execution.strftime("%Y-%m-%d")

    if not executions:
        return {
            "status": "WAITING_FOR_FIRST_EXECUTION_SESSION",
            "as_of": as_of.strftime("%Y-%m-%d"),
            "written": False,
            "release_status": "BLOCKED",
        }

    rows = []
    for _path, signal in signal_items:
        execution = executions.get(signal["signal_date"])
        if execution is None or pd.Timestamp(execution) > as_of:
            continue
        for target in signal["targets"]:
            rows.append({
                "effective_date": pd.Timestamp(execution),
                "ticker": target["ticker"],
                "target_weight": target["target_weight"],
            })
    target_schedule = pd.DataFrame(rows)
    first_execution = pd.Timestamp(target_schedule["effective_date"].min())
    prefix_tickers = sorted(
        set(target_schedule["ticker"].astype(str)) - {"__CASH__"}
    )
    cost_metrics = {}
    replay_results = {}
    for cost in COSTS:
        result = v28.replay_with_individual_trailing_stop(
            raw_close,
            nasdaq,
            target_schedule,
            first_execution,
            as_of,
            trailing_stop_fraction=float(
                protocol["model"]["risk_specification"]["trailing_stop_fraction"]
            ),
            transaction_cost_bps=float(cost),
        ).copy()
        if result.empty or result.index[-1] != as_of:
            raise RuntimeError("v42 mark lacks the completed as-of session")
        result.loc[first_execution, "benchmark"] = 0.0
        result["qqq"] = _qqq_returns(qqq, result.index)
        result.loc[first_execution, "qqq"] = 0.0
        strategy_nav = (1.0 + result["strategy"]).cumprod()
        nasdaq_nav = (1.0 + result["benchmark"]).cumprod()
        qqq_nav = (1.0 + result["qqq"]).cumprod()
        cost_metrics[str(cost)] = {
            "strategy_nav": float(strategy_nav.iloc[-1]),
            "nasdaq_nav": float(nasdaq_nav.iloc[-1]),
            "qqq_total_return_nav": float(qqq_nav.iloc[-1]),
            "excess_vs_nasdaq_percentage_points": float(
                (strategy_nav.iloc[-1] - nasdaq_nav.iloc[-1]) * 100.0
            ),
            "strategy_maximum_drawdown": _maximum_drawdown(strategy_nav),
            "nasdaq_maximum_drawdown": _maximum_drawdown(nasdaq_nav),
        }
        replay_results[cost] = result

    for event in prior_marks:
        prior = event["payload"]
        prior_date = pd.Timestamp(prior["as_of"])
        current_prefix = _market_prefix_sha256(
            Path(bundle),
            tickers=list(prior["market_prefix_tickers"]),
            as_of=prior_date,
        )
        if current_prefix != prior["market_prefix_sha256"]:
            raise RuntimeError(
                "new bundle revises an already frozen prospective input prefix"
            )
        for cost in COSTS:
            result = replay_results[cost].loc[:prior_date]
            recomputed = float((1.0 + result["strategy"]).prod())
            recorded = float(prior["cost_metrics"][str(cost)]["strategy_nav"])
            if abs(recomputed - recorded) > 1e-10:
                raise RuntimeError(
                    "new bundle revises an already frozen prospective valuation"
                )

    period_evaluation = _period_evaluation(
        replay_results[50], first_execution=first_execution
    )
    payload = {
        "as_of": as_of.strftime("%Y-%m-%d"),
        "first_execution_date": first_execution.strftime("%Y-%m-%d"),
        "cost_metrics": cost_metrics,
        "period_evaluation_50bps": period_evaluation,
        "bundle_manifest_sha256": manifest_sha,
        "market_prefix_tickers": prefix_tickers,
        "market_prefix_sha256": _market_prefix_sha256(
            Path(bundle), tickers=prefix_tickers, as_of=as_of
        ),
        "contains_index_etf_holdings": False,
        "parameters_changed_after_observation": False,
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
    }
    event = append_event(
        path=ledger_path,
        protocol_sha256=protocol_sha,
        event_type="VALUATION_APPENDED",
        payload=payload,
    )
    return {
        "status": "APPENDED_PROSPECTIVE_MARK",
        "written": True,
        "event_hash": event["event_hash"],
        **payload,
    }


def status(
    *,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    events = read_ledger(ledger_path)
    if not events or events[0]["protocol_sha256"] != protocol_sha:
        raise RuntimeError("v42 ledger/protocol binding is invalid")
    signals = [event for event in events if event["event_type"] == "SIGNAL_FROZEN"]
    executions = [
        event for event in events if event["event_type"] == "EXECUTION_DATE_BOUND"
    ]
    marks = [event for event in events if event["event_type"] == "VALUATION_APPENDED"]
    latest = marks[-1]["payload"] if marks else None
    return {
        "status": (
            "PROSPECTIVE_OBSERVATION_ACTIVE"
            if signals else "WAITING_FOR_FIRST_PROSPECTIVE_SIGNAL"
        ),
        "model_version": MODEL_VERSION,
        "protocol_sha256": protocol_sha,
        "ledger_event_count": len(events),
        "frozen_signal_count": len(signals),
        "bound_execution_count": len(executions),
        "valuation_count": len(marks),
        "latest_signal_date": signals[-1]["payload"]["signal_date"] if signals else None,
        "latest_mark": latest,
        "official_training_year_wins": 0,
        "official_reused_2026_diagnostic_wins": 0,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "broker_action_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("freeze-protocol")

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
