import os

import pandas as pd

from src.conf import CLEANED_DATA_DIR, STOCK_LIST_FILE


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
    """加载股票的历史价格数据，并去除重复行"""
    ticker = ticker.lower()
    file_path = os.path.join(CLEANED_DATA_DIR, f"{ticker}.csv")
    if os.path.exists(file_path):
        # 加载数据
        df = pd.read_csv(file_path, index_col="date", parse_dates=True)

        # 去重处理：根据索引和列的值去重
        df = df[~df.index.duplicated(keep='last')]  # 针对日期索引的去重
        df = df.drop_duplicates()  # 针对整行数据去重

        return df

    # 如果文件不存在，返回空 DataFrame
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
