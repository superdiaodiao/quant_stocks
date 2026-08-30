import json
import os
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v43_isolated_prospective_v28_observation as v43


def test_v43_protocol_supersedes_zero_signal_v42_and_preserves_score_partition(
    tmp_path: Path,
) -> None:
    protocol_path = tmp_path / "protocol.json"
    ledger_path = tmp_path / "ledger.jsonl"

    result = v43.freeze_protocol(protocol_path, ledger_path)

    assert result["supersedes"]["v42_signal_count"] == 0
    assert result["evidence_partition"]["2020_2025"][
        "counts_as_official_comparison"
    ] is False
    assert result["evidence_partition"]["2020_2025"]["official_year_wins"] == 0
    assert result["evidence_partition"]["2026_01_07"][
        "counts_as_official_comparison"
    ] is False
    assert result["runtime_isolation"]["shared_companyfacts_cache_writable"] is False
    assert result["contains_index_etf_holdings"] is False
    assert [event["event_type"] for event in v43.read_ledger(ledger_path)] == [
        "PROTOCOL_FROZEN"
    ]


def test_v43_ledger_rejects_backdated_signal(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    protocol_sha = "a" * 64
    v43.append_event(
        path=ledger_path,
        protocol_sha256=protocol_sha,
        event_type="PROTOCOL_FROZEN",
        payload={"protocol_sha256": protocol_sha},
    )
    v43.append_event(
        path=ledger_path,
        protocol_sha256=protocol_sha,
        event_type="SIGNAL_FROZEN",
        payload={"signal_date": "2026-09-30", "signal_sha256": "b" * 64},
    )

    with pytest.raises(RuntimeError, match="strictly increasing"):
        v43.append_event(
            path=ledger_path,
            protocol_sha256=protocol_sha,
            event_type="SIGNAL_FROZEN",
            payload={"signal_date": "2026-08-31", "signal_sha256": "c" * 64},
        )


def test_hardlink_cache_clone_can_replace_target_without_mutating_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "formal_cache"
    target = tmp_path / "isolated_cache"
    source.mkdir()
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    payload = source / "CIK0000000001.json.gz"
    payload.write_bytes(b"formal")
    monkeypatch.setattr(
        v43.fundamentals_update,
        "verify_companyfacts_cache_manifest",
        lambda _path: None,
    )

    result = v43._hardlink_clone_cache(source, target)
    assert result["status"] == "HARDLINK_CLONED_AND_VERIFIED"
    assert os.stat(payload).st_ino == os.stat(target / payload.name).st_ino

    replacement = target / (payload.name + ".tmp")
    replacement.write_bytes(b"isolated")
    os.replace(replacement, target / payload.name)
    assert payload.read_bytes() == b"formal"
    assert (target / payload.name).read_bytes() == b"isolated"


def test_trim_qqq_removes_rows_after_declared_as_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "qqq.csv"

    def fake_refresh(path: Path) -> Path:
        pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2026-08-28", "2026-08-31", "2026-09-01"]
                ),
                "close": [100.0, 101.0, 102.0],
                "cash_dividend": 0.0,
            }
        ).to_csv(path, index=False)
        return path

    monkeypatch.setattr(v43, "V42_REFRESH_CORE_PRICE", fake_refresh)
    v43._trim_qqq(output, pd.Timestamp("2026-08-31"))
    frame = pd.read_csv(output, parse_dates=["date"])
    assert frame["date"].max() == pd.Timestamp("2026-08-31")
    assert not frame["date"].gt("2026-08-31").any()


def test_orphan_signal_is_recovered_only_after_deterministic_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    protocol_sha = "a" * 64
    v43.append_event(
        path=ledger_path,
        protocol_sha256=protocol_sha,
        event_type="PROTOCOL_FROZEN",
        payload={"protocol_sha256": protocol_sha},
    )
    payload = {
        "signal_date": "2026-08-31",
        "targets": [{"ticker": "AAA", "target_weight": 0.2}],
        "model_version": v43.MODEL_VERSION,
    }
    orphan = signals_dir / "signal_2026-08-31.json"
    orphan.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    monkeypatch.setattr(
        v43,
        "_validated_protocol",
        lambda _path: ({"model": {}}, protocol_sha),
    )
    monkeypatch.setattr(
        v43,
        "_validated_bundle",
        lambda _path, _purpose: ({"as_of": "2026-08-31"}, "b" * 64),
    )
    monkeypatch.setattr(v43, "_build_signal_payload", lambda **_kwargs: payload)

    result = v43.freeze_signal(
        bundle=tmp_path / "bundle",
        protocol_path=tmp_path / "protocol.json",
        ledger_path=ledger_path,
        signals_dir=signals_dir,
    )

    assert result["status"] == "RECOVERED_AND_FROZEN_PROSPECTIVE_SIGNAL"
    event = v43.read_ledger(ledger_path)[-1]
    assert event["payload"]["recovered_after_preledger_crash"] is True


def test_v43_bundle_validator_requires_isolation_and_exact_qqq_cutoff(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    qqq = bundle / "qqq.csv"
    qqq.write_text(
        "date,close,cash_dividend\n2026-08-31,100,0\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": 2,
        "runner_version": v43.MODEL_VERSION,
        "purpose": "MARK",
        "as_of": "2026-08-31",
        "readiness_gates": {"ready": True},
        "files": {"qqq.csv": v43._sha256(qqq)},
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
    v43._validated_bundle(bundle, "MARK")

    manifest["runtime_isolation"]["shared_companyfacts_cache_modified"] = True
    (bundle / "bundle_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="isolation claim"):
        v43._validated_bundle(bundle, "MARK")


def test_stage_wrapper_atomically_promotes_cutoff_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    protocol_sha = "a" * 64
    v43.append_event(
        path=ledger_path,
        protocol_sha256=protocol_sha,
        event_type="PROTOCOL_FROZEN",
        payload={"protocol_sha256": protocol_sha},
    )

    def fake_core_refresh(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-31", "2026-09-01"]),
                "close": [100.0, 101.0],
                "cash_dividend": 0.0,
            }
        ).to_csv(path, index=False)
        path.with_suffix(".provenance.json").write_text(
            json.dumps({"source": "synthetic"}), encoding="utf-8"
        )
        return path

    monkeypatch.setattr(v43, "V42_REFRESH_CORE_PRICE", fake_core_refresh)

    def fake_base_stage(**kwargs) -> dict:
        stamp = pd.Timestamp(kwargs["as_of"])
        suffix = f"{stamp:%Y-%m-%d}_{kwargs['purpose'].lower()}"
        bundle = Path(kwargs["bundles_dir"]) / suffix
        bundle.mkdir(parents=True)
        qqq_work = Path(kwargs["work_dir"]) / "market" / "qqq.csv"
        v43.v42.refresh_core_price(qqq_work)
        (bundle / "qqq.csv").write_bytes(qqq_work.read_bytes())
        manifest = {
            "schema_version": 1,
            "purpose": kwargs["purpose"],
            "as_of": f"{stamp:%Y-%m-%d}",
            "readiness_gates": {"ready": True},
            "files": {"qqq.csv": v43._sha256(bundle / "qqq.csv")},
            "price_files": {},
        }
        (bundle / "bundle_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return {"bundle": str(bundle)}

    monkeypatch.setattr(v43.v42, "stage_bundle", fake_base_stage)
    result = v43.stage_bundle(
        as_of="2026-08-31",
        purpose="SIGNAL",
        bundles_dir=tmp_path / "bundles",
        work_dir=tmp_path / "work",
        signals_dir=tmp_path / "signals",
        ledger_path=ledger_path,
    )

    assert result["status"] == "FROZEN_ISOLATED_INPUT_BUNDLE"
    bundle = Path(result["bundle"])
    manifest, _manifest_sha = v43._validated_bundle(bundle, "SIGNAL")
    assert manifest["schema_version"] == 2
    assert manifest["runtime_isolation"]["shared_companyfacts_cache_modified"] is False
    qqq = pd.read_csv(bundle / "qqq.csv", parse_dates=["date"])
    assert qqq["date"].max() == pd.Timestamp("2026-08-31")
