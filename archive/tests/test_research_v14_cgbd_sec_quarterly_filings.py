from pathlib import Path

import pandas as pd

from scripts.research_v14_cgbd_sec_quarterly_filings import (
    FILING_SPECS,
    parse_cgbd_statement,
    validate_filing_specs,
)


def test_cgbd_specs_are_same_cik_continuous_history():
    validate_filing_specs()
    assert len(FILING_SPECS) == 28
    assert FILING_SPECS[0][0] == "2015-03-31"
    assert FILING_SPECS[-1][0] == "2021-12-31"


def test_cgbd_parser_accepts_custom_three_month_period_header(tmp_path: Path):
    filler = "".join(f"<tr><td>Detail {i}</td><td></td></tr>" for i in range(30))
    path = tmp_path / "cgbd.htm"
    path.write_text(
        "<html><body>TCG BDC, Inc. (in thousands)"
        "<table><tr><td></td><td>For the three month periods ended</td>"
        "<td>For the three month periods ended</td></tr>"
        "<tr><td></td><td>March 31, 2019</td><td>March 31, 2019</td></tr>"
        + filler +
        "<tr><td>Total investment income</td><td>$</td><td>51,983</td></tr>"
        "<tr><td>Net increase (decrease) in net assets resulting from operations</td>"
        "<td>$</td><td>22,355</td></tr></table></body></html>"
    )
    assert parse_cgbd_statement(
        path, period_end=pd.Timestamp("2019-03-31"), annual=False
    ) == {"revenue": 51_983_000.0, "net_income": 22_355_000.0}
