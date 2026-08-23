#!/usr/bin/env python3
"""Refresh and observe v6 once; safe to schedule, but does not install itself."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

import pandas as pd

from scripts.research_v6_market_refresh import refresh
from scripts.research_v6_observe import observe
from src.research.shadow_evaluation import nasdaq_calendar_for_year


DEFAULT_SUMMARY = Path("output/research_v6_walkforward_defensive_ensemble_shadow_summary.json")
DEFAULT_ROOT = Path("output/research_only/v6_market")
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_MODEL_DIR = Path("output/daily/can-slim-v6-walkforward-defensive-ensemble-shadow")


def latest_completed_session(local_date: str | pd.Timestamp) -> pd.Timestamp:
    cutoff = pd.Timestamp(local_date).normalize() - pd.Timedelta(days=1)
    sessions = []
    for year in range(cutoff.year - 1, cutoff.year + 1):
        calendar = nasdaq_calendar_for_year(year)
        sessions.extend(calendar.sessions_in_range(
            pd.Timestamp(year, 1, 1), min(cutoff, pd.Timestamp(year, 12, 31))
        ))
    normalized = pd.DatetimeIndex(sessions).tz_localize(None).normalize()
    eligible = normalized[normalized <= cutoff]
    if not len(eligible):
        raise ValueError("no completed Nasdaq session before local date")
    return eligible[-1]


def scheduled_run(
    *,
    local_date: str | pd.Timestamp,
    summary_path: Path = DEFAULT_SUMMARY,
    root: Path = DEFAULT_ROOT,
    qqq_path: Path = DEFAULT_QQQ,
    model_dir: Path = DEFAULT_MODEL_DIR,
    workers: int = 16,
) -> dict:
    expected = latest_completed_session(local_date)
    market = refresh(
        expected_session=expected,
        summary_path=summary_path,
        root=root,
        qqq_path=qqq_path,
        workers=workers,
    )
    if not market["readiness"]["ready_for_v6_signal"]:
        result = {
            "status": "MARKET_DATA_NOT_READY",
            "expected_session": expected.strftime("%Y-%m-%d"),
            "market": market,
            "observation": None,
            "release_status": "BLOCKED",
            "broker_action_authorized": False,
        }
    else:
        observation = observe(
            as_of=expected,
            summary_path=summary_path,
            model_dir=model_dir,
            qqq_path=qqq_path,
            price_dir=root / "prices",
            index_path=root / "nasdaq_index.csv",
        )
        result = {
            "status": "COMPLETED_RESEARCH_ONLY_OBSERVATION_RUN",
            "expected_session": expected.strftime("%Y-%m-%d"),
            "market": market,
            "observation": observation,
            "release_status": "BLOCKED",
            "broker_action_authorized": False,
        }
    output = model_dir / "latest_scheduled_run.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result["output"] = str(output)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-date", default=date.today().isoformat())
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--qqq", type=Path, default=DEFAULT_QQQ)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    print(json.dumps(scheduled_run(
        local_date=args.local_date,
        summary_path=args.summary,
        root=args.root,
        qqq_path=args.qqq,
        model_dir=args.model_dir,
        workers=args.workers,
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
