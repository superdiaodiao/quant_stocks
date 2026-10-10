import pandas as pd

from scripts.research_v14_sny_quarterly_reports import (
    _annual_columns,
    _row_value,
    _slot_columns,
)


def _statement() -> pd.DataFrame:
    return pd.DataFrame([
        ["€ million", None, "Q4 2021", "Q4 2021", None, "FY 2021", "FY 2021"],
        ["Net sales", None, None, 9_994, None, None, 37_761],
        ["Net income attributable to equity holders of Sanofi", None, None, 1_131, None, None, 6_223],
    ])


def test_sny_selects_direct_quarter_not_annual_column() -> None:
    table = _statement()
    columns = _slot_columns(table, "Q4 2021")
    assert _row_value(table, "Net sales", columns) == 9_994_000_000.0
    assert _row_value(
        table, "Net income attributable to equity holders of Sanofi", columns
    ) == 1_131_000_000.0


def test_sny_annual_columns_accept_fy_header() -> None:
    table = _statement()
    columns = _annual_columns(table, 2021)
    assert _row_value(table, "Net sales", columns) == 37_761_000_000.0


def test_sny_slot_selection_accepts_ifrs_footnote_suffix() -> None:
    table = pd.DataFrame([
        ["€ million", None, "Q1\u00a02017\u00a0(1)", "Q1\u00a02017\u00a0(1)"],
        ["Net sales", None, None, 8_648],
    ])
    columns = _slot_columns(table, "Q1 2017")
    assert _row_value(table, "Net sales", columns) == 8_648_000_000.0
