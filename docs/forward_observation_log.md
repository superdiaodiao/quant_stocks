# Forward observation log: B1 S3-Yb, B2 SEL-A / SEL-P, B3 S-MISP

Written by `scripts/forward_observation.py` (frozen rules: `docs/forward_observation_checklist.md` section B; owner
decisions 2026-10-09 for B1 / B2 and 2026-10-10 for B3). Do not edit the tables by hand: each run recomputes every forward month from the start and
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

The B3 table (S-MISP short overlay) has its own columns and evaluation; they are described above that table.

## Status

- As-of date of the last run: 2026-10-26; data through: 2026-10-26.
- B1 S3-Yb (U18 basket): 1 month(s) through 2026-10; cumulative excess vs H_match -0.05 pp; no verdict before month 24, verdict at month 36.
- B2 SEL-A (QQQ, config 29876): 1 month(s) through 2026-10; cumulative excess vs H_match +0.07 pp; no verdict before month 24, verdict at month 36.
- B2 SEL-P (QQQ, config 29916): 1 month(s) through 2026-10; cumulative excess vs H_match +0.07 pp; no verdict before month 24, verdict at month 36.
- B3 S-MISP short overlay (N=10, k=20%, MN): **observation starts at the 2026-10-30 month-end signal** (the last session of October 2026), executed at the next close; no forward month yet.

## B1 S3-Yb (U18 basket)

Rule: config `S3-Yb`, stock parameters (`scripts/research_selective_t.py`).

<!-- table:B1:start -->
| month | n | T-trades | strategy | H_base | H_match | w | ONEQ | excess vs H_match (pp) | cum. excess vs H_match (pp) | t | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10 | 1 | INTC 10-14→open; AMD 10-19→open; META 10-23→open | -1.74% | -1.58% | -1.69% | 0.745 | -3.95% | -0.05 | -0.05 |  | 10-13..10-26 (10) | provisional |
<!-- table:B1:end -->

## B2 SEL-A (QQQ, config 29876)

Rule: config 29876 `RSI2 p2 B OPP H10 1/2 res25 ALL` (`scripts/research_t_grid.py`).

<!-- table:SEL-A:start -->
| month | n | T-trades | strategy | H_base | H_match | w | ONEQ | excess vs H_match (pp) | cum. excess vs H_match (pp) | t | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10 | 1 | 10-19→10-22 -0.89% tgt | -3.59% | -3.40% | -3.66% | 0.806 | -3.95% | +0.07 | +0.07 |  | 10-13..10-26 (10) | provisional |
<!-- table:SEL-A:end -->

## B2 SEL-P (QQQ, config 29916)

Rule: config 29916 `RSI2 p2 B OPP H20 1 res25 ALL` (`scripts/research_t_grid.py`).

<!-- table:SEL-P:start -->
| month | n | T-trades | strategy | H_base | H_match | w | ONEQ | excess vs H_match (pp) | cum. excess vs H_match (pp) | t | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10 | 1 | 10-19→10-22 -0.89% tgt | -3.59% | -3.40% | -3.66% | 0.806 | -3.95% | +0.07 | +0.07 |  | 10-13..10-26 (10) | provisional |
<!-- table:SEL-P:end -->

## B3 S-MISP short overlay (N=10, k=20%, MN)

Rule: configuration S-MISP N=10 k=20% MN of `docs/research_ledger_short_overlay.md` section 0 (frozen code
`scripts/research_short_overlay.py`; fresh monthly signal from `scripts/forward_smisp.py`; checklist section B3).
Observed, not traded.

**Forward window.** In-sample data end at the 2026-10-09 close. Signal at the last session of each month, executed at
the next session's close: the first signal is the 2026-10-30 close, the account ($10k) opens at the 2026-11-02 close,
so month 1 is the partial 2026-11 measured from that close (month 24 = 2028-10, month 36 = 2029-10).

**Columns.** *shorts*: the names held after the rebalance at the close of the month's first session (whole shares,
the research simulator). *strategy*: the month's return of the simulated account (registered tiered borrow fee;
research `month_returns`, the first month from the first trade's close). *QQQ*, *ONEQ*: total return over the same
days (ONEQ is the judged benchmark). *excess vs ONEQ*: strategy − ONEQ. *short leg*: the research ideal equal-weight
loser basket (no rounding, no costs) from the month's trade close to the next trade close (dates shown), and
*short leg − QQQ* over the same days (negative is good for a short). *IBKR fee*: report only; each short's FEERATE
from the latest IBKR snapshot on or before each calendar day of that holding period: mean over the names of each
name's mean, and the maximum (% a year); names with AVAILABLE = 0 or missing from the file are listed.
*strategy at IBKR fees*: report-only sensitivity, the strategy with the IBKR fee instead of the registered tier
(each short's value approximated as an equal share of the short book). *cum. excess vs ONEQ*: (strategy chained
from the start) − (ONEQ chained from the start), percentage points. *t*: monthly-excess t vs ONEQ (shown from
n = 3). *status*: `final` once the data hold the next month's trade session; else `provisional`.

**Evaluation (fixed 2026-10-10).** No verdict before 24 months. At 36 months positive only if the cumulative excess
over ONEQ is > 0 and the monthly-excess t vs ONEQ is ≥ 2. QQQ and the short leg vs QQQ are reported alongside.
The registered tiered borrow fee is the rule; the IBKR fees are report only.

<!-- table:B3:start -->
| month | n | shorts (after the month's rebalance) | strategy | QQQ | ONEQ | excess vs ONEQ (pp) | short leg (holding period) | short leg − QQQ (pp) | IBKR fee mean / max (%/yr) | unborrowable / not in IBKR file | strategy at IBKR fees | cum. excess vs ONEQ (pp) | t | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
<!-- table:B3:end -->

## B4: seven candidates added 2026-10-24

Owner decision 2026-10-24 (`docs/forward_observation_checklist.md` section B4): M1, M2, M3, M6
(`docs/research_ledger_megacap.md` 0.4), LEV-B3 (`docs/research_ledger_sector_lev.md` B3; the S-MISP line above
keeps the name B3), V3 (`docs/research_ledger_voltarget.md`) and BT4b (`docs/research_ledger_bitcoin.md`), with the
rules frozen as registered (`quant/strategies/registry.py`), simulated by the research code
(`quant/observation/b4.py`). Observed, not traded. Returns in percent, tickers and ratios only; no price levels.

**Forward window.** Data after the 2026-10-23 close. LEV-B3 and V3 open at the **2026-10-26 close** ($10k, traded to
the target in force: B3's SMA state at the 10-23 close, V3's exposure decided at the 2026-09-30 month end); month 1
is the partial 2026-10 (month 24 = 2028-09, month 36 = 2029-09). The monthly lines take their first signal at the
**2026-10-30 month end** and open at the next close (2026-11-02, $10k); month 1 is the partial 2026-11 (month 24 =
2028-10, month 36 = 2029-10). Months are calendar months, the first one measured from the opening close.

**Columns.** *strategy*: the month's return of the simulated account (research engines, IBKR costs). *ONEQ*, *QQQ*:
buy and hold from the same opening close (one order, research costs). *excess*: strategy − benchmark (pp).
*cum. excess*: (strategy chained from the start) − (benchmark chained from the start), pp. *t*: monthly-excess t
(mean / sd × √n, shown from n = 3). *status*: `final` once the line itself holds a session of a later month (a
mega-cap line waits for its month-end signal file, BT4b for the BTC close of the session, i.e. 00:00 UTC);
otherwise `provisional`, replaced by the next run. Matched comparisons: LEV-B3 vs **constant 1.37x** (63% QQQ +
37% QLD, monthly; the audit's benchmark); V3 vs **the constant portfolio at V3's average realised exposure ē to
date** (month m uses ē from the start through m's last session); BT4b vs QQQ alone; the mega-cap lines are fully
invested, their matched comparison is QQQ.

**Evaluation (fixed 2026-10-24, before any forward data).** No verdict before 24 months. At 36 months a line is
positive only if its cumulative excess over ONEQ is > 0 **and** its monthly-excess t vs ONEQ is ≥ 2. The matched
comparison is reported alongside and decides whether there is any edge beyond leverage / exposure: only with a
cumulative excess > 0 and t ≥ 2 against it too; otherwise a positive result vs ONEQ is recorded as exposure, not an
edge. No early abandon rule. 36 months has low power for these high-tracking-error lines (checklist B4).

### B4 status

- As-of date of the B4 run: 2026-10-26.
- B4 M1 mega-cap top 10, equal weight: **observation starts at the 2026-10-30 month-end signal**, executed at the next close; no forward month yet.
- B4 M2 mega-cap top 5, equal weight: **observation starts at the 2026-10-30 month-end signal**, executed at the next close; no forward month yet.
- B4 M3 mega-cap top 20, best 5 by 6-month momentum: **observation starts at the 2026-10-30 month-end signal**, executed at the next close; no forward month yet.
- B4 M6 mega-cap top 10, cap-weighted: **observation starts at the 2026-10-30 month-end signal**, executed at the next close; no forward month yet.
- B4 LEV-B3 QQQ SMA200 1.5x / 1.0x (sector_lev B3): **opens at the 2026-10-26 close**; no forward month yet.
- B4 V3 QQQ volatility target (long-run vol / 20-day vol, cap 1.5x): **opens at the 2026-10-26 close**; no forward month yet.
- B4 BT4b 90% QQQ + 10% BTC, monthly: **observation starts at the 2026-10-30 month-end signal**, executed at the next close; no forward month yet.

### B4 M1 mega-cap top 10, equal weight

Top 10 Nasdaq common stocks by point-in-time market cap (one class per company), equal weight. Signal at each month's last session from fresh data (Nasdaq listing after the close, SEC filings before it, Alpaca / Yahoo prices; `state/forward_observation/megacap/m_<date>.csv`), traded at the next close; 25% rebalance band, IBKR costs. *holdings*: the targets executed in the month.

<!-- table:M1:start -->
| month | n | holdings after the month's rebalance | strategy | ONEQ | QQQ | excess vs ONEQ (pp) | excess vs QQQ (pp) | cum. excess vs ONEQ (pp) | t vs ONEQ | cum. excess vs QQQ (pp) | t vs QQQ | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
<!-- table:M1:end -->

### B4 M2 mega-cap top 5, equal weight

Top 5 by point-in-time market cap, equal weight. Signal at each month's last session from fresh data (Nasdaq listing after the close, SEC filings before it, Alpaca / Yahoo prices; `state/forward_observation/megacap/m_<date>.csv`), traded at the next close; 25% rebalance band, IBKR costs. *holdings*: the targets executed in the month.

<!-- table:M2:start -->
| month | n | holdings after the month's rebalance | strategy | ONEQ | QQQ | excess vs ONEQ (pp) | excess vs QQQ (pp) | cum. excess vs ONEQ (pp) | t vs ONEQ | cum. excess vs QQQ (pp) | t vs QQQ | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
<!-- table:M2:end -->

### B4 M3 mega-cap top 20, best 5 by 6-month momentum

Among the top 20 by market cap, the 5 with the best 126-session total return, equal weight. Signal at each month's last session from fresh data (Nasdaq listing after the close, SEC filings before it, Alpaca / Yahoo prices; `state/forward_observation/megacap/m_<date>.csv`), traded at the next close; 25% rebalance band, IBKR costs. *holdings*: the targets executed in the month.

<!-- table:M3:start -->
| month | n | holdings after the month's rebalance | strategy | ONEQ | QQQ | excess vs ONEQ (pp) | excess vs QQQ (pp) | cum. excess vs ONEQ (pp) | t vs ONEQ | cum. excess vs QQQ (pp) | t vs QQQ | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
<!-- table:M3:end -->

### B4 M6 mega-cap top 10, cap-weighted

Top 10 by market cap, weighted by market cap (a sanity line: should track QQQ). Signal at each month's last session from fresh data (Nasdaq listing after the close, SEC filings before it, Alpaca / Yahoo prices; `state/forward_observation/megacap/m_<date>.csv`), traded at the next close; 25% rebalance band, IBKR costs. *holdings*: the targets executed in the month.

<!-- table:M6:start -->
| month | n | holdings after the month's rebalance | strategy | ONEQ | QQQ | excess vs ONEQ (pp) | excess vs QQQ (pp) | cum. excess vs ONEQ (pp) | t vs ONEQ | cum. excess vs QQQ (pp) | t vs QQQ | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
<!-- table:M6:end -->

### B4 LEV-B3 QQQ SMA200 1.5x / 1.0x (sector_lev B3)

QQQ adjusted close vs its 200-session SMA with a 2% band: on (above 1.02 × SMA) → 50% QQQ + 50% QLD (1.5x), off (below 0.98 × SMA) → 100% QQQ; traded at the close after a state change and after each month end. *SMA200 state*: at the month's last session; in parentheses the switches decided in the month (→on / →off at that close, traded at the next close).

<!-- table:LEV-B3:start -->
| month | n | SMA200 state | exposure (mean held) | strategy | constant 1.37x | excess vs 1.37x (pp) | ONEQ | QQQ | excess vs ONEQ (pp) | cum. excess vs ONEQ (pp) | t vs ONEQ | cum. excess vs 1.37x (pp) | t vs 1.37x | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
<!-- table:LEV-B3:end -->

### B4 V3 QQQ volatility target (long-run vol / 20-day vol, cap 1.5x)

e = min(1.5, σLR / σ20) at each month end (σLR: std of every QQQ daily return since 1999-03-11, σ20: the last 20, both × √252); e ≥ 1 → (2 − e) QQQ + (e − 1) QLD, e < 1 → e QQQ + T-bill ETF. *target e*: decided at the previous month end (traded at the month's first close; the first month: the target in force at the opening close) → decided at this month's end.

<!-- table:V3:start -->
| month | n | target e in force → next | exposure (mean held) | strategy | constant ē | ē to date | excess vs constant ē (pp) | ONEQ | QQQ | excess vs ONEQ (pp) | cum. excess vs ONEQ (pp) | t vs ONEQ | cum. excess vs constant ē (pp) | t vs constant ē | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
<!-- table:V3:end -->

### B4 BT4b 90% QQQ + 10% BTC, monthly

90% QQQ + 10% BTC (Bitstamp close of UTC day S on US session S; IBKR spot crypto costs, Paxos $0.15 a month), rebalanced at the close after each month end. *IBIT variant*: the BTC leg as IBIT (stock costs), report only.

<!-- table:BT4b:start -->
| month | n | BTC weight (mean held) | BTC | strategy | ONEQ | QQQ | excess vs ONEQ (pp) | excess vs QQQ (pp) | IBIT variant (report only) | cum. excess vs ONEQ (pp) | t vs ONEQ | cum. excess vs QQQ (pp) | t vs QQQ | sessions | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
<!-- table:BT4b:end -->
