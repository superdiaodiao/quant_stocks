import gzip
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_krys_2018q4_profit_residual import (
    EXPECTED_ANNUAL,
    EXPECTED_Q4,
    extract_annual_net_loss,
    run,
)
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot
from src.io.fundamentals_update import merge_fundamentals, parse_companyfacts_quarterly


SOURCE = Path("output/data_provenance/krys_2018_annual/krys_2018_10k.htm")


def test_krys_extracts_first_filed_2018_annual_loss():
    assert extract_annual_net_loss(SOURCE) == EXPECTED_ANNUAL


def test_krys_q4_residual_is_known_for_2019_missing_signals(tmp_path: Path):
    report = run(output_dir=tmp_path)
    assert report["q4_residual"] == EXPECTED_Q4
    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"
    with gzip.open(
        "output/research_only/v14/companyfacts_cache/CIK0001711279.json.gz",
        "rt", encoding="utf-8",
    ) as handle:
        envelope = json.load(handle)
    quarterly = parse_companyfacts_quarterly(
        "KRYS", envelope["payload"], envelope["fetched_at"]
    )
    merged = merge_fundamentals(
        quarterly, pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    )
    for signal_date in ("2019-04-30", "2019-08-30"):
        snapshot = quarterly_profit_ttm_snapshot(
            merged, pd.Timestamp(signal_date), maximum_age_days=150
        )
        assert snapshot.loc["KRYS", "net_income_ttm"] < 0
