# 根据移动平均线生成买卖信号
import numpy as np

from conf import LONG_MA, SHORT_MA
from strategy.ma.position import ma_calculate_position


def fixed_ma_strategy(df, short_ma=SHORT_MA, long_ma=LONG_MA):

    df["short_ma"] = df["close"].rolling(short_ma).mean()
    df["long_ma"] = df["close"].rolling(long_ma).mean()

    # 当10周移动平均线穿越30周移动平均线，而两者的斜率都向上，
    # 并且价格又同时位于两条移动平均线的上方时，这代表买进信号
    df["buy_signal"] = (
        (df["short_ma"] > df["long_ma"])
        & (df["short_ma"].shift(1) <= df["long_ma"].shift(1))
        & (df["short_ma"].diff() > 0)
        & (df["long_ma"].diff() > 0)
    )

    # 当10周移动平均线穿越30周移动平均线，而两者的斜率都向下，
    # 并且价格又同时位于两条移动平均线的下方时，这代表卖出信号
    df["sell_signal"] = (
        (df["short_ma"] < df["long_ma"])
        & (df["short_ma"].shift(1) >= df["long_ma"].shift(1))
        & (df["short_ma"].diff() < 0)
        & (df["long_ma"].diff() < 0)
    )

    df["signal"] = np.where(
        df["buy_signal"],
        1,  # 买入信号
        np.where(
            df["sell_signal"],
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

    return df.sort_index(ascending=True)
