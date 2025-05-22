import pandas as pd
from tqdm import tqdm

from src.conf import (
    DEFAULT_END_DATE,
    LONG_MA,
    SHORT_MA,
    STRATEGY_NAME,
    VOLUMN_THREDHOLD,
)
from src.io.read_data import load_stocks_data, get_stock_list
from src.io.save_files import save_signals
from src.strategy.common import calculate_moving_average, calculate_week_avg_volume
from src.strategy.dow_theory.gen_strategy import dow_theory_strategy
from src.strategy.ma.gen_fix_strategy import fixed_ma_strategy
from src.strategy.ma.gen_strategy import ma_strategy


def get_specific_strategy(df, strategy):
    if strategy == "ma":
        return ma_strategy(df)
    elif strategy == "fixed_ma":
        return fixed_ma_strategy(df)
    elif strategy == "dow_theory":
        # 前两个ma策略的计算已经在strategy中，dow_theory_strategy没有，所以这里要计算，方便最后的信号中也携带
        df = calculate_moving_average(df, short_ma=SHORT_MA, long_ma=LONG_MA)

        return dow_theory_strategy(df)
    else:
        raise ValueError(f"未知策略: {strategy}")


# end_date = DEFAULT_END_DATE, 默认结束日期为昨天
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
        "50d_ma",
        "rsi",
        "adx",
        "volume_avg_w",
        "volume_avg_w_pct",
    ]

    for ticker in tqdm(tickers, desc="分析股票"):
        df = load_stocks_data(ticker, end_date)
        if (
            df.empty
            or len(df) < LONG_MA
            or df.iloc[-1].loc["volume"] < VOLUMN_THREDHOLD
            or df["volume"].iloc[-LONG_MA:].mean() < VOLUMN_THREDHOLD
        ):
            continue

        df = calculate_week_avg_volume(df)

        original_max_df_date = df.index[-1]
        print(original_max_df_date)

        df = get_specific_strategy(df, STRATEGY_NAME)

        if df.empty:
            print(f"{ticker}没有满足策略条件的数据，跳过")
            continue

        print(df.tail())

        max_df_date = df.index[-1]

        if abs(df.iloc[-1]["signal"]) >= 0:  # 买入/卖出信号

            # 如果不需要历史记录，则只保留end_date的记录
            if not add_his_rec:
                df = df[df.index == pd.to_datetime(end_date)]
                if df.empty:
                    print(f"{ticker}没有满足策略条件的数据，跳过")
                    continue

            action = "buy" if (df.iloc[-1]["signal"] == 1) else "sell"

            if action == "buy" and (
                df.iloc[-1].loc["close"] < df.iloc[-1].loc["short_ma"]
                or df.iloc[-1].loc["close"] < df.iloc[-1].loc["long_ma"]
            ):
                print(f"{ticker}的价格低于均线, 不加入buy信号")
                continue

            recommendations.append(
                {
                    "imp_date": max_df_date,
                    "ticker": ticker,
                    "action": action,
                    "price": df.iloc[-1]["close"],
                    "short_ma": df.iloc[-1]["short_ma"],
                    "long_ma": df.iloc[-1]["long_ma"],
                    "50d_ma": df.iloc[-1]["50d_ma"],
                    "rsi": df.iloc[-1]["rsi"],
                    "adx": df.iloc[-1]["adx"],
                    "volume_avg_w": df.iloc[-1]["volume_avg_w"],
                    "volume_avg_w_pct": df.iloc[-1]["volume_avg_w_pct"],
                }
            )

    recommendations.sort(
        key=lambda element: (
            element["imp_date"],
            element["action"],
            element["adx"],
            element["rsi"],
            element["volume_avg_w_pct"],
            element["volume_avg_w"],
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
