# 2011–2019 样本外检验：数据报告

> 按 [检验规则](holdout_2011_2019_plan.md) 第 4 节编写。本报告和所有输入文件在运行之前
> 提交；编写过程中**没有计算或查看任何 2011–2019 的策略收益**，只看了数据覆盖、成交额、
> 财报可用性和价格口径。结论：**第 4.4 节的四项数据标准全部满足，可以运行。**

输入文件在 `output/research_only/holdout_2011_2019/inputs/`；来自 Tiingo 的价格按其
条款只存本地（`research_cache/holdout_2011_2019/`），由 `inputs/manifest.json` 里的
SHA-256 绑定。

## 1. 价格

### 1.1 来源

- 现有价格文件（`cleaned_stocks_data/price`，拆股与分红复权）；
- Tiingo 补充 28 家已退市或转板公司（`adjClose` 与现有口径一致；原始 `close` 另存为
  `raw_close`，供 10 美元最低价判断）。代码被其他证券重新使用的 `CA`（现为 ETF）、
  `SPLS`（现为 PIMCO 基金）已剔除；
- QQQ 总收益：Tiingo `adjClose`，2010–2020。与仓库已有的 QQQ 历史
  （`output/research_only/qqq_nasdaq_history.csv`，含分红）在 2018–2020 年重叠的
  755 个交易日逐日一致（差异中位数与 99 分位均为 0.000%）。

### 1.2 口径核对（规则 4.1）

用 AAPL、CSCO、AMGN 比较 2011–2019 的日收益：现有数据与 Tiingo `adjClose` 的差异
中位数约 0.006%，99 分位约 0.04–0.05%，满足“中位数 < 0.01%”。

### 1.3 退市后的补位行

Tiingo 在退市后会补上价格不变、零成交（或个位数成交）的行（如 Seagen、Splunk 补到
2026 年，Celgene 在 2019-11-20 后多出 2 天）。已截到最后一个真实交易日。

### 1.4 被收购公司的终值（`terminal_returns_supplement.csv`）

冻结回放在价格序列结束而没有终值记录时，按 **−100%** 计算
（`src/research/data_quality.py:stock_returns_with_delisting_penalty`）。窗口内结束的
11 个序列全部登记了终值，对价逐一取自 SEC 完成交割的 8-K：

| 代码 | 最后交易日 | 对价 | 终值收益 |
|---|---|---|---|
| CELG | 2019-11-20 | $50 + 1 股 BMY + 1 份 CVR（按 0 计，后来未兑付） | −1.69% |
| ESRX | 2018-12-20 | $48.75 + 0.2434 股 CI | +0.20% |
| WFM | 2017-08-28 | $42.00 现金 | +0.02% |
| YHOO | 2017-06-16 | 未套现，改名 Altaba（投资公司），按最后收盘价结束 | 0 |
| SIAL | 2015-11-18 | $140.00 现金 | +0.17% |
| SHPG | 2019-01-04 | 每 ADS $90.99 + 5.034 股 TAK ADS | +4.15% |
| GMCR | 2016-03-02 | $92.00 现金 | +0.36% |
| CTRX | 2015-07-23 | $61.50 现金 | +0.05% |
| VMED | 2013-06-07 | $17.50 + 0.2582 LBTYA + 0.1928 LBTYK | −0.05% |
| WCRX | 2013-09-30 | 0.160 股 Actavis；取不到 Actavis 价格，按最后收盘价结束 | 0 |
| KRFT | 2015-07-02 | 1 股 KHC + $16.50 特别股息 | +1.44% |

现有价格文件中没有在窗口内结束的序列。

### 1.5 覆盖率（规则 4.4 第 1 条，门槛 ≥ 80%）

纳指 100 年底成分股（维基百科历史版本）中有价格的比例：

| 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 | 2018 | 2019 |
|---|---|---|---|---|---|---|---|---|
| 82% | 85% | 88% | 86% | 89% | 90% | 91% | 97% | 99% |

主区间 2012–2019 全部达标。实际略高于表中数字：WLTW 以现代码 WTW 存在；CTRX、GMCR、
WCRX 在 2016–2017 属于维基名单的过期条目（已退市）。

**仍缺的公司**（没有可用的免费历史价格）：Broadcom（老 BRCM）、Linear Technology、
Altera、SanDisk（老 SNDK）、DirecTV、Dell（2013 年私有化前）、CA、21st Century Fox
（FOX/FOXA 2013–2019）、Viacom（VIAB）、BlackBerry（RIMM）、Sears、Bed Bath &
Beyond、Apollo Group、BMC、Endo、Life Technologies、Staples、News Corp（2013 年拆分前）、
VimpelCom、Teva/Infosys/Randgold（外国）。其中前几家（BRCM、LLTC、ALTR、SNDK、DTV、
FOXA、VIAB）是这套规则可能选中的类型，运行结果需带着这一局限解读。

## 2. 财报（规则 4.2）

- 现有 SEC 季度数据只含目前仍上市的公司。为 40 个 CIK（38 个股票代码）补下载了 SEC companyfacts
  （`sec_cik_map.csv`），用现有的 `parse_companyfacts_quarterly` 解析，得到
  `quarterly_supplement.csv`（与现有文件同格式）：
  - 退市公司 27 家（Express Scripts、Mylan、Perrigo 各含新旧两个主体）；
  - 仍上市但缺旧主体数据的：Avago/Broadcom Ltd、Google Inc、Marvell（2021 年前）、
    Walgreen Co、Liberty Global Inc；原先完全没有的 Garmin、Expeditors、Stericycle；
    数据不全的 Monster、Incyte。
- 每个 CIK 都在 SEC 公司记录里核对过名称。更正：`PARA` 在 2019-12 前的价格是 CBS，
  不是 Viacom，因此没有把 Viacom 的财报挂在 PARA 上。
- Virgin Media 以英镑报账，解析结果为 0 行（按规则视为不盈利）。
- 外国申报公司（20-F）不补，见规则 4.2。

**可用率**（有价格的成分股中，信号日能取得“4 个连续财季 + 550 天内披露”的比例；
规则 4.4 第 2 条按不含外国公司计算，门槛 ≥ 90%）：

| 日期 | 全部 | 不含外国公司 |
|---|---|---|
| 2011（不判定） | 62–77% | 66–81% |
| 2012 | 91% | 94% |
| 2013 | 92% | 96% |
| 2014 | 91% | 95% |
| 2015 | 90–91% | 95–96% |
| 2016 | 88% | 93% |
| 2017 | 90% | 96% |
| 2018 | 93% | 100% |
| 2019 | 91% | 98% |

剩余缺口：刚上市不足 4 个季度（Facebook 2012–13、PayPal、Kraft Heinz、新 Fox 2019，
按规则本应排除）、Liberty 系列追踪股、Google→Alphabet 过渡的 2016 年（GOOG/GOOGL）。

## 3. 股票池

### 3.1 没有市值门槛

冻结规则的股票池是信号日在 Nasdaq 上市的普通股，**没有**市值门槛
（`refresh_universe(min_market_cap=0)`）。README 和规则草稿曾误写为 3 亿美元，已更正。
证券类型按 `src/io/security_universe.py` 的名称规则剔除。

### 3.2 当时是否在 Nasdaq 上市

价格文件来自目前在 Nasdaq 上市的股票，其中一部分在 2011–2019 年还在 NYSE。判定方法：

1. **Nasdaq 官方上市名单快照**（仓库里 2015-01-10、2018 年 8 份、2019 年 4 份、
   2020-02-25）：当天在名单上即为在 Nasdaq 上市。
2. **改过代码的**：旧代码在 2015-01-10 名单上即算在 Nasdaq（`nasdaq_symbol_history.csv`，
   45 条，如 FB→META、PCLN→BKNG、SYMC→GEN、ISIS→IONS、ONNN→ON）。
3. **转板**：`nasdaq_listing_overrides.csv`（51 条）。有 SEC 8-K 的用公告的生效日
   （PepsiCo 2017-12-20、CSX 2015-12-22、T-Mobile 2015-10-27、Texas Instruments
   2012-01-03、Mondelez 2012-06-26、Western Digital 2012-06-01、Analog Devices
   2012-04-02、Marriott 2013-10-21）；没有的用快照前后区间中较晚的日期（从严）；
   2020-02-25 之前从未出现在任何快照中的（Honeywell、Apache、AEP、Host、Palo Alto、
   Xerox、AstraZeneca 等 22 家）整个窗口剔除。反方向转出 Nasdaq 的：Oracle
   （2013-07 转 NYSE）、Perrigo（2013-12 转 NYSE）。
4. **2015-01 之后上市的新股**：第一个交易日在上一份快照之后、并出现在下一份快照中，
   视为上市即在 Nasdaq。
5. 其余在快照中缺席、又不属于以上情况的：从第一次出现在快照的日期起计入（从严）。

**局限**：2015 年以前没有快照。2011–2014 年的转板靠 SEC 全文检索 2010-06 至 2015-03
的转板 8-K（约 200 家，大公司已逐一核对）；检索不到的漏网转板会使少数股票在转板前被
错误计入。能进入流动性池的都是大公司，这类情况预计很少。

## 4. 已知局限（运行结果须一并说明）

1. 缺少几家可能被选中的大公司的价格（第 1.5 节）；
2. 外国公司按不盈利处理，与 2020–2025 开发回放对少数外国公司补过季度数据的做法不同；
3. 2011–2014 年 Nasdaq 上市状态的判定精度低于 2015 年以后；
4. 纳指 100 历年成分股取自维基百科历史版本，个别年份含过期条目，只用于衡量覆盖率，
   不参与选股。

## 5. 结论

| 规则 4.4 | 结果 |
|---|---|
| 1. 价格覆盖率 ≥ 80%（2012–2019） | 85–99%，满足 |
| 2. 财报可用率 ≥ 90%（不含外国公司） | 93–100%，满足 |
| 3. 口径核对 | 满足（第 1.2 节） |
| 4. 缺失清单 | 已列出（第 1.5、2 节） |

下一步：编写 2011–2019 的数据加载器，把上述输入接到冻结的选股与回放代码上（冻结文件
逐字节不改，运行时校验 SHA-256），提交后运行一次。
