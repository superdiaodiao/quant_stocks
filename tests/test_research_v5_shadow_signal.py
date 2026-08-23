from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts.research_v5_shadow_signal import MODEL_VERSION, record_signal
from scripts.research_v5_shadow_signal import _nasdaq_sessions_between


def test_v5_signal_waits_for_month_end_before_warmup(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({
        "model_version": MODEL_VERSION,
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "forward_evidence_start": "2026-08-10",
    }))

    result = record_signal(
        decision_date="2026-08-10",
        summary_path=summary,
        v4_history_path=tmp_path / "v4.csv",
        state_history_path=tmp_path / "state.csv",
        qqq_path=tmp_path / "qqq.csv",
        output_dir=tmp_path,
    )

    assert result["status"] == "WAITING_FOR_MONTH_END_SIGNAL"
    assert result["written"] is False
    assert result["release_status"] == "BLOCKED"


def test_v5_month_end_signal_does_not_backfill_missing_warmup(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({
        "model_version": MODEL_VERSION,
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "forward_evidence_start": "2026-08-10",
    }))

    result = record_signal(
        decision_date="2026-08-31",
        summary_path=summary,
        v4_history_path=tmp_path / "v4.csv",
        state_history_path=tmp_path / "state.csv",
        qqq_path=tmp_path / "qqq.csv",
        output_dir=tmp_path,
    )

    assert result["status"] == "WAITING_FOR_RELATIVE_STRENGTH_WARMUP"
    assert result["observed_return_intervals"] == 0
    assert result["required_return_intervals"] == 63


def test_nasdaq_contiguous_window_can_cross_calendar_year() -> None:
    sessions = _nasdaq_sessions_between(
        pd.Timestamp("2026-11-02"), pd.Timestamp("2027-02-01")
    )

    assert sessions[0] == pd.Timestamp("2026-11-02")
    assert sessions[-1] == pd.Timestamp("2027-02-01")
    assert any(stamp.year == 2026 for stamp in sessions)
    assert any(stamp.year == 2027 for stamp in sessions)
