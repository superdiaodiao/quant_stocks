# 周度反转检验（2012–2026）数据报告：数据版本 1

> 按[数据计划](reversal_2012_2026_data_plan.md)第 6 节（"Data report"）编写，2026-10-03。本报告只描述数据，**没有计算任何信号、策略收益、组合收益或跨股票的收益汇总**。下面先给 owner 一段中文摘要，后面是英文的工程正文。所有数字都来自文件，出处写在每张表或每段后面。

## 给 owner 的摘要

**这是什么。** 这是"周度、行业调整、剔除财报日的短期反转"检验要用的数据，是**数据版本 1 的候选版**。它来自 2026-10-03 的完整重建，检查结果文件生成于北京时间 12:32:55（UTC 04:32:55）。

**版本 1 还没有冻结。** 计划第 0 节写的是：重建、检查和人工复核都**通过**之后，版本 1 才冻结，不必等以后的免费 Tiingo 月份。现在这个条件**没有满足**：17 项检查未通过，人工复核队列还有 382 条没结（见下文和正文第 3 节）。要冻结，需要你先逐项接受这些未通过项（例如认定它们是免费来源补不了的缺口），或者等它们修好。11 月的 Tiingo 只做数据版本 2，作稳健性对照。

**检查结果。** 计划第 6 节的 35 项检查里，17 项通过，17 项未通过，1 项缺输入、无法判断。未通过的原因可以分成四类：

1. **数据本身缺，免费来源都没有。** 例如 BMC、Life Technologies、Molex、Viacom 早年的价格，没有任何免费来源有。这些缺口会形成"幸存者偏差"：样本里缺了一些后来退市的大公司，样本不再代表当时真实的前 250 名。这会让检验结果偏多少、往哪个方向偏，本报告没有算，也算不出来（对反转检验，方向事先不知道）。
2. **等 11 月的 Tiingo。** 10 月的 500 个免费代码已经全部用完。已经排进 11 月计划的有 371 个新代码，例如 19 只核对样本股，以及 38 只 3–5 亿美元流通市值的退市股。
3. **人工复核还有没结的条目。** 大多数是某一天只有一个价格来源，没有第二个来源可以对照。
4. **规则怎么读要你定。** 例如 Yahoo 只保留到分、Tiingo 多一位小数，这种四舍五入差异算不算"已解释"。

**2% 规则。** 每年 250 个"前 250 名位置"乘以周数，就是当年的"名额"。看其中有多少被"拿不到价格的股票"占着：

- **模型读数**（你定的判定口径）超过 2% 的年份是 **2012、2013、2018**。2012 年最高，约 4.9%–5.0%（两种算法的结果，见正文第 4 节）。
- 按 §0 的决定，这三年**只报告，不拿来判断策略**。
- "校准读数"给每个规模不明的周按抽样比例加一点，得到的仍是这三年。
- "上限读数"把每个规模不明的周都当作前 250 名。这是最坏情况，不作判定。超过 2% 的年份要分两种口径看：
  - 按检查 6 实际用来判通过/不通过的口径（正文表 B，只算补不齐的股票），上限读数在上面三年之外只多出 **2019 和 2021**；
  - 按第 12 步的全部缺失原因（正文表 A），上限读数在 **2018–2024 每年**都超过 2%。

**终值。** 你今天定的 D5 是：退市后找不到任何价值的，按 −55% 记，另按 −100% 做压力测试。数据里有 50 条记录正好是这种情况，文件里的状态写作 `awaiting_d5`（意思是"等 D5 决定"）。版本 1 的文件只给它们打了标记，没有写入数值；检验运行时按 D5 规则记。其中 12 条也在 11 月 Tiingo 计划里，版本 2 拿到价格后可能就不再属于这种情况。

**还要你看的事项**列在正文第 8.2 节，主要有：

- 是否接受 17 项未通过的检查和 382 条未结的复核条目，让版本 1 冻结（见上）；
- 已经是公开发行人（在 SEC 有报告）的公司新挂牌的股票——这类发行人的 IPO、分拆出来的新股、新的股票类别、破产重组后的新股、原先不上市的 REIT 挂牌——头 25 个交易日算不算"新股"（young，即不算缺数据）。问的是这几类，不是泛指所有新上市公司；
- 分红抽样该用哪一对来源比；
- 核对样本（V 样本，50 只拿 Tiingo 和 Yahoo 逐日对比的股票）里的四舍五入差异算不算已解释；
- 三种读数的用法，以及"短窗口周"（short_window：有收盘价、但 50 日成交额窗口里不到 25 行数据的周，见术语）按证据判断的读法，要你确认；
- 3 家破产股的终值只依据一家供应商的第一个场外收盘价。

**术语。**

- **名字-周（name-week）**：一只股票在一周里的一条记录。
- **前 250 名位置（slot）**：每周按成交额排出的前 250 个位置。
- **unfillable（补不齐）**：试过所有免费来源都拿不到价格。
- **unknown size（规模不明）**：某周既没有市值、流通市值，也没有成交额证据，无法判断它大不大。
- **单一来源日**：那一天只有一家供应商有价格，没有第二家可以对照。
- **人工复核 / verifier（核验员）**：先有人对照 SEC 文件或公司公告给出结论（verdict），再由另一位独立抽查；抽查中改过的结论记作"corrected by verifier"。
- **终值（terminal value）**：股票退市那一刻，持有者实际得到的价值，例如现金对价或换来的股票。
- **awaiting_d5**：终值文件里的一种状态，表示"退市后找不到任何价值，等 D5 规则来定"。
- **V 样本（V sample）**：计划里抽出的 50 只股票，同一天拿 Tiingo 和 Yahoo 的收益逐日对比，用来检查两家供应商是否一致。
- **short_window（短窗口周）**：这一周股票有收盘价，但它算 50 日成交额用的窗口里不到 25 行数据，又不适用"新股"规则。这种周按其他证据（规模、成交额排名等）判断它是不是前 250 名。

---

## Engineering body

Data version 1, candidate. Plan §0 freezes version 1 once the rebuild, the checks and the hand reviews **pass**. That condition is not met: 17 checks fail (§3) and the review queue has 382 open items (§3.2 #18). Version 1 is frozen only once the owner accepts these failures or they are fixed (§8.2). Scope: data only. No signal, strategy return, portfolio return, long-short spread or cross-stock return aggregate was computed for this report.

- The step outputs say the same: "no return of any kind is read or computed" (`universe_summary.json` `no_returns`); "counts, shares of days or names and id lists only" (`validation_summary.json` `no_returns_aggregated`); "this summary holds counts only" (`reconcile/summary.json` `no_returns_aggregated`).
- The extra tables computed for this report count rows, sources and name-weeks only. They are marked *(computed for this report)* with the file and the method.
- The report quotes no vendor price levels and no dividend amounts.

### File aliases used in citations

| Alias | Path |
|---|---|
| INPUTS | `output/research_only/reversal_2012_2026/inputs/` |
| CACHE | `/Users/bytedance/code/quant_stocks/research_cache/reversal_2012_2026/` (local only, not committed) |
| [VS] | `INPUTS/validation_summary.json` (generated 2026-10-03T04:32:55Z = 12:32:55 +0800, code_version 2026-10-03.2; "the 12:33 build" below means this rebuild) |
| [MF] | `INPUTS/manifest.json` |
| [US] | `CACHE/universe/universe_summary.json` (built 2026-10-03T04:31:53Z) |
| [CBY] | `CACHE/universe/completeness_by_year.csv` |
| [RS] | `CACHE/reconcile/summary.json` (built 2026-10-03T04:09:06Z) |
| [WS] | `INPUTS/weekly_universe_summary.csv` |
| [T300] | `INPUTS/weekly_universe_top300.csv.gz` |
| [PANEL] | `CACHE/prices/daily_panel.csv.gz` |
| [M2] | `CACHE/prefilter/tiingo_month2_plan.csv` |
| [R10] | `CACHE/review/round10/` (`merged/merge_summary.json`, `merged/*_verdicts.csv`, `gaps/`) |
| [R10D] | `CACHE/round10_defects.json` |
| [QL] | `CACHE/quota_ledger.csv` |

"Universe" below means name-weeks with dv50 rank or dv20 rank ≤ 250 in [T300] ([VS] `universe_definition`). That is 198,579 name-weeks of 1,155 securities ([VS] `sic_ff49`; *computed for this report*: unique `security_id` in [T300] rows with either rank ≤ 250).

---

## 1. Sources

Request counts, endpoints and fetch windows come from [MF] `sources` (built from `raw_index.csv.gz`). No request was made after 2026-10-02T20:24:29Z ([QL], latest `fetched_utc`). Keys and the SEC contact string are not shown.

| Source | What it covers in this data | Requests | Fetched (UTC) |
|---|---|---|---|
| Nasdaq Data Link WIKI Prices | Raw OHLCV, splits and dividends to 2018-03-27; primary source for 2011–2017 rows | 21 | 2026-10-01 01:32 – 03:52 |
| Tiingo daily (free tier) | Delisted names 2018–2024, verification sample, late starts; `supported_tickers.zip` for range matching | 500 (+1 zip) | 2026-10-01 12:53 – 10-02 09:40 |
| Yahoo v8 chart (unofficial) | Full history of names still listed, with split and dividend events | 2,451 | 2026-10-01 12:46 – 10-02 08:51 |
| Nasdaq API | 100-name dividend cross-check sample (step 7b); QQQ tail and dividends | 102 | 2026-10-01 03:58 – 10-02 17:41 |
| Wayback Machine | Nasdaq symbol-directory files and company lists (market cap), 2010–2019 | 175 | 2026-10-01 01:31 – 03:58 |
| SEC EDGAR (all endpoints) | Form 25 list (EFTS), submissions JSON, index headers (acceptance times, SIC), XBRL frames (public float), merger and distribution documents, review documents | 149,200 in total, of which: headers 117,892, docs 13,048, submissions 10,168, archives 5,081, terminal docs 1,964, other 1,047 | 2026-10-01 01:33 – 10-02 20:24 |
| Ken French data library | 5-factor 2×3, momentum, short-term reversal, 49 industries (daily), Siccodes 49/17/12; CRSP build 202608 pinned by SHA-256 | 11 | 2026-10-01 01:35 – 01:36 |
| CBOE | VIX daily history | 3 | 2026-10-01 |
| Invesco | QQQ distribution history | 1 | 2026-10-01 |
| Repo stored files (`cleaned_stocks_data/price`) | A third vote on returns only, never a price level (plan 4.2) | 0 | local |

The SEC total is the sum of the 18 `sec*` entries in [MF] `sources`. Tiingo used 500 unique symbols, which is the whole October free allowance: [QL] has 500 rows and 500 unique symbols for `tiingo`, month 2026-10, all status 200.

Rows of the canonical price panel by primary source: Yahoo 4,565,681, WIKI 1,705,914, Tiingo 680,201. That is 6,951,796 rows over 3,210 securities ([VS] `panel_integrity`). Precedence is WIKI → Tiingo → Yahoo up to 2017-10-31, and Tiingo → Yahoo → WIKI from 2017-11-01 ([RS] `rules`).

Vendor terms: Tiingo, Yahoo and Nasdaq values stay in CACHE and are never committed. Only ranks, IDs and SEC facts are committed (plan §8 item 8, owner decision 2026-10-02).

---

## 2. Coverage by year

### 2.1 Listings

| Year | Snapshots in index: Wayback company list / Wayback symbol file / repo symbol file | Weeks by snapshot source used (company list / Wayback symbol / repo) | Max snapshot age (days) | Mean listed common stocks per week | Mean foreign filers excluded per week | Mean investment companies excluded per week |
|---|---|---|---|---|---|---|
| 2012 | 8 / 1 / 0 | 31 / 21 / 0 | 119 | 2,225 | 222 | 24.7 |
| 2013 | 10 / 2 / 0 | 48 / 4 / 0 | 80 | 2,166 | 220 | 26.0 |
| 2014 | 8 / 2 / 0 | 37 / 15 / 0 | 70 | 2,217 | 232 | 30.3 |
| 2015 | 13 / 5 / 1 | 34 / 19 / 0 | 53 | 2,268 | 259 | 30.6 |
| 2016 | 28 / 8 / 0 | 31 / 21 / 0 | 40 | 2,237 | 260 | 30.4 |
| 2017 | 23 / 5 / 0 | 43 / 9 / 0 | 61 | 2,219 | 259 | 27.8 |
| 2018 | 2 / 10 / 8 | 4 / 36 / 12 | 38 | 2,238 | 285 | 26.9 |
| 2019 | 3 / 6 / 4 | 1 / 27 / 24 | 58 | 2,274 | 315 | 24.7 |
| 2020 | 0 / 0 / 123 | 0 / 0 / 53 | 56 | 2,315 | 346 | 23.1 |
| 2021 | 0 / 0 / 137 | 0 / 0 / 52 | 25 | 2,579 | 441 | 23.0 |
| 2022 | 0 / 0 / 29 | 0 / 0 / 52 | 28 | 2,757 | 520 | 22.1 |
| 2023 | 0 / 0 / 23 | 0 / 0 / 52 | 28 | 2,696 | 555 | 21.0 |
| 2024 | 0 / 0 / 24 | 0 / 0 / 52 | 31 | 2,579 | 589 | 21.0 |
| 2025 | 0 / 0 / 12 | 0 / 0 / 52 | 30 | 2,481 | 677 | 20.6 |
| 2026 | 0 / 0 / 9 | 0 / 0 / 29 | 29 | 2,411 | 718 | 21.5 |

Sources:

- The snapshot counts are [VS] `listing_snapshots` `snapshots_by_year`. The index also holds 11 captures from 2010–2011 used as earlier anchors (2010: 1 Wayback symbol file; 2011: 7 company lists and 3 symbol files). With the 504 captures of 2012–2026 that makes 515 snapshots in all.
- The other columns are *computed for this report* from [WS]: the `snapshot_source` count, the maximum of `snapshot_age_days`, and the means of `n_listed_common`, `n_foreign_excluded` and `n_investment_company_excluded` by calendar year of `week_end`.

All 759 weeks have a snapshot. No week is older than 160 days; the maximum is 119 days. All 413 symbol files have at least 1,000 rows ([VS] `listing_snapshots`).

### 2.2 Prices

| Year | Panel rows Tiingo / WIKI / Yahoo | Securities in panel | Panel: name-days with ≥ 2 sources agreeing within 0.5% (share of ≥ 2-source days) | Universe name-days with a vendor row (formation week + 4 weeks) | Universe-week days: share with a single source |
|---|---|---|---|---|---|
| 2011 | 3,650 / 144,165 / 32,787 | 1,233 | 0.99757 | — | — |
| 2012 | 6,104 / 252,982 / 56,116 | 1,295 | 0.99361 | 1.000000 | 0.267 |
| 2013 | 6,199 / 269,025 / 60,384 | 1,393 | 0.99503 | 0.999433 | 0.277 |
| 2014 | 9,600 / 283,307 / 71,638 | 1,525 | 0.99863 | 0.999788 | 0.250 |
| 2015 | 16,931 / 275,801 / 92,998 | 1,619 | 0.99578 | 0.999916 | 0.210 |
| 2016 | 21,684 / 264,866 / 113,777 | 1,671 | 0.98608 | 0.999986 | 0.173 |
| 2017 | 39,701 / 212,746 / 159,871 | 1,725 | 0.98729 | 0.999746 | 0.104 |
| 2018 | 102,235 / 3,022 / 320,907 | 1,835 | 0.98978 | 0.996697 | 0.200 |
| 2019 | 95,473 / 0 / 350,362 | 1,864 | 0.99223 | 1.000000 | 0.196 |
| 2020 | 88,969 / 0 / 379,932 | 2,008 | 0.99424 | 1.000000 | 0.157 |
| 2021 | 85,236 / 0 / 446,736 | 2,307 | 0.99875 | 0.999684 | 0.120 |
| 2022 | 73,764 / 0 / 496,126 | 2,350 | 0.99451 | 0.999957 | 0.080 |
| 2023 | 56,432 / 0 / 511,392 | 2,362 | 0.98911 | 1.000000 | 0.050 |
| 2024 | 38,185 / 0 / 534,279 | 2,351 | 0.99113 | 1.000000 | 0.012 |
| 2025 | 25,695 / 0 / 554,705 | 2,408 | 0.99246 | 0.999858 | 0.010 |
| 2026 | 10,343 / 0 / 383,671 | 2,429 | 0.99315 | 0.999718 | 0.013 |

Sources:

- Panel rows and securities are *computed for this report* from [PANEL]: counts of `src_primary` and unique `security_id` by year of `date`.
- The agreement column is [VS] `multi_source_agreement` `by_year`. Sources there are Tiingo, Yahoo, WIKI and the stored file's vote where valid.
- The vendor-row column is [VS] `universe_vendor_source` `by_year`.
- The last column is *computed for this report*. It joins [T300] universe name-weeks to [PANEL] rows of that security in the same week (the 7 days ending on `week_end`). It gives the share with `n_sources` = 1, where the stored-file vote counts as a source as the reconcile step counts it. Over all years: 955,661 days, 139,153 with one source. The one-source days are mostly WIKI-only in 2012–2017 and Tiingo-only in 2018–2023.

Other price facts:

- Universe top-250 name-weeks with a canonical close in the week: 0.99943–1.0 in every year (*computed for this report*: `close_in_week` = Y in [T300] universe rows).
- Days with two or more **vendors** (stored file not counted): 1,581,753. Of these, 0.99701 agree within 0.5% ([RS] `multi_vendor_days.vendor_only`).
- Dividends table: 32,230 rows over 996 securities; 22,592 of the rows have one source only ([RS] `dividends`).

### 2.3 Earnings dates

| Year | Universe company-quarters | Share with an event (Item 2.02 or usable 10-Q/10-K fallback) | JSON acceptance time labelled UTC but actually Eastern |
|---|---|---|---|
| 2012 | 1,172 | 0.985 | 0.450 |
| 2013 | 1,193 | 0.982 | 0.438 |
| 2014 | 1,210 | 0.988 | 0.427 |
| 2015 | 1,219 | 0.982 | 0.405 |
| 2016 | 1,202 | 0.983 | 0.378 |
| 2017 | 1,225 | 0.976 | 0.344 |
| 2018 | 1,239 | 0.978 | 0.304 |
| 2019 | 1,211 | 0.977 | 0.280 |
| 2020 | 1,267 | 0.976 | 0.252 |
| 2021 | 1,243 | 0.974 | 0.211 |
| 2022 | 1,197 | 0.984 | 0.169 |
| 2023 | 1,204 | 0.985 | 0.135 |
| 2024 | 1,216 | 0.984 | 0.091 |
| 2025 | 1,216 | 0.988 | 0.046 |
| 2026 | 586 | 0.995 | 0.004 |

Source: [VS] `earnings_coverage` `by_year` and `earnings_header_times` `json_mismatch_rate_by_year`.

- **Totals.** 17,600 company-quarters, with a share of 0.981989; 0.991648 counting any fallback.
- **Gaps between events.** 35,640 gaps; 0.944697 lie within 60–120 days.
- **Headers.** All 41,253 event headers were fetched, and 41,251 resolve the acceptance time from the header.
- **Hand check.** 40 of 40 events match the company release ([VS] `earnings_gaps`, `earnings_header_times`, `earnings_hand_sample`; INPUTS/`earnings_hand_checks.csv`).

### 2.4 SIC and FF49

| Year | Universe name-weeks | With an FF49 code in [T300] | SIC from a header on or before the week | Earliest header (flagged) | SIC from the master file | Unmatched SIC sent to FF49 "Other" |
|---|---|---|---|---|---|---|
| 2012 | 13,451 | 13,451 | 13,351 | 49 | 51 | 0 |
| 2013 | 13,577 | 13,577 | 13,499 | 53 | 25 | 0 |
| 2014 | 13,577 | 13,577 | 13,496 | 29 | 52 | 0 |
| 2015 | 13,948 | 13,948 | 13,854 | 43 | 51 | 0 |
| 2016 | 13,582 | 13,582 | 13,512 | 18 | 52 | 0 |
| 2017 | 13,655 | 13,655 | 13,593 | 10 | 52 | 0 |
| 2018 | 13,776 | 13,773 | 13,694 | 27 | 52 | 0 |
| 2019 | 13,630 | 13,626 | 13,535 | 39 | 52 | 0 |
| 2020 | 13,963 | 13,963 | 13,883 | 44 | 36 | 10 |
| 2021 | 13,740 | 13,740 | 13,629 | 59 | 52 | 11 |
| 2022 | 13,508 | 13,508 | 13,445 | 11 | 52 | 18 |
| 2023 | 13,526 | 13,509 | 13,489 | 8 | 12 | 52 |
| 2024 | 13,530 | 13,525 | 13,513 | 12 | 0 | 26 |
| 2025 | 13,581 | 13,581 | 13,562 | 19 | 0 | 0 |
| 2026 | 7,535 | 7,535 | 7,507 | 28 | 0 | 0 |

Source: *computed for this report* from [T300] universe rows, using `ff49` not blank and the `sic_basis` prefix by year.

- **Why [VS] and [T300] differ (about 0.3 points).** [T300] gives an FF49 code to 198,550 of 198,579 universe name-weeks (0.99985). [VS] `sic_ff49` gives 0.99655 (197,894 with a Siccodes49 range) and 0.99714 with unmatched SIC counted as "Other". The 656 name-weeks between 197,894 and 198,550 have two causes:
  - [T300] maps the unmatched SIC 3990 (117 name-weeks) to FF49 49 "Other", as plan 5.2 proposes; [VS] counts them as missing until that rule is registered.
  - [VS] counts only header SIC (`with_sic` 198,011). It treats the 539 name-weeks whose SIC comes from the master file (SBNY, `sic_basis` = master) as having no SIC, while [T300] gives them a code.
  - Both [VS] shares pass the ≥ 0.99 threshold.
- **No FF49 code at all.** Only OZK (1569650), 29 name-weeks, has none.
- **SIC changes and blank checks.** 92 universe CIKs change SIC. For 35 universe CIKs the 6770 blank-check code is replaced by the operating SIC, 202 rows in all ([VS] `sic_ff49`).

---

## 3. Validation checks

[VS] counts: 17 pass, 17 fail, 1 no_input. The `failed` list in [VS] has 18 names because it also includes the no_input check `universe_fetch_margin`.

### 3.1 All checks

| # | Check | Status | Key number ([VS] `numbers`) |
|---|---|---|---|
| 1 | factor_rows | pass | 107 series × 3,655 rows, 0 failing |
| 2 | factor_hashes | pass | 7 zips, build 202608, 0 hash failures |
| 3 | qqq_join | pass | 755 overlap days, max diff 0; 60 dividends |
| 4 | ff_industry_maps | pass | FF49 598 ranges, no overlaps |
| 5 | listing_snapshots | pass | 759/759 weeks, max age 119 days |
| 6 | form25 | pass | 5,037 rows, 0 duplicates; all 2,508 sue_lt rows found |
| 7 | security_master | pass | 8,281 securities, 0 ticker overlaps, 0 foreign (Y) in universe |
| 8 | candidates_resolved | **fail** | 115 open of 3,035 rows |
| 9 | earnings_coverage | pass | 0.982 |
| 10 | earnings_gaps | pass | 0.945 within 60–120 days |
| 11 | earnings_hand_sample | pass | 40/40 |
| 12 | earnings_header_times | pass | 41,253/41,253 fetched |
| 13 | sic_ff49 | pass | 0.99655 |
| 14 | panel_integrity | pass | 6,951,796 rows, 0 defects |
| 15 | tiingo_row_check | **fail** | max err 0.638; 21 of 500 files over 1e-8 |
| 16 | multi_source_agreement | **fail** | 0.992628 (needs 0.995); 1,890 unresolved |
| 17 | v_sample | **fail** | 31 of 50 compared; 0.972 within 1e-4 |
| 18 | review_queue | **fail** | 382 open of 1,010 |
| 19 | stored_comparison | pass | 2025-06-24 cluster and 2023 switch both show |
| 20 | split_agreement | **fail** | agree 0.665 (needs 0.98); 9 unreviewed |
| 21 | known_cases | pass | 13/13 reproduced |
| 22 | dividends | **fail** | Tiingo–Nasdaq 18 usable names (needs 100) |
| 23 | terminal_coverage | **fail** | 2 of 493 early-ending series without a row |
| 24 | terminal_open | **fail** | 187 open rows |
| 25 | universe_build | pass | 759 weeks, ranks unique |
| 26 | universe_vendor_source | **fail** | 0.999645 (needs 100%) |
| 27 | universe_proxy_margin | **fail** | max 18 in a week; 8,627 unknown-size name-weeks |
| 28 | universe_capture_coverage | **fail** | 95 of 95 company-list dates below 99% |
| 29 | universe_nasdaq100 | **fail** | 936 of 957 member-years complete |
| 30 | universe_form25 | **fail** | 71 of 477 fail |
| 31 | universe_fetch_margin | **no_input** | 1 required B-B name pending |
| 32 | universe_unfillable | **fail** | model > 2% in 2012, 2013, 2018 |
| 33 | universe_listed_gaps | **fail** | 569 blocking name-weeks |
| 34 | plan_files | **fail** | 1 value outside the plan's set |
| 35 | manifest_coverage | pass | 49 files, 0 missing |

### 3.2 Each failure: reason and kind

The **kind** column uses four labels:

- **gap**: a data gap that no free source fills.
- **M2**: pending the November Tiingo month. A match means the security is in [M2].
- **review**: an open hand-review item.
- **owner**: a reading for the owner to decide.

Counts are from [VS] unless another file is named.

**#8 candidates_resolved: 115 open.**

The check counts candidates with no vendor series over 95% of the needed sessions and no unfillable window covering the need. Its note says "the Tiingo month-1 run is still filling". That note is stale: the month-1 run finished with 500 of 500 symbols (§1), and [VS] `live_inputs_moved_on` is empty.

| Status | Open | In [M2] | Kind |
|---|---|---|---|
| conditional_tier_c | 38 | 38 | M2 (tier C was triggered, see #31) |
| pending_month2 | 23 | 23 | M2 (Yahoo fallbacks) |
| yahoo_failed | 10 | 10 | M2 |
| partial | 27 | 13 | M2 for 13; gap for 14 (the vendor series is shorter than the need) |
| done (coverage < 95%) | 14 | 0 | gap (vendor series ends before the need; e.g. CAVM 0.943, FINL 0.919, MRVL 0.920) |
| no_data_in_window | 2 | 0 | gap / review: PNT (1811764; the B-B sample name behind #31's no_input, not in [M2]), and RNAM (requested under a wrong ticker; [R10D] says Avidity should be re-asked as RNA, but RNA is not in [M2]) |
| no_data | 1 | 0 | gap (ILG) |

*The In [M2] column is computed for this report*: the open rows were recomputed with `reversal_data_validate._candidate_rows` on the same files (115 rows, matching [VS]) and matched to [M2] `security_id`. In total, 84 rows are M2 and 31 are gap.

**#15 tiingo_row_check: maximum error 0.638 (WINMQ); 21 files over 1e-8.**

The rows over the limit are of three kinds: sub_cent 685, dividend_times_split 10, other 39. 20 of the 21 files are over only through these two kinds (sub_cent and dividend_times_split). 766 panel rows carry the `tiingo_adj_identity` flag, and 695 of them have another source within 0.5%.

- The error is in Tiingo's own `adjClose`. The canonical record never reads `adjClose` (plan 4.2), so this is a vendor quirk and not a defect in the data: **gap**.
- One question stays open: on days that carry both a split factor and cash, is the cash per old share or per new share? [R10D] `checks_misc` names possible double counts in AFSI, LBAI and LBRDA/LBRDK 2025-07-15: **review**.

**#16 multi_source_agreement: share 0.992628 (needs 0.995); 1,890 unresolved days.**

- Only 38 of the unresolved days are universe days, and the universe share is 0.999156.
- By year, the unresolved days are 2011 92, 2012 120, 2013 167, 2014 92, 2015 323, 2016 359, 2017 283 and 2018 425; after 2018 there are at most 9 a year. So they sit in the WIKI years. Most are two-source days with no third source to break the tie: **gap**.
- The 38 universe days are **review**. [R10D] `checks_misc` notes that round 10 gave no verdicts for the universe days.

**#17 v_sample: 31 of 50 names compared; share within 1e-4 0.972333 (needs 0.995).**

- **19 names were not compared.** Their Tiingo request was deferred for quota (`deferred_quota`): **M2**. [M2] holds 20 `deferred_quota` rows.
- **2,641 of the 2,690 days over 1e-4 are rounding.** Yahoo prints closes to the cent, while Tiingo keeps a sub-cent digit. Counting those days as explained, the share is 0.999496, which is above 0.995. Whether rounding counts as "explained" is for the owner to decide: **owner**.
- **49 days are not rounding.** Of these, 34 are in the panel window (26 level_gap, 8 event_differs) and 15 are before the Nasdaq listing: **review**.
  - Example: NFLX 2013-10-22, where Tiingo repeats a close. This is a known case in plan 4.4 R3, and the canonical row uses WIKI by majority.

**#18 review_queue: 382 open of 1,010 items.**

- **All items by rule (plan §6):** R4 263, R3 237, R1b 217, R1 184, R6 68, R7 28, R1/R2 5, R1c 5, R9 3; 1,010 in all ([VS] `review_queue.by_rule`).
- **Open by rule:** R4 200, R1b 94, R3 56, R1 23, R1c 3, R9 3, R7 2, R1/R2 1 ([VS] `open_by_rule`).
- **Round 10 left 370 of these unresolved** ([R10] `merged/moves_verdicts.csv`, classification `unresolved`; by rule R4 198, R1b 93, R3 55, R1 19, R9 3, R7 2).
- **Why they stay open** (*computed for this report*: openings of the `notes` field of the 370 unresolved rows, lower-cased):
  - One source only: "single source, no halt document" 104 (38 start with these words and 66 with "unresolved: single source, no halt document"), "wiki only" 38, "tiingo only" 32, "only yahoo" / "yahoo only" 30 (23 + 7; 2 of the "only yahoo" notes are Yahoo plus the stored file).
  - Two sources and no third: all 32 notes starting "only wiki" name a second vendor ("only wiki and tiingo …" or "only wiki (x%) and yahoo (y%) …"). These are two-source days that disagree, with nothing to break the tie.
- **Kind:** mostly **gap**, since no second or third source exists; the rest are **review**.

All 1,010 queue items by rule and year of the event (plan §6 table: "counts by rule and year reported"; [RS] `review_queue.by_rule_year`):

| Year | R1 | R1b | R1c | R1/R2 | R3 | R4 | R6 | R7 | R9 | All |
|---|---|---|---|---|---|---|---|---|---|---|
| 2011 | 2 | 1 | 0 | 0 | 2 | 1 | 0 | 0 | 0 | 6 |
| 2012 | 5 | 8 | 0 | 0 | 6 | 22 | 0 | 4 | 0 | 45 |
| 2013 | 6 | 6 | 0 | 0 | 11 | 16 | 0 | 5 | 0 | 44 |
| 2014 | 10 | 11 | 0 | 0 | 9 | 21 | 25 | 5 | 0 | 81 |
| 2015 | 9 | 13 | 0 | 1 | 31 | 16 | 10 | 7 | 0 | 87 |
| 2016 | 12 | 10 | 1 | 0 | 6 | 18 | 1 | 3 | 0 | 51 |
| 2017 | 5 | 7 | 0 | 0 | 17 | 42 | 22 | 4 | 0 | 97 |
| 2018 | 5 | 12 | 1 | 0 | 55 | 31 | 2 | 0 | 0 | 106 |
| 2019 | 13 | 10 | 0 | 2 | 6 | 24 | 3 | 0 | 0 | 58 |
| 2020 | 19 | 44 | 2 | 0 | 14 | 21 | 0 | 0 | 0 | 100 |
| 2021 | 27 | 53 | 0 | 0 | 4 | 11 | 0 | 0 | 0 | 95 |
| 2022 | 18 | 26 | 0 | 1 | 5 | 16 | 4 | 0 | 0 | 70 |
| 2023 | 12 | 15 | 1 | 1 | 60 | 18 | 0 | 0 | 0 | 107 |
| 2024 | 19 | 0 | 0 | 0 | 1 | 5 | 0 | 0 | 3 | 28 |
| 2025 | 20 | 0 | 0 | 0 | 8 | 1 | 0 | 0 | 0 | 29 |
| 2026 | 2 | 1 | 0 | 0 | 2 | 0 | 1 | 0 | 0 | 6 |
| All | 184 | 217 | 5 | 5 | 237 | 263 | 68 | 28 | 3 | 1,010 |

R2 appears only jointly as R1/R2, and R5 and R8 have no item in the scoped queue. Before scoping (all listed days), the reconcile step found 54,489 entries ([RS] `review_queue.by_rule_all`).

**#20 split_agreement: agree 0.664918 (needs 0.98).**

- **Structural reason.** Only 265 of the 1,716 vendor events have two or more vendor values. A single-vendor event cannot "agree". Among the events with two or more values, the agree share is 0.762264: **gap**.
- **Still open: review.**
  - 9 disagreements are unreviewed (for example AGEN 2023-04-26, ENSG 2019-10-01, VIVK 2025-09-05).
  - 8 distributions or spin-offs have no SEC URL.
  - 9 special distributions above 10% have no SEC URL.
- **Verdicts not yet applied.** [RS] `review_event_changes` lists 14 items whose verdict would change S or D but is not applied to the series (6 distributions, 8 moves). They stay open: **review**.

**#22 dividends.**

- **Tiingo–Yahoo.** The ex-date match is 0.984709, which passes. 21 amounts differ by more than $0.001, but only 3 of them are in the panel window.
- **Tiingo–Nasdaq: 18 usable names, where 100 are needed.** Tiingo files exist only for the month-1 names: **M2 / owner** (the owner has to say which pair the 100-name sample belongs to).
- **Canonical–Nasdaq: 96 usable names, ex-date share 0.997702, 30 amount gaps.**
  - LBTYA, LBTYK and ZG are distribution-only names and compare nothing.
  - On 4 days, CBSH paid a stock dividend (cash and stock) with no split factor in the canonical series. [R10D] `checks_misc` says 3 of them (2017-11-29, 2022-12-01, 2023-12-01) are in no queue and need the company's declaration: **review**.
  - CBRL 2020-04-16, CBIO 2023-01-13, STRA 2017-09-01, IART 2015-07-02 and SSRM 2023-11-09/10 stay unresolved without company releases ([R10D] `checks_misc`): **review**.

**#23 terminal_coverage: 2 of 493 universe series ending early have no terminal row.**

- The two are 1102993 (series ends 2026-07-16) and 875355 (2026-08-26).
- There is 1 `unknown` row: LION 2052959, a master-file attribution. The security is the NYSE-listed New Lionsgate, and the master file gives it two Nasdaq screener rows that belong to another entity.
- Kind: **review**.

**#24 terminal_open: 187 of 950 rows are open.**

| Status | Rows | Kind |
|---|---|---|
| no_vendor_price | 82 | gap (no vendor series reaches the security's last session, and the stored files are never used for it; 1 is in [M2]) |
| needs_acquirer_price | 44 | gap / review (the stock part needs an acquirer close that no vendor series has; none in [M2]) |
| pending_price | 30 | M2 (all 30 securities are in [M2]: tier_c 27, yahoo_fallback 3; for 2 of these rows the acquirer is in [M2] too) |
| needs_review | 29 | review (1 in [M2]) |
| unknown | 2 | review |

*The [M2] matches are computed for this report* from INPUTS/`terminal_returns_2012_2026.csv` `status`, `security_id` and `acquirer_security_id`.

There are also 50 `awaiting_d5` rows, all `bankruptcy_otc`. The check counts them as closed. D5 was decided at 2026-10-03 13:16 +0800 (commit 710c01a2f), after the 12:33 build. They therefore carry no value in data version 1, and the test books −55%, with −100% as a stress test. 12 of the 50 securities are in [M2] (tier_c 9, yahoo_fallback 3), so version 2 may find a vendor price for them and change their D5 status.

**Terminal-return breakdown (plan §6).** All 950 rows by status: computed 582, no_terminal_return 131, no_vendor_price 82, awaiting_d5 50, needs_acquirer_price 44, pending_price 30, needs_review 29, unknown 2 ([VS] `terminal_open.by_status`). By terminal type: cash_merger 464, stock_merger 158, bankruptcy_otc 145, exchange_move 93, mixed 86, liquidation 3, unknown 1 ([VS] `terminal_coverage.by_type`).

**#26 universe_vendor_source: 367 of 1,034,779 universe name-days (formation week plus 4 weeks) have no vendor row; share 0.999645.**

- These days fall on 37 securities. 214 of them are on unfillable names: **gap**.
- The lowest year is 2018, at 0.996697.
- With a one-week hold, 115 days are missing (share 0.999883).

**#27 universe_proxy_margin.**

Plan rule: among unpriced names, 0 names at or above the band-median proxy in ≥ 95% of weeks, and never more than 3 in a week.

- **The rule fails.** Only 81 of 759 weeks have 0 such names, 540 weeks have more than 3, and the maximum is 18 in one week. Excluding unfillable names, the maximum is 4: **gap** (unfillable names, see §4).
- **Unknown size.** 8,627 unpriced name-weeks over 398 securities have no market cap, no float and no step-6 dollar volume. By year: 2018 456, 2019 438, 2020 697, 2021 1,322, 2022 1,895, 2023 2,676, 2024 931, other years ≤ 81.
  - Tiingo would serve most of them: [US] `month2_leads.unknown_not_listed_now` lists 387 securities and 8,524 name-weeks.
  - [M2] has 293 `unknown_size_delisted` rows covering 6,587 unknown name-weeks: **M2**.
- **Dollar volume above the cut.** 159 name-weeks have a step-6 dollar volume at or above the rank-250 cut while the proxy is below it. They come from PARA and LAZR (unfillable), RNA (`tiingo_pending`) and SSYS (`series_gap`): **gap / M2**.

**#28 universe_capture_coverage: 95 of 95 company-list dates fail.**

- **Top-200 share by market cap.** The minimum is 0.95 (2012-01-24). The rule needs ≥ 99%, with misses allowed only from unfillable names.
- **Weekly market-cap-weighted coverage.** The minimum is 0.936785 before 2023, with 164 weeks below 97%. From 2023 the minimum is 0.981678, with 52 weeks below 99%.
- **Why.** Step 12's capture-date gaps are unfillable 159, tiingo_pending 37 and vendor_outside_canonical_set 4 ([US] `check_2_capture_dates`). The unknown-size weeks also fail this check by rule.
- **Kind:** **gap** (unfillable) plus **M2** (pending and unknown-size).
- After 2020 there is no market-cap source, so from 2021 the weighting uses float.

**#29 universe_nasdaq100: 21 of 957 member-years incomplete; 6 of them are unfillable.**

Round 10 classified the failing items ([R10] `gaps/universe_gaps.csv`; written 10:22 before the 12:33 rebuild):

- **10 are check logic (owner: validate).** The check counts sessions before the first trade in a member's first week (FB 2012, FOX/FOXA 2019, and the 1:1 successors AVGO 2018 and QVCA 2014). It also counts sessions after the last price in a member's last week (WCRX, CTRX, GMCR, APOL, SPLS). Both kinds should be cut.
- **7 are documented gaps.** BMC and LIFE in 2011–2012, LMCA in 2012, and the WIKI-wide gap of 2017-11-08 for CA and SHLD.
- **2 are fixable from the cache.** GOOG 2014 and LMCK 2014 have listing intervals that start late.
- **2 are unresolved.** LMCA 2014 and 2015 have WIKI single-day gaps.
- Kind: **review** (logic and intervals) plus **gap**.

**#30 universe_form25: 71 of 477 common-stock Form 25s with float ≥ $1B fail (share ok 0.851153).** Of these, 39 have no series and 32 have a series that ends elsewhere.

For 74 such rows in the pre-rebuild build, round 10 found:

- 38 documented gaps (unfillable, rank above 300, foreign or non-common);
- 25 hand-offs to code, including 13 rows whose public float is a unit error about 1,000 times too large;
- 6 unresolved: YRIV, SDC, SPWR, FMCI, TSIA and MDRX, where the last price date does not match the terminal row;
- 3 fixable from the cache;
- 2 pending for month 2.

Kind: **gap / review / M2**.

**#31 universe_fetch_margin: no_input.**

- **Why no input.** Of the 112 required tier B-B and B-C sample names, 1 (PNT, 1811764, tier B-B, state `pending_fetch`) has no fetched data in the panel.
- **The screen is too tight.** 12 tier B-B or B-C names reached rank ≤ 250, so the plan's action is to lower the screen one tier in month 2.
- **Tier C is triggered.** 2 B-C sample names reached rank ≤ 300 (1561387, 1626199). So all of tier C is to be fetched in month 2: 57 names, 38 still open.
- Kind of the no_input: **gap / review**. PNT has no row in [M2] (no `security_id` 1811764 and no ticker PNT), so version 2 will not clear the no_input unless PNT is added to the plan (§7, §9). It is the same PNT as in #8.
- Kind of the month-2 actions: **M2**. [M2] has the 38 `tier_c` rows.

**#32 universe_unfillable: the model share is above 2% in 2012, 2013 and 2018.** Under §0 these years are reported and not used to judge the strategy. See §4. Kind: **gap**.

**#33 universe_listed_gaps: 569 blocking name-weeks, 48 securities.**

| Reason | Name-weeks | Securities and kind |
|---|---|---|
| no_vendor_source | 238 | SRGA 226: the RTIX segment 2019-03..2020-07 is RTIX in [M2] (rank 38, **M2**), and the SRGA segment 2020-07..2023-06 has no source (**gap**). QVCGB 12 has no vendor primary (**gap**) |
| series_gap | 174 | XPER 172 (no free source: WIKI's TSRA ends 2017-02-22, Tiingo's XPER is the post-2022 company; **gap**); RDUS 2 (the unfillable window should be extended; **review**) |
| short_window_top250_evidence | 157 | Short 50-session window with top-250 evidence: proxy only 86, unknown 56, own dv20 rank ≤ 300 65 (overlapping). Round 10 tied most of them to the owner's young-rule question for new equity of existing issuers (48 items in [R10] `gaps/universe_gaps.csv`): **owner** |

Per-security spans are in [VS] `universe_listed_gaps.details`. The SRGA, XPER and QVCGB notes come from [R10D] `gaps.listed_gaps`.

**#34 plan_files.** Column `proxy` in `unfillable.csv` holds the value `none` in 10 rows. That value is outside the plan's set {mcap, float}: these names have neither proxy. All 23 planned files exist with every planned column. Kind: **review** (a documentation item).

---

## 4. Missing share of top-250 slots by year (plan §3.3 check 6; §0 2% rule)

Slots in a year = 250 × the weeks in that year. There are two populations.

**Table A, step 12: every missing reason except the pending ones.** Each missing name-week counts at its evidence rule's measured top-250 hit rate, as an expected count ([CBY]; [US] `check_6_three_estimates`).

| Year | Model | Calibrated | Upper | Unknown-size name-weeks | Unfillable-only (model) |
|---|---|---|---|---|---|
| 2012 | **4.875%** | **4.876%** | **5.090%** | 28 | 4.360% |
| 2013 | **2.704%** | **2.704%** | **2.719%** | 2 | 2.445% |
| 2014 | 1.224% | 1.225% | 1.348% | 16 | 0.921% |
| 2015 | 0.707% | 0.710% | 1.311% | 80 | 0.615% |
| 2016 | 0.822% | 0.823% | 0.869% | 6 | 0.668% |
| 2017 | 1.117% | 1.118% | 1.317% | 26 | 0.839% |
| 2018 | **2.474%** | **2.506%** | **5.597%** | 406 | 1.902% |
| 2019 | 1.826% | 1.860% | **5.087%** | 424 | 1.299% |
| 2020 | 1.481% | 1.531% | **6.274%** | 635 | 0.898% |
| 2021 | 1.593% | 1.610% | **11.046%** | 1,229 | 0.854% |
| 2022 | 0.966% | 0.992% | **14.597%** | 1,772 | 0.592% |
| 2023 | 0.601% | 0.638% | **19.940%** | 2,514 | 0.488% |
| 2024 | 0.415% | 0.487% | **7.384%** | 906 | 0.379% |
| 2025 | 0.225% | 0.226% | 0.440% | 28 | 0.223% |
| 2026 | 0.016% | 0.017% | 0.292% | 20 | 0.000% |

Years above 2%:

- model: 2012, 2013, 2018;
- calibrated: 2012, 2013, 2018;
- upper: 2012, 2013 and 2018 through 2024 ([US] `check_6_three_estimates.years_over_2pct`).

**Table B, validate check 6: unfillable names only.** A week counts in full when the size proxy reaches the median of ranks 200–250, or when step 6's dollar volume reaches the rank-250 cut ([VS] `universe_unfillable` `by_year`). This is the reading the pass/fail uses.

| Year | Model | Calibrated | Upper | Unknown-size name-weeks | Rate used for unknown weeks |
|---|---|---|---|---|---|
| 2012 | **5.031%** | **5.031%** | **5.100%** | 9 | 0.444% (pooled) |
| 2013 | **3.192%** | **3.192%** | **3.192%** | 0 | 0.444% (pooled) |
| 2014 | 1.015% | 1.016% | 1.054% | 5 | 0.444% (pooled) |
| 2015 | 0.672% | 0.672% | 0.672% | 0 | 0.444% (pooled) |
| 2016 | 0.785% | 0.785% | 0.785% | 0 | 0.444% (pooled) |
| 2017 | 0.915% | 0.915% | 0.915% | 0 | 0.444% (pooled) |
| 2018 | **2.669%** | **2.673%** | **2.992%** | 42 | 1.035% (stratum B) |
| 2019 | 1.885% | 1.889% | **2.300%** | 54 | 1.035% (stratum B) |
| 2020 | 0.966% | 0.973% | 1.645% | 90 | 1.035% (stratum B) |
| 2021 | 0.700% | 0.703% | **2.092%** | 181 | 0.190% (stratum A) |
| 2022 | 0.585% | 0.586% | 1.192% | 79 | 0.190% (stratum A) |
| 2023 | 0.492% | 0.493% | 1.062% | 74 | 0.190% (stratum A) |
| 2024 | 0.385% | 0.386% | 0.562% | 23 | 1.035% (stratum B) |
| 2025 | 0.231% | 0.231% | 0.231% | 0 | 0.444% (pooled) |
| 2026 | 0.000% | 0.000% | 0.014% | 1 | 0.444% (pooled) |

Years above 2%:

- model: 2012, 2013, 2018;
- calibrated: 2012, 2013, 2018;
- upper: 2012, 2013, 2018, 2019, 2021 ([VS] `universe_unfillable.three_estimates.years_over_2pct`).

The estimate is 2,542 name-weeks on the model, 2,544.85 calibrated and 3,100 upper.

Both populations put the same three years above 2% on the model. Table B is usually higher than Table A's unfillable-only column, because B counts a qualifying week in full while A weights it by the measured hit rate. It is not higher in every year: in 2021 B is 0.700% against A's 0.854%, and in 2022 B is 0.585% against A's 0.592% ([CBY] `unfillable_only` 0.00854 and 0.00592); in 2026 both are 0.000%.

The calibration sample is [R10] `gaps/unknown_size_sample.csv`:

- 40 names, drawn with seed 20261003: stratum A has 30 names (2021–2023), stratum B has 10 (2018–2020 and 2024).
- Rates: A 0.001898, B 0.01035, pooled 0.004439.
- Verdicts: 25 no, 8 implausible, 7 possible.
- Its SHA-256 is be4a3827…6996a, and it matches the value written in the code ([VS] `three_estimates.calibration_sample.matches` = true; [US] `unknown_size_calibration`).
- Caveats from [R10D] `gaps.unknown_size`:
  - Stratum B has only 10 names, and one name (BEAT, size ratio 56.8 to the cut) carries most of its expected value.
  - The 7 "possible" names (CINC, AVEO, IMGO, LYLT, LBC, THOR, BEAT) could not be settled without volume data.

If the class-capped QRTEB weeks (25 name-weeks) are left out, 2018 is 2.569% and 2019 is 1.792% on Table B's model ([VS] `universe_unfillable.step12_class_capped`).

**How to read this.** These shares are reported as known survivor bias. Under the 2026-10-02 decision (plan §0), a year above 2% on the model is not used to judge the strategy. The calibrated and upper readings are reported beside it and decide nothing.

### 4.1 The largest unfillable names

INPUTS/`unfillable.csv` has 133 rows for 131 securities. By proxy, 99 rows are `mcap`, 24 `float` and 10 `none`. Its estimate column `est_weeks_in_top250` sums to 2,353.

The largest estimates, in name-weeks: VIA 377, PARA 217, INFO 148, AMTD 124, QVCAQ 112, LIFE 111, MOLX 102, BUFF 99, BMC 90, CZR 90, PNFP 73, ARCP 68, ULTI 67, ALTE 67, LSI 67, COHR 53, LMCA 50, LUFK 41, MOLXA 41, ARBA 40.

Most rows are names whose WIKI series ends on 2018-03-27, or which have no usable WIKI file, and whose ticker Tiingo does not serve for the right entity (`sources_tried`). The per-security estimate used by validate is [VS] `universe_unfillable.estimated_by_security`.

---

## 5. Hand review (round 10)

Round 10 checked about 2,000 queue items against primary sources (SEC filings and company releases), and independent verifiers spot-checked them (commit 30c6d2a7b). The merged verdicts feed the build. Outcomes are from [R10] `merged/merge_summary.json`.

| Queue | Verdicts | Outcomes |
|---|---|---|
| moves (R1–R9 price moves) | 895 (842 in 21 batches + 53 mechanical; 2 mechanical rejected) | unresolved 370, market_move_no_adjustment 165, vendor_error 141, stored_error 81, vendor_gap_unfilled 60, halt 32, flat_genuine 16, unrecorded_event 14, market_move_second_source 11, event_confirmed 5 |
| splits | 418 (409 batch + 9 mechanical) | confirmed 411, corrected 2, not_a_split 4, reclassify_distribution 1 |
| distributions | 347 | confirmed 258, not_a_distribution 62, corrected 20, unresolved 7 |
| terminal | 174 | approve 85, correct 32, hold 28, price_gap 22, decide_type 7 |
| terminal_price | 140 | price_gap 115, correct 25 |

Data changes from the verdicts:

- moves: 141 applied, 14 not applied;
- splits and distributions: 89 rows listed for the owner, not applied by the round-10 merge ([R10] `merge_summary.data_changes`).

After the 12:33 rebuild:

- **Moves:** applied 4, not applied 8, superseded 2. **Distributions:** applied 80, not applied 6, superseded 1. **Splits:** applied 6, type only 1. In all, 14 items stay open ([RS] `review_event_changes`).
- **Split table:** 573 confirmed, 19 corrected, 51 not_a_distribution, 4 not_a_split, 6 unresolved, and 75 series changed ([RS] `split_events.hand_review`).
- **Special distributions:** 258 confirmed, 20 corrected, 62 not_a_distribution, 7 unresolved, and 80 series changed ([RS] `special_distributions.hand_review`).
- **Terminal:** approvals lifted 113 needs_review rows to computed ([R10D] `merge_not_fixed`).

### 5.1 Verifier corrections by queue

| Queue | Verdicts | Verdicts a verifier corrected | Share | Verdicts with any verifier mark |
|---|---|---|---|---|
| moves | 895 | 64 | 7.2% (7.6% of the 842 batch items) | 67 |
| splits | 418 | 2 | 0.5% | 9 |
| distributions | 347 | 11 | 3.2% | 23 |
| terminal | 174 | 8 | 4.6% | 8 |
| terminal_price | 140 | 9 | 6.4% | 9 |
| all | 1,974 | 94 | 4.8% | 116 |

How the table was made (*computed for this report*):

- A verdict counts as "corrected" when its note contains "corrected by verifier".
- "Any verifier mark" means "verifier" appears in the note or in the `reviewer` field.
- Files read: [R10] `merged/moves_verdicts.csv`, `split_verdicts.csv`, `distribution_verdicts.csv` and `terminal_verdicts.csv` (`queue` column).

The files do not record how many items each verifier checked. So the rate is corrections per verdict, which is a lower bound on the error rate among the items the verifiers actually checked.

Typical corrections:

- the EDGAR header gives the acceptance time in Eastern time, but the reviewer had used UTC, which moves the event session;
- a second local source the reviewer had missed;
- terminal hold and price-gap reclassifications.

### 5.2 Gap review

[R10] `gaps/universe_gaps.csv` classifies 212 gap items from the pre-rebuild build:

- universe_listed_gaps 117, universe_form25 74, universe_nasdaq100 21;
- by category: code_bug_fixed 109, documented_gap 47, code_bug_handoff 35, unresolved 10, fixable_cached 8, pending_month2_tiingo 3.

The current state of each check is in §3.2.

---

## 6. Known biases

1. **Survivor bias in 2012–2013 and 2018.** Large names that no free source carries: BMC, Life Technologies, Molex/MOLXA, Viacom, Liberty (LMCA), Altera, Ariba, Lufkin, Lincare and others before 2016; AMTD, INFO, CZR, ULTI, COHR and others in 2018–2020; PARA (813828.B) from 2019-12 to 2025-08: of its 283 estimated name-weeks, 210 come from a size proxy at or above the band median (2019-12-27..2023-12-29), 72 from dollar volume at or above the rank-250 cut (2024-01-05..2025-05-30) and 1 from dollar volume without a proxy (2025-08-15) ([VS] `universe_proxy_margin`, `universe_unfillable.estimated_by_security`; unfillable window 2019-11-07..2025-08-17). Tiingo cuts histories of names delisted in 2016–2017 at 2016-01-04, so the 2012–2015 period of names WIKI lacks is lost (plan §8 item 9). The size of the bias is in §4.
2. **Unknown-size weeks cluster in 2018–2024.** These are mostly names in merger or bankruptcy limbo whose last 10-K float expired ([R10D] `gaps.unknown_size`: about 30 of the 40 sampled). The model gives them nothing; the upper bound counts them all (§4).
3. **Foreign filers are excluded (D4).** The mean excluded per week rises from 222 in 2012 to 718 in 2026 ([WS]). 53 Nasdaq-100 member-years are excluded on this ground ([VS] `universe_nasdaq100`). Universe names whose foreign status is UNKNOWN stay in, and 2 are ranked (1288784, 1569650; [VS] `security_master`).
4. **Investment companies are excluded (D6).** 48 securities, 18,993 name-weeks, of which 122 former top-250 name-weeks: AABA, 2017-06 to 2019-10 ([US] `investment_companies`).
5. **SEC header SIC versus Ken French SIC.** Ken French assigns industries from CRSP/Compustat SIC each June; here the SEC header SIC is used point in time (plan 5.2). Some industry assignments will differ from the benchmark portfolios'.
6. **Every share class is kept.** The build keeps all classes ([US] `share_classes`), so classes of one company (GOOG/GOOGL, FOX/FOXA, LBTYA/K, …) can sit in the same industry. Plan §8 item 4 still lists this as an owner question.
7. **One-source days.** On 139,153 universe-week days only one source has the row (§2.2). That is 17–28% of days in 2012–2016 (WIKI only) and 12–20% in 2018–2021 (mostly Tiingo only), against about 1% from 2024. A wrong vendor row on those days cannot be caught by a vote.
8. **Yahoo rounding.** Yahoo prints closes to the cent, Tiingo to a sub-cent digit. In the V sample this explains 2,641 of 2,690 days with |Δr| > 1e-4 ([VS] `v_sample`). Most universe days from 2018 are Yahoo-primary (§2.2), so returns there carry cent rounding.
9. **Yahoo volume.** 86,597 panel rows carry `yahoo_volume_unverified` ([RS] `yahoo_volume_flag_rows`). Dollar-volume ranks on Yahoo-only days rest on Yahoo volume.
10. **The mirror leak.** Before this test, the 2012–2019 work had already shown that 21-day momentum loses money in active names ([strategy directions](strategy_directions_2026_09.md), section on candidate one, line 82). Short-term reversal is close to the mirror image of that signal, so a small amount of prior information about the test period exists. It is registered again here, as plan §6 asks.

## 7. Known residual problems

- **Names no free source has.** XPER 2017-03..2020-06; SRGA 2020-07..2023-06; QVCGB 2025-03..05 (no vendor primary); the 133 unfillable rows (§4.1). Also partial series that stop before the need: 31 candidate rows (§3.2 #8).
- **Wrong or missing requests not in the November plan.** Avidity was requested as RNAM, and RNA is not in [M2]. CALD was missed by the B-A tier ([R10D] `gaps.listed_gaps`). PNT (1811764), the B-B sample name with no data, is not in [M2] either, so #31 stays no_input in version 2 (§3.2 #8, #31). None of the three is in the November plan.
- **Unapplied verdicts.** 14 S/D verdicts are not applied to the series ([RS] `review_event_changes.open_items`). The same distribution may be counted twice, as both S and cash, in AFSI, LBAI and LBRDA/LBRDK ([R10D] `merge_not_fixed`).
- **Halts.** UCFI and NUTR keep constant-close, zero-volume rows to the window end. No document dates the end of the halt ([R10D] `merge_defects`).
- **WIKI-wide missing session 2017-11-08.** WIKI lacks that session for 981 of 981 target series, so the next row's return spans two sessions (R6, [R10] `moves/mechanical_verdicts.csv`).
- **Stored-file break of 2025-06-24.** 26 unit_break rows on that day. The stored vote is left out on every listed break security that was priced that day ([VS] `stored_comparison`).
- **XBRL float unit errors.** 13 Form 25 rows have a float about 1,000 times too large ([R10D] `gaps.listed_gaps`). [VS] `form25` `float_check_flag` marks them, along with other flags (for example `float_above_5T` 4, `above_listed_market_cap` 12).
- **Master-file attribution issues.** LION (§3.2 #23). EGLX is mapped to a non-filing CIK 1760247, while the issuer is CIK 1854233, a 40-F filer: 132 unknown name-weeks 2021–2023 ([R10D] `gaps.unknown_size`). Listing intervals that start or end on the wrong date: GOOG, LMCK, Z, WMGI, WSTC, HERO ([R10D] `merge_not_fixed`).
- **Single-vendor OTC closes.** Some bankruptcy terminal rows rest on one vendor's first OTC close, which plan 4.5 allows (CAMP, AMRS, FTD; [R10D] `merge_not_fixed`).
- **Earnings.** 2 events keep the JSON time because the header has none ([VS] `earnings_header_times`). In every year, at least 0.974 of universe company-quarters have an event (§2.3).
- **Not run.** Plan check 3.3.7 (the FINRA volume check) needs requests and was not run ([US] `check_7_finra`).
- **Hand-offs not done.** [R10D] `merge_not_fixed` also lists:
  - the MTCH 2020-07 rows;
  - the 1:1 reorganisation links for RMCF, CPHC and NWE;
  - the NYSE-transfer window rows (FCS, HST, SNH, KDP, SNI, GOV, TW, BPR, CSX, APA, ONB, STRZ).

  Its `merge_defects` entry also says the successor hand-off rewrote some linked series beyond ASRT and QVCGA, for example LBTYB moving to Tiingo. That was not re-checked for this report.

## 8. Owner decisions and open questions

### 8.1 Decided (plan §0 and §8, all before any returns were seen)

| Date | Decision |
|---|---|
| 2026-10-01 | D2: industry benchmark = Ken French 49 industries. D4: foreign filers (20-F/40-F/6-K) excluded from the universe |
| 2026-10-02 | D6: closed-end funds and BDCs leave the base from the first week the issuer is one (SEC evidence); banks stay |
| 2026-10-02 | Data version 1 is frozen once the rebuild, the checks and the hand reviews **pass**, whatever Tiingo has fetched; later Tiingo months build version 2, which is only a robustness check with the rules unchanged; if it changes the conclusion, the conclusion counts as unreliable. **Not yet met:** 17 checks fail and 382 review items are open, so version 1 is not frozen (§8.2) |
| 2026-10-02 | A year above 2% of top-250 slots is reported but not used to judge the strategy; no further sources are sought after the free-source survey |
| 2026-10-02 | Conventions: a 1:1 holding-company reorganisation continues the same security (CRSP one PERMNO); the repo stored files count as a second source when a large move is reviewed |
| 2026-10-02 | SEC contact is read from the git-ignored `.env.sec` (§8 item 7); vendor terms: keep the stored data and keep using Tiingo/Yahoo/Nasdaq, local only (§8 item 8) |
| 2026-10-03 | D5: a delisting with no terminal value found books −55% (Shumway and Warther 1999, Nasdaq performance delistings); −100% reported as a stress test |

### 8.2 Written down for the owner to confirm, or still open

- **Freezing version 1.** The §0 condition (rebuild, checks and hand reviews pass) is not met: 17 checks fail (§3.1) and 382 of 1,010 review items are open (§3.2 #18). The owner has to accept each failure (for example as a free-source gap, §3.2) or have it fixed before version 1 is frozen and the test is run.
- **2026-10-03 code conventions for the completeness checks** (plan §0):
  - the 2% rule is read on the model;
  - short_window weeks are judged by their evidence.
- **The FF49 "Other" rule** for an unmatched SIC (3990; 117 name-weeks) is not yet registered (§2.4).
- **The young rule.** Do the first 25 sessions of new equity from an issuer that was already public (that issuer's IPO, a spin-off, a new class, post-bankruptcy shares, a non-traded REIT's listing) count as young, that is, not missing? 48 items are behind it, and most of the short_window blockers in §3.2 #33 depend on it. Related questions:
  - Keep pre-transfer NYSE rows for the dv windows (12 names)?
  - Count OTC, foreign or SPAC-shell rows in the dv windows (23 names)? ([R10D] `gaps.listed_gaps`)
- **V sample.** Do rounding days count as explained (§3.2 #17)?
- **Dividend sample.** Which pair (Tiingo–Nasdaq or canonical–Nasdaq) do the 100 names belong to (§3.2 #22)?
- **Single-vendor OTC closes** behind the CAMP, AMRS and FTD terminal values (§7).
- **Plan §8 still open:**
  - item 4: multi-class issuers (the build keeps all classes);
  - item 5: dollar-volume window (20 or 50 days) and N (150, 200 or 250);
  - item 6: is 2026-07-17 the last signal date or the last return date;
  - item 8: Ken French redistribution terms.

## 9. What data version 2 (November Tiingo) will add

[M2] plans 374 rows for fetch month 2026-11, with 371 new symbols, all within the 480-symbol plan and the 500 cap:

| Group | Rows | Expected top-250 name-weeks | Unknown-size name-weeks it would size | Missing name-weeks covered |
|---|---|---|---|---|
| unknown_size_delisted | 293 | 9.6 | 6,587 | 9,371 |
| tier_c (B-C rest, triggered by the sample) | 38 | 15.5 | 432 | 4,971 |
| yahoo_fallback (Yahoo answered no rows) | 23 | 105.0 | 4 | 5,640 |
| deferred_quota (V sample and others) | 20 | 0.0 | 0 | 221 |
| all | 374 | 130.1 | 7,023 | — |

Source: *computed for this report*, sums of [M2] columns by `group`.

Expected effects:

- The V sample can be completed: 19 names (#17).
- Tiingo–Nasdaq dividend pairs gain names (#22).
- 30 `pending_price` terminal rows get their price (#24), and 12 `awaiting_d5` securities are fetched, which may change their D5 status (#24).
- The RTIX segment of SRGA gets a source (#33).
- Most unknown-size weeks get a size, which narrows the upper reading in §4 for 2018–2024.

Version 2 will not add RNA (Avidity), CALD or PNT unless they are added to the plan (§7); without PNT, #31 stays no_input. Names that no free source has stay as they are.

The plan's month-2 actions from check 5 (#31) also apply: lower the screen one tier, and fetch all of tier C.

## 10. Manifest, commit and hashes

**Build and manifest.**

- Scripts commit for this build: `ce635796b55bb0740c720f0cca066b74b0e15b0b` ([MF] and [VS] `scripts_git_commit`). All 15 scripts and their tests are listed with `uncommitted: false` ([MF] `scripts`).
- The later commit `710c01a2f` changes only `docs/reversal_2012_2026_data_plan.md`, adding the D5 line.
- Manifest generated 2026-10-03T04:32:55Z. It lists 49 files under `sha256`, with `files_missing` empty. `raw_index_sha256` is `05966262e6cd8893ea527096da83fd2aef3b98fda749a732c2e112d19c75336c`.
- SHA-256 of `INPUTS/manifest.json` itself: `fa9126459e723bc80f694092a8920893ef4c0b471e95fdb663bed94d91b49095` (*computed for this report*).
- Every one of the 49 hashes in [MF] was re-checked against the files on disk for this report, and 0 mismatched.

**Hashes of the files the test reads** (from [MF] `sha256`):

| File | SHA-256 |
|---|---|
| INPUTS/validation_summary.json | 6371741e20c6210977f101df6e84dd09ec894e26d805b3b34d5a2d3149e3706b |
| INPUTS/weekly_universe_top300.csv.gz | 5d26965b8180fc2dd4c2785936882217796e68a0bf9e64c8b75cb984485d403c |
| INPUTS/weekly_universe_summary.csv | a6ad3eeeaa03f3486fddab7abc8c1ee106ed4c827a14507b1cf4222d00e4505a |
| INPUTS/security_master.csv | befbb0427ea3e150c67f1f21e46cbb00be24985660b47daab1469525aa73283d |
| INPUTS/ticker_intervals.csv | 8054b58ecb1f977ac527f32fd173c590943b8afc9b04df14fcf70661ad35b037 |
| INPUTS/terminal_returns_2012_2026.csv | 77b1e787b460fff95763d68e12dcca7794c5e176c067d7c086c9c5714908db4b |
| INPUTS/split_events.csv | 9fde8fccb281e5d9f0e075c30a3862051864e6b136a99e2d156ad5ac7091ec37 |
| INPUTS/special_distributions.csv | 9d096e4a4884769137955d40891aa0ab9f993ee546b646feebbc19e5d8a4ce53 |
| INPUTS/reviewed_moves.csv | 07191216685e2c7a9b9fd703f51012e710dd3abf087bbaa60f185596c0d03441 |
| INPUTS/earnings_events.csv | 52e2a3ca30dbcf21ea3ce434e900e1f35f126b0ec88185da60eddf75b0e20ebc |
| INPUTS/earnings_fallback_periodic.csv | c2f397bd374069b40d32c321ce8a2391ddf53e5ac9df8c98109ed2c7fc023d06 |
| INPUTS/sic_history.csv | fdeba8ec17328058ded5b7cac1d2f43dcb96ecce1aebb3c4f315094f78b3642e |
| INPUTS/ff_industry_maps.csv | d7e1b8c8cb00576f1e3221b1402713c309f16159826c6828bd15b449ca1291b3 |
| INPUTS/unfillable.csv | f0ec7fcf04623b244a0614c3d61ac6d7cc687061e944e8e73f5ea928e160e08f |
| INPUTS/candidate_fetch_list.csv | fbc2ebf030e46f17f87984b978bc0728ac29d88890b455859ba1f1d49a0543dd |
| INPUTS/form25_nasdaq_2012_2026.csv | e53c38ffe8cfea72227c0527f4df8ab2007f4ddebd7818d9759a1e53a5147ac8 |
| INPUTS/exchange_moves.csv | 298c0f6b1a33c8f35bf0b6c3e1088167ad7308fe7b14e2fb1b6cc49415987c9a |
| INPUTS/listing_snapshots_index.csv | de579ec1ce2bbcdd8988c3bfcec7ac565585ec4e6af5862c66a6d8517c243e8f |
| INPUTS/periodic_form_history.csv | ecdcf80f7a3b047f4d4aa77634d2f984533c0757d6e7fde8ecefd0fc6faa5d3d |
| INPUTS/earnings_hand_checks.csv | 2abbae50ec3d1d0993d3fa47214476ad7aa55287f1fdb2641bfb42efb24390e1 |
| CACHE/prices/daily_panel.csv.gz | 395c25678542fb3384ec433f84cb89b911ef129dc78601dc9affa2a1029e6c38 |
| CACHE/dividends.csv | 4294c175b992bcb6f8342bf9835eae20095e3eaa1476bde4ad6ec6637282c6fd |
| CACHE/universe/weekly_listed.csv.gz | 1abdc17cd05d1c0f82585f7b3c5e244238a1378c5472821060153ff6c8248d7c |
| CACHE/universe/weekly_liquidity.csv.gz | 361b69b42d2253996012c7a36039dd111349980d4ee642fccd3fb9d01bb43652 |
| CACHE/universe/completeness_by_year.csv | c7eb284bd3a4d78657b8a04a78b2e939af515a4dc09978d5a3359b62ec57c911 |
| CACHE/universe/universe_summary.json | 0e1be9f4d83033427c23b5ca6ceb88838b98f3896d905ca32d26a9c2ff315044 |
| CACHE/factors/ind49_daily.csv.gz | 3bfe96818a2652df1ae519081e5c1d0547df95d0d900c13806b25b682505b6a0 |
| CACHE/factors/ff5_2x3_daily.csv | b07b6053eb507cbdcc6746204cc5a1b90362226fcdecea23f846966ff48dce5d |
| CACHE/factors/mom_daily.csv | 18b73c3cb976c2eb6bc84f489587b01c8c76858f9624e56b24bafc560f2d0054 |
| CACHE/factors/st_rev_daily.csv | 305a6cf1e4f42cc41d06640c87268b927a53f98c42dec2ae01cc99cdf2d32f98 |
| CACHE/factors/qqq_joined.csv | 8092ecb678ba9f11ac8535da8943ac0fb0111ca5a30445cf8a992f35928c88ad |
| CACHE/factors/vix_daily.csv | c103880dde3c3595fb78a4f9bbd2fb73fcd3585188418b05363dafe46996bcab |

The other 17 entries (Ken French raw zips, the full FF map, and the step-12 side files) are in [MF] `sha256`.

**Hashes of files this report cites that are outside the manifest** (*computed for this report*):

| File | SHA-256 |
|---|---|
| CACHE/review/round10/gaps/unknown_size_sample.csv | be4a3827a2ad5554b102f722390866ba5510c973f9ec66e3a7d5cc73b106996a |
| CACHE/review/round10/gaps/universe_gaps.csv | b1fa5d04164dc4b1415bcea87d4b93a22c330f59547e81abf72ce7b0451fbaa9 |
| CACHE/review/round10/merged/merge_summary.json | 4bf87590559fc9c573922b92467d53589f5274da41e3804778996dcee37678cc |
| CACHE/review/round10/merged/moves_verdicts.csv | aeec772f4b12cdbdb3d4a3d0e4817237bb8d26b776eb7a7f59872b1e0dd22bc7 |
| CACHE/review/round10/merged/split_verdicts.csv | efafd2c35df9407d821cecced1481a3d2aa282cb88fa30c3f4ebd597f6744baf |
| CACHE/review/round10/merged/distribution_verdicts.csv | 2737f6c47caa3b42c171a7082772b4fbc94cc805e4bccac7d11e4f0993971c38 |
| CACHE/review/round10/merged/terminal_verdicts.csv | 6a1f73c06201a3444d7b2376d00c57d0bf71d6e404abd5058710229ba2778857 |
| CACHE/round10_defects.json | ad904abaf89f6831ff78da4b4c04c7dbfc17d51a9af02fdd9d93700eaafc59ad |
| CACHE/reconcile/summary.json | cd19c3084c3e7832ad341eaa0c40b9174f338b92eb0738c43f04b23fc57c962c |
| CACHE/prefilter/tiingo_month2_plan.csv | d156541c145f665e0120f83cb147d180619826cdd1d017b02fbed346733e31d0 |
| CACHE/quota_ledger.csv | 69cb6e99a44ccd302ec63703f7dfa81d6f0a358915773b19ce016074feac762c |
| CACHE/tiingo/fetch_status.csv | ef4f1cbdc3a8310ca290ca191d152ea4d32a4817eb8e35d143079f22fb89156d |

Nothing in INPUTS is committed yet. Committing the inputs, `validation_summary.json`, the manifest and this report needs the owner's approval (plan §1.1, step 14).
