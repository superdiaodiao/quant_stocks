from src.research.fundamentals_repair_batch import (
    coverage_snapshot,
    enforce_previous_batch_decision,
    repair_batch_decision,
)
from src.research import fundamentals_repair_batch
import json
import pandas as pd
import pytest


def test_coverage_snapshot_keeps_selector_level_metrics():
    result = coverage_snapshot({
        "missing_financial_observations": 100,
        "missing_financial_symbol_count": 20,
        "financial_coverage": 0.9,
    })

    assert result == {
        "missing_financial_observations": 100,
        "missing_financial_symbol_count": 20,
        "financial_coverage": 0.9,
    }
    assert coverage_snapshot({
        "missing_financial_observations": 100,
        "missing_financial_symbols": ["A", "B"],
        "financial_coverage": 0.9,
    })["missing_financial_symbol_count"] == 2


def test_repair_batch_decision_uses_measured_yield():
    assert repair_batch_decision(20, 60, 0)["recommended_action"] == (
        "CONTINUE_SAME_BATCH_SIZE"
    )
    assert repair_batch_decision(20, 20, 0)["recommended_action"] == (
        "REDUCE_BATCH_SIZE"
    )
    assert repair_batch_decision(20, 4, 0)["recommended_action"] == (
        "PAUSE_FETCH_AND_REVIEW_SOURCES"
    )
    assert repair_batch_decision(20, 60, 1)["recommended_action"] == (
        "REVIEW_FAILURES"
    )
    assert repair_batch_decision(0, 0, 0)["recommended_action"] == (
        "NO_FETCH_WORK"
    )


def test_previous_pause_blocks_unreviewed_batch(tmp_path):
    pd.DataFrame([{
        "requested_ciks": 10,
        "recommended_action": "PAUSE_FETCH_AND_REVIEW_SOURCES",
    }]).to_csv(tmp_path / "index.csv", index=False)

    with pytest.raises(RuntimeError, match="requires PAUSE"):
        enforce_previous_batch_decision(
            tmp_path, 5, override_stop=False
        )
    enforce_previous_batch_decision(
        tmp_path, 5, override_stop=True
    )


def test_previous_reduce_requires_smaller_limit(tmp_path):
    pd.DataFrame([{
        "requested_ciks": 20,
        "recommended_action": "REDUCE_BATCH_SIZE",
    }]).to_csv(tmp_path / "index.csv", index=False)

    with pytest.raises(RuntimeError, match="smaller --limit"):
        enforce_previous_batch_decision(
            tmp_path, 20, override_stop=False
        )
    enforce_previous_batch_decision(
        tmp_path, 10, override_stop=False
    )


def _batch_audit(batch_id, action, requested_ciks=10):
    return {
        "batch_id": batch_id,
        "as_of": "2026-07-31",
        "before": {"missing_financial_observations": 10},
        "after": {"missing_financial_observations": 9},
        "delta": {"recovered_observations": 1},
        "update": {
            "requested_ciks": requested_ciks,
            "failure_count": 0,
        },
        "decision": {
            "recovered_observations_per_requested_cik": 0.1,
            "recommended_action": action,
        },
        "audit_path": (
            "output/fundamentals_repair_batches/"
            f"{batch_id}.json"
        ),
    }


def test_authoritative_json_pause_blocks_when_index_is_stale(tmp_path):
    pd.DataFrame([{
        "batch_id": "20260730T210927Z",
        "requested_ciks": 20,
        "recommended_action": "CONTINUE_SAME_BATCH_SIZE",
    }]).to_csv(tmp_path / "index.csv", index=False)
    audit = _batch_audit(
        "20260730T211358Z",
        "PAUSE_FETCH_AND_REVIEW_SOURCES",
    )
    (tmp_path / f"{audit['batch_id']}.json").write_text(
        json.dumps(audit), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="requires PAUSE"):
        enforce_previous_batch_decision(
            tmp_path, 5, override_stop=False
        )


def test_index_rebuild_recovers_unindexed_json_audit(tmp_path):
    prior = _batch_audit(
        "20260730T210927Z",
        "CONTINUE_SAME_BATCH_SIZE",
        requested_ciks=20,
    )
    current = _batch_audit(
        "20260730T211358Z",
        "PAUSE_FETCH_AND_REVIEW_SOURCES",
    )
    (tmp_path / f"{prior['batch_id']}.json").write_text(
        json.dumps(prior), encoding="utf-8"
    )
    (tmp_path / f"{current['batch_id']}.json").write_text(
        json.dumps(current), encoding="utf-8"
    )

    path = fundamentals_repair_batch._update_index(tmp_path, current)
    rebuilt = pd.read_csv(path)

    assert rebuilt["batch_id"].astype(str).tolist() == [
        prior["batch_id"],
        current["batch_id"],
    ]
    assert rebuilt["recommended_action"].tolist() == [
        "CONTINUE_SAME_BATCH_SIZE",
        "PAUSE_FETCH_AND_REVIEW_SOURCES",
    ]
