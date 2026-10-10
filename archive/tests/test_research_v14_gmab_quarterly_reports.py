import pandas as pd

from scripts.research_v14_gmab_quarterly_reports import (
    _annual_from_flat_table,
    _number,
)


def test_number_handles_dkk_parentheses():
    assert _number("(1,472)") == -1_472
    assert _number("5,366") == 5_366


def test_annual_flat_statement_selects_requested_year():
    table = pd.DataFrame(
        [[
            "Primary Income Statement (DKK million) Note 2019 2018 "
            "Revenue 5,366 3,025 Net result before tax 2,859 1,612 "
            "Net result 2,166 1,472"
        ]]
    )
    assert _annual_from_flat_table(table, 2019) == {
        "revenue": 5_366.0,
        "net_income": 2_166.0,
    }
    assert _annual_from_flat_table(table, 2018) == {
        "revenue": 3_025.0,
        "net_income": 1_472.0,
    }
