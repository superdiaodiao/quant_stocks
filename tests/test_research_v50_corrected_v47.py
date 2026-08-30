from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50_corrected_v47 as v50


def test_development_protocol_uses_portable_repository_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "development_protocol.json"

    result = v50.freeze_development_protocol(path)

    for binding in result["input_bindings"].values():
        assert not Path(binding["path"]).is_absolute()
    monkeypatch.chdir(tmp_path)
    validated, protocol_sha = v50._validated_development_protocol(path)
    assert validated["correction_specification"] == v50.correction_specification()
    assert protocol_sha == v50._sha256(path)


def test_signal_staging_requires_the_same_utc_date() -> None:
    stamp = pd.Timestamp("2026-08-31")

    assert v50._signal_staging_is_timely(
        stamp, datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)
    )
    assert not v50._signal_staging_is_timely(
        stamp, datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        v50._signal_staging_is_timely(stamp, datetime(2026, 8, 31, 23, 0))


def test_late_signal_stage_fails_before_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        v43,
        "stage_bundle",
        lambda **_kwargs: pytest.fail("late bundle reached refresh runtime"),
    )

    with pytest.raises(RuntimeError, match="refuses late SIGNAL staging"):
        v50.stage_bundle(
            as_of="2026-08-31",
            purpose="SIGNAL",
            bundles_dir=tmp_path,
            work_dir=tmp_path / "work",
            signals_dir=tmp_path / "signals",
            ledger_path=tmp_path / "ledger.jsonl",
            observed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )


def test_signal_bundle_validator_rejects_late_created_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "runner_version": v50.MODEL_VERSION,
        "purpose": "SIGNAL",
        "as_of": "2026-08-31",
        "created_at": "2026-09-01T00:00:00+00:00",
    }
    monkeypatch.setattr(
        v50,
        "V43_VALIDATED_BUNDLE",
        lambda *_args: (manifest, "a" * 64),
    )

    with pytest.raises(RuntimeError, match="staged after"):
        v50._validated_bundle(tmp_path, "SIGNAL")


def test_prospective_protocol_keeps_training_wins_non_official(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "development_status": "PASS",
        "research_forward_observation_ready": True,
        "gates": {"all": True},
        "positive_training_years_50bps": 6,
    }
    monkeypatch.setattr(v50, "_development_manifest", lambda: manifest)
    monkeypatch.setattr(v50, "_development_bindings", lambda: {})
    monkeypatch.setattr(v50, "_zero_signal_v48_events", lambda: [{}])
    monkeypatch.setattr(
        v50,
        "_file_binding",
        lambda path: {"path": str(path), "sha256": "b" * 64},
    )
    monkeypatch.setattr(v50, "_git_head", lambda: "c" * 40)
    monkeypatch.setattr(v43, "append_event", lambda **_kwargs: None)
    protocol_path = tmp_path / "protocol.json"
    ledger_path = tmp_path / "ledger.jsonl"

    result = v50.freeze_protocol(protocol_path, ledger_path)

    assert result["corrected_training_positive_years_50bps"] == 6
    assert result["evidence_partition"]["2020_2025"][
        "official_year_wins"
    ] == 0
    assert result["price_policy"][
        "automatic_heuristic_adjustment_allowed"
    ] is False
    assert result["release_status"] == "BLOCKED"
    assert json.loads(protocol_path.read_text(encoding="utf-8"))[
        "model_version"
    ] == v50.MODEL_VERSION
