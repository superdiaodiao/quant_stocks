import pytest

from scripts.research_v14_okta_legacy_quarterly_reports import (
    _accounting_values,
    validate_statement,
)


def _statement() -> bytes:
    return b"""
      <html><body>Okta, Inc.
      CONSOLIDATED STATEMENTS OF OPERATIONS
      (amounts in thousands)
      <table>
        <tr><td></td><td>Three months ended April 30, 2018</td></tr>
        <tr><td>Total revenue</td><td>83,621</td><td>83,621</td></tr>
        <tr><td>Net loss</td><td>$</td><td>(25,962</td><td>)</td></tr>
        <tr><td>Non-GAAP net loss</td><td>(9,999)</td></tr>
      </table></body></html>
    """


def test_validate_statement_accepts_direct_gaap_quarter() -> None:
    validate_statement(_statement(), 83_621, -25_962)


def test_accounting_values_handles_duplicates_and_parenthesis_cells() -> None:
    assert _accounting_values(["Total revenue", "83621", "83621", None]) == [83_621]
    assert _accounting_values(["Net loss", "$", "(25,962", ")"]) == [-25_962]


def test_validate_statement_rejects_wrong_values_or_non_gaap_substitute() -> None:
    with pytest.raises(ValueError, match="total revenue/net loss"):
        validate_statement(_statement(), 99_999, -25_962)
    broken = _statement().replace(b"<td>Net loss</td>", b"<td>Adjusted net loss</td>")
    with pytest.raises(ValueError, match="total revenue/net loss"):
        validate_statement(broken, 83_621, -25_962)


def test_validate_statement_rejects_identity_and_scale() -> None:
    with pytest.raises(ValueError, match="identity"):
        validate_statement(_statement().replace(b"Okta, Inc.", b"Other Corp."), 83_621, -25_962)
    with pytest.raises(ValueError, match="scale"):
        validate_statement(_statement().replace(b"amounts in thousands", b"per share"), 83_621, -25_962)
