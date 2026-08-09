# IBKR 自动交易可行性评估（研究版）

更新时间：2026-08-02  
结论：`NOT_READY / BLOCKED`。本文件是项目准入证据的整理，不连接 IBKR、不创建订单，也不改变冻结策略。

## 结论摘要

IBKR 在技术上可以承载“月度选股、收盘附近执行、每日状态检查”的交易系统，但当前项目尚未达到 paper 或真实资金准入。当前代码只生成推荐、账本和人工参考仓位计划；没有券商连接器或订单提交路径。

阻断原因是研究证据而非缺少一个 API 开关：

- 冻结后 shadow 尚未积累 252 个连续交易日和 12 个闭合月度周期；
- shadow ledger 仍是本地文件，未获得外部时间/不可变存储锚定；
- PIT 股票池存在 2023-05-31 快照缺口；
- 30 bps 成本压力下历史胜率仍只有 4/6 年；
- 当前 release gate 明确为 `BLOCKED`。

## 九项可行性检查

### 1. API 与运行环境

Client Portal API、TWS API 和 IB Gateway 都可以作为候选执行接口。正式实现前必须在专用 paper 账户验证认证、会话续期、断线重连、订单状态订阅和账户/持仓查询；本仓库当前没有任何 IBKR 登录或下单代码。

### 2. 运行位置

GitHub Actions 适合生成不可变推荐和 provenance，不适合作为需要长期保持券商会话的唯一执行主机。云主机或家用常驻主机都必须单独验证网络、2FA、进程守护、时钟和重连；任何环境都不能绕过 IBKR 账户权限和人工安全控制。当前 workflow 不应启用。

### 3. 月度选股与收盘执行

正式策略是月度选股、下一个交易日收盘执行；每日任务只更新数据、状态和 shadow 记录，不重新选股。执行器必须使用美股交易日历、明确的 America/New_York 截止时间，并在北京时间跨日时保持幂等。迟到、漏跑或尚未到执行日的记录不得计入前向绩效。

### 4. 订单与公司事件

fractional shares、MOC/LOC、部分成交、拒单、停牌、公司行动、现金和汇率都必须以 paper/真实回报校准。当前参考仓位计划默认整股向下取整，并明确标记为 `REFERENCE_ONLY_NOT_AN_ORDER`；它不是成交证据。

### 5. 成本对齐

冻结回测使用单边 10 bps；研究压力测试已覆盖 0/10/30/50 bps，但 30 bps 下仍只有 4/6 年跑赢。真实成本必须拆分佣金、交易所/监管费、点差、滑点和汇率影响，并由 paper 成交回报重新估计，不能把 10 bps 当作实盘承诺。

### 6. 安全与恢复

任何未来执行器都必须具备订单幂等键、目标/实际仓位对账、拒单和部分成交恢复、kill switch、人工确认、审计日志和最大名义金额限制。当前项目仅实现研究账本和人工参考计划，未实现券商订单生命周期，因此不具备自动交易能力。

### 7. Shadow → Paper → 小资金

晋级必须按冻结策略积累至少 252 个连续前向交易日、12 个闭合月度周期，其中至少 7/12 个周期跑赢 Nasdaq；每个周期还必须有合法外部来源，ledger 哈希必须可验证。达到这些条件后仍需独立 paper 阶段，再由人工批准小资金，而不是自动切换。

### 8. 北京时间运行日程

北京时间通常在美股交易日晚上进入下一自然日。调度器应在美股收盘确认后生成状态，执行窗口按交易所实际日历计算，避免把周末、节假日或夏令时切换当成正常执行日。当前 `daily_pipeline` 仍是研究/shadow 流程。

### 9. 最小闭环与明确结论

最小闭环是：冻结推荐 → 人工核对 → paper 账户下单 → 回报/持仓对账 → 不可变审计 → kill switch 演练。当前只完成冻结推荐、研究账本和人工参考计划，尚未完成后四项。因此结论是：**研究框架可继续验证，当前不应自动交易，也不应连接真实 IBKR 账户。**

## 证据索引

- release gate：`output/daily/can-slim-top3-v1/release_gate.json`
- shadow 状态：`output/daily/can-slim-top3-v1/shadow_evaluation.json`
- 人工参考计划：`output/manual_position_plan_2026-07-31.csv`
- 冻结策略回放：`output/can_slim_fixed_top3_summary.json`
- 正式财务与 coverage：`cleaned_stocks_data/financial/`
- 调度、账本和门禁实现：`src/research/daily_pipeline.py`、`src/research/shadow_ledger.py`、`src/research/production_gate.py`

