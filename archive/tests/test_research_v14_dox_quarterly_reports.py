import pandas as pd

from scripts.research_v14_dox_quarterly_reports import _columns, _row_value


def test_dox_selects_quarter_not_six_month_columns() -> None:
    table = pd.DataFrame([
        [None, "Three months ended", "Three months ended", "Six months ended", "Six months ended"],
        [None, 2019, 2019, 2019, 2019],
        ["Revenue", "$", 1019657, "$", 2031712],
        ["Net income", "$", 124279, "$", 225971],
    ])
    columns = _columns(table, year=2019, period_phrase="Three months ended")
    assert _row_value(table, "Revenue", columns) == 1_019_657_000.0


def test_dox_accepts_footnoted_year_header() -> None:
    table = pd.DataFrame([
        [None, "Three months ended", "Three months ended"],
        [None, "2021(a)", "2021(a)"],
        ["Revenue", "$", 1087309],
    ])
    columns = _columns(table, year=2021, period_phrase="Three months ended")
    assert _row_value(table, "Revenue", columns) == 1_087_309_000.0


def test_dox_gaap_row_does_not_match_non_gaap_row() -> None:
    table = pd.DataFrame([
        [None, "Three months ended", "Three months ended"],
        [None, 2021, 2021],
        ["Net income", "$", 123525],
        ["Non-GAAP net income", "$", 147470],
    ])
    columns = _columns(table, year=2021, period_phrase="Three months ended")
    assert _row_value(table, "Net income", columns) == 123_525_000.0
