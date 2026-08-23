import pandas as pd

from scripts.research_v14_team_sec_quarterly_filings import (
    _is_registry_boundary_truncation,
    _period_columns,
    _row_value,
)


def test_team_parser_uses_three_month_group_not_same_year_annual_group():
    table = pd.DataFrame([
        [None, "Three Months Ended June 30,", "Three Months Ended June 30,", None,
         "Fiscal Year Ended June 30,", "Fiscal Year Ended June 30,"],
        [None, 2019, 2019, None, 2019, 2019],
        ["Total revenues", "334,586", "334,586", None, "1,210,127", "1,210,127"],
        ["Net loss", "(237,517", "(237,517", None, "(637,621", "(637,621"],
    ])
    columns = _period_columns(table, 2019)
    assert columns == [1, 2]
    assert _row_value(table, "Total revenues", columns) == 334_586
    assert _row_value(table, "Net loss", columns) == -237_517
    annual_columns = _period_columns(table, 2019, "Fiscal Year Ended")
    assert annual_columns == [4, 5]
    assert _row_value(table, "Total revenues", annual_columns) == 1_210_127


def test_team_annual_cross_check_only_allows_registry_start_boundary():
    assert _is_registry_boundary_truncation(
        fiscal_start=pd.Timestamp("2016-07-01"),
        fiscal_end=pd.Timestamp("2017-06-30"),
        registry_start=pd.Timestamp("2017-03-31"),
    )
    assert not _is_registry_boundary_truncation(
        fiscal_start=pd.Timestamp("2017-07-01"),
        fiscal_end=pd.Timestamp("2018-06-30"),
        registry_start=pd.Timestamp("2017-03-31"),
    )
