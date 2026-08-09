import pytest

from scripts.select_shadow_artifact import select_shadow_artifact


def _run(
    run_id,
    branch="master",
    conclusion="success",
    run_number=None,
):
    return {
        "id": run_id,
        "run_number": run_number or run_id,
        "head_branch": branch,
        "conclusion": conclusion,
    }


def _artifact(
    artifact_id,
    run_id,
    *,
    name=None,
    expired=False,
    created_at="2026-07-30T00:00:00Z",
):
    return {
        "id": artifact_id,
        "name": name or f"fixed-top3-shadow-{run_id}",
        "expired": expired,
        "created_at": created_at,
        "workflow_run": {"id": run_id},
    }


def test_selects_latest_artifact_from_exact_successful_default_branch_run():
    selected = select_shadow_artifact(
        {"workflow_runs": [_run(10), _run(20)]},
        {"artifacts": [
            _artifact(100, 10, created_at="2026-07-29T00:00:00Z"),
            _artifact(200, 20, created_at="2026-07-30T00:00:00Z"),
        ]},
        current_run_id=30,
        default_branch="master",
    )

    assert selected == 200


def test_selects_canonical_artifact_from_later_api_pages():
    selected = select_shadow_artifact(
        [
            {"workflow_runs": [_run(300, conclusion="failure")]},
            {"workflow_runs": [_run(200)]},
        ],
        [
            {"artifacts": [_artifact(303, 300, name="other-artifact")]},
            {"artifacts": [_artifact(202, 200)]},
        ],
        current_run_id=400,
        default_branch="master",
    )

    assert selected == 202


def test_ignores_same_name_from_other_workflow_or_feature_branch():
    selected = select_shadow_artifact(
        {"workflow_runs": [_run(10), _run(20, branch="feature")]},
        {"artifacts": [
            _artifact(999, 999),
            _artifact(200, 20),
            _artifact(100, 10),
        ]},
        current_run_id=30,
        default_branch="master",
    )

    assert selected == 100


def test_no_prior_canonical_artifact_allows_first_bootstrap():
    selected = select_shadow_artifact(
        {"workflow_runs": []},
        {"artifacts": [_artifact(999, 999)]},
        current_run_id=30,
        default_branch="master",
    )

    assert selected is None


def test_expired_canonical_history_cannot_silently_restart():
    with pytest.raises(RuntimeError, match="is expired"):
        select_shadow_artifact(
            {"workflow_runs": [_run(10)]},
            {"artifacts": [_artifact(100, 10, expired=True)]},
            current_run_id=30,
            default_branch="master",
        )


def test_older_rerun_artifact_cannot_roll_back_newer_run():
    selected = select_shadow_artifact(
        {"workflow_runs": [
            _run(10, run_number=10),
            _run(20, run_number=20),
        ]},
        {"artifacts": [
            _artifact(100, 10, created_at="2026-07-31T00:00:00Z"),
            _artifact(200, 20, created_at="2026-07-30T00:00:00Z"),
        ]},
        current_run_id=30,
        default_branch="master",
    )

    assert selected == 200


def test_latest_expired_artifact_cannot_fall_back_to_older_run():
    with pytest.raises(RuntimeError, match="refusing to fall back"):
        select_shadow_artifact(
            {"workflow_runs": [_run(10), _run(20)]},
            {"artifacts": [
                _artifact(100, 10),
                _artifact(200, 20, expired=True),
            ]},
            current_run_id=30,
            default_branch="master",
        )


def test_latest_run_requires_exact_canonical_artifact_name():
    with pytest.raises(RuntimeError, match="no canonical artifact"):
        select_shadow_artifact(
            {"workflow_runs": [_run(20)]},
            {"artifacts": [
                _artifact(
                    200,
                    20,
                    name="fixed-top3-shadow-20-lookalike",
                ),
            ]},
            current_run_id=30,
            default_branch="master",
        )
