import pandas as pd

from scripts.research_v14_rdwr_quarterly_reports import _number, _extract


def test_number_handles_split_parenthesis_cell():
    assert _number("(4,078") == -4_078
    assert _number(")") is None


def test_extract_selects_current_three_month_gaap_values(tmp_path):
    table = pd.DataFrame(
        [
            [None, "For the three months ended", None, None, None],
            [None, "June 30", None, "June 30", None],
            [None, 2020, None, 2019, None],
            ["Revenues", "58,445", None, "60,454", None],
            ["Net income", "673", None, "3,746", None],
        ]
    )
    path = tmp_path / "report.html"
    table.to_html(path, index=False, header=False)
    assert _extract(path, 2020) == {
        "revenue": 58_445_000.0,
        "net_income": 673_000.0,
    }
