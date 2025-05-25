import time

import akshare as ak
import pandas as pd
from datetime import datetime

from src.conf import (
    DEFAULT_END_DATE,
    DEFAULT_START_DATE,
    STOCK_EPS_FILE,
)
from src.io.read_data import fetch_stock_eps, get_stock_list, load_stocks_data
from src.io.save_files import save_stocks_data

MAX_RETRIES = 5  # 最大重试次数
RETRY_DELAY = 10  # 每次重试的间隔时间（秒）

end_date = DEFAULT_END_DATE
# 2025年0529发现接口有问题, 暂时不使用
# current_nasdaq_df = pd.read_csv(NASDAQ_INDEX_FILE, index_col="日期", parse_dates=True, encoding="utf-8")
# nasdaq_max_date = (
#     current_nasdaq_df.sort_index().index[-1]
#     if not current_nasdaq_df.empty
#     else end_date
# )
nasdaq_max_date = end_date

# 美股财报的正常更新周期（90天一季）
report_date_threshold = 90
# 当前日期
current_date = datetime.now()


def fetch_data_with_retries(fetch_function, *args, **kwargs):
    """
    一个通用的带重试机制的函数，用于请求数据
    :param fetch_function: 请求数据的方法
    :param args: 方法的参数
    :param kwargs: 方法的关键字参数
    :return: 请求成功的数据 或 空DataFrame
    """
    retries = 0
    while retries < MAX_RETRIES:
        try:
            return fetch_function(*args, **kwargs)
        except Exception as e:
            retries += 1
            if retries >= MAX_RETRIES:
                print(f"请求失败，已重试 {MAX_RETRIES} 次: {e}")
                return pd.DataFrame()

            print(
                f"数据请求失败，重试 {retries} / {MAX_RETRIES} 次，等待 {RETRY_DELAY} 秒..."
            )
            time.sleep(RETRY_DELAY)


def update_nasdaq_index_data():
    """更新纳斯达克指数数据
    2025年0529发现接口有问题, 暂时不使用
    """
    pass


#     if pd.to_datetime(nasdaq_max_date) >= pd.to_datetime(end_date):
#         print("NASDAQ index data is already up to date.")
#         nasdaq_df = current_nasdaq_df
#     else:
#         print("Fetching latest NASDAQ index data...")
#         nasdaq_df = fetch_data_with_retries(ak.index_global_hist_em, symbol="纳斯达克")

#     if nasdaq_df is None or nasdaq_df.empty:
#         raise ValueError("Failed to fetch NASDAQ index data.")

#     nasdaq_df["imp_date"] = pd.to_datetime(nasdaq_df["日期"])
#     nasdaq_df = nasdaq_df.sort_values(by="imp_date")

#     nasdaq_df["change_rate"] = (
#         nasdaq_df["最新价"] - nasdaq_df["最新价"].shift(1)
#     ) / nasdaq_df["最新价"].shift(1)

#     nasdaq_df.drop(columns=["imp_date"], inplace=True)
#     nasdaq_df.reset_index(drop=True, inplace=True)
#     nasdaq_df.to_csv(NASDAQ_INDEX_FILE, index=False)
#     return nasdaq_df


# 更新股票每股收益数据, 一般每季度更新一次
def update_stocks_eps_data(
    stock_list=None,
    start_date=None,
    end_date=None,
    target_file=STOCK_EPS_FILE,
):
    """更新股票每股收益数据"""

    stock_list = get_stock_list() if stock_list is None else stock_list
    # 读取原始 eps.csv 数据
    eps_data = pd.read_csv(target_file)
    eps_data["report_date"] = pd.to_datetime(eps_data["report_date"], errors="coerce")

    # 初始化一个 DataFrame 用于存储更新后的数据
    updated_data = eps_data.copy()

    for stock in stock_list:
        print(f"正在处理股票 {stock} 的 EPS 数据...")
        # 如果指定了日期范围，则使用该范围强制更新
        if start_date is not None and end_date is not None:
            new_data = fetch_stock_eps(stock, start_date=start_date, end_date=end_date)
            if not new_data.empty:
                # 合并新数据到更新后的数据
                updated_data = pd.concat([updated_data, new_data], ignore_index=True)
            continue

        # 筛选出当前股票的历史数据
        stock_data = eps_data[eps_data["ticker"] == stock]

        # 获取最近财报日期（若无数据，设定为极小值）
        if not stock_data.empty:
            latest_report_date = stock_data["report_date"].max()
        else:
            latest_report_date = pd.to_datetime(DEFAULT_START_DATE)

        # 计算最近财报日期距离现在的天数
        days_since_latest_report = (current_date - latest_report_date).days
        print(
            f"股票 {stock} 最近财报日期: {latest_report_date.strftime('%Y-%m-%d')}，距离现在 {days_since_latest_report} 天"
        )

        # 判断是否需要更新
        if days_since_latest_report > report_date_threshold:
            print(f"股票 {stock} 的 EPS 数据不是最新，开始更新...")

            # 调用 akshare 接口获取最新数据
            new_data = fetch_stock_eps(
                stock, DEFAULT_START_DATE, current_date.strftime("%Y-%m-%d")
            )
            if not new_data.empty:
                # 合并新数据到更新后的数据
                updated_data = pd.concat([updated_data, new_data], ignore_index=True)
                print(f"股票 {stock} 的 EPS 数据更新成功！")
        else:
            print(f"股票 {stock} 的 EPS 数据是最新，无需更新。")

    # 格式化、去重、排序
    updated_data["report_date"] = updated_data["report_date"].apply(
        lambda x: pd.to_datetime(x, errors="coerce").strftime("%Y-%m-%d")
    )
    updated_data = updated_data.drop_duplicates(
        subset=["ticker", "std_report_date"],
        keep="last"
    )
    updated_data = updated_data.sort_values(
        by=["ticker", "report_date", "std_report_date"], ascending=[True, False, False]
    )  # ticker 升序, report_date 降序, std_report_date 降序

    # 保存到文件
    updated_data.to_csv(target_file, index=False)
    print(f"数据更新完成，并保存到文件：{target_file}")


def update_stocks_recent_data(interface_type="sina", tickers=[]):
    """更新股票最近几天的数据"""
    tickers = get_stock_list() if len(tickers) == 0 else tickers

    for ticker in tickers:
        try:
            ### 见https://akshare.akfamily.xyz/data/stock/stock.html#id56

            df_load = load_stocks_data(ticker)
            if not df_load.empty:
                if df_load.index[-1] == pd.to_datetime(nasdaq_max_date):
                    print(f"{ticker} already has latest data.")
                    continue
                else:
                    start_date = df_load.index[-1].strftime("%Y-%m-%d")
            else:
                start_date = DEFAULT_START_DATE

            if interface_type == "sina":
                ## 新浪财经接口
                symbol = ticker.upper()
                df = fetch_data_with_retries(ak.stock_us_daily, symbol=symbol)
            else:
                ## 东方财富接口
                symbol = "105." + ticker
                df = fetch_data_with_retries(
                    ak.stock_us_hist,
                    symbol=symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                )

            if df is None or df.empty:
                print(f"{ticker} could not update")
                continue

            df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]

            # 需要转化为int64，范围是[-2^63, 2^63-1]
            df["volume"] = df["volume"].astype("int64")

            print(
                f"Successfully get the data of {ticker} from {start_date} to {end_date}"
            )

            df = df.rename(
                columns={
                    "日期": "date",
                    "开盘": "open",
                    "收盘": "close",
                    "最高": "high",
                    "最低": "low",
                    "成交量": "volume",
                }
            )
            df["ticker"] = ticker
            df = df[["date", "ticker", "open", "high", "low", "close", "volume"]]
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.set_index("date")

            save_stocks_data(ticker.lower(), df)
            print(f"Saved the latest data of {ticker}")
        except Exception as e:
            print(f"{ticker}更新失败: {e}")
