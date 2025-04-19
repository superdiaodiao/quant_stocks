import datetime

import akshare as ak

from update_data import update_recent_data

symbol = "105." + "aapl".upper()
today = datetime.date.today()
start_date = (today + datetime.timedelta(days=-3)).strftime("%Y%m%d")
end_date = today.strftime("%Y%m%d")

# stock_us_hist_df = ak.stock_us_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date)
# print(stock_us_hist_df.columns)
# print(stock_us_hist_df)

update_recent_data()
