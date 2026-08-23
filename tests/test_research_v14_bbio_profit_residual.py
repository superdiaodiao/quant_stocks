import gzip
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_bbio_profit_residual import EXPECTED_Q4, run
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot
from src.io.fundamentals_update import merge_fundamentals, parse_companyfacts_quarterly


def test_bbio_q4_residual_is_not_backdated(tmp_path: Path):
    report = run(output_dir=tmp_path)
    assert report["q4_residual"] == EXPECTED_Q4
    assert report["cutoff"] == "2020-08-11"
    assert report["point_in_time_proven"] is True
    assert report["parameters_frozen"] is False
    assert report["release_status"] == "BLOCKED"
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts.iloc[0]["available_date"] == "2020-08-11"
    assert facts.iloc[0]["value"] == EXPECTED_Q4


def test_bbio_residual_makes_negative_ttm_known_by_first_missing_signal(
    tmp_path: Path,
):
    run(output_dir=tmp_path)
    with gzip.open(
        "output/research_only/v14/companyfacts_cache/CIK0001743881.json.gz",
        "rt", encoding="utf-8",
    ) as handle:
        envelope = json.load(handle)
    quarterly = parse_companyfacts_quarterly(
        "BBIO", envelope["payload"], envelope["fetched_at"]
    )
    merged = merge_fundamentals(
        quarterly, pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    )
    snapshot = quarterly_profit_ttm_snapshot(
        merged, pd.Timestamp("2020-11-30"), maximum_age_days=150
    )
    assert snapshot.loc["BBIO", "fiscal_end"] == pd.Timestamp("2020-09-30")
    assert snapshot.loc["BBIO", "net_income_ttm"] < 0
