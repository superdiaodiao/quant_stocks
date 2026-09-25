#!/usr/bin/env python3
"""Rehearse the exact v50r3 SIGNAL staging and selection on a completed session.

A prospective SIGNAL must be staged and frozen inside its window (30 minutes
after the month-end close until pre-market trading opens on the next session),
and the v51 August recovery needed nine attempts because gates that nobody had
exercised failed one after another.  This script runs the r3 staging path
(current universe, price and index downloads, QQQ, the isolated SEC refresh,
the readiness gates) and the r3 selector on a completed session in a scratch
directory, times each phase, and reports what would have failed and whether
the run fits the next window with a retry to spare.

It never writes the r3 ledger, signals, bundles, or formal data.  The targets
it prints are diagnostics for a non-month-end session, not a signal.  Run it
after a US close (20:30 UTC or later during daylight time) on the machine,
network, and worker counts the real run will use, and never inside a
month-end window: it holds the same staging lock as the real run.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

import pandas as pd

from scripts import research_v24_stock_momentum_development as v24
from scripts import research_v42_prospective_v28_observation as v42
from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50r3_corrected_v47 as r3
from src.io.security_universe import investable_common_equities
from src.research import prospective_schedule as schedule
from src.research import prospective_marks as marks
from src.research.corrected_stock_policy import large_liquid_ranking


REHEARSAL_ROOT = Path("output/research_only/v50/r3_rehearsals")
# Do not hold the staging lock when a real month-end window is about to open.
QUIET_BEFORE_WINDOW = timedelta(hours=3)
# A window should hold the measured staging time twice: one retry.
ATTEMPTS_TO_FIT = 2
TIMED_FUNCTIONS = {
    "universe_refresh": (v42, "refresh_universe"),
    "price_seed": (v42, "seed_cache"),
    "price_update": (v42, "update_all"),
    "index_reconcile": (v42, "reconcile_research_index"),
    "qqq_refresh": (v43, "_trim_qqq"),
}


def _timed(name: str, function, timings: dict):
    def wrapper(*args, **kwargs):
        started = time.monotonic()
        try:
            result = function(*args, **kwargs)
        except BaseException:
            timings[name] = {
                "seconds": round(time.monotonic() - started, 1),
                "status": "FAILED",
            }
            raise
        timings[name] = {
            "seconds": round(time.monotonic() - started, 1),
            "status": "OK",
        }
        return result

    return wrapper


@contextmanager
def _phase_timers(timings: dict):
    original = {name: getattr(module, attribute)
                for name, (module, attribute) in TIMED_FUNCTIONS.items()}
    try:
        for name, (module, attribute) in TIMED_FUNCTIONS.items():
            setattr(module, attribute, _timed(name, original[name], timings))
        yield
    finally:
        for name, (module, attribute) in TIMED_FUNCTIONS.items():
            setattr(module, attribute, original[name])


@contextmanager
def _rehearsal_runtime(as_of: pd.Timestamp, timings: dict | None = None):
    with ExitStack() as stack:
        # The same lift a catch-up SIGNAL uses, for the rehearsal session.
        stack.enter_context(r3.signal_session(as_of))
        if timings is not None:
            stack.enter_context(_phase_timers(timings))
        stack.enter_context(r3._runtime(rehearsal=True))
        if timings is not None:
            # Installed after r3._runtime so its restore also undoes this.
            v43._refresh_fundamentals_isolated = _timed(
                "fundamentals_refresh", r3._refresh_fundamentals_isolated, timings
            )
        yield


def blocking_window(now: datetime) -> dict | None:
    """Return the month-end window a rehearsal would compete with, if any."""
    now = schedule.as_utc(now)
    today = pd.Timestamp(now.date())
    recent = schedule.month_end_sessions(
        today - pd.Timedelta(days=schedule.NEXT_SESSION_SEARCH_DAYS), today
    )
    for session in recent:
        opens, closes = schedule.signal_window(session)
        if opens - QUIET_BEFORE_WINDOW <= now < closes:
            return {
                "signal_date": f"{session:%Y-%m-%d}",
                "opens": opens.isoformat(timespec="seconds"),
                "closes": closes.isoformat(timespec="seconds"),
            }
    return None


def next_signal_date(now: datetime) -> pd.Timestamp:
    protocol_path = r3.resolve(r3.PROTOCOL_PATH)
    if protocol_path.is_file():
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        first, _missed = r3.signal_dates(protocol)
        if schedule.signal_window(first)[0] > now:
            return first
    return schedule.first_signal_date_after(now, r3.EARLIEST_PROSPECTIVE_SIGNAL_DATE)


def window_fit(staging_seconds: float, signal_date: pd.Timestamp) -> dict:
    opens, closes = schedule.signal_window(signal_date)
    window_minutes = (closes - opens).total_seconds() / 60.0
    staging_minutes = staging_seconds / 60.0
    return {
        "signal_date": f"{signal_date:%Y-%m-%d}",
        "window_opens_utc": opens.isoformat(timespec="seconds"),
        "window_closes_utc": closes.isoformat(timespec="seconds"),
        "window_minutes": round(window_minutes, 1),
        "staging_minutes": round(staging_minutes, 1),
        "attempts_that_fit": (
            math.floor(window_minutes / staging_minutes)
            if staging_minutes > 0 else None
        ),
        "fits_with_one_retry": (
            staging_minutes * ATTEMPTS_TO_FIT <= window_minutes
        ),
        "latest_start_for_one_attempt_utc": (
            closes - timedelta(seconds=staging_seconds)
        ).isoformat(timespec="seconds"),
    }


def price_coverage(work: Path, as_of: pd.Timestamp) -> dict:
    """Which staged universe names lack a price file or the as-of close."""
    universe_path = work / "market" / "current_universe.csv"
    if not universe_path.is_file():
        return {"universe_staged": False}
    current = investable_common_equities(
        pd.read_csv(universe_path, keep_default_na=False)
    )
    tickers = sorted(
        set(current["Symbol"].dropna().astype(str).str.upper()) - v42.FORBIDDEN_ETFS
    )
    price_dir = work / "market" / "prices"
    missing, stale = [], []
    for ticker in tickers:
        path = price_dir / f"{ticker.lower()}.csv"
        if not path.is_file():
            missing.append(ticker)
            continue
        latest = v42._latest_date(path)
        if latest != as_of:
            stale.append({
                "ticker": ticker,
                "latest_date": None if latest is None else f"{latest:%Y-%m-%d}",
            })
    exact = len(tickers) - len(missing) - len(stale)
    return {
        "universe_staged": True,
        "universe_ticker_count": len(tickers),
        "missing_price_files": missing,
        "without_as_of_close_count": len(stale),
        "without_as_of_close_sample": stale[:50],
        "exact_as_of_fraction": round(exact / len(tickers), 4) if tickers else None,
        "all_price_files_present_gate": not missing,
        "exact_at_least_98pct_gate": bool(tickers) and exact / len(tickers) >= 0.98,
    }


def ranked_pool_corporate_actions(bundle: Path, as_of: pd.Timestamp) -> dict:
    """Split-like moves in the ranked pool that nothing explains.

    Prices are the live SIGNAL's: the frozen table, the supplement and the
    provider rescalings measured by the price updates.  The staging gate
    already refuses such a move for every pool candidate; this reports the
    pool's own view next to the selection.
    """
    validation, events = r3._live_validation(
        provider=r3._bundle_provider_adjustments(bundle),
        supplement=r3._bundle_supplement(bundle),
    )
    inputs = r3._signal_inputs(bundle, as_of, validation)
    spec = r3._selected_model()["selector_specification"]
    ranking = large_liquid_ranking(as_of, spec, inputs)
    pool = [str(ticker) for ticker in ranking.index]
    close = inputs["close"]
    position = int(close.index.get_loc(as_of))
    relevant = max(int(spec["lookback_sessions"]), v24.STOCK_MA_DAYS - 1)
    window_start = close.index[max(0, position - relevant)]
    moves = marks.unexplained_moves(
        inputs["raw_close"], validation, pool, r3.review_start(validation, window_start),
        as_of,
    )
    return {
        "ranked_liquid_pool": pool,
        "window_start": f"{window_start:%Y-%m-%d}",
        "validation_last_reviewed_date": (
            f"{validation['split_date'].max():%Y-%m-%d}" if len(validation) else None
        ),
        "price_events": events,
        "identity_breaks": inputs.get("identity_breaks", {}),
        "unreviewed_split_like_jumps": [
            {
                "ticker": row.ticker,
                "date": f"{pd.Timestamp(row.split_date):%Y-%m-%d}",
                "raw_price_ratio": round(float(row.raw_price_ratio), 4),
                "reason": row.reason,
            }
            for row in moves.itertuples(index=False)
        ],
    }


def _error(exc: BaseException) -> dict:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback_tail": traceback.format_exc().strip().splitlines()[-6:],
    }


def _git_state() -> dict:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=r3.REPO_ROOT, capture_output=True, text=True
        ).stdout.strip()

    return {"head": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def rehearse(
    *,
    as_of: pd.Timestamp,
    scratch: Path,
    workers: int,
    fundamental_workers: int,
    now: datetime,
    lock_path: str | Path = r3.STAGING_LOCK_PATH,
) -> dict:
    as_of = pd.Timestamp(as_of).normalize()
    closure = r3.current_code_closure()
    report: dict = {
        "schema_version": 1,
        "research_only": True,
        "rehearsal_of": r3.MODEL_VERSION,
        "as_of": f"{as_of:%Y-%m-%d}",
        "started_at": now.isoformat(timespec="seconds"),
        "scratch_dir": r3._portable_path(scratch),
        "workers": workers,
        "fundamental_workers": fundamental_workers,
        "code_closure_sha256": closure["sha256"],
        "code_closure_file_count": closure["file_count"],
        "git": _git_state(),
        "runtime_environment": r3.runtime_environment(),
        "ledger_written": False,
        "signal_written": False,
        "not_a_prospective_signal": True,
    }
    frozen_path = r3.resolve(r3.PROTOCOL_PATH)
    if frozen_path.is_file():
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
        report["matches_frozen_r3_closure"] = (
            frozen.get("code_closure", {}).get("sha256") == closure["sha256"]
        )
    timings: dict = {}
    report["phases"] = timings
    bundles = scratch / "bundles"
    work = scratch / "work"
    bundle = bundles / f"{as_of:%Y-%m-%d}_signal"
    failures: list[str] = []
    warnings: list[str] = []

    started = time.monotonic()
    try:
        with r3.staging_lock(lock_path), _rehearsal_runtime(as_of, timings):
            staged = v43.stage_bundle(
                as_of=as_of,
                purpose="SIGNAL",
                bundles_dir=bundles,
                work_dir=work,
                signals_dir=scratch / "signals",
                ledger_path=scratch / "no_ledger.jsonl",
                workers=workers,
                fundamental_workers=fundamental_workers,
            )
        report["staging"] = {"status": staged.get("status")}
    except r3.StagingInProgress as exc:
        report["staging"] = {"status": "LOCKED", "error": _error(exc)}
        failures.append("another v50r3 staging holds the lock")
    except BaseException as exc:  # a rehearsal reports every failure mode
        if isinstance(exc, KeyboardInterrupt):
            raise
        report["staging"] = {"status": "FAILED", "error": _error(exc)}
        failures.append(f"staging failed: {type(exc).__name__}: {exc}")
    staging_seconds = time.monotonic() - started
    report["staging_seconds"] = round(staging_seconds, 1)
    report["price_coverage"] = price_coverage(work, as_of)
    missing = report["price_coverage"].get("missing_price_files") or []
    if missing:
        failures.append(f"{len(missing)} universe names have no price file")

    if report["staging"]["status"] == "FROZEN_ISOLATED_INPUT_BUNDLE":
        with _rehearsal_runtime(as_of):
            manifest, manifest_sha = r3._validated_bundle_contents(bundle, "SIGNAL")
            refresh = manifest.get("fundamentals_refresh") or {}
            report["readiness_gates"] = manifest.get("readiness_gates")
            report["fundamentals"] = {
                "sec_unmapped_policy": refresh.get("sec_unmapped_policy"),
                "as_of_filter": refresh.get("as_of_filter"),
                "refresh_elapsed_seconds": refresh.get("refresh_elapsed_seconds"),
                "deferred_by_limit_ticker_count": refresh.get(
                    "deferred_by_limit_ticker_count"
                ),
                "failure_count": len(refresh.get("failures") or []),
                "failure_sample": (refresh.get("failures") or [])[:20],
            }
            created_at = pd.Timestamp(manifest["created_at"]).tz_convert("UTC")
            opens, closes = schedule.staging_window(as_of)
            report["bundle_created_at"] = created_at.isoformat()
            report["created_inside_session_window"] = (
                opens <= created_at.to_pydatetime() < closes
            )
            try:
                protocol = {
                    "model": r3._selected_model(),
                    "code_closure": {"sha256": closure["sha256"]},
                }
                payload = r3._build_signal_payload(
                    signal_date=as_of,
                    bundle=bundle,
                    protocol=protocol,
                    protocol_sha="0" * 64,
                    manifest_sha=manifest_sha,
                )
                report["selection"] = {
                    "status": "OK",
                    "market_regime_on": payload["market_regime_on"],
                    "selected_count": payload["selected_count"],
                    "diagnostic_targets": payload["targets"],
                }
            except Exception as exc:
                report["selection"] = {"status": "FAILED", "error": _error(exc)}
                failures.append(f"selection failed: {type(exc).__name__}: {exc}")
        try:
            scan = ranked_pool_corporate_actions(bundle, as_of)
            report["corporate_actions"] = scan
            if scan["unreviewed_split_like_jumps"]:
                warnings.append(
                    "ranked pool has split-like jumps the frozen validation table "
                    "never reviewed; momentum may be distorted"
                )
        except Exception as exc:
            report["corporate_actions"] = {"status": "FAILED", "error": _error(exc)}
            warnings.append(f"corporate-action scan failed: {exc}")
        policy = (report.get("fundamentals") or {}).get("sec_unmapped_policy") or {}
        if policy.get("unmapped_ticker_count"):
            warnings.append(
                f"{policy['unmapped_ticker_count']} universe names have no SEC CIK "
                "and keep their prior fundamentals (not refreshed)"
            )

    fit = window_fit(staging_seconds, next_signal_date(now))
    report["next_window_fit"] = fit
    if report["staging"]["status"] == "FROZEN_ISOLATED_INPUT_BUNDLE" and not fit[
        "fits_with_one_retry"
    ]:
        warnings.append(
            "one staging takes more than half of the next SIGNAL window; start "
            "at the window opening and raise workers if the sources allow"
        )
    report["failures"] = failures
    report["warnings"] = warnings
    report["verdict"] = "FAIL" if failures else ("WARN" if warnings else "PASS")
    report["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return report


def default_as_of(now: datetime) -> pd.Timestamp:
    session = schedule.latest_completed_session(now)
    if session is None:
        raise RuntimeError("no completed Nasdaq session in the last ten days")
    return session


def main(argv: list[str] | None = None) -> int:
    os.chdir(r3.REPO_ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        help="completed Nasdaq session to rehearse (default: latest completed)",
    )
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--fundamental-workers", type=int, default=4)
    parser.add_argument("--scratch-root", type=Path, default=REHEARSAL_ROOT)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete the scratch staging directory after writing the report",
    )
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    blocking = blocking_window(now)
    if blocking is not None:
        print(json.dumps({
            "verdict": "REFUSED",
            "reason": (
                "a real month-end SIGNAL window is open or opens within "
                f"{QUIET_BEFORE_WINDOW}; the rehearsal would hold its lock"
            ),
            "window": blocking,
        }, indent=2, sort_keys=True))
        return 1
    as_of = (
        pd.Timestamp(args.as_of).normalize() if args.as_of else default_as_of(now)
    )
    if not schedule.is_session(as_of):
        raise SystemExit(f"{as_of:%Y-%m-%d} is not a Nasdaq session")
    completed = schedule.latest_completed_session(now)
    if completed is None or as_of > completed:
        raise SystemExit(f"{as_of:%Y-%m-%d} has not closed (+30 min) yet")
    name = f"{as_of:%Y-%m-%d}_{now:%Y%m%dT%H%M%SZ}"
    root = r3.resolve(args.scratch_root)
    scratch = root / name
    scratch.mkdir(parents=True, exist_ok=False)
    report = rehearse(
        as_of=as_of,
        scratch=scratch,
        workers=args.workers,
        fundamental_workers=args.fundamental_workers,
        now=now,
    )
    report_path = root / f"{name}_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    if args.cleanup:
        shutil.rmtree(scratch)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(f"report: {r3._portable_path(report_path)}", file=sys.stderr)
    return 1 if report["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
