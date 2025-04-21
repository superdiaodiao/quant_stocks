import numpy as np
import matplotlib.pyplot as plt
import talib

from src.io.read_data import load_stocks_data
from src.strategy.common import (
    calculate_breadth_oscillator,
    calculate_moving_average,
    calculate_price_oscillator,
)
from strategy.ma.gen_fix_strategy import fixed_ma_strategy


# 计算RSI指标（重要性尚不及移动平均线）
def calculate_rsi(df, window=14):
    df["rsi"] = talib.RSI(df["close"], window)  # type: ignore # RSI指标
    return df


# 绘图
def plot_signals(df):
    plt.figure(figsize=(15, 8))
    plt.plot(df["close"], label="close Price", alpha=0.5)
    plt.plot(df["short_ma"], label="Short MA (10)", alpha=0.75)
    plt.plot(df["long_ma"], label="Long MA (30)", alpha=0.75)
    plt.scatter(
        df.index[df["buy_signal"]],
        df["close"][df["buy_signal"]],
        label="Buy Signal",
        marker="^",
        color="green",
    )
    plt.scatter(
        df.index[df["sell_signal"]],
        df["close"][df["sell_signal"]],
        label="Sell Signal",
        marker="v",
        color="red",
    )
    plt.legend()
    plt.title("Buy/Sell Signals with Moving Averages")
    plt.show()


# 股票代码、时间范围
symbol = "kros"  # 示例股票如苹果
end_date = "2025-04-17"

# 获取数据
df = load_stocks_data(symbol, end_date)

# 计算指标
df = calculate_moving_average(df, 5, 20)  # 书中说用10周和30周移动平均线
df = fixed_ma_strategy(df)
df = calculate_rsi(df)

# 生成模拟的宽度和价格震荡指标
advancers = np.random.randint(500, 1000, len(df))
decliners = np.random.randint(500, 1000, len(df))
df = calculate_breadth_oscillator(df, advancers, decliners)
df = calculate_price_oscillator(df)

# 显示数据和信号
print(
    df[
        [
            "close",
            "short_ma",
            "long_ma",
            "rsi",
            "Breadth_Oscillator",
            "Price_Oscillator",
            "buy_signal",
            "sell_signal",
        ]
    ].tail(10)
)

# 绘制图像
plot_signals(df)
