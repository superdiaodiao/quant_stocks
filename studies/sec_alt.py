"""Two signals from free SEC data (pre-registered in docs/research_ledger_sec_alt.md, section 0, before any result).

(I) Insider buying (Form 4, SEC "Insider Transactions Data Sets"):
    I1  opportunistic officer / director open-market purchase (code P) >= $50k in one Form 4 -> buy at the close of
        the session after the FILING date, hold 63 sessions, at most 10 names, NAV / 10 each.
    I2  cluster buys: >= 3 distinct opportunistic officers / directors buying within a 30-day trade-date window.
    I3  I1 restricted to market cap < $5B (XBRL frames dei:EntityCommonStockSharesOutstanding x raw close).
    "Routine" insiders (Cohen-Malloy-Pomorski): an open-market trade in the same calendar month in each of the three
    previous calendar years (filings known before 1 January) -> their purchases are dropped.
(II) 13F cloning (EDGAR 13F-HR information tables):
    F1  Berkshire Hathaway top 10 by reported value, priced names equal weight, re-weighted at the close of the
        session after each filing's acceptance date.
    F2  union of the top 10 of Akre Capital, Gardner Russo & Quinn, Ruane Cunniff & Goldfarb (chosen before results).

Prices: the frozen Nasdaq panel via quant.data.panel.load_window (total-return index, terminal returns,
successor stitching); names outside the panel are skipped and counted. Costs: IBKR Pro Tiered for the actual dollar
size (scripts/research_reversal_dev.order_cost / half_spread). Benchmark ONEQ total return (QQQ reported).
Account: $10,000 cash at the close of 2013-12-31, last session 2026-08-31; halves 2014-2019 and 2020-2026-08.
SEC readers: quant.data.sec_alt; signals and engines: quant.strategies.sec_alt; metrics:
quant.evaluation.periods.segment_metrics.

Raw SEC files are cached locally under research_cache/sec_alt/ (never in git). The SEC User-Agent comes from
src/io/sec_contact.py and is never printed; requests are throttled to <= 3 per second.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd

from quant.data import panel
from quant.data import sec_alt as sd
from quant.data import version as dv
from quant.data.guards import assert_window
from quant.data.rates import daily_rf
from quant.evaluation.criteria import ab_verdict_on, bonferroni_t
from quant.evaluation.periods import segment_metrics
from quant.paths import ROOT
from quant.strategies import sec_alt as sa

# ======================================================================== frozen configuration

MAIN = Path("/Users/bytedance/code/quant_stocks")
CACHE = MAIN / "research_cache/sec_alt"
RAW_INSIDER = CACHE / "raw/insider"
RAW_FRAMES = CACHE / "raw/frames"
RAW_13F = CACHE / "raw/13f"
BENCH_DIR = MAIN / "research_cache/benchmarks"
INPUTS = dv.INPUTS
OUT = dv.versioned(ROOT / "output/research_only/sec_alt")
LEDGER = ROOT / "docs/research_ledger_sec_alt.md"

START = "2013-12-31"          # $10,000 cash at this close; first return 2014-01-02
END = "2026-08-31"            # last panel session
HALVES = (("2014-2019", "2014-01-01", "2019-12-31"), ("2020-2026-08", "2020-01-01", END))
ACCOUNT = 10_000.0
INSIDER_QUARTERS = [f"{y}q{q}" for y in range(2011, 2027) for q in range(1, 5) if (y, q) <= (2026, 2)]
FRAME_QUARTERS = [f"CY{y}Q{q}I" for y in range(2012, 2027) for q in range(1, 5) if (2012, 4) <= (y, q) <= (2026, 2)]
LAST_FORM4_FILING = "2026-06-30"
SHARES_LAG_DAYS = 45
MIN_PURCHASE = 50_000.0
CLUSTER_N, CLUSTER_DAYS = 3, 30
HOLD_SESSIONS = 63
K = 10
MCAP_MAX = 5e9
REBAL_BAND = 0.01
BERKSHIRE = {"Berkshire Hathaway": ["1067983"]}
F2_MANAGERS = {"Akre Capital": ["1112520"], "Gardner Russo & Quinn": ["860643"],
               "Ruane Cunniff & Goldfarb": ["728014", "1720792"]}
BAD_TITLE = re.compile(r"prefer|note|debenture|warrant|\bunits?\b|\bright", re.I)
N_TRIALS = 5
WINDOW = {"perf_start": START, "perf_end": END, "price_start": "2013-01-01",
          "universe_start": "2013-01-01", "judged_from": "2014-01-01"}

# Hand review of the name matches (identity and share class only; done before any return was computed).
# cusip -> panel security_id; CUSIPs not listed here are treated as "not in the panel".
# Checked against security_master (name, share class, Nasdaq listing); the panel prices a name only on sessions it
# has a close (e.g. Roper is in the panel only after its 2023 move to Nasdaq). Everything else in the 13F top tens
# (NYSE names such as WFC, KO, AXP, MA, V; foreign ordinaries / ADRs; BIDU, ICLR, SCHW not in the panel) is unpriced.
CUSIP_REVIEW: dict[str, str] = {
    "037833100": "320193",            # Apple
    "500754106": "1637459",           # Kraft Heinz
    "25490A309": "1465112",           # DIRECTV
    "16119P108": "1091667",           # Charter Communications class A
    "00507V109": "718877",            # Activision Blizzard
    "02079K305": "1652044.A",         # Alphabet class A
    "02079K107": "1652044.C",         # Alphabet class C
    "38259P508": "1288776.A",         # Google Inc class A (pre-2015; continues as 1652044.A)
    "256746108": "935703",            # Dollar Tree
    "778296103": "745732",            # Ross Stores
    "67103H107": "898173",            # O'Reilly Automotive
    "87236Y108": "1173431",           # TD Ameritrade
    "25470F302": "1437107.A",         # Discovery series A (later Warner Bros. Discovery)
    "78388J106": "1034054",           # SBA Communications class A (pre-2017 CUSIP)
    "78410G104": "1034054",           # SBA Communications class A (REIT CUSIP)
    "776696106": "882835",            # Roper (panel prices it from 2023-04 only)
    "92345Y106": "1442145",           # Verisk class A
    "G3075P101": "1363829",           # Enstar
    "00724F101": "796343",            # Adobe
    "22160N109": "1057352",           # CoStar
    "009066101": "1559720",           # Airbnb class A
    "217204106": "900075",            # Copart
    "64110L106": "1065280",           # Netflix
    "311900104": "815556",            # Fastenal
    "45168D104": "874716",            # IDEXX
    "24906P109": "818479",            # Dentsply Sirona
    "023135106": "1018724",           # Amazon
    "30303M102": "1326801",           # Facebook / Meta class A
    "225310101": "885550",            # Credit Acceptance
    "595112103": "723125",            # Micron
    "531229854": "1560385.T-FWONK",   # Liberty Media series C Formula One (pre-2023 CUSIP)
    "531229755": "1560385.T-FWONK",   # Liberty Media series C Liberty Formula One (post-2023 CUSIP)
}


# ======================================================================== data

def load_prices():
    data = panel.load_window("sec_alt", WINDOW)
    assert data.guard["effective_end"] == END, data.guard["effective_end"]
    assert_window(data.sessions, "2013-01-01", END, "panel sessions")
    return data


def load_insider_trades():
    return sd.load_insider_trades(CACHE, INSIDER_QUARTERS, RAW_INSIDER, LAST_FORM4_FILING)


def load_shares():
    return sd.load_shares(FRAME_QUARTERS, RAW_FRAMES, SHARES_LAG_DAYS)


def load_13f(managers: dict):
    return sd.load_13f(managers, RAW_13F, END)


def period_metrics(nav, oneq, qqq, rf, start: str, end: str, res: dict | None = None) -> dict:
    return segment_metrics(nav, oneq, qqq, rf, start, end, res, account_start=START, account=ACCOUNT)


def judge(halves: list[dict], full: dict) -> dict:
    """A / B on the two halves with the full-period t (``criteria.ab_verdict_on``, keys A, B, pass)."""
    return ab_verdict_on(halves, full)


# ======================================================================== run

def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    data = load_prices()
    print("DATE GUARD:", data.guard["assertion"])
    sessions = data.sessions
    perf = sessions[(sessions >= pd.Timestamp(START)) & (sessions <= pd.Timestamp(END))]
    rank_of = sa.rank_lookup(data.universe)
    rf = daily_rf(perf, END)
    oneq, qqq = (sa.benchmark_nav(t, perf, BENCH_DIR, END, ACCOUNT) for t in ("ONEQ", "QQQ"))
    secmap = sd.load_secmap(data.perf_idx.columns, INPUTS)

    entries, counts, q = sa.insider_entries(
        data, secmap, load_insider_trades(), load_shares(), bad_title=BAD_TITLE, min_purchase=MIN_PURCHASE,
        cluster_n=CLUSTER_N, cluster_days=CLUSTER_DAYS, mcap_max=MCAP_MAX, start=START, end=END)
    res = {k: sa.run_insider(entries[k], data, rank_of, start=START, end=END, account=ACCOUNT, k=K,
                             hold=HOLD_SESSIONS) for k in ("I1", "I2", "I3")}
    for k in res:
        counts[k] = {**counts[k], "skipped_book_full": res[k]["skipped_full"],
                     "skipped_already_held": res[k]["skipped_already_held"], "skipped_no_cash": res[k]["skipped_cash"]}
        entries[k].to_csv(OUT / f"signals_{k}.csv", index=False)

    cmap = dict(CUSIP_REVIEW)
    # a reviewed security that never made the weekly top-300 universe after 2013 is not loaded -> unpriced
    missing = sorted({v for v in cmap.values() if v not in data.perf_idx.columns})
    cmap = {c: v for c, v in cmap.items() if v in data.perf_idx.columns}
    f1top, f2top = load_13f(BERKSHIRE), load_13f(F2_MANAGERS)
    u = pd.concat([f1top, f2top]).drop_duplicates("cusip")[["cusip", "name", "title"]]
    u.assign(security_id=u["cusip"].map(cmap)).sort_values("cusip").to_csv(OUT / "cusip_map.csv", index=False)
    t1, cov1 = sa.f13_targets(f1top, sessions, cmap, START, END)
    t2, cov2 = sa.f13_targets(f2top, sessions, cmap, START, END)
    res["F1"], res["F2"] = (sa.run_targets(t, data, rank_of, start=START, end=END, account=ACCOUNT, band=REBAL_BAND)
                            for t in (t1, t2))
    for name, top, cov in (("F1", f1top, cov1), ("F2", f2top, cov2)):
        top.assign(security_id=top["cusip"].map(cmap)).to_csv(OUT / f"top10_{name}.csv", index=False)
        cov.to_csv(OUT / f"coverage_{name}.csv", index=False)
        counts[name] = {"filings": int(len(cov)), "avg_top10_priced_n": float(cov["top10_priced_n"].mean()),
                        "avg_top10_value_share_priced": float(cov["top10_value_share_priced"].mean()),
                        "filings_with_zero_priced": int((cov["top10_priced_n"] == 0).sum()),
                        "rebalances": res[name]["n_rebalances"]}

    rows, verdict, navs = [], {}, {"ONEQ": oneq, "QQQ": qqq}
    for k, r in res.items():
        full = period_metrics(r["nav"], oneq, qqq, rf, "2014-01-01", END, r)
        hs = [period_metrics(r["nav"], oneq, qqq, rf, a, b, r) for _, a, b in HALVES]
        verdict[k] = judge(hs, full)
        for lab, m in [("full", full)] + [(h[0], x) for h, x in zip(HALVES, hs)]:
            rows.append({"config": k, "segment": lab, **m})
        navs[k] = r["nav"]
        r["trades"].to_csv(OUT / f"trades_{k}.csv", index=False)
    metrics = pd.DataFrame(rows)
    metrics.to_csv(OUT / "metrics.csv", index=False)
    pd.DataFrame(navs).to_csv(OUT / "daily_nav.csv")
    summary = {"pre_registration": str(LEDGER.relative_to(ROOT)), "trials": N_TRIALS,
               "reviewed_cusip_targets_not_loaded": missing,
               "bonferroni_t_one_sided_5pct": bonferroni_t(N_TRIALS), "window": [START, END],
               "halves": HALVES, "verdict": verdict, "coverage_and_counts": counts,
               "terminal_events_in_panel": int(len(data.terminal_events)), "date_guard": data.guard["assertion"]}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    show = ["cagr", "oneq_cagr", "qqq_cagr", "max_dd", "oneq_max_dd", "t_monthly_excess_vs_oneq", "trades"]
    for k in res:
        print(k, verdict[k])
        print(metrics[metrics["config"] == k].set_index("segment")[show].to_string())
    print(json.dumps(counts, indent=1, default=str))
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fetch", action="store_true", help="download / parse the SEC raw files only")
    ap.add_argument("--cusip-candidates", action="store_true", help="write the name-match table for the hand review")
    args = ap.parse_args(argv)
    if args.fetch:
        print("Form 4 rows:", len(load_insider_trades()))
        print("shares rows:", len(load_shares()))
        for mg in (BERKSHIRE, F2_MANAGERS):
            print("13F top-10 rows:", len(load_13f(mg)))
        return
    if args.cusip_candidates:
        data = load_prices()
        top = pd.concat([load_13f(BERKSHIRE), load_13f(F2_MANAGERS)])
        u = top.drop_duplicates("cusip")[["cusip", "name", "title"]]
        c = sd.cusip_candidates(u, data.perf_idx.columns, INPUTS)
        OUT.mkdir(parents=True, exist_ok=True)
        c.to_csv(OUT / "cusip_candidates.csv", index=False)
        print(c.to_string())
        return
    run(args)


if __name__ == "__main__":
    main()
