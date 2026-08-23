from __future__ import annotations

import pandas as pd
import json
from pathlib import Path

from scripts.research_v6_weekly_mark import (
    last_nasdaq_session_of_week,
    record_weekly_mark,
)


def test_week_end_uses_thursday_when_friday_is_holiday() -> None:
    assert last_nasdaq_session_of_week(pd.Timestamp("2026-07-03")) == pd.Timestamp(
        "2026-07-02"
    )


def test_normal_week_ends_on_friday() -> None:
    assert last_nasdaq_session_of_week(pd.Timestamp("2026-09-04")) == pd.Timestamp(
        "2026-09-04"
    )


def test_week_end_waits_for_first_execution(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({
        "model_version": "can-slim-v6-walkforward-defensive-ensemble-shadow",
        "release_status": "BLOCKED",
        "forward_evidence_start": "2026-09-01",
    }))

    result = record_weekly_mark(
        as_of="2026-09-04",
        summary_path=summary,
        model_dir=tmp_path / "model",
        qqq_path=tmp_path / "qqq.csv",
    )

    assert result["status"] == "WAITING_FOR_FIRST_EXECUTION"
    assert result["written"] is False


def test_pre_forward_week_waits_without_writing(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({
        "model_version": "can-slim-v6-walkforward-defensive-ensemble-shadow",
        "release_status": "BLOCKED",
        "forward_evidence_start": "2026-09-01",
    }))

    result = record_weekly_mark(
        as_of="2026-08-07",
        summary_path=summary,
        model_dir=tmp_path / "model",
        qqq_path=tmp_path / "qqq.csv",
    )

    assert result["status"] == "WAITING_FOR_FORWARD_START"
    assert result["written"] is False
