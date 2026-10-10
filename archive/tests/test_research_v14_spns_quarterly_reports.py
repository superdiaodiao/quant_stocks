import pytest

from scripts.research_v14_spns_quarterly_reports import (
    _accounting_values,
    validate_statement,
)


def _statement(*, non_gaap: bool = False) -> bytes:
    heading = (
        "CONDENSED CONSOLIDATED NON-GAAP STATEMENTS OF INCOME"
        if non_gaap
        else "CONDENSED CONSOLIDATED STATEMENTS OF INCOME"
    )
    return f"""
      <html><body>SAPIENS INTERNATIONAL CORPORATION N.V. AND ITS SUBSIDIARIES
      <p>{heading}</p><p>U.S. dollars in thousands (except per share amounts)</p>
      <table>
        <tr><td></td><td>Three months ended June 30, 2020</td><td>2019</td></tr>
        <tr><td>Revenue</td><td>$</td><td>93063</td><td>79529</td></tr>
        <tr><td>Net income</td><td>9330</td><td>6866</td></tr>
        <tr><td>Net income attributable to Sapiens' shareholders</td><td>9100</td></tr>
      </table></body></html>
    """.encode()


def test_validate_statement_accepts_current_quarter_consolidated_gaap() -> None:
    validate_statement(_statement(), "2020-06-30", 93_063, 9_330)


def test_validate_statement_accepts_gaap_net_income_loss_label() -> None:
    validate_statement(
        _statement().replace(b"Net income</td>", b"Net income (loss)</td>"),
        "2020-06-30", 93_063, 9_330,
    )


def test_accounting_values_deduplicates_sec_cells() -> None:
    assert _accounting_values(["Revenue", "$", "93063", "93063", None]) == [93_063]
    assert _accounting_values(["Net income", "(1,234", ")"]) == [-1_234]


def test_validate_statement_rejects_non_gaap_and_attributable_values() -> None:
    with pytest.raises(ValueError, match="consolidated GAAP"):
        validate_statement(_statement(non_gaap=True), "2020-06-30", 93_063, 9_330)
    with pytest.raises(ValueError, match="consolidated GAAP"):
        validate_statement(_statement(), "2020-06-30", 93_063, 9_100)


def test_validate_statement_rejects_comparative_period_values() -> None:
    with pytest.raises(ValueError, match="consolidated GAAP"):
        validate_statement(_statement(), "2020-06-30", 79_529, 6_866)


def test_validate_statement_rejects_identity_period_and_scale() -> None:
    with pytest.raises(ValueError, match="identity"):
        validate_statement(
            _statement().replace(b"SAPIENS INTERNATIONAL", b"OTHER ISSUER"),
            "2020-06-30", 93_063, 9_330,
        )
    with pytest.raises(ValueError, match="quarter"):
        validate_statement(_statement(), "2020-09-30", 93_063, 9_330)
    with pytest.raises(ValueError, match="scale"):
        validate_statement(
            _statement().replace(b"U.S. dollars in thousands", b"shares"),
            "2020-06-30", 93_063, 9_330,
        )
