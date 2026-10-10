# 周度反转检验（2012–2026）的数据补全计划

> 2026-10-01 制定。用途：为[候选方向报告](strategy_directions_2026_09.md)里的首选方向补齐数据。只补数据、只做数据检查，不计算任何信号或收益；检验规则会在数据报告提交之后另行写定。下面是给执行者看的英文工程计划。


Scope: data only. Nothing in this plan computes signals, strategy or portfolio returns, or long-short spreads. Validation may use single-stock daily returns only to flag bad data points. It never averages or ranks stocks by return.

Periods: development 2012-01 to 2016-12, test 2017-01 to 2022-12, holdout 2023-01 to 2026-07-17.

Status of this document: nothing has been fetched for it. It is built from the four scout reports and a check of the repo on 2026-10-01. I could not confirm the Tiingo limits myself: the pricing page is rendered by JavaScript and `curl` showed no numbers. The figures used here come from the scouts (500 unique symbols a month, 50 requests an hour, 1,000 a day, 1 GB a month, internal use only). Anything else unconfirmed is marked **[unverified]**.

---

## 0. Decisions this plan assumes (owner to confirm; each is also in §8)

| # | Assumption used for sizing | Why it matters |
|---|---|---|
| D1 | Universe: the top 150–250 Nasdaq-listed common stocks by median raw close × raw volume (20-day and 50-day windows are both stored), with raw close ≥ $10, ranked each Friday. **Prices are fetched down to rank 300** as a buffer. | Decides how many delisted names to fetch. |
| D2 | Industry return benchmark: **Ken French 49-industry daily value-weighted portfolios** by default. An own-panel alternative stays possible. | An own "larger panel" needs about 1,000+ more names priced, which is 2–3 more months of Tiingo. |
| D3 | Prices are needed through 2026-08-31, covering the last signal on 2026-07-17 plus a hold of up to 4 weeks. | Fetch end date. The cost per symbol does not change. |
| D4 | Foreign filers (6-K, no Item 2.02): either take earnings dates from a tested free source, or exclude them from the universe (see §5.1). | About 11% of names have no earnings flag. |
| D5 | A delisting with no terminal value found books −100%, which is what `stock_returns_with_delisting_penalty` does today. | This is a long-side risk for a reversal strategy. |
| D6 | Investment companies are not common stock: closed-end funds and business development companies leave the universe base from the first week the issuer is one (SEC evidence: N-54A, N-2, N-CSR/N-CSRS, N-PX or SIC 6726). This follows the CRSP common-stock convention (share codes 10/11). Banks stay. | Most size-unknown names in 2018–2024 are small BDCs and closed-end funds. |

Owner decisions so far, all made before any returns were seen:
- 2026-10-01: D2 is Ken French 49 industries; D4 excludes foreign filers from the universe.
- 2026-10-02: D6 as written above.
- 2026-10-02, no waiting for more fills:
  - **Data version 1** is frozen once the rebuild, the checks and the hand reviews pass, whatever Tiingo has fetched by then. The test runs on it.
  - Later free Tiingo months build **data version 2**. The test rules say in advance that version 2 is only a robustness check, with the rules unchanged. If it changes the conclusion, the conclusion counts as unreliable.
  - A year whose missing share exceeds 2% of top-250 slots is reported, but it is not used to judge the strategy. The share is read on the **model** estimate of §3.3 check 6, in which a week of unknown size counts for nothing. Two other readings are reported beside it: the **calibrated** reading and the **upper** bound. Neither decides pass or fail. *(How the checks read this rule, written down 2026-10-03 for the owner to confirm.)*
  - No further sources are sought after the free-source survey of 2026-10-02.
- 2026-10-03, **data version 1 frozen** by the owner, accepting the 17 failing checks and 382 open review items as documented in `docs/reversal_2012_2026_data_report.md` (gaps no free source fills, names waiting for the November Tiingo month, single-source days). Inputs commit: the one that adds `output/research_only/reversal_2012_2026/inputs/`. `special_distributions.csv` stays local, because it carries vendor price levels (`prior_close_raw`); its sha256 is in the manifest.
- 2026-10-03, D5 decided: a delisting with no terminal value found (no OTC price, no consideration) books **−55%**, the Shumway and Warther (1999) estimate for Nasdaq performance-related delistings. −100% is reported beside it as a stress test. Decided before any returns were seen.
- 2026-10-02, two conventions (both follow CRSP practice; set before any returns were seen):
  - A 1:1 holding-company reorganisation or reincorporation (Google to Alphabet, 2015-10) continues the same security, as CRSP keeps one PERMNO. Dollar-volume windows and returns run across the successor link; no day is counted twice.
  - When a large daily move is reviewed, the repo's own stored price files count as a second source beside the vendors.
- 2026-10-03, how the completeness checks read the missing weeks. These are code conventions from the round-10 review, for the owner to confirm. They were set before any returns were seen.
  - **2% rule on the model.** §3.3 check 6 compares only the model estimate with the 2% limit. A week with no size evidence gets nothing in the model. The calibrated reading gives such a week the round-10 sample's rate. The upper bound counts it as a full top-250 week.
  - **short_window weeks.** In a short_window week the name has a canonical close, but its 50-session window holds fewer than 25 rows and no young-listing rule applies. Such a week is judged by its evidence, the same way as a name with no price. It blocks completeness when any of these could put it in the top 250:
    - step 6's dollar volume reaches the rank-250 cut;
    - a size proxy alone reaches the band median;
    - the series' own canonical dv20 rank is within 300;
    - it has no evidence at all.

    The other short_window weeks are judged small and are listed (§6).
- 2026-10-05, **data version 2 fill sources** (after the free-source survey `docs/data_sources_survey.md`; decided before any returns were seen; version 2 stays a robustness check with the rules unchanged):
  - (a) All three new fill sources are used: archive.org Wayback captures of old Yahoo history pages and `table.csv` files; companiesmarketcap.com daily market cap; the QuantQuote free S&P 500 daily pack (the archive.org copy). The owner accepts their terms risk (companiesmarketcap ToS §5; QuantQuote has no licence text; the archived pages' content is Yahoo's, covered by the 2026-10-02 Yahoo decision).
  - (b) QuantConnect: the owner accepts the risk under its terms v1.4 §2.6, which bans exporting platform data via runtime statistics. Pulling QC values back to this machine is allowed when useful (lowest precedence, or a tie-break vote). Uploading local data to QC stays off.
  - Precedence and second-source rules for v2 are those of the survey's §5: v1's order first, then the archived `table.csv`, the archived history pages (latest capture holding the session), QuantQuote, companiesmarketcap (QuantConnect last). Two sources within 0.5% confirm each other; captures of the same Yahoo page are one source, and archived Yahoo is the same source as live Yahoo; rows are looked up by the ticker in use on each date; a capture that shows more than 5 sessions after the delisting is dropped; a week counts as priced only when every session has a row.
- 2026-10-10, **data version 2.1 = version 2 + an Alpaca fill** (owner decision; made before any returns were seen; v2.1 is a robustness check like v2, with every rule of v1 and v2 unchanged):
  - (a) **Source.** Alpaca market data, SIP feed (`GET https://data.alpaca.markets/v2/stocks/bars`, `feed=sip`, `timeframe=1Day`; the free plan serves history from 2016-01-04, delisted symbols included), plus its corporate actions (`/v1/corporate-actions`). The owner accepts the terms risk. Raw bodies and parsed series stay local under `research_cache/reversal_2012_2026_v2_1_alpaca/` and are never committed. The keys stay in `.env.alpaca` and are never printed or logged.
  - (b) **Targets.** Every row of the month-2 Tiingo plan (`research_cache/reversal_2012_2026/prefilter/tiingo_month2_plan.csv`, 374 rows, fetch month 2026-11) whose needed span reaches 2016-01-04 or later. Window: from 110 calendar days before `needed_start` (the dv50 warm-up) but not before 2016-01-04, to 45 days after `needed_end`. A delisted name is fetched to at least 30 days after its delisting, so the end of its series can be checked. Requests: raw, split-adjusted and fully adjusted daily bars, and the corporate actions; at most 150 requests a minute (the free limit is about 200).
  - (c) **Entity confirmation.** These rules were written before any Alpaca series was fetched or compared. A series is used only if every rule passes; otherwise it is `ambiguous`, enters nothing, and its weeks stay with the Tiingo plan.
    - **A1 asked as the right company.** The symbol is the ticker the security holds in `ticker_intervals.csv`. `asof` is a date inside that interval (the interval's observed end, at most today). Alpaca maps an entity across ticker changes as of `asof`, so the default `asof` (today) would read today's holder of a reused ticker; the check of 2026-10-10 showed this for ACET. Bars are kept only on XNAS sessions inside this security's own listing (the v2 `_listing_required`: the interval spans for this ticker, before `delist_date`).
    - **A2 no ticker clash (CIK).** A kept date must not fall inside an interval in which another CIK holds the same ticker. If more than 10% of the kept dates clash, the series is `ambiguous`; otherwise the clashing dates are dropped.
    - **A3 filler rows.** Bars that repeat the previous close on zero volume (rule R5, the terminal step's `filler_flags`) are not trades. They are dropped before A4 and A5.
    - **A4 delisting.** This applies to a security whose `delist_date` falls inside the window. (i) Its last real bar on or before the delisting must lie within 10 XNAS sessions of the last session before `delist_date`. (ii) Real bars more than 5 sessions after `delist_date` make the series `ambiguous`, unless the master records a transfer or a successor. Alpaca's mapping can chain a reorganised equity onto the old one, as WOLF 2025-09 shows.
      - **Amended 2026-10-10, after the first confirm run and before any fill or rebuild.** For (i), the last real bar may also lie within 10 sessions of the end of the security's own Nasdaq listing in the company lists, which is the latest `ticker_intervals` end on or before `delist_date`.
      - **Why:** the first confirm run (on a partial fetch) left 89 series ambiguous under (i), and almost all of them end at the last listed snapshot. `delist_date` is the Form 25 effective date. When Nasdaq suspends trading first, that date comes weeks after the last trade, and the SIP carries no OTC trades after the suspension. For example, EVLO's last bar is 2023-12-11, it was last listed 2023-11-28 and absent from 2023-12-15, and its Form 25 took effect 2024-01-06. So the reference date in (i) was wrong; the amendment does not loosen the identity test.
      - Both verdicts are kept in `entity.csv` (`verdict` and `verdict_as_first_written`), and the report gives both counts.
    - **A5 continuity (R9).** Inside the kept listing span, a gap of more than 10 sessions makes the series `ambiguous`, and so does a raw close jump of more than 50% across a gap of more than 1 session. Alpaca must also hold at least 90% of the listed sessions in the kept span.
    - **A6 second source, returns.** Where the security has v2 canonical rows (Tiingo, Yahoo, WIKI or archive) or archived Yahoo captures with returns on the same consecutive sessions, there must be at least 5 such days. On at least 95% of them, Alpaca's total return must agree within 0.5%, plus cent rounding below $1 (0.005/C_t + 0.005/C_{t−1}). This is the 95% share of rule R8. The median ratio of raw closes must also lie within 2% of 1 (the R9 level check). With 1–4 overlapping days, only the level check applies.
    - **A7 second source, levels.** Where at least 3 Nasdaq company-list snapshots (`prefilter/lists.csv.gz`, `last_sale`) fall in the kept span, Alpaca's raw close on the snapshot's `as_of_session` or on an adjacent session must lie within 2% of `last_sale` on at least 80% of them (the R9 Wayback `LastSale` check).
    - A series that passes with no overlap for A6 or A7 is used, and is labelled `confirmed_dates_only`.
  - (d) **Canonical record.** S comes from Alpaca's split records, cross-checked by the ratio of raw to split-adjusted closes. A ratio change with no record is used when it fits `_split_ratio`, and the row is flagged. D comes from the cash-dividend records, with the ratio of split-adjusted to fully adjusted closes as a fallback. A spin-off record leaves the row's return blank (`v21_spinoff_unvalued`). `tr` = (C·S + D)/C₋₁ − 1 on Alpaca's own consecutive rows. Rows need a raw volume.
  - (e) **Precedence.** From 2016-01-04: to 2017-10-31, WIKI → Tiingo → **Alpaca** → Yahoo; from 2017-11-01, Tiingo → **Alpaca** → Yahoo → WIKI; then archived `table.csv`, archived pages, QuantQuote, companiesmarketcap, as in v2. Why next to Tiingo:
    - The SIP is the consolidated tape. Its daily bar has the official raw close and the consolidated volume as traded. Yahoo's raw close, by contrast, is rebuilt from split-adjusted history, and WIKI is a community file that ends in 2018.
    - Tiingo stays first from 2017-11, and WIKI stays first before then. v1's rows were checked against them (the Tiingo adjClose identity, the WIKI reference role), and keeping them first leaves every Tiingo row of v2 unchanged.

    In practice:
    - Alpaca fills sessions no source holds, with whole weeks only and the v2 splice rule (`v21_splice_blank`).
    - Where a lower-ranked source (Yahoo; WIKI from 2017-11-01; archive) holds a row and Alpaca's return agrees within 0.5%, with the same S and D, Alpaca's raw close and volume become the row's level (`v21_alpaca_primary:{old}`). The return is kept.
    - Where Alpaca disagrees, it is a vote and the v1 R3 rule decides.
  - (f) **Second source.** Alpaca is an independent source for the two-source checks (R3, `n_sources`, `max_src_diff`). It is a different vendor pipeline from Tiingo, Yahoo (live or archived), WIKI, QuantQuote and companiesmarketcap. It votes on v1/v2 `disagree_unresolved` days as v2's third votes do (`v21_third_vote:alpaca>{source}`). The entity checks above (A6) use the same comparisons, but a series is never confirmed by itself.
  - (g) **When the study robustness comparison is rerun on v2.1.** This threshold was set before any v2.1 number was seen. The comparison is rerun if any one of these holds:
    - the model missing share of validate check 6 moves by more than 0.10 percentage points in any year 2012–2026;
    - a year crosses the 2% line;
    - the weekly top-250 sets differ from v2's in more than 0.5% of all top-250 slots, or in more than 1% of any one year's slots;
    - more than 0.1% of v2's universe name-days change their daily return by more than 1e-6.

    Otherwise the report says the rerun is not needed.

---

## 1. Target outputs

### 1.1 Committed inputs: `output/research_only/reversal_2012_2026/inputs/`

These are small files, and SEC-public or fact-level only. They contain no vendor price levels. Commits happen only with the owner's approval.

| File | Columns | Approx. size |
|---|---|---|
| `manifest.json` | `generated_utc`, `scripts_git_commit`, `sources{name:{endpoint_template (no key), fetched_from, fetched_to, requests, license_note}}`, `sha256{relative_path: hash}` for every file here and every file under `research_cache/reversal_2012_2026/` that the test reads, `raw_index_sha256` | — |
| `listing_snapshots_index.csv` | `snapshot_date, source (repo_symdir / wayback_symdir / wayback_companylist / commoncrawl_symdir), capture_timestamp, original_url, rows, common_rows, has_market_cap, sha256` | ~450 rows |
| `security_master.csv` | `security_id, cik, first_ticker, name, share_class, first_listed, last_listed, delist_date, delist_form25_accession, foreign_filer (Y/N), multi_class_group, price_sources, identity_notes` | ~3–4k rows |
| `ticker_intervals.csv` | `security_id, ticker, start, end, exchange, source, source_url` | ~5k rows |
| `form25_nasdaq_2012_2026.csv` | `filing_date, effective_date, accession, subject_cik, subject_name, filer_cik, class_of_security, classification (common_delisting / other_class / reorg / transfer), public_float_usd, float_check_flag` | ~4.4k rows |
| `exchange_moves.csv` | `security_id, ticker, date, from_exchange, to_exchange, source_url` (merges `holdout_2011_2019/inputs/nasdaq_listing_overrides.csv`) | ~100–200 rows |
| `candidate_fetch_list.csv` | `security_id, ticker_for_source, needed_start, needed_end, reason (A_delisted_2012_19 / B_delisted_2020_24 / C_late_start / verify_sample / tierC_sample), prefilter_metric, prefilter_value, planned_source, tiingo_range_match, status, fetch_month` | ~1.5k rows |
| `split_events.csv` | `security_id, ticker, ex_date, split_factor (new shares per old share), event_type (split / reverse_split / spinoff / distribution / unit_break), tiingo, yahoo, wiki, nasdaq (value or blank), agree (Y/N), sec_url, verified_at, notes` | ~600 rows |
| `special_distributions.csv` | `security_id, ex_date, cash, prior_close_raw, pct_of_prior, classification, sec_url` (only rows with `divCash` > 10% of prior close or an odd `splitFactor`) | ~50–100 rows |
| `reviewed_moves.csv` | Same columns as `stocks_list_dir/nasdaq/reviewed_market_moves.csv` (`ticker, event_date, classification, source_url, verified_at, notes`), plus `security_id, sources_agreeing` | ~300–800 rows |
| `terminal_returns_2012_2026.csv` | Existing `terminal_returns.csv` columns (`ticker, last_price_date, terminal_return, consideration_per_share, source_url, verified_at`), plus `security_id, delist_date, terminal_type (cash_merger / stock_merger / mixed / liquidation / exchange_move / bankruptcy_otc / unknown), consideration_cash, consideration_shares, acquirer_security_id` | ~400–600 rows |
| `earnings_events.csv` | `cik, security_id, accession, form, items, acceptance_json_raw, acceptance_header_et, tz_resolution (header / json_utc / json_et_rule), filing_date, d0_session, first_in_fiscal_quarter, source` | ~15–18k rows, ~2.5 MB |
| `earnings_fallback_periodic.csv` | The same fields for 10-Q/10-K acceptances, for companies without Item 2.02 filings | ~3k rows |
| `sic_history.csv` | `cik, observed_date, sic, source_accession` | ~15k rows |
| `ff_industry_maps.csv` | `scheme (FF49 / FF17 / FF12), industry_id, short_name, sic_lo, sic_hi` (from Siccodes49/17/12) | ~1k rows |
| `weekly_universe_summary.csv` | `week_end, n_listed_common, n_with_vendor_prices, n_price_ge_10, cutoff_rank250_dv_bucket, n_unresolved_candidates, mcap_weighted_coverage, snapshot_age_days` | ~760 rows |
| `weekly_universe_top300.csv.gz` | `week_end, security_id, ticker, dv50_rank, dv20_rank, price_ge_10` (ranks only, no vendor values) | ~1.5 MB gz |
| `unfillable.csv` | `security_id, ticker, needed_start, needed_end, sources_tried, est_weeks_in_top250, proxy (mcap / float)` | ~20–60 rows |
| `validation_summary.json` | Output of every check in §6 | — |

### 1.2 Local-only files: `research_cache/reversal_2012_2026/`

Create this as a real directory in the main checkout, `/Users/bytedance/code/quant_stocks/research_cache/reversal_2012_2026`. Symlink it into the worktree, as is done for `sue_lt_2020_2026` today. It is already ignored through `.git/info/exclude` (the `research_cache` line).

```
raw/tiingo/{TICKER}__{fetched_utc}.json.gz          # {meta, prices, request:{path, params(no key)}}
raw/tiingo/supported_tickers_{date}.zip
raw/yahoo/{SYMBOL}__{fetched_utc}.json.gz           # v8 chart, events=div,splits
raw/wiki/WIKI_PRICES_{fetched_utc}.zip (or pages/NNNN.csv.gz)
raw/nasdaq/dividends/{SYMBOL}.json.gz               # optional cross-check
raw/wayback/{capture_ts}__{slug}.txt|csv
raw/sec/efts/25-NSE_{start}_{end}_{from}.json
raw/sec/frames/{concept}_{period}.json
raw/sec/submissions/CIK##########.json.gz (+ -submissions-NNN.json.gz pages)
raw/sec/headers/{cik}/{accession}-index-headers.html.gz
raw/sec/docs/{cik}/{accession}/{document}           # 25-NSE XML, merger 8-Ks
raw/kf/*.zip   raw/vix/VIX_History.csv
raw_index.csv.gz          # path, url_redacted, fetched_utc, http_status, bytes, sha256
quota_ledger.csv          # source, month, symbol, request_n, status, fetched_utc
listings/symdir/, listings/companylist/   # normalised snapshots (import_nasdaq_trader_files output)
prices/{security_id}.csv  # date, close_raw, volume_raw, split_factor, div_cash, tr, src_primary, n_sources, max_src_diff, flags
prices/daily_panel.csv.gz # long format of the above (~4M rows)
dividends.csv             # security_id, ex_date, cash_as_paid, sources
universe/weekly_listed.csv.gz, universe/weekly_liquidity.csv.gz  # with dv values
```

The repo has no `pyarrow`, so everything is CSV or CSV.gz.

---

## 2. Steps in order

Shared rules for every script:
- Each script checks the cache before any request and writes atomically (a temp file, then rename). Re-running after a stop fetches only what is missing.
- Each request appends a row to `quota_ledger.csv` and `raw_index.csv.gz`.
- Keys are read inside Python from `/Users/bytedance/code/quant_stocks/.env.tiingo` and `.env.nasdaqdatalink`. Tiingo gets its key in the `Authorization: Token …` header, so it never appears in a URL. Nasdaq Data Link needs `api_key` as a query parameter, so its URLs are redacted before logging.
- The SEC User-Agent is read from a new `.env.sec` (`SEC_USER_AGENT=…`), which the owner supplies. The existing `SEC_HEADERS` in `src/io/financial_update.py` uses `data@example.com`, and SEC may block a placeholder.

New code layout (none of this exists yet):

| Script | Main functions | Reuses |
|---|---|---|
| `scripts/reversal_data_common.py` | `read_env_key(path, name)`, `cached_get(url, cache_path, headers, limiter, redact)`, `SlidingWindowLimiter(per_hour, per_day, per_second)`, `QuotaLedger.append/used(source, month)`, `sha256_file`, `update_manifest` | `_canonical_json_bytes` and `_payload_sha256` pattern (`scripts/sec_submission_triage.py`); `_wait_for_sec_request_slot` (`src/io/fundamentals_update.py`, 8 requests/second) |
| `scripts/reversal_data_listings.py` | `wayback_cdx(url, from, to)`, `fetch_wayback_raw(ts, url)` (uses the `id_` raw form), `parse_company_list(text)`, `build_snapshot_index()` | `import_nasdaq_trader_files(paths, snapshot_dir=research_cache/…/listings/symdir)`, which accepts http URLs, dates each file by its "File Creation Time" footer and reads Common Crawl WARC ranges; `investable_common_equities` (`src/io/security_universe.py`) |
| `scripts/reversal_data_form25.py` | `efts_search(form, start, end, from_)`, `filter_nasdaq_filer(hits, cik=1354457)`, `fetch_form25_doc`, `classify_form25`, `fetch_float_frames(concept, periods)`, `check_float_units` | Compare its 2020–2026 output with `output/research_only/sue_lt_2020_2026/inputs/sec_form25_nasdaq_2020_2026.csv` |
| `scripts/reversal_data_security_master.py` | `collect_tickers()`, `resolve_ciks()`, `build_intervals()`, `detect_ticker_reuse()` | `load_ticker_cik_registry`, `load_cached_sec_ticker_maps`, `load_historical_ticker_ciks`, `fetch_sec_submissions`, `_write_cached_submission`, `_read_cached_submission` (sec_submission_triage); `resolve_historical_ticker_ciks`, `historical_ticker_cik_chains` (fundamentals_update); `issuer_map` (research_sue_lt_2020_2026); `security_identity.csv`, `nasdaq_symbol_history*.csv` |
| `scripts/reversal_data_prices.py` | `fetch_tiingo_daily(ticker, start, end)`, `fetch_yahoo_chart(symbol)`, `fetch_wiki_bulk()` / `fetch_wiki_pages(tickers)`, `tiingo_range_match(ticker, need_start, need_end, supported)` | `_yahoo_url`, `_read_json`, `fetch_yahoo_actions`, `fetch_nasdaq_cash_distributions` (`src/research/corporate_action_validation.py`); `fetch_history` (`src/io/nasdaq_update.py`) as a fallback only |
| `scripts/reversal_data_prefilter.py` | `wiki_dollar_volume_ranks()`, `mcap_ranks_from_companylists()`, `float_tiers()`, `build_candidate_list()`, `tier_c_sample(seed)` | `load_ohlc_panel` (`src/research/panel_data.py`) for the stored files, converted to raw close × raw volume |
| `scripts/reversal_data_reconcile.py` | `to_canonical(source_frame, source)`, `total_return(c, s, d)`, `compare_sources`, `flag_moves`, `build_split_table`, `splice_segments` | `_split_ratio`, `reconcile_provider_history` (nasdaq_update); `detect_common_split_events`, `load_confirmed_price_adjustments` (`src/research/data_quality.py`); `load_reviewed_market_moves` (corporate_action_validation) |
| `scripts/reversal_data_terminal.py` | `terminal_candidates()`, `fetch_merger_docs()`, `terminal_value(row)` | `terminal_map`, `complete_terminal` (research_sue_lt_2020_2026); `terminal_returns` (research_holdout_2011_2019); the existing `terminal_returns*.csv` files |
| `scripts/reversal_data_earnings.py` | `fetch_submission_pages(cik)` (pages whose `filingTo` ≥ 2012-01-01), `extract_item202`, `fetch_index_header(cik, acc)`, `parse_header(acceptance, sic)`, `d0_session(ts_et)`, `sic_history()` | `fetch_sec_submissions`; `nasdaq_calendar_for_year` (`src/research/shadow_evaluation.py`, XNAS through `exchange_calendars`) |
| `scripts/reversal_data_factors.py` | `fetch_kf(zipname)`, `parse_kf_daily(csv)` (preamble, `-99.99`/`-999`, two blocks in the 49-industry file), `parse_siccodes(txt)`, `qqq_total_return_join()` | `qqq_returns` (research_holdout_2011_2019) reads `research_cache/holdout_2011_2019/qqq_tiingo_2010_2020.csv` |
| `scripts/reversal_data_universe.py` | `weekly_listed(week_end)`, `weekly_liquidity()`, `completeness_checks()` | The logic of `build_universe` (research_sue_lt_2020_2026) for staleness, extended back to 2011 |
| `scripts/reversal_data_validate.py` | Each check in §6 as a function returning a dict, then `write_validation_summary` | Must not import any signal or backtest module; this is a code-review item |

### Step table

| # | Step | Source | Requests | Rate limit used | Runtime |
|---|---|---|---|---|---|
| 0 | Set up the cache directory and symlink, `.env.sec`, the quota ledger, and record Tiingo's current-month usage | — | 0 | — | 0.5 h |
| 1 | Ken French files: 5-factor, momentum and short-term reversal daily; 49-industry daily; Siccodes49/17/12. Also VIX history and the QQQ total-return join | mba.tuck.dartmouth.edu, CBOE CDN [unverified URL: `cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv`], local files | 7 + 1 | ≤ 1/s | 10 min |
| 2 | Listing snapshots, 2011–2019: CDX for `nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt`, `ftp.nasdaqtrader.com/SymbolDirectory/nasdaqlisted.txt` and the nasdaq.com company-list URL. Fetch about 14 symbol-file and about 22 company-list captures. Optionally search the Common Crawl index for more symbol-file dates. | Wayback (and Common Crawl) | ~5 CDX + ~40 captures (+ ~10 Common Crawl) | 1 per 3 s (limits [unverified]) | 15 min |
| 3 | Form 25-NSE list, 2012-01 to 2026-09: EFTS in half-year slices of 100 hits per page, keeping Nasdaq as filer (CIK 1354457). Then 25-NSE primary documents for candidate companies only. Public float from XBRL frames `dei/EntityPublicFloat` (CY2011Q1I–CY2026Q2I), plus `EntityCommonStockSharesOutstanding` for the unit check. | efts.sec.gov, data.sec.gov | ~45 + ~600 + ~62 + ~62 | 7/s | 5 min |
| 4 | Security master: submissions JSON for every CIK that appears in the snapshots, the Form 25 list or the stored files within the top-400 screen (about 1,300 CIKs); use `formerNames` and `tickers` | data.sec.gov | ~1,300 main files (older pages come in step 10) | 7/s | 4 min |
| 5 | WIKI Prices, whole table: `datatables/WIKI/PRICES?qopts.export=true`. The export request itself is untested, and the file size is [unverified], maybe several hundred MB zipped. Fallback: paged calls with `ticker=`, `date.gte=2011-06-01` and `qopts.cursor_id`, 10,000 rows per page, about 250 pages. | Nasdaq Data Link (free WIKI, ends 2018-03-27) | 1–2 (fallback ~250) | Free-key limits [unverified] | 30–60 min download |
| 6 | Pre-filter and candidate list (§3.2), run offline; also compare ticker date ranges against `supported_tickers.zip` | Local files, plus 1 Tiingo zip (no key) | 1 | — | 1 h |
| 7 | Yahoo chart for every active or still-listed candidate and every current top-300 name (about 800–950 symbols) | query1.finance.yahoo.com v8 (unofficial) | ~900 | 1 per 2 s; stop on the first 429 and resume later | 30–45 min |
| 7b | Nasdaq dividends endpoint for a 100-name cross-check sample | api.nasdaq.com | 100 | 1/s | 2 min |
| 8 | Tiingo priority list, October budget (§7) | api.tiingo.com daily prices, `startDate=2011-06-01&endDate=2026-08-31`, one request per symbol, no meta call | ≤ ~470 | 45/hour, 900/day (kept under 50 and 1,000) | ~10.5 h of wall time, unattended |
| 9 | Reconcile: canonical series, split table, dividend table, suspicious-move queue | Local files | 0 | — | Code: 0.5 day. Review: 1–3 days by hand |
| 10 | Earnings and SIC: older submissions pages for CIKs in the universe; header files for **every** Item 2.02 8-K of universe companies from 2011-10 on, plus one 10-K/10-Q header per company-year where there is no 8-K | data.sec.gov, www.sec.gov/Archives | ~700 pages + ~13–16k headers | 7/s | 35–45 min |
| 11 | Terminal values: merger 8-Ks (Items 2.01, 3.01, 5.01) or proxy documents for each series in the universe that ends before 2026-08-31 | SEC | ~600–900 | 7/s | Fetch 3 min; review 2–3 days |
| 12 | Universe build and completeness checks (§3.3); produce the list for month 2 | Local files | 0 | — | 0.5 day |
| 13 | Tiingo month 2 (from 2026-11-01): tier B/C remainder, names found by completeness checks, retries | Tiingo | ≤ 480 | As in step 8 | ~11 h |
| 14 | Validation, manifest and data report (§6); commit with owner approval | — | 0 | — | 1 day |

Resuming after a quota stop:
- Tiingo stops on HTTP 429, on any error body that mentions a limit, or when `QuotaLedger.used('tiingo', month)` reaches the cap.
- `candidate_fetch_list.csv.status` records `done`, `deferred_quota`, `no_data` or `wrong_entity`.
- The next run, or the next month, picks up `deferred_quota` rows in priority order.
- SEC, Yahoo and Wayback use the same cache-first logic, so nothing is re-downloaded.

---

## 3. Universe construction

### 3.1 Weekly listed set

**Week end.** The last XNAS session of each calendar week, from `nasdaq_calendar_for_year`. That is about 759 weeks from 2012-01-06 to 2026-07-17.

**Snapshot sources, in order of preference by date:**
- Repo `nasdaq_listed_*` snapshots from 2018 on.
- Wayback symbol files (dated by their footer): 2011-01-01 through 2014-07-11, plus 5 dates in 2015.
- Wayback company lists (dated by capture timestamp): 2011-11-13 through 2019-06-11. These fill the 2015-01 to 2018-01 hole and give market cap for the screen.
- Optional Common Crawl symbol-file captures.

**Membership rule for security s on date t.** Take the most recent snapshot on or before t (from any Nasdaq source). The security is listed if either:
- it appears in that snapshot, or in the snapshots immediately before and after t (the one-missing-snapshot tolerance already used in `build_universe`), and it is not removed by a Nasdaq Form 25 whose effective date is on or before t, and it is not moved to another exchange by `exchange_moves.csv` before t; or
- its first vendor price date is after that snapshot and on or before t, and the next snapshot contains it. This covers IPOs between snapshots.

**Common-stock filter.**
- `investable_common_equities` on the name, plus the ETF, Test Issue and NextShares flags where the file has them. The company lists have no ETF flag, so they are matched to the symbol-file rows nearest in time.
- `known_non_common_symbols`, as used in `load_ohlc_panel`.
- SPACs are removed by the existing name pattern.
- ADR handling follows D4.
- **Flag:** the filter's "Depositary Shares" patterns treat ADR names inconsistently. List every ADR in `security_master` and let the protocol decide.

**Staleness.** Record `snapshot_age_days`. In 2012–2014 the gaps are up to about 4–5 months, so every week with an age over 160 days is flagged.

Output: `universe/weekly_listed.csv.gz` (cache) and `weekly_universe_summary.csv` (committed).

### 3.2 Candidate list for price completion (liquidity pre-filter)

A name is added to `candidate_fetch_list.csv` when it has no vendor raw series covering its listed interval and at least one of the following holds:

| Rule | Window | Metric | Threshold |
|---|---|---|---|
| A1 | 2012-01 to 2018-03 | 50-day median of WIKI raw close × raw volume, ranked against every listed common stock priced from WIKI, Yahoo, Tiingo or the stored files (stored files converted to raw) | Rank ≤ 300 in any week |
| A2 | 2011-11 to 2019-06 | Wayback company-list market cap | Rank ≤ 400 on any capture date |
| B-A | Nasdaq Form 25 delisting, 2018-04 to 2024-06 | Maximum XBRL public float in the 3 years before delisting | ≥ $1B (about 128 uncovered, from the scout) |
| B-B | Same | Same | $500M–$1B (about 91 uncovered) |
| B-C | Same | Same | $300M–$500M: a **seeded random sample of 20**. If any one reaches rank ≤ 300 in any week, fetch all of tier C in month 2. Otherwise stop and record the result. |
| C | File starts more than 60 days after the name first appears in a snapshot | A1, A2 or B-A also holds for the missing window | Active names are fetched from Yahoo, delisted ones from Tiingo |
| V | Verification | Stratified set of 50 active names: all names with odd split factors, special dividends above 10%, splits after 2023, and the 2025-06-24 break names (KLAC, NFLX, BKNG, HON, CRWD, AZN, LCID, TLRY and others) | Always included |

Public float is checked for unit errors: it is flagged when float ÷ (shares outstanding × nearest raw close) falls outside [0.05, 1.5], using the shares-outstanding frames from step 3. Examples already seen: SeqLL $4.8T, CenterState $2.3T.

**Routing rules (these save Tiingo quota):**
- 2012-01 to 2018-03-27: use WIKI if it has the ticker and the entity check passes (§4.4). The 46 `sue_lt_2020_2026` supplements, which start in 2017, get their 2012–2016 history from WIKI rather than a second Tiingo download. Only names absent from WIKI, or failing its entity check, go to Tiingo.
- 2018-03-28 onward, for delisted names: Tiingo, and only if `tiingo_range_match` holds, meaning a single `supported_tickers` row whose start and end cover the needed interval. A reused ticker whose only row is the newer company goes to `wrong_entity` without spending a symbol.
- Active names: Yahoo, which returns the full history of the current company under its current ticker. Tiingo is used only for the V sample, or if Yahoo fails.
- A name that no free source has goes to `unfillable.csv` (known so far: BMC, Lincare, MOLX, ONXX, PMTC, CNSIV, FMCN, MRKT). Renamed tickers are tried first: for example RIMM became BBRY and then BB, and YNDX became NBIS. [unverified: whether those vendors keep the older history under the new ticker.]

### 3.3 Checking that the top-250 is complete (without returns)

1. **Proxy-margin check, every week.** Among listed common stocks without vendor prices, count those whose market-cap proxy (Wayback market cap, or float from XBRL frames, carried forward up to 12 months) is at least the median proxy of the names ranked 200–250 that week. Target: 0 in at least 95% of weeks, and never more than 3.
2. **Capture-date coverage.** On each Wayback company-list date, the top 200 Nasdaq common stocks by market cap must have vendor prices on that date (≥ 99%, with exceptions only from `unfillable.csv`). Market-cap-weighted coverage of Nasdaq common stock must be ≥ 97% before 2023 and ≥ 99% from 2023.
3. **Nasdaq-100 check.** Every year-end member in `holdout_2011_2019/inputs/nasdaq100_members_wikipedia_yearend.json` (2011–2019) has prices for that whole year: 100%. Optionally, extend 2020–2025 from Wikipedia revisions [not in repo].
4. **Form 25 check.** Every Nasdaq Form 25 for common stock with float ≥ $1B has one of two things: a vendor series ending within 5 sessions of the effective date (or of the merger close), or a documented exclusion.
5. **Fetch-margin check.** Among names fetched in month 1, count how many land at rank ≤ 250. If tier B or C names land in the top 250, the screen is too tight, so lower it one tier in month 2.
6. **Unfillable impact.** For each year, estimate how many top-250 name-weeks are held by unfillable names. An unfillable name-week is one inside the name's `unfillable.csv` window with no vendor price. Step 12 gives the same three readings for every missing reason except the pending ones, with the unfillable-only share beside them. The three readings are:
   - **model**: a week counts when the name's size proxy reaches the median of ranks 200–250 that week. The proxy is the market cap, or the float when the float is at the median and the market cap is below it. A week also counts when step 6's dollar volume reaches the rank-250 cut, whatever the proxy says. A week with no size evidence ("unknown size") counts for nothing.
   - **calibrated**: the model, plus each unknown-size week times the round-10 hand-review sample's top-250 rate for that year. The rates are 0.19% for 2021–2023, 1.04% for 2018–2020 and 2024, and the pooled 0.44% for the other years. They are written in the code with the SHA-256 of the sample file, and validate reports whether the file still matches.
   - **upper**: the model, plus every unknown-size week counted as a top-250 week.

   Acceptance: the **model** is ≤ 2% of slots (250 × weeks) in every year. The calibrated and upper readings are reported beside it and do not decide pass or fail. A year above 2% on the model is reported as known survivor bias in the data report. Under §0 it is not used to judge the strategy.
7. **Optional independent volume check [unverified; test ≤ 5 requests first].** FINRA daily short-sale volume files (`cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt`, reportedly from 2018-08; per-facility files earlier). They give an off-exchange share-volume rank for every symbol, including delisted ones. Any symbol in that list's top 500 by share volume that is Nasdaq-listed and has no prices gets flagged. This would take about 2,000 requests for 2018–2024.

---

## 4. Unit and total-return reconciliation

### 4.1 Canonical record per security and session

`close_raw` (as traded), `volume_raw`, `split_factor` S (new shares per old share on the ex-date, otherwise 1), `div_cash` D (as paid on the ex-date).

- Total return: r_t = (C_t·S_t + D_t)/C_{t−1} − 1. This is the CRSP definition, and it matches Tiingo's `adjClose` ratio to 1e-11 on the scouts' test names.
- Price-only return is **not stored as an input**, because it is wrong on spin-off days (EBAY would show −57%).
- The $10 filter and dollar volume use `close_raw` and `volume_raw`.

### 4.2 Converting each source to the canonical record

| Source | Conversion |
|---|---|
| Tiingo | `close`, `volume`, `splitFactor` and `divCash` map directly. Row check: `adjClose_t/adjClose_{t−1}` equals the formula to within 1e-8. |
| Yahoo chart | C = `close` × product of split ratios after t. Volume and dividend amounts use the same product (volume divided, dividends multiplied). S comes from `events.splits`, D from `events.dividends`. Do not use `adjclose`: it uses the multiplicative method and differs by up to 0.107% on special-dividend days. |
| WIKI | `close`, `volume`, `ex-dividend` and `split_ratio` map directly (raw). |
| Stored files (`cleaned_stocks_data/price`) | **Used only as a third vote on returns** inside segments that do not cross a known break (2025-06-24, HON 2026-06-29) or an ex-date after 2023. Never used for price levels. |
| Nasdaq API | Fallback only. Split-adjusted to the fetch date, price only, back to 2016-09 (rolling 10 years). Add D from the dividends endpoint. |

### 4.3 Split and distribution table

`split_events.csv` is built from every S ≠ 1 in any source for the security's listed interval.

- **Match rule:** same ex-date (±1 session) and the same ratio within 0.1% across the available sources (Tiingo, Yahoo, WIKI, Nasdaq calendar). Acceptance: ≥ 98% agree, and 100% of disagreements are reviewed.
- **Classification:** `_split_ratio(S)` returns n/m with n, m ≤ 100, and D ≈ 0 means split or reverse split. Anything else, or D above 10% of the prior raw close, is a spin-off or distribution. It must carry an SEC 8-K URL (Items 2.01, 3.03 or 5.03) or a Form 10 or information statement.
- **Known cases to reproduce:**
  - Spin-offs recorded as `divCash`: EBAY 2015-07-20, CTXS 2017-02-01, LVNTA 2014-08-28, DISCK 2014-08-07.
  - Spin-offs recorded as an odd `splitFactor`: HON 2025-10-30 (1.061), NUAN 2019-10-02 (1.155), HON 2026-06-29 (0.9535, which is the HONA distribution plus a 1-for-2 reverse split; 8-K 0000773840-26-000084).
  - Ordinary splits: PRPL 1:25 (2026-07-20), SIRI 1:10 (2024-09-10), NFLX 10:1 (2025-11-17), BKNG 25:1 (2026-04-06), KLAC 10:1 (2026-06-12), MNST 2:1 (2026-08-11).
- `confirmed_price_adjustments.csv` (1 row) and `corporate_actions.csv` (10 rows) are read and must match.

### 4.4 Rules for suspicious data points (each must be resolved; the counts go in the report)

| Rule | Trigger | Resolution |
|---|---|---|
| R1 big move | \|r\| ≥ 40%, or a raw price ratio ≥ 2× or ≤ 0.5× with no split event | Second source within 0.5%, or an SEC or press document. Record in `reviewed_moves.csv`. |
| R2 hidden split | The price ratio fits `_split_ratio` and no source has S ≠ 1 that day | Must resolve to either a split event or a market move |
| R3 source disagreement | Sources differ by more than 0.5% in r on a normal day | Majority of the available sources (Tiingo, Yahoo or WIKI, stored file). With only two that disagree, check an SEC or press document or mark the day `unresolved`. Known cases: NFLX 2013-10-22 (Tiingo repeats 354.99), KLAC 2015-01-23 and MNST 2015-03-12/13 (stored file wrong). |
| R4 flat run | ≥ 3 identical raw closes, or zero volume inside the listed interval | Check another source; if genuine (a halt), keep it and flag it |
| R5 filler after delisting | Constant closes after the last real trade (SGEN 161 rows, SPLK 186, EVBG 143) | Cut at the last session with volume > 0, and join to the Form 25 or merger close date |
| R6 session gaps | A missing XNAS session inside the listed interval | Fill from another source, or flag it as a halt with a source |
| R7 level check | Raw close differs by more than 1% between sources | Resolve before the $10 filter can be trusted |
| R8 splice | Where two sources are joined (WIKI → Tiingo, WIKI → Yahoo) | ≥ 20 overlapping sessions with raw close within 0.5% and r within 1e-4 on 95% of them. Only returns are chained, never levels. |
| R9 entity check (WIKI, Tiingo) | A gap of more than 10 sessions, or a raw price jump over 50% across a gap, or data outside the security's listed interval (DTV, ALTR, LIFE, DELL mixes; BEAT, CCXI, RDUS, FOXA and DISCA reuse in the repo) | Keep only rows inside the `ticker_intervals` span of the security, and check that the price level lines up with the Wayback `LastSale` on capture dates within 2% |

### 4.5 Terminal returns (step 11)

- **Scope:** every universe security whose series ends before 2026-08-31.
- **Cash merger:** consideration ÷ last raw close − 1, booked on the session after the last trade.
- **Stock merger:** shares × the acquirer's raw close on the effective date, plus any cash.
- **Exchange move:** no terminal return. The series continues from a vendor for up to 4 weeks, so a position held through the exit is still priced.
- **Bankruptcy or OTC:** use the OTC price if a vendor carries one; otherwise the D5 rule applies.
- **Record format:** the existing `terminal_returns.csv` format with the extra columns. Unknown type must be ≤ 5% of rows, each listed.
- **Existing rows** are reused and cross-checked where they overlap: 210 + 11 + 3.

---

## 5. Earnings dates, SIC and FF49, factors

### 5.1 Earnings

- **Events:** from submissions `filings.recent` plus older pages, take 8-K and 8-K/A filings whose `items` include `2.02`.
- **Times:** fetch the `-index-headers.html` for **every** such filing of universe companies from 2011-10 on, about 13–16k requests. That settles the mixed UTC and Eastern labelling of `acceptanceDateTime` (the scout found 7 of 9 checked filings were Eastern time labelled `Z`). The header's `ACCEPTANCE-DATETIME` in Eastern time is the value used. The JSON value is kept, and the mismatch rate is reported.
- **D0** = the first XNAS session whose close (16:00, or 13:00 on early-close days) comes after the acceptance time. The protocol's 3-day window (D0−1, D0, D0+1) is built from this. `first_in_fiscal_quarter` is stored, because the protocol must choose between all Item 2.02 events and only the first in each fiscal quarter.
- **Fallback:** companies with no Item 2.02 filing in a company-year get the 10-Q/10-K acceptance, from the header, in `earnings_fallback_periodic.csv`.
- **Foreign filers (D4).** Two tests of ≤ 20 requests each before deciding:
  - (a) Submissions `primaryDocDescription` on 6-Ks containing "results" or "earnings" [unverified field content].
  - (b) The Nasdaq earnings calendar `api.nasdaq.com/api/calendar/earnings?date=` for 2013, 2016 and 2020 dates [unverified historical depth].
  - If neither gives at least 90% of quarters for a 10-name sample, the protocol excludes 20-F, 40-F and 6-K filers from the universe. That exclusion is decided before any returns are seen.

### 5.2 SIC and FF49

- **Point-in-time SIC** comes from the "STANDARD INDUSTRIAL CLASSIFICATION" line in the same headers, plus one 10-K header per company-year where there is no 8-K. SIC at date t is the latest header on or before t. Before the first header, the earliest header is used and flagged.
- **6770 blank checks** are replaced by the first operating SIC after the merger (as with LCID) and flagged.
- **Industry maps:** FF49 from `Siccodes49.txt` (598 ranges, no overlaps). FF17 and FF12 are also mapped. A SIC code with no match (for example 9995) goes to FF49 "Other"; this is a protocol rule to register.
- **Known difference:** Ken French assigns industries from Compustat or CRSP SIC each June. Ours is SEC header SIC, so assignments will differ; state this in the report.

### 5.3 Factors and benchmarks

- **Ken French daily files:** 5-factor 2×3, momentum, short-term reversal, and 49 industries (value-weighted and equal-weighted blocks). This is the 202608 build, pinned by SHA-256. The scout's `/tmp/kf` copies are compared by hash against a fresh download.
- **Row check:** exactly 3,655 rows from 2012-01-03 to 2026-07-17, with no missing codes, and dates equal to XNAS sessions.
- **QQQ total return:** `research_cache/holdout_2011_2019/qqq_tiingo_2010_2020.csv` (SHA 3099…9930) for 2012-01-03 to 2017-12-29, joined to `output/research_only/qqq_nasdaq_history.csv` (SHA ebfb…d0d0) from 2018-01-02.
  - Overlap for 2018–2020 must agree within 1e-9 on returns.
  - Total dividends 2012–2026-07-17: 60.
  - Yahoo is missing 2020-09-21 ($0.38824); this is noted and not used.
- **VIX:** the daily close, for the VIX terciles in the plan.
- **IBKR borrow:** no history exists. A daily FTP recorder would help only the forward observation, and it needs the owner's approval as a standing job.

---

## 6. Validation thresholds and the data report

| Dataset | Check | Accept |
|---|---|---|
| Listings | Snapshot counts by year; age ≤ 160 days; parsed rows ≥ 1,000 per symbol file | Every week has a snapshot; long gaps listed |
| Universe | §3.3 checks 1–6 | As stated there. Check 6 is read on the model; the calibrated and upper readings are reported beside it |
| Universe | Listed name-weeks that step 12 marks missing (`missing_reason`) | 0 blocking name-weeks. A week blocks when its reason is one of these: `series_gap`, `fetched_pending_reconcile`, `answer_not_in_panel`, `no_vendor_source`, or a reason the validator does not know. A pending week blocks when no open candidate row of its source covers it. A `short_window` week blocks when it has top-250 evidence (§0: step 6's dollar volume at the rank-250 cut, a size proxy alone at the band median, its own canonical dv20 rank within 300, or no evidence). Other `short_window` weeks are listed as judged small. Weeks after the series' last price or delisting do not count |
| Universe | Every universe name-day comes from a vendor raw source (Tiingo, Yahoo or WIKI) | 100% |
| Prices | Tiingo row check (formula) | Maximum error ≤ 1e-8 |
| Prices | Days with two or more sources: r agrees within 0.5% | ≥ 99.5% of name-days; 0 unresolved after majority vote |
| Prices | Active names, Yahoo-derived r against Tiingo r for the 50-name V sample | \|Δr\| ≤ 1e-4 on ≥ 99.5% of days; the rest explained |
| Prices | R1–R9 queues | 0 open items; counts by rule and year reported |
| Prices | Stored-file comparison | Days with more than 0.5% difference, by year. The 2025-06-24 cluster (69 files) and the switch to price-only dividends in 2023–2025 must appear as expected. |
| Splits | Cross-source agreement | ≥ 98%; every odd ratio or distribution above 10% has an SEC URL |
| Dividends | Tiingo against Nasdaq (100-name sample, from 2013-08) and against Yahoo after split scaling | Ex-date match ≥ 98%; amounts within $0.001 |
| Terminal | Coverage of universe series ending early | 100% have a row; unknown ≤ 5% |
| Earnings | Domestic company-quarters in the universe with an event | ≥ 95% (Item 2.02 or fallback). Gaps between events of 60–120 days for ≥ 90%. 20 events checked by hand against IR press releases. |
| Earnings | Header-resolved acceptance time | 100% of the headers fetched; JSON mismatch rate reported |
| SIC/FF49 | Universe name-weeks with an FF49 code | ≥ 99%; SIC changes and 6770 fixes listed |
| Factors | Rows, dates, hashes | Exactly as in §5.3 |
| Manifest | Every file the test will read is hashed; nothing unhashed is read | 100% |

**Data report:** `docs/reversal_2012_2026_data_report.md`, committed together with the inputs and `validation_summary.json` **before** the protocol is frozen and before any return is computed. It contains:
- sources, versions, fetch dates and request counts;
- coverage tables by year for listings, prices, earnings and SIC;
- the unfillable list with its estimated top-250 impact by year;
- counts for each R1–R9 rule;
- the terminal-return breakdown;
- known biases: survivors before 2016 where WIKI and Tiingo both fail, SEC SIC versus Ken French SIC, and foreign-filer handling;
- the decisions still open from §8.

The report must contain **no** aggregated returns, and the ledgers must record that none were computed. The small "mirror leak" from the 2012–2019 momentum result is noted again there.

---

## 7. What the free tiers allow

| Source | Limit (as reported) | Need | Months |
|---|---|---|---|
| Tiingo | 500 unique symbols a month, 50 requests an hour, 1,000 a day, 1 GB a month, internal use only. Whether the month is a calendar month or a rolling 30 days is [unverified]. The scouts already used about 15–25 October symbols [check the exact count on the account page]. | Month 1: B-A about 110 + B-B about 70 + A about 40–60 + C about 10 + V 50 + tier-C sample 20 + retry reserve about 50, so **about 350–390** | **1** for the strict list |
| Tiingo (broad) | — | Tier C in full, names found by completeness checks, reused-ticker retries: about 150–300 more | **+1 (November)** |
| Tiingo (own-panel industry, only if D2 is rejected) | — | About 1,000+ more names | +2–3 more |
| Yahoo chart | Unofficial, no documented quota; yfinance 0.2.55 already hit `YFRateLimitError` | About 900 | Should fit this month [unverified at this volume] |
| WIKI | Free with a key; limits [unverified] | 1–2 export calls or about 250 pages | This month |
| SEC | ≤ 10 requests a second (plan for 7) | About 20–22k in total | About 1 hour in total, this month |
| Wayback, Ken French, CBOE | Polite pacing | About 60 | This month |

Bandwidth: Tiingo JSON is about 0.95 MB for a full history, so about 390 MB per month. That is under 1 GB; `format=csv` would roughly halve it.

**Fallbacks, in order:**
1. If Yahoo is blocked: run active names on Tiingo instead (+700–900 symbols, so 2 more months). Or use the Nasdaq API from 2016-09 plus the stored files as the vote for 2012–2016. That is weaker: it has no raw levels before the break, so it needs the V-sample checks to pass.
2. If Tiingo's month-1 quota runs out: carry `deferred_quota` rows to November, in priority order B-A, then A, then B-B, then C.
3. If the owner allows paying: Tiingo Power for one month ($30 a month per the strategy doc; not re-verified) removes the 500-symbol cap, so everything is done in October.
4. For names absent from both Tiingo and WIKI: paid sources only (Sharadar SEP, Norgate, historicaldata.net; prices [unverified]). Otherwise they stay as documented survivor bias.

---

## 8. Risks and open questions for the owner

1. **Industry benchmark (D2).** Ken French 49-industry portfolios (all CRSP stocks, free, no survivor issue) or an own Nasdaq panel ("larger panel", fewer than 5 names falls back to the market)? The own panel costs 2–3 more Tiingo months, and before 2023 it would inherit survivor bias.
2. **Foreign filers (D4).** Exclude, or use a tested free calendar? This must be decided before any returns are seen.
3. **Unknown terminal value (D5).** −100%, as the repo does now, or a pre-registered alternative such as −30%?
4. **Multi-class issuers** (GOOG/GOOGL, FOX/FOXA, DISCA/DISCK, LBTYA/K, NWSA/NWS): keep all classes or only the most liquid? Both classes can land in the same industry and the same side.
5. **Dollar-volume window** (20 or 50 days) and N (150, 200 or 250). Both windows are stored; the protocol picks one.
6. **Date conventions.** Is 2026-07-17 the last signal date (prices then run to 2026-08-14), or the last return date?
7. **SEC User-Agent.** Needs a real contact string supplied by the owner in `.env.sec`. The placeholder may get blocked. *Settled 2026-10-02:* the owner's contact is in the main checkout's `.env.sec`, which is git-ignored. `src/io/sec_contact.py` reads it for both `SEC_HEADERS` and `reversal_data_common`.
8. **Vendor terms.** Tiingo is internal use only and Yahoo is unofficial, so raw and vendor-derived values stay in `research_cache`; only ranks, IDs and SEC facts are committed. The redistribution terms for Ken French data are unclear.
   *Checked 2026-10-02 against the official terms:*
   - Tiingo §1.6(a) (2026-08-05) bars free-plan users from storing Tiingo data persistently.
   - Yahoo's terms §2(d)(ix) (2026-08-04) bar automated collection without permission.
   - nasdaq.com/legal bars automated capture and storing for later use.
   - SEC, Ken French (local use), the WIKI table and the Internet Archive (research use) are fine.
   - EODHD would allow private storage (paid).

   *Owner decision 2026-10-02:* keep the stored data and keep using these sources; the owner accepts the risk. The data stays local and is never shared or committed; only ranks, IDs and SEC facts are committed. Request rates stay polite.
9. **Known residual gaps.** The 2012–2015 period for names delisted in 2016–2017 that WIKI does not have is lost: Tiingo cuts those histories at 2016-01-04. Some large names cannot be filled at all (BMC, LIFE, ONXX, MOLX, Lincare). The development period will carry some survivor bias; the report quantifies it.
10. **Unverified mechanics:**
    - the WIKI bulk-export size and rate limits;
    - whether Tiingo's month is a calendar month or rolling;
    - how far back the FINRA and Nasdaq earnings-calendar endpoints go;
    - CBOE VIX URL;
    - the date-dependence of SEC `acceptanceDateTime` labelling. Fetching headers sidesteps it, but SEC may rewrite the JSON, so raw files are hashed.
11. **Ken French revisions.** The files are revised monthly and no daily versions are archived. Pin 202608 by hash and do not refresh after the protocol is frozen.
12. **IBKR borrow recorder.** It would be a standing daily job (FTP `usa.txt`) and needs explicit approval. It is not needed for the backtest.
13. **Budget.** Every pull above goes beyond the scouts' ~20-per-source probe limit. Steps 1–7 and 10 cost no Tiingo quota and can start once you approve. Step 8 spends about 350–390 of this month's Tiingo symbols.

Key paths:
- New outputs: `/Users/bytedance/code/quant_stocks/output/research_only/reversal_2012_2026/inputs/` and `/Users/bytedance/code/quant_stocks/research_cache/reversal_2012_2026/` (with a worktree symlink).
- Existing code reused: `/Users/bytedance/code/quant_stocks/.claude/worktrees/suggest-task-visibility-25008f/src/io/nasdaq_update.py`, `src/research/corporate_action_validation.py`, `src/research/data_quality.py`, `src/io/security_universe.py`, `src/io/fundamentals_update.py`, `src/research/shadow_evaluation.py`, `scripts/sec_submission_triage.py`, `scripts/research_sue_lt_2020_2026.py`, `scripts/research_holdout_2011_2019.py`.