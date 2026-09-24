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
  requires Nasdaq to report the market as Closed, so the window depended on
  the Composite history row alone; Nasdaq's history API also keeps serving a
  response cached per query for hours, so a retry repeated a response fetched
  before the row existed (every Nasdaq request now carries a unique
  parameter).  The signal executes at the next session's close; r3's window
  runs from 30 minutes after the official close until pre-market trading
  opens on that next session (04:00 New York), and no SIGNAL_FROZEN event is
  written after it closes;
* inherited paths are repository-relative, so launching from another working
  directory read an empty ledger.  r3 entry points run from the repository
  root;
* the r2 protocol hash-bound five of the runtime's code files.  r3 binds the
  complete project-local import closure of its runner and scheduler, and every
  protocol validation recomputes it;
* the inherited MARK path could stop valuing the portfolio for good: it
  required a current close, and no unreviewed split-like jump, for every stock
  ever targeted (so one later delisting or split of a stock sold months ago
  blocked every future mark), it could not stage an all-cash month, and it
  re-downloaded QQQ and recent index rows, so any vendor revision of an
  already-valued row broke the frozen prefix check.  r3 stages MARK bundles
  itself: it needs closes only for stocks held into or bought on the mark
  date, checks price events only inside holding windows, carries
  already-valued rows forward unchanged, values an all-cash month against the
  benchmark, and takes post-freeze splits, market moves and terminal returns
  only from an append-only supplement of sourced events.

The observation runs from a pinned copy of the repository: a git worktree on
the ``live/v50r3`` branch, which receives only the freeze commit and the
scheduler's ledger commits.  The command-line entry points that write the
protocol, bundles or ledger refuse any other checkout, so master can keep
changing the files this protocol binds.

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
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
from src.conf import NASDAQ_300M_STOCK_LIST_FILE, NASDAQ_INDEX_FILE
from src.io import fundamentals_update, nasdaq_update
from src.research import corrected_stock_policy
from src.research import prospective_marks as marks
from src.research import prospective_schedule as schedule
from src.research.code_closure import (
    closure_differences,
    closure_digest,
    project_import_closure,
)
from src.research.corrected_stock_policy import VALIDATION_PATH
from src.research.data_quality import detect_common_split_events


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
# Append-only, git-tracked record of sourced events after the frozen
# corporate-action table; every mark binds the rows it used.
SUPPLEMENT_PATH = OUTPUT_DIR / "sourced_event_supplement.csv"
MARK_PROCEDURE = "v50r3-exposure-aware-mark"
# The only branch whose checkout may write the r3 protocol, bundles, or ledger.
LIVE_BRANCH = "live/v50r3"
SETUP_SCRIPT = "scripts/setup_v50r3_live.sh"
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
V42_LOAD_MARK_MARKET = v42._load_mark_market

# Rehearsals stage a completed non-month-end session after the fact, so they
# relax only the SIGNAL-window check inside the fundamentals refresh.
_REFRESH_OPTIONS = {"enforce_signal_window": True}
_HELD_LOCKS: dict[str, int] = {}


class StagingInProgress(RuntimeError):
    """Another process holds the exclusive v50r3 staging lock."""


def resolve(path: str | Path) -> Path:
    """Resolve a repository-relative path independently of the CWD."""
    return r1._resolve_path(path)


def current_branch() -> str | None:
    """The branch checked out in this copy of the repository, if any."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch not in {"", "HEAD"} else None


def require_live_checkout() -> None:
    """Refuse to write r3 state from anywhere but the pinned live copy."""
    branch = current_branch()
    if branch != LIVE_BRANCH:
        raise SystemExit(
            f"v50r3 writes its protocol, bundles and ledger only from a "
            f"{LIVE_BRANCH} checkout; this copy is on "
            f"{branch or 'a detached HEAD'}. Create the live copy with "
            f"{SETUP_SCRIPT} and run the command there."
        )


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
        "mark_procedure": MARK_PROCEDURE,
        "mark_closes_required": "stocks_held_into_or_bought_on_the_mark_date",
        "mark_price_events_checked": "inside_holding_windows_only",
        "mark_input_rows": "already_valued_rows_carried_forward_unchanged",
        "mark_post_freeze_events": "append_only_sourced_event_supplement",
        "mark_unsourced_terminal_return": "fail_closed",
        "mark_all_cash": "valued_against_the_benchmark",
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


def _json_default(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _formal_market_bindings() -> dict[str, str]:
    return {
        "nasdaq_index": v42._sha256(NASDAQ_INDEX_FILE),
        "nasdaq_universe": v42._sha256(NASDAQ_300M_STOCK_LIST_FILE),
    }


def _ledger_target_schedule(events: list[dict], as_of: pd.Timestamp) -> pd.DataFrame:
    """Targets of frozen signals effective by ``as_of``, from the ledger itself."""
    bound = {
        event["payload"]["signal_date"]: event["payload"]["execution_date"]
        for event in events
        if event["event_type"] == "EXECUTION_DATE_BOUND"
    }
    rows = []
    for event in events:
        if event["event_type"] != "SIGNAL_FROZEN":
            continue
        payload = event["payload"]
        signal_date = pd.Timestamp(payload["signal_date"]).normalize()
        if signal_date >= as_of:
            continue
        execution = pd.Timestamp(
            bound.get(payload["signal_date"]) or schedule.next_session(signal_date)
        ).normalize()
        if execution > as_of:
            continue
        rows.extend(
            {
                "effective_date": execution,
                "ticker": str(target["ticker"]),
                "target_weight": float(target["target_weight"]),
            }
            for target in payload["targets"]
        )
    return pd.DataFrame(rows, columns=["effective_date", "ticker", "target_weight"])


def _latest_valued_bundle(events: list[dict], bundles_dir: Path) -> dict | None:
    """The bundle behind the latest mark, verified against its ledger event."""
    valued = [event for event in events if event["event_type"] == "VALUATION_APPENDED"]
    if not valued:
        return None
    payload = valued[-1]["payload"]
    as_of = pd.Timestamp(payload["as_of"]).normalize()
    path = Path(bundles_dir) / f"{as_of:%Y-%m-%d}_mark"
    if not path.is_dir():
        return {"as_of": as_of, "path": None}
    _manifest, manifest_sha = _validated_bundle(path, "MARK")
    if manifest_sha != payload["bundle_manifest_sha256"]:
        raise RuntimeError(
            f"the {as_of:%Y-%m-%d} MARK bundle does not match its ledger event"
        )
    return {"as_of": as_of, "path": path}


def _seed_from_valued_bundle(
    prior: dict | None, tickers: list[str], prices: Path, index_path: Path
) -> list[str]:
    """Restore missing work files from the latest valued bundle, not formal data."""
    if not prior or prior["path"] is None:
        return []
    seeded = []
    for ticker in tickers:
        target = prices / f"{ticker.lower()}.csv"
        source = prior["path"] / "prices" / target.name
        if not target.exists() and source.is_file():
            shutil.copy2(source, target)
            seeded.append(ticker)
    if not index_path.exists() and (prior["path"] / "nasdaq_index.csv").is_file():
        shutil.copy2(prior["path"] / "nasdaq_index.csv", index_path)
        seeded.append("NASDAQ")
    return seeded


def _refresh_index_history(index_path: Path, end: pd.Timestamp) -> dict:
    """Extend the isolated Composite history as update_all does, without stocks.

    update_all with no tickers refreshes the whole formal universe, so an
    all-cash mark must not call it.
    """
    existing = pd.read_csv(index_path, parse_dates=["date"])
    start = existing["date"].max().date() + timedelta(days=1)
    rows = 0
    if start <= end.date():
        data = nasdaq_update.fetch_history(
            "COMP", start, end.date(), asset_class="index"
        )
        data["change_rate"] = data["close"].pct_change()
        rows = nasdaq_update._atomic_merge(index_path, data)
    return {"end": f"{end:%Y-%m-%d}", "requested_ticker_count": 0, "index_rows": rows}


def _carry_valued_prefix(
    prior: dict | None, bundle: Path, tickers: list[str]
) -> dict:
    """Replace staged rows up to the latest mark with the rows that mark used."""
    if prior is None:
        return {"source": None, "reason": "no earlier valuation"}
    if prior["path"] is None:
        return {
            "source": None,
            "reason": (
                "the latest valued bundle is missing locally; fresh rows are "
                "used and the valuation re-verifies every frozen prefix"
            ),
        }
    inputs = [
        ("NASDAQ", "nasdaq_index.csv", ["close"]),
        ("QQQ", "qqq.csv", ["close", "cash_dividend"]),
    ]
    inputs.extend(
        (ticker, f"prices/{ticker.lower()}.csv", ["close", "volume"])
        for ticker in tickers
        if (prior["path"] / "prices" / f"{ticker.lower()}.csv").is_file()
    )
    audits = {}
    for name, relative, compared in inputs:
        target = bundle / relative
        if not target.is_file():
            continue
        combined, audit = marks.carry_frozen_rows(
            pd.read_csv(target), pd.read_csv(prior["path"] / relative),
            prior["as_of"], compared,
        )
        temporary = target.with_name(target.name + ".tmp")
        combined.to_csv(temporary, index=False, date_format="%Y-%m-%d")
        os.replace(temporary, target)
        audits[name] = audit
    return {
        "source": _portable_path(prior["path"]),
        "frozen_through": f"{prior['as_of']:%Y-%m-%d}",
        "carried_inputs": sorted(audits),
        "revised_frozen_rows": sum(
            audit["revised_frozen_rows"] for audit in audits.values()
        ),
        "revisions": {
            name: audit
            for name, audit in audits.items()
            if audit["revised_frozen_rows"]
        },
    }


def _stage_mark_bundle(
    *,
    stamp: pd.Timestamp,
    bundles_dir: Path,
    work_dir: Path,
    ledger_path: Path,
    workers: int,
    supplement_path: str | Path,
) -> dict:
    """Stage an exposure-aware MARK bundle in the isolated work directory."""
    if not v43._is_nasdaq_session(stamp):
        raise ValueError("v50r3 MARK as-of must be a Nasdaq trading session")
    events = v43.read_ledger(ledger_path)
    latest_mark = v43._latest_event_date(events, "VALUATION_APPENDED", "as_of")
    if latest_mark is not None and stamp <= latest_mark:
        raise RuntimeError("v50r3 refuses an already-valued mark bundle date")
    stamp_text = f"{stamp:%Y-%m-%d}"
    suffix = f"{stamp_text}_mark"
    final = bundles_dir / suffix
    if final.exists():
        manifest, manifest_sha = _validated_bundle(final, "MARK")
        return {
            "status": "ALREADY_STAGED_AND_VERIFIED",
            "purpose": "MARK",
            "as_of": manifest["as_of"],
            "bundle": str(final),
            "manifest_sha256": manifest_sha,
            "release_status": "BLOCKED",
        }
    target_schedule = _ledger_target_schedule(events, stamp)
    if target_schedule.empty:
        raise RuntimeError(f"no frozen signal is effective by {stamp_text}")
    tickers = sorted(set(target_schedule["ticker"]) - {marks.CASH})
    required = marks.closes_required(target_schedule, stamp)
    terminal = marks.supplement_terminal_returns(
        marks.load_supplement(resolve(supplement_path))
    )
    prior = _latest_valued_bundle(events, bundles_dir)

    market = work_dir / "market"
    prices = market / "prices"
    index_path = market / "nasdaq_index.csv"
    qqq_path = market / "qqq.csv"
    prices.mkdir(parents=True, exist_ok=True)
    formal_before = _formal_market_bindings()
    seeded = _seed_from_valued_bundle(prior, tickers, prices, index_path)
    seed = v42.seed_cache(tickers, price_dir=prices, index_path=index_path)
    if tickers:
        update = v42.update_all(
            end=stamp.date(),
            workers=workers,
            tickers=tickers,
            price_dir=prices,
            index_path=index_path,
        )
    else:
        update = _refresh_index_history(index_path, stamp)
    index_refresh = v42.reconcile_research_index(
        stamp,
        index_path=index_path,
        provenance_path=market / "index_close_provenance.json",
    )
    v43._trim_qqq(qqq_path, stamp)
    formal_after = _formal_market_bindings()
    if formal_after != formal_before:
        raise RuntimeError("formal market inputs changed during v50r3 MARK staging")
    qqq_provenance = market / "qqq.provenance.json"
    if not qqq_provenance.is_file():
        raise RuntimeError("v50r3 staged QQQ provenance is missing")

    temporary = work_dir / "bundle_builds" / f".{suffix}.tmp"
    if temporary.exists():
        raise RuntimeError(f"stale v50r3 MARK build exists: {temporary}")
    (temporary / "prices").mkdir(parents=True)
    try:
        for ticker in tickers:
            source = prices / f"{ticker.lower()}.csv"
            if source.is_file():
                shutil.copy2(source, temporary / "prices" / source.name)
        index = pd.read_csv(index_path)
        index.loc[
            pd.to_datetime(index["date"], errors="raise").dt.normalize().le(stamp)
        ].to_csv(temporary / "nasdaq_index.csv", index=False)
        shutil.copy2(qqq_path, temporary / "qqq.csv")
        shutil.copy2(
            market / "index_close_provenance.json",
            temporary / "index_close_provenance.json",
        )
        shutil.copy2(qqq_provenance, temporary / "qqq.provenance.json")
        carry = _carry_valued_prefix(prior, temporary, tickers)

        missing = [
            ticker
            for ticker in tickers
            if not (temporary / "prices" / f"{ticker.lower()}.csv").is_file()
        ]
        price_bindings = v42._price_manifest(
            temporary / "prices", [ticker for ticker in tickers if ticker not in missing]
        )
        unpriced, sourced_terminal = [], []
        for ticker in required:
            latest = (price_bindings.get(ticker) or {}).get("latest_date")
            if latest == stamp_text:
                continue
            if latest is not None and (ticker, pd.Timestamp(latest)) in terminal:
                sourced_terminal.append({"ticker": ticker, "last_price_date": latest})
                continue
            unpriced.append({"ticker": ticker, "latest_date": latest})
        gates = {
            "nasdaq_through_as_of": v42._latest_date(temporary / "nasdaq_index.csv")
            == stamp,
            "qqq_through_as_of": v42._latest_date(temporary / "qqq.csv") == stamp,
            "all_required_price_files_present": not missing,
            "held_positions_priced_at_as_of": not unpriced,
        }
        if not all(gates.values()):
            failed = sorted(name for name, passed in gates.items() if not passed)
            raise RuntimeError(
                f"v50r3 MARK bundle is not ready: {failed}; missing price files "
                f"{missing}; held positions without a {stamp_text} close "
                f"{unpriced}. Retry once the closes are published; if a held "
                "stock stopped trading, record its sourced TERMINAL_RETURN with "
                "record-sourced-event"
            )
        manifest = {
            "schema_version": 2,
            "research_only": True,
            "purpose": "MARK",
            "as_of": stamp_text,
            "created_at": _iso(_utc_now()),
            "runner_version": MODEL_VERSION,
            "mark_procedure": MARK_PROCEDURE,
            "price_files": price_bindings,
            "files": {
                name: v42._sha256(temporary / name)
                for name in (
                    "nasdaq_index.csv",
                    "qqq.csv",
                    "index_close_provenance.json",
                    "qqq.provenance.json",
                )
            },
            "readiness_gates": gates,
            "mark_exposure": {
                "targeted_tickers": tickers,
                "closes_required": required,
                "sourced_terminal_returns": sourced_terminal,
            },
            "prefix_carry": carry,
            "market_refresh": {
                "seeded_from_valued_bundle": seeded,
                "seed": seed,
                "update": update,
                "index_refresh": index_refresh,
            },
            "runtime_isolation": {
                "formal_market_bindings_before": formal_before,
                "formal_market_bindings_after": formal_after,
                "formal_market_files_modified": False,
                "formal_financial_files_modified": False,
                "shared_companyfacts_cache_modified": False,
                "qqq_cutoff": stamp_text,
            },
            "formal_market_files_modified": False,
            "formal_financial_files_modified": False,
            "release_status": "BLOCKED",
            "broker_action_authorized": False,
        }
        with (temporary / "bundle_manifest.json").open("x", encoding="utf-8") as handle:
            handle.write(
                json.dumps(manifest, indent=2, sort_keys=True, default=_json_default)
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, final)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return {
        "status": "FROZEN_ISOLATED_INPUT_BUNDLE",
        "purpose": "MARK",
        "as_of": stamp_text,
        "bundle": str(final),
        "manifest_sha256": v42._sha256(final / "bundle_manifest.json"),
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
    }


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
        "code_branch": current_branch(),
        "operations": {
            "live_branch": LIVE_BRANCH,
            "writers": (
                "the scheduler and runner command lines of a live-branch "
                "checkout only"
            ),
            "watchdog_reads": LIVE_BRANCH,
            "setup": SETUP_SCRIPT,
        },
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
        "mark_policy": {
            "procedure": MARK_PROCEDURE,
            "closes_required": "stocks held into or bought on the mark date",
            "price_events_checked": "inside holding windows only",
            "post_freeze_sourced_events": SUPPLEMENT_PATH.as_posix(),
            "sourced_event_types": list(marks.EVENT_TYPES),
            "sourced_events_append_only": True,
            "held_stock_without_later_close": (
                "fail_closed until the closes arrive or a sourced terminal "
                "return is recorded"
            ),
            "already_valued_input_rows": "carried forward unchanged",
            "all_cash_month": "valued against the benchmark",
            "after_missed_signal_window": "the held portfolio keeps being valued",
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
    supplement_path: str | Path = SUPPLEMENT_PATH,
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
            if purpose == "MARK":
                result = _stage_mark_bundle(
                    stamp=stamp,
                    bundles_dir=Path(bundles_dir),
                    work_dir=Path(work_dir),
                    ledger_path=Path(ledger_path),
                    workers=workers,
                    supplement_path=supplement_path,
                )
            else:
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


_MARK_CONTEXT: dict = {}


def _load_mark_market(
    bundle: Path, as_of: pd.Timestamp
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """v42's mark inputs, with every Composite session on the price index.

    An all-cash month has no stock files, and a stock whose history ended has
    no recent rows; the replay still needs each session to value the rest.
    """
    raw_close, nasdaq, qqq = V42_LOAD_MARK_MARKET(bundle, as_of)
    start = v42.FIRST_PROSPECTIVE_SIGNAL_DATE - pd.Timedelta(days=400)
    sessions = nasdaq.index[(nasdaq.index >= start) & (nasdaq.index <= as_of)]
    # An empty panel has a plain Index; keep the union a DatetimeIndex.
    dates = pd.DatetimeIndex(raw_close.index).union(pd.DatetimeIndex(sessions))
    return raw_close.reindex(dates).sort_index(), nasdaq, qqq


def _market_prefix_sha256(
    bundle: Path, *, tickers: list[str], as_of: pd.Timestamp
) -> str:
    """Digest of the valuation's input rows through one mark date.

    The Composite and QQQ must reach ``as_of``.  A stock's rows are digested
    as stored: whether a held stock needed a later close is decided by the
    staging gate and the replay, not here, so a stock sold before it stopped
    trading does not block every later mark.
    """
    as_of = pd.Timestamp(as_of).normalize()
    digest = hashlib.sha256()
    inputs = [
        ("NASDAQ", Path(bundle) / "nasdaq_index.csv", ["close"], True),
        ("QQQ", Path(bundle) / "qqq.csv", ["close", "cash_dividend"], True),
    ]
    inputs.extend(
        (
            ticker,
            Path(bundle) / "prices" / f"{ticker.lower()}.csv",
            ["close", "volume"],
            False,
        )
        for ticker in sorted(tickers)
    )
    for name, path, columns, through_as_of in inputs:
        frame = pd.read_csv(path)
        if "cash_dividend" in columns and "cash_dividend" not in frame.columns:
            frame["cash_dividend"] = 0.0
        missing = {"date", *columns} - set(frame.columns)
        if missing:
            raise RuntimeError(
                f"v50r3 prefix input {name} lacks columns: {sorted(missing)}"
            )
        dates = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
        present = dates.loc[dates.le(as_of)]
        if present.empty:
            raise RuntimeError(f"v50r3 prefix input {name} has no rows by {as_of.date()}")
        if through_as_of and present.max() < as_of:
            raise RuntimeError(f"v50r3 prefix input {name} is stale at {as_of.date()}")
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(marks.rows_digest_text(frame, columns, as_of).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _mark_replay(
    raw_close: pd.DataFrame,
    index_close: pd.Series,
    target_schedule: pd.DataFrame,
    start,
    end,
    *,
    trailing_stop_fraction: float,
    transaction_cost_bps: float,
) -> pd.DataFrame:
    """r1's sourced hybrid-stop replay, judged only inside holding windows.

    Price jumps outside a stock's holding windows cannot change the value and
    are recorded instead of blocking.  Sourced splits and market moves from the
    supplement join the frozen table.  A held stock whose prices stop inside a
    holding window needs a sourced terminal return; none is ever assumed.
    """
    if trailing_stop_fraction != r1.PORTFOLIO_TRAILING_STOP_FRACTION:
        raise RuntimeError("v50r3 portfolio-stop interface binding changed")
    supplement = _MARK_CONTEXT["supplement"]
    end = pd.Timestamp(end).normalize()
    windows = marks.holding_windows(target_schedule, end)
    terminal = marks.supplement_terminal_returns(supplement)
    ended = marks.unpriced_holdings(raw_close, windows, end)
    unsourced = [
        row
        for row in ended
        if (row["ticker"], pd.Timestamp(row["last_price_date"])) not in terminal
    ]
    if unsourced:
        details = ", ".join(
            f"{row['ticker']} (last close {row['last_price_date']})"
            for row in unsourced
        )
        raise RuntimeError(
            "held positions have no close after their last stored date inside a "
            f"holding window: {details}. Retry once the closes are published; if "
            "trading ended, record a sourced TERMINAL_RETURN with "
            "record-sourced-event"
        )
    validation = corrected_stock_policy.load_corporate_action_validation(
        resolve(VALIDATION_PATH)
    )
    sourced = marks.supplement_validation_rows(supplement)
    if len(sourced):
        _refuse_frozen_table_duplicates(validation, sourced)
        validation = pd.concat([validation, sourced], ignore_index=True)
    ignored: list[dict] = []
    original_events = corrected_stock_policy._unresolved_target_events
    original_returns = corrected_stock_policy.stock_returns_with_delisting_penalty

    def events_inside_holdings(*args, **kwargs) -> pd.DataFrame:
        events = original_events(*args, **kwargs)
        if events.empty:
            return events
        inside = marks.inside_holdings(events, windows)
        ignored.extend(
            {
                "ticker": str(row.ticker),
                "date": f"{pd.Timestamp(row.split_date):%Y-%m-%d}",
                "raw_price_ratio": round(float(row.raw_price_ratio), 6),
            }
            for row in events.loc[~inside].itertuples(index=False)
        )
        return events.loc[inside]

    def sourced_terminal_returns(close, delisting_return=-1.0, terminal_returns=None):
        return original_returns(close, delisting_return, terminal_returns=terminal)

    corrected_stock_policy._unresolved_target_events = events_inside_holdings
    corrected_stock_policy.stock_returns_with_delisting_penalty = (
        sourced_terminal_returns
    )
    try:
        result = corrected_stock_policy.replay_with_sourced_hybrid_stop(
            raw_close,
            index_close,
            target_schedule,
            start,
            end,
            validation=validation,
            entry_loss_fraction=r1.ENTRY_LOSS_FRACTION,
            portfolio_stop_fraction=r1.PORTFOLIO_TRAILING_STOP_FRACTION,
            transaction_cost_bps=transaction_cost_bps,
        )
    finally:
        corrected_stock_policy._unresolved_target_events = original_events
        corrected_stock_policy.stock_returns_with_delisting_penalty = original_returns
    _MARK_CONTEXT["holding_exposure"] = {
        "ignored_price_jumps_outside_holdings": ignored,
        "sourced_terminal_returns_applied": ended,
    }
    return result


def _refuse_frozen_table_duplicates(
    frozen: pd.DataFrame, sourced: pd.DataFrame
) -> None:
    """A sourced event may not re-adjudicate, or double-apply, a frozen one."""
    known = set(zip(frozen["ticker"], frozen["split_date"], strict=True))
    duplicates = [
        f"{row.ticker}@{row.split_date:%Y-%m-%d}"
        for row in sourced.itertuples(index=False)
        if (row.ticker, row.split_date) in known
    ]
    if duplicates:
        raise RuntimeError(
            "sourced events duplicate the frozen corporate-action table: "
            + ", ".join(duplicates)
        )


def _verify_supplement_is_append_only(
    ledger_path: str | Path, supplement: pd.DataFrame
) -> None:
    bindings = [
        event["payload"]["sourced_event_supplement"]
        for event in v43.read_ledger(ledger_path)
        if event["event_type"] == "VALUATION_APPENDED"
        and "sourced_event_supplement" in event["payload"]
    ]
    if not bindings:
        return
    rows = int(bindings[-1]["rows"])
    if len(supplement) < rows or marks.supplement_digest(
        supplement.head(rows)
    ) != bindings[-1]["sha256"]:
        raise RuntimeError(
            "the sourced event supplement no longer begins with the rows an "
            "earlier mark used; it is append-only"
        )


@contextmanager
def _mark_runtime(supplement_path: str | Path = SUPPLEMENT_PATH):
    """Bind v42's valuation to the r3 exposure-aware mark rules."""
    path = resolve(supplement_path)
    supplement = marks.load_supplement(path)
    binding = {
        "path": _portable_path(path),
        "rows": int(len(supplement)),
        "sha256": marks.supplement_digest(supplement),
    }
    original_append = v43.append_event

    def append_event(**kwargs):
        if kwargs.get("event_type") == "VALUATION_APPENDED":
            _verify_supplement_is_append_only(kwargs["path"], supplement)
            kwargs["payload"] = {
                **kwargs["payload"],
                "mark_procedure": MARK_PROCEDURE,
                "sourced_event_supplement": binding,
                "holding_exposure": _MARK_CONTEXT.get("holding_exposure"),
            }
        return original_append(**kwargs)

    replacements = [
        (v42, "_load_mark_market", _load_mark_market),
        (v42, "_market_prefix_sha256", _market_prefix_sha256),
        (v42.v28, "replay_with_individual_trailing_stop", _mark_replay),
        (v43, "append_event", append_event),
    ]
    originals = [(module, name, getattr(module, name)) for module, name, _ in replacements]
    _MARK_CONTEXT.clear()
    _MARK_CONTEXT.update({"supplement": supplement, "binding": binding})
    try:
        for module, name, value in replacements:
            setattr(module, name, value)
        yield _MARK_CONTEXT
    finally:
        for module, name, value in originals:
            setattr(module, name, value)
        _MARK_CONTEXT.clear()


def append_mark(
    *,
    bundle: str | Path,
    protocol_path: str | Path = PROTOCOL_PATH,
    ledger_path: str | Path = LEDGER_PATH,
    signals_dir: str | Path = SIGNALS_DIR,
    lock_path: str | Path = STAGING_LOCK_PATH,
    supplement_path: str | Path = SUPPLEMENT_PATH,
) -> dict:
    with (
        staging_lock(lock_path),
        _runtime(),
        _mark_runtime(supplement_path) as context,
    ):
        result = v43.append_mark(
            bundle=bundle,
            protocol_path=protocol_path,
            ledger_path=ledger_path,
            signals_dir=signals_dir,
        )
        if result.get("written"):
            result["mark_procedure"] = MARK_PROCEDURE
            result["sourced_event_supplement"] = context["binding"]
            result["holding_exposure"] = context.get("holding_exposure")
    result["corrected_hybrid_risk_replay_verified"] = True
    return result


def record_sourced_event(
    *,
    ticker: str,
    event_type: str,
    event_date: str | pd.Timestamp,
    source_url: str,
    adjustment_factor: float | None = None,
    terminal_return: float | None = None,
    note: str = "",
    protocol_path: str | Path = PROTOCOL_PATH,
    supplement_path: str | Path = SUPPLEMENT_PATH,
    price_dir: str | Path = WORK_DIR / "market" / "prices",
    lock_path: str | Path = STAGING_LOCK_PATH,
    now: datetime | None = None,
) -> dict:
    """Append one sourced post-freeze event after checking it against stored prices."""
    protocol, _protocol_sha = _validated_protocol(protocol_path)
    now = schedule.as_utc(now or _utc_now())
    if now < pd.Timestamp(protocol["frozen_at"]):
        raise RuntimeError("sourced events are recorded only after the freeze")
    ticker = str(ticker).strip().upper()
    event_type = str(event_type).strip().upper()
    date = pd.Timestamp(event_date).normalize()
    path = resolve(price_dir) / f"{ticker.lower()}.csv"
    if not path.is_file():
        raise RuntimeError(f"no staged prices for {ticker} at {path}")
    frame = pd.read_csv(path, parse_dates=["date"])
    closes = (
        frame.assign(date=frame["date"].dt.normalize())
        .drop_duplicates("date", keep="last")
        .set_index("date")["close"]
        .sort_index()
        .dropna()
    )
    if event_type in {marks.SPLIT, marks.MARKET_MOVE}:
        _refuse_frozen_table_duplicates(
            corrected_stock_policy.load_corporate_action_validation(
                resolve(VALIDATION_PATH)
            ),
            pd.DataFrame({"ticker": [ticker], "split_date": [date]}),
        )
        jumps = detect_common_split_events(closes.to_frame(ticker))
        match = jumps.loc[pd.to_datetime(jumps["split_date"]).dt.normalize().eq(date)]
        if match.empty:
            raise RuntimeError(
                f"{ticker} has no split-like price jump on {date:%Y-%m-%d} in {path}"
            )
        ratio = float(match.iloc[0]["raw_price_ratio"])
        if event_type == marks.SPLIT:
            if adjustment_factor is None:
                raise ValueError("a SPLIT needs its adjustment factor")
            if abs(ratio / float(adjustment_factor) - 1.0) > marks.JUMP_TOLERANCE:
                raise RuntimeError(
                    f"{ticker}'s stored jump ratio {ratio:.4f} does not match the "
                    f"adjustment factor {adjustment_factor}"
                )
        evidence = {"stored_price_ratio": ratio}
    elif event_type == marks.TERMINAL_RETURN:
        if terminal_return is None:
            raise ValueError("a TERMINAL_RETURN needs its terminal return")
        last = pd.Timestamp(closes.index.max()).normalize()
        if last != date:
            raise RuntimeError(
                f"{ticker}'s last stored close is {last:%Y-%m-%d}, not {date:%Y-%m-%d}"
            )
        completed = schedule.latest_completed_session(now)
        if completed is None or completed <= date:
            raise RuntimeError(
                f"no completed session after {date:%Y-%m-%d} has passed without "
                f"a {ticker} close yet"
            )
        evidence = {"last_stored_close": float(closes.iloc[-1])}
    else:
        raise ValueError(f"unsupported sourced event type: {event_type}")
    with staging_lock(lock_path):
        row = marks.append_supplement_event(
            resolve(supplement_path),
            {
                "recorded_at": _iso(now),
                "ticker": ticker,
                "event_type": event_type,
                "event_date": f"{date:%Y-%m-%d}",
                "adjustment_factor": (
                    None if adjustment_factor is None else repr(float(adjustment_factor))
                ),
                "terminal_return": (
                    None if terminal_return is None else repr(float(terminal_return))
                ),
                "source_url": source_url,
                "note": note,
            },
        )
    return {
        "status": "SOURCED_EVENT_RECORDED",
        "event": row,
        "evidence": evidence,
        "supplement": _portable_path(resolve(supplement_path)),
        "next_step": "commit the supplement; the next mark binds it",
    }


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
    event_parser = subparsers.add_parser(
        "record-sourced-event",
        help="append a sourced split, market move, or terminal return",
    )
    event_parser.add_argument("--ticker", required=True)
    event_parser.add_argument("--type", required=True, choices=list(marks.EVENT_TYPES))
    event_parser.add_argument(
        "--date", required=True,
        help="jump date (first close in new units) or, for TERMINAL_RETURN, "
        "the last stored close",
    )
    event_parser.add_argument("--factor", type=float, help="SPLIT price factor, e.g. 0.5")
    event_parser.add_argument("--terminal-return", type=float)
    event_parser.add_argument("--source-url", required=True)
    event_parser.add_argument("--note", default="")
    subparsers.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "status" and not resolve(PROTOCOL_PATH).is_file():
        print(json.dumps({
            "status": "PROTOCOL_NOT_FROZEN",
            "model_version": MODEL_VERSION,
            "protocol_path": PROTOCOL_PATH.as_posix(),
            "live_branch": LIVE_BRANCH,
            "release_status": "BLOCKED",
        }, indent=2, sort_keys=True))
        return 3
    if args.command != "status":
        require_live_checkout()
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
    elif args.command == "record-sourced-event":
        result = record_sourced_event(
            ticker=args.ticker,
            event_type=args.type,
            event_date=args.date,
            adjustment_factor=args.factor,
            terminal_return=args.terminal_return,
            source_url=args.source_url,
            note=args.note,
        )
    else:
        result = status()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
