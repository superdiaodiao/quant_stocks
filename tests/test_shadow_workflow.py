from pathlib import Path


WORKFLOW = Path(".github/workflows/workflow_run_script.yml")


def test_scheduled_shadow_workflow_restores_and_seals_cumulative_ledger():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'cron: "30 0 * * *"' in workflow
    assert "Sunday/Monday are intentional retries" in workflow
    assert "actions: read" in workflow
    assert "artifact-metadata: write" in workflow
    assert "attestations: write" in workflow
    assert "id-token: write" in workflow
    assert "Restore cumulative shadow ledger" in workflow
    assert "recommendation_history.csv" in workflow
    assert "sha256sum --check recommendation_history.sha256" in workflow
    assert (
        "Prior shadow artifact lacks ${required_file}; "
        "refusing to re-attest unverified history."
    ) in workflow
    assert "recommendation_history.attestation.json" in workflow
    assert "verify_shadow_ledger" in workflow
    assert 'if not result["externally_anchored"]' in workflow
    assert "SHADOW_PREVIOUS_ARTIFACT_ID=" in workflow
    assert "Seal cumulative shadow ledger" in workflow
    assert (
        "sha256sum recommendation_history.csv "
        "> recommendation_history.sha256"
    ) in workflow
    assert "retention-days: 90" in workflow
    assert "uses: actions/attest@v4" in workflow
    assert "recommendation_history.provenance.json" in workflow
    assert "gh attestation verify" in workflow
    assert "--cert-identity" in workflow
    assert "SHADOW_DEFAULT_BRANCH" in workflow
    assert "actions/workflows/workflow_run_script.yml/runs" in workflow
    assert "scripts/select_shadow_artifact.py" in workflow
    assert workflow.count("--paginate") >= 2
    assert workflow.count("--slurp") >= 2
    assert "@refs/heads/" in workflow
    assert "recommendation_history.attestation.json" in workflow
    assert workflow.index(
        "Verify signed shadow ledger provenance"
    ) < workflow.index("Upload shadow recommendation artifact")


def test_shadow_workflow_fails_if_prior_artifact_has_ambiguous_ledgers():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'if [[ "${#histories[@]}" -ne 1 ]]' in workflow
    assert (
        'Expected one prior recommendation history, '
        'found ${#histories[@]}.'
    ) in workflow
