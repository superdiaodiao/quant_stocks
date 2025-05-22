import time

import akshare as ak
import pandas as pd

from src.conf import DEFAULT_END_DATE, DEFAULT_START_DATE, NASDAQ_INDEX_FILE
from src.io.read_data import get_stock_list, load_stocks_data
from src.io.save_files import save_stocks_data


MAX_RETRIES = 5  # 最大重试次数
RETRY_DELAY = 10  # 每次重试的间隔时间（秒）

end_date = DEFAULT_END_DATE


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
    """更新纳斯达克指数数据"""
    nasdaq_df = fetch_data_with_retries(ak.index_global_hist_em, symbol="纳斯达克")

    if nasdaq_df is None or nasdaq_df.empty:
        raise ValueError("Failed to fetch NASDAQ index data.")

    nasdaq_df["imp_date"] = pd.to_datetime(nasdaq_df["日期"])
    nasdaq_df = nasdaq_df.sort_values(by="imp_date")

    nasdaq_df["change_rate"] = (
        nasdaq_df["最新价"] - nasdaq_df["最新价"].shift(1)
    ) / nasdaq_df["最新价"].shift(1)

    nasdaq_df.drop(columns=["imp_date"], inplace=True)
    nasdaq_df.to_csv(NASDAQ_INDEX_FILE, index=False)
    return nasdaq_df


def update_stocks_recent_data(interface_type="sina", tickers=[]):
    """更新股票最近几天的数据"""
    tickers = get_stock_list() if len(tickers) == 0 else tickers

    nasdaq_df = pd.read_csv(NASDAQ_INDEX_FILE, index_col="日期", parse_dates=True)
    nasdaq_max_date = (
        nasdaq_df.sort_index().index[-1] if not nasdaq_df.empty else end_date
    )

    for ticker in tickers:
        try:
            ### 见https://akshare.akfamily.xyz/data/stock/stock.html#id56

            df_load = load_stocks_data(ticker)
            if not df_load.empty:
                if df_load.index[-1] == nasdaq_max_date:
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
