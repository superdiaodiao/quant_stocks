import os

import pandas as pd
import akshare as ak

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    DEFAULT_END_DATE,
    DEFAULT_START_DATE,
    NASDAQ_300M_STOCK_LIST_FILE,
    SP500_VIX_FILE,
)


def load_csv(file_path):
    """加载 CSV 文件，确保无空行或格式问题"""
    if os.path.exists(file_path):
        df = pd.read_csv(
            file_path, skip_blank_lines=True, keep_default_na=False, encoding="utf-8"
        )
        df.dropna(how="all", inplace=True)  # 移除全空行
        print(f"加载的数据总行数: {len(df)}")
        return df

    print(f"文件 {file_path} 不存在，将创建空 DataFrame...")
    return pd.DataFrame()


def get_vix_data(file_path=SP500_VIX_FILE):
    """加载 VIX 数据"""
    if not os.path.exists(file_path):
        print(f"VIX 数据文件 {file_path} 不存在，将创建空 DataFrame...")
        return pd.DataFrame()

    print(f"加载 VIX 数据文件: {file_path}")
    df = pd.read_csv(file_path, index_col="DATE", parse_dates=True)
    df = df.sort_index()  # 按照日期索引进行升序排序
    df = df[~df.index.duplicated(keep="last")]  # 针对日期索引的去重

    df.rename(
        columns={
            "OPEN": "vix_open",
            "HIGH": "vix_high",
            "LOW": "vix_low",
            "CLOSE": "vix_close",
        },
        inplace=True,
    )

    return df


def load_stocks_data(
    ticker="AAPL", end_date=DEFAULT_END_DATE, dir=CLEANED_PRICE_DATA_DIR
):
    """加载股票的历史价格数据，并去除重复行"""
    ticker = ticker.lower()
    file_path = os.path.join(dir, f"{ticker}.csv")
    print(f"加载股票数据文件: {file_path}")
    if os.path.exists(file_path):
        # 加载数据
        df = pd.read_csv(file_path, index_col="date", parse_dates=True)
        df = df.sort_index()  # 按照日期索引进行升序排序

        # 去重处理：根据索引和列的值去重
        df = df[~df.index.duplicated(keep="last")]  # 针对日期索引的去重
        df = df.drop_duplicates()  # 针对整行数据去重

        df = df[df.index <= pd.to_datetime(end_date)]

        return df

    # 如果文件不存在，返回空 DataFrame
    return pd.DataFrame()


def get_stock_list(file_path=NASDAQ_300M_STOCK_LIST_FILE):
    """获取股票列表"""
    if not os.path.exists(file_path):
        print(f"股票列表文件 {file_path} 不存在, 将创建空 DataFrame...")
    else:
        print(f"股票列表文件 {file_path} 存在，加载数据...")
    # 加载 CSV 文件
    stock_list = load_csv(file_path)
    print("========= 股票列表文件内容 =========")
    print(stock_list.head())
    print(stock_list.columns)  # 打印列名检查是否完全对齐
    if "Symbol" not in stock_list.columns:
        print("列名 'Symbol' 不存在，检查文件格式...")
    else:
        print("列名 'Symbol' 存在，提取数据...")
    return stock_list["Symbol"].tolist()


def fetch_stock_eps(stock, start_date=DEFAULT_START_DATE, end_date=DEFAULT_END_DATE):
    """
    从 akshare 接口获取指定股票的最新 EPS 数据
    """
    column_dict = {
        "SECURITY_CODE": "ticker",
        "REPORT_DATE": "report_date",
        "STD_REPORT_DATE": "std_report_date",
        "BASIC_EPS": "basic_eps",
        "DILUTED_EPS": "diluted_eps",
    }
    try:
        eps_df = ak.stock_financial_us_analysis_indicator_em(
            symbol=stock, indicator="单季报"
        )
        if not eps_df.empty:
            # 提取需要的字段
            eps_df = eps_df[column_dict.keys()]
            # 重命名列
            eps_df.rename(
                columns=column_dict,
                inplace=True,
            )
            # 过滤日期范围
            eps_df["report_date"] = pd.to_datetime(
                eps_df["report_date"], errors="coerce"
            )
            eps_df = eps_df[
                (eps_df["report_date"] >= pd.to_datetime(start_date))
                & (eps_df["report_date"] <= pd.to_datetime(end_date))
            ]
            if eps_df.empty:
                print(f"{stock} 在指定日期范围内没有 EPS 数据。")
                return pd.DataFrame(columns=list(column_dict.values()))

            # 格式化日期和数据类型
            eps_df["report_date"] = eps_df["report_date"].apply(
                lambda x: pd.to_datetime(x, errors="coerce").strftime("%Y-%m-%d")
            )
            eps_df["std_report_date"] = eps_df["std_report_date"].apply(
                lambda x: pd.to_datetime(x, errors="coerce").strftime("%Y-%m-%d")
            )
            eps_df["basic_eps"] = eps_df["basic_eps"].astype(float)
            eps_df["diluted_eps"] = eps_df["diluted_eps"].astype(float)

            print(f"获取股票 {stock} 的 EPS 数据成功！")
            return eps_df
        else:
            print(f"{stock} 没有可用的 EPS 数据, 返回空 DataFrame。")
            return pd.DataFrame(columns=list(column_dict.values()))
    except Exception as e:
        print(f"获取股票 {stock} 数据失败，错误信息：{e}")
        return pd.DataFrame(columns=list(column_dict.values()))


# 过滤股票
def filter_data(df, cap_threshold=3000000000, country="United States"):

    df = df[df["Market Cap"] >= cap_threshold & df["Country"] == country]

    return df
