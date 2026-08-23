#!/usr/bin/env python3
"""Read-only readiness audit for v6 research-only forward inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.research.shadow_evaluation import nasdaq_calendar_for_year
from src.research.universe_history import load_universe_snapshots, universe_as_of


DEFAULT_SUMMARY = Path("output/research_v6_walkforward_defensive_ensemble_shadow_summary.json")
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_PRICE_DIR = Path("output/research_only/v6_market/prices")
DEFAULT_INDEX = Path("output/research_only/v6_market/nasdaq_index.csv")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _latest_date(path: Path) -> pd.Timestamp | None:
    if not path.is_file():
        return None
    dates = pd.read_csv(path, usecols=["date"])["date"]
    return pd.to_datetime(dates.iloc[-1]).normalize() if len(dates) else None


def _session_staleness(last: pd.Timestamp, expected: pd.Timestamp) -> int:
    sessions = []
    for year in range(last.year, expected.year + 1):
        calendar = nasdaq_calendar_for_year(year)
        start = max(last, pd.Timestamp(year, 1, 1))
        end = min(expected, pd.Timestamp(year, 12, 31))
        sessions.extend(calendar.sessions_in_range(start, end))
    normalized = pd.DatetimeIndex(sessions).tz_localize(None).normalize()
    return int((normalized > last).sum())


def build_readiness(
    *,
    expected_session: str | pd.Timestamp,
    summary_path: Path = DEFAULT_SUMMARY,
    price_dir: Path = DEFAULT_PRICE_DIR,
    index_path: Path = DEFAULT_INDEX,
    qqq_path: Path = DEFAULT_QQQ,
    universe: list[str] | None = None,
) -> dict:
    expected = pd.Timestamp(expected_session).normalize()
    calendar = nasdaq_calendar_for_year(expected.year)
    if expected not in pd.DatetimeIndex(
        calendar.sessions_in_range(expected, expected)
    ).tz_localize(None).normalize():
        raise ValueError("expected_session is not a Nasdaq trading session")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    runtime = summary["bindings"]["runtime_code"]
    runtime_checks = {
        path: Path(path).is_file() and _sha256(Path(path)) == sha
        for path, sha in runtime.items()
    }
    quarterly = summary["quarterly_input"]
    quarterly_path = Path(quarterly["path"])
    quarterly_ok = quarterly_path.is_file() and _sha256(quarterly_path) == quarterly["sha256"]
    index_latest = _latest_date(index_path)
    qqq_latest = _latest_date(qqq_path)
    symbols = sorted(
        universe
        if universe is not None
        else (universe_as_of(load_universe_snapshots(), expected) or [])
    )
    missing = []
    stale = []
    exact = 0
    within_five = 0
    for ticker in symbols:
        path = price_dir / f"{ticker.lower()}.csv"
        last = _latest_date(path)
        if last is None:
            missing.append(ticker)
            continue
        sessions = _session_staleness(last, expected) if last < expected else 0
        if last >= expected:
            exact += 1
        else:
            stale.append({
                "ticker": ticker,
                "latest_date": last.strftime("%Y-%m-%d"),
                "stale_sessions": sessions,
            })
        if sessions <= 5:
            within_five += 1
    count = len(symbols)
    exact_fraction = exact / count if count else 0.0
    within_five_fraction = within_five / count if count else 0.0
    gates = {
        "nasdaq_index_through_expected_session": index_latest is not None
        and index_latest >= expected,
        "qqq_through_expected_session": qqq_latest is not None and qqq_latest >= expected,
        "active_universe_has_no_missing_price_file": not missing,
        "active_universe_exact_session_coverage_at_least_98pct": exact_fraction >= 0.98,
        "active_universe_within_five_sessions_at_least_99_9pct": within_five_fraction >= 0.999,
        "frozen_quarterly_sha_verified": quarterly_ok,
        "runtime_code_sha_verified": bool(runtime_checks) and all(runtime_checks.values()),
    }
    return {
        "schema_version": 1,
        "research_only": True,
        "expected_session": expected.strftime("%Y-%m-%d"),
        "ready_for_v6_signal": bool(all(gates.values())),
        "gates": gates,
        "unsatisfied_gates": [name for name, passed in gates.items() if not passed],
        "nasdaq_latest_date": index_latest.strftime("%Y-%m-%d") if index_latest is not None else None,
        "qqq_latest_date": qqq_latest.strftime("%Y-%m-%d") if qqq_latest is not None else None,
        "active_universe": {
            "symbols": count,
            "missing_price_files": len(missing),
            "missing_sample": missing[:20],
            "exact_expected_session": exact,
            "exact_expected_session_fraction": exact_fraction,
            "within_five_sessions": within_five,
            "within_five_sessions_fraction": within_five_fraction,
            "stale_price_files": len(stale),
            "stale_sample": stale[:20],
        },
        "bindings": {
            "quarterly_verified": quarterly_ok,
            "runtime_code_checks": runtime_checks,
            "summary_sha256": _sha256(summary_path),
        },
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-session", required=True)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_PRICE_DIR)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--qqq", type=Path, default=DEFAULT_QQQ)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = build_readiness(
        expected_session=args.expected_session,
        summary_path=args.summary,
        price_dir=args.price_dir,
        index_path=args.index,
        qqq_path=args.qqq,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
