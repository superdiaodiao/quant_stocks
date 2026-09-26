# quant_stocks

Nasdaq 股票的月度选股研究项目。现在运行的是 **v50r3**：每月选 5 只股票，只做前瞻
观察记录（影子账本），**不连接券商、不下单**。

> 当前状态（2026-09-26）：r3 协议已由 `v50r3 freeze` 工作流冻结在 `live/v50r3`
> 分支，第一期信号日是 **2026-09-30**（北京时间 10-01 04:30–16:00 之间冻结）。
> 8-31 那期错过，不回填。`release_status=BLOCKED`、`promotion_eligible=false`：
> 历史回放只是训练诊断，只有前瞻账本算数。

## 1. 项目目标

1. 更新 Nasdaq 股票池、价格和财务数据；
2. 用与正式运行完全相同的逻辑做历史回放；
3. 在尽量避免过拟合的前提下，与 Nasdaq Composite 比较；
4. 记录每次调仓的股票、价格、股数、成本、现金和盈亏；
5. 在模型通过真实前瞻验证后，给出可人工检查的股票建议；
6. 项目本身永远不自动下单。

## 2. 当前策略（v50r3）

运行入口是 `scripts/research_v50r3_corrected_v47.py`。选股核心沿用 v42/v50r1 冻结的
规则，r3 只修运行时，不改模型，参数也没有因数据修复重新搜索。

### 2.1 选股规则（按代码执行顺序）

每月最后一个交易日收盘后：

1. **大盘过滤**：Nasdaq Composite 收盘价在 200 日均线之上才持股，否则整月持现金；
2. **股票池**：信号日当天 Nasdaq 选股接口里市值 ≥ 3 亿美元的普通股（剔除 ETF）；
3. **技术筛选**：信号日价格单位下股价 ≥ 10 美元；50 日中位成交额 ≥ 1000 万美元；
   股价在自身 200 日均线之上；63 个交易日涨幅跑赢纳指同期涨幅；
4. **盈利**：最近 4 个连续财季净利润合计 > 0（最新一季首次披露不超过 550 天），
   缺数据按不盈利；
5. **流动性池**：在盈利股票里取 50 日中位成交额最大的 25 只；
6. **排名**：按“63 日涨幅 − 纳指涨幅”从高到低取前 5 只，每只 20%。不足 5 只就少持，
   其余为现金。

数据有疑点时一律失败关闭，不带疑点继续：排名池或持仓遇到未解释的疑似拆股、可能进入
排名池的股票缺信号日收盘价，都会让信号不能冻结（详见[运维手册](docs/v50r3_operations.md)）。

代码位置：`src/research/corrected_stock_policy.py`（技术筛选、流动性池、排名）、
`scripts/research_v50r3_corrected_v47.py`（盈利判断、数据读取）、
`src/research/prospective_replay.py`（成交与止损）。

### 2.2 成交与止损

- 信号在月末收盘后冻结，**下一个交易日收盘价**模拟成交；每月调仓时全部重新调回等权；
- **个股止损 20%**：收盘价 ≤ 参考价 × 80% 时卖出，这部分持现金到下个信号。参考价是
  **最近一次月度调仓那天的收盘价**，每月重置（继续持有的股票也一样），不是最初买入价；
- **组合止损 25%**：组合净值比峰值回撤 25% 时全部清仓；峰值在每次调仓时重置，只在
  持有股票时检查；
- 止损在收盘时发现、**下一个收盘**卖出；与月度调仓同一天时止损优先，不允许卖出后
  立即按原价买回；
- 没有止盈规则；
- 成本按 10/30/50 bps 三种假设计算（50 bps 是压力测试）；主基准是 Nasdaq Composite
  价格收益，QQQ 只作参考。

例：9-30 美股收盘后出信号 → 10-01 收盘模拟买入 → 10 月内每个收盘检查止损、每日估值
→ 10-30 收盘后出下一期信号。

### 2.3 账本

回测和前瞻都是自融资账户：初始资金标准化为 100 万美元，允许小数股，两次调仓之间
权重随价格漂移，成本从现金中扣除。执行价缺失时不用旧价格或未来价格替代；停牌时延后
卖出。前瞻结果只追加、带哈希链，写在 `live/v50r3` 分支的
`output/research_only/v50/corrected_v47_20260924_r3/` 下。

## 3. 运行与运维

**全部在 GitHub Actions 上运行，本机不需要任何定时任务。**

### 3.1 两种运行

| | 做什么 | 什么时候 |
|---|---|---|
| **SIGNAL** | 选出当月 5 只股票并永久冻结 | 月末收盘后 30 分钟起，到下一交易日纽约时间 04:00 止；北京时间夏令时约次日 04:30–16:00（冬令时 05:30–17:00）。Nasdaq 收盘后 4–5 小时才发布数据，实际约北京时间早上 8–9 点开始，GitHub 上约 2.5–3 小时 |
| **MARK** | 按当天收盘价给持仓估值，与纳指、QQQ 比较 | 冻结信号之后的每个交易日 |

错过 SIGNAL 窗口时，错过的日期不回填；调度器在之后第一个交易日的窗口里**补跑**一次，
账本记为 `CATCH_UP`。

### 3.2 GitHub 工作流

| 工作流 | 触发 | 作用 |
|---|---|---|
| `v50r3 scheduler` | 每小时 :17、:47（UTC）+ 手动 | 读 `live/v50r3`，判断该做什么，到期就跑 SIGNAL/MARK 并推送账本，结果发到 issue #2 |
| `v50r3 window waker` | 工作日 19:07/21:07/23:07 UTC | GitHub 定时任务常晚几个小时，它在 SIGNAL 窗口里等 Nasdaq 发布数据，一发布就立即启动 scheduler |
| `signal watchdog` | 每天 12:15 UTC；每月 28 日到次月 5 日每小时 | 失联报警：窗口错过没补、协议或代码被改、估值落后超过 2 个交易日时变红 |
| `v50r3 record sourced event` | 手动 | **最主要的人工补救入口**：补录拆股、真实涨跌或退市终值 |
| `v50r3 test email` | 手动 | 测试通知能否发出 |
| `v50r3 rehearsal` | 手动 | 完整演练一次 SIGNAL，不写账本 |
| `v50r3 freeze` | 手动（已执行过，会拒绝再跑） | 一次性冻结 |
| `Portable tests` | 每次 push/PR | 跑不依赖数据包的测试，复验 r1/r2/r3 协议 |

旧的 CAN SLIM 日常流程（`workflow_run_script.yml`）已删除。v50r2 已被 r3
接替，从未产生过信号。

### 3.3 出问题怎么办

1. **看通知**：每次 SIGNAL/MARK 的结果（包括失败、被停牌卡住、错过窗口）都会在 issue #2
   「v50r3 观察记录（自动）」下发评论，并写明要不要你操作；配置了 `MAIL_USERNAME` /
   `MAIL_PASSWORD` 仓库 secrets 时同时发邮件。
2. **疑似拆股卡住**：日志里 `unexplained_split_like_moves` 列出股票和日期。查到公开来源后，
   在 Actions 手动运行 `v50r3 record sourced event`（类型 `SPLIT` / `MARKET_MOVE` /
   `TERMINAL_RETURN`），它推送后会自动重新启动 scheduler。
3. **只是停牌**：不要登记，等恢复交易即可。
4. **看账本**：

   ```bash
   git fetch origin live/v50r3
   git show origin/live/v50r3:output/research_only/v50/corrected_v47_20260924_r3/prospective_ledger.jsonl
   ```

规矩：不要手工改 `live/v50r3`，不要在它和 master 之间互相合并。冻结后发现运行时缺陷，
照 r2 → r3 的做法另起 r4。完整说明（通知规则、本机备用运行、补跑、估值规则、退出码）
见 **[docs/v50r3_operations.md](docs/v50r3_operations.md)**。

## 4. 目录与文档

| 位置 | 内容 |
|---|---|
| `scripts/research_v50r3_*.py`、`src/research/prospective_*.py` | 当前运行代码 |
| `scripts/research_v*.py` | v2–v51 研究序列（历史，大多不再运行） |
| `src/io/` | Nasdaq、SEC 等数据下载与修复 |
| `tests/` | 测试；`tests/data_dependent_test_files.txt` 列出需要数据包的测试 |
| [docs/v50r3_operations.md](docs/v50r3_operations.md) | v50r3 运维手册 |
| [docs/data_release.md](docs/data_release.md) | 数据为什么不放进 Git、数据包内容、发布新数据 Release、研究缓存冷归档 |
| [docs/data_audit.md](docs/data_audit.md) | 数据审计、重跑固定策略、SEC 研究证据包 |
| [docs/history/v50_review_and_runtime_fixes.md](docs/history/v50_review_and_runtime_fixes.md) | v50 回放数字、2026-09-25 审查结论、r2/r3 修了什么 |
| [docs/history/legacy_results.md](docs/history/legacy_results.md) | 旧 Top3、adaptive、v2–v6 等历史结果 |
| [docs/history/legacy_can_slim_pipeline.md](docs/history/legacy_can_slim_pipeline.md) | 已停用的 CAN SLIM 日常流水线、Walk-forward、旧定时任务 |
| [docs/history/HANDOFF_2026-08-24.md](docs/history/HANDOFF_2026-08-24.md) | 2026-08-24 交接记录 |

历史回放的要点（详见上表）：2020–2025 修正回放在 50 bps 成本下复合 +317.7%，纳指
+159.0%，最大回撤 32.8%；但本模型是在同一段数据上比较约 130 个组合后选中的，超额收益
集中在 2024 年的三个月，统一收益口径后 2023 年跑输。它只是待前瞻检验的假设。

## 5. 首次安装

### 5.1 克隆代码

```bash
git clone https://github.com/superdiaodiao/quant_stocks.git
cd quant_stocks
```

旧 Git 历史曾包含大量数据。如果只需要当前版本，可以使用浅克隆减少下载：

```bash
git clone --depth 1 https://github.com/superdiaodiao/quant_stocks.git
cd quant_stocks
```

### 5.2 创建 Python 环境

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

### 5.3 安装 zstd

macOS：

```bash
brew install zstd
```

Ubuntu/Debian：

```bash
sudo apt-get update
sudo apt-get install -y zstd
```

### 5.4 下载并恢复数据

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

### 5.5 运行测试

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
```

没有下载数据包时，`tests/data_dependent_test_files.txt` 里列出的测试会失败；CI 只跑其余的
测试（见 `.github/workflows/tests.yml`）。


数据包下载、校验和发布新 Release 的细节见 [docs/data_release.md](docs/data_release.md)。

## 6. 研究原则

- 每次研究先冻结规则，再看新数据；时间经过本身不等于通过；
- 历史年份一律是训练诊断，不计入胜场；
- 数据有疑点时失败关闭，不猜测、不回填。

## 7. 常见问题

### 为什么不是每天选一批新股票？

正式参数是月度选股。每日运行的作用是更新数据、显示当前状态、积累前向记录，并保持
月度组合不变。若每天重新选股，就与已经验证的策略不同。

### 可以持有少于 5 只吗？

可以。5 是上限，不是必须买满的数量。

### 为什么不和 QQQ 比较？

主基准仍是 Nasdaq Composite。QQQ 是 ETF，成分、分红和费用结构不同，只作为
次要总收益参考，不替代主基准，也不会进入股票持仓。

### 历史多数年份跑赢是否代表可以直接生产？

不代表。历史结果受到参数研究过程影响；v50r1 修复后仍把 2020-2025 标为训练诊断，
2026 年 1-7 月标为研究者已暴露的复用诊断。只有 2026-09-30 起按冻结协议逐日留下的
完整前瞻记录，才计入正式比较（8-31 的窗口已错过，不回填）。

### 是否支持 IBKR？

当前不支持自动下单。输出文件可以供人工检查，但项目没有券商提交逻辑。

以下 2026-08-03 paper-readiness 内容仅是早期 Top3 分支的历史快照：当时冻结后 shadow 只有
`2026-07-31` 的待执行记录，已完成前向周期为 0，ledger 仍是本地未外部锚定；
静态 PIT universe、成本压力和正式财务来源审计也未全部通过。当前 10 万美元、
10 bps 的整股参考计划位于
`output/manual_position_plan_2026-07-31.csv`（摘要带推荐 CSV、策略和 data
manifest SHA），它是 `REFERENCE_ONLY_NOT_AN_ORDER`，不构成 paper 成交或 IBKR
授权证据。只有外部锚定的冻结后前向记录、完整 PIT 数据审计和人工复核完成后，
才应重新评估准入；不能用历史回放、reference plan 或 SEC raw cache 覆盖这些门槛。

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

## 8. 风险说明

- 本项目仅用于研究和辅助决策；
- 回测不代表未来表现；
- 集中持有最多 5 只股票可能产生较大波动和回撤；
- 财务数据、公司行动、退市收益和历史股票池仍可能存在供应商误差；
- 模拟使用收盘成交，不保证实盘一定能以相同价格成交；
- 交易成本压力测试不能覆盖所有冲击成本和流动性风险；
- 在真实使用前，必须完成冻结协议规定的前瞻观察节点并人工复核；时间经过本身不等于通过。
