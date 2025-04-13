import yfinance as yf
from tqdm import tqdm

from src.conf import WEB_SEARCH_BATCH_SIZE, UPDATE_DAYS
from src.read_data import get_stock_list
from src.save_files import save_stocks_data


def update_recent_data():
    """更新最近几天的数据"""
    tickers = get_stock_list()

    for i in tqdm(range(0, len(tickers), WEB_SEARCH_BATCH_SIZE), desc="更新数据"):
        batch = tickers[i:i + WEB_SEARCH_BATCH_SIZE]
        try:
            data = yf.download(batch, period=f"{UPDATE_DAYS}d", group_by="ticker", progress=False)
            for ticker in batch:
                if ticker in data:
                    save_stocks_data(ticker, data[ticker])
        except Exception as e:
            print(f"批量更新失败: {e}")
