# Forward observation log: B1 S3-Yb, B2 SEL-A / SEL-P

Written by `scripts/forward_observation.py` (frozen rules: `docs/forward_observation_checklist.md` section B; owner
decision 2026-10-09). Do not edit the tables by hand: each run recomputes every forward month from the start and
replaces the rows of the months it computes (one row per month, never duplicated). Returns in percent and dates
only; no vendor price levels.

**Forward window.** In-sample data end at the 2026-10-09 close. The accounts open at the **2026-10-12 close** (base
bought at that close, $10k per account); the first forward return session is the next session. Under the frozen
simulators a signal is read at a close and acted on at the next open, so the earliest T-entry is the open after the
first forward close.

**Columns.**
- *month*: calendar month (New York sessions); *n*: month count since the start (2026-10 = 1, a partial month).
- *T-trades*: trades that closed in the month (`entry→exit` as MM-DD, net return of the traded shares after costs,
  `tgt` = limit / OPP exit, `time` = time stop) and trades still open at the month's last session (`MM-DD→open`).
  B1 lists the symbol. A trade's net return = its P&L after both orders' costs / the entry value of the traded
  shares (as the research trip statistics).
- *strategy*: the month's return of the account (B1: equal-weight mean of the 18 accounts' daily returns, chained).
- *H_base*: the same account (same base, reserve, month-end resets, costs) with no T-trading. Report only.
- *H_match*: same average exposure (the main comparison). Month m uses w = the mean daily exposure the frozen
  simulator reports for the account run from the start through the last forward session of month m (exposure
  realised to date; no later data). H_match's daily return = w × the instrument's daily total return (B1: per
  account, basket mean). *w* is that exposure (B1: mean over the 18 accounts).
- *ONEQ*: buy and hold, bought at the 2026-10-12 close with one order (IBKR Tiered + 2 bp). Report only.
- *excess vs H_match*: strategy − H_match for the month, in percentage points.
- *cum. excess vs H_match*: (strategy chained from the start) − (H_match chained from the start), percentage points.
- *t*: t of the monthly excess vs H_match from the start through the month (mean / sd × √n), shown from n = 3.
- *sessions*: first..last forward session of the month in the data (count).
- *status*: `final` once the data hold a session of a later month; otherwise `provisional`. The frozen simulators
  treat the window's last session specially (an open T-trade is closed at that close with its costs, no month-end
  reset, no new signal), so the month holding the last session of the data is recomputed by the next run. A
  provisional row is replaced by the next run; a final row should never change (the runner warns if it does).

**Evaluation (fixed 2026-10-09).** Main comparison H_match. No verdict before 24 months. At 36 months the verdict is
positive only if the cumulative excess over H_match is > 0 and the monthly-excess t is ≥ 2. No early abandon rule;
observed, not traded.

## Status

- As-of date of the last run: 2026-10-08; data through: 2026-10-08.
- **observation starts 2026-10-12** (accounts open at the 2026-10-12 close). No forward sessions yet, so no trades and no returns.

## B1 S3-Yb (U18 basket)

Rule: config `S3-Yb`, stock parameters (`scripts/research_selective_t.py`).

<!-- table:B1:start -->
| month | n | T-trades | strategy | H_base | H_match | w | ONEQ | excess vs H_match (pp) | cum. excess vs H_match (pp) | t | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
<!-- table:B1:end -->

## B2 SEL-A (QQQ, config 29876)

Rule: config 29876 `RSI2 p2 B OPP H10 1/2 res25 ALL` (`scripts/research_t_grid.py`).

<!-- table:SEL-A:start -->
| month | n | T-trades | strategy | H_base | H_match | w | ONEQ | excess vs H_match (pp) | cum. excess vs H_match (pp) | t | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
<!-- table:SEL-A:end -->

## B2 SEL-P (QQQ, config 29916)

Rule: config 29916 `RSI2 p2 B OPP H20 1 res25 ALL` (`scripts/research_t_grid.py`).

<!-- table:SEL-P:start -->
| month | n | T-trades | strategy | H_base | H_match | w | ONEQ | excess vs H_match (pp) | cum. excess vs H_match (pp) | t | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
<!-- table:SEL-P:end -->
