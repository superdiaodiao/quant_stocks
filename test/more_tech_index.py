import numpy as np
import talib
from tqdm import tqdm

from read_data import get_stock_list, load_stocks_data


def add_technical_indicators(df):
    """添加技术指标到DataFrame"""
    df["rsi"] = talib.RSI(df["close"], timeperiod=14)  # RSI指标
    df["adx"] = talib.ADX(df["high"], df["low"], df["close"], timeperiod=14)  # ADX平均趋向指数
    return df

def refined_strategy(df, short_ma, long_ma):
    """结合RSI和ADX改进均线策略"""
    df["short_ma"] = df["close"].rolling(short_ma).mean()
    df["long_ma"] = df["close"].rolling(long_ma).mean()
    df["signal"] = np.where(
        (df["short_ma"] > df["long_ma"]) & (df["rsi"] < 70) & (df["adx"] > 20), 1,  # 买入信号
        np.where((df["short_ma"] < df["long_ma"]) & (df["rsi"] > 30), 0, np.nan)  # 卖出信号
    )
    df["position"] = df["signal"].ffill().fillna(0)
    return df


# tickers = get_stock_list()
tickers = ['NYAX']

recommendations = []

for ticker in tqdm(tickers, desc="分析股票"):
    df = load_stocks_data(ticker)
    df = add_technical_indicators(df)
    df = refined_strategy(df, short_ma=5, long_ma=20)
    # 查看信号和仓位
    print(df[["close", "short_ma", "long_ma", "rsi", "adx", "signal", "position"]].tail())

import matplotlib.pyplot as plt

def plot_strategy_performance(df):
    """绘制策略表现"""
    df["portfolio"] = (1 + df["position"] * df["close"].pct_change()).cumprod()
    plt.figure(figsize=(14, 7))
    plt.plot(df["close"], label="Price")
    plt.plot(df["short_ma"], label="Short MA")
    plt.plot(df["long_ma"], label="Long MA")
    plt.plot(df["portfolio"], label="Portfolio Value", linestyle="--")
    plt.legend()
    plt.show()