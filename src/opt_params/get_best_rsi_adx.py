import pandas as pd

from src.conf import NASDAQ_INDEX_FILE
from src.io.read_data import load_stocks_data


# 加载文件路径或DataFrame
file_path = "output/historical_signals.csv"  # 替换为实际的文件路径


def calculate_returns(data, start_date, end_date):
    # 将数据读入 DataFrame
    df = pd.read_csv(data) if isinstance(data, str) else data
    df["imp_date"] = pd.to_datetime(df["imp_date"], format="%Y-%m-%d", errors="coerce")

    # 筛选指定日期区间的数据, 并且只保留买入信号
    mask = (
        (df["imp_date"] >= pd.to_datetime(start_date))
        & (df["imp_date"] <= pd.to_datetime(end_date))
        & (df["action"] == "buy")
    )
    selected_stocks = df[mask]

    # 保存收益率计算结果
    results = []

    for _, row in selected_stocks.iterrows():
        ticker = row["ticker"]
        purchase_price = row["price"]
        purchase_date = row["imp_date"]

        # 获取下一个交易日的价格
        stock_data = load_stocks_data(ticker)
        next_trading_date = stock_data[stock_data.index > purchase_date].index.min()

        if pd.isna(next_trading_date):
            continue

        next_price = stock_data.loc[next_trading_date, "close"]

        if next_price is not None:
            # 计算收益率
            return_rate = (next_price - purchase_price) / purchase_price

            # 将 RSI 和 ADX 分布加入结果中
            results.append(
                {
                    "ticker": ticker,
                    "imp_date": row["imp_date"],
                    "return_rate": return_rate,
                    "price": purchase_price,
                    "next_price": next_price,
                    "rsi": row["rsi"],
                    "adx": row["adx"],
                }
            )

    # 转换为 DataFrame
    result_df = pd.DataFrame(results)

    # 将 RSI 和 ADX 分组，并计算收益率分布
    result_df["rsi_group"] = (result_df["rsi"] // 5) * 5
    result_df["adx_group"] = (result_df["adx"] // 5) * 5

    # 将分组统计后的索引重置以包含分组列
    rsi_distribution = (
        result_df.groupby(["imp_date", "rsi_group"])["return_rate"]
        .describe()
        .reset_index()
    )
    rsi_distribution["score"] = rsi_distribution["mean"] / rsi_distribution["std"]

    adx_distribution = (
        result_df.groupby(["imp_date", "adx_group"])["return_rate"]
        .describe()
        .reset_index()
    )
    adx_distribution["score"] = adx_distribution["mean"] / adx_distribution["std"]

    rsi_distribution.round(4).to_csv("test/test_data/rsi_distribution.csv", index=False)
    adx_distribution.round(4).to_csv("test/test_data/adx_distribution.csv", index=False)
    print(f"RSI/ADX 分布数据已保存")

    return result_df, rsi_distribution, adx_distribution


def select_trading_group(df, group_col, min_count=10):
    """
    根据指定条件选择 RSI/ADX 的最佳分组，产生交易决策。

    参数：
    - df (pd.DataFrame): 包含历史收益率分布的 DataFrame，必须有 group 分组列和目标列。
    - group_col (str): 用于分组的列名，例如 "rsi_group" 或 "adx_group"。
    - min_count (int): 最小样本数量阈值，低于该阈值的组会被忽略。

    返回：
    - selected_group (float/int): 被选中的分组名。
    - selected_score (float): 被选中分组的评分结果。
    """

    # 过滤掉 count 小于 min_count 的组
    filtered_data = df[df["count"] >= min_count].copy()

    # 如果没有满足的组，返回 None
    if filtered_data.empty:
        print(f"没有任何分组 count >= {min_count}，无法选择分组。")
        return None, None, None

    # 找出每天评分最高的分组
    daily_max_groups = filtered_data.loc[
        filtered_data.groupby("imp_date")["score"].idxmax()
    ]

    # 统计每天最高分组的出现次数
    group_counts = daily_max_groups[group_col].value_counts()
    print(f"分组出现次数统计：\n{group_counts}")

    # 找出出现次数最多的分组
    max_count = group_counts.max()  # 出现的最大次数
    top_groups = group_counts[
        group_counts == max_count
    ].index  # 找到出现次数最多的分组（可能有多个）

    if len(top_groups) == 1:
        # 如果只有一个分组出现次数最多，直接返回
        most_frequent_group = top_groups[0]
    else:
        # 如果有多个分组出现次数相同，则按平均评分选择
        avg_scores = (
            daily_max_groups[daily_max_groups[group_col].isin(top_groups)]
            .groupby(group_col)["score"]
            .mean()
        )

        # 按评分降序排序，若评分相同则按分组名称升序
        most_frequent_group = avg_scores.sort_values(ascending=False).index[0]

    # 计算最终选中分组的平均【次日均收益率，也就是mean】
    most_frequent_group_mean_return = (
        daily_max_groups.loc[daily_max_groups[group_col] == most_frequent_group, "mean"]
        .mean()
        .round(4)
    )

    # 计算最终选中分组的最新【次日均收益率，也就是mean】
    most_frequent_group_latest_return = (
        filtered_data.loc[filtered_data[group_col] == most_frequent_group]
        .sort_values("imp_date", ascending=False)["mean"]
        .iloc[0]
        .round(4)
    )

    return (
        most_frequent_group,
        most_frequent_group_mean_return,
        most_frequent_group_latest_return,
    )


def get_best_rsi_adx_decisions(is_test=False):

    ## 如果需要生成指定日期范围的数据，可以取消下面的注释并设置日期范围
    # from strategy.analyze import analyze_stocks
    # from src.conf import DEFAULT_END_DATE

    # date_list_end = DEFAULT_END_DATE
    # date_list_start = "2025-03-03" # historical_signals.csv的开始日期, 方便对比

    # end_date_list = pd.date_range(date_list_start, date_list_end).sort_values().tolist()
    # for end_date in end_date_list:
    #     end_date = end_date.strftime("%Y-%m-%d")
    #     analyze_stocks(is_test=False,end_date=end_date,add_his_rec=False)

    nasdaq_df = pd.read_csv(NASDAQ_INDEX_FILE)

    decisions = []

    df = pd.read_csv(file_path, index_col="imp_date", parse_dates=True).sort_index(
        ascending=False
    )

    if not is_test:
        end_date_list = [df.index[0]]
    else:
        end_date_list = df.index.sort_values().drop_duplicates().tolist()

    for end_date in end_date_list:
        end_date = end_date.strftime("%Y-%m-%d")
        start_date = pd.to_datetime(end_date) - pd.DateOffset(days=20)

        all_data, rsi_dist, adx_dist = calculate_returns(
            file_path, start_date, end_date
        )
        print(f"所有股票及收益率数据: \n{all_data}")
        print(f"\nRSI收益率分布: \n{rsi_dist}")
        print(f"\nADX收益率分布: \n{adx_dist}")

        rsi_group, rsi_mean_return, rsi_latest_return = select_trading_group(
            rsi_dist, "rsi_group", min_count=10
        )
        adx_group, adx_mean_return, adx_latest_return = select_trading_group(
            adx_dist, "adx_group", min_count=10
        )

        # 基于 RSI 和 ADX 的分组选择，生成简单的决策逻辑
        decision = {
            "end_date": end_date,
            "rsi_group": rsi_group,
            "rsi_mean_return": rsi_mean_return,
            "rsi_latest_return": rsi_latest_return,
            "adx_group": adx_group,
            "adx_mean_return": adx_mean_return,
            "adx_latest_return": adx_latest_return,
        }

        print(f"交易策略决策如下：\n{decision}")

        # 检查是否有匹配数据
        filtered_df = nasdaq_df[nasdaq_df["日期"] == end_date]

        if not filtered_df.empty:
            nasdaq_change_rate = filtered_df["change_rate"].iloc[0].round(4)
            print(f"Change rate for {end_date}: {nasdaq_change_rate}")
        else:
            print(f"No data found for end_date: {end_date}.")
            nasdaq_change_rate = None  # 或者设置其他默认值
        decision["nasdaq_change_rate"] = nasdaq_change_rate

        decisions.append(decision)

    output_file = "output/rsi_adx_decisions.csv"

    if not is_test:
        old_decisions_df = pd.read_csv(output_file)
        new_decisions_df = pd.DataFrame(decisions)
        decisions_df = (
            pd.concat([old_decisions_df, new_decisions_df], ignore_index=True)
            .drop_duplicates(subset=["end_date"], keep="last")
            .sort_values(by="end_date", ascending=False)
            .reset_index(drop=True)
        )
    else:
        decisions_df = pd.DataFrame(decisions)

    print(decisions_df)

    # RSI 最新/平均回报大于等于 NASDAQ 变化率的比例
    latest_rsi_condition = (
        decisions_df["rsi_latest_return"] >= decisions_df["nasdaq_change_rate"]
    )
    mean_rsi_condition = (
        decisions_df["rsi_mean_return"] >= decisions_df["nasdaq_change_rate"]
    )

    # ADX 最新/平均回报大于等于 NASDAQ 变化率的比例
    latest_adx_condition = (
        decisions_df["adx_latest_return"] >= decisions_df["nasdaq_change_rate"]
    )
    mean_adx_condition = (
        decisions_df["adx_mean_return"] >= decisions_df["nasdaq_change_rate"]
    )

    print(
        f"RSI 最新回报不低于大盘收益的比例: {latest_rsi_condition.mean():.2%}\n"
        f"ADX 最新回报不低于大盘收益的比例: {mean_rsi_condition.mean():.2%}\n"
        f"RSI 平均回报不低于大盘收益的比例: {latest_adx_condition.mean():.2%}\n"
        f"ADX 平均回报不低于大盘收益的比例: {mean_adx_condition.mean():.2%}"
    )

    decisions_df.to_csv(output_file, index=False, mode="w")
    print(f"交易策略决策已保存到 {output_file}")
