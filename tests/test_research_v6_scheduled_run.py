from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.research_v6_scheduled_run import (
    latest_completed_session,
    scheduled_run,
)


def test_local_monday_resolves_prior_friday() -> None:
    assert latest_completed_session("2026-08-10") == pd.Timestamp("2026-08-07")


def test_scheduled_run_stops_before_observation_when_data_not_ready(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "scripts.research_v6_scheduled_run.refresh",
        lambda **kwargs: {"readiness": {"ready_for_v6_signal": False}},
    )
    called = []
    monkeypatch.setattr(
        "scripts.research_v6_scheduled_run.observe",
        lambda **kwargs: called.append(kwargs),
    )

    result = scheduled_run(
        local_date="2026-08-10",
        summary_path=tmp_path / "summary.json",
        root=tmp_path / "market",
        qqq_path=tmp_path / "qqq.csv",
        model_dir=tmp_path / "model",
    )

    assert result["status"] == "MARKET_DATA_NOT_READY"
    assert result["observation"] is None
    assert called == []
    assert result["broker_action_authorized"] is False
