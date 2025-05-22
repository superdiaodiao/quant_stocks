import numpy as np

from src.conf import LONG_MA, SHORT_MA
from src.strategy.common import calculate_moving_average, calculate_rsi_adx


def ma_strategy(df, short_ma=SHORT_MA, long_ma=LONG_MA):
    """结合RSI和ADX改进均线策略"""
    df = calculate_moving_average(df, short_ma=short_ma, long_ma=long_ma)
    df = calculate_rsi_adx(df)

    df.dropna(subset=["short_ma", "long_ma", "rsi", "adx"]).copy()

    df["signal"] = np.where(
        (df["short_ma"] > df["long_ma"]),
        1,  # 买入信号
        np.where(
            (df["short_ma"] < df["long_ma"]),
            0,  # 卖出信号
            np.nan,  # 无信号
        ),
    )

    return df.sort_index(ascending=True)
