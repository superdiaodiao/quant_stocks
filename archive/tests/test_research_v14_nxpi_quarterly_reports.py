from pathlib import Path

import pandas as pd

from scripts.research_v14_nxpi_quarterly_reports import (
    EXPECTED_ENDS,
    _longest_chain,
    _period_columns,
    _row_value,
)


def test_nxpi_parser_selects_exact_three_month_period() -> None:
    table = pd.DataFrame([
        [None, "Three Months Ended", "Three Months Ended", "Full Year"],
        [None, "December 31, 2017", "October 1, 2017", "2017"],
        ["Revenue", "2,456", "2,387", "9,256"],
        ["Net income (loss)", "752", "70", "2,215"],
        ["Net income (loss) attributable to stockholders", "750", "68", "2,210"],
    ])
    columns = _period_columns(
        table,
        period_label="December 31, 2017",
        period_phrases=("Three months ended",),
    )
    assert columns == [1]
    assert _row_value(table, label="Revenue", columns=columns) == 2_456
    assert _row_value(table, label="Net income (loss)", columns=columns) == 752


def test_nxpi_parser_handles_split_currency_and_value_columns() -> None:
    table = pd.DataFrame([
        [None, "For the three months ended", "For the three months ended"],
        [None, "April 2, 2017", "April 2, 2017"],
        ["Revenue", "$", "2,211"],
        ["Net income (loss)", "$", "1,358"],
    ])
    columns = _period_columns(
        table,
        period_label="April 2, 2017",
        period_phrases=("Three months ended",),
    )
    assert columns == [1, 2]
    assert _row_value(table, label="Revenue", columns=columns) == 2_211
    assert _row_value(table, label="Net income (loss)", columns=columns) == 1_358


def test_nxpi_quarter_chain_requires_all_twelve_quarters() -> None:
    assert _longest_chain(EXPECTED_ENDS) == 12
    assert _longest_chain(EXPECTED_ENDS[:4] + EXPECTED_ENDS[5:]) < 12


def test_nxpi_registry_is_complete_and_sec_only() -> None:
    registry = pd.read_csv(
        Path("stocks_list_dir/nasdaq/nxpi_quarterly_reports.csv"),
        parse_dates=["fiscal_end", "filed_date"],
    )
    assert len(registry) == 12
    assert set(registry["ticker"]) == {"NXPI"}
    assert set(registry["cik"]) == {1413447}
    assert set(registry["form"]) == {"6-K", "8-K"}
    assert registry["fiscal_end"].tolist() == EXPECTED_ENDS
    assert registry["source_url"].str.startswith(
        "https://www.sec.gov/Archives/edgar/data/1413447/"
    ).all()
