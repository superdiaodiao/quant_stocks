import pandas as pd

from scripts.kaggle_minute_price_repair import validate_overlap


def test_minute_daily_overlap_allows_small_ohlc_but_not_close_drift() -> None:
    dates = pd.date_range("2025-07-01", periods=5, freq="B")
    local = pd.DataFrame({
        "date": dates, "open": 10.0, "high": 10.2, "low": 9.8,
        "close": 10.0, "volume": 100,
    })
    source = local.copy()
    source["high"] *= 0.995
    assert validate_overlap(source, local)["passed"] is True
    source["close"] *= 0.95
    assert validate_overlap(source, local)["passed"] is False


def test_minute_overlap_allows_extended_hours_low_difference() -> None:
    dates = pd.date_range("2025-07-01", periods=6, freq="B")
    local = pd.DataFrame({
        "date": dates, "open": 10.0, "high": 10.2, "low": 9.8,
        "close": 10.0, "volume": 100,
    })
    source = local.copy()
    source.loc[:1, "low"] *= 0.95
    report = validate_overlap(source, local)
    assert report["passed"] is True
    assert report["field_within_1pct"]["low"] == 4 / 6


def test_empty_minute_source_is_rejected_without_dtype_error() -> None:
    local = pd.DataFrame({
        "date": pd.to_datetime(["2025-01-02"]), "open": [1], "high": [1],
        "low": [1], "close": [1], "volume": [1],
    })
    source = pd.DataFrame(columns=local.columns)
    report = validate_overlap(source, local)
    assert report["passed"] is False
    assert report["sessions"] == 0
