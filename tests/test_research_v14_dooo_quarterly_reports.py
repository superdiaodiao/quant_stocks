import pytest

from scripts.research_v14_dooo_quarterly_reports import validate_statement


def _statement() -> bytes:
    return b"""
    <html><body>BRP Inc. NASDAQ: DOOO
    Unaudited Condensed Consolidated Interim Financial Statements
    For the three-month periods ended April 30, 2019 and 2018
    [in millions of Canadian dollars]
    Revenues $1,333.7 $1,136.7
    Net income $23.8 $13.4
    Attributable to shareholders $24.0
    Normalized net income $52.7
    </body></html>
    """


def test_validate_statement_accepts_direct_brp_fiscal_quarter() -> None:
    validate_statement(_statement(), "2019-04-30", 1_333.7, 23.8)


def test_validate_statement_accepts_predeclared_restated_direct_quarter() -> None:
    statement = b"""
    <html><body>BRP Inc.
    Management's Discussion and Analysis
    [in millions of Canadian dollars]
    Summary of Consolidated Quarterly Results
    Three-month periods ended January 31, 2018 April 30, 2018 July 31, 2018
    Total Revenues 1,226.0 1,136.7 1,207.0
    Net income (loss) 70.0 13.4 41.0
    </body></html>
    """
    validate_statement(statement, "2018-04-30", 1_136.7, 13.4)


def test_validate_statement_rejects_comparative_or_normalized_values() -> None:
    with pytest.raises(ValueError, match="predeclared"):
        validate_statement(_statement(), "2019-04-30", 1_136.7, 13.4)
    with pytest.raises(ValueError, match="predeclared"):
        validate_statement(_statement(), "2019-04-30", 1_333.7, 52.7)


def test_validate_statement_rejects_calendar_quarter_and_bad_identity() -> None:
    with pytest.raises(ValueError, match="not predeclared"):
        validate_statement(_statement(), "2019-03-31", 1_333.7, 23.8)
    with pytest.raises(ValueError, match="identity"):
        validate_statement(_statement().replace(b"BRP Inc.", b"OTHER"),
                           "2019-04-30", 1_333.7, 23.8)
