import hashlib
import json
from pathlib import Path

import pytest

from src.conf import PROJECT_PATH


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_q1_fp_guard_candidate_sensitivity_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_q1_fp_guard_supersedes_the_old_2025_candidate_uplift() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    scope = evidence["scope"]
    candidate = evidence["candidate_files"]
    reference = evidence["reference_files"]
    replay = evidence["current_input_replay"]
    conclusions = evidence["conclusions"]

    assert evidence["research_only"] is True
    assert evidence["release_status"] == "BLOCKED"
    assert evidence["formal_validation_rerun"] is False
    assert evidence["formal_outputs_written"] is False
    assert scope["cache_manifest_sha256"] == (
        "6c8a87fcc71cfcd5cdefeb9470880787ca5e7a920802bcb6a5ca51844bfcd35b"
    )
    assert scope["rebuild_recipe_sha256"] == (
        "6f0998be33d325e5b673d26f9d96fd0ec556afdf923fa4fbcc2ac0634be43531"
    )
    assert scope["parser_sha256"] == (
        "ca195d5a26d3bae13d1a51372b0a616a3c9bc5e377a88293256cb82127527739"
    )

    for section in (candidate, reference):
        for name in ("annual", "quarterly"):
            path = Path(PROJECT_PATH) / section[f"{name}_path"]
            assert _sha256(path) == section[f"{name}_sha256"]
    assert reference["annual_sha256"] == (
        "62f6c624b2fac85118ea6d49646870f1a56fa053687cb617dc468d856e19c34d"
    )
    assert reference["quarterly_sha256"] == (
        "1be16a6342217d6771eca7d2ca49156e726b424ebfbfe7b90dfa6c232ea8bf69"
    )
    assert candidate["annual_sha256"] == (
        "24511dcb45eb1b84bbab306af75e765175e6b81dd280f07eec8beec6ddead5dc"
    )
    assert candidate["quarterly_sha256"] == (
        "aabcd5443ed4910fad656fb85ba43caa68ffd176baf735c1ce3497179206fd14"
    )

    assert replay["reference_wins_vs_nasdaq"] == 5
    assert replay["candidate_wins_vs_nasdaq"] == 5
    assert replay["changed_2025_signal_count"] == 0
    assert replay["changed_2025_signals"] == []
    assert replay["candidate_2025_strategy"] == pytest.approx(
        replay["reference_2025_strategy"]
    )
    assert replay["candidate_2025_strategy"] == pytest.approx(
        0.22847423969527347
    )
    assert replay["nasdaq_2025"] == pytest.approx(0.20357524453555675)

    checks = {row["ticker"]: row for row in evidence["pit_growth_feature_checks"]}
    assert checks["DAVE"]["candidate"]["revenue_ttm_growth"] == pytest.approx(
        checks["DAVE"]["reference"]["revenue_ttm_growth"]
    )
    assert checks["DAVE"]["candidate"]["revenue_ttm_growth"] == pytest.approx(
        0.3931043298818459
    )
    assert checks["COMM"]["candidate"]["revenue_ttm_growth"] == pytest.approx(
        0.1864993181473812
    )
    assert conclusions["old_2025_candidate_uplift_survives_q1_fp_guard"] is False
    assert conclusions["old_2025_selection_changes_survive_q1_fp_guard"] is False
    assert conclusions["candidate_changes_current_input_win_count"] is False
    assert conclusions["formal_4_of_6_result_revalidated_by_this_run"] is False

