import os

import pandas as pd

from src.conf import DEFAULT_START_DATE, NASDAQ_300M_STOCK_LIST_FILE, CLEANED_DATA_DIR
from src.io.read_data import get_stock_list
from src.io.save_files import save_stocks_data


def init_stock_list(list_file=NASDAQ_300M_STOCK_LIST_FILE):
    """初始化股票列表存储"""
    title_str = "Symbol,Name,Last Sale,Net Change,% Change,Market Cap,Country,IPO Year,Volume,Sector,Industry"
    if not os.path.exists(list_file):
        df = pd.DataFrame(columns=title_str.split(","))
        df.to_csv(list_file, index=False)


def init_historical_data(source_dir: str):
    """
    初始化历史数据存储：从指定目录中读取文件，并只保存指定日期的数据。
    :param source_dir: 历史数据的根目录
    """
    # 确保目标存储目录存在
    os.makedirs(CLEANED_DATA_DIR, exist_ok=True)
    tickers = get_stock_list()

    # 起点日期
    start_date = pd.to_datetime(DEFAULT_START_DATE)

    # 遍历指定目录及其子目录
    for root, dirs, files in os.walk(source_dir):
        for file in files:
            if file.endswith(".txt") and str(file.split(".")[0]).upper() in tickers:
                file_path = os.path.join(root, file)
                try:
                    # 读取 CSV 文件到 DataFrame
                    stock_data = pd.read_csv(
                        file_path,
                        header=0,  # 第一行为列名
                        names=[
                            "ticker",
                            "period",
                            "date",
                            "time",
                            "open",
                            "high",
                            "low",
                            "close",
                            "volume",
                            "openint",
                        ],
                        parse_dates=["date"],  # 将日期字段解析为 datetime 类型
                        usecols=[
                            "ticker",
                            "date",
                            "open",
                            "high",
                            "low",
                            "close",
                            "volume",
                        ],  # 只保留需要的列
                        dtype={
                            "ticker": str,
                            "open": float,
                            "high": float,
                            "low": float,
                            "close": float,
                            "volume": float,  # 改为浮点类型，避免转换错误
                        },
                    )
                    # ticker去掉后缀，如AAPL.US改为AAPL
                    stock_data["ticker"] = stock_data["ticker"].str.split(".").str[0]
                    # 检查数据有效性，处理缺失值，确保 `volume` 为整数
                    stock_data["volume"] = stock_data["volume"].fillna(0).astype(int)

                    # 筛选历史的数据
                    stock_data = stock_data[stock_data["date"] >= start_date]

                    # 去除重复数据，并按照日期排序
                    stock_data = stock_data.drop_duplicates(
                        subset=["date"]
                    ).sort_values(by="date")

                    # 从文件名提取股票代码（假设文件名为 ticker.csv 或 ticker.txt）
                    ticker = file.split(".")[0]

                    # 保存结构化数据
                    save_stocks_data(ticker, stock_data.set_index("date"))
                    print(f"保存股票 {ticker} 的自从{start_date}以来的数据成功！")
                except Exception as e:
                    print(f"处理文件 {file} 时出错：{e}")
