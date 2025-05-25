from datetime import datetime
import backtrader as bt
import pandas as pd
from multiprocessing import Pool
from itertools import product

from src.conf import (
    DEFAULT_END_DATE,
    LONG_MA,
    NASDAQ_300M_STOCK_LIST_FILE,
    NASDAQ_GLOBAL_SELECT_10B_STOCK_LIST_FILE,
    SHORT_MA,
    STRATEGY_NAME,
)
from src.io.read_data import get_stock_list, load_stocks_data
from src.io.init_data import init_stock_list
from src.strategy.analyze import analyze_stocks, get_specific_strategy
from src.strategy.common import (
    calculate_bollinger_bands,
    calculate_kdj,
    calculate_macd,
    calculate_week_avg_volume,
)


class BacktraderStrategy(bt.Strategy):
    """
    主策略程序
    """

    params = (("maperiod", 20),)

    def __init__(self):
        """
        初始化函数
        """
        self.data_close = self.datas[0].close
        # 初始化交易指令、买卖价格和手续费
        self.order = None
        self.buy_price = None
        self.buy_comm = None

    def next(self):
        """
        执行逻辑
        """
        # 当前持仓状态
        print(f"当前持仓数量：{self.position.size}, 当前资金：{self.broker.get_cash()}")

        if self.order:  # 检查是否有指令等待执行
            return

        current_signal = self.datas[0].signal[0]  # 获取信号列的值
        print(f"当前信号：{current_signal}，时间：{self.datas[0].datetime.date(0)}")
        # current_rsi = self.datas[0].rsi[0]
        # current_adx = self.datas[0].adx[0]
        print(
            # f"当前 RSI 值：{current_rsi}"
            # f"当前 ADX 值：{current_adx}"
            f"当前收盘价：{self.data_close[0]}"
            f"当前 volume_avg_w_pct 值: {self.datas[0].volume_avg_w_pct[0]}"
            f"当前 volume 值: {self.datas[0].volume[0]}"
            # f"当前 macd_buy_signal 值: {self.datas[0].macd_buy_signal[0]}"
            # f"当前 macd_sell_signal 值: {self.datas[0].macd_sell_signal[0]}"
            f"当前 kdj_buy_signal 值: {self.datas[0].kdj_buy_signal[0]}"
            f"当前 kdj_sell_signal 值: {self.datas[0].kdj_sell_signal[0]}"
        )

        # 执行交易逻辑
        if not self.position:  # 没有持仓时
            if (
                current_signal == 1
                # and current_rsi >= 30
                # and current_adx >= 25
                # and self.datas[0].short_ma[0] > self.datas[0].long_ma[0]
                # and self.data_close[0] > self.datas[0].ma_50[0]
                # and self.datas[0].volume_avg_w_pct[0] > 0  # 周均成交量大于上周
                # and self.datas[0].volume[0] > 100000  # 成交量大于10万
                # and self.datas[0].macd_buy_signal[0] == 1.0  # MACD买入信号
                and self.datas[0].kdj_buy_signal[0] == 1.0  # KDJ买入信号
                and self.datas[0].bollinger_buy_signal[0] == 1.0  # 布林带买入信号
            ):
                buy_size = int(8000 / self.data_close[0])  # 单次交易不超过8000刀
                print(f"买入信号: {self.data_close[0]}, size: {buy_size}")
                self.order = self.buy(size=buy_size)  # 执行买入操作
        else:
            if (
                current_signal == 0
                # and current_rsi <= 70
                # and current_adx <= 20
                # and self.datas[0].short_ma[0] < self.datas[0].long_ma[0]
                # and self.data_close[0] < self.datas[0].ma_50[0]
                # and self.datas[0].macd_sell_signal[0] == 1.0  # MACD卖出信号
                and self.datas[0].kdj_sell_signal[0] == 1.0  # KDJ卖出信号
                and self.datas[0].bollinger_sell_signal[0] == 1.0  # 布林带卖出信号
            ):
                sell_size = self.position.size
                print(f"卖出信号: {self.data_close[0]}, size: {sell_size}")
                self.order = self.sell(size=sell_size)  # 执行卖出操作

    def notify_order(self, order):
        """
        通知订单状态变化
        """
        if order.status in [order.Completed]:
            if order.isbuy():
                self.buy_price = order.executed.price
                self.buy_comm = order.executed.comm
                print(
                    f"买入完成: {order.executed.size} @ {order.executed.price} 手续费: {order.executed.comm}"
                )
            elif order.issell():
                print(
                    f"卖出完成: {order.executed.size} @ {order.executed.price} 手续费: {order.executed.comm}"
                )
            self.order = None  # 清除订单状态
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            print("订单失败")
            self.order = None


class CustomPandasData(bt.feeds.PandasData):
    """
    自定义 Pandas 数据类，扩展以包含新的字段。
    """

    lines = (
        "signal",
        # "rsi",
        # "adx",
        # "ma_50",
        "volume",
        "volume_avg_w_pct",
        # "macd_buy_signal",
        # "macd_sell_signal",
        "kdj_buy_signal",
        "kdj_sell_signal",
        "bollinger_buy_signal",
        "bollinger_sell_signal",
    )

    # 默认情况下，Backtrader 将尝试匹配列到字段。如果列未指定，设置为 -1 (不存在)
    params = (
        ("signal", -1),
        # ("rsi", -1),
        # ("adx", -1),
        # ("ma_50", -1),
        ("volume", -1),
        ("volume_avg_w_pct", -1),
        # ("macd_buy_signal", -1),
        # ("macd_sell_signal", -1),
        ("kdj_buy_signal", -1),
        ("kdj_sell_signal", -1),
        ("bollinger_buy_signal", -1),
        ("bollinger_sell_signal", -1),
    )


def simple_test_strategy(
    test_mode="batch", ticker=None, end_date=DEFAULT_END_DATE, add_his_rec=False
):
    """
    简单测试策略函数
    """
    if test_mode == "single":
        if ticker is None:
            raise ValueError("Ticker must be provided in single test mode.")
        df = load_stocks_data(ticker, end_date=end_date)
        if df.empty:
            print(f"数据不存在: {ticker}")
            return

        print(f"=== {ticker} 的数据预览 ===")
        print(df.tail(10))

        df = get_specific_strategy(df, SHORT_MA, LONG_MA, STRATEGY_NAME)
        calculate_kdj(df)

        print("=== 策略计算基本列结果 ===")
        print(df.tail(10))
        print(f"\nsignal统计结果: {df['signal'].value_counts().to_dict()}\n")

    elif test_mode == "batch":
        init_stock_list()

        analyze_stocks(is_test=True, end_date=end_date, add_his_rec=add_his_rec)


def backtrader_test_strategy(
    stock_list=["AAPL"],
    start_date="2025-01-01",
    end_date=DEFAULT_END_DATE,
    bollinger_window=20,
    bollinger_num_std=0,
    strategy_name=STRATEGY_NAME,
):
    """
    使用 Backtrader 进行策略回测
    """
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
        calculate_week_avg_volume(df)
        # calculate_macd(df)
        calculate_kdj(df)
        calculate_bollinger_bands(
            df, window=bollinger_window, num_std=bollinger_num_std
        )

        cerebro = bt.Cerebro()  # 初始化回测系统
        data = CustomPandasData(
            dataname=df,  # type: ignore
            fromdate=datetime.strptime(start_date, "%Y-%m-%d"),  # type: ignore
            todate=datetime.strptime(end_date, "%Y-%m-%d"),  # type: ignore
        )

        cerebro.adddata(data)  # 将数据传入回测系统
        cerebro.addstrategy(BacktraderStrategy)  # 将交易策略加载到回测系统中
        start_cash = 10000
        cerebro.broker.setcash(start_cash)  # 设置初始资本为 10000
        cerebro.broker.setcommission(commission=0.002)  # 设置交易手续费为 0.2%

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
        "test/test_data/300M_bt_test_results.csv",
        index=False,
        header=False,
        mode="a",
    )


def run_year_backtest(params):
    """用于执行单次回测的函数"""
    year, bollinger_window, bollinger_num_std, strategy_name = params
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"
    print(
        f"Testing {strategy_name} strategy with {bollinger_window} and {bollinger_num_std} from {start_date} to {end_date}"
    )
    backtrader_test_strategy(
        stock_list,
        start_date,
        end_date,
        bollinger_window,
        bollinger_num_std,
        strategy_name,
    )


if __name__ == "__main__":

    """
    If we buy "KROS" on 2025-04-14, based on the data of 2025-04-11 and original strategy,
    and will gain about 12.5% profit in a week.
    Therefore, we need to test the strategy on this stock with the date of 2025-04-11 by:

    1. batch test:
    simple_test_strategy(test_mode="batch", end_date="2025-04-11", add_his_rec=True)
    and "KROS" should be in the recommendation list or in the near former recommendation list.

    2. single test:
    simple_test_strategy(test_mode="single", ticker="KROS", end_date="2025-04-11")
    and the result should be similar to the one in batch test.

    Similar situation happens to "BCAX" on 2025-04-18:
    simple_test_strategy(test_mode="batch", end_date="2025-04-18", add_his_rec=True)
    simple_test_strategy(test_mode="single", ticker="BCAX", end_date="2025-04-18")
    """

    # necessary test
    # simple_test_strategy(test_mode="batch", end_date="2025-04-11", add_his_rec=True)
    simple_test_strategy(test_mode="single", ticker="KROS", end_date="2025-04-11")
    # simple_test_strategy(test_mode="batch", end_date="2025-04-18", add_his_rec=True)
    simple_test_strategy(test_mode="single", ticker="BCAX", end_date="2025-04-18")

    # other test
    # simple_test_strategy(test_mode="batch", end_date=DEFAULT_END_DATE, add_his_rec=True)

    # backtrader test
    stock_list = ["aapl"]
    backtrader_test_strategy(
        stock_list, "2022-01-01", "2022-12-31", 50, 3, "dow_theory"
    )

    # stock_list = get_stock_list(file_path=NASDAQ_300M_STOCK_LIST_FILE)
    # year_list = [str(i) for i in range(2020, 2026)]
    # bollinger_window_list = [20, 30, 50]
    # bollinger_num_std_list = [0, 1, 2, 3]
    # strategy_name_list = ["dow_theory"]

    # param_combinations = list(
    #     product(
    #         year_list, bollinger_window_list, bollinger_num_std_list, strategy_name_list
    #     )
    # )

    # # 使用 Pool 进行并行处理
    # with Pool(processes=16) as pool:  # 选择适当的进程数，例如 4
    #     pool.map(run_year_backtest, param_combinations)
