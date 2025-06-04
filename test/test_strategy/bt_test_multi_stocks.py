from datetime import datetime
import sys
import backtrader as bt
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
    多股票池回测策略
    """
    (
        year,
        bollinger_window,
        bollinger_num_std,
        strategy_name,
        # buy_condition_indices,
        # sell_condition_indices,
    ) = params

    # stock_list = ["aapl", "nvda", "tsla"]
    stock_list = get_stock_list(file_path=NASDAQ_300M_STOCK_LIST_FILE)
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"

    total_cnt = len(stock_list)
    result = {}

    cerebro = bt.Cerebro()

    vix_df = get_vix_data()
    vix_df.index = pd.to_datetime(vix_df.index)

    for ticker in stock_list:
        print(f"加载股票数据: {ticker}")

        df = load_stocks_data(ticker, end_date=end_date)
        if df.empty or len(df) < 14:
            print(f"{ticker} 数据量不足，跳过回测")
            total_cnt -= 1
            continue

        df = get_specific_strategy(
            df, SHORT_MA, LONG_MA, strategy_name
        )  # 使用 Dow Theory 策略
        calculate_kdj(df)
        calculate_macd(df)
        calculate_bollinger_bands(
            df, window=bollinger_window, num_std=bollinger_num_std
        )
        calculate_donchian_channel(df, window=bollinger_window)
        calculate_keltner_channel(
            df, atr_window=bollinger_window, multiplier=bollinger_num_std
        )
        calculate_pe(df, get_sorted_eps_data())

        df = pd.merge(
            left=df,
            right=vix_df,
            how="left",  # 左连接：以 df 为主
            left_index=True,  # 使用左表的索引作为连接键
            right_index=True,  # 使用右表的索引作为连接键
        )

        data = CustomPandasData(
            dataname=df,  # type: ignore
            fromdate=datetime.strptime(start_date, "%Y-%m-%d"),  # type: ignore
            todate=datetime.strptime(end_date, "%Y-%m-%d"),  # type: ignore
        )
        cerebro.adddata(data, name=ticker)

    # 将买入/卖出条件组合传递给策略
    cerebro.addstrategy(
        BacktraderStrategy,
        # buy_condition_indices=buy_condition_indices,
        # sell_condition_indices=sell_condition_indices,
    )

    start_cash = 1000000  # 初始资金
    cerebro.broker.setcash(start_cash)
    cerebro.broker.setcommission(commission=0.0002)  # 设置交易手续费为 0.02%
    # 添加分析模块
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="ta")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")

    cerebro_results = cerebro.run()
    strat = cerebro_results[0]

    final_portfolio_value = cerebro.broker.getvalue()
    print(f"初始资金: {start_cash}")
    print(f"回测期间：{start_date} 至 {end_date}")
    print(f"最终资金: {final_portfolio_value}")
    return_rate = (final_portfolio_value - start_cash) / start_cash
    print(f"收益率: { return_rate:.2f}")

    # 总交易次数(open+closed): analysis['total']['total'],
    # 已完成交易数: analysis['total']['closed'],
    # 未完成交易数(只买入未卖出): analysis['total']['open'],
    # 盈利次数: analysis['won']['total'], 亏损次数: analysis['lost']['total']

    ta_analysis = strat.analyzers.ta.get_analysis()
    total_trades = ta_analysis.get("total", {}).get("total", 0)
    sell_trades = ta_analysis.get("total", {}).get("closed", 0)
    won_trades = ta_analysis.get("won", {}).get("total", 0)
    won_rate = won_trades / total_trades if total_trades > 0 else 0.0
    max_drawdown = strat.analyzers.dd.get_analysis()["max"]["drawdown"]

    # 将结果写入 CSV
    result = {
        "start_date": start_date,
        "bollinger_window": bollinger_window,
        "bollinger_num_std": bollinger_num_std,
        "strategy_name": strategy_name,
        # "buy_condition_combination": "-".join(map(str, buy_condition_indices)),
        # "sell_condition_combination": "-".join(map(str, sell_condition_indices)),
        "total_cnt": total_cnt,
        "total_trades": total_trades,
        "sell_trades": sell_trades,
        "won_rate": round(won_rate, 4),
        "max_drawdown": round(max_drawdown / 100, 4),
        "return_rate": round(return_rate, 4),
    }
    result_df = pd.DataFrame([result])
    result_df.to_csv(
        "test/test_data/300M_bt_test_results_multi_stocks.csv",
        index=False,
        header=False,
        mode="a",
        float_format="%.4f",
    )


if __name__ == "__main__":

    # backtrader test
    year_list = [str(i) for i in range(2020, 2026)]
    bollinger_window_list = [20, 30, 50]  # 50效果好
    bollinger_num_std_list = [1, 2, 3]  # 3效果好
    strategy_name_list = ["dow_theory"]

    # 构造所有参数组合
    total_buy_conditions = list(range(1))  # 买入条件总计
    total_sell_conditions = list(range(1))  # 卖出条件总计
    buy_combinations = list(combinations(total_buy_conditions, 1))
    # buy_combinations += list(combinations(total_buy_conditions, 2))
    sell_combinations = list(combinations(total_sell_conditions, 1))  # 0-2组合效果好
    # sell_combinations += list(combinations(total_sell_conditions, 2))

    # 构建参数列表
    param_combinations = list(
        product(
            year_list,
            bollinger_window_list,
            bollinger_num_std_list,
            strategy_name_list,
            # buy_combinations,
            # sell_combinations,
        )
    )

    # 多进程运行
    processes = 16  # 可根据 CPU 核数量调整
    with Pool(processes=processes) as pool:
        pool.map(backtrader_multi_stock_strategy, param_combinations)
