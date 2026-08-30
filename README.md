# quant_stocks

这是一个面向 Nasdaq 股票的量化研究与每日选股项目。当前唯一处于前瞻观察入口的
候选是 `v50r1-corrected-v47-sourced-actions`：固定使用月度 Top 5 股票选择器、
20% 建仓损失止损和 25% 组合回撤止损，并与 **Nasdaq Composite** 比较。
早期 `can-slim-top3-v1` 及 v14-v49 产物继续保留为历史审计和对照证据，但不再是
当前运行入口。

> 当前状态：`SHADOW / 暂不允许真实下单`。
>
> v50r1 已修复公司行动、最低价格时点、止损与月度调仓同日优先级、迟到信号数据包和
> 本地绝对路径绑定问题；修正后的 2020-2025 回放通过预先声明的诊断门槛。但这些年份
> 仍是训练诊断，官方前瞻胜场为 0，`release_status=BLOCKED`、
> `promotion_eligible=false`。代码不连接券商，也不向 IBKR 自动提交订单。

## 1. 项目目标

本项目希望完成一条可以长期维护的完整链路：

1. 更新 Nasdaq 股票池、价格和财务数据；
2. 使用与每日推荐完全相同的逻辑进行历史回放；
3. 在尽量避免过拟合的前提下，与 Nasdaq Composite 比较；
4. 记录每次调仓的股票、价格、股数、成本、现金和盈亏；
5. 在模型通过真实前向验证后，每日给出可人工检查的股票建议；
6. 项目本身永远不自动下单。

## 2. 当前冻结候选

### 2.1 选股规则

当前冻结入口是 `scripts/research_v50_corrected_v47.py`，选择与风险参数均未因本次
数据修复重新搜索：

- 股票池：信号日当时已知的 Nasdaq 普通股历史快照；
- 先按 63 个交易日相对 Nasdaq 动量和 50 日中位成交额排序，取流动性池前 25；
- 只保留当时可确认盈利的股票，每月最多等权持有 5 只；
- 最低名义股价 10 美元只使用信号日当时的价格单位判断，禁止未来拆股信息回写；
- 收益、动量、均线和止损只使用有来源确认的公司行动连续价格；
- 已复核为市场下跌的整数倍价格跳变保持原始跌幅，不再被启发式拆股推断抹掉；
- 排名池或目标持仓遇到未解决公司行动时失败关闭，不带疑点继续计算；
- 20% 建仓损失止损与 25% 组合回撤止损；风险退出为现金，直到下一次冻结月度目标；
- 止损先于同日月度调仓，同一收盘触发止损时禁止卖出后立即原价重入；
- 市场过滤：Nasdaq Composite 位于 200 日均线允许的风险状态；
- 主基准：Nasdaq Composite 价格收益；QQQ 仅作次要总收益参考。

### 2.2 信号与成交时点

- 每月最后一个已完成 Nasdaq 交易日收盘后产生信号；
- 在下一个共同交易日收盘价模拟成交；
- 后续交易日不重新选股，但每个收盘都检查已冻结的风险规则；
- 下一次月度信号到来时重新计算目标组合；
- 研究诊断使用 10/30/50 bps 三个总成本假设；50 bps 是压力测试，不是 IBKR 费率；
- 不进行自动券商下单。

例如：

- 6 月 30 日美股收盘后得到信号；
- 7 月 1 日美股收盘模拟买入；
- 7 月其余时间保持该批股票，直到下一个月度调仓。

北京时间通常在美股交易日晚上进入下一交易日凌晨。程序应在前一美股交易日完成后运行，
再根据是否到达执行日输出 `BUY_NEXT_CLOSE`、`HOLD_POSITION` 或 `HOLD_CASH`。

### 2.3 数据与持仓约束

回测采用自融资账户：

- 初始资金标准化为 1,000,000 美元；
- 支持小数股，因此账户规模不会改变收益率；
- 调仓后持有固定股数；
- 两次调仓之间权重会随股票价格自然漂移；
- 不存在未记录、未收费的每日恒定权重再平衡；
- 每次买入、增持、减持和卖出都会进入逐笔账本；
- 交易成本从现金和组合净值中真实扣除。
- 月度执行价缺失时失败关闭，不以旧价格或未来价格替代；
- 停牌导致的日度退出价缺失时延后卖出，不按陈旧价格成交；
- SIGNAL 数据包必须在信号日同一 UTC 日期创建，禁止事后补录未来股票池；
- 冻结输入使用仓库相对路径和 SHA256，可在不同 checkout 中复核。

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

## 3. 当前历史结果（回顾性样本内，仅作附录）

v50r1 修正回放保持了 v30/v47 的 348 个冻结月度目标，目标差异数为 0；在 50bps
压力成本下，2020-2025 六个训练年分别超越 Nasdaq `+5.3068pp`、`+9.8594pp`、
`+20.6767pp`、`+0.0109pp`、`+7.3515pp`、`+4.7116pp`。复合策略收益为
`+317.6699%`，Nasdaq 为 `+159.0330%`；最大回撤为 `32.8123%`。2023 年优势只有
约 1 个基点，不能称为强稳健性。这些结果全部是修正后的训练诊断，不计入正式前瞻
胜场，也不解除 `BLOCKED`。

可审计产物位于
`output/research_only/v50/corrected_v47_20260831_r1/`；当前协议状态为
`WAITING_FOR_FIRST_PROSPECTIVE_SIGNAL`。第一笔允许进入账本的信号日期是
`2026-08-31`，必须在该日 Nasdaq 收盘后且 UTC 日期仍为 2026-08-31 时创建。
换算为北京时间/新加坡时间，实际窗口是 **2026-09-01 04:00–08:00**，不是
2026-08-31 凌晨。

当前操作入口：

```bash
PYTHONPATH=. .venv/bin/python scripts/research_v50_corrected_v47.py status

# 仅在北京时间/新加坡时间 2026-09-01 04:00–08:00 执行：
PYTHONPATH=. .venv/bin/python scripts/research_v50_corrected_v47.py \
  stage-bundle --as-of 2026-08-31 --purpose SIGNAL
PYTHONPATH=. .venv/bin/python scripts/research_v50_corrected_v47.py \
  freeze-signal --bundle \
  output/research_only/v50/corrected_v47_20260831_r1/bundles/2026-08-31_signal

# 后续估值日分别创建 MARK 包并追加；YYYY-MM-DD 必须是实际估值日：
PYTHONPATH=. .venv/bin/python scripts/research_v50_corrected_v47.py \
  stage-bundle --as-of YYYY-MM-DD --purpose MARK
PYTHONPATH=. .venv/bin/python scripts/research_v50_corrected_v47.py \
  append-mark --bundle \
  output/research_only/v50/corrected_v47_20260831_r1/bundles/YYYY-MM-DD_mark
```

如果错过 SIGNAL 的同日 UTC 窗口，程序会拒绝事后补建；不得通过修改日期或复用未来
股票池绕过。这一限制是为了让前瞻证据可复核，不影响历史训练数据继续用于诊断。

下面的 Top3 和 adaptive 结果是旧分支历史附录，不是当前 v50r1 运行结果。

修正财务数据和价格处理后，固定股数、自融资、完整计费的 2021–2026
历史回放如下。由于历史候选股票价格覆盖不完整，这些数字只能视为研究诊断，
不能视为已经通过的数据完整性验证：

| 年份 | 策略收益 | Nasdaq Composite | 超额收益 |
|---|---:|---:|---:|
| 2021 | 44.64% | 21.39% | 23.25% |
| 2022 | -18.51% | -33.10% | 14.59% |
| 2023 | 38.75% | 43.42% | -4.67% |
| 2024 | 286.56% | 28.64% | 257.92% |
| 2025 | 13.28% | 20.36% | -7.07% |
| 2026（截至 7 月 17 日） | 36.07% | 9.80% | 26.27% |

这张表是回顾性历史回放，不是样本外证据。下面的 chronological walk-forward
按时间顺序冻结参数、逐年只用过去数据选择参数，但它评估的是单独的 adaptive
研究分支 `can-slim-v2`，不是正式冻结的 `can-slim-top3-v1`：该分支按年更新、
使用 Top 5、且不使用季度财务。因此它是 adaptive 分支的样本外压力测试，不能
替代或验证冻结 Top 3 的前向资格。

| 测试年 | walk-forward 策略收益 | Nasdaq Composite | 样本外超额收益 |
|---|---:|---:|---:|
| 2022 | -16.90% | -33.10% | 16.20% |
| 2023 | -19.99% | 43.42% | -63.42% |
| 2024 | 88.17% | 28.64% | 59.53% |
| 2025 | 24.43% | 20.36% | 4.07% |
| 2026（截至 7 月 17 日） | 4.65% | 9.80% | -5.15% |

该 adaptive walk-forward 共 5 个测试年、3/5 年跑赢，仍为 `BLOCKED`；完整逐年
结果见 `output/can_slim_walk_forward.csv` 和
`output/can_slim_walk_forward_summary.json`。正式冻结 Top 3 的唯一绩效晋级证据
仍是 2026-07-18 之后、逐周期可追溯的 shadow forward 记录；固定策略历史表和
adaptive walk-forward 都只能作为诊断，不能替代它。

需要正确理解这些数字：

- 这是历史研究结果，不等于未来收益承诺；
- 参数曾参考历史表现，不能把全部历史年份称为完全样本外；
- 机器报告明确标记
  `historical_evidence_class = RETROSPECTIVE_IN_SAMPLE`；历史年度胜率、最差
  年度超额和 block bootstrap 置信区间全部是信息项，不参与生产放行；
- 严格计算为 4/6 年跑赢；2023 年落后 Nasdaq 约 4.67 个百分点，2025 年落后
  约 7.07 个百分点；
- 2026-07-31 的数据版本交叉回放确认，原 `data-2026-07-24` Release
  仍会精确复现旧的 6/6 结果；只把季度财务换成当前版本，即使价格、指数和
  股票池全部保留 Release 版本，也会变成当前 4/6。反过来，在当前其他数据上
  只换回 Release 季度财务仍为 6/6。因此这不是冻结参数或策略代码回归，
  而是季度财务覆盖扩充后的实质性结果变化；
- Release 季度财务有 267,638 行、1,386 个 ticker；当前正式文件有 296,957 行、
  1,473 个 ticker，增加 87 个 ticker 和 30,663 个事实键，同时有 1,375 个
  Release 事实键经重解析后不再保留。2026-07-31 这次交叉回放截取的 raw cache
  当时仅覆盖当前可投资股票池 222/1,618（13.7%）；这是历史截面，不代表当前缓存。
  当前受控 raw-only 状态已覆盖 required universe 的 1,604/1,620，另有 16 个官方不可用
  代码；`cache_resolution_coverage=1.0` 且 ordinary unresolved=0（官方不可用负向证据
  已绑定 manifest）。
- 现有 copy snapshot 已通过逐文件验证，基于它的离线 `--reparse-cache full --dry-run`
  也已通过缓存覆盖和配方门槛，但年度/季度与冻结正式 CSV 的 exact/content comparison
  仍不一致；因此不能用“coverage gate 已通过”替代正式文件可精确复现，正式 CSV SHA 保持冻结，
  任何发布新版本前都必须先审计 direct/derived 差异并重新生成覆盖证明；
- 不应把“当前 raw cache 已闭环”误解成“冻结正式财务可从头精确复现”。以不可变
  snapshot `manifest-600157c4ca4d1fc7` 和正式文件绑定 scope 做的最新离线比较见
  `output/data_provenance/companyfacts_rebuild_dry_runs/manifest-600157c4ca4d1fc7-current.json`：
  年度文件仍有 197 个 ticker、季度文件有 951 个 ticker 的事实内容变化（比较忽略
  `fetched_at`）。只读恢复审计
  `output/data_provenance/companyfacts_historical_source_recovery.json` 也没有在本机 Git
  不可达对象或公开 GitHub Release/Actions inventory 中找到原始 raw payload。该负结果
  不排除私有备份或其他获授权存储；但在取得该历史快照前，当前完整 cache 只能支持新的
  受控数据版本，不能作为冻结 CSV 的精确来源，`BLOCKED` 不得因此解除；
- 2021–2024 的历史股票池成员价格覆盖明显不完整，2025 年还存在一批
  止于 2025-07-09 的历史文件，缺失竞争者可能改变横截面排名；
- 按当前 PIT 财务库复查，最初只有 `PRPL`、`SENEB`、`SEZL` 三只缺价格股票
  曾通过利润和收入门槛；`PRPL`、`SENEB` 已用 Nasdaq 官方历史补齐，`SEZL`
  经 SEC 来源确认在相关信号前仅上市约 11 个交易日，不满足 253 日回看要求。
  当前可观察数据内没有尚未解决的强候选，但其余股票大多是价格与财务同时缺失，
  仍不能据此认定它们在现实中不合格；
- 当前每日数据审计 `PASS` 只证明当前推荐数据可用，不代表历史回测数据完整；
- 报告把代码证据拆成两类：`strategy_code` 从固定 selector 和股票池回溯两个
  根模块出发，用 AST 递归解析全部项目内 `src.*` 导入，自动哈希实际参与筛选、
  收益模拟、财务快照、证券身份、退市收益、股票池过滤和共同交易日逻辑的闭包；
  `source_code` 哈希整个 `src/`。修改 SEC 下载器或报告格式会改变后者，但不会
  冒充冻结模型逻辑变化；固定参数仍逐字段保存在 `model_snapshots`，参数变化不能
  借此隐藏。新增策略本地依赖会自动进入指纹，不再依靠人工同步清单。股票类型过滤
  已从网络更新器提取为纯模块，避免下载代码与策略指纹耦合；
- 历史 PIT 阻塞的最小范围现已单独披露：67 个正式月度信号中，只有
  `2023-05-31` 的成员快照超过预设 40 天上限（使用 2023-04-18 快照，
  年龄 43 天）；应用时点证券类型连续性并补回 `ESOA` 的 195 行固定版本历史
  价格，并按有 SEC 与重叠区间证据的复权因子补回 `NEGG` 后，价格缺失代码共
  2795 个，其中
  7 个拥有当时可见的 PIT
  季度财务。逐信号审计共得到 69 条可观察记录：`DGICB`、`IKT`、`KELYB`、
  `LANC`、`LBTYB`、`VYNE` 的 68 条记录均至少未通过正利润、利润增长或收入
  增长之一；唯一通过全部财务门槛的 `SEZL` 已确认当时仅上市 11 个交易日，
  不满足 253 日回看要求。另有 2788 个代码在所有相关缺价信号上都没有可用的
  PIT 财务；`IKT`、`VYNE` 则属于混合情况，在部分缺价信号有财务、另一些信号
  没有。此前的 3258 是“至少一次同时缺财务”的非互斥口径，不能称为“其余”。
  对这 2788 个代码仍不能证明当时必然不合格，因此继续保持
  BLOCKED；这也说明补数重点不仅是价格，还包括候选股票当时可见的财务记录；
- 历史股票池来源可用
  `python -m src.research.universe_snapshot_provenance` 离线复核。当前 364 份
  快照中 357 份固定到公开 Git commit，7 份固定到 Common Crawl 的精确
  WARC 字节区间；67 个正式月度信号实际使用的 62 份快照全部具有不可变来源。
  该检查只证明来源可重建，不会把较晚提交倒推成更早可用，也不会消除
  `2023-05-31` 的 43 天快照年龄阻塞；
- 历史来源有时会把完整名称截成短名称。证券一旦在某份当时可知快照中明确标为
  ETF、ETN、权证、优先股等，后续只有模糊短名称时会继续按非普通股排除；只有
  后续明确出现 `Common Stock`、普通/存托股等标签才从该日开始重新纳入，不会
  用未来类型改写过去。该规则相对逐快照孤立判断在 67 个信号中识别出 617 个
  曾被明确识别、后来名称变模糊的非普通证券，使缺价集合减少 466 个；例如
  `GLDI`、`SLVO` 的
  `Exchange Traded Notes/ETNs`。正式 Top 3 和年度收益未因此改变；
- `output/historical_pit_gap_priorities.csv` 将上述 2788 个代码按受影响的正式
  月度信号次数降序排列，并区分“完全没有价格文件”“价格历史开始过晚”和“价格
  历史提前结束”，以及“信号附近缺失但后来又恢复”的内部空洞。排序不使用后来
  收益、后来财务结果或是否最终入选，避免为了补数又引入事后偏差；价格文件首尾
  日期只用于判断需要补头部、尾部、内部区间还是整段数据；
- “没有可用 PIT 财务增长快照”不再等同于“完全没有原始财务”。逐信号拆分后，
  2785 个代码至少有一次确实没有当时原始利润/收入事实；另有 5 个代码存在更容易
  修复的局部情况：`NESR`、`CORZ`、`LLYVA`、`LLYVK` 有原始事实但季度链不足，
  `OPI` 能计算增长但超过 550 天新鲜度上限。CSV 同时保留
  “影响次数排名”和“修复就绪排名”；后者优先已有部分价格和原始财务的代码，
  只表示补数快慢，不表示投资重要性；
- 历史审计现在还逐个信号输出缺失指标分类：`NO_REVENUE_FACT`、
  `NO_NET_INCOME_FACT` 和 `NO_REVENUE_AND_NET_INCOME_FACTS`。这只描述当时
  可见的原始 Company Facts 指标缺口，不把缺收入误报成季度链不足，也不改变正式
  财务 CSV 或放行状态；诊断结果位于 `missing_financial_metric_gap_*` 字段；
- 历史信号成员的价格覆盖与财务增长覆盖是两个独立门槛。每个当时股票池成员都必须
  在信号日拥有 selector 可直接使用的 PIT 增长快照，即同时具备当时已公开的原始
  利润/收入事实、足够的季度链，并且快照不超过正式 550 天年龄上限。补齐某只股票
  的价格不会把它的财务缺口自动视为解决，也不会让生产门槛虚假改善。当前 67 个
  正式信号中，最差信号日只有约 21.76% 的股票池成员具备可用增长快照；全期共有
  4395 个代码至少在一个信号日缺少该快照，因此
  `signal_member_financials_complete=false`。按互斥的“代码 × 信号日”观测分解，
  157889 条缺口中有 138863 条（约 88.0%）在当时完全没有原始 PIT 利润/收入
  事实，17733 条（约 11.2%）已有事实但季度链不足，1293 条（约 0.8%）可以计算
  但超过 550 天上限。同一代码在不同信号日可能跨类别，因此代码并集数量不能直接
  相加；
- 对生产门槛而言，还会严格复用 selector 的非财务横截面，只审计在风险开启信号上
  已满足价格、253 日历史、`$10M` 流动性、相对成交量和 52 周高点条件的潜在候选。
52 个风险开启信号共有 14594 条潜在候选观测，其中 2143 条缺可用财务，覆盖率
约 85.32%，最差信号约 73.33%，涉及 367 个代码。这个口径不会忽略缺价格股票：
  历史成员价格完整性仍是独立前置门槛；它只是避免要求本来不可能通过技术条件的
  低价、低流动性股票也必须拥有完整财务；
- 剩余 367 个代码按修复路径拆为：78 个同时已有利润和收入但季度链仍不完整、
  50 个只有净利润而没有已解析收入事实、79 个只有外国发行人 20-F/40-F 年报并
  需要新的季度来源、22 个只有年报或季度标签尚未映射、138 个没有已解析 SEC
  财务。该分类进入补数优先级 CSV，避免把需要新数据源的问题误当成解析器小修；
- `ESOA` 的缺失价格已从 `ARKMD/stooq` 的固定 commit
  `6ae7c9b04dc8b98612d1ee9594baa64362b4ade1` 补入 195 行，覆盖
  2022-03-24 至 2022-12-30；精确 source path、URL 和 commit 记录在
  `output/data_provenance/stooq_github_import.json`。导入只填缺失日期，不覆盖
  已有 Nasdaq 行；
- `NEGG` 从同一固定 commit 补入 3080 行，覆盖 2010-04-21 至
  2022-12-30。SEC 2025-03-14 的 6-K 明确披露 20 合 1 并预计
  2025-04-07 起按合并后股数交易；镜像与本地 508 个重叠交易日的价格比中位数
  为 20、成交量比中位数为 0.05，且全部落在各自中位数 ±1% 内。因此导入明确
  使用 `price_factor=20`、`volume_factor=0.05`，成交量按整股四舍五入；边界
  收盘价由 2022-12-30 的 `$26.20` 连续到 2023-01-03 的 `$26.60`。这些因子、
  SEC URL、重叠验证说明和固定 source URL 均写入 provenance。导入器现在会在
  写入前自动用重叠日期验证请求的价量因子：默认至少 20 个重叠交易日、因子与
  中位比例误差不超过 1%，且至少 95% 的重叠日位于中位比例 ±1% 内；任一条件
  不满足都会拒绝导入，不能只靠人工备注绕过；
- 通过 SEC 同 CIK 更名证据和固定 Stooq 历史文件，研究性切分补齐了
  `PMDI→PMD`、`PHLT→PFMT`、`ENGC→ENG`、`MTVA→NRBO`、`PAMT→PTSI`、
  `PVLA→PIRS`、`HURA→KTRA`、`SCNX→MEDS`、`IRD→OCUP`、`PENG→SGH` 和
  `GRCE→ACST` 的可用历史段。Yahoo PMDI 的 adjusted close 与 Stooq 在
  992 个重叠交易日中 99.9% 位于 1% 内；PMD 的 2024-12-13 至 12-31
  尾部也按重叠期比例验证后补入。每条边界、CIK、固定 commit、payload
  SHA 和导入行数记录在 `output/data_provenance/` 的 identity/alias
  provenance 文件中。所有这些来源的许可证/市场数据权利仍未验证，故只
  属于 research-only，不能解除 `BLOCKED` 或替代 licensed PIT/终值来源；
- 对一批完全缺少历史文件的退市/OTC 代码，新增了
  `scripts/otc_historical_price_repair.py`。它缓存 OTC Markets 图表所用的
  Edgar Online OHLCV 响应，并用 Yahoo chart 的重叠交易日做独立校验；只有
  至少 20 个非微价重叠日、其中至少 95% 价差在 1% 内时才填入缺失日期，且
  已有日期永不覆盖。该路径补入了 `AFIB`、`AUVI`、`CLVR`、`GBNY`、
  `NEXI`、`NVOS`、`THMO`、`TRVN`、`VINO` 的研究性历史，原始响应、URL、
  hash 和校验结果保存在 `output/data_provenance/otc_historical_price_cache/`
  与 `otc_historical_price_repair_2026-08-04.json`；`CATC` 因 OTC 端点无历史
  且 Yahoo 映射为互惠基金而拒绝。该数据的许可证、PIT 权利及退市终值仍未
  验证，不能解除 `BLOCKED` 或替代 licensed PIT/终值来源；
- `GVP` 另有完整 OTC OHLCV，但 Yahoo 对该已退市代码返回 404，因此只在显式
  `--allow-edgar-only` 下以 `EDGAR_ONLY_UNVERIFIED` 研究状态缓存；它有公司名
  与 SEC CIK 线索，但没有独立价格重叠验证，不能视为正式补数。
- 项目原有 AkShare 新浪美股接口可以覆盖大量近期退市代码。新增
  `scripts/sina_historical_price_repair.py` 将新浪原始响应以 gzip 缓存并记录 payload
  SHA，同时绑定实际使用的 AkShare 解码器文件和解码源码 SHA；默认 dry-run，至少
  20 个重叠交易日且 OHLC 稳定一致后才允许 `--apply`，已有日期永不覆盖，并按
  `historical_data_audit.json` 的最后 PIT 成员日截断。2026-08-08 的全量运行对 254 个
  当时剩余代码中 241 个追加了 24,696 行。后续修复没有普遍降低门槛：拆股代码
  只允许使用截至本地末日、至少 20 日逐字段完全稳定的最长尾段；10--19 日尾段
  还必须由 SEC 唯一 CIK/后继 ticker 证据绑定；3--9 日尾段则必须再有固定 Git
  commit 的 Stooq 镜像，并验证完整本地文件或至少 20 日恒定比例历史重叠。由此
  安全补入 CAEP、CCCX、CGCT、CLRC、CLEU、CYCC、JFBR、KWE、QVCGA、STBX、
  STSS、VAPE、WAI 等尾段。原始缓存和逐代码校验记录在
  `output/data_provenance/sina_historical_price_cache/` 与
  `sina_historical_price_repair_full_2026-08-08.json` 及对应的
  `sina_fixed_mirror_sec_*` / `sina_sec_exact_tail_*` provenance 中；
- 最新审计把挂牌期价格缺口从 279 降至 0。最后三个表面缺口没有生成合成收盘价：
  CLRC 的新浪序列在 PIT 截止日后仍有下一笔成交，OPT 与 SDM 则分别有 SEC 文件
  明确确认持续停牌/交易暂停。上述记录按当前价格文件 SHA、行情响应 SHA 和不可变
  SEC filing SHA 绑定在
  `source_confirmed_non_trading_intervals_2026-08-08.json`，审计同时保留
  `raw_apparent_missing_price_histories_while_still_listed=3` 和解析后的
  `missing_price_histories_while_still_listed=0`；
- 终值审计另加入 40 天观察窗，将本轮统一停在 2026-07-17 等近期日期的行情标记为
  right-censored，而不是误判成退市。2026-08-08 又以历史 Nasdaq 快照公司名补做
  SEC CIK 搜索，将当时 345 个 unresolved 中的唯一 CIK 覆盖到 337 个；submissions、
  8-K/6-K 原文均以 gzip 缓存并绑定 payload SHA。只对文件明确确认的纯现金普通股
  对价或清算分配写入终值，CVR、股票换股、现金/股票选择权和资产分配权全部保留为
  review；另有 37 个同 CIK 后继 ticker 尾段通过 Stooq 至少 20 日重叠及连续性门槛后
  补入；多后继证券的 issuer 只有在普通股候选唯一通过、warrant/preferred 候选明确
失败后才应用。成熟的 unresolved terminal candidates 已从 2,292 降至 151，已观察终值为
196；正式策略已选持仓仍为
  `selected_position_terminal_returns_complete=true`。这些改进仍未解决开源行情许可、
  全市场终值仍未闭环，因此整体保持 research-only / `BLOCKED`；
- 2026-08-08 从 Stooq 官方历史数据库手工下载的 `U.S. daily TXT` 批量归档以
  SHA-256 `3b818755da09c4754f5758b5140df869fbd68cfcd16c3c7634331212f70d1fb0`
  固定；页面明确限制为个人用途，因此原始 ZIP 和抽取行不提交、不再分发，只用于
  本地 research-only 审计。逐 member SHA/CRC、重叠校验和导入前后价格文件 SHA
  记录在 `stooq_unresolved_terminal_*_2026-08-08.json`。其中 15 个 ticker 的
  4,898 行尾段通过至少 20 个重叠交易日及连续性门槛后补入；新浪缓存和本轮联网
  原始响应又分别安全补入 215 个 ticker / 2,242 行和 101 个 ticker / 1,464 行。
  BLBX、BRKL、DMN、PHLT 等含内部空洞或长时间跳跃的候选没有导入；新增
  `scripts/eastmoney_historical_price_repair.py` 将原有东方财富 `stock_us_hist`
  路径改为同时探测 105/106/107 市场、缓存原始 JSON、绑定 AkShare 实现 SHA，
  并要求 ticker 身份及本地 OHLC 重叠同时通过。当前端点仍被服务端直接断开，
  `eastmoney_historical_price_probe_2026-08-09.json` 已保留失败 checkpoint，未把它
  当作成功证据。2026-08-09 又按安装版 AkShare 的 105/106/107 规则探测 17 个
  SEC transition 原 ticker，51 次请求均由服务端以 empty response 断开；逐 ticker
  checkpoint 保存在 `eastmoney_sec_transition_historical_17_dry_run_2026-08-09.json`，
  没有写入价格文件；
- 2026-08-09 对上述 Stooq 官方归档重新扫描当前 196 个 unresolved 原 ticker：187 个
  不在归档，8 个与本地历史完全重合但没有新行，`GLBL` 因交叉验证不通过被拒绝；
  因此未伪造任何同 ticker 补数。随后将 SEC transition probe 与归档中的后继 ticker
  联结，确认 `CCCX→INFQ` 属于同一 CIK `0002007825`，155 个重叠交易日的 OHLC
  和成交量完全一致，且首个后继交易日距本地末日仅 4 天；据此补入 105 行直到
  2026-07-17，审计 unresolved 从 196 降至 195。归档 SHA、member SHA/CRC、SEC
  payload SHA、追加行 SHA 和价格文件前后 SHA 均保存在
  `sec_stooq_alias_cccx_infq_applied_2026-08-09.json`；
- 对当前 unresolved 全集完成新的 SEC transition probe 后，又确认并补入
  `ABVE→ABVEF`、`CYCC→BGMS`、`DYNX→DYNC`、`HSON→STRR` 四条连续后继尾段，
  分别增加 26、212、223、215 行。四组均绑定唯一 CIK，并有 144–1,178 个交易日
  的稳定历史重叠；同一发行人的 preferred、unit 或 warrant 候选要么没有 Stooq
  数据，要么交叉验证失败，因此只显式选择普通股。固定截止日审计的 unresolved
  由 195 进一步降至 191；全量 SEC probe、dry-run 候选和四次应用证据均保存在
  `output/data_provenance/`，原始 Stooq ZIP 仍不进入 Git；
- 新浪 SEC alias 导入器同步支持多后继 dry-run，并要求 apply 时显式指定唯一普通股。
  全量候选验证后只采用 `JFBR→NEXR` 与 `NYMT→ADAM`；同 CIK 的 `NEXRW` warrant
  及 `ADAMG/H/I/L/M/N/O/Z` 优先股或债务类别均未混入。两条无历史重叠链仅在 SEC
  唯一 CIK、旧 ticker 末日后 1–7 天连续开始时通过，共补入 75 和 219 行，原始
  新浪 payload、AkShare decoder、SEC probe、追加行及文件前后 SHA 全部绑定；固定
  截止日 unresolved 由 191 降至 189；
- 对跨 CIK 的 SPAC business combination 单独绑定完成 filing：`SKGR` 每股明确换取
  一股 Webull Class A ordinary share，且 SEC 确认 `BULL` 于旧 ticker 末日次日
  2025-04-11 开始交易。新增导入器默认 dry-run，要求 filing 关键句、有效日、换股
  比例及 Stooq member 全部一致，按 1:1 接入 317 行至固定分析截止日，而不是错误地
  把连续持仓压成一次终值；
- 同一跨 CIK 导入器支持后继 ticker 在重组前已有行情、但仅从 SEC 生效日开始截取。
  Liberty SiriusXM completion filing 明确 `LSXMA/LSXMB/LSXMK` 每股换取 `0.8375`
  股 New Sirius common stock，旧三类于 2024-09-09 退市，因此从次日按
  `0.8375 × SIRI` 分别接入 464 行；没有错误接到同 CIK 但属于 Formula One 的
  `FWONA/FWONK`。此外，EXPI filing 明确 ticker 于 2026-05-08 改为 `AGNT`；本地文件
  已含到 05-11，故只从下一交易日 05-12 接入 46 行，避免重复日期。四条连续持仓链
  令 unresolved 从 183 降至 179；
- Nasdaq 历史快照明确把 `IACVV` 标为 `Common Stock Ex-distribution
  When-Issued`。证券类型过滤器原先只识别带空格的 `When Issued`，漏掉连字符写法，
  从而把这个分拆期间的临时交易代码误列为普通股终值缺口。过滤规则现同时接受
  `When Issued` 与 `When-Issued`，并有快照回归测试；`IACVV` 被正确归入 non-common
  exclusion，unresolved 从 179 降至 178，没有错误接到多年后的 SEC 当前 ticker
  `PPLI`；
- 2026-08-09 新增 `scripts/sec_sina_alias_price_import.py`，把已有 SEC 唯一 CIK/
  唯一后继 ticker 证据与项目原有 AkShare 新浪行情链路连接起来。存在重叠时仍要求
  至少 20 个稳定 OHLC 交易日；正式换代码没有重叠日时，只接受 SEC 搜索与 submissions
  CIK 完全一致、旧代码末日到新代码首日相隔 1--7 天的连续尾段，且不做推测性复权。
  原始新浪响应、AkShare 解码源码、SEC probe、审计输入和追加行均以 SHA 固定，默认
  dry-run、逐 ticker checkpoint。共为 14 个历史代码补入后继行情，其中 12 个延续至
  分析截止日并从 unresolved 移除；`CTCX→XAGE` 与 `MULN→BINI` 虽补齐第一层改名，
  后继行情自身仍提前终止，继续保留为 unresolved，未伪造终值；
- 同日为 `scripts/sec_otc_alias_price_import.py` 增加 terminal-tail 模式，将 Stooq
  明确缺失的同 CIK OTC 后继代码交给 Edgar Online 行情交叉验证。30 个后继尾段均
  通过至少 20 个 OHLC 重叠交易日后补入并延续到分析截止日；其中包括 XAGE 的 OTC
  尾段，因此 `CTCX→XAGE` 的第二层也完成闭环。来源权限仍未验证，整体继续保持
  research-only / `BLOCKED`；
- 2026-08-09 依据 SEC 最终 proration results 补齐 HONE 和 OPOF：HONE 全体股份
  最终为 84.99% 换取每股 0.765 EBC、15.01% 取得每股 $12；OPOF 最终为 60%
  换取每股 1.14 TOWN、40% 取得每股 $41。计算使用全体最终分配比例而不是假定
  个别股东选择，SEC payload SHA、后继首个交易日价格及价格文件 SHA 均由离线
  测试复算；同批还补齐 MTTR 的固定混合对价（每股 $2.75 加 0.03552 股 CSGP）；
- 同批从已缓存的完成 8-K 中补齐两笔纯现金普通股并购：IAS 每股 `$10.30`、ICCH
  每股 `$23.50`。CVR、divested-asset right、换股及现金/股票选择权的相似金额命中
  继续拒绝，未将证券面值或现金下限误作完整终值；
- APTO 的完成申报确认每股现金对价为 `C$2.41`；使用加拿大央行 `FXCADUSD`
  2026-07-02（加拿大假日后的首个观测日）官方日均汇率 `0.7052`，换算终值为
  `$1.699532`。SEC filing 与央行 JSON 均保存原始 gzip、URL 和 payload SHA，并由
  离线测试复算；全量 328 份候选事件申报现已完成缓存。ZYXI 的 Plan Exhibit 明确
  旧股仍可能取得全额偿债后的剩余出售款，因此继续保留 unresolved，没有写零；
- 完整 filing 扫描另确认三笔固定换股：GRDI 每股换 `0.069593885 CLSK`、PFC
  每股换 `0.80 WSBC`、CYTH 每股换 `0.3525 RFL`。前两者使用本地后继首个交易日
  价格及文件 SHA；RFL 使用新浪原始响应，并额外绑定 SEC 唯一 CIK/ticker probe、
  AkShare 解码结果与 gzip/payload SHA。含 CVR 或最终比例未定的相似交易仍未采用；
- 同一离线扫描又补齐四笔固定换股：BFIN 每股换 `0.480 FFBC`、ESSA 每股换
  `0.8547 CCNE`、FFWM 每股换 `0.16083 FSUN`、FLIC 每股换 `0.5175 CNOB`。
  后继价格全部使用并购生效后的首个交易日，并绑定本地价格文件 SHA；
- 最新离线 filing 扫描补齐 `EXAI→RXRX`（`0.7729`）、`ZEUS→RYI`
  （`1.7105`）及 `BRY→CRC`（`0.0718`）三笔固定换股。RXRX 使用本地正式行情；
  RYI 使用绑定原始 gzip 和独立 SEC CIK probe 的新浪行情；CRC 使用刚下载的 Stooq
  官方归档 member SHA/CRC。三条 terminal value 分别为 `$4.668316`、
  `$46.234815`、`$3.168534`，逐条重放测试通过；与 SKGR 连续尾段合计令
  unresolved 从 189 降至 185、observed 从 172 增至 175；
- SEC 现金句式扫描补上“converted into the right to receive cash in an amount
  equal to `$X`”且没有 `per share` 后缀的完成交易，并把上下文窗口扩大到覆盖较长的
  common-stock exclusion 条款。由此确认 `AVDX` 为纯现金 `$10.00`、`PHLT` 为纯现金
  `$7.75`，两者均无 CVR、资产收益权或后续分配；逐条 payload SHA 和原文关键句重放
  后，unresolved 从 185 降至 183、observed 从 175 增至 177。`WBA` 等虽有固定现金
  部分但仍附带 divested-asset proceed right，继续保留 unresolved；
- Smart Share Global（`EM`）私有化条款确认每 ADS 毛对价为 `$1.25`，同时明确
  ADS cancellation fee 为 `$0.05`，因此采用可复算的净终值 `$1.20`，而不是把毛额
  直接当作股东所得。条款 filing payload SHA 为
  `26b5dcba0f5509b6584eb91ba237fdfabc5a7b0f17b180293257aebe7dcfe124`，并另以
  2026-04-30 完成交易的 6-K 及其 gzip 缓存绑定完成事实；相对最后收盘 `$1.19`
  的终值收益为 `0.840336%`。固定截止日审计由此更新为 unresolved 174、observed 178；
- CureVac（`CVAC`）最终要约修订明确换股比例为每股 `0.05363 BNTX ADS`，并说明
  该比例基于截至 2025-11-25 的十日 VWAP；随后要约按原定 2025-12-03 到期、没有
  延期，因此没有使用初始材料中的近似比例。完成 6-K 另确认 2026-01-06 开盘前停止
  CVAC 交易；以同日 BNTX 收盘 `$98.09` 计算终值 `$5.2605667`，相对 CVAC 最后
  收盘 `$4.66` 的收益为 `12.887697%`。最终比例 filing、完成 filing、BNTX 行情文件
  和各自 SHA 均由离线测试重放；固定截止日审计进一步更新为 unresolved 173、observed 179；
- 网络恢复后重新获取新浪 `ALTM` 原始序列，与本地 256 个重叠交易日通过全历史
  OHLC/成交量尺度校验，并补入 323 行至 2025-03-05；这修复了此前把 2025-01-10
  误当作最后成交日的问题。SEC 完成 8-K 明确 3 月 6 日开盘前停牌、每股取得
  `$5.85` 现金；以真实最后收盘 `$5.84` 计算终值收益 `0.171233%`。新浪原始 gzip、
  payload SHA、导入报告、补后价格文件和 SEC filing 均绑定，审计更新为 unresolved 172、
  observed 180；
- 对剩余代码重新刷新新浪与 OTC 后继行情后，只采用真正延长末日的候选：新浪先把
  `BCAN`、`CCCM`、`CGBS` 分别延长 17、16、6 个交易日；其余 17 个新浪候选仅能
  填内部旧空洞，未冒充终值闭环。OTC 路径又确认 `ABP→ABPO`、`ADN→ADNH`、
  `CSCI→CSCIF`、`LNW→LNWO` 四条唯一 CIK 连续后继链，并延续至固定截止日；
  `BCAN→FMTOF` 因中间相隔 85 天而明确拒绝。四条有效链使 unresolved 进一步降至 168；
- 修复 SEC triage 只检查“最后价格日之后”文件造成的盲点后，逐一检查最后交易日当天
  的完成 8-K。由此补齐七笔无 CVR、无后续分配权的纯现金终值：`TSVT $5.00`、
  `AMED $101.00`、`DENN $6.25`、`EMKR $3.10`、`LGTY $14.30`、`DALN $16.50`
  及 `GIFI $12.00`；同时确认 `HMNF` 每股固定换取 `1.25 ALRS`，按下一交易日
  ALRS 收盘计算。所有完成文件、原始 payload SHA、后继价格 SHA 和公式均由离线
  测试重放；`FFNW` 仍有最终清算分配、`THTX` 仍有 CVR，未把首笔现金当作完整终值。
  固定截止日审计更新为 unresolved 160、observed 188；
- 两笔固定换股继续闭环：`HTBK` 在长期停牌后每股换取 `0.65 CVBF`，使用 SEC 完成
  文件、CVBF 唯一 CIK probe 与新浪 2026-08-03 首个完成后交易日价格计算；`THCP`
  完成 Coincheck 业务合并后每股 1:1 换取 `CNCK`，SEC 文件明确一股换一股，CNCK
  probe 与新浪 2024-12-11 首日行情分别固定身份和价格。Stooq 的 CNCK 归档从次日
  才开始，因此没有用次日价格掩盖缺失的首日。审计更新为 unresolved 158、observed 190；
- `CCCM` 的完成文件核对表明它并未在 2025-07-30 产生终值，而是在次日把普通股
  ticker 直接改为 `BRR`。按文件明确的生效日和 1:1 身份转换，从 Stooq 官方归档
  接入 242 行至固定截止日，并绑定 filing phrase、member SHA/CRC、追加行 SHA 及
  文件前后 SHA；审计因此更新为 unresolved 157、observed 仍为 190。`CGBS` 虽明确
  转 OTC，但当前 Edgar/Sina/Stooq 均没有可验证的后续序列；`CLOE` 后续合并终止并
  计划清算但尚未披露最终赎回额；`OMGA` 为 Chapter 11 资产出售，三者继续保留；
- 三笔银行合并继续闭环：`PVBC` 的最终全体 proration 明确为 50% 股份取得
  `0.691 NBBK`、50% 股份取得 `$13.00`，按首个完成后交易日 NBBK 收盘计算聚合
  终值；`FFIC` 每股固定换取 `0.85 OCFC`，`LNKB` 每股固定换取 `0.1350 BHRB`。
  后两者的本地价格文件各含两个 SEC 明确停牌后的重复伪尾行，现已删除，并以完成
  文件、后继价格文件、修复后原 ticker 文件和逐文件 SHA 离线绑定。固定截止日审计
  因此更新为 unresolved 154、observed 193；
- 随后又闭环三笔银行合并：`MBCN` 每股固定换取 `2.6 FMNB`，`FSFG` 每股固定
  换取 `0.85 FRME`；`PBBK` 的代理书明确整体 allocation 为 80% 股份换取
  `0.785 NWFL`、20% 股份取得 `$19.75`，完成文件确认交易完成及选择/分配机制。
  三笔均按首个完成后交易日的后继股票收盘计算，原始 SEC 文件以 gzip 缓存，payload、
  后继价格文件及公式 SHA 均由离线测试重放。固定截止日审计更新为 unresolved 151、
  observed 196；
- SEC 8-K 明确 CommScope 于 2026-01-14 更名为 Vistance Networks，普通股继续在
  Nasdaq 交易并由 `COMM` 改为 `VISN`。本地两代码有 1,516 个重叠交易日，收盘价
  全部在 1% 内且价格尺度一致，`VISN` 又从生效日无缝延续；因此将其登记为
  `issuer_rename`，不再把 `COMM` 在 2026-01-13 结束误作退市终值。SEC 原始缓存、
  payload、两份价格文件 SHA 和重叠统计保存在
  `sec_security_identity_comm_visn_2026-08-09.json`，未写合成价格或终值；固定截止日
  审计由 unresolved 151 降至 150，observed 仍为 196；
- Golden Entertainment（`GDEN`）完成文件明确每股先取得已宣告并支付的 `$2.75`
  现金分配，随后固定换取 `0.902` 股 VICI。以完成日 2026-04-30 的 Stooq 官方
  VICI 收盘 `$29.20` 计算，终值为 `$29.0884`，相对 GDEN 最后收盘 `$28.55`
  的收益为 `1.885814%`。SEC payload、GDEN 行情、Stooq 官方归档及 VICI member
  SHA/CRC 和公式均由离线测试重放；没有遗漏现金分配，也没有使用选择权或近似比例；
  固定截止日审计更新为 unresolved 149、observed 197；
- FARM 的完成申报明确普通股每股仅取得 `$1.29` 现金，已补入终值；同批审查的
  ACLX、ADVM、CKPT、DRRX、ELEV、HLVX、MRSN、NURO 含 CVR，SNCY 为现金加股票，
  STER 为现金/股票选择权及 proration，均继续拒绝。`otc_historical_price_repair.py`
  也改为默认 dry-run、必须显式 `--apply`；对 43 个同 ticker 候选的新浪探测没有
  找到可延长末日的尾段，因此没有为填内部旧缺口而偏离终值目标；
- 全文现金条款扫描进一步补齐 HSII 每股 `$59.00` 和 VMEO 每股 `$7.85` 两笔纯现金
  并购；并补齐 PCH 的 `$0.61 + 1.8185 RYN`、SNCY 的 `$4.10 + 0.1557 ALGT`
  固定混合对价。RYN 行情同样绑定新浪原始响应、SEC 唯一 ticker/CIK 与 payload SHA；
- 同日继续从 SEC 完成文件补齐 FYBR、IROQ、ISPO、ZIMV 四个固定现金并购终值。
  FRZA 每股换取 0.611666275 VEEE，但 VEEE 后续实施 1-for-10 与 1-for-37
  反向拆股；两份 SEC 原文分别缓存并绑定 SHA 后，按累计 370 倍计算与复权行情
  等价的换股比例。SWKH 的最终文件虽然披露选择权、proration 和汇总金额，但汇总
  股票数仍使用 `approximately`，因此尚未用近似总股数写入终值；
- 继续补齐 CFB→BUSE、SLRN→ALMS、CVGW→AVO 的固定换股/混合对价。
  KIRK 先按 SEC 同一 CIK 的改名 KIRK→TBHC 处理：Stooq 两代码有 1,146 个
  重叠交易日且 OHLC/成交量均 100% 通过，共补入 171 行至 2026-04-01；随后再按
  SEC 固定比例 0.1993 股 BBBY 计算并购终值。AKYA 的完成文件仍保留对现金和
  股票组件进行上/下调整的条款，因此尚未采用披露的初始数值；
- 破产终值只在 SEC 同时闭环后写入 `-100%`：MODV 的计划于 2025-12-29
  生效且全部旧权益明确“for no consideration”注销；IRBT 的 8-K 证明计划于
  2026-01-23 生效及旧普通股注销，正式 Plan Exhibit 又明确 Class 8 Existing
  Equity Interests “without any distribution”。ME、MRIN 等仍有 trust interest 或
  后续按比例分配，不按零值处理；
- Wag! Group（`PET`）的 8-K 同时确认重组计划于 2025-09-01 实质完成、全部旧普通股
  和其他旧权益当日取消并灭失，且重组后 100% 股权另行发行给原担保债权人。因此按
  与 MODV/IRBT 相同标准写入 `-100%`，而不是仅凭停牌或计划草案推断；SEC payload、
  最后价格文件 SHA、最后交易日及终值表均由离线测试重放；
- Zynex（`ZYXI`）的 2026-03-26 8-K 确认重组计划当日生效、旧普通股及其他旧权益
  全部取消并灭失，重组后的 1,000 股新普通股另行发行给 Plan Sponsor；因此同样按
  `-100%` 处理。与仍可取得 trust interest 或剩余现金分配的 ME/MRIN 不同，该记录
  没有旧股东后续分配权；该结论由生效 8-K、固定 SEC payload SHA、最后价格文件
  SHA 和离线重放测试共同约束；
- SEC 当前 ticker 与唯一 CIK 证据又确认 `ISRL→ISRLF`、`PMD→PMDI`、
  `BTM→BTMCQ` 三条 OTC 后继行情。前两条分别追加 154 与 385 行，并从 terminal
  unresolved 移除；BTM 的本地 2026-05-26 末行与上一交易日 OHLCV 完全重复，
  BTMCQ 同日有 443,349 股真实成交，且此前 64 个重叠交易日 OHLC 100% 通过，
  因此只在显式 `--replace-carried-terminal-row` 下替换该单行并追加至固定截止日。
  BTM 仍无注销/并购/清算终值证据，故不能因行情已延长而误报闭环。三条记录均绑定
  SEC CIK、原始 gzip、payload/追加行/最终文件 SHA；固定截止日审计当前为
  `observed_terminal_returns=199`、`unresolved_terminal_returns=145`；
- 后续价格身份链又补齐 `VAPE→BNC` 与 `VMCA→VMCAF`：前者由 SEC 唯一 CIK
  和 Stooq 853 个完全匹配的重叠交易日绑定，追加 238 行；后者在三个 OTC
  候选中只有普通股 VMCAF 通过最近 252 日 OHLCV 1:1 校验，追加 340 行。
  `HCVI→NAMM` 则使用项目原有 Nasdaq 公共历史 API 生成固定截止日快照；API
  按当前代码回标的历史与本地 HCVI 有 424 个重叠交易日，OHLC 和有效成交量均
  100% 通过校验，SEC 完成 8-K 又明确旧普通股每股换取一股 PubCo 普通股并于
  2025-06-06 以 NAMM 开始交易，因此从 2025-04-04 至 2026-07-17 追加 322 行。
  Nasdaq 查询 URL、规范化响应帧 SHA、快照文件 SHA、SEC payload SHA、追加帧
  SHA 及文件前后 SHA 均写入 `output/data_provenance/`，可离线重放；固定截止日
  当时审计为 `observed_terminal_returns=199`、`unresolved_terminal_returns=141`。
  这些均为持续交易的证券身份链，不虚构并购终值，也不增加 observed terminal
  return；市场数据权利仍为 research-only，项目继续保持 `BLOCKED`；
- `GOEV` 的本地行情原先提前停在 2025-01-10。项目既有 Stooq 格式归档中的
  `GOEVQ` 与其有 1,377 个重叠交易日，OHLC 全部在 1% 内、成交量 95.86%
  在 5% 内，并继续给出 2025-01-13 至 01-28 的 11 个真实交易日；SEC
  2025-01-24 的 8-K 明确普通股将从 01-29 开盘起停牌。该尾段现已补入并绑定
  归档文件 SHA、重叠帧 SHA、SEC 原文 payload SHA、追加帧 SHA及价格文件前后
  SHA。Chapter 7 资产处置诉讼截至 2026-03 仍在进行，因此只修正最后交易日，
  不把破产申请本身直接写成 `-100%` 终值；
- `STER` 的完成 8-K 明确：未作有效公司行动选择或未按期交付选择表的持有人，
  每股默认收到 `$16.73` 现金；`INFN` 的 SEC 合并协议同样明确，未及时收到有效
  Election Form 的股份视为 Cash Election Stock，每股收到 `$6.65`，完成 8-K 又
  确认 2025-02-28 开盘前停牌。项目的历史回测没有模拟或提交持有人选择，因此使用
  这些明示的 non-election 默认结果；没有使用 `STER` 近似的 71%/29% 整体
  proration，也没有使用 `INFN` 近似的 58% 重分配比例。SEC 原始缓存 SHA、最后
  价格文件 SHA、默认处理原文和终值公式保存在
  `sec_default_non_election_terminal_evidence_2026-08-09.json` 并由离线测试重放；
  固定 `2026-07-17` 截止日审计现为 `observed_terminal_returns=201`、
  `unresolved_terminal_returns=139`。这些修复不改变正式年度/季度财务文件，项目仍为
  research-only、`BLOCKED`；
- `STKL` 的新浪原始响应在 SEC 确认交易完成并要求 Nasdaq 于 2026-05-01 17:00 ET
  停牌后，又重复生成了 05-04、05-05 两行与 05-01 完全相同的 OHLCV。两行已删除，
  价格文件恢复为先前按最后 PIT 成员日截断并通过全历史交叉验证的 SHA；完成 8-K
  同时明确每股普通股取得 `$6.50` 现金，因此最后有效收盘 `$6.50` 的终值为 `0.0`。
  新浪原始响应、SEC 原始申报、删除行、修复前后价格 SHA 和法律原文均绑定在
  `stkl_post_completion_duplicate_tail_repair_2026-08-09.json`，并由离线测试重放。
  固定截止日审计更新为 `observed_terminal_returns=202`、
  `unresolved_terminal_returns=138`；FFNW 的 `$22.00` 仅是初次清算分配且仍明确存在
  未定最终分配，THTX 另含不可交易 CVR，因此二者仍不被误写为完整终值；
- `CUTR` 的 SEC 重组支持协议附件包含完整 Chapter 11 计划：Class 7 Existing Common
  Interests 在生效日全部取消、释放并灭失，持有人不取得或保留任何财产或分配。
  Cutera 官方页面又确认公司已于 2025-05-01 完成财务重组并退出 Chapter 11，之后
  作为由新所有人支持的私营公司运营。SEC 计划、发行人完成公告和新浪最后价格原始
  响应全部以 gzip 与 SHA 固定在
  `cutr_zero_equity_terminal_evidence_2026-08-09.json`，离线测试复核最后价格日、
  原文和 `-100%` 公式。固定截止日审计进一步更新为
  `observed_terminal_returns=203`、`unresolved_terminal_returns=137`；
- `PARA` 不是现金终值：完成 8-K 明确，未作现金/股票选择的 Class B 股东先按
  1:1 取得 New Paramount Class B，并继续以 1 股 New Paramount Class B 持有；旧
  `PARA` 于 2025-08-06 收盘停牌，官方 Nasdaq API 的 `PSKY` 行情从次日连续开始。
  因此按冻结回测“不主动提交公司行动选择”的规则，将 237 个 `PSKY` 交易日以
  1:1 接回 `PARA` 至固定截止日，而不是用现金选择权或 proration 推算终值。原文件
  另有一行 2026-08-07 的 `PARA` ticker-reuse 污染，已在接续前删除。SEC payload、
  Nasdaq snapshot frame、污染行、追加帧和价格文件前后 SHA 分别固定在
  `para_ticker_reuse_contamination_repair_2026-08-09.json` 与
  `sec_business_combination_para_psky_applied_2026-08-09.json`；固定截止日审计的
  unresolved 降至 136，2025 年最大内部价格缺口从 9 降至 8。`PARA` 未出现在冻结
  Top 3 trade ledger，因此该修复提高全股票池真实性，但不重写既有正式策略收益；
- 2026-08-09 继续通过新浪全历史重叠和 SEC CIK 绑定的 OTC 后继行情补齐
  `DTCK/EPWK/STBX/BLMZ/BHAT/UOKA` 等真实价格尾段；3--9 日短尾必须同时通过固定
  Git commit 镜像，成交量仅允许逐日及中位比例均在 0.1% 内的拆股舍入误差。
  `AKYA` 则使用收购方 QTRX 后续 10-Q 披露的最终实际结算值 `0.1470` 股 QTRX 加
  `$0.37`，没有使用完成公告中仍可调整的初始 `0.1461/$0.38`。所有原始响应、
  价格文件前后 SHA 和公式均有独立 provenance 与离线测试。固定截止日审计现为
  `observed_terminal_returns=204`、`unresolved_terminal_returns=125`。其中又从 SEC
  同 CIK 搜索结果识别出 `BCAN→FMTO→FMTOF` 的两段历史 ticker 链：新浪 `FMTO`
  从 `BCAN` 末日下一交易日连续开始，OTC `FMTOF` 与 `FMTO` 有 42 个 OHLC 完全
  一致的重叠日；另以 12 个完全一致的近期重叠日接续 `MULN→BINI`。同一历史 display
  alias 方法还以 588/172 个重叠日分别确认 `BGXX→BGXXQ`、`BHIL→BHILQ`。这些行情均追加
  至固定分析截止日，原始响应、SEC 搜索结果及价格文件前后 SHA 已绑定。这些股票均未
  出现在既有冻结交易、候选或信号中，项目仍保持 `BLOCKED`；
- 2026-08-09 raw-only 复核确认 SEC Company Facts 当前股票池已达到
  `cache_resolution_coverage=1.0`：1,620 个 required symbol 中 1,604 个有 payload，
  16 个由 manifest/state 绑定的官方不可用或不可寻址证据闭环，未解决数为 0；命令
  明确报告 `formal_outputs_read=false`、`formal_outputs_written=false`。随后从不可变
  snapshot `manifest-6c8a87fcc71cfcd5` 进行 recipe-bound full rebuild dry-run，发现旧
  full-reparse 路径错误删除了 formal 中实际存在的 `derived_bank_revenue`；修复后候选
  annual 增加 4,085 行，quarterly 增加 22,388 行及 4 个 ticker。当时的候选 CSV 冻结参数
  内存敏感性为 5/6（仅 2023 未跑赢；2025 改为跑赢），但该结论后来被 Q1 `fp` 误标
  防护重放推翻（见下文），不能再作为完整 Company Facts 会改善年度胜负的证据。197 个 annual ticker 和
  917 个 quarterly ticker 仍有内容差异，因此该结果只作为
  `companyfacts_complete_cache_candidate_sensitivity_2026-08-09.json` 研究证据，未覆盖
  正式 CSV、未改写正式 4/6 artifact，release 继续 `BLOCKED`；
- 随后的 candidate 差异审计发现，部分 SEC 10-Q 会把季度长度事实错误标成 `fp=FY`；
  旧 parser 因此漏掉 BSY 等可直接从冻结 payload 重放的季度。修复后新 recipe SHA 为
  `d8aa5bdc29058a76cbd114d6ded52d0b653ef88214d45355bc28921d47fd031f`，quarterly
  candidate 从 319,345 增至 319,500 行。再利用旧不可变 snapshot 的 release-selection
  行级证明，只恢复 8 条 `derived_proven` 且当前 candidate 完全没有对应
  ticker/fiscal-period/metric 的事实；41 条公式未证明的旧派生事实保持排除。最终
  layered quarterly 为 319,508 行，冻结参数内存回放仍为 5/6、只输 2023，2025
  策略/Nasdaq 分别为 36.406%/20.358%。证据见
  `companyfacts_layered_candidate_sensitivity_2026-08-09.json`；正式财务 CSV 和旧
  validation artifact 仍未改写，release 继续 `BLOCKED`；
- 继续审计发现同一 52/53 周季度有时会在 Company Facts 中同时出现相邻的
  fiscal-end 坐标（例如 AVAV 的 `2021-01-30/31`），旧 Q4 residual 逻辑会把它们
  误计为两个季度并挤掉真实 Q1。新 recipe 仅在 metric 和 value 完全一致且日期相差
  不超过 7 天时归并坐标；完整 snapshot 离线重建后 quarterly 从 319,500 增至
  319,604 行，严格公式证明增加 122 条。再从新 release-selection 中只恢复 EXEL、
  TLRY 两条当前 parser 仍缺失但公式已证明的旧 Q4，layered quarterly 为 319,606 行。
  相对 formal 的 exact PIT key 缺口从 175 降至 87，完全没有 candidate
  fiscal-period/metric 的缺口从 40 降至 35；冻结参数回放仍为 5/6、只输 2023，说明
  本轮提高了历史财务可重放性但没有人为优化策略结果。证据见
  `companyfacts_near_duplicate_quarter_end_sensitivity_2026-08-09.json`；正式财务文件
  SHA 和正式 4/6 artifact 均保持不变；
- 继续逐事实回放 DAVE 后发现，SEC payload 同时包含正确的 2025-Q1 revenue
  `107.979M`，以及一条把同一数值错误标为 `fp=Q1`、却覆盖 2024 年九个月的事实。
  旧 parser 把后者当 YTD 并减去真实 2024-H1，虚构出 DAVE 2024-Q3 revenue
  `-45.768M`。parser 现拒绝 duration 超过 135 天的 `fp=Q1` 事实参与 Q2/Q3
  YTD 派生；基于同一不可变 snapshot 的新 recipe
  `6f0998be33d325e5b673d26f9d96fd0ec556afdf923fa4fbcc2ac0634be43531`
  重建后，该虚假行消失。固定参数、当前其他研究输入不变的内存回放中，candidate
  与 reference 均为 5/6，2025 收益都为 22.847%，且 2025 没有任何 Top 3 选择变化。
  因而上述 36.406% 以及 SOFI/COMM 替换 DAVE/TTMI 的“提升”是 parser 假象，旧的
  complete/layered/near-duplicate candidate 5/6 改善解读全部废止。当前研究输入是在
  正式 validation 后继续修复过价格和终值的状态；这里的 5/6 不会覆盖、也没有重新
  验证 2026-07-31 的正式 4/6。SHA 绑定证据见
  `companyfacts_q1_fp_guard_candidate_sensitivity_2026-08-09.json`，release 继续
  `BLOCKED`；
- 同一 Q1-guard candidate 又通过完整历史 readiness 重放：2021–2026 所有“财务
  筛选已通过但信号日缺价格”的记录均已得到可复核分类。SEZL 在 2023-08-31 尚不足
  253 个交易日；PPBI 在 2025-09-30 信号前已完成交易；APLS 的 SEC 8-K 明确记载
  tender 于 2026-05-13 到期、merger 于 05-14 生效，因此它在 05-29 已不可买。
  APLS 的 `$41 + CVR up to $4` 终值仍未猜测或写入，但其终止交易日期已用原始 SEC
  payload SHA `52bac6231c8fc70058ee30d25934f70ad5c4886fcea4cf346eca60fcc79ad4d6`
  独立绑定。候选/正式审计的
  `unresolved_observable_potential_competitor_symbols` 现均为空；11 组季度值冲突的
  顺序敏感性也没有改变任何 ticker-signal 的财务资格。证据见
  `companyfacts_q1_fp_guard_candidate_readiness_2026-08-09.json`。这证明当前未完成项
  没有已观察到的 Top 3 竞争者，不等于广义数据全量完整：仍有 105 个未知退市终值，
  `observed_delisting_returns_complete=false`，release 继续 `BLOCKED`；
- 7 月 31 日 4/6 历史锚点与当前研究回放 5/6 还使用了不同的历史价格版本。
  `2025-10-31` 的锚点名单为 `CRDO|TTMI|WDC`，当前研究名单为
  `COMM|CRDO|WDC`，不能再表述为“2025 Top 3 没有变化”。关闭 SEC 已证实的
  `COMM→VISN` 同发行人更名连续性后，当前回放仍为 5/6、2025 收益仍为
  22.847%、该日名单也不变，因此名单差异不是更名复制逻辑造成；除 VIRT 外，
  所有当年入选 ticker 的本地价格文件均在
  2026-08-02 被整段刷新；当前聚合 price SHA 为 `d0694673...`，不能把当前 22.847%
  直接写回正式 13.285% artifact。为判断新版价格是否只是供应商漂移，使用 SHA
  `3b818755...` 的 Stooq 2026-08-08 日线包独立交叉验证 26 个当年入选 ticker：25 个
  在 2021-01-01 至 2026-07-17 的全部重叠 close 均为 100% 一致，且没有待补行；唯一
  未通过严格全 OHLC 门槛的 VIRT，其 close 仍 100% 一致，差异来自少量 open/low，
  且本地 VIRT 文件没有参与 8 月 2 日刷新。因此当前 5/6 是“新版价格得到独立支持后
  的研究结果”，不应再追求把它逐分钱还原成旧报表；它仍不是冻结实现的正式
  validation。证据见 `stooq_2025_selected_price_cross_validation_2026-08-09.json`
  和 `issuer_rename_sensitivity_2026-08-10.json`；
- 对唯一陈旧信号 `2023-05-31` 进一步做了边界影响诊断：分别使用
  2023-04-18 快照、下一份 2023-06-16 快照以及两者并集，合格股票和 Top 3
  完全一致；后一快照新增 28 只股票，其中没有一只通过当日正式筛选。即使把所有
  有历史价格的代码都放入故意过宽的压力池，Top 3 仍不变。该结果说明已观察到的
  股票池变化没有改变本次选择，但后一快照不是当时可知数据，不能据此解除 PIT
  阻塞或把 43 天快照事后认定为合格；
- 对空档期来源又做了可复核搜索：同源 GitHub 路径在 2023-04-19 至 05-31
  没有新提交；GitHub 当前索引返回的 100 个 `nasdaqlisted.txt` 和 14 个
  `nasdaqlisted.csv` 路径中，只有 Hazelcast 项目的 `.txt` 在窗口内提交过
  文件，但该样本仅 3171 行、没有内嵌 Creation
  Time、仍包含陈旧代码 `AAAP`，属于 2023-05-19 才导入项目的旧样本，不能把
  commit 时间冒充数据观察时间。Common Crawl `CC-MAIN-2023-23` 抓到了
  Nasdaq Trader 站点及同目录 `options.txt`，但两个正式 listings URL 都没有
  capture；Internet Archive 的 FTP TimeMap 为空，dynamic URL 的 CDX 本次网络
  超时，后者不能解释为“无快照”。完整查询、哈希和拒绝原因记录在
  `output/data_provenance/nasdaq_snapshot_gap_search_2023-05-31.json`；
- 后续找到 `rreichel3/US-Stock-Symbols` 的逐夜 Git 历史；其 README 明确说明
  `nasdaq_full_tickers.json` 是 Nasdaq 原始列表。用五个固定 commit 分别补入
  2022-07-23、2023-05-19、2023-07-08、2023-09-23、2024-04-20 快照，每行均
  绑定仓库、commit、源路径和保守观察日期，manifest 另记录原始 payload 与落盘
  snapshot SHA。股票池审计由 `maximum_snapshot_gap_days=63`、5 个超限空档降为
  `maximum_snapshot_gap_days=34`、0 个超限空档，
  `point_in_time_universe_complete_from_2021=true`。只针对受影响信号做的冻结 Top 3
  选择对照显示 4 个实际受影响调仓日的选股均未变化（第五份快照在下一调仓日前
  已被原有快照覆盖），没有重跑正式 validation。证据见
  `output/data_provenance/us_stock_symbols_snapshot_import_2026-08-08.json` 和
  `output/data_provenance/universe_snapshot_gap_selection_impact_2026-08-08.json`；
- `OPI` 在 2025 年停牌、进入破产重整后，于 2026-06-18 将新重整普通股重新使用
  同一 ticker。SEC 最终 8-K 明确旧普通股全部注销、旧股东没有获得分配且权益价值
  为 0；价格已拆为 `OPI_PRE_REORG` 与新 `OPI`，旧股记录 `-100%` 终值，未用新股
  价格拼接旧股收益。另用 SEC 最终交易文件补入 AKRO、ETNB、VRNA 的保证现金
  终值；AKRO/ETNB 的条件 CVR 在没有实际付款证据前按 0 计，避免前视高估。对应
  provenance 为 `opi_reorganization_identity_split_2026-08-08.json` 和
  `akro_etnb_vrna_terminal_values_2026-08-08.json`；
- SEC 后继 ticker 分流进一步确认 AADI→WHWK、ALRN→RNTX、AGFY→RYM、
  AGH→PUSA 均为同一证券的名称/代码连续变更，已按正式生效日拆分 provider
  历史而非计作退市。新增 2026-02-21 的固定 Nasdaq 快照还暴露了 ABP、BLBX、
  CASI、LVRO、MGIC、NITO 六个此前被月度快照掩盖的挂牌期尾段；均已通过新浪
  原始响应和本地重叠校验补齐。当前挂牌期价格缺口仍为 0，股票池 2021 年以来
  仍满足 40 天 PIT 上限；
- 股票代码身份按有来源的生效日期拆分。SEC 2025-06-30 的 8-K 明确确认
  Lancaster Colony 在 2025-07-01 开盘起由 `LANC` 改为 `MZTI`；价格修复会
  保留并合并既有 `LANC` 历史，只让 `MZTI` 保留生效日后的记录，重复运行不会
  清空旧代码文件。EPS 与季度利润/收入数据使用同一身份映射，映射文件也纳入
  正式输入指纹；这种有来源的同发行人更名会从“退市终止收益缺失”中单列，但
  持仓跨越更名日时，回测与 shadow 账户会把旧代码股数、成本基础和入场日期
  1:1 迁移到新代码，并用旧代码最后有效收盘到新代码生效日收盘计算连续收益；
  该身份事件不产生买卖、换手或交易成本，也不会被当作现金退出；
- SEC 2024-01-23 的 8-K 同时确认了另一种不能连续处理的情形：旧 `CORZ`
  普通股在破产重整生效日被注销且不再具有任何效力，2024-01-24 起上市的是新发行
  普通股，虽然仍使用 `CORZ`。历史股票池因此把旧证券映射为内部身份
  `CORZ_PRE2024`，新证券继续使用 `CORZ`；财务和未来补入的旧价格也会按同一日期
  边界拆分。当前固定镜像和 Nasdaq 接口都没有旧股 2022 年价格，所以该别名仍在
  PIT 补数清单中，不能用 2024 年新股价格倒填或把破产前后视为连续持仓；
- 预设的 30 bps 单边成本压力门槛当前未通过：复合收益仍高于 Nasdaq，
  但只有 4/6 个年份跑赢，低于至少 75% 年份跑赢的晋级规则；
- 这项门槛同时混合了两类问题，机器报告现已拆开披露但不改变预设规则：
  30 bps 下复合收益仍高于 Nasdaq，且相对正式 10 bps 没有新增失败年份；
  门槛失败来自历史年度广度本身只有 4/6，而不是成本从 10 bps 增至 30 bps
  导致新的年度翻负；
- 新增 0 bps 毛收益基线后，2023 年和 2025 年在不计成本时已经分别落后
  Nasdaq 约 2.57 和 5.24 个百分点，因此两年的单边成本盈亏平衡点都记为
  `0 bps`；其余四年在已回放的 `50 bps` 下仍跑赢。该诊断说明这两年不是
  佣金或滑点假设造成的失败，不参与选参或生产放行；
- 逐笔成交容量使用执行日前 50 个交易日的中位日成交额，先把回测成交额按
  当时组合净值归一化，再缩放到假设账户规模。10 万美元账户的历史单笔参与率
  最大约 `0.34%`，没有一笔超过 `1%`；100 万美元账户的中位数约 `0.35%`、
  95 分位约 `2.51%`、最大约 `3.42%`，其中 99/377 笔超过 `1%`，但没有
  超过 `5%`。若要求每笔不超过 `1%`，历史最差交易对应的账户容量约为
  29.2 万美元，10 分位约 53.7 万美元；
- 上述容量使用全日成交额，只是时点安全的流动性代理，不代表收盘竞价一定能以
  同样参与率成交，也没有估算市场冲击。十万美元量级的成交假设相对温和；
  百万美元量级必须通过 paper/真实成交回报重新校准成本，不能直接沿用 10 bps；
- 财务时效敏感性保持为研究诊断，不改冻结的 `550` 天参数：将季度最大年龄收紧
  到 `120` 天后，201 个已选持仓观测中只有 2 个超过 120/150 天，最大年龄为
  336 天（中位数 36 天、P90 87 天）；原始 Top 3 只在 2 个信号变化，实际执行
  Top 3 只在 `2023-01-31` 变化。完整逐信号对照在
  `output/can_slim_fixed_top3_financial_freshness_impact.csv`，不能把“影响有限”
  当成历史 PIT 财务完整性的证明；
- 集中度和路径风险也不能由累计收益掩盖：实现损益中单票最大年度贡献为
  2024 年 `APP` 约 23.3%、2025 年 `APP` 约 48.1%、2026 年 `LITE` 约 66.7%；
  历史策略最大回撤约 `-39.6%`，最长一次从峰值到恢复约 832 个日历日。对应的
  `output/can_slim_fixed_top3_concentration.csv` 与
  `output/can_slim_fixed_top3_drawdown_episodes.csv` 仅用于风险披露，不构成
  paper/实盘放行证据；
- 截至 2026-07-30，正式入选股票涉及的 5 个“接近整数倍”的价格跳变中，
  Nasdaq 已确认 `VISN 2026-04-28` 为现金分配；SEC 一手文件进一步确认
  `ARWR 2016-11-30` 是停止三项药物开发后的真实暴跌、
  `ORGO 2025-02-28` 是业绩披露后的真实上涨、`APPS 2025-02-06` 是业绩超预期
  并上调指引后的真实上涨。这三项被明确标为
  `MARKET_MOVE_NO_ADJUSTMENT`，不能再由整数倍启发式回调；
- 按最新正式入选集合重跑后，只产生 3 个接近整数倍的候选事件：
  `VISN 2026-04-28` 已确认是现金分配，`ARWR 2016-11-30` 与
  `APPS 2025-02-06` 已由一手来源确认是真实市场波动；当前入选集合的公司行动
  验证为 `PASS`，没有未解决或抓取失败事件；
- 全市场 702 个启发式候选中，新增三项审核只把未解决数从 572 降至 569，
  87 个来源抓取失败保持不变；这说明改动是针对三项 SEC 证据的确定性覆盖，
  不是利用当天网络结果重新分类全市场历史。公司行动选择影响重跑后，原始 Top 3
  有 2 个信号发生变化、执行 Top 3 有 1 个信号发生变化，但年度收益差仍为 0；
- 将全市场未解决事件与公司行动对照场景中的入选信号按时间交叉后，只有两个事件
  位于相关股票上：`TGTX 2023-08-01` 到 2025-03-31/04-30 入选相隔
  417/438 个交易日；`CORT 2025-12-31` 后没有再次入选。两者都没有进入
  策略 253 个交易日的价格回看窗口，因此
  `events_affecting_selected_price_lookback = 0`。这只证明当前正式选择不受影响，
  不代表这些事件本身已被解决；
- 尾部依赖压力测试逐年同时剔除策略最佳单日和 Nasdaq 同一日后，仍为
  4/6 年跑赢，没有年度因此由赢转输；
- 逐年同时剔除策略最佳月份和 Nasdaq 同一月份后，只剩 2/6 年跑赢；
  2021 和尚未结束的 2026 年由赢转输。这是一项故意严苛的机械敏感性
  测试，不是可交易反事实，但说明历史优势明显依赖少数强势月份；
- 2024 年并非只靠一个月份：剔除当年最佳月份后，策略约 `+117.60%`、
  Nasdaq 同期约 `+21.12%`，仍超额约 `+96.48` 个百分点；尽管如此，
  该年收益和集中度仍不应假设可以重复；
- 正式 Top 3 的季度财务年龄并非普遍接近 550 天上限：2021–2026 的 201 个
  入选位置中，中位数为 42 天、90 分位为 87 天，只有 2 个超过 120 天；
- 这两个陈旧位置都来自 SNDX（304 天和 336 天）。2022-12-30 当时市场风控
  关闭，没有形成持仓；2023-01-31 的执行组合会在 120 天压力口径下由
  `SNDX` 替换为 `AXON`，使 2023 年收益从约 `+33.95%` 变为 `+45.58%`；
- 120/150/200 天场景因此都得到 5/6，但这是看过历史结果后的敏感性证据，
  不能据此事后修改冻结参数。若未来开发新版本，应把较短财务年龄预先写死为
  独立候选，再走新的时间顺序和前向验证；
- research-v2 已按上述要求预先固定 `150/365/550` 天财务年龄网格，与
  Top 3/5/10 和两档流动性组合成 18 个候选；每个生效年度只使用此前 36 个月及
  扩展历史排名，参数在年度内冻结。当前数据上的 2022–2026 walk-forward 只有
  3/5 年跑赢：2023 年超额约 `-24.73` 个百分点，2025 年超额约 `-3.43`
  个百分点；30/50 bps 成本下仍只有 3/5。样本外中位超额为正，但未达到预写的
  至少 4/5 年跑赢和 30 bps 下至少 4/5 的门槛；
- 这套 v2 共审计 196 个样本外持仓、86 个标的，按 Nasdaq 交易日历没有缺失
  持仓价格，也没有持有未知退市终值标的。唯一超过 50% 的单日事件是 SEZL
  2024-11-08 的真实上涨；使用固定 SHA 的 Stooq 归档复核 712 个重叠交易日通过且
  0 缺行。完整输入 manifest、输出 SHA、门槛和失败项绑定在
  `output/data_provenance/research_v2_evaluation_2026-08-10.json`。数据门通过不等于
  策略门通过，v2 不晋级，release 继续 `BLOCKED`；
- 同一 18 候选和完全相同的年度 walk-forward 规则已在最终 proven-only 季度财务
  上重跑，仍只有 3/5 年跑赢：2023 超额约 `-17.96` 个百分点，2025 约 `-7.65`
  个百分点，30/50 bps 也都是 3/5。200 个样本外持仓、94 个标的没有缺失持仓
  价格或未知终值；输入 manifest、选参快照和逐产物 SHA 绑定在
  `output/data_provenance/research_v2_proven_only_bank_v3_evaluation_2026-08-10.json`。
  因而 v2 的失败不是仅靠剔除未验证财务行即可修复，也没有依据扩大网格追逐历史
  胜率；
- v2 失败后没有继续扩大历史网格。曾冻结一个机制更简单的 forward-only
  challenger：`can-slim-v3-fresh-top3-shadow` 保留 Top 3 集中领导股、`$10M`
  流动性门槛和 growth 模式，只把财务最大年龄固定为 150 天。它在未收紧财务
  行级证据前的历史诊断曾为 6/6，但该结果现已撤回，不再作为 shadow 候选；
- 对当前 parser recipe 重新审计 97,226 条派生财务记录并修复 bank revenue operand
  的期限选择后，92,251 条有公式证明，4,975 条不能由绑定的 SEC 原始 operand
  复算；全部 12,564 条 `derived_bank_revenue` 已通过。`--exclude-unproven-derived`
  fail-closed 重放得到 314,615 条 proven-only 季度记录，SHA 为
  `13d77de9104445d89a4bf289eea4f93496394faaf02f7fddad40e916bff871c4`。
  在完全相同的 Top 3/150 天配置上重算后只剩 4/6：2021、2023 未跑赢；30/50
  bps 下均为 3/6。持仓价格和退市终值仍完整，但结果对未验证派生财务行敏感，
  因此 v3 在产生任何 forward observation 前被标记为
  `INVALIDATED_DATA_RELIABILITY`，继续 `BLOCKED`；
- 原始 v3 证据仍保留在
  `output/data_provenance/research_v3_fresh_top3_2026-08-10.json` 作为审计记录；
  最终 proven-only 重算、数据 manifest 和逐产物 SHA 绑定在
  `output/data_provenance/research_v3_fresh_top3_proven_only_bank_v3_2026-08-10.json`。
  `output/research_v3_fresh_top3_shadow_summary.json` 已清空 active config/snapshot 并
  绑定撤回证据，不会继续累计这个失效候选的 shadow 记录；
- 在最终 proven-only 数据上对原有 18 个候选逐一重跑 10/30/50 bps 后，只有
  candidate 15/16/17 在三档成本下都保持 4/5；三者均为 Top 10、`$10M` 流动性
  门槛，区别仅为 150/365/550 天财务年龄。为避免放宽数据新鲜度，新的
  forward-only v4 固定 candidate 15（Top 10、150 天），不再扩大参数网格。它在
  2022–2026 的 10/30/50 bps 下均为 4/5，但 2023 年仍落后 Nasdaq 约 32.56
  个百分点；该配置是在看过历史结果后选择，历史表现明确标记为 contaminated，
  不能作为样本外晋级证据；
- v4 共审计 508 个持仓位置、194 个标的，缺失持仓价格和未知退市终值均为 0。
  最大历史回撤约 `-23.92%`，低于 Top 3 的约 `-39.55%`；SEZL 2024-11-08
  单日大涨在 Top 10 中权重为 10%，将该事件归零后 2024 仍超额约 13.95 个百分点。
  全候选成本筛查绑定在
  `output/data_provenance/research_candidate_cost_screen_proven_only_bank_v3_13d77de9_2026-08-10.json`，
  v4 回放、数据 manifest 和逐产物 SHA 绑定在
  `output/data_provenance/research_v4_cost_robust_top10_proven_only_bank_v3_2026-08-10.json`；
  `output/research_v4_cost_robust_top10_shadow_summary.json` 只把它冻结为新的
  `FROZEN_FORWARD_ONLY` challenger，仍为 `BLOCKED`、前向月数为 0，也没有替换
  正式 v1、连接每日生产或授权券商操作；
- v4 的纯本地前向信号入口为
  `PYTHONPATH=. .venv/bin/python scripts/research_v4_shadow_signal.py`。它从冻结清单
  读取并校验 proven-only 季度输入 SHA，只在 `2026-08-10` 冻结日之后形成的首个
  月度信号才写入独立目录
  `output/daily/can-slim-v4-cost-robust-top10-shadow/`。当前本地最新月度信号仍是
  2026-07-31，因此实跑返回 `WAITING_FOR_FIRST_POST_FREEZE_SIGNAL` 且不写任何账本；
  它不会把冻结前信号倒灌为 forward evidence，也不会写正式 v1 目录、启用 Workflow
  或外部下单；
- v4 的研究专用晋级进度口径由
  `PYTHONPATH=. .venv/bin/python scripts/research_v4_shadow_status.py` 单独输出。状态文件
  绑定冻结摘要、数据 manifest、策略代码和 proven-only 季度输入 SHA，并分别列出
  12 个完整前向月份、12 次月度信号、30 bps 后正超额、40% 最大回撤以及持续数据
  完整性门槛；该脚本本身不会把 `promotion_eligible` 改为 true。2026-08-10 实跑仍为
  0 个月、0 次信号、`BLOCKED`，没有倒灌 2026-07-31 的冻结前信号；
- 在不改写 v4 的前提下，新增 research-v5 风险机制候选：当 v4 最近 63 个交易日
  领先 QQQ 时保持 100% v4；当 v4 落后且 QQQ 位于 200 日均线上方时，下月使用
  50% v4 + 50% QQQ；当 v4 落后且 QQQ 趋势关闭时，下月使用 50% v4 + 50% 现金。
  所有判断只取上一个完整月末，按月再平衡。真实 Nasdaq QQQ close 路径在 30 bps
  下仍为 4/5，2022–2026 中位超额约 12.91%，最差年度超额约 -25.34%，最大回撤
  约 -23.88%，水下时间约 92.81%；50 bps 下仍为 4/5。QQQ 收益已纳入 Nasdaq
  ex-date 现金分红并绑定原始 dividend payload SHA；月初旧持仓先走完当日收益，再
  在收盘换仓，避免把下一期权重提前一天。相对 v4，最差年度改善约 7.22 个百分点、
  水下时间略有改善，最大回撤基本持平，但中位超额下降约 6.36 个百分点；
  该组合规则是在查看历史诊断后形成，明确标记为
  `historical_selection_contaminated=true`、`BLOCKED`，只能作为新的并行 forward
  challenger，不能替代独立验证。可复现入口为
  `PYTHONPATH=. .venv/bin/python scripts/research_v5_trend_core_satellite.py --refresh-core-price`；
- v5 的整股审计覆盖 2022-01 至 2026-07 的 55 个月度调度，其中 40 个月有股票
  目标，共 398 个股票目标。`$10k` 账户有 32.5% 的有效选股月至少一只股票无法
  买入，所有月份取整现金拖累中位约 6.80%、最大约 21.47%；`$25k` 对应
  22.5%、3.06%、15.18%；`$100k` 所有股票目标至少能买 1 股，取整现金拖累中位
  约 0.64%、最大约 4.00%。连续整股、月末收盘换仓、30 bps 回放中，三档资金仍均
  为 4/5：`$10k/$25k/$100k` 中位年度超额分别约 7.02%/8.69%/10.26%，最差年度
  约 -26.28%/-26.24%/-25.57%，最大回撤约 -22.65%/-23.16%/-23.43%。小账户
  2026 相对分数股基线最多损失约 5.89 个百分点，因此分数股结果仍不能直接外推；
  在 30 bps 成本之外再加 10 bps 不利滑点，并假设每次至少成交 75%、余单随后交易日
  继续执行的压力场景中，三档账户仍为 4/5，但 `$10k/$25k/$100k` 中位年度超额
  降至约 3.36%/5.88%/7.38%，最差年度约 -27.69%/-27.61%/-26.84%，最大回撤
  约 -23.40%/-23.78%/-24.04%；
  审计入口为
  `PYTHONPATH=. .venv/bin/python scripts/research_v5_execution_sensitivity.py`。该结果只
  使用收盘价并包含 QQQ 现金分红；部分成交和滑点只是确定性压力，不是券商真实
  成交、截止时间、市场冲击或拒单证据；
- v5 当前规则、研究摘要、QQQ/分红输入、连续整股结果和执行压力结果已冻结到
  `output/research_v5_qqq_relative_trend_core_shadow_summary.json`，状态为
  `FROZEN_FORWARD_ONLY`、`BLOCKED`，forward 起点为 2026-08-10，历史结果继续标记
  `historical_selection_contaminated=true`。信号入口
  `PYTHONPATH=. .venv/bin/python scripts/research_v5_shadow_signal.py --decision-date YYYY-MM-DD`
  只接受真实 Nasdaq 月末，并要求冻结日起至少 63 个 v4/QQQ forward 收益间隔、同日
  v4 冻结组合和完整 QQQ 200 日趋势窗口；当前 2026-08-10 实跑只返回
  `WAITING_FOR_MONTH_END_SIGNAL`，期望首个月末为 2026-08-31，没有写入或倒灌信号；
- v5 的 63-session warmup 不会使用冻结前历史补齐。每个真实交易日可先用
  `scripts/shadow_forward_observation.py` 形成同日 v4 观测，再用
  `PYTHONPATH=. .venv/bin/python scripts/research_v5_forward_state.py --v4-observation PATH`
  追加一行相对强弱状态；状态行绑定 v4 观测、QQQ 输入和 v5 冻结摘要 SHA，保持
  append-only，并明确写入 `counts_as_promotion_evidence=false`。这里的 v4 NAV 是
  `chained_monthly_standalone_fixed_positions_with_full_entry_cost`：每个持仓月使用
  独立固定持仓回报并重新计整仓入场成本，口径偏保守但不是 turnover-aware 的真实
  连续账户净值，因此只能生成未来 v5 决策，不能单独作为晋级或实盘证据；
- v6 research 将年度 time-frozen CAN SLIM walk-forward 组合降到总资金的 25%，
  其余 75% 由两个等权风险子账户在 QQQ/现金间配置；两个子账户分别使用 42/45
  session 相对强弱，并共同使用 QQQ 100-session 趋势，只以前一完整月末决定下月
  配置。该规则仍是在查看 2022–2026 结果后形成，因此
  `historical_selection_contaminated=true`，不会替代真实前向证据。50 bps 分数股诊断
  为 4/5 跑赢 Nasdaq、最差年度超额约 -4.38%、最大回撤约 -19.17%；相对更严格的
  QQQ 对照只有 3/5，最差年度约落后 9.13%，但全期年化/波动/最大回撤约为
  19.47%/20.46%/-19.17%，同期 QQQ 约为 13.55%/23.38%/-34.83%。邻近 lookback
  组合会退化到 3/5，说明阈值敏感性仍是重要风险，不能把历史结果解释成已验证；
- v6 的连续整股 50 bps 回放在 `$10k/$25k/$100k` 均为 4/5；最差年度超额约
  -4.55%/-4.38%/-4.11%，最大回撤约 -17.84%/-18.91%/-19.04%。额外 10 bps
  不利滑点和每次至少 75% 成交的压力下仍为 4/5，最差年度约
  -5.64%/-5.47%/-5.29%。不过 `$10k` 有 57.5% 的有股票目标月份至少一只股票无法
  买入，`$25k` 为 35%，只有 `$100k` 降到 2.5%；因此小账户 canary 必须接受显著
  取整偏差并逐笔对账。复现入口为
  `PYTHONPATH=. .venv/bin/python scripts/research_v6_walkforward_defensive_ensemble.py`
  和 `scripts/research_v6_execution_sensitivity.py`；
- 为缩短原先 12 个月等待，v6 保持月度交易，但按周记录真实净值与 QQQ 对照。冻结
  manifest 使用两级门槛：从首个可执行月度信号的执行日 `2026-09-01` 起，13 个完整
  前向周、13 次周度盯市、至少 3 次月度决策后，只能进入有限 canary 审查；26 个完整
  前向周、26 次周度盯市、至少 6 次月度决策后，才进入正式晋级审查。13 周并不被
  描述为完整统计验证，任何券商连接、订单或资金上限仍需单独明确授权。直接改成周频、
  双周频或 2–3 周确认的历史实验在 50 bps 下明显退化，因此没有用高换手人为制造更多
  “信号”；门槛由 `scripts/research_v6_shadow_manifest.py` 固化；
- v6 月末入口为
  `PYTHONPATH=. .venv/bin/python scripts/research_v6_shadow_signal.py --decision-date YYYY-MM-DD`。
  它只接受真实 Nasdaq 月末，使用 manifest 内冻结的年度配置快照和 proven quarterly
  SHA，自动重放基础策略到决策日，再生成 25% 股票与 QQQ/现金目标；不会刷新参数、
  连接券商或创建订单。2026-08-10 实跑为 `WAITING_FOR_MONTH_END_SIGNAL`；将日期提前
  干跑到 2026-08-31 时，由于当前本地源数据只到 2026-08-07，返回
  `WAITING_FOR_MONTH_END_SOURCE_DATA`，不会拿 2026-07-31 信号冒充前向信号；
- 首次执行后，每个完整周末使用
  `PYTHONPATH=. .venv/bin/python scripts/research_v6_weekly_mark.py --as-of YYYY-MM-DD`
  重放 `$25k` 虚拟整股账户，固定使用 50 bps、额外 10 bps 滑点和 75% 分批成交，
  同时绑定冻结 summary、全部目标文件和 QQQ 输入 SHA。非完整周末、缺失月度执行或
  周末价格未到时只返回 WAITING，不会追加证据。`scripts/research_v6_forward_status.py`
  从 append-only 周度 mark 和月度决策计算 13/26 周门槛，但始终保持
  `release_status=BLOCKED`、`broker_action_authorized=false`；
- 日常只需运行一个幂等入口：
  `PYTHONPATH=. .venv/bin/python scripts/research_v6_observe.py --as-of YYYY-MM-DD`。
  它依次尝试月末信号、完整周末整股 mark 和状态更新；普通交易日只返回相应 WAITING，
  不制造观测。2026-08-10 真实干跑结果为 signal=`WAITING_FOR_MONTH_END_SIGNAL`、
  weekly mark=`WAITING_FOR_WEEK_END`、0 周/0 决策、`broker_action_authorized=false`。
  该入口目前只是手工 one-shot，没有启用 cron 或 GitHub Workflow；
- 完整单次刷新与观察入口为
  `PYTHONPATH=. .venv/bin/python scripts/research_v6_scheduled_run.py`。它按新加坡本地日期
  自动解析前一个已完成的 Nasdaq session，先刷新隔离市场缓存，readiness 全部通过后
  才调用 v6 observe；2026-08-10 实跑解析到 2026-08-07，市场 readiness=true，
  signal=`WAITING_FOR_MONTH_END_SIGNAL`、weekly=`WAITING_FOR_FORWARD_START`，且正式市场/
  财务文件均未修改；
- macOS LaunchAgent 模板已准备在 `ops/com.quant-stocks.v6-shadow.plist`，计划周二至周六
  新加坡时间 09:00 运行上述单次任务。该 plist 当前只是仓库文件，没有复制到
  `~/Library/LaunchAgents`，也没有执行 `launchctl bootstrap`；启用会产生持续网络请求和
  research-only 本地写入，必须在用户明确确认后执行；
- LaunchAgent 操作入口为 `scripts/research_v6_launchd.py`：无参数只输出安装 dry-run，
  `--status` 只读检查模板/已安装 SHA、launchctl loaded 状态和最后一次 scheduled run；
  `--apply` 才会复制并 bootstrap。`--unload` 默认同样只 dry-run，配合 `--apply` 时仅
  bootout、保留已安装 plist，便于恢复。当前实跑状态为 `PREPARED_NOT_INSTALLED`，最后
  一次手工 scheduled run 的 market readiness=true、正式 release 仍为 BLOCKED；
- 策略最大回撤仍可能较大；
- 完整 2021–2026 净值路径的最大回撤为约 `-39.55%`：2021-02-09 高点后
  于 2021-03-08 触底，至 2021-08-09 恢复，峰值到恢复 125 个交易日、
  181 个自然日。最长水下期则从 2021-08-09 高点后开始，直到
  2023-07-17 才恢复，连续水下 485 个交易日、峰值到恢复 707 个自然日；
- 策略约 92.01% 的证据期交易日低于此前净值高点；同期 Nasdaq 约为
  89.78%，因此“多数时间水下”并非策略独有，但 Top 3 的波动体验更剧烈；
- 截至正式回测终点 2026-07-17，策略仍比 2026-06-22 的净值高点低约
  `31.17%`，同期 Nasdaq 当前回撤约 `5.81%`。历史累计高收益不等于当前
  持有体验平稳；
- 真实生产资格必须依赖冻结后的前向表现，而不能继续通过调整参数改善历史结果；
- 修改正式模型后，前向观察时钟必须重新开始。

正式结果文件：

| 文件 | 说明 |
|---|---|
| `output/can_slim_fixed_top3_summary.json` | 冻结参数、验证状态和统计摘要 |
| `output/can_slim_fixed_top3_annual.csv` | 年度策略、Nasdaq 和超额收益 |
| `output/can_slim_fixed_top3_backtest.csv` | 每日收益、仓位、换手、现金和净值 |
| `output/can_slim_fixed_top3_cost_stress.csv` | 0/10/30/50 bps 成本压力测试 |
| `output/can_slim_fixed_top3_liquidity_capacity.csv` | 逐笔归一化成交额、历史流动性参与率与容量 |
| `output/can_slim_fixed_top3_trade_ledger.csv` | 完整逐笔交易账本 |
| `output/can_slim_walk_forward.csv` | chronological walk-forward 样本外逐年结果 |
| `output/can_slim_walk_forward_summary.json` | walk-forward 测试年数、胜率和状态 |
| `output/can_slim_survivorship_by_year.json` | 各年度 PIT 股票池后来不再出现成员及其价格覆盖审计；按 SEC 来源的终止收益证据、已证实更名和未证实成员缺席拆分，后者仍仅为研究代理口径 |
| `output/data_provenance/sec_submission_triage.json` | 对未解决历史价格缺口的 SEC submissions 候选线索；缓存 payload SHA 并支持离线复核，不自动修改正式数据 |
| `output/data_provenance/stockanalysis_price_triage.json` | 对 SEC 线索中的价格缺口评估公开历史页的实际覆盖、与本地数据的重叠比率及缓存 SHA；仅研究用，不构成来源许可或正式导入授权 |
| `output/open_source_price_audit_2026-08-04.json` | 对公开 GitHub 价格数据候选的缺口覆盖、可见许可证和重叠证据审计；本次结果不能作为正式导入授权，也不会解除 `BLOCKED` |
| `output/historical_pit_gap_priorities.csv` | 历史 PIT 价格与财务缺口的无未来收益补数优先级 |
| `output/can_slim_technical_candidate_financial_coverage.json` | 复用正式 selector 前置条件的逐信号潜在候选财务覆盖 |
| `output/can_slim_technical_candidate_financial_priorities.csv` | 按受影响风险开启信号次数排序的潜在候选财务补数清单 |
| `output/can_slim_fixed_top3_robustness_summary.json` | 数据口径、集中度和尾部依赖压力摘要 |
| `output/can_slim_fixed_top3_concentration.csv` | 按年已实现盈亏的单票、前两票和前四票集中度（非年度总收益的精确归因） |
| `output/can_slim_fixed_top3_tail_dependency.csv` | 逐年剔除最佳单日/月份的敏感性明细 |
| `output/can_slim_fixed_top3_financial_freshness_impact.csv` | 550 天与 120 天口径的逐信号选择影响 |
| `output/can_slim_fixed_top3_path_risk.json` | 完整证据期的跨年回撤、恢复和当前水下状态 |
| `output/can_slim_fixed_top3_drawdown_episodes.csv` | 策略与 Nasdaq 的逐次回撤区间 |
| `output/can_slim_selected_data_audit_fixed_top3.json` | 实际入选股票的数据审计 |
| `output/daily/can-slim-top3-v1/shadow_observations/` | 通过真实收盘、并与本地 Nasdaq 锚点重叠校验的 shadow-only 观测；执行收盘仅作锚点，只有其后的收盘才计入 forward，不计入放行门槛，也不修改正式价格缓存 |
| `output/daily/can-slim-top3-v1/shadow_observations/status.json` | 预承诺 shadow 前向进度、剩余交易日、12 个周期/7 个胜出周期和外部锚定状态；仅研究状态，不替代 release gate |

在本地收盘数据发布后，可追加一个真实前向观测：

```bash
PYTHONPATH=. .venv/bin/python scripts/shadow_forward_observation.py \
  --observation-date YYYY-MM-DD
```

该命令是幂等的；重复日期不会重复写入。它只写入 `shadow_observations/`，
不会把公开源数据混入正式价格缓存，也不会改变 `release_gate.json`。

查看进度：

```bash
PYTHONPATH=. .venv/bin/python scripts/shadow_forward_status.py
```

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
└── financial/                     # EPS、季度财务、原始 SEC 缓存和覆盖率

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
- Nasdaq 指数必须覆盖审计日期当天或之前最近一个已完成的官方交易日；周末和
  节假日会自动回退到上一交易日，但缺少任何一个已经完成的交易日都会失败，
  不再用“5 个自然日以内”掩盖连续行情缺口；
- 历史回放还会逐日对照 Nasdaq 官方交易日历，检查基准是否缺交易日、重复日期、
  混入非交易日或存在非法收盘价；任一异常都会阻止历史数据完整性通过；
- 当前股票池价格覆盖率至少 95%；
- 新鲜财务覆盖率至少 90%；
- 直接复用正式 selector 的 TTM 同比计算：在 550 天正式上限内能够实际算出
  `net_income + revenue` 增长的股票中，200 天内仍新鲜可算的比例至少 90%；
  同时单独披露全股票池原始覆盖率和简单指标覆盖率，避免把外国发行人等结构性
  不覆盖与本次更新失败混为一谈；
- SEC Company Facts 的 Q2/Q3 有时只披露半年或九个月累计利润。解析器会在同一
  累计起点、申报日不晚于当前申报、相邻季度间隔 60–135 天的条件下，用累计值之差
  推导单季值，并把来源标记为 `derived_ytd:*`；显式单季值始终优先。此前只推导
  Q4，会把 MNST 等公司误报为季度链不足。对 9 个高优先级代码做官方 SEC
  定向刷新后新增 29 条推导记录（MNST 28 条、TCBI 1 条），MNST 的 37 条潜在候选
  财务缺口全部消除；缺口观测由 3274 降至 3237，代码由 421 降至 420，Top 3 和
  年度收益均未改变。其余银行/外国发行人缺口不是同一种累计值问题；
- SEC 对部分银行使用 `RevenuesNetOfInterestExpense`，官方定义为包含净利息收入
  及交易损益等的总收入，因此可以作为银行 revenue；`InterestIncomeExpenseNet`
  只表示净利息收入，不能替代总收入。官方 Company Facts 验证显示该标签对当前
  2021–2026 缺口没有实际改善，只为 HWC 增加了 4 条早期记录。定义和原始事实取自
  `https://data.sec.gov/api/xbrl/companyfacts/CIK0000750577.json`；
- BPOP、UMBF 等银行没有统一总收入标签，但同时披露标准化的净利息收入和非利息
  收入。解析器只在两项具有相同财政期、申报日和 accession 时合成为
  `derived_bank_revenue:*`，显式总收入仍优先。7 家银行新增 912 条可追溯记录，
  消除 224 条潜在候选缺口和 7 个代码，使覆盖率从 78.51% 升至 80.05%。
  FCNCA 因此在 2023-07、2023-10、2023-11、2024-01 和 2024-03 五个信号进入
  Top 3，使 2023 年收益从 41.47% 更新为 33.95%，2024 年从 253.41% 更新为
  291.65%；这再次证明缺失候选会实质改变历史成绩。继续定向刷新 FITB、CBSH、
  FFIN、SLM、PNFP、IBOC、AGNC 后又消除 144 条缺口和 5 个代码，收益与 Top 3
  未再变化；OZK 的 SEC 当前 ticker map 指向无效 Company Facts 地址并返回 404，
  需要另行确认历史 CIK；
- BKNG 在 2015 年后不再持续披露 `NetIncomeLoss`，但持续披露
  `NetIncomeLossAvailableToCommonStockholdersBasic`。解析器现在仅在公司净利润
  和 `ProfitLoss` 都不可用时，才把普通股股东可得利润作为最低优先级 fallback，
  并保留原始 concept。对 BKNG、PAYX 定向刷新后又消除 65 条缺口，代码减少 2 个；
  该修复通过横截面百分位间接使 2023-03-31 的第三名由 `SNEX` 变为 `AGYS`，
  因而 2023 年收益从 43.34% 降至 41.47%，旧成绩已按新数据如实更新；
- 历史优先级还包含已退出当前股票池、因而不在 SEC 当前 ticker map 的代码。
  定向刷新现在支持显式 `--ticker-cik TICKER=CIK`，未知历史代码会明确失败而不是
  静默显示 `requested=0`。SEC Company Facts 确认 COOP 的历史 CIK 为 `933136`；
  补齐后其 36 条缺口全部消除，并在 2021-04-30、2021-09-30 两个信号直接进入
  Top 3，分别替换 `ORGO`、`ARCB`，使 2021 年收益从 47.45% 更新为 55.66%；
- 同样通过 SEC 官方 Company Facts 实体名验证 ALTR=`1701732`、
  BECN=`1124941`、ANSS=`1013462` 三个已退出当前 ticker map 的历史 CIK；定向
  刷新后消除 89 条缺口和 3 个代码，Top 3 与收益未再变化；
- HOLX=`859737` 经官方实体名验证后，与 PNFP、SKYW、VIRT、XEL、NWE 一起刷新，
  又消除 71 条缺口和 3 个代码。VIRT 在 2021-01 信号进入 Top 3 并替换 FRHC，
  SKYW 在 2024-10、2025-01 信号分别替换 ITRI、DAVE；2021 收益因此从 55.66%
  更新为 47.37%，2024 从 291.65% 更新为 297.31%，2025 从 22.40% 更新为
  12.63%，使历史跑赢年份从 5/6 降为 4/6；
- 继续核对 SEC 实体后确认 PNFP 在 2025 年末由旧主体 CIK `1115055` 迁至
  新主体 `2082866`，NWE 在 2023 年由旧主体 CIK `73088` 迁至新控股主体
  `1993004`；补抓旧主体后两者分别恢复 294、278 条季度记录。XEL 在 2019 年后
  改用标准总公用事业收入标签 `RegulatedAndUnregulatedOperatingRevenue`，
  解析器新增该总收入标签后恢复近年收入链。三项合计消除 68 条潜在候选缺口和
  3 个代码，Top 3 与年度收益未改变；
- VLY 的银行收入和 VTRS 的普通股股东利润仍是旧解析产物，强制重建后分别恢复
  142 条银行收入、44 条此前遗漏的净利润记录，又消除 42 条缺口和 2 个代码。
  VLY 本身没有进入 2021-02-26 Top 3，但加入合格横截面后改变了百分位排名，
  使第三名由 `FRHC` 变为 `AMKR`；2021 年收益由 47.37% 更新为 46.55%，其余
  年度不变。候选财务覆盖率最终由 82.13% 提升至 82.88%，同时再次证明未入选
  候选也可能通过横截面排名间接改变选择；
- PCVX、TSEM、CCOI 的后续核对显示三种不同情形：PCVX 有完整净利润事实但没有
  SEC 收入事实，TSEM 是只有 20-F 的外国发行人，二者不能靠猜标签补齐；审计
  profile 因此新增“只有净利润、无收入事实”的独立分类。CCOI 则是普通股利润
  fallback 加入前的旧解析产物，强制重建后净利润由 24 条增至 131 条，消除
  19 条候选缺口和 1 个代码。CCOI 在 2023-12-29 信号直接进入 Top 3 并替换
  `NBIX`，使 2024 年收益由 297.31% 更新为 286.56%；覆盖率由 82.88% 提升至
  83.01%，其余年度不变；
- 最新 Top 20 中，SLAB 是 fallback 加入前的旧解析结果，ITCI 则在 2025 年
  被收购后退出当前 ticker map。SEC 官方实体页确认 ITCI 历史 CIK 为 `1567514`；
  强制重建 SLAB 并按历史 CIK 补抓 ITCI 后，分别得到 280、188 条利润/收入记录，
  合计再消除 40 条候选缺口，净减少 1 个缺口代码，覆盖率由 83.01% 提升至
  83.29%。两者虽然在部分时点通过增长阈值，但正式 Top 3 和年度收益均未改变；
- 将检查范围扩展到优先级 21–50 后，一次性强制重建 EWBC、SFNC、TBBK、INDB、
  COLB、FIBK、SBCF、CVBF、OSIS、ACLS、KRYS、MDGL 共 12 个美国发行人；其中
  10 个恢复跨证据期的完整 TTM 链，KRYS、MDGL 只在后期形成收入链。该批次消除
  162 条候选缺口和 10 个代码，覆盖率由 83.29% 升至 84.40%，最差信号由
  71.67% 升至 73.33%。2025-12-31 信号由 `MU` 变为 `VICR`，使 2025 年收益
  由 12.63% 更新为 13.28%；其余年度不变；
- 原始缓存与断点续跑链路完成真实试点后，按潜在候选财务缺口优先级一次批量刷新
  CCEP、ARCC、DOX、OZK、CHKP、ASML、ARGX、SNY、SBLK、AZN、ICLR、NVMI、
  TCOM、GBDC、PCVX、TSEM、DSGX、GLNG、LOGI、WIX。20 个请求中 19 个成功形成
  原始缓存；OZK 的 SEC 当前 CIK 返回 404 并进入失败冷却。LOGI 恢复全部 17 条
  候选财务缺口，并进入 2021-01-29 信号、延续至 2021-02-26 持仓，使 2021 年
  收益由 46.55% 更新为 55.71%；其余年度不变。候选财务覆盖率由 84.40% 升至
  84.51%，缺口代码由 382 降至 381。该变化再次说明缺失候选会实质改变历史成绩；
- 随后专门选择 `SEC_QUARTERLY_PARTIAL` 的高优先级缺口，批量刷新 KRYS、MDGL、
  CLDX、UNIT、VNOM、LLYVK、UTHR、ACT、CSWC、APA；10 个 CIK 全部成功并进入原始
  缓存。ACT、CLDX、UTHR 恢复完整季度链，共消除 33 条候选缺口和 3 个代码，使
  覆盖率由 84.51% 升至 84.74%，缺口代码由 381 降至 378；其余 7 个代码虽然原始
  输入已可复现，但历史季度链仍不完整。该批次没有改变任何 Top 3 或年度收益；
- 第二批按同一 profile 刷新 BANR、ZION、ZNTL、VKTX、MRVL、WAFD、AXSM、ORKA、
  RCKT、KURA，10 个 CIK 全部成功。该批次再消除 16 条候选缺口和 2 个代码，使
  覆盖率升至 84.85%、缺口代码降至 376；固定 Top 3 和各年度收益均未变化；
- 启用可复现优先级批处理后，首批请求 FSV、MNDY、QFIN、XP、ATAT、STNE、AGNC、
  ASND、CAMT、GMAB、HTHT、LEGN、MMYT、BNTX、DLO、GGAL、MLTX、TIGO、WSBC、
  CIGI；20 个 CIK 全部进入原始缓存，缓存代码从 45 增至 65。XP、STNE、ASND、
  BNTX、GGAL 没有当前解析器可用的季度结果；WSBC 恢复完整链，消除 11 条候选缺口
  和 1 个代码，使覆盖率由 84.85% 升至 84.93%、缺口代码由 376 降至 375。该批
  代码没有直接进入交易账本，但新增合格横截面改变了百分位排名，使 2026 回放收益
  由 26.05% 更新为 36.07%；其余年度不变。原始输入仍已固定，后续扩展外国发行人
  数据源或解析规则时无需再次逐只下载；
- 改用缓存专用可行动性排名后，下一批 20 个唯一 CIK 全部成功，无失败。LKFN、
  QURE、SYBT、TFSL、TLN 五个代码恢复完整链，共消除 20 条候选缺口和 5 个代码，
  使覆盖率由 84.93% 升至 85.06%、缺口代码由 375 降至 370；Top 3 和年度收益
  均未变化。manifest CIK 数只从 65 增至 84，说明一个请求主体此前已由另一 ticker
  别名缓存，也由此推动后续按 CIK 识别本地缓存、避免别名重复联网的修复；
- 第三批首次完整验证 CIK 级限流与本地别名复用：网络处理 20 个唯一 CIK、21 个
  ticker，BATRA/BATRK 共享请求；WAFDP 从已有 CIK 纯本地解析。全部成功，缓存绑定
  代码由 85 增至 107。净缺口代码由 370 降至 369，但缺口观测仍为 2180，Top 3
  和年度收益不变，说明只剩 1–2 个缺口的 partial 尾部边际收益已明显下降；缓存
  调度因此进一步把低缺口 partial 和低缺口 NO_PARSED 分层后置；
- 第四批混合 6 个高缺口 NO_PARSED 与 14 个低缺口 partial，20 个 CIK 全部成功
  缓存，但 6 个 NO_PARSED 均只有 20-F/40-F/6-K 等外国定期报告，当前解析为空；
  仅 CAKE 完全补齐，净减少 1 条缺口和 1 个代码。由此停止把模糊 NO_PARSED
  直接视为高可行动性，并新增原始 payload 画像。当前 124 个缓存 CIK 中，85 个
  为 `US_GAAP_WITH_10Q`，39 个为 `FOREIGN_PERIODIC_NO_10Q`；
- 优先级表新增 `raw_sec_cache_profile` 和 `recommended_data_action`。剩余缺口目前
  拆为：263 个未缓存代码（1220 条）可继续抓 SEC；38 个已确认需外国季度来源
  （621 条）；64 个 US-GAAP 代码需重解析或接受历史长度限制（271 条）；2 个
  临床阶段公司需确认“无经营收入”政策（31 条）。原始画像同时记录当前支持的直接
  收入概念和银行收入组件，因此不会再把 AGNC/BUSE 的历史口径限制与 PCVX/MLTX
  的真实无营收混为“待补标签”；
- 概念映射检查确认 ARCC、GBDC 两个 BDC 使用标准
  `GrossInvestmentIncomeOperating` 作为营业总投资收入。加入该标签并从缓存离线
  重解析后，GBDC 完全补齐，ARCC 缺口由 36 降至 20，合计净消除 36 条缺口和
  1 个代码，使覆盖率升至 85.32%；Top 3 和年度收益不变。PCVX、MLTX 属于真实
  无营收阶段，未人为填造收入；AGNC 的 2020 年后收入口径不连续，暂不冒险拼接；

可按上述优先级只刷新指定代码，避免为了验证少数缺口重新请求全部股票：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --as-of YYYY-MM-DD --force --tickers MNST BPOP UMBF
```

刷新任务按唯一 SEC CIK 合并请求：多个股类或历史代码若属于同一发行主体，只下载
一次 Company Facts，再分别解析各代码。成功抓取的代码会整段替换旧解析历史，
避免规则修正后旧错误行残留；抓取失败的代码不会删除已有数据。

首次补齐完整原始缓存时，可以分批断点续跑，只请求尚未缓存的代码：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --cache-missing-only --limit 20 --workers 4
```

若目标是优先降低会影响历史选股的财务缺口，可直接复用验证报告生成的优先级表：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --cache-missing-only \
  --cache-priority-file \
    output/can_slim_technical_candidate_financial_priorities.csv \
  --limit 20 --workers 4
```

优先级文件必须包含 `ticker`；优先使用 `cache_refresh_priority_rank`，否则回退到
`priority_rank` 或文件行序。缓存专用排名把已有部分季度链、最可能通过新版
Company Facts 补齐的代码前置，把明确需要新季度来源的外国发行人后置；原始缺口
严重度排名仍单独保留，不改变研究披露。文件未列出的当前股票池代码仍按原顺序接在
后面，不会被永久排除。已缓存代码和仍在失败冷却期的代码会被跳过，随后自动由下一
优先代码补位。Coverage JSON 会
记录优先级文件路径、SHA-256、排序规则、实际命中的优先代码数和本批请求代码，
因此每批下载选择可以复核。
`--limit` 限制唯一 SEC CIK 请求数，不是 ticker 行数；同一发行主体的多个股类或
历史代码会一起进入本批，不会拆开，也不会重复消耗请求名额。缓存补齐模式选中一个
CIK 后，还会顺带解析股票池中所有尚未缓存的同 CIK 别名，包括原先处于 ticker
级失败冷却的别名；这不增加网络请求，并避免以后为同一主体再次下载。因此
`requested_tickers` 可能略多于 `--limit`，但 `requested_ciks` 不会超过它。
审计会把“本批实际请求”“符合条件但因 `--limit` 延后”和“处于失败冷却”分开
计数，并只保留前 20 个样例；不会再把所有尚未轮到的代码误报为冷却。`--limit`
必须是正整数，0 或负数会在任何文件或网络操作前拒绝。

每个成功批次都会更新 manifest、正式解析结果和覆盖审计；再次运行会跳过已有缓存，
继续处理下一批。`--tickers ...` 可把缺口检查限制到指定代码。该模式必须联网，
不能与 `--offline-cache` 或 `--reparse-cache` 同时使用。若本次没有任何需要刷新或
成功解析的代码，任务不会重新序列化年度和季度正式 CSV，只更新必要的状态与审计。
未形成原始缓存的失败代码会记录独立的 `cache_last_attempt`、状态和原因，并按
`--refresh-after-days` 冷却，避免在每个后续批次重复占位；需要立即重试时可显式
增加 `--force`。已经成功保存原始响应、但解析结果为空的代码仍视为“原始缓存完成”，
不会因为当前解析器无法提取季度链而重复下载同一响应。
在 2026-07-30 的实际网络条件下，20-code 优先级批次约耗时 10 分钟；SEC 延迟和
重试会显著影响时长，因此推荐以 20 为常规批次，确认网络稳定后再提高 `--limit`。
全股票池模式遇到 SEC 当前 ticker map 不认识的代码时，会跳过这些代码并在审计中
单独列出，不会阻塞所有可抓取代码；但用户通过 `--tickers` 显式请求未知代码时仍会
严格失败，必须提供经核实的 `--ticker-cik`。

需要按“修复一个 CIK 实际减少多少历史候选缺口”控制边际收益时，使用有界修复批次：

```bash
PYTHONPATH=. .venv/bin/python -m src.research.fundamentals_repair_batch \
  --as-of YYYY-MM-DD --limit 20 --workers 4
```

每批的原子 JSON 是权威审计记录，`index.csv` 只是可重建索引。即使进程在 JSON
落盘后、CSV 更新前中断，下一次仍会读取最新 JSON 的继续、缩小批次、暂停或复核
决定，不会因索引落后绕过停止信号；下次成功更新索引时会自动补回遗漏的 JSON 批次。
批后 payload 画像只解压本批 ticker 对应的不同 CIK，不再扫描全部原始缓存。只有在
人工复核来源或解析器确实发生变化后，才应使用 `--override-stop`。

在线刷新使用的 SEC ticker→CIK 映射也会按规范化内容计算 SHA-256，并以
`ticker_maps/ticker_map_<sha256>.json.gz` 内容寻址保存。映射未变化时复用同一
文件；SEC 后续增删映射时保留新快照。Coverage JSON 记录本次使用的映射路径和哈希，
缓存 manifest 同时校验所有映射快照，避免 live ticker map 的变化无法复现。

可随时只读查看进度，并同时验证 manifest、文件清单和 SHA-256：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --cache-audit-only
```

输出包含股票池覆盖率、已缓存 CIK 数、压缩/旧格式文件数、总字节数、缺口数量和
前 20 个缺口样例。增加 `--tickers ...` 时只审计指定代码。该命令不联网、不写文件。

历史代码若已不在 SEC 当前 ticker map，必须同时提供经官方实体页确认的 CIK：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --as-of YYYY-MM-DD --force --tickers COOP --ticker-cik COOP=933136
```

每次成功访问 SEC Company Facts 时，原始 JSON 会按 CIK 原子写入
`cleaned_stocks_data/financial/sec_companyfacts_cache/`，并生成包含来源 URL、
抓取时间、字节数和 SHA-256 的 `manifest.json`。离线重解析和在线刷新都会在
任何缓存/网络写入前逐文件核对 manifest 的文件清单、字节数和 SHA-256；缓存被
修改、缺失或出现未登记文件时会失败，不能先为变化后的输入静默重新生成哈希。
目录中已有 payload 却没有 manifest 时同样拒绝自动接管。若新 ticker 映射到已缓存
CIK，则直接读取本地 payload、解析该别名并更新绑定，不再访问 SEC。解析器规则变化
后可以完全离线。manifest 同时作为 ticker→CIK 的本地索引；增量重解析只解压目标
CIK，不会为更新少数股票而展开全部缓存，但正式写入前仍会核对完整文件清单、字节数
和 SHA-256。增量重解析只 upsert 指定代码的新事实，其他代码和本批未重新出现的
旧事实保持不变。

增量 `reparse_state` 的 ticker 指纹同样绑定主 parser、输出 schema 与 Python/Pandas
运行时；对登记的外部季度 ticker 还绑定外部季度 parser 和该 ticker 的注册表项。因而
外部季度 parser 变化会只使受影响 ticker 重新解析，不会把无关的国内 payload 误判为
已更新或强制全量重跑。

新增缓存默认使用确定性 gzip 格式 `CIK##########.json.gz`；同一份内容会产生稳定
字节，方便 SHA-256 复核。旧的未压缩 `.json` 仍可直接读取且不会被自动删除。
当前真实样本从约 5.27 MB 降至约 381 KB（约为原大小的 7.23%），显著降低完整
股票池缓存的本地磁盘和哈希读取成本。

只补原始缓存、完全不读取或写入正式年度/季度 CSV 时，必须显式增加
`--raw-cache-only`。`--limit` 按唯一 CIK 计数，同一 CIK 的多个 ticker 会一起
绑定；独立的 raw-cache state 会跳过已成功项、冷却临时失败，并默认不再请求已确认
404 的 Company Facts。在线 CLI 默认将外层批次限制为 25 个 CIK（可显式用 `--limit` 调整）；任务每完成 5 个 CIK
会原子更新 state，并立即重签绑定全部 payload 与 state 的 manifest。刷新开始时还会
写入事务 journal；收到中断时会取消尚未开始的请求、等待正在执行的请求停止，再做一次
最终 checkpoint。即使进程在 payload 已落盘但 checkpoint 间隔尚未到达时硬中断，下一次
加锁验证也会在确认既有 payload 未被外部改动后自动补写 manifest，因此已成功的部分
批次可直接续跑，无需手工重签 manifest：
checkpoint 会复用上一版已验证的未变更 CIK 条目，只重新计算本批发生变化的 payload，
避免每 5 个 CIK 都重新读取和哈希整个历史缓存；完整审计和 snapshot 验证仍会逐文件核对。

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --cache-missing-only --raw-cache-only \
  --cache-priority-file output/can_slim_technical_candidate_financial_priorities.csv \
  --limit 25 --workers 4
```

不带 `--raw-cache-only` 的 `--cache-missing-only` 保留原有语义：抓取成功后还会把
解析结果非破坏性合并进正式财务 CSV。需要严格隔离数据获取和正式发布时不要省略
该参数。

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --reparse-cache incremental --tickers EWBC
```

只有在已完成差异审计并取得明确的数据发布授权后，才可以从缓存完全重建正式年度与季度
CSV；全量模式必须同时指定不可变 snapshot 和 recipe-bound 的 v2 scope：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --reparse-cache full \
  --cache-snapshot <immutable-snapshot-dir> \
  --full-rebuild-scope <recipe-bound-scope-v2.json>
```

在冻结正式财务版本期间，先运行同一门槛的无写入预演：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --reparse-cache full --dry-run \
  --cache-snapshot <immutable-snapshot-dir> \
  --full-rebuild-scope <recipe-bound-scope-v2.json>
```

该命令完整验证 manifest、历史 ticker 覆盖和所有 raw payload，并在一次性临时目录
中重建年度/季度 CSV。输出会列出正式与重建文件的 SHA-256、行数、ticker 集合和每个
ticker 的事实行数差异；临时文件随后删除。它不会写正式 CSV、coverage、raw cache 或
`reparse_state.json`，因此可作为发布新数据版本前的必经比较步骤。
报告中的 `formal_content_match` 与 `formal_rebuild_gate` 是机器可消费的放行信号：
只有 annual/quarterly 两侧事实内容（忽略 `fetched_at`）都一致时才是 `PASS`；任一侧
有行数或事实差异都会明确返回 `BLOCKED_FORMAL_CONTENT_MISMATCH`，不能仅因 raw
coverage gate 通过就替换冻结正式文件。
同一 immutable snapshot、scope 和运行时配方的重复离线重解析还必须产生相同的 annual/
quarterly 字节 SHA；该确定性由测试持续约束。

v2 scope 除 raw snapshot、正式 ticker 集和正式 CSV SHA 外，还绑定 Company Facts
parser、外部季度 parser、外部季度注册表、输出 schema、Python/Pandas 运行时的内容
配方。执行前必须与当前环境逐项匹配；否则 full/dry-run 会在解析或写入前拒绝。旧 v1
scope 只保留为历史诊断，不能原地覆盖升级，也不能用于新的命令行 full rebuild；应以新
路径创建 v2 scope，保留旧证据不被改写。
本轮 checkpoint 与 comparison 代码更新后，曾生成的
`output/data_provenance/companyfacts_rebuild_scopes/manifest-ff932997f3143f50-copy-current.json`
因 parser 已继续变化而被 recipe SHA 门槛正确拒绝；随后重新生成并绑定当前代码的
scope 为
`output/data_provenance/companyfacts_rebuild_scopes/manifest-ff932997f3143f50-copy-batch25.json`，
其 live recipe SHA 为 `9c5b6b51528631901445ec7f9262eb378c8beea1755affe170efc6f6195cec3e`。
对应的最新 full dry-run 证据为
`output/data_provenance/companyfacts_rebuild_dry_runs/manifest-ff932997f3143f50-copy-scope-446b137bc783f3d7.json`；
它已通过 raw coverage 和 recipe gates，但
`formal_rebuild_gate=BLOCKED_FORMAL_CONTENT_MISMATCH`，因此仍不可替换冻结正式文件。
Python API 若传入 immutable manifest SHA，也必须同时传入 recipe SHA（反之亦然），
避免下游脚本只绑定 raw payload 而漏掉解析器版本。

两个模式都先完成年度和季度临时文件，再成对替换正式输出；第二个文件替换失败时
会把第一个文件回滚到原版本，不能留下年度与季度版本不一致的状态。`incremental`
只对指定代码做非破坏性 upsert，保留本批 payload 没有重新解析出来的正式事实；
若年度和季度两侧都没有新事实，则不会读取或重写约 61 MB 的正式 CSV。它不要求
缓存覆盖整个股票池。`full` 从空表开始生成，且必须先覆盖当前全部可投资代码以及
两个正式输出中已经存在的代码，部分缓存绝不能覆盖正式全量文件。该完整性检查
位于实际写入函数内部，不只存在于命令行参数解析层；未来脚本直接调用 Python API
也不能绕过。只有 manifest 绑定的官方 ticker-map 缺失或 Company Facts 404
负向证据可满足该覆盖门槛；timeout 和普通抓取失败仍会阻断全量重建。全量预检只
读取 manifest 和压缩文件字节以核对覆盖率与 SHA-256，不会
为研究画像额外解压全部 payload；显式 `--cache-audit-only` 仍会生成详细 payload
画像。刷新与离线重建共享跨进程文件锁，本地同时启动两个任务时会串行执行，
避免缓存、年度 CSV 和季度 CSV 相互覆盖。

当前 raw cache manifest 可验证，但它不是正式财务版本的等价物：活动 cache state
含 1,425 个 `raw_cached` ticker、11 个官方 Company Facts 404 和 5 个不在精确
SEC ticker-map 的代码；这些是缓存状态计数，不代表当前股票池的历史 PIT 财务已闭环。
当前 required universe 已缓存 1,602/1,618；另有 16 个官方不可用代码，
`cache_resolution_coverage=1.0`、ordinary unresolved=0。raw-only 批次全部明确
`formal_outputs_read=false`、`formal_outputs_written=false`，不会改变正式年度/季度 CSV。

对冻结正式 CSV 的来源审计显示：年度 249,734 行中 247,801 行、季度 296,957 行中
218,488 行可直接绑定当前 immutable SEC raw snapshot，直接 raw match coverage 均为
1.0；但其余 80,402 行是历史派生选择。公式审计目前只有 73,577 行匹配、6,825
行失败（其中 Q4 operand unresolved 1,987、value mismatch 4,838），所以 raw coverage
100% 不等于当前 parser 可以精确重建 formal annual/quarterly。报告位于
`output/data_provenance/companyfacts_formal_source_audit_manifest-ff932997f3143f50-copy.json`
和 `output/data_provenance/companyfacts_formal_formula_audit_manifest-ff932997f3143f50-copy.json`；
release-selection lockfile 在逐行 proof 完整前必须 fail-closed，不能把当前 raw-only
cache 当成正式发布或 IBKR 准入证据。

当前研究 lockfile `output/data_provenance/companyfacts_release_selection/manifest-ff932997f3143f50-copy.jsonl.gz`
已绑定 copy snapshot `manifest-ff932997f3143f50-copy`：raw 466,289 行、逐行
`derived_proven` 73,577 行、`derived_unproven` 6,825 行。它只证明来源选择和
已审计 operand，不改变正式 CSV；默认 replay 会因 6,825 条未证明派生行而阻断。

- 股票价格中没有超出审计日期的未来行；
- EPS 数据包含 PIT 所需字段。

#### 历史价格缺口的 SEC 线索（研究用）

当 PIT 价格缺口可能与并购、退市或更名有关时，先生成只读 SEC submissions
线索，而不是直接写入价格、`terminal_returns.csv` 或身份映射：

```bash
PYTHONPATH=. .venv/bin/python scripts/sec_submission_triage.py --refresh
PYTHONPATH=. .venv/bin/python scripts/sec_submission_triage.py
```

首条命令原子缓存每个 SEC submissions payload，报告绑定逻辑 payload SHA-256；第二条
只从该缓存离线重建报告。候选标签如
`PRICE_SOURCE_AND_TERMINAL_RETURN_REVIEW` 和 `IDENTITY_TRANSITION_REVIEW`
仅指明下一步人工核查所需证据，绝不等价于已确认的终止收益或更名。任何正式价格、
终止收益或身份数据的修改都必须单独决定并重新验证。

#### 候选价格来源的离线可复核评估（研究用）

当 SEC 线索显示某个价格缺口值得继续调查时，可以先缓存公开历史页的原始字节，
并量化它是否真正覆盖缺口、以及与本地 Nasdaq 行在重叠日是否一致：

```bash
PYTHONPATH=. .venv/bin/python scripts/stockanalysis_price_triage.py --refresh
PYTHONPATH=. .venv/bin/python scripts/stockanalysis_price_triage.py
```

第一条命令只把原始 HTML 封装为带 SHA-256 的 research cache；第二条完全离线复放。
报告会把“完整覆盖且重叠一致”仍标为
`REVIEW_REQUIRES_LICENSE_AND_FORMAL_DATA_AUTHORIZATION`：它只说明下一步可申请来源/
许可和正式数据恢复审查，绝不会自动写入任何价格 CSV、终止收益、身份映射、coverage
或 validation artifact。

### 7.3 重跑固定策略

```bash
PYTHONPATH=. .venv/bin/python -m src.research.can_slim_validation
```

运行后应重新生成第 3 节列出的正式结果。重点检查：

- 年度结果是否与 README 接近；
- `passed_every_historical_year` 是否符合预期；
- 交易成本压力测试是否通过；
- 逐笔账本净值能否与每日回测净值对账。

完整验证只在单次进程内复用只读中间结果：4,142 个价格文件的日期元数据只加载
一次，历史审计与候选覆盖共享同一批 PIT 季度增长快照，0/10/30/50 bps 成本回放
共享与成本无关的 selector 结果和预调整价格。缓存不会跨运行持久化，也不会跳过
输入指纹或门禁。2026-07-31 在当前数据集上的轻量计时由约 231.6 秒降至 136.5 秒；
优化前后回测、账本、年度、成本压力、流动性和候选覆盖产物逐字节一致，summary
除必然变化的全源码 SHA-256 外结构化一致。

八个正式验证产物会先全部写入同目录临时文件，再依次替换；正常异常或某个
`os.replace` 失败时会回滚已经替换的文件，避免留下普通错误造成的混合版本。
包含八个文件大小和 SHA-256 的 `can_slim_validation_artifacts_manifest.json` 最后提交。
会验证当前八个文件是否属于同一次写入，但“manifest 可验证”不等于“仍是最初冻结
版本”。当前工作区这八个文件报告 5/6，策略依赖 SHA 为 `728cc6e340...`，与
2026-07-31 冻结证据 `736b28e72f...` 不同；因此它们只能视为后续 research snapshot，
不能再标成原冻结 validation。正式年度/季度财务 SHA 仍保持不变，release 继续
`BLOCKED`，在明确新模型版本前不要重签或覆盖冻结结论。
POSIX 不提供跨多个文件的断电级原子替换，因此机器断电、内核崩溃或
`SIGKILL` 后不承诺自动回滚；但独立 production gate 和每日推荐流水线都会先验证
manifest，任何缺失、部分更新或混合版本都会拒绝继续。重新运行完整验证成功后，
才能再次执行 production gate。

季度财务发生增量更新后，可把上一个 Release 解压到临时目录，并固定其他全部
输入，只比较两个季度财务版本：

```bash
PYTHONPATH=. .venv/bin/python \
  -m src.research.quarterly_data_version_impact \
  --reference-quarterly /path/to/release/cleaned_stocks_data/financial/quarterly_fundamentals_point_in_time.csv
```

命令不会修改正式财务文件或冻结模型，会生成年度收益差、目标组合变化月份以及
包含双方 SHA-256 和事实键增删数量的 JSON 摘要。任何使历史胜负、目标组合或
候选覆盖发生变化的增量批次，都应先解释其数据来源，再更新 README 和验证产物。

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
流水线任一步骤失败时会原子地把 `pipeline_status.json` 改为 `FAIL` 并记录
异常类型和原因，旧的 `PASS` 不会继续留在状态文件中误导监控；失败发生在推荐
保存之前时，不会向 recommendation history 追加记录。

`pipeline_status.json` 只说明当天任务是否成功；`release_gate.json` 才说明能否
晋级。后者把阻塞拆成 `static_research`、`evidence_integrity` 和
`forward_time_and_performance` 三类，并列出剩余连续交易日、闭合月度周期和
胜出周期。只要静态研究或证据完整性仍有失败，`waiting_only_is_sufficient`
就是 `false`，不会暗示“等满一年即可自动实盘”。

历史终止收益同时保留两个口径：全市场仍有 94 个未解决记录，继续作为数据质量
背景披露；正式逐笔账本交易股票与这 95 个代码的交集为 0，因此
`selected_position_terminal_returns_complete=true`。生产门槛只把实际持仓
相关终止收益作为净值可计算性检查，不会把从未持有股票的终止收益误报为组合
收益缺失；股票池和候选价格完整性仍由独立 PIT 门槛严格阻止，不能借此绕过。

## 9. 每日推荐输出

正式模型目录：

```text
output/daily/can-slim-top3-v1/
```

可能包含：

- `recommendations_YYYY-MM-DD.csv`
- `recommendations_YYYY-MM-DD.json`
- `recommendation_history.csv`
- `recommendation_history.sha256`
- `recommendation_history.provenance.json`
- `recommendation_history.attestation.json`
- `shadow_evaluation.json`
- `pipeline_status.json`
- `release_gate.json`

主要动作：

| action | 含义 |
|---|---|
| `BUY_NEXT_CLOSE` | 当前为调仓执行窗口，计划在下一交易收盘执行 |
| `HOLD_POSITION` | 本月组合已确定，继续持有 |
| `HOLD_CASH` | 市场过滤关闭、没有合格股票，或该信号日尚无生效的冻结模型快照 |

`action_reason` 会进一步区分 `MARKET_REGIME_OFF`、
`NO_QUALIFYING_STOCKS` 和 `MODEL_NOT_YET_EFFECTIVE_AT_EXECUTION`。最后一种表示
本次月度信号对应的执行日早于模型冻结生效日，并非模型文件丢失。即使当前没有可执行模型，
推荐文件也会明确输出一行内部现金标识 `__CASH__`，不再用空文件表示。
之所以不使用 `CASH`，是因为它同时是 Nasdaq 上 Pathward Financial 的真实股票
代码；真实 `CASH` 股票若入选必须按股票正常计价，不能被误当成现金。

股票推荐行还包含只读流动性提示：

- `current_median_dollar_volume_50d`：截至 `as_of` 已知的 50 日中位成交额；
- `full_target_participation_at_100000_account` 与
  `full_target_participation_at_1000000_account`：10 万/100 万美元账户把该股票
  完整建到 `target_weight` 时，占上述成交额的比例；
- `full_target_account_capacity_at_1pct` 与
  `full_target_account_capacity_at_5pct`：完整目标仓位分别受 1%/5% 参与率约束时
  对应的账户容量。

这些字段在首次冻结组合复用后按当前数据重新计算，不会改变股票和权重。它们是假设
从零建到完整目标仓位的保守可读性提示，不是实际调仓差额、建议股数、收盘竞价容量或
自动订单。现金行保持为空值。

如果需要把权重换算为人工参考股数，可显式运行纯本地计算器：

```bash
PYTHONPATH=. .venv/bin/python -m src.research.manual_position_plan \
  --recommendations output/daily/can-slim-top3-v1/recommendations_YYYY-MM-DD.csv \
  --account-equity-usd 100000 \
  --holdings my_holdings.csv \
  --transaction-cost-bps 10 \
  --output output/manual_position_plan.csv
```

`my_holdings.csv` 至少包含 `ticker,shares`；对于不在推荐文件中的现有股票，还需
提供 `current_price`。输出同时包含目标金额、整股目标数量、当前股数、参考增减
数量、成本、剩余现金和账户对应的完整目标参与率。默认使用整股并向下取整；只有
明确增加 `--fractional-shares` 才计算小数股。

该工具不会读取券商账户、不会查询可用资金、不会生成券商订单格式，也不会提交
交易。动作名称使用 `REFERENCE_INCREASE`、`REFERENCE_DECREASE` 和
`REFERENCE_HOLD`，JSON 状态固定为 `REFERENCE_ONLY_NOT_AN_ORDER`。输入价格是
参考收盘价，真实成交前仍需人工核对停牌、公司行动、现金、汇率和实际报价。

Shadow 前向资格以 Nasdaq 官方交易日历中的真实收盘时刻判断；正常交易日按
美东 16:00，黑色星期五等提前收盘日按美东 13:00。执行日若不是 Nasdaq
交易日会直接拒绝计入证据，避免把收盘后生成或日期错误的记录算作前向结果。

对于同一个 `signal_date + model_version`：

- 第一次真实记录的股票集合和权重被冻结；
- 第一次记录对应的 GitHub repository、workflow、run ID、run attempt 和 run URL
  也同时冻结，后续运行不能把旧信号冒充为新 run 生成；
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

`schedule_run.sh` 使用项目内 `.venv/bin/python` 执行每日流程，并在主流程完成后
幂等探测最近已完成的真实收盘，追加 shadow-only 观测和进度状态：

```bash
chmod 755 schedule_run.sh
```

北京时间 09:00 的 crontab 示例：

```cron
0 9 * * 2-6 /data/quant_stocks/schedule_run.sh
```

GitHub Actions 对应北京时间每天 08:30。星期二至星期六处理前一晚已经结束的
美股交易日；星期日和星期一作为冗余重试，避免周五恰逢月末时因一次任务延迟而
错过周一收盘执行前的证据窗口。

注意：

- 定时任务必须运行在持久化目录；
- `output/daily/` 不能每天被删除，否则前向记录会丢失；
- GitHub Actions 每次运行前会分页枚举全部成功 run 和 artifact，从最近一次
  未过期的 canonical artifact 恢复累计 `recommendation_history.csv`；
  SHA-256、provenance manifest 和 Sigstore attestation 必须全部验证通过后
  才允许追加，旧历史不能由当前 run 未经验证地重新签名；运行结束后重新封存
  完整 ledger，artifact 保留 90 天；
- provenance 清单记录 ledger SHA-256、仓库、workflow、GitHub run ID、run attempt、
  运行链接和上一 artifact ID；`shadow_evaluation.json` 会区分内容完整性验证和
  GitHub Actions 外部锚定；
- GitHub Actions 使用 `actions/attest@v4` 为 ledger 和 provenance manifest
  生成 Sigstore 签名的 SLSA build provenance，并用默认分支上的精确 workflow
  身份离线复验；
  签名 bundle 随 artifact 保存。签名、复验或 bundle 保存失败时，不上传当天
  artifact；
- 累计 ledger 只从仓库默认分支的历史 artifact 恢复；功能分支或其他 ref
  即使生成同名 artifact，也不能进入 canonical 前向证据链。每个首次冻结组合
  同时记录 default branch、git ref、40 位 commit SHA 和触发事件；
- 恢复时还会把 artifact 的 `workflow_run.id` 与这个推荐 workflow 的成功
  run ID 列表交叉验证；默认分支上其他 workflow 伪造同名前缀也会被忽略。
  如果 canonical artifact 曾存在但只剩过期版本，任务必须失败，不能把证据
  中断误当成首次启动；
- 找到历史 artifact 但无法唯一恢复 ledger，或 checksum 不匹配时，workflow
  必须失败，不能静默从零重新累计；首次运行或没有任何历史 artifact 时才允许
  创建新 ledger；
- 每次运行会记录市场/财务更新失败数、基准最新日期、价格与财务覆盖率，以及
  `material_missing_strategy_prices`。即使总价格覆盖率仍超过 95%，只要有一只
  缺少基准日价格的股票此前已具备 253 个交易日、价格不低于 `$10` 且 50 日
  中位成交额不低于 `$10M`，推荐流程就必须失败，避免潜在 Top 3 被静默排除；
- 尚未到执行日的 `PENDING_EXECUTION` 只算已记录信号，不累计前向周期或交易日；
- Shadow 绩效使用与正式回测一致的自融资固定持仓账户，按真实换手收费，不做
  每日恒定权重再平衡；
- 生产门槛同时要求至少 252 个不重复前向交易日和 12 个已完成月度周期，防止
  一套过期组合仅靠持有一年就满足时长要求；
- 最后一个仍在持有中的开放周期不计入这 12 个“已完成”周期；逐周期收益从本次
  执行前净值算到下一次执行前净值，使用同一连续自融资账户，因此不会重复收取
  全额建仓成本，也不会遗漏相邻执行日之间的持仓收益；
- 至少 12 个闭合周期中必须有严格多数跑赢 Nasdaq（12 个周期时至少 7 个）。
  即使累计超额为正，若只是一个暴涨月掩盖多数月份落后，仍不能晋级；
- 相邻冻结信号必须来自连续自然月份。若 workflow 漏跑、迟到执行日后才补录，
  或其他故障造成跳月，`evidence_gap_count` 增加；生产门槛使用的 252 个交易日、
  12 个闭合周期、周期胜率和逐周期来源校验全部从最后一次缺口后的下一个及时
  信号重新累计；累计策略收益和 Nasdaq 收益也使用同一连续区间。旧的正常月份
  和历史盈利不能与故障后的月份拼接通过门槛；
- 生产门槛不会读取名为 `out_of_sample_*` 的历史回放字段，也不会使用样本内
  bootstrap 放行。唯一的绩效晋级证据是冻结后、逐周期可追溯且不可回看的
  shadow forward 相对 Nasdaq 表现；
- 即使前向天数和收益达标，如果 ledger 哈希未通过或仅来自可修改的本地文件，
  生产门槛仍保持 `BLOCKED`；
- 每个计入前向绩效的月度周期也必须分别具有合法 GitHub Actions 来源；只要混入
  一个 legacy、本地补录或来源字段冲突的周期，就不能晋级；
- Shadow ledger 必须只包含一个非空 `model_version`；同一信号的执行日必须
唯一，组合股票不得重复，权重必须有限、非负且合计不超过 100%。异常记录会
整体阻止计分，不会通过取最早日期、忽略坏行或合并不同模型继续计算；
- 对同一个 `as_of + model_version` 的重复日流程必须幂等：如果冻结组合和证据
 完全相同，则不追加新行；如果组合发生变化，则直接失败，不能把同一信号伪装成
 新版本写入 ledger；
- Artifact Attestation 证明的是“这些字节由指定 GitHub workflow 在可验证时间
产生”，不证明策略有效、数据正确或未来收益；它不能替代前向周期、数据审计和
  风险门槛；
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

## 14. 研究缓存冷归档与恢复

SEC Company Facts 的 Wayback 历史快照体积约 33GB，不适合提交到 Git。两份
18,595-CIK 快照的全量审计记录在
`output/data_provenance/companyfacts_snapshot_variant_audit_2026-08-10.json`：
16,997 个文件字节完全一致，其余 1,598 个文件去掉顶层 `symbols` 元数据后也完全
一致，`semantic_difference_count=0`。因此冷归档只保存
`wayback-20250414-symbols-v2`；原始 WARC URL、ZIP SHA、capture 时间和展开统计由
`companyfacts_wayback_warc_extraction.json` 绑定。

创建可上传的 zstd 压缩分卷（每卷不超过 1900MB）以及机器可读 catalog：

```bash
scripts/create_research_cache_archive.sh
```

产物位于 `dist/research-cache-sec-companyfacts-2025-04-14/`，catalog 同时写到
`research_cache/sec-companyfacts-2025-04-14.json`。catalog 固定 snapshot manifest、
source evidence、variant audit、整包 SHA-256、每个分卷的大小和 SHA-256。上传到独立的
research-only GitHub Release 时，目录内也会带上两份 evidence JSON，使用：

```bash
gh release create research-cache-sec-companyfacts-2025-04-14 \
  --repo superdiaodiao/quant_stocks \
  --title "Research cache: SEC Company Facts 2025-04-14" \
  --notes "Research-only reproducibility cache; not a formal data release." \
  dist/research-cache-sec-companyfacts-2025-04-14/*
```

已发布的归档入口：
[`research-cache-sec-companyfacts-2025-04-14`](https://github.com/superdiaodiao/quant_stocks/releases/tag/research-cache-sec-companyfacts-2025-04-14)。

下载并恢复时必须显式给出恢复父目录；脚本拒绝覆盖已存在的同名快照。若 parts 目录
已经包含全部分卷（例如手工下载），不会重复联网下载：

```bash
restore_parent="$(mktemp -d)"
scripts/restore_research_cache_archive.sh \
  research_cache/sec-companyfacts-2025-04-14.json \
  dist/research-cache-restore \
  "$restore_parent"
```

恢复流程依次验证每个分卷 SHA、拼接后的整包 SHA、解压后的 snapshot manifest；只有
三层都通过才报告成功。该快照仍缺当前后续研究范围内的 104 个 symbol，只能作为
research archive 和离线复放输入，不能替代正式 annual/quarterly 数据、正式 validation
或解除 `BLOCKED` 状态。

## 15. 常见问题

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
2026 年 1-7 月标为研究者已暴露的复用诊断。只有 2026-08-31 起按冻结协议逐日留下的
完整前瞻记录，才计入正式比较。

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

## 16. 风险说明

- 本项目仅用于研究和辅助决策；
- 回测不代表未来表现；
- 集中持有最多 5 只股票可能产生较大波动和回撤；
- 财务数据、公司行动、退市收益和历史股票池仍可能存在供应商误差；
- 模拟使用收盘成交，不保证实盘一定能以相同价格成交；
- 交易成本压力测试不能覆盖所有冲击成本和流动性风险；
- 在真实使用前，必须完成冻结协议规定的前瞻观察节点并人工复核；时间经过本身不等于通过。

### 研究性 SEC 完成申报证据包

对于 AVDX、PPBI 的历史价格缺口，`scripts/sec_completion_evidence.py` 只缓存 SEC 8-K 原始 HTML 字节、SHA-256 和可离线复放的审阅包：

    PYTHONPATH=. .venv/bin/python scripts/sec_completion_evidence.py --refresh
    PYTHONPATH=. .venv/bin/python scripts/sec_completion_evidence.py

该工具不会改写正式价格、终止收益、证券身份、年度/季度财务 CSV、coverage 或 validation artifacts。AVDX 的现金条款和 PPBI 的换股条款仍须结合有许可的 PIT 价格、最后可交易日与正式影响审计后，才可另行申请正式数据变更授权。

### 不可变 SEC Company Facts 输入快照

活跃 Company Facts cache 可以刷新，因此在任何候选 full rebuild 前应先固定其原始输入快照：

    PYTHONPATH=. .venv/bin/python scripts/companyfacts_cache_snapshot.py
    PYTHONPATH=. .venv/bin/python scripts/companyfacts_cache_snapshot.py --verify-snapshot <snapshot-dir>
    PYTHONPATH=. .venv/bin/python scripts/companyfacts_cache_snapshot.py --create-full-rebuild-scope <snapshot-dir> --rebuild-scope <new-recipe-v2-scope.json>
    PYTHONPATH=. .venv/bin/python scripts/companyfacts_cache_snapshot.py --record-full-dry-run <snapshot-dir> --rebuild-scope <recipe-v2-scope.json>

快照在 cache lock 内把 manifest 引用的原始文件复制到独立目录（新快照的
`storage_method=copy`）；后续活跃 payload 原子替换时，旧快照字节仍可离线复放。
旧版 hard-link 快照只作为兼容格式逐文件校验，不能把已被活跃 cache 原地更新污染的
目录当成 immutable 输入。v2 scope 同时固定该 snapshot 对应的 formal annual/quarterly
ticker 并集、两份 formal 文件 SHA、ticker-set SHA，以及 parser/runtime recipe SHA；它
不从当天可变的 current-universe 文件推导范围。full rebuild 只解析 scope 内有
manifest-bound raw payload 的 ticker；有官方负向证据的不可寻址 ticker 会留在 scope
审计中，但不会被伪造成 raw payload。`--record-full-dry-run` 还会记录 recipe 是否与当前
parser 匹配；它只记录 snapshot、scope、候选 annual/quarterly SHA 和与正式文件的比较，
不会写入正式数据；任何 mismatch 都阻断正式替换，仍需显式授权。

未显式指定 `--rebuild-report` 时，dry-run 报告文件名由 snapshot 与 scope 的语义内容共同寻址；scope 身份包含正式输出 SHA、ticker-set SHA 和 parser recipe SHA。相同输入会复用同一路径，不同 recipe 或正式基线会并存，避免后一次诊断覆盖旧版本证据。

正式 full reparse 也拒绝隐式范围：必须同时给出已验证的不可变 raw snapshot 与该 snapshot 绑定的 scope，例如：

    PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
      --reparse-cache full --dry-run \
      --cache-snapshot <snapshot-dir> --full-rebuild-scope <scope.json>

这条命令仅输出对比；它不改变正式 annual/quarterly CSV 或 reparse state。没有这两个显式输入时，full 模式会在任何正式文件写入前拒绝运行。

### Company Facts formal 来源与 release-selection 证明（研究用）

正式 CSV 的 direct rows 可以用 immutable raw snapshot 做逐行来源审计；派生 rows
还必须通过同一 parser recipe 的 operand/formula 审计。生成证明报告和逐行锁文件：

```bash
PYTHONPATH=. .venv/bin/python scripts/companyfacts_formal_source_audit.py \
  --snapshot <immutable-snapshot-dir> \
  --annual-output output/annual_fundamentals.csv \
  --quarterly-output output/quarterly_fundamentals.csv \
  --output output/data_provenance/companyfacts_formal_source_audit.json
PYTHONPATH=. .venv/bin/python scripts/companyfacts_formal_formula_audit.py \
  --snapshot <immutable-snapshot-dir> \
  --annual-output output/annual_fundamentals.csv \
  --quarterly-output output/quarterly_fundamentals.csv \
  --output output/data_provenance/companyfacts_formal_formula_audit.json
PYTHONPATH=. .venv/bin/python scripts/companyfacts_release_selection_manifest.py \
  --snapshot <immutable-snapshot-dir> --create \
  --annual-output output/annual_fundamentals.csv \
  --quarterly-output output/quarterly_fundamentals.csv \
  --formula-audit output/data_provenance/companyfacts_formal_formula_audit.json \
  --manifest output/data_provenance/companyfacts_release_selection/selection.jsonl.gz
```

lockfile 只接受与 snapshot、formal 文件 SHA 和 parser recipe 完全一致的 formula
audit。direct raw rows 必须在该 snapshot 中找到同一 CIK；派生 rows 只有逐行
`dataset + ordinal + row_sha256` proof 标记为 matched 时才会以 `derived_proven`
进入 replay，其余 rows 默认 fail-closed。`--exclude-unproven-derived` 可生成只含
raw 与 proven-derived 行的研究数据集；`--allow-unproven-derived` 仅用于显式的
研究诊断，不能作为正式 annual/quarterly 替换或 release/IBKR 准入授权。
