import os
import re

from src.conf import CLEANED_DATA_DIR, SOURCE_DIR
from src.io.init_data import init_stock_list, init_historical_data
from src.io.update_data import update_recent_data
from src.strategy.analyze import analyze_stocks

# 创建存储文件夹
os.makedirs(CLEANED_DATA_DIR, exist_ok=True)

if __name__ == "__main__":

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
    update_recent_data(interface_type="sina")

    # 分析股票并输出推荐信号
    recommendations = analyze_stocks()
    if recommendations:
        print("\n=== 推荐的交易信号 ===")
        for rec in recommendations:
            print(rec)
    else:
        print("没有推荐的交易信号。")
