import backtrader as bt


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
        "macd_buy_signal",
        "macd_sell_signal",
        "kdj_buy_signal",
        "kdj_sell_signal",
        "bollinger_buy_signal",
        "bollinger_sell_signal",
        "donchian_buy_signal",
        "donchian_sell_signal",
        "keltner_buy_signal",
        "keltner_sell_signal",
        "pe",
        "vix_close",
    )

    # 默认情况下，Backtrader 将尝试匹配列到字段。如果列未指定，设置为 -1 (不存在)
    params = (
        ("signal", -1),
        # ("rsi", -1),
        # ("adx", -1),
        # ("ma_50", -1),
        ("volume", -1),
        ("volume_avg_w_pct", -1),
        ("macd_buy_signal", -1),
        ("macd_sell_signal", -1),
        ("kdj_buy_signal", -1),
        ("kdj_sell_signal", -1),
        ("bollinger_buy_signal", -1),
        ("bollinger_sell_signal", -1),
        ("donchian_buy_signal", -1),
        ("donchian_sell_signal", -1),
        ("keltner_buy_signal", -1),
        ("keltner_sell_signal", -1),
        ("pe", -1),
        ("vix_close", -1),
    )


class BacktraderStrategy(bt.Strategy):
    """
    策略：增加对买入条件和卖出条件的组合支持
    """

    def __init__(self):
        # 存储所有股票信号和持仓信息
        self.signals = {data: data.signal for data in self.datas}

    def next(self):
        """
        每天执行逻辑
        """
        for data in self.datas:
            current_signal = data.signal[0]
            current_price = data.close[0]

            if current_signal is None or current_price is None:
                continue

            # 动态调整组合
            current_vix_close = data.vix_close[0]
            if current_vix_close >= 30:  # 极高
                dynamic_buy_combinations = [0, 2, 3]
                dynamic_sell_combinations = [0, 1]
            elif current_vix_close >= 25 and current_vix_close < 30:  # 高
                dynamic_buy_combinations = [0, 2, 3]
                dynamic_sell_combinations = [0, 1]
            elif current_vix_close >= 15 and current_vix_close < 25:  # 中
                dynamic_buy_combinations = [0, 1]
                dynamic_sell_combinations = [0, 1]
            else:  # 低
                dynamic_buy_combinations = [1, 2]
                dynamic_sell_combinations = [0, 1]

            # 动态生成买入条件组合
            buy_conditions = [
                data.bollinger_buy_signal[0] == 1.0,
                # data.macd_buy_signal[0] == 1.0,
                data.kdj_buy_signal == 1.0,
                # data.donchian_buy_signal[0] == 1.0,
                # data.keltner_buy_signal[0] == 1.0,
                current_signal == 1.0,
                data.pe[0] <= 100,
            ]
            # selected_buy_conditions = [
            #     buy_conditions[i] for i in self.params.buy_condition_indices  # type: ignore
            # ]
            selected_buy_conditions = [
                buy_conditions[i] for i in dynamic_buy_combinations
            ]

            # 检查买入条件
            if any(selected_buy_conditions) and not self.getposition(data):
                size = int(8000 / data.close[0])
                if size > 0:
                    print(f"买入 {data._name} @ {current_price}")
                    self.buy(data=data, size=size)

            # 动态生成卖出条件组合
            sell_conditions = [
                data.bollinger_sell_signal[0] == 1.0,
                # data.macd_sell_signal[0] == 1.0,
                data.kdj_sell_signal == 1.0,
                # data.donchian_sell_signal[0] == 1.0,
                # data.keltner_sell_signal[0] == 1.0,
                current_signal == 0,
            ]
            # selected_sell_conditions = [
            #     sell_conditions[i] for i in self.params.sell_condition_indices  # type: ignore
            # ]
            selected_sell_conditions = [
                sell_conditions[i] for i in dynamic_sell_combinations
            ]

            # 检查卖出条件
            if any(selected_sell_conditions) and self.getposition(data):
                print(f"卖出 {data._name} @ {current_price}")
                self.sell(data=data, size=self.getposition(data).size)

    def notify_order(self, order):
        """
        通知订单状态变化
        """
        if order.status in [order.Completed]:
            if order.isbuy():
                print(
                    f"买入完成: {order.executed.size} @ {order.executed.price}, 手续费: {order.executed.comm}"
                )
            elif order.issell():
                print(
                    f"卖出完成: {order.executed.size} @ {order.executed.price}, 手续费: {order.executed.comm}"
                )
