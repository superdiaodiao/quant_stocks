from conf import DEFAULT_END_DATE
from src.read_data import load_stocks_data
from src.analyze import analyze_stocks, refined_strategy
from src.init_data import init_stock_list


def test_strategy(test_mode='batch', ticker=None, end_date=DEFAULT_END_DATE):
    if test_mode == 'single':
        if ticker is None:
            raise ValueError("Ticker must be provided in single test mode.")
        df = load_stocks_data(ticker, end_date=end_date)
        if df.empty:
            print(f"数据不存在: {ticker}")
            return
        df = refined_strategy(df)

        print(df.sort_index().tail(10))

    elif test_mode == 'batch':
        init_stock_list()

        recommendations = analyze_stocks(is_test=True, end_date=end_date, add_his_rec=True)
        print("\n=== 推荐的交易信号 ===")
        for rec in recommendations:
            print(rec)

if __name__ == "__main__":

    # test_strategy(test_mode="batch", end_date="2025-04-16")
    test_strategy(test_mode='single', ticker='AAPL')
