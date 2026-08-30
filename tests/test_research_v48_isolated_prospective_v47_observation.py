import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v42_prospective_v28_observation as v42
from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v47_hybrid_entry_portfolio_stop as v47
from scripts import research_v48_isolated_prospective_v47_observation as v48


def test_v48_protocol_binds_v47_and_excludes_training_wins(tmp_path: Path) -> None:
    protocol_path = tmp_path / "protocol.json"
    ledger_path = tmp_path / "ledger.jsonl"

    result = v48.freeze_protocol(protocol_path, ledger_path)

    assert result["supersedes"]["v43_signal_count"] == 0
    assert result["model"]["risk_specification"]["entry_loss_fraction"] == 0.20
    assert (
        result["model"]["risk_specification"][
            "portfolio_trailing_stop_fraction"
        ]
        == 0.25
    )
    assert result["development_evidence"]["positive_training_years_50bps"] == 6
    assert result["development_evidence"][
        "training_years_counted_as_official_wins"
    ] == 0
    assert result["evidence_partition"]["2020_2025"]["official_year_wins"] == 0
    assert result["release_status"] == "BLOCKED"
    assert [event["event_type"] for event in v48.read_ledger(ledger_path)] == [
        "PROTOCOL_FROZEN"
    ]


def test_v43_supersession_record_requires_and_reports_zero_signals(
    tmp_path: Path,
) -> None:
    successor = tmp_path / "v48_protocol.json"
    ledger = tmp_path / "v48_ledger.jsonl"
    v48.freeze_protocol(successor, ledger)

    record = v48.write_v43_supersession(
        path=tmp_path / "superseded.json",
        successor_protocol=successor,
    )

    assert record["status"] == "SUPERSEDED_BEFORE_FIRST_SIGNAL"
    assert record["v43_signal_count"] == 0
    assert v43.read_ledger(v43.LEDGER_PATH)[0]["event_type"] == "PROTOCOL_FROZEN"


def test_hybrid_adapter_passes_only_the_frozen_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_replay(*args, **kwargs):
        captured.update(kwargs)
        return pd.DataFrame({"strategy": [0.0], "benchmark": [0.0]})

    monkeypatch.setattr(v47, "replay_with_hybrid_stop", fake_replay)
    dates = pd.DatetimeIndex(["2026-09-01"])
    result = v48._hybrid_replay_adapter(
        pd.DataFrame({"A": [100.0]}, index=dates),
        pd.Series([1000.0], index=dates),
        pd.DataFrame(
            {
                "effective_date": dates,
                "ticker": ["A"],
                "target_weight": [1.0],
            }
        ),
        dates[0],
        dates[0],
        trailing_stop_fraction=0.25,
        transaction_cost_bps=50.0,
    )

    assert not result.empty
    assert captured["entry_loss_fraction"] == 0.20
    assert captured["portfolio_stop_fraction"] == 0.25
    assert captured["transaction_cost_bps"] == 50.0


def test_hybrid_adapter_rejects_changed_portfolio_threshold() -> None:
    dates = pd.DatetimeIndex(["2026-09-01"])
    with pytest.raises(RuntimeError, match="interface binding changed"):
        v48._hybrid_replay_adapter(
            pd.DataFrame({"A": [100.0]}, index=dates),
            pd.Series([1000.0], index=dates),
            pd.DataFrame(
                {
                    "effective_date": dates,
                    "ticker": ["A"],
                    "target_weight": [1.0],
                }
            ),
            dates[0],
            dates[0],
            trailing_stop_fraction=0.20,
            transaction_cost_bps=50.0,
        )


def test_append_mark_installs_hybrid_replay_and_restores_v42(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = v42.v28.replay_with_individual_trailing_stop

    def fake_append_mark(**_kwargs):
        assert v42.v28.replay_with_individual_trailing_stop is v48._hybrid_replay_adapter
        assert v43.MODEL_VERSION == v48.MODEL_VERSION
        return {"status": "APPENDED_PROSPECTIVE_MARK"}

    monkeypatch.setattr(v43, "append_mark", fake_append_mark)
    result = v48.append_mark(bundle=tmp_path / "bundle")

    assert result["hybrid_risk_replay_verified"] is True
    assert v42.v28.replay_with_individual_trailing_stop is original
    assert v43.MODEL_VERSION != v48.MODEL_VERSION


def test_v48_bundle_validator_requires_v48_runner_version(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    qqq = bundle / "qqq.csv"
    qqq.write_text(
        "date,close,cash_dividend\n2026-08-31,100,0\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": 2,
        "runner_version": v48.MODEL_VERSION,
        "purpose": "MARK",
        "as_of": "2026-08-31",
        "readiness_gates": {"ready": True},
        "files": {"qqq.csv": v48._sha256(qqq)},
        "price_files": {},
        "runtime_isolation": {
            "formal_market_bindings_before": {"x": "a"},
            "formal_market_bindings_after": {"x": "a"},
            "formal_market_files_modified": False,
            "formal_financial_files_modified": False,
            "shared_companyfacts_cache_modified": False,
        },
    }
    (bundle / "bundle_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )

    validated, _sha = v48._validated_bundle(bundle, "MARK")
    assert validated["runner_version"] == v48.MODEL_VERSION


def test_v48_status_names_the_frozen_hybrid_model(
    tmp_path: Path,
) -> None:
    protocol_path = tmp_path / "protocol.json"
    ledger_path = tmp_path / "ledger.jsonl"
    v48.freeze_protocol(protocol_path, ledger_path)

    result = v48.status(protocol_path=protocol_path, ledger_path=ledger_path)

    assert result["status"] == "WAITING_FOR_FIRST_PROSPECTIVE_SIGNAL"
    assert result["model_version"] == v48.MODEL_VERSION
    assert result["official_training_year_wins"] == 0
    assert result["supersedes_v43_before_first_signal"] is True
    assert result["frozen_risk_model"].endswith("portfolio_trailing_stop_25pct")
