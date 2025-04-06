import random
from typing import List

import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import mysql.connector
from mysql.connector import pooling
import schedule
import time

from pandas import DataFrame
from tqdm import tqdm
import warnings

warnings.filterwarnings('ignore')

# MySQL配置
DB_CONFIG = {
    'host': 'localhost',
    'user': 'quant_user',
    'password': 'secure_password',
    'database': 'quant_trading'
}

# 创建数据库连接池
db_pool = pooling.MySQLConnectionPool(
    pool_name="quant_pool",
    pool_size=5,
    **DB_CONFIG
)

# 配置参数
HISTORICAL_YEARS = 5  # 初始下载多少年的历史数据
UPDATE_DAYS = 2  # 每次更新获取最近几天数据
MIN_MARKET_CAP = 1e9  # 最小市值(10亿美元)
MIN_AVG_VOLUME = 1e6  # 最小日均成交量(100万股)
MAX_STOCKS = 5  # 最大分析股票数量
SHORT_MA = 5
LONG_MA = 20
WEB_SEARCH_BATCH_SIZE = 2  # 批量查询、下载股票的个数
INSERT_BATCH_SIZE = 10  # 批量写入处理大小


def get_db_connection():
    """从连接池获取数据库连接"""
    return db_pool.get_connection()


def init_database() -> None:
    """初始化MySQL数据库和表结构"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 创建股票列表表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_list (
            ticker VARCHAR(20) PRIMARY KEY,
            name VARCHAR(100),
            sector VARCHAR(50),
            industry VARCHAR(50),
            market_cap FLOAT,
            avg_volume FLOAT,
            last_updated DATE,
            INDEX idx_market_cap (market_cap),
            INDEX idx_last_updated (last_updated)
        )
        ''')

        # 创建价格数据表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_data (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ticker VARCHAR(20),
            date DATE,
            open FLOAT,
            high FLOAT,
            low FLOAT,
            close FLOAT,
            volume FLOAT,
            UNIQUE KEY uniq_ticker_date (ticker, date),
            INDEX idx_ticker (ticker),
            INDEX idx_date (date)
        )
        ''')

        # 创建信号记录表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            date DATE,
            ticker VARCHAR(20),
            signal_type ENUM('BUY', 'SELL', 'HOLD'),
            price FLOAT,
            short_ma FLOAT,
            long_ma FLOAT,
            signal_strength FLOAT,
            volatility FLOAT,
            sharpe_ratio FLOAT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_date_ticker (date, ticker),
            INDEX idx_signal_type (signal_type)
        )
        ''')

        conn.commit()
        print("数据库表初始化完成")

    except mysql.connector.Error as err:
        print(f"数据库初始化错误: {err}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def get_sp500_tickers() -> List[str]:
    """获取S&P 500成分股(示例)"""
    try:
        table = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')[0]
        return table['Symbol'].tolist()
    except Exception as e:
        print(f"获取SP500成分股失败: {e}")
        # 备用方案
        return ['AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META', 'TSLA', 'NVDA', 'JPM', 'V', 'WMT']


def save_stock_info(conn, ticker, info) -> None:
    """保存股票基本信息到MySQL"""
    try:
        cursor = conn.cursor()

        cursor.execute('''
        INSERT INTO stock_list 
        (ticker, name, sector, industry, market_cap, avg_volume, last_updated)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            name = VALUES(name),
            sector = VALUES(sector),
            industry = VALUES(industry),
            market_cap = VALUES(market_cap),
            avg_volume = VALUES(avg_volume),
            last_updated = VALUES(last_updated)
        ''', (
            ticker,
            info.get('shortName', ''),
            info.get('sector', ''),
            info.get('industry', ''),
            info.get('marketCap', 0),
            info.get('averageVolume', 0),
            datetime.now().date()
        ))

        conn.commit()
    except mysql.connector.Error as err:
        print(f"保存股票{ticker}信息失败: {err}")
    finally:
        cursor.close()


def batch_insert_data(conn, ticker, data) -> None:
    """批量插入股票价格数据到MySQL"""
    if data.empty:
        return
    else:
        required_columns = {'Open', 'High', 'Low', 'Close', 'Volume'}
        if not required_columns.issubset(data.columns):
            raise ValueError(f"Data is missing required columns: {required_columns - set(data.columns)}")

    try:
        cursor = conn.cursor()

        # 准备批量插入数据
        records = []
        for date, row in data.iterrows():
            records.append((
                ticker,
                date.to_pydatetime().date(),
                row['Open'],
                row['High'],
                row['Low'],
                row['Close'],
                row['Volume']
            ))

        # 使用批量插入语句
        insert_query = '''
        INSERT INTO stock_data 
        (ticker, date, open, high, low, close, volume)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            open = VALUES(open),
            high = VALUES(high),
            low = VALUES(low),
            close = VALUES(close),
            volume = VALUES(volume)
        '''

        # 分批执行防止数据包过大
        for i in range(0, len(records), INSERT_BATCH_SIZE):
            batch = records[i:i + INSERT_BATCH_SIZE]
            cursor.executemany(insert_query, batch)
            conn.commit()

    except mysql.connector.Error as err:
        print(f"批量插入{ticker}数据失败: {err}")
        conn.rollback()
    finally:
        cursor.close()


def download_historical_data(tickers) -> None:
    """下载历史数据并存入MySQL"""
    conn = get_db_connection()

    try:
        # 获取股票基本信息并筛选
        valid_tickers = []
        for ticker in tqdm(tickers, desc="获取股票信息"):
            try:
                print("Begin to process historical data for {}".format(ticker))
                stock = yf.Ticker(ticker)
                info = stock.info

                market_cap = info.get('marketCap', 0)
                avg_volume = info.get('averageVolume', 0)

                if market_cap >= MIN_MARKET_CAP and avg_volume >= MIN_AVG_VOLUME:
                    print("We can get the info of {}".format(ticker))
                    save_stock_info(conn, ticker, info)
                    valid_tickers.append(ticker)
                else:
                    print("We can not save data for {}".format(ticker))
            except Exception as e:
                print(f"处理股票{ticker}时出错: {e}")
                continue

        # 分批下载历史价格数据
        for i in tqdm(range(0, len(valid_tickers), WEB_SEARCH_BATCH_SIZE), desc="下载历史数据"):
            batch = valid_tickers[i:i + WEB_SEARCH_BATCH_SIZE]
            try:
                data = yf.download(
                    batch,
                    period=f"{HISTORICAL_YEARS}y",
                    group_by='ticker',
                    progress=False
                )
                print(f"\nWe have saved data of {batch}")

                # 存入数据库
                for ticker in batch:
                    if ticker in data:
                        df = data[ticker]
                        if not df.empty:
                            print(df.head())
                            batch_insert_data(conn, ticker, df)
            except Exception as e:
                print(f"下载、插入批次{i}-{i + WEB_SEARCH_BATCH_SIZE}时出错: {e}")
                continue
            finally:
                time.sleep(random.uniform(5, 15))  # 请求间隔

    finally:
        if conn.is_connected():
            conn.close()


def update_recent_data() -> None:
    """更新最近几天的数据"""
    conn = get_db_connection()

    try:
        # 获取需要更新的股票列表
        cursor = conn.cursor(dictionary=True)
        cursor.execute('SELECT ticker FROM stock_list')
        tickers = [row['ticker'] for row in cursor.fetchall()]
        cursor.close()

        # 获取每只股票的最新日期
        cursor = conn.cursor()
        cursor.execute('SELECT ticker, MAX(date) as max_date FROM stock_data GROUP BY ticker')
        max_dates = {row[0]: row[1] for row in cursor.fetchall()}
        cursor.close()

        # 确定需要更新的日期范围
        if not max_dates:
            print("没有找到历史数据，请先运行初始下载")
            return

        latest_date_in_db = max(max_dates.values())
        days_missing = (datetime.now().date() - latest_date_in_db).days

        if days_missing <= 0:
            print("数据已是最新")
            return

        update_start = latest_date_in_db + timedelta(days=1)
        update_end = datetime.now().date()

        print(f"更新数据从 {update_start} 到 {update_end}")

        # 分批更新
        for i in tqdm(range(0, len(tickers), WEB_SEARCH_BATCH_SIZE), desc="更新数据"):
            batch = tickers[i:i + WEB_SEARCH_BATCH_SIZE]
            try:
                data = yf.download(
                    batch,
                    start=update_start,
                    end=update_end,
                    group_by='ticker',
                    progress=False
                )

                # 存入新数据
                for ticker in batch:
                    if ticker in data:
                        df = data[ticker]
                        if not df.empty:
                            batch_insert_data(conn, ticker, df)

                            # 更新股票列表中的最后更新日期
                            cursor = conn.cursor()
                            cursor.execute(
                                'UPDATE stock_list SET last_updated = %s WHERE ticker = %s',
                                (datetime.now().date(), ticker)
                            )
                            conn.commit()
                            cursor.close()
            except Exception as e:
                print(f"更新批次{i}-{i + WEB_SEARCH_BATCH_SIZE}时出错: {e}")
                continue

    finally:
        if conn.is_connected():
            conn.close()


def get_stock_data_from_db(ticker, days) -> DataFrame or None:
    """从MySQL获取股票数据"""
    conn = get_db_connection()

    try:
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)

        query = '''
        SELECT date, open, high, low, close, volume 
        FROM stock_data 
        WHERE ticker = %s AND date BETWEEN %s AND %s
        ORDER BY date
        '''

        df = pd.read_sql(
            query,
            conn,
            params=(ticker, start_date, end_date)
        )

        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            return df
        return None

    finally:
        if conn.is_connected():
            conn.close()


def save_signal_to_db(signal_data) -> None:
    """保存信号到MySQL"""
    conn = get_db_connection()

    try:
        cursor = conn.cursor()

        insert_query = '''
        INSERT INTO signals 
        (date, ticker, signal_type, price, short_ma, long_ma, signal_strength, volatility, sharpe_ratio)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        '''

        cursor.execute(insert_query, (
            datetime.now().date(),
            signal_data['ticker'],
            'BUY',
            signal_data['price'],
            signal_data['short_ma'],
            signal_data['long_ma'],
            signal_data['signal_strength'],
            signal_data['volatility'],
            signal_data['sharpe']
        ))

        conn.commit()
    except mysql.connector.Error as err:
        print(f"保存信号失败: {err}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


def analyze_stocks() -> List:
    """分析股票生成信号"""
    conn = get_db_connection()

    try:
        # 获取要分析的股票列表
        query = f'''
        SELECT ticker FROM stock_list 
        ORDER BY market_cap DESC 
        LIMIT {MAX_STOCKS}
        '''

        df_tickers = pd.read_sql(query, conn)
        tickers = df_tickers['ticker'].tolist()

        recommendations = []

        for ticker in tqdm(tickers, desc="分析股票"):
            try:
                # 从数据库获取数据
                df = get_stock_data_from_db(ticker, LONG_MA + 10)  # 多取10天缓冲

                if df is None or len(df) < LONG_MA:
                    continue

                # 计算指标，用close而不是adj_close
                df['short_ma'] = df['close'].rolling(window=SHORT_MA).mean()
                df['long_ma'] = df['close'].rolling(window=LONG_MA).mean()
                df['signal'] = np.where(df['short_ma'] > df['long_ma'], 1, 0)
                df['position'] = df['signal'].diff()

                last_row = df.iloc[-1]
                prev_row = df.iloc[-2]

                # 只关注买入信号
                if last_row['position'] == 1:
                    daily_returns = df['close'].pct_change().dropna()
                    volatility = daily_returns.std() * np.sqrt(252)
                    sharpe_ratio = daily_returns.mean() / daily_returns.std() * np.sqrt(252)

                    signal_data = {
                        'ticker': ticker,
                        'price': last_row['close'],
                        'short_ma': last_row['short_ma'],
                        'long_ma': last_row['long_ma'],
                        'signal_strength': (last_row['short_ma'] - last_row['long_ma']) / last_row['long_ma'] * 100,
                        'volatility': volatility,
                        'sharpe': sharpe_ratio
                    }

                    recommendations.append(signal_data)
                    save_signal_to_db(signal_data)
            except Exception as e:
                print(f"分析{ticker}时出错: {e}")
                continue

        # 按信号强度排序
        recommendations.sort(key=lambda x: -x['signal_strength'])

        # 生成报告
        print("\n=== 股票交易建议 ===")
        print(f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"分析股票数量: {len(tickers)}")
        print(f"发现买入信号: {len(recommendations)}\n")

        for i, rec in enumerate(recommendations[:20]):  # 显示前20个
            print(f"{i + 1}. {rec['ticker']}")
            print(f"价格: ${rec['price']:.2f} | 短期MA: {rec['short_ma']:.2f} | 长期MA: {rec['long_ma']:.2f}")
            print(
                f"信号强度: {rec['signal_strength']:.2f}% | 波动率: {rec['volatility']:.2f} | 夏普比率: {rec['sharpe']:.2f}\n")

        return recommendations

    finally:
        if conn.is_connected():
            conn.close()


def daily_job() -> None:
    """每日执行的任务"""
    if datetime.now().weekday() >= 5:  # 周末不运行
        return

    print(f"\n{datetime.now().strftime('%Y-%m-%d')} 开始每日更新...")

    # 1. 更新数据
    update_recent_data()

    # 2. 分析数据
    analyze_stocks()


if __name__ == "__main__":
    # 初始化数据库
    init_database()

    # 首次运行下载历史数据
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM stock_list")
    count = cursor.fetchone()[0]
    print("count: " + str(count))
    cursor.close()
    conn.close()

    if count == 0:
        print("首次运行，下载历史数据...")
        sp500_tickers = get_sp500_tickers()
        download_historical_data(sp500_tickers[:MAX_STOCKS])  # 限制数量
    else:
        update_recent_data()

    # 设置定时任务 (每个交易日收盘后运行)
    # schedule.every().weekday.at("16:00").do(daily_job)

    # 立即运行一次
    daily_job()

    # # 保持程序运行
    # while True:
    #     schedule.run_pending()
    #     time.sleep(60)
