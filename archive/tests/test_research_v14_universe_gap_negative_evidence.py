import hashlib
import json

import pytest

from scripts.research_v14_universe_gap_negative_evidence import (
    EXPECTED_2019_GITHUB_CATALOG_DATES,
    STALE_SIGNAL_DATES,
    TARGET_FILE_NAMES,
    build,
    classify_exact_searches,
    exact_search_queries,
    validate_local_archive_evidence,
)


def _write_json(path, payload) -> str:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _local_fixture_kwargs(tmp_path):
    audit = tmp_path / "audit.json"
    commoncrawl = tmp_path / "commoncrawl.json"
    github = tmp_path / "github.json"
    pinned = tmp_path / "pinned.json"
    audit_sha = _write_json(audit, {"price_audit": {
        "stale_signal_snapshot_dates": list(STALE_SIGNAL_DATES),
        "maximum_signal_snapshot_age_days": 30,
        "maximum_observed_signal_snapshot_age_days": 98,
    }})
    commoncrawl_sha = _write_json(commoncrawl, {
        "requested_years": [2015, 2020],
        "collection_count": 64,
        "checked_indexes": [f"index-{index}" for index in range(64)],
        "errors": [],
        "captures": [
            {"observed_at": f"2018-01-{index + 1:02d}"}
            for index in range(28)
        ] + [
            {"observed_at": f"2020-01-{index + 1:02d}"}
            for index in range(8)
        ],
    })
    github_sha = _write_json(github, {
        "query": '"File Creation Time:" filename:nasdaqlisted.txt',
        "search_result_count": 153,
        "records": [
            {"observed_at": date} for date in EXPECTED_2019_GITHUB_CATALOG_DATES
        ] + [
            {"observed_at": "2020-01-01"} for _ in range(116)
        ],
        "errors": [{"error": "missing Nasdaq header or file creation time"}],
    })
    pinned_sha = _write_json(pinned, {"snapshot": {
        "observed_at": "2019-07-15",
        "rows": 3462,
        "sha256": "a" * 64,
    }})
    return {
        "final_audit_path": audit,
        "final_audit_sha256": audit_sha,
        "commoncrawl_path": commoncrawl,
        "commoncrawl_sha256": commoncrawl_sha,
        "github_path": github,
        "github_sha256": github_sha,
        "pinned_path": pinned,
        "pinned_sha256": pinned_sha,
    }


def test_exact_search_envelope_covers_five_dates_and_two_file_names() -> None:
    rows = exact_search_queries()
    assert len(rows) == 10
    assert {row["signal_date"] for row in rows} == set(STALE_SIGNAL_DATES)
    assert {row["file_name"] for row in rows} == set(TARGET_FILE_NAMES)
    assert rows[0]["stamp"] == "03292019"
    assert rows[-1]["stamp"] == "09302019"


def test_local_archive_evidence_locks_all_64_indexes_and_no_2019_capture(
    tmp_path,
) -> None:
    evidence = validate_local_archive_evidence(**_local_fixture_kwargs(tmp_path))
    assert evidence["commoncrawl"] == {
        "checked_index_count": 64,
        "capture_count": 36,
        "capture_2019_count": 0,
        "error_count": 0,
    }
    assert evidence["github_broad_catalog"]["record_2019_dates"] == list(
        EXPECTED_2019_GITHUB_CATALOG_DATES
    )
    assert evidence["snapshot_policy"]["stale_signal_dates"] == list(
        STALE_SIGNAL_DATES
    )


def test_exact_zero_results_classify_source_exhaustion() -> None:
    rows = [{**row, "total_count": 0, "items": []} for row in exact_search_queries()]
    result = classify_exact_searches(rows)
    assert result["query_count"] == 10
    assert result["status"] == "NO_EXACT_DATE_CAPTURE_FOUND"


def test_exact_search_requires_complete_envelope_and_rejects_hits() -> None:
    rows = [{**row, "total_count": 0, "items": []} for row in exact_search_queries()]
    with pytest.raises(RuntimeError, match="envelope is incomplete"):
        classify_exact_searches(rows[:-1])
    rows[3]["total_count"] = 1
    rows[3]["items"] = [{"html_url": "https://example.test/candidate"}]
    with pytest.raises(RuntimeError, match="require review"):
        classify_exact_searches(rows)


def test_build_freezes_skip_dates_without_modifying_universe(tmp_path) -> None:
    calls = []

    def search(query):
        calls.append(query)
        return {"total_count": 0, "items": []}

    report = build(
        tmp_path / "negative.json",
        search=search,
        local_evidence_kwargs=_local_fixture_kwargs(tmp_path),
        checked_at="2026-08-29T10:40:00+00:00",
    )
    persisted = json.loads((tmp_path / "negative.json").read_text())
    assert len(calls) == 10
    assert report["classification"] == "SOURCE_EXHAUSTED_EXCLUDE_SIGNAL_DATES"
    assert report["execution_policy"]["excluded_signal_dates"] == list(
        STALE_SIGNAL_DATES
    )
    assert report["execution_policy"]["carry_prior_holdings_forward"]
    assert persisted["formal_universe_modified"] is False
    assert persisted["release_status"] == "BLOCKED"


def test_local_evidence_rejects_catalog_or_stale_date_drift(tmp_path) -> None:
    kwargs = _local_fixture_kwargs(tmp_path)
    kwargs["commoncrawl_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="binding changed"):
        validate_local_archive_evidence(**kwargs)
