from pathlib import Path

import pandas as pd

from scripts.research_v14_azn_quarterly_results import extract_period, run


SOURCE = Path("output/data_provenance/azn_quarterly")


def test_azn_h1_source_uses_direct_q2_not_half_year() -> None:
    assert extract_period(SOURCE / "azn_2018q2.htm", year=2018, phrase="quarter ended") == {
        "revenue": 5_155_000_000.0, "net_income": 319_000_000.0,
    }


def test_azn_2021_h1_source_binds_split_header_to_direct_q2() -> None:
    assert extract_period(SOURCE / "azn_2021q2.htm", year=2021, phrase="quarter ended") == {
        "revenue": 8_220_000_000.0, "net_income": 550_000_000.0,
    }


def test_azn_q3_source_uses_direct_q3_not_nine_months() -> None:
    assert extract_period(SOURCE / "azn_2018q3.htm", year=2018, phrase="quarter ended") == {
        "revenue": 5_340_000_000.0, "net_income": 406_000_000.0,
    }


def test_azn_fy_source_uses_direct_q4_not_full_year() -> None:
    assert extract_period(SOURCE / "azn_2018q4.htm", year=2018, phrase="quarter ended") == {
        "revenue": 6_417_000_000.0, "net_income": 1_009_000_000.0,
    }


def test_azn_run_binds_continuous_pit_chain_and_blocked_gates(tmp_path: Path) -> None:
    report = run(output_dir=tmp_path)
    assert report["accepted_accounting_basis"] == "IFRS_AS_FILED_WITH_SEC"
    assert report["accepted_quarter_count"] == 16
    assert report["fact_count"] == 32
    assert report["point_in_time_proven"] is True
    assert report["parameters_frozen"] is False
    assert report["policy_status"] == "RESEARCH_PRETRAINING_ONLY_UNFROZEN"
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    assert len(report["filing_sources"]) == 16
    assert all(len(row["sha256"]) == 64 for row in report["filing_sources"])
    assert all(
        abs(value) <= 1_000_000.0
        for check in report["annual_identity_checks"]
        for value in check["difference"].values()
    )
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts.groupby("fiscal_end")["metric"].nunique().eq(2).all()
