#!/usr/bin/env python3
"""Audit PIT inputs required before fitting an adaptive v14 walk-forward."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.financial.quarterly_fundamentals import (
    load_quarterly_fundamentals,
    quarterly_growth_snapshot,
)
from src.research.shadow_evaluation import nasdaq_calendar_for_year
from src.research.universe_history import load_universe_snapshots, universe_as_of


DEFAULT_QUARTERLY = Path(
    "output/data_provenance/companyfacts_proven_only_manifest-"
    "6c8a87fcc71cfcd5-recipe-6f0998be-q1-fp-guard-bank-duration-v3/quarterly.csv"
)
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_PREFIX = Path("output/research_v14_adaptive_data_audit")


def _snapshot_binding(snapshot_dir: Path | None) -> dict:
    if snapshot_dir is None:
        return {"path": "formal_default", "files": None}
    files = [
        {"path": str(path), "sha256": _sha256(path)}
        for path in sorted(snapshot_dir.glob("nasdaq_listed_*.csv"))
    ]
    return {"path": str(snapshot_dir), "files": files}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def monthly_decision_sessions(
    sessions: pd.DatetimeIndex, start: str, end: str
) -> pd.DatetimeIndex:
    bounded = sessions[
        (sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))
    ]
    if not len(bounded):
        return pd.DatetimeIndex([])
    series = pd.Series(bounded, index=bounded)
    return pd.DatetimeIndex(series.groupby(bounded.to_period("M")).max())


def snapshot_age(
    snapshots: dict[pd.Timestamp, set[str]], decision: pd.Timestamp
) -> tuple[pd.Timestamp | None, int | None, set[str] | None]:
    eligible = [stamp for stamp in snapshots if stamp <= decision]
    if not eligible:
        return None, None, None
    stamp = max(eligible)
    return stamp, int((decision - stamp).days), snapshots[stamp]


def price_bounds(price_dir: Path) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    bounds = {}
    for path in sorted(price_dir.glob("*.csv")):
        try:
            dates = pd.to_datetime(
                pd.read_csv(path, usecols=["date"])["date"], errors="coerce"
            ).dropna()
        except (OSError, ValueError):
            continue
        if len(dates):
            bounds[path.stem.upper()] = (dates.min().normalize(), dates.max().normalize())
    return bounds


def training_windows(effective_year: int) -> dict[str, dict[str, str]]:
    train_end = pd.Timestamp(effective_year - 1, 12, 31)
    return {
        "v6_36_month": {
            "start": pd.Timestamp(effective_year - 3, 1, 1).strftime("%Y-%m-%d"),
            "end": train_end.strftime("%Y-%m-%d"),
        },
        "v7_4_completed_year": {
            "start": pd.Timestamp(effective_year - 4, 1, 1).strftime("%Y-%m-%d"),
            "end": train_end.strftime("%Y-%m-%d"),
        },
    }


def build_audit(
    *,
    start: str = "2015-01-01",
    end: str = "2021-12-31",
    maximum_snapshot_age_days: int = 30,
    price_warmup_days: int = 400,
    maximum_financial_age_days: int = 550,
    price_dir: Path = Path(CLEANED_PRICE_DATA_DIR),
    index_path: Path = Path(NASDAQ_INDEX_FILE),
    qqq_path: Path = DEFAULT_QQQ,
    quarterly_path: Path = DEFAULT_QUARTERLY,
    snapshot_dir: Path | None = None,
    include_financial: bool = True,
) -> tuple[pd.DataFrame, dict]:
    sessions = []
    for year in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1):
        calendar = nasdaq_calendar_for_year(year)
        sessions.extend(calendar.sessions_in_range(
            pd.Timestamp(year, 1, 1), pd.Timestamp(year, 12, 31)
        ))
    sessions = pd.DatetimeIndex(sessions).tz_localize(None).normalize()
    decisions = monthly_decision_sessions(sessions, start, end)
    index_dates = set(pd.read_csv(index_path, parse_dates=["date"])["date"].dt.normalize())
    qqq_dates = set(pd.read_csv(qqq_path, parse_dates=["date"])["date"].dt.normalize())
    snapshots = load_universe_snapshots(snapshot_dir)
    bounds = price_bounds(price_dir)
    fundamentals = (
        load_quarterly_fundamentals(quarterly_path) if include_financial else None
    )
    rows = []
    for decision in decisions:
        stamp, age, universe = snapshot_age(snapshots, decision)
        universe = set(universe or set())
        warmup = decision - pd.Timedelta(days=price_warmup_days)
        covered_prices = {
            ticker for ticker in universe
            if ticker in bounds
            and bounds[ticker][0] <= warmup
            and bounds[ticker][1] >= decision
        }
        financial = set()
        if fundamentals is not None and universe:
            financial = set(quarterly_growth_snapshot(
                fundamentals, decision, maximum_financial_age_days
            ).index)
        rows.append({
            "decision_date": decision.strftime("%Y-%m-%d"),
            "year": int(decision.year),
            "snapshot_date": stamp.strftime("%Y-%m-%d") if stamp is not None else None,
            "snapshot_age_days": age,
            "snapshot_within_limit": age is not None and age <= maximum_snapshot_age_days,
            "universe_symbols": len(universe),
            "price_warmup_symbols": len(covered_prices),
            "price_warmup_fraction": len(covered_prices) / len(universe) if universe else 0.0,
            "financial_growth_symbols": len(financial & universe),
            "financial_growth_fraction": len(financial & universe) / len(universe) if universe else 0.0,
            "nasdaq_session_present": decision in index_dates,
            "qqq_session_present": decision in qqq_dates,
        })
    frame = pd.DataFrame(rows)
    yearly = {}
    for year, group in frame.groupby("year"):
        yearly[str(year)] = {
            "monthly_decisions": len(group),
            "snapshot_months_within_limit": int(group["snapshot_within_limit"].sum()),
            "maximum_snapshot_age_days": (
                int(group["snapshot_age_days"].dropna().max())
                if group["snapshot_age_days"].notna().any() else None
            ),
            "minimum_price_warmup_fraction": float(group["price_warmup_fraction"].min()),
            "minimum_financial_growth_fraction": float(group["financial_growth_fraction"].min()),
            "nasdaq_month_ends_present": int(group["nasdaq_session_present"].sum()),
            "qqq_month_ends_present": int(group["qqq_session_present"].sum()),
        }
    summary = {
        "schema_version": 1,
        "research_only": True,
        "requested_period": {"start": start, "end": end},
        "requirements": {
            "maximum_snapshot_age_days": maximum_snapshot_age_days,
            "price_warmup_days": price_warmup_days,
            "maximum_financial_age_days": maximum_financial_age_days,
            "monthly_point_in_time_decisions": True,
        },
        "training_windows": {
            str(year): training_windows(year) for year in range(2019, 2022)
        },
        "yearly": yearly,
        "all_months_snapshot_ready": bool(frame["snapshot_within_limit"].all()),
        "all_month_ends_have_benchmarks": bool(
            frame[["nasdaq_session_present", "qqq_session_present"]].all().all()
        ),
        "earliest_universe_snapshot": min(snapshots).strftime("%Y-%m-%d"),
        "input_bindings": {
            "nasdaq_index": {"path": str(index_path), "sha256": _sha256(index_path)},
            "qqq": {"path": str(qqq_path), "sha256": _sha256(qqq_path)},
            "quarterly": {"path": str(quarterly_path), "sha256": _sha256(quarterly_path)},
            "universe_snapshots": _snapshot_binding(snapshot_dir),
        },
        "ledger_level_price_delisting_audit_complete": False,
        "adaptive_training_eligible": False,
        "release_status": "BLOCKED",
    }
    return frame, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default="2021-12-31")
    parser.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    parser.add_argument("--qqq", type=Path, default=DEFAULT_QQQ)
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--skip-financial", action="store_true")
    args = parser.parse_args()
    frame, summary = build_audit(
        start=args.start, end=args.end, qqq_path=args.qqq,
        snapshot_dir=args.snapshot_dir,
        include_financial=not args.skip_financial
    )
    csv_path = args.prefix.with_suffix(".csv")
    json_path = args.prefix.with_suffix(".json")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(csv_path, index=False)
    summary["monthly_output"] = str(csv_path)
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({**summary, "summary_output": str(json_path)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
