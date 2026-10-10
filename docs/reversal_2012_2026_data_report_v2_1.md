# 周度反转检验（2012–2026）数据报告：数据版本 2.1（v2 + Alpaca 补数）

> 2026-10-10 构建。按 owner 当天的决定（[数据计划](reversal_2012_2026_data_plan.md)第 0 节，2026-10-10 条），把 Alpaca 的 SIP 日线加为补数来源。本报告只描述数据，**没有计算任何信号、策略收益、组合收益或跨股票收益汇总**。版本 1 和版本 2 的文件都没有改动。下面先给 owner 一段中文摘要，后面是英文工程正文。全部数字以 `output/research_only/reversal_2012_2026/v2_1_report/`（`summary.json`、`tables.md`）为准。

## 给 owner 的摘要

**这是什么。** 版本 2.1 = 版本 2 + 用 Alpaca 补上原本排在 11 月 Tiingo 免费额度里的股票。规则一条不改，和 v2 一样只是稳健性对照版。v2.1 放在单独的目录里，v1、v2 一个文件都没动（构建后检查过，没有文件比开始时间新）。

**补数结果（11 月计划里的 374 行）：**

| 结果 | 行数 | 说明 |
|---|---|---|
| 已补上（filled） | 322 | Alpaca 的序列通过全部"同一家公司"检查，进入数据 |
| 存疑（ambiguous） | 46 | 有一条检查没通过，不用；仍交给 11 月的 Tiingo |
| Alpaca 没有数据（no_bars） | 2 | UREE、OAS |
| 覆盖不到（not_covered） | 4 | 需要的日期全在 2016 年以前，Alpaca 免费历史从 2016-01-04 开始 |

- 另有 26 行已补上的股票，2016 年以前的那一段仍然缺，留给 Tiingo。
- 2016 年以后需要的 143,331 个股票-交易日里，补上了 123,100 个（86%）。
- 按计划估计，这些股票在前 250 名里约占 130 个周名额；补上的股票覆盖了其中约 128 个。

**2% 规则（判定用的模型读数，validate 检查 6）：** 和 v2 比几乎没动。只有 2020 年从 0.309% 降到 0.302%，超过 2% 的仍然只有 2012 年（2.63%，Alpaca 覆盖不到 2012）。原因是检查 6 只数"补不到"（unfillable）的股票，而这次补上的大多是"等 Tiingo"（pending）或"规模未知"的股票。

| 年份 | v2 | v2.1 |
|---|---|---|
| 2012 | 2.631% | 2.631%（仍超 2%） |
| 2013 | 1.908% | 1.908% |
| 2014 | 0.308% | 0.308% |
| 2015 | 0.083% | 0.083% |
| 2016 | 0.669% | 0.669% |
| 2017 | 0.908% | 0.908% |
| 2018 | 1.654% | 1.654% |
| 2019 | 0.723% | 0.723% |
| 2020 | 0.309% | 0.302% |
| 2021 | 0.015% | 0.015% |
| 2022 | 0.223% | 0.223% |
| 2023 | 0.085% | 0.085% |
| 2024 | 0.031% | 0.031% |
| 2025 | 0.231% | 0.231% |
| 2026 | 0.000% | 0.000% |

明显变好的是**"上限读数"**（把规模未知的周全当成前 250 名）。按第 12 步的口径（表 A）：
- 2023 年从 18.4% 降到 5.4%，2022 年从 13.0% 降到 4.5%，2021 年从 8.6% 降到 3.6%；
- **规模未知、缺价格的股票-周**从 7,427 个降到 2,479 个（补上了 4,948 个）。

**两家来源对得上的情况：**
- Alpaca 和已有来源同一天都有数据的 46,306 天里，46,263 天（99.9%）日收益差在 0.5% 以内。
- 有两家以上来源的股票-日增加了 16,999 个。
- "两家打架、没有多数"的日子从 1,889 增加到 2,021，新增的几乎都在新补的小盘股上。全部样本的一致率从 0.99262 变为 0.99251，前 250 名里基本不变（0.99914 → 0.99914）。

**检查（validate）：** v2 是 17 通过 / 17 未通过 / 1 缺输入；v2.1 也是 17 / 17 / 1，没有哪一项改变通过/未通过的状态。中途有一次 earnings_header_times 变成未通过：新进入前 300 名的公司还有 13 份 SEC 头文件没下载，补下载以后恢复通过。数字上变好的有：终值未结 166 → 147，规模未知的股票 369 只 → 151 只，规模证据不够的周 466 → 374。也有一项看起来变差：阻塞的股票-周从 98,111 变成 111,665。原因和 v2 一样，新补价格的股票剩下的缺失周换了标签，这些周以前就缺。

**研究结论会不会变：不会，不需要重跑。** 是否重跑的门槛在看到任何 v2.1 数字之前就写进了计划第 0 节。实际变化都远低于门槛：
- 模型读数最大只动了 0.0075 个百分点（门槛 0.10）；
- 前 250 名的名额只换了 0.09%，最多的一年 0.36%（门槛 0.5% 和 1%）；
- v2 前 250 名的股票-日里只有 13 个日收益变了，占 0.0014%，全在一只股票上（门槛 0.1%）。

**11 月的 Tiingo：** 新方案只需要 137 个代码（原来 374 行），省出约 340 个额度。新方案写在 `research_cache/reversal_2012_2026_v2_1/prefilter/tiingo_month2_plan_v2_1_proposal.csv`，原计划文件没有改。额度分给：
- 46 只存疑的股票；
- 2 只 Alpaca 没有数据的股票；
- 30 行 2016 年以前的缺口；
- 60 只抽查样本，用来核对 Alpaca 和 Tiingo 是否一致。

**一处事后修改（必须说明）：** 检查规则在抓数据之前就写好了，但有一条规则的参照日期选错了，在第一次检查之后、补数和重建之前改过一次，详见正文 §2。
- 原来要求"Alpaca 的最后一天离退市日期不超过 10 个交易日"。可是 `delist_date` 是 Form 25 的生效日，纳斯达克往往先停牌，几周以后 Form 25 才生效。停牌以后场外的成交又不在 SIP 里。
- 所以改成：离退市日期，**或者**离公司在纳斯达克名单里最后出现的日期，不超过 10 个交易日都算通过。
- 按原规则会是 221 只通过、146 只存疑；按改后的规则是 321 只通过、46 只存疑。两种结果都存在 `entity.csv` 里。

**术语：**
- **SIP（证券信息处理器）**：美国所有交易所和场外成交汇总成的"合并行情带"，收盘价和成交量是官方口径。
- **Alpaca**：一家券商的行情 API，免费账户可以拿 2016 年以来的 SIP 日线，包括已退市的股票。
- **asof**：Alpaca 的一个参数，表示"按哪一天的代码对应关系去找公司"。代码会被别的公司重新使用，所以每只股票都按它自己用这个代码的那段时间去查。
- **同一家公司检查（A1–A7）**：确认 Alpaca 返回的确实是我们要的那家公司，而不是后来用了同一代码的别家。
- **存疑（ambiguous）**：有一条检查没通过，这只股票的 Alpaca 数据一行都不用。
- **两源一致**：两家独立来源的同一天日收益差在 0.5% 以内。
- **上限读数**：把规模未知的股票-周全部当成前 250 名，是最悲观的估计。
- **不动点**：整套离线重建反复运行，直到两轮输出完全相同为止。

---

## Engineering body

Scope: data only. No signal, strategy return, portfolio return or cross-stock return aggregate was computed. The report quotes no vendor price level. The materiality test counts name-days whose own return changed; it averages nothing.

### 0. What version 2.1 is

- **Owner decision 2026-10-10** (plan §0): Alpaca SIP daily bars (`/v2/stocks/bars`, `feed=sip`) and Alpaca corporate actions (`/v1/corporate-actions`) are a fill source. The owner accepts the terms risk. Raw bodies and series stay in `research_cache/reversal_2012_2026_v2_1_alpaca/` (local, never committed). The keys are read from `.env.alpaca` inside Python; they travel only in headers and are never logged.
- **A robustness version.** v2.1 is v2 plus the Alpaca fill. Every v1/v2 rule is unchanged.
- **Copies.** `REVERSAL_DATA_VERSION=v2.1` points every step at its own copies (`scripts/reversal_data_common.py`):
  - `research_cache/reversal_2012_2026_v2_1/`, an APFS clone of the v2 cache, then rebuilt;
  - `output/research_only/reversal_2012_2026/inputs_v2_1/`, a copy of `inputs_v2/`, then rebuilt.

  The v2 rules are switched by `V2_PLUS` (v2 and v2.1) and the Alpaca parts by `V2_1`. The v2 archive fill (`reversal_data_v2_fill/`) is read only; its captures are not re-parsed.
- **v1 and v2 untouched.** `find -newer` against the first file of this build (the Alpaca `targets.csv`) returns nothing in the v1 cache, the v2 cache, the v2 fill cache, `inputs/` or `inputs_v2/`.

### 1. Targets and fetch

Code: `scripts/reversal_data_v2_1_alpaca.py` (steps `targets`, `fetch`, `confirm`, `proposal`, `status`).

- **Targets.** The 374 rows of `research_cache/reversal_2012_2026/prefilter/tiingo_month2_plan.csv` (fetch month 2026-11):
  - groups: unknown_size_delisted 293, tier_c 38, yahoo_fallback 23, deferred_quota 20;
  - 370 rows (369 securities) need dates from 2016-01-04 on; the other 4 need only earlier dates;
  - 340 rows need only dates from 2016 on.
- **Request units.** One unit per (security, ticker held in the window), 406 in all. The ticker is the one the security holds in `ticker_intervals.csv`. `asof` is the observed end of that ticker interval, so Alpaca maps this company and not today's holder of a reused ticker.

  A probe of 2026-10-10 showed why this matters:
  - ACET with the default `asof` returns today's ACET, whose history is TORC/Adicet (around $8.90 in January 2019);
  - with `asof=2018-06-01` it returns Aceto (around $0.88), followed by zero-volume filler rows after its delisting.

  CREE with `asof=2021-06-01` continues as WOLF, and WOLF's default history chains the post-reorganisation NYSE equity of 2025-09 onto the old one.
- **Window.** From 110 days before `needed_start`, but not before 2016-01-04, to 45 days after `needed_end`. For a delisted name, the window runs to at least 30 days after `delist_date`.
- **Requests.** 4 per unit: raw, split-adjusted and fully adjusted bars, plus corporate actions. That is 1,624 requests, all HTTP 200, 0 failures, under 150 a minute, logged in the fill cache's own `raw_index.csv.gz` and `quota_ledger.csv`.

### 2. Entity confirmation (rules A1–A7) and the canonical record

The rules were written into plan §0 (c) before any Alpaca series was fetched. Constants are in the script.

- **A1** Ask with the ticker in use and an `asof` inside its interval. Keep bars only on XNAS sessions inside this ticker's own listing intervals and before `delist_date`.
- **A2** Ticker clash. Drop dates inside another CIK's interval of the same ticker. If more than 10% of the dates clash, the series is ambiguous.
- **A3** Filler rows (repeat close on zero or near-zero volume, rule R5) are dropped first.
- **A4** For a security delisted inside the window:
  - (i) the last real bar ends near the delisting (see the amendment below);
  - (ii) real bars more than 5 sessions after `delist_date` make the series ambiguous, unless the master records a transfer or a successor.
- **A5** Continuity (R9), on the kept span. Any of these makes the series ambiguous: coverage of the listed sessions below 90%; a gap of more than 10 sessions; a split-adjusted close jump of more than 50% across a gap.
- **A6** Second source, returns and levels. The second sources are v2 canonical rows (Tiingo, Yahoo, WIKI, archive) and archived Yahoo captures.
  - With at least 5 overlapping return days, at least 95% must agree within 0.5% plus cent rounding (R8's share).
  - With any overlapping level day, the median |raw close ratio − 1| must be at most 2% (R9).
- **A7** Nasdaq company-list `last_sale`. With at least 3 snapshots in the span, at least 80% must lie within 2% of Alpaca's close on the as-of session or an adjacent one.
- A series with no A6 or A7 overlap is used and labelled `confirmed_dates_only`.

**Amendment to A4 (i), made after the first confirm run and before any fill or rebuild.** Rule (i) as first written asked the last real bar to lie within 10 sessions of `delist_date`. The first run, on a partial fetch, left 89 series ambiguous on that rule alone. Nearly all of them end at the last company-list snapshot in which the security is listed, and `delist_date` is the Form 25 effective date. When Nasdaq suspends trading first, the Form 25 follows weeks later, and the SIP has no OTC trades. For example, EVLO's last bar is 2023-12-11, it was last listed 2023-11-28 and absent from 2023-12-15, and its Form 25 took effect 2024-01-06.

The amended (i) also passes when the last bar lies within 10 sessions of the latest Nasdaq `ticker_intervals` end on or before `delist_date`. The identity tests (A1, A2, A5–A7) are unchanged. Plan §0 (c) records the amendment. `entity.csv` keeps both verdicts:

| Verdict (369 securities) | as first written | as amended (used) |
|---|---|---|
| accepted | 221 | 321 |
| ambiguous | 146 | 46 |
| no_bars | 2 | 2 |

Ambiguous under the amended rules (a security can fail several rules):

| Rule failed | Securities |
|---|---|
| A5 coverage below 90% (thin names with no-trade days; SPACs) | 33 |
| A5 gap over 10 sessions | 10 |
| A5 jump over 50% across a gap | 9 |
| A4 (ii) real bars after the delisting with no recorded successor (e.g. RTIX, whose holding-company successor is not linked in the master) | 3 |
| A4 (i) ends early even against the listing end | 2 |
| A6 returns (KIN: 15 of 16 days agree) | 1 |
| A6 level | 1 |

Second sources of the 321 accepted series:
- returns: 115 series, 48 of them also with `last_sale`;
- `last_sale` only: 12;
- levels only, or levels with `last_sale`: 2;
- dates only: 192, mostly delisted small caps from 2020 on, after the company-list captures end.

Over the accepted series, the second-source checks agree as follows:
- A6: 60,487 of 60,542 return days within 0.5% (99.91%);
- A7: 1,591 of 1,594 snapshots within 2%.

**Canonical record** (plan §0 (d), 4.1):
- **S** comes from the split records, checked against the change of raw / split-adjusted close (52 split rows).
- **D** comes from the cash-dividend records (394 rows with D > 0). Where there is no record, D is read from the split-adjusted / fully adjusted ratio, as a fallback.
  - Correction before the build: the first parse took the rounding noise of the adjusted closes as dividends (5,714 rows, median 0.02% of the close). The fallback now needs at least 0.2% and a ratio that holds the next session; 1 row remains.
- Spin-off records blank the day's return (2 rows).
- tr = (C·S + D)/C₋₁ − 1 across consecutive sessions only.

The accepted series hold 147,317 rows. 518 of them have |r| ≥ 40%, mostly penny-stock moves; they were not hand-reviewed, as v2's archive rows were not either.

### 3. Precedence and second source (plan §0 (e), (f))

**Precedence, proposed and used.** From 2016-01-04:
- to 2017-10-31: WIKI → Tiingo → **Alpaca** → Yahoo;
- from 2017-11-01: Tiingo → **Alpaca** → Yahoo → WIKI;
- then the v2 order (archived `table.csv`, archived pages, QuantQuote, companiesmarketcap).

**Why next to Tiingo:**
- The SIP bar is the consolidated tape's own official close and consolidated volume, as traded. Yahoo's raw close is rebuilt from split-adjusted history, and WIKI is a community file that ends in 2018.
- Keeping Tiingo first (and WIKI first before 2017-11) leaves every Tiingo-led v2 row as it was, because v1's checks (the Tiingo adjClose identity, WIKI as the pre-2017 reference) were made against them.
- The terminal step uses the same place: `VENDOR_ORDER` tiingo_step8 → tiingo → alpaca → wiki → yahoo → archive.

**How the rule is implemented.** `scripts/reversal_data_v2_1_fill.py`, called inside step 9 after the v2 fixes and before the archive fills:
- **Fills.** Sessions no source holds get Alpaca's row, whole weeks only, under the v2 splice rule. Archived captures, QuantQuote and companiesmarketcap vote on these rows.
- **Days a source already holds.** Alpaca counts as one more independent source (`n_sources` + 1, `max_src_diff`).
  - Where the primary ranks below Alpaca, Alpaca's return agrees, and S and D are the same, Alpaca's raw close and volume become the level (`v21_alpaca_primary:{old}`; the return is kept).
  - Where Alpaca disagrees with a single-source row, the day is `disagree_unresolved` (R3). Where it disagrees with an agreeing majority, it is the minority (`v21_alpaca_minority`).
- **Third votes.** Alpaca votes on v1/v2 `disagree_unresolved` days (`v21_third_vote:alpaca>{source}`).

**Independence.** Alpaca is a vendor pipeline separate from Tiingo, Yahoo (live or archived), WIKI, QuantQuote and companiesmarketcap, so the plan's R3 rule lets it count as a second source. A series is never confirmed by itself.

| Step 9 facts (fixed point) | Count |
|---|---|
| Alpaca series read / securities changed / first series | 321 / 321 / 248 |
| Alpaca fill rows added | 98,974 |
| Rows whose level moved to Alpaca (Yahoo, post-2017 WIKI or archive primary, returns agree) | 40,579 |
| Votes on days already held: agree / disagree | 46,263 / 43 |
| New `disagree_unresolved`: on single-source days / on fill rows against archive, QuantQuote or companiesmarketcap | 26 / 111 |
| Fill rows with a second source | 14,318 |
| Third votes on v1/v2 unresolved days: seen / settled | 1 / 0 |
| v2 archive fill after Alpaca: securities, rows (v2 alone: 474 securities, 150,498 rows) | 442, 136,457 |

Panel: 7,187,227 rows over 3,768 securities (v2: 7,102,294 rows over 3,644). 139,553 rows have `alpaca` as primary, by year: 2016 2,740; 2017 4,515; 2018 15,078; 2019 11,251; 2020 14,661; 2021 18,982; 2022 22,054; 2023 25,215; 2024 13,381; 2025 7,750; 2026 3,926.

### 4. Rebuild

`scripts/reversal_data_v2_1_build.py` runs prefilter → yahoo → reconcile → terminal → earnings → universe → validate offline with `REVERSAL_DATA_VERSION=v2.1`, until two passes have equal hashes of the watched inputs and cache files (the v2 build's list). The archive parse is not part of it.

The first build reached a fixed point after 3 passes. Validate then showed 13 Item 2.02 headers of newly priced companies not yet cached. As for v2, they were fetched from SEC (`REVERSAL_DATA_VERSION=v2.1 reversal_data_earnings.py --fetch-only --sec-rate 4`: 13 headers, plus the submissions pages of new CIKs). The build was then rerun from the earnings step: a fixed point after 2 passes, with validate at 17 pass / 17 fail / 1 no_input.

Logs and pass hashes are in `research_cache/reversal_2012_2026_v2_1/v2_1_build/`. Counts come from `scripts/reversal_data_v2_1_report.py` and are written to `output/research_only/reversal_2012_2026/v2_1_report/`.

### 5. What changed against v2

#### 5.1 The 374 plan rows

| Group | filled | ambiguous | no_bars | not_covered |
|---|---|---|---|---|
| unknown_size_delisted (293) | 250 | 38 | 1 | 4 |
| tier_c (38) | 36 | 2 | 0 | 0 |
| yahoo_fallback (23) | 19 | 4 | 0 | 0 |
| deferred_quota (20) | 17 | 2 | 1 | 0 |
| all (374) | 322 | 46 | 2 | 4 |

- Needed sessions from 2016-01-04: 143,331, of which the accepted series hold 123,100 (85.9%).
- Needed sessions before 2016: 23,782, in 34 rows (4 rows need nothing later). Alpaca cannot cover them.
- The plan's expected top-250 weeks: 128.09 sit in filled rows, 2.02 in ambiguous rows and 0 in the rest.
- The per-row table is `v2_1_report/plan_rows.csv`, and the per-security entity facts are `v2_1_report/entity_verdicts.csv`.

#### 5.2 Missing share of top-250 slots by year

Table B is validate check 6 (unfillable names, the 2% rule on the model). Table A is step 12 (every non-pending reason).

| Year | B model v2 | B model v2.1 | B upper v2 | B upper v2.1 | A model v2 | A model v2.1 | A upper v2 | A upper v2.1 | v2.1 over 2% |
|---|---|---|---|---|---|---|---|---|---|
| 2012 | 2.631% | 2.631% | 2.677% | 2.677% | 2.576% | 2.575% | 2.784% | 2.783% | **yes** |
| 2013 | 1.908% | 1.908% | 1.908% | 1.908% | 1.370% | 1.369% | 1.370% | 1.369% | no |
| 2014 | 0.308% | 0.308% | 0.346% | 0.346% | 0.527% | 0.527% | 0.650% | 0.650% | no |
| 2015 | 0.083% | 0.083% | 0.083% | 0.083% | 0.220% | 0.220% | 0.824% | 0.824% | no |
| 2016 | 0.669% | 0.669% | 0.669% | 0.669% | 0.675% | 0.673% | 0.721% | 0.719% | no |
| 2017 | 0.908% | 0.908% | 0.908% | 0.908% | 1.095% | 1.097% | 1.295% | 1.297% | no |
| 2018 | 1.654% | 1.654% | 1.885% | 1.885% | 1.686% | 1.577% | 3.586% | 2.223% | no |
| 2019 | 0.723% | 0.723% | 0.785% | 0.785% | 0.558% | 0.554% | 2.227% | 1.300% | no |
| 2020 | 0.309% | 0.302% | 0.762% | 0.755% | 0.744% | 0.718% | 4.374% | 2.182% | no |
| 2021 | 0.015% | 0.015% | 1.285% | 1.146% | 0.613% | 0.571% | 8.552% | 3.632% | no |
| 2022 | 0.223% | 0.223% | 0.769% | 0.769% | 0.535% | 0.505% | 13.004% | 4.505% | no |
| 2023 | 0.085% | 0.085% | 0.654% | 0.654% | 0.222% | 0.197% | 18.430% | 5.404% | no |
| 2024 | 0.031% | 0.031% | 0.208% | 0.208% | 0.063% | 0.054% | 6.540% | 2.185% | no |
| 2025 | 0.231% | 0.231% | 0.231% | 0.231% | 0.225% | 0.225% | 0.440% | 0.371% | no |
| 2026 | 0.000% | 0.000% | 0.014% | 0.014% | 0.018% | 0.018% | 0.293% | 0.293% | no |

The 2% rule (B model) is unchanged except in 2020. Check 6 counts only `unfillable.csv` names, and the filled names were pending or not candidates. The years over 2% are still 2012 only. The upper bounds of Table A fall sharply in 2018–2024 because the unknown-size weeks are now priced.

#### 5.3 Weeks closed and unknown-size weeks resolved

| Year | v2 missing name-weeks | closed in v2.1 | closed and in v2.1's top 250 | v2 unknown-size | resolved |
|---|---|---|---|---|---|
| 2016 | 35,939 | 343 | 0 | 6 | 0 |
| 2017 | 32,451 | 450 | 0 | 26 | 0 |
| 2018 | 27,050 | 1,517 | 20 | 274 | 190 |
| 2019 | 21,128 | 856 | 23 | 223 | 126 |
| 2020 | 22,591 | 1,552 | 45 | 534 | 340 |
| 2021 | 25,050 | 2,331 | 32 | 1,076 | 678 |
| 2022 | 27,949 | 3,238 | 0 | 1,736 | 1,208 |
| 2023 | 25,674 | 4,163 | 0 | 2,513 | 1,810 |
| 2024 | 18,947 | 1,770 | 0 | 864 | 583 |
| 2025 | 10,662 | 721 | 3 | 28 | 9 |
| 2026 | 2,089 | 310 | 3 | 24 | 4 |

- Unknown-size missing name-weeks fall from 7,427 to 2,479: 4,948 resolved, by a price or by size evidence.
- No week before 2016 changes. No name-week is missing in v2.1 that was priced in v2.
- The names entering the top 250 are mostly Wolfspeed as CREE (87 name-weeks, 2017-10 to 2021-10, a Yahoo-fallback row of the plan), then CIK 1639225 (32), 1032033 (17) and 921738 (15).

#### 5.4 Two-source agreement (validate `multi_source_agreement`, still failing as in v2)

| | v2 | v2.1 |
|---|---|---|
| Name-days with ≥ 2 sources | 5,159,967 | 5,176,966 |
| … agreeing within 0.5% | 5,121,860 | 5,138,198 |
| Share | 0.992615 | 0.992511 |
| `disagree_unresolved` | 1,889 | 2,021 |
| Universe name-days with ≥ 2 sources | 872,309 | 873,580 |
| Universe share / unresolved | 0.99914 / 44 | 0.999138 / 45 |

- The share falls slightly because the new two-source days are on small, low-priced names, where the archived Yahoo captures disagree more often.
- On the 46,306 days both held, Alpaca agrees with the existing sources on 99.9%.

#### 5.5 Validate, v2 against v2.1

v2 {pass 17, fail 17, no_input 1}; v2.1 {pass 17, fail 17, no_input 1}. No check changed status (`v2_1_report/validate_v2_v2_1.csv`). Before the 13 SEC headers were fetched, `earnings_header_times` had failed (41,347 of 41,358 headers present).

Numbers that moved:
- `terminal_open`: open 166 → 147; awaiting D5 45 → 43 (DRTT now computed, ELMS needs review);
- terminal statuses: computed 608 → 629, pending_price 18 → 1;
- `universe_unfillable`: unknown-size name-weeks 443 → 425;
- `universe_vendor_source`: missing 1,142 → 1,105;
- `universe_listed_gaps`: missing name-weeks 425,359 → 408,108, and pending name-weeks 9,842 → 2,771. Blocking name-weeks rise from 98,111 to 111,665. As in v2, newly priced names' remaining missing weeks are relabelled `series_gap`, which counts as blocking; they were missing before too. 30 pending name-weeks of 8 securities now have no open candidate.
- `terminal_coverage`: series without a row 16 → 19;
- `panel_integrity`: 3,768 files.
- `universe_proxy_margin`: weeks with no unpriced top-250 evidence 86 → 124; unknown-size securities 369 → 151; most unknown-size names in one week 99 → 28.

### 6. Robustness rerun of the studies: not needed

The threshold was set in plan §0 (g) before any v2.1 number was seen. The rerun was due if any one of the following held, and none did:

| Test | Threshold | v2.1 |
|---|---|---|
| Model share move in any year | > 0.10 pp | max 0.0075 pp (2020) |
| A year crossing 2% | any | none |
| Top-250 slots changed, all years | > 0.5% | 0.088% (175 of 198,687) |
| Top-250 slots changed, worst year | > 1% | 0.358% (2020) |
| v2 universe name-days whose return changed by > 1e-6 | > 0.1% | 0.0014% (13 of 928,723, one security) |

So the comparison of `docs/robustness_data_v2.md` was not rerun on v2.1, and no study verdict can flip on this data change. The filled names are small delisted stocks outside the top 250 in almost every week. The studies that buy outside the top 300 (S12 insider I2/I3, S13) could see more tradable names, but the pre-registered threshold does not cover them; the comparison stays available (`study_data_version.py` still accepts v1/v2 only, and adding v2.1 there is a one-line change if the owner wants it run).

### 7. The November Tiingo plan: a proposal

`research_cache/reversal_2012_2026_v2_1/prefilter/tiingo_month2_plan_v2_1_proposal.csv` holds the original columns plus `proposal_priority`, `proposal_reason`, `proposal_start`, `proposal_end`, `proposal_cum_symbols` and `proposal_within_480`. A copy without vendor values is in `v2_1_report/tiingo_month2_proposal.csv`. The original `tiingo_month2_plan.csv` is unchanged.

| Priority | What | Rows |
|---|---|---|
| P1 | Ambiguous under A1–A7: whole needed span | 46 |
| P2 | Alpaca has no bars (UREE, OAS): whole span | 2 |
| P3 | Before 2016-01-04: 4 rows needing only pre-2016 dates (whole span), 26 filled rows' pre-2016 part (`needed_start` to 2016-02-05, at least 20 sessions overlapping Alpaca for the R8 splice) | 30 |
| P4 | Verification samples, seed 20261010: 40 accepted on dates only, 20 accepted with a second source (whole span, to measure Alpaca against Tiingo) | 60 |
| drop | Covered by Alpaca | 236 |

- The proposal needs 137 unique symbols of the 480 budget, so about 340 symbols are free for other names.
- The v2.1 rebuild's own month-2 plan has 290 rows (v2: 363), because the filled names left it.
- A sensible use of the freed quota is the v2.1 `month2_leads` (names the completeness checks point to); that is for the owner to decide.

### 8. Tests

`tests/test_reversal_data_v2_1.py` (new, 28 tests) covers:
- the v2.1 version switch;
- every entity rule of `judge` (A2, A4 as written and as amended, A4 (ii) with and without a successor, A5 coverage, gap and jump, A6 returns and level, A7, combined failures, no_bars);
- the interval mask, filler rows, return and level overlap with cent rounding, the adjacent-session `last_sale` match, continuity across gaps and splits, and the window;
- the canonical record: split records, splits from the bars, a contradicted record, dividends from records and bars, rounding noise ignored, spin-offs, no return across a gap;
- bar parsing;
- the fill: precedence, the level swap below Alpaca only, an unresolved single-source disagreement, the minority, whole weeks and the splice rule, votes (majority and unresolved), and the third vote.

`tests/test_reversal_data_v2.py` changed only the message of the refused-version test. The suites of reconcile, terminal, universe, validate, common and study_data_version pass unchanged.

### 9. Files

Code:
- New: `scripts/reversal_data_v2_1_alpaca.py`, `scripts/reversal_data_v2_1_fill.py`, `scripts/reversal_data_v2_1_build.py`, `scripts/reversal_data_v2_1_report.py`.
- Changed: `scripts/reversal_data_common.py` (`v2.1`, `V2_PLUS`, `V2_1`, `V2_1_ALPACA`), `scripts/reversal_data_v2_fill.py` (the Alpaca hook), and `scripts/reversal_data_reconcile.py`, `scripts/reversal_data_terminal.py`, `scripts/reversal_data_universe.py`, `scripts/reversal_data_validate.py` (`V2_PLUS`; `alpaca` as a vendor source under v2.1). Behaviour under v1 and v2 is unchanged.

Docs: plan §0 (2026-10-10 entry, including the A4 amendment); this report.

Data that may be committed (no vendor price level):
- `output/research_only/reversal_2012_2026/inputs_v2_1/`. Its `special_distributions.csv` stays local, as in v1 and v2, and is added to `.gitignore`.
- `output/research_only/reversal_2012_2026/v2_1_report/`.

Local only:
- `research_cache/reversal_2012_2026_v2_1_alpaca/` (raw bodies, series, `entity.csv`, `plan_rows.csv`, the ledger, logs);
- `research_cache/reversal_2012_2026_v2_1/` (the v2.1 cache and the Tiingo proposal).

Nothing was committed or pushed.

### 10. Open items for the owner

1. **The A4 amendment.** Confirm it, or ask for the 100 names it admits to go back to Tiingo.
2. **The 192 accepted series with no second source.** Their identity rests on A1–A5 (`asof` mapping, listing dates, delisting end). The P4 sample checks 40 of them against Tiingo in November.
3. **The 518 Alpaca rows with |r| ≥ 40%.** They are not hand-reviewed; most are penny-stock moves of delisted names.
4. **The 137 new `disagree_unresolved` days** on small names.
5. **The freed Tiingo quota.**
