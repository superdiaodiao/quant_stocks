# 全局配置
from datetime import date, timedelta
from pathlib import Path


PROJECT_PATH = str(Path(__file__).resolve().parents[1]) + "/"
STOCK_PRICE_SOURCE_DIR = PROJECT_PATH + "his_data/us/nasdaq/stocks_price"
STOCK_EPS_FILE = PROJECT_PATH + "his_data/us/nasdaq/finance_info/eps.csv"

NASDAQ_PATH = PROJECT_PATH + "stocks_list_dir/nasdaq/"
NASDAQ_GLOBAL_SELECT_10B_STOCK_LIST_FILE = NASDAQ_PATH + "global_select/g_s_10B.csv"
NASDAQ_GLOBAL_SELECT_2B_STOCK_LIST_FILE = NASDAQ_PATH + "global_select/g_s_2B.csv"
NASDAQ_GLOBAL_SELECT_300M_STOCK_LIST_FILE = NASDAQ_PATH + "global_select/g_s_300M.csv"
NASDAQ_GLOBAL_MARKET_300M_STOCK_LIST_FILE = NASDAQ_PATH + "global_market/g_m_300M.csv"
NASDAQ_300M_STOCK_LIST_FILE = NASDAQ_PATH + "nasdaq_300M.csv"
NASDAQ_INDEX_FILE = NASDAQ_PATH + "nasdaq_index.csv"  # 纳斯达克指数文件
SP500_VIX_FILE = PROJECT_PATH + "his_data/us/sp500/vix.csv"  # VIX指数文件

CLEANED_PRICE_DATA_DIR = PROJECT_PATH + "cleaned_stocks_data/price"  # 保存历史价格数据的目录
CLEANED_EPS_DATA_FILE = PROJECT_PATH + "cleaned_stocks_data/financial/trailing_eps.csv"
POINT_IN_TIME_EPS_FILE = PROJECT_PATH + "cleaned_stocks_data/financial/eps_point_in_time.csv"
FINANCIAL_COVERAGE_FILE = PROJECT_PATH + "cleaned_stocks_data/financial/financial_coverage.json"
POINT_IN_TIME_FUNDAMENTALS_FILE = PROJECT_PATH + "cleaned_stocks_data/financial/fundamentals_point_in_time.csv"
FUNDAMENTALS_COVERAGE_FILE = PROJECT_PATH + "cleaned_stocks_data/financial/fundamentals_coverage.json"
FUNDAMENTALS_REFRESH_STATE_FILE = PROJECT_PATH + "cleaned_stocks_data/financial/fundamentals_refresh_state.json"
POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE = PROJECT_PATH + "cleaned_stocks_data/financial/quarterly_fundamentals_point_in_time.csv"
QUARTERLY_FUNDAMENTALS_COVERAGE_FILE = PROJECT_PATH + "cleaned_stocks_data/financial/quarterly_fundamentals_coverage.json"

OUTPUT_PATH = PROJECT_PATH + "output/"  # 保存输出结果的目录
HISTORICAL_SIGNAL_FILE = OUTPUT_PATH + "historical_signals.csv"  # 历史信号文件
SIGNAL_FILE = OUTPUT_PATH + "signals.csv"  # 保存交易信号的文件

# 策略配置参数
STRATEGY_NAME = "dow_theory"  # 策略名称: "ma", "fixed_ma", "dow_theory"
SHORT_MA = 5  # 短期均线
LONG_MA = 20  # 长期均线
VOLUMN_THREDHOLD = 100000  # 成交量阈值

# 日期配置
DEFAULT_START_DATE = "2020-01-01"  # 默认开始日期
DEFAULT_END_DATE = (date.today() - timedelta(days=1)).strftime(
    "%Y-%m-%d"
)  # 默认结束日期为昨天
