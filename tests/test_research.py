import numpy as np
import pandas as pd
import pytest

from src.research.metrics import annual_returns
from src.research.data_quality import back_adjust_common_splits, stock_returns_with_delisting_penalty
from src.strategy.common import (
    calculate_bollinger_bands,
    calculate_donchian_channel,
    calculate_keltner_channel,
)


def test_donchian_uses_prior_window():
    df = pd.DataFrame({"high": [1, 2, 3, 4], "low": [0, 0, 0, 0], "close": [1, 2, 3, 4]})
    calculate_donchian_channel(df, window=2)
    assert df.loc[2, "donchian_upper"] == 2
    assert df.loc[2, "donchian_buy_signal"] == 1


def test_keltner_band_is_frozen_before_the_signal_close():
    dates = pd.bdate_range("2024-01-01", periods=25)
    close = pd.Series([10.0] * 24 + [20.0], index=dates)
    df = pd.DataFrame({"close": close, "high": close + 0.5, "low": close - 0.5})

    calculate_keltner_channel(df, window=20, atr_window=14, multiplier=1.5)

    assert df.loc[dates[-1], "keltner_middle"] == 10.0
    assert df.loc[dates[-1], "keltner_buy_signal"] == 1


def test_bollinger_does_not_fire_every_day_below_mean():
    close = pd.Series([10.0] * 20 + [8.0, 8.1, 8.2])
    df = pd.DataFrame({"close": close})
    calculate_bollinger_bands(df, window=20, num_std=1)
    assert df["bollinger_buy_signal"].sum() <= 1


def test_annual_returns_compounds_by_calendar_year():
    idx = pd.to_datetime(["2023-12-29", "2024-01-02"])
    result = pd.DataFrame({"strategy": [0.1, 0.2], "benchmark": [0.05, 0.1]}, index=idx)
    annual = annual_returns(result)
    assert annual.loc[2023, "strategy"] == pytest.approx(0.1)
    assert annual.loc[2024, "excess"] == pytest.approx(0.1)


def test_split_adjustment_removes_ten_for_one_price_jump():
    idx = pd.date_range("2025-01-01", periods=3, freq="B")
    close = pd.DataFrame({"NFLX": [1000.0, 100.0, 101.0]}, index=idx)
    adjusted = back_adjust_common_splits(close)
    assert adjusted.loc[idx[0], "NFLX"] == pytest.approx(100.0)
    assert adjusted.pct_change(fill_method=None).loc[idx[1], "NFLX"] == pytest.approx(0.0)


def test_missing_prices_resume_from_last_trade_and_ended_history_is_penalized_once():
    idx = pd.date_range("2025-01-01", periods=5, freq="B")
    close = pd.DataFrame({
        "RESUMES": [10.0, np.nan, 11.0, 12.0, 13.0],
        "ENDS": [10.0, 9.0, 8.0, np.nan, np.nan],
        "NOT_LISTED": [np.nan, np.nan, 5.0, 6.0, 7.0],
    }, index=idx)
    returns = stock_returns_with_delisting_penalty(close)
    assert returns.loc[idx[1], "RESUMES"] == 0
    assert returns.loc[idx[2], "RESUMES"] == pytest.approx(0.1)
    assert returns.loc[idx[3], "ENDS"] == -1
    assert returns.loc[idx[4], "ENDS"] == 0
    assert pd.isna(returns.loc[idx[1], "NOT_LISTED"])
