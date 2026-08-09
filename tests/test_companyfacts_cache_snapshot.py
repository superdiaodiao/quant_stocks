import json
import os

import pandas as pd
import pytest

from scripts.companyfacts_cache_snapshot import (
    SNAPSHOT_METADATA_NAME,
    _companyfacts_full_rebuild_scope_identity,
    _default_rebuild_report_path,
    create_companyfacts_cache_snapshot,
    create_companyfacts_full_rebuild_scope,
    record_snapshot_full_rebuild_dry_run,
    verify_companyfacts_cache_snapshot,
)


def test_default_rebuild_report_path_is_scope_content_addressed():
    scope = {
        "format_version": 2,
        "created_at": "2026-08-01T00:00:00+00:00",
        "snapshot": {
            "snapshot_id": "manifest-example",
            "cache_manifest_sha256": "a" * 64,
        },
        "formal_outputs": {
            "annual": {"sha256": "b" * 64},
            "quarterly": {"sha256": "c" * 64},
        },
        "required_symbol_count": 2,
        "required_symbols_sha256": "d" * 64,
        "rebuild_recipe_sha256": "e" * 64,
    }
    identity = _companyfacts_full_rebuild_scope_identity(scope)
    path = _default_rebuild_report_path("manifest-example", scope)
    assert path.name == f"manifest-example-scope-{identity[:16]}.json"

    recreated = {**scope, "created_at": "2026-08-02T00:00:00+00:00"}
    assert _companyfacts_full_rebuild_scope_identity(recreated) == identity

    changed = {**scope, "rebuild_recipe_sha256": "f" * 64}
    assert _companyfacts_full_rebuild_scope_identity(changed) != identity
    assert _default_rebuild_report_path("manifest-example", changed) != path
from src.io.fundamentals_update import (
    _companyfacts_cache_path,
    _write_companyfacts_cache,
    companyfacts_full_rebuild_recipe,
    companyfacts_full_rebuild_recipe_sha256,
    load_companyfacts_full_rebuild_inputs,
    verify_companyfacts_cache_manifest,
    write_companyfacts_cache_manifest,
)


def _payload(value: int) -> dict:
    return {
        "cik": "0000000123",
        "entityName": "Example Corp",
        "facts": {"us-gaap": {"Example": {"value": value}}},
    }


def _active_cache(cache_dir) -> None:
    _write_companyfacts_cache(
        "EXMP",
        123,
        _payload(1),
        pd.Timestamp("2025-01-01T00:00:00Z"),
        cache_dir,
    )
    write_companyfacts_cache_manifest(cache_dir)
    assert verify_companyfacts_cache_manifest(cache_dir)["verified"] is True


def _formal_outputs(tmp_path):
    annual = tmp_path / "annual.csv"
    quarterly = tmp_path / "quarterly.csv"
    pd.DataFrame({"ticker": ["EXMP", "ANNUAL"]}).to_csv(annual, index=False)
    pd.DataFrame({"ticker": ["EXMP", "QUARTERLY"]}).to_csv(
        quarterly, index=False
    )
    return annual, quarterly


def test_snapshot_copies_manifest_bound_inputs_and_survives_active_refresh(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    active_payload = _companyfacts_cache_path(123, cache_dir)
    before = active_payload.read_bytes()

    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    snapshot_dir = tmp_path / "cache" / "snapshots" / snapshot["snapshot_id"]
    snapshot_payload = _companyfacts_cache_path(123, snapshot_dir)
    assert snapshot["reused"] is False
    assert snapshot["storage_method"] == "copy"
    assert snapshot_payload.read_bytes() == before
    assert os.stat(active_payload).st_ino != os.stat(snapshot_payload).st_ino
    assert verify_companyfacts_cache_snapshot(snapshot_dir)["verified"] is True

    _write_companyfacts_cache(
        "EXMP",
        123,
        _payload(2),
        pd.Timestamp("2025-02-01T00:00:00Z"),
        cache_dir,
    )
    write_companyfacts_cache_manifest(cache_dir)
    assert active_payload.read_bytes() != before
    assert snapshot_payload.read_bytes() == before
    assert os.stat(active_payload).st_ino != os.stat(snapshot_payload).st_ino
    assert verify_companyfacts_cache_snapshot(snapshot_dir)["verified"] is True


def test_snapshot_copy_isolated_from_in_place_sidecar_refresh(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    sidecar = cache_dir / "historical_ticker_ciks.json"
    sidecar.write_text('{"EXMP": 123}\n', encoding="utf-8")
    write_companyfacts_cache_manifest(cache_dir)

    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    snapshot_dir = cache_dir / "snapshots" / snapshot["snapshot_id"]
    snapshot_sidecar = snapshot_dir / "historical_ticker_ciks.json"
    before = snapshot_sidecar.read_bytes()

    sidecar.write_text('{"EXMP": 123, "OLD": 456}\n', encoding="utf-8")
    assert snapshot_sidecar.read_bytes() == before
    with pytest.raises(RuntimeError, match="historical ticker CIK integrity mismatch"):
        verify_companyfacts_cache_manifest(cache_dir)


def test_snapshot_is_idempotent_for_the_same_manifest(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)

    first = create_companyfacts_cache_snapshot(cache_dir)
    second = create_companyfacts_cache_snapshot(cache_dir)

    assert first["snapshot_id"] == second["snapshot_id"]
    assert second["reused"] is True
    assert verify_companyfacts_cache_snapshot(
        cache_dir / "snapshots" / first["snapshot_id"]
    )["referenced_file_count"] == first["referenced_file_count"]


def test_snapshot_verification_rejects_replaced_tampered_payload(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    snapshot_dir = cache_dir / "snapshots" / snapshot["snapshot_id"]
    payload_path = _companyfacts_cache_path(123, snapshot_dir)
    replacement = payload_path.with_suffix(payload_path.suffix + ".replacement")
    replacement.write_bytes(b"tampered")
    os.replace(replacement, payload_path)

    with pytest.raises(ValueError, match="snapshot file hash mismatch"):
        verify_companyfacts_cache_snapshot(snapshot_dir)
    assert verify_companyfacts_cache_manifest(cache_dir)["verified"] is True


def test_snapshot_metadata_never_claims_formal_output_reproducibility(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    metadata = json.loads(
        (cache_dir / "snapshots" / snapshot["snapshot_id"] / SNAPSHOT_METADATA_NAME).read_text(
            encoding="utf-8"
        )
    )

    assert metadata["research_only"] is True
    assert "does not assert" in metadata["warning"]


def test_full_rebuild_scope_binds_snapshot_and_exact_formal_ticker_union(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    snapshot_dir = cache_dir / "snapshots" / snapshot["snapshot_id"]
    annual, quarterly = _formal_outputs(tmp_path)
    scope_path = tmp_path / "scope.json"

    result = create_companyfacts_full_rebuild_scope(
        snapshot_dir,
        scope_path=scope_path,
        annual_output=annual,
        quarterly_output=quarterly,
    )
    inputs = load_companyfacts_full_rebuild_inputs(snapshot_dir, scope_path)

    assert result["research_only"] is True
    assert result["reused"] is False
    assert inputs["required_symbols"] == ["ANNUAL", "EXMP", "QUARTERLY"]
    assert inputs["scope"]["formal_outputs"]["annual"]["sha256"]
    assert inputs["cache_manifest_sha256"] == snapshot["cache_manifest_sha256"]
    assert inputs["rebuild_recipe_bound"] is True
    assert inputs["rebuild_recipe"] == companyfacts_full_rebuild_recipe()
    assert inputs["rebuild_recipe_sha256"] == (
        companyfacts_full_rebuild_recipe_sha256()
    )

    reused = create_companyfacts_full_rebuild_scope(
        snapshot_dir,
        scope_path=scope_path,
        annual_output=annual,
        quarterly_output=quarterly,
    )
    assert reused["reused"] is True


def test_full_rebuild_scope_rejects_ticker_hash_tampering(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    snapshot_dir = cache_dir / "snapshots" / snapshot["snapshot_id"]
    annual, quarterly = _formal_outputs(tmp_path)
    scope_path = tmp_path / "scope.json"
    create_companyfacts_full_rebuild_scope(
        snapshot_dir,
        scope_path=scope_path,
        annual_output=annual,
        quarterly_output=quarterly,
    )
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    scope["required_symbols"] = ["MUTATED"]
    scope["required_symbol_count"] = 1
    scope_path.write_text(json.dumps(scope), encoding="utf-8")

    with pytest.raises(ValueError, match="ticker hash"):
        load_companyfacts_full_rebuild_inputs(snapshot_dir, scope_path)


def test_full_rebuild_scope_rejects_recipe_hash_tampering(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    snapshot_dir = cache_dir / "snapshots" / snapshot["snapshot_id"]
    annual, quarterly = _formal_outputs(tmp_path)
    scope_path = tmp_path / "scope.json"
    create_companyfacts_full_rebuild_scope(
        snapshot_dir,
        scope_path=scope_path,
        annual_output=annual,
        quarterly_output=quarterly,
    )
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    scope["rebuild_recipe"]["runtime"]["pandas_version"] = "tampered"
    scope_path.write_text(json.dumps(scope), encoding="utf-8")

    with pytest.raises(ValueError, match="recipe hash does not match"):
        load_companyfacts_full_rebuild_inputs(snapshot_dir, scope_path)


def test_full_rebuild_scope_retains_legacy_readability_but_never_upgrades_in_place(
    tmp_path,
):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    snapshot_dir = cache_dir / "snapshots" / snapshot["snapshot_id"]
    annual, quarterly = _formal_outputs(tmp_path)
    scope_path = tmp_path / "scope.json"
    create_companyfacts_full_rebuild_scope(
        snapshot_dir,
        scope_path=scope_path,
        annual_output=annual,
        quarterly_output=quarterly,
    )
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    scope["format_version"] = 1
    scope.pop("rebuild_recipe")
    scope.pop("rebuild_recipe_sha256")
    scope_path.write_text(json.dumps(scope), encoding="utf-8")

    inputs = load_companyfacts_full_rebuild_inputs(snapshot_dir, scope_path)
    assert inputs["rebuild_recipe_bound"] is False
    assert inputs["rebuild_recipe"] is None

    with pytest.raises(FileExistsError, match="legacy unbound format"):
        create_companyfacts_full_rebuild_scope(
            snapshot_dir,
            scope_path=scope_path,
            annual_output=annual,
            quarterly_output=quarterly,
        )


def test_full_rebuild_inputs_reject_nonimmutable_snapshot_metadata(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    snapshot_dir = cache_dir / "snapshots" / snapshot["snapshot_id"]
    annual, quarterly = _formal_outputs(tmp_path)
    scope_path = tmp_path / "scope.json"
    create_companyfacts_full_rebuild_scope(
        snapshot_dir,
        scope_path=scope_path,
        annual_output=annual,
        quarterly_output=quarterly,
    )
    metadata_path = snapshot_dir / SNAPSHOT_METADATA_NAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["storage_method"] = "untrusted"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="copied or legacy hard-linked"):
        load_companyfacts_full_rebuild_inputs(snapshot_dir, scope_path)


def test_snapshot_rebuild_provenance_requires_a_nonmutating_dry_run(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    snapshot_dir = cache_dir / "snapshots" / snapshot["snapshot_id"]
    annual, quarterly = _formal_outputs(tmp_path)
    scope_path = tmp_path / "scope.json"
    create_companyfacts_full_rebuild_scope(
        snapshot_dir,
        scope_path=scope_path,
        annual_output=annual,
        quarterly_output=quarterly,
    )
    report_path = tmp_path / "provenance.json"
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        return {
            "dry_run": True,
            "formal_outputs_written": False,
            "annual_output_written": False,
            "quarterly_output_written": False,
            "parsed_outputs_written": False,
            "rebuild_recipe_matched": True,
            "annual_comparison": {"exact_byte_match": False},
        }

    result = record_snapshot_full_rebuild_dry_run(
        snapshot_dir,
        scope_path=scope_path,
        report_path=report_path,
        annual_output=annual,
        quarterly_output=quarterly,
        runner=runner,
    )

    assert calls[0]["cache_dir"] == snapshot_dir
    assert result["formal_outputs_written"] is False
    assert calls[0]["required_symbols"] == ["ANNUAL", "EXMP", "QUARTERLY"]
    assert calls[0]["include_ticker_deltas"] is True
    assert calls[0]["expected_rebuild_recipe_sha256"] == (
        companyfacts_full_rebuild_recipe_sha256()
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["format_version"] == 2
    assert report["research_only"] is True
    assert report["snapshot"]["snapshot_id"] == snapshot["snapshot_id"]
    assert report["scope"]["verified"] is True
    assert report["scope"]["rebuild_recipe_sha256"] == (
        companyfacts_full_rebuild_recipe_sha256()
    )
    assert report["dry_run"]["annual_comparison"]["exact_byte_match"] is False


def test_snapshot_rebuild_provenance_isolates_formal_outputs_from_runner(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    snapshot_dir = cache_dir / "snapshots" / snapshot["snapshot_id"]
    annual, quarterly = _formal_outputs(tmp_path)
    scope_path = tmp_path / "scope.json"
    create_companyfacts_full_rebuild_scope(
        snapshot_dir,
        scope_path=scope_path,
        annual_output=annual,
        quarterly_output=quarterly,
    )
    annual_before = annual.read_bytes()
    quarterly_before = quarterly.read_bytes()

    def runner(*, output, quarterly_output, **_kwargs):
        assert output != annual
        assert quarterly_output != quarterly
        output.write_text("temporary annual mutation", encoding="utf-8")
        quarterly_output.write_text("temporary quarterly mutation", encoding="utf-8")
        return {
            "dry_run": True,
            "formal_outputs_written": False,
            "annual_output_written": False,
            "quarterly_output_written": False,
            "parsed_outputs_written": False,
            "rebuild_recipe_matched": True,
        }

    record_snapshot_full_rebuild_dry_run(
        snapshot_dir,
        scope_path=scope_path,
        report_path=tmp_path / "provenance.json",
        annual_output=annual,
        quarterly_output=quarterly,
        runner=runner,
    )

    assert annual.read_bytes() == annual_before
    assert quarterly.read_bytes() == quarterly_before


def test_snapshot_rebuild_provenance_rejects_a_mutating_runner(tmp_path):
    cache_dir = tmp_path / "cache"
    _active_cache(cache_dir)
    snapshot = create_companyfacts_cache_snapshot(cache_dir)
    snapshot_dir = cache_dir / "snapshots" / snapshot["snapshot_id"]
    annual, quarterly = _formal_outputs(tmp_path)
    scope_path = tmp_path / "scope.json"
    create_companyfacts_full_rebuild_scope(
        snapshot_dir,
        scope_path=scope_path,
        annual_output=annual,
        quarterly_output=quarterly,
    )
    report_path = tmp_path / "provenance.json"

    with pytest.raises(ValueError, match="formal output write"):
        record_snapshot_full_rebuild_dry_run(
            snapshot_dir,
            scope_path=scope_path,
            report_path=report_path,
            annual_output=annual,
            quarterly_output=quarterly,
            runner=lambda **_kwargs: {
                "dry_run": True,
                "formal_outputs_written": True,
            },
        )
    assert not report_path.exists()
