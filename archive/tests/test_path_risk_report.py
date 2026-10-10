import pandas as pd
import pytest

from scripts.path_risk_report import drawdown_episodes


def test_drawdown_episode_records_peak_trough_and_recovery():
    dates = pd.bdate_range("2026-01-02", periods=5)
    returns = pd.Series(
        [0.10, -0.10, 0.0, 1 / 9, 0.10],
        index=dates,
    )

    episodes, summary = drawdown_episodes(returns)

    assert len(episodes) == 1
    episode = episodes.iloc[0]
    assert episode["peak_date"] == "2026-01-02"
    assert episode["trough_date"] == "2026-01-05"
    assert episode["recovery_date"] == "2026-01-07"
    assert episode["underwater_sessions"] == 2
    assert episode["maximum_drawdown"] == pytest.approx(-0.10)
    assert summary["currently_underwater"] is False
    assert summary["maximum_drawdown"] == pytest.approx(-0.10)


def test_unrecovered_drawdown_is_reported_as_current():
    dates = pd.bdate_range("2026-01-02", periods=3)
    returns = pd.Series([0.10, -0.20, 0.05], index=dates)

    episodes, summary = drawdown_episodes(returns)

    assert len(episodes) == 1
    assert not episodes.iloc[0]["recovered"]
    assert pd.isna(episodes.iloc[0]["recovery_date"])
    assert summary["currently_underwater"] is True
    assert summary["current_underwater_episode"]["underwater_sessions"] == 2
    assert summary["current_underwater_episode"]["recovery_date"] is None
