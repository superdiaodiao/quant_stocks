import numpy as np
import pandas as pd
from src.strategy.ma.gen_strategy import ma_strategy
from tqdm import tqdm

from src.conf import DEFAULT_END_DATE, LONG_MA, STRATEGY_NAME
from src.io.read_data import load_stocks_data, get_stock_list
from src.io.save_files import save_signals
from strategy.dow_theory.gen_strategy import dow_theory_strategy
from strategy.ma.gen_fix_strategy import fixed_ma_strategy


def get_specific_strategy(df, strategy):
    if strategy == "ma":
        return ma_strategy(df)
    elif strategy == "fixed_ma":
        return fixed_ma_strategy(df)
    elif strategy == "dow_theory":
        return dow_theory_strategy(df)
    else:
        return df


def analyze_stocks(is_test=False, end_date=DEFAULT_END_DATE, add_his_rec=False):
    """分析股票并生成交易信号"""
    tickers = get_stock_list()

    recommendations = []
    signal_columns = [
        "imp_date",
        "ticker",
        "action",
        "price",
        "rsi",
        "adx",
    ]

    for ticker in tqdm(tickers, desc="分析股票"):
        df = load_stocks_data(ticker, end_date)
        if df.empty or len(df) < LONG_MA:
            continue

        original_max_df_date = df.index[-1]
        print(original_max_df_date)

        df = get_specific_strategy(df, STRATEGY_NAME)

        if df.empty:
            print(f"{ticker}没有满足策略条件的数据，跳过")
            continue

        print(df.tail())

        max_df_date = df.index[-1]
        add_his_rec = add_his_rec or max_df_date == pd.to_datetime(end_date)

        if abs(df.iloc[-1]["signal"]) >= 0 and add_his_rec:  # 买入/卖出信号
            mean_volume = df["volume"].mean()

            if mean_volume >= 100000:
                action = "buy" if (df.iloc[-1]["signal"] == 1) else "sell"

                recommendations.append(
                    {
                        "imp_date": max_df_date,
                        "ticker": ticker,
                        "action": action,
                        "price": df.iloc[-1]["close"],
                        "rsi": df.iloc[-1]["rsi"],
                        "adx": df.iloc[-1]["adx"],
                    }
                )

    recommendations.sort(
        key=lambda element: (
            element["imp_date"],
            element["action"],
            element["rsi"],
        ),
        reverse=True,
    )

    if not is_test:
        save_signals(recommendations, signal_columns)
    else:
        if recommendations:
            print("\n=== 推荐的交易信号 ===")
            for rec in recommendations:
                print(rec)
        else:
            print("没有推荐的交易信号。")

    return recommendations
