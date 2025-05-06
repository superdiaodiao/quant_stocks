import numpy as np

from numba import njit
from src.strategy.common import calculate_rsi_adx


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


@njit  # 使用 numba 加速函数
def calculate_trendlines_numba(low, high, n, window):
    """
    使用 numba 加速的道氏趋势线计算。
    Args:
        low (ndarray): 低点数组。
        high (ndarray): 高点数组。
        n (int): 数据长度。
        window (int): 滑动窗口大小。
    Returns:
        upward_trends (ndarray), downward_trends (ndarray): 上升趋势线和下降趋势线的数组。
    """
    # 初始化结果数组
    upward_trends = np.full(n, np.nan)
    downward_trends = np.full(n, np.nan)

    # 遍历数据点，按窗口找到趋势线起点和终点
    for i in range(n - window):
        # 当前窗口
        start = i
        end = i + window

        ## 计算上升趋势线 ##
        # 找到最低的低点作为起点
        low_start_idx = np.argmin(low[start:end]) + start  # 获取全局索引
        low_start = low[low_start_idx]

        # 遍历窗口中剩余的低点，找到最高点前的一个低点作为终点
        best_end_idx, best_slope = -1, -np.inf
        for j in range(low_start_idx + 1, end):
            slope = (low[j] - low_start) / (j - low_start_idx)
            valid = True
            for k in range(low_start_idx, j + 1):
                if low[k] < low_start + slope * (k - low_start_idx):  # 检查是否穿越价格
                    valid = False
                    break
            if valid and slope > best_slope:
                best_end_idx = j
                best_slope = slope

        # 更新上升趋势线
        if best_end_idx > -1:
            for k in range(low_start_idx, best_end_idx + 1):
                upward_trends[k] = low_start + best_slope * (k - low_start_idx)

        ## 计算下降趋势线 ##
        # 找到最高的高点作为起点
        high_start_idx = np.argmax(high[start:end]) + start  # 获取全局索引
        high_start = high[high_start_idx]

        # 遍历窗口中剩余的高点，找到最低点前的一个高点作为终点
        best_end_idx, best_slope = -1, np.inf
        for j in range(high_start_idx + 1, end):
            slope = (high[j] - high_start) / (j - high_start_idx)
            valid = True
            for k in range(high_start_idx, j + 1):
                if high[k] > high_start + slope * (
                    k - high_start_idx
                ):  # 检查是否穿越价格
                    valid = False
                    break
            if valid and slope < best_slope:
                best_end_idx = j
                best_slope = slope

        # 更新下降趋势线
        if best_end_idx > -1:
            for k in range(high_start_idx, best_end_idx + 1):
                downward_trends[k] = high_start + best_slope * (k - high_start_idx)

    return upward_trends, downward_trends


def calculate_trendlines(df, period="short"):
    """
    根据道氏理论计算趋势线，使用 numba 进行优化。
    Args:
        df (pd.DataFrame): 包含 'low', 'high', 和 'close' 的输入数据框。
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

    # 转换为 NumPy 数组
    low = df["low"].to_numpy()
    high = df["high"].to_numpy()
    n = len(df)

    # 使用 numba 加速计算
    upward_trends, downward_trends = calculate_trendlines_numba(low, high, n, window)

    # 结果赋值回 DataFrame
    df["upward_trend"] = upward_trends
    df["downward_trend"] = downward_trends

    return df


def generate_signals(df, window=20):
    """
    TODO: 书中讲的1-2-3准则的信号没有2B的早, 而且趋势线相对难以画出, 这部分先搁置
    """

    # 计算趋势线
    # df = calculate_trendlines(df, period="short")
    # df["trend_line_up"] = df["upward_trend"]
    # df["trend_line_down"] = df["downward_trend"]

    # # 多头趋势反转信号 (buy)
    # df["buy_condition1"] = df["close"] > df["trend_line_down"]
    # df["buy_condition2"] = df["low"] > df["low"].shift(1)
    # df["buy_condition3"] = df["high"] > df["high"].rolling(window).max()
    # df["123_buy_signal"] = (
    #     df["buy_condition1"] & df["buy_condition2"] & df["buy_condition3"]
    # )

    # # 空头趋势反转信号 (sell)
    # df["sell_condition1"] = df["close"] < df["trend_line_up"]
    # df["sell_condition2"] = df["high"] < df["high"].shift(1)
    # df["sell_condition3"] = df["low"] < df["low"].rolling(window).min()
    # df["123_sell_signal"] = (
    #     df["sell_condition1"] & df["sell_condition2"] & df["sell_condition3"]
    # )

    # ==================== 2B 准则 ====================

    # 在盘中的短线趋势中，价格创新高（新低）之后，如果2B准则成立，通常会发生在一天之内或更短。
    # 在中期趋势中，价格创新高或新低之后，如果2B准则得以成立，它通常会发生在3～5天之内。
    # 在市场的主要（长期）转折点上，价格创新高或新低之后，如果2B准则得以成立，通常会发生在7～10天之内。
    # 在股票市场中，价格创新高之后，随后走势的成交量通常会低于正常水平，但反转的确认（即当价格跌破先前的高点时）却会爆发大额成交量

    ### 如果是短期的话，应该用以下的做法 ###
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

    ### 如果是非中长期的话，应该用以下的做法 ###
    # from scipy.signal import argrelextrema

    # # 寻找局部高点和低点
    # n = 2  # 设置前后点数，用于判断局部极值
    # df["local_high"] = df["close"].iloc[argrelextrema(df["close"].values, np.greater, order=n)[0]]
    # df["local_low"] = df["close"].iloc[argrelextrema(df["close"].values, np.less, order=n)[0]]

    # # 2B 顶信号
    # df['2B_top_condition1'] = df['close'] > df['local_high'].shift(1)
    # df['2B_top_condition2'] = df['close'] < df['local_high'].shift(1)
    # df['2B_sell_signal'] = df['2B_top_condition1'] & df['2B_top_condition2']

    # # 2B 底信号
    # df['2B_bottom_condition1'] = df['close'] < df['local_low'].shift(1)
    # df['2B_bottom_condition2'] = df['close'] > df['local_low'].shift(1)
    # df['2B_buy_signal'] = df['2B_bottom_condition1'] & df['2B_bottom_condition2']

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

    df = generate_signals(df, window)

    calculate_rsi_adx(df)

    return df.sort_index(ascending=True)
