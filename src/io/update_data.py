import akshare as ak
import pandas as pd

from src.conf import DEFAULT_END_DATE
from src.io.read_data import get_stock_list, load_stocks_data
from src.io.save_files import save_stocks_data

end_date = DEFAULT_END_DATE


def update_recent_data(interface_type="sina"):
    """更新最近几天的数据"""
    tickers = get_stock_list()

    for ticker in tickers:
        try:
            ### 见https://akshare.akfamily.xyz/data/stock/stock.html#id56

            if interface_type == "sina":
                ## 新浪财经接口
                symbol = ticker.upper()
                start_date = load_stocks_data(ticker).index[-1].strftime("%Y-%m-%d")
                df = ak.stock_us_daily(symbol=symbol)
                df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]

                # 需要转化为int64，范围是[-2^63, 2^63-1]
                df["volume"] = df["volume"].astype("int64")
            else:
                ## 东方财富接口
                symbol = "105." + ticker
                start_date = load_stocks_data(ticker).index[-1].strftime("%Y%m%d")
                df = ak.stock_us_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                )

            if df.empty:
                print(f"{ticker} could not update")
                continue

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
