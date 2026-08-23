import pandas as pd

from scripts.research_v14_chkp_quarterly_reports import (
    _columns,
    _row_value,
    _values_agree,
)


def test_chkp_selects_three_month_value_not_six_month_value() -> None:
    table = pd.DataFrame([
        [None, "Three Months Ended", "Three Months Ended", None, "Six Months Ended", "Six Months Ended"],
        [None, "2019", "2019", None, "2019", "2019"],
        ["Total revenues", "$", 488.1, None, "$", 960.0],
        ["Net income", "$", 150.0, None, "$", 300.0],
    ])
    columns = _columns(table, year=2019, period_phrase="Three Months Ended")
    assert _row_value(table, "Total revenues", columns, 1_000_000.0) == 488_100_000.0


def test_chkp_scaling_preserves_thousand_reports() -> None:
    table = pd.DataFrame([
        [None, "Three Months Ended", "Three Months Ended"],
        [None, "2018", "2018"],
        ["Total revenues", "$", 452326],
    ])
    columns = _columns(table, year=2018, period_phrase="Three Months Ended")
    assert _row_value(table, "Total revenues", columns, 1_000.0) == 452_326_000.0


def test_chkp_million_comparator_tolerance_is_bounded() -> None:
    original = {"revenue": 525_556_000.0, "net_income": 238_249_000.0}
    rounded = {"revenue": 525_600_000.0, "net_income": 238_300_000.0}
    assert not _values_agree(original, rounded)
    assert _values_agree(original, rounded, tolerance=100_000.01)
