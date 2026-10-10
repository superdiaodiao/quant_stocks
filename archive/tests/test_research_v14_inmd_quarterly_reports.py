import pytest

from scripts.research_v14_inmd_quarterly_reports import (
    _parse_statement_columns,
    parse_statement,
)


def _statement(
    *,
    period: str = "June 30",
    revenue: str = "87,325",
    income: str = "40,925",
) -> bytes:
    return f"""
      <html><body>
      INMODE LTD.
      CONDENSED CONSOLIDATED STATEMENTS OF INCOME
      (U.S. dollars in thousands, except for per share data)
      (Unaudited)
      Three months ended {period}, Six months ended {period},
      2021 2020 2021 2020
      REVENUES {revenue} 30,765 152,849 71,206
      COST OF REVENUES 12,723 4,695 22,802 10,879
      NET INCOME {income} 8,580 67,671 15,071
      NET INCOME ATTRIBUTABLE TO INMODE LTD. 40,925 8,588 67,568 15,021
      INMODE LTD. RECONCILIATION OF GAAP CONDENSED CONSOLIDATED
      STATEMENTS OF INCOME TO NON-GAAP CONDENSED CONSOLIDATED
      STATEMENTS OF INCOME
      REVENUES 999,999 1 2 3
      NET INCOME 999,999 1 2 3
      </body></html>
    """.encode()


def test_parse_statement_uses_current_gaap_total_net_income() -> None:
    values = parse_statement(_statement(), "2021-06-30")
    assert values == {"revenue": 87_325_000.0, "net_income": 40_925_000.0}


def test_parse_statement_preserves_comparative_and_cumulative_columns() -> None:
    parsed = _parse_statement_columns(_statement(), "2021-06-30")
    assert parsed["comparative"] == {
        "revenue": 30_765_000.0,
        "net_income": 8_580_000.0,
    }
    assert parsed["cumulative"] == {
        "revenue": 152_849_000.0,
        "net_income": 67_671_000.0,
    }


def test_parse_statement_preserves_parenthesized_loss() -> None:
    values = parse_statement(
        _statement(income="(412,601)"), "2021-06-30"
    )
    assert values["net_income"] == -412_601_000.0


def test_parse_statement_rejects_wrong_period_or_identity() -> None:
    with pytest.raises(ValueError, match="requested quarter"):
        parse_statement(_statement(), "2021-09-30")
    with pytest.raises(ValueError, match="identity"):
        parse_statement(b"<html>REVENUES 1 NET INCOME 1</html>", "2021-06-30")


def test_parse_statement_rejects_non_gaap_only_document() -> None:
    raw = b"""
      INMODE LTD. RECONCILIATION OF GAAP CONSOLIDATED STATEMENTS OF INCOME
      TO NON-GAAP CONSOLIDATED STATEMENTS OF INCOME
      (U.S. dollars in thousands) (Unaudited)
      Three months ended June 30
      REVENUES 999 1 2 3 NET INCOME 999 1 2 3
    """
    with pytest.raises(ValueError, match="primary GAAP"):
        parse_statement(raw, "2021-06-30")
