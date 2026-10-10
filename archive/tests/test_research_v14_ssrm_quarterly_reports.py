import pandas as pd

from scripts.research_v14_ssrm_quarterly_reports import _extract, _number


def test_number_handles_accounting_parentheses_and_currency():
    assert _number("(12,345)") == -12345
    assert _number("$678") == 678
    assert _number("—") is None


def test_extract_selects_three_month_current_year(tmp_path):
    table = pd.DataFrame([
        [None, "Three months ended June 30", None, "Six months ended June 30"],
        [None, 2019, 2018, 2019],
        ["Revenue", "155,149", "104,028", "281,399"],
        ["Net income", "19,752", "7,653", "22,115"],
    ])
    path = tmp_path / "statement.htm"
    path.write_text(table.to_html(index=False, header=False))
    assert _extract(path, 2019, "three months ended") == {
        "revenue": 155_149_000.0,
        "net_income": 19_752_000.0,
    }
