import numpy as np
import pandas as pd
from src.strategy.common import calculate_max_drawdown, calculate_sharpe_ratio
from src.strategy.ma.gen_strategy import ma_strategy
from tqdm import tqdm

from src.conf import DEFAULT_END_DATE, LONG_MA
from src.io.read_data import load_stocks_data, get_stock_list
from src.io.save_files import save_signals


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

        df = ma_strategy(df)

        if df.empty:
            print(f"{ticker}没有满足策略条件的数据，跳过")
            continue

        print(df.tail())

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
