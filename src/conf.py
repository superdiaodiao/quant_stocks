# 全局配置
PROJECT_PATH = "/data/quant_stocks/"
SOURCE_DIR = PROJECT_PATH + "his_data/us/nasdaq stocks"
STOCK_LIST_FILE = PROJECT_PATH + "nasdaq_300M.csv"  # 保存股票基本信息的文件
HISTORICAL_DATA_DIR = PROJECT_PATH + "stock_data"  # 保存历史数据的目录
SIGNAL_FILE = PROJECT_PATH + "signals.csv"  # 保存交易信号的文件

# 配置参数
UPDATE_DAYS = 2  # 更新最近N天的数据
SHORT_MA = 5  # 短期均线
LONG_MA = 20  # 长期均线
WEB_SEARCH_BATCH_SIZE = 2  # 每次批量下载的股票数量
