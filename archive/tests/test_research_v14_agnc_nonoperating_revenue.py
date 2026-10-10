from pathlib import Path

import gzip
import json
import pandas as pd

from scripts.research_v14_agnc_nonoperating_revenue import (
    CACHE,
    EXPECTED_Q4_2020,
    SPECS,
    extract_quarters,
    run,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.io.fundamentals_update import merge_fundamentals


BASE = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_latest_service_revenue_coordinates_zs_"
    "adpt_preipo_bbio_arct_krys_ozk_algm_ddog_tw_mdb_q4/quarterly.csv"
)


def test_agnc_proves_concept_cutover_and_reconciles_quarters():
    with gzip.open(CACHE, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)["payload"]
    assert extract_quarters(payload) == {"2020-12-31": EXPECTED_Q4_2020, **SPECS}


def test_agnc_manifest_binds_research_only_source(tmp_path: Path):
    report = run(output_dir=tmp_path)
    assert report["accepted_quarter_count"] == 4
    assert report["fact_count"] == 5
    assert report["recovered_quarters"]["2020-12-31"]["net_income"] == 775_000_000.0
    assert report["comparative_cutover"]["values_identical"] is True
    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"


def test_agnc_growth_snapshot_is_no_longer_stale(tmp_path: Path):
    run(output_dir=tmp_path)
    base = pd.read_csv(BASE, parse_dates=["fiscal_end", "available_date"])
    before = quarterly_growth_snapshot(
        base, pd.Timestamp("2021-05-28"), maximum_age_days=150
    )
    merged = merge_fundamentals(
        base, pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    )
    after = quarterly_growth_snapshot(
        merged, pd.Timestamp("2021-05-28"), maximum_age_days=150
    )
    assert "AGNC" not in before.index
    assert after.loc["AGNC", "fiscal_end"] == pd.Timestamp("2021-03-31")
    assert after.loc["AGNC", "growth_available_date"] == pd.Timestamp("2021-05-07")
