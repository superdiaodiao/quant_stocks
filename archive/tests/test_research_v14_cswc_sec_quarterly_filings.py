from pathlib import Path

import pandas as pd

from scripts.research_v14_cswc_sec_quarterly_filings import (
    FILING_SPECS,
    parse_cswc_statement,
    validate_filing_specs,
)


def test_cswc_specs_use_march_fiscal_year_ends():
    validate_filing_specs()
    assert len(FILING_SPECS) == 23
    annual_ends = [end for end, _, form, _, _ in FILING_SPECS if form == "10-K"]
    assert annual_ends == [
        "2017-03-31", "2018-03-31", "2019-03-31",
        "2020-03-31", "2021-03-31",
    ]


def test_cswc_parser_uses_full_statement_not_conflicting_mda_summary(
    tmp_path: Path,
):
    filler = "".join(f"<tr><td>Detail {i}</td><td></td></tr>" for i in range(30))
    full = (
        "<table><tr><td></td><td>Three Months Ended</td><td>Three Months Ended</td></tr>"
        "<tr><td></td><td>September 30,</td><td>September 30,</td></tr>"
        "<tr><td></td><td>2017</td><td>2017</td></tr>"
        f"{filler}"
        "<tr><td>Total investment income</td><td>$</td><td>8,509</td></tr>"
        "<tr><td>Net increase in net assets from operations</td>"
        "<td>$</td><td>8,643</td></tr></table>"
    )
    compact = (
        "<table><tr><td></td><td>Three Months Ended</td><td>Three Months Ended</td></tr>"
        "<tr><td></td><td>September 30,</td><td>September 30,</td></tr>"
        "<tr><td></td><td>2017</td><td>2017</td></tr>"
        "<tr><td>Total investment income</td><td>$</td><td>8,509</td></tr>"
        "<tr><td>Net increase in net assets from operations</td>"
        "<td>$</td><td>8,642</td></tr></table>"
    )
    path = tmp_path / "cswc.htm"
    path.write_text(
        "<html><body>Capital Southwest Corporation (in thousands)"
        + full + compact + "</body></html>"
    )
    assert parse_cswc_statement(
        path, period_end=pd.Timestamp("2017-09-30"), annual=False
    ) == {"revenue": 8_509_000.0, "net_income": 8_643_000.0}
