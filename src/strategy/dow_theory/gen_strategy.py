import pandas as pd


def calculate_trendline(data, window=20, trend_type="up"):
    if trend_type == "up":
        # 选取最近N个低点连线
        lows = data["low"].rolling(window=window).min()
        return lows.iloc[-1]  # 返回趋势线当前值
    else:
        # 选取最近N个高点连线
        highs = data["high"].rolling(window=window).max()
        return highs.iloc[-1]


# 123法则的Python判断条件（以多头趋势反转为例）
def check_123_rules(data):
    # 条件1：价格跌破上升趋势线
    condition1 = data["close"].iloc[-1] < calculate_trendline(data, trend_type="up")

    # 条件2：反弹未创新高
    condition2 = data["high"].iloc[-1] < data["high"].iloc[-2]

    # 条件3：跌破前低确认反转
    condition3 = data["low"].iloc[-1] < data["low"].min()  # 最近N周期最低

    return condition1 & condition2 & condition3


def generate_signals(df):
    signals = pd.DataFrame(index=df.index)
    signals["trend_line"] = calculate_trendline(df)
    signals["condition1"] = df["close"] < signals["trend_line"]
    signals["condition2"] = df["high"] < df["high"].shift(1)
    signals["condition3"] = df["low"] < df["low"].rolling(50).min()
    signals["sell_signal"] = (
        signals["condition1"] & signals["condition2"] & signals["condition3"]
    )
    return signals
