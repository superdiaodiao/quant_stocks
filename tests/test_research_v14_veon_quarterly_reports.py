import pytest

from scripts.research_v14_veon_quarterly_reports import (
    _accounting_values,
    validate_statement,
)


def _statement() -> bytes:
    return b"""
    <html><body>VEON Ltd. Unaudited interim condensed consolidated financial statements
    <p>In millions of U.S. dollars</p>
    <table>
      <tr><td></td><td>Six-month period 2020</td><td>2019</td>
          <td>Three-month period ended June 30, 2020</td><td>2019</td></tr>
      <tr><td>Total operating revenues</td><td>3988</td><td>4385</td><td>1892</td><td>2261</td></tr>
      <tr><td>Profit / (loss) for the period</td><td>294</td><td>605</td><td>175</td><td>75</td></tr>
      <tr><td>Attributable to owners</td><td>160</td></tr>
    </table></body></html>
    """


def test_validate_statement_accepts_direct_three_month_ifrs_values() -> None:
    validate_statement(_statement(), "2020-06-30", 1_892, 175)


def test_accounting_values_handles_parenthesized_losses() -> None:
    assert _accounting_values(["Profit / (loss)", "(644", ")", None]) == [-644]


def test_validate_statement_rejects_ytd_or_attributable_values() -> None:
    with pytest.raises(ValueError, match="three-month consolidated"):
        validate_statement(_statement(), "2020-06-30", 3_988, 294)
    with pytest.raises(ValueError, match="three-month consolidated"):
        validate_statement(_statement(), "2020-06-30", 1_892, 160)


def test_validate_statement_rejects_identity_period_and_scale() -> None:
    with pytest.raises(ValueError, match="identity"):
        validate_statement(_statement().replace(b"VEON", b"OTHER"),
                           "2020-06-30", 1_892, 175)
    with pytest.raises(ValueError, match="quarter"):
        validate_statement(_statement(), "2020-09-30", 1_892, 175)
    with pytest.raises(ValueError, match="scale"):
        validate_statement(_statement().replace(b"millions of U.S. dollars", b"units"),
                           "2020-06-30", 1_892, 175)
