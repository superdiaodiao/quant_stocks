from main import init_historical_data

SOURCE_DIR = "/data/quant_stocks/his_data/us/nasdaq stocks"
STOCK_LIST_FILE = "stock_list.csv"  # 保存股票基本信息的文件
HISTORICAL_DATA_DIR = "./stock_data"  # 保存历史数据的目录
SIGNAL_FILE = "signals.csv"  # 保存交易信号的文件

init_historical_data(SOURCE_DIR)
