import numpy as np


def calculate_trendline(df, window=20, trend_type="up"):
    """计算趋势线"""
    if trend_type == "up":
        # 多头趋势：选取最近 N 个低点连线
        lows = df["low"].rolling(window=window).min()
        return lows.iloc[-1]  # 返回趋势线当前值
    else:
        # 空头趋势：选取最近 N 个高点连线
        highs = df["high"].rolling(window=window).max()
        return highs.iloc[-1]


def generate_signals(df, window=20):
    # 计算趋势线
    df["trend_line_up"] = df["low"].rolling(window=window).min()
    df["trend_line_down"] = df["high"].rolling(window=window).max()

    # 多头趋势反转信号 (buy)
    df["buy_condition1"] = df["close"] > df["trend_line_down"]
    df["buy_condition2"] = df["low"] > df["low"].shift(1)
    df["buy_condition3"] = df["high"] > df["high"].rolling(window).max()
    df["123_buy_signal"] = (
        df["buy_condition1"] & df["buy_condition2"] & df["buy_condition3"]
    )

    # 空头趋势反转信号 (sell)
    df["sell_condition1"] = df["close"] < df["trend_line_up"]
    df["sell_condition2"] = df["high"] < df["high"].shift(1)
    df["sell_condition3"] = df["low"] < df["low"].rolling(window).min()
    df["123_sell_signal"] = (
        df["sell_condition1"] & df["sell_condition2"] & df["sell_condition3"]
    )

    # ==================== 2B 准则 ====================

    ## 1. 2B 顶信号：价格创新高但未持续，随后下跌
    df["prev_high"] = df["high"].shift(1)  # 先前的高点
    df["2B_top_condition1"] = df["high"] > df["prev_high"]  # 当前高点突破先前高点
    df["2B_top_condition2"] = df["close"] < df["prev_high"]  # 跌回至先前高点下方
    df["2B_sell_signal"] = df["2B_top_condition1"] & df["2B_top_condition2"]

    ## 2. 2B 底信号：价格创新低但未持续，随后反弹
    df["prev_low"] = df["low"].shift(1)  # 先前的低点
    df["2B_bottom_condition1"] = df["low"] < df["prev_low"]  # 当前低点突破先前低点
    df["2B_bottom_condition2"] = (
        df["close"] > df["prev_low"]
    )  # 收盘价重新回到先前低点上方
    df["2B_buy_signal"] = df["2B_bottom_condition1"] & df["2B_bottom_condition2"]

    df["signal"] = np.where(
        df["2B_buy_signal"],
        1,  # 买入信号
        np.where(
            df["2B_sell_signal"],
            0,  # 卖出信号
            np.nan,  # 无信号
        ),
    )

    return df
