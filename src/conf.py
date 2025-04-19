# 全局配置
from datetime import date


PROJECT_PATH = "/data/quant_stocks/"
SOURCE_DIR = PROJECT_PATH + "his_data/us/nasdaq stocks"
STOCK_LIST_FILE = (
    PROJECT_PATH + "stocks_list_dir/nasdaq/nasdaq_300M.csv"
)  # 保存股票基本信息的文件
CLEANED_DATA_DIR = PROJECT_PATH + "cleaned_stocks_data"  # 保存历史数据的目录
SIGNAL_FILE = PROJECT_PATH + "signals.csv"  # 保存交易信号的文件

# 配置参数
SHORT_MA = 5  # 短期均线
LONG_MA = 20  # 长期均线

# 日期配置
DEFAULT_START_DATE = "2020-01-01"  # 默认开始日期
DEFAULT_END_DATE = date.today().strftime("%Y-%m-%d")  # 默认结束日期
