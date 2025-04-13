import os

import pandas as pd

from src.conf import HISTORICAL_DATA_DIR, STOCK_LIST_FILE


def load_csv(file_path):
    """加载 CSV 文件，确保无空行或格式问题"""
    if os.path.exists(file_path):
        df = pd.read_csv(file_path, skip_blank_lines=True, keep_default_na=False, encoding="utf-8")
        df.dropna(how="all", inplace=True)  # 移除全空行
        print(f"加载的数据总行数: {len(df)}")
        return df

    print(f"文件 {file_path} 不存在，将创建空 DataFrame...")
    return pd.DataFrame()


def load_stocks_data(ticker):
    """加载股票的历史价格数据"""
    ticker = ticker.lower()
    file_path = os.path.join(HISTORICAL_DATA_DIR, f"{ticker}.csv")
    if os.path.exists(file_path):
        return pd.read_csv(file_path, index_col="date", parse_dates=True)
    return pd.DataFrame()


def get_stock_list():
    stock_list = load_csv(STOCK_LIST_FILE)
    print("========= 股票列表文件内容 =========")
    print(stock_list.head())
    print(stock_list.columns)  # 打印列名检查是否完全对齐
    if "Symbol" not in stock_list.columns:
        print("列名 'Symbol' 不存在，检查文件格式...")
    else:
        print("列名 'Symbol' 存在，提取数据...")
    return stock_list['Symbol'].tolist()
