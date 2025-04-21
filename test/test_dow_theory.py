import matplotlib.pyplot as plt
from strategy.dow_theory.gen_strategy import generate_signals


def plot_signals(df, signals):
    plt.figure(figsize=(15, 7))
    plt.plot(df["close"], label="BTC Price")
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


import ccxt
import pandas as pd


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


# 运行流程
btc_data = fetch_btc_data()
signals = generate_signals(btc_data)
plot_signals(btc_data, signals)

# 输出交易信号详情
latest_signal = signals[signals["sell_signal"]].iloc[-1]
print(f"卖出时间：{latest_signal.name.date()}") # type: ignore
print(f"触发价格：{latest_signal['close']:.2f}")
print(f"止损建议：{latest_signal['trend_line']:.2f}")
