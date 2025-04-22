# 全局配置
from datetime import date


PROJECT_PATH = "/data/quant_stocks/"
SOURCE_DIR = PROJECT_PATH + "his_data/us/nasdaq stocks"

NASDAQ_PATH = PROJECT_PATH + "stocks_list_dir/nasdaq/"
NASDAQ_GLOBAL_SELECT_300M_STOCK_LIST_FILE = NASDAQ_PATH + "global_select/g_s_300M.csv"
NASDAQ_GLOBAL_MARKET_300M_STOCK_LIST_FILE = NASDAQ_PATH + "global_market/g_m_300M.csv"
NASDAQ_300M_STOCK_LIST_FILE = NASDAQ_PATH + "nasdaq_300M.csv"

CLEANED_DATA_DIR = PROJECT_PATH + "cleaned_stocks_data"  # 保存历史数据的目录

OUTPUT_PATH = PROJECT_PATH + "output/"  # 保存输出结果的目录
HISTORICAL_SIGNAL_FILE = OUTPUT_PATH + "historical_signals.csv" # 历史信号文件
SIGNAL_FILE = OUTPUT_PATH + "signals.csv"  # 保存交易信号的文件

# 策略配置参数
STRATEGY_NAME = "dow_theory"  # 策略名称: "ma", "fixed_ma", "dow_theory"
SHORT_MA = 5  # 短期均线
LONG_MA = 20  # 长期均线

# 日期配置
DEFAULT_START_DATE = "2020-01-01"  # 默认开始日期
DEFAULT_END_DATE = date.today().strftime("%Y-%m-%d")  # 默认结束日期
