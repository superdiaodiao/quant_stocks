from datetime import datetime
import sys
import backtrader as bt
import numpy as np
import pandas as pd
from multiprocessing import Pool
from itertools import combinations, product

from src.conf import (
    LONG_MA,
    NASDAQ_300M_STOCK_LIST_FILE,
    NASDAQ_GLOBAL_SELECT_10B_STOCK_LIST_FILE,
    SHORT_MA,
    STRATEGY_NAME,
)
from src.financial.pe import calculate_pe, get_sorted_eps_data
from src.io.read_data import get_stock_list, get_vix_data, load_stocks_data
from src.strategy.analyze import get_specific_strategy
from src.strategy.common import (
    calculate_bollinger_bands,
    calculate_donchian_channel,
    calculate_kdj,
    calculate_keltner_channel,
    calculate_macd,
    calculate_week_avg_volume,
)
from bt_test_utils import BacktraderStrategy, CustomPandasData


def backtrader_multi_stock_strategy(params):
    """
    多股票池回测策略, 返回主要结果
    """
    (
        year,
        bollinger_window,
        bollinger_num_std,
        strategy_name,
        min_conditions,
        max_positions,
        position_size_pct,
        min_trade_interval,
    ) = params

    try:
        stock_list = get_stock_list(file_path=NASDAQ_300M_STOCK_LIST_FILE)
        start_date = f"{year}-01-01"
        end_date = f"{year}-12-31" if year != "2025" else "2025-04-01"
        total_cnt = len(stock_list)
        result = {}

        cerebro = bt.Cerebro()
        vix_df = get_vix_data()
        vix_df.index = pd.to_datetime(vix_df.index)

        for ticker in stock_list:
            df = load_stocks_data(ticker, end_date=end_date)
            if df.empty or len(df) < 14:
                total_cnt -= 1
                continue
            df = get_specific_strategy(df, SHORT_MA, LONG_MA, strategy_name)
            calculate_kdj(df)
            calculate_macd(df)
            calculate_bollinger_bands(
                df, window=bollinger_window, num_std=bollinger_num_std
            )
            calculate_pe(df, get_sorted_eps_data())
            df = pd.merge(
                left=df, right=vix_df, how="left", left_index=True, right_index=True
            )
            data = CustomPandasData(
                dataname=df,
                fromdate=datetime.strptime(start_date, "%Y-%m-%d"),
                todate=datetime.strptime(end_date, "%Y-%m-%d"),
            )
            cerebro.adddata(data, name=ticker)

        # 传递参数
        cerebro.addstrategy(
            BacktraderStrategy,
            min_conditions=min_conditions,
            max_positions=max_positions,
            position_size_pct=position_size_pct,
            min_trade_interval=min_trade_interval,
        )

        start_cash = 1000000
        cerebro.broker.setcash(start_cash)
        cerebro.broker.setcommission(commission=0.0003)
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="ta")
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")

        cerebro_results = cerebro.run()
        strat = cerebro_results[0]
        final_portfolio_value = cerebro.broker.getvalue()
        return_rate = (final_portfolio_value - start_cash) / start_cash

        ta_analysis = strat.analyzers.ta.get_analysis()
        total_trades = ta_analysis.get("total", {}).get("total", 0)
        sell_trades = ta_analysis.get("total", {}).get("closed", 0)
        won_trades = ta_analysis.get("won", {}).get("total", 0)
        won_rate = won_trades / total_trades if total_trades > 0 else 0.0
        max_drawdown = strat.analyzers.dd.get_analysis()["max"]["drawdown"]

        result = {
            "year": year,
            "bollinger_window": bollinger_window,
            "bollinger_num_std": bollinger_num_std,
            "strategy_name": strategy_name,
            "min_conditions": min_conditions,
            "max_positions": max_positions,
            "position_size_pct": position_size_pct,
            "min_trade_interval": min_trade_interval,
            "total_cnt": total_cnt,
            "total_trades": total_trades,
            "sell_trades": sell_trades,
            "won_rate": round(won_rate, 4),
            "max_drawdown": round(max_drawdown / 100, 4),
            "return_rate": round(return_rate, 4),
        }

        # 保存每组参数结果
        result_df = pd.DataFrame([result])
        result_df.to_csv(
            "test/test_data/300M_bt_grid_search_results.csv",
            index=False,
            header=False,
            mode="a",
            float_format="%.4f",
        )
    except Exception as e:
        print(f"参数: {params} 发生异常: {e}")


if __name__ == "__main__":
    # 定义参数空间
    year_list = ["2021", "2023"]
    bollinger_window_list = [20, 50]
    bollinger_num_std_list = [1, 3]
    strategy_name_list = ["dow_theory"]
    min_conditions_list = [2, 3, 4]
    max_positions_list = [5, 10, 15]
    position_size_pct_list = [0.05, 0.1, 0.2]
    min_trade_interval_list = [3, 5, 10]

    param_combinations = list(
        product(
            year_list,
            bollinger_window_list,
            bollinger_num_std_list,
            strategy_name_list,
            min_conditions_list,
            max_positions_list,
            position_size_pct_list,
            min_trade_interval_list,
        )
    )

    # 多进程运行
    processes = 16
    with Pool(processes=processes) as pool:
        pool.map(backtrader_multi_stock_strategy, param_combinations)

    # 结果分析
    result_csv = "test/test_data/300M_bt_grid_search_results.csv"
    df = pd.read_csv(result_csv, header=None)
    df.columns = [
        "year",
        "bollinger_window",
        "bollinger_num_std",
        "strategy_name",
        "min_conditions",
        "max_positions",
        "position_size_pct",
        "min_trade_interval",
        "total_cnt",
        "total_trades",
        "sell_trades",
        "won_rate",
        "max_drawdown",
        "return_rate",
    ]
    # 筛选最佳参数（如：最大收益且回撤小于某阈值）
    df["max_drawdown"] = pd.to_numeric(df["max_drawdown"], errors="coerce")
    best = (
        df[df["max_drawdown"] < 0.15]
        .sort_values("return_rate", ascending=False)
        .groupby(["year"])
        .head(10)
    )
    print("最佳参数Top10：")
    print(best)
    best.to_csv("test/test_data/300M_bt_grid_search_best.csv", index=False)
