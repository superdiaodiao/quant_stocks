from datetime import datetime
import backtrader as bt
import pandas as pd
from multiprocessing import Pool
from itertools import product

from financial.pe import calculate_pe, get_sorted_eps_data
from src.conf import (
    DEFAULT_END_DATE,
    LONG_MA,
    NASDAQ_300M_STOCK_LIST_FILE,
    NASDAQ_GLOBAL_SELECT_10B_STOCK_LIST_FILE,
    SHORT_MA,
    STRATEGY_NAME,
)
from src.io.read_data import get_stock_list, get_vix_data, load_stocks_data
from src.strategy.analyze import get_specific_strategy
from src.strategy.common import (
    calculate_bollinger_bands,
    calculate_donchian_channel,
    calculate_kdj,
    calculate_keltner_channel,
    calculate_macd,
)
from bt_test_utils import BacktraderStrategy, CustomPandasData


def backtrader_test_strategy(params):
    """
    使用 Backtrader 进行策略回测
    """
    year, bollinger_window, bollinger_num_std, strategy_name = params

    # stock_list = ["AAPL"]
    stock_list = get_stock_list(file_path=NASDAQ_300M_STOCK_LIST_FILE)
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"
    print(
        f"Testing {strategy_name} strategy with {bollinger_window} and {bollinger_num_std} from {start_date} to {end_date}"
    )
    total_cnt = len(stock_list)
    earning_cnt = 0
    earning_rate = 0.0
    result = {}

    # 这几个指标计算耗时过长, 批量测试时候最好不要加，单个股票可以加
    # max_drawdown_list = []
    # sharperatio_list = []
    # won_rate_list = []

    result["start_date"] = start_date
    result["bollinger_window"] = bollinger_window
    result["bollinger_num_std"] = bollinger_num_std
    result["strategy_name"] = strategy_name

    vix_df = get_vix_data()
    vix_df.index = pd.to_datetime(vix_df.index)

    for ticker in stock_list:
        print(f"正在回测: {ticker}")

        df = load_stocks_data(ticker, end_date=end_date)
        if df.empty:
            print(f"数据不存在: {ticker}")
            total_cnt -= 1
            continue
        elif len(df) < 14:
            print(f"{ticker} 数据量不足，跳过回测")
            total_cnt -= 1
            continue

        df = get_specific_strategy(
            df, SHORT_MA, LONG_MA, strategy_name
        )  # 使用 Dow Theory 策略
        # df.rename(columns={"50d_ma": "ma_50"}, inplace=True)
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

        cerebro = bt.Cerebro()  # 初始化回测系统
        data = CustomPandasData(
            dataname=df,  # type: ignore
            fromdate=datetime.strptime(start_date, "%Y-%m-%d"),  # type: ignore
            todate=datetime.strptime(end_date, "%Y-%m-%d"),  # type: ignore
        )

        cerebro.adddata(data)  # 将数据传入回测系统
        cerebro.addstrategy(BacktraderStrategy)  # 将交易策略加载到回测系统中
        start_cash = 1000000
        cerebro.broker.setcash(start_cash)
        cerebro.broker.setcommission(commission=0.0002)  # 设置交易手续费为 0.02%

        # # 回测后添加分析模块
        # cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="ta")
        # cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
        # cerebro.addanalyzer(
        #     bt.analyzers.SharpeRatio, _name="sp", riskfreerate=0.03
        # )  # 夏普比率

        cerebro_results = cerebro.run()
        strat = cerebro_results[0]

        port_value = cerebro.broker.getvalue()  # 获取回测结束后的总资金
        pnl = port_value - start_cash  # 盈亏统计
        return_rate = pnl / start_cash  # 计算收益率

        # # 统计单只股票交易盈利次数比例
        # ta_analysis = strat.analyzers.ta.get_analysis()
        # total_trades = ta_analysis.get("total", {}).get("total", 0)
        # won_trades = ta_analysis.get("won", {}).get("total", 0)
        # won_rate = won_trades / total_trades if total_trades > 0 else 0.0
        # won_rate_list.append(won_rate)

        # # 统计最大回撤和夏普比率
        # max_drawdown = strat.analyzers.dd.get_analysis()["max"]["drawdown"]
        # max_drawdown_list.append(max_drawdown)
        # sharperatio = strat.analyzers.sp.get_analysis()["sharperatio"]
        # sharperatio_list.append(sharperatio)

        # print(strat.analyzers.ta.get_analysis())  # 输出详细交易统计

        print(f"初始资金: {start_cash}\n回测期间：{start_date}:{end_date}")
        print(f"最终资金: {round(port_value, 2)}")
        # print(f"总交易次数: {total_trades}")
        # print(f"盈利交易次数: {won_trades}")
        # print(f"盈利比: {round(won_rate * 100, 2)}%")
        print(f"净收益: {round(pnl, 2)}")
        print(f"收益率: {round(return_rate * 100, 2)}%")
        # print(f"最大回撤: {round(max_drawdown, 2)}%")
        # if sharperatio is None:
        #     print("夏普比率: 无法计算")
        # else:
        #     print(f"夏普比率: {round(sharperatio, 2)}")
        print(f"回测结果: {ticker} 从 {start_date} 到 {end_date} 的回测完成。\n")

        if return_rate:
            earning_rate += return_rate
            if return_rate > 0 and earning_cnt:
                earning_cnt += 1
            elif return_rate == 0:
                total_cnt -= 1

    total_earning_rate = round(earning_cnt / total_cnt * 100, 2)
    average_earning_rate = round(earning_rate / total_cnt * 100, 2)
    # avg_max_drawdown = round(sum(max_drawdown_list) / len(max_drawdown_list), 2)
    # avg_sharperatio = round(sum(sharperatio_list) / len(sharperatio_list), 2)
    # avg_won_rate = round(sum(won_rate_list) / len(won_rate_list) * 100, 2)

    print(f"Total stocks tested: {total_cnt}, Earning stocks: {earning_cnt}")
    print(f"Total earning rate: {total_earning_rate}%")
    print(f"Average earning rate: {average_earning_rate}%")
    # print(f"Average max drawdown: {avg_max_drawdown}%")
    # print(f"Average Sharpe Ratio: {avg_sharperatio}")
    # print(f"Average won rate: {avg_won_rate}%")
    print("All tests completed.")

    result["total_stocks"] = total_cnt
    result["earning_stocks"] = earning_cnt
    result["total_earning_rate"] = total_earning_rate
    result["average_earning_rate"] = average_earning_rate
    # result["avg_max_drawdown"] = avg_max_drawdown
    # result["avg_sharperatio"] = avg_sharperatio

    result_df = pd.DataFrame([result])
    result_df.to_csv(
        "test/test_data/300M_bt_test_results_every_stock.csv",
        index=False,
        header=False,
        mode="a",
    )


if __name__ == "__main__":

    # backtrader test
    # stock_list = ["aapl"]
    # backtrader_test_strategy(
    #     stock_list, "2022-01-01", "2022-12-31", 50, 3, "dow_theory"
    # )
    year_list = [str(i) for i in range(2020, 2026)]
    bollinger_window_list = [20, 30, 50]
    bollinger_num_std_list = [0, 1, 2, 3]
    strategy_name_list = ["dow_theory"]

    param_combinations = list(
        product(
            year_list, bollinger_window_list, bollinger_num_std_list, strategy_name_list
        )
    )

    # 使用 Pool 进行并行处理
    with Pool(processes=16) as pool:  # 选择适当的进程数，例如 4
        pool.map(backtrader_test_strategy, param_combinations)
