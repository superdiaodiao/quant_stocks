from pathlib import Path

import pandas as pd

from scripts.research_v14_nmfc_sec_quarterly_filings import (
    FILING_SPECS,
    parse_nmfc_statement,
    validate_filing_specs,
)


def test_nmfc_filing_specs_are_complete_continuous_and_timely():
    validate_filing_specs()
    assert len(FILING_SPECS) == 20
    assert sum(form == "10-K" for _, _, form, _, _ in FILING_SPECS) == 5


def test_nmfc_parser_prefers_parent_result_when_noncontrolling_row_exists(
    tmp_path: Path,
):
    path = tmp_path / "nmfc.htm"
    path.write_text(
        "<html><body>New Mountain Finance Corporation (in thousands)"
        "<table>"
        "<tr><td></td><td>Three Months Ended</td><td>Three Months Ended</td></tr>"
        "<tr><td></td><td>March 31, 2020</td><td>March 31, 2020</td></tr>"
        "<tr><td>Total investment income</td><td>$</td><td>74,084</td></tr>"
        "<tr><td>Net (decrease) increase in net assets resulting from operations</td>"
        "<td>$</td><td>(172,422)</td></tr>"
        "<tr><td>Net (decrease) increase in net assets resulting from operations "
        "related to New Mountain Finance Corporation</td>"
        "<td>$</td><td>(172,357)</td></tr>"
        "</table></body></html>"
    )
    assert parse_nmfc_statement(
        path, period_end=pd.Timestamp("2020-03-31"), annual=False
    ) == {"revenue": 74_084_000.0, "net_income": -172_357_000.0}
