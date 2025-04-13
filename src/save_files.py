import os

import pandas as pd

from src.conf import CLEANED_DATA_DIR, SIGNAL_FILE
from src.read_data import load_csv


def save_stocks_data(ticker, data):
    """保存股票历史价格数据为 CSV 文件"""
    file_path = os.path.join(CLEANED_DATA_DIR, f"{ticker}.csv")
    if os.path.exists(file_path):
        existing_data = pd.read_csv(file_path, index_col="date", parse_dates=True)
        data = pd.concat([existing_data, data]).drop_duplicates().sort_index()
    data.to_csv(file_path)


def save_signals(recommendations):
    """保存交易信号到 CSV 文件"""
    signal_columns = ["imp_date", "ticker", "action", "price", "short_ma", "long_ma", "volatility", "sharpe_ratio"]

    # 加载已有的信号数据
    signals = load_csv(SIGNAL_FILE)

    # 转换新推荐的数据为 DataFrame，确保列顺序一致
    new_data = pd.DataFrame(recommendations).reindex(columns=signal_columns)

    # 清理并格式化关键列，确保一致性
    if not signals.empty:
        signals["imp_date"] = pd.to_datetime(signals["imp_date"], errors="coerce")
        signals["ticker"] = signals["ticker"].str.strip()

    new_data["imp_date"] = pd.to_datetime(new_data["imp_date"], errors="coerce")
    new_data["ticker"] = new_data["ticker"].str.strip()

    combined_signals = pd.concat([signals, new_data], ignore_index=True)
    combined_signals = combined_signals.drop_duplicates(subset=["imp_date", "ticker", "action"], keep="last")

    combined_signals = combined_signals.reindex(columns=signal_columns)
    combined_signals.to_csv(SIGNAL_FILE, index=False)
    print("保存后的信号记录数: ", len(combined_signals))
