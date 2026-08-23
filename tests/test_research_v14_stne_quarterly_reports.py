import pytest

from scripts.research_v14_stne_quarterly_reports import validate_statement


EVIDENCE = {
    "scale": 1000,
    "revenue_text": "535,773",
    "income_text": "177,036",
}


def _statement() -> bytes:
    return b"""
      <html><body>STONECO LTD.
      Unaudited interim consolidated statement of profit and other
      comprehensive income For the three months ended March 31, 2019 and 2018
      (In thousands of Brazilian Reais, unless otherwise stated)
      Total revenue and income 16 535,773 288,028
      Net income for the period 177,036 24,691
      Adjusted Net Income 999,999 999,999
      </body></html>
    """


def test_validate_statement_accepts_total_ifrs_lines() -> None:
    validate_statement(_statement(), "2019-03-31", EVIDENCE)


def test_validate_statement_rejects_component_or_adjusted_values() -> None:
    bad_revenue = dict(EVIDENCE, revenue_text="168,763")
    with pytest.raises(ValueError, match="total revenue"):
        validate_statement(_statement(), "2019-03-31", bad_revenue)
    bad_income = dict(EVIDENCE, income_text="999,999")
    with pytest.raises(ValueError, match="IFRS net income"):
        validate_statement(_statement(), "2019-03-31", bad_income)


def test_validate_statement_rejects_wrong_issuer_period_or_currency() -> None:
    with pytest.raises(ValueError, match="identity"):
        validate_statement(_statement().replace(b"STONECO LTD.", b"OTHER LTD."),
                           "2019-03-31", EVIDENCE)
    with pytest.raises(ValueError, match="fiscal period"):
        validate_statement(_statement(), "2019-06-30", EVIDENCE)
    with pytest.raises(ValueError, match="currency"):
        validate_statement(_statement().replace(b"Brazilian Reais", b"USD"),
                           "2019-03-31", EVIDENCE)
