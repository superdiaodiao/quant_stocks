import pandas as pd

from scripts.research_v14_dsgx_sec_quarterly_filings import (
    _period_columns,
    _row_value,
)


def test_dsgx_period_parser_selects_requested_period_and_year():
    table = pd.DataFrame([
        [None, "Three Months Ended", "Three Months Ended", None, "Nine Months Ended"],
        [None, "October 31,", "October 31,", None, "October 31,"],
        [None, 2019, 2018, None, 2019],
        ["REVENUES", "83,026", "70,008", None, "241,570"],
        ["NET INCOME", "9,666", "7,901", None, "25,559"],
    ])
    columns = _period_columns(
        table,
        year=2019,
        period_phrase="Three Months Ended",
    )
    assert _row_value(table, labels=("REVENUES",), columns=columns) == 83_026
    assert _row_value(table, labels=("NET INCOME",), columns=columns) == 9_666


def test_dsgx_q4_identity_uses_annual_less_nine_months():
    annual = {"revenue": 275_171.0, "net_income": 31_277.0}
    nine_months = {"revenue": 204_141.0, "net_income": 23_385.0}
    derived = {metric: annual[metric] - nine_months[metric] for metric in annual}
    assert derived == {"revenue": 71_030.0, "net_income": 7_892.0}


def test_dsgx_period_parser_handles_split_annual_header_columns():
    table = pd.DataFrame([
        ["Year Ended", "Year Ended", None, None, "January 31,", "January 31,"],
        [None, None, None, None, 2017, 2017],
        ["REVENUES", "REVENUES", None, None, None, "203,779"],
        ["NET INCOME", "NET INCOME", None, None, None, "23,838"],
    ])
    columns = _period_columns(table, year=2017, period_phrase="Year Ended")
    assert _row_value(table, labels=("REVENUES",), columns=columns) == 203_779
