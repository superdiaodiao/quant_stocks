import os
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import time
import random

from tqdm import tqdm


# 全局配置
PROJECT_PATH = "/data/quant_stocks/"
SOURCE_DIR = PROJECT_PATH + "his_data/us/nasdaq stocks"
STOCK_LIST_FILE = PROJECT_PATH + "nasdaq_300M.csv"  # 保存股票基本信息的文件
HISTORICAL_DATA_DIR = PROJECT_PATH + "stock_data"  # 保存历史数据的目录
SIGNAL_FILE = PROJECT_PATH + "signals.csv"  # 保存交易信号的文件

# 配置参数
UPDATE_DAYS = 2  # 更新最近N天的数据
SHORT_MA = 5  # 短期均线
LONG_MA = 20  # 长期均线
WEB_SEARCH_BATCH_SIZE = 2  # 每次批量下载的股票数量

# 创建存储文件夹
os.makedirs(HISTORICAL_DATA_DIR, exist_ok=True)


def init_historical_data_storage_from_directory(source_dir: str):
    """
    初始化历史数据存储：从指定目录中读取文件，并只保存最近五年的数据。
    :param source_dir: 历史数据的根目录
    """
    # 确保目标存储目录存在
    os.makedirs(HISTORICAL_DATA_DIR, exist_ok=True)
    tickers = get_stock_list()

    # 当前日期和时间
    current_date = datetime.now()
    five_years_ago = current_date - timedelta(days=5 * 365)  # 最近五年的起点日期

    # 遍历指定目录及其子目录
    for root, dirs, files in os.walk(source_dir):
        for file in files:
            if file.endswith(".txt") and str(file.split('.')[0]).upper() in tickers:
                file_path = os.path.join(root, file)
                try:
                    # 读取 CSV 文件到 DataFrame
                    stock_data = pd.read_csv(
                        file_path,
                        header=0,  # 第一行为列名
                        names=["ticker", "period", "date", "time", "open", "high", "low", "close", "volume", "openint"],
                        parse_dates=["date"],  # 将日期字段解析为 datetime 类型
                        usecols=["ticker", "date", "open", "high", "low", "close", "volume"],  # 只保留需要的列
                        dtype={
                            "ticker": str,
                            "open": float,
                            "high": float,
                            "low": float,
                            "close": float,
                            "volume": float  # 改为浮点类型，避免转换错误
                        }
                    )

                    # 检查数据有效性，处理缺失值，确保 `volume` 为整数
                    stock_data["volume"] = stock_data["volume"].fillna(0).astype(int)

                    # 筛选最近五年的数据
                    stock_data = stock_data[stock_data["date"] >= five_years_ago]

                    # 去除重复数据，并按照日期排序
                    stock_data = stock_data.drop_duplicates(subset=["date"]).sort_values(by="date")

                    # 从文件名提取股票代码（假设文件名为 ticker.csv 或 ticker.txt）
                    ticker = file.split('.')[0]

                    # 保存结构化数据
                    save_historical_data(ticker, stock_data.set_index("date"))
                    print(f"保存股票 {ticker} 的最近五年数据成功！")
                except Exception as e:
                    print(f"处理文件 {file} 时出错：{e}")


def init_file(file_path, columns):
    """初始化 CSV 文件（如果不存在）"""
    if not os.path.exists(file_path):
        print(f"{file_path} not exists")
        df = pd.DataFrame(columns=columns)
        df.to_csv(file_path, index=False)


def load_csv(file_path):
    """加载 CSV 文件，处理可能的文件问题"""
    if os.path.exists(file_path):
        df = pd.read_csv(file_path, skip_blank_lines=False, keep_default_na=False, encoding="utf-8")
        print(f"加载的数据总行数: {len(df)}")
        return df

    print(f"文件 {file_path} 不存在，将创建空 DataFrame...")
    return pd.DataFrame()


def save_csv(file_path, df):
    """保存 DataFrame 到 CSV 文件"""
    df.to_csv(file_path, index=False)


def save_historical_data(ticker, data):
    """保存股票历史价格数据为 CSV 文件"""
    file_path = os.path.join(HISTORICAL_DATA_DIR, f"{ticker}.csv")
    if os.path.exists(file_path):
        existing_data = pd.read_csv(file_path, index_col="date", parse_dates=True)
        data = pd.concat([existing_data, data]).drop_duplicates().sort_index()
    data.to_csv(file_path)


def load_historical_data(ticker):
    """加载股票的历史价格数据"""
    ticker = ticker.lower()
    file_path = os.path.join(HISTORICAL_DATA_DIR, f"{ticker}.csv")
    if os.path.exists(file_path):
        return pd.read_csv(file_path, index_col="date", parse_dates=True)
    return pd.DataFrame()


def init_stock_list_storage():
    """初始化股票列表存储"""
    title_str = "Symbol,Name,Last Sale,Net Change,% Change,Market Cap,Country,IPO Year,Volume,Sector,Industry"
    init_file(STOCK_LIST_FILE, title_str.split(","))


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


def update_recent_data():
    """更新最近几天的数据"""
    tickers = get_stock_list()

    for i in tqdm(range(0, len(tickers), WEB_SEARCH_BATCH_SIZE), desc="更新数据"):
        batch = tickers[i:i + WEB_SEARCH_BATCH_SIZE]
        try:
            data = yf.download(batch, period=f"{UPDATE_DAYS}d", group_by="ticker", progress=False)
            for ticker in batch:
                if ticker in data:
                    save_historical_data(ticker, data[ticker])
        except Exception as e:
            print(f"批量更新失败: {e}")


def analyze_stocks():
    """分析股票并生成交易信号"""
    tickers = get_stock_list()

    recommendations = []

    for ticker in tqdm(tickers, desc="分析股票"):
        df = load_historical_data(ticker)
        if df.empty or len(df) < LONG_MA:
            continue

        df["short_ma"] = df["close"].rolling(SHORT_MA).mean()
        df["long_ma"] = df["close"].rolling(LONG_MA).mean()
        df["signal"] = np.where(df["short_ma"] > df["long_ma"], 1, 0)
        df["position"] = df["signal"].diff()

        if df.iloc[-1]["position"] == 1:  # 买入条件
            print(df.tail())
            daily_returns = df["close"].pct_change().dropna()
            volatility = daily_returns.std() * np.sqrt(252)
            sharpe_ratio = daily_returns.mean() / daily_returns.std() * np.sqrt(252)

            recommendations.append({
                "ticker": ticker,
                "price": df.iloc[-1]["close"],
                "short_ma": df.iloc[-1]["short_ma"],
                "long_ma": df.iloc[-1]["long_ma"],
                "volatility": volatility,
                "sharpe_ratio": sharpe_ratio
            })

    save_signals(recommendations)
    return recommendations


def save_signals(recommendations):
    """保存交易信号到 CSV 文件"""
    init_file(SIGNAL_FILE, ["ticker", "price", "short_ma", "long_ma", "volatility", "sharpe_ratio"])
    signals = load_csv(SIGNAL_FILE)
    for rec in recommendations:
        signals = pd.concat([signals, pd.DataFrame([rec])], ignore_index=True)
    save_csv(SIGNAL_FILE, signals)


if __name__ == "__main__":
    # 路径定义：假设你的本地文件路径如下

    # 初始化股票列表和信号数据库
    init_stock_list_storage()
    init_file(SIGNAL_FILE, ["ticker", "price", "short_ma", "long_ma", "volatility", "sharpe_ratio"])
    print("===============")

    # 如果尚无本地历史数据存储，则从目录导入数据
    local_files = os.listdir(HISTORICAL_DATA_DIR)
    if not local_files:
        print("历史数据文件为空，从目录初始化数据...")
        init_historical_data_storage_from_directory(SOURCE_DIR)
    else:
        print("历史数据已存在，无需初始化。")

    # 更新数据
    # update_recent_data()

    # 分析股票并输出推荐信号
    recommendations = analyze_stocks()
    print("\n=== 推荐的交易信号 ===")
    for rec in recommendations:
        print(rec)
