"""Audit retained price histories that end before the benchmark series."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_300M_STOCK_LIST_FILE, NASDAQ_INDEX_FILE, PROJECT_PATH
from src.io.financial_update import investable_common_equities
from src.io.terminal_returns import load_observed_terminal_returns
from src.research.universe_history import (
    known_non_common_symbols,
    load_universe_snapshots,
    snapshot_coverage,
)


def audit_snapshot_price_coverage(
    snapshots: dict[pd.Timestamp, set[str]],
    start: str = "2024-10-05",
    maximum_price_lag_days: int = 7,
    minimum_lookback_rows: int = 253,
) -> dict:
    """Measure whether every dated member was actually observable then."""
    metadata = {}
    for path in sorted(Path(CLEANED_PRICE_DATA_DIR).glob("*.csv")):
        dates = pd.read_csv(path, usecols=["date"], parse_dates=["date"])["date"].dropna()
        if len(dates):
            metadata[path.stem.upper()] = dates.sort_values().reset_index(drop=True)
    rows = []
    missing_union: set[str] = set()
    for observed_at, members in sorted(snapshots.items()):
        if observed_at < pd.Timestamp(start):
            continue
        current, lookback_ready = set(), set()
        for ticker in members:
            dates = metadata.get(ticker)
            if dates is None:
                continue
            known = dates.loc[dates <= observed_at]
            if known.empty or (observed_at - known.iloc[-1]).days > maximum_price_lag_days:
                continue
            current.add(ticker)
            if len(known) >= minimum_lookback_rows:
                lookback_ready.add(ticker)
        missing = members - current
        missing_union.update(missing)
        rows.append({
            "observed_at": observed_at.strftime("%Y-%m-%d"),
            "members": len(members),
            "price_current": len(current),
            "price_current_coverage": len(current) / max(len(members), 1),
            "lookback_ready": len(lookback_ready),
            "lookback_ready_coverage": len(lookback_ready) / max(len(members), 1),
            "missing_price_count": len(missing),
        })
    minimum_current = min((row["price_current_coverage"] for row in rows), default=0.0)
    minimum_lookback = min((row["lookback_ready_coverage"] for row in rows), default=0.0)
    return {
        "start": start,
        "maximum_price_lag_days": maximum_price_lag_days,
        "minimum_lookback_rows": minimum_lookback_rows,
        "snapshots_tested": len(rows),
        "minimum_price_current_coverage": minimum_current,
        "minimum_lookback_ready_coverage": minimum_lookback,
        # Recent IPOs legitimately have fewer than 253 observations and are
        # excluded by the strategy until mature. Completeness means their
        # observed history is present, not that every listing is signal-ready.
        "complete": bool(rows) and minimum_current == 1.0,
        "missing_price_symbols": sorted(missing_union),
        "by_snapshot": rows,
    }


def audit_historical_price_terminations() -> dict:
    benchmark = pd.read_csv(NASDAQ_INDEX_FILE, usecols=["date"], parse_dates=["date"])
    benchmark_end = benchmark["date"].max()
    current = investable_common_equities(pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE))
    current_symbols = set(current["Symbol"].dropna().astype(str).str.upper())
    universe_snapshots = load_universe_snapshots()
    last_membership = {
        ticker: observed_at
        for observed_at, members in sorted(universe_snapshots.items())
        for ticker in members
    }
    ended = []
    price_paths = sorted(Path(CLEANED_PRICE_DATA_DIR).glob("*.csv"))
    for path in price_paths:
        dates = pd.read_csv(path, usecols=["date"], parse_dates=["date"])["date"]
        if dates.empty:
            continue
        latest = dates.max()
        if latest < benchmark_end:
            ticker = path.stem.upper()
            last_listed = last_membership.get(ticker)
            listed_after_price_end = bool(
                last_listed is not None and last_listed > latest + pd.Timedelta(days=7)
            )
            ended.append({
                "ticker": ticker,
                "last_price_date": latest.strftime("%Y-%m-%d"),
                "last_membership_date": (
                    last_listed.strftime("%Y-%m-%d") if last_listed is not None else None
                ),
                "listed_after_price_end": listed_after_price_end,
                "in_current_common_equity_universe": ticker in current_symbols,
            })
    by_date = Counter(item["last_price_date"] for item in ended)
    current_ended = [item for item in ended if item["in_current_common_equity_universe"]]
    known_non_common = known_non_common_symbols()
    research_common_ended = [item for item in ended if item["ticker"] not in known_non_common]
    excluded_non_common_ended = [item for item in ended if item["ticker"] in known_non_common]
    missing_price_while_listed = [
        item for item in research_common_ended if item["listed_after_price_end"]
    ]
    candidate_terminal_histories = [
        item for item in research_common_ended if not item["listed_after_price_end"]
    ]
    observed = load_observed_terminal_returns()
    observed_keys = {
        (row.ticker, row.last_price_date.strftime("%Y-%m-%d"))
        for row in observed.itertuples(index=False)
    }
    for item in candidate_terminal_histories:
        item["observed_terminal_return"] = (
            item["ticker"], item["last_price_date"]
        ) in observed_keys
    unresolved_common_ended = [
        item for item in candidate_terminal_histories if not item["observed_terminal_return"]
    ]
    universe_coverage = snapshot_coverage(
        universe_snapshots, "2021-01-01", benchmark_end.strftime("%Y-%m-%d")
    )
    recent_universe_coverage = snapshot_coverage(
        universe_snapshots, "2024-10-05", benchmark_end.strftime("%Y-%m-%d")
    )
    snapshot_price_coverage = audit_snapshot_price_coverage(universe_snapshots)
    report = {
        "status": "INCOMPLETE_STRESSED" if unresolved_common_ended else "PASS",
        "benchmark_latest_date": benchmark_end.strftime("%Y-%m-%d"),
        "retained_price_files": len(price_paths),
        "histories_ending_early": len(ended),
        "current_common_equities_ending_early": len(current_ended),
        "termination_dates": dict(sorted(by_date.items())),
        "research_common_equity_histories_ending_early": len(research_common_ended),
        "missing_price_histories_while_still_listed": len(missing_price_while_listed),
        "candidate_terminal_histories": len(candidate_terminal_histories),
        "observed_terminal_returns": len(candidate_terminal_histories) - len(unresolved_common_ended),
        "unresolved_terminal_returns": len(unresolved_common_ended),
        "excluded_non_common_histories_ending_early": len(excluded_non_common_ended),
        "delisting_returns_complete": not unresolved_common_ended,
        "universe_snapshot_coverage": universe_coverage,
        "universe_snapshot_coverage_from_2024_10_05": recent_universe_coverage,
        "snapshot_price_coverage_from_2024_10_05": snapshot_price_coverage,
        "point_in_time_universe_complete_from_2021": universe_coverage["full_period_covered"],
        "backtest_policy": (
            "first session after final observed price receives its sourced terminal return; "
            "unresolved rows receive -100% only in incomplete-data stress diagnostics"
        ),
        "ended_histories": ended,
        "research_common_equity_ended_histories": research_common_ended,
        "missing_price_while_listed_histories": missing_price_while_listed,
        "candidate_terminal_history_rows": candidate_terminal_histories,
        "unresolved_terminal_return_histories": unresolved_common_ended,
    }
    output = Path(PROJECT_PATH) / "output/historical_data_audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def backtest_data_readiness(start: str, end: str) -> dict:
    """Return authoritative preflight evidence for a historical backtest."""
    snapshots = load_universe_snapshots()
    membership = snapshot_coverage(snapshots, start, end)
    prices = audit_snapshot_price_coverage(snapshots, start=start)
    history = audit_historical_price_terminations()
    checks = {
        "point_in_time_membership_complete": membership["full_period_covered"],
        "snapshot_member_prices_complete": prices["complete"],
        "observed_delisting_returns_complete": history["delisting_returns_complete"],
    }
    return {
        "start": start,
        "end": end,
        "checks": checks,
        "complete": all(checks.values()),
        "universe_snapshot_coverage": membership,
        "snapshot_price_coverage": prices,
        "research_common_equity_histories_ending_early": history[
            "research_common_equity_histories_ending_early"
        ],
        "missing_price_histories_while_still_listed": history[
            "missing_price_histories_while_still_listed"
        ],
        "unresolved_terminal_returns": history["unresolved_terminal_returns"],
    }


def require_complete_backtest_data(start: str, end: str) -> dict:
    readiness = backtest_data_readiness(start, end)
    if not readiness["complete"]:
        failed = [name for name, passed in readiness["checks"].items() if not passed]
        missing_prices = len(readiness["snapshot_price_coverage"]["missing_price_symbols"])
        raise RuntimeError(
            "Backtest data is incomplete; refusing to produce a validation result. "
            f"Failed checks: {', '.join(failed)}; missing historical price symbols: "
            f"{missing_prices}; ended histories without observed terminal returns: "
            f"{readiness.get('unresolved_terminal_returns', readiness['research_common_equity_histories_ending_early'])}. "
            "Use the explicit incomplete-data research option only for diagnostics."
        )
    return readiness


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    report = audit_historical_price_terminations()
    compact = {
        key: value
        for key, value in report.items()
        if key not in {"ended_histories", "research_common_equity_ended_histories"}
    }
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
