#!/usr/bin/env python3
"""Run the idempotent local v6 signal, weekly mark, and status checks once."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from scripts.research_v6_forward_status import build_status
from scripts.research_v6_shadow_signal import record_signal
from scripts.research_v6_weekly_mark import record_weekly_mark


def observe(
    *,
    as_of: str | pd.Timestamp,
    summary_path: Path,
    model_dir: Path,
    qqq_path: Path,
    price_dir: Path = Path("output/research_only/v6_market/prices"),
    index_path: Path = Path("output/research_only/v6_market/nasdaq_index.csv"),
) -> dict:
    stamp = pd.Timestamp(as_of).normalize()
    signal = record_signal(
        decision_date=stamp,
        summary_path=summary_path,
        base_state_path=model_dir / "base_forward_state.csv",
        qqq_path=qqq_path,
        output_dir=model_dir,
        price_dir=price_dir,
        index_path=index_path,
    )
    mark = record_weekly_mark(
        as_of=stamp,
        summary_path=summary_path,
        model_dir=model_dir,
        qqq_path=qqq_path,
        price_dir=price_dir,
    )
    status = build_status(
        summary_path,
        model_dir / "weekly_marks.csv",
        model_dir / "monthly_decisions.csv",
    )
    status_path = model_dir / "forward_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    return {
        "as_of": stamp.strftime("%Y-%m-%d"),
        "signal": signal,
        "weekly_mark": mark,
        "status": status,
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
        "status_output": str(status_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("output/research_v6_walkforward_defensive_ensemble_shadow_summary.json"),
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path("output/daily/can-slim-v6-walkforward-defensive-ensemble-shadow"),
    )
    parser.add_argument(
        "--qqq", type=Path,
        default=Path("output/research_only/qqq_nasdaq_history.csv"),
    )
    parser.add_argument(
        "--price-dir", type=Path,
        default=Path("output/research_only/v6_market/prices"),
    )
    parser.add_argument(
        "--index", type=Path,
        default=Path("output/research_only/v6_market/nasdaq_index.csv"),
    )
    args = parser.parse_args()
    print(json.dumps(observe(
        as_of=args.as_of,
        summary_path=args.summary,
        model_dir=args.model_dir,
        qqq_path=args.qqq,
        price_dir=args.price_dir,
        index_path=args.index,
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
