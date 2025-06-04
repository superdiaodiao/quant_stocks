import os

import pandas as pd
import wget

from src.conf import (
    CLEANED_EPS_DATA_FILE,
    CLEANED_PRICE_DATA_DIR,
    HISTORICAL_SIGNAL_FILE,
    SIGNAL_FILE,
    SP500_VIX_FILE,
    STOCK_EPS_FILE,
)
from src.io.read_data import load_csv


vix_data_file_url = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
)
temp_vix_file = "temp_vix_history.csv"
vix_output_path = SP500_VIX_FILE
local_vix_df = pd.read_csv(vix_output_path, index_col="DATE", parse_dates=True)
max_local_vix_date = local_vix_df.index.max()


# 本地保存路径
def save_vix_data():
    """
    下载 VIX 历史数据，判断日期并更新本地文件。
    """

    try:
        # 下载 CSV 文件到临时路径
        print(f"正在下载文件：{vix_data_file_url}")
        wget.download(vix_data_file_url, temp_vix_file)
        print("\n下载完成。")

        remote_df = pd.read_csv(temp_vix_file, index_col="DATE", parse_dates=True)
        max_remote_vix_date = remote_df.index.max()

        # 比较远程数据的最大日期和本地文件的最大日期
        if max_remote_vix_date >= max_local_vix_date:

            # 保存远程数据到文件（覆盖模式）
            if max_remote_vix_date == max_local_vix_date:
                print(
                    f"远程文件的最大日期 {max_remote_vix_date.strftime('%Y-%m-%d')} 等于本地最大日期，不做更新。"
                )
            else:
                print(
                    f"远程文件的最大日期 {max_remote_vix_date.strftime('%Y-%m-%d')} 大于本地最大日期 {max_local_vix_date.strftime('%Y-%m-%d')}，更新本地文件。"
                )
                # 需要更新本地文件
                remote_df.to_csv(vix_output_path)
                print(f"数据已保存到文件：{vix_output_path}")
            return True
        else:
            print(
                f"远程文件的最大日期 {max_remote_vix_date.strftime('%Y-%m-%d')} 小于本地最大日期 {max_local_vix_date.strftime('%Y-%m-%d')}，不做更新。"
            )
            return False

    finally:
        # 删除临时文件
        if os.path.exists(temp_vix_file):
            os.remove(temp_vix_file)
            print("临时文件已删除。")


def save_pe_denominator(
    df=None, eps_file=STOCK_EPS_FILE, pe_deno_file=CLEANED_EPS_DATA_FILE
):

    df = pd.read_csv(eps_file) if df is None else df

    df = df.drop_duplicates(subset=["ticker", "std_report_date"], keep="last")
    df["std_report_date"] = pd.to_datetime(df["std_report_date"])
    df["report_date"] = pd.to_datetime(df["report_date"])

    # 单独处理函数
    def process_func(group):
        # 填充 eps 列：优先使用 diluted_eps，如果为空则用 basic_eps
        group["eps"] = group["diluted_eps"].fillna(group["basic_eps"])

        # 按 std_report_date 排序
        group = group.sort_values(by="std_report_date").reset_index(drop=True)

        # 检测是否为连续的正常季度（间隔应为 3 个月，即 90 天）
        group["month_diff"] = (
            group["std_report_date"]
            .diff()
            .apply(lambda x: (x.days // 30) if pd.notna(x) else 0)
        )
        group["is_continuous"] = (group["month_diff"] == 3) | (
            group["std_report_date"] == group["std_report_date"].min()
        )

        # 判断 4 个季度窗是否连续，使用滑动窗口检查连续性
        group["valid_window"] = (
            group["is_continuous"]
            .rolling(window=4, min_periods=4)
            .apply(lambda x: x.all(), raw=True)
        )

        # 计算前 4 个季度 eps 的总和
        group["trailing_eps"] = round(
            group["eps"].rolling(window=4, min_periods=4).sum(), 2
        )

        # 如果窗口无效（不连续），则 trailing_eps 置为 NaN
        group.loc[group["valid_window"] != 1, "trailing_eps"] = np.nan

        return group

    # 分组处理每只股票
    res_df = df.groupby("ticker", group_keys=False).apply(process_func)

    # 只保留需要的列
    res_df = res_df[
        [
            "ticker",
            "report_date",
            "std_report_date",
            "basic_eps",
            "diluted_eps",
            "trailing_eps",
        ]
    ]

    # 排序结果
    res_df["report_date"] = res_df["report_date"].apply(
        lambda x: pd.to_datetime(x, errors="coerce").strftime("%Y-%m-%d")
    )
    res_df["std_report_date"] = res_df["std_report_date"].apply(
        lambda x: pd.to_datetime(x, errors="coerce").strftime("%Y-%m-%d")
    )
    res_df = res_df.sort_values(
        by=["ticker", "report_date", "std_report_date"], ascending=[True, False, False]
    )  # type: ignore

    if pe_deno_file:
        res_df.to_csv(pe_deno_file, index=False)
        print(f"PE 分母数据已保存到 {pe_deno_file}")

    return res_df


def save_stocks_data(ticker, data):
    """保存股票历史价格数据为 CSV 文件"""
    file_path = os.path.join(CLEANED_PRICE_DATA_DIR, f"{ticker}.csv")
    if os.path.exists(file_path):
        existing_data = pd.read_csv(file_path, index_col="date", parse_dates=True)
        data = pd.concat([existing_data, data])
        data = data[~data.index.duplicated(keep="last")].sort_index()
    data.to_csv(file_path)


def clean_and_sort_signals(signals, signal_columns):
    """
    去重和排序信号数据，确保只保留最新数据，并按照指定规则排序。
    """
    signals["imp_date"] = pd.to_datetime(signals["imp_date"], errors="coerce")

    signals = signals.drop_duplicates(
        subset=["imp_date", "ticker", "action"], keep="last"
    )
    signals = signals.sort_values(
        by=["imp_date", "action", "rsi"], ascending=[False, False, False]
    )
    return signals.reindex(columns=signal_columns)


def save_signals(recommendations, signal_columns):
    # 加载已有的信号数据
    if os.path.exists(SIGNAL_FILE):
        signals = load_csv(SIGNAL_FILE)
    else:
        signals = pd.DataFrame(columns=signal_columns)

    # 转换新推荐的数据为 DataFrame，确保列顺序一致
    new_data = pd.DataFrame(recommendations).reindex(columns=signal_columns)

    # 格式化关键列，确保数据一致性
    if not signals.empty:
        signals["imp_date"] = pd.to_datetime(signals["imp_date"], errors="coerce")
        signals["ticker"] = signals["ticker"].str.strip()

    new_data["imp_date"] = pd.to_datetime(new_data["imp_date"], errors="coerce")
    new_data["ticker"] = new_data["ticker"].astype(str).str.strip()

    # 合并 signals 和 new_data
    combined_signals = pd.concat([signals, new_data], ignore_index=True)
    # 清理并排序
    combined_signals = clean_and_sort_signals(combined_signals, signal_columns)

    # 将合并后的信号追加到历史文件
    if os.path.exists(HISTORICAL_SIGNAL_FILE):
        historical_signals = load_csv(HISTORICAL_SIGNAL_FILE)
        historical_signals = pd.concat(
            [historical_signals, combined_signals], ignore_index=True
        )
    else:
        historical_signals = combined_signals

    # 清理并排序历史信号（去重）
    historical_signals = historical_signals.drop_duplicates(keep="last")
    historical_signals = clean_and_sort_signals(historical_signals, signal_columns)

    # 保存到历史文件
    historical_signals.to_csv(HISTORICAL_SIGNAL_FILE, index=False)

    # 保存新信号到 SIGNAL_FILE（只保留本次推荐数据）
    clean_new_data = clean_and_sort_signals(new_data, signal_columns)
    clean_new_data.to_csv(SIGNAL_FILE, index=False)
