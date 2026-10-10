from pathlib import Path

from scripts.research_v14_eslt_quarterly_reports import extract_quarter


def test_eslt_extracts_q1_current_quarter_gaap_values() -> None:
    values = extract_quarter(Path("output/data_provenance/eslt_quarterly/eslt_2019q1.htm"), 1)
    assert values == {"revenue": 1_021_723_000.0, "net_income": 50_457_000.0}


def test_eslt_extracts_q3_current_quarter_not_ytd_values() -> None:
    values = extract_quarter(Path("output/data_provenance/eslt_quarterly/eslt_2021q3.htm"), 3)
    assert values == {"revenue": 1_363_596_000.0, "net_income": 91_906_000.0}


def test_eslt_q4_ignores_acquisition_pro_forma_table_in_millions() -> None:
    values = extract_quarter(Path("output/data_provenance/eslt_quarterly/eslt_2018q4.htm"), 4)
    assert values == {"revenue": 1_077_840_000.0, "net_income": 1_127_000.0}
