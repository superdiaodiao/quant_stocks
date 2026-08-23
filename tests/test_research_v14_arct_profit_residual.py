import gzip
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_arct_profit_residual import EXPECTED_Q4, run
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot
from src.io.fundamentals_update import merge_fundamentals, parse_companyfacts_quarterly


def test_arct_q4_residual_keeps_april_signal_unresolved(tmp_path: Path):
    report = run(output_dir=tmp_path)
    assert report["q4_residual"] == EXPECTED_Q4
    assert report["cutoff"] == "2020-05-08"
    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts.iloc[0]["available_date"] == "2020-05-08"


def test_arct_residual_is_known_only_after_cutoff(tmp_path: Path):
    run(output_dir=tmp_path)
    with gzip.open(
        "output/research_only/v14/companyfacts_cache/CIK0001768224.json.gz",
        "rt", encoding="utf-8",
    ) as handle:
        envelope = json.load(handle)
    quarterly = parse_companyfacts_quarterly(
        "ARCT", envelope["payload"], envelope["fetched_at"]
    )
    merged = merge_fundamentals(
        quarterly, pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    )
    april = quarterly_profit_ttm_snapshot(
        merged, pd.Timestamp("2020-04-30"), maximum_age_days=150
    )
    october = quarterly_profit_ttm_snapshot(
        merged, pd.Timestamp("2020-10-30"), maximum_age_days=150
    )
    assert "ARCT" not in april.index
    assert october.loc["ARCT", "net_income_ttm"] < 0
