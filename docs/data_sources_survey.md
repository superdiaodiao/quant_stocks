# 免费数据来源调查与试点（为数据版本 2 做准备）

> 2026-10-05。owner 问："除了等下个月的免费 Tiingo，肯定还有别的地方能拿到数据，再找找。"
> 这是**数据调查**，不是策略检验：没有计算任何信号、组合收益或跨股票收益汇总，只统计覆盖天数、来源之间逐日收益是否一致。
> 冻结的数据版本 1（`output/research_only/reversal_2012_2026/inputs/` 和本地 `research_cache/reversal_2012_2026/`）**只读、没有改动**，也没有重建。试点抓到的原始数据全部放在本地 `research_cache/data_source_probe/`（不进 git），请求记录写在它自己的 `raw_index.csv.gz` / `quota_ledger.csv`，没有写进版本 1 的请求账本。
> 代码：`scripts/data_source_probe.py`（测试 `tests/test_data_source_probe.py`）；只含计数和比例的结果：`output/research_only/data_source_probe/`。

## 给 owner 的摘要

**结论一句话：** 真正能补上"退市大公司"缺口的免费来源只有一个半——**互联网档案馆（archive.org）存下来的旧版雅虎历史价格页面**，外加两个小来源（companiesmarketcap.com 的日度市值、档案馆里存着的 QuantQuote 免费标普 500 日线包）。它们能把试点名单里约一半的缺失"前 250 名名额"补回来，但大多只有**一家来源**，而且后两家的使用条款需要你来定。QuantConnect 数据最全，但它 10 月 2 日更新的条款**明确禁止**用任何方式（包括"运行时统计"）把数据带出平台，所以这次没有用，以前那次数据审计的做法也需要你看一下（见下文第 3 条）。

1. **能用的来源**（试点名单 70 只缺口股票）：
   - **档案馆存的雅虎页面**（旧的 `table.csv` 下载文件 + 2004–2015 年的 `q/hp` 历史价格页 + 2016 年以后的 `quote/…/history` 页，后者一页就有一整年的日线，还带分红和拆股记录）：70 只里有 54 只在需要的时间段内至少有一部分数据。和版本 1 已经核对过的日子比，99.65% 的日收益相差不到 0.5%。
   - **companiesmarketcap.com**：能对上 9 只（例如 BMC、CA 有完整的日度市值）。用"市值之比"当价格收益，和其他来源 97%–98% 一致；不含分红。
   - **QuantQuote 免费标普 500 日线包**（2015 年档案馆存档，1998-01 到 2013-08-09）：只覆盖当时还在标普 500 里的公司，对缺口名单有 LIFE、BMC、MOLX、LSI 4 只；与版本 1 的一致度 99.57%。只有复权价，没有原始价格。
2. **能补多少**（"名额"= 前 250 名位置 × 周）：
   - 42 只"补不齐"（unfillable）的大公司一共估计占 2,033 个名额。把三家来源合起来、只算**整周每天都有价格**的周，大约能补回 **1,004 个（49%）**。完整补齐（≥95% 的交易日）的只有 BMC、CA、VPHM 3 只；28 只能补 20%–95%；4 只（ILG、GPRO、NVLS、LMCB）一点也没有。
   - 有**两家新来源互相印证**的只有 BMC 和 MOLX（以及 LIFE、LSI 的一部分）。其他补上的日子都是"单一来源"，和版本 1 里 2012–2016 年那些只有 WIKI 一家的日子性质一样。
   - 等 Tiingo 的 6 只里 4 只能部分补上（约 151 / 597 个名额），Tiingo 11 月仍然是主力。
   - SRGA、TSRA、QVCGB 仍然没有任何来源；XPER 能补 55% 的交易日。
   - **D5 名单**（50 只破产退市、按 −55% 记的股票）：试点 10 只里有 4 只找到了退市后的场外价格（NESR 从雅虎的 NESRF；BBBY、SIVB、GTAT 从档案馆的雅虎页面）。按计划 4.5 节，有场外价格就该用场外价格，不再套 D5。SEC 文件里，50 只中 30 只有破产重整的 8-K，15 只的文件里有"老股东什么也拿不到"的说法（这是关键词筛查，需要人工确认）。
   - **1,890 个两家来源打架的日子**：试点 8 只股票的 987 个这类日子里，档案馆雅虎页面能给 255 天提供第三票。
3. **需要你定的条款问题：**
   - **QuantConnect**：条款 1.4 版（2026-10-02 更新）第 2.6 节写明：不能用日志、图表、**运行时统计**等任何方式把平台数据带出，**分拆到多次回测也不行**，用 AI 代理也算你本人，"为了核对数据"也不例外，违反可以直接封号。所以这次**没有跑任何 QC 回测**。10 月 5 日那次数据审计正是用运行时统计把 QC 的周收益带回本机的，按新条款这有风险——要不要继续保留那批结果、以后还用不用这个办法，请你决定。合法的办法只有通过 Lean CLI **付费购买**数据。
   - **companiesmarketcap.com**：条款第 5 条只允许"新闻、评论"类的编辑用途；其他用途"未经许可不得复制、分发或做衍生作品"。是否接受这个风险要你定。
   - **QuantQuote 免费包**：档案馆里没找到它的许可文字，公司现在也不再提供这个包；是否使用要你定。
   - **档案馆的雅虎页面**：档案馆本身允许研究使用，但内容还是雅虎的。你 10 月 2 日已经接受了雅虎的条款风险；请确认这个决定也覆盖"档案馆存的雅虎页面"。
4. **试过但不能用的**：stooq（要过浏览器验证，我们不绕过）、macrotrends（Cloudflare 拦截）、MarketWatch / 华尔街日报（DataDome 拦截）、Nasdaq 官网接口（没有退市股，同代码会混进新公司）、东方财富（没有退市股，代码重用，服务器断开连接后停止）、OTC Markets 图表接口（只有现在的代码）、Nasdaq Data Link 的 Sharadar / QuoteMedia（现有的免费 key 查不到数据）、Hugging Face / Kaggle 上的数据集（太小，或者要注册、要订阅）。Alpha Vantage、EODHD、FMP、Twelve Data、Polygon、marketstack 本机都没有 key，按规定没有注册，所以没测。没找到公开的、带个股价格的 CRSP 衍生文件。
5. **建议**：数据版本 2 按第 5 节的顺序加入档案馆雅虎页面（不需要新的条款决定，只需你确认第 3 条最后一项）；companiesmarketcap 和 QuantQuote 等你定了条款再加，而且只当第二票或最后的补缺；QuantConnect 不用。即使全部加上，2012、2013 年的缺失比例大概率仍在 2% 以上（缺口只能补回一半左右），这些年份仍按你 10 月 2 日的决定"只报告、不判断"。

**术语：**
- **名额（slot / name-week）**：每周前 250 名中的一个位置。缺口的大小用"估计占了多少个名额"衡量（来自版本 1 的 `missing_by_security.csv`）。
- **单一来源日**：那天只有一家来源有价格，没法互相核对。
- **第二来源规则**：两家来源的日收益相差不到 0.5%，才算"互相印证"（数据计划 R3）。
- **档案馆抓取（capture）**：archive.org 在某个时间点存下的一份网页副本。旧版雅虎历史页一份只有最近约 66 个交易日，2016 年以后一份有约一年。
- **CDX**：archive.org 的"存档目录"查询接口，用来查某个网址被存过哪些副本。
- **场外价格（OTC price）**：股票从纳斯达克退市后在场外市场的成交价。代码常加 Q（破产）或 F（外国）后缀。
- **代码重用**：退市公司的代码后来分给了别的公司（例如 LIFE 现在是另一家公司，PARA 在东方财富里是 Banzai）。按代码直接取数会混进错的公司。

---

## Engineering body

### 1. Method

**Gap list** (`output/research_only/data_source_probe/gap_list.csv`, built by `data_source_probe.py gaplist` from v1 files only):

| Category | Names | Source of the list | Need window |
|---|---|---|---|
| `unfillable` | 42 | top 42 by `est_top250_weeks` among `missing_reason = unfillable` in `CACHE/universe/missing_by_security.csv` (2,033 of the 2,151 estimated name-weeks of that reason) | `first_missing`..`last_missing` |
| `named_gap` | 4 | XPER, SRGA, TSRA, QVCGB (report §7) | same |
| `tiingo_pending` | 6 | top 6 by estimate among `tiingo_pending` (DISCB, LMCB, CREE, RNA, EVLO, BTAI) | same |
| `d5_terminal` | 10 | `awaiting_d5` rows of `terminal_returns_2012_2026.csv` (NESR, ASTI, BBBY, SIVB, GTAT, DNDN, UPL, PDLI, EXXI, SGYP) | last Nasdaq price (or delisting − 60 days) .. delisting + 30 days |
| `unresolved_days` | 8 | securities with the most `disagree_unresolved` panel days (987 of the 1,890 days) | first..last such day |
| `control` | 12 | v1 securities with long multi-source histories (ALTR, BRCM, DTV, LLTC, XLNX, CELG, ESRX, MSFT, INTC, CSCO, VIAB, MXIM) | 2012-01-01..2020-12-31 |

70 gap securities plus 12 controls. Prioritised by estimated top-250 name-weeks, as the task asked.

**Coverage** = share of the need window's XNAS sessions (v1 panel dates with ≥ 500 securities) that have a row from the source. **Complete week** = every session of the week has a row. **Agreement** = |daily return(source) − daily return(v1)| ≤ 0.5%, only on days whose previous row is also the previous session in the source (no gap-spanning returns). Adjusted-close sources are compared with v1 `tr`; price-only sources (market cap, raw closes) with v1 price return `close_raw·S/close_raw(t−1) − 1`. "Known-good" v1 days: `n_sources ≥ 2` and no `disagree`/`review`/`glitch` flag. Single-source v1 days are counted separately: agreement there is a genuine second source.

**Identity rules** (learned in the pilot, applied in `report`):
- companiesmarketcap: a delisted name accepts only a `<TICKER>.defunct.<year>` search result within a year of the delisting, or a guessed slug whose page title carries `(TICKER)` and whose series ends within a year of the delisting. The bare ticker is today's holder (LIFE → Ethos Technologies, AMTD → AMTD IDEA).
- archive.org pages: a capture that shows more than 5 sessions after the delisting is dropped whole; stray later rows are cut.

**Rate limits used:** archive.org 15 requests a minute, 2 names at a time (it refused connections at 30 a minute: 19 CDX and 11 page errors, retried later); companiesmarketcap 1 every 3 s; SEC ≤ 7 a second with the contact from `src/io/sec_contact.py` (never printed); Nasdaq 1 every 2 s; Yahoo 1 every 2 s; Eastmoney 1 a second until it dropped connections, then stopped. Requests 2026-10-05 13:03–14:14 UTC: archive.org 348 CDX + 323 pages (+ the 36.6 MB QuantQuote archive), companiesmarketcap 190 (+91 cached 404s of slug guesses), SEC 50 searches + 230 documents, Nasdaq 68, Yahoo 66 (30 of them 429s before switching to the v1 request headers), Eastmoney 37.

### 2. One row per source

Coverage counts are of the 70 gap names (`coverage_matrix.csv`; union in `union_coverage_gap_names.csv`). "Full" ≥ 95% of need sessions, "partial" 20–95%.

| Source | Gap-list coverage | History depth | Delisted support | Splits / dividends | Agreement with existing sources | Rate limit | Terms (what the page says) | Recommendation |
|---|---|---|---|---|---|---|---|---|
| **archive.org: Yahoo history pages** (`q/hp`, 2004–2015, ~66 rows a capture; `quote/X/history`, 2016+, ~1 year a capture, JSON store to 2022, HTML table after) **and old `table.csv` files** | 54 with rows in window; full 1, partial 41. Old `table.csv` captures are rare: 79 matching captures over all 82 names, few inside the windows | 2004 → today (capture dates) | Yes, while the name was listed; captures are sparse and irregular | Pages carry dividend and split rows (272 events over 26 names); `table.csv` has raw Close and Adj Close; pages have Close (split-adjusted at capture) and Adj Close | vs v1 known-good days 14,039 / 14,088 = **99.65%** (controls 99.72%); vs v1 single-source days: controls 4,892 / 4,895, gap names 1,387 / 1,448 (the misses are cent rounding at sub-$1 prices, e.g. BBBY in 2023); vs QuantQuote on gap names 868 / 871; third vote on 255 of 987 unresolved days | ~15 a minute in practice; a page answers in seconds to a minute | Internet Archive: research use allowed (checked 2026-10-02, plan §8 item 8); the content is Yahoo's, whose §2(d)(ix) bars automated collection (owner accepted Yahoo risk on 2026-10-02) | **Use in v2** (after Tiingo/Yahoo/WIKI) once the owner confirms the Yahoo decision covers archived copies |
| **companiesmarketcap.com** daily market cap | 9 matched (BMC, CA defunct pages; PARA, LAZR, INOV, VG, SIVB, PDLI, BTAI by guessed slug); full 2 (BMC 99.3%, CA 96.7%) | 1990s → delisting; daily only for some pages (BMC 4,200 rows, CA 5,498, LLTC 5,081), sparse for others (MSFT 685) | Yes (`.defunct.<year>` entries) | Market cap only: price return plus share-count changes, no dividends; a splits page | vs v1 single-source days (controls, mostly LLTC) 1,447 / 1,453 = 99.6%; vs QuantQuote on BMC 393 / 400; vs archive.org pages 260 / 267 | 1 every 3 s used; robots.txt allows these pages | ToS (2023-11-10) §5: editorial use only may reproduce; "for all other non-editorial or commercial uses, users may not reproduce, distribute, or create derivative works without explicit permission" | **Owner decision.** If accepted: second vote and last-resort fill only, flagged as price return from market cap |
| **QuantQuote free S&P 500 daily** (archive.org copy of `quantquote_daily_sp500_83986.zip`, 500 files) | 4 in window (LIFE, BMC, MOLX, LSI), all partial (data stop 2013-08-09) | 1998-01-02 → 2013-08-09 | Only names in the S&P 500 at the 2013 file date (survivor-biased itself) | Split- and dividend-adjusted closes only; no raw level, no events | vs v1 known-good 3,505 / 3,520 = **99.57%**; single-source 2,240 / 2,257 (controls); vs archive.org on gap names 868 / 871 | One 36.6 MB file | No licence text found in the archived product page; the product is no longer offered | **Owner decision.** If accepted: vote or fill for 2012–2013 returns only (cannot drive the $10 filter or dollar volume) |
| **QuantConnect** | Most gap names exist on QC (its 2026-10-05 delisting runs list LIFE, MOLX, ARBA, ALTE, CYMI, LUFK, AMLN, QSFT, GPRO, BUFF, ILG, CA, ULTI, ARRY, AMTD, PS, COHR, VG, TIVO, NESR, ASTI, …) | 1998 → today | Yes | Yes (factor files) | 99.8% weekly (docs/audit_data_v1_vs_quantconnect.md) | — | Terms v1.4 (2026-10-02) §2.6: platform data "may be used only inside the Site's hosted environment"; extraction is barred "including … custom or runtime statistics", "splitting Platform Data across multiple backtests", via AI agents, and "for … research, testing or 'verification'"; breach may end the account. Only exit: paid data via the Lean CLI | **Do not use.** No QC run was made for this survey. Owner to decide about the 2026-10-05 audit results |
| **Yahoo v8 chart** (already a v1 source) for OTC tickers of D5 names (`T+Q`, `T+F`) | 1 of 10 D5 names: NESRF (Pink), 62 rows from 2023-05-22 | — | OTC lines still served are rare; Q tickers 404 | as v1 | — | Answers 429 to a full browser User-Agent string and 200 to v1's `Mozilla/5.0` header | Owner accepted (2026-10-02) | **Use** for the D5 OTC price where it exists |
| **SEC EDGAR** (full-text search, 8-K plan documents) | 30 of the 50 D5 names have plan-related 8-Ks within a year of the delisting; 15 contain an "old equity receives nothing" statement, 1 (MRIN) a "receives something" statement (regex screen, `sec_d5_plan_evidence.csv`) | 2001+ | Yes | Deal consideration and distributions (already v1's terminal source) | — | ≤ 10 a second | Public; fair-access policy | **Use** for hand-checked terminal evidence; it cannot supply an OTC price |
| stooq.com | not tested | — | — | — | — | — | CSV now behind a JavaScript proof-of-work browser check; an earlier repo note (2026-08-08) says "intended solely for personal use" | **Not used** (we do not bypass bot checks) |
| macrotrends.net | not tested | — | — | — | — | — | Cloudflare challenge (403) on the data endpoint; robots `Content-Signal: search=yes,ai-train=no,use=reference` | **Not used** |
| MarketWatch / WSJ CSV | not tested | — | — | — | — | — | DataDome challenge (401) | **Not used** |
| Nasdaq.com API (already a v1 source) | 0 delisted names ("Symbol not exists" or 0 rows); PARA rows disagree with archived Yahoo (74 / 576), a reused-ticker mix | rolling ~10 years; answers only part of long ranges | No | price only | — | — | nasdaq.com/legal bars automated capture (owner accepted 2026-10-02) | **No gap value** |
| Eastmoney push2his | 0 delisted names (BMC, AMTD: `data: null`); PARA returns Banzai International | — | No | raw or adjusted | — | dropped connections after the first request (36 `RemoteDisconnected`), so stopped | not reviewed (no coverage) | **Not used** |
| OTC Markets chart feed (charting.edgar-online.com) | 0 (BBBYQ, SIVBQ empty; AMTD returns AMTD IDEA) | — | No (current symbols only) | — | — | — | not reviewed (no coverage) | **Not used** |
| Nasdaq Data Link with the existing free key | SHARADAR/SEP, SHARADAR/ACTIONS, QUOTEMEDIA/PRICES answer with 0 rows; EOD 403 | — | — | — | — | — | subscription needed | **Only WIKI** (already used) |
| Hugging Face / Kaggle / GitHub dumps | none usable: the public HF sets are small and recent; `paperswithbacktest/Stocks-Daily-Price` is gated behind a subscription; Kaggle needs an account | — | — | — | — | — | — | **Not used** (no sign-ups) |
| Alpha Vantage, EODHD, FMP, Twelve Data, Polygon, marketstack | not tested: the project `.env*` files hold only Tiingo, Nasdaq Data Link and the SEC contact | — | — | — | — | — | — | **Not used** (no key; no sign-ups) |
| CRSP-derived public files | none with security-level daily prices found (Ken French: portfolios; Open Source Asset Pricing: signals) | — | — | — | — | — | — | — |
| Local caches of earlier studies | `research_cache/megacap_oos2/` holds archive.org Yahoo CSVs for some 2012–2013 names (TLAB, CMVT, …) and companiesmarketcap pages; `research_cache/tiingo_delisted/` (30 names) has none of the gap list (its `ca.json` is a later CA) | — | — | — | — | — | — | Reuse the OOS2 cache in v2 (no new requests) |

### 3. How much of each gap category could be closed

From `union_coverage_gap_names.csv` (archive.org pages and CSVs, companiesmarketcap and QuantQuote together; Nasdaq and Eastmoney excluded for entity mixing).

| Category | Names | Full (≥ 95%) | Partial (20–95%) | < 20% | None | Estimated top-250 name-weeks | … covered by complete weeks | Two new sources on ≥ 50% of days |
|---|---|---|---|---|---|---|---|---|
| unfillable (survivor bias) | 42 | 3 (BMC, CA, VPHM) | 28 | 7 | 4 (ILG, GPRO, NVLS, LMCB) | 2,033 | **1,004 (49%)** | 2 (BMC, MOLX) |
| named gaps | 4 | 0 | 1 (XPER 55%) | 0 | 3 (SRGA, TSRA, QVCGB) | 4 | 2 | 0 |
| Tiingo pending (6 of the 371-name November queue) | 6 | 0 | 4 (DISCB 26%, CREE 57%, EVLO 44%, BTAI 33%) | 1 (RNA 18%) | 1 (LMCB 2013–2016) | 597 | 151 | 0 |
| unresolved two-source days | 8 names, 987 days | — | — | — | — | — | third vote on 255 days (26%) | — |
| D5 terminal (10 of 50) | 10 | OTC price after the last Nasdaq day: 4 (NESR via Yahoo NESRF; BBBY, SIVB, GTAT via archived Yahoo pages) | | | 6 | — | — | — |

Largest names (complete-week share): VIA 68% (283 of 415 weeks), PARA 40%, AMTD 39%, ARCP 48%, LIFE 76%, INFO 31%, BMC 99%, CZR 58%, MOLX 82%, LSI 80%, LUFK 90%, ULTI 70%, CA 97%, VPHM 99%, FIRE 88%, TLAB 88%.

Not closed by any free source found: LMCA/LMCB 2012–2013 (6% / 0%), ILG, GPRO, NVLS, HGSI, SRGA, TSRA, QVCGB. The 2012 and 2013 shares in the data report's Table B (5.03% and 3.19%) would fall by roughly half if the whole unfillable list behaved like the pilot, so 2012 at least stays above 2%; that is an expectation from the pilot, not a measurement.

Other gap categories:
- **The 1,890 single-source or disagreeing days:** sit mostly in 2011–2018 WIKI years. Archived Yahoo pages give a third vote on about a quarter of them in the pilot names.
- **The 371 November Tiingo names:** archived pages cover part of them; Tiingo stays the primary source for v2 (full raw history with splits and dividends).
- **The 5 errors and 8 unrecorded corporate actions from the QC audit:** SEC 8-Ks stay the source of record. Archived Yahoo pages carry dividend and split rows, a second check where captured; LMCA 2014 is barely captured (6%).

### 4. Pilot caveats

- Ticker per date matters. The gap list uses one ticker per security, so PARA was asked as PARA for 2019-12..2022-02, when the line traded as VIAC. v2 must query each ticker of `ticker_intervals.csv` for its own span. The same goes for CREE/WOLF and other renamed lines.
- Archived Yahoo closes are rounded to the cent. Below about $1 this alone can move a daily return by more than 0.5% (BBBY 2023: 30 of 63 days).
- The pages are not raw data. `quote/X/history` "Close" is split-adjusted as of the capture date. Raw levels for the $10 filter come from undoing later splits with the page's own split rows, or from `table.csv`, which keeps raw Close.
- Volume was not parsed in this pilot. The pages carry it, so dollar-volume ranks could use it, with the plan's Yahoo-volume flag.
- companiesmarketcap market cap mixes price with share-count changes: a buyback or issuance shows up as a return on the day the site updates shares.
- The SEC plan-text screen is a regex count, not a reading. Every hit needs a hand check before it is used.

### 5. Proposed v2 fill plan

Conventions stay those of the data plan (§4.1–4.5, §6): canonical record `close_raw`, `volume_raw`, S, D; total return r = (C·S + D)/C₋₁ − 1; returns only are chained across sources, never levels (R8); R1–R9 apply to every new row. Version 2 remains a robustness check with the rules unchanged (plan §0).

**Precedence for a session** (new sources only enter where v1's sources have no row):

1. v1 order unchanged: WIKI → Tiingo → Yahoo to 2017-10-31; Tiingo → Yahoo → WIKI from 2017-11-01 (Tiingo month 2 included).
2. archive.org Yahoo `table.csv` (raw Close + Adj Close).
3. archive.org Yahoo history pages: the latest capture that holds the session wins. Raw close is rebuilt from the page's split rows, D from its dividend rows.
4. QuantQuote 2012-01..2013-08-09 (only if the owner accepts its terms): returns only, no level (the $10 filter and dollar volume then come from the companies list `LastSale` or SEC cover-page prices, as in OOS2).
5. companiesmarketcap (only if the owner accepts its terms): price return only, last resort, flagged `mcap_return`.

**Second-source and identity rules:**
- A filled day is single-source unless a second, independent source agrees within 0.5% (R3). Two archived captures of the same Yahoo page are **one** source. Yahoo (live) and archived Yahoo are also one source.
- Entity: rows only inside the security's `ticker_intervals` span for that ticker. Drop a capture that shows more than 5 sessions after the delisting. A level check against the Wayback `LastSale` on capture dates within 2% (R9).
- Splice: where a filled segment touches a v1 segment, ≥ 20 overlapping sessions with r within 1e-4 on 95% of them (R8). Where there is no overlap, the segment stands alone and its first return is blank.
- Rounding: on days with close below $1, a 0.5% disagreement explained by cent rounding counts as rounding (the owner's V-sample question applies here too).
- Weeks: a formation or holding week counts as priced only if every session has a row; otherwise it stays in the missing tables.

**Terminal values (plan 4.5):**
- An OTC close within 10 sessions after the last Nasdaq session, from Yahoo (`T+Q`, `T+F`) or an archived Yahoo page, replaces D5 for that row. Single-vendor OTC closes are listed for the owner, as for CAMP, AMRS and FTD.
- SEC plan statements ("old equity receives nothing") are recorded beside D5 as evidence for the −100% stress test. They do not change the −55% rule.
- QuantConnect is not used, as a source or as a vote.

**Cost of a full v2 run:** about 2,000 names × (2 CDX queries + up to 13 pages) at 15 a minute is about 30 hours of archive.org time, spread over several days. Raw bodies stay under `research_cache/`; only counts, IDs and SEC facts go to git.

### 6. Files

- `scripts/data_source_probe.py` (steps `gaplist`, `wayback`, `cmc`, `quantquote`, `eastmoney`, `nasdaq`, `yahooq`, `sec_d5`, `report`); `tests/test_data_source_probe.py`.
- `output/research_only/data_source_probe/`: `gap_list.csv`, `coverage_matrix.csv`, `coverage_by_name_source.csv`, `agreement_by_name_source.csv`, `cross_source_agreement_gap_names.csv`, `union_coverage_gap_names.csv`, `d5_post_delisting_prices.csv` (dates and counts only), `sec_d5_plan_evidence.csv`, `summary.json`. No vendor price level is in these files.
- Local only: `research_cache/data_source_probe/` (raw bodies, parsed series, request ledger, logs).
