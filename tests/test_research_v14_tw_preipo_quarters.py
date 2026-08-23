from pathlib import Path

import pandas as pd

from scripts.research_v14_tw_preipo_quarters import EXPECTED, extract_quarters, run
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.io.fundamentals_update import merge_fundamentals


SOURCE = Path("output/data_provenance/tw_2019_ipo/tw_2019_s1.htm")
BASE = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_latest_service_revenue_coordinates_zs_"
    "adpt_preipo_bbio_arct_krys_ozk_algm_ddog_preipo/quarterly.csv"
)


def test_tw_extracts_explicit_predecessor_successor_quarters():
    assert extract_quarters(SOURCE) == EXPECTED


def test_tw_manifest_preserves_issuer_boundary(tmp_path: Path):
    report = run(output_dir=tmp_path)
    assert report["accepted_quarter_count"] == 8
    assert report["predecessor_quarter_count"] == 7
    assert report["successor_quarter_count"] == 1
    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert len(facts) == 16
    assert facts[facts["fiscal_end"].eq("2018-12-31")]["taxonomy"].str.endswith(
        "SUCCESSOR"
    ).all()


def test_tw_growth_window_is_available_only_after_s1(tmp_path: Path):
    run(output_dir=tmp_path)
    merged = merge_fundamentals(
        pd.read_csv(BASE), pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    )
    before = quarterly_growth_snapshot(
        merged, pd.Timestamp("2019-02-28"), maximum_age_days=550
    )
    after = quarterly_growth_snapshot(
        merged, pd.Timestamp("2020-05-29"), maximum_age_days=550
    )
    assert "TW" not in before.index
    assert after.loc["TW", "fiscal_end"] == pd.Timestamp("2019-09-30")
    assert after.loc["TW", "growth_available_date"] == pd.Timestamp("2019-11-08")
