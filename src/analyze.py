from datetime import date

import numpy as np
import pandas as pd
import talib
from tqdm import tqdm

from src.conf import DEFAULT_END_DATE, SHORT_MA, LONG_MA
from src.read_data import load_stocks_data, get_stock_list
from src.save_files import save_signals


def calculate_max_drawdown(df):
    """计算最大回撤"""
    if len(df) == 0 or df["daily_return"].isna().all():
        return None  # 返回 None 表示无法计算
    cumulative_returns = (1 + df["daily_return"]).cumprod()  # 从每日收益率生成累计收益
    peak = cumulative_returns.expanding(min_periods=1).max()
    drawdown = cumulative_returns - peak
    return drawdown.min()


def calculate_sharpe_ratio(df):
    return round(df["daily_return"].mean() / (df["daily_return"].std() + 1e-8) * np.sqrt(252), 4)

def add_rsi_adx_index(df):
    """添加技术指标到DataFrame"""
    df["rsi"] = talib.RSI(df["close"], timeperiod=14)  # RSI指标
    df["adx"] = talib.ADX(df["high"], df["low"], df["close"], timeperiod=14)  # ADX平均趋向指数
    return df


def refined_strategy(df, short_ma = SHORT_MA, long_ma = LONG_MA):
    """结合RSI和ADX改进均线策略"""
    df["short_ma"] = df["close"].rolling(short_ma).mean()
    df["long_ma"] = df["close"].rolling(long_ma).mean()

    add_rsi_adx_index(df)

    df.dropna(subset=["short_ma", "long_ma", "rsi", "adx"], inplace=True)

    df["signal"] = np.where(
        (df["short_ma"] > df["long_ma"]) & (df["rsi"] < 70) & (df["adx"] > 20), 1,  # 买入信号
        np.where(
            (df["short_ma"] < df["long_ma"]) & (df["rsi"] > 70) & (df["adx"] < 25), 0,  # 卖出信号
            np.nan  # 无信号
        )
    )

    # 初始化字段
    df["holding"] = 0  # 是否持仓，1表示持仓，0表示空仓
    df["entry_price"] = np.nan  # 持仓的买入价格
    df["position"] = 0  # 持仓状态：1表示买入，-1表示卖出，0表示无操作
    df["daily_return"] = 0  # 每日收益率

    # 循环计算策略逻辑
    for i in range(1, len(df)):  # 从第1天开始（0索引是不可用的）
        # 累计收益率公式：当前价格涨幅 = (当前价格 - 买入价格) / 买入价格
        if df.iloc[i - 1]["holding"] == 1:  # 如果上一天有持仓
            current_return = (df.iloc[i]["close"] - df.iloc[i - 1]["entry_price"]) / df.iloc[i - 1]["entry_price"]

            if current_return >= 0.10:
                df.at[df.index[i], "position"] = -1  # 卖出
                df.at[df.index[i], "holding"] = 0    # 清仓后不再持仓
                df.at[df.index[i], "entry_price"] = np.nan  # 清空买入价格记录

            elif df.iloc[i]["signal"] == 0 and current_return > 0:
                df.at[df.index[i], "position"] = -1
                df.at[df.index[i], "holding"] = 0
                df.at[df.index[i], "entry_price"] = np.nan

            else:
                df.at[df.index[i], "position"] = 0  # 无操作

        # 开仓逻辑
        elif df.iloc[i - 1]["holding"] == 0:  # 如果上一天未持仓
            if df.iloc[i - 1]["signal"] == 1:  # 上一天出现买入信号
                df.at[df.index[i], "position"] = 1  # 开仓买入
                df.at[df.index[i], "holding"] = 1  # 更新状态为持仓
                df.at[df.index[i], "entry_price"] = df.iloc[i]["close"]  # 记录当日买入价格
            else:
                df.at[df.index[i], "position"] = 0  # 未触发买入信号，无操作

    df["daily_return"] = df["position"] * df["close"].pct_change()
    df.dropna(inplace=True)

    return df


def analyze_stocks(is_test=False, end_date=DEFAULT_END_DATE, add_his_rec=False):
    """分析股票并生成交易信号"""
    tickers = get_stock_list()

    recommendations = []
    signal_columns = ["imp_date", "ticker", "action", "price", "short_ma", "long_ma", "rsi", "adx", "volatility",
                      "sharpe_ratio", "max_drawdown"]

    for ticker in tqdm(tickers, desc="分析股票"):
        df = load_stocks_data(ticker, end_date)
        if df.empty or len(df) < LONG_MA:
            continue

        df = refined_strategy(df)

        if df.empty:
            continue

        print(df[["close", "short_ma", "long_ma", "signal", "position", "holding", "daily_return"]].tail())

        add_his_rec = add_his_rec or df.index[-1] == pd.to_datetime(end_date)

        if abs(df.iloc[-1]["position"]) == 1 and add_his_rec:  # 买入/卖出信号
            volatility = df["daily_return"].std() * np.sqrt(252)
            sharpe_ratio = calculate_sharpe_ratio(df)
            max_drawdown = calculate_max_drawdown(df)
            mean_volume = df["volume"].mean()

            if sharpe_ratio >= 1.5 and max_drawdown >= -0.10 and mean_volume >= 100000:
                action = "buy" if (df.iloc[-1]["position"] == 1) else "sell"

                recommendations.append({
                    "imp_date": end_date,
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
                })

    recommendations.sort(key=lambda element: (element['imp_date'], element['action'], element['sharpe_ratio']),
                         reverse=True)
    if not is_test:
        save_signals(recommendations, signal_columns)
    return recommendations
