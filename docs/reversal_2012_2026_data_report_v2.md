# 周度反转检验（2012–2026）数据报告：数据版本 2

> 2026-10-05/06 构建。按 owner 2026-10-05 的决定（[数据计划](reversal_2012_2026_data_plan.md)第 0 节）用新来源补缺口、改错。本报告只描述数据，**没有计算任何信号、策略收益、组合收益或跨股票收益汇总**。冻结的数据版本 1 一个文件也没有改。下面先给 owner 一段中文摘要，后面是英文工程正文。

## 给 owner 的摘要

**最终版（2026-10-06，档案馆抓取完成后重建）。** 下面"中间版"的数字已被这一版取代；全部表格以 `output/research_only/reversal_2012_2026/v2_report/`（`tables.md`、`summary.json`）为准。
- 档案馆 1,186/1,186 只全部抓完，0 失败；新增 150,498 行价格，涉及 474 只股票。
- 离线重建在第 2 轮到达不动点；补下载新公司的 SEC 头文件后，validate 与 v1 状态完全相同（17 通过 / 17 未通过 / 1 无输入，没有一项改变）。
- v1 缺失、估计占前 250 名的 3,353 个名额里，补上约 1,318 个（39%）。
- 终值：已算出的从 582 增加到 608；D5 50 只中 45 只仍按 −55%（3 只待复核、1 只已算出、1 只缺价格）。
- 2% 规则（判定用的模型读数，validate 检查 6）：只剩 2012 年超标。

   | 年份 | 版本 1 | 版本 2 |
   |---|---|---|
   | 2012 | 5.03% | 2.63%（仍超 2%） |
   | 2013 | 3.19% | 1.91% |
   | 2014 | 1.02% | 0.31% |
   | 2015 | 0.67% | 0.08% |
   | 2016 | 0.78% | 0.67% |
   | 2017 | 0.92% | 0.91% |
   | 2018 | 2.67% | 1.65% |
   | 2019 | 1.88% | 0.72% |
   | 2020 | 0.97% | 0.31% |
   | 2021 | 0.70% | 0.02% |
   | 2022 | 0.58% | 0.22% |
   | 2023 | 0.49% | 0.08% |
   | 2024 | 0.38% | 0.03% |
   | 2025 | 0.23% | 0.23% |
   | 2026 | 0.00% | 0.00% |


**这是什么。** 数据版本 2 是版本 1 的"稳健性对照版"：规则一条不改，只是(1)用你 10 月 5 日批准的三个新来源补缺口，(2)改掉 QuantConnect 审计找到的 5 个错误和 8 个漏记的公司行为，(3)用美国证监会（SEC）文件确认 50 只"D5"退市股的终值。版本 1 的文件一个字节都没动；版本 2 放在单独的目录里（见正文第 0 节）。

**这是一份中间版本。** 档案馆（archive.org）的抓取在后台按"价值从大到小"的顺序进行。本版用的是 10 月 5 日 17:19（UTC）的快照：1,186 只目标股票里完成了 232 只，包括全部 50 只 D5 股票和缺口最大的那批股票。按实测速度，剩下的大约还要 6 小时（比调查估计的 30 小时快，因为改成了按整个网址前缀批量查目录）。抓完以后按正文第 9 节重跑一条命令，就能得到最终的版本 2。

**结果（中间版）：**

1. **2% 规则（按判定用的"模型读数"，validate 检查 6）：**

   | 年份 | 版本 1 | 版本 2 |
   |---|---|---|
   | 2012 | 5.03% | **2.91%**（仍超过 2%） |
   | 2013 | 3.19% | **1.90%**（降到 2% 以下） |
   | 2018 | 2.67% | **1.95%**（降到 2% 以下） |

   其余年份两个版本都低于 2%，版本 2 更低，例如 2019 年从 1.89% 降到 0.72%。所以超过 2% 的年份从 3 个（2012、2013、2018）减少到 1 个（2012）。

   按另一种口径（第 12 步，所有非待补原因），2012 年从 4.88% 降到 3.34%，2013 年从 2.70% 降到 1.55%，2018 年从 2.47% 降到 1.78%。"上限读数"在 2018–2024 年仍高，原因是规模不明的周还很多，它们要等 11 月的 Tiingo。

2. **补上的名额：** 版本 1 缺失、估计占前 250 名的约 3,353 个名额里，版本 2 补上了约 1,098 个（33%）。补上的名字-周共 8,823 个，其中 1,078 个在版本 2 里确实排进了前 250 名。新增的价格行有 44,220 行，来自 119 只股票，其中 74 只是版本 1 完全没有价格的股票。所有新增的行都来自档案馆存的雅虎页面，而且只按整周补。
3. **两家来源打架的日子：** 用独立的第三票裁决了 7 天。另有 21 个新补的日子与 QuantQuote 或 companiesmarketcap 不一致，标成"未解决"。所以"未解决"的总数从 1,890 变成 1,904。
4. **改错（12 项，5 个自己的错 + 8 个漏记的公司行为，LMCA 2014 年 7 月那一项两边都算）：**
   - **已改 8 项：** LMCA 2014 年两次、FLEX、UNTD、LMCA 和 LMCK 2016 年、SPWR、LSXMK。做法与版本 1 处理 ZG 和 LBTYK 时一样，按分出去的证券自己的收盘价估值。例如 LMCA 2014-07-24 的单日收益，从错误的 −65.4% 变成 +0.4%。
   - **没改成 4 项：** ENVX 的认股权证、GLIBA 的优先股、QRTEA 的优先股，以及 CALD 最后 5 个交易日。原因是找不到这些价格：雅虎已经没有这些代码，档案馆也没有存。
5. **D5 的 50 只股票（逐份读了 SEC 文件）：**
   - 17 只是破产，计划写明老股东什么也拿不到：仍按 −55% 记，−100% 压力测试有了依据；
   - 19 只其实不是破产：因为不达标、主动退市、SPAC 到期或现金并购而离开纳斯达克；
   - 7 只老股东拿到一些东西；
   - 7 只破产了，但还没有计划。

   版本 2 里有 5 只不再按 D5 记：
   - **CTCM**：是每股 2.0503 美元的现金并购，现在按并购算出了终值；
   - **QTNT**：每股 0.01 美元，但缺最后一天的价格，标成"缺价格"；
   - **SIVB、TPIC、LGCY**：找到了退市后的场外价格，但只有一家来源，按规则等你复核。

   其余 45 只仍按 D5 记。
6. **检查结果：** 35 项检查中，版本 1 和版本 2 都是 17 项通过、17 项未通过、1 项缺输入，没有哪一项改变通过/未通过的状态，但数字有进有退：
   - **变好：** 未结的人工复核从 382 条降到 376 条；未结的终值从 187 条降到 181 条；纳斯达克 100 成分股的完整年份从 936 增加到 938；"每周缺失的大公司"最多从 18 只降到 13 只。
   - **变差：** 有 3 项因为新增股票而变差。一个原因是标签：给原来完全没有价格的股票补了部分周以后，它其余的缺失周按版本 1 的规则改记为 series_gap（"序列没有覆盖这一周"），而 series_gap 算"阻塞"，所以阻塞的名字-周从 569 变成 21,593。这些周版本 1 里本来就缺，只是换了标签。另外两项：没有终值行的早结束序列从 2 个变成 5 个，前 250 名里缺供应商行的名字-日从 367 变成 713。
7. **QuantConnect 没有用。** 要让它报告缺口股票，就得把缺口名单传上去，而上传在 10 月 5 日已经被本机的安全检查拦下。反过来让 QC 自己抽样，每次回测只能带回几百个数，要跑几百次。所以这次跳过了。
8. **版本 1 没有被改动：** 构建结束后检查过，版本 1 的缓存和 inputs 里没有任何文件比构建开始的时间更新。

**术语：**
- **名额（slot）**：每周前 250 名中的一个位置；一年的名额 = 250 × 周数。
- **2% 规则**：某年缺价格的股票估计占了超过 2% 的名额，这一年只报告、不用来判断策略（你 10 月 2 日的决定）。"模型读数"是判定口径；"校准读数""上限读数"只作参考。
- **档案馆抓取（capture）**：archive.org 在某一天存下的一份雅虎网页或 CSV 文件副本。
- **补满整周**：一周里每个交易日都有价格，才把这一周补进去；补不满的周仍算缺失。
- **第三票**：两个来源打架的日子，用独立的第三个来源投票，看谁对。
- **D5**：退市后找不到任何价值的股票按 −55% 记（另做 −100% 压力测试）。
- **场外价格（OTC）**：股票从纳斯达克退市后在场外市场的成交价；计划 4.5 节规定，有场外价格就用它，不再套 D5。
- **validate（检查）**：数据计划第 6 节的 35 项自动检查。

---

## Engineering body

Scope: data only. No signal, strategy return, portfolio return, long-short spread or cross-stock return aggregate was computed. The report quotes no vendor price level. Single-stock day returns appear only where version 1's audit already quoted them (the corrected corporate-action days, §4).

### 0. What version 2 is, and what it is not

- **Owner decisions of 2026-10-05** (plan §0): the three new fill sources are used (archive.org Wayback captures of old Yahoo `table.csv` files and history pages; companiesmarketcap.com daily market cap; the QuantQuote free S&P 500 pack from archive.org), their terms risk accepted; QuantConnect values may be pulled back (its terms v1.4 §2.6 risk accepted).
- **A robustness version.** Every rule of version 1 is unchanged (plan §0). If version 2 changes a conclusion, the conclusion counts as unreliable.
- **Version 1 is untouched.** Version 2 lives in its own copies: `research_cache/reversal_2012_2026_v2/` (a copy-on-write clone of the v1 cache, then rebuilt), `output/research_only/reversal_2012_2026/inputs_v2/` (a clone of the v1 inputs, then rebuilt) and `research_cache/reversal_2012_2026_v2_fill/` (the new sources' raw bodies, parsed series and their own request ledger). `REVERSAL_DATA_VERSION=v2` points every pipeline step at the copies (`scripts/reversal_data_common.py`); the default stays v1. A check after the build found no v1 file written (§8).
- **Not in this version:** the November Tiingo month (it opens 2026-11-01), so the 371 month-2 symbols are still pending; and QuantConnect (§6).

### 1. New sources and the fetch

Code: `scripts/reversal_data_v2_archive.py` (steps `targets`, `index`, `fetch`, `parse`, `cmc`, `quantquote`, `status`). Every body is cached before it is parsed, a 404 is cached as a marker, and every request is logged in the fill cache's own `raw_index.csv.gz` / `quota_ledger.csv` (the v1 ledgers are never appended to). Bodies already fetched by the survey pilot (`research_cache/data_source_probe/raw/`) and by the mega-cap OOS2 study (`research_cache/megacap_oos2/raw/`) are reused without a request.

**Targets** (`targets`, offline, from v1 files only): every v1 security with a missing listed name-week whose expected top-250 weight (`weekly_listed.p_top250`) is at least 0.001 or whose size is unknown; each such week from 75 calendar days before (the dv50 warm-up) to 28 days after (a 4-week hold), under the ticker in use that week (`weekly_listed.ticker`); the 50 `awaiting_d5` names (the last Nasdaq weeks, plus the OTC tickers T+Q and T+F to 30 days after the delisting); the securities with unresolved two-source days (a third vote); CALD's last sessions; and the three distributed securities of the fixes that no local file or live Yahoo chart prices (QRTEP, GLIBP, ENVXW). Order: the D5, fix and child names first, then by summed expected top-250 weight, largest first. The list has 1,622 need spans of 1,186 securities under 1,364 tickers: 1,100 spans of missing weeks (955 securities), 368 spans of unresolved days, 50 + 100 D5 spans (Nasdaq and OTC tickers), CALD and the three child securities.

**Capture index** (`index`): the CDX index of the whole URL prefix of the three old `table.csv` hosts and of `finance.yahoo.com/q/hp` (56 index pages instead of one query per ticker), plus one CDX query per ticker for `finance.yahoo.com/quote/T/history`. The bulk index holds 242,799 usable captures (209,909 `q/hp` pages, 32,827 daily `table.csv` files, 63 dividend files) of 51,966 tickers; 837 of the target tickers have a `q/hp` page or a `table.csv` file.

**Capture choice:** a greedy cover of each need span: `table.csv` files first (their URL's a..f span, daily files only), then pages (a `q/hp` capture covers the 95 calendar days up to its timestamp, about 66 sessions; a `quote/T/history` capture the 365 days up to it), each capture taken only while it adds at least 5 uncovered sessions.

**Rates** (as the survey): archive.org at most 15 requests a minute, 2 seconds apart, 6 in flight (it refused connections at about 30 a minute); companiesmarketcap one request every 3 s; Yahoo (the distributed securities' charts) one every 2 s; SEC at most 7 a second with the contact from `src/io/sec_contact.py` (never printed). No account was created and no bot check was met or bypassed.

**Parsing** (`parse`): each capture becomes its canonical record. Whether a capture's Close is as traded (old `q/hp` pages and `table.csv` files) or split-adjusted as of the capture (`quote/T/history`) is decided at each in-window split by the capture's own Adj Close; raw close = Close × the later split ratios in adjusted mode, raw volume = volume ÷ them, dividends from the capture's dividend rows (as paid). Identity rules: rows only inside the security's own `ticker_intervals` spans for that ticker (the ticker in use on each date), and up to the delisting; a capture that shows more than 5 sessions after the delisting is dropped whole (survey rule), except under the OTC tickers of D5 names, whose rows are kept inside the D5 span only.

**Status of the long fetch (interim).** The fetch started 2026-10-05 16:05 UTC and runs in the background (`nohup caffeinate -i ... reversal_data_v2_archive.py fetch`). At the snapshot this build was made from (17:19 UTC) it had finished 232 of 1,186 target securities in priority order: the 54 D5, fix and child names and the 178 heaviest gap names by expected top-250 weight. It had made 586 page requests and 686 CDX queries to archive.org (all under 15 a minute, no refusal; 3 network errors retried), with 0 failed securities. From the measured pace (8 securities in about 3 minutes) the rest takes about 6 more hours, less than the survey's 30-hour estimate because the bulk index replaced most per-ticker queries. The snapshot parsed into 270 usable captures (7 dropped for showing more than 5 sessions after a delisting, 14 with no rows, 2 outside the ticker's spans) and 57,084 dated rows for 133 securities.

companiesmarketcap (`cmc`): the probe's identity rules (a delisted name takes only a `T.defunct.<year>` search result within a year of its delisting, or a guessed slug whose title carries `(T)` and whose series ends within a year of the delisting), for the first 500 target securities in priority order. It made 1,674 requests (search, guessed slugs and pages) and matched 104 securities. QuantQuote (`quantquote`, offline): the pack the probe downloaded, every target ticker's file inside the security's own spans: 13 target securities have rows (QuantQuote holds only the S&P 500 members of its 2013 file date).

### 2. How the new rows enter (precedence and second-source rules)

Code: `scripts/reversal_data_v2_fill.py`, called inside step 9 (`reversal_data_reconcile.py`) when `REVERSAL_DATA_VERSION=v2`, after every security's v1-rule series is built and before the tables are written; the terminal step (`reversal_data_terminal.py`) reads the archived rows too. The survey's §5 plan, as implemented:

- **Precedence.** v1's order first, unchanged (WIKI → Tiingo → Yahoo to 2017-10-31; Tiingo → Yahoo → WIKI from 2017-11-01). A new row enters only on a session no v1 source holds. Then the archived `table.csv`, then the archived history pages (the latest capture holding the session), then QuantQuote, then companiesmarketcap; QuantConnect is not used (§6). `src_primary` of a new row is `archive`, flagged `archive_csv` or `archive_page`.
- **Levels.** QuantQuote (split- and dividend-adjusted closes) and companiesmarketcap (market cap) carry no raw level, so they never make a panel row: the $10 filter and dollar volume need the raw close and volume. They vote on archived rows, and the sessions only they cover go to a local return-only file (`prices/v2_return_only.csv.gz`) for the owner to decide on (§9).
- **Returns.** `tr` = (C·S + D)/C₋₁ − 1 from the capture's own consecutive rows (plan 4.1); only returns are chained, never levels (R8). A first archived row after a v1 row keeps its return only when the capture also holds the previous session and its raw close there is within 0.5% of v1's (else `v2_splice_blank`); after a gap of sessions the return is blank (`gap_return_blank`).
- **Second source (R3).** Two sources within 0.5% confirm each other (plus cent rounding below $1: 0.005/C_t + 0.005/C_{t−1}, the v1 stored-vote allowance). Captures of the same Yahoo page are one source, and archived Yahoo is the same source as live Yahoo. QuantQuote is compared on total return, companiesmarketcap on price return (the archived row's C·S/C₋₁ − 1). `n_sources` counts every source with a return that day and `max_src_diff` the widest gap. Two that disagree with no majority flag the day `disagree_unresolved`; when QuantQuote and companiesmarketcap agree against the archive, the day takes QuantQuote's return (`v2_majority:quantquote`).
- **Whole weeks.** A calendar week gets archived rows only when every listed XNAS session of the week has a row afterwards (v1 or archive); otherwise none of its rows enter and the week stays in the missing tables. The last week of a series that ends within 10 days of its delisting counts to its last row.
- **Third votes.** On a v1 `disagree_unresolved` day, archived Yahoo (independent of WIKI, Tiingo and the stored file, never of live Yahoo) and QuantQuote vote. When exactly one of the disagreeing sources agrees within 0.5% with an independent vote, the day takes that source's return (and its level when it is a vendor), the flag becomes `v2_third_vote:{voter}>{source}`, and the R3 queue row closes as `vendor_error` or `stored_error`.
- **New series.** A security that was not a step-9 target (v1 `not_candidate` gaps) gets a series when the archive fills whole weeks for it.
- **Terminal step.** The archived rows are a source of the last Nasdaq trade (lowest precedence, `archive`), and the OTC tickers' archived rows (`archive_otc`) count only after the last row any source has under the Nasdaq ticker, so plan 4.5's OTC close can value a D5 row.

### 3. What changed against version 1 (interim build)

The build: `scripts/reversal_data_v2_build.py`, from the archive snapshot of 2026-10-05 17:19 UTC (232 of 1,186 target securities fetched). A first run reached a fixed point after 3 passes; validate then showed 48 Item 2.02 headers of newly priced companies not yet cached, so they were fetched from SEC (`reversal_data_earnings.py --fetch-only --sec-rate 4`, 48 requests), and the build was rerun from the terminal step to a fixed point after 2 passes (the hashes of the watched inputs and cache files are equal between the last two passes; `research_cache/reversal_2012_2026_v2/v2_build/fixed_point.json`). Counts: `scripts/reversal_data_v2_report.py` → `output/research_only/reversal_2012_2026/v2_report/`.

**Fills.** 44,220 archived rows for 119 securities (74 had no v1 series at all): 3,802 from `table.csv` files and 40,418 from history pages; by year 2011 673, 2012 1,833, 2013 1,583, 2014 830, 2015 555, 2016 72, 2017 129, 2018 5,433, 2019 11,700, 2020 8,073, 2021 7,059, 2022 4,326, 2023 1,416, 2024 538. The panel has 6,996,016 rows over 3,309 securities (v1 6,951,796 over 3,210). A spot check of the first parsed securities (7 names with v1 rows on the same days) found the archived raw closes within 0.5% of v1's on 92–100% of the overlapping days per name (the misses are below $1, cent rounding). Return-only days (QuantQuote or companiesmarketcap only, no level): 1,382, kept out of the panel. No `v2_splice_blank` was needed.

**Third votes.** 20 v1 `disagree_unresolved` days had an independent vote; 7 were settled (`v2_third_vote:archive>tiingo` ×5 for SGYP 2018-02, `archive>wiki` ×2 for VTNR 2013-05). Archived rows added 21 days on which QuantQuote or companiesmarketcap disagrees with no majority, so the panel holds 1,904 `disagree_unresolved` days (v1 1,890).

#### Missing share of top-250 slots by year, v1 against v2 (Table B: validate check 6, unfillable names; Table A: step 12, every non-pending reason)

| Year | B model v1 | B model v2 | B calibrated v2 | B upper v2 | A model v1 | A model v2 | A upper v1 | A upper v2 | v2 over 2% (B model) |
|---|---|---|---|---|---|---|---|---|---|
| 2012 | 5.031% | 2.908% | 2.908% | 2.954% | 4.875% | 3.338% | 5.090% | 3.546% | **yes** |
| 2013 | 3.192% | 1.900% | 1.900% | 1.900% | 2.704% | 1.550% | 2.719% | 1.566% | no |
| 2014 | 1.015% | 0.308% | 0.308% | 0.346% | 1.224% | 0.592% | 1.348% | 0.716% | no |
| 2015 | 0.672% | 0.083% | 0.083% | 0.083% | 0.707% | 0.237% | 1.311% | 0.840% | no |
| 2016 | 0.785% | 0.669% | 0.669% | 0.669% | 0.822% | 0.683% | 0.869% | 0.730% | no |
| 2017 | 0.915% | 0.915% | 0.915% | 0.915% | 1.117% | 1.117% | 1.317% | 1.317% | no |
| 2018 | 2.669% | 1.946% | 1.949% | 2.177% | 2.474% | 1.781% | 5.597% | 4.396% | no |
| 2019 | 1.885% | 0.723% | 0.724% | 0.785% | 1.826% | 0.784% | 5.087% | 2.800% | no |
| 2020 | 0.966% | 0.309% | 0.314% | 0.762% | 1.481% | 0.898% | 6.274% | 4.671% | no |
| 2021 | 0.700% | 0.015% | 0.018% | 1.285% | 1.593% | 0.769% | 11.046% | 8.823% | no |
| 2022 | 0.585% | 0.223% | 0.224% | 0.769% | 0.966% | 0.590% | 14.597% | 13.213% | no |
| 2023 | 0.492% | 0.085% | 0.086% | 0.654% | 0.601% | 0.231% | 19.940% | 18.561% | no |
| 2024 | 0.385% | 0.031% | 0.033% | 0.208% | 0.415% | 0.067% | 7.384% | 6.790% | no |
| 2025 | 0.231% | 0.231% | 0.231% | 0.231% | 0.225% | 0.225% | 0.440% | 0.440% | no |
| 2026 | 0.000% | 0.000% | 0.000% | 0.014% | 0.016% | 0.016% | 0.292% | 0.292% | no |

#### Gap slots closed by year (v1 missing listed name-weeks that v2 prices and ranks)

| Year | v1 missing name-weeks | closed in v2 | v1 expected top-250 slots (model) | of which closed | closed and in v2's top 250 |
|---|---|---|---|---|---|
| 2012 | 51,188 | 352 | 678.44 | 180.65 | 243 |
| 2013 | 44,712 | 327 | 435.95 | 144.21 | 139 |
| 2014 | 41,820 | 166 | 247.94 | 93.35 | 35 |
| 2015 | 41,034 | 115 | 165.93 | 76.57 | 1 |
| 2016 | 35,997 | 16 | 143.92 | 8.67 | 0 |
| 2017 | 32,694 | 26 | 175.15 | 0.01 | 0 |
| 2018 | 31,276 | 990 | 370.97 | 86.64 | 100 |
| 2019 | 29,891 | 2,431 | 295.09 | 150.07 | 181 |
| 2020 | 28,261 | 1,652 | 264.71 | 106.72 | 104 |
| 2021 | 29,212 | 1,438 | 261.83 | 109.63 | 137 |
| 2022 | 30,602 | 892 | 137.23 | 47.93 | 45 |
| 2023 | 26,649 | 296 | 78.87 | 47.93 | 46 |
| 2024 | 19,386 | 122 | 54.16 | 45.36 | 47 |
| 2025 | 10,658 | 0 | 35.19 | 0.00 | 0 |
| 2026 | 2,086 | 0 | 7.90 | 0.00 | 0 |
| all | 455,466 | 8,823 | 3,353.28 | 1,097.74 | 1,078 |

#### Validate: v1 {'pass': 17, 'fail': 17, 'no_input': 1} against v2 {'pass': 17, 'fail': 17, 'no_input': 1}

| Check | v1 | v2 |
|---|---|---|
| (no check changed status) | | |


How to read the slot table: "closed" means a v1 missing listed name-week that v2 prices and ranks (not missing in v2); "of which closed" weights each by v1's expected top-250 count (`p_top250`). The largest closures by expected slots: VIA 160.9, PARA 114.9, VIAC 103.8, ARCP 82.3, AMTD 79.5, BMC 74.0, CREE 54.6, LAZR 53.8, MOLX 42.4, DISCB 39.4, INFO 38.8, CZR 38.0 (2012–2015 alone: VIA 138.5, ARCP 82.3, BMC 74.0, MOLX 42.4, LUFK 32.1, DISCB 26.1, LIFE 26.0, VPHM 25.2). CA stays missing: companiesmarketcap gives no volume, so it cannot be ranked. One name-week missing in v2 was not missing in v1.

**Missing-reason labels.** 21,214 name-weeks of v1 `not_candidate` names that now have a partial series are labelled `series_gap` (a series that does not reach the week) under step 12's unchanged rule; validate counts `series_gap` as blocking, so `universe_listed_gaps` has 21,593 blocking name-weeks (v1 569). They were missing in v1 too; only the label moved. The expected-count tables above do not depend on the label.

**Validate.** 17 pass, 17 fail, 1 no_input, as in v1; no check changed status. Numbers that moved: review_queue open 382 → 376; terminal_open 187 → 181; universe_nasdaq100 complete 936 → 938 member-years; universe_proxy_margin worst week 18 → 13 names, name-weeks 3,838 → 2,590; universe_capture_coverage failing dates 95 → 92 (minimum mcap-weighted coverage 0.936 → 0.941); universe_unfillable years over 2% on the model 2012, 2013, 2018 → 2012 (upper 2012, 2018); universe_vendor_source missing universe name-days 367 → 713 and terminal_coverage series without a row 2 → 5 (both from names newly in the universe); multi_source_agreement 0.992628 → 0.992623 (unresolved 1,890 → 1,904); universe_form25 failing 71 → 72; candidates_resolved 115 open (unchanged).

### 4. Fixes: the 5 own errors and 8 unrecorded corporate actions of the QC audit

Committed table: `inputs_v2/v2_fixes.csv` (12 fixes; the LMCA 2014-07-24 C-share dividend is both own error 1 and one of the 8 unrecorded events). It holds SEC facts only: the share ratios, the documented cash, the SEC URL and the terms; no price level. Applied inside step 9 (`reversal_data_v2_fill.apply_fixes`); the local `reconcile/v2_fixes_applied.csv` keeps the values used.

**Rule (unchanged from version 1).** A distribution of another security's shares is valued as version 1 valued ZG 2015 and LBTYK 2015 (`REVIEW_EVENT_EXCEPTIONS`): the distributed security at its own close on its first regular-way session, booked as cash D on the parent's ex-date row (the CRSP convention of EBAY 2015-07-20's `spinoff_cash`); the parent's own share ratio (UNTD's 1-for-7, LMCA's and LMCK's 0.25 new shares in 2016, GLIBA's 0.63) is S. `tr` is recomputed from the row's own prior close, C₋₁ = (C·S_old + D_old)/(1 + tr_old), so no level is chained. Version 1 left these open only because "the distributed security needs a value"; the value now comes from local data or one Yahoo chart: the child's canonical v2 series, its WIKI raw file, the cached Yahoo chart restored to raw, a Yahoo chart fetched once (`prefetch-children`), or an archived history page of the child.

| Fix | Finding | Security, ex-date | Terms (SEC) | Child close from | Applied | Day return v1 → v2 |
|---|---|---|---|---|---|---|
| F01 | own error 1; unrecorded LMCA 2014 | LMCA 2014-07-24 | 2 LMCK per LMCA (EX-99.1 2014-07-23) | FWONK Yahoo chart (LMCK's later ticker) | yes | −65.4% → +0.4% |
| F02 | own error 2 | LMCA 2014-11-04 | ¼ LBRDA per LMCA (8-K 2014-11-04) | LBRDA canonical | yes | −23.2% → +1.2% |
| F03 | own error 3 | FLEX 2024-01-03 | 0.174185 NXT per FLEX (8-K 2024-01-02) | NXT canonical (a second source agrees) | yes | +7.7% → +5.0% |
| F04 | own error 4 | ENVX 2025-07-17 (the ex-date per the company FAQ, not 07-21) | 1 ENVXW per 7 shares | none: the Yahoo ENVXW chart holds only its last day, no archived page | **no** | — |
| F05 | own error 5 | CALD, last 5 sessions to 2018-04-04 | merger with SAP (8-K 2018-04-05) | needs archived rows: no capture holds them | **no** | — |
| F06 | unrecorded UNTD 2013 | UNTD 2013-11-01 | 1 FTD per 5 pre-split + 1-for-7 (8-K 2013-11-06) | FTD canonical (a second source agrees) | yes | −78.5% → −4.1% |
| F07 | unrecorded LMCA 2016 | LMCA 2016-04-18 | 1 LSXMA + 0.1 BATRA + 0.25 new LMCA (8-K 2016-04-19) | LSXMA canonical, BATRA Yahoo | yes | −51.3% → −3.2% |
| F08 | unrecorded LMCK 2016 | LMCK 2016-04-18 | 1 LSXMK + 0.1 BATRK + 0.25 new LMCK | LSXMK canonical, BATRK Yahoo | yes | −35.2% → −3.6% |
| F09 | unrecorded GLIBA 2018 | GLIBA 2018-03-12 | 0.63 GLIBA + 0.2 GLIBP (8-K 2018-03-08) | none: no GLIBP close anywhere local, on Yahoo or archived | **no** | — |
| F10 | unrecorded SPWR 2020 | SPWR 2020-08-27 | 1 MAXN per 8 (8-K 2020-08-27) | MAXNQ Yahoo chart (Maxeon's history, split-restored) | yes | +23.8% → −1.5% |
| F11 | unrecorded QRTEA 2020 | QRTEA 2020-09-15 | 1.50 USD + 0.03 QRTEP (8-K 2020-08-26) | none: no QRTEP close (Yahoo 404, no archived page) | **no** | — |
| F12 | unrecorded LSXMK 2023 | LSXMK 2023-08-04 | 1 new LSXMK + 0.25 LLYVK (8-K 2023-08-03) | LLYVK Yahoo chart | yes | −11.4% → −2.7% |

8 of the 12 are applied; their day returns now match the terms (the v1 audit and review notes estimated about −4% for UNTD, −3.6% for LMCK, −2.7% for LSXMK and −1.5% for SPWR). Four stay open because no source gives the needed price: ENVX (warrant), GLIBA (preferred), QRTEA (preferred), and CALD's last five sessions; they stay open review items, as in v1. Applying them updated 5 split-table rows, added 3, and closed 6 open review rows. Most child closes rest on one source (a second source agreed for NXT and FTD).

Each applied fix closes its open review rows (`reviewed_moves.csv`: classification `event_confirmed`, the SEC URL, a note) and updates or adds the `split_events.csv` row (event type `spinoff`, the effective factor (C·S + D)/C, the SEC URL).

### 5. The 50 D5 names: SEC evidence and terminal values

Committed table: `inputs_v2/v2_d5_sec_evidence.csv` (50 rows: decision, the key document's accession, URL and filing date, a short paraphrase, the OTC symbol where a document names it; no price level, no return). Every document was read by hand (the pilot's regex screen counted many statements about intercompany claims, which say nothing about the common stock). Requests: SEC only, at most 7 a second, cached under the fill cache.

| SEC decision | Names | Tickers |
|---|---|---|
| zero_recovery: a confirmed plan or effective-date 8-K cancels the old common stock with no distribution | 17 | BBBY, LAZR, GTAT, DNDN, GPOR, SIVB, FRAN, EXXI, REXX, CRGE, SGYP, LNCO, LGCY, CYXT, APPH, VIEW, VTNR |
| not_bankruptcy: delisted for a listing failure, a voluntary delisting, a SPAC deadline or (CTCM) a cash merger, while still operating | 19 | VXRT, UPL, TEAR, NESR, WLB, ASTI, AFIB, DCTH, ELOX, EMAT, PFSA, ONCY, DRTT, CTCM, ODT, TSP, SLAM, AONC, DIAL |
| recovery: the old equity gets something | 7 | MRIN (pro rata residual cash), PDLI (dissolution, no per-share figure), NIHD (dissolution, 1.54–2.70 USD estimated), QTNT (0.01 USD cash per share), GMDA (one CVR per share), CYT (dissolution, 3.10–3.16 USD estimated), LIAN (4.80 USD special dividend before the last Nasdaq day) |
| pending_or_no_plan: bankruptcy or chapter 7 with no stated equity treatment | 7 | SNBR, UNXL, GOEV, TPIC, EVLO, ELMS, VORB |

How v2 uses them (plan 4.5, unchanged):
- **A fixed per-share consideration is a terminal value**, booked by the cash-merger rule: CTCM (2.0503 USD, merger closing 8-K 0001104659-16-122229) and QTNT (0.01 USD, 8-K 0001437749-23-017339) are added to the terminal step's reviewed terms in v2.
- **An OTC close after the last Nasdaq session replaces D5** (plan 4.5): from the archived Yahoo pages of the Nasdaq and OTC tickers, and from live Yahoo charts of the OTC tickers T+Q and T+F (8 of the 50 names have one: DRTT, NESR, GOEV, ELMS, VIEW, SLAM, DIAL, SNBR). The bare ticker is never asked after a delisting (it may be another company's).
- **Ranges, CVRs and pro rata residuals** (NIHD, CYT, PDLI, MRIN, GMDA, LIAN) have no fixed value in the documents: they stay with the D5 rule and are listed for the owner.
- **zero_recovery rows keep −55%** (the D5 rule); the SEC statement is the evidence for the −100% stress test. The decision and the document are written beside every terminal row (`sec_equity_decision`, `sec_equity_accession`, `sec_equity_url`).

Result in v2 (terminal step): 45 rows stay `awaiting_d5`; CTCM is `computed` (cash merger at 2.0503); QTNT is `no_vendor_price` (its consideration is known but no vendor has its last Nasdaq close); SIVB, TPIC and LGCY are `needs_review`: an archived or live-Yahoo OTC close after the last Nasdaq day now values them, but from one vendor only, and the guard holds a value far from the last close for review (as v1 did for CAMP, AMRS, FTD). Terminal statuses over all 951 rows: computed 593 (v1 582), awaiting_d5 45 (50), no_vendor_price 68 (82), needs_review 35 (29), needs_acquirer_price 48 (44), pending_price 28 (30), no_terminal_return 131, unknown 2. Per-name table: `v2_report/d5_resolution.csv`.

### 6. QuantConnect: not used in this build

The owner allowed pulling QC values back (terms risk accepted). It was not done, for a practical reason: pulling weekly returns for the gap names needs QC to know which names and weeks to report, which means sending the gap list (local data) to QC, and that upload path is the one the local safety check refused during the 2026-10-05 audit (audit doc §1.8). The other way, QC choosing its own sample and returning values through runtime statistics, carries a few hundred values per backtest; the gaps here are thousands of name-weeks, so it would take hundreds of backtests. QC therefore stays out of v2 as a source and as a vote. Nothing was run on QC for this build.

### 7. Tests

`tests/test_reversal_data_v2.py` (new): the version switch (v1 default, v2 copies, an unknown version refused), the fetcher's own ledger, the URL and capture-span helpers, the greedy capture cover, the split-text parser, raw-close recovery (adjusted against as-traded captures), the post-delisting count, the fix arithmetic (the row's own prior close), the committed fix table (12 rows, SEC URLs, no level columns), whole-week filling, capture precedence (table.csv before pages, latest capture, returns inside one capture), the second-source count and an unresolved disagreement, the third vote (WIKI against Tiingo settled; archived Yahoo never counted against live Yahoo), cent rounding, the terminal step's D5 evidence columns and its OTC rows after the last Nasdaq row, and the table post-processing of a fix. The existing suites of every pipeline step pass unchanged (`tests/test_reversal_data_*.py`, `tests/test_data_source_probe.py`, `tests/test_audit_data_v1_qc.py`, `tests/test_research_reversal_dev.py`).

### 8. Files

Code (new): `scripts/reversal_data_v2_archive.py` (fetch and parse of the new sources), `scripts/reversal_data_v2_fill.py` (fixes, fills, third votes, table post-processing; `prefetch-children`), `scripts/reversal_data_v2_build.py` (the offline rebuild to a fixed point), `scripts/reversal_data_v2_report.py` (the counts of this report).
Code (changed, behaviour under v1 unchanged): `scripts/reversal_data_common.py` (`REVERSAL_DATA_VERSION`), `scripts/reversal_data_reconcile.py` (the v2 hook), `scripts/reversal_data_terminal.py` (archived and OTC rows, D5 evidence columns, two reviewed terms; v2 only), `scripts/reversal_data_universe.py` and `scripts/reversal_data_validate.py` (`archive` counts as a vendor source in v2; v2 manifest keys).
Committed data (no vendor price level): `output/research_only/reversal_2012_2026/inputs_v2/` (the rebuilt v2 inputs, plus `v2_fixes.csv` and `v2_d5_sec_evidence.csv`; `special_distributions.csv` stays local as in v1, `.gitignore`), `output/research_only/reversal_2012_2026/v2_report/` (counts and IDs).
Local only: `research_cache/reversal_2012_2026_v2/` (the v2 cache; build logs and pass hashes in `v2_build/`), `research_cache/reversal_2012_2026_v2_fill/` (raw bodies, parsed series, request ledger, `progress.json`, logs).
Version 1: `find` over the v1 cache and `inputs/` for files newer than the start of the v2 build returns nothing.

### 9. How to resume the fetch and finish version 2

The archive fetch keeps running in the background after this report. Everything below runs from the worktree with `PYTHONPATH=.`:

- **Status:** `.venv/bin/python scripts/reversal_data_v2_archive.py status` (or read `research_cache/reversal_2012_2026_v2_fill/progress.json`; log `logs/fetch.log`).
- **Stop cleanly:** `touch research_cache/reversal_2012_2026_v2_fill/STOP` (it stops between batches; delete the file before restarting).
- **Resume or restart** (after a stop, a reboot or a crash; finished securities are skipped, every body is cached): `nohup caffeinate -i env PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_archive.py fetch >> research_cache/reversal_2012_2026_v2_fill/logs/fetch.log 2>&1 &`
- **When it has finished:** rebuild offline to a fixed point and refresh the counts: `.venv/bin/python scripts/reversal_data_v2_build.py` then `.venv/bin/python scripts/reversal_data_v2_report.py` (the tables of §3 are regenerated in `output/research_only/reversal_2012_2026/v2_report/tables.md`). If newly filled companies bring Item 2.02 filings whose headers are not cached, run `REVERSAL_DATA_VERSION=v2 .venv/bin/python scripts/reversal_data_earnings.py --fetch-only --sec-rate 4` first (SEC, at most 4 a second), as was done for this build.
- **Not in v2 yet:** the November Tiingo month (from 2026-11-01). Version 2 remains a robustness check whatever it adds.

Open items for the owner:
1. The return-only days (QuantQuote or companiesmarketcap with no raw level): kept out of the panel, listed locally in `prices/v2_return_only.csv.gz`. Using them would need a rule for the $10 filter and dollar volume on those days.
2. The four fixes no source can price (ENVX warrant, GLIBA's GLIBP leg, QRTEA's preferred, CALD's last five sessions).
3. The D5 rows whose documents give a range, a CVR or a residual (NIHD, CYT, PDLI, MRIN, GMDA, LIAN); UPL and WLB, delisted for listing failures and bankrupt months later; and the three rows held for review on a single-vendor OTC close (SIVB, TPIC, LGCY).
4. The fill rests mostly on one source: archived Yahoo is the same source as live Yahoo, and QuantQuote and companiesmarketcap cover few of the filled days.
