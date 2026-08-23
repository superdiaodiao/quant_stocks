from pathlib import Path

import pandas as pd

from scripts.research_v14_gds_quarterly_reports import (
    NET_LABELS,
    REVENUE_LABELS,
    _longest_chain,
    _period_columns,
    _row_values,
)


def test_gds_parser_selects_current_rmb_three_month_column_only():
    table = pd.DataFrame(
        [
            [None, "Three months ended", "Three months ended", "Three months ended"],
            [None, "June 30, 2018", "June 30, 2019", "June 30, 2019"],
            [None, "RMB", "RMB", "US$"],
            ["Total net revenue", "637,510", "985,189", "143,509"],
            ["Net loss", "(102,077)", "(93,159)", "(13,570)"],
        ]
    )
    columns = _period_columns(
        table,
        fiscal_end=pd.Timestamp("2019-06-30"),
        period_phrase="Three months ended",
    )
    assert columns == [2]
    assert _row_values(table, REVENUE_LABELS, columns) == [985_189]
    assert _row_values(table, NET_LABELS, columns) == [-93_159]


def test_gds_parser_does_not_use_year_ended_column_for_q4():
    table = pd.DataFrame(
        [
            [None, "Three months ended", "Year ended"],
            [None, "December 31, 2020", "December 31, 2020"],
            [None, "RMB", "RMB"],
            ["Total net revenue", "1,873,645", "5,739,061"],
            ["Net loss", "(271,684)", "(669,220)"],
        ]
    )
    columns = _period_columns(
        table,
        fiscal_end=pd.Timestamp("2020-12-31"),
        period_phrase="Three months ended",
    )
    assert columns == [1]


def test_gds_quarter_chain_requires_all_twenty_quarters():
    complete = list(pd.date_range("2017-03-31", "2021-12-31", freq="QE"))
    assert _longest_chain(complete) == 20
    assert _longest_chain(complete[:10] + complete[11:]) < 20


def test_gds_registry_is_complete_and_sec_only():
    registry = pd.read_csv(
        Path("stocks_list_dir/nasdaq/gds_quarterly_reports.csv"),
        parse_dates=["fiscal_end", "filed_date"],
    )
    assert len(registry) == 20
    assert set(registry["ticker"]) == {"GDS"}
    assert set(registry["cik"]) == {1526125}
    assert registry["source_url"].str.startswith(
        "https://www.sec.gov/Archives/edgar/data/1526125/"
    ).all()
    assert registry["fiscal_end"].tolist() == list(
        pd.date_range("2017-03-31", "2021-12-31", freq="QE")
    )
