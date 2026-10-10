# 项目架构（目标设计与分阶段迁移）

> 状态：第 1 阶段（共享核心库 `quant/` + 3 个试点）、第 2 阶段（其余 10 月份研究、`quant/strategies`、
> `quant/observation`）和第 3 阶段（数据流水线进 `pipelines/`、各数据商代码进 `quant/data/sources/`、v2.1 进
> `quant.data.version`，见第 10 节）已完成；第 4 阶段（旧研究脚本移到 `archive/`）另行进行。
> 本文用中文说明，代码里的名字（模块、函数、目录）保持英文。

## 1. 为什么要改

现状（2026-10，master `dc99a861f`）：

- `scripts/` 有约 450 个文件：297 个是 v14–v51 的旧研究脚本，约 40 个是 10 月份的研究
  （`research_<主题>.py`），其余是数据流水线 `reversal_data_*`、前瞻观察 `forward_*`、SEC/Yahoo 修复脚本等。
- **最大的问题：10 月份的研究互相当库用。** 例如：成本模型在 `research_reversal_dev`，面板加载在
  `research_livermore`，ONEQ 基准在 `research_indicators`，月度/日期守卫在 `research_qqq_timing`，
  t 值/IR 在 `research_calendar`。于是改一个研究就可能悄悄改了另一个研究（甚至前瞻观察）的结果。
- 每个研究又各自重写一遍指标（CAGR、最大回撤、月度超额 t 值）、对 ONEQ 的 A/B 通过标准、Bonferroni、
  成本模型调用和模拟器。同一个东西有 3–9 份拷贝，写法略有不同（例如两个同名的 `max_drawdown`，一个吃
  “净值序列”，一个吃“收益序列”）。
- `src/` 里是更早的模块，主要服务于已冻结的 v50/sue 实盘协议；`qc/` 是 QuantConnect 算法，必须自包含。

## 2. 目标结构

```
quant/                      可复用的核心库（新）；src/ 保留给已冻结的旧代码
  paths.py                  ROOT（当前检出）、MAIN_CHECKOUT、CACHE_ROOT（大缓存只在主检出里）
  data/                     数据层：只读数据、做日期守卫，不做任何策略判断
    version.py              数据版本开关 v1 / v2 / v2.1：唯一声明处，研究和流水线都读它
    guards.py               日期守卫：assert_dev_dates / truncate_dev，assert_window / truncate_window
    calendar.py             交易日历：month_end_mask、last_session_of_each_month
    rates.py                无风险利率：Ken French 日 RF + FRED DTB3 补尾
    benchmarks.py           基准：oneq_on_sessions（ONEQ 是所有通过标准的比较对象）
    panel.py                Nasdaq 时点股票面板：load_window、make_index、退市终值常数
    ohlc.py                 按真实成交价重建的日线 OHLC（整股引擎、挂限价单用）
    market_cap.py           时点市值（SEC 股数 → 公众流通值 → Nasdaq 名单 → 成交额代理）
    sources/                各数据商：请求地址、带缓存和请求台账的下载、解析（密钥只从环境变量/.env 读，绝不打印）
      http.py               cached_get（先缓存再解析、404 记标记、请求台账 Ledger 由调用方给）、限速器、原子写、脱敏
      sec.py                SEC 联系人（src/io/sec_contact，绝不打印）、每秒 7 次的共享限速器、请求头
      yahoo.py              parse_chart、parse_ohlc、split_events、parse_ohlc_payload；v8 图表请求地址 chart_url
      tiingo.py / alpaca.py 接口地址、ticker 的 URL 写法、返回体解析
      archive_org.py        Wayback 原始抓取地址、Yahoo 旧页面（table.csv、q/hp、quote/history）解析、raw_from_capture
      companiesmarketcap.py 搜索结果 / 猜测页面属于哪个 ticker（cmc_pick、cmc_slugs）
      quantquote.py         QuantQuote 免费标普 500 日线包的读取
      ibkr_borrow_fees.py   IBKR 借券费文件解析和每日快照读取（记录器 scripts/record_borrow_fees.py 必须自包含）
      megacap_oos2.py       1998–2013 超大盘样本外的取数和解析（SEC 封面、Wayback、Yahoo、CMC、Tiingo）
      sec_cache.py、daily_series.py   分拆 / NDX 研究的缓存请求（第 2 阶段）
  signals/                  信号：纯函数，只用到 t 日收盘为止的数据
    technical.py            sma_state、trailing_return、vol_state、inverse_vol_weights、rsi_wilder、momentum_frames
  strategies/               每个策略一个模块：规则 + 引擎（从研究里原样抽出）；registry.py 登记冻结的命名策略
  backtest/                 回测层
    costs.py                IBKR Pro Tiered 成本：etf_order_cost（ETF 简化模型）、ibkr_order_cost（股票完整模型）、
                            rank_half_spread、tick_half_spread
    execution.py            执行约定：分位取整、限价单“穿价成交”、信号次日执行、整股买入持有
    target_weights.py       ETF 配置引擎：目标权重表，目标变化才交易
    rebalance.py            股票组合引擎：按总回报指数持仓，先卖后买，25% 不交易带
  evaluation/               评估层：只吃收益/净值序列
    metrics.py              cagr_of、max_drawdown、monthly、yearly、t_and_ir、core_metrics、relative_metrics …
    criteria.py             A/B 通过标准（ab_criteria）、bonferroni_t、bh_reject、deflated_sharpe
    periods.py              两折切分 halves、return_period_metrics、nav_window_metrics
  observation/              前瞻观察：runner（入口 scripts/forward_observation.py）、prices、smisp
  cli.py                    python -m quant list / study <name> / strategies / run <strategy>
  prereg.py                 事先登记块（PREREG-BEGIN/END）的摘要和冻结文件
studies/                    每个事先登记的研究一个文件：读数据 → 策略 + 引擎 → 评估 → 写 output/research_only/<name>/
pipelines/                  数据构建：只写数据文件；研究只读它们的输出，从不导入 pipelines
  reversal_data/            反转 2012–2026 数据：每个步骤一个模块，build.py 是 v1 / v2 / v2.1 通用的不动点运行器
  megacap_oos2/build.py     超大盘样本外（OOS.2）写输出的步骤（qqq、sec）
qc/                         不动（QuantConnect 算法必须自包含）
scripts/                    （第 4 阶段后）只剩薄的命令行入口和旧命令的转发
archive/                    （第 4 阶段）旧研究脚本，只移动不删除
src/                        已冻结的旧代码（v50/sue 实盘机器），不再往里加新东西
```

相对最初提案的改动和理由：

1. **`evaluation` 不依赖 `data`。** 指标只吃序列，这样单元测试不需要任何数据文件。
2. **`cli.py` 是唯一可以“往上看”的地方。** 它要列出并运行 `studies/`，所以它导入 `studies`；`quant` 的其它
   模块都不允许导入 `studies`。
3. **`quant` 只从 `src/` 借一样东西：** `src/research/ibkr_cost_calibration.base_stock_commission_usd`
   （已校准的 IBKR Tiered 佣金表）。这是只读复用，不改它；第 2 阶段可以把它复制进 `quant.backtest.costs`
   并由测试保证一致。
4. **同一概念的不同口径都保留，并起不同的名字。** 冻结结果是用它们算出来的，统一口径会改变数字：
   `etf_order_cost`（ETF 简化成本）与 `ibkr_order_cost`（股票完整成本）；`max_drawdown`（净值）与
   `max_drawdown_of_returns`（收益）；`assert_dev_dates`（比较日期，只管终点）与 `assert_window`
   （字符串比较，管两端）。
5. **研究专属的参数不放进 `quant`。** `quant` 的函数没有隐藏的模块状态，所有阈值都是参数
   （例如 `market_caps(..., shares_vs_float=...)`、`simulate_index_units(..., qqq_hs=...)`）；研究模块把
   冻结值绑定进去。

## 3. 依赖规则

```
studies/<name>.py ──► quant.strategies ──► quant.signals ─┐
        │                                                 ├──► quant.data
        └──────────► quant.backtest ──────────────────────┘
        └──────────► quant.evaluation   （不依赖任何其它层）
quant.cli ──► studies（只有它）
```

- 研究只导入 `quant`，**研究之间不互相导入**。两个研究需要同一段代码时，把它挪进 `quant`。
- `quant.data` 不导入 `signals / backtest / evaluation / strategies`。
- `pipelines/` 可以导入 `quant`（版本开关、各数据商代码）；`quant` 和 `studies` 都不导入 `pipelines`，也不导入
  任何数据脚本（`tests/test_pipeline_layout.py` 检查）。
- `quant.signals`、`quant.evaluation` 是纯函数：不读文件、不联网、不依赖模块级可变状态。
- `qc/` 不导入 `quant`（QuantConnect 上跑不了本地包）。
- **已冻结的代码不导入 `quant` 或 `studies`**（见第 6 节：闭包摘要目前只追踪 `src`、`scripts`）。

## 4. 命名规范

- 模块、函数、变量：英文 `snake_case`；类：`CamelCase`；冻结常数：`UPPER_CASE`，放在研究文件顶部。
- 研究文件名 = 台账主题名：`studies/<topic>.py` ↔ `docs/research_ledger_<topic>.md` ↔
  `output/research_only/<topic>/`（数据版本 v2 写 `<topic>_v2/`，由 `quant.data.version.versioned` 决定）。
- 函数名说清楚口径：`*_of_returns` 吃收益，否则吃净值；`*_bps` 是基点，否则是小数；
  日期参数用 ISO 字符串 `YYYY-MM-DD`。
- 每个研究模块的结构固定：文档字符串（规则、执行、成本、数据、输出、用法）→ 冻结参数 → 规则 →
  `run(args)` → `main(argv)`。

## 5. 台账和文档如何引用代码

- 新写的台账引用 `studies/<name>.py` 和命令 `PYTHONPATH=. .venv/bin/python -m quant study <name>`，
  并写上运行时的提交号。
- 已写进台账的旧命令（`scripts/research_<name>.py`）继续有效：迁移后的旧脚本是一个转发文件，
  `python scripts/research_<name>.py` 调用新模块的 `main`；`import scripts.research_<name>` 得到的就是
  `studies.<name>` 这个模块对象本身（`sys.modules` 别名），所以其它脚本读写 `mc.SHARES_VS_FLOAT` 这类
  属性仍然作用在真正的代码上。转发文件在第 4 阶段之后也保留。
- 输出目录名、输出文件名、文件格式在迁移中**一律不变**；台账里引用的结果文件因此仍然有效。

## 6. 冻结协议怎么处理

以下内容在任何阶段都**不修改、不移动、不改指向**，除非主人重新冻结：

| 冻结对象 | 范围 | 怎么验证 |
|---|---|---|
| v50 / v50r2 / v50r3 实盘协议 | `scripts/research_v50*_corrected_v47.py`、`v50r3_*.py`、`research_v50r3_*` 及其导入闭包 | `python scripts/research_v50*_corrected_v47.py status`；`src/research/code_closure.py` 计算闭包摘要，`.github/workflows/tests.yml` 每次推送都检查 |
| sue_lt_v1 | `scripts/sue_lt_v1*.py` 及其闭包（docs/sue_lt_v1_protocol.md，约 2026-10-30 冻结） | `python scripts/sue_lt_v1.py status` |
| 工作流引用的文件 | `.github/workflows/*.yml` 里出现的所有文件（含 `tests/data_dependent_test_files.txt`） | 迁移前后对照工作流里的路径 |
| QuantConnect 主程序 | 台账里记了哈希的 `qc/*/main.py` | 哈希比对 |
| 前瞻观察的行为 | `tests/golden/forward_observation_synthetic.json` 锁定 | `tests/test_forward_observation.py` |

迁移规则：

1. 动手前先算一遍所有冻结根（工作流引用的脚本、`research_v50*`、`v50r3_*`、`sue_lt_v1*`）的导入闭包，
   **被改动的文件不得出现在任何闭包里**。
2. `code_closure.PROJECT_PACKAGES` 只有 `("src", "scripts")`，它看不见 `quant/` 和 `studies/`。所以冻结代码
   一旦导入 `quant`，闭包摘要会漏掉这部分代码。要让冻结代码用上 `quant`，必须先扩展 `code_closure`
   （它本身也在闭包里），再由主人重新冻结。在那之前，冻结代码只能继续用它自己的副本。
3. 前瞻观察不是闭包冻结而是行为冻结：它导入的研究模块（如 `research_selective_t`、`research_megacap`）
   可以迁移，但必须让前瞻观察的黄金测试保持通过。

## 7. 分阶段计划和验收标准

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| 1（已完成） | 写本文；从 10 月份研究里**抽取**（不重写）共享部分建 `quant/`：成本模型、数据加载（面板、基准、版本开关）、指标与通过标准、执行约定；试点迁移 3 个形态不同的研究 | `tests/quant/` 单元测试证明每个 `quant` 函数与原函数结果完全相等；3 个试点的全部输出文件与迁移前**逐字节相同**；旧命令和旧导入仍可用；相关测试全部通过；冻结检查通过 |
| 2（已完成） | 其余约 37 个 10 月份研究和前瞻观察迁到 `quant`（`studies/` + `quant/strategies` + `quant/observation`）；研究之间不再互相导入；`python -m quant run <strategy>` | 每个研究：迁移前生成黄金输出，迁移后逐字节相同（做不到的写明原因，并且数值差 ≤ 1e-12）；前瞻观察黄金测试通过；`grep "from scripts import research_" studies quant` 为空 |
| 3（已完成） | 数据流水线整理到 `pipelines/reversal_data/`，下载器进 `quant/data/sources/`；v2.1 进 `quant.data.version` | 用同一份原始缓存离线重建 v1 / v2 / v2.1，改动前后输出逐字节相同（第 10 节） |
| 4 | 约 300 个 v14–v51 旧研究脚本移到 `archive/`（只移动，不删除）；`scripts/` 只剩命令行入口和转发文件 | 冻结闭包内的文件不动；`compileall` 通过；所有台账里的命令仍能找到文件（转发或在 `archive/` 的明确新路径） |

每个阶段单独提交。

## 8. 第 1 阶段做了什么

### 8.1 抽取到 `quant/` 的内容（原函数 → 新位置）

| 原位置 | 新位置 |
|---|---|
| `research_qqq_timing`：`order_cost` 与佣金常数 | `quant.backtest.costs.etf_order_cost` |
| `research_reversal_dev`：`order_cost`、`half_spread`、费用常数、`REBALANCE_BAND` | `quant.backtest.costs.ibkr_order_cost`、`rank_half_spread` |
| `research_megacap` / `research_canslim_dev`：`order_cost`（按金额） | `quant.backtest.costs.ibkr_value_cost` |
| `research_intraday_t`：`hs_of` | `quant.backtest.costs.tick_half_spread` |
| `research_intraday_t`：`ceil_cent`、`floor_cent`、`fill_t1`、`fill_t2`、`buy_hold_oneq` | `quant.backtest.execution`：同名取整、`fill_sell_limit`、`fill_buy_limit`、`buy_hold_returns` |
| `research_regime`：`simulate`、`buy_hold` | `quant.backtest.target_weights` |
| `research_megacap`：`simulate`、`buy_hold` | `quant.backtest.rebalance` |
| `research_qqq_timing`：`cagr_of`、`max_drawdown`、`monthly`、`yearly`、`cagr_months`、`bonferroni_t`、`deflated_sharpe` | `quant.evaluation.metrics` / `criteria` |
| `research_calendar`：`t_and_ir`、`core_metrics`、`relative_metrics`、`longest_drawdown` | `quant.evaluation.metrics` |
| `research_regime`：`period_metrics`、`halves`、`evaluate` 的 A/B 判断 | `quant.evaluation.periods`、`criteria.ab_criteria` |
| `research_megacap`：`window_metrics`、`longest_drawdown_days`、`criteria` 的 A/B 判断 | 同上 |
| `research_selective_t`：`judge` 的 A/B 判断、`bh_reject`、`rsi_wilder`、`ohlc_frame` | `criteria`、`signals.technical`、`data.sources.yahoo` |
| `study_data_version` | `quant.data.version` |
| `research_qqq_timing` / `research_canslim_dev`：日期守卫 | `quant.data.guards` |
| `research_qqq_timing`：`parse_chart`、`load_kf_rf`、`load_dtb3`、`fill_rf`；`research_calendar.parse_ohlc`；`research_intraday_t.split_events` | `quant.data.sources.yahoo`、`quant.data.rates` |
| `research_indicators.oneq_on_sessions` | `quant.data.benchmarks` |
| `research_livermore.load_window`、`research_reversal_dev.make_index` | `quant.data.panel` |
| `research_intraday_t`：`Series_`、`build` | `quant.data.ohlc.OHLCSeries`、`build_real_ohlc` |
| `research_megacap`：市值相关 8 个函数 | `quant.data.market_cap` |
| `research_regime`：`month_end_mask`；`research_megacap.signal_sessions` | `quant.data.calendar` |
| `research_regime` 的信号函数；`research_megacap.momentum_frames` | `quant.signals.technical` |

原函数当时原样留在还没迁移的脚本里；第 2 阶段迁移那些研究时已删除这些副本（见第 9 节）。

### 8.2 三个试点

| 研究 | 形态 | 迁移后 | 等价性 |
|---|---|---|---|
| `regime` | ETF 择时 / 配置（目标权重引擎） | `studies/regime.py` | 4 个输出文件逐字节相同，标准输出相同 |
| `megacap` | 选股（时点市值、月度调仓引擎） | `studies/megacap.py` | 7 个输出文件逐字节相同，标准输出相同 |
| `selective_t` | 事件 / 交易级（限价单穿价成交、T+1 结算） | `studies/selective_t.py` | 7 个输出文件逐字节相同；标准输出只有 numpy 警告里的文件路径不同（警告现在从 `quant/evaluation/metrics.py` 发出） |

验证方法：迁移前用旧脚本在同一份数据上跑两次（结果完全相同，说明可重复），保存全部输出；迁移后再跑，
逐文件 `diff`；文件哈希写进 `tests/golden/pilot_studies_sha256.json`，`tests/quant/test_pilot_goldens.py`
以后每次都能复查。

把这三个模块当库用的下游脚本也在迁移前后各跑一次：voltarget、sector_lev、mean_reversion、leverage_methods
（完整运行）和 fundamentals、ml_cross_section（`--check`，其中 fundamentals 走了临时改
`mc.SHARES_VS_FLOAT = inf` 的路径）输出逐字节相同。index_exclusion、short_overlay、megacap_oos2/3、t_grid
需要干净检出里没有的上游输出，没有整跑；它们的单元测试、`tests/quant/test_study_library_surface.py`
（锁定 `mc.SHARES_VS_FLOAT`、`mc.QQQ_HS` 临时覆盖仍然生效）和前瞻观察黄金测试都通过。

### 8.3 已知问题（留给后续阶段）

- （已解决）`.github/workflows/tests.yml` 现在也运行 `tests/quant`（主人在 22ff8cf15 加上）。
- `code_closure` 看不见 `quant/`、`studies/`（见第 6 节第 2 条）。
- `cagr_of` 在模拟净值跌破 0 时得到 NaN 并发出 numpy 警告（`selective_t` 的保证金变体里出现过）；这是原有
  行为，为保证结果不变没有修改。

## 9. 第 2 阶段做了什么

### 9.1 迁移表

每个研究都是把 `scripts/research_<name>.py` 原样移到 `studies/<name>.py`（保留历史），旧路径留一个 12 行的转发
文件（运行时调用 `main`，被导入时就是新模块本身，见第 5 节）。研究之间共用的代码一律原样（不改一个算式）挪进
`quant`：

| 研究 | 共用部分挪到 |
|---|---|
| qqq_timing | `data.index_history`（QQQ/NDX 拼接、QLD、RF）、`backtest.exposure`（现金/1x/2x 套筒引擎）、`data.synthetic`（合成 2x）、`signals.technical`（trend_state、realised_vol、month_end_only）、`evaluation.metrics.exposure_metrics`、`criteria.timing_criteria` |
| calendar、intraday_t、reversal_dev | 第 1 阶段已抽出的函数；本地副本删除 |
| dow_theory、voltarget、leverage_methods | 只用上面这些 |
| sector_lev | `data.sector_etfs`（ETF 面板与各条腿）、`backtest.flagged_weights`、`evaluation.equity`、`criteria.ab_verdict` |
| regime（第 1 阶段） | `data.etf_panel`、`backtest.target_weights.two_state / entry_index`（mean_reversion 用） |
| canslim_dev | `data.eps`（时点 EPS）、`strategies.canslim`（加载、筛选、引擎、指标） |
| livermore | `strategies.livermore`；研究窗口 `WINDOWS` 挪到 `data.panel` |
| indicators、grid_check、walk_forward、stops | `signals.indicators`、`strategies.indicators`（含 grid_check 的参数网格 `make_rule`）；`data.calendar.decision_year`；stop_rules → `backtest.stop_rules` |
| oneil、earnings_events、mean_reversion、osap_screen、osap_backtest、sec_alt | 只用上面这些；各自的窗口不再写进 livermore 的 `WINDOWS`，而是直接传给 `panel.load_window` |
| megacap（第 1 阶段） | `strategies.megacap`（M1–M6 规则、`build_targets`、引擎绑定） |
| fundamentals、ml_cross_section | `strategies.fundamentals`、`strategies.ml_features`、`evaluation.stats.t_sf`、`quant.prereg` |
| short_overlay、index_exclusion | `strategies.short_overlay`（index_exclusion 也用 `quant.prereg`） |
| megacap_oos2、megacap_oos3 | `data.megacap_history`（1998–2013 时点市值排名的构建） |
| spinoffs（+ _events、_sec）、ndx_recon | `data.sources.sec_cache`（原 research_spinoffs_sec）、`data.spinoff_events`（原 research_spinoffs_events）、`data.sources.daily_series`、`backtest.costs.volume_tier_half_spread / ibkr_order_total` |
| selective_t（第 1 阶段）、t_grid | `strategies.selective_t`、`strategies.t_grid`（前瞻观察 B1、B2 的引擎） |
| benchmark_composite | 只用 `evaluation.metrics` |
| forward_observation / forward_prices / forward_smisp | `quant/observation/runner.py / prices.py / smisp.py`；工作流不变（仍调用 `scripts/forward_observation.py`） |

原来几个研究“临时改别的模块常数”的写法改成了显式参数，结果不变：`research_megacap.SHARES_VS_FLOAT = inf` →
`market_caps(..., shares_vs_float=np.inf)`；`QQQ_HS = ONEQ_HS` → `simulate(..., qqq_hs=ONEQ_HS)`；
`research_qqq_timing.HALF_SPREAD = hs` → `exposure.simulate(..., half_spread=hs)`；往 livermore 的 `WINDOWS`
里登记窗口 → 直接把窗口传给 `load_window`。`grep "from scripts import research_" studies quant` 为空，
`tests/quant/test_study_library_surface.py` 检查研究之间没有互相导入。

### 9.2 冻结策略登记表

`quant/strategies/registry.py` 是冻结参数唯一的声明处：S3-Yb（B1）、SEL-A / SEL-P（B2，t_grid 配置 29876 /
29916）、S-MISP N10 k20 MN（B3）、S/P top-10（只作引用：`qc/sp_top10_trade/main.py`，在 QuantConnect 上运行）、
M1–M6。`strategies.megacap.RULES`、`strategies.selective_t.STOCKS`、前瞻观察的 B1/B2/B3 参数都从这里读。
`python -m quant strategies` 列出它们，`python -m quant run <名字> [参数]` 调用登记的入口（观察中的策略就是前瞻
观察本身，例如 `run SEL-A --dry-run`）。登记表不导入任何 `quant` 模块，保证前瞻观察能在 `quant.data.version`
加载之前把数据版本设成 v2。

### 9.3 等价性怎么验证的

做法同第 1 阶段：基准是 master `22ff8cf15` 的一份完整拷贝，新代码是另一份拷贝，两边放入同一份
`output/research_only`（上游研究的冻结文件、预测等），用同一个脚本依次运行每条命令，记录每条命令新写出的每个
文件的哈希，逐文件比较（标准输出也比较）。结果：

- 全部命令（每个研究的默认 / dev 运行和台账里的其它模式，含 livermore / indicators / oneil 的一次性检验在拷贝
  里重跑；数据 v2 研究的 --check / --run）的输出文件逐字节相同，标准输出相同。仅有的差别都与内容无关：`.gz`
  文件头里的时间戳；summary / results JSON 里的 `runtime_s`；short_overlay / index_exclusion 的 `run_log.txt`
  里的耗时；numpy 警告里的文件路径。
- 前瞻观察：`tests/golden/forward_observation_synthetic.json` 通过；`--dry-run --as-of 2026-10-09` 在两份拷贝里
  的完整输出相同；`tests/test_forward_observation_entry.py` 检查工作流调用的入口在新进程里能运行并选中数据 v2。
- 哈希记录在 `tests/golden/phase2_studies_sha256.json`：`inprocess` 部分由 `tests/quant/test_phase2_goldens.py`
  每次重跑核对（慢的几项设 `QUANT_GOLDEN_SLOW=1` 才跑）；`harness` 部分（一次性检验、数据 v2 研究、读自己旧输出
  的研究、几分钟以上的运行）是上面两份拷贝对照的记录。
- `tests/quant/test_quant_core.py` 继续把每个 `quant` 函数和原函数比较；原函数已从脚本里删掉，所以在
  `tests/quant/originals/` 留了一份冻结拷贝（只给测试用）。

### 9.4 依赖规则的例外（第 3 阶段已处理前三条，见 10.4）

- `quant.data.megacap_history` 导入 `scripts/megacap_oos2_data.py`（OOS.2 的取数脚本）；megacap_oos3 研究直接用
  `scripts/reversal_data_common.cached_get`，spinoffs 用 `scripts/reversal_data_v2_archive`：这些是数据流水线，
  第 3 阶段搬进 `pipelines/` 和 `quant/data/sources/`。
- `quant.data.sector_etfs.data_checks` 用了 `evaluation.metrics` 的 `cagr_of`、`monthly`（纯函数）。
- `quant` 读 `src` 的地方：`ibkr_cost_calibration`（成本表）、`io.sec_contact`（SEC 联系人）、
  `io.security_universe`（S-MISP 观察）。
- 研究里仍有不少“研究专属但写法相近”的函数（各自的 period_metrics、perf_metrics、criteria 标签等），它们的
  口径或输出键不同，合并会改变输出，保持原样。

## 10. 第 3 阶段做了什么

### 10.1 路径对照表

步骤模块是原文件原样移动（先单独提交一次纯移动，保留 `git log --follow` 历史），之后只改导入；旧路径留转发文件：
运行旧命令 = 用 `runpy` 把新模块当 `__main__` 运行（与原来“脚本作为 `__main__`、被别处导入的是另一份模块”的行为
完全一样）；导入旧路径 = 得到新模块本身（`sys.modules` 别名，同第 5 节）。

| 原路径 | 新路径 |
|---|---|
| `scripts/reversal_data_<step>.py`（common、form25、security_master、listings、wiki、prefilter、tiingo、yahoo、reconcile、review、terminal、earnings、factors、universe、validate、v2_archive、v2_fill、v2_report、v2_1_alpaca、v2_1_fill、v2_1_report） | `pipelines/reversal_data/<step>.py` |
| `scripts/reversal_data_v2_build.py`、`scripts/reversal_data_v2_1_build.py` | `pipelines/reversal_data/build.py --version v2 / v2.1`（一个运行器；旧命令转发并带上版本） |
| `scripts/data_source_probe.py` | `pipelines/reversal_data/source_probe.py` |
| `scripts/robustness_data_v2_compare.py` | `pipelines/reversal_data/robustness_v2_compare.py` |
| `scripts/megacap_oos2_data.py` | 取数 / 解析 / 读取：`quant/data/sources/megacap_oos2.py`；写输出的步骤（qqq、sec）：`pipelines/megacap_oos2/build.py`（重新导出旧模块的全部名字） |
| `scripts/study_data_version.py` | `quant/data/version.py`（两份副本合成一份；旧路径是它的别名） |
| `reversal_data_common` 的 cached_get、限速器、原子写、脱敏、读 .env | `quant/data/sources/http.py`（请求台账 `Ledger` 由调用方给；`common` 在每次调用时取自己的 `RAW_INDEX` / `QUOTA_LEDGER`，所以探针、v2 抓取器、Alpaca 改台账的写法照旧有效） |
| `reversal_data_common` 的 SEC 联系人、每秒 7 次限速器、请求头 | `quant/data/sources/sec.py`（联系人仍只从 `src/io/sec_contact` 读，绝不打印） |
| `reversal_data_tiingo`：API、url_ticker、parse_body、is_quota_text、to_frame | `quant/data/sources/tiingo.py` |
| `reversal_data_v2_1_alpaca`：BASE、ADJUSTMENTS、CA_TYPES、bars_frame、corporate_actions | `quant/data/sources/alpaca.py` |
| `reversal_data_yahoo`：CHART、HEADERS、chart_url | `quant/data/sources/yahoo.py` |
| `data_source_probe` 的 Yahoo 旧页面解析 + `reversal_data_v2_archive` 的 URL 解析、带成交量的解析、raw_from_capture | `quant/data/sources/archive_org.py`（新增 `capture_url`，探针、v2 抓取器、分拆研究共用） |
| `data_source_probe`：cmc_pick、cmc_slugs、CMC_SUFFIX | `quant/data/sources/companiesmarketcap.py` |
| 探针和 v2 抓取器各写一遍的 QuantQuote 读取 | `quant/data/sources/quantquote.py` |
| `quant/observation/smisp.py`：parse_borrow_text、BorrowSnapshots | `quant/data/sources/ibkr_borrow_fees.py`（smisp 从这里导入，名字不变） |

不动的：`scripts/record_borrow_fees.py`（借券费记录器）。`.github/workflows/borrow_fees.yml` 只稀疏检出这一个文件、
用裸 `python3` 运行，它导入不了本仓库的包，所以它必须保持自包含；它也是工作流引用的冻结文件（第 6 节）。读取端在
`quant/data/sources/ibkr_borrow_fees.py`。

### 10.2 版本开关

`quant.data.version` 是 v1 / v2 / v2.1 的唯一声明处（`VERSIONS`、`CACHE_NAME`、`INPUTS_NAME`、`V2_PLUS`、
`IS_V2_1`、`RAW_INDEX` / `QUOTA_LEDGER`），流水线的 `common` 和研究都从这里读。`IS_V2` 的意思不变（恰好是 v2），
所以事先登记在冻结 v2 上的研究（ml_cross_section、short_overlay、index_exclusion、前瞻观察 B3）遇到 v2.1 仍然拒绝；
其它研究在 v2.1 下读 `inputs_v2_1/`、写 `<study>_v2_1/`（`versioned`）。

`REVERSAL_DATA_MAIN_CHECKOUT`（只用于测试）把各版本缓存指到一个按主检出布局的临时目录；不设置时所有路径与原来
相同。等价性检查就是靠它在临时副本里重建，真缓存一个字节都没写（重建前后对真缓存和真 inputs 做了 `find -newer`，
没有任何文件被改）。

### 10.3 运行器 `pipelines/reversal_data/build.py`

`python -m pipelines.reversal_data.build --version {v1,v2,v2.1} [--max-passes N] [--from STEP]`：每一轮依次运行
prefilter → yahoo → reconcile → terminal → earnings → universe → validate（v2 只在第一轮先跑 archive parse），每步是
`python -m pipelines.reversal_data.<step>` 子进程，直到监视的输出哈希与上一轮相同。v2 / v2.1 的轮数上限、日志目录
（`<cache>/v2_build`、`v2_1_build`）、`pass_N.json` 的键名都与原来两个脚本相同；v1 是冻结的，必须加 `--allow-v1`
（用于临时副本）。

### 10.4 依赖规则

第 2 阶段 9.4 的前三条例外已去掉：`quant.data.megacap_history` 和 megacap_oos2 / oos3 研究只导入
`quant.data.sources.megacap_oos2`（oos3 原来借用的 `reversal_data_common.cached_get` 换成同一请求台账的
`megacap_oos2.cached_get`）；spinoffs 只导入 `quant.data.sources.archive_org`。`tests/test_pipeline_layout.py` 检查
`quant/`、`studies/` 不导入 `pipelines` 和任何数据脚本，旧路径都转发到新模块，记录器保持自包含。

### 10.5 数据不变是怎么验证的

1. **改动前**：在当前代码（master + 只加了 `REVERSAL_DATA_MAIN_CHECKOUT` 的提交 `532ae8159`）的 `git archive` 上，
   建一个临时树：代码 + 入库的 inputs，`research_cache` 里五个会被写的目录（v1、v2、v2.1 缓存、v2 fill、v2.1
   Alpaca）用 APFS 克隆（保留修改时间），其余只读缓存和 `cleaned_stocks_data`、`stocks_list_dir` 用符号链接。
   网络用一个不可达的代理堵死。依次离线运行：v2 的 archive parse，然后 v1 / v2 / v2.1 并行各跑一轮
   form25（`--offline --out-dir`，写到临时目录）→ prefilter → yahoo → reconcile → terminal → earnings → universe →
   validate。security_master 没有包括：它在当前代码上离线运行就报错（`successor_security` 的 KeyError，改动前就
   如此，见 10.7），它的输出是从入库文件克隆的原样。
2. **改动后**：把临时树挪开，在**同一路径**重建一个新的（重构后代码 `836347e87`），用旧命令（经转发文件）把同样的
   步骤再跑一遍；之后又在修正（`b0acc83f4`）后的代码上用新运行器 `build --version v2 / v1 --allow-v1 / v2.1
   --max-passes 1` 再跑一遍。
3. **比较**：`inputs/`、`inputs_v2/`、`inputs_v2_1/` 的全部文件、form25 的输出，以及任一次运行写过的每个缓存文件
   （旧命令那一遍 28,942 个文件，新运行器那一遍 29,501 个），逐文件比 sha256。

结果：

- 三个版本 inputs 里的 61 个数据文件（csv / csv.gz）、form25 的 12 个输出、缓存里的每个 csv / csv.gz（价格文件、
  `prices/daily_panel.csv.gz`、prefilter、universe、earnings、terminal 的全部表）**逐字节相同**。
- 不同的只有 JSON 摘要、reconcile 的状态 pickle 和面板签名，逐键核对过，原因只有这些：
  - 时间戳和耗时：`generated_utc`、`updated_utc`、`built_utc`、`finished_utc`、`panel_built_utc`、`modified` /
    `modified_utc`、`runtime_s` / `runtime_seconds` / `seconds` / `timings_s`、`peak_rss_mb`、reconcile 日志文件名
    里的时间；
  - 代码哈希：manifest 的 `scripts`（各步骤源码的 sha256；改动后还多记录了 `pipelines/reversal_data/*.py`）；
    reconcile 每只证券状态的 `signature` 和 `daily_panel.signature` 里含 `reconcile.py` 自身字节的哈希
    （`CODE_HASH`，任何改动都会让它变），所以 v2.1 改动前的那次“面板未变、跳过”在改动后变成“重写面板”：重写出的
    `daily_panel.csv.gz` 与原来逐字节相同，只是 reconcile 摘要的 `panel` 一项记录了行数而不是 `unchanged`；
  - 上面这些 JSON 自己的 sha256 和字节数（出现在 manifest 和 validation_summary 的 `inputs_read` 里）；
  - 新运行器自己的记录（`v2_build/pass_1.json` 等），改动前那一遍没有用运行器，比较的对象是克隆来的旧记录。
- 第一遍比较还发现 prefilter 摘要里记录 RELIST_JUNCTIONS 来源的文件名从 `reversal_data_reconcile.py` 变成了
  `reconcile.py`：已改回（`b0acc83f4`），新运行器的第二遍确认这一项已经相同，其余结果与第一遍一样（数据文件
  0 个不同）。
- 顺带发现（与本次重构无关）：当前代码离线重建出的 v1 / v2 `terminal_returns_2012_2026.csv`、`reviewed_moves.csv`
  与入库版本不同（入库的是旧代码建的），三个版本还多写出一个未入库的 `special_distributions.csv`；v2.1 除时间戳外
  与入库版本一致。改动前后两次重建彼此一致，所以这不是重构造成的。

其它检查：`compileall`（src、scripts、tests、quant、studies、pipelines）；可移植 CI 测试集（372 个文件，3077 通过）、
`tests/quant`（含 `QUANT_GOLDEN_SLOW=1` 的第 2 阶段黄金和试点黄金）、数据依赖的 reversal 测试、前瞻观察黄金测试全部
通过；冻结根（v50 / v50r2 / v50r3 / sue_lt_v1 和工作流引用的脚本，31 个根、72 个文件）的导入闭包摘要改动前后完全
相同，四个 `status` 的输出与第 2 阶段记录的逐字节相同（v50r3 在 master 上一直是 `PROTOCOL_NOT_FROZEN`）。

### 10.6 行数

| | 改动前 | 改动后 |
|---|---|---|
| `scripts/` 里本阶段范围内的文件（26 个流水线脚本 + study_data_version + robustness 比较 + megacap_oos2_data + data_source_probe + record_borrow_fees） | 39,851 | 469（27 个转发文件，每个 10–15 行，共 396 行，加原样不动的记录器 73 行） |
| `pipelines/` | 0 | 38,682 |
| `quant/data/sources/` 新增模块（http、sec、tiingo、alpaca、archive_org、companiesmarketcap、quantquote、ibkr_borrow_fees、megacap_oos2） | 0 | 1,320 |
| `quant/data/version.py` + `quant/data/sources/yahoo.py` | 34 + 83 | 59 + 106 |
| `quant/observation/smisp.py` | 1,011 | 934 |

合计约 +700 行，几乎全是转发文件、模块文档字符串和新运行器的版本表；没有改任何算式。

### 10.7 留下的问题

- `security_master --offline` 在当前代码上运行失败（`successor_security` 里 `same["security_id"]` KeyError，与重构
  无关）；当前代码重建的 v1 / v2 有两张表与入库版本不同（10.5）。这两件事需要主人决定是修代码还是重新冻结。
- reconcile 的缓存签名含整个 `reconcile.py` 的哈希：只改注释或导入也会让下一次运行重算所有证券并重写面板（结果
  相同，只是慢）。
- 各步骤仍是几千行的大模块（reconcile 4,781 行、prefilter 4,057 行、universe 3,922 行），里面混着取数、解析和规则；
  本阶段只把与数据商相关、能原样搬出的部分搬进了 `quant/data/sources`，取数循环（配额、缓存布局、台账）仍留在步骤里。
- 三处仍有相近但不完全相同的副本，合并会改输出，所以保留：`megacap_oos2` 自己的 `_yahoo_csv_span`、`wayback_csv`
  （Wayback 地址不做 `&amp;` 替换）、CMC 页面的取数；`listings`、`factors` 各自的 Wayback 地址函数。
- 步骤之间仍有运行期互相改模块全局变量的写法（探针导入时改 `common.RAW_INDEX`，v2 抓取器改 `probe.RAW`），行为
  保持原样。
- `factors` 仍导入 `scripts.research_v5_trend_core_satellite`（第 4 阶段如果移动它，要留转发文件）。
- `.github/workflows/tests.yml` 的 `compileall` 只覆盖 `src scripts tests`，不含 `quant studies pipelines`（工作流
  文件属于冻结范围，本阶段没有改，建议主人加上）。
