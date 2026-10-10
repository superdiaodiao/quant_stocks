from pathlib import Path

import pandas as pd

from scripts.research_v14_paas_quarterly_reports import (
    COMPARATIVE_RESTATEMENTS,
    _longest_chain,
    _metric_pair_from_tables,
    _metric_values,
    _period_columns,
)


def test_paas_parser_selects_three_month_column_not_ytd():
    table = pd.DataFrame([
        [None, "Three months ended June 30,", "Six months ended June 30,"],
        [None, 2019, 2019],
        ["Revenue (Note 22)", "282,948", "525,100"],
        ["Net earnings for the period", "18,499", "12,200"],
    ])
    columns = _period_columns(
        table, fiscal_end=pd.Timestamp("2019-06-30"),
        period_phrase="Three months ended",
    )
    assert columns == [1]
    assert _metric_values(table, columns, metric="revenue") == [282_948]
    assert _metric_values(table, columns, metric="net_income") == [18_499]


def test_paas_parser_accepts_q4_direct_table_and_excludes_adjusted_earnings():
    table = pd.DataFrame([
        [None, "Three months ended December 31,", "Year ended December 31,"],
        [None, 2017, 2017],
        ["Revenue", "226,031", "816,828"],
        ["Net earnings for the period", "49,664", "123,451"],
        ["Adjusted earnings for the period", "19,219", "77,705"],
    ])
    columns = _period_columns(
        table, fiscal_end=pd.Timestamp("2017-12-31"),
        period_phrase="Three months ended",
    )
    assert _metric_values(table, columns, metric="revenue") == [226_031]
    assert _metric_values(table, columns, metric="net_income") == [49_664]


def test_paas_parser_accepts_exact_net_earnings_only():
    table = pd.DataFrame([
        [None, "Three months ended December 31,"],
        [None, 2020],
        ["Net earnings", "169,034"],
        ["Adjusted earnings", "57,974"],
        ["Net earnings attributable to equity holders", "168,900"],
    ])
    columns = _period_columns(
        table, fiscal_end=pd.Timestamp("2020-12-31"),
        period_phrase="Three months ended",
    )
    assert _metric_values(table, columns, metric="net_income") == [169_034]


def test_paas_parser_accepts_unique_split_consolidated_tables():
    header = [None, "Three months ended March 31,", "Three months ended March 31,"]
    years = [None, 2021, 2020]
    revenue_table = pd.DataFrame([
        header,
        years,
        ["Revenue (Note 22)", "368,099", "358,430"],
        ["Net loss and comprehensive loss", "(7,562)", "(77,221)"],
    ])
    net_income_table = pd.DataFrame([
        header,
        years,
        ["Net loss for the period", "(7,562)", "(77,221)"],
    ])
    assert _metric_pair_from_tables(
        [revenue_table, net_income_table],
        fiscal_end=pd.Timestamp("2021-03-31"),
        period_phrase="Three months ended",
    ) == (368_099, -7_562)


def test_paas_quarter_chain_requires_all_twenty_quarters():
    complete = list(pd.date_range("2017-03-31", "2021-12-31", freq="QE"))
    assert _longest_chain(complete) == 20
    assert _longest_chain(complete[:5] + complete[6:]) < 20


def test_paas_restatements_are_later_comparative_filings_only():
    assert COMPARATIVE_RESTATEMENTS == {
        pd.Timestamp("2020-03-31"): pd.Timestamp("2019-03-31"),
        pd.Timestamp("2020-06-30"): pd.Timestamp("2019-06-30"),
    }


def test_paas_registry_is_complete_sec_only_and_direct():
    registry = pd.read_csv(
        Path("stocks_list_dir/nasdaq/paas_quarterly_reports.csv"),
        parse_dates=["fiscal_end", "filed_date"],
    )
    assert len(registry) == 20
    assert set(registry["ticker"]) == {"PAAS"}
    assert set(registry["cik"]) == {771992}
    assert set(registry["source_kind"]) == {
        "interim_financial_statements", "q4_results_release"
    }
    assert registry["source_url"].str.startswith(
        "https://www.sec.gov/Archives/edgar/data/771992/"
    ).all()
    assert registry["fiscal_end"].tolist() == list(
        pd.date_range("2017-03-31", "2021-12-31", freq="QE")
    )
