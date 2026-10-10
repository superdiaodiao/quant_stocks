from pathlib import Path

import pandas as pd

from scripts.research_v14_ddog_preipo_q4 import (
    EXPECTED_Q4,
    EXPECTED_Q4_DATES,
    S1_EXPECTED,
    extract_s1_values,
    run,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.io.fundamentals_update import merge_fundamentals


SOURCE = Path("output/data_provenance/ddog_2019_ipo/ddog_2019_s1.htm")
BASE = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_latest_service_revenue_coordinates_zs_"
    "adpt_preipo_bbio_arct_krys_ozk_algm_preipo/quarterly.csv"
)


def test_ddog_extracts_s1_annual_and_h1_values():
    assert extract_s1_values(SOURCE) == S1_EXPECTED


def test_ddog_q4_residuals_preserve_last_input_dates(tmp_path: Path):
    report = run(output_dir=tmp_path)
    assert report["q4_residuals"] == EXPECTED_Q4
    assert report["q4_available_dates"] == EXPECTED_Q4_DATES
    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"


def test_ddog_growth_window_resolves_by_september_signal(tmp_path: Path):
    run(output_dir=tmp_path)
    merged = merge_fundamentals(
        pd.read_csv(BASE), pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    )
    before = quarterly_growth_snapshot(
        merged, pd.Timestamp("2020-07-31"), maximum_age_days=550
    )
    after = quarterly_growth_snapshot(
        merged, pd.Timestamp("2020-09-30"), maximum_age_days=550
    )
    assert "DDOG" not in before.index
    assert after.loc["DDOG", "fiscal_end"] == pd.Timestamp("2020-06-30")
    assert after.loc["DDOG", "growth_available_date"] == pd.Timestamp("2020-08-07")
