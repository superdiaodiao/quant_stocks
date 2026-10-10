from pathlib import Path

import pandas as pd

from scripts.research_v14_iclr_2017_quarterly_reports import (
    extract_annual,
    extract_direct_quarter,
    run,
)


SOURCE = Path("output/data_provenance/iclr_2017_quarterly")


def test_iclr_q2_uses_three_month_not_six_month_column() -> None:
    assert extract_direct_quarter(SOURCE / "iclr_2017q2.htm") == {
        "revenue": 431_023_000.0,
        "net_income": 64_817_000.0,
    }


def test_iclr_q3_uses_three_month_not_nine_month_column() -> None:
    assert extract_direct_quarter(SOURCE / "iclr_2017q3.htm") == {
        "revenue": 440_323_000.0,
        "net_income": 74_154_000.0,
    }


def test_iclr_annual_is_original_audited_pair() -> None:
    assert extract_annual(SOURCE / "iclr_2017_20f.htm") == {
        "revenue": 1_758_439_000.0,
        "net_income": 281_488_000.0,
    }


def test_iclr_2018_q1_is_not_delayed_to_2019_comparative() -> None:
    assert extract_direct_quarter(SOURCE / "iclr_2018q1.htm", year=2018) == {
        "revenue": 620_125_000.0,
        "net_income": 78_098_000.0,
    }


def test_iclr_2018_annual_closes_q4_at_original_filing_date() -> None:
    assert extract_annual(SOURCE / "iclr_2018_20f.htm", year=2018) == {
        "revenue": 2_595_777_000.0,
        "net_income": 322_656_000.0,
    }


def test_iclr_run_binds_pit_chain_and_blocked_gates(tmp_path: Path) -> None:
    report = run(output_dir=tmp_path)
    assert report["accepted_quarter_count"] == 8
    assert report["direct_quarter_count"] == 6
    assert report["derived_q4_count"] == 2
    assert report["fact_count"] == 16
    assert all(check["exact_arithmetic_identity"] for check in report["annual_identity_checks"])
    assert report["point_in_time_proven"] is True
    assert report["parameters_frozen"] is False
    assert report["policy_status"] == "RESEARCH_PRETRAINING_ONLY_UNFROZEN"
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    assert len(report["filing_sources"]) == 8
    assert all(len(row["sha256"]) == 64 for row in report["filing_sources"])
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    assert facts.loc[facts["fiscal_end"].eq("2017-03-31"), "available_date"].eq(
        "2017-04-27"
    ).all()
