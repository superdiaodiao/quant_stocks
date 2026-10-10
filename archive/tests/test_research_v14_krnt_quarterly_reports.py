import pandas as pd

from scripts.research_v14_krnt_quarterly_reports import (
    NET_LABELS,
    REVENUE_LABELS,
    _longest_chain,
    _period_columns,
    _row_value,
)


def test_krnt_parser_selects_three_month_columns_not_ytd_columns():
    table = pd.DataFrame(
        [
            [None, "Six Months Ended", "Six Months Ended", None,
             "Three Months Ended", "Three Months Ended"],
            [None, "June 30,", "June 30,", None, "June 30,", "June 30,"],
            [None, 2020, 2020, None, 2020, 2020],
            ["Total revenues", None, "63,648", None, None, "37,436"],
            ["Net income (loss)", None, "(14,626", None, None, "(4,572"],
        ]
    )
    columns = _period_columns(
        table, year=2020, period_phrase="Three Months Ended"
    )
    assert columns == [4, 5]
    assert _row_value(table, REVENUE_LABELS, columns) == (
        "Total revenues",
        37_436,
    )
    assert _row_value(table, NET_LABELS, columns) == (
        "Net income (loss)",
        -4_572,
    )


def test_krnt_parser_normalizes_double_space_period_headers():
    table = pd.DataFrame(
        [
            [None, "Three  Months Ended", "Three  Months Ended"],
            [None, "September 30,", "September 30,"],
            [None, 2019, 2019],
            ["Total  revenues", None, "44,580"],
            ["Net  income", None, "1,955"],
        ]
    )
    columns = _period_columns(
        table, year=2019, period_phrase="Three Months Ended"
    )
    assert _row_value(table, REVENUE_LABELS, columns)[1] == 44_580
    assert _row_value(table, NET_LABELS, columns)[1] == 1_955


def test_krnt_quarter_chain_requires_all_twenty_quarters():
    complete = list(pd.date_range("2017-03-31", "2021-12-31", freq="QE"))
    assert _longest_chain(complete) == 20
    assert _longest_chain(complete[:8] + complete[9:]) < 20
