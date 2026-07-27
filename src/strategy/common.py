import numpy as np
import pandas as pd
import talib

from src.conf import LONG_MA, SHORT_MA


def monthly_signal_dates(
    trading_dates: pd.DatetimeIndex, start: str, end: str | None
) -> pd.DatetimeIndex:
    """Return the final observed trading close of each requested month."""
    dates = pd.DatetimeIndex(trading_dates).drop_duplicates().sort_values()
    mask = dates >= pd.Timestamp(start)
    if end is not None:
        mask &= dates <= pd.Timestamp(end)
    dates = dates[mask]
    return pd.DatetimeIndex(
        pd.Series(dates, index=dates).groupby(dates.to_period("M")).last().to_numpy()
    )


def scheduled_signal_dates(
    trading_dates: pd.DatetimeIndex,
    start: str,
    end: str | None,
    frequency: str = "monthly",
) -> pd.DatetimeIndex:
    """Return completed-period closes usable as signals without look-ahead."""
    dates = pd.DatetimeIndex(trading_dates).drop_duplicates().sort_values()
    mask = dates >= pd.Timestamp(start)
    if end is not None:
        mask &= dates <= pd.Timestamp(end)
    dates = dates[mask]
    if frequency == "daily":
        return dates
    period = {"weekly": "W-FRI", "monthly": "M"}.get(frequency)
    if period is None:
        raise ValueError(f"Unsupported signal frequency: {frequency}")
    return pd.DatetimeIndex(
        pd.Series(dates, index=dates).groupby(dates.to_period(period)).last().to_numpy()
    )


def next_trading_date(
    trading_dates: pd.DatetimeIndex, signal_date: pd.Timestamp
) -> pd.Timestamp | None:
    dates = pd.DatetimeIndex(trading_dates).drop_duplicates().sort_values()
    later = dates[dates > pd.Timestamp(signal_date)]
    return pd.Timestamp(later[0]) if len(later) else None


def market_regime_is_on(
    signal_date: pd.Timestamp,
    index_close: pd.Series,
    moving_average_days: int,
) -> bool:
    """Canonical point-in-time market regime used by backtest and daily output."""
    known = index_close.loc[:pd.Timestamp(signal_date)].dropna()
    if len(known) < moving_average_days:
        return False
    moving_average = known.rolling(moving_average_days).mean().iloc[-1]
    return bool(known.iloc[-1] > moving_average)


def online_monthly_rebalance_context(
    trading_dates: pd.DatetimeIndex,
    decision_date=None,
) -> dict:
    """Resolve the same month-end signal and next-close execution used in backtests."""
    dates = pd.DatetimeIndex(trading_dates).drop_duplicates().sort_values()
    if dates.empty:
        raise ValueError("No trading dates available")
    decision = pd.Timestamp(decision_date or pd.Timestamp.today().date()).normalize()
    dates = dates[dates.normalize() <= decision]
    if dates.empty:
        raise ValueError(f"No trading dates available on or before {decision.date()}")
    previous_month = decision.to_period("M") - 1
    completed = dates[dates.to_period("M") == previous_month]
    if completed.empty:
        raise ValueError(f"No completed-month trading data for {previous_month}")
    signal_date = pd.Timestamp(completed[-1])
    execution_date = next_trading_date(dates, signal_date)
    return {
        "as_of": pd.Timestamp(dates[-1]),
        "signal_date": signal_date,
        "execution_date": execution_date,
        "order_pending": execution_date is None,
    }


def online_rebalance_context(
    trading_dates: pd.DatetimeIndex,
    decision_date=None,
    frequency: str = "monthly",
) -> dict:
    """Resolve the latest completed scheduled signal and next-close execution."""
    if frequency == "monthly":
        return online_monthly_rebalance_context(trading_dates, decision_date)
    dates = pd.DatetimeIndex(trading_dates).drop_duplicates().sort_values()
    if dates.empty:
        raise ValueError("No trading dates available")
    decision = pd.Timestamp(decision_date or pd.Timestamp.today().date()).normalize()
    known = dates[dates.normalize() <= decision]
    if known.empty:
        raise ValueError(f"No trading dates available on or before {decision.date()}")
    if frequency == "daily":
        signal_date = pd.Timestamp(known[-1])
    elif frequency == "weekly":
        current_week = decision.to_period("W-FRI")
        completed = known[known.to_period("W-FRI") < current_week]
        if completed.empty:
            raise ValueError(f"No completed-week trading data before {current_week}")
        signal_date = pd.Timestamp(completed[-1])
    else:
        raise ValueError(f"Unsupported signal frequency: {frequency}")
    execution_date = next_trading_date(known, signal_date)
    return {
        "as_of": pd.Timestamp(known[-1]),
        "signal_date": signal_date,
        "execution_date": execution_date,
        "order_pending": execution_date is None,
    }


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

    df["short_ma"] = round(df["close"].rolling(short_ma).mean(), 4)
    df["long_ma"] = round(df["close"].rolling(long_ma).mean(), 4)

    df["50d_ma"] = round(df["close"].rolling(50).mean(), 4)

    return df


# 计算RSI和ADX指标
def calculate_rsi_adx(df, timeperiod=14):
    df["rsi"] = round(talib.RSI(df["close"], timeperiod=timeperiod), 2)  # type: ignore # RSI指标
    df["adx"] = round(
        talib.ADX(  # type: ignore # ADX指标
            df["high"], df["low"], df["close"], timeperiod=timeperiod
        ),
        2,
    )  # ADX平均趋向指数
    return df


# 计算MACD和买卖信号
# MACD指标是移动平均收敛/发散指标，常用于判断趋势的强弱和方向
# MACD的计算公式如下：
# DIF = EMA(Close, FastPeriod) - EMA(Close, SlowPeriod)
# DEA = EMA(DIF, SignalPeriod)
# MACD = 2 * (DIF - DEA)
# 其中，EMA为指数移动平均，FastPeriod、SlowPeriod和SignalPeriod分别为快速、慢速和信号线的周期。
def calculate_macd(df, fastperiod=12, slowperiod=26, signalperiod=9):
    df["macd"], df["macd_signal"], df["macd_hist"] = talib.MACD(  # type: ignore
        df["close"],
        fastperiod=fastperiod,
        slowperiod=slowperiod,
        signalperiod=signalperiod,
    )

    # MACD零上金叉
    macd_buy_signal = (df["macd"] > df["macd_signal"]) & (
        df["macd_hist"].shift(1) < 0
    )  # 柱状线由负转正
    df["macd_buy_signal"] = macd_buy_signal.astype(int)

    # MACD顶背离
    macd_sell_signal = (df["macd"] < df["macd_signal"]) & (
        df["high"] > df["high"].shift(2)
    )
    df["macd_sell_signal"] = macd_sell_signal.astype(int)

    return df


# 计算KDJ和买卖信号
# KDJ指标是随机指标的改进版，常用于判断超买超卖状态
# KDJ的计算公式如下：
# RSV = (C - L14) / (H14 - L14) * 100
# K = K-1 * (1 - α) + RSV * α
# D = D-1 * (1 - α) + K * α
# J = 3 * K - 2 * D
# 其中，C为当前收盘价，L14为过去14天的最低价，H14为过去14天的最高价，
# α为平滑系数（通常取1/3），K和D的初始值通常取50。
def calculate_kdj(
    df, window=14, alpha=1 / 3, adjust=False, buy_threshold=30, sell_threshold=70
):

    high_14 = df["high"].rolling(window=window).max()
    low_14 = df["low"].rolling(window=window).min()
    df["rsv"] = (df["close"] - low_14) / (high_14 - low_14) * 100
    df["k"] = round(df["rsv"].ewm(alpha=alpha, adjust=adjust).mean(), 2)  # K值
    df["d"] = round(df["k"].ewm(alpha=alpha, adjust=adjust).mean(), 2)  # D值
    df["j"] = round(3 * df["k"] - 2 * df["d"], 2)  # J值

    # KDJ超卖区金叉
    kdj_buy_signal = (df["k"] > df["d"]) & (df["j"] < buy_threshold)
    df["kdj_buy_signal"] = kdj_buy_signal.astype(int)

    # KDJ超买死叉
    kdj_sell_signal = (df["k"] < df["d"]) & (df["j"] > sell_threshold)
    df["kdj_sell_signal"] = kdj_sell_signal.astype(int)

    return df


# 计算布林带和买卖信号
# 布林带是基于移动平均线和标准差的波动率指标，常用于判断价格的波动范围
def calculate_bollinger_bands(df, window=50, num_std=3):

    df["ma"] = df["close"].rolling(window=window).mean()  # 中轨（移动平均线）
    std = df["close"].rolling(window=window).std()  # 滚动标准差
    df["upper"] = df["ma"] + (std * num_std)  # 上轨
    df["lower"] = df["ma"] - (std * num_std)  # 下轨

    if num_std == 0:
        # A zero-width band is useful as a simple mean-crossing baseline.
        bollinger_buy_signal = (df["close"] > df["ma"]) & (
            df["close"].shift(1) <= df["ma"].shift(1)
        )
        bollinger_sell_signal = (df["close"] < df["ma"]) & (
            df["close"].shift(1) >= df["ma"].shift(1)
        )
    else:
        # Mean reversion: enter only after price has actually touched the lower
        # band and crossed back inside.  The old implementation fired on almost
        # every observation below/above the mean.
        bollinger_buy_signal = (df["close"] > df["lower"]) & (
            df["close"].shift(1) <= df["lower"].shift(1)
        )
        bollinger_sell_signal = (df["close"] < df["upper"]) & (
            df["close"].shift(1) >= df["upper"].shift(1)
        )

    df["bollinger_buy_signal"] = bollinger_buy_signal.astype(int)
    df["bollinger_sell_signal"] = bollinger_sell_signal.astype(int)

    return df


def calculate_donchian_channel(df, window=20):
    """
    计算唐奇安通道

    :param df: 包含 close, high, low 列的数据框 (DataFrame)
    :param window: 唐奇安通道的窗口期长度，默认 20
    :return: 增加唐奇安上轨/中轨/下轨以及买卖信号列的数据框
    """
    # 上轨：过去 window 天的最高值
    df["donchian_upper"] = df["high"].shift(1).rolling(window=window).max()
    # 下轨：过去 window 天的最低值
    df["donchian_lower"] = df["low"].shift(1).rolling(window=window).min()
    # 中轨：上轨和下轨的均值
    df["donchian_middle"] = (df["donchian_upper"] + df["donchian_lower"]) / 2

    # 买入信号：收盘价突破上轨
    df["donchian_buy_signal"] = (df["close"] > df["donchian_upper"]).astype(int)
    # 卖出信号：收盘价跌破下轨
    df["donchian_sell_signal"] = (df["close"] < df["donchian_lower"]).astype(int)

    return df


def calculate_keltner_channel(df, window=20, atr_window=14, multiplier=1.5):
    """
    计算肯特纳通道

    :param df: 包含 close, high, low 列的数据框 (DataFrame)
    :param window: 移动平均窗口期长度，默认为 20
    :param atr_window: ATR 的窗口期长度，默认为 14
    :param multiplier: ATR 的乘数，默认为 1.5
    :return: 增加肯特纳中轨/上轨/下轨以及买卖信号列的数据框
    """
    # 中轨：收盘价的简单滑动均线
    df["keltner_middle"] = df["close"].rolling(window=window).mean().shift(1)

    # 使用 ta-lib 计算 ATR
    df["atr"] = talib.ATR(  # type: ignore
        df["high"], df["low"], df["close"], timeperiod=atr_window
    ).shift(1)

    # 上轨
    df["keltner_upper"] = df["keltner_middle"] + multiplier * df["atr"]
    # 下轨
    df["keltner_lower"] = df["keltner_middle"] - multiplier * df["atr"]

    # 买入信号：收盘价突破上轨
    df["keltner_buy_signal"] = (df["close"] > df["keltner_upper"]).astype(int)
    # 卖出信号：收盘价跌破下轨
    df["keltner_sell_signal"] = (df["close"] < df["keltner_lower"]).astype(int)

    return df


def calculate_sharpe_ratio(df):
    return round(
        df["daily_return"].mean() / (df["daily_return"].std() + 1e-8) * np.sqrt(252), 4
    )


def calculate_week_avg_volume(df):
    """
    高效计算每一行对应的【本周到当前日的成交量均值 (volume_avg_w)】
    和 【与上周相比的变化率 (volume_avg_w_pct)】。
    """
    # 创建辅助列
    df["week_start"] = df.index.to_period("W-SUN").start_time + pd.Timedelta(days=1)

    # 按周分组计算结果，动态计算本周均值
    week_group = df.groupby("week_start")

    # 1. 计算所有行对应的【本周到当前日期的均值】
    df["volume_avg_w"] = (
        week_group["volume"]
        .expanding(min_periods=1)  # 在该周内从周一开始累积计算均值
        .mean()
        .astype(int)
        .reset_index(level=0, drop=True)  # 去掉分组索引，恢复原表索引
    )

    # 2. 计算每周的「上周周一到周五的均值」
    # 获取每周一到周五的数据并计算均值
    last_week_avg = df.groupby("week_start")["volume"].mean()

    # 将 "上周的均值" 映射到当前周
    df["last_week_avg"] = df["week_start"].map(
        lambda x: last_week_avg.get(x - pd.Timedelta(days=7), np.nan)  # 寻找上一周
    )

    # 3. 计算变化率
    df["volume_avg_w_pct"] = (
        (df["volume_avg_w"] - df["last_week_avg"]) / df["last_week_avg"]
    ).round(
        2
    )  # 变化率保留两位小数

    return df


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
