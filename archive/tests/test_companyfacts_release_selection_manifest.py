import pandas as pd
import pytest
from pathlib import Path

from scripts import companyfacts_release_selection_manifest as manifest
from src.io.fundamentals_update import OUTPUT_COLUMNS


def _row(**overrides):
    values = {
        "ticker": "ABC",
        "fiscal_end": "2024-03-31",
        "available_date": "2024-05-01",
        "metric": "net_income",
        "value": "10.0",
        "taxonomy": "us-gaap",
        "concept": "NetIncomeLoss",
        "form": "10-Q",
        "accession": "0000000000-24-000001",
        "fetched_at": "2026-07-30",
    }
    values.update(overrides)
    return values


def _patch_environment(monkeypatch):
    monkeypatch.setattr(
        manifest,
        "verify_companyfacts_cache_snapshot",
        lambda path: {
            "snapshot_id": "manifest-test",
            "cache_manifest_sha256": "a" * 64,
            "referenced_file_count": 1,
        },
    )
    monkeypatch.setattr(
        manifest,
        "companyfacts_full_rebuild_recipe_sha256",
        lambda: "b" * 64,
    )
    monkeypatch.setattr(
        manifest,
        "_raw_sources_for_requests",
        lambda snapshot, requests: {
            "ABC": {
                manifest._raw_key(
                    "us-gaap",
                    "NetIncomeLoss",
                    "2024-03-31",
                    "2024-05-01",
                    "10-Q",
                    "0000000000-24-000001",
                    "10.0",
                ): 123,
            }
        },
    )


def test_create_binds_raw_rows_and_marks_derived_rows(tmp_path, monkeypatch):
    _patch_environment(monkeypatch)
    annual = tmp_path / "annual.csv"
    quarterly = tmp_path / "quarterly.csv"
    output = tmp_path / "selection.jsonl.gz"
    pd.DataFrame([_row()])[OUTPUT_COLUMNS].to_csv(annual, index=False)
    pd.DataFrame(
        [_row(concept="derived_q4:NetIncomeLoss", value="4.0")]
    )[OUTPUT_COLUMNS].to_csv(quarterly, index=False)

    result = manifest.create_release_selection_manifest(
        "snapshot",
        annual_output=annual,
        quarterly_output=quarterly,
        output=output,
    )

    assert result["evidence_counts"] == {
        "annual": {"raw": 1},
        "quarterly": {"derived_unproven": 1},
    }
    _, records = manifest._iter_manifest(output)
    rows = list(records)
    assert rows[0]["evidence"]["source_cik"] == 123
    assert rows[1]["evidence"]["type"] == "derived_unproven"


def test_replay_fails_closed_on_unproven_derived_rows(tmp_path, monkeypatch):
    _patch_environment(monkeypatch)
    annual = tmp_path / "annual.csv"
    quarterly = tmp_path / "quarterly.csv"
    manifest_path = tmp_path / "selection.jsonl.gz"
    out_annual = tmp_path / "rebuilt-annual.csv"
    out_quarterly = tmp_path / "rebuilt-quarterly.csv"
    pd.DataFrame([_row()])[OUTPUT_COLUMNS].to_csv(annual, index=False)
    pd.DataFrame(
        [_row(concept="derived_q4:NetIncomeLoss", value="4.0")]
    )[OUTPUT_COLUMNS].to_csv(quarterly, index=False)
    manifest.create_release_selection_manifest(
        "snapshot",
        annual_output=annual,
        quarterly_output=quarterly,
        output=manifest_path,
    )

    with pytest.raises(RuntimeError, match="unproven derived"):
        manifest.replay_release_selection_manifest(
            "snapshot",
            manifest=manifest_path,
            annual_output=out_annual,
            quarterly_output=out_quarterly,
        )
    assert not out_annual.exists()
    assert not out_quarterly.exists()


def test_replay_explicitly_allows_diagnostic_unproven_rows(tmp_path, monkeypatch):
    _patch_environment(monkeypatch)
    annual = tmp_path / "annual.csv"
    quarterly = tmp_path / "quarterly.csv"
    manifest_path = tmp_path / "selection.jsonl.gz"
    out_annual = tmp_path / "rebuilt-annual.csv"
    out_quarterly = tmp_path / "rebuilt-quarterly.csv"
    pd.DataFrame([_row()])[OUTPUT_COLUMNS].to_csv(annual, index=False)
    pd.DataFrame(
        [_row(concept="derived_q4:NetIncomeLoss", value="4.0")]
    )[OUTPUT_COLUMNS].to_csv(quarterly, index=False)
    manifest.create_release_selection_manifest(
        "snapshot",
        annual_output=annual,
        quarterly_output=quarterly,
        output=manifest_path,
    )

    result = manifest.replay_release_selection_manifest(
        "snapshot",
        manifest=manifest_path,
        annual_output=out_annual,
        quarterly_output=out_quarterly,
        allow_unproven_derived=True,
    )
    assert result["unproven_derived_rows"] == 1
    assert out_annual.read_bytes() == annual.read_bytes()
    assert out_quarterly.read_bytes() == quarterly.read_bytes()


def test_replay_can_exclude_unproven_derived_rows(tmp_path, monkeypatch):
    _patch_environment(monkeypatch)
    annual = tmp_path / "annual.csv"
    quarterly = tmp_path / "quarterly.csv"
    manifest_path = tmp_path / "selection.jsonl.gz"
    out_annual = tmp_path / "rebuilt-annual.csv"
    out_quarterly = tmp_path / "rebuilt-quarterly.csv"
    pd.DataFrame([_row()])[OUTPUT_COLUMNS].to_csv(annual, index=False)
    pd.DataFrame(
        [
            _row(concept="derived_q4:NetIncomeLoss", value="4.0"),
            _row(),
        ]
    )[OUTPUT_COLUMNS].to_csv(quarterly, index=False)
    manifest.create_release_selection_manifest(
        "snapshot",
        annual_output=annual,
        quarterly_output=quarterly,
        output=manifest_path,
    )

    result = manifest.replay_release_selection_manifest(
        "snapshot",
        manifest=manifest_path,
        annual_output=out_annual,
        quarterly_output=out_quarterly,
        exclude_unproven_derived=True,
    )

    assert result["unproven_derived_rows"] == 1
    assert result["excluded_unproven_derived_rows"] == 1
    assert result["quarterly_rows"] == 1
    assert result["manifest_quarterly_rows"] == 2
    rebuilt = pd.read_csv(out_quarterly)
    assert rebuilt["ticker"].tolist() == ["ABC"]


def test_replay_rejects_conflicting_unproven_modes(tmp_path, monkeypatch):
    _patch_environment(monkeypatch)

    with pytest.raises(ValueError, match="mutually exclusive"):
        manifest.replay_release_selection_manifest(
            "snapshot",
            manifest=tmp_path / "unused.jsonl.gz",
            annual_output=tmp_path / "annual.csv",
            quarterly_output=tmp_path / "quarterly.csv",
            allow_unproven_derived=True,
            exclude_unproven_derived=True,
        )


def test_formula_proof_is_bound_and_replay_accepts_only_proven_derived_rows(
    tmp_path, monkeypatch
):
    _patch_environment(monkeypatch)
    annual = tmp_path / "annual.csv"
    quarterly = tmp_path / "quarterly.csv"
    manifest_path = tmp_path / "selection.jsonl.gz"
    out_annual = tmp_path / "rebuilt-annual.csv"
    out_quarterly = tmp_path / "rebuilt-quarterly.csv"
    annual_row = _row()
    quarterly_row = _row(concept="derived_q4:NetIncomeLoss", value="4.0")
    pd.DataFrame([annual_row])[OUTPUT_COLUMNS].to_csv(annual, index=False)
    pd.DataFrame([quarterly_row])[OUTPUT_COLUMNS].to_csv(quarterly, index=False)
    proof_values = manifest._row_values(quarterly_row)
    proof = {
        "dataset": "quarterly",
        "ordinal": 0,
        "row_sha256": manifest._row_sha256(proof_values),
        "matched": True,
        "reason": "",
        "operand_count": 4,
    }
    monkeypatch.setattr(
        manifest,
        "_load_formula_audit_proofs",
        lambda *args, **kwargs: (
            {manifest._row_identity("quarterly", 0, proof_values): proof},
            {"path": "formula.json", "sha256": "c" * 64, "format_version": 2,
             "row_proof_count": 1},
        ),
    )
    manifest.create_release_selection_manifest(
        "snapshot",
        annual_output=annual,
        quarterly_output=quarterly,
        output=manifest_path,
        formula_audit="formula.json",
    )
    _, records = manifest._iter_manifest(manifest_path)
    rows = list(records)
    assert rows[1]["evidence"]["type"] == "derived_proven"
    result = manifest.replay_release_selection_manifest(
        "snapshot",
        manifest=manifest_path,
        annual_output=out_annual,
        quarterly_output=out_quarterly,
    )
    assert result["unproven_derived_rows"] == 0
    assert out_quarterly.read_bytes() == quarterly.read_bytes()


def test_raw_source_resolution_streams_payloads_and_prefers_chain_order(monkeypatch):
    monkeypatch.setattr(
        manifest,
        "cached_companyfacts_cik_chains_for_symbols",
        lambda symbols, snapshot: {"ABC": (200, 100)},
    )
    payload = {
        "facts": {
            "us-gaap": {
                "Revenue": {
                    "units": {
                        "USD": [{
                            "end": "2024-03-31",
                            "filed": "2024-05-01",
                            "form": "10-Q",
                            "accn": "acc",
                            "val": 10,
                        }]
                    }
                }
            }
        }
    }
    calls = []
    monkeypatch.setattr(
        manifest,
        "_read_companyfacts_cache",
        lambda cik, snapshot: (calls.append(cik) or (payload, pd.Timestamp("2026-01-01"))),
    )
    key = manifest._raw_key(
        "us-gaap", "Revenue", "2024-03-31", "2024-05-01", "10-Q", "acc", "10"
    )
    result = manifest._raw_sources_for_requests(Path("snapshot"), {"ABC": {key}})
    assert result == {"ABC": {key: 200}}
    assert calls == [200, 100]


def test_raw_key_treats_integer_and_decimal_sec_values_as_equal():
    assert manifest._raw_key(
        "us-gaap", "Revenue", "2024-03-31", "2024-05-01", "10-Q", "acc", 10
    ) == manifest._raw_key(
        "us-gaap", "Revenue", "2024-03-31", "2024-05-01", "10-Q", "acc", "10.0"
    )
