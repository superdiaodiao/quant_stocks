import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from src.conf import PROJECT_PATH
from src.financial.quarterly_fundamentals import (
    load_quarterly_fundamentals,
    quarterly_growth_snapshot,
)


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_complete_cache_candidate_sensitivity_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_complete_cache_candidate_replays_pit_fact_checks_without_formal_writes() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    candidate = evidence["candidate_files"]
    formal = evidence["formal_files_unchanged"]
    annual_path = Path(PROJECT_PATH) / candidate["annual_path"]
    quarterly_path = Path(PROJECT_PATH) / candidate["quarterly_path"]

    assert evidence["research_only"] is True
    assert evidence["formal_outputs_written"] is False
    assert evidence["release_status"] == "BLOCKED"
    assert _sha256(annual_path) == candidate["annual_sha256"]
    assert _sha256(quarterly_path) == candidate["quarterly_sha256"]
    assert _sha256(
        Path(PROJECT_PATH) / "cleaned_stocks_data/financial/fundamentals_point_in_time.csv"
    ) == formal["annual_sha256"]
    assert _sha256(
        Path(PROJECT_PATH)
        / "cleaned_stocks_data/financial/quarterly_fundamentals_point_in_time.csv"
    ) == formal["quarterly_sha256"]

    candidate_frame = load_quarterly_fundamentals(quarterly_path)
    formal_frame = load_quarterly_fundamentals(
        Path(PROJECT_PATH)
        / "cleaned_stocks_data/financial/quarterly_fundamentals_point_in_time.csv"
    )
    for check in evidence["pit_growth_feature_checks"]:
        signal_date = pd.Timestamp(check["signal_date"])
        candidate_growth = quarterly_growth_snapshot(candidate_frame, signal_date).loc[
            check["ticker"]
        ]
        formal_growth = quarterly_growth_snapshot(formal_frame, signal_date).loc[
            check["ticker"]
        ]
        assert candidate_growth["growth_available_date"] == pd.Timestamp(
            check["growth_available_date"]
        )
        assert candidate_growth["financial_age_days"] == check["financial_age_days"]
        assert candidate_growth["revenue_growth"] == pytest.approx(
            check["candidate_revenue_ttm_growth"]
        )
        assert formal_growth["revenue_growth"] == pytest.approx(
            check["formal_revenue_ttm_growth"]
        )
        assert candidate_growth["net_income_growth"] == pytest.approx(
            check["candidate_net_income_ttm_growth"]
        )
        assert formal_growth["net_income_growth"] == pytest.approx(
            check["formal_net_income_ttm_growth"]
        )

    sensitivity = evidence["in_memory_fixed_parameter_sensitivity"]
    assert sensitivity["formal_wins_vs_nasdaq"] == 4
    assert sensitivity["candidate_wins_vs_nasdaq"] == 5
    assert sensitivity["candidate_failed_years"] == [2023]
