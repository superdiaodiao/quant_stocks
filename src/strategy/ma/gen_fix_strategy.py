# 根据移动平均线生成买卖信号
import numpy as np

from conf import LONG_MA, SHORT_MA
from strategy.common import calculate_moving_average, calculate_rsi_adx


def fixed_ma_strategy(df, short_ma=SHORT_MA, long_ma=LONG_MA):

    calculate_moving_average(df, short_ma, long_ma)
    
    calculate_rsi_adx(df)

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

    return df.sort_index(ascending=True)
