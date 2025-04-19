from itertools import product

import numpy as np
import pandas as pd
from tqdm import tqdm

from analyze import refined_strategy, calculate_max_drawdown, calculate_sharpe_ratio
from src.read_data import load_stocks_data, get_stock_list


# 优化SHORT_MA和LONG_MA的参数
def optimize_ma_params(start_ma_range, end_ma_range, tickers, metric="sharpe"):
    """
    网格搜索优化SHORT_MA和LONG_MA参数
    :param start_ma_range: 起始MA参数区间 (tuple) (例如 (3, 10))
    :param end_ma_range: 结束MA参数区间 (tuple) (例如 (20, 60))
    :param tickers: 股票列表
    :param metric: 优化目标，默认是'sharpe'，也可以是'return'或'drawdown'
    """
    results = []

    short_ma_range = range(start_ma_range[0], start_ma_range[1] + 1)
    long_ma_range = range(end_ma_range[0], end_ma_range[1] + 1)

    # 遍历不同的SHORT_MA和LONG_MA参数组合
    for short_ma, long_ma in tqdm(
        product(short_ma_range, long_ma_range), desc="优化参数"
    ):
        if short_ma >= long_ma:  # 短期均线必须小于长期均线
            continue

        # 统计不同参数组合的策略表现
        metrics = backtest_strategy(tickers, short_ma, long_ma)
        results.append(
            {
                "short_ma": short_ma,
                "long_ma": long_ma,
                "sharpe": metrics["sharpe_ratio"],
                "annual_return": metrics["annual_return"],
                "max_drawdown": metrics["max_drawdown"],
            }
        )

    # 转换为DataFrame，按metric进行排序
    results_df = pd.DataFrame(results)

    if metric == "sharpe":
        optimal_params = results_df.sort_values(by="sharpe", ascending=False).iloc[0]
    elif metric == "return":
        optimal_params = results_df.sort_values(
            by="annual_return", ascending=False
        ).iloc[0]
    elif metric == "drawdown":
        optimal_params = results_df.sort_values(by="max_drawdown", ascending=True).iloc[
            0
        ]
    else:
        raise ValueError(
            "Unsupported metric, choose from 'sharpe', 'return', or 'drawdown'."
        )

    print("Optimal Parameters:")
    print(f"Short MA: {optimal_params['short_ma']}")
    print(f"Long MA: {optimal_params['long_ma']}")
    print(f"Sharpe Ratio: {optimal_params['sharpe']}")
    print(f"Annual Return: {optimal_params['annual_return']}")
    print(f"Max Drawdown: {optimal_params['max_drawdown']}")
    results_df.to_csv("optimization_results.csv", index=False)
    return optimal_params, results_df


def backtest_strategy(tickers, short_ma, long_ma):
    """
    使用指定MA参数回测策略
    :param tickers: 股票列表
    :param short_ma: 短期MA
    :param long_ma: 长期MA
    :return: 策略表现的几个核心指标
    """
    sharpe_ratios = []
    annual_returns = []
    max_drawdowns = []

    for ticker in tickers:
        df = load_stocks_data(ticker)
        if df.empty or len(df) < long_ma:
            continue

        # 策略逻辑
        df = refined_strategy(df, short_ma, long_ma)

        if df.empty:
            continue

        # 计算指标（确保有效性）
        try:
            sharpe_ratio = calculate_sharpe_ratio(df)
            if np.isnan(sharpe_ratio):
                print(f"The sharpe ratio of {ticker} is nan.")
                continue  # 跳过无效值
        except ZeroDivisionError:
            print(f"Zero Division Error happened to {ticker}.")
            continue  # 避免除以 0 异常

        annual_return = (1 + df["daily_return"].mean()) ** 252 - 1
        max_drawdown = calculate_max_drawdown(df)

        # 仅存储有效指标
        if not np.isnan(sharpe_ratio):
            sharpe_ratios.append(sharpe_ratio)
            annual_returns.append(annual_return)
            max_drawdowns.append(max_drawdown)

    # 确保计算时忽略 NaN 值
    return {
        "sharpe_ratio": np.nanmean(sharpe_ratios),  # 使用 np.nanmean 忽略 NaN
        "annual_return": np.nanmean(annual_returns),
        "max_drawdown": np.nanmin(max_drawdowns),
    }


# 参数范围
short_ma_range = (3, 10)  # 短期均线范围
long_ma_range = (20, 50)  # 长期均线范围

# 股票列表
tickers = get_stock_list()

optimal_params, results_df = optimize_ma_params(short_ma_range, long_ma_range, tickers)

# 查看所有结果
print(results_df.sort_values(by="sharpe", ascending=False))
