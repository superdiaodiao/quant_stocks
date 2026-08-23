from pathlib import Path

import pandas as pd

from scripts.research_v14_slrc_sec_quarterly_filings import (
    FILING_SPECS,
    parse_slrc_statement,
    validate_filing_specs,
)


def _html(*, annual: bool) -> str:
    heading = (
        "<tr><td></td><td>Year ended</td><td>Year ended</td></tr>"
        "<tr><td></td><td>2019</td><td>2019</td></tr>"
        if annual else
        "<tr><td></td><td>Three months ended</td><td>Three months ended</td></tr>"
        "<tr><td></td><td>March 31, 2019</td><td>March 31, 2019</td></tr>"
    )
    return (
        "<html><body>Solar Capital Ltd. (in thousands)"
        f"<table>{heading}"
        "<tr><td>Total investment income</td><td>$</td><td>39,259</td></tr>"
        "<tr><td>Net increase in net assets resulting from operations</td>"
        "<td>$</td><td>24,832</td></tr></table></body></html>"
    )


def test_filing_specs_are_complete_continuous_and_timely():
    validate_filing_specs()
    assert len(FILING_SPECS) == 20
    assert sum(form == "10-K" for _, _, form, _, _ in FILING_SPECS) == 5


def test_parse_slrc_statement_handles_direct_and_annual_headers(tmp_path: Path):
    direct = tmp_path / "direct.htm"
    direct.write_text(_html(annual=False))
    annual = tmp_path / "annual.htm"
    annual.write_text(_html(annual=True))
    expected = {"revenue": 39_259_000.0, "net_income": 24_832_000.0}
    assert parse_slrc_statement(
        direct, period_end=pd.Timestamp("2019-03-31"), annual=False
    ) == expected
    assert parse_slrc_statement(
        annual, period_end=pd.Timestamp("2019-12-31"), annual=True
    ) == expected
