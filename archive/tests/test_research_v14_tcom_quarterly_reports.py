from pathlib import Path

from scripts.research_v14_tcom_quarterly_reports import extract_quarter, run


def test_tcom_extracts_2018q3_gaap_loss_with_parentheses() -> None:
    values = extract_quarter(
        Path("output/data_provenance/tcom_quarterly/tcom_2018q3.htm"), 2018, 3
    )
    assert values == {"revenue": 9_355_000_000.0, "net_income": -1_139_000_000.0}


def test_tcom_q4_uses_current_quarter_not_annual_columns() -> None:
    values = extract_quarter(
        Path("output/data_provenance/tcom_quarterly/tcom_2019q4.htm"), 2019, 4
    )
    assert values == {"revenue": 8_335_000_000.0, "net_income": 2_008_000_000.0}


def test_tcom_q2_uses_three_month_not_six_month_columns() -> None:
    values = extract_quarter(
        Path("output/data_provenance/tcom_quarterly/tcom_2021q2.htm"), 2021, 2
    )
    assert values == {"revenue": 5_890_000_000.0, "net_income": -647_000_000.0}


def test_tcom_run_produces_continuous_paired_chain(tmp_path: Path) -> None:
    report = run(output_dir=tmp_path)
    assert report["point_in_time_proven"] is True
    assert report["accepted_quarter_count"] == 16
    assert report["fact_count"] == 32
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
