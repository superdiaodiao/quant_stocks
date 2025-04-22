import numpy as np

from strategy.common import calculate_rsi_adx


"""
道氏理论的趋势线计算方法：
1.选择考虑的期间：长期（数个月至数年）、中期（数个星期至数个月）或短期（数天至数个星期）。
在既定的期间内，如果趋势线的斜率变动非常明显，可能同时存在数条趋势线。

2.上升趋势线：在考虑的期间内，以最低的低点为起点，向右上方绘制一条直线，
连接最高之高点前的某一个低点，而使这条直线在两个低点之间未穿越任何价位。
延伸这条直线而经过最高的高点。当趋势线经过所考虑最高之高点后，它可能穿越某些价位.

3.下降趋势线：在考虑的期间内，以最高的高点为起点，向右下方绘制一条直线，
连接最低之低点前的某一个高点，而使这条直线在两个高点之间不会穿越任何价位。
延伸这条直线而经过最低的低点.
"""
def calculate_trendlines(df, period="short"):
    """
    根据道氏理论计算上升和下降趋势线。
    Args:
        df (pd.DataFrame): 包含 'low', 'high', 和 'close' 列的输入数据框。
        period (str): 确定趋势的时间范围，可以是 'short', 'medium', or 'long'。
    Returns:
        pd.DataFrame: 包含新列 upward_trend 和 downward_trend 的数据框。
    """
    # 根据时期设定滑动窗口范围
    if period == "short":
        window = 10  # 短期，默认10天窗口
    elif period == "medium":
        window = 30  # 中期，默认30天窗口
    elif period == "long":
        window = 90  # 长期，默认90天窗口
    else:
        raise ValueError("Invalid period. Choose among 'short', 'medium', 'long'.")

    # 初始化趋势线
    df["upward_trend"] = np.nan
    df["downward_trend"] = np.nan

    # 遍历数据点，按窗口找到趋势线起点和终点
    for i in range(len(df) - window):
        # 当前窗口数据
        df_window = df.iloc[i : i + window]

        ### 计算上升趋势线 ###
        # 找到最低的低点作为起点
        low_start_idx = df_window["low"].idxmin()
        low_start = df.loc[low_start_idx, "low"]

        # 遍历窗口中剩余的低点，找到最高点前的一个低点作为终点
        best_end_idx, best_slope = None, None
        for j in df.index[
            df.index.get_loc(low_start_idx)
            + 1 : df.index.get_loc(df_window.index[-1])
            + 1
        ]:
            slope = (df.loc[j, "low"] - low_start) / ((j - low_start_idx).days)
            trendline_values = [
                low_start + slope * ((k - low_start_idx).days)
                for k in df.index[
                    df.index.get_loc(low_start_idx) : df.index.get_loc(j) + 1
                ]
            ]
            if np.all(df["low"].loc[low_start_idx:j] >= trendline_values):
                best_end_idx, best_slope = j, slope

        # 更新上升趋势线
        if best_end_idx:
            df.loc[low_start_idx:best_end_idx, "upward_trend"] = [
                low_start + best_slope * ((k - low_start_idx).days)
                for k in df.index[
                    df.index.get_loc(low_start_idx) : df.index.get_loc(best_end_idx) + 1
                ]
            ]

        ### 计算下降趋势线 ###
        # 找到最高的高点作为起点
        high_start_idx = df_window["high"].idxmax()
        high_start = df.loc[high_start_idx, "high"]

        # 遍历窗口中剩余的高点，找到最低点前的一个高点作为终点
        best_end_idx, best_slope = None, None
        for j in df.index[
            df.index.get_loc(high_start_idx)
            + 1 : df.index.get_loc(df_window.index[-1])
            + 1
        ]:
            slope = (df.loc[j, "high"] - high_start) / ((j - high_start_idx).days)
            trendline_values = [
                high_start + slope * ((k - high_start_idx).days)
                for k in df.index[
                    df.index.get_loc(high_start_idx) : df.index.get_loc(j) + 1
                ]
            ]
            if np.all(df["high"].loc[high_start_idx:j] <= trendline_values):
                best_end_idx, best_slope = j, slope

        # 更新下降趋势线
        if best_end_idx:
            df.loc[high_start_idx:best_end_idx, "downward_trend"] = [
                high_start + best_slope * ((k - high_start_idx).days)
                for k in df.index[
                    df.index.get_loc(high_start_idx) : df.index.get_loc(best_end_idx)
                    + 1
                ]
            ]

    return df


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


def dow_theory_strategy(df, window=20):
    # 计算趋势线
    calculate_trendlines(df)

    df = generate_signals(df, window)

    calculate_rsi_adx(df)

    return df.sort_index(ascending=True)
