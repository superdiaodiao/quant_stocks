import pandas as pd

from scripts.research_v14_sec_filing_exhibit_financials import (
    _parse_accounting_number,
    parse_zlab_h1_2019,
)


def test_parse_accounting_number_handles_parentheses_and_dashes():
    assert _parse_accounting_number("(83,273,723") == -83_273_723
    assert _parse_accounting_number("3,420,183") == 3_420_183
    assert _parse_accounting_number("—") is None


def test_parse_zlab_h1_2019_requires_exact_table_and_values(tmp_path):
    table = pd.DataFrame([
        ["For the six months ended June 30,", None, None, None, None],
        [None, 2019, 2019, 2018, 2018],
        ["Revenue", None, "3,420,183", None, "—"],
        ["Net loss", None, "(83,273,723", None, "(41,490,428"],
    ])
    exhibit = tmp_path / "exhibit.htm"
    exhibit.write_text(table.to_html(index=False, header=False))

    rows = parse_zlab_h1_2019(exhibit).set_index("metric")
    assert rows.loc["revenue", "value"] == 3_420_183
    assert rows.loc["net_income", "value"] == -83_273_723
    assert set(rows["qtrs"]) == {2}
    assert set(rows["filed_date"]) == {pd.Timestamp("2019-09-03")}
