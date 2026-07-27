# quant_stocks

这是一个面向 Nasdaq 股票的量化研究与每日选股项目。目前唯一正式保留的股票策略是
`can-slim-top3-v1`：使用公开可观察的 CAN SLIM 思想、点时（Point-in-Time）
季度财务数据和价格强度进行选股，并与 **Nasdaq Composite** 比较。

> 当前状态：`SHADOW / 暂不允许真实下单`。
>
> 历史回测已经通过，但策略在 2026-07-18 才正式冻结。冻结后的真实前向样本尚未达到
> 生产要求，因此代码只生成推荐文件，不连接券商、不向 IBKR 自动提交订单。

## 1. 项目目标

本项目希望完成一条可以长期维护的完整链路：

1. 更新 Nasdaq 股票池、价格和财务数据；
2. 使用与每日推荐完全相同的逻辑进行历史回放；
3. 在尽量避免过拟合的前提下，与 Nasdaq Composite 比较；
4. 记录每次调仓的股票、价格、股数、成本、现金和盈亏；
5. 在模型通过真实前向验证后，每日给出可人工检查的股票建议；
6. 项目本身永远不自动下单。

## 2. 当前固定策略

### 2.1 选股规则

当前冻结参数位于 `src/research/can_slim_validation.py` 的
`fixed_top3_config()`：

- 股票池：当时已知的 Nasdaq 普通股历史快照；
- 最多持有：3 只；
- 不要求买满：当期没有股票满足条件时可以少持有或持有现金；
- 选满 3 只时：每只目标权重为 1/3；
- 最低股价：10 美元；
- 最近 50 个交易日最低中位成交额：1,000 万美元；
- 最低季度利润增长：25%；
- 最低季度收入增长：10%；
- 最低相对成交量：0.8；
- 当前价格至少达到 52 周最高价的 85%；
- 使用季度 PIT 净利润和收入数据；
- 使用 12-1 月相对强度、3 月价格强度、接近 52 周高点和成交量共同评分；
- 市场过滤：Nasdaq Composite 位于 200 日均线允许的风险状态；
- 对比基准：只使用 Nasdaq Composite，不使用 QQQ。

这是公开 CAN SLIM 思想的可复现实现，不声称复制 IBD 的专有评分、图表标记或买卖信号。

### 2.2 信号与成交时点

- 每月最后一个已完成交易日收盘后产生信号；
- 在下一个交易日收盘价模拟成交；
- 后续交易日不重新选股，只更新价格和持仓状态；
- 下一次月度信号到来时重新计算目标组合；
- 单边交易成本按 10 bps（0.10%）计入；
- 不进行自动券商下单。

例如：

- 6 月 30 日美股收盘后得到信号；
- 7 月 1 日美股收盘模拟买入；
- 7 月其余时间保持该批股票，直到下一个月度调仓。

北京时间通常在美股交易日晚上进入下一交易日凌晨。程序应在前一美股交易日完成后运行，
再根据是否到达执行日输出 `BUY_NEXT_CLOSE`、`HOLD_POSITION` 或 `HOLD_CASH`。

### 2.3 回测持仓模型

回测采用自融资账户：

- 初始资金标准化为 1,000,000 美元；
- 支持小数股，因此账户规模不会改变收益率；
- 调仓后持有固定股数；
- 两次调仓之间权重会随股票价格自然漂移；
- 不存在未记录、未收费的每日恒定权重再平衡；
- 每次买入、增持、减持和卖出都会进入逐笔账本；
- 交易成本从现金和组合净值中真实扣除。

逐笔账本字段包括：

- 信号日期、执行日期；
- 股票代码；
- `BUY` / `SELL` 方向；
- `BUY` / `INCREASE` / `REDUCE` / `SELL` 动作；
- 调仓原因；
- 调仓前权重和调仓后目标权重；
- 成交价格、股数、成交金额；
- 交易成本；
- 成交后现金和组合净值；
- 原始建仓日期和成本；
- 已实现盈亏和已实现收益率。

## 3. 当前历史结果

修正为固定股数、自融资、完整计费后，2021–2026 年结果如下：

| 年份 | 策略收益 | Nasdaq Composite | 超额收益 |
|---|---:|---:|---:|
| 2021 | 41.38% | 21.39% | 19.99% |
| 2022 | -18.51% | -33.10% | 14.59% |
| 2023 | 54.93% | 43.42% | 11.51% |
| 2024 | 229.99% | 28.64% | 201.35% |
| 2025 | 29.43% | 20.36% | 9.07% |
| 2026（截至 7 月 17 日） | 21.31% | 9.80% | 11.51% |

需要正确理解这些数字：

- 这是历史研究结果，不等于未来收益承诺；
- 参数曾参考历史表现，不能把全部历史年份称为完全样本外；
- 2024 年收益非常高，组合集中度也高，未来不应假设可以重复；
- 策略最大回撤仍可能较大；
- 真实生产资格必须依赖冻结后的前向表现，而不能继续通过调整参数改善历史结果；
- 修改正式模型后，前向观察时钟必须重新开始。

正式结果文件：

| 文件 | 说明 |
|---|---|
| `output/can_slim_fixed_top3_summary.json` | 冻结参数、验证状态和统计摘要 |
| `output/can_slim_fixed_top3_annual.csv` | 年度策略、Nasdaq 和超额收益 |
| `output/can_slim_fixed_top3_backtest.csv` | 每日收益、仓位、换手、现金和净值 |
| `output/can_slim_fixed_top3_cost_stress.csv` | 10/30/50 bps 成本压力测试 |
| `output/can_slim_fixed_top3_trade_ledger.csv` | 完整逐笔交易账本 |
| `output/can_slim_selected_data_audit_fixed_top3.json` | 实际入选股票的数据审计 |

## 4. 为什么数据不再直接放进 Git

项目数据包含数千只股票的历史价格、PIT 财务数据和数百个股票池快照：

- `cleaned_stocks_data`：约 450 MB；
- `stocks_list_dir`：约 327 MB；
- `his_data`：约 553 MB；
- 未压缩合计超过 1.3 GB。

如果继续直接提交 CSV：

- 每次更新都会产生大量 Git diff；
- 历史 Blob 永远保留，仓库会持续膨胀；
- clone、fetch、review 和回滚越来越慢；
- 将 CSV 改成 `.gz` 后直接提交也不能解决版本膨胀，因为 Git 很难对压缩二进制做增量存储。

因此项目采用：

- **Git 仓库**：代码、测试、中文说明、小型正式回测结果；
- **GitHub Release**：完整版本化数据包；
- **SHA-256**：保证下载内容与发布内容一致。

## 5. 数据包内容

当前数据 Release：

- Tag：`data-2026-07-24`
- 文件：`quant_stocks_data_2026-07-24.tar.zst`
- 元数据：`data_release/latest.json`

数据包包含：

```text
cleaned_stocks_data/
├── price/                         # 清洗后的股票价格
└── financial/                     # EPS、季度财务和覆盖率

stocks_list_dir/
└── nasdaq/
    ├── nasdaq_300M.csv            # 当前候选股票池
    ├── nasdaq_index.csv           # Nasdaq Composite
    ├── snapshots/                 # PIT 历史股票池快照
    ├── corporate_actions.csv      # 公司行动
    ├── security_identity.csv      # 证券类型识别
    └── terminal_returns.csv       # 退市/终止收益

his_data/
├── us/nasdaq/                     # 原始历史数据，供旧初始化流程恢复
└── us/sp500/vix.csv               # 旧策略使用的 VIX 历史
```

`output/` 不放入数据包。正式回测结果保存在 Git，日常推荐和本机审计结果由运行环境自行积累。

## 6. 首次安装

### 6.1 克隆代码

```bash
git clone https://github.com/superdiaodiao/quant_stocks.git
cd quant_stocks
```

旧 Git 历史曾包含大量数据。如果只需要当前版本，可以使用浅克隆减少下载：

```bash
git clone --depth 1 https://github.com/superdiaodiao/quant_stocks.git
cd quant_stocks
```

### 6.2 创建 Python 环境

推荐 Python 3.12：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt pytest
```

也可以使用普通 `venv`：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install pytest
```

### 6.3 安装 zstd

macOS：

```bash
brew install zstd
```

Ubuntu/Debian：

```bash
sudo apt-get update
sudo apt-get install -y zstd
```

### 6.4 下载并恢复数据

```bash
chmod +x scripts/download_data_release.sh
scripts/download_data_release.sh
```

脚本会：

1. 读取 `data_release/latest.json`；
2. 从 GitHub Release 下载数据包；
3. 计算 SHA-256；
4. 与发布清单比较；
5. 检查压缩包中是否存在绝对路径或 `..` 路径；
6. 通过后解压到项目根目录。

如果本地已经存在数据，脚本默认拒绝覆盖。确认需要恢复指定快照时：

```bash
scripts/download_data_release.sh --force
```

## 7. 验证安装

### 7.1 运行测试

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
```

### 7.2 审计数据

将日期替换为数据包对应的最近完整数据日期：

```bash
PYTHONPATH=. .venv/bin/python -m src.research.data_audit \
  --as-of 2026-07-24
```

审计重点检查：

- Nasdaq 指数日期不能来自未来；
- Nasdaq 指数足够新；
- 当前股票池价格覆盖率至少 95%；
- 新鲜财务覆盖率至少 90%；
- 股票价格中没有超出审计日期的未来行；
- EPS 数据包含 PIT 所需字段。

### 7.3 重跑固定策略

```bash
PYTHONPATH=. .venv/bin/python -m src.research.can_slim_validation
```

运行后应重新生成第 3 节列出的正式结果。重点检查：

- 年度结果是否与 README 接近；
- `passed_every_historical_year` 是否符合预期；
- 交易成本压力测试是否通过；
- 逐笔账本净值能否与每日回测净值对账。

## 8. 日常数据更新和推荐

唯一日常入口：

```bash
PYTHONPATH=. .venv/bin/python main.py --workers 8
```

等价命令：

```bash
PYTHONPATH=. .venv/bin/python -m src.research.daily_pipeline --workers 8
```

流程顺序：

1. 更新当前 Nasdaq 股票池、价格和 Nasdaq Composite；
2. 更新 PIT EPS；
3. 更新 SEC 季度利润和收入；
4. 执行数据完整性审计；
5. 使用冻结的 `can-slim-top3-v1` 生成当日状态；
6. 写入本地 recommendation history；
7. 更新 shadow evaluation；
8. 不提交真实订单。

若只想使用已经下载的数据复现，不访问外部数据源：

```bash
PYTHONPATH=. .venv/bin/python -m src.research.daily_pipeline --skip-update
```

还可以分别跳过：

```bash
PYTHONPATH=. .venv/bin/python -m src.research.daily_pipeline \
  --skip-market-update

PYTHONPATH=. .venv/bin/python -m src.research.daily_pipeline \
  --skip-financial-update
```

无论是否跳过更新，数据审计都不会被绕过。

## 9. 每日推荐输出

正式模型目录：

```text
output/daily/can-slim-top3-v1/
```

可能包含：

- `recommendations_YYYY-MM-DD.csv`
- `recommendations_YYYY-MM-DD.json`
- `recommendation_history.csv`
- `shadow_evaluation.json`

主要动作：

| action | 含义 |
|---|---|
| `BUY_NEXT_CLOSE` | 当前为调仓执行窗口，计划在下一交易收盘执行 |
| `HOLD_POSITION` | 本月组合已确定，继续持有 |
| `HOLD_CASH` | 市场过滤关闭或没有合格股票 |

对于同一个 `signal_date + model_version`：

- 第一次真实记录的股票集合和权重被冻结；
- 后续每日运行不会换成当天重新计算的新组合；
- 只刷新价格、动作、运行时间和模式；
- 这样回测与未来正式运行使用的是同一套逻辑。

## 10. Walk-forward 与过拟合控制

仓库保留 chronological walk-forward 研究能力，但它目前不能自动修改正式参数。

原则：

- 参数选择只能使用当时已经发生的数据；
- 不允许随机打乱年份，因为时间序列的市场状态和信息可得性有顺序；
- 年度参数更新必须只使用上一年末以前的数据；
- adaptive 策略必须用与固定策略完全相同的固定股数、成本和成交时点模拟；
- adaptive 只有在 chronological walk-forward 中稳定超过固定策略后才能晋升；
- 当前 Top 3 参数仍然保持冻结。

随机打乱年份不能解决前视偏差，也不能替代真正的时间顺序验证。可以使用 bootstrap
估计不确定性，但正式验证仍必须保持时间顺序。

## 11. 创建新的数据 Release

完成数据更新和审计后，生成数据包：

```bash
chmod +x scripts/create_data_release.sh
scripts/create_data_release.sh 2026-07-24
```

输出：

```text
dist/
├── quant_stocks_data_2026-07-24.tar.zst
├── quant_stocks_data_2026-07-24.tar.zst.sha256
└── quant_stocks_data_2026-07-24.json
```

建议发布步骤：

1. 数据审计 PASS；
2. 重跑固定策略；
3. 全量测试通过；
4. 生成 `.tar.zst`；
5. 再次核对 SHA-256；
6. 更新 `data_release/latest.json`；
7. 提交并推送代码；
8. 创建 GitHub Release 并上传三个文件。

使用 GitHub CLI：

```bash
gh release create data-2026-07-24 \
  dist/quant_stocks_data_2026-07-24.tar.zst \
  dist/quant_stocks_data_2026-07-24.tar.zst.sha256 \
  dist/quant_stocks_data_2026-07-24.json \
  --repo superdiaodiao/quant_stocks \
  --title "quant_stocks 数据快照 2026-07-24" \
  --notes "用于复现 can-slim-top3-v1 的完整数据快照。"
```

不要把 `dist/` 中的压缩包提交进 Git。

## 12. 定时任务

`schedule_run.sh` 使用项目内 `.venv/bin/python` 执行每日流程：

```bash
chmod 755 schedule_run.sh
```

北京时间 09:00 的 crontab 示例：

```cron
0 9 * * 2-6 /data/quant_stocks/schedule_run.sh
```

对应星期二至星期六早上，处理前一晚已经结束的美股交易日。

注意：

- 定时任务必须运行在持久化目录；
- `output/daily/` 不能每天被删除，否则前向记录会丢失；
- 更新或审计失败时，当天不生成新推荐；
- 当前 Codex 自动任务仍处于暂停状态；
- 项目不会自动提交 Git，也不会自动向券商下单。

## 13. 目录说明

```text
src/
├── financial/
│   ├── eps.py
│   └── quarterly_fundamentals.py
├── io/
│   ├── financial_update.py
│   ├── fundamentals_update.py
│   ├── nasdaq_update.py
│   ├── corporate_actions.py
│   ├── security_identity.py
│   └── terminal_returns.py
└── research/
    ├── can_slim.py
    ├── can_slim_validation.py
    ├── can_slim_walk_forward.py
    ├── can_slim_daily_recommendations.py
    ├── can_slim_data_audit.py
    ├── daily_pipeline.py
    ├── data_audit.py
    ├── data_quality.py
    ├── panel_data.py
    ├── shadow_evaluation.py
    ├── production_gate.py
    └── universe_history.py

scripts/
├── create_data_release.sh
└── download_data_release.sh

data_release/
└── latest.json
```

## 14. 常见问题

### 为什么不是每天选一批新股票？

正式参数是月度选股。每日运行的作用是更新数据、显示当前状态、积累前向记录，并保持
月度组合不变。若每天重新选股，就与已经验证的策略不同。

### 可以持有少于 3 只吗？

可以。3 是上限，不是必须买满的数量。

### 为什么不和 QQQ 比较？

项目目标明确要求与 Nasdaq Composite 比较。QQQ 是 ETF，成分和费用结构不同，
当前正式代码、数据审计和报告都不使用 QQQ。

### 历史 6/6 跑赢是否代表可以直接生产？

不代表。历史结果受到参数研究过程影响。真正生产需要冻结后的前向证据。

### 是否支持 IBKR？

当前不支持自动下单。输出文件可以供人工检查，但项目没有券商提交逻辑。

### 数据包下载失败怎么办？

检查：

```bash
gh auth status
zstd --version
cat data_release/latest.json
```

也可以在 GitHub Release 页面手工下载，然后使用 `.sha256` 文件校验。

### 为什么完整 clone 仍然可能较大？

旧 Git 历史曾经提交过原始价格文件。当前提交移出数据只能阻止仓库继续膨胀，
不会自动清除已经存在的历史 Blob。只需要当前代码时，请使用 `git clone --depth 1`。
若未来决定彻底清理历史，需要单独执行历史重写和强制推送；这不属于普通发布流程。

## 15. 风险说明

- 本项目仅用于研究和辅助决策；
- 回测不代表未来表现；
- 集中持有 3 只股票可能产生较大波动和回撤；
- 财务数据、公司行动、退市收益和历史股票池仍可能存在供应商误差；
- 模拟使用收盘成交，不保证实盘一定能以相同价格成交；
- 交易成本压力测试不能覆盖所有冲击成本和流动性风险；
- 在真实使用前，应继续进行至少一年的冻结后 shadow 验证并人工复核。
