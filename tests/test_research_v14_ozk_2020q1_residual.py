from pathlib import Path

import pandas as pd

from scripts.research_v14_ozk_2020q1_residual import EXPECTED_Q1, run
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.io.fundamentals_update import merge_fundamentals


BASE = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_latest_service_revenue_coordinates_zs_"
    "adpt_preipo_bbio_arct_krys_profit/quarterly.csv"
)


def test_ozk_q1_residual_uses_contemporaneous_q2_release(tmp_path: Path):
    report = run(output_dir=tmp_path)
    assert report["q1_residual"] == EXPECTED_Q1
    assert report["available_date"] == "2020-07-23"
    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts["available_date"].eq("2020-07-23").all()
    assert set(facts["value"]) == set(EXPECTED_Q1.values())


def test_ozk_growth_is_unavailable_before_q2_release(tmp_path: Path):
    run(output_dir=tmp_path)
    merged = merge_fundamentals(
        pd.read_csv(BASE), pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    )
    before = quarterly_growth_snapshot(
        merged, pd.Timestamp("2020-06-30"), maximum_age_days=550
    )
    after = quarterly_growth_snapshot(
        merged, pd.Timestamp("2020-08-31"), maximum_age_days=550
    )
    assert before.loc["OZK", "fiscal_end"] == pd.Timestamp("2019-12-31")
    assert after.loc["OZK", "fiscal_end"] == pd.Timestamp("2020-06-30")
    assert after.loc["OZK", "growth_available_date"] == pd.Timestamp("2020-07-23")
