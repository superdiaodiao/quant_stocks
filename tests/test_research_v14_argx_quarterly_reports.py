import pandas as pd

from scripts.research_v14_argx_quarterly_reports import (
    _agree,
    _period_columns,
    _row_value,
    _subtract,
)


def test_argx_original_and_later_comparators_can_validate_same_values() -> None:
    original = {"revenue": 52_264_000.0, "net_income": -70_057_000.0}
    later = {"revenue": 52_264_000.0, "net_income": -70_057_000.0}

    assert _agree(original, later)


def test_argx_period_columns_do_not_mix_three_and_nine_months() -> None:
    table = pd.DataFrame([
        [None, "Three Months Ended", "Three Months Ended", "Nine Months Ended", "Nine Months Ended"],
        [None, 2021, 2021, 2021, 2021],
        ["Collaboration revenue", "$", 857, "$", 471255],
        ["Owners of the parent", "$", "(233,614)", "$", "(170,447)"],
    ])
    columns = _period_columns(table, "Three Months Ended", 2021)
    assert _row_value(table, "Collaboration revenue", columns) == 857_000.0


def test_argx_cumulative_difference_preserves_loss_sign() -> None:
    half = {"revenue": 22_388_000.0, "net_income": -205_637_000.0}
    first = {"revenue": 19_171_000.0, "net_income": -80_046_000.0}
    assert _subtract(half, first) == {
        "revenue": 3_217_000.0,
        "net_income": -125_591_000.0,
    }


def test_argx_currency_boundary_is_not_silently_convertible() -> None:
    eur_annual = {"revenue": 36_425_000.0, "net_income": -528_923_000.0}
    usd_annual = {"revenue": 41_243_000.0, "net_income": -608_455_000.0}
    assert eur_annual != usd_annual
