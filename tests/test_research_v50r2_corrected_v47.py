from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
import pandas as pd
import pytest

from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50_corrected_v47 as r1
from scripts import research_v50r2_corrected_v47 as r2


def test_r2_leaves_the_r1_runner_and_development_replay_untouched() -> None:
    r1_protocol = json.loads(
        (r2.REPO_ROOT / r2.V50R1_PROTOCOL_PATH).read_text(encoding="utf-8")
    )
    runner_binding = r1_protocol["input_bindings"]["runner"]
    assert runner_binding["path"] == "scripts/research_v50_corrected_v47.py"
    assert r2._sha256(runner_binding["path"]) == runner_binding["sha256"]
    assert r2.DEVELOPMENT_DIR == r1.OUTPUT_DIR
    assert r2.DEVELOPMENT_PROTOCOL_PATH == r1.DEVELOPMENT_PROTOCOL_PATH
    assert r2.OUTPUT_DIR != r1.OUTPUT_DIR
    assert r2.MODEL_VERSION != r1.MODEL_VERSION
    assert r2.SUPERSEDED_MODEL_VERSION == r1.MODEL_VERSION
    assert r2._selected_model() == r1._selected_model()


def test_first_prospective_signal_is_the_september_month_end() -> None:
    assert r2.MISSED_SIGNAL_DATE == pd.Timestamp("2026-08-31")
    assert r2.FIRST_PROSPECTIVE_SIGNAL_DATE == pd.Timestamp("2026-09-30")
    assert r2.FIRST_PROSPECTIVE_SIGNAL_DATE > r2.MISSED_SIGNAL_DATE
    assert r2.runtime_repair_specification()[
        "missed_signal_backfill_allowed"
    ] is False


def test_runtime_normalizes_numpy_scalars_in_bundle_manifests() -> None:
    gate = pd.Series([pd.Timestamp("2026-09-01")]).le(
        pd.Timestamp("2026-09-30")
    ).all()
    assert isinstance(gate, np.generic)
    manifest = {"readiness_gates": {"fundamentals_no_future_available_date": gate}}
    original_v42_json = v43.v42.json
    original_v43_json = v43.json

    with pytest.raises(TypeError):
        original_v42_json.dumps(manifest, indent=2, sort_keys=True)

    with r2._runtime():
        assert v43.MODEL_VERSION == r2.MODEL_VERSION
        encoded = v43.v42.json.dumps(manifest, indent=2, sort_keys=True)
        assert json.loads(encoded) == {
            "readiness_gates": {"fundamentals_no_future_available_date": True}
        }
        plain = {"b": 1, "a": [True, "x", 2.5, None]}
        assert v43.json.dumps(plain, sort_keys=True) == json.dumps(
            plain, sort_keys=True
        )
        with pytest.raises(TypeError, match="not JSON serializable"):
            v43.v42.json.dumps({"x": object()})

    assert v43.v42.json is original_v42_json
    assert v43.json is original_v43_json
    assert v43.MODEL_VERSION != r2.MODEL_VERSION


def test_missed_august_signal_is_never_backfilled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        v43,
        "stage_bundle",
        lambda **_kwargs: pytest.fail("missed-window bundle reached refresh"),
    )

    with pytest.raises(RuntimeError, match="never backfilled"):
        r2.stage_bundle(
            as_of="2026-08-31",
            purpose="SIGNAL",
            bundles_dir=tmp_path,
            work_dir=tmp_path / "work",
            signals_dir=tmp_path / "signals",
            ledger_path=tmp_path / "ledger.jsonl",
            observed_at=datetime(2026, 8, 31, 22, 0, tzinfo=timezone.utc),
        )


def test_late_signal_stage_fails_before_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        v43,
        "stage_bundle",
        lambda **_kwargs: pytest.fail("late bundle reached refresh runtime"),
    )

    with pytest.raises(RuntimeError, match="refuses late SIGNAL staging"):
        r2.stage_bundle(
            as_of="2026-09-30",
            purpose="SIGNAL",
            bundles_dir=tmp_path,
            work_dir=tmp_path / "work",
            signals_dir=tmp_path / "signals",
            ledger_path=tmp_path / "ledger.jsonl",
            observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )


def test_signal_bundle_validator_enforces_runner_date_and_timeliness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "runner_version": r2.MODEL_VERSION,
        "purpose": "SIGNAL",
        "as_of": "2026-09-30",
        "created_at": "2026-10-01T00:00:00+00:00",
    }
    monkeypatch.setattr(
        r1, "V43_VALIDATED_BUNDLE", lambda *_args: (manifest, "a" * 64)
    )
    with pytest.raises(RuntimeError, match="staged after"):
        r2._validated_bundle(tmp_path, "SIGNAL")

    manifest["created_at"] = "2026-09-30T21:05:00+00:00"
    validated, _sha = r2._validated_bundle(tmp_path, "SIGNAL")
    assert validated is manifest

    manifest["as_of"] = "2026-08-31"
    manifest["created_at"] = "2026-08-31T21:05:00+00:00"
    with pytest.raises(RuntimeError, match="missed or pre-r2"):
        r2._validated_bundle(tmp_path, "SIGNAL")

    manifest["runner_version"] = r1.MODEL_VERSION
    with pytest.raises(RuntimeError, match="not frozen by the v50r2 runner"):
        r2._validated_bundle(tmp_path, "SIGNAL")


def test_prospective_protocol_supersedes_r1_and_keeps_training_non_official(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "development_status": "PASS",
        "research_forward_observation_ready": True,
        "gates": {"all": True},
        "positive_training_years_50bps": 6,
    }
    monkeypatch.setattr(r1, "_development_manifest", lambda: manifest)
    monkeypatch.setattr(r1, "_zero_signal_v48_events", lambda: [{}])
    monkeypatch.setattr(r2, "_zero_signal_v50r1_events", lambda: [{}])
    monkeypatch.setattr(
        r2, "_file_binding", lambda path: {"path": str(path), "sha256": "b" * 64}
    )
    monkeypatch.setattr(r2, "_git_head", lambda: "c" * 40)
    monkeypatch.setattr(v43, "append_event", lambda **_kwargs: None)
    protocol_path = tmp_path / "protocol.json"
    ledger_path = tmp_path / "ledger.jsonl"

    result = r2.freeze_protocol(protocol_path, ledger_path)

    assert result["model_version"] == r2.MODEL_VERSION
    assert result["supersedes"]["model_version"] == r1.MODEL_VERSION
    assert result["supersedes"]["v50r1_signal_count"] == 0
    assert result["runtime_repair"] == r2.runtime_repair_specification()
    assert result["model"] == r1._selected_model()
    assert result["corrected_training_positive_years_50bps"] == 6
    assert result["evidence_partition"]["2020_2025"]["official_year_wins"] == 0
    assert result["evidence_partition"]["2026_08"]["role"] == (
        "MISSED_WINDOW_NOT_BACKFILLED"
    )
    assert result["evidence_partition"]["prospective"]["first_signal_date"] == (
        "2026-09-30"
    )
    assert result["signal_policy"]["missed_signal_dates"] == ["2026-08-31"]
    assert result["release_status"] == "BLOCKED"
    assert result["promotion_eligible"] is False
    assert {"runner", "r1_runner", "v50r1_protocol", "v50r1_ledger"} <= set(
        result["input_bindings"]
    )
    with pytest.raises(RuntimeError, match="will not be overwritten"):
        r2.freeze_protocol(protocol_path, ledger_path)


def test_frozen_r2_protocol_is_tracked_hash_bound_and_verifiable() -> None:
    protocol_path = r2.REPO_ROOT / r2.PROTOCOL_PATH
    if not protocol_path.exists():
        pytest.skip("v50r2 protocol has not been frozen yet")
    tracked = set(
        subprocess.run(
            ["git", "ls-files"],
            cwd=r2.REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    for name, binding in protocol["input_bindings"].items():
        relative = binding["path"]
        assert not Path(relative).is_absolute(), f"{name} is checkout-absolute"
        assert relative in tracked, f"clean checkout is missing {relative}"
        payload = (r2.REPO_ROOT / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == binding["sha256"], name
    validated, _sha = r2._validated_protocol(protocol_path)
    assert validated["supersedes"]["model_version"] == r1.MODEL_VERSION
    supersession = json.loads(
        (r2.REPO_ROOT / r2.V50R1_SUPERSESSION_PATH).read_text(encoding="utf-8")
    )
    assert supersession["successor_protocol"]["sha256"] == r2._sha256(
        protocol_path
    )
    events = v43.read_ledger(r2.REPO_ROOT / r2.LEDGER_PATH)
    assert [event["event_type"] for event in events] == ["PROTOCOL_FROZEN"]
    assert events[0]["payload"]["first_prospective_signal_date"] == "2026-09-30"
