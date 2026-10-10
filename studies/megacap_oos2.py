"""Mega-cap out-of-sample test 1999-2013 on a self-built point-in-time Nasdaq market-cap ranking (no QuantConnect).

Pre-registered in docs/research_ledger_megacap.md, section OOS.2. Rules unchanged from section 0.4:
  M1 top 10 Nasdaq-listed US common stocks by point-in-time market cap, equal weight, monthly
  M2 top 5, equal weight
  M6 top 10, cap-weighted
Execution and costs from section 0.3 (signal at the last session of a month, trade at the next session's close,
IBKR Tiered + fees + half-spread, $10k, fractional shares, 25% band). Judged vs QQQ total return 1999-03-10..2013-12-31
(A: CAGR above QQQ and monthly excess t >= 2.13; B: max drawdown >= 10 pp shallower and CAGR within 3 pp);
ONEQ (2003-10..2013-12) is reported only.

Data (built by scripts/megacap_oos2_data.py; vendor price levels stay under research_cache/megacap_oos2/):
  candidates   output/research_only/megacap_oos2/candidates_input.csv (Nasdaq-100 Trust holdings + additions, CIKs,
               price sources, Nasdaq listing interval)
  shares       SEC cover-page shares outstanding (sec_cover_facts.csv) and XBRL dei (sec_dei_shares.csv); a fact is
               usable at signal s when filed < s and filed within 400 days of s
  market cap   shares(as of date a) x F(a) x close_adj(s), where close_adj is split-adjusted to a reference date T and
               F(a) is the product of the splits after a (up to T). This equals shares x raw close x later splits, and
               needs no split dates: only which side of each split the share count was measured on (see split_units).
  prices       Yahoo -> Tiingo -> Wayback copies of Yahoo's old table.csv -> companiesmarketcap.com daily market caps

Usage:  PYTHONPATH=. .venv/bin/python scripts/research_megacap_oos2.py [--check-ranks]
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from quant.data.megacap_history import (  # noqa: F401  (o2.* names read by tests)
    END, FULL, JUDGE_FROM, LOCAL, ONEQ_FROM, PERIODS, SNAPSHOT_SIGNALS, build, engine_panels, fix_share_outliers,
    load_oneq, load_qqq, market_caps, period_stats, rank_table, signal_sessions, split_units,
)
from quant.evaluation.metrics import yearly as _yearly
from quant.paths import output_dir
from quant.strategies import megacap as mc
from quant.data.sources import megacap_oos2 as md   # the OOS.2 sources (SEC facts, candidate list, prices)

OUT = output_dir("megacap_oos2")
T_THRESHOLD = 2.13                               # one-sided 5% Bonferroni for 3 trials (OOS.0)
RULES = tuple(r for r in mc.RULES if r.name in ("M1", "M2", "M6"))


# ======================================================================== metrics vs QQQ


def criteria(full: dict) -> dict:
    a = full["cagr"] > full["bench_cagr"] and full["t_monthly_excess"] >= T_THRESHOLD
    a_nominal = full["cagr"] > full["bench_cagr"] and full["t_monthly_excess"] >= 2.0
    b = (abs(full["bench_max_dd"]) - abs(full["max_dd"])) * 100 >= 10 and full["cagr"] >= full["bench_cagr"] - 0.03
    return {"A": bool(a), "A_nominal_t_ge_2": bool(a_nominal), "B": bool(b), "pass": bool(a or b)}


# ======================================================================== ranking checks


def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    cand, sessions, panel, srcs, facts, metas = build()
    srcs.to_csv(OUT / "price_sources.csv", index=False)
    facts.assign(asof=facts["asof"].dt.date, filed=facts["filed"].dt.date).to_csv(OUT / "sec_shares_pit.csv", index=False)
    signals = signal_sessions(sessions)
    mcaps = market_caps(cand, facts, panel, signals)
    ranks = rank_table(mcaps, cand)
    ranks.drop(columns=["mcap_bn"]).to_csv(OUT / "ranks_top20_by_month.csv", index=False)   # ranks + IDs only in git
    ranks.to_csv(LOCAL / "ranks_top20_by_month_with_mcap.csv", index=False)
    dec = ranks[ranks["s"].str[5:7] == "12"]
    print("December top 10 (market cap $bn; * = not from SEC shares x price):")
    for s, g in dec.groupby("s"):
        print(" ", s, ", ".join(f"{k}({m:.0f}{'' if str(x).startswith('sec') else '*'})"
                                for k, m, x in zip(g.key[:10], g.mcap_bn[:10], g.src[:10])),
              "| 11-15:", ", ".join(g.key[10:15]))
    src_share = mcaps.merge(ranks[ranks["rank"] <= 20][["s", "key"]].assign(s=lambda x: pd.to_datetime(x["s"])),
                            on=["s", "key"])["src"].value_counts(normalize=True).round(3)
    print("market-cap source share among monthly top 20:", src_share.to_dict())
    if args.check_ranks:
        return {}
    return simulate_all(cand, sessions, panel, facts, signals, mcaps, ranks, srcs)


def simulate_all(cand, sessions, panel, facts, signals, mcaps, ranks, srcs) -> dict:
    idx, close, last_row, term = engine_panels(cand, sessions, panel, facts)
    term.to_csv(OUT / "terminal_events.csv", index=False)
    qqq = load_qqq()
    oneq = load_oneq()
    qqq_close = qqq["close"].reindex(sessions)
    qqq_lvl = qqq["adjclose"].reindex(sessions)
    ranked = mcaps.rename(columns={"key": "security_id", "src": "mcap_src"}).assign(dv50_rank=1.0)
    results, by_year, holds, navs = {}, [], [], {}
    for rule in RULES:
        tg = mc.build_targets(rule, ranked, {}, qqq_close)
        sim = mc.simulate(tg, sessions, idx, close, last_row, qqq_lvl.fillna(1.0), qqq_close.fillna(50.0))
        nav = sim["nav"]
        jd = nav.index[nav.index >= pd.Timestamp(JUDGE_FROM)]
        bq = mc.buy_hold(qqq["adjclose"], qqq["close"], jd, mc.QQQ_HS)
        od = nav.index[nav.index >= oneq.index.min()]
        bo = mc.buy_hold(oneq["adjclose"], oneq["close"], od, mc.ONEQ_HS)
        per = {p: period_stats(nav, bq, a, b) for p, (a, b) in PERIODS.items()}
        per_oneq = period_stats(nav, bo, ONEQ_FROM, END)
        crit = criteria(per[FULL])
        r = nav.pct_change().dropna()
        yq, yo, ys = _yearly(bq.pct_change().dropna()), _yearly(bo.pct_change().dropna()), _yearly(r)
        for y, v in ys.items():
            by_year.append({"rule": rule.name, "year": y, "strategy": v, "qqq": yq.get(y), "oneq": yo.get(y)})
        # crash: 2000-03-10 (Nasdaq top) .. 2002-10-09 (bottom), and the drawdown inside 1999-03-10..2002-12-31
        crash = {}
        for nm, s_ in (("strategy", nav), ("qqq", bq)):
            v = s_[(s_.index >= pd.Timestamp("2000-03-10")) & (s_.index <= pd.Timestamp("2002-10-09"))]
            crash[nm] = float(v.iloc[-1] / v.iloc[0] - 1)
        for s, lst in tg.items():
            for k, (sid, w, rk, mcap, srcx) in enumerate(lst, 1):
                holds.append({"rule": rule.name, "signal": s.date().isoformat(), "slot": k, "key": sid,
                              "weight": round(w, 4), "mcap_src": srcx})
        navs[rule.name] = nav
        results[rule.name] = {"start": str(sim["start"].date()), "periods": per, "vs_oneq_2003_10_2013": per_oneq,
                              "criteria_vs_qqq": crit, "crash_2000_03_10_to_2002_10_09": crash,
                              "avg_names_held": float(sim["names"].mean()), "orders": int(sim["orders"].sum()),
                              "cost_usd": float(sim["cost"].sum())}
        f = per[FULL]
        print(f"{rule.name}: full CAGR {f['cagr']:+.1%} vs QQQ {f['bench_cagr']:+.1%} | MDD {f['max_dd']:.1%} vs "
              f"{f['bench_max_dd']:.1%} | t {f['t_monthly_excess']:.2f} | A {crit['A']} B {crit['B']} pass {crit['pass']}")
    navs["QQQ_from_1999_03_10"] = bq
    navs["ONEQ_from_2003_10"] = bo
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    pd.DataFrame(holds).to_csv(OUT / "holdings_by_signal.csv", index=False)
    pd.DataFrame(navs).to_csv(LOCAL / "nav_daily.csv")
    snap = pd.DataFrame(holds)
    snap = snap[snap["signal"].isin(SNAPSHOT_SIGNALS)]
    snap.to_csv(OUT / "holdings_snapshots.csv", index=False)
    out = {"periods": PERIODS, "t_threshold": T_THRESHOLD, "results": results,
           "price_sources": srcs.to_dict("records"), "terminal_events": term.to_dict("records")}
    (OUT / "results.json").write_text(json.dumps(out, indent=1, default=lambda x: round(x, 6) if isinstance(x, float)
                                                 else str(x)) + "\n")
    return out


# ======================================================================== data checks (no returns)

def venue_check(cover: pd.DataFrame, cand: pd.DataFrame) -> pd.DataFrame:
    """Each 10-K cover's section 12(b) text against the Nasdaq interval of the candidate list. Before Nasdaq became an
    exchange (2006-08) a Nasdaq company registered under 12(g) only ('none_12b'). Rows that disagree are flagged."""
    k = cover[cover["form"].astype(str).str.startswith("10-K")].copy()
    iv = cand.set_index("key")[["nasdaq_from", "nasdaq_to"]]
    rows = []
    for r in k.itertuples():
        a, b = iv.loc[r.key, "nasdaq_from"], iv.loc[r.key, "nasdaq_to"]
        filed = pd.Timestamp(r.filed)
        inside = (not a or filed >= pd.Timestamp(a)) and (not b or filed <= pd.Timestamp(b))
        v = r.venue_12b if isinstance(r.venue_12b, str) else ""
        says_nasdaq = v in ("NASDAQ", "none_12b")
        flag = (inside and v in ("NYSE", "AMEX")) or ((not inside) and v == "NASDAQ")
        rows.append({"key": r.key, "filed": r.filed, "venue_12b": v, "inside_nasdaq_interval": inside,
                     "flag": bool(flag), "consistent_nasdaq": bool(inside and says_nasdaq),
                     "text": str(r.venue_text)[:160] if isinstance(r.venue_text, str) else ""})
    return pd.DataFrame(rows)


def share_qc(facts: pd.DataFrame, jump: float = 0.2) -> pd.DataFrame:
    """Share counts in price-basis units (shares x F) whose ratio to the previous count is off by more than ``jump``
    (log scale): parse errors, a count measured on the other side of a split than assumed, or real events
    (mergers, spin-offs). Each flagged row is checked by hand (sec_share_overrides.csv)."""
    out = []
    for key, g in facts.groupby("key"):
        g = g.sort_values(["asof", "filed"]).copy()
        u = g["shares"] * g["F"]
        g["units"] = u
        g["ratio_prev"] = u / u.shift(1)
        g["ratio_next"] = u.shift(-1) / u
        bad = (np.abs(np.log(g["ratio_prev"])) > np.log(1 + jump)) | (np.abs(np.log(g["ratio_next"])) > np.log(1 + jump))
        out.append(g[bad.fillna(False)])
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check-ranks", action="store_true", help="only build and print the market-cap ranking")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()


def sanity_vs_published(ranks: pd.DataFrame) -> pd.DataFrame:
    """December top 15 against companiesmarketcap.com's year-end market caps (an independent vendor series) and the
    Nasdaq-100 Trust's September 30 schedule rank (SEC-published; NDX weights include foreign names, exclude
    financials and use modified cap weights, so only gross disagreements are informative)."""
    rows = []
    dec = ranks[(ranks["s"].str[5:7] == "12") & (ranks["rank"] <= 15)]
    cmc = {}
    for k, slug in md.CMC_SLUGS.items():
        try:
            cmc[k] = md.cmc_year_end_caps(slug)
        except Exception:  # noqa: BLE001
            cmc[k] = {}
    for r in dec.itertuples():
        y = int(r.s[:4])
        ref = cmc.get(r.key, {}).get(y)
        rows.append({"s": r.s, "rank": r.rank, "key": r.key, "mcap_bn": r.mcap_bn, "src": r.src,
                     "cmc_year_end_bn": round(ref / 1e9, 1) if ref else None,
                     "ratio_to_cmc": round(r.mcap_bn / (ref / 1e9), 3) if ref else None})
    out = pd.DataFrame(rows)
    out["flag"] = out["ratio_to_cmc"].notna() & ((out["ratio_to_cmc"] - 1).abs() > 0.15)
    return out
