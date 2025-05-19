import akshare as ak
import pandas as pd

from src.conf import DEFAULT_END_DATE, DEFAULT_START_DATE, NASDAQ_INDEX_FILE
from src.io.read_data import get_stock_list, load_stocks_data
from src.io.save_files import save_stocks_data

end_date = DEFAULT_END_DATE


def update_nasdaq_index_data():

    nasdaq_df = ak.index_global_hist_em(symbol="纳斯达克")
    nasdaq_df["imp_date"] = pd.to_datetime(nasdaq_df["日期"])
    nasdaq_df = nasdaq_df.sort_values(by="imp_date")

    nasdaq_df["change_rate"] = (
        nasdaq_df["最新价"] - nasdaq_df["最新价"].shift(1)
    ) / nasdaq_df["最新价"].shift(1)

    nasdaq_df.drop(columns=["imp_date"], inplace=True)
    nasdaq_df.to_csv(NASDAQ_INDEX_FILE, index=False)
    return nasdaq_df


def update_stocks_recent_data(interface_type="sina", tickers=[]):
    """更新最近几天的数据"""
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
                df = ak.stock_us_daily(symbol=symbol)
            else:
                ## 东方财富接口
                symbol = "105." + ticker
                df = ak.stock_us_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                )

            df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]

            # 需要转化为int64，范围是[-2^63, 2^63-1]
            df["volume"] = df["volume"].astype("int64")

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
