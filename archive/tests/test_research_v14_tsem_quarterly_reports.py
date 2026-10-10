import pytest

from scripts.research_v14_tsem_quarterly_reports import (
    _accounting_values,
    validate_statement,
)


def _statement() -> bytes:
    return b"""
      <html><body>Tower Semiconductor Ltd.
      CONSOLIDATED STATEMENTS OF OPERATIONS
      (U.S. dollars in thousands)
      <table>
        <tr><td></td><td>Three months ended June 30, 2020</td></tr>
        <tr><td>REVENUES</td><td>$</td><td>310090</td></tr>
        <tr><td>NET PROFIT</td><td>21474</td></tr>
        <tr><td>NET PROFIT ATTRIBUTABLE TO THE COMPANY</td><td>19052</td></tr>
        <tr><td>ADJUSTED NET PROFIT</td><td>99999</td></tr>
      </table></body></html>
    """


def test_validate_statement_accepts_consolidated_gaap_quarter() -> None:
    validate_statement(_statement(), "2020-06-30", 310_090, 21_474)


def test_accounting_values_deduplicates_sec_cells() -> None:
    assert _accounting_values(["REVENUES", "$", "310090", "310090", None]) == [310_090]
    assert _accounting_values(["NET PROFIT", "(1,234", ")"]) == [-1_234]


def test_validate_statement_rejects_attributable_or_adjusted_profit() -> None:
    with pytest.raises(ValueError, match="revenues/net profit"):
        validate_statement(_statement(), "2020-06-30", 310_090, 19_052)
    broken = _statement().replace(b"<td>NET PROFIT</td>", b"<td>ADJUSTED PROFIT</td>")
    with pytest.raises(ValueError, match="revenues/net profit"):
        validate_statement(broken, "2020-06-30", 310_090, 21_474)


def test_validate_statement_rejects_identity_period_and_scale() -> None:
    with pytest.raises(ValueError, match="identity"):
        validate_statement(_statement().replace(b"Tower Semiconductor", b"Other"), "2020-06-30", 310_090, 21_474)
    with pytest.raises(ValueError, match="quarter"):
        validate_statement(_statement(), "2020-09-30", 310_090, 21_474)
    with pytest.raises(ValueError, match="scale"):
        validate_statement(_statement().replace(b"U.S. dollars in thousands", b"shares"), "2020-06-30", 310_090, 21_474)
