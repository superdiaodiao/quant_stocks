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
    改进后的交易策略
    """

    params = (
        ("min_conditions", 2),  # 最少满足的条件数
        ("max_positions", 15),  # 最大持仓数量
        ("position_size_pct", 0.2),  # 每个仓位的资金比例
        ("min_trade_interval", 3),  # 最小交易间隔(天)
        ("vix_threshold_high", 30),
        ("vix_threshold_mid", 25),
        ("vix_threshold_low", 15),
    )

    def __init__(self):
        self.last_trade_date = {}  # 记录每个股票的最后交易日期
        self.conditions_weights = {
            "bollinger": 1.0,
            "kdj": 1.2,
            "signal": 1.5,
            "pe": 0.8,
        }
        self.total_conditions = len(self.conditions_weights)

        # 记录交易统计
        self.trade_count = 0
        self.position_count = 0

    def next(self):
        current_date = self.datas[0].datetime.date(0)

        # 检查是否有异常交易活动
        if self.trade_count > 100 and len(self.datas) < 50:
            print(f"异常交易活动检测: {self.trade_count} 笔交易")
            return

        buy_candidates = []
        for data in self.datas:
            # 检查数据有效性
            if any(
                getattr(data, line)[0] is None for line in data.lines.getlinealiases()
            ):
                continue

            # 检查最小交易间隔
            if data._name in self.last_trade_date:
                days_since_last = (current_date - self.last_trade_date[data._name]).days
                if days_since_last < self.p.min_trade_interval:
                    continue

            # 计算买入分数
            current_signal = data.signal[0]
            current_vix = data.vix_close[0]
            vix_weights = self.get_vix_adjusted_weights(current_vix)
            buy_conditions = [
                (data.bollinger_buy_signal[0] == 1.0, "bollinger"),
                (data.kdj_buy_signal[0] == 1.0, "kdj"),
                (current_signal == 1.0, "signal"),
                (data.pe[0] <= 100, "pe"),
            ]
            buy_score = sum(
                vix_weights[name] * weight
                for condition, name in buy_conditions
                if condition
                for weight in [self.conditions_weights[name]]
            )
            position = self.getposition(data)
            if buy_score > 0 and not position:
                buy_candidates.append((buy_score, data))

        # 排序，分数高的优先
        buy_candidates.sort(reverse=True, key=lambda x: x[0])

        # 动态阈值
        buy_threshold = (
            self.p.min_conditions
            * sum(self.conditions_weights.values())
            / self.total_conditions
        )

        # 依次买入，直到达到最大持仓
        for buy_score, data in buy_candidates:
            if self.position_count >= self.p.max_positions:
                break
            if buy_score >= buy_threshold:
                self.execute_buy(data, current_date)

        # 卖出逻辑保持不变
        for data in self.datas:
            position = self.getposition(data)
            if not position:
                continue
            current_signal = data.signal[0]
            current_vix = data.vix_close[0]
            vix_weights = self.get_vix_adjusted_weights(current_vix)
            sell_conditions = [
                (data.bollinger_sell_signal[0] == 1.0, "bollinger"),
                (data.kdj_sell_signal[0] == 1.0, "kdj"),
                (current_signal == 0, "signal"),
            ]
            sell_score = sum(
                vix_weights[name] * weight
                for condition, name in sell_conditions
                if condition
                for weight in [self.conditions_weights[name]]
            )
            sell_threshold = buy_threshold * 0.8
            if sell_score >= sell_threshold:
                self.execute_sell(data, current_date)


    def get_vix_adjusted_weights(self, current_vix):
        """根据VIX调整条件权重"""
        if current_vix >= self.p.vix_threshold_high:
            return {"bollinger": 1.2, "kdj": 1.0, "signal": 1.3, "pe": 0.7}
        elif current_vix >= self.p.vix_threshold_mid:
            return {"bollinger": 1.1, "kdj": 1.1, "signal": 1.2, "pe": 0.8}
        elif current_vix >= self.p.vix_threshold_low:
            return {"bollinger": 1.0, "kdj": 1.2, "signal": 1.1, "pe": 0.9}
        else:
            return {"bollinger": 0.9, "kdj": 1.3, "signal": 1.0, "pe": 1.0}

    def execute_buy(self, data, current_date):
        """执行买入操作"""
        if self.position_count >= self.p.max_positions:
            return

        cash_per_trade = self.broker.getcash() * self.p.position_size_pct
        size = int(cash_per_trade / data.close[0])

        if size > 0:
            self.buy(data=data, size=size)
            self.last_trade_date[data._name] = current_date
            self.trade_count += 1
            self.position_count += 1
            print(f"买入 {data._name} @ {data.close[0]}")

    def execute_sell(self, data, current_date):
        """执行卖出操作"""
        position = self.getposition(data)
        if position.size > 0:
            self.sell(data=data, size=position.size)
            self.last_trade_date[data._name] = current_date
            self.trade_count += 1
            self.position_count -= 1
            print(f"卖出 {data._name} @ {data.close[0]}")

    def notify_order(self, order):
        """订单状态通知"""
        if order.status in [order.Completed]:
            if order.isbuy():
                print(
                    f"买入完成: {order.executed.size} @ {order.executed.price}, 手续费: {order.executed.comm}"
                )
            elif order.issell():
                print(
                    f"卖出完成: {order.executed.size} @ {order.executed.price}, 手续费: {order.executed.comm}"
                )
