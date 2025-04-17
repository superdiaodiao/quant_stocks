from datetime import date

import numpy as np
import talib
from tqdm import tqdm

from src.conf import SHORT_MA, LONG_MA
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

    df["position"] = np.where(
        (df["signal"] == 1) & (df["signal"].shift(1) != 1), 1,  # 产生买入信号
        np.where((df["signal"] == 0) & (df["signal"].shift(1) != 0), -1, 0)  # 产生卖出信号，否则无变化
    )

    df["daily_return"] = df["position"] * df["close"].pct_change()
    df.dropna(inplace=True)

    return df


def analyze_stocks():
    """分析股票并生成交易信号"""
    tickers = get_stock_list()

    recommendations = []
    signal_columns = ["imp_date", "ticker", "action", "price", "short_ma", "long_ma", "rsi", "adx", "volatility",
                      "sharpe_ratio", "max_drawdown"]

    for ticker in tqdm(tickers, desc="分析股票"):
        df = load_stocks_data(ticker)
        if df.empty or len(df) < LONG_MA:
            continue

        end_date = df.index[-1]
        df = refined_strategy(df)

        if df.empty:
            continue

        if abs(df.iloc[-1]["position"]) == 1:  # 买入/卖出信号
            print(df[["close", "short_ma", "long_ma", "signal", "position"]].tail())
            volatility = df["daily_return"].std() * np.sqrt(252)
            sharpe_ratio = calculate_sharpe_ratio(df)
            max_drawdown = calculate_max_drawdown(df)

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

    save_signals(recommendations, signal_columns)
    return recommendations
