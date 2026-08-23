import pandas as pd

from scripts.research_v14_gbdc_sec_quarterly_filings import _date_columns
from scripts.research_v14_dsgx_sec_quarterly_filings import _row_value


def test_gbdc_annual_quarter_table_selects_exact_period() -> None:
    table = pd.DataFrame([
        [None, "September 30, 2019", "September 30, 2019", None,
         "June 30, 2019", "June 30, 2019"],
        ["Total investment income", "$", "42,000", None, "$", "40,000"],
        ["Net increase in net assets resulting from operations",
         "$", "18,000", None, "$", "17,000"],
    ])
    columns = _date_columns(table, pd.Timestamp("2019-09-30"))
    assert _row_value(
        table, labels=("Total investment income",), columns=columns
    ) == 42_000
    assert _row_value(
        table,
        labels=("Net increase in net assets resulting from operations",),
        columns=columns,
    ) == 18_000


def test_gbdc_date_columns_rejects_absent_period() -> None:
    table = pd.DataFrame([[None, "September 30, 2019"], ["value", 1]])
    try:
        _date_columns(table, pd.Timestamp("2019-06-30"))
    except ValueError as error:
        assert "no column" in str(error)
    else:
        raise AssertionError("missing GBDC period should be rejected")
