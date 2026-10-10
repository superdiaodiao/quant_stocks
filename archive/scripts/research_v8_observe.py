#!/usr/bin/env python3
"""Run the idempotent local v8 signal, weekly mark, and status checks once."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from scripts.research_v8_forward_status import build_status
from scripts.research_v8_shadow_signal import record_signal
from scripts.research_v8_weekly_mark import record_weekly_mark


DEFAULT_MANIFEST = Path("output/research_v8_monthly_risk_budget_blend_shadow_summary.json")
DEFAULT_MODEL_DIR = Path("output/daily/can-slim-v8-monthly-risk-budget-blend-shadow")
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_ROOT = Path("output/research_only/v6_market")


def observe(
    *,
    as_of: str | pd.Timestamp,
    manifest_path: Path = DEFAULT_MANIFEST,
    model_dir: Path = DEFAULT_MODEL_DIR,
    qqq_path: Path = DEFAULT_QQQ,
    root: Path = DEFAULT_ROOT,
) -> dict:
    stamp = pd.Timestamp(as_of).normalize()
    signal = record_signal(
        decision_date=stamp, manifest_path=manifest_path,
        qqq_path=qqq_path, output_dir=model_dir,
        price_dir=root / "prices", index_path=root / "nasdaq_index.csv",
        universe_path=root / "current_universe.csv",
    )
    mark = record_weekly_mark(
        as_of=stamp, manifest_path=manifest_path, model_dir=model_dir,
        qqq_path=qqq_path, price_dir=root / "prices",
    )
    status = build_status(
        manifest_path, model_dir / "weekly_marks.csv",
        model_dir / "monthly_decisions.csv",
        runtime_state_path=model_dir / "runtime_state.json",
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
    args = parser.parse_args()
    print(json.dumps(observe(as_of=args.as_of), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
