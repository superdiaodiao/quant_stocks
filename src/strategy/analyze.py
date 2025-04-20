import numpy as np
import pandas as pd
import talib
from numba import jit
from tqdm import tqdm

from src.conf import DEFAULT_END_DATE, SHORT_MA, LONG_MA
from src.io.read_data import load_stocks_data, get_stock_list
from src.io.save_files import save_signals


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


def calculate_sharpe_ratio(df):
    return round(
        df["daily_return"].mean() / (df["daily_return"].std() + 1e-8) * np.sqrt(252), 4
    )


def add_rsi_adx_index(df):
    """添加技术指标到DataFrame"""
    df["rsi"] = talib.RSI(df["close"], timeperiod=14)  # type: ignore # RSI指标
    df["adx"] = talib.ADX(  # type: ignore # ADX指标
        df["high"], df["low"], df["close"], timeperiod=14
    )  # ADX平均趋向指数
    return df


@jit(nopython=True)
def calculate_position_numba(close, signal):
    n = len(close)
    position = np.zeros(n, dtype=np.int64)  # 初始化 position 列
    entry_price = np.full(n, np.nan)  # 初始化 entry_price 列

    for i in range(1, n):  # 从第1天开始处理
        if position[i - 1] == 1:  # 如果上一天有持仓
            # 当前收益率 = (当前价格 - 买入价格) / 买入价格
            current_return = (close[i] - entry_price[i - 1]) / entry_price[i - 1]

            if current_return >= 0.15 or signal[i] == 0:  # 当前收益率 >= 10%或者卖出信号
                # 卖出
                position[i] = 0  # 清仓后不再持仓
                entry_price[i] = np.nan  # 清空买入价格记录
            else:
                # 无操作
                position[i] = 1  # 保持持仓
                entry_price[i] = entry_price[i - 1]  # 保留买入价格

        elif position[i - 1] == 0:  # 如果上一天未持仓
            if signal[i - 1] == 1:  # 上一天出现买入信号
                # 开仓买入
                position[i] = 1  # 更新状态为持仓
                entry_price[i] = close[i]  # 记录买入价格
            else:
                # 无操作
                position[i] = 0
                entry_price[i] = np.nan  # 保持为缺失值

    return position, entry_price


def refined_strategy(df, short_ma=SHORT_MA, long_ma=LONG_MA):
    """结合RSI和ADX改进均线策略"""
    df["short_ma"] = df["close"].rolling(short_ma).mean()
    df["long_ma"] = df["close"].rolling(long_ma).mean()

    add_rsi_adx_index(df)

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
    position, entry_price = calculate_position_numba(close, signal)
    df["position"] = position
    df["entry_price"] = entry_price

    df["daily_return"] = df["position"].shift(1) * df["close"].pct_change()
    # df.dropna(subset=["signal", "position"], inplace=True)

    return df.sort_index(ascending=True)


def analyze_stocks(is_test=False, end_date=DEFAULT_END_DATE, add_his_rec=False):
    """分析股票并生成交易信号"""
    tickers = get_stock_list()

    recommendations = []
    signal_columns = [
        "imp_date",
        "ticker",
        "action",
        "price",
        "short_ma",
        "long_ma",
        "rsi",
        "adx",
        "volatility",
        "sharpe_ratio",
        "max_drawdown",
    ]

    for ticker in tqdm(tickers, desc="分析股票"):
        df = load_stocks_data(ticker, end_date)
        if df.empty or len(df) < LONG_MA:
            continue

        original_max_df_date = df.index[-1]
        print(original_max_df_date)

        df = refined_strategy(df)

        if df.empty:
            print(f"{ticker}没有满足策略条件的数据，跳过")
            continue

        print(
            df[
                [
                    "close",
                    "short_ma",
                    "long_ma",
                    "signal",
                    "position",
                    "daily_return",
                ]
            ].tail()
        )

        max_df_date = df.index[-1]
        add_his_rec = add_his_rec or max_df_date == pd.to_datetime(end_date)

        if abs(df.iloc[-1]["signal"]) >= 0 and add_his_rec:  # 买入/卖出信号
            volatility = df["daily_return"].std() * np.sqrt(252)
            sharpe_ratio = calculate_sharpe_ratio(df)
            max_drawdown = calculate_max_drawdown(df)
            mean_volume = df["volume"].mean()

            if mean_volume >= 100000:
                action = "buy" if (df.iloc[-1]["signal"] == 1) else "sell"

                recommendations.append(
                    {
                        "imp_date": max_df_date,
                        "ticker": ticker,
                        "action": action,
                        "price": df.iloc[-1]["close"],
                        "short_ma": df.iloc[-1]["short_ma"],
                        "long_ma": df.iloc[-1]["long_ma"],
                        "rsi": df.iloc[-1]["rsi"],
                        "adx": df.iloc[-1]["adx"],
                        "volatility": volatility,
                        "sharpe_ratio": sharpe_ratio,
                        "max_drawdown": max_drawdown,
                    }
                )

    recommendations.sort(
        key=lambda element: (
            element["imp_date"],
            element["action"],
            element["sharpe_ratio"],
        ),
        reverse=True,
    )
    if not is_test:
        save_signals(recommendations, signal_columns)
    return recommendations
