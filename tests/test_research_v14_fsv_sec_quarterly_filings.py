import pandas as pd

from scripts.research_v14_dsgx_sec_quarterly_filings import (
    _period_columns,
    _row_value,
)


def test_fsv_parser_selects_three_month_column_not_nine_month_column():
    table = pd.DataFrame([
        [None, "Three months ended", "Three months ended", None, "Nine months ended"],
        [None, "September 30", "September 30", None, "September 30"],
        [None, 2019, 2018, None, 2019],
        ["Revenues", "672,253", "506,400", None, "1,700,000"],
        ["Net earnings (loss)", "26,336", "31,664", None, "(241,199)"],
    ])
    columns = _period_columns(table, year=2019, period_phrase="Three months")
    assert _row_value(table, labels=("Revenues",), columns=columns) == 672_253
    assert _row_value(
        table,
        labels=("Net earnings (loss)", "Net earnings"),
        columns=columns,
    ) == 26_336


def test_fsv_parser_selects_twelve_month_column_not_three_month_column():
    table = pd.DataFrame([
        [None, "Three months", "Three months", None, "Twelve months", "Twelve months"],
        [None, "ended December 31", "ended December 31", None, "ended December 31", "ended December 31"],
        [None, 2021, 2020, None, 2021, 2020],
        ["Revenues", "856,945", "750,000", None, "3,249,072", "2,772,415"],
        ["Net earnings", "35,395", "30,000", None, "156,130", "109,590"],
    ])
    columns = _period_columns(table, year=2021, period_phrase="Twelve months")
    assert _row_value(table, labels=("Revenues",), columns=columns) == 3_249_072
