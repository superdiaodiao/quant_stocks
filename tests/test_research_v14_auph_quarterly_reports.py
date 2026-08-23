from pathlib import Path

from scripts.research_v14_auph_quarterly_reports import extract_statement, run


def test_auph_2017q1_preserves_reported_derivative_driven_loss() -> None:
    values = extract_statement(
        Path("output/data_provenance/auph_quarterly/auph_2017q1_financial.htm")
    )
    assert values == {"revenue": 30_000.0, "net_income": -51_941_000.0}


def test_auph_2018q3_sums_licensing_and_contract_revenue() -> None:
    values = extract_statement(
        Path("output/data_provenance/auph_quarterly/auph_2018q3_financial.htm")
    )
    assert values == {"revenue": 375_000.0, "net_income": -18_342_000.0}


def test_auph_2019_annual_ifrs_values_are_direct() -> None:
    values = extract_statement(
        Path("output/data_provenance/auph_quarterly/auph_2019fy_financial.htm"),
        annual=True,
    )
    assert values == {"revenue": 318_000.0, "net_income": -123_846_000.0}


def test_auph_run_closes_each_year_and_excludes_basis_boundary(tmp_path: Path) -> None:
    report = run(output_dir=tmp_path)
    assert report["accepted_accounting_basis"] == "IFRS"
    assert report["accepted_quarter_count"] == 12
    assert report["fact_count"] == 24
    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    assert set(report["annual_closure_values"]) == {2017, 2018, 2019}
