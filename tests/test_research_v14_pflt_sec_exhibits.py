import pandas as pd

from scripts.research_v14_pflt_sec_exhibits import (
    EXPECTED_Q4,
    EXPECTED_QUARTERLY_CLOSURE_DIFFERENCE,
    FILINGS,
    _accounting_number,
    _period_value,
)


def test_pflt_sources_are_contemporaneous_and_comparatives_match() -> None:
    assert FILINGS["2019Q1"]["filed"] == "2019-02-06"
    assert FILINGS["2020Q1"]["filed"] == "2020-02-05"
    assert FILINGS["2021FY"]["filed"] == "2021-11-17"
    assert all(item["accession"].startswith("0001171843-") for item in FILINGS.values())
    for current_key, comparison_key in (
        ("2019Q1", "2020Q1"),
        ("2019Q2", "2020Q2"),
        ("2019Q3", "2020Q3"),
        ("2019FY", "2020FY"),
        ("2020Q1", "2021Q1"),
        ("2020Q2", "2021Q2"),
        ("2020Q3", "2021Q3"),
        ("2020FY", "2021FY"),
    ):
        assert FILINGS[current_key]["current"] == FILINGS[comparison_key]["prior"]


def test_pflt_q4_residuals_and_source_discrepancies_are_explicit() -> None:
    assert EXPECTED_Q4 == {
        2019: (23_881_603.0, 7_373_820.0),
        2020: (21_755_738.0, 16_986_054.0),
        2021: (21_619_094.0, 4_004_632.0),
    }
    assert EXPECTED_QUARTERLY_CLOSURE_DIFFERENCE == {
        2019: (0.0, 0.0),
        2020: (-3.0, 0.0),
        2021: (0.0, 1.0),
    }


def test_pflt_accounting_number_handles_split_parentheses() -> None:
    assert _accounting_number("26,326,437") == 26_326_437.0
    assert _accounting_number("(21,101,049") == -21_101_049.0
    assert _accounting_number(")") is None
    assert _accounting_number("$") is None


def test_pflt_period_parser_selects_heading_and_year() -> None:
    table = pd.DataFrame([
        [None, "Three Months Ended March\xa031", "Three Months Ended March\xa031", None, "Three Months Ended March\xa031", "Three Months Ended March\xa031"],
        [None, 2021, 2021, None, 2020, 2020],
        ["Total investment income", "$", "19,435,021", None, "$", "26,326,437"],
        ["Net increase (decrease) in net assets resulting from operations", "$", "11,673,345", None, "$", "(21,101,049"],
    ])
    assert _period_value(
        table, "revenue", "Three Months Ended March 31", 2021
    ) == 19_435_021.0
    assert _period_value(
        table, "net_income", "Three Months Ended March 31", 2020
    ) == -21_101_049.0
