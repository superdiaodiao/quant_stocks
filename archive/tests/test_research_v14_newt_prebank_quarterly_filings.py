from pathlib import Path

import pandas as pd
import pytest

from scripts.research_v14_newt_prebank_quarterly_filings import (
    FILING_SPECS,
    parse_newt_bdc_statement,
    validate_filing_specs,
)


def _html() -> str:
    return (
        "<html><body>NEWTEK BUSINESS SERVICES CORP. AND SUBSIDIARIES "
        "(In Thousands, except for Per Share Data)"
        "<table>"
        "<tr><td></td><td>Three Months Ended March 31, 2019</td>"
        "<td>Three Months Ended March 31, 2019</td></tr>"
        "<tr><td>Total investment income</td><td>$</td><td>13,764</td></tr>"
        "<tr><td>Net increase in net assets resulting from operations</td>"
        "<td>$</td><td>9,083</td></tr>"
        "</table></body></html>"
    )


def test_newt_specs_stop_before_bank_conversion():
    validate_filing_specs()
    assert len(FILING_SPECS) == 20
    assert max(pd.Timestamp(row[0]) for row in FILING_SPECS) == pd.Timestamp(
        "2021-12-31"
    )


def test_newt_parser_accepts_prebank_statement_and_rejects_bank_era(
    tmp_path: Path,
):
    path = tmp_path / "newt.htm"
    path.write_text(_html())
    assert parse_newt_bdc_statement(
        path, period_end=pd.Timestamp("2019-03-31"), annual=False
    ) == {"revenue": 13_764_000.0, "net_income": 9_083_000.0}
    with pytest.raises(ValueError, match="bank-era"):
        parse_newt_bdc_statement(
            path, period_end=pd.Timestamp("2023-03-31"), annual=False
        )
