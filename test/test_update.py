import datetime

import akshare as ak

symbol = '105.' + 'AAPL'
today = datetime.date.today()
start_date = (today + datetime.timedelta(days=-3)).strftime('%Y%m%d')
end_date = today.strftime('%y%m%d')

stock_us_hist_df = ak.stock_us_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date)
print(stock_us_hist_df.describe())
print(stock_us_hist_df)