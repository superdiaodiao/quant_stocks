import yfinance as yf
import time
import random

tickers = ['AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META']
failed_tickers = []
valid_tickers = []

# 针对单只股票逐一下载数据
for ticker in tickers:
    for attempt in range(3):
        try:
            data = yf.download(ticker, period="7d", group_by="ticker", progress=False)
            if not data.empty:  # 如果数据不是空的
                print(f"{ticker} 下载成功")
                print(data.head())
                valid_tickers.append(ticker)
            else:
                print(f"{ticker} 数据为空，跳过")
            break  # 成功后跳出重试
        except Exception as e:
            print(f"{ticker} 下载失败，原因: {e}")
            if attempt == 2:  # 在达到最大重试次数后记录失败
                failed_tickers.append(ticker)
            time.sleep(5)  # 等待一段时间后重试
        time.sleep(random.uniform(5, 15))  # 请求间隔

print(f"有效股票: {valid_tickers}")
print(f"失败股票: {failed_tickers}")