#!/usr/bin/env python3
"""Supersede the zero-signal v50r2 protocol with a SIGNAL-runtime repair.

v50r3 is a runtime repair, not a model change.  It reuses v50r1's immutable
development replay, selector, and 20%/25% risk thresholds, keeps the r2
NumPy-scalar repair, and leaves the r1, r2, v42, and v43 runners untouched so
that their protocols stay independently verifiable.

Defects found before the first prospective signal:

* the SIGNAL fundamentals refresh passed the whole current universe as
  explicit tickers, and the SEC refresher aborts when any listed name has no
  SEC CIK (for example FDIC-filing banks), so every attempt and every retry
  would have failed after the price downloads.  r3 refreshes only CIK-mapped
  names from one pinned SEC ticker map and keeps unmapped names in the
  universe (the v51r9 policy); no CIK is invented;
* EDGAR dates filings accepted after its daily cutoff on the next business
  day, and the inherited readiness gate refused the whole bundle when such a
  row appeared.  r3 removes staged parsed rows first available after the
  signal date, which were not public at the signal, and records them;
* a failed or interrupted attempt left build directories that blocked every
  retry in the same window.  r3 serialises staging behind an exclusive lock
  and quarantines never-promoted build directories before retrying;
* the inherited same-UTC-date window lasted about 3.5 hours, all of it during
  Nasdaq after-hours trading, while the Composite official-close fallback
  requires Nasdaq to report the market as Closed, so the window depended on a
  history row that can lag the close by hours.  The signal executes at the
  next session's close; r3's window runs from 30 minutes after the official
  close until pre-market trading opens on that next session (04:00 New York),
  and no SIGNAL_FROZEN event is written after it closes;
* inherited paths are repository-relative, so launching from another working
  directory read an empty ledger.  r3 entry points run from the repository
  root;
* the r2 protocol hash-bound five of the runtime's code files.  r3 binds the
  complete project-local import closure of its runner and scheduler, and every
  protocol validation recomputes it.

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import platform
import sys
import time

import exchange_calendars
import numpy as np
import pandas as pd

from scripts import research_v42_prospective_v28_observation as v42
from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v48_isolated_prospective_v47_observation as v48
from scripts import research_v50_corrected_v47 as r1
from scripts import research_v50r2_corrected_v47 as r2
from src.io import fundamentals_update
from src.research import prospective_schedule as schedule
from src.research.code_closure import (
    closure_differences,
    closure_digest,
    project_import_closure,
)
from src.research.corrected_stock_policy import VALIDATION_PATH


MODEL_VERSION = "v50r3-corrected-v47-sourced-actions"
SUPERSEDED_MODEL_VERSION = r2.MODEL_VERSION
# Windows missed before r3 existed; they are never backfilled.
PRIOR_MISSED_SIGNAL_DATES = (r2.MISSED_SIGNAL_DATE,)
EARLIEST_PROSPECTIVE_SIGNAL_DATE = r2.FIRST_PROSPECTIVE_SIGNAL_DATE
REPO_ROOT = r1.REPO_ROOT
DEVELOPMENT_PROTOCOL_PATH = r1.DEVELOPMENT_PROTOCOL_PATH
DEVELOPMENT_OUTPUT_DIR = r1.DEVELOPMENT_OUTPUT_DIR
V50R1_PROTOCOL_PATH = r1.PROTOCOL_PATH
V50R1_LEDGER_PATH = r1.LEDGER_PATH
V50R2_PROTOCOL_PATH = r2.PROTOCOL_PATH
V50R2_LEDGER_PATH = r2.LEDGER_PATH
V50R2_SUPERSESSION_PATH = r2.OUTPUT_DIR / "superseded_by_v50r3.json"
OUTPUT_DIR = Path("output/research_only/v50/corrected_v47_20260924_r3")
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
LEDGER_PATH = OUTPUT_DIR / "prospective_ledger.jsonl"
SIGNALS_DIR = OUTPUT_DIR / "signals"
BUNDLES_DIR = OUTPUT_DIR / "bundles"
WORK_DIR = OUTPUT_DIR / "staging_work"
STAGING_LOCK_PATH = OUTPUT_DIR / "staging.lock"
SCHEDULER_PATH = Path("scripts/research_v50r3_scheduled_run.py")
CODE_CLOSURE_ROOTS = (
    "scripts/research_v50r3_corrected_v47.py",
    SCHEDULER_PATH.as_posix(),
)
UNMAPPED_CLASSIFICATION = "SEC_CIK_UNAVAILABLE_NOT_DROPPED_OR_GUESSED"
# Nasdaq-listed names without an SEC CIK are a small tail (FDIC filers, very
# recent listings).  A much larger gap means the SEC ticker map itself is
# broken, and refreshing a fraction of the universe must fail closed.
MAXIMUM_UNMAPPED_FRACTION = 0.05
SEC_TICKER_MAP_ATTEMPTS = 3
AS_OF_FILTER_SAMPLE_ROWS = 50
PARSED_FUNDAMENTAL_FILES = ("fundamentals.csv", "quarterly.csv")

_sha256 = r1._sha256
_portable_path = r1._portable_path
_file_binding = r1._file_binding
_git_head = r1._git_head
_hybrid_replay_adapter = r1._hybrid_replay_adapter

# Rehearsals stage a completed non-month-end session after the fact, so they
# relax only the SIGNAL-window check inside the fundamentals refresh.
_REFRESH_OPTIONS = {"enforce_signal_window": True}
_HELD_LOCKS: dict[str, int] = {}


class StagingInProgress(RuntimeError):
    """Another process holds the exclusive v50r3 staging lock."""


def resolve(path: str | Path) -> Path:
    """Resolve a repository-relative path independently of the CWD."""
    return r1._resolve_path(path)


def runtime_repair_specification() -> dict:
    """Describe the r3 runtime repair; the model and thresholds are unchanged."""
    return {
        "bundle_manifest_scalars": "numpy_scalars_normalized_to_python",
        "development_replay": "reused_from_v50r1_unchanged",
        "r1_runner_modified": False,
        "r2_runner_modified": False,
        "sec_unmapped_tickers": (
            "refresh_cik_mapped_tickers_only_keep_unmapped_in_universe"
        ),
        "sec_ticker_map": "one_pinned_snapshot_per_refresh",
        "invented_cik_allowed": False,
        "maximum_unmapped_fraction": MAXIMUM_UNMAPPED_FRACTION,
        "future_available_fundamentals": (
            "removed_from_staged_parsed_outputs_and_recorded"
        ),
        "stale_build_recovery": (
            "quarantine_never_promoted_builds_under_exclusive_lock"
        ),
        "signal_window": (
            "official_close_plus_30_minutes_until_next_session_premarket_open"
        ),
        "signal_frozen_after_window_allowed": False,
        "working_directory": "repository_root",
        "code_binding": "complete_project_import_closure",
        "selection_missing_value_policy_changed": False,
        "missed_signal_backfill_allowed": False,
        "new_threshold_search": False,
    }


def _selected_model() -> dict:
    return r1._selected_model()


def current_code_closure() -> dict:
    files = project_import_closure(CODE_CLOSURE_ROOTS, REPO_ROOT)
    return {
        "roots": list(CODE_CLOSURE_ROOTS),
        "file_count": len(files),
        "files": files,
        "sha256": closure_digest(files),
    }


def runtime_environment() -> dict:
    return {
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "numpy": np.__version__,
        "exchange_calendars": exchange_calendars.__version__,
    }


def _zero_signal_v50r2_events() -> list[dict]:
    events = v43.read_ledger(resolve(V50R2_LEDGER_PATH))
    if [event["event_type"] for event in events] != ["PROTOCOL_FROZEN"]:
        raise RuntimeError("v50r2 can only be superseded before its first signal")
    return events


def signal_dates(protocol: dict) -> tuple[pd.Timestamp, list[pd.Timestamp]]:
    """Return the protocol's first legal SIGNAL date and its missed windows."""
    policy = protocol.get("signal_policy") or {}
    first = pd.Timestamp(policy["first_prospective_signal_date"]).normalize()
    missed = [
        pd.Timestamp(item).normalize()
        for item in policy.get("missed_signal_dates", [])
    ]
    if first < EARLIEST_PROSPECTIVE_SIGNAL_DATE or not schedule.is_month_end_session(
        first
    ):
        raise RuntimeError("v50r3 first prospective signal date is invalid")
    if any(item >= first for item in missed):
        raise RuntimeError("v50r3 missed signal dates must precede the first signal")
    return first, missed


def _validated_protocol(
    path: str | Path = PROTOCOL_PATH,
) -> tuple[dict, str]:
    item = resolve(path)
    protocol = json.loads(item.read_text(encoding="utf-8"))
    protocol_sha = _sha256(item)
    if protocol.get("model_version") != MODEL_VERSION:
        raise RuntimeError("unexpected v50r3 model version")
    if protocol.get("model") != _selected_model():
        raise RuntimeError("v50r3 frozen model binding changed")
    if protocol.get("runtime_repair") != runtime_repair_specification():
        raise RuntimeError("v50r3 runtime repair specification changed")
    if protocol.get("release_status") != "BLOCKED" or protocol.get(
        "promotion_eligible"
    ):
        raise RuntimeError("v50r3 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(binding["path"]) != binding["sha256"]:
            raise RuntimeError(f"v50r3 frozen input changed: {name}")
    frozen = protocol.get("code_closure") or {}
    closure = current_code_closure()
    if (
        frozen.get("roots") != closure["roots"]
        or frozen.get("files") != closure["files"]
        or frozen.get("sha256") != closure["sha256"]
    ):
        differences = closure_differences(
            frozen.get("files") or {}, closure["files"]
        )
        raise RuntimeError(f"v50r3 frozen code closure changed: {differences}")
    signal_dates(protocol)
    return protocol, protocol_sha


def _validated_bundle_contents(
    bundle: str | Path, expected_purpose: str | None = None
) -> tuple[dict, str]:
    """Validate everything except the SIGNAL date and timeliness rules."""
    manifest, manifest_sha = r1.V43_VALIDATED_BUNDLE(bundle, expected_purpose)
    if manifest.get("runner_version") != MODEL_VERSION:
        raise RuntimeError("bundle was not frozen by the v50r3 runner")
    if manifest.get("purpose") == "SIGNAL":
        refresh = manifest.get("fundamentals_refresh") or {}
        policy = refresh.get("sec_unmapped_policy") or {}
        if (
            policy.get("classification") != UNMAPPED_CLASSIFICATION
            or policy.get("invented_cik_count") != 0
            or "as_of_filter" not in refresh
        ):
            raise RuntimeError(
                "v50r3 SIGNAL bundle lacks the repaired fundamentals refresh"
            )
    return manifest, manifest_sha


def _validated_bundle(
    bundle: str | Path, expected_purpose: str | None = None
) -> tuple[dict, str]:
    manifest, manifest_sha = _validated_bundle_contents(bundle, expected_purpose)
    if manifest.get("purpose") == "SIGNAL":
        created_at = pd.Timestamp(manifest["created_at"])
        if created_at.tzinfo is None:
            raise RuntimeError("v50r3 SIGNAL bundle created_at lacks a timezone")
        as_of = pd.Timestamp(manifest["as_of"])
        if as_of < EARLIEST_PROSPECTIVE_SIGNAL_DATE:
            raise RuntimeError(
                "v50r3 refuses a SIGNAL bundle for a missed or pre-r3 date"
            )
        opens, closes = schedule.signal_window(as_of)
        if not opens <= created_at.tz_convert("UTC").to_pydatetime() < closes:
            raise RuntimeError(
                "v50r3 SIGNAL bundle was not staged inside its SIGNAL window "
                f"[{_iso(opens)}, {_iso(closes)})"
            )
    return manifest, manifest_sha


def _build_signal_payload(**kwargs) -> dict:
    payload = r1._build_signal_payload(**kwargs)
    payload["model_version"] = MODEL_VERSION
    payload["runtime_repair"] = runtime_repair_specification()
    payload["code_closure_sha256"] = kwargs["protocol"]["code_closure"]["sha256"]
    payload["runtime_environment"] = runtime_environment()
    return payload


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _signal_staging_is_timely(
    stamp: str | pd.Timestamp,
    observed_at: datetime | None = None,
) -> bool:
    """Whether a SIGNAL for ``stamp`` may start staging at ``observed_at``."""
    observed = schedule.as_utc(observed_at or _utc_now())
    opens, closes = schedule.signal_window(pd.Timestamp(stamp))
    return opens <= observed < closes


def _fetch_sec_ticker_map() -> dict[str, int]:
    error: Exception | None = None
    for attempt in range(SEC_TICKER_MAP_ATTEMPTS):
        try:
            mapping = fundamentals_update.fetch_sec_ticker_map()
            return {str(ticker).upper(): int(cik) for ticker, cik in mapping.items()}
        except Exception as exc:  # network errors are retried, then re-raised
            error = exc
            if attempt + 1 < SEC_TICKER_MAP_ATTEMPTS:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"SEC ticker map unavailable: {error}") from error


def _clone_isolated_cache(source: Path, target: Path) -> dict:
    """Clone the SEC cache, replacing an isolated clone that fails to verify.

    The isolated cache is scratch space hard-linked from the shared cache, so a
    clone left inconsistent by an interrupted refresh is set aside and rebuilt
    instead of blocking every retry.
    """
    try:
        return v43._hardlink_clone_cache(source, target)
    except RuntimeError:
        if not target.exists():
            raise
        quarantined = _quarantine(
            [target], Path(target).parent.parent / "failed_attempts", "isolated_cache"
        )
        result = v43._hardlink_clone_cache(source, target)
        return {**result, "replaced_unverifiable_isolated_cache": quarantined}


def _trim_future_available_rows(work: Path, as_of: pd.Timestamp) -> dict:
    """Remove parsed rows first available after ``as_of``; keep bytes otherwise."""
    summary = {"field": "available_date", "maximum_allowed": f"{as_of:%Y-%m-%d}"}
    for filename in PARSED_FUNDAMENTAL_FILES:
        path = Path(work) / filename
        if not path.is_file():
            summary[filename] = {"present": False}
            continue
        # Strings only: ticker "NA" and every value must round-trip unchanged.
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        available = pd.to_datetime(
            frame["available_date"], errors="coerce", format="ISO8601"
        )
        future = available.gt(as_of)
        removed = frame.loc[future]
        if len(removed):
            temporary = path.with_suffix(path.suffix + ".tmp")
            frame.loc[~future].to_csv(temporary, index=False)
            os.replace(temporary, path)
        sample_columns = [
            column
            for column in ("ticker", "metric", "fiscal_end", "available_date")
            if column in removed.columns
        ]
        summary[filename] = {
            "present": True,
            "source_rows": int(len(frame)),
            "removed_future_rows": int(len(removed)),
            "removed_tickers": sorted(set(removed.get("ticker", []))),
            "removed_sample": removed[sample_columns]
            .head(AS_OF_FILTER_SAMPLE_ROWS)
            .to_dict(orient="records"),
        }
    return summary


def _refresh_fundamentals_isolated(
    *,
    as_of: pd.Timestamp,
    universe_path: Path,
    tickers: list[str],
    work: Path,
    workers: int,
) -> dict:
    """v43's isolated SEC refresh, restricted to CIK-mapped names."""
    stamp = pd.Timestamp(as_of).normalize()
    started = _utc_now()
    if _REFRESH_OPTIONS["enforce_signal_window"]:
        _opens, closes = schedule.signal_window(stamp)
        if started >= closes:
            raise RuntimeError(
                f"v50r3 SIGNAL staging reached the fundamentals refresh at "
                f"{_iso(started)}, after its {stamp:%Y-%m-%d} window closed at "
                f"{_iso(closes)}; the bundle could never be frozen"
            )
    timings: dict[str, float] = {}
    before = v43._formal_financial_bindings()
    v42._initialize_fundamental_work(work)
    source_cache = Path(fundamentals_update.SEC_COMPANYFACTS_CACHE_DIR)
    isolated_cache = Path(work) / "companyfacts_cache"
    clock = time.monotonic()
    clone = _clone_isolated_cache(source_cache, isolated_cache)
    timings["isolated_cache_clone"] = round(time.monotonic() - clock, 3)

    clock = time.monotonic()
    ticker_map = _fetch_sec_ticker_map()
    timings["sec_ticker_map"] = round(time.monotonic() - clock, 3)
    known = set(ticker_map) | set(
        fundamentals_update.load_historical_ticker_ciks(isolated_cache)
    )
    requested = list(dict.fromkeys(str(ticker).upper() for ticker in tickers))
    mapped = [ticker for ticker in requested if ticker in known]
    unmapped = sorted(set(requested) - set(mapped))
    if not mapped:
        raise RuntimeError("SEC ticker map resolved none of the staged universe")
    unmapped_fraction = len(unmapped) / len(requested)
    if unmapped_fraction > MAXIMUM_UNMAPPED_FRACTION:
        raise RuntimeError(
            f"{len(unmapped)} of {len(requested)} staged tickers lack an SEC CIK "
            f"({unmapped_fraction:.1%} > {MAXIMUM_UNMAPPED_FRACTION:.0%}); the SEC "
            "ticker map looks incomplete"
        )

    replacements = {
        "NASDAQ_300M_STOCK_LIST_FILE": str(universe_path),
        "FUNDAMENTALS_REFRESH_STATE_FILE": str(Path(work) / "refresh_state.json"),
        "FUNDAMENTALS_COVERAGE_FILE": str(Path(work) / "coverage.json"),
        "QUARTERLY_FUNDAMENTALS_COVERAGE_FILE": str(
            Path(work) / "quarterly_coverage.json"
        ),
        # The refresher snapshots the map it uses; pin it to the one filtered
        # above so a map published mid-run cannot reintroduce the abort.
        "fetch_sec_ticker_map": lambda: dict(ticker_map),
    }
    original = {name: getattr(fundamentals_update, name) for name in replacements}
    clock = time.monotonic()
    try:
        for name, value in replacements.items():
            setattr(fundamentals_update, name, value)
        audit = fundamentals_update.update_fundamentals(
            as_of=stamp.date(),
            workers=workers,
            refresh_after_days=0,
            output=Path(work) / "fundamentals.csv",
            quarterly_output=Path(work) / "quarterly.csv",
            force=True,
            tickers=mapped,
            cache_dir=isolated_cache,
        )
    finally:
        for name, value in original.items():
            setattr(fundamentals_update, name, value)
    timings["update_fundamentals"] = round(time.monotonic() - clock, 3)
    after = v43._formal_financial_bindings()
    if after != before:
        raise RuntimeError("formal/shared financial inputs changed during v50r3 refresh")
    clock = time.monotonic()
    as_of_filter = _trim_future_available_rows(Path(work), stamp)
    timings["as_of_filter"] = round(time.monotonic() - clock, 3)
    return {
        **audit,
        "isolated_cache": clone,
        "formal_financial_bindings_before": before,
        "formal_financial_bindings_after": after,
        "formal_financial_files_modified": False,
        "shared_companyfacts_cache_modified": False,
        "sec_unmapped_policy": {
            "classification": UNMAPPED_CLASSIFICATION,
            "explicit_stage_ticker_count": len(requested),
            "mapped_ticker_count": len(mapped),
            "unmapped_ticker_count": len(unmapped),
            "unmapped_tickers": unmapped,
            "unmapped_fraction": round(unmapped_fraction, 6),
            "maximum_unmapped_fraction": MAXIMUM_UNMAPPED_FRACTION,
            "kept_in_signal_universe": True,
            "invented_cik_count": 0,
            "mapped_tickers_refreshed": True,
            "selection_missing_value_policy_changed": False,
        },
        "as_of_filter": as_of_filter,
        "refresh_started_at": started.isoformat(timespec="seconds"),
        "refresh_elapsed_seconds": timings,
    }


@contextmanager
def _runtime(*, rehearsal: bool = False):
    """Bind the isolated v43 runtime to r3; keep r2's manifest-scalar repair."""
    replacements = {
        "MODEL_VERSION": MODEL_VERSION,
        "_validated_protocol": _validated_protocol,
        "_validated_bundle": _validated_bundle,
        "_build_signal_payload": _build_signal_payload,
        "_refresh_fundamentals_isolated": _refresh_fundamentals_isolated,
    }
    original = {name: getattr(v43, name) for name in replacements}
    original_v42_json = v43.v42.json
    original_v43_json = v43.json
    original_options = dict(_REFRESH_OPTIONS)
    try:
        for name, value in replacements.items():
            setattr(v43, name, value)
        v43.v42.json = r2._JsonScalarProxy(original_v42_json)
        v43.json = r2._JsonScalarProxy(original_v43_json)
        _REFRESH_OPTIONS["enforce_signal_window"] = not rehearsal
        yield
    finally:
        _REFRESH_OPTIONS.update(original_options)
        v43.json = original_v43_json
        v43.v42.json = original_v42_json
        for name, value in original.items():
            setattr(v43, name, value)


@contextmanager
def staging_lock(path: str | Path = STAGING_LOCK_PATH):
    """Hold the exclusive, non-blocking v50r3 staging lock (re-entrant)."""
    item = resolve(path)
    key = str(item)
    if _HELD_LOCKS.get(key):
        _HELD_LOCKS[key] += 1
        try:
            yield
        finally:
            _HELD_LOCKS[key] -= 1
        return
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StagingInProgress(f"another v50r3 staging holds {item}") from exc
        _HELD_LOCKS[key] = 1
        try:
            yield
        finally:
            _HELD_LOCKS.pop(key, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def stale_build_paths(work_dir: str | Path, suffix: str) -> list[Path]:
    """Build paths that only a crashed or interrupted staging can leave behind."""
    work = Path(work_dir)
    builds = work / "bundle_builds"
    return [
        builds / suffix,
        builds / f".{suffix}.tmp",
        work / "fundamentals" / "companyfacts_cache.clone_tmp",
    ]


def _quarantine(paths: list[Path], root: Path, label: str) -> list[dict]:
    stamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    target_root = Path(root) / f"{label}_{stamp}"
    target_root.mkdir(parents=True, exist_ok=False)
    moved = []
    for path in paths:
        target = target_root / path.name.lstrip(".")
        os.replace(path, target)
        moved.append({"from": str(path), "to": str(target)})
    return moved


def quarantine_stale_builds(work_dir: str | Path, suffix: str) -> list[dict]:
    """Set aside never-promoted builds; call only while holding the lock."""
    stale = [path for path in stale_build_paths(work_dir, suffix) if path.exists()]
    if not stale:
        return []
    return _quarantine(stale, Path(work_dir) / "failed_attempts", suffix)


def freeze_protocol(
    path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
    *,
    now: datetime | None = None,
) -> dict:
    item = resolve(path)
    ledger = resolve(ledger_path)
    if item.exists() or ledger.exists():
        raise RuntimeError("v50r3 protocol/ledger will not be overwritten")
    r1._zero_signal_v48_events()
    r2._zero_signal_v50r1_events()
    _zero_signal_v50r2_events()
    frozen_at = schedule.as_utc(now or _utc_now())
    first = schedule.first_signal_date_after(
        frozen_at, EARLIEST_PROSPECTIVE_SIGNAL_DATE
    )
    missed = [
        *PRIOR_MISSED_SIGNAL_DATES,
        *(
            session
            for session in schedule.month_end_sessions(
                EARLIEST_PROSPECTIVE_SIGNAL_DATE, first
            )
            if session < first
        ),
    ]
    missed_text = [f"{date:%Y-%m-%d}" for date in missed]
    first_window = schedule.signal_window(first)
    manifest = r1._development_manifest()
    protocol = {
        "schema_version": 4,
        "research_only": True,
        "model_version": MODEL_VERSION,
        "status": "FROZEN_WAITING_FOR_FIRST_SIGNAL",
        "frozen_at": frozen_at.isoformat(timespec="seconds"),
        "code_commit": _git_head(),
        "supersedes": {
            "model_version": SUPERSEDED_MODEL_VERSION,
            "protocol": _file_binding(V50R2_PROTOCOL_PATH),
            "ledger": _file_binding(V50R2_LEDGER_PATH),
            "v50r2_signal_count": 0,
            "reason": [
                "the SIGNAL fundamentals refresh aborted when any current "
                "universe member lacked an SEC CIK, so every attempt and retry "
                "would have failed after the price downloads",
                "filings dated after the signal by EDGAR's daily cutoff made "
                "the inherited readiness gate reject the whole bundle",
                "failed or interrupted attempts left build directories that "
                "blocked every retry within the SIGNAL window",
                "the same-UTC-date window lasted about 3.5 hours inside Nasdaq "
                "after-hours trading, while the Composite official-close "
                "fallback requires a Closed market status",
                "repository-relative runtime paths depended on the launch "
                "directory",
                "the r2 protocol bound only five of the runtime's code files",
            ],
        },
        "runtime_repair": runtime_repair_specification(),
        "runtime_environment_at_freeze": runtime_environment(),
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
            "missed_windows": {
                "role": "MISSED_WINDOW_NOT_BACKFILLED",
                "signal_dates": missed_text,
                "counts_as_official_comparison": False,
                "official_year_wins": 0,
            },
            "prospective": {
                "first_signal_date": f"{first:%Y-%m-%d}",
                "counts_as_official_comparison": True,
            },
        },
        "signal_policy": {
            "frequency": "completed calendar-month final Nasdaq session",
            "execution": "next common trading-session close",
            "universe_staging": "inside the SIGNAL window",
            "window_opens_after_official_close_minutes": int(
                schedule.SIGNAL_WINDOW_OPEN_BUFFER.total_seconds() // 60
            ),
            "window_closes": (
                "04:00 America/New_York on the next Nasdaq session, when "
                "pre-market trading opens (exclusive)"
            ),
            "signal_frozen_before_window_closes": True,
            "first_signal_window_utc": {
                "opens": _iso(first_window[0]),
                "closes": _iso(first_window[1]),
            },
            "first_prospective_signal_date": f"{first:%Y-%m-%d}",
            "late_signal_bundle_allowed": False,
            "missed_signal_dates": missed_text,
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
            "code_closure_verified_on_every_protocol_validation": True,
        },
        "input_bindings": {
            "runner": _file_binding(__file__),
            "scheduler": _file_binding(SCHEDULER_PATH),
            "r2_runner": _file_binding(r2.__file__),
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
            "v50r2_protocol": _file_binding(V50R2_PROTOCOL_PATH),
            "v50r2_ledger": _file_binding(V50R2_LEDGER_PATH),
            "corporate_action_validation": _file_binding(VALIDATION_PATH),
            "reviewed_market_moves": _file_binding(
                "stocks_list_dir/nasdaq/reviewed_market_moves.csv"
            ),
        },
        "code_closure": current_code_closure(),
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
            "first_prospective_signal_date": f"{first:%Y-%m-%d}",
            "missed_signal_dates": missed_text,
            "code_closure_sha256": protocol["code_closure"]["sha256"],
            "official_training_year_wins": 0,
            "superseded_v48_signal_count": 0,
            "superseded_v50r1_signal_count": 0,
            "superseded_v50r2_signal_count": 0,
        },
    )
    return {**protocol, "protocol": _file_binding(item), "ledger": str(ledger)}


def write_v50r2_supersession(
    *,
    path: str | Path = V50R2_SUPERSESSION_PATH,
    successor_protocol: str | Path = PROTOCOL_PATH,
) -> dict:
    item = resolve(path)
    if item.exists():
        raise RuntimeError("v50r2 supersession record will not be overwritten")
    _zero_signal_v50r2_events()
    successor, _sha = _validated_protocol(successor_protocol)
    record = {
        "schema_version": 1,
        "status": "SUPERSEDED_BEFORE_FIRST_SIGNAL",
        "recorded_at": _utc_now().isoformat(timespec="seconds"),
        "v50r2_protocol": _file_binding(V50R2_PROTOCOL_PATH),
        "v50r2_ledger": _file_binding(V50R2_LEDGER_PATH),
        "v50r2_signal_count": 0,
        "successor_protocol": _file_binding(successor_protocol),
        "successor_first_prospective_signal_date": successor["signal_policy"][
            "first_prospective_signal_date"
        ],
        "reason": (
            "the r2 SIGNAL fundamentals refresh aborted on SEC-unmapped universe "
            "members and left retry-blocking build directories; r3 repairs the "
            "runtime only, reusing the r1 development replay unchanged"
        ),
    }
    item.parent.mkdir(parents=True, exist_ok=True)
    with item.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return record


def stage_bundle(
    *,
    as_of: str | pd.Timestamp,
    purpose: str,
    bundles_dir: str | Path = BUNDLES_DIR,
    work_dir: str | Path = WORK_DIR,
    signals_dir: str | Path = SIGNALS_DIR,
    ledger_path: str | Path = LEDGER_PATH,
    protocol_path: str | Path = PROTOCOL_PATH,
    lock_path: str | Path = STAGING_LOCK_PATH,
    workers: int = 16,
    fundamental_workers: int = 4,
    observed_at: datetime | None = None,
) -> dict:
    purpose = str(purpose).upper()
    stamp = pd.Timestamp(as_of).normalize()
    protocol, _protocol_sha = _validated_protocol(protocol_path)
    first, missed = signal_dates(protocol)
    if purpose == "SIGNAL" and stamp < first:
        raise RuntimeError(
            f"v50r3 refuses a SIGNAL before {first:%Y-%m-%d}; missed windows "
            f"{[f'{date:%Y-%m-%d}' for date in missed]} are never backfilled"
        )
    suffix = f"{stamp:%Y-%m-%d}_{purpose.lower()}"
    existing = Path(bundles_dir) / suffix
    if purpose == "SIGNAL" and not existing.exists():
        if not _signal_staging_is_timely(stamp, observed_at):
            opens, closes = schedule.signal_window(stamp)
            raise RuntimeError(
                f"v50r3 stages the {stamp:%Y-%m-%d} SIGNAL only inside its "
                f"window [{_iso(opens)}, {_iso(closes)}): the current universe "
                "must be captured after the close and before the next session"
            )
    with staging_lock(lock_path):
        recovered = [] if existing.exists() else quarantine_stale_builds(
            work_dir, suffix
        )
        with _runtime():
            result = v43.stage_bundle(
                as_of=stamp,
                purpose=purpose,
                bundles_dir=Path(bundles_dir),
                work_dir=Path(work_dir),
                signals_dir=Path(signals_dir),
                ledger_path=Path(ledger_path),
                workers=workers,
                fundamental_workers=fundamental_workers,
            )
    result["recovered_stale_builds"] = recovered
    return result


@contextmanager
def _signal_deadline(signal_date: pd.Timestamp):
    """Refuse to write a SIGNAL_FROZEN event once its window has closed.

    The check runs where the event is appended, and the event records the
    checked instant, so every frozen signal carries a timestamp inside its
    window.  Verifying an already frozen signal appends nothing.
    """
    _opens, closes = schedule.signal_window(signal_date)
    original = v43.append_event

    def append_event(**kwargs):
        if kwargs.get("event_type") == "SIGNAL_FROZEN":
            now = schedule.as_utc(_utc_now())
            if now >= closes:
                raise RuntimeError(
                    f"the {signal_date:%Y-%m-%d} SIGNAL window closed at "
                    f"{_iso(closes)}; v50r3 never freezes a signal once "
                    "pre-market trading has opened on the execution session"
                )
            kwargs["recorded_at"] = _iso(now)
        return original(**kwargs)

    v43.append_event = append_event
    try:
        yield
    finally:
        v43.append_event = original


def freeze_signal(
    *,
    bundle: str | Path,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
    signals_dir: str | Path = SIGNALS_DIR,
    lock_path: str | Path = STAGING_LOCK_PATH,
) -> dict:
    protocol, _protocol_sha = _validated_protocol(protocol_path)
    first, _missed = signal_dates(protocol)
    manifest = json.loads(
        (Path(bundle) / "bundle_manifest.json").read_text(encoding="utf-8")
    )
    signal_date = pd.Timestamp(manifest["as_of"]).normalize()
    if signal_date < first:
        raise RuntimeError(
            f"v50r3 refuses a SIGNAL before its first date {first:%Y-%m-%d}"
        )
    with staging_lock(lock_path), _runtime(), _signal_deadline(signal_date):
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
    lock_path: str | Path = STAGING_LOCK_PATH,
) -> dict:
    original_replay = v42.v28.replay_with_individual_trailing_stop
    try:
        v42.v28.replay_with_individual_trailing_stop = _hybrid_replay_adapter
        with staging_lock(lock_path), _runtime():
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
    protocol, _protocol_sha = _validated_protocol(protocol_path)
    first, missed = signal_dates(protocol)
    result["supersedes_v48_before_first_signal"] = True
    result["supersedes_v50r1_before_first_signal"] = True
    result["supersedes_v50r2_before_first_signal"] = True
    result["corrected_price_policy"] = "SOURCED_ACTIONS_ONLY"
    result["late_signal_bundle_allowed"] = False
    result["first_prospective_signal_date"] = f"{first:%Y-%m-%d}"
    result["missed_signal_dates"] = [f"{date:%Y-%m-%d}" for date in missed]
    result["code_closure_sha256"] = protocol["code_closure"]["sha256"]
    result["code_closure_file_count"] = protocol["code_closure"]["file_count"]
    result["code_closure_verified"] = True
    result["runtime_repair"] = runtime_repair_specification()
    return result


def main(argv: list[str] | None = None) -> int:
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("freeze-protocol")
    subparsers.add_parser("write-v50r2-supersession")
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
    args = parser.parse_args(argv)
    if args.command == "status" and not resolve(PROTOCOL_PATH).is_file():
        print(json.dumps({
            "status": "PROTOCOL_NOT_FROZEN",
            "model_version": MODEL_VERSION,
            "protocol_path": PROTOCOL_PATH.as_posix(),
            "release_status": "BLOCKED",
        }, indent=2, sort_keys=True))
        return 3
    if args.command == "freeze-protocol":
        result = freeze_protocol()
    elif args.command == "write-v50r2-supersession":
        result = write_v50r2_supersession()
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
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
