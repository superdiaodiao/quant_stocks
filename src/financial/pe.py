import pandas as pd

from src.conf import CLEANED_EPS_DATA_FILE

def get_sorted_eps_data(eps_file=CLEANED_EPS_DATA_FILE):
    """
    确保 df_eps 按照 report_date 排序，并且索引为 report_date
    """
    df_eps = pd.read_csv(eps_file, parse_dates=["report_date"])  # 加载每股收益数据

    if not isinstance(df_eps.index, pd.DatetimeIndex):
        df_eps["report_date"] = pd.to_datetime(df_eps["report_date"])
        df_eps.set_index("report_date", inplace=True)

    df_eps.sort_index(inplace=True)

    return df_eps


def calculate_pe(df_stocks, df_eps):
    """计算股票的市盈率（PE）
    通过将股票的收盘价除以每股收益（EPS）来计算市盈率（PE）。
    确保 df_stocks 按照索引排序，并且索引为日期类型。
    如果 df_stocks 没有日期索引，则尝试从 df_stocks 中的 'date' 列创建索引。
    如果 df_stocks 既没有日期索引，也没有 'date' 列，则抛出错误。
    """
    # 检查索引是否为日期类型
    if not isinstance(df_stocks.index, pd.DatetimeIndex):
        # 如果索引不是日期类型，且有 'date' 列
        if "date" in df_stocks.columns:
            df_stocks.index = pd.to_datetime(df_stocks["date"])  # 将 date 列转换为索引
            df_stocks.drop(columns=["date"], inplace=True)  # 可选：删除原来的 date 列
        else:
            raise ValueError("df_stocks 既没有日期索引，也没有 'date' 列！")

    # 按 ticker 和索引进行 merge_asof 匹配
    df_result = pd.merge_asof(
        left=df_stocks,
        right=df_eps,
        by="ticker",  # 以 ticker 匹配
        left_index=True,  # 使用左表索引进行匹配
        right_index=True,  # 使用右表索引进行匹配
        direction="backward",  # 找最近的日期
    )

    # 计算 PE
    df_stocks["pe"] = round(df_result["close"] / df_result["trailing_eps"], 2)

    return df_stocks  # 返回结果
