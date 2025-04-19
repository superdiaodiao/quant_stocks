from conf import DEFAULT_END_DATE
import numpy as np
from src.read_data import load_stocks_data
from src.analyze import analyze_stocks, calculate_max_drawdown, calculate_sharpe_ratio, refined_strategy
from src.init_data import init_stock_list


def test_strategy(
    test_mode="batch", ticker=None, end_date=DEFAULT_END_DATE, add_his_rec=False
):
    if test_mode == "single":
        if ticker is None:
            raise ValueError("Ticker must be provided in single test mode.")
        df = load_stocks_data(ticker, end_date=end_date)
        if df.empty:
            print(f"数据不存在: {ticker}")
            return

        print(f"=== {ticker} 的数据预览 ===")
        print(df.tail(10))

        df = refined_strategy(df)

        print("=== 策略计算基本列结果 ===")
        print(df.tail(10))
        print(
            f"\nsignal统计结果: {df['signal'].value_counts().to_dict()}\n"
        )
        
        print("=== 指标计算结果 ===")
        volatility = df["daily_return"].std() * np.sqrt(252)
        sharpe_ratio = calculate_sharpe_ratio(df)
        max_drawdown = calculate_max_drawdown(df)
        mean_volume = df["volume"].mean()
        print(f"波动率: {volatility:.2f}")
        print(f"夏普比率: {sharpe_ratio:.2f}")
        print(f"最大回撤: {max_drawdown:.2f}")
        print(f"平均成交量: {mean_volume:.2f}")

    elif test_mode == "batch":
        init_stock_list()

        recommendations = analyze_stocks(
            is_test=True, end_date=end_date, add_his_rec=add_his_rec
        )
        print("\n=== 推荐的交易信号 ===")
        for rec in recommendations:
            print(rec)


if __name__ == "__main__":

    # If we buy "KROS" on 2025-04-14, based on the data of 2025-04-11 and original strategy,
    # and will gain about 12.5% profit in a week.
    # Therefore, we need to test the strategy on this stock with the date of 2025-04-11 by:
    #
    # 1. batch test: 
    # test_strategy(test_mode="batch", end_date="2025-04-11", add_his_rec=True)
    # and "KROS" should be in the recommendation list.
    #
    # 2. single test: 
    # test_strategy(test_mode="single", ticker="KROS", end_date="2025-04-11")
    # and the result should be similar to the one in batch test.
    #
    
    test_strategy(test_mode="batch", end_date="2025-04-11", add_his_rec=True)
    test_strategy(test_mode="single", ticker="KROS", end_date="2025-04-11")
