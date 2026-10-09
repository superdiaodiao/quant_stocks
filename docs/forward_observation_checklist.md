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

## B. Candidates — observation started (owner decision 2026-10-09)

**Decision (2026-10-09):** the owner chose to observe both candidates, with the rules frozen exactly as below. B2 is recorded as two separate lines, SEL-A (config 29876) and SEL-P (config 29916).

- **Forward window:** data after the 2026-10-09 close. The first possible trade is at the 2026-10-12 open. Accounts start at the 2026-10-12 close.
- **Recording:** each month, append the month's T-trades and returns for the strategy, H_base, H_match and ONEQ to `docs/forward_observation_log.md`.
- **Evaluation (fixed now, before any forward data):**
  - The main comparison is H_match.
  - There is no verdict before 24 months.
  - At 36 months the verdict is positive only if the cumulative excess over H_match is > 0 **and** the monthly-excess t is ≥ 2.
  - These are small-edge candidates, so there is no early abandon rule. They are observed, not traded.

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
