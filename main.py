import os

from src.conf import (
    CLEANED_DATA_DIR,
    DEFAULT_END_DATE,
    HISTORICAL_SIGNAL_FILE,
    SOURCE_DIR,
)
from src.io.read_data import load_csv
from src.io.init_data import init_stock_list, init_historical_data
from src.io.update_data import update_nasdaq_index_data, update_stocks_recent_data
from src.strategy.analyze import analyze_stocks

# 创建存储文件夹
os.makedirs(CLEANED_DATA_DIR, exist_ok=True)

if __name__ == "__main__":

    # 更新nasdaq大盘数据
    update_nasdaq_index_data()

    # 初始化股票列表和信号数据库
    init_stock_list()

    # 如果尚无本地历史数据存储，则从目录导入数据
    local_files = os.listdir(CLEANED_DATA_DIR)
    if not local_files:
        print("历史数据文件为空，从目录初始化数据...")
        init_historical_data(SOURCE_DIR)
    else:
        print("历史数据已存在，无需初始化。")

    # 更新数据
    historical_signals = load_csv(HISTORICAL_SIGNAL_FILE)
    if historical_signals["imp_date"].max() == DEFAULT_END_DATE:
        print("数据已是最新，无需更新。")
    else:
        print("数据不是最新的，开始更新...")
        update_stocks_recent_data(interface_type="sina")

    # 分析股票并输出推荐信号
    recommendations = analyze_stocks()
    if recommendations:
        print("\n=== 推荐的交易信号 ===")
        for rec in recommendations:
            print(rec)
    else:
        print("没有推荐的交易信号。")
