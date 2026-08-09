#!/usr/bin/env python3
"""Summarize progress toward the precommitted shadow-forward requirement."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from src.conf import PROJECT_PATH
from src.research.shadow_policy import (
    FORWARD_EVIDENCE_START,
    MIN_COMPLETED_MONTHLY_PERIODS,
    MIN_CONTIGUOUS_SESSIONS,
    MIN_WINNING_PERIODS,
    REQUIRE_EXTERNAL_ANCHOR,
)


DEFAULT_OBSERVATION_DIR = (
    Path(PROJECT_PATH) / "output/daily/can-slim-top3-v1/shadow_observations"
)


def build_status(observation_dir: str | Path = DEFAULT_OBSERVATION_DIR) -> dict:
    directory = Path(observation_dir)
    index_path = directory / "index.json"
    index = {}
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    observations = list(index.get("observations", []))
    dates = sorted({item.get("observation_date") for item in observations if item.get("observation_date")})
    latest = dates[-1] if dates else None
    session_count = int(sum(item.get("forward_sessions", 0) for item in observations))
    status = (
        "IN_PROGRESS"
        if session_count
        else "EXECUTION_ANCHOR_RECORDED"
        if observations
        else "NOT_STARTED"
    )
    return {
        "schema_version": 1,
        "research_only": True,
        "status": status,
        "forward_evidence_start": FORWARD_EVIDENCE_START,
        "latest_observation_date": latest,
        "observed_sessions": session_count,
        "minimum_contiguous_sessions": MIN_CONTIGUOUS_SESSIONS,
        "minimum_completed_monthly_periods": MIN_COMPLETED_MONTHLY_PERIODS,
        "minimum_winning_periods": MIN_WINNING_PERIODS,
        "remaining_sessions_lower_bound": max(MIN_CONTIGUOUS_SESSIONS - session_count, 0),
        "unanchored_observations": sum(
            item.get("status") == "UNANCHORED_FORWARD_OBSERVATION"
            for item in observations
        ),
        "execution_anchor_records": sum(
            item.get("status") == "EXECUTION_ANCHOR_ONLY"
            for item in observations
        ),
        "externally_anchored_observations": sum(
            item.get("status") == "EXTERNALLY_ANCHORED_FORWARD_OBSERVATION"
            for item in observations
        ),
        "external_anchor_required": REQUIRE_EXTERNAL_ANCHOR,
        "release_eligible": False,
        "release_block_reason": (
            "Requires 252 contiguous sessions, 12 completed periods, "
            "strict majority wins, positive excess, and external anchoring"
        ),
        "index": str(index_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation-dir", default=str(DEFAULT_OBSERVATION_DIR))
    parser.add_argument("--output")
    args = parser.parse_args()
    result = build_status(args.observation_dir)
    output = Path(args.output) if args.output else Path(args.observation_dir) / "status.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    result["output"] = str(output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
