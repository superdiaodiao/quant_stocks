import pandas as pd


def calculate_trendline(data, window=20, trend_type="up"):
    """计算趋势线"""
    if trend_type == "up":
        # 多头趋势：选取最近 N 个低点连线
        lows = data["low"].rolling(window=window).min()
        return lows.iloc[-1]  # 返回趋势线当前值
    else:
        # 空头趋势：选取最近 N 个高点连线
        highs = data["high"].rolling(window=window).max()
        return highs.iloc[-1]


def generate_signals(df, window=20):
    """生成买卖信号"""
    signals = pd.DataFrame(index=df.index)

    # 计算趋势线
    signals["trend_line_up"] = df["low"].rolling(window=window).min()
    signals["trend_line_down"] = df["high"].rolling(window=window).max()

    # 多头趋势反转信号 (buy)
    signals["buy_condition1"] = df["close"] > signals["trend_line_down"]
    signals["buy_condition2"] = df["low"] > df["low"].shift(1)
    signals["buy_condition3"] = df["high"] > df["high"].rolling(window).max()
    signals["buy_signal"] = (
        signals["buy_condition1"]
        & signals["buy_condition2"]
        & signals["buy_condition3"]
    )

    # 空头趋势反转信号 (sell)
    signals["sell_condition1"] = df["close"] < signals["trend_line_up"]
    signals["sell_condition2"] = df["high"] < df["high"].shift(1)
    signals["sell_condition3"] = df["low"] < df["low"].rolling(window).min()
    signals["sell_signal"] = (
        signals["sell_condition1"]
        & signals["sell_condition2"]
        & signals["sell_condition3"]
    )

    # ==================== 2B 准则 ====================

    ## 1. 2B 顶信号：价格创新高但未持续，随后下跌
    signals["prev_high"] = df["high"].shift(1)  # 先前的高点
    signals["2B_top_condition1"] = (
        df["high"] > signals["prev_high"]
    )  # 当前高点突破先前高点
    signals["2B_top_condition2"] = (
        df["close"] < signals["prev_high"]
    )  # 跌回至先前高点下方
    signals["2B_sell_signal"] = (
        signals["2B_top_condition1"] & signals["2B_top_condition2"]
    )

    ## 2. 2B 底信号：价格创新低但未持续，随后反弹
    signals["prev_low"] = df["low"].shift(1)  # 先前的低点
    signals["2B_bottom_condition1"] = (
        df["low"] < signals["prev_low"]
    )  # 当前低点突破先前低点
    signals["2B_bottom_condition2"] = (
        df["close"] > signals["prev_low"]
    )  # 收盘价重新回到先前低点上方
    signals["2B_buy_signal"] = (
        signals["2B_bottom_condition1"] & signals["2B_bottom_condition2"]
    )

    return signals
