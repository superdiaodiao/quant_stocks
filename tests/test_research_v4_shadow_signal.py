from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import scripts.research_v4_shadow_signal as module


def test_pre_freeze_signal_is_not_written(tmp_path: Path, monkeypatch) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "model_version": module.MODEL_VERSION,
                "policy_status": "FROZEN_FORWARD_ONLY",
                "release_status": "BLOCKED",
                "forward_evidence_start": "2026-08-10",
            }
        )
    )
    recommendations = pd.DataFrame(
        {
            "ticker": ["__CASH__"],
            "action_reason": ["MODEL_NOT_YET_EFFECTIVE_AT_EXECUTION"],
        }
    )
    monkeypatch.setattr(
        module,
        "generate_can_slim_shadow_recommendations",
        lambda **_kwargs: (
            recommendations,
            {
                "as_of": "2026-08-07",
                "signal_date": "2026-07-31",
                "model_snapshot_effective_start": None,
            },
        ),
    )
    monkeypatch.setattr(
        module,
        "save_can_slim_shadow_recommendations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pre-freeze signal must not be saved")
        ),
    )

    result = module.record_signal(summary_path=summary, output_dir=tmp_path)

    assert result["written"] is False
    assert result["status"] == "WAITING_FOR_FIRST_POST_FREEZE_SIGNAL"
