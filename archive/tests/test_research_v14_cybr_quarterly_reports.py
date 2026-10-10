from pathlib import Path

import pandas as pd

from scripts.research_v14_cybr_quarterly_reports import (
    EXPECTED_ENDS,
    _longest_chain,
    _period_columns,
    _row_value,
)


def test_cybr_parser_selects_direct_gaap_rows() -> None:
    table = pd.DataFrame([
        [None, "Three months ended", "Three months ended"],
        [None, "March 31, 2021", "March 31, 2020"],
        ["Total revenues", "84,052", "61,016"],
        ["Net income", "17,616", "11,682"],
        ["Non-GAAP net income", "20,485", "13,481"],
    ])
    columns = _period_columns(
        table,
        fiscal_end=pd.Timestamp("2021-03-31"),
        period_phrase="Three months ended",
    )
    assert columns == [1]
    assert _row_value(table, labels=("Total revenues",), columns=columns) == (
        "Total revenues", 84_052,
    )
    assert _row_value(
        table,
        labels=(
            "Net income for the period", "Net income (loss) for the period",
            "Net income", "Net income (loss)",
        ),
        columns=columns,
    ) == ("Net income", 17_616)


def test_cybr_chain_requires_all_twenty_quarters() -> None:
    assert _longest_chain(EXPECTED_ENDS) == 20
    assert _longest_chain(EXPECTED_ENDS[:5] + EXPECTED_ENDS[6:]) < 20


def test_cybr_parser_accepts_period_and_date_in_same_header() -> None:
    table = pd.DataFrame([
        [None, "Three months ended December 31,", "Three months ended December 31,"],
        [None, 2017, 2016],
        ["Total revenues", "57,378", "50,212"],
        ["Net income for the period", "15,684", "12,009"],
    ])
    assert _period_columns(
        table,
        fiscal_end=pd.Timestamp("2017-12-31"),
        period_phrase="Three months ended",
    ) == [1]


def test_cybr_net_labels_accept_exact_gaap_net_loss() -> None:
    table = pd.DataFrame([
        [None, "Three Months Ended"],
        [None, "June 30, 2021"],
        ["Net loss", "(22,758)"],
        ["Non-GAAP net income", "13,500"],
    ])
    assert _row_value(
        table,
        labels=("Net income", "Net loss"),
        columns=[1],
    ) == ("Net loss", -22_758)


def test_cybr_registry_is_complete_sec_only_and_identity_continuous() -> None:
    registry = pd.read_csv(
        Path("stocks_list_dir/nasdaq/cybr_quarterly_reports.csv"),
        parse_dates=["fiscal_end", "filed_date"],
    )
    assert len(registry) == 20
    assert set(registry["ticker"]) == {"CYBR"}
    assert set(registry["cik"]) == {1598110}
    assert set(registry["form"]) == {"6-K"}
    assert registry["fiscal_end"].tolist() == EXPECTED_ENDS
    assert registry["source_url"].str.startswith(
        "https://www.sec.gov/Archives/edgar/data/1598110/"
    ).all()
