import numpy as np
from numba import jit


@jit(nopython=True)
def ma_calculate_position(close, signal):
    n = len(close)
    position = np.zeros(n, dtype=np.int64)  # 初始化 position 列
    entry_price = np.full(n, np.nan)  # 初始化 entry_price 列

    for i in range(1, n):  # 从第1天开始处理
        if position[i - 1] == 1:  # 如果上一天有持仓
            # 当前收益率 = (当前价格 - 买入价格) / 买入价格
            current_return = (close[i] - entry_price[i - 1]) / entry_price[i - 1]

            if (
                current_return >= 0.15 or signal[i] == 0
            ):  # 当前收益率 >= 10%或者卖出信号
                # 卖出
                position[i] = 0  # 清仓后不再持仓
                entry_price[i] = np.nan  # 清空买入价格记录
            else:
                # 无操作
                position[i] = 1  # 保持持仓
                entry_price[i] = entry_price[i - 1]  # 保留买入价格

        elif position[i - 1] == 0:  # 如果上一天未持仓
            if signal[i - 1] == 1:  # 上一天出现买入信号
                # 开仓买入
                position[i] = 1  # 更新状态为持仓
                entry_price[i] = close[i]  # 记录买入价格
            else:
                # 无操作
                position[i] = 0
                entry_price[i] = np.nan  # 保持为缺失值

    return position, entry_price
