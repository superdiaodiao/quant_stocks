# Forward observation: monthly checklist

This file makes the monthly update mechanical. The protocol itself lives in `docs/research_ledger_qc_factors.md` section 4, and if the two ever differ, section 4 wins. Results are appended to section 4.1 there, never here.

## A. S/P top-10 (active since 2026-08; frozen 2026-10-05)

### Fixed inputs

- **Code:** `qc/sp_top10_trade/main.py`, frozen at commit `9f4516b22`, SHA-256 `f1362c4cbb3c1a522696ef8a689f18efb15f60a1f9dd1936fdbb33076acc64dd`.
- **QC project:** 37374631 (`main.py` in the project must hash to the same value).
- **Allowed edit:** `self.set_end_date(...)` only. `FACTOR = "SP"` and `EXCLUDE = ()` stay unchanged.
- **Benchmark:** ONEQ total return, as computed by the same run (runtime stats `MJ26`, ...).

### Monthly steps

1. **Data check.** In a probe project (e.g. `ForwardObs_DataProbe`, 37583373), run a read-only backtest. The probe records:
   - the last daily bar of SPY, QQQ and ONEQ;
   - the last fundamental universe date.

   Then decide:
   - **Data stops before the month to be recorded:** log a dated "pending" line in 4.1 and stop. Do not substitute another source.
2. **Code check.** Confirm all three hashes are equal:
   - the local file;
   - `git show 9f4516b22:qc/sp_top10_trade/main.py`;
   - the QC project file.
3. **Run.** Edit only the end date, compile, and run one backtest. Log the backtest ID and end date in 4.1.
   - **End-date convention (open point for the owner):** in the frozen code, month M's return runs from M's rebalance close to the rebalance close of M+1 (first trading day of M+1). With the end date at the month-end of M, the run records M's picks but only M−1's return. Two readings:
     - (i) End date = latest month-end. Each update records the previous month's return and the current month's picks.
     - (ii) End date = first trading day of the next month. Picks and return are recorded together.

     Either reading uses the same frozen rule. Pick one before the first recording and keep it.
4. **Read outputs (short fields only).**
   - Runtime stats:
     - `MS26`/`MJ26` (strategy and judged-benchmark monthly %, in month order);
     - `MISC` (`end` equity, `months`);
     - `PASS`.
   - Picks:
     - use `backtests/orders/read`, filtered to orders whose tag starts with `B <yyyymm>` / `U` / `T` / `X`;
     - tag fields: `r` (rank), `v` (S/P), `mc` ($M), `p`, `k`, `rv`, `s` (sector), name;
     - names kept from the previous month have no new order, so take the full top 10 from the month's log line `yyyymm SYM:v:mc:p ...` if the log is not truncated. Otherwise take them from the held positions after the rebalance.
5. **Flag each of the 10 picks, (a)–(d) of section 4.**
   - (a) `rv` < 0.5 or > 2. If `rv` is missing, write "untestable" for (a).
   - (b) `k` < 0.5 or > 2. Note: multi-class companies often show k ≈ 0.25/0.5 from a wrong shares field. Still flag them, and add a note.
   - (c) First price date in QC (`symbol.id.date`) less than 365 days before the selection date.
   - (d) Holding company consolidating a listed subsidiary (e.g. PAGP/PAA). This is a manual judgement from the company name and filings; write the reason.
6. **Returns.**
   - **Raw:** the strategy's monthly return from `MS..`.
   - **Filtered (open point for the owner, to be fixed before the first recording):** section 4 says "reported raw and filtered" but does not say how. Two candidates:
     - (i) Equal-weight paper return of the unflagged picks, from the same selection-day close to the next rebalance close. This needs no code change; prices come from the run.
     - (ii) A sensitivity run with the flagged `symbol.id`s in `EXCLUDE` (as in 3.7.4). This backfills with the next names, but it is a second run and an edit beyond the end date.

     Reading (i) is the closer fit to "applied in reporting and does not change the frozen rule".
   - **ONEQ:** from `MJ..`.
7. **Append one row per month to the 4.1 table:**
   - picks with flags;
   - raw and filtered return;
   - ONEQ;
   - excess, raw and filtered;
   - cumulative excess since 2026-08, defined as the strategy's cumulative return minus ONEQ's cumulative return, in percentage points, both chained from 2026-08;
   - abandon status (−25 points or worse at any time → abandon).
8. **Clocks.** No verdict before 24 months (2028-07). The verdict comes at 36 months (2029-07). A positive verdict needs all of:
   - cumulative excess > 0;
   - monthly-excess t ≥ 2;
   - no data-error month driving the result.
9. **Don't:** change rules, parameters, universe or costs; rerun to get a different number (only API/code errors justify a rerun, and each one is logged); delete QC projects.

## B. Candidates — observation started (owner decisions 2026-10-09 and 2026-10-10)

**Added 2026-10-10:** B3, the S-MISP short overlay (N = 10, k = 20%, MN), with its own forward window, comparisons and evaluation (section B3 below). The rest of this preamble and B0–B2 are about B1 and B2.

**Decision (2026-10-09):** the owner chose to observe both candidates, with the rules frozen exactly as below. B2 is recorded as two separate lines, SEL-A (config 29876) and SEL-P (config 29916).

- **Forward window:** data after the 2026-10-09 close. Accounts start at the 2026-10-12 close, as the frozen simulators do. The first possible T-trade is therefore at the 2026-10-14 open. The earlier phrase "first trade at the 2026-10-12 open" was a wording slip, corrected 2026-10-09 before any forward data. Month 1 is the partial 2026-10, so month 36 is 2029-09.
- **Recording:** each month, append the month's T-trades and returns for the strategy, H_base, H_match and ONEQ to `docs/forward_observation_log.md`.
- **Evaluation (fixed now, before any forward data):**
  - The main comparison is H_match.
  - There is no verdict before 24 months.
  - At 36 months the verdict is positive only if the cumulative excess over H_match is > 0 **and** the monthly-excess t is ≥ 2.
  - These are small-edge candidates, so there is no early abandon rule. They are observed, not traded.

### B0. Monthly run (mechanical)

The runner `scripts/forward_observation.py` does the whole monthly update for B1, SEL-A and SEL-P (and B3, see B3.7). It imports the frozen simulators (`research_selective_t.run_one` for S3-Yb; `research_t_grid.make_inst` / `exact_run` / `hmatch_series` / `hbase_of` / `oneq_for` for configs 29876 and 29916) and does not reimplement them. Tests: `tests/test_forward_observation.py` (synthetic data).

1. **When:** after 18:00 New York time on the first trading day of the new month, or later. A month's row is `final` only once the data hold a session of a later month. The frozen simulators treat the window's last session specially: an open T-trade is closed at that close, and there is no month-end reset and no new signal. So the month holding the last session is `provisional`, and the next run replaces it.
2. **Run** from the repo root:

   ```
   PYTHONPATH=. .venv/bin/python scripts/forward_observation.py --fetch
   ```

   - `--fetch` re-downloads the 20 charts (18 names, QQQ, ONEQ). **Source (owner decision 2026-10-10):** Tiingo first from 2026-11 (`TIINGO_API_KEY` from the environment or `.env.tiingo`; the free plan's 500 unique symbols a month were used up in October), Yahoo v8 as fallback (one request per 2 s, stop at the first 401/403/429). Tiingo's raw OHLCV, `adjClose`, `divCash` and `splitFactor` are written in the Yahoo chart format, so the frozen loaders are unchanged (`scripts/forward_prices.py`).
   - **History state:** RSI2's expanding percentiles need QQQ from 1999 and must not move with the vendor. The committed `state/forward_observation/price_history_returns.csv.gz` (about 1 MB) keeps the history through the 2026-10-09 decision close as ratios only (close / previous close, open / high / low over the close, adjusted-close ratio, dividend / close, split ratio; no price levels): QQQ from 1999, ONEQ from 2003, the 18 stocks from 2024-01-02 (S3-Yb needs only the SMA20; checked: S3-Yb returns over 2026-03..2026-10 are identical with the 2011 and the 2024 start). Each run fetches only the last 30 days before the state end plus the new days, rebuilds the levels backwards from the fetched close on 2026-10-09 and checks the overlapping days (`fetch_sources.csv`; on 2026-10-10 all 20 matched to 5e-15, and the rebuilt QQQ / ONEQ matched the research caches to 3e-13).
   - Raw bodies go to `research_cache/forward_observation/raw/` (local only, never in Git). The request log is `research_cache/forward_observation/fetch_log.csv`.
   - The default as-of date is the last complete New York session. `--as-of YYYY-MM-DD` sets it explicitly, and a later date is clamped to that session.
   - Every vendor frame is truncated at the as-of date right after parsing, and this is asserted.
3. **Check the console:**
   - `DATA`: first and last date and row count per symbol, plus `DATA FLAGS` if any OHLC are missing or inconsistent.
   - `OVERLAP`: fresh closes vs the research caches through 2026-09-30. Before the start these matched exactly for all 20 symbols.
   - `WARNING ... a final row changed`: this means a vendor revision. Note it in this checklist for the owner. Do not edit the log by hand: the runner rewrites it on every run.
4. **Output:** `docs/forward_observation_log.md`, with one table per line (B1, SEL-A, SEL-P) and one row per month.
   - Each run recomputes every forward month from the 2026-10-12 start and replaces the rows of the months it computes. Rerunning for the same month replaces that row and never adds a duplicate.
   - The runner refuses to write the log for an as-of date earlier than the log's last "data through" date. Use `--dry-run`, which prints the log and writes nothing, to test an earlier date.
   - Daily returns, exposures and trades per run are saved locally in `research_cache/forward_observation/runs/` (not in Git).
5. **H_match (fixed 2026-10-09, before any forward data):**
   - For month m, each account's w_m is the mean daily exposure that the frozen simulator reports for the account run from the start through the last forward session of month m, or through the as-of date for the latest month. This is the exposure realised to date.
   - H_match's daily return in month m = w_m × the instrument's daily total return. For B1, H_match is computed per account and the basket is the mean.
   - No later data enter a month's H_match, so final rows do not change.
   - The console also prints the whole-window H_match (one w over the full window, the research definition) for reference.
6. **Account start:** the accounts open at the 2026-10-12 close, so the first forward return session is 2026-10-13.
   - Under the frozen code a signal is read at a close and acted on at the next open. The first forward signal is therefore at the 2026-10-13 close, and the earliest T-entry is the 2026-10-14 open.
   - **Resolved 2026-10-09:** accounts start at the 2026-10-12 close and the first T-trade is at the 2026-10-14 open, as the runner already does.
7. **Month count:** 2026-10, a partial month from 10-13, is month 1. Month 24 is 2028-09 and month 36 is 2029-09.

These were named as the only possible forward-observation candidates in their ledgers. Nothing below starts until the owner decides. If observation starts, the rule must be used exactly as written here, with no parameter changes. The forward window would start at the first month after the decision.

### B1. Stock basket S3-Yb (`docs/research_ledger_selective_t.md`, sections 0.3–0.5 and 6)

**Status in sample:**
- It formally passes "T-trading gain" versus H_base: +0.6 / +0.7 points/yr, t 2.67. That is below Bonferroni 2.77.
- Versus the same average exposure (H_match), the gain is only +0.04 / +0.15 points, t 0.48.
- Its ONEQ comparison is void because of survivorship bias.

**Frozen rule:**
- **Universe:** U18, one $10k cash account each: AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AVGO, COST, NFLX, CSCO, INTC, ADBE, QCOM, PEP, AMD, CMCSA, AMGN.
- **Base position and reserve:**
  - Base B = floor(0.75 × (10,000 − 5) / (price × (1 + half-spread))) shares, bought at the first-day close.
  - The rest stays as settled-cash reserve (T+1 settlement; buys use settled cash only).
  - At each month-end close, if no T-trade is open and the base is outside 70–80% of the account, reset it to 75%.
- **Trigger:** day-t close ≤ (1 − 10%) × SMA20_t. SMA20 uses split-adjusted, not dividend-adjusted, closes and includes day t.
- **Entry:** at the t+1 opening auction (MOO), buy q = min(floor(B/3), floor((settled cash − 5) / (1.10 × close_t))) shares.
- **Exit:** a limit sell, re-placed each day before the open:
  - Limit = min(SMA20 through the previous close, buy price × 1.06).
  - If that SMA20 ≤ buy price, the limit is buy price × 1.06 instead.
  - Fill if open ≥ limit (fill at the open). Otherwise fill only if high ≥ limit + max(1¢, 0.05% × limit) (fill at the limit).
  - Forced exit at the close (MOC) on the 20th holding day; the entry day counts as day 1.
- At most one open T-trade per account. Dividends are paid on shares held at the prior close.
- **Costs:** IBKR Tiered (`research_reversal_dev.order_cost`) plus a 2 bp half-spread (minimum $0.005 / price).
- **Comparisons:**
  - H_base: same account, no T-trading.
  - H_match: same average exposure.
  - ONEQ: report only.
- **Code:** `scripts/research_selective_t.py` (config S3-Yb, stock parameters).
- **Data needed:** daily OHLC, splits and dividends for the 18 names, and ONEQ (Yahoo v8 chart; the owner has accepted the Yahoo ToS risk); T-bill. The work is local, not on QC.

### B2. QQQ RSI2-dip plateau (`docs/research_ledger_t_grid.md`, sections 0.3–0.6 and results)

**Status in sample:**
- Walk-forward picked this family almost every year from 2005: +0.4 to +0.6 points/yr versus H_match, t 1.1–1.6. It did not pass.
- It loses to H100 by about 1.9 points/yr and to ONEQ by about 0.8 points/yr.

**Frozen rule:**
- Both selectors picked members of the family. The latest walk-forward pick (2026) for each selector is in `output/research_only/t_grid/walk_forward_QQQ.csv`:
  - **SEL-A:** `RSI2 p2 B OPP H10 1/2 res25 ALL` (config 29876).
  - **SEL-P:** `RSI2 p2 B OPP H20 1 res25 ALL` (config 29916).

  The owner picks one (or both, reported separately) before starting.
- **Account:**
  - One $10k cash account in QQQ.
  - Base = floor(0.75 × (10,000 − 5) / (price × (1 + 1 bp))) shares, with a 25% settled-cash reserve (T+1).
  - At month-end, if no trade is open or queued and the base is off by more than ±5 points from 75%, reset it to 75%.
- **Indicator:**
  - RSI2 = Wilder RSI(2) on split-adjusted closes.
  - Threshold = the 2nd percentile of RSI2's own expanding history from its first value through t−1, needing at least 200 values (pandas `expanding().quantile(0.02).shift(1)`).
  - Median = the same expanding 50th percentile through t−1.
- **Trigger (B = buy dip):** RSI2_t ≤ the p2 threshold at the day-t close, with no trend filter (ALL). At the t+1 opening auction (MOO), buy q = min(floor(f × B), floor((settled + pending cash − 5) / (1.10 × close_t))) shares, where f = 1/2 (SEL-A) or 1 (SEL-P).
- **Exit (OPP):**
  - When RSI2_t ≥ its expanding median at a close, sell q at the next open.
  - Otherwise there is a forced sell at the close on holding day 10 (SEL-A) or 20 (SEL-P).
  - At most one open trade.
- **Costs:** IBKR Tiered plus a 1 bp half-spread (minimum $0.005 / price).
- **Comparisons:**
  - H_match: same average exposure; this is the main comparison.
  - H_base, H100, and ONEQ: report only.
- **Code:** `scripts/research_t_grid.py` (exact evaluation for one config ID).
- **Data needed:** QQQ and ONEQ daily OHLC, splits and dividends from 1999 onward, needed for the expanding percentile history (Yahoo chart, as in `research_cache/calendar/raw/`); T-bill. The work is local, not on QC.

### B3. S-MISP short overlay (`docs/research_ledger_short_overlay.md`, section 0 and results 1.1–1.7)

**Decision (2026-10-10):** the owner added one more candidate to forward observation: configuration **S-MISP, N = 10, k = 20%, MN**, with the rules frozen exactly as registered in section 0 of the ledger (registration SHA-256 `7b7c0e788decdfe918f1c42cc62188938f4dedf3d448086c234ca33442f4c9c5`, `output/research_only/short_overlay/frozen_prereg.json`) and implemented in `scripts/research_short_overlay.py` (commit `a5171bf7b`, file SHA-256 `3330710d4abc2b4ccf6988f64272977c52eb8c92496d077672d723039bbfecb8`). Everything in B3.1–B3.5 was written on 2026-10-10, before any forward data. It is observed, not traded.

**Status in sample:** the best of the 32 registered configurations, and it did not pass. Both folds beat ONEQ (H1 2012-02..2018-12: 17.3% vs 14.1% a year; H2 2019-01..2026-08: 27.2% vs 20.8%), but the fold t-values were 1.36 and 1.91 and the pooled monthly-excess t was 2.34, below the Bonferroni hurdle of 3.0. The short leg alone (worst 10 names) had an alpha vs QQQ of −23.7% a year (t −3.8), mostly from H2. Part of the MN excess is simply QQQ beating ONEQ plus a little leverage.

#### B3.1 Frozen rule (ledger section 0, this configuration only)

- **Universe:** the U300 monthly panel of `docs/research_ledger_ml_cross_section.md`: at each signal date s, the top 300 Nasdaq common stocks by 50-day median dollar volume (raw close ≥ $10, latest universe week on or before s), excluding financials (SIC 6000–6999), one share class per company. Fundamentals are point in time as in `research_fundamentals`: a fact is usable only when it was filed strictly before s.
- **Signal (S-MISP, a simplified Stambaugh–Yu–Yuan mispricing composite):** in the U300 at each s, each of 8 anomalies is turned into a cross-sectional percentile rank (0–1, ties get the average rank) in its "good" direction, and the ranks are averaged with equal weight. A stock needs at least 4 of the 8, or it has no score that month. Lower = more overpriced.
  - accruals (NI − CFO) / average assets: low is good;
  - net share issuance ln(SH₀ / SH₋₁): low is good;
  - asset growth: low is good;
  - net operating assets: low is good;
  - gross profit / assets: high is good;
  - ROA: high is good;
  - 12-1 momentum (I₋₂₁ / I₋₂₅₂ − 1 on the total-return index): high is good;
  - Altman Z (a financial-distress proxy): high is good.
  Only data up to the close of s are used; ties are broken by security ID.
- **Short basket:** sort from worst to best at each s. A short held from last month stays if it is still among the worst **2N = 20**; the remaining places are filled from the worst down to **N = 10** names. Each short's target value = k × equity ÷ N = **2% of equity**.
- **Whole shares:** shorts must be whole shares (IBKR does not allow fractional shorts): shares = target ÷ raw close, **rounded to the nearest share**. A name that rounds to 0 shares is skipped and the next name in the order is used (still within the worst 20; with none left, fewer than N names are held). QQQ may be fractional.
- **MN overlay:** QQQ = (1 + k) × equity = **120%** of equity; shorts = k = **20%**. The extra 20% of QQQ is bought with the IBKR margin loan; the short proceeds are collateral for the borrowed shares and do not offset the margin loan.
- **Timing:** decide at the s close; trade at the **next session's close** (the house convention). Targets are recomputed from the equity at the trade.
- **Rebalance bands:** a continuing short is not traded while its value is within 25% of target (`REBALANCE_BAND`); QQQ is not traded while it is within 2% of equity of its target. Shorts that leave the worst 20 are covered in full.
- **Reg T initial margin:** 50% on longs and 50% on shorts. If after trading 0.5 × (QQQ value + short value) > 0.95 × equity, the extra QQQ and the shorts are scaled down by the same factor to exactly 0.95 × equity (this binds only for k = 50%; not for this configuration).
- **Maintenance margin and forced liquidation (checked at each close):** requirement = 25% × QQQ value + Σ over shorts [price > $16.67: 30% of value; $5–16.67: $5 per share; $2.50–5: 100% of value; < $2.50: $2.50 per share]. If equity < requirement: cover all shorts at that close and sell QQQ down to 100% of equity, with 0.5% extra slippage plus normal costs; hold only QQQ until the next rebalance.
- **Squeezes and gaps:** marked to market daily with the total-return index; during a trading halt the price stays flat and the gap shows when trading resumes. Shorts pay substitute dividends (included through the total-return index).
- **Delisted shorts:** covered at the terminal value on its booking day (no commission).
- **Account:** starts with **$10,000** cash; QQQ and the shorts are bought / sold on the first day.
- **Costs and interest (per order / per day):**
  - commissions and fees: IBKR Pro Tiered (`research_reversal_dev.order_cost`: $0.0035 a share, minimum $0.35, maximum 1%; exchange and clearing fees; SEC fee and FINRA TAF on sales and short sales);
  - half-spread: `research_reversal_dev.half_spread` (2–8 bp by the signal-date dollar-volume rank, at least half a tick); QQQ 1 bp;
  - **borrow fee (a year, accrued per calendar day / 360)**, tiered at each s: the top 100 of that month's U300 by point-in-time market cap 0.5%; others 2%; hard-to-borrow proxy = the top 10% of 3-month daily-return volatility (`vol_3m`) in that month's U300, outside the top 100 by market cap: 10%;
  - margin interest: IBKR Pro USD tier 1, BM + 1.5% with BM = EFFR (previous session's EFFR, calendar days / 360); loan = max(0, −(cash − short value)): short proceeds do not offset the loan;
  - cash interest: USD 0% on the first $10k, BM − 0.5% above, scaled by equity / $100k below $100k; short proceeds earn nothing below $100k equity (so nothing for this $10k account).
- **Code:** `research_short_overlay.Cfg(signal="S-MISP", n=10, k=0.20, mode="MN")`, run through `misp_score`, `borrow_rates`, `signal_scores`, `loser_orders` / `select_shorts`, `schedule`, `simulate`, `month_returns` and `basket_diagnostics`.

#### B3.2 Forward window (what the code does)

- In-sample data end at the **2026-10-09 close**. Forward returns use only sessions after it.
- Signals are the last session of each month from **2026-10-30** (the last trading day of October 2026) on (`forward_smisp.signal_dates` = `research_megacap.signal_sessions`; a month's last session is known as such once the data hold a later session).
- `research_short_overlay.schedule` executes each signal at the **next session's close**: the first trade is at the **2026-11-02 close**. The account opens with $10,000 cash at that close (about 120% QQQ and 10 shorts of about 2% each, whole shares), so the first forward return session is 2026-11-03. Each later signal (the last session of month m) is executed at the close of the first session of month m + 1.
- The signal at s uses only prices through the s close (Yahoo is asked for bars up to s + 1 day and every frame is cut at s and asserted) and SEC facts filed strictly before s; the 12-1 momentum, 3-month volatility and 50-day dollar volume look back from s, as in the research.
- Monthly rows: calendar months; month 1 is the partial **2026-11**, measured from the 2026-11-02 close (research `month_returns`: the first month from the first value). Month 24 is 2028-10 and month 36 is 2029-10.
- A month's row is `final` once the data hold the next month's trade session (the close of the next month's first session); the month holding the last session is `provisional` and is replaced by the next run.

#### B3.3 Comparisons and evaluation (fixed 2026-10-10)

- **Comparisons:** ONEQ total return (adjusted close, same days, no costs; the judged benchmark, as in the ledger) and QQQ total return; plus the **short leg alone vs QQQ**: the research ideal equal-weight loser basket (`basket_diagnostics`: the buffered worst-10 names, no rounding, no costs) from each trade close to the next trade close, minus QQQ over the same days. For a short, a negative value is good.
- **Evaluation:** no verdict before **24 months** (2028-10). At **36 months** (2029-10) the verdict is positive only if the cumulative excess over ONEQ is **> 0** and the monthly-excess t vs ONEQ is **≥ 2**. Cumulative excess = (strategy chained from the start) − (ONEQ chained from the start), in percentage points. No early abandon rule. Observed, not traded.

#### B3.4 Borrow fees

- **The registered tiered assumption stays the rule** (0.5% / 2% / 10%, B3.1). The strategy column and the evaluation use it.
- **Report only:** the real IBKR fees from the daily snapshots of `scripts/record_borrow_fees.py` (launchd at 06:30 and 21:00 local; raw files `research_cache/borrow_fees/raw/usa_*.txt.gz`, log `research_cache/borrow_fees/log.csv`; columns SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI) are recorded alongside. For each calendar day a short is held, its FEERATE (% a year) is read from the latest snapshot whose own file time (the `#BOF` stamp, New York time) is on or before that day; symbols are matched by ticker with class separators written as a space (BRK.B → `BRK B`).
- The log shows, per month: the mean (over the shorts, of each short's mean daily FEERATE) and the maximum FEERATE; every short with **AVAILABLE = 0** (not borrowable) on any day, and every short missing from the file; and a sensitivity return with the IBKR fee in place of the tier (each short's value approximated as an equal share of the short book; a name missing from the file keeps its tier rate).
- The snapshots only start on 2026-10-10. A day with no snapshot on or before it shows "no snapshot".

#### B3.5 Monthly signal from fresh data (`scripts/forward_smisp.py`)

The frozen data v2 end in 2026-08 (prices) and 2026-07-17 (universe weeks), so each forward signal is built from fresh data, once, right after the signal close, and frozen in `state/forward_observation/smisp/s_<date>.csv` plus `_meta.json` (committed: scores, ranks, borrow tiers and tickers, no price levels; later runs reuse it and never recompute it). Raw vendor bodies stay in the local cache (`research_cache/forward_observation/smisp/`, or `.cache/forward_observation/` / `$FORWARD_OBS_CACHE` on GitHub Actions), never in Git. Steps, all imported from the research code unless stated otherwise:

1. **Listing:** a Nasdaq Trader symbol-directory snapshot (`nasdaqlisted.txt`, the source of the frozen listing snapshots) fetched after the s close; common stock = `src.io.security_universe.investable_common_equities` (no ETFs, test issues, warrants, units, rights, preferreds, SPAC-style names...).
2. **Identity:** a symbol that is the ticker of a security in the frozen v2 `weekly_listed` last week (2026-07-17; identity and flags copied once into the committed `state/forward_observation/smisp_frozen_base_2026-07-17.csv`) is that security, with its frozen flags (common stock, foreign filer, investment company; a known security keeps its frozen common-stock flag even when the current Nasdaq name reads "When-Issued", as CEG and SNDK did in the 2026-10-10 file), CIK, SIC (latest frozen top-300 header SIC, else the security master) and multi-class group. Other symbols get a CIK from SEC `company_tickers_exchange.json`: a frozen security under a new ticker (same CIK, old ticker no longer listed) keeps its frozen identity; any other is a **new listing**, classified from its SEC submissions: SIC, foreign filer (the latest periodic report is a 20-F / 40-F, or only 6-K / F-1-type filings), investment company (SIC 6726 or N-2 / N-54A / N-CSR... filings). SPAC shell = SIC 6770 (re-read from SEC submissions for new listings and for frozen ex-SPAC names). A symbol without a CIK is left out.
3. **Prices (owner decision 2026-10-10: Alpaca first, Yahoo fallback):** daily bars of every universe-base name (about 2,400, financials included because they take rank places), from s − 460 days to s. **Alpaca** (`scripts/forward_prices.py`): `GET https://data.alpaca.markets/v2/stocks/bars`, `feed=sip`, `timeframe=1Day`, 200 symbols per request, at most 200 requests a minute (about 10 minutes for the whole universe), read three times: `adjustment=raw` (real close and volume), `adjustment=split` (split-adjusted OHLCV; split ratios = the steps of raw / split, snapped to simple fractions) and `adjustment=all` (split and dividend adjusted close: the total return). This matches the frozen code's convention: the research panel uses the real close for the $10 test, whole shares and market caps, real close × real volume for dollar volume, and a dividend-inclusive total-return index for momentum, volatility and daily marking. The bars are written as Yahoo-format charts, so the loaders are unchanged. **Yahoo** v8 charts only for the symbols Alpaca does not return (at most one request every 2 s, stop at 401/403/429, resumable). Keys: `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` from the environment (GitHub secrets) or `.env.alpaca`; never printed.
4. **Universe:** as the universe builder: week end = the last session of the latest calendar week ending on or before s; week close = the last close at most 5 sessions before it; dv50 = median of daily dollar volume over the last 50 sessions (at least 25 rows); rank descending (ties by security ID) among base names with a week close ≥ $10 and a dv50; top 300 with a close at s (at most 5 sessions stale); then `research_fundamentals.universes` (no financials, one class per company).
5. **Fundamentals and market cap:** SEC companyfacts of the U300 CIKs fetched after s (≤ 2 requests a second, User-Agent from `src/io/sec_contact.py`), facts filed strictly before s only; then `facts_from_payload`, `eps_states`, `company_states`, `market_caps` (shares × price, public float, company lists; `SHARES_VS_FLOAT` off as in the panel build), `reject_small_mcaps`, `asof_states`, `market_factors`, `composites`, and `research_ml_cross_section.price_features` (12-1 momentum, 3-month volatility).
6. **Scores and order:** `research_short_overlay.signal_scores` (S-MISP composite, borrow tiers), then `loser_orders` / `schedule` / `simulate` in the runner.
7. **Daily simulation:** the charts of every name that was ever in a worst-20 list are re-downloaded each run (Alpaca first, Yahoo fallback; `smisp/held/`); EFFR from the NY Fed CSV (same format as the research input, `smisp/EFFR_nyfed.csv`), re-fetched each run.

**Validation before any forward data (2026-10-10).** The whole pipeline was run on fresh data for the last frozen signal, **2026-07-31**, and compared with the frozen v2 panel (scratch run, not kept):

- Alpaca vs the frozen v2 price panel (267 frozen-U300 names, 2025-04-01..2026-07-31, 82,898 name-days): daily total return within 1 bp on 95.6% and within 10 bp on 99.98% of name-days (median |difference| 5e-8); real close within 10 bp on 99.2%.
- Alpaca vs Yahoo (2,374 names, 724,206 name-days): total return within 10 bp on 97.3% (median 4e-8; the larger gaps are mostly small caps); real close within 10 bp on 97.5%; dollar volume within 10% on most days (different volume consolidation). QQQ: total return within 1 bp on 97% of days 2016–2026. Rare single-day close differences remain (e.g. TSLA 2020-12-18, the S&P inclusion close: 5%).
- Universe at the same week (2026-07-17): 293 of the frozen top 300 reproduced (rank correlation 0.9996). The 7 differences are names delisted after July (EA, KHC, QRVO, WBD; not in the 2026-10-10 Nasdaq file, which a real forward run takes right after the signal) and a few rank-300 boundary names.
- Signal at 2026-07-31: U300 262 names fresh vs 271 frozen (the frozen panel used the 07-17 universe week, the fresh run the 07-31 week as the rule says; plus the delisted names); on the 256 common names the S-MISP score rank correlation is 0.9996, the accounting anomaly inputs are identical (1e-6) for 99.6% of names and momentum / Altman Z are rank-identical (correlation ≥ 0.9995); borrow tier agreement 98.8%. **Worst 20: 19 of 20 the same, worst 10: 9 of 10**; the one difference (RUN) closed at $9.81 in the 07-31 week, below the $10 rule.
- Runtime: about 11 minutes including SEC (about 400 requests at 2 a second).

#### B3.6 Inputs that cannot be refreshed faithfully (documented 2026-10-10, before any forward data)

For each gap the most faithful option was chosen now and is not changed later:

1. **Security-master flags of known names** (foreign filer by week, investment company spans, Form 25 dates) are frozen at 2026-07-17 and not rebuilt; only listing (snapshot), the common-stock name test and SPAC status (SIC 6770) are fresh. A known name that becomes a foreign filer later stays in.
2. **New listings** are classified from their latest SEC filings only, not with the builder's full rules (MIXED foreign regimes week by week, N-54A / N-54C spans, the "young" and boundary-trim rules). A new listing without 25 rows has no dv50 and is not ranked, as in the builder.
3. **Listing timing:** one Nasdaq snapshot fetched after the signal close, instead of snapshot runs with a two-missing-snapshot tolerance. A name delisted between s and the snapshot is missed, so the run should be done within a few days after the month end. If no snapshot after the close exists the runner fetches one; an older one is never used for a new signal.
4. **Prices** come from Alpaca (Yahoo for the few names Alpaca lacks), not the canonical v2 panel (a merge of WIKI / Tiingo / Yahoo / stored files with split restores and reviewed fixes). Vendor bars are taken as given (validation above).
5. **SIC:** known names keep the frozen SIC (latest top-300 header, else security master); `sic_history` is not refreshed except where SEC submissions are read (new listings, SPAC checks).
6. **Companyfacts:** one fresh copy per company (the frozen build merged every local copy), so a fact that SEC later dropped from companyfacts is lost; only facts filed strictly before s are used.
7. **Market-cap fallbacks:** the frozen company lists (to 2026-08) age out after 365 days; the predecessor-CIK share-count link (`successor_ciks`) is not extended to new successors. Shares × price from fresh companyfacts is the main source, as in the panel.
8. **Terminal values:** the forward data have no delisting terminal returns. A short whose Yahoo rows stop before the last session is covered at its last value on the next session (0% terminal return, the research rule for a delisting without a terminal record). Yahoo often deletes delisted tickers, so a failed download keeps the previous file.
9. **Ticker changes of a held name** during a month: the chart is fetched by the ticker at the signal; if it stops, the short is covered at its last value (flagged by the stop in the data).
10. **IBKR BM** is approximated by EFFR, as registered.
11. **Borrow-fee snapshots** exist only where `scripts/record_borrow_fees.py` runs (launchd on the owner's Mac; `$FORWARD_BORROW_DIR`). A GitHub Actions run without them shows "no snapshot" in the report-only fee columns; the strategy, the evaluation and the other columns do not depend on them.

These gaps make the forward universe and data close to, but not identical with, data v2. They are not reasons to change the rule.

#### B3.7 Monthly steps

1. **When:** after 18:00 New York time on the first trading day of the new month (the signal is the previous month's last session; it is executed at that first day's close), as in B0.
2. **Run:**

   ```
   PYTHONPATH=. .venv/bin/python scripts/forward_observation.py --fetch --fetch-smisp
   ```

   - `--fetch-smisp` computes any due S-MISP signal (Nasdaq snapshot, SEC tickers / submissions / companyfacts, about 2,400 charts from Alpaca with Yahoo for the rest: about 10–15 minutes), then re-downloads the held names' charts and the EFFR. If a source stops (Yahoo 401/403/429, an SEC or network error) or a chart is still missing, run the same command again: it resumes, and the signal is computed only when every chart is present or known to have no data.
   - Raw files stay in the local cache (never in Git). The signal of a month is frozen in `state/forward_observation/smisp/s_<date>.csv` with `s_<date>_meta.json` (counts: universe base, ranked, U300, scored, chart sources, new listings in U300, names without companyfacts or market cap, worst 20). **Commit these two files** after the run (they hold no price levels); a GitHub Actions run reads them.
   - GitHub Actions needs the secrets `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`, `TIINGO_API_KEY` and `SEC_USER_AGENT` (the SEC contact string).
3. **Check the console:** the signal summary (U300 about 270–280 names and S-MISP coverage about 90–99% in sample) and the `B3:` line (chained strategy, QQQ, ONEQ, costs, number of IBKR snapshots).
4. **Output:** the B3 table of `docs/forward_observation_log.md`, one row per month: the month's shorts (tickers), strategy, QQQ, ONEQ, excess vs ONEQ, the short leg and short leg − QQQ, the IBKR fee summary (mean / max, unborrowable or missing names), the strategy at IBKR fees (report only), cumulative excess vs ONEQ, t and status. Returns in percent and tickers only, no price levels.
5. **Don't:** recompute a frozen signal file, change the rule, the universe steps or the fee tiers, or trade it.
