import matplotlib.pyplot as plt
import ccxt
import pandas as pd


def plot_btc_signals(df, signals):
    plt.figure(figsize=(15, 7))
    plt.plot(df["close"], label="Price")
    plt.plot(signals["trend_line"], linestyle="--", color="orange", label="Trend Line")
    plt.scatter(
        signals[signals["sell_signal"]].index,
        df.loc[signals["sell_signal"], "close"],
        marker="v",
        color="red",
        s=100,
        label="Sell Signal",
    )
    plt.legend()
    plt.show()


def fetch_btc_data():
    exchange = ccxt.binance({"enableRateLimit": True})
    ohlcv = exchange.fetch_ohlcv(
        "BTC/USDT", "1d", since=exchange.parse8601("2024-01-01T00:00:00Z")
    )
    df = pd.DataFrame(
        ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp")


def plot_stock_signals(df, signals):
    plt.figure(figsize=(15, 7))
    plt.plot(df["close"], label="Close Price")
    plt.scatter(
        df.index[signals["2B_sell_signal"]],
        df["close"][signals["2B_sell_signal"]],
        color="red",
        label="2B Sell Signal",
        marker="v",
        s=100,
    )
    plt.scatter(
        df.index[signals["2B_buy_signal"]],
        df["close"][signals["2B_buy_signal"]],
        color="green",
        label="2B Buy Signal",
        marker="^",
        s=100,
    )
    plt.legend()
    plt.show()


def plot_trendlines(df):
    plt.figure(figsize=(14, 7))
    plt.plot(df.index, df["close"], label="Close Price", color="blue", linewidth=2)
    plt.plot(
        df.index,
        df["upward_trend"],
        label="Upward Trendline",
        color="green",
        linestyle="--",
        linewidth=1.5,
    )
    plt.plot(
        df.index,
        df["downward_trend"],
        label="Downward Trendline",
        color="red",
        linestyle="--",
        linewidth=1.5,
    )
    plt.title("Trendlines based on Dow Theory", fontsize=16)
    plt.xlabel("Date", fontsize=12)
    plt.ylabel("Price", fontsize=12)
    plt.legend()
    plt.grid(alpha=0.3)
    plt.show()
