from __future__ import annotations

import pandas as pd
import pytest

from scripts.research_v14_meoh_quarterly_reports import parse_quarter


def _statement(
    *, revenue_label: str = "Revenue 4",
    income_label: str = "Net income (loss) (attributable to Methanex shareholders)",
) -> bytes:
    table = pd.DataFrame([
        ["($ millions except per share amounts)", "Sep 30 2020", "Jun 30 2020", "Sep 30 2019", "Nine Months Ended"],
        [revenue_label, "581", "512", "765", "1,839"],
        ["Adjusted revenue", "515", "453", "723", "1,644"],
        [income_label, "(88)", "(65)", "(10)", "(130)"],
        ["Adjusted net income (loss)", "(79)", "(64)", "(21)", "(135)"],
    ])
    return table.to_html(index=False, header=False).encode()


def test_parse_quarter_accepts_current_unadjusted_ifrs_rows() -> None:
    assert parse_quarter(_statement(), "2020-09-30") == {
        "revenue": 581_000_000.0,
        "net_income": -88_000_000.0,
    }


def test_parse_quarter_rejects_adjusted_or_component_substitutes() -> None:
    with pytest.raises(RuntimeError, match="not unique"):
        parse_quarter(_statement(revenue_label="Adjusted revenue"), "2020-09-30")
    with pytest.raises(RuntimeError, match="not unique"):
        parse_quarter(
            _statement(income_label="Adjusted net income (loss)"), "2020-09-30"
        )


def test_parse_quarter_rejects_wrong_period_or_currency_scale() -> None:
    with pytest.raises(RuntimeError, match="quarter end"):
        parse_quarter(_statement(), "2020-06-30")
    with pytest.raises(RuntimeError, match="USD millions"):
        parse_quarter(_statement().replace(b"$ millions", b"CAD thousands"),
                      "2020-09-30")
