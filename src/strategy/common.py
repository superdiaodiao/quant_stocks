import numpy as np
import talib

from conf import LONG_MA, SHORT_MA


def calculate_max_drawdown(df):
    """仅计算首次持仓信号之后的最大回撤"""
    if len(df) == 0 or df["daily_return"].isna().all():
        return None  # 返回 None 表示无法计算

    # 找到首次持仓的时间点
    first_position_index = df[df["position"] == 1].index.min()

    # 如果没有有效持仓信号，返回 None
    if first_position_index is None:
        return None

    # 只保留从 first_position_index 开始的数据
    df = df.loc[first_position_index:]

    # 从每日收益率生成累计收益
    cumulative_returns = (1 + df["daily_return"]).cumprod()
    # 记录历史最大累计收益
    peak = cumulative_returns.expanding(min_periods=1).max()
    # 计算回撤比例
    drawdown = (cumulative_returns - peak) / peak
    # 返回最大回撤（最大负值）
    return drawdown.min()


# 计算移动平均线
def calculate_moving_average(df, short_ma=SHORT_MA, long_ma=LONG_MA):
    df["short_ma"] = df["close"].rolling(short_ma).mean()
    df["long_ma"] = df["close"].rolling(long_ma).mean()
    return df


# 计算RSI和ADX指标
def calculate_rsi_adx(df):
    df["rsi"] = talib.RSI(df["close"], timeperiod=14)  # type: ignore # RSI指标
    df["adx"] = talib.ADX(  # type: ignore # ADX指标
        df["high"], df["low"], df["close"], timeperiod=14
    )  # ADX平均趋向指数
    return df


def calculate_sharpe_ratio(df):
    return round(
        df["daily_return"].mean() / (df["daily_return"].std() + 1e-8) * np.sqrt(252), 4
    )


# 宽度震荡指标
# 登记上涨与下跌家数的净值；我以纽约证券交易所近10天以来的资料为准，
# 并计算、登录、绘制上涨与下跌家数之净值的移动总和。
# 换言之，每天都会计算前一天上涨与下跌家数的净值，然后加总近10天的数据。
def calculate_breadth_oscillator(df, advancers, decliners):
    df["Net_Advances"] = advancers - decliners
    df["Breadth_Oscillator"] = df["Net_Advances"].rolling(window=10).mean()
    return df


# 价格震荡指标（重要性也高于行情宽度震荡指标）
# 以纽约综合指数每天的收盘价为基准计算，计算前一天与先前第5天的收盘价差值。
# 然后，加总最近10天以来的上述差值，这便是10天移动总和。
# 最后，再以昨天的10天移动总和，减去先前第10天的10天移动总和，这便是5天价差的10天净移动总和
def calculate_price_oscillator(df):
    df["5d_Price_Change"] = df["close"] - df["close"].shift(5)
    df["10d_Momentum"] = df["5d_Price_Change"].rolling(window=10).sum()
    df["Price_Oscillator"] = df["10d_Momentum"] - df["10d_Momentum"].shift(10)
    return df
