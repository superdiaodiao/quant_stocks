import matplotlib.pyplot as plt
from tqdm import tqdm

from strategy.ma.gen_strategy import ma_strategy
from src.io.read_data import load_stocks_data


def plot_strategy_performance(df):
    """绘制策略表现"""
    df["portfolio"] = (1 + df["position"] * df["close"].pct_change()).cumprod()
    plt.figure(figsize=(14, 7))
    plt.plot(df["close"], label="Price")
    plt.plot(df["short_ma"], label="Short MA")
    plt.plot(df["long_ma"], label="Long MA")
    plt.plot(df["portfolio"], label="Portfolio Value", linestyle="--")
    plt.legend()
    plt.show()


# tickers = get_stock_list()
tickers = ["KROS"]

recommendations = []

for ticker in tqdm(tickers, desc="分析股票"):
    df = load_stocks_data(ticker)
    df = ma_strategy(df)
    # 查看信号和仓位
    print(
        df[["close", "short_ma", "long_ma", "rsi", "adx", "signal", "position"]].tail()
    )
    plot_strategy_performance(df)
