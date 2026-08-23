from __future__ import annotations

from pathlib import Path

from scripts.research_v6_observe import observe


def test_observer_never_authorizes_broker_action(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.research_v6_observe.record_signal",
        lambda **kwargs: {"status": "WAITING_FOR_MONTH_END_SIGNAL"},
    )
    monkeypatch.setattr(
        "scripts.research_v6_observe.record_weekly_mark",
        lambda **kwargs: {"status": "WAITING_FOR_WEEK_END"},
    )
    monkeypatch.setattr(
        "scripts.research_v6_observe.build_status",
        lambda *args: {"release_status": "BLOCKED", "promotion_eligible": False},
    )

    result = observe(
        as_of="2026-08-10",
        summary_path=tmp_path / "summary.json",
        model_dir=tmp_path / "model",
        qqq_path=tmp_path / "qqq.csv",
    )

    assert result["release_status"] == "BLOCKED"
    assert result["broker_action_authorized"] is False
    assert (tmp_path / "model/forward_status.json").is_file()
