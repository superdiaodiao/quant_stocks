from pathlib import Path

import pandas as pd

from scripts.research_v14_bili_quarterly_reports import (
    COMPARATIVE_ENDS,
    EXPECTED_ENDS,
    _longest_chain,
    _period_columns,
    _row_value,
)


def test_bili_parser_selects_rmb_three_month_column_only() -> None:
    table = pd.DataFrame([
        [None, "For the Three Months Ended", "For the Three Months Ended", "For the Three Months Ended"],
        [None, "March 31,", "March 31,", "March 31,"],
        [None, 2019, 2020, 2020],
        [None, "RMB", "RMB", "US$"],
        ["Total net revenues", "1,373,507", "2,315,535", "327,024"],
        ["Net loss", "(195,638)", "(538,555)", "(76,067)"],
        ["Net loss", "(195,638)", "(538,555)", "(76,067)"],
        ["Adjusted net loss", "(165,400)", "(474,600)", "(67,032)"],
    ])
    columns = _period_columns(
        table,
        fiscal_end=pd.Timestamp("2020-03-31"),
        period_phrase="Three Months Ended",
    )
    assert columns == [2]
    assert _row_value(table, label="Total net revenues", columns=columns) == 2_315_535
    assert _row_value(table, label="Net loss", columns=columns) == -538_555


def test_bili_chain_requires_all_twelve_quarters() -> None:
    assert _longest_chain(EXPECTED_ENDS) == 12
    assert _longest_chain(EXPECTED_ENDS[:4] + EXPECTED_ENDS[5:]) < 12


def test_bili_prior_year_comparatives_keep_later_filing_availability() -> None:
    assert COMPARATIVE_ENDS == {
        pd.Timestamp("2018-03-31"): pd.Timestamp("2017-03-31"),
        pd.Timestamp("2018-06-30"): pd.Timestamp("2017-06-30"),
        pd.Timestamp("2018-09-30"): pd.Timestamp("2017-09-30"),
        pd.Timestamp("2018-12-31"): pd.Timestamp("2017-12-31"),
    }


def test_bili_parser_accepts_combined_date_year_header() -> None:
    table = pd.DataFrame([
        [None, "For the Three Months Ended"],
        [None, "December 31, 2019"],
        [None, "RMB"],
        ["Total net revenues", "2,007,769"],
        ["Net loss", "(387,240)"],
    ])
    columns = _period_columns(
        table,
        fiscal_end=pd.Timestamp("2019-12-31"),
        period_phrase="Three Months Ended",
    )
    assert columns == [1]


def test_bili_registry_is_complete_sec_only_and_timely() -> None:
    registry = pd.read_csv(
        Path("stocks_list_dir/nasdaq/bili_quarterly_reports.csv"),
        parse_dates=["fiscal_end", "filed_date"],
    )
    assert len(registry) == 12
    assert set(registry["ticker"]) == {"BILI"}
    assert set(registry["cik"]) == {1723690}
    assert set(registry["form"]) == {"6-K"}
    assert registry["fiscal_end"].tolist() == EXPECTED_ENDS
    assert registry["source_url"].str.startswith(
        "https://www.sec.gov/Archives/edgar/data/1723690/"
    ).all()
    assert (registry["filed_date"] > registry["fiscal_end"]).all()
