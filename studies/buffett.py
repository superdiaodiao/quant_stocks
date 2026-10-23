"""Buffett-related strategies (pre-registered in docs/research_ledger_buffett.md, section 0).

B0 (reference): BRK.B buy-and-hold vs ONEQ / QQQ / SPY. B1 (2 trials): Berkshire's 13F (CIK 1067983, 1999-2026,
NYSE included) cloned from the first session after each filing - B1a the top 10 equal-weighted, B1b every priced
position weighted by reported value; only information public at the filing date (amendment rule 0.4.1). B2 (4 trials):
a textbook Buffett screen (ROE > 15% five years running, debt / equity < 0.5, steady earnings growth, earnings yield
above the 10-year yield or lowest P/E; 10 or 20 names, annual) on the point-in-time Nasdaq panel's top 300 (data v2.1).
B3: the Frazzini-Kabiller-Pedersen factor replication (report only) and 50% QUAL + 50% USMV at 1.0x / 1.6x on an
IBKR margin loan (2 trials). Judged against ONEQ (A / B, Bonferroni over 8 trials); QQQ and SPY reported.
Data (local only, research_cache/buffett): SEC 13F texts, OpenFIGI, Yahoo / archive.org / Alpaca prices, AQR
factor files, FRED DGS10 (quant.data.berkshire_13f, berkshire_cusips, buffett_prices, aqr). Outputs:
output/research_only/buffett/ (returns, weights, ratios and statistics only, never vendor price levels).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study buffett [--fetch [--offline]] [--data-only | --register | --run]

Post-run fixes (2026-10-23, ledger run log): the 13F small-restatement rule no longer double-counts a restated name;
the B1 price series drop Alpaca's zero-volume placeholder bars, end before a stale run and add back distributions a
source misses (``--fetch --offline`` rebuilds the series file from the raw cache without any request); reported in
addition (not trials): B1 with no trade band, B2 with extended XBRL debt tags, the paper's factor regressions and a
factor decomposition of R2. Every --run appends the SHA-256 of each output file to run_history.jsonl.
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2.1")    # B2 is registered on data v2.1

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import pickle  # noqa: E402
import time  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant.backtest.margin_weights import simulate_margin_weights  # noqa: E402
from quant.backtest.rebalance import buy_hold_level, simulate_index_units  # noqa: E402
from quant.data import aqr  # noqa: E402
from quant.data import berkshire_13f as b13  # noqa: E402
from quant.data import berkshire_cusips as bc  # noqa: E402
from quant.data import buffett_prices as bp  # noqa: E402
from quant.data import version as dv  # noqa: E402
from quant.data.calendar import last_session_of_each_month  # noqa: E402
from quant.data.effr import load_effr  # noqa: E402
from quant.data.rates import daily_rf  # noqa: E402
from quant.data.sources import alpaca, openfigi  # noqa: E402
from quant.evaluation.criteria import bonferroni_t  # noqa: E402
from quant.evaluation.metrics import max_drawdown  # noqa: E402
from quant.paths import CACHE_ROOT, MAIN_CHECKOUT, ROOT, output_dir  # noqa: E402
from quant.reporting import Preregistration, RunLog, json_rounded, write_json  # noqa: E402
from quant.strategies import buffett as bf  # noqa: E402
from quant.strategies import fundamentals as fs  # noqa: E402

# ======================================================================== frozen configuration

END, PERIOD2, B2_END = "2026-09-30", "2026-10-03", "2026-08-31"
CACHE = CACHE_ROOT / "buffett"
RAW = CACHE / "raw"
OUT = output_dir("buffett")
LEDGER = ROOT / "docs/research_ledger_buffett.md"
PREREG = Preregistration(LEDGER, OUT / "prereg_frozen.json", OUT)
ENV_ALPACA = MAIN_CHECKOUT / ".env.alpaca"
EFFR_DIR = CACHE_ROOT / "leverage_methods/raw"
ACCOUNT = 10_000.0
BENCH_SYMBOLS = ("SPY", "QQQ", "ONEQ", "^IXIC", "BRK-B", "BRK-A", "QUAL", "USMV", "SPLV")
BENCH_FIRST = {"BRK-A": "1975-01-01"}         # BRK-A from its first Yahoo row (1980-03) for the B3 replication
ONEQ_FIRST_RETURN = "2003-10-02"
HS_BUYHOLD = 5e-4
# B1
B1_ENTRY = "1999-05-18"                       # first session after the first filing (1999-05-17)
B1_FOLDS = (("1999-05-19", "2012-12-31"), ("2013-01-02", END))
ONEQ_REAL_FROM = "2003-10-02"                 # the ONEQ basis: first return after ONEQ's first close
B1_TRIALS = {"B1a": "top10_ew", "B1b": "all_vw"}
B1_SENSITIVITY = {"B1a_vw (top10 value-weighted)": ("top10_vw", "public"),
                  "B1a originals only": ("top10_ew", "originals"), "B1b originals only": ("all_vw", "originals")}
B1_BAND_SENSITIVITY = {"B1a band 0": ("top10_ew", 0.0), "B1b band 0": ("all_vw", 0.0)}   # added after the review
STALE_HELD_REPORT = 10                        # data check: report held windows with this many unchanged sessions
PRICE_CHECK = {"lo": 0.80, "hi": 1.25, "max_fail": 0.20}
SERIES_FILE = CACHE / "derived/b1_series.pkl"   # the B1 price series, built by --fetch, read by --data-only / --run
ALPACA_CROSS_FROM = "2016-01-04"
# B2
B2_SIGNAL_MONTH = 4
B2_FIRST, B2_LAST = "2014-04-30", "2026-04-30"
B2_TRIALS = {"B2-EY10": ("EY", 10), "B2-EY20": ("EY", 20), "B2-PE10": ("PE", 10), "B2-PE20": ("PE", 20)}
B2_HALVES = (("2014-05-02", "2019-12-31"), ("2020-01-02", B2_END))
B2_CACHE = CACHE / "fundamentals_v2_1"
# B3
B3_TRIALS = {"B3i-1.0": ({"QUAL": 0.5, "USMV": 0.5}, 1.0), "B3i-1.6": ({"QUAL": 0.5, "USMV": 0.5}, 1.6)}
B3_SENSITIVITY = {"QUAL+SPLV 1.0": ({"QUAL": 0.5, "SPLV": 0.5}, 1.0), "QUAL+SPLV 1.6": ({"QUAL": 0.5, "SPLV": 0.5}, 1.6)}
B3_ENTRY = "2013-08-01"
B3_HS = 2e-4
MARGIN_SPREAD, MAINT, LIQ_SLIP = 0.015, 0.25, 0.005
FKP_SPREADS = (0.005, 0.015)
FKP_FACTOR_MODELS = {"MKT": ["MKT"], "4F": ["MKT", "SMB", "HML", "UMD"], "4F+BAB": ["MKT", "SMB", "HML", "UMD", "BAB"],
                     "6F": ["MKT", "SMB", "HML", "UMD", "BAB", "QMJ"]}      # the paper's Table 4 (added later)
FKP_REG_PERIODS = ("1980-04..2011-12", "2012-01..2026-08", "1980-04..2026-08")
FKP_PERIODS = {"1980-04..2011-12": ("1980-04", "2011-12"), "2012-01..2026-08": ("2012-01", "2026-08"),
               "1980-04..2026-08": ("1980-04", "2026-08"), "1976-01..2011-12": ("1976-01", "2011-12"),
               "1980s": ("1980-04", "1989-12"), "1990s": ("1990-01", "1999-12"), "2000s": ("2000-01", "2009-12"),
               "2010s": ("2010-01", "2019-12"), "2020s": ("2020-01", "2026-08"), "2000-2002": ("2000-01", "2002-12"),
               "2008": ("2008-01", "2008-12"), "2022": ("2022-01", "2022-12")}
AQR_LAST = "2026-08"
N_TRIALS = len(B1_TRIALS) + len(B2_TRIALS) + len(B3_TRIALS)
WINDOWS = {"2000-2002": ("2000-01-01", "2002-12-31"), "2008": ("2008-01-01", "2008-12-31"),
           "2020 crash-rebound": ("2020-02-20", "2020-06-30"), "2022": ("2022-01-01", "2022-12-31"),
           "1990s part": ("1996-01-01", "1999-12-31"), "2000s": ("2000-01-01", "2009-12-31"),
           "2010s": ("2010-01-01", "2019-12-31"), "2020s": ("2020-01-01", END)}


def led():
    return bp.ledger(CACHE)


def alpaca_headers():
    return alpaca.env_headers(ENV_ALPACA)


# ======================================================================== data

def benchmarks(fetch: bool, log) -> dict:
    """Sessions (SPY's), total-return levels and unadjusted closes of the benchmark / ETF symbols, ONEQ* and the
    daily T-bill."""
    for s in BENCH_SYMBOLS:
        if fetch:
            bp.fetch_yahoo(RAW, s, led(), PERIOD2, BENCH_FIRST.get(s, bp.YAHOO_FIRST))
    ser = {s: bp.yahoo_series(RAW, s, END, BENCH_FIRST.get(s, bp.YAHOO_FIRST)) for s in BENCH_SYMBOLS}
    sessions = ser["SPY"].tr.index
    sessions = sessions[sessions <= pd.Timestamp(END)]
    lvl = pd.DataFrame({s: v.tr.reindex(sessions) for s, v in ser.items()})
    px = pd.DataFrame({s: v.raw.reindex(sessions) for s, v in ser.items()})
    r_ixic = lvl["^IXIC"].pct_change()
    r_oneq = lvl["ONEQ"].pct_change()
    star = r_ixic.where(sessions < pd.Timestamp(ONEQ_FIRST_RETURN), r_oneq)
    lvl["ONEQ*"] = bf.level_from_returns(star.where(sessions >= sessions[1]))
    lvl.loc[sessions[0], "ONEQ*"] = 1.0
    rf = daily_rf(sessions, END)
    checks = {s: {"first": str(v.tr.index[0].date()), "last": str(v.tr.index[-1].date()), "rows": int(len(v.tr))}
              for s, v in ser.items()}
    log("benchmarks:", json.dumps(checks))
    return {"sessions": sessions, "lvl": lvl, "px": px, "rf": rf, "checks": checks}


def b1_universe(sessions: pd.DatetimeIndex, fetch: bool, log, mode: str = "public", offline: bool = False) -> dict:
    """13F states, CUSIP map, price series, 13F price check, accepted CUSIPs and the price matrices. ``fetch`` builds
    the price series file (``offline``: from the raw cache only); otherwise the frozen file is read."""
    fl, tables, events = b13.load(RAW / "13f", END, bc.CUSIP_FIXES, mode)
    states = b13.states_by_trade_session(events, sessions)
    checks = pd.concat([e["positions"].assign(report_date=e["report_date"], filing_date=e["filing_date"])
                        for e in events], ignore_index=True).drop_duplicates(["cusip", "report_date"])
    cus = sorted(checks["cusip"].unique())
    figi = openfigi.map_cusips(cus, RAW / "openfigi", led(), log=log) if fetch and not offline else \
        {c: json.loads(openfigi.cache_path(RAW / "openfigi", c).read_text()) for c in cus}
    tick = {c: (openfigi.us_listing(figi[c]) or {}).get("ticker") for c in cus}
    specs = {c: bc.spec_for(c, tick[c]) for c in cus}
    if fetch:
        win = bp.need_windows(checks, END)
        series, status = bp.build_series(specs, win, RAW, led(), END, PERIOD2, alpaca_headers, log=log,
                                         distributions=bc.DISTRIBUTIONS, offline=offline)
        SERIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        SERIES_FILE.write_bytes(pickle.dumps({"series": series, "status": status}))
    else:                                   # data stage and run: the frozen series file only (no request)
        got = pickle.loads(SERIES_FILE.read_bytes())
        series, status = got["series"], got["status"]
    pc = bp.price_check(checks[["cusip", "report_date", "filing_date", "value", "shares"]], series,
                        PRICE_CHECK["lo"], PRICE_CHECK["hi"])
    acc = bp.accept(pc, PRICE_CHECK["max_fail"])
    acc["accepted_exception"] = acc.index.isin(list(bc.ACCEPT)) & ~acc["accepted"]
    acc["accepted"] = acc["accepted"] | acc.index.isin(list(bc.ACCEPT))
    accepted = sorted(acc.index[acc["accepted"]])
    lv, px, last = bp.matrices(series, accepted, sessions)
    names = checks.groupby("cusip").agg(name=("name", "first"), title=("title", "first"))
    w = pd.concat([e["positions"].assign(w=e["positions"]["value"] / e["positions"]["value"].sum(),
                                         acc=e["accession"]) for e in states.values()])
    cmap = (pd.DataFrame({"cusip": cus}).set_index("cusip").join(names)
            .assign(figi_ticker=pd.Series(tick), skip_reason=pd.Series(bc.SKIP))
            .join(status.set_index("cusip")).join(acc)
            .assign(sum_state_weight=w.groupby("cusip")["w"].sum(), max_state_weight=w.groupby("cusip")["w"].max(),
                    states=w.groupby("cusip").size()))
    cmap["accepted"] = cmap["accepted"].fillna(False).astype(bool)
    first = {c: series[c].tr.index[0] for c in accepted}

    def priced_on(c, t):
        return c in last.index and first[c] <= t and last[c] >= t and np.isfinite(lv.at[t, c])

    return {"filings": fl, "events": events, "states": states, "series": series, "pc": pc, "acc": acc,
            "accepted": accepted, "lv": lv, "px": px, "last": last, "map": cmap.reset_index(), "priced_on": priced_on,
            "checks": checks}


def alpaca_crosscheck(u: dict, sessions: pd.DatetimeIndex, log) -> pd.DataFrame:
    """Yahoo vs Alpaca daily total returns on the sessions each Yahoo-priced CUSIP was held after 2016 (report)."""
    held: dict = {}
    trade = list(u["states"].items())
    for k, (t, e) in enumerate(trade):
        t_next = trade[k + 1][0] if k + 1 < len(trade) else pd.Timestamp(END)
        if t_next < pd.Timestamp(ALPACA_CROSS_FROM):
            continue
        days = sessions[(sessions > max(t, pd.Timestamp(ALPACA_CROSS_FROM))) & (sessions <= t_next)]
        for c in e["positions"]["cusip"]:
            if c in u["accepted"] and u["series"][c].source == "yahoo":
                held.setdefault(c, []).append(days)
    rows = []
    hdr = None
    for c, parts in sorted(held.items()):
        days = parts[0].append(parts[1:]).unique() if len(parts) > 1 else parts[0]
        sym = u["series"][c].symbol
        asym = sym.replace("-", ".")
        hdr = hdr or alpaca_headers()
        try:
            a = bp.alpaca_series(RAW, asym, END, led(), hdr)
        except Exception as exc:  # noqa: BLE001  (reported, not fatal)
            rows.append({"cusip": c, "symbol": sym, "error": type(exc).__name__})
            continue
        if a is None:
            rows.append({"cusip": c, "symbol": sym, "error": "no_alpaca_bars"})
            continue
        rows.append({"cusip": c, "symbol": sym, **bp.crosscheck(u["series"][c].tr, a.tr, pd.DatetimeIndex(days))})
    df = pd.DataFrame(rows)
    log(f"alpaca cross-check: {len(df)} CUSIPs")
    return df


def stale_while_held(u: dict, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Data check (added after the review): each priced holding's longest run of unchanged total-return levels between
    its trade session and the next state's (up to its series end); rows with at least ``STALE_HELD_REPORT``."""
    rows = []
    trade = list(u["states"].items())
    for k, (t, e) in enumerate(trade):
        t_next = trade[k + 1][0] if k + 1 < len(trade) else sessions[-1]
        for c in e["positions"]["cusip"]:
            if not u["priced_on"](c, t):
                continue
            w = u["lv"][c][(u["lv"].index >= t) & (u["lv"].index <= min(t_next, u["last"][c]))]
            n = bp.longest_flat_run(w)
            if n >= STALE_HELD_REPORT:
                rows.append({"trade_session": str(t.date()), "cusip": c, "symbol": u["series"][c].symbol,
                             "source": u["series"][c].source, "flat_sessions": n, "held_sessions": int(len(w) - 1)})
    return pd.DataFrame(rows, columns=["trade_session", "cusip", "symbol", "source", "flat_sessions", "held_sessions"])


def coverage_tables(u: dict, sessions) -> pd.DataFrame:
    rows = []
    for rule in ("top10_ew", "all_vw"):
        _, cov = bf.b1_targets(u["states"], sessions, u["priced_on"], rule)
        rows.append(cov.assign(rule=rule))
    return pd.concat(rows, ignore_index=True)


def data_only(log) -> dict:
    b = benchmarks(False, log)
    u = b1_universe(b["sessions"], False, log)
    OUT.mkdir(parents=True, exist_ok=True)
    u["filings"].to_csv(OUT / "filings.csv", index=False)
    u["map"].to_csv(OUT / "cusip_map.csv", index=False, float_format="%.6g")
    u["pc"].to_csv(OUT / "price_check.csv", index=False, float_format="%.6g")
    cov = coverage_tables(u, b["sessions"])
    cov.to_csv(OUT / "b1_coverage.csv", index=False, float_format="%.6g")
    xc = alpaca_crosscheck(u, b["sessions"], log)
    xc.to_csv(OUT / "alpaca_crosscheck.csv", index=False, float_format="%.6g")
    stale = stale_while_held(u, b["sessions"])
    m = u["map"]
    fl = u["filings"]
    a = cov[cov["rule"] == "all_vw"]
    t = cov[cov["rule"] == "top10_ew"]
    summary = {
        "filings": int(len(fl)), "originals": int((fl["form"] == "13F-HR").sum()),
        "amendments": int((fl["form"] == "13F-HR/A").sum()),
        "parse_rows_ok": int(fl["rows_ok"].sum()), "parse_value_ok": int(fl["value_ok"].sum()),
        "parse_both_ok": int((fl["rows_ok"] & fl["value_ok"]).sum()),
        "parse_exceptions": fl.loc[~(fl["rows_ok"] & fl["value_ok"]), ["filing_date", "form", "accession",
                                                                        "entry_total", "rows_parsed", "value_total",
                                                                        "value_parsed"]].to_dict("records"),
        "events": int(len(u["events"])), "event_kinds": pd.Series([e["kind"] for e in u["events"]]).value_counts().to_dict(),
        "trade_sessions": int(len(u["states"])), "first_trade": str(min(u["states"]).date()),
        "last_trade": str(max(u["states"]).date()),
        "cusips": int(len(m)), "figi_mapped": int(m["figi_ticker"].notna().sum()),
        "hand_mapped": int(m["cusip"].isin(list(bc.HAND)).sum()), "skipped": int(m["cusip"].isin(list(bc.SKIP)).sum()),
        "with_series": int((m["status"] == "series").sum()), "accepted": int(m["accepted"].sum()),
        "rejected_by_price_check": m.loc[(m["status"] == "series") & ~m["accepted"],
                                         ["cusip", "name", "symbol", "checked", "failed_share", "median_ratio",
                                          "sum_state_weight"]].to_dict("records"),
        "no_data": m.loc[m["status"] == "no_data", ["cusip", "name", "spec", "sum_state_weight"]].to_dict("records"),
        "coverage_all_vw": {"mean_value_share_priced": float(a["value_share_priced"].mean()),
                            "min_value_share_priced": float(a["value_share_priced"].min()),
                            "min_at": a.loc[a["value_share_priced"].idxmin(), "trade_session"],
                            "mean_by_decade": a.assign(dec=a["trade_session"].str[:3]).groupby("dec")
                            ["value_share_priced"].mean().round(4).to_dict()},
        "coverage_top10": {"mean_top10_priced": float(t["top10_priced"].mean()), "min_top10_priced": int(t["top10_priced"].min()),
                           "mean_top10_value_share_priced": float(t["top10_value_share_priced"].mean()),
                           "states_below_8_of_10": int((t["top10_priced"] < 8).sum())},
        "alpaca_crosscheck": {"cusips": int(len(xc)), "with_numbers": int(xc.get("diff_pp", pd.Series()).notna().sum()),
                              "median_abs_diff_pp": float(xc["diff_pp"].abs().median()) if "diff_pp" in xc else None,
                              "over_1pp": xc.loc[xc.get("diff_pp", pd.Series(dtype=float)).abs() > 1.0,
                                                 ["cusip", "symbol", "sessions", "diff_pp", "corr"]].to_dict("records")
                              if "diff_pp" in xc else []},
        "benchmarks": b["checks"],
        "stale_while_held": {"min_run": STALE_HELD_REPORT, "rows": stale.to_dict("records")},
    }
    f = aqr.load_factors(RAW, AQR_LAST)
    summary["aqr"] = {k: {"first": str(f[k].first_valid_index()), "last": str(f[k].last_valid_index())} for k in f}
    y = aqr.load_dgs10(RAW, END)
    summary["dgs10"] = {"first": str(y.index[0].date()), "last": str(y.index[-1].date())}
    summary["series_file_sha256"] = hashlib.sha256(SERIES_FILE.read_bytes()).hexdigest()
    write_json(OUT / "data_checks.json", summary, default=json_rounded)
    return summary


def fetch_all(log, offline: bool = False) -> None:
    """Download and cache every raw input and build the B1 price series file; ``offline``: only rebuild the series
    file from the raw cache (no request)."""
    if offline:
        b = benchmarks(False, log)
        b1_universe(b["sessions"], True, log, offline=True)
        return
    aqr.fetch(RAW, led())
    b = benchmarks(True, log)
    u = b1_universe(b["sessions"], True, log)
    alpaca_crosscheck(u, b["sessions"], log)


# ======================================================================== evaluation helpers

def evaluate(nav: pd.Series, lvl: pd.DataFrame, rf: pd.Series, bases: dict) -> tuple[dict, list[dict]]:
    """Judged verdicts and metric rows. ``bases``: name -> (benchmark column, (full start, end), [part windows])."""
    verdicts, rows = {}, []
    for name, (col, full, parts) in bases.items():
        b = lvl[col].dropna()
        mf = bf.vs(nav, b, rf, *full)
        mp = [bf.vs(nav, b, rf, *p) for p in parts]
        verdicts[name] = bf.judge(mp, mf)
        for lab, m in [("full", mf)] + [(f"part{k + 1}", x) for k, x in enumerate(mp)]:
            rows.append({"basis": name, "benchmark": col, "part": lab, **m})
    return verdicts, rows


def windows_table(levels: dict) -> pd.DataFrame:
    rows = []
    for w, (a, b) in WINDOWS.items():
        for k, s in levels.items():
            rows.append({"window": w, "series": k, "return": bf.window_return(s, a, b),
                         "max_dd": bf.window_max_dd(s, a, b) if not s[s.index < pd.Timestamp(a)].empty else np.nan})
    return pd.DataFrame(rows)


def comparisons(nav: pd.Series, lvl: pd.DataFrame, rf: pd.Series, start: str, parts: list) -> dict:
    """The same A / B against QQQ and SPY (report only), on the given windows (where the benchmark exists)."""
    out = {}
    for col in ("QQQ", "SPY"):
        b = lvl[col].dropna()
        if b.index[0] >= pd.Timestamp(start):
            continue
        out[col] = bf.judge([bf.vs(nav, b, rf, *p) for p in parts], bf.vs(nav, b, rf, start, parts[-1][1]))
    return out


# ======================================================================== B0, B1, B2, B3

def run_b0(b: dict, log) -> dict:
    s, lvl, px, rf = b["sessions"], b["lvl"], b["px"], b["rf"]
    out = {}
    for sym, entry in (("BRK-B", "1996-05-09"), ("BRK-A", "1996-01-02")):
        d = s[s >= pd.Timestamp(entry)]
        nav = buy_hold_level(lvl[sym], px[sym], d, HS_BUYHOLD, ACCOUNT)
        first = str(d[1].date())
        res = {"entry": entry}
        for col, start in (("SPY", first), ("ONEQ*", first), ("QQQ", "1999-03-11"), ("ONEQ", ONEQ_REAL_FROM)):
            st = max(start, first)
            res[f"vs_{col}"] = bf.vs(nav, lvl[col].dropna(), rf, st, END)
        out[sym] = res
        log(f"B0 {sym}: CAGR {res['vs_SPY']['cagr']:+.2%} (SPY {res['vs_SPY']['oneq_cagr']:+.2%}, ONEQ* "
            f"{res['vs_ONEQ*']['oneq_cagr']:+.2%}); max DD {res['vs_SPY']['max_dd']:.1%}")
    return out


def run_b1(b: dict, u: dict, rule: str, log, band: float | None = None) -> dict:
    s = b["sessions"]
    targets, cov = bf.b1_targets(u["states"], s, u["priced_on"], rule)
    kw = {} if band is None else {"band": band}
    sim = simulate_index_units(targets, s, u["lv"], u["px"], u["last"], b["lvl"]["SPY"], b["px"]["SPY"],
                               account=ACCOUNT, end=END,
                               hs_fn=lambda sid, d, p: bf.b1_half_spread(p, d), **kw)
    nav = sim["nav"]
    assert str(nav.index[0].date()) == B1_ENTRY, nav.index[0]
    h_real = bf.halves_by_sessions(nav, ONEQ_REAL_FROM, END)
    bases = {"ONEQ*": ("ONEQ*", (B1_FOLDS[0][0], END), list(B1_FOLDS)),
             "ONEQ_real": ("ONEQ", (ONEQ_REAL_FROM, END), list(h_real))}
    verdicts, rows = evaluate(nav, b["lvl"], b["rf"], bases)
    ok = verdicts["ONEQ*"]["pass"] and verdicts["ONEQ_real"]["pass"]
    yrs = len(nav) / 252
    prev = nav.shift(1).fillna(ACCOUNT)
    extra = {"cost_drag_per_year": float((sim["cost"] / prev).sum() / yrs),
             "turnover_per_year": float(sim["traded"].sum() / 2 / nav.mean() / yrs),
             "avg_names": float(sim["names"].mean()), "orders": int(sim["orders"].sum())}
    return {"nav": nav, "sim": sim, "coverage": cov, "verdicts": verdicts, "pass": bool(ok), "rows": rows,
            "extra": extra, "vs_other": comparisons(nav, b["lvl"], b["rf"], B1_FOLDS[0][0], list(B1_FOLDS)),
            "halves_real": h_real, "targets": targets}


def b2_inputs(log) -> dict:
    if dv.DATA_VERSION != "v2.1":
        raise SystemExit(f"B2 is registered on data v2.1, not {dv.DATA_VERSION}")
    listed = dv.CACHE / "universe/weekly_listed.csv.gz"
    data, sigs, cand, panels, oneq_lvl, oneq_px = fs.prepare(B2_FIRST, listed, B2_CACHE, fs.SIGN,
                                                             perf_start="2014-01-01")
    ciks = sorted(cand["cik"].dropna().astype(int).unique())
    annual, _ = fs.extract_all(ciks, B2_CACHE)
    end = data.spec["effective_end"]
    annual = annual[annual["filed"].astype(str) <= end]
    path = B2_CACHE / "buffett_states.csv.gz"
    if path.exists():
        states = pd.read_csv(path, dtype={"filed": str, "fy0_end": str})
    else:
        states = pd.concat([bf.buffett_states(g) for _, g in annual.groupby("cik")], ignore_index=True)
        states.to_csv(path, index=False, compression="gzip")
    sig = [x for x in sigs if x.month == B2_SIGNAL_MONTH and pd.Timestamp(B2_FIRST) <= x <= pd.Timestamp(B2_LAST)]
    u = panels["U300"]
    u = u[u["s"].isin(sig)][["s", "security_id", "ticker", "cik", "dv50_rank", "mcap", "mcap_src", "sic"]].copy()
    m = bf.asof_buffett(u, states)
    m = pd.concat([m.reset_index(drop=True), bf.qualifies(m).reset_index(drop=True)], axis=1)
    y10 = aqr.load_dgs10(RAW, END)
    log(f"B2: {len(sig)} signals, U300 rows {len(m)}, data {dv.DATA_VERSION}, panel end {end}")
    return {"data": data, "sig": sig, "m": m, "y10": y10, "oneq_lvl": oneq_lvl}


def run_b2(i: dict, rule: str, n: int, b: dict, log) -> dict:
    data = i["data"]
    tg, picks = {}, []
    for s in i["sig"]:
        g = i["m"][i["m"]["s"] == s]
        y = float(i["y10"][i["y10"].index <= s].iloc[-1])
        sel = bf.b2_select(g, rule, n, y)
        tg[s] = [(r.security_id, 1.0 / len(sel), r.dv50_rank, r.mcap, r.mcap_src) for r in sel.itertuples()] \
            if len(sel) else []
        picks.append({"signal": str(s.date()), "y10": y, "qualifiers": int((g["q_roe"] & g["q_de"] & g["q_growth"]).sum()),
                      "held": int(len(sel)), "tickers": " ".join(sel["ticker"].astype(str)),
                      "mean_roe_min": float(sel["roe_min"].mean()) if len(sel) else np.nan,
                      "mean_de": float(sel["de"].mean()) if len(sel) else np.nan,
                      "mean_ep": float(sel["ep"].mean()) if len(sel) else np.nan})
    sim = simulate_index_units(tg, data.sessions, data.perf_idx, data.close, data.last_row, data.qqq_perf_idx,
                               data.qqq_close, account=ACCOUNT, end=B2_END)
    nav = sim["nav"]
    lvl = b["lvl"]
    bases = {"ONEQ": ("ONEQ", (B2_HALVES[0][0], B2_END), list(B2_HALVES))}
    verdicts, rows = evaluate(nav, lvl, b["rf"], bases)
    yrs = len(nav) / 252
    prev = nav.shift(1).fillna(ACCOUNT)
    return {"nav": nav, "sim": sim, "picks": pd.DataFrame(picks), "verdicts": verdicts,
            "pass": verdicts["ONEQ"]["pass"], "rows": rows,
            "extra": {"cost_drag_per_year": float((sim["cost"] / prev).sum() / yrs),
                      "turnover_per_year": float(sim["traded"].sum() / 2 / nav.mean() / yrs),
                      "avg_names": float(sim["names"].mean())},
            "vs_other": comparisons(nav, lvl, b["rf"], B2_HALVES[0][0], list(B2_HALVES))}


def b2_debt_extended(i: dict, log) -> tuple[dict, pd.DataFrame]:
    """B2 inputs with D/E recomputed from the extended debt tags (sensitivity added after the review; only rows that
    pass the ROE and growth tests can change), and the rows whose D/E test flips."""
    m = i["m"]
    cand = m[m["q_roe"] & m["q_growth"]]
    facts = pd.concat([fs.annual_instants(int(c), bf.DEBT_ALT_CONCEPTS) for c in sorted(cand["cik_i"].unique())],
                      ignore_index=True)
    ext = bf.debt_extended(cand, facts)
    de_ext = ext["debt_ext"] / cand["be0"].astype(float)
    m2 = m.copy()
    m2.loc[cand.index, "q_de"] = np.isfinite(de_ext) & (de_ext < bf.DE_MAX)
    flips = cand.assign(de_registered=cand["de"], de_extended=de_ext, debt_source=ext["debt_ext_source"])
    flips = flips[flips["q_de"] != m2.loc[cand.index, "q_de"]]
    flips = flips.assign(signal=flips["s"].dt.strftime("%Y-%m-%d"))[["signal", "ticker", "cik_i", "de_registered",
                                                                       "de_extended", "debt_source"]]
    log(f"B2 debt sensitivity: {len(cand)} ROE + growth rows, {len(flips)} change the D/E test")
    return {**i, "m": m2}, flips


def run_b3i(b: dict, weights: dict, lev: float, log) -> dict:
    s, lvl, px = b["sessions"], b["lvl"], b["px"]
    cols = list(weights)
    rets = lvl[cols].pct_change()
    bm = load_effr(END, EFFR_DIR)
    bm_s = bm.reindex(s.union(bm.index)).ffill().reindex(s)
    me = last_session_of_each_month(s)
    entry = pd.Timestamp(B3_ENTRY)
    execs = [s[s > m][0] for m in me if m >= entry and m < pd.Timestamp(END) and len(s[s > m])]
    execs = [x for x in execs if x > entry]
    sim = simulate_margin_weights(weights, lev, rets, px, bm_s, execs, entry, pd.Timestamp(END),
                                  {c: B3_HS for c in cols}, spread=MARGIN_SPREAD, maint=MAINT, liq_slip=LIQ_SLIP,
                                  equity=ACCOUNT)
    nav = sim["nav"]
    first = str(nav.index[1].date())
    h = bf.halves_by_sessions(nav, first, END)
    verdicts, rows = evaluate(nav, lvl, b["rf"], {"ONEQ": ("ONEQ", (first, END), list(h))})
    yrs = len(nav) / 252
    prev = nav.shift(1).fillna(ACCOUNT)
    return {"nav": nav, "sim": sim, "verdicts": verdicts, "pass": verdicts["ONEQ"]["pass"], "rows": rows,
            "halves": h, "extra": {"cost_drag_per_year": float((sim["cost"] / prev).sum() / yrs),
                                   "interest_drag_per_year": float((sim["interest"] / prev).sum() / yrs),
                                   "avg_gross": float(sim["gross"].mean()), "liquidations": sim["liquidations"],
                                   "orders": sim["orders"]},
            "vs_other": comparisons(nav, lvl, b["rf"], first, list(h))}


def run_b3_replication(b: dict, log) -> dict:
    f = aqr.load_factors(RAW, AQR_LAST)
    r1, k = bf.fkp_section7(f)
    port = {"R1 (section 7)": r1, "MKT+RF": f["MKT"] + f["RF"]}
    for sp in FKP_SPREADS:
        port[f"R2 1.6x s={sp:.1%}"] = bf.fkp_levered(f, {c: bf.FKP_COL6[c] for c in ("MKT", "BAB", "QMJ")},
                                                     bf.FKP_LEVERAGE, sp)
    # BRK-A's own Yahoo rows from 1980 (b["lvl"] is on SPY's sessions, which start in 1993)
    brk = bp.yahoo_series(RAW, "BRK-A", END, BENCH_FIRST["BRK-A"]).tr
    m = brk.groupby(brk.index.to_period("M")).last().pct_change().dropna()
    port["BRK-A"] = m
    rows, regs = [], []
    for pname, (a, z) in FKP_PERIODS.items():
        brk_in = m.index[0] <= pd.Period(a, "M")      # BRK-A rows start 1980-04: no BRK row for an earlier window
        for name, r in port.items():
            if name == "BRK-A" and not brk_in:
                continue
            x = r[(r.index >= pd.Period(a, "M")) & (r.index <= pd.Period(z, "M"))]
            rows.append({"period": pname, "series": name, **bf.monthly_stats(x, f["RF"])})
            if name not in ("BRK-A",) and brk_in:
                y = m[(m.index >= pd.Period(a, "M")) & (m.index <= pd.Period(z, "M"))] - f["RF"]
                regs.append({"period": pname, "regress_BRK_excess_on": name,
                             **bf.ols(y, x - f["RF"].reindex(x.index))})
    # added after the review (report only): the paper's Table 4 regressions of BRK-A's excess return on the factors,
    # and what R2 is made of (market part alone, BAB and QMJ compounded as excess returns)
    fregs, dec = [], []
    for pname in FKP_REG_PERIODS:
        a, z = FKP_PERIODS[pname]
        mm = m[(m.index >= pd.Period(a, "M")) & (m.index <= pd.Period(z, "M"))]
        y = mm - f["RF"].reindex(mm.index)
        for mname, cols in FKP_FACTOR_MODELS.items():
            fregs.append({"period": pname, "model": mname, **bf.ols_multi(y, f[cols])})
        sub = f[(f.index >= pd.Period(a, "M")) & (f.index <= pd.Period(z, "M"))]
        lev_mkt = bf.FKP_LEVERAGE * bf.FKP_COL6["MKT"]
        parts = {"MKT+RF": sub["MKT"] + sub["RF"],
                 f"RF + {lev_mkt:.3f} MKT - 0.6 x 0.5%/12 (R2's market part)":
                     sub["RF"] + lev_mkt * sub["MKT"] - (bf.FKP_LEVERAGE - 1) * FKP_SPREADS[0] / 12,
                 "BAB (long-short excess)": sub["BAB"], "QMJ (long-short excess)": sub["QMJ"],
                 f"R2 1.6x s={FKP_SPREADS[0]:.1%}": port[f"R2 1.6x s={FKP_SPREADS[0]:.1%}"].reindex(sub.index),
                 "BRK-A": m.reindex(sub.index)}
        for nm, r in parts.items():
            r = r.dropna()
            dec.append({"period": pname, "series": nm, "months": int(len(r)),
                        "cagr": float(np.prod(1 + r.values) ** (12 / len(r)) - 1) if len(r) else np.nan})
    log(f"B3 replication: k = {k:.3f}")
    monthly_out = pd.DataFrame({k2: v for k2, v in port.items() if k2 != "BRK-A"})
    monthly_out.index = monthly_out.index.astype(str)
    return {"stats": pd.DataFrame(rows), "regs": pd.DataFrame(regs), "k": k, "monthly": monthly_out,
            "port": port, "rf": f["RF"], "factor_regs": pd.DataFrame(fregs), "decomposition": pd.DataFrame(dec)}


# ======================================================================== run

def run(args) -> None:
    t0 = time.time()
    log = RunLog()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.fetch:
        fetch_all(log.say, offline=args.offline)
    if args.data_only:
        s = data_only(log.say)
        log.say(json.dumps({k: s[k] for k in ("filings", "parse_both_ok", "cusips", "accepted", "coverage_all_vw",
                                               "coverage_top10")}, default=json_rounded))
        log.write(OUT / "run_log_data.txt")
        return
    if args.register:
        PREREG.register()
        return
    if args.fetch and not args.run:
        log.write(OUT / "run_log_fetch.txt")
        return
    frozen = json.loads((OUT / "prereg_frozen.json").read_text())
    if frozen["sha256"] != PREREG.hash():
        raise SystemExit("ledger section 0 changed since --register; refusing to run")
    log.say(f"registered sha256 {frozen['sha256']} (matches); trials {N_TRIALS}, Bonferroni t {bonferroni_t(N_TRIALS):.2f}")
    want = json.loads((OUT / "data_checks.json").read_text())["series_file_sha256"]
    if hashlib.sha256(SERIES_FILE.read_bytes()).hexdigest() != want:
        raise SystemExit("the B1 price series file changed since --data-only; refusing to run")
    log.say(f"B1 price series file sha256 {want} (matches the data stage)")
    b = benchmarks(False, log.say)
    lvl, rf = b["lvl"], b["rf"]
    summary = {"sha256": frozen["sha256"], "n_trials": N_TRIALS, "bonferroni_t": bonferroni_t(N_TRIALS),
               "data_version_b2": dv.DATA_VERSION, "trials": {}, "sensitivity": {}}
    metric_rows, navs = [], {}
    # B0
    b0 = run_b0(b, log.say)
    summary["B0"] = {k: {kk: (vv if not isinstance(vv, dict) else
                              {x: vv[x] for x in ("start", "end", "cagr", "oneq_cagr", "excess_vs_oneq", "max_dd",
                                                  "oneq_max_dd", "dd_shallower_than_oneq_pp", "vol", "sharpe",
                                                  "t_monthly_excess_vs_oneq")})
                         for kk, vv in v.items()} for k, v in b0.items()}
    brkb = buy_hold_level(lvl["BRK-B"], b["px"]["BRK-B"], b["sessions"][b["sessions"] >= pd.Timestamp("1996-05-09")],
                          HS_BUYHOLD, ACCOUNT)
    # B1
    u = b1_universe(b["sessions"], False, log.say)
    for name, rule in B1_TRIALS.items():
        r = run_b1(b, u, rule, log.say)
        navs[name] = r["nav"]
        summary["trials"][name] = {"rule": rule, "verdicts": r["verdicts"], "pass": r["pass"], **r["extra"],
                                   "vs_QQQ_SPY": r["vs_other"], "halves_oneq_real": r["halves_real"]}
        metric_rows += [{"trial": name, **x} for x in r["rows"]]
        r["coverage"].to_csv(OUT / f"b1_coverage_{name}.csv", index=False, float_format="%.6g")
        log.say(f"{name}: pass {r['pass']} | " + " | ".join(
            f"{k}: A {v['A']} B {v['B']} t {v['t_full']:+.2f}" for k, v in r["verdicts"].items()))
        if name == "B1a":
            hold = []
            for sgn, tg in r["targets"].items():
                for c, w, *_ in tg:
                    hold.append({"signal": str(sgn.date()), "cusip": c, "symbol": u["series"][c].symbol, "weight": w})
            pd.DataFrame(hold).to_csv(OUT / "b1a_holdings.csv", index=False, float_format="%.6g")
    for name, (rule, mode) in B1_SENSITIVITY.items():
        uu = u if mode == "public" else {**u, "states": b13.states_by_trade_session(
            b13.load(RAW / "13f", END, bc.CUSIP_FIXES, mode)[2], b["sessions"])}
        r = run_b1(b, uu, rule, lambda *a: None)
        navs[name] = r["nav"]
        summary["sensitivity"][name] = {"verdicts": r["verdicts"], "pass_if_it_were_a_trial": r["pass"],
                                        **r["extra"]}
        metric_rows += [{"trial": name, **x} for x in r["rows"]]
    for name, (rule, band) in B1_BAND_SENSITIVITY.items():
        r = run_b1(b, u, rule, lambda *a: None, band=band)
        navs[name] = r["nav"]
        summary["sensitivity"][name] = {"verdicts": r["verdicts"], "pass_if_it_were_a_trial": r["pass"],
                                        **r["extra"]}
        metric_rows += [{"trial": name, **x} for x in r["rows"]]
    # B2
    i2 = b2_inputs(log.say)
    picks = []
    for name, (rule, n) in B2_TRIALS.items():
        r = run_b2(i2, rule, n, b, log.say)
        navs[name] = r["nav"]
        summary["trials"][name] = {"rule": rule, "n": n, "verdicts": r["verdicts"], "pass": r["pass"], **r["extra"],
                                   "vs_QQQ_SPY": r["vs_other"]}
        metric_rows += [{"trial": name, **x} for x in r["rows"]]
        picks.append(r["picks"].assign(trial=name))
        log.say(f"{name}: pass {r['pass']} | ONEQ: A {r['verdicts']['ONEQ']['A']} B {r['verdicts']['ONEQ']['B']} "
                f"t {r['verdicts']['ONEQ']['t_full']:+.2f}")
    pd.concat(picks).to_csv(OUT / "b2_selections.csv", index=False, float_format="%.6g")
    i2x, flips = b2_debt_extended(i2, log.say)
    flips.to_csv(OUT / "b2_debt_sensitivity.csv", index=False, float_format="%.6g")
    for name, (rule, n) in B2_TRIALS.items():
        r = run_b2(i2x, rule, n, b, log.say)
        sname = f"{name} debt tags extended"
        navs[sname] = r["nav"]
        summary["sensitivity"][sname] = {"verdicts": r["verdicts"], "pass_if_it_were_a_trial": r["pass"], **r["extra"],
                                         "vs_QQQ_SPY": r["vs_other"]}
        metric_rows += [{"trial": sname, **x} for x in r["rows"]]
    # B3
    for name, (w, lev) in {**B3_TRIALS, **B3_SENSITIVITY}.items():
        r = run_b3i(b, w, lev, log.say)
        navs[name] = r["nav"]
        key = "trials" if name in B3_TRIALS else "sensitivity"
        summary[key][name] = {"weights": w, "leverage": lev, "verdicts": r["verdicts"], "pass": r["pass"],
                              **r["extra"], "halves": r["halves"], "vs_QQQ_SPY": r["vs_other"]}
        metric_rows += [{"trial": name, **x} for x in r["rows"]]
        log.say(f"{name}: pass {r['pass']} | ONEQ: A {r['verdicts']['ONEQ']['A']} B {r['verdicts']['ONEQ']['B']} "
                f"t {r['verdicts']['ONEQ']['t_full']:+.2f}")
    b3w = navs["B3i-1.0"]
    first3 = str(b3w.index[1].date())
    brk3 = buy_hold_level(lvl["BRK-B"], b["px"]["BRK-B"], b3w.index, HS_BUYHOLD, ACCOUNT)
    summary["sensitivity"]["BRK.B buy-and-hold, B3 window"] = {
        "verdicts": evaluate(brk3, lvl, b["rf"], {"ONEQ": ("ONEQ", (first3, END), list(bf.halves_by_sessions(
            brk3, first3, END)))})[0]}
    rep = run_b3_replication(b, log.say)
    rep["stats"].to_csv(OUT / "b3_replication_stats.csv", index=False, float_format="%.6g")
    rep["regs"].to_csv(OUT / "b3_replication_regressions.csv", index=False, float_format="%.6g")
    rep["monthly"].to_csv(OUT / "b3_replication_monthly.csv", float_format="%.6f")
    rep["factor_regs"].to_csv(OUT / "b3_replication_factor_regressions.csv", index=False, float_format="%.6g")
    rep["decomposition"].to_csv(OUT / "b3_replication_decomposition.csv", index=False, float_format="%.6g")
    summary["B3_replication"] = {"k_scale": rep["k"]}
    # tables
    pd.DataFrame(metric_rows).to_csv(OUT / "metrics.csv", index=False, float_format="%.6g")
    levels = {**{k: v for k, v in navs.items()}, "BRK.B": brkb, "ONEQ*": lvl["ONEQ*"], "ONEQ": lvl["ONEQ"].dropna(),
              "QQQ": lvl["QQQ"].dropna(), "SPY": lvl["SPY"].dropna()}
    windows_table(levels).to_csv(OUT / "windows.csv", index=False, float_format="%.6g")
    bf.yearly_table(levels, "1996-01-01", END).to_csv(OUT / "by_year.csv", float_format="%.6f")
    bf.monthly_table(levels, "1996-01-01", END).to_csv(OUT / "monthly_returns.csv", float_format="%.6f")
    dd = {k: {"max_dd": max_drawdown(v / v.iloc[0]), "trough": str((v / v.cummax()).idxmin().date())}
          for k, v in levels.items()}
    summary["max_drawdowns"] = dd
    passed = [k for k, v in summary["trials"].items() if v["pass"]]
    a_bonf = [k for k, v in summary["trials"].items()
              if any(x["A"] and x["t_full"] >= bonferroni_t(N_TRIALS) for x in v["verdicts"].values())]
    summary["verdict"] = {"passed": passed, "A_bonferroni": a_bonf, "useful": bool(passed)}
    summary["runtime_s"] = round(time.time() - t0, 1)
    write_json(OUT / "summary.json", summary, default=json_rounded)
    log.say(f"passed: {passed or 'none'}; A_bonferroni: {a_bonf or 'none'}; {summary['runtime_s']}s")
    log.write(OUT / "run_log.txt")
    record_run(args.label or "")


def record_run(label: str) -> None:
    """Append the SHA-256 of every output file to run_history.jsonl (so a later rerun cannot hide an earlier result)."""
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.iterdir())
             if p.is_file() and p.name != "run_history.jsonl"}
    entry = {"at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"), "label": label,
             "series_file_sha256": hashlib.sha256(SERIES_FILE.read_bytes()).hexdigest(), "files": files}
    with open(OUT / "run_history.jsonl", "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", action="store_true", help="download and cache every raw input (logged)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--data-only", action="store_true", help="coverage, price check, cross-check (no returns)")
    g.add_argument("--register", action="store_true", help="write the SHA-256 of ledger section 0")
    g.add_argument("--run", action="store_true", help="the registered run")
    ap.add_argument("--offline", action="store_true",
                    help="with --fetch: rebuild the series file from the raw cache only")
    ap.add_argument("--label", default="", help="a note stored with this run's output hashes (run_history.jsonl)")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
