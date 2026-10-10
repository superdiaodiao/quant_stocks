import pandas as pd

from scripts.research_v14_jd_quarterly_reports import _columns, _row_value


def test_jd_selects_rmb_column_not_usd_translation() -> None:
    table = pd.DataFrame([
        [None, "For the three months ended", "For the three months ended", "For the three months ended"],
        [None, "March 31, 2019", "March 31, 2019", "March 31, 2019"],
        [None, "RMB", None, "US$"],
        ["Total net revenues", 121081059, None, 18041642],
    ])
    columns = _columns(table, year=2019, period_phrase="For the three months ended")
    assert _row_value(table, "Total net revenues", columns) == 121_081_059_000.0


def test_jd_accounting_loss_preserves_negative_sign() -> None:
    table = pd.DataFrame([
        [None, "For the three months ended", "For the three months ended"],
        [None, "June 30, 2018", "June 30, 2018"],
        [None, "RMB", None],
        ["Net loss", "(2,277,175)", None],
    ])
    columns = _columns(table, year=2018, period_phrase="For the three months ended")
    assert _row_value(table, "Net loss", columns) == -2_277_175_000.0


def test_jd_exact_gaap_row_does_not_match_non_gaap_or_attributable_rows() -> None:
    table = pd.DataFrame([
        [None, "For the three months ended", "For the three months ended"],
        [None, "December 31, 2021", "December 31, 2021"],
        [None, "RMB", None],
        ["Net income/(loss)", "(5,318,720)", None],
        ["Net income/(loss) attributable to ordinary shareholders", "(5,165,000)", None],
        ["Non-GAAP net income attributable to ordinary shareholders", 3_565_000, None],
    ])
    columns = _columns(table, year=2021, period_phrase="For the three months ended")
    assert _row_value(table, "Net income/(loss)", columns) == -5_318_720_000.0
