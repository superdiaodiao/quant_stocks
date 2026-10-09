# 策略研究汇总（2026-10-01 至 10-05）

目标：用约 1 万美元的 IBKR 账户，找到一个能稳定跑赢纳指的策略。比较对象是 QQQ，或纳斯达克综合指数（ONEQ），都算分红。

每项研究都按同一套做法：
- 规则和通过标准先写进台账，再计算结果。
- 用没碰过的数据检验，或者把历史分成两半，两半各检验一次。
- 按真实成本扣费。

所有台账都在 `docs/research_ledger_*.md`。

## 结论

没有找到能稳定跑赢纳指的规则。有两个候选进入观察，都不建议投真钱。

## 各方向结果

| 方向 | 台账 | 结果 |
|---|---|---|
| 周度短线反转 | research_ledger_reversal_2012_2026.md | 扣成本后不显著 |
| CAN SLIM | research_ledger_canslim.md | 232 种组合都没跑赢 QQQ |
| 欧奈尔完整方法（含带柄茶杯，两轮交叉检验） | research_ledger_oneil.md | 两轮都没通过 |
| 利弗莫尔 | research_ledger_livermore.md | 两段检验都没通过 |
| QQQ 均线择时 | research_ledger_qqq_timing.md | 一次性检验没通过 |
| 道氏理论 | research_ledger_dow_theory.md | 一次性检验没通过 |
| 技术指标和通道（含你原来的策略） | research_ledger_indicators.md | 没通过；QuantConnect 上扩大股票池后仍没通过 |
| 网格调参和滚动选参 | research_ledger_indicators.md 第 7–9 节 | 网格挑出的最优参数在新数据上不灵 |
| 止损调整 | research_ledger_stops.md | 没通过 |
| 按大盘状态切换持仓 | research_ledger_regime.md | 没通过 |
| 日历和隔夜效应 | research_ledger_calendar.md | 没通过 |
| 行业轮动 | research_ledger_sector_lev.md | 没通过 |
| 适度加杠杆 B3（均线上方 1.5 倍，下方 1 倍） | research_ledger_sector_lev.md、audit_leverage_b3.md | 形式上通过；审计发现多赚的部分主要来自杠杆，择时部分不显著；从泡沫顶部起算最惨跌掉 87% |
| 集中持有巨头 | research_ledger_megacap.md | 2014–2026 年通过；1999–2013 年用自建的时点市值排名重新检验，M1/M2/M6 都不通过（2000–2002 跌 73–77%），说明是那个年代特有的 |
| 内部人士买入、复制大师持仓 | research_ledger_sec_alt.md | 没通过（只覆盖纳斯达克，数据不全） |
| 212 个学术信号 | research_ledger_osap.md | 2015 年后，在大中盘里只做多，0 个通过 |
| 本地 40 个财务指标（含组合打分） | research_ledger_fundamentals.md | 96 次检验，0 个通过 |
| 全市场 20 个财务指标（QuantConnect） | research_ledger_qc_factors.md | 0 个通过；市销率最低的 10 只最接近 |
| 市销率最低的 10 只，真实交易复核 | research_ledger_qc_factors.md 第 3–4 节 | 统计显著性 3.09，刚过门槛；去掉错误数据后 2.84，不过线；最惨跌掉 70%；已列为未来观察对象 |
| 神奇公式、Piotroski、小盘股动量（QuantConnect） | research_ledger_qc_smallcap.md | 没通过 |
| 中小盘财报超预期、价值加动量（QuantConnect） | research_ledger_qc_round2.md | 没通过 |
| QQQ 波动率控仓（8 种设置） | research_ledger_voltarget.md | 1 种形式上通过，但其实是一直拿 1.37 倍杠杆；和同倍数不择时相比都没多赚，只是回撤浅 |
| 短线超跌反弹 RSI(2)（QQQ 和前 100 大股票） | research_ledger_mean_reversion.md | 没通过；QQQ 加杠杆版的"通过"来自 QQQ 本身强于 ONEQ |
| 大盘股财报事件（公告溢价、公告反应漂移） | research_ledger_earnings_events.md | 两折都没通过 |
| 每天机械做T、当天了结（QQQ、QLD，5 条规则） | research_ledger_intraday_t.md | 10 个配置全部不如"拿着不动"；每次来回毛利接近 0，成本 5–7 bp |
| 有条件 / 跨天做T（涨过头卖、高开卖、大跌加买；QQQ 和 18 只超大市值股） | research_ledger_selective_t.md | 18 个配置 17 个不通过；"涨过头先卖"最确定地亏（卖飞）；个股"低于均线 10% 加买"形式上通过但和同仓位持有几乎无差，可能是幸存者偏差，只列入观察 |
| 做T 参数全网格（184,320 个配置：方向、指标、门槛、了结、比例、备用金、趋势过滤 × QQQ/18 只大股/每月前 10） | research_ledger_t_grid.md | 两折 + 逐年滚动挑选后 6 个组合全部不通过；最稳定的一片（QQQ 上 RSI2 极低时加买）每年只多 0.3–0.6 点，t ≤ 1.6 |
| 卖期权（QQQ 卖看跌、备兑看涨、轮动；前 10 大股票卖看跌，QuantConnect） | research_ledger_qc_options.md | 测的是 30 天、0.25 delta、持有到期这一种设定，4 个都不通过；只在下跌年赢 |
| 卖期权全网格（QQQ 3,584 + SPY 参考：delta、天数、管理、VIX 过滤） | research_ledger_qc_options_grid.md | 不加杠杆的卖期权 0 个跑赢 ONEQ；只有"持有 ONEQ + 再卖看跌"样本外通过，但 β 1.4–1.56，扣杠杆后 α 为负；1 万美元做不了 |
| 加杠杆方式对比（融资、QQQ+QLD、TQQQ、期货；1.0–2.0 倍；1999–2026） | research_ledger_leverage_methods.md | 决策参考，不判通过：全段多赚约 1 点/年，1999–2009 越加越亏、2010–2026 多赚很多；定期调仓从未被强平，买了不管会被强平；1 万美元做不了期货 |
| 大市值个股卖看跌全网格（6,720 配置：前 5/10、全美/纳指、CSP/轮动/叠加） | research_ledger_qc_stock_puts.md | 不加杠杆 0 个两段都赢 ONEQ；叠加版样本外"通过"但 β 1.3–1.6、α 不显著、2022 年 −60%；极价外卖看跌有 2–3%/年小 α 但总收益低；1 万美元现在卖不了 |
| 买 QQQ 看涨期权加杠杆（384 配置，到期 3/6/12/24 个月） | research_ledger_qc_leaps.md | 期权杠杆比同敞口固定杠杆每年贵约 1.3 点；3 个月最贵，6–12 个月接近免费杠杆；1 万美元做不出 1.25–2 倍 |
| 机器学习选股（弹性网、提升树、神经网络；64 特征；逐年重训 2016–2026） | research_ledger_ml_cross_section.md | 8 个试验 0 个通过，没有一个 t ≥ 2；提升树有弱预测力（IC 0.032），但只能认出最差的股票，只做多用不上 |
| 分拆股（233 个事件，2012–2026；子公司/母公司，持有 12/24 个月） | research_ledger_spinoffs.md | 4 条规则两段都不通过；子公司相对小中盘基准有分拆效应（+18.6%，t 2.3），但对纳指不显著，中位数跑输；母公司明显跑输 |
| 因子基金、IBD 50（FFTY）、sell put 指数 | 只是对比，没有台账 | 这十几年都跑输 QQQ |

两轮独立审计没有发现会改变结论的方法错误：
- `audit_timing_studies.md`（择时类）；
- `audit_stock_studies.md`（选股类，含逐日按实盘方式模拟的交叉核对）。

去掉可疑财务数据后的复核（只作对照，原判定不改）：神奇公式、Piotroski、全市场 18 个财务指标重跑后，仍然 0 个通过。详见 research_ledger_qc_smallcap.md 第 2 节、research_ledger_qc_factors.md 第 5 节。

## 规律

- 择时类：能少亏，但总在急跌后的反弹里踏空。扣成本后，打不过一直持有。
- 选股类：多数持仓比纳指更分散、更不集中于巨头。2014–2026 年是巨头领涨的年代，这类策略结构上吃亏。
- 唯一在近十年收益明显超过 QQQ 的，是多承担风险的做法：加杠杆，或者更集中地持有巨头。它们是押注，不是被证实的规律。

## 观察中的候选（不投真钱）

1. 市销率最低的 10 只。观察规则见 research_ledger_qc_factors.md 第 4 节：
   - 从 2026-08 起每月记录选股和收益；
   - 至少观察 24 个月才下结论；
   - 第 36 个月判定是否有效；
   - 任何时候累计落后综合指数 25 个点就放弃。
2. 适度加杠杆 B3，只作风险偏好的参考。审计建议的验证问题是：均线择时，比一直持有 1.37 倍杠杆更好吗？

## 尝试次数

各台账合计超过 3,000 个配置。其中包括：
- 事后的网格对照 1,268 个；
- 212 个学术信号的筛选。

这么多尝试里，偶尔出现一个看起来显著的结果，本身就不奇怪。所以任何候选都必须用未来数据验证。
