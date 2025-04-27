import os

import pandas as pd

from src.conf import CLEANED_DATA_DIR, HISTORICAL_SIGNAL_FILE, SIGNAL_FILE
from src.io.read_data import load_csv


def save_stocks_data(ticker, data):
    """保存股票历史价格数据为 CSV 文件"""
    file_path = os.path.join(CLEANED_DATA_DIR, f"{ticker}.csv")
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
