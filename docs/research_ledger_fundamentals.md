# 财务报表指标"因子动物园"（fundamental factor zoo）：研究台账

> 代码 `scripts/research_fundamentals.py`，测试 `tests/test_research_fundamentals.py`，结果 `output/research_only/fundamentals/`。
> 价格、退市终值、QQQ 的加载规则同 `scripts/research_livermore.load_window`（但保留面板里全部证券，不只前 300 名）；
> 时点市值复用 `scripts/research_megacap.market_caps`；组合引擎、指标复用 `scripts/research_megacap`（`simulate`、`window_metrics`）；
> ONEQ 复用 `scripts/research_indicators.oneq_on_sessions`；成本复用 `scripts/research_reversal_dev.order_cost / half_spread`（IBKR Pro Tiered，1 万美元）。

## 0. 先说清楚

- **不是样本外。** 2014–2026 已被前面十几项研究反复看过；OSAP 筛查（`docs/research_ledger_osap.md`）已知"盈利能力"一族在 2015 年以后
  大中盘多头腿里最好但 t < 3，而且我们自己的 P1（毛利/总资产）、P2（经营利润+折旧+研发/总资产）在本面板上两段都不通过。
  `docs/research_ledger_megacap.md` 也已知 2014–2026 是"超大市值集中"的年代，等权 10 只的组合天然吃亏。
  所以本研究只是"教科书定义在已知年份上的一次性普查"：因子定义、方向、组合规则全部事先写死，不调参，每个配置只跑一次，结果不论好坏照记。
- **宽股票池有幸存者偏差**（`docs/audit_stock_studies.md` 第 7.2 节）：面板在前 300 名以外不完整，后来退市的中小盘股只有 35–45% 有价格。
  宽池的结果会偏好（退市的差公司缺得多），只报告；**判定只看前 300 名股票池**。

<!-- PREREG-BEGIN -->
## 1. 事先登记（2026-10-05，写代码、算任何收益之前写下）

### 1.1 数据

- 价格：冻结面板第 1 版 `research_cache/reversal_2012_2026/prices/daily_panel.csv.gz`（2011-06-01 至 2026-08-31，约 3,200 只纳斯达克证券，
  含已退市）。退市当天按 `terminal_returns_2012_2026.csv` 记终值，`awaiting_d5` 记 −55%，没有终值记录的记 0%（与之前各研究相同）。
  继承证券（`continued_as`）拼接同 `load_window`。
- 基本面：SEC XBRL companyfacts（本机缓存 `research_canslim_dev.CF_DIRS`；2026-10-05 按 ≤ 3 次/秒、`src/io/sec_contact.py` 的 User-Agent
  补抓面板里缺的非外国公司 CIK，抓取记录 `output/research_only/fundamentals/companyfacts_fetch_log.json`）。
- 季度 EPS：`research_canslim_dev.extract_eps_facts / eps_states`（与 CAN SLIM、止损研究相同的时点 EPS 状态，含 10-Q）。
- 市值：`research_megacap.market_caps`（SEC 股数 × 收盘价，股数申报日严格早于信号日、400 天内；否则公众流通市值；否则纳斯达克名单快照；
  并含 megacap 第 0.2a 节的单位错检查）。**"成交额折算"的市值不用**（记为没有市值）。股数缓存另存 `research_cache/fundamentals/`，不覆盖 megacap 的缓存。
- **1.1a 市值数据规则的补充（登记前的管道检查发现，只看了市值和因子排名，没有算任何收益）**：
  - 不少公司在某些年份把股数按"千股"申报（例如 MAT 2020、RGEN 2019 / 2022–2025、GRMN、BRKR、EA、FLIR 的个别年份），算出的市值小 1000 倍，
    会让价值类、规模、rd_mcap 因子的前 10 名被这些错误占满。规则：**市值 / 50 日成交额中位数 低于当月 U300 成交额前 50 名该比值中位数的 1/30**，视为单位错，市值记为空。
  - megacap 第 0.2a 节的"股数 × 价格超过公众流通市值 2.5 倍就改用流通市值"在这里**不用**：它是为外国 ADR 设的（本研究已剔除外国公司），
    而对刚上市、流通股少的公司（例如 ACIA 2017、ARWR 2014、FLGT 2020）会把市值低估 3–10 倍。高估方向的单位错仍由 megacap 的"市值 / 成交额超过前 50 名中位数 10 倍"规则剔除（剔除后记为空）。
  - 修正后，U300 的市值 97% 来自 SEC 股数，市值低于 5 亿美元的"名字-月"只剩 69 个（都是成交活跃的小盘股，如 TKMR、PEIX、CODX）。
  - 经营利润 > 收入（标签错，例如 MGPI 2019）→ op_margin 为空。
- 行业：前 300 名文件里的时点 SIC；没有的用 SEC submissions 文件里的当前 SIC（行业极少变，只用于剔除金融业）；都没有的不剔除。

### 1.2 时点规则（point in time）

- 只用 10-K / 10-K/A / 10-KT 里的**年度**数字（期长 340–390 天的流量；期末时点的存量）。同一科目同一期取**信号日之前（严格早于）**最后一次申报的值
  （以后的重述从它自己的申报日起才生效；后一年 10-K 里追溯调整过拆股的上年每股数据因此自动一致）。
- 每个公司在每个申报日之后有一个"状态"：FY0 = 已申报的最近一个财年（以净利润、收入、经营利润、经营现金流任一年度事实的期末日为准），
  FY−1 … FY−4 = 期末日在 FY0 期末前 k 年 ±20 天的财年。存量（资产负债表）取期末日与该财年期末相差 ≤ 7 天的时点值。
- 状态只在申报日**之后**的信号日使用（`filed < s`）；FY0 期末距信号日 > 550 天的整个状态不用（太旧）。
- 季度 EPS 增长用 `eps_states` 的 `c_growth`（最近一季 EPS 对去年同季），只在其 `avail` 日之后使用，季末距信号日 > 200 天不用。
- 某个科目在 FY0 缺失 → 用到它的因子在该公司该月为空（不回退到更早的财年），不填 0（除非下表写明"缺记 0"）。

### 1.3 科目（us-gaap，USD；每组按顺序取第一个有值的）

| 记号 | 科目 |
|---|---|
| REV 收入 | Revenues, RevenueFromContractWithCustomerExcludingAssessedTax, RevenueFromContractWithCustomerIncludingAssessedTax, SalesRevenueNet, SalesRevenueGoodsNet, SalesRevenueServicesNet |
| COGS 营业成本 | CostOfRevenue, CostOfGoodsAndServicesSold, CostOfGoodsSold, CostOfServices, CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization |
| GP 毛利 | GrossProfit；没有就 REV − COGS；毛利 > 收入视为标签错（空） |
| OI 经营利润 | OperatingIncomeLoss |
| NI 净利润 | NetIncomeLoss, NetIncomeLossAvailableToCommonStockholdersBasic, ProfitLoss |
| EPS | EarningsPerShareDiluted, EarningsPerShareBasicAndDiluted, EarningsPerShareBasic（USD/shares） |
| DA 折旧摊销 | DepreciationDepletionAndAmortization, DepreciationAndAmortization, DepreciationAmortizationAndAccretionNet, Depreciation（缺记 0） |
| RD 研发 | ResearchAndDevelopmentExpense, ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost |
| INT 利息费用 | InterestExpense, InterestExpenseDebt, InterestExpenseNonoperating |
| PTI 税前利润 | IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest, IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments |
| CFO 经营现金流 | NetCashProvidedByUsedInOperatingActivities, NetCashProvidedByUsedInOperatingActivitiesContinuingOperations |
| CAPEX 资本开支 | PaymentsToAcquirePropertyPlantAndEquipment, PaymentsToAcquireProductiveAssets（缺记 0） |
| DIV 现金分红 | PaymentsOfDividends, PaymentsOfDividendsCommonStock（缺记 0） |
| BUY 回购 | PaymentsForRepurchaseOfCommonStock（缺记 0） |
| TA 总资产 | Assets |
| CA / CL 流动资产 / 负债 | AssetsCurrent / LiabilitiesCurrent |
| TL 总负债 | Liabilities；没有就 TA − 股东权益（含少数股东） |
| BE 股东权益 | StockholdersEquity, StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest |
| CASH 现金 | CashAndCashEquivalentsAtCarryingValue, CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents, Cash（缺记 0） |
| STD 短期债务 | DebtCurrent；没有就 LongTermDebtCurrent + ShortTermBorrowings（各自缺记 0） |
| LTD 长期债务 | LongTermDebtNoncurrent；没有就 LongTermDebt（缺记 0） |
| RE 留存收益 | RetainedEarningsAccumulatedDeficit |
| AR / INV / AP | AccountsReceivableNetCurrent / InventoryNet / AccountsPayableCurrent（变化量缺记 0） |
| TP 应交税费 | TaxesPayableCurrent, AccruedIncomeTaxesCurrent（缺记 0） |
| SH 加权稀释股数 | WeightedAverageNumberOfDilutedSharesOutstanding, WeightedAverageNumberOfSharesOutstandingBasic（shares） |

DEBT = LongTermDebtNoncurrent + STD；没有 LongTermDebtNoncurrent 时 DEBT = LongTermDebt + ShortTermBorrowings（LongTermDebt 已含一年内到期部分；缺记 0）；EBIT = OI，没有 OI 时 PTI + INT；EBITDA = EBIT + DA；
MC = 信号日时点市值；EV = MC + DEBT − CASH。下标 0 / −1 / −k 表示 FY0 / FY−1 / FY−k；"均值资产" = (TA₀ + TA₋₁)/2（缺 TA₋₁ 时用 TA₀）。

### 1.4 因子（40 个，方向事先按文献定："+" = 数值越大越好，"−" = 越小越好）

| 族 | 因子 | 定义 | 方向 | 文献 |
|---|---|---|---|---|
| 价值 value | ep | NI₀ / MC | + | Basu 1977 |
| | bm | BE₀ / MC（BE ≤ 0 为空） | + | Fama–French 1992 |
| | sp | REV₀ / MC | + | Barbee–Mukherji–Raines 1996 |
| | cfp | CFO₀ / MC | + | Lakonishok–Shleifer–Vishny 1994 |
| | ebit_ev | EBIT₀ / EV（EV ≤ 0 为空） | + | Loughran–Wellman 2011 |
| | fcf_yield | (CFO₀ − CAPEX₀) / MC | + | LSV 1994 / Hou 等 |
| | payout_yield | (DIV₀ + BUY₀) / MC | + | Boudoukh 等 2007 |
| 盈利 profitability | roe | NI₀ / BE₀（BE ≤ 0 为空） | + | Haugen–Baker 1996 |
| | roa | NI₀ / TA₀ | + | Balakrishnan 等 2010 |
| | roic | EBIT₀ / (DEBT₀ + BE₀ − CASH₀)（分母 ≤ 0 为空） | + | Greenblatt 2006 |
| | gpa | GP₀ / TA₀ | + | Novy-Marx 2013 |
| | opa | OI₀ / TA₀ | + | Fama–French 2015 (RMW 的资产版) |
| | gross_margin | GP₀ / REV₀ | + | Novy-Marx 2013 |
| | op_margin | OI₀ / REV₀ | + | Soliman 2008 |
| | cbop | (OI₀ + DA₀ + RD₀ − ΔAR − ΔINV + ΔAP) / TA₀（DA、RD、Δ 缺记 0） | + | Ball 等 2016 |
| 成长 growth | sales_g1 | REV₀ / REV₋₁ − 1（REV₋₁ ≤ 0 为空） | + | 成长的教科书方向（Lakonishok 等认为相反，这里按"成长"族的字面方向） |
| | sales_g3 | (REV₀ / REV₋₃)^(1/3) − 1 | + | 同上 |
| | eps_g1 | (EPS₀ − EPS₋₁) / \|EPS₋₁\|（EPS₋₁ = 0 为空） | + | 盈利动量 |
| | eps_g3 | (EPS₀ − EPS₋₃) / \|EPS₋₃\| | + | 同上 |
| | q_eps_g | 最近一季 EPS 对去年同季的增长（`c_growth`） | + | O'Neil / Foster 等 1984 |
| | opinc_g1 | (OI₀ − OI₋₁) / \|OI₋₁\| | + | 同上 |
| 质量/应计 quality | accruals | (NI₀ − CFO₀) / 均值资产 | − | Sloan 1996（现金流量表口径，Hribar–Collins 2002） |
| | sloan | [(ΔCA − ΔCASH) − (ΔCL − ΔSTD − ΔTP) − DA₀] / 均值资产 | − | Sloan 1996 |
| | cash_conv | CFO₀ / NI₀（NI₀ ≤ 0 为空） | + | 盈余质量 |
| | piotroski | F 分数 9 项：ROA>0、CFO>0、ΔROA>0、CFO>NI、Δ(LTD/TA)<0（两年 LTD 都是 0 也算 1 分）、Δ流动比率>0、股数没增加（SH₀ ≤ SH₋₁）、Δ毛利率>0、Δ资产周转>0；至少 7 项算得出，得分 × 9 / 项数 | + | Piotroski 2000 |
| | altman_z | 1.2·(CA−CL)/TA + 1.4·RE/TA + 3.3·EBIT/TA + 0.6·MC/TL + 1.0·REV/TA（全部要有） | + | Altman 1968 |
| | earn_var | 最近 5 个财年 ROA（NI/TA）的标准差，至少 3 年 | − | Dichev 1998 / QMJ |
| | noa | [(TA₀ − CASH₀) − (TA₀ − DEBT₀ − BE₀)] / TA₋₁ | − | Hirshleifer 等 2004 |
| 投资 investment | asset_g | TA₀ / TA₋₁ − 1 | − | Cooper–Gulen–Schill 2008 |
| | capex_assets | CAPEX₀ / TA₀ | − | Titman–Wei–Xie 2004 |
| | share_iss | ln(SH₀ / SH₋₁)（净发行；越小 = 回购越多） | − | Pontiff–Woodgate 2008 |
| 杠杆/流动性 leverage | de | DEBT₀ / BE₀（BE ≤ 0 为空） | − | QMJ 的"安全"（低杠杆好） |
| | nd_ebitda | (DEBT₀ − CASH₀) / EBITDA₀（EBITDA ≤ 0 为空） | − | 信用分析惯例 |
| | current_ratio | CA₀ / CL₀ | + | Piotroski 2000 |
| | int_cov | EBIT₀ / INT₀（INT 科目存在且为 0 而 EBIT > 0 → 记 1000；INT 科目不存在 → 空） | + | 信用分析惯例 |
| 效率 efficiency | asset_turn | REV₀ / TA₀ | + | Soliman 2008 |
| | d_asset_turn | REV₀/TA₀ − REV₋₁/TA₋₁ | + | Soliman 2008 |
| | rd_sales | RD₀ / REV₀（没有研发科目为空） | + | Chan–Lakonishok–Sougiannis 2001 |
| | rd_mcap | RD₀ / MC | + | 同上 |
| 规模 size | size | ln(MC) | − | Banz 1981 |

Beneish 操纵指数（可选项）不做：要的科目（销售费用、坏账准备等）XBRL 口径太乱。

**综合分（事先定）**：每个信号日在股票池内，把每个因子乘上方向后换成横截面百分位排名，再标准化成 z 分（均值 0、标准差 1）。
- 族综合分 = 该族可用因子 z 分的等权平均，至少要有该族一半（向上取整）的因子可用。7 个族综合分：value、profitability、growth、quality、investment、leverage、efficiency
  （size 只有一个因子，不另做综合分）。
- 全族综合分 ALL = 7 个族综合分的等权平均（至少 4 个族可用）；size 不进 ALL（它是市场指标，不是报表指标）。

### 1.5 股票池（每个信号日 s = 每月最后一个交易日；s 收盘及以前的数据）

- **U300（判定用）**：s 当天或之前最近一周的每周名单（`weekly_universe_top300.csv.gz`，2026-07-17 之后沿用该周），有 `dv50_rank`（成交额前约 300、原始收盘 ≥ 10 美元），
  s 有收盘价、价格序列没结束；剔除金融业（SIC 6000–6999，同 Fama–French / OSAP）；同一公司多类股只留成交额排名最前的一类。
- **UW（宽池，只报告，有幸存者偏差）**：s 当天或之前最近一周 `weekly_listed.csv.gz` 里 `eligible`（纳斯达克普通股、非外国、非 SPAC、非投资公司）的面板证券，
  s 有收盘价、价格序列没结束、原始收盘 ≥ 5 美元、**时点市值 ≥ 3 亿美元**（1.1 节的市值，没有就不进）；剔除金融业；同一公司（CIK）只留当周成交额最大的一类。
- 每个因子只在算得出该因子的股票里排名。

### 1.6 组合与成本

- 每个因子 / 综合分、每个股票池：按（乘了方向的）值从高到低取前 **10** 只，**等权**，每月调仓；同值按证券编号。可排名的股票不足 10 只的月份不调仓（持仓照旧）。
- s 收盘决策，**下一个交易日收盘**成交（`research_megacap.simulate`）：卖出不再入选的，买入新入选的，继续持有的只有偏离目标 25% 以上才调回（仓库统一 `REBALANCE_BAND = 0.25`）；
  先卖后买，现金不足按比例缩小；退市转现金；现金无利息。
- 成本：IBKR Tiered 每单佣金（每股 0.0035、最低 0.35、最高 1%）+ 交易所 / 清算费 + 卖出监管费 + 半价差（按当周成交额排名 2–8 bp，排名不在前 300 的 10 bp，且不少于半个最小报价单位）。允许碎股。
- 第一笔信号 2013-12-31，2014-01-02 收盘建仓；一次连续模拟到 2026-08-31。

### 1.7 区间、指标、通过标准

- 区间：**前段 2014-01-01 至 2019-12-31**、**后段 2020-01-01 至 2026-08-31**、全段 2014-01 至 2026-08（月超额 t 值用全段）。
- 每个配置每个区间报告：CAGR、对 ONEQ / QQQ 的超额、波动、最大回撤、最长回撤（天）、夏普（无风险利率 0）、索提诺、卡玛、信息比率、对 ONEQ 的 beta / 年化 alpha、
  跑赢 ONEQ 的年份占比、最差年份 / 月份、换手率、成交笔数、成本拖累。
- **单因子 / 综合分的通过标准（同 megacap）**：A = 前后两段 CAGR 都高于 ONEQ，且全段月超额 t ≥ 2；或 B = 两段最大回撤都比 ONEQ 浅 ≥ 10 个百分点且 CAGR 落后 ≤ 3 个百分点。
- **多重检验**：48 个规则（40 因子 + 8 综合分）× 2 个股票池 = **96 个检验**全部计数。每个检验的 p 值 = 全段月超额（策略 − ONEQ）t 值的单侧 p（t 分布，自由度 = 月数 − 1）。
  报告：名义 t ≥ 2 的个数、"纯运气预期个数"、Bonferroni（96 个，单侧 5%）通过数、Benjamini–Hochberg 错误发现率 5% 通过数；也单独对 U300 的 48 个报告。
- **信息系数（IC，只作诊断）**：每个信号日，在股票池内算（乘了方向的）因子值与"s 收盘到下一个信号日收盘"总收益（含退市终值）的 Spearman 秩相关；
  报告各区间月均 IC、t 值、IC > 0 的月份占比。

### 1.8 判定用检验：滚动选因子（walk-forward）

- 每年 1 月（信号日 = 上一年 12 月最后一个交易日 s），对每个候选算**过去 36 个完整月**（Y−3 年 1 月至 Y−1 年 12 月）的月收益夏普（该候选单独运行、扣成本后的净值的月末收益，
  均值 / 标准差 × √12），选最高的一个（同值按名字），**之后 12 个信号日**（Y−1 年 12 月至 Y 年 11 月的月末）都用它的前 10 名调仓。只用到 s 收盘及以前的净值。
- 两个选择器：**WF-F** 在 40 个单因子里选；**WF-C** 在 8 个综合分（7 族 + ALL）里选。各在 U300、UW 跑一次 → 4 个配置。第一年 2017（用 2014–2016），到 2026-08-31。
  换选的因子时按引擎正常卖出 / 买入并收成本。
- **通过标准（2017-01-01 至 2026-08-31，对 ONEQ）**：CAGR 高于 ONEQ 且月超额 t ≥ 2；**或** 最大回撤比 ONEQ 浅 ≥ 10 个百分点且 CAGR 落后 ≤ 3 个百分点。
  **判定以 U300 的 WF-F、WF-C 为准**（任一通过即记"通过"，同时报告 2 次试验的 Bonferroni 门槛 t = 1.96）；UW 的两个有幸存者偏差，只报告。
  另报 2017–2019、2020–2026-08 两段和每年选中的因子。
- 参照（不算试验）：QQQ 买入持有、ONEQ 买入持有。

### 1.9 运行规则

- 先只做数据管道检查（覆盖率、公式抽查），不算任何收益；然后 `--register` 把本节（PREREG-BEGIN 到 PREREG-END）的 SHA-256 写进
  `output/research_only/fundamentals/frozen_prereg.json`；`--run` 校验哈希一致才运行。全部配置只跑一次，看结果后不改任何规则；只允许修代码错误（不改规则），每次都记在下面。
- **试验次数：单因子/综合分 96 个 + 滚动选因子 4 个 = 100。**
<!-- PREREG-END -->

## 试过的次数（累计）

| 日期 | 批次 | 配置数 | 累计 |
|---|---|---|---|
| 2026-10-05 | 第 1 节登记的 96 个单因子/综合分配置 + 4 个滚动选因子配置，各跑一次 | 100 | 100 |

## 2. 结果（2026-10-05，`--register` 后 `--run` 跑一次；登记哈希 4b3cad19…）

**运行记录**：登记前做了管道检查（覆盖率、AAPL / MSFT / INTC / COST / AMGN / CSCO 的 ROE、毛利/资产、EPS 增长等与公开财报对照一致；
发现股数单位错后补写 1.1a），并用"每月把因子值随机打乱"的数据跑通代码路径（不看真实因子结果）。之后登记、跑一次，没有改过规则或代码。
日期保护：价格 / 收益都在 [2011-06-01, 2026-08-31]；每行断言"申报日 < 信号日"、"季度 EPS 可得日 < 信号日"全部通过。
信号 152 个（2013-12-31 至 2026-07-31），退市事件 810 个按终值入账。SEC companyfacts 补抓 1,135 个 CIK，3 个 SEC 没有。
U300 每月约 270 只可排名，UW 约 730–1,030 只；大多数因子覆盖 90% 以上（毛利类约 75%，研发类约 55–65%，季度 EPS 增长约 55–74%）。

**判定：不通过。** U300 的两个滚动选因子配置都跑输 ONEQ；96 个单因子 / 综合分配置没有一个通过事先标准，也没有一个通过多重检验。

### 2.1 多重检验（全段 2014-01 至 2026-08 月超额 vs ONEQ）

| | 全部 96 个 | 只看 U300 的 48 个 |
|---|---|---|
| 名义 t ≥ 2（纯运气预期约 2.2 个 / 1.1 个） | **0** | **0** |
| Bonferroni（单侧 5%，t ≈ 3.28 / 3.08） | 0 | 0 |
| Benjamini–Hochberg 错误发现率 5% | 0 | 0 |
| 按事先标准（A 或 B）通过 | 0 | 0 |
| t ≤ −2（显著**跑输** ONEQ） | 18 | 14 |

- U300 里全段 t 最高的：rd_mcap（研发/市值）0.63、q_eps_g（季度 EPS 增长）0.46、cash_conv 0.34、roe 0.22，都远不到 2。
  前后两段都跑赢 ONEQ 的只有 U300 的 roe（+15.0% / +19.4% 对 +14.9% / +18.6%，t 0.22）和 UW 的 asset_g（宽池，偏好）。
- U300 48 个配置全段 CAGR 中位数 +8.3%（ONEQ +16.8%、QQQ +19.0%），最大回撤中位数 −51%（ONEQ −35%）；只有 4 个全段 CAGR 高于 ONEQ。
  "盈利能力"一族（roa、opa、gpa、roic、op_margin、C_profitability）t 在 −1.9 到 −2.7 之间，是跑输最显著的一批之一。
- 成本不是问题：换手每年单边 1–6 倍，成本拖累 0.2–1.2%/年（size 最高）。完整指标见 `summary.csv`（每个配置、每个区间的 CAGR、超额、波动、最大 / 最长回撤、夏普、索提诺、卡玛、IR、beta / alpha、跑赢年份占比、最差年 / 月、换手、成本）。

### 2.2 信息系数（诊断，不判定）

- 横截面上，盈利能力因子**确实有预测力**：U300 全段月均 IC 最高的是 cbop 0.043（t 4.1）、int_cov 0.041（3.7）、C_profitability 0.041（3.6）、op_margin 0.038（3.6）、
  opinc_g1、roa、roic、roe（0.026–0.039，t 3.2–3.5）；UW 结果几乎一样。U300 有 14 个 IC t ≥ 2，UW 21 个。
- size 的 IC 是 −0.049（t −4.6）：按"小的好"方向是反的，即这段时间里**越大的公司越涨**。价值类（bm、ep、sp）IC 接近 0 或为负。
- 为什么 IC 显著而组合跑输：IC 是"在股票池内部谁比谁好"，而组合要和按市值加权、被少数巨头带动的 ONEQ 比。等权 10 只好公司能跑赢池内平均，
  却补不上"等权池子本身每年落后市值加权指数约 3 个点"（OSAP 和 megacap 研究里看到的同一现象），再加上只持 10 只的个股风险（回撤普遍 −40% 到 −60%）。

### 2.3 判定用：滚动选因子（2017-01 至 2026-08，对 ONEQ）

| 配置 | CAGR | ONEQ | QQQ | 月超额 t | 最大回撤（ONEQ −35%） | 最长回撤（天） | 夏普 | 跑赢 ONEQ 年份 | 换手/年 | 成本/年 | 判定 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **U300 WF-F（40 个单因子里选）** | +13.7% | +18.9% | +21.2% | −0.55 | −42.0% | 1,419 | 0.64 | 4/10 | 2.3 倍 | 0.3% | **不通过** |
| **U300 WF-C（8 个综合分里选）** | +13.3% | +18.9% | +21.2% | −0.69 | −42.6% | 1,762 | 0.63 | 4/10 | 2.5 倍 | 0.3% | **不通过** |
| UW WF-F（宽池，只报告） | +17.6% | +18.9% | +21.2% | +0.05 | −54.1% | 1,489 | 0.71 | 5/10 | 2.1 倍 | 0.4% | 不通过 |
| UW WF-C（宽池，只报告） | +7.9% | +18.9% | +21.2% | −1.09 | −55.2% | 2,029 | 0.41 | 3/10 | 2.6 倍 | 0.5% | 不通过 |

- 分段：U300 WF-C 在 2017–2019 年化 +20.9%（ONEQ +19.5%），2020–2026-08 只有 +10.0%（ONEQ +18.6%）；WF-F 两段都落后。
- 每年选中的（U300）：WF-F 2017–18 int_cov、2019 rd_mcap、2020–22 roe、2023 share_iss、2024 cfp、2025 q_eps_g、2026 op_margin；
  WF-C 2017–21 质量族、2022–23 投资族、2024 价值族、2025–26 全族。**选中的因子每隔一两年就换，说明过去 3 年最好的因子在下一年没有持续性。**
- 逐年（%）：

| 年 | U300 WF-F | U300 WF-C | UW WF-F | UW WF-C | ONEQ | QQQ |
|---|---|---|---|---|---|---|
| 2017 | 40.2 | 28.6 | 50.2 | 37.8 | 28.4 | 31.5 |
| 2018 | −18.0 | 13.9 | −10.2 | −7.1 | −3.2 | −0.1 |
| 2019 | 25.5 | 20.3 | 18.0 | 54.0 | 37.2 | 39.0 |
| 2020 | 42.6 | 49.9 | 101.3 | 18.6 | 44.9 | 48.6 |
| 2021 | 35.4 | 12.1 | 36.6 | −19.1 | 22.1 | 27.4 |
| 2022 | −22.7 | 10.3 | −29.2 | −15.0 | −32.1 | −32.6 |
| 2023 | 2.8 | −4.4 | 21.0 | 32.4 | 45.7 | 54.9 |
| 2024 | −5.2 | −11.1 | −28.8 | −21.6 | 29.3 | 25.6 |
| 2025 | 51.6 | 10.7 | 71.3 | 14.1 | 20.9 | 20.8 |
| 2026（到 8 月） | 8.2 | 8.6 | 4.7 | 9.3 | 14.0 | 17.0 |

### 2.4 解读与局限

- 结论：在 2014–2026 的纳斯达克大中盘里，**40 个教科书财务指标、7 个族综合分、全族综合分，以及"每年选过去最好的因子"都没能按事先标准跑赢 ONEQ**；
  0 个过多重检验，反而有 14 个（U300）显著跑输。与 OSAP 筛查、P1 / P2 检验、megacap 研究的结论一致：这段时间的超额收益来自"集中持有最大的几家公司"，
  不是来自财务报表上的"好公司"特征。
- 财报指标并非没信息（盈利能力的 IC t 3–4），但这点信息在"只做多、10 只、等权、和市值加权指数比"的条件下变不成超额。
- 局限：只用年报（最多滞后约 15 个月），季度数据只用于季度 EPS 增长；XBRL 科目口径不一（例如餐饮零售的营业成本口径让 gpa 偏向餐饮股，见 OSAP 台账 2.1 节）；
  宽池有幸存者偏差（结果偏好，仍然不通过）；名单不含外国 ADR；年份已被反复看过，不是样本外。
- **不建议用于实盘。** 不再调参数重跑。
