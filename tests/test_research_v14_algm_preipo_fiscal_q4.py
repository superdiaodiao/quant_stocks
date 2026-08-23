from pathlib import Path

import pandas as pd

from scripts.research_v14_algm_preipo_fiscal_q4 import (
    EXPECTED_Q4,
    EXPECTED_Q4_DATES,
    S1_EXPECTED,
    extract_s1_values,
    run,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.io.fundamentals_update import merge_fundamentals


SOURCE = Path("output/data_provenance/algm_2020_ipo/algm_2020_s1.htm")
BASE = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_latest_service_revenue_coordinates_zs_"
    "adpt_preipo_bbio_arct_krys_ozk_profit/quarterly.csv"
)


def test_algm_extracts_explicit_s1_periods():
    assert extract_s1_values(SOURCE) == S1_EXPECTED


def test_algm_q4_residuals_preserve_input_dates(tmp_path: Path):
    report = run(output_dir=tmp_path)
    assert report["q4_residuals"] == EXPECTED_Q4
    assert report["q4_available_dates"] == EXPECTED_Q4_DATES
    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert len(facts) == 8
    q4 = facts[facts["fiscal_end"].isin(EXPECTED_Q4)]
    assert dict(zip(q4["fiscal_end"], q4["available_date"])) == EXPECTED_Q4_DATES


def test_algm_growth_window_resolves_only_after_first_10k(tmp_path: Path):
    run(output_dir=tmp_path)
    merged = merge_fundamentals(
        pd.read_csv(BASE), pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    )
    before = quarterly_growth_snapshot(
        merged, pd.Timestamp("2021-04-30"), maximum_age_days=550
    )
    after = quarterly_growth_snapshot(
        merged, pd.Timestamp("2021-10-29"), maximum_age_days=550
    )
    assert "ALGM" not in before.index
    assert after.loc["ALGM", "fiscal_end"] == pd.Timestamp("2021-09-24")
    assert after.loc["ALGM", "growth_available_date"] == pd.Timestamp("2021-10-29")
