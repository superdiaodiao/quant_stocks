from datetime import datetime, timezone

from src.research.shadow_ledger import (
    portfolio_source_columns,
    verify_shadow_ledger,
    write_shadow_ledger_manifest,
)


CANONICAL_GITHUB_ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "owner/repository",
    "GITHUB_WORKFLOW": "Fixed Top 3 shadow recommendations",
    "GITHUB_RUN_ID": "12345",
    "GITHUB_RUN_ATTEMPT": "2",
    "SHADOW_DEFAULT_BRANCH": "master",
    "GITHUB_REF": "refs/heads/master",
    "GITHUB_SHA": "a" * 40,
    "GITHUB_EVENT_NAME": "schedule",
}


def _history(tmp_path):
    path = tmp_path / "recommendation_history.csv"
    path.write_text(
        "as_of,ticker,target_weight\n2026-07-30,CASH,0.0\n",
        encoding="utf-8",
    )
    return path


def test_local_ledger_is_integral_but_not_externally_anchored(tmp_path):
    history = _history(tmp_path)
    write_shadow_ledger_manifest(
        history,
        environment={},
        sealed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )

    result = verify_shadow_ledger(history)

    assert result["status"] == "VERIFIED_LOCAL_UNANCHORED"
    assert result["integrity_verified"] is True
    assert result["externally_anchored"] is False


def test_github_actions_ledger_records_external_run_anchor(tmp_path):
    history = _history(tmp_path)
    write_shadow_ledger_manifest(history, environment=CANONICAL_GITHUB_ENV)

    result = verify_shadow_ledger(history)

    assert result["status"] == "VERIFIED_GITHUB_ACTIONS"
    assert result["integrity_verified"] is True
    assert result["externally_anchored"] is True
    assert result["source"]["previous_artifact_id"] is None
    assert result["source"]["run_url"].endswith("/actions/runs/12345")


def test_github_actions_ledger_carries_verified_ancestry(tmp_path):
    import hashlib
    import json

    history = _history(tmp_path)
    write_shadow_ledger_manifest(history, environment=CANONICAL_GITHUB_ENV)
    manifest_path = history.with_name(
        "recommendation_history.provenance.json"
    )
    previous_sha256 = hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()

    history.write_text(
        history.read_text(encoding="utf-8")
        + "2026-07-31,AAPL,1.0\n",
        encoding="utf-8",
    )
    write_shadow_ledger_manifest(history, environment={
        **CANONICAL_GITHUB_ENV,
        "GITHUB_RUN_ID": "12346",
        "GITHUB_RUN_ATTEMPT": "1",
        "SHADOW_PREVIOUS_ARTIFACT_ID": "987",
    })

    result = verify_shadow_ledger(history)

    assert result["status"] == "VERIFIED_GITHUB_ACTIONS"
    assert result["ancestry_verified"] is True
    assert result["ancestry_depth"] == 1
    assert [
        source["run_id"] for source in result["trusted_sources"]
    ] == ["12345", "12346"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["ancestry"][0]["artifact_id"] == "987"
    assert manifest["ancestry"][0]["manifest_sha256"] == previous_sha256


def test_tampered_ancestry_blocks_external_anchor(tmp_path):
    import json

    history = _history(tmp_path)
    write_shadow_ledger_manifest(history, environment=CANONICAL_GITHUB_ENV)
    manifest_path = history.with_name(
        "recommendation_history.provenance.json"
    )
    history.write_text(
        history.read_text(encoding="utf-8")
        + "2026-07-31,AAPL,1.0\n",
        encoding="utf-8",
    )
    write_shadow_ledger_manifest(history, environment={
        **CANONICAL_GITHUB_ENV,
        "GITHUB_RUN_ID": "12346",
        "SHADOW_PREVIOUS_ARTIFACT_ID": "987",
    })
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["ancestry"][0]["ledger_sha256"] = "not-a-sha256"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_shadow_ledger(history)

    assert result["ancestry_verified"] is False
    assert result["externally_anchored"] is False


def test_previous_artifact_requires_restored_manifest(tmp_path):
    import pytest

    history = _history(tmp_path)

    with pytest.raises(RuntimeError, match="without its provenance"):
        write_shadow_ledger_manifest(history, environment={
            **CANONICAL_GITHUB_ENV,
            "SHADOW_PREVIOUS_ARTIFACT_ID": "987",
        })


def test_schema_one_manifest_remains_verifiable_during_migration(tmp_path):
    import json

    history = _history(tmp_path)
    write_shadow_ledger_manifest(history, environment=CANONICAL_GITHUB_ENV)
    manifest_path = history.with_name(
        "recommendation_history.provenance.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest.pop("ancestry")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_shadow_ledger(history)

    assert result["status"] == "VERIFIED_GITHUB_ACTIONS"
    assert result["ancestry_verified"] is True
    assert result["ancestry_depth"] == 0


def test_portfolio_source_columns_capture_the_originating_run():
    result = portfolio_source_columns(CANONICAL_GITHUB_ENV)

    assert result == {
        "portfolio_source_kind": "github_actions_run",
        "portfolio_repository": "owner/repository",
        "portfolio_workflow": "Fixed Top 3 shadow recommendations",
        "portfolio_run_id": "12345",
        "portfolio_run_attempt": "2",
        "portfolio_run_url": (
            "https://github.com/owner/repository/actions/runs/12345"
        ),
        "portfolio_default_branch": "master",
        "portfolio_git_ref": "refs/heads/master",
        "portfolio_git_sha": "a" * 40,
        "portfolio_event_name": "schedule",
    }


def test_feature_branch_claim_is_not_an_external_anchor(tmp_path):
    history = _history(tmp_path)
    write_shadow_ledger_manifest(history, environment={
        **CANONICAL_GITHUB_ENV,
        "GITHUB_REF": "refs/heads/feature",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
    })

    result = verify_shadow_ledger(history)

    assert result["integrity_verified"] is True
    assert result["externally_anchored"] is False
    assert result["status"] == "VERIFIED_LOCAL_UNANCHORED"


def test_ledger_edit_after_sealing_is_detected(tmp_path):
    history = _history(tmp_path)
    write_shadow_ledger_manifest(history, environment={})
    history.write_text(
        "as_of,ticker,target_weight\n2026-07-30,AAPL,1.0\n",
        encoding="utf-8",
    )

    result = verify_shadow_ledger(history)

    assert result["status"] == "LEDGER_MISMATCH"
    assert result["integrity_verified"] is False
    assert result["externally_anchored"] is False


def test_duplicate_shadow_signal_is_rejected_even_with_valid_hash(tmp_path):
    history = tmp_path / "recommendation_history.csv"
    history.write_text(
        "as_of,ticker,model_version,target_weight\n"
        "2026-07-31,AAPL,can-slim-top3-v1,0.5\n"
        "2026-07-31,AAPL,can-slim-top3-v1,0.5\n",
        encoding="utf-8",
    )
    write_shadow_ledger_manifest(history, environment={})

    result = verify_shadow_ledger(history)

    assert result["status"] == "INVALID_LEDGER"
    assert result["integrity_verified"] is False
    assert result["hash_verified"] is True
    assert result["duplicate_key_count"] == 1


def test_existing_legacy_ledger_without_manifest_is_not_verified(tmp_path):
    history = _history(tmp_path)

    result = verify_shadow_ledger(history)

    assert result["status"] == "MISSING_MANIFEST"
    assert result["integrity_verified"] is False
    assert result["externally_anchored"] is False
