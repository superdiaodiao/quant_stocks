from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v51_august_recovery as v51


def test_recovery_protocol_is_late_only_and_keeps_release_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        v51.v50,
        "status",
        lambda: {
            "frozen_signal_count": 0,
            "bound_execution_count": 0,
            "valuation_count": 0,
        },
    )
    monkeypatch.setattr(v51, "_git_head", lambda: "a" * 40)
    monkeypatch.setattr(
        v51,
        "_input_bindings",
        lambda: {"fixture": {"path": "fixture", "sha256": "b" * 64}},
    )
    path = tmp_path / "protocol.json"

    result = v51.freeze_recovery_protocol(
        path,
        observed_at=datetime(2026, 9, 4, 7, tzinfo=timezone.utc),
    )

    assert result["status"] == "FROZEN_LATE_DIAGNOSTIC_ONLY"
    assert result["recovery_specification"][
        "eligible_for_original_august_prospective_score"
    ] is False
    assert result["broker_connection_used"] is False
    assert result["release_status"] == "BLOCKED"
    assert json.loads(path.read_text(encoding="utf-8"))[
        "target_not_inspected_before_this_freeze"
    ] is True


def test_recovery_protocol_refuses_nonempty_v50_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        v51.v50,
        "status",
        lambda: {
            "frozen_signal_count": 1,
            "bound_execution_count": 0,
            "valuation_count": 0,
        },
    )

    with pytest.raises(RuntimeError, match="signal count"):
        v51.freeze_recovery_protocol(
            tmp_path / "protocol.json",
            observed_at=datetime(2026, 9, 4, 7, tzinfo=timezone.utc),
        )


def test_source_locked_universe_rejects_future_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "future.csv"
    snapshot.write_text(
        "Symbol,ETF,Test Issue,Observed At\nTEST,N,N,2026-09-01\n",
        encoding="utf-8",
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "current_universe.csv").write_text("old\n", encoding="utf-8")
    (bundle / "bundle_manifest.json").write_text(
        json.dumps({"files": {"current_universe.csv": "old"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(v51, "UNIVERSE_SNAPSHOT", snapshot)

    with pytest.raises(RuntimeError, match="future-dated"):
        v51._replace_bundle_universe(bundle)


def test_build_diagnostic_never_mutates_v50_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text("{}\n", encoding="utf-8")
    report_path = tmp_path / "diagnostic.json"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    ledger = tmp_path / "v50_ledger.jsonl"
    ledger.write_text('{"event":"frozen"}\n', encoding="utf-8")
    snapshot = tmp_path / "snapshot.csv"
    snapshot.write_text(
        "Symbol,ETF,Test Issue,Observed At\nAAA,N,N,2026-07-01\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(v51.v50, "LEDGER_PATH", ledger)
    monkeypatch.setattr(v51, "UNIVERSE_SNAPSHOT", snapshot)
    monkeypatch.setattr(
        v51,
        "_validated_recovery_protocol",
        lambda _path: ({"status": "frozen"}, "p" * 64),
    )
    monkeypatch.setattr(v51, "_stage_late_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(v51, "_replace_bundle_universe", lambda _bundle: {})
    monkeypatch.setattr(
        v51,
        "_validated_late_bundle",
        lambda _bundle: ({"created_at": "2026-09-04T07:00:00+00:00"}, "m" * 64),
    )
    monkeypatch.setattr(
        v51.v50,
        "_validated_protocol",
        lambda: ({"model": {}}, "v" * 64),
    )
    monkeypatch.setattr(
        v51.v50,
        "_build_signal_payload",
        lambda **_kwargs: {
            "targets": [{"ticker": "AAA", "target_weight": 0.2}],
            "market_regime_on": True,
            "signal_staging_timeliness_verified": True,
        },
    )
    before = ledger.read_bytes()

    result = v51.build_late_diagnostic(
        protocol_path=protocol_path,
        report_path=report_path,
        bundles_dir=tmp_path / "bundles",
        work_dir=tmp_path / "work",
        observed_at=datetime(2026, 9, 4, 7, tzinfo=timezone.utc),
    )

    assert ledger.read_bytes() == before
    assert result["v50_ledger_unchanged"] is True
    assert result["eligible_for_original_august_prospective_score"] is False
    assert result["model_output"]["prospective_signal"] is False
    assert result["model_output"]["signal_staging_timeliness_verified"] is False
    assert result["model_output"]["targets"] == [
        {"ticker": "AAA", "target_weight": 0.2}
    ]


def test_recovery_dates_are_valid_sessions() -> None:
    assert v51.v43._is_nasdaq_session(v51.SOURCE_SIGNAL_DATE)
    assert v51.v43.v42._is_month_end_signal(v51.SOURCE_SIGNAL_DATE)
    assert v51.v43._is_nasdaq_session(
        v51.EARLIEST_RECOVERY_SHADOW_EXECUTION_DATE
    )
    assert v51.v43.v42._is_month_end_signal(v51.NEXT_OFFICIAL_SIGNAL_DATE)
    assert pd.Timestamp("2026-09-04") > v51.SOURCE_SIGNAL_DATE
