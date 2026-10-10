import pandas as pd

from scripts.research_v14_pdd_quarterly_reports import _extract, _number


def test_number_handles_rmb_parentheses():
    assert _number("(2,335,000)") == -2_335_000
    assert _number("4,545,204") == 4_545_204
    assert _number("—") is None


def test_extract_uses_current_year_rmb_not_usd(tmp_path):
    table = pd.DataFrame([
        [None, "For the three months ended June 30", None, None],
        [None, 2019, 2019, 2019],
        [None, "RMB", "US$", "RMB"],
        ["Total Revenues", "7,290,008", "1,061,909", "7,290,008"],
        ["Net loss", "(1,003,000)", "(145,000)", "(1,003,000)"],
    ])
    path = tmp_path / "pdd.htm"
    path.write_text(table.to_html(index=False, header=False))
    assert _extract(path, 2019, "three months ended") == {
        "revenue": 7_290_008_000.0,
        "net_income": -1_003_000_000.0,
    }
