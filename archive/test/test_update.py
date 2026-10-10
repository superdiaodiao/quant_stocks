import datetime

import akshare as ak

from src.io.update_data import update_stocks_recent_data

# dongcai interface
symbol = "105." + "aapl".upper()
today = datetime.date.today()
start_date = (today + datetime.timedelta(days=-3)).strftime("%Y%m%d")
end_date = today.strftime("%Y%m%d")

# stock_us_hist_df = ak.stock_us_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date)
# print(stock_us_hist_df.columns)
# print(stock_us_hist_df)

# sina interface
symbol = "aal".upper()
stock_us_hist_df = ak.stock_us_daily(symbol=symbol)
stock_us_hist_df = stock_us_hist_df[
    (stock_us_hist_df["date"] >= start_date) & (stock_us_hist_df["date"] <= end_date)
]

print(stock_us_hist_df.columns)
print(stock_us_hist_df)


# update_stocks_recent_data()
