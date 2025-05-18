from src.conf import DEFAULT_END_DATE, STRATEGY_NAME
from src.io.read_data import load_stocks_data
from src.io.init_data import init_stock_list
from src.strategy.analyze import analyze_stocks, get_specific_strategy


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

        df = get_specific_strategy(df, STRATEGY_NAME)

        print("=== 策略计算基本列结果 ===")
        print(df.tail(10))
        print(f"\nsignal统计结果: {df['signal'].value_counts().to_dict()}\n")

    elif test_mode == "batch":
        init_stock_list()

        analyze_stocks(is_test=True, end_date=end_date, add_his_rec=add_his_rec)


if __name__ == "__main__":

    """
    If we buy "KROS" on 2025-04-14, based on the data of 2025-04-11 and original strategy,
    and will gain about 12.5% profit in a week.
    Therefore, we need to test the strategy on this stock with the date of 2025-04-11 by:

    1. batch test:
    test_strategy(test_mode="batch", end_date="2025-04-11", add_his_rec=True)
    and "KROS" should be in the recommendation list or in the near former recommendation list.

    2. single test:
    test_strategy(test_mode="single", ticker="KROS", end_date="2025-04-11")
    and the result should be similar to the one in batch test.

    Similar situation happens to "BCAX" on 2025-04-18:
    test_strategy(test_mode="batch", end_date="2025-04-18", add_his_rec=True)
    test_strategy(test_mode="single", ticker="BCAX", end_date="2025-04-18")
    """

    # necessary test
    # test_strategy(test_mode="batch", end_date="2025-04-11", add_his_rec=True)
    test_strategy(test_mode="single", ticker="KROS", end_date="2025-04-11")
    # test_strategy(test_mode="batch", end_date="2025-04-18", add_his_rec=True)
    test_strategy(test_mode="single", ticker="BCAX", end_date="2025-04-18")

    # other test
    # test_strategy(test_mode="batch")
