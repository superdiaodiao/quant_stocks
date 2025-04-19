from src.analyze import analyze_stocks
from src.init_data import init_stock_list


if __name__ == "__main__":

    # 初始化股票列表和信号数据库
    init_stock_list()

    # 分析股票并输出推荐信号
    recommendations = analyze_stocks(is_test=True, end_date="2025-04-16")
    print("\n=== 推荐的交易信号 ===")
    for rec in recommendations:
        print(rec)
