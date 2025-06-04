import pandas as pd
from tqdm import tqdm

from financial.pe import calculate_pe, get_sorted_eps_data
from src.conf import (
    DEFAULT_END_DATE,
    LONG_MA,
    SHORT_MA,
    STRATEGY_NAME,
    VOLUMN_THREDHOLD,
)
from src.io.read_data import get_vix_data, load_stocks_data, get_stock_list
from src.io.save_files import save_signals
from src.strategy.common import (
    calculate_bollinger_bands,
    calculate_kdj,
    calculate_moving_average,
    calculate_week_avg_volume,
)
from src.strategy.dow_theory.gen_strategy import dow_theory_strategy
from src.strategy.ma.gen_fix_strategy import fixed_ma_strategy
from src.strategy.ma.gen_strategy import ma_strategy


def get_specific_strategy(
    df, short_ma=SHORT_MA, long_ma=LONG_MA, strategy=STRATEGY_NAME
):
    if strategy == "ma":
        return ma_strategy(df, short_ma=short_ma, long_ma=long_ma)
    elif strategy == "fixed_ma":
        return fixed_ma_strategy(df, short_ma=short_ma, long_ma=long_ma)
    elif strategy == "dow_theory":
        # 前两个ma策略的计算已经在strategy中，dow_theory_strategy没有，所以这里要计算，方便最后的信号中也携带
        df = calculate_moving_average(df, short_ma=short_ma, long_ma=long_ma)

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
        "k",
        "d",
        "j",
    ]

    vix_df = get_vix_data()
    vix_df.index = pd.to_datetime(vix_df.index)

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
        df = get_specific_strategy(df, SHORT_MA, LONG_MA, STRATEGY_NAME)
        calculate_kdj(df)
        calculate_bollinger_bands(df)
        calculate_pe(df, get_sorted_eps_data())

        if df.empty:
            print(f"{ticker}没有满足策略条件的数据，跳过")
            continue

        df = pd.merge(
            left=df,
            right=vix_df,
            how="left",  # 左连接：以 df 为主
            left_index=True,  # 使用左表的索引作为连接键
            right_index=True,  # 使用右表的索引作为连接键
        )

        current_vix_close = df.iloc[-1]["vix_close"]
        if current_vix_close >= 25:  # 高
            dynamic_buy_combinations = [0, 2, 3]
        elif current_vix_close >= 15 and current_vix_close < 25:  # 中
            dynamic_buy_combinations = [0, 1]
        else:  # 低
            dynamic_buy_combinations = [1, 2]

        dynamic_sell_combinations = [0, 1]

        buy_conditions = [
            df.iloc[-1]["bollinger_buy_signal"] == 1.0,
            df.iloc[-1]["kdj_buy_signal"] == 1.0,
            df.iloc[-1]["signal"] == 1.0,
            df.iloc[-1]["pe"] <= 100,
        ]
        selected_buy_conditions = [buy_conditions[i] for i in dynamic_buy_combinations]
        sell_conditions = [
            df.iloc[-1]["bollinger_sell_signal"] == 1.0,
            df.iloc[-1]["kdj_sell_signal"] == 1.0,
        ]
        selected_sell_conditions = [
            sell_conditions[i] for i in dynamic_sell_combinations
        ]

        print(df.tail())

        max_df_date = df.index[-1]

        if any(selected_buy_conditions) or any(
            selected_sell_conditions
        ):  # 买入/卖出趋势信号，还需要结合其他指标进行判断

            # 如果不需要历史记录，则只保留end_date的记录
            if not add_his_rec:
                df = df[df.index == pd.to_datetime(end_date)]
                if df.empty:
                    print(f"{ticker}没有满足策略条件的数据，跳过")
                    continue

            if any(selected_buy_conditions):
                action = "buy"
            elif any(selected_sell_conditions):
                action = "sell"
            else:
                action = "hold"
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
                    "k": df.iloc[-1]["k"],
                    "d": df.iloc[-1]["d"],
                    "j": df.iloc[-1]["j"],
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
