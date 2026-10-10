from scripts.companyfacts_historical_source_audit import (
    audit_external_github_companyfacts_archive,
    audit_wayback_companyfacts_captures,
    _looks_like_companyfacts_path,
    _parse_unreachable_objects,
    audit_historical_companyfacts_sources,
)


def test_wayback_companyfacts_capture_inventory_is_non_mutating():
    def fetcher(_url):
        return [
            ["timestamp", "original", "statuscode", "digest", "length"],
            ["20250414082852", "https://example/companyfacts.zip", "200", "D", "128"],
        ]

    result = audit_wayback_companyfacts_captures(fetcher=fetcher)
    assert result["capture_count"] == 1
    assert result["captures"][0]["digest"] == "D"
    assert result["research_only"] is True


def test_external_github_archive_records_fixed_tree_and_cik_overlap():
    def github_fetcher(url):
        if "/git/trees/" in url:
            return {
                "truncated": False,
                "tree": [
                    {"path": "raw/companyfacts/CIK0000002488.json", "size": 10, "sha": "blob1"},
                    {"path": "raw/companyfacts/CIK0000006951.json", "size": 20, "sha": "blob2"},
                    {"path": "README.md", "size": 5, "sha": "readme"},
                ],
            }
        return {
            "sha": "commit1",
            "commit": {"author": {"date": "2026-07-15T10:04:49Z"}},
        }

    result = audit_external_github_companyfacts_archive(
        github_repository="example/archive",
        github_ref="commit1",
        required_ciks={2488, 1234},
        github_fetcher=github_fetcher,
    )

    assert result["commit_sha"] == "commit1"
    assert result["candidate_count"] == 2
    assert result["required_cik_overlap"] == [2488]
    assert result["required_cik_overlap_count"] == 1
    assert result["coverage"] == 0.5


def test_raw_cache_path_detection_ignores_source_code_names():
    assert _looks_like_companyfacts_path(
        "cache/CIK0000123456.json.gz"
    )
    assert _looks_like_companyfacts_path(
        "cache/raw_cache_refresh_state.json"
    )
    assert _looks_like_companyfacts_path(
        "cache/sec_companyfacts_cache/manifest.json"
    )
    assert not _looks_like_companyfacts_path(
        "tests/test_companyfacts_cache_snapshot.py"
    )


def test_parse_unreachable_objects_ignores_other_object_types():
    parsed = _parse_unreachable_objects([
        "unreachable blob blob-id",
        "unreachable tree tree-id",
        "unreachable commit commit-id",
        "dangling blob ignored-id",
    ])

    assert parsed == {"blob": ["blob-id"], "tree": ["tree-id"]}


def test_historical_source_audit_reports_no_candidate_without_raw_paths(tmp_path):
    def git_runner(_repo, *args):
        if args[:2] == ("fsck", "--no-reflogs"):
            return "unreachable blob blob-id\nunreachable tree tree-id\n"
        if args[:3] == ("ls-tree", "-r", "--name-only"):
            return "scripts/companyfacts_cache_snapshot.py\n"
        raise AssertionError(args)

    def blob_scanner(_repo, blob_ids):
        assert blob_ids == ["blob-id"]
        return {
            "unreachable_blob_count": 1,
            "unreachable_blob_bytes": 100,
            "scanned_blob_bytes": 100,
            "gzip_blob_candidate_count": 0,
            "gzip_blob_candidates": [],
            "companyfacts_json_candidate_count": 0,
            "companyfacts_json_candidates": [],
        }

    def github_fetcher(url):
        if url.endswith("/releases"):
            return [{"assets": [{"name": "data.tar.zst", "size": 10}]}]
        return {"artifacts": [{"name": "fixed-top3-shadow", "size_in_bytes": 10}]}

    result = audit_historical_companyfacts_sources(
        tmp_path,
        git_runner=git_runner,
        blob_scanner=blob_scanner,
        github_fetcher=github_fetcher,
    )

    assert result["candidate_source_count"] == 0
    assert result["recovery_status"] == "NO_HISTORICAL_RAW_SOURCE_FOUND"
    assert result["unreachable_tree_raw_cache_matches"] == []
    assert result["remote"]["raw_cache_release_asset_count"] == 0
    assert result["remote"]["raw_cache_workflow_artifact_count"] == 0


def test_historical_source_audit_surfaces_only_plausible_raw_candidates(tmp_path):
    def git_runner(_repo, *args):
        if args[:2] == ("fsck", "--no-reflogs"):
            return "unreachable blob blob-id\nunreachable tree tree-id\n"
        if args[:3] == ("ls-tree", "-r", "--name-only"):
            return "cache/sec_companyfacts_cache/CIK0000123456.json.gz\n"
        raise AssertionError(args)

    def blob_scanner(_repo, _blob_ids):
        return {
            "unreachable_blob_count": 1,
            "unreachable_blob_bytes": 100,
            "scanned_blob_bytes": 100,
            "gzip_blob_candidate_count": 1,
            "gzip_blob_candidates": [{"object_id": "blob-id", "bytes": 100}],
            "companyfacts_json_candidate_count": 0,
            "companyfacts_json_candidates": [],
        }

    result = audit_historical_companyfacts_sources(
        tmp_path,
        include_network=False,
        git_runner=git_runner,
        blob_scanner=blob_scanner,
    )

    assert result["candidate_source_count"] == 2
    assert result["recovery_status"] == "CANDIDATE_SOURCES_FOUND"
    assert result["unreachable_tree_raw_cache_matches"] == [{
        "tree": "tree-id",
        "paths": ["cache/sec_companyfacts_cache/CIK0000123456.json.gz"],
    }]
