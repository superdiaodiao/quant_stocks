import pytest

from scripts.research_v14_cigi_quarterly_reports import parse_statement


def _statement(revenue="635,123", income="5,463") -> bytes:
    return f"""
      <html><body>
      COLLIERS INTERNATIONAL GROUP INC.
      CONSOLIDATED STATEMENTS OF EARNINGS (LOSS)
      (Unaudited) (in thousands of US dollars)
      Three months ended March 31
      Revenues $ {revenue} $ 1
      Net earnings (loss) {income} 1
      Net earnings attributable to Company 999 999
      </body></html>
    """.encode()


def test_parse_statement_uses_current_total_net_earnings() -> None:
    values = parse_statement(_statement(), "2019-03-31")
    assert values == {"revenue": 635_123_000.0, "net_income": 5_463_000.0}


def test_parse_statement_preserves_parenthesized_loss() -> None:
    values = parse_statement(_statement(income="( 412,601 )"), "2019-03-31")
    assert values["net_income"] == -412_601_000.0


def test_parse_statement_rejects_wrong_period_or_identity() -> None:
    with pytest.raises(ValueError, match="requested quarter"):
        parse_statement(_statement(), "2019-06-30")
    with pytest.raises(ValueError, match="identity"):
        parse_statement(b"<html>Revenues 1 Net earnings 1</html>", "2019-03-31")
