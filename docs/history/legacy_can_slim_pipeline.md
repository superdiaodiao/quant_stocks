# 已停用：CAN SLIM Top3 日常流水线

> 从原 README 第 8、9、10、12、13 节移来。这套 `can-slim-top3-v1` 日常流程已停用，对应的 GitHub 工作流 `workflow_run_script.yml` 已于 2026-09-26 删除（可在 git 历史里找回）。当前运行的是 v50r3，见 [运维手册](../v50r3_operations.md)。

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
