import hashlib
import json
from pathlib import Path

import pandas as pd

from src.conf import PROJECT_PATH
from src.io.fundamentals_update import (
    _read_companyfacts_cache,
    parse_companyfacts_quarterly,
)


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_near_duplicate_quarter_end_sensitivity_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_adjacent_fiscal_end_rebuild_replays_avav_q4_from_raw_operands() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    snapshot = Path(PROJECT_PATH) / (
        "cleaned_stocks_data/financial/sec_companyfacts_cache/snapshots/"
        "manifest-6c8a87fcc71cfcd5"
    )
    payload, fetched_at = _read_companyfacts_cache(1368622, snapshot)

    quarterly = parse_companyfacts_quarterly("AVAV", payload, fetched_at)
    recovered = quarterly.loc[
        quarterly["fiscal_end"].eq(pd.Timestamp("2021-04-30"))
        & quarterly["metric"].eq("net_income")
    ]

    assert set(recovered["value"]) == {10_972_000.0}
    assert {
        "0001558370-21-008684",
        "0001558370-22-010392",
        "0001558370-23-011469",
    }.issubset(set(recovered["accession"]))
    assert evidence["formula_audit"]["formula_match_count_after"] == 73_699
    assert evidence["formula_audit"]["formula_failure_count_after"] == 6_703


def test_adjacent_fiscal_end_candidate_and_sensitivity_are_hash_bound() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert evidence["research_only"] is True
    assert evidence["release_status"] == "BLOCKED"
    for section, path_key, sha_key in (
        ("scope", "path", "sha256"),
        ("formula_audit", "path", "sha256"),
        ("release_selection", "path", "sha256"),
        ("base_candidate", "annual_path", "annual_sha256"),
        ("base_candidate", "quarterly_path", "quarterly_sha256"),
        ("proven_fallback_layer", "annual_path", "annual_sha256"),
        ("proven_fallback_layer", "quarterly_path", "quarterly_sha256"),
    ):
        record = evidence[section]
        path = Path(PROJECT_PATH) / record[path_key]
        assert _sha256(path) == record[sha_key]

    comparison = evidence["normalized_formal_comparison"]
    assert comparison["prior_exact_pit_keys_missing"] == 175
    assert comparison["current_exact_pit_keys_missing"] == 87
    assert comparison["prior_no_candidate_fiscal_metric"] == 40
    assert comparison["current_no_candidate_fiscal_metric"] == 35
    sensitivity = evidence["in_memory_fixed_parameter_sensitivity_2021_2026"]
    assert sensitivity["wins_vs_nasdaq"] == 5
    assert sensitivity["failed_years"] == [2023]
    assert sensitivity["strategy_sha256"] == (
        "736b28e72f368a48bd815f5a50fa52dc"
        "da554fed88a971f56a652e1bba35f2f6"
    )
