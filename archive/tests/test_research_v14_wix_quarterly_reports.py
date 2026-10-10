import pandas as pd

from scripts.research_v14_wix_quarterly_reports import _extract, _number


def test_number_handles_split_parenthesis_cell():
    assert _number("(39,153") == -39_153
    assert _number(")") is None


def test_extract_direct_gaap_revenue(tmp_path):
    table = pd.DataFrame([
        [None, None, "Three Months Ended", None, None, None, "Three Months Ended", None, None],
        [None, None, "March 31", None, None, None, "March 31", None, None],
        [None, None, 2018, None, None, None, 2019, None, None],
        ["Revenue", None, "$", "137775", None, None, "$", "174290", None],
        ["Net loss", None, "$", "(19,811", ")", None, "$", "(30,740", ")"],
    ])
    path = tmp_path / "direct.html"
    table.to_html(path, index=False, header=False)
    assert _extract(path, 2019) == {
        "revenue": 174_290_000.0,
        "net_income": -30_740_000.0,
        "revenue_derivation": "direct_three_month_gaap",
    }


def test_extract_component_gaap_revenue_and_positive_income(tmp_path):
    table = pd.DataFrame([
        [None, None, "Three Months Ended", None, None, None, "Three Months Ended", None, None],
        [None, None, "June 30", None, None, None, "June 30", None, None],
        [None, None, 2020, None, None, None, 2021, None, None],
        ["Revenue", None, None, None, None, None, None, None, None],
        ["Creative Subscriptions", None, "$", "190169", None, None, "$", "235891", None],
        ["Business Solutions", None, None, "45890", None, None, None, "80515", None],
        ["Cost of Revenue", None, None, None, None, None, None, None, None],
        ["Net income (loss)", None, "$", "(57,736", ")", None, "$", "37957", None],
        ["Non-GAAP net income (loss)", None, "$", "(14,170", ")", None, "$", "(15,820", ")"],
    ])
    path = tmp_path / "components.html"
    table.to_html(path, index=False, header=False)
    assert _extract(path, 2021) == {
        "revenue": 316_406_000.0,
        "net_income": 37_957_000.0,
        "revenue_derivation": "sum_direct_three_month_gaap_revenue_components",
    }
