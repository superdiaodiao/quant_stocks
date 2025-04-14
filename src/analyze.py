from datetime import date

import numpy as np
import talib
from tqdm import tqdm

from src.conf import SHORT_MA, LONG_MA
from src.read_data import load_stocks_data, get_stock_list
from src.save_files import save_signals


def add_rsi_adx_index(df):
    """添加技术指标到DataFrame"""
    df["rsi"] = talib.RSI(df["close"], timeperiod=14)  # RSI指标
    df["adx"] = talib.ADX(df["high"], df["low"], df["close"], timeperiod=14)  # ADX平均趋向指数
    return df


def refined_strategy(df):
    """结合RSI和ADX改进均线策略"""
    df["short_ma"] = df["close"].rolling(SHORT_MA).mean()
    df["long_ma"] = df["close"].rolling(LONG_MA).mean()

    add_rsi_adx_index(df)

    df["signal"] = np.where(
        (df["short_ma"] > df["long_ma"]) & (df["rsi"] < 70) & (df["adx"] > 20), 1,  # 买入信号
        np.where((df["short_ma"] < df["long_ma"]) & (df["rsi"] > 30), 0, np.nan)  # 卖出信号
    )
    df["position"] = df["signal"].diff().fillna(0)
    return df


def analyze_stocks():
    """分析股票并生成交易信号"""
    tickers = get_stock_list()

    recommendations = []

    for ticker in tqdm(tickers, desc="分析股票"):
        df = load_stocks_data(ticker)
        if df.empty or len(df) < LONG_MA:
            continue

        end_date = df.index[-1]
        df = refined_strategy(df)

        if df.iloc[-1]["position"] == 1 or df.iloc[-1]["position"] == -1:  # 买入/卖出信号
            print(df[["close", "short_ma", "long_ma", "signal", "position"]].tail())
            daily_returns = df["close"].pct_change().dropna()
            volatility = daily_returns.std() * np.sqrt(252)
            sharpe_ratio = round(daily_returns.mean() / daily_returns.std() * np.sqrt(252), 4)

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
            })

    recommendations.sort(key=lambda element: (element['imp_date'], element['action'], element['sharpe_ratio']),
                         reverse=True)

    save_signals(recommendations)
    return recommendations
