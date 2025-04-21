import numpy as np
import talib
from conf import LONG_MA, SHORT_MA
from strategy.ma.position import ma_calculate_position


def ma_strategy(df, short_ma=SHORT_MA, long_ma=LONG_MA):
    """结合RSI和ADX改进均线策略"""
    df["short_ma"] = df["close"].rolling(short_ma).mean()
    df["long_ma"] = df["close"].rolling(long_ma).mean()

    df["rsi"] = talib.RSI(df["close"], timeperiod=14)  # type: ignore # RSI指标
    df["adx"] = talib.ADX(  # type: ignore # ADX指标
        df["high"], df["low"], df["close"], timeperiod=14
    )  # ADX平均趋向指数

    df.dropna(subset=["short_ma", "long_ma", "rsi", "adx"], inplace=True)

    df["signal"] = np.where(
        (df["short_ma"] > df["long_ma"]) & (df["rsi"] < 70) & (df["adx"] > 20),
        1,  # 买入信号
        np.where(
            (df["short_ma"] < df["long_ma"]) & (df["rsi"] > 70) & (df["adx"] < 30),
            0,  # 卖出信号
            np.nan,  # 无信号
        ),
    )

    close = df["close"].values
    signal = df["signal"].values

    # 调用 Numba 加速逻辑
    position, entry_price = ma_calculate_position(close, signal)
    df["position"] = position
    df["entry_price"] = entry_price

    df["daily_return"] = df["position"].shift(1) * df["close"].pct_change()
    # df.dropna(subset=["signal", "position"], inplace=True)

    return df.sort_index(ascending=True)
