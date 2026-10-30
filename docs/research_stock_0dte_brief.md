# Single-stock same-day-expiry (0DTE) options: research brief for pre-registration

*QuantConnect/AlgoSeek minute quotes · US$10k IBKR Pro Tiered account · 2026-10-30*

This brief feeds the draft ledger [L-s0], which is not yet frozen. It fills that ledger's open placeholders: §0.1 public facts, §0.3 item 8 fees, §0.6.6 comparators, §0.6.7 calibration and §0.6.8 day-trading limit. It combines six topic sweeps and their adversarial checks. Where a check corrected or refuted a claim, the brief uses the corrected version and marks it **(corrected)** or **(refuted)**.

**Confirm the scope first.** The owner wrote 「这个都不用测试，我想测试的是股票」 ("no need to test that; what I want to test is stocks"). The workflow and the draft ledger read this as same-day options *on individual stocks*. It could also mean *the shares themselves*. Items tagged **[stock-only]** (§3.2, §4.1 F) can be tested on stock bars, with no option spreads, fees or margin.

**Tags.** Confidence: H / M / L. Evidence:
- R: read on the cited page by a sweep or its check.
- S: secondary summary or search snippet.
- I: my inference or arithmetic.
- P: this project's own probe or ledger.
- U: not found or unverified.

The shared web-search budget ran out before this synthesis, and IBKR pages return HTTP 403. Nothing new could be checked at this step. A follow-up message can resume searching.

## 0. What changes the design

1. **Calendar (H).** From 2012-02 to 2026-01-30, a single stock had a same-day expiry **only on Fridays**, or on the Thursday before when Friday was an exchange holiday. From 2026-02-02, a quarterly list of mega-caps also gets **Monday and Wednesday** expiries. No single stock has Tuesday/Thursday or daily expiries. In QC data, the top-10 names by dollar volume had a same-day contract on 92–100% of the Fridays they were members, in every year since 2012. The earlier claim that weeklies were rare before 2014 is **refuted** (§1.1).
2. **Sample end (M).** The last visible QC bar is 2026-07-30, exactly 90 days before the probe date. That matches QC's organization-level out-of-sample holdout, not a stale feed **(corrected)**. Check the setting, and write a fixed end date into the pre-registration (§6).
3. **Exit (M).** Three things keep the draft's 15:50 flatten as the main rule: physical settlement, the 4:00–5:30 pm exercise window, and IBKR's discretionary liquidation before expiry (2–3 pm ET in forum anecdotes). Add an earlier-flatten sensitivity for any leg that would deliver shares (§1.3, §2.3).
4. **Fees (U).** The draft charges $0.65/$0.50/$0.25 per contract plus $0.05. These rates could not be re-read (IBKR 403). The **$1.00 per-order minimum binds on 1-lot single-leg trades**: a lotto option under $0.05 pays $1.00 per side, not $0.25. Apply the minimum to single-leg comparators, not only in the 1-lot replay (§2.1).
5. **Day-trading rule (H; IBKR status U).** FINRA removed the pattern-day-trader rule and its $25k minimum, effective 2026-06-04; firms may phase it in until 2027-10-20. Whether IBKR has adopted it was **not found**. Report both regimes, and name the deciding one before running (§2.4).
6. **Margin (H).** Reg T would allow one naked at-the-money put on NVDA (≈$4.3k) or SPCX (≈$2.9k). But an assignment delivers $14k–57k of stock, and gap losses are uncapped. Cash-secured puts fit none of the Monday/Wednesday names. Defined-risk spreads and long options are the only structures that scale, which is what the draft's K10 set contains (§2.2).
7. **Prior (M).** No study of returns on single-stock same-day options was found. Delta-hedged equity-option losses accrue **overnight**: −0.4% overnight vs +0.1% intraday per day (mid quotes, 2004–2013). Pinning on weekly Fridays is ~1 point more frequent than on other days, and end-of-day effects are a few bp. Expect a null intraday result after costs (§3).
8. **Costs (H).** On short-dated options traded by retail, quoted spreads average 12.6% of mid (6.6% effective). The QQQ study already found the seller's premium ≈ a small account's execution cost. Measure spreads by name and time of day from AlgoSeek, and keep touch fills as the deciding case (§3.1).
9. **Comparators.** Add four comparators that mirror the QQQ study, plus:
   - an opening-range breakout;
   - the Augen pin ratio fly;
   - an earnings-day iron fly sized for K10;
   - a Chinese "buy the crushed lotto ticket" rule.
   Also decide explicitly whether to add a one-night (Thursday→Friday) arm. Overnight is the only place the literature finds the premium (§4.2).
10. **Calibration (M).** No live fund reproduces 1-lot single-stock 0DTE fills: they use FLEX and swap legs, pick strikes by discretion, and are tiny or closed. Calibrate fills from where AlgoSeek trades print relative to quotes. Under draft §0.6.7, the conservative-fill case must also pass (§5).
11. **QC terms (H).** Terms v1.5 (2026-10-08) §2.6(c) now names charts, plots, logs and runtime statistics as banned export channels, and calls bypassing output limits a breach. That is more explicit than the v1.4 clause the owner accepted on 2026-10-05, and this project exports through charts. This is the owner's decision (§6).

## 1. Market structure that fixes the test calendar

### 1.1 Same-day expiries for single stocks, by period

| Period | Single-stock same-day expiries | Names | Conf. | Source |
|---|---|---|---|---|
| 2005-10-28 → mid-2010 | none (weeklys only on SPX/OEX, later XSP/XEO) | — | H | [cboe2009], [rhoads] R |
| end-May or 2010-06-25 → 2011 | Friday weeklys on a few stocks; ≤5 classes per exchange until Feb 2011 | few | M | [rhoads], [zcc2015], [ise2011] R |
| 2012-02-03 → 2026-01-30 | **Fridays only** (weekly or third-Friday monthly); the Thursday before when Friday is a holiday | top-10 by dollar volume: same-day contract on 92–100% of member-Fridays each year; ranks 11–20: 98–100%; all 744 member-Thursdays were holiday shifts | H | probe v2 [L-s0] P; [ise2011] R |
| 2026-02-02 → 2026-06-30 | Fri + **Mon, Wed** | AAPL, AMZN, AVGO, GOOGL, META, MSFT, NVDA, TSLA | H | [ota], [miaxQ1], [miaxQ2], [box005] R |
| Q3 2026 (in QC data from 2026-07-13) | Fri + Mon, Wed | the above + AMD, INTC, MU | H | [miaxQ3], [box064], [fr17460] R; [L-s0] P |
| Q4 2026 | Fri + Mon, Wed | AAPL, AMD, AMZN, AVGO, GOOGL, META, MSFT, MU, NVDA, SPCX, TSLA. INTC dropped, SPCX added **(corrected: earlier sweep said "not found")** | H | [miaxQ4] R |
| any date | no Tue/Thu or daily single-stock expiries; Tue/Thu exist only for Tier-1 ETFs, since 2026-08-18 | — | H | [sec106100], [miaxAug] R |

All other P10/P20 names in the draft (BAC, NFLX, PLTR, BABA, JPM, …) stay Friday-only in 2026 (I, from the lists above).

**Friday rule.** A weekly series expires on Friday, or "on the first business day immediately prior" if Friday is not a business day [ise2011]. Today up to five Friday weeklies may be listed at once. No weekly is listed on a Friday that already has a standard, monthly or quarterly expiry [iseA1]. R, H.

**Saturday-dated monthlies.**
- Standard monthlies officially expired at 11:59 pm ET on the Saturday after the third Friday; trading ended on Friday.
- OCC filing SR-OCC-2013-04 moved standard contracts expiring after 2015-02-01 to Friday. Some contracts listed before the change kept Saturday dates as far out as Dec 2016 [occ2013].
- LEAN maps these Saturday dates back to the prior trading day [leanSym]. R, H.
- So define a 0DTE day as *last trading day = today*. Testing *expiry = today* made the QQQ probe miss 2013-03-15 [L-zero] (P).

**Holidays and early closes.**
- 2026 Friday closures are Apr 3, Jun 19, Jul 3 and Dec 25, so those weeks expire on Thursday. Early closes at 1:00 pm fall on Fri Nov 27 and Thu Dec 24 [nyse].
- On early-close days the exercise cut-off is the close + 1 h 30 min; for example, 2:30 pm ET on 2025-11-28 [finra2025].
- A holiday Monday series expires the next business day; a holiday Wednesday series expires the prior business day. Both land on Tuesday [sec106100]. The probe saw a Tuesday expiry on 2026-05-26 [L-s0]. R/P, H.

**Monday/Wednesday program** (SR-ISE-2025-15). Filed 2025-05-01; Amendment No. 1 filed 2025-07-01; approved 2026-01-16 as Rel. 34-104624, 91 FR 2806 [sec104624]. R, H.
- **Eligibility**, measured at the prior quarter-end:
  - market cap above $700bn (ETFs: AUM above $50bn);
  - more than 10 million option sides traded in the quarter's last month (OCC data);
  - position limit of at least 250,000 contracts;
  - membership in the Penny Interval Program.
- **Listing.** Up to 2 Monday and 2 Wednesday expiries beyond the current week. Cboe's file on 2026-10-30 lists Mondays 11/02 and 11/09 and Wednesdays 11/04 and 11/11 [weeklys].
  - None on a day that already has a standard, monthly or quarterly expiry.
  - **None on a day with an after-close Earnings Announcement**, meaning official SEC-filed quarterly or annual results; guidance and pre-announcements do not count. The day *after* a report is not excluded.
- **Settlement and strikes.** p.m.-settled and physically settled. Minimum strike intervals: $0.50 below $100, $1 for $100–150, $2.50 above $150. Each exchange may open 30 series per expiry, plus series matching those listed elsewhere [sec103434].
- **List changes.** The list is published by the close of each quarter's first trading day. A name that drops off cannot list new series from the quarter's second day, but strikes already listed trade to expiry [fr17457].
- **Rollout.** Listing began 2026-01-26 on Nasdaq's six options markets [ota] and on Cboe C1/BZX/C2/EDGX [cboeNotice]. The first expiries were Mon 02-02, Wed 02-04 (GOOGL skipped for earnings), 02-09 and 02-11 [firstrade], [miaxQ1].
  - Cboe's matching filing SR-CBOE-2026-007 took effect on filing. Its PDF was created 2026-01-20, so "filed on the approval day" is doubtful **(corrected)** [cboe2026].
- **Third-party lists are wrong.** my0dteoptions listed AMD and MU in April 2026; SpotGamma's 2026-08-18 guide still says Fridays only [my0dteList], [spotgamma]. Take expiries from exchange notices and the data.

**Point-in-time universe.**
- The weeklys pilot was approved 2005-07-12 with 5 classes. Its series cap started at **5**, not 20 as first stated **(corrected)**: 5, then 7 (Aug 2006), then 20 (Oct 2008). First weeklys listed 2005-10-28; the program became permanent 2009-04-27 [cboe2009], [rhoads].
- Class caps per exchange rose 5 → 15 (Feb 2011) → 30 (Nov 2011) → 50, with 30 series per expiry (Oct 2013). At that point weeklies were 31% of all options volume across exchanges [ise2011], [phlx2013].
- CBOE's list of 2012-05-16 already had AAPL, NVDA, AMZN, MSFT, GOOG, NFLX, BAC and INTC, but not TSLA, FB or AMD [slope] (S, L). TSLA's same-day weekly was its most traded series on 2015-01-23 [schaeffers].
- First weekly date per name: U. Extract it from QC.
- Today's Cboe list has 584 equity + 112 ETP rows (another count gives 586 equity) [weeklys]. Never use it to build history.

**Context (ETFs, not stocks).** SPY/QQQ have had Mon–Thu expiries since Nov 2022 [sec96281]; IWM Mon/Wed since Oct 2021 [miax2021]. With about one 0DTE day per week per stock, the QQQ study's daily results cannot be compared day for day.

### 1.2 Tick sizes and strike grids

**Penny Interval Program.** Made permanent 2020-04-01 (Rel. 34-88532); in force from 2020-07-01. R, H.
- Penny classes: $0.01 below $3, $0.05 at or above $3. Non-penny classes: $0.05 and $0.10 [penny], [cboePenny].
- It started with 363 classes. Annual review uses Jun–Nov volume: top-300 classes with an underlying below $200 join on the first January trading day; classes outside the top 425 leave on the first April trading day [penny].
- The pilot began in 2007 with 13 classes [oic].
- Design (I): classify penny status per name and date from the quotes, as draft rule 1 does. A class above $200 cannot join at the annual review.

**Strike intervals.** R, H.
- Since 2012-08-29, weekly strikes are $0.50 apart below $75 and $1 apart for $75–150 (SR-Phlx-2012-78) [phlx2012].
- Current short-term minimums: $0.50 below $100, $1 for $100–150, $2.50 above $150 [sec104624].
- The 2021 strike-limit matrix applies only to weeklies more than 21 days from listing [bx2021].
- Strikes may be added up to and on the expiration day [cboe2013]. Rel. 34-70685 is a notice of filing, not an approval **(corrected)**.
- Practitioner claim (S, L): near-the-money TSLA 0DTE strikes are $2.50–5 apart [my0dte].

**Design (I).** The draft's K10 cap is a $300 maximum loss per contract. Names above about $300 then fit only W1 wings (the adjacent strike, $2.50 apart); W1% and W2% wings are $5 or more and mostly fail. AMZN and GOOG(L) before their mid-2022 splits had grids of $2.50 or more, often $5–10 (M), so they fall mostly outside K10.

### 1.3 Settlement, exercise and after-close risk

**Basics.** R, H.
- Single-stock options are American-style and physically settled in 100 shares. Trading ends at 4:00 pm ET, or 1:00 pm on early-close days [ethFaq], [nyse].
- OCC automatically exercises anything at least $0.01 in the money at the 4:00 pm close, for all account types. This has applied since the June 2008 expiry; the threshold was $0.05 before [ric2008].
- Holders may submit exercise instructions until 5:30 pm ET; firms may set an earlier cut-off [finra2021]. Members may submit contrary exercise advice for customer accounts until 7:30 pm ET [r2360].
- So a short leg that is out of the money at 4:00 pm can still be assigned.

**After-close strike breaks**, from ISE data [cboe2026], [iseA1]. R; Friday counts read off a chart, M.
- **Non-earnings Mondays, 2022/23/24/25** (2025 partial; days with a break, largest close-to-next-open move):
  - NVDA 1/1/6/5, 6.31% (2025);
  - TSLA 3/1/3/3, 3.21% (2022);
  - AVGO 3/6/9/4, 9.05% (2025);
  - META's largest move: 8.00% (2022).
- **Non-earnings Wednesdays, 2024:** NVDA 15, AVGO 15 (QQQ 16, SPY 7). Largest moves in 2025: AAPL 8.21%, META 6.66%, AVGO 6.65%, NVDA 6.26%.
- **Fridays, 2022–2024:** AAPL 0, META 1, MSFT 1, GOOGL 2, NVDA 2, AMZN 3, AVGO 3, TSLA 4 (IWM ≈15, SPY ≈7).
- ISE estimated 66 extra breaks in 2024 for its 9-name sample (22 Monday, 44 Wednesday).
- The sweeps define "strike break" two ways: a strike between the 4:00 and 5:30 pm prices, or a strike between the close and the next open. Which is right: U.

**Cboe extended hours.** Approved 2026-05-28 (Rel. 34-105569) [cboePR], [ethFaq], [sec105569]. R, H.
- Cboe C1 only, limit orders only, at most 100 classes.
- Sessions: 7:30–9:25 am, and a 4:00–4:15 pm "Curb" session.
- Eligibility over the prior 6 months: options volume of at least 150,000 contracts a day, market cap of at least $50bn, and stock volume of at least 10m shares a day.
- Expiring single-stock options trade until 4:15 pm in Curb, but OCC still uses the 4:00 pm close and the 4:00 pm national best bid and offer (NBBO).
- Launched 2026-10-19 [thetrade] (R; the FAQ itself lists only test classes from 2026-09-21). About 25 stocks + 5 ETFs [moomooEth] (S).
- Not in historical data.

**Earnings.** R, H. The exclusion exists only in the Monday/Wednesday paragraphs of the rule; Friday weeklies have **no** earnings exclusion [iseA1]. So Friday 0DTE exists the day after a Thursday after-close report, and on days with a Friday pre-open report.

**Dividends** (I; general option theory, no source). A short call can be exercised early on the last day before the ex-dividend date. This matters only when that day is the expiry Friday. The draft does not model it; list it among the limitations.

### 1.4 Where the volume is

**0DTE share.** R/S, M.
- 0DTE was 19.6% of US option contracts at the start of 2022 and 28% by the end of 2025 [secRT].
- It was 24.1% in 2025 (21.5% in 2024), about 14m of 60.4m contracts a day.
- SPX 0DTE traded 2.3m contracts a day: 59% of SPX volume but only ~16% of all 0DTE contracts **(corrected: 0DTE volume is not mainly SPX)**. SPX is 6% of all contracts and 37% of premium [tradersMag].
- SIFMA puts single stocks at about 20% of 0DTE volume, but its own tables do not reconcile [sifma].

**Concentration.** R/S, M.
- Monday/Wednesday cycles on the 8 stocks + IBIT reached ~3m contracts a day within days; TSLA + NVDA were over half [cboeQ1].
- In Q2 2026 NVDA and TSLA were each 9% of single-stock option volume and AAPL 3%. Cboe says Monday/Wednesday expiries on these names "approach 6 million contracts per day" [cboeQ2].
- On Friday 2026-07-10, NVDA traded 2.74m same-day contracts (54.5% of its option volume; 56.3% before noon) and TSLA 2.28m (67.7%; 50.6% before noon) [strasmore].

**Who is on the other side.** R, H. Excluding market makers, individual customers were 73.5% of expiry-day volume vs 54.3% on other days; institutions were 6.8% vs 21.8% (Dec 2025) [secRT].

## 2. Account constraints for a US$10k IBKR Pro Tiered account

### 2.1 Fees per contract

| Component | Amount | Status | Source |
|---|---|---|---|
| IBKR Tiered commission (≤10,000 contracts a month) | $0.65 per contract when premium ≥ $0.10; $0.50 for $0.05–0.10; $0.25 below $0.05; **$1.00 minimum per order** | U: page returns 403; rates as used in the QQQ study | [ibkrComm], [L-zero] |
| Exchange fee, customer equity options | Cboe C1: "Free" (code CK; schedule effective 2026-10-08). C1 charges $0.75 (Penny) / $1.25 (non-Penny) on orders it routes elsewhere. Other venues not checked | R (C1); U (others) | [cboeFees] |
| OCC clearing fee, options regulatory fee (ORF), CAT fee | — | U (pages return 403) | — |
| FINRA trading activity fee (sales only) | $0.00279 per contract sold; which version is in force on 2026-10-30 is not confirmed | R, M | [taf] |
| SEC Section 31 fee (sales only) | $27.80 per $1m through 2025-05-13; $0 from 2025-05-14 to 2026-04-03; $20.60 per $1m from 2026-04-04 (≈$0.002 on a $100 sale) | R, H (charged on premium: I) | [sec31a], [sec31b] |
| Exercise or assignment | no IBKR commission | S, L | [ibkb1767] |

- **The draft's model** charges $0.65/$0.50/$0.25 + $0.05 per contract on opening and closing, nothing at expiry, and the $1 minimum only in the 1-lot replay [L-s0]. Nothing verifiable contradicts it. The conservative case's extra $0.30 per contract per leg covers an unknown venue take fee (I).
- **The per-order minimum (I).** One-lot multi-leg orders already exceed $1: a vertical costs $1.30 and a condor $2.60 per side. One-lot single-leg orders do not.
  - A long call costs $1.00 per side, not $0.65.
  - A lotto option under $0.05 costs $1.00, not $0.25. On a $0.03 option ($3 of premium), that is 33% of premium if it expires and 67% if it is sold back.
  - Apply max($1, sum of legs) to every 1-lot single-leg comparator, or report both versions.
- **Example (I).** The Augen 10/30/20 ratio fly is 60 contracts per side, about $78 of commission per Friday round trip. Its claimed profit is about 6% a month on $1,500–2,000 of margin, i.e. $90–120 [so2019].

### 2.2 Margin by structure (FINRA 4210 rule text, R, H [r4210]; IBKR house rules U)

| Structure | FINRA minimum | Fits $10k? (I) |
|---|---|---|
| Long call or put (≤9 months to expiry) | 100% of premium | yes; but K10's $300 cap excludes at-the-money options on names above ~$300 |
| Debit vertical | the net debit | yes |
| Credit vertical, iron condor or iron fly | the lesser of the naked requirement and the maximum loss (intrinsic values netted at every strike); long legs paid in full; the short leg may not expire after the long leg | yes: wing width minus credit |
| Naked short call or put | 100% of option value + 20% of the underlying, minus any out-of-the-money amount. Floor: option value + 10% of the underlying (calls) or of the strike value (puts) | margin fits for some names. At-the-money put at 2026-10-29 closes [sa], plus premium: ≈$2.9k SPCX, $4.3k NVDA, $7.0k AAPL, $8.1k TSLA, $10.6k MSFT, $11.3k AMD (QQQ ≈$12.8k). But an assignment delivers $14k–57k of stock |
| Covered call | no margin on the call; needs 100 shares | Monday/Wednesday names cost $14k–57k for 100 shares; only stocks under ~$100 fit |
| Cash-secured put | strike × 100 in cash (I) | none of the Monday/Wednesday names |
| Any margin account | at least $2,000 equity ((b)(4)) | — |

- **(corrected)** "Naked selling is impossible at $10k" holds for QQQ but not for every stock. The real constraints are assignment size, uncapped gap losses, and IBKR's house margin and permissions (U). The draft's K10 rule (no naked calls; puts only if fully cash-secured) is stricter than Reg T. That is a design choice, consistent with the owner's view.
- **Notional per contract** at 2026-07-31 closes (local data) [L-px]: NVDA $20.1k, AMZN $27.2k, AAPL $30.9k, TSLA $31.1k, GOOGL $35.6k, AVGO $38.9k, MSFT $46.5k, META $55.7k. Friday-only names: MSTR $9.3k, COIN $14.6k.

### 2.3 Liquidation on expiration day

Exchange and OCC rules are in §1.3.

**IBKR's stated policy** (S, L: quoted from earlier search snippets; the pages return 403).
- It "will simulate the effect of expiration assuming plausible underlying price scenarios".
- It may close expiring positions on the last trading day, let long in-the-money options lapse, sell shares due for delivery, or restrict the account to closing trades.
- It reserves the right to block an exercise or close short options when a deficit would result [ibkrDel].
- Liquidation of a deficit left after expiration "typically begin[s] at approximately 9:40 AM ET" the next business day [ibkrLesson].
- Any position may be liquidated, not only the delivered shares [ibkb1767].

**Time of day on expiration day: U.** Forum anecdotes, all dated 2020-09-24, report forced closes around 2:00–3:00 pm ET that looked discretionary [eliteLiq]. **(corrected)** The earlier claim "about 2.5 h before expiry, or by 4 pm the day before" is not in that thread.

**Design (I).** Keep FLAT 15:50 as the main rule. Add a FLAT15 sensitivity: at 15:00, close at the touch any leg that would deliver shares at the 15:00 price. This models IBKR closing positions before the trader does. Keep HOLD and ASN as sensitivities.

### 2.4 Day-trading (pattern day trader) status

**New regime** (R, H).
- SR-FINRA-2025-017 was approved 2026-04-14 (Rel. 34-105226, 91 FR 20731) and took effect 2026-06-04. Firms may phase it in until 2027-10-20 [rn2610].
- It removes the day-trade count, the pattern-day-trader label and the $25,000 minimum. Rule 4210(f)(8)(B) now reads "Reserved" [r4210].
- Firms must calculate an intraday margin deficit on any day with an "IML-reducing transaction" (IML = intraday margin level). That covers any purchase or sale, including from exercise or assignment, the expiration of a long option that lowers the IML, and withdrawals.
- A deficit must be met "as promptly as possible" and may stay outstanding until the 15th business day. If a customer habitually fails to meet deficits and one is still unmet at the 5th business day, the account is restricted for 90 days. Deficits smaller than 5% of equity or $1,000 (whichever is less) are not counted. No real-time monitoring is required [rn2610].
- Interpretation (d)(2)(A)/01 lists accounts that "opened positions in options on their expiration date ('0DTE')" among those to review. (d)(2)(B)(v)a lets a firm treat the legs of one spread order as simultaneous [finraInterp].
- Firms may require more equity than the rule [finraInv]. FINRA deleted 19 day-trading interpretations, including 'Option Day Trade', 'Spreads' and 'Option Spreads and Combinations as Day Trades' [rn2611].
- SR-FINRA-2026-022 (effective 2026-10-22) concerns binary and other new option types, not this rule [fin2026022].

**Old regime** (in force until 2026-06-04; firms may keep it during the phase-in, I).
- A day trade was "the purchasing and selling or the selling and purchasing of the same security on the same day" in a margin account.
- A pattern day trader made ≥4 day trades in 5 business days, unless those were ≤6% of all trades. Two tickers each bought and sold on one day count as two day trades [rn2113].
- The $25,000 minimum was not re-read.
- Whether an option left to expire, or a spread, counted as a day trade: U. The deleted interpretations' text was not retrieved.

**IBKR's adoption date and any house minimum: not found (U).**

**What each regime allows a $10k margin account (I).**
- New rule: unlimited same-day exits, as long as intraday deficits are met. A fully paid defined-risk spread should not create one.
- Old rule: at most 3 same-day round trips per rolling 5 business days. Single-stock 0DTE days are Fridays, so that is about 3 stock-day exits per Friday. Positions whose legs all expire worthless at 15:50 have no closing trade.

**Design.** Pre-register both versions in the 1-lot replay (draft §0.6.8), and name the deciding one before running. Until IBKR confirms, the conservative default is the old rule, counting every stock-day with any closing trade as one day trade.

### 2.5 Option permissions and account type

Not found (U).
- FINRA 4210(b)(4) requires $2,000 equity in a margin account. Brokers must do due diligence before approving an options account (ISE Options 10 §6(b) and FINRA 2360(b)(19)(B), as quoted in [sec104624]).
- Industry practice, unverified for IBKR: naked options need the top permission level and a margin account; spreads need a margin account.
- Whether an IBKR cash account can hold spreads or cash-secured puts: U.
- K10 is already the fallback set that needs only spreads and long options.

## 3. Evidence: who earns the premium

### 3.1 The cost hurdle

**Arithmetic** (I, M; extends the academic sweep).
- With penny ticks, a round trip costs 1–2 ticks plus 2 × $0.70 in fees: $2.40–3.40 per contract.
- An at-the-money 0DTE option is worth about 0.4 × S × σ × √t.
  - $100 stock at 40% IV: ≈$0.99 at 09:45, so costs are 2.4–3.4% of premium. At 13:00 it is ≈$0.68, so 3.5–5.0%.
  - A $30 stock: ≈$0.30, so 8–11%. A $0.10 out-of-the-money option: 24–34%.
  - $400 stock at 60% IV: ≈$6.0 at 09:45. With $0.05 ticks, 1–2 ticks plus fees cost $6.4–11.4 per contract, or 1.1–1.9% of premium.
- So costs are the smallest share of premium for at-the-money options on high-priced names, and those are exactly what K10 excludes for long premium.

**Break-even for a short at-the-money straddle held to 15:50** (I). (Implied move − average realized move to the exit) ÷ implied move must exceed the cost share. That is about 2.5–3.5% at 1–2 ticks, and about 13% at the 12.6% average quoted spread.

**Fill rule (I).** The draft fills at mid ± 0.25 × spread (F = 0.25). That matches the retail ratio of effective to quoted spread in Bryzgalova et al.: 6.6 ÷ 12.6 means fills about 26% of the quoted spread away from mid [bps2023]. But that improvement comes from wholesaler auctions, which an IBKR Pro order routed straight to an exchange may not get. Keep touch fills as the deciding case.

**Spreads by time of day.**
- One day of SPY data (contracts with mid $1–10): median spread $0.03 in the 09:00 and 12:00 buckets, $0.10–0.11 from 13:00 to 15:00. As a share of mid, 0.8% at 09:00 rising to 2.67% at 15:00 [strasmore] (R; weak as evidence).
- No measurement exists for single stocks. A vendor gives at-the-money ranges with no data source: TSLA $0.05–0.20, NVDA $0.05–0.15, AAPL $0.03–0.10, MSFT/GOOGL $0.05–0.15, AMZN/META $0.10–0.25 [my0dte] (S, L).

### 3.2 Documented effects

| Effect | Size and sample | Relevance to single-stock 0DTE | Beats small-account costs? |
|---|---|---|---|
| Volatility premium is earned overnight [mn2020], [cxoMN] | delta-hedged S&P options −0.7%/day = −1.0% overnight + 0.3% intraday. Equity options −0.4% overnight, +0.1% intraday (2004–2013, mid, gross). S&P options under 3 weeks −2.6% / +0.07% | an intraday window predicts ≈0, or a small gain for buyers; whether 0DTE contracts were in the sample: U | no (intraday) |
| Lower option returns over market closures [js2018] | significant; no size in the abstract | Monday expiries and Thursday→Friday holds only | — |
| Lottery-like (high-skew) options [bv2014] | 10–50%/week spread across skewness-sorted portfolios | cheap out-of-the-money wings | gross yes; but ticks and fees take 20–35% of wing premium |
| Retail short-dated demand [bps2023] | retail >60% of volume, half of it in options under a week. Short-term buyers lose; sellers profit after costs. Retail lost $2.1bn (Nov 2019–Jun 2021) and paid $6.4bn relative to mid vs $0.9bn in commissions. Quoted spread under a week 12.6% (6.6% effective). Trades under $250: 23.5% (11.6%); slightly out of the money: 28.7% (13.9%). The last two cover all maturities **(corrected)** | the transfer runs mostly through the spread | a seller who pays the spread is on the losing side of it |
| Retail, trader-level, 2020–2022 [bmur] | average option trade −0.93% (t −3.57) after costs; purchases −3.95%; naked sales +20% (13% of trades). Single-stock option trades +0.15% vs index/ETF −3%. 0DTE = 23% of trades, 93.3% opened as purchases, −4.6% per trade, 62% on S&P/SPY | partly | small |
| Earnings [dss], [qpRetail], [gxz] | retail loses 5–9% around earnings, 10–14% when expected volatility is high; spreads ≈20% of price (S). At-the-money straddles bought 3 days before the report earn +3.34% (10–60 days to expiry, mid, 1996–2013); unconditional straddles −0.19%/day | the day after a Thursday report; the pre-report premium needs an overnight hold | possibly, overnight only |
| Pinning at strikes [npp2005], [zcc2015], [elms] | ≥16.5 bp per expiry (1996–2002, monthly). Weekly Fridays 6.9% vs 5.4–6.2% pinned on other days, monthly 10.0% ($5 grid, within $0.125). Logit weekly dummy 0.098 (t 2.05; not significant within a fixed grid). SPY 2016–2025: no pinning (preprint) | weak on weekly Fridays | no |
| Max pain [fgz] | ≈0.4%/week long-short, monthly, small illiquid stocks; a reversal effect (S) | not mega-caps | no |
| Dealer gamma [ppw2006], [npw2021] | a 1-SD change in net gamma ↔ 36.8 bp of a 310 bp mean absolute return (≈12%); 1990–2001; holds without expiry weeks | needs open interest by investor type (not in QC); an open-interest proxy needs an assumed sign | big enough in principle; not observable |
| SPX 0DTE gamma [agpv] | negative dealer gamma adds ≤3.3 pts of realized vol (daily, annualized), 6.4 pts (30-minute) | index only | — |
| End-of-day reversal [bds2023] **[stock-only]** | 3.45–4.45 bp/day long-short (1993–2019); also in stocks without options | stocks | no, for options |
| Gamma-signed last-half-hour momentum [bm2021] **[stock-only]** | 18 bp per 1 pt of return up to 15:30; Sharpe >4 gross, ≈break-even after ~5 bp costs; mostly market-wide | stocks | no |
| Third-Friday opening spike [btw] | SPX opening settlement price +18.5 bp above the prior close (2003–2021, t >4.5); reverts by about noon; fading it had Sharpe 1.3 gross / 0.8 net; no effect on close-settled expiries | biases 09:30–10:00 entries on third Fridays; **[stock-only]** test | — |
| Expiration-week drift [ss2013], [qpOE] **[stock-only]** | S&P 100 stocks +0.528%/week in option-expiry week; 67.9–70.7 bp spread for option-heavy names (1996–2008) | stocks | — |
| Selling after deep in-the-money call exercise [chiang] | −0.8 pt on the expiry date, partly recovered | late selling pressure on short calls; **[stock-only]** test | — |
| Historical vs implied volatility gap [gs2009] | straddle long-short 22.5%/month at mid, 7.5% at the full quoted spread (monthly) | as a filter (draft's RICH) | maybe |
| Stock characteristics [chtz], [ch2013] | 8 of 12 characteristics predict returns: 1.28–3.92%/month **at mid** **(corrected)**, monthly Sharpe 0.63–2.00. Quoted spreads ≈20%; e.g. new-share-issuance strategy 1.46% → 0.62% at the full spread. Returns fall with stock-specific volatility | filters only | no, at daily turnover |
| Timed orders [mp2020], [cxoMP] | effective spread under 40% of the usual estimate (1.3¢ vs 8.1¢ quoted, 2003–2006) | minute bars cannot reproduce it | optimistic sensitivity only |
| Retail SPX 0DTE [bbg2023], [neudata] | over 75% of retail SPX trades are 0DTE; losses despite price improvement. $358k/day (snippet) vs ≈$184k/day (project ledger) | index only | — |

### 3.3 What could beat costs, and the project's priors

**Candidates (I).**
1. Earnings mispricing. But the documented premium is *before* the report; a pure 0DTE trade on the post-report Friday starts after implied volatility has collapsed.
2. Dealer gamma state. Big enough (≈12% of the expected move vs a 2.4–3.4% cost), but it needs data QC lacks.
3. Implied vs realized volatility richness (the draft's RICH filter). The evidence is monthly only.
4. The lottery premium on cheap wings. Large in percentage terms, but ticks and per-contract fees take 20–35% of premium.

Pinning, max pain, end-of-day, third-Friday and expiry-week effects are a few bp to tens of bp per event, below option round-trip costs. Several are cheap **[stock-only]** side tests.

**Project priors** [L-zero] (P):
- **QQQ.** The premium ≈ a small account's execution cost. Three costs of similar size explain it: fills at the touch (about 90% of fills land on the bid or ask under F=0.25 rounding), IBKR commissions, and the 15:50 exit. Holding to the close would have given t +2.26.
- **SPXW.** Selling far out-of-the-money put spreads at 15:00 on Fridays gave t 16.5, read as "the tail has not hit yet". Friday 15:00 straddles were slightly overpriced: realized ÷ implied 0.85 vs 0.97 on other days (t −2.05).

**Platform statistics.** Option Alpha's 0DTE trades (81% SPY): Monday best; Tuesday, Thursday and Friday net negative [oa2023]. The options.cafe breakout rule did worst on Fridays [optcafe].

**Expect a null.** Pre-register the minimum detectable effect.

## 4. Strategy catalogue

Notation follows the draft. Structures: PCS put credit spread, CCS call credit spread, IC iron condor, IF iron fly, SP/SC naked short put/call, SG short strangle, SD/LD short/long at-the-money straddle, RR risk reversal, LC/LP long call/put, LG long strangle, DVC/DVP debit call/put spread, IFP iron fly at the highest-open-interest strike, IFM iron fly at max pain, MPD debit spread toward max pain, "-BE" each side stopped separately. Wings: W1 = adjacent strike; W1% / W2% = about 1% / 2% of price. Entry times: 09:45, 10:30, 12:00, 13:30, 15:00, or LAD (all five, equal weight). Credit trades are managed by H (hold) / stop at 2× credit / stop at 3× / TP25 (close when 25% of the credit is captured); debit trades by H / stop at half / take profit at 2× (TP2) / 3× (TP3). Filters: ALL / EARN / NOEARN / GAP / MOM / RICH / UP / V20. Default exit: flatten at 15:50.

### 4.1 Families (deduplicated, English and Chinese sources)

**A. Buying premium: directional or lottery tickets**
- **A1. Friday-afternoon lotto** (末日轮, "买彩票"; Hstong; Raging Bull; moomoo) [hstong2024], [ragingbull], [moomooApr].
  - Rule: on expiry day at 15:00, buy a 0.05-delta call or put and hold to 15:50. Draft: LC0.05 and LP0.05, 15:00, H, ALL.
  - Evidence: anecdotes only. Example: TSLA $350 0DTE calls traded ~215,600 contracts, peaked at $3.35 and were $0.15 by 12:25 pm. Retail 0DTE purchases average −4.6% per trade [bmur].
  - Must-have comparator.
- **A2. Morning out-of-the-money call, sold at 3×** (Hstong, "aggressive" style). Draft: LC0.10, 09:45, TP3.
- **A3. Opening-range breakout** (options.cafe, 2026-03-03) [optcafe].
  - Rule: the range is 9:30–9:35. On the first break, buy an at-the-money call (break up) or put (break down); one trade a day. Exits: take profit at +100%, stop at −50%, time stop at 15:30. If stop and target fall in the same bar, it counts as a loss.
  - Evidence: SPY, Feb 2024–Mar 2026, 303 trades, 41.25% wins, +$14,860 (+59.44% on $25k), max drawdown $2,611 (7.60%). Fridays: 103 trades, 39.81% wins, +$4,356, the weakest day. No commissions or slippage; fills ≈ mid at the bar close; Tuesday and Thursday were dropped after looking at the results.
  - The draft has no breakout trigger and no combined stop + target. Add an exact version if the engine supports it. Otherwise approximate with LC/LP0.50, 09:45, MOM filter, reporting TP2 and stop-at-half separately. At-the-money premium on names above $300 exceeds the $300 K10 cap (I).
- **A4. Opening-momentum scalp** (Jianshu, "末日期权六大交易原则", TSLA) [jianshu] (S, snippet).
  - Rule: trade only the first 30 minutes, exit within 10–20 minutes, tight stop, one try only.
  - Minute bars cannot model this well. Exploratory only: LC/LP0.50, 09:45, MOM, stop at half.
- **A5. In-the-money option in the direction of a news or earnings gap** (Raging Bull) [ragingbull]. Approximate: EARN filter + gap direction, LC/LP0.50 at 09:45. In-the-money deltas are not in the grid and exits were discretionary. Exploratory.
- **A6. Expiry-day out-of-the-money strangle** (Hstong, "conservative" style) → LG0.25, already in the grid.
- **A7. Momentum chase** → LC0.25, 10:30, TP3, MOM (draft comparator).
- **A8. Buy the crushed lotto ticket** (Eastmoney; source trades Chinese commodity options, never tested on US stocks) [eastmoney].
  - Rule: on expiry day only, buy the option one strike out of the money, only after it has fallen ≥90% intraday and only before 11:00. Add an equal amount after a further ≥50% drop. Stop at −20%. Take profit only at ≥2×, on the whole position. Never hold overnight.
  - Testable with minute quotes (path-dependent trigger). Add as an exploratory comparator.
- **A9. Cheap out-of-the-money TSLA scalp with a +10% target** (moomoo) [moomooScalp]. On a $0.14 option a 1¢ spread is ≈7%, so the target is mostly cost (I). Document; don't test.
- **A10. VWAP/RSI/MACD scalps** from vendors [my0dte], [otc]. Thresholds not given; not testable.
- **A11. "Gamma squeeze" call buying.** Anecdote: TSLA $220 calls rose 70% to $0.68 on a 4% stock move, with volume ≈9× open interest [moomooTSLA]. A possible objective trigger: same-day call volume ÷ prior-day open interest at 10:30, from AlgoSeek trade bars. Exploratory.

**B. Selling premium with defined risk**
- **B1. tastylive-style 16-delta trades.** The draft has PCS0.16-W1, 09:45, TP25. Also add IC0.16-W1, 09:45, TP25, the QQQ study's comparator [L-zero].
- **B2. Option Alpha late-morning iron fly/condor** [oa2023], [oa2022].
  - Platform data: ~230k 0DTE trades since Sep 2021, 81% SPY, under 6% outside SPY/QQQ/IWM. Iron fly wins 66.76%, condor 70.19%; 49.64% of traders were profitable. Profitable traders entered around 10:15 and exited just after 12:00. Condors opened more than 2 hours after the open averaged +37% (759 trades) vs −0.36% for earlier entries. None of these statistics include costs.
  - Add IF-W1, 12:00, TP25 (the QQQ study's comparator). Optionally an IF at 10:30 with a 12:00 time exit, if the engine supports time exits.
- **B3. Breakeven iron condor** → IC-BE0.10-W1%, LAD (draft).
- **B4. Chambless-style 5-delta spreads** (QQQ comparator; also the defined-risk form of the Chinese 捡烟头 style, i.e. selling deep out-of-the-money options near expiry) → add PCS0.05-W1 and CCS0.05-W1, 09:45, stop at 3×.
- **B5. 5-day-moving-average-filtered credit spreads** (Option Alpha) [oa2023]. As reported: short call spreads on a "buy" signal and short put spreads on a "sell" signal; profit factor >2, win rate >75%, 37,894 trades, with rules optimized on the data. The signal direction may be a reporting error (U). Exploratory.
- **B6. Panic-day deep out-of-the-money put sale** (Tiger, 期权小班长, 2021) [tigerXBZ].
  - Rule: after a sharp sell-off with an implied-volatility spike, sell expiring puts about 16–20% out of the money, at support (weekly 30-period or monthly 20-period moving average). Never before earnings; close rather than be assigned.
  - Defined-risk version: PCS0.05-W1, 09:45, H, plus a new pre-registered sell-off filter (e.g. prior-day return below −2 × 20-day σ). Exploratory.
- **B7. Far out-of-the-money same-day call sale on a call-buying frenzy** (段永平 / Duan Yongping) [xueqiuDuan].
  - Anecdote: on 2020-12-18 he sold 200 TSLA $750 calls for about $1; TSLA closed at $695. He wrote "小赌怡情，不要模仿" ("a small bet for fun; don't copy").
  - The naked version is not feasible at $10k. Test CCS0.05-W1 with a call-volume trigger. Exploratory.
- **B8. Selling the earnings-week volatility collapse.**
  - Sources: Hstong sold the NVDA $225 straddle for $1,698 ahead of Wednesday earnings; macrochen's screener; Tiger 格雷期权 (Grey Options) [hstongNVDA], [macrochen], [tigerGL2].
  - Pure 0DTE version: IF or IC on the expiry after the report, with the EARN filter. The draft has SD, 09:45, H, EARN, which is naked and outside K10; add IF-W1, 09:45, H, EARN as the K10 version.
- **B9. Generic Chinese 0DTE guide** [einverne]: sellers enter 10:00–14:00; buyers use 0.2–0.4 delta in the last hour; cut at −50%; risk 1–2% per trade. Already covered by the grid.

**C. Pinning, max pain, and open-interest walls**
- **C1. Augen-style midday pin ratio call fly** (SteadyOptions, 2019-08-29) [so2019].
  - Rule: AAPL (also GS, BBY), every Friday expiry. Enter 12:00–12:30. S = the strike nearest the price; skip the day if the price is midway between strikes. Buy 1 call at the next strike below S, sell 3 at S, buy 2 at the next strike above (traded as 10/30/20). Exit 10–15 minutes before the close, or at +30% profit. Margin about $1,500–2,000 for 10/30/20.
  - Claims: the stock pins "50% of the time" (within $0.40, no chance baseline) and the trade earns "6% every month" on margin. P&L was estimated from stock prices, not quotes; no costs; 18 months of one hand-picked ticker.
  - Add the exact 1:3:2 ratio fly at 12:00, with exit at 15:50 or +30%. The grid's IFP is a symmetric iron fly at 13:30, a different trade. At minimum, move the IFP comparator to 12:00.
- **C2. SteadyOptions 2026 variants** (MSFT, about 39 weeks, Black–Scholes prices, no P&L reported) [so2026]:
  - (a) Thursday ≈16:00: buy 10 next-day calls at S, sell 30 at the next strike up; close Friday 10:00. This is overnight, excluded by draft §0.8.
  - (b) Expiry day, 11:00–13:00: the 1:3:2 fly. Same as C1.
  - (c) An iron condor around a "mid-way pin", 11:00–13:00 ≈ IC0.16 at 12:00.
  - (d) A long straddle at S, delta-hedged with shares ≈ LD at 12:00, unhedged.
  - Filter: skip if VIX > 20.
- **C3. Broken-wing butterfly at the highest-open-interest strike** (Elite Trader journal, Jan 2021) [elitePin]. Entered on Monday, so not 0DTE. Only its add-on is 0DTE: a long at-the-money straddle on expiry day in high-open-interest names, entered "around 9 AM" as written and closed before 2 pm. That is LD, 09:45, with a 14:00 time exit, if supported. The one realized trade (TLT fly) made +$44 before $40 of commission.
- **C4. Max-pain fly, debit spread toward max pain, call-wall trading** → IFM-W1%, 12:00, H and MPD, 12:00, H (draft). The Sina AI article cites TSLA contracts dated 2026-07-03, which did not exist: Jul 3 was a holiday, so that weekly expired Thursday Jul 2 [sinaAI], [nyse]. Do not use it.
- **For this whole family:** compare against a chance baseline (pin band ÷ strike interval) and against non-expiry days. Report monthly and weekly Fridays separately [zcc2015], [elms], [fgz].

**D. Stock plus option (income)**
- **D1. Friday 0DTE covered call** → SC0.25, 09:45, H, stocks ≤$100 (draft). Evidence is index-only:
  - SPX 0DTE covered call sold at 10:00, 2018 to present: at the money 6.8%/yr; 0.25% out of the money 8.9%; 0.5% 12.5%; 0.75% 13.1%; 1.0% 14.2% (Sharpe 0.79); SPX itself 12.4% [omCC].
  - XDTE 15.34% vs SPY 16.73% over the year to about 2026-09-26 [wallst247].
  - Out of the money did best, so also add SC0.10 (I).
- **D2. Cash-secured put** → SP0.16, 09:45, H, in K10 (strike ≤$100), draft.
- **D3. Duan-style covered calls after a sharp rise, or puts on stocks you want to own** [sinaDuan]. Needs 100 shares; not feasible at $10k.

**E. Overnight and earnings variants** (draft §0.8 currently excludes overnight holds)
- **E1. One-night arm:** open Thursday at 15:50, exit Friday at 15:50. Motivated by the overnight premium [mn2020] and SteadyOptions' Thursday ratio spread; a tastylive video title reads "1DTE … only on Fridays" (title only, L) [youtube]. Decide: either keep it excluded and state the expected intraday null in §0.1, or add it as a separately pre-registered and gated arm (defined risk only; Thursdays before earnings excluded).
- **E2. Long straddle bought before earnings** (Xueqiu; Hstong's TSLA $240 straddle ≈$3,282; Option Alpha backtests) [xueqiuStraddle], [hstong2025], [oaStraddle]. Option Alpha: AAPL 2007–~2017 won 41.38% with an average −1.31%; FB 27%, +0.70%; CMG 35.48%, −2.59%; costs not stated. Overnight, so excluded.

**F. [stock-only] tests** (if 股票 means the shares)
- F1. Fade the open-to-noon move on third Fridays [btw].
- F2. End-of-day reversal: sort stocks on the return from the prior close to 15:00; trade 15:30→16:00 [bds2023].
- F3. Last-half-hour momentum signed by dealer gamma (needs daily open interest, which QC has) [bm2021].
- F4. Hold large caps in option-expiry week; out of sample after 2010 [ss2013].
- F5. Open interest in deep in-the-money calls → expiry-day drop and next-day rebound [chiang].
- F6. Max-pain reversal sort [fgz].
- F7. Pinning on stock bars: distance from the 15:00 price to the highest-open-interest strike vs the 16:00 close, with non-expiry days as a placebo [npp2005], [zcc2015].

### 4.2 Named comparators a fair study must include

All are reported but cannot be selected by the walk-forward. Apply the $1-per-order minimum to 1-lot single-leg comparators.

| # | Comparator | Exact configuration | Draft status |
|---|---|---|---|
| 1 | Friday-afternoon lotto call | LC0.05 15:00 H ALL | in draft |
| 2 | Friday-afternoon lotto put | LP0.05 15:00 H ALL | in draft |
| 3 | Morning out-of-the-money call, sold at 3× | LC0.10 09:45 TP3 ALL | in draft |
| 4 | tastylive 16-delta put spread, TP25 | PCS0.16-W1 09:45 TP25 ALL | in draft |
| 5 | tastylive 16-delta iron condor, TP25 (as in the QQQ study) | IC0.16-W1 09:45 TP25 ALL | **add** |
| 6 | Option Alpha at-the-money iron fly, TP25 (as in the QQQ study) | IF-W1 12:00 TP25 ALL | **add** |
| 7 | Chambless-style 5-delta spreads (as in the QQQ study) | PCS0.05-W1 and CCS0.05-W1 09:45 stop at 3× ALL | **add** |
| 8 | Breakeven iron condor | IC-BE0.10-W1% LAD ALL | in draft |
| 9 | Augen/SteadyOptions pin ratio fly | 1:3:2 call fly at the nearest strike, 12:00, exit 15:50 or +30% | **add (new structure)** |
| 10 | Pin iron fly | IFP-W1% 12:00 H ALL | **move from 13:30 to 12:00** |
| 11 | Max-pain iron fly | IFM-W1% 12:00 H ALL | in draft |
| 12 | Debit spread toward max pain | MPD 12:00 H ALL | in draft |
| 13 | Opening-range breakout, at-the-money | breakout trigger; +100% target and −50% stop; 15:30 time stop | **add if the engine allows** |
| 14 | Post-earnings expiry, short/long straddle | SD / LD 09:45 H EARN | in draft |
| 15 | Post-earnings expiry iron fly (K10 version) | IF-W1 09:45 H EARN | **add** |
| 16 | 0DTE covered call | SC0.25 and SC0.10 09:45 H ALL, stock ≤$100 | **add SC0.10** |
| 17 | 0DTE cash-secured put | SP0.16 09:45 H ALL (K10) | in draft |
| 18 | Momentum chase | LC0.25 10:30 TP3 MOM | in draft |
| 19 | Crushed-lotto rebound (Chinese source) | ≥90% intraday drop before 11:00; add after −50%; stop −20%; take profit at 2× | **add (exploratory)** |
| 20 | Intraday-drift control | RRB at each entry time, H ALL | in draft (report only) |

Also tag third Fridays and report entries before 12:00 on those days separately, because of the opening bias [btw] (I).

## 5. Calibration: live products and how to use them

| Product | What it does | Window | Can listed minute quotes replicate it? | Key numbers |
|---|---|---|---|---|
| Tuttle MSTK (MSTR 0DTE covered call) [tuttle485], [saMSTK], [tuttleLaunch] | each business day at or after the open, sells out-of-the-money calls expiring that day against a synthetic long (deep in-the-money, longer-dated calls). May use 1–2 days to expiry; no fixed strike | 2025-10-23 → last trade 2026-04-14 (118 trading days); liquidated | partly: MSTR had only Friday listed expiries, so the Mon–Thu legs were listed weeklies 1–4 days out, or FLEX (U) | 0.99% fee. Paid $0.03 weekly in Mar–Apr 2026. Last trade $7.93 (52-week range $6.59–26.55). MSTR fell 51.8% over the same window. "First single-stock 0DTE ETF" is not established: the launch release claims only a first on MSTR ("Strategy") **(corrected)** |
| xETFs NYYY (NVDA), TYYY (TSLA) [xetfsPR], [etfcentral], [etfdbTYYY], [saNYYY], [saTYYY] | about 100% long via swaps plus a FLEX call/put pair expiring 2027-01-15. Sells short-term calls every trading day, usually ≤1 week to expiry, on ~10% of notional (cap 25%) | from 2026-05-15 (~115 trading days to Oct 2026) | partly: NVDA/TSLA have listed Mon/Wed/Fri expiries; strike model is proprietary | 0.99% fee. AUM ≈$1.68m (NYYY) and ≈$0.40m (TYYY). TYYY closure announced 2026-10-20. No performance found. NVDA −10.9% from 2026-05-15 to 07-31 |
| YieldMax TSLY, NVDY and ~22 others [nvdy], [tsly], [zacks] | synthetic long from 1–6-month at-the-money options; sells calls 0–15% out of the money, ≤1 month to expiry (1–8 days in practice, per a TSLY 2025-07-31 holdings snippet); uses listed and FLEX options and swaps | TSLY from 2022-11-22; NVDY from 2023-05-10 | approximately only | NVDY expenses 1.09%. TSLY 50.24% / 29.51% / 14.17% (2023/24/25) vs TSLA +101.7% / +62.5% / +11.4%. NVDY 115.87% / 27.91% (2024/25) vs NVDA +171.2% / +38.9%. Since inception: TSLY 16.96%/yr vs TSLA 36.78%/yr; NVDY 67.27%/yr vs NVDA 102.47%/yr. 22 of 24 YieldMax single-stock funds lagged their stock (to May 2025) |
| Kurv TSLP [tslp] | discretionary; mostly FLEX options, generally European-style and cash-settled | from 2023-10-26 | no | 40.85% (2024) and 9.41% (2025) vs TSLA 62.5% and 11.4%. Since inception 31.83%/yr vs TSLA 43.09%/yr. −9.93% year-to-date at 2026-06-30. Fee 1.15% gross / 1.00% net |
| GraniteShares YieldBOOST (TSYY, …) [gsSupp], [gsTSR] | sells puts on 2× leveraged single-stock ETFs, 40% out of the money to 10% in the money, 1 week to 1 month, directly or through swaps | TSYY from 2024-12-17 | no: FLEX strikes and leveraged-ETF underlyings | TSYY −5.44% over the year to 2026-06-30 vs TSLA +32.4%; −20.89%/yr since inception vs TSLA −8.24%/yr. Expenses 1.08%; assets $78.7m. Its Thursday 07/02 expiry is just the holiday shift **(corrected)** |
| Others [rex], [saMST], [nvdw], [tmgn] | REX Growth & Income: 1.25× via swaps and options, calls on ~50% of notional, tenor U. Defiance MST: 1.5–2× plus call credit spreads. Bitwise IMST etc.: launched about 2025-04-01–03 **(corrected)**, uses FLEX. Roundhill WeeklyPay sells no options (exclude). TappAlpha TMGN writes 0DTE calls on an index of 10 mega-caps (index-based) | — | no | — |

**How to use them** (I unless tagged).
1. **Main fill calibration comes from AlgoSeek itself.** For every candidate 0DTE contract-minute, place the minute's trade prints within the same minute's quote, as a fraction of the spread from mid. Break this down by name, year, time of day and moneyness. This shows where real trades print on the contracts the study trades. It does not show an IBKR 1-lot's own fill, so touch fills stay decisive.
2. **Fund residuals are exploratory.** Residual = the fund's daily total NAV return + expense ratio ÷ 252, minus β × the stock's return (β ≈ 1). Compare it with the simulated option overlay priced at mid and at the touch, using the fund's actual strikes and expiries from its monthly holdings reports (N-PORT). Use only windows whose holdings show standard listed series.
   - The test is weak: windows are ≤120 days, strikes are discretionary, and FLEX and swap legs appear.
   - NYYY's 10% call overlay is worth only ≈0.01–0.05% of NAV a day, which swap tracking noise swamps.
3. **Apply the draft's own rule.** §0.6.7 says that if no fund can be replicated, the decision must also hold under conservative fills. GO condition 5 should therefore use its "conservative fills still pass" branch.
4. **Benchmarks.** Every single-stock option-income fund with more than a year of history lagged its own stock over its life; TSLY in 2025 was the only calendar-year win. In Europe, 7 of 19 single-asset option products beat their underlying in 2025, 12 of 19 had a higher Sharpe ratio, and all had smaller drawdowns [incomeshares] (R). Keep buy-and-hold of the same stocks next to ONEQ, and keep the intraday-alpha gate.
5. **Data routes.**
   - QC US Equities: AlgoSeek, free of survivorship bias, ~27,500 securities from 1998; ETFs are not explicitly named, so check that MSTK, NYYY and TYYY load [qcEquities].
   - Yahoo or Tiingo adjusted closes as a cross-check.
   - SEC EDGAR N-PORT for each option's strike, expiry and quantity (made public quarterly; not re-checked for 2026).
   - YieldMax's website publishes NAV, premium/discount and median bid-ask spread [nvdy].

## 6. QuantConnect data facts and limits

**Coverage.**
- AlgoSeek US Equity Options, built from OPRA: from "January 2012*". Some data is earlier; in 2012 AlgoSeek moved from 24 to 48 OPRA channels.
- About 4,000 underlyings; standard and weekly series; minute, hour and daily trade bars, quote bars and open interest; regular hours only. Minute data is free on every tier; tick and second data are not on Free [qcOpt], [qcOptData], [qcPricing]. R, H.
- No page says the quotes are the NBBO. FLEX is not covered (I).
- Monday/Wednesday single-stock series are present: probe v2 found them from 2026-02-02 for the 8 names and from 2026-07-13 for MU, AMD and INTC [L-s0] (P, H).

**Open interest.**
- One value per day; the docs also say "a few points per day", so they are inconsistent. The time stamp and which OCC day it reflects are undocumented. The live feed serves open interest "as it is broadcast by the exchanges each morning" [qcHandling], [qcAnn].
- Probe: 98% of same-day contracts within ±3% of the price (26,081 of 26,528) have open interest in the universe file [L-s0].
- Use only the D-1 universe-file value, as the draft does.

**Data end (corrected).**
- In every probe, the last visible bar is 90 days before the run date: 2026-07-09 for runs on 2026-10-07, and 2026-07-30 16:00 for the probe on 2026-10-28. Earlier runs on 2026-10-07 had reached 2026-07-31 [L-zero].
- This matches QC's organization-level "Backtesting Out of Sample Period". LEAN quietly moves the end date to `OutOfSampleMaxEndDate`, writing only an engine trace line [qcGuide], [qcAdmin], [leanSetup]. Whether the owner or QC set it: U.
- QC says the data updates nightly [qcOptData].
- Design: freeze the end date in the pre-registration (draft: 2026-07-29) regardless of the holdout. If the holdout is lifted, report 2026-07-30 onward as a separate later segment; it adds Q3 Monday/Wednesday data for AMD, INTC and MU.

**Option chains.**
- In backtests, `option_chain()` and `set_filter` read the **previous trading day's** universe file: the contract list plus D-1 OHLCV, open interest, implied volatility and greeks, with a fallback of up to 3 days [leanUniv], [leanChain], [qcUnivDocs].
- So there is no look-ahead, but a 0DTE contract's greeks are 1-day-to-expiry closing values from the day before, and strikes first listed on day D are missing.
- QC staff, 2026: the universe "is EOD data (previous trading day's close)", so after a gap the wanted options fall outside a delta filter [qcAnn].
- Precedent: on 2025-04-07 (a −4.5% gap) some low-delta QQQ structures could not open [L-zero].
- Since PR #9237 (2026-02-19) LEAN's default intraday pricing model is indicator-based [pr9237]. Compute delta and implied volatility from minute mid prices, as the draft does, and probe whether day-D strikes can be fetched directly through History.

**Identifying expiries.**
- `OptionSymbol.GetLastDayOfTrading` handles pre-2015 Saturday dates and holidays [leanSym].
- `expiration(min,max)` and `zero_dte()` count to the last trading date only since PR #9796 (2026-09-16); before it, `zero_dte()` on 2012-02-17 returned nothing [pr9796].
- `expiring_after`, `expiring_before` and `expiration(dates)` still compare the raw `ID.Date` [leanFilter].
- Weeklies are in the default filter only since PR #9162 (2025-12-31) [pr9162].
- Record the LEAN version for each run, and keep the engine's own "last trading day = today" test.

**Quote bars.** If one side's OHLC is all zero, that side becomes `None` and the mid copies the other side. Bars exist only for minutes with activity. Fill-forward is on by default, so pass `fill_forward=False`. Only the last size is stored [leanQB], [qcHandling], [qcHistory].

**Speed.**
- QC documents no hard limits. PR #9669 (2026-08-25) adds one-time warnings for:
  - a single History call of ≥5,000,000 cells. A minute quote bar is ≈390 × 10 = 3,900 cells per contract-day, so that is ≈1,280 contract-days per call;
  - 30 overlapping large calls in a row;
  - a time step longer than 3 minutes;
  - a universe of more than 1,000 symbols at minute resolution.
- Running out of memory used to show up as "Corrupt zip" [pr9669], [leanHist]. Each subscription takes about 5 MB of RAM [qcRes].
- Measured: QQQ 2–4 s per expiry day, SPXW 4–5 s; the stock-puts grid took 8,617 s for 3,602 days; probe v2 took 1,169 s [L-zero], [L-puts], [L-s0].
- Budget (I): about 755 Fridays (2012-02-03 to 2026-07-29) × 20 names ≈ 15,100 stock-days, plus about 400 Monday/Wednesday stock-days. At 1–4 s each, that is 4–17 h for P20, so at least 2 segments under the 12 h cap.

**Hard limits (Free tier).**
- 10 minutes per time step (scheduled events + consolidators + `on_data`). In backtests, events scheduled outside market hours all fire together at 9:31. A backtest can run for 12 h at most. `train()` on the free node gets a 20-minute reserve that refills 1 minute a day, not ~30 minutes **(corrected)** [qcSched], [qcRes], [qcDeploy].
- One free backtest node: 2 cores, 3.3 GHz, 8 GB RAM, a 20 s launch delay, 200 backtests a day [qcRes].
- Logs: 10 KB per backtest and 3 MB a day of reading.
- Charts: 10 distinct series names, counted across all charts, and 4,000 points per chart+series pair. Overflow is skipped with a one-time message rather than stopping the run (that is what the code does; the docs say otherwise).
- Runtime statistics: at most 50, each cut to 200 characters; values that look like base64 or hex are dropped (PR #9760, 2026-08-27).
- Results over 700 MB show an empty page. No Object Store on Free [qcPricing], [qcCharting], [leanResults], [pr9760], [qcResults], [qcObj].
- 10,000 orders per backtest; 25 files per project, 32 KB each (a 35,460-character file was rejected); no API on Free [qcPricing], [qcFiles], [L-puts]. "Backtest Download Requests 25" counts data files imported per backtest, not result downloads **(corrected)** [qcPricing].

**Terms (new since the owner's 2026-10-05 acceptance).** Terms v1.5 (2026-10-08) §2.6(c) lists charts, plots, logs and custom or runtime statistics as prohibited ways of moving Platform Data out. It also calls circumventing output limits a breach in itself, and the resources page says logs may not be used to export dataset information [qcTerms], [qcRes]. The project's export channel is charts. This is the owner's decision.

**Universe and corporate actions.**
- **Coarse fundamentals.** For day T, `Price` is the raw closing-auction price and `DollarVolume` is T's dollar volume (exact formula U). They become visible on T+1, so in backtests selection runs at midnight. About 30,000 equities in total, ~8,000 a day, from 1998, no OTC [leanCoarse], [qcCoarse]. The draft's ranking on prior-month dollar volume is point-in-time.
- **Morningstar data was replaced** for the whole history from 2026-09-23. The old dataset left LEAN master on 2026-10-10 and is fully retired on 2026-10-31.
  - About a third of shared values differ, usually by more than 20%. Coverage grows from 8,717 to 9,705 companies, and `has_fundamental_data` is true for more securities. QC says old backtests "will not reproduce" [qcMstar], [qcMig].
  - The draft's common-stock, ADR and share-class filters may rely on these fields. Record the dataset version; probe v2 (2026-10-30) already ran on the new data (I).
  - In the old data, `exchange_id` was not point-in-time [L-puts].
- **Price adjustment.** Option data is never adjusted for splits or dividends. Equity data defaults to ADJUSTED. `add_option` forces the underlying to RAW; pass RAW in every equity History call [qcIndiv], [qcSettings].
- **Splits.** LEAN does not support option splits: on the split-warning day it closes held options with market-on-close orders [leanAlgo], [qcCorp]. That does not affect a paper ledger. But on the ex-date, the D-1 universe still lists pre-split strikes, so the draft's rule of skipping the ex-date is right. How odd-split contracts (not 100 shares) appear: U.
- **Survivorship.** Current QC pages do not say whether options for later-delisted stocks are kept; only an old announcement says "survivorship bias free" [qcAnn15483]. Indirect evidence (P): probe v2's 92–100% coverage was measured over point-in-time members, including names delisted later. Still, check 2–3 delisted members (e.g. TWTR 2022, ATVI 2023, CELG 2019).
- **Known gaps.**
  - QQQ 0DTE days with no quotes: 2019-11-29 and 2023-12-27; QQQ quote interruptions 2021-12 to 2022-05; SPXW 2024-08-01 [L-zero].
  - A forum thread reports about 50 SPXW days with missing chains between 2022-05 and 2025-03 [forum19084].
  - Some "missing" strikes simply had not been listed yet [forum13447].
  - Count stock-days with no quotes as "no trade", by name and year.
- **Assignment model.** If real orders were used, LEAN's default assignment model would assign American-style shorts within 4 days of expiry when they are more than 5% in the money and exercise is profitable [leanAssign]. The paper ledger avoids this.

## 7. Contradictions, unverified items, open questions

**7.1 Corrections applied** (the claim, then the version used):
1. "Single-stock weeklies were rare before 2014" (StockPuts probe) → **refuted.** Probe v2 shows 92–100% Friday coverage from 2012. The old test counted ≥5 expiries within 53 days, i.e. how many expiries were listed at once, not whether any existed [L-puts], [L-s0].
2. "QC data is stale" → it matches the 90-day organization holdout.
3. The weeklys series cap started at 5, not 20, and limiting it to index options was CBOE's choice, not the rule.
4. The Q4 2026 list was found: INTC out, SPCX in.
5. The 23.5% and 28.7% spreads cover all maturities; the figure for options under a week is 12.6% quoted / 6.6% effective.
6. Cao-Han-Tong-Zhan returns are at mid and the Sharpe ratios are monthly. The 2022 RFS version reports an annual Sharpe above 2 after costs [chtzRFS].
7. "0DTE volume is mainly SPX" → SPX is only ≈16% of 0DTE contracts.
8. IBKR liquidation "2.5 h before / the day before" → not in the cited thread; anecdotes say 2–3 pm ET.
9. "Naked selling is impossible at $10k" → Reg T allows some 1-lots; the real limits are assignment and gap risk.
10. Rel. 34-70685 is a notice of filing, not an approval.
11. Bitwise funds launched about 2025-04-01–03.
12. TSYY's Thursday expiry is a holiday shift; "short puts held inside swaps" is unsupported.
13. MSTK is the first such fund on MSTR, not the first single-stock 0DTE fund.
14. The Hstong scan dated 2026-08-07 covers Thursday 08-06's session, i.e. contracts with 1 day left, on an unusual day (SPCX lock-up; HTZ +29.5%) [hstongScan].
15. The free node's `train()` reserve is 20 minutes, not 30.
16. "Backtest Download Requests" counts data imports.
17. Cboe's filing "on the same day as approval" is doubtful.

**7.2 Unresolved contradictions.**
- When single-stock weeklies began: end of May 2010 [rhoads] vs 2010-06-25 [zcc2015].
- Equity rows in the Cboe weeklys file: 584 vs 586.
- The two definitions of a "strike break" (§1.3).
- Retail SPX 0DTE losses: $358k/day [bbg2023], [neudata] vs ≈$184k/day [L-zero].
- Bogousslavsky–Muravyev versions: $15bn and 2.4m parent trades vs $20bn and 2.1m trades [bmur], [cxoBM]; median expiry "under a day" vs "one day".
- How INTC met the $700bn test in Q3: not reconciled.
- QC open interest "once a day" vs "a few points a day".
- QC docs say chart overflow stops the run; the code skips the points.
- Sign of dealer gamma: Beckmeyer–Moerke assume customers buy puts and write calls, while retail data show 69% of retail volume in calls [bps2023].
- The direction of Option Alpha's moving-average rule as reported.
- Practitioners' "pins 50% of the time" (wide band, no baseline) vs academic 6.9% (within $0.125). These cannot be compared.

**7.3 Not found or unverified** (and how to resolve):
- **IBKR** (pages return 403; the owner can read them logged in, or ask IBKR):
  - Tiered option commissions and pass-through fees (OCC, ORF, CAT, venue take fees);
  - any exercise or assignment fee;
  - liquidation time on expiration day, and the exercise/lapse cut-off;
  - when IBKR adopted FINRA's intraday margin standard, and any house minimum;
  - permission levels, cash-account rules for spreads and cash-secured puts, and house margin on naked options.
- The FINRA trading activity fee in force on 2026-10-30. How the old rule treated expiring options and spreads.
- Exchange lists beyond MIAX's (Cboe also posts them). Any filing after Aug 2026 for single-stock Tuesday/Thursday expiries (exchange rulebooks return 403).
- First weekly date per name: take it from QC.
- **QC:**
  - the open-interest time stamp;
  - strikes first listed on day D;
  - coverage of delisted underlyings;
  - how odd-split contracts appear;
  - the `dollar_volume` formula;
  - who set the holdout.
- **Literature:**
  - Bakshi–Kapadia's single-stock paper;
  - sizes in Stivers–Sun and Cao–Han;
  - Boyer–Vorkink's and de Silva et al.'s sample periods;
  - whether Muravyev–Ni included same-day expiries;
  - any study of single-name 0DTE returns at all.
- **Chinese platforms.** Xueqiu, Zhihu, Jianshu and 1point3acres returned 403 (snippets only). Gelonghui, Xiaohongshu and WeChat were not reached, and Reddit was blocked.
- **Funds:**
  - MSTK's N-PORT holdings (FLEX or listed);
  - NYYY/TYYY's strike rule;
  - YieldMax's call tenors over time (the TSLY holdings PDF returns 404).
- The Curb session go-live date: the press says 2026-10-19; the FAQ does not list it.

**7.4 Decisions to make before freezing.**
1. Owner intent: options on stocks, or the shares themselves?
2. Which day-trading regime decides the 1-lot replay.
3. The one-night (Thursday→Friday) arm: keep it excluded and state the expected null, or add it as a separately gated arm.
4. Whether to extend past 2026-07-29 if the holdout is removed. Decide now.
5. Comparators 5, 6, 7, 9, 13, 15, 16 (SC0.10) and 19; move IFP to 12:00.
6. Apply the $1-per-order minimum to 1-lot single-leg comparators.
7. A FLAT15 sensitivity for legs that would deliver shares.
8. Separate reporting for third Fridays, and for monthly vs weekly expiries.
9. The QC Terms v1.5 export clause (owner's decision).

## Sources

[ise2011]: https://www.sec.gov/files/rules/sro/ise/2011/34-63878.pdf
[phlx2013]: https://www.sec.gov/files/rules/sro/phlx/2013/34-70682.pdf
[cboe2013]: https://www.sec.gov/files/rules/sro/cboe/2013/34-70685.pdf
[cboe2009]: https://sec.gov/rules/sro/cboe/2009/34-59824.pdf
[rhoads]: https://catalogimages.wiley.com/images/db/pdf/9781118616123.excerpt.pdf
[occ2013]: https://www.sec.gov/rules/sro/occ/2013/34-69480.pdf
[phlx2012]: https://www.govinfo.gov/content/pkg/FR-2012-09-05/pdf/2012-21766.pdf
[bx2021]: https://www.govinfo.gov/content/pkg/FR-2021-02-19/pdf/2021-03342.pdf
[sec104624]: https://www.sec.gov/files/rules/sro/ise/2026/34-104624.pdf
[sec103434]: https://www.sec.gov/files/rules/sro/ise/2025/34-103434.pdf
[iseA1]: https://www.sec.gov/comments/sr-ise-2025-15/srise202515-619387-1817874.pdf
[cboe2026]: https://cdn.cboe.com/resources/regulation/rule_filings/approved/2026/SR-CBOE-2026-007.pdf
[ota]: https://www.nasdaqtrader.com/MicroNews.aspx?id=OTA2026-2
[cboeNotice]: https://www.cboe.com/notices/content/?id=58202
[firstrade]: https://help.firstrade.info/en/articles/13604696-new-monday-wednesday-options-expirations-now-available
[miaxQ1]: https://www.miaxglobal.com/alert/2026/01/20/miax-exchange-group-options-markets-listing-monday-and-wednesday-weekly-0
[miaxQ2]: https://www.miaxglobal.com/alert/2026/04/06/miax-exchange-group-options-markets-listing-monday-and-wednesday-weekly-2
[miaxQ3]: https://www.miaxglobal.com/alert/2026/07/01/miax-exchange-group-options-markets-listing-monday-and-wednesday-weekly-1
[miaxQ4]: https://www.miaxglobal.com/alert/2026/10/01/miax-exchange-group-options-markets-qualified-securities-q4-2026
[box005]: https://boxexchange.com/assets/Notice-2026-005-New-Short-Term-Option-Series.pdf
[box064]: https://boxexchange.com/assets/Notice-2026-064-New-Short-Term-Option-Series.pdf
[fr17460]: https://www.govinfo.gov/content/pkg/FR-2026-08-27/pdf/2026-17460.pdf
[fr17457]: https://www.govinfo.gov/content/pkg/FR-2026-08-27/pdf/2026-17457.pdf
[sec106100]: https://www.sec.gov/files/rules/sro/ise/2026/34-106100.pdf
[miaxAug]: https://www.miaxglobal.com/alert/2026/08/17/miax-exchange-group-options-markets-listing-addtional-weekly-expirations
[sec96281]: https://www.sec.gov/files/rules/sro/ise/2022/34-96281.pdf
[miax2021]: https://www.miaxglobal.com/alert/2021/10/01/miax-exchange-group-options-markets-listing-monday-and-wednesday-weekly
[weeklys]: https://www.cboe.com/available_weeklys/get_csv_download/
[slope]: https://slopeofhope.com/socialtrade/item/602
[schaeffers]: <https://www.schaeffersresearch.com/content/options/2015/01/23/tesla-motors-inc-(tsla)-rally-electrifies-short-term-bulls>
[penny]: https://www.govinfo.gov/content/pkg/FR-2020-04-07/html/2020-07224.htm
[cboePenny]: https://cdn.cboe.com/resources/product_update/2022/Penny-Interval-Program_122122.pdf
[oic]: https://www.optionseducation.org/news/penny-increments
[ric2008]: <https://m.nasdaqtrader.com/Content/NewsAlerts/ISERics/RIC-2008-29$OCC_Rule_Change_Automatic_exercise_reduction$20080616.pdf>
[finra2021]: https://www.finra.org/rules-guidance/notices/information-notice-020321
[finra2025]: https://www.finra.org/rules-guidance/notices/information-notice-20251117
[r2360]: https://www.finra.org/rules-guidance/rulebooks/finra-rules/2360
[ethFaq]: https://www.cboe.com/document/tech-spec/document/technical-specifications/equity-options-extended-trading-hours-faq/
[cboePR]: https://ir.cboe.com/news/news-details/2026/Cboe-Receives-SEC-Approval-to-Offer-Extended-Trading-Hours-for-Select-Multi-Listed-Single-Stock-Options/default.aspx
[sec105569]: https://www.sec.gov/files/rules/sro/cboe/2026/34-105569.pdf
[thetrade]: https://www.thetradenews.com/cboe-extends-trading-hours-to-single-stock-and-etf-options/
[moomooEth]: https://www.moomoo.com/us/learn/options-extended-trading-hours
[nyse]: https://www.nyse.com/markets/hours-calendars
[secRT]: https://www.sec.gov/files/roundtable-options-market-structure.pdf
[tradersMag]: https://www.tradersmagazine.com/departments/options/vol-report-0dte-flex-options-are-2025-heroes/
[sifma]: https://www.sifma.org/wp-content/uploads/2026/04/SIFMA-Insights-Equities-and-Options-Monthly-Metrics_April_2026.pdf
[cboeQ1]: https://www.cboe.com/insights/posts/the-state-of-the-options-industry-q-1-2026
[cboeQ2]: https://www.cboe.com/insights/posts/state-of-the-options-industry-options-market-continued-to-break-records-in-q-2-2026
[strasmore]: https://www.strasmore.com/blog/lang/zh-hans/when-do-0dte-options-trade
[ibkrComm]: https://www.interactivebrokers.com/en/pricing/commissions-options.php
[cboeFees]: https://www.cboe.com/us/options/membership/fee_schedule/cone/
[taf]: https://www.finra.org/rules-guidance/rulebooks/corporate-organization/section-1-member-regulatory-fees
[sec31a]: https://www.sec.gov/rules-regulations/fee-rate-advisories/2025-2
[sec31b]: https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2
[ibkb1767]: https://ibkb.interactivebrokers.com/article/1767
[r4210]: https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210
[sa]: https://stockanalysis.com/stocks/tsla/history/
[ibkrDel]: https://www.interactivebrokers.com/en/trading/delivery-exercise-actions.php
[ibkrLesson]: https://www.interactivebrokers.com/campus/trading-lessons/exercise-and-assignment/
[eliteLiq]: https://www.elitetrader.com/et/threads/wondering-about-forced-liquidation-of-long-options-just-before-expiration.350496/
[rn2610]: https://www.finra.org/rules-guidance/notices/26-10
[rn2611]: https://www.finra.org/rules-guidance/notices/26-11
[finraInterp]: https://www.finra.org/rules-guidance/guidance/interpretations-finras-margin-rule
[finraInv]: https://www.finra.org/investors/investing/investment-products/stocks/day-trading
[fin2026022]: https://www.finra.org/rules-guidance/rule-filings/sr-finra-2026-022
[rn2113]: https://www.finra.org/rules-guidance/notices/21-13
[npp2005]: https://scholars.hkbu.edu.hk/en/publications/stock-price-clustering-on-option-expiration-dates-3/
[zcc2015]: https://wpunj.edu/Weekly%20Options%20on%20Stock%20Pinning%20upto%20page%208.pdf
[elms]: https://api.crossref.org/works/10.2139/ssrn.6564078
[btw]: https://afajof.org/management/viewp.php?n=98196
[ppw2006]: https://www.ou.edu/dam/price/Finance/CFS/paper/pdf/pearsonPoteshmanWhite.pdf
[npw2021]: https://academic.oup.com/rfs/article/34/4/1952/5873006
[bm2021]: https://www.efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2021-Leeds/papers/EFMA%202021_stage-2049_question-Full%20Paper_id-237.pdf
[bds2023]: http://www.efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2024-Lisbon/papers/EndofDayReversal_withnames.pdf
[agpv]: https://cdn.cboe.com/resources/education/research_publications/gammasqueezes.pdf
[bv2014]: https://ideas.repec.org/a/bla/jfinan/v69y2014i4p1485-1527.html
[bps2023]: https://lbsresearch.london.edu/id/eprint/2827/1/The%20Journal%20of%20Finance%20-%202023%20-%20BRYZGALOVA%20-%20Retail%20Trading%20in%20Options%20and%20the%20Rise%20of%20the%20Big%20Three%20Wholesalers.pdf
[dss]: https://academic.oup.com/rof/article-lookup/doi/10.1093/rof/rfaf052
[qpRetail]: https://quantpedia.com/how-retail-losses-money-in-option-trading/
[bmur]: https://www.lsu.edu/business/files/event-files/2025-finance-mardi-gras/retail_option_trading_v2.pdf
[cxoBM]: https://www.cxoadvisory.com/?p=45195
[bbg2023]: https://api.crossref.org/works/10.2139/ssrn.4404704
[neudata]: https://neudata.co/literature-reviews/zero-day-to-expiry-options-a-losing-bet-for-retail-traders
[mn2020]: https://ideas.repec.org/a/eee/jfinec/v136y2020i1p219-238.html
[cxoMN]: https://www.cxoadvisory.com/equity-options/intraday-versus-overnight-option-returns/
[mp2020]: https://ideas.repec.org/a/oup/rfinst/v33y2020i11p4973-5014..html
[cxoMP]: https://www.cxoadvisory.com/equity-options/timing-option-trades-to-suppress-trading-frictions/
[js2018]: https://ideas.repec.org/a/bla/jfinan/v73y2018i2p861-900.html
[gs2009]: http://www.ruf.rice.edu/~jgsfss/goyal_041808.pdf
[ch2013]: https://optionmetrics.com/research/j-cao-and-b-han-cross-section-of-option-returns-and-idiosyncratic-stock-volatility/
[chtz]: https://abfer.org/media/abfer-events-2016/annual-conference/investment-finance/226-option-return-predictability.pdf
[chtzRFS]: https://ideas.repec.org/a/oup/rfinst/v35y2022i3p1394-1442..html
[gxz]: https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/anticipating-uncertainty-straddles-around-earnings-announcements/7B34877AD5E06304BA3C55FBA3219FDD
[ss2013]: https://ideas.repec.org/a/eee/jbfina/v37y2013i11p4226-4240.html
[qpOE]: https://quantpedia.com/strategies/option-expiration-week-effect
[chiang]: https://ideas.repec.org/a/eee/empfin/v28y2014icp273-290.html
[fgz]: https://optioncharts.io/blog/how-accurate-is-max-pain-in-option-trading
[optcafe]: https://options.cafe/blog/0dte-opening-range-breakout-strategy-spy-backtested-results/
[so2019]: https://steadyoptions.com/articles/pinning-down-the-%E2%80%98option-pinning%E2%80%99-r523/
[so2026]: https://steadyoptions.com/articles/strike-price-effects-or-pinning-revisted-r830/
[oa2023]: https://optionalpha.com/blog/0dte-options-strategy-performance
[oa2022]: https://optionalpha.com/blog/0dte
[elitePin]: https://www.elitetrader.com/et/threads/weekly-pin-butterfly-strategy.354495/
[ragingbull]: https://ragingbull.com/total-alpha/friday-lotto-options/
[oaStraddle]: https://optionalpha.com/podcast/long-straddle-earnings-option-strategy
[my0dte]: https://my0dteoptions.com/blog/best-tickers-0dte-options-trading/
[my0dteList]: https://my0dteoptions.com/blog/stocks-with-0dte-options/
[otc]: https://optiontradingcoach.com/2025/04/04/scalping-profits-0dte-options-tsla-nvda/
[spotgamma]: https://spotgamma.com/0dte/
[moomooTSLA]: https://www.moomoo.com/community/feed/nvidia-0dte-holders-move-to-longer-dated-options-tesla-options-113012716470277
[moomooScalp]: https://www.moomoo.com/community/feed/my-earnings-playbook-scalping-short-term-profit-using-tsla-0dte-114994622627846
[moomooApr]: https://www.moomoo.com/community/feed/tesla-options-turn-into-scalpers-market-as-0dte-traders-brace-116381155196933
[omCC]: https://optionmetrics.com/blog/spx-0dte-covered-call-optimal-strike-selection/
[wallst247]: https://247wallst.com/investing/etf/2026/09/30/wall-street-now-sells-11-weekly-pay-etfs-only-three-have-beaten-the-sp-here-they-are/
[youtube]: https://www.youtube.com/watch?v=w_3JexgJ7e0
[jianshu]: https://www.jianshu.com/p/fb63b5329fb1
[hstong2024]: https://www.hstong.com/news/detail/23112716051601842
[hstongScan]: https://www.hstong.com/news/detail/26080708300105341
[hstong2025]: https://www.hstong.com/news/detail/25042211345835771
[hstongNVDA]: https://news.hstong.com/post/content/26051814244439517
[tigerXBZ]: https://www.laohu8.com/post/877289130
[tigerGL2]: https://www.laohu8.com/post/454410920182280
[xueqiuDuan]: https://xueqiu.com/7616738657/273832329
[sinaDuan]: https://finance.sina.cn/hkstock/gggd/2021-06-30/detail-ikqcfnca4205484.d.html
[xueqiuStraddle]: https://xueqiu.com/9132882841/211300917
[macrochen]: https://github.com/macrochen/earnings-iv-crush-strategy-skill
[eastmoney]: https://caifuhao.eastmoney.com/news/20251029123157304034820
[einverne]: https://blog.einverne.info/post/2026/04/0dte-options-strategies-guide.html
[sinaAI]: https://finance.sina.com.cn/stock/usstock/summary/2026-07-01/doc-iniffqch1288119.shtml
[tuttle485]: https://www.sec.gov/Archives/edgar/data/1771146/000183988225001546/zeodte_485apos-011025.htm
[tuttleLaunch]: https://www2.newsfilecorp.com/release/271479/Tuttle-Capital-Management-Launches-FirstEver-0DTE-Covered-Call-ETF-on-Strategy
[saMSTK]: https://stockanalysis.com/etf/mstk/
[xetfsPR]: https://finviz.com/news/354930/exchange-traded-concepts-announces-launch-of-xetfs-nvda-daily-income-etf-nyse-nyyy-and-xetfs-tsla-daily-income-etf-nyse-tyyy-with-xetfs
[etfcentral]: https://www.etfcentral.com/news/nyyy-and-tyyy-a-new-spin-on-covered-calls-for-nvda-and-tesla-investors
[etfdbTYYY]: https://etfdb.com/fs-insight-reports/TYYY_insight_report.pdf
[saNYYY]: https://stockanalysis.com/etf/nyyy/
[saTYYY]: https://stockanalysis.com/etf/tyyy/
[nvdy]: https://yieldmaxetfs.com/wp-content/uploads/funds/NVDY/files/summary-prospectus.pdf
[tsly]: https://yieldmaxetfs.com/wp-content/uploads/funds/TSLY/files/summary-prospectus.pdf
[zacks]: https://finviz.com/news/61105/high-yield-etfs-big-payouts-bigger-trade-offs
[tslp]: https://www.sec.gov/Archives/edgar/data/0001782952/000199937126021674/tslp-497k_093026.htm
[gsSupp]: https://www.sec.gov/Archives/edgar/data/1689873/000149315225006679/form497.htm
[gsTSR]: https://graniteshares.com/media/q5uphl0s/grsh-tsyy-tsr-20260630.pdf
[incomeshares]: https://incomeshares.com/en/insights/incomeshares-etp-performance-vs-underlying-assets-2025
[rex]: https://rexshares.com/rex-growth-and-income-etfs
[saMST]: https://stockanalysis.com/etf/mst/
[nvdw]: https://www.roundhillinvestments.com/etf/nvdw/
[tmgn]: https://www.tappalphafunds.com/etfs/tmgn
[qcEquities]: https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/algoseek/us-equities
[qcOpt]: https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/algoseek/us-equity-options
[qcOptData]: https://www.quantconnect.com/data/algoseek-us-equity-options
[qcPricing]: https://www.quantconnect.com/pricing
[qcAnn]: https://www.quantconnect.com/announcements/21283/equity-and-index-options-datafeed/
[qcGuide]: https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/research-guide
[qcAdmin]: https://www.quantconnect.com/docs/v2/cloud-platform/organizations/administration
[qcDeploy]: https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/deployment
[leanSetup]: https://github.com/QuantConnect/Lean/blob/master/Engine/Setup/BacktestingSetupHandler.cs
[leanUniv]: https://github.com/QuantConnect/Lean/blob/master/Common/Data/UniverseSelection/OptionUniverse.cs
[leanChain]: https://github.com/QuantConnect/Lean/blob/master/Engine/DataFeeds/BacktestingChainProvider.cs
[qcUnivDocs]: https://www.quantconnect.com/docs/v2/writing-algorithms/universes/equity-options
[pr9237]: https://github.com/QuantConnect/Lean/pull/9237
[pr9796]: https://github.com/QuantConnect/Lean/pull/9796
[pr9162]: https://github.com/QuantConnect/Lean/pull/9162
[leanSym]: https://github.com/QuantConnect/Lean/blob/master/Common/Securities/Option/OptionSymbol.cs
[leanFilter]: https://github.com/QuantConnect/Lean/blob/master/Common/Securities/ContractSecurityFilterUniverse.cs
[leanQB]: https://github.com/QuantConnect/Lean/blob/master/Common/Data/Market/QuoteBar.cs
[qcHandling]: https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/equity-options/handling-data
[qcHistory]: https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/history-requests
[pr9669]: https://github.com/QuantConnect/Lean/pull/9669
[leanHist]: https://github.com/QuantConnect/Lean/blob/master/Algorithm/QCAlgorithm.History.cs
[qcSched]: https://www.quantconnect.com/docs/v2/writing-algorithms/scheduled-events
[qcRes]: https://www.quantconnect.com/docs/v2/cloud-platform/organizations/resources
[qcCharting]: https://www.quantconnect.com/docs/v2/writing-algorithms/charting
[leanResults]: https://github.com/QuantConnect/Lean/blob/master/Engine/Results/BacktestingResultHandler.cs
[pr9760]: https://github.com/QuantConnect/Lean/pull/9760
[qcResults]: https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/results
[qcObj]: https://www.quantconnect.com/docs/v2/cloud-platform/object-store
[qcFiles]: https://www.quantconnect.com/docs/v2/cloud-platform/projects/files
[qcTerms]: https://www.quantconnect.com/terms
[leanCoarse]: https://github.com/QuantConnect/Lean/blob/master/Common/Data/UniverseSelection/CoarseFundamental.cs
[qcCoarse]: https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/quantconnect/us-equity-coarse-universe
[qcMstar]: https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/morningstar/us-fundamental-data.md
[qcMig]: https://www.quantconnect.com/docs/v2/writing-algorithms/universes/equity/morningstar-migration.md
[qcIndiv]: https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/equity-options/requesting-data/individual-contracts
[qcSettings]: https://www.quantconnect.com/docs/v2/writing-algorithms/universes/settings.md
[leanAlgo]: https://github.com/QuantConnect/Lean/blob/master/Engine/AlgorithmManager.cs
[qcCorp]: https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/corporate-actions
[qcAnn15483]: https://www.quantconnect.com/announcements/15483/options-and-futures-trading-on-quantconnect/
[forum19084]: https://www.quantconnect.com/forum/discussion/19084/option-data-missing-on-some-days/
[forum13447]: https://www.quantconnect.com/forum/discussion/13447/option-chain-provider-is-missing-some-contracts/
[leanAssign]: https://github.com/QuantConnect/Lean/blob/master/Common/Securities/Option/DefaultOptionAssignmentModel.cs
[L-s0]: </Users/bytedance/code/quant_stocks/.claude/worktrees/suggest-task-visibility-25008f/docs/research_ledger_stock_0dte.md>
[L-zero]: </Users/bytedance/code/quant_stocks/.claude/worktrees/suggest-task-visibility-25008f/docs/research_ledger_zero_dte.md>
[L-puts]: </Users/bytedance/code/quant_stocks/.claude/worktrees/suggest-task-visibility-25008f/docs/research_ledger_qc_stock_puts.md>
[L-px]: </Users/bytedance/code/quant_stocks/.claude/worktrees/suggest-task-visibility-25008f/cleaned_stocks_data/price/>