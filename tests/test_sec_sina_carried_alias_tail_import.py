import pandas as pd

from scripts.sec_sina_carried_alias_tail_import import _carried_suffix_validation


def _frame(dates, closes, volumes):
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "ticker": "TEST",
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": volumes,
    })


def test_carried_suffix_validation_accepts_short_exact_repeated_tail():
    local = _frame(
        ["2026-06-11", "2026-06-12", "2026-06-15"],
        [17.43, 17.43, 17.43],
        [965200, 965200, 965200],
    )
    source = _frame(
        ["2026-06-12", "2026-06-15", "2026-06-16"],
        [17.37, 16.85, 16.30],
        [302416, 590366, 360031],
    )
    result = _carried_suffix_validation(local, source)
    assert result["passed"] is True
    assert result["carried_dates"] == ["2026-06-12", "2026-06-15"]
    assert result["anchor_date"] == "2026-06-11"


def test_carried_suffix_validation_ignores_unrelated_old_ticker_reuse_segment():
    local = _frame(
        ["2026-06-05", "2026-06-08", "2026-06-09"],
        [11.90, 11.90, 11.90],
        [33712, 33712, 33712],
    )
    source = _frame(
        ["1980-03-17", "2026-06-08", "2026-06-09", "2026-06-10"],
        [14.63, 13.80, 16.00, 21.94],
        [3100, 1079256, 4880482, 1863731],
    )
    result = _carried_suffix_validation(local, source)
    assert result["passed"] is True
    assert result["source_first_date"] == "2026-06-08"
    assert result["source_boundary_floor"] == "2026-05-30"


def test_carried_suffix_validation_rejects_real_local_tail_or_missing_source_date():
    real_tail = _frame(
        ["2026-06-11", "2026-06-12"], [17.43, 17.37], [965200, 302416]
    )
    source = _frame(["2026-06-12"], [17.37], [302416])
    assert _carried_suffix_validation(real_tail, source)["passed"] is False

    carried = _frame(
        ["2026-06-11", "2026-06-12", "2026-06-15"],
        [17.43, 17.43, 17.43],
        [965200, 965200, 965200],
    )
    missing_date = _frame(["2026-06-12"], [17.37], [302416])
    assert _carried_suffix_validation(carried, missing_date)["passed"] is False
