from pathlib import Path

from scripts.research_v14_camt_quarterly_reports import (
    extract_2019q2_discontinued_event,
    extract_statement,
    run,
)


SOURCE_DIR = Path("output/data_provenance/camt_quarterly")


def test_camt_q2_uses_current_quarter_not_six_months() -> None:
    values = extract_statement(SOURCE_DIR / "camt_2021q2.htm", year=2021)
    assert values == {"revenue": 67_450_000.0, "net_income": 15_652_000.0}


def test_camt_q3_uses_current_quarter_not_nine_months() -> None:
    values = extract_statement(SOURCE_DIR / "camt_2021q3.htm", year=2021)
    assert values == {"revenue": 70_686_000.0, "net_income": 18_505_000.0}


def test_camt_q4_uses_current_quarter_not_full_year() -> None:
    values = extract_statement(SOURCE_DIR / "camt_2021q4.htm", year=2021)
    assert values == {"revenue": 74_171_000.0, "net_income": 12_760_000.0}


def test_camt_2019q2_preserves_gaap_discontinued_operation() -> None:
    event = extract_2019q2_discontinued_event(SOURCE_DIR / "camt_2019q2.htm")
    assert event == {
        "continuing_operations_net_income": 6_026_000.0,
        "discontinued_operations_net_income": 1_163_000.0,
        "gaap_total_net_income": 7_189_000.0,
    }
    assert extract_statement(SOURCE_DIR / "camt_2019q2.htm", year=2019)[
        "net_income"
    ] == event["gaap_total_net_income"]


def test_camt_run_binds_continuous_pit_chain_and_blocked_gates(tmp_path: Path) -> None:
    report = run(output_dir=tmp_path)
    assert report["accepted_accounting_basis"] == "US_GAAP"
    assert report["accepted_quarter_count"] == 16
    assert report["fact_count"] == 32
    assert report["point_in_time_proven"] is True
    assert report["parameters_frozen"] is False
    assert report["policy_status"] == "RESEARCH_PRETRAINING_ONLY_UNFROZEN"
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    assert len(report["filing_sources"]) == 16
    assert all(len(source["sha256"]) == 64 for source in report["filing_sources"])
    assert all(
        source["accession"].replace("-", "") in source["source_url"]
        for source in report["filing_sources"]
    )
    assert len(report["recovered_quarters"]) == 16
    assert (tmp_path / "strict_quarterly_facts.csv").exists()
