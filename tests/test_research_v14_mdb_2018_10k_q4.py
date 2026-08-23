from pathlib import Path

import gzip
import json
import pandas as pd

from scripts.research_v14_mdb_2018_10k_q4 import (
    CACHE,
    EXPECTED_INPUTS,
    EXPECTED_Q4,
    extract_inputs,
    run,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.io.fundamentals_update import merge_fundamentals


BASE = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_latest_service_revenue_coordinates_zs_"
    "adpt_preipo_bbio_arct_krys_ozk_algm_ddog_tw_preipo/quarterly.csv"
)


def test_mdb_extracts_exact_annual_and_ytd_sec_inputs():
    with gzip.open(CACHE, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)["payload"]
    assert extract_inputs(payload) == EXPECTED_INPUTS


def test_mdb_q4_residuals_preserve_last_input_date(tmp_path: Path):
    report = run(output_dir=tmp_path)
    assert report["q4_residuals"] == EXPECTED_Q4
    assert report["available_date"] == "2018-03-30"
    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"


def test_mdb_growth_window_is_repaired_before_2019_signals(tmp_path: Path):
    run(output_dir=tmp_path)
    base = pd.read_csv(BASE, parse_dates=["fiscal_end", "available_date"])
    before = quarterly_growth_snapshot(
        base, pd.Timestamp("2019-02-28"), maximum_age_days=550
    )
    merged = merge_fundamentals(
        base, pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    )
    after = quarterly_growth_snapshot(
        merged, pd.Timestamp("2019-02-28"), maximum_age_days=550
    )
    assert "MDB" not in before.index
    assert after.loc["MDB", "fiscal_end"] == pd.Timestamp("2018-10-31")
    assert after.loc["MDB", "growth_available_date"] == pd.Timestamp("2018-12-06")
