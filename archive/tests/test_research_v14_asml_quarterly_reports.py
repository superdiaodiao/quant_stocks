import pandas as pd

from scripts.research_v14_asml_quarterly_reports import (
    _values_agree,
    parse_statement_text,
)


def test_asml_q1_selects_current_quarter_and_eur_units() -> None:
    text = """ASML - Summary US GAAP Consolidated Statements of Operations
Three months ended,
Apr 2, Apr 1,
2017 2018
(in millions EUR, except per share data)
Total net sales 1,943.6 2,285.0
Net income 452.1 539.7
"""
    parsed = parse_statement_text(
        text, fiscal_end=pd.Timestamp("2018-04-01"), fiscal_quarter=1
    )
    assert parsed["current"] == {
        "revenue": 2_285_000_000.0,
        "net_income": 539_700_000.0,
    }
    assert parsed["prior_year_comparison"]["revenue"] == 1_943_600_000.0
    assert parsed["prior_year_fiscal_end"] == pd.Timestamp("2017-04-02")
    assert parsed["annual"] is None


def test_asml_q4_separates_quarter_from_annual_columns() -> None:
    text = """ASML - Summary US GAAP Consolidated Statements of Operations
Three months ended, Twelve months ended,
Dec 31, Dec 31, Dec 31, Dec 31,
(unaudited, in millions €, except per share data) 2020 2021 2020 2021
Total net sales 4,254.1 4,985.6 13,978.5 18,611.0
Net income 1,350.5 1,773.4 3,553.7 5,883.2
"""
    parsed = parse_statement_text(
        text, fiscal_end=pd.Timestamp("2021-12-31"), fiscal_quarter=4
    )
    assert parsed["current"]["revenue"] == 4_985_600_000.0
    assert parsed["annual"]["revenue"] == 18_611_000_000.0
    assert parsed["prior_year_comparison"]["net_income"] == 1_350_500_000.0


def test_asml_parser_rejects_calendar_quarter_end_substitution() -> None:
    text = """ASML - Summary US GAAP Consolidated Statements of Operations
Three months ended,
Mar 29, Apr 4,
2020 2021
(unaudited, in millions €, except per share data)
Total net sales 2,440.6 4,363.9
Net income 390.6 1,331.4
"""
    try:
        parse_statement_text(
            text, fiscal_end=pd.Timestamp("2021-03-31"), fiscal_quarter=1
        )
    except ValueError as error:
        assert "does not contain" in str(error)
    else:
        raise AssertionError("calendar quarter substitution should be rejected")


def test_asml_annual_identity_allows_only_disclosed_rounding_precision() -> None:
    quarter_sum = {"revenue": 11_819_900_000.0, "net_income": 2_592_300_000.0}
    annual = {"revenue": 11_820_000_000.0, "net_income": 2_592_300_000.0}
    assert not _values_agree(quarter_sum, annual)
    assert _values_agree(quarter_sum, annual, absolute_tolerance=100_000.01)
    assert not _values_agree(quarter_sum, annual, absolute_tolerance=99_999.99)
