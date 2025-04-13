from datetime import date

import numpy as np
from tqdm import tqdm

from src.conf import SHORT_MA, LONG_MA
from src.read_data import load_stocks_data, get_stock_list
from src.save_files import save_signals


def analyze_stocks():
    """分析股票并生成交易信号"""
    tickers = get_stock_list()

    recommendations = []

    for ticker in tqdm(tickers, desc="分析股票"):
        df = load_stocks_data(ticker)
        if df.empty or len(df) < LONG_MA:
            continue

        df["short_ma"] = df["close"].rolling(SHORT_MA).mean()
        df["long_ma"] = df["close"].rolling(LONG_MA).mean()
        df["signal"] = np.where(df["short_ma"] > df["long_ma"], 1, 0)
        df["position"] = df["signal"].diff().fillna(0) #信号变化点

        if df.iloc[-1]["position"] == 1 or df.iloc[-1]["position"] == -1:  # 买入/卖出信号
            print(df[["close", "short_ma", "long_ma", "signal", "position"]].tail())
            daily_returns = df["close"].pct_change().dropna()
            volatility = daily_returns.std() * np.sqrt(252)
            sharpe_ratio = round(daily_returns.mean() / daily_returns.std() * np.sqrt(252), 4)

            action = "buy" if(df.iloc[-1]["position"] == 1) else "sell"

            recommendations.append({
                "imp_date": date.today(),
                "ticker": ticker,
                "action": action,
                "price": df.iloc[-1]["close"],
                "short_ma": df.iloc[-1]["short_ma"],
                "long_ma": df.iloc[-1]["long_ma"],
                "volatility": volatility,
                "sharpe_ratio": sharpe_ratio
            })

    recommendations.sort(key=lambda element: (element['imp_date'], element['action'], element['sharpe_ratio']),
                         reverse=True)

    save_signals(recommendations)
    return recommendations
