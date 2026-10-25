"""Many-angle scorecard of the forward-observation candidates (quant.evaluation.scorecard; docs/evaluation_scorecard.md
section 6). DESCRIPTIVE ONLY: every rule is the frozen one, run unchanged or read from its study's saved output, and
nothing here is a verdict (each study's pre-registered A / B rule stays the only one).

Series (daily returns; the glue below mirrors each study's own run and is checked against its saved output on every
run, results in checks.json):
  ONEQ, QQQ, T-bill  quant.data.sector_etfs (research_cache/sector_lev/raw, through 2026-09-30): ONEQ total return from
                     its first return 2003-10-02 and, before it, the Nasdaq Composite PRICE index (sector_lev's spliced
                     benchmark, "ONEQ*"; it has no dividends, which flatters a strategy by up to about 1 point a year
                     before 2003-10); QQQ total return from 1999-03-11; Ken French RF, then DTB3 / 252.
  T01                qqq_timing T01_L200_b2 (1x QQQ above SMA200 x 1.02, T-bill ETF below x 0.98):
                     quant.backtest.exposure on quant.data.index_history, one run from the 1999-03-10 close to
                     2026-09-30 (the dev run and the 2015-2026 one-shot test are its two pieces); checked against
                     qqq_timing_dev / qqq_timing_oneshot.
  LEV-B3             sector_lev B3 (QQQ SMA200 band 2%: 1.5x via QQQ + QLD, else 1.0x), flagged-weights engine from the
                     1999-03-10 close; checked against sector_lev/monthly_returns.csv. Matched comparison (registry):
                     constant 1.37x, month-end rebalance, same entry.
  V3                 voltarget V3 (exposure min(1.5, expanding QQQ vol / vol20) at month ends) from the 2003-10-01
                     close; report-only long window from the 2000-04-03 close (the study's spliced long window);
                     matched comparison: the constant portfolio at V3's own average exposure. Checked against voltarget.
  BT4b               bitcoin BT4b (90% QQQ + 10% BTC, month-end rebalance) from the 2014-09-30 close, Bitstamp primary
                     close; checked against bitcoin/monthly_returns.csv.
  M1 M2 M3 M6        megacap/nav_daily.csv, judged window 2014-01-02 .. 2026-08-31 (checked against summary.csv); out
                     of sample 1999-03-11 .. 2013-12-31 from the studies' own derived NAVs in
                     research_cache/megacap_oos2/built (M1 M2 M6) and megacap_oos3/built (M3) (checked against their
                     results.json).
  S3-Yb              selective_t S3-Yb, net, the U18 basket (18 accounts of $10k, equal-weight mean of daily returns),
                     2012-01-03 .. 2026-09-30; matched comparison H_match (basket of per-account same-exposure holds).
                     Its ONEQ comparison is void (the 18 names are today's large caps: survivorship). Checked against
                     selective_t/results.json.
  SEL-A SEL-P        t_grid configs 29876 / 29916 (registry), exact re-run on QQQ from the grid's first valid session
                     2000-03-03; matched comparison H_match. Checked against t_grid/surface_QQQ.csv.gz.
  S-MISP             short_overlay S-MISP N10 k20 MN, main variant, the two registered folds chained (H1 opens at the
                     2012-02-01 close, H2 at the 2019-01-02 close, each a fresh $10k account; returns from each opening
                     value, as the study reports them, so 2019-01-02 itself has no return) from nav_daily.csv.gz.
  S/P top-10         NOT SCORED: it runs on QuantConnect (not used here) and no monthly series is saved locally; the
                     ledger's annual returns (docs/research_ledger_qc_factors.md 3.7.1) give a few annual figures only.

Windows: full (each series' own history), common 2014-10-01 .. 2026-08-31 (every scored candidate), long 2003-10-02 ..
2026-09-30 (those with history before 2014; includes 2008) and oos 1999-03-11 .. 2013-12-31 (the mega-cap out-of-sample
test, with the benchmarks and the long-history lines on the same days). Benchmark for every relative metric: ONEQ*.

Practicality inputs: orders, cost drag and turnover from each run (engine counters; turnover from the held
weights), or from the study's summary (mega-cap, S-MISP, S3-Yb); SEL cost drag is approximate (trips x cost per trip x
trade size).

Outputs (metrics only, no price levels): output/research_only/scorecard/ -- scorecard_long.csv (every metric, window,
series), compare_<window>.csv (metric x series), headline_<window>.csv (the report's metrics by angle),
angle_detail / angle_ranks_<window>.csv (descriptive ranks, 1 = best), matched.csv (excess over each line's matched
comparison), coverage.csv, checks.json (reproduction checks), results.json, run_log.txt. Raw data are read, never
written.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study scorecard_candidates [--n-boot 2000]
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np
import pandas as pd

from quant.backtest import exposure as ex_engine
from quant.backtest import flagged_weights as fw
from quant.backtest.costs import CASH_ETF_FEE, PAXOS_MONTHLY_FEE, etf_order_cost, ibkr_crypto_cost
from quant.data import bitcoin as bd
from quant.data import index_history
from quant.data import sector_etfs as etfs
from quant.data.rates import daily_rf
from quant.data.synthetic import synthetic_2x
from quant.evaluation import scorecard as sc
from quant.evaluation.events import grid_trip_summary
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, monthly
from quant.paths import CACHE_ROOT, ROOT, output_dir
from quant.reporting import RunLog, json_default, write_json
from quant.strategies import bitcoin as bt
from quant.strategies import registry
from quant.strategies import selective_t as st
from quant.strategies import t_grid as tg
from quant.strategies import voltarget as vt
from quant.strategies.qqq_timing import Rule, target_exposure
from quant.strategies.sector_lev import part_b_targets

OUT = output_dir("scorecard")
RESEARCH = ROOT / "output/research_only"
END = "2026-09-30"
N_BOOT = 2000
WINDOWS = {"common": ("2014-10-01", "2026-08-31"), "long": ("2003-10-02", "2026-09-30"),
           "oos": ("1999-03-11", "2013-12-31")}
COVER_DAYS = 7                 # a series "has" a window when it starts / ends within 7 days of its ends

# ---- frozen values of the studies (copied, each checked by reproducing the study's saved returns)
T01_RULE = Rule("T01", 200, 0.02)                    # docs/research_ledger_qqq_timing.md: the one-shot rule
T01_ENTRY = index_history.QQQ_FIRST_CLOSE             # studies/qqq_timing.py DEV_START
T01_SPREAD = 0.0069599408878067825                    # studies/qqq_timing.py FROZEN_SPREAD (2x leg; unused at <= 1x)
LEV = registry.get("LEV-B3").frozen                   # on 1.5, off 1.0, sma 200, band 0.02, matched_exposure 1.37
V3 = registry.get("V3").frozen
VOL_PARAMS = vt.VolParams(target_vol=V3["target_vol"], vol_short=V3["vol_short"], vol_quarter=V3["vol_quarter"],
                          vol_lr_min=V3["vol_lr_min"], sma_len=V3["sma_len"], sma_band=V3["sma_band"])
V3_ENTRY, V3_LONG_ENTRY = "2003-10-01", "2000-04-03"  # studies/voltarget.py ONEQ_START; its long-window entry
BT = registry.get("BT4b").frozen
BT_ENTRY = "2014-09-30"                               # studies/bitcoin.py ENTRY
ST_FULL = ("2012-01-03", st.END)                      # studies/selective_t.py STOCK_PERIODS["full"]
ST_SOURCES = {"QQQ": CACHE_ROOT / "calendar/raw/chart_QQQ.json", "ONEQ": CACHE_ROOT / "calendar/raw/chart_ONEQ.json"}
ST_RAW = CACHE_ROOT / "selective_t/raw"
TG_PANEL = CACHE_ROOT / "reversal_2012_2026_v2/prices"           # read only (t_grid's T10 base; not used here)
TG_TOP20 = RESEARCH / "megacap_v2/top20_by_month.csv"
SEL = {"SEL-A": registry.get("SEL-A").frozen["config_id"], "SEL-P": registry.get("SEL-P").frozen["config_id"]}
MEGA = registry.B4_MEGACAP                                        # M1 M2 M3 M6
MEGA_FULL = ("2014-01-02", "2026-08-31")                          # studies/megacap.py "Full 2014-2026-08"
MEGA_OOS_NAV = {"M1": "megacap_oos2", "M2": "megacap_oos2", "M6": "megacap_oos2", "M3": "megacap_oos3"}
SMISP = "S-MISP_N10_k20_MN"

# S/P top-10, docs/research_ledger_qc_factors.md 3.7.1 (annual %, strategy / ONEQ-else-QQQ / QQQ; 2026 = Jan-Jun)
SP_YEARS = {2003: (69.1, 42.3, 42.5), 2004: (18.6, 7.4, 9.8), 2005: (9.5, 5.7, 6.0), 2006: (25.6, 9.0, 5.9),
            2007: (-24.3, 6.6, 14.8), 2008: (-24.7, -36.5, -38.2), 2009: (165.4, 42.0, 50.4), 2010: (39.9, 17.9, 20.0),
            2011: (7.5, -0.9, 4.2), 2012: (39.8, 18.7, 19.1), 2013: (59.6, 34.8, 31.4), 2014: (33.8, 14.6, 19.8),
            2015: (-12.4, 5.1, 7.5), 2016: (78.8, 13.2, 11.0), 2017: (13.7, 30.1, 34.4), 2018: (4.0, -5.4, -2.4),
            2019: (23.1, 37.0, 40.7), 2020: (50.7, 40.9, 44.1), 2021: (116.4, 25.6, 30.5), 2022: (15.0, -33.0, -33.4),
            2023: (28.6, 41.6, 50.9), 2024: (8.5, 32.9, 28.9), 2025: (2.5, 21.0, 20.8), 2026: (38.4, 13.0, 18.6)}
SP_LEDGER = {"cagr_2003_2026_06": 0.278, "max_dd_daily": -0.703, "t_vs_oneq": 3.09, "n_trials": 40,
             "t_without_flagged_names": 2.84, "orders": 2015, "run_years": 23.5}

# ---- practicality and confidence inputs (scorecard meta), from the studies' ledgers and outputs (cited)
DATA_PRICES = "只要 QQQ 日收盘价（复权）"
META = {
    "ONEQ": dict(n_trials=None, oos_status="none", permissions=[], positions=1, trades_per_year=0.0,
                 turnover_one_way_per_year=0.0, cost_drag_per_year=0.0, min_account_usd=10_000,
                 data_dependence="无（买入持有）"),
    "QQQ": dict(n_trials=None, oos_status="none", permissions=[], positions=1, trades_per_year=0.0,
                turnover_one_way_per_year=0.0, cost_drag_per_year=0.0, min_account_usd=10_000,
                data_dependence="无（买入持有）"),
    # 142 counted rules in the dev grid; the 2015-2026 one-shot test of T01 did not pass (qqq_timing_oneshot)
    "T01": dict(n_trials=142, oos_status="failed", permissions=[], positions=1, min_account_usd=10_000,
                data_dependence=DATA_PRICES),
    "LEV-B3": dict(n_trials=8, oos_status="none", permissions=["leveraged_etf"], positions=2, min_account_usd=10_000,
                   data_dependence=DATA_PRICES),
    "V3": dict(n_trials=8, oos_status="none", permissions=["leveraged_etf"], positions=2, min_account_usd=10_000,
               data_dependence=DATA_PRICES + "、国债利率"),
    "BT4b": dict(n_trials=13, oos_status="none", permissions=["crypto"], positions=2, min_account_usd=10_000,
                 data_dependence="QQQ 收盘价、BTC 现货收盘价（多家交易所核对）"),
    # megacap: 6 rules; all fail out of sample 1999-2013 (megacap_oos2 / oos3)
    **{m: dict(n_trials=6, oos_status="failed", permissions=[], positions=10 if m in ("M1", "M6") else 5,
               min_account_usd=10_000, data_dependence="纳斯达克时点市值排名：SEC 股数、价格、成交额（每月）")
       for m in MEGA},
    # 18 configurations x assets; one $10k cash account per stock (registry note): 18 accounts
    "S3-Yb": dict(n_trials=18, oos_status="none", permissions=[], positions=18, timing_sensitive=True,
                  min_account_usd=180_000, data_dependence="18 只股票的日线开高低收、拆股、分红（开盘集合竞价 + 限价单）"),
    # 61,440 grid configurations on the QQQ base; the walk-forward (honest out-of-sample) selection did not pass
    **{k: dict(n_trials=tg_n, oos_status="failed", permissions=[], positions=1, timing_sensitive=True,
               min_account_usd=10_000, data_dependence="QQQ 日线开高低收（1999 年起的 RSI2 历史分位数）、开盘集合竞价")
       for k, tg_n in (("SEL-A", 61_440), ("SEL-P", 61_440))},
    # 32 registered configurations; min account from short_overlay/feasibility.csv (whole-share shorts)
    "S-MISP": dict(n_trials=32, oos_status="none", permissions=["margin", "short"], positions=11, margin_leverage=1.4,
                   min_account_usd=37_600, data_dependence="300 只股票的 SEC 财报（8 个异常因子）、价格、成交额、借券费（每月）"),
}

# ---- the nine angles of the report: (metric, +1 higher is better / -1 lower is better); ranks are descriptive only
ANGLES = {
    "收益": [("cagr", 1), ("excess_cagr_vs_oneq", 1)],
    "风险调整后收益": [("sharpe", 1), ("sortino", 1), ("calmar_mar", 1), ("omega_0", 1)],
    "回撤痛苦程度": [("max_drawdown", 1), ("ulcer_index", -1), ("max_dd_duration_years", -1), ("p_dd50_10y", -1)],
    "尾部风险": [("worst_day", 1), ("worst_month", 1), ("cvar_5_monthly", 1), ("skew_monthly", 1)],
    "稳定性": [("positive_months_share", 1), ("years_beating_oneq_share", 1), ("rolling_3y_min", 1),
            ("rolling_5y_win_rate_vs_oneq", 1)],
    "和大盘的关系": [("down_capture_vs_oneq", -1), ("capture_spread", 1), ("crisis_dotcom_2000_2002_vs_oneq", 1),
               ("crisis_gfc_2008_vs_oneq", 1), ("crisis_covid_2020q1_vs_oneq", 1), ("crisis_bear_2022_vs_oneq", 1)],
    "投资者效用": [("ce_crra_2", 1), ("ce_crra_3", 1), ("ce_crra_5", 1)],
    "实际可操作性": [("operational_complexity", -1), ("cost_drag_per_year", -1), ("min_account_usd", -1),
               ("trades_per_year", -1)],
    "统计可信度": [("t_vs_oneq", 1), ("excess_cagr_ci_lo", 1), ("deflated_sharpe", 1), ("n_trials", -1)],
}
BENCHMARKS = ("ONEQ", "QQQ", "ONEQ*")
SELF_COMPARISONS = ("years_beating_oneq_share", "rolling_5y_win_rate_vs_oneq")   # ONEQ against itself: not ranked
HEADLINE = {   # the report's compact table: a few metrics per angle (all are in compare_<window>.csv)
    "收益": ["cagr", "excess_cagr_vs_oneq", "excess_cagr_vs_qqq"],
    "风险调整后收益": ["volatility_ann", "sharpe", "sortino", "calmar_mar", "ir_vs_oneq"],
    "回撤痛苦程度": ["max_drawdown", "max_dd_duration_years", "longest_underwater_years", "ulcer_index", "p_dd50_10y",
               "recovery_years_max", "contrib_max_dd_usd"],
    "尾部风险": ["worst_day", "worst_month", "worst_year", "cvar_5_monthly", "skew_monthly", "excess_kurtosis_monthly"],
    "稳定性": ["positive_months_share", "years_beating_oneq_share", "rolling_3y_min", "rolling_5y_min",
            "rolling_5y_win_rate_vs_oneq"],
    "和大盘的关系": ["beta_vs_oneq", "corr_vs_oneq", "up_capture_vs_oneq", "down_capture_vs_oneq",
               "crisis_dotcom_2000_2002_vs_oneq", "crisis_gfc_2008_vs_oneq", "crisis_covid_2020q1_vs_oneq",
               "crisis_bear_2022_vs_oneq"],
    "投资者效用": ["ce_crra_1", "ce_crra_3", "ce_crra_5", "volatility_drag", "kelly_fraction", "kelly_ci_lo"],
    "实际可操作性": ["trades_per_year", "turnover_one_way_per_year", "cost_drag_per_year", "min_account_usd", "permissions",
               "operational_complexity", "margin_call_drop"],
    "统计可信度": ["t_vs_oneq", "p_vs_oneq", "excess_cagr_ci_lo", "excess_cagr_ci_hi", "deflated_sharpe", "years_of_data",
              "n_trials", "oos_status"],
}


# ======================================================================== helpers

def years_of(r: pd.Series) -> float:
    return len(r) / TRADING_DAYS


def returns_from_nav(nav: pd.Series) -> pd.Series:
    """Daily returns of a value path, from its first value (the studies' own convention: an account's first row is
    its value after the opening trade, so the opening cost is not a return)."""
    return nav.astype(float).pct_change().dropna()


def calendar_cagr(r: pd.Series, base: pd.Timestamp) -> float:
    """CAGR over calendar days from ``base`` (the value before the first return) to the last return, as the mega-cap
    and short-overlay studies report it (the scorecard uses 252 sessions a year instead)."""
    return float(np.prod(1 + r.to_numpy()) ** (365.25 / (r.index[-1] - base).days) - 1)


def one_way_turnover(held: pd.DataFrame, rets: pd.DataFrame) -> float:
    """One-way turnover a year of a target-weight engine: ``held`` are the weights held into each session (after the
    previous close's trade); before the trade at the close they have drifted to held * (1 + r)."""
    h = held.fillna(0.0)
    r = rets.reindex(index=h.index, columns=h.columns).fillna(0.0)
    pre = h * (1 + r)
    pre = pre.div(pre.sum(axis=1), axis=0)
    post = h.shift(-1)
    traded = 0.5 * (post - pre).abs().sum(axis=1).iloc[:-1]
    return float(traded.sum() / years_of(h))


def max_abs_diff(a: pd.Series, b: pd.Series) -> dict:
    j = pd.concat([a, b], axis=1, join="inner").dropna()
    return {"months": int(len(j)), "max_abs_diff": float((j.iloc[:, 0] - j.iloc[:, 1]).abs().max()) if len(j) else None}


def saved_monthly(study: str, column: str, file: str = "monthly_returns.csv") -> pd.Series:
    df = pd.read_csv(RESEARCH / study / file, index_col=0)
    s = df[column].dropna()
    s.index = pd.PeriodIndex(s.index.astype(str), freq="M")
    return s


def cut(r: pd.Series, a: str, b: str) -> pd.Series | None:
    """``r`` on [a, b] when it covers the window (first / last return within COVER_DAYS of the ends), else None."""
    a_, b_ = pd.Timestamp(a), pd.Timestamp(b)
    if r.index[0] > a_ + pd.Timedelta(days=COVER_DAYS) or r.index[-1] < b_ - pd.Timedelta(days=COVER_DAYS):
        return None
    return r.loc[a_:b_]


# ======================================================================== the series

def etf_lines(d: etfs.Data, log: RunLog, checks: dict) -> tuple[dict, dict, dict]:
    """T01, LEV-B3 (+ 1.37x), V3 (+ constant e-bar, + long window) and their practicality figures."""
    series, refs, prac = {}, {}, {}
    # T01 on the qqq_timing data and engine
    data = index_history.load_dev_data(END, index_history.QQQ_RAW)
    p = (1 + data.r1).cumprod()
    rc = data.rf - CASH_ETF_FEE / TRADING_DAYS
    tgt = target_exposure(T01_RULE, p, data.r1)
    sim = ex_engine.simulate(tgt, data.r1, synthetic_2x(data.r1, data.rf, T01_SPREAD), rc, data.price, T01_ENTRY, END)
    r = sim["ret"]
    series["T01"] = r
    e = sim["exposure_held"]
    prac["T01"] = {"trades_per_year": sim["orders"] / years_of(r), "cost_drag_per_year": sim["cost_frac"] / years_of(r),
                   "turnover_one_way_per_year": float(e.diff().abs().sum() / years_of(r)),
                   "average_exposure": float(e.mean())}
    m = monthly(r)
    checks["T01"] = {
        "vs_qqq_timing_dev_1999_2014": max_abs_diff(m, saved_monthly("qqq_timing_dev", "T01_L200_b2")),
        "vs_qqq_timing_oneshot_2015_2026": max_abs_diff(m, saved_monthly("qqq_timing_oneshot", "T01_L200_b2"))}
    # LEV-B3 and its matched constant 1.37x (sector_lev data and flagged-weights engine)
    w, flag = part_b_targets("B3", d.p_qqq, {"B3": (LEV["on"], LEV["off"])}, LEV["sma"], LEV["band"])
    s_i = fw.entry_index(w, flag, d, False)
    sim = fw.simulate(w, flag, d.rets, d.close, s_i)
    r = sim["ret"]
    series["LEV-B3"] = r
    prac["LEV-B3"] = {"trades_per_year": sim["orders"] / years_of(r),
                      "cost_drag_per_year": sim["cost_frac"] / years_of(r),
                      "turnover_one_way_per_year": one_way_turnover(sim["held"], d.rets),
                      "average_exposure": float(vt.held_exposure(sim["held"]).mean())}
    checks["LEV-B3"] = {"entry_close": str(d.sessions[s_i].date()),
                        "vs_sector_lev_monthly": max_abs_diff(monthly(r), saved_monthly("sector_lev", "B3"))}
    cw, cf = part_b_targets("C137", d.p_qqq, {"C137": (LEV["matched_exposure"],) * 2}, LEV["sma"], LEV["band"])
    refs["LEV-B3"] = ("恒定 1.37 倍（63% QQQ + 37% QLD，月末再平衡）", fw.simulate(cw, cf, d.rets, d.close, s_i)["ret"])
    # V3, its constant-exposure comparison and the report-only long window (voltarget)
    w, flag, _ = vt.targets("V3", d, VOL_PARAMS)
    s_i = fw.entry_index(w, flag, d, True, V3_ENTRY)
    sim = fw.simulate(w, flag, d.rets, d.close, s_i)
    r = sim["ret"]
    series["V3"] = r
    e_bar = float(vt.held_exposure(sim["held"]).mean())
    prac["V3"] = {"trades_per_year": sim["orders"] / years_of(r), "cost_drag_per_year": sim["cost_frac"] / years_of(r),
                  "turnover_one_way_per_year": one_way_turnover(sim["held"], d.rets), "average_exposure": e_bar}
    cw, cf = vt.constant_targets(e_bar, d.sessions)
    refs["V3"] = (f"恒定 {e_bar:.2f} 倍（V3 自己的平均仓位）", fw.simulate(cw, cf, d.rets, d.close, s_i)["ret"])
    s_l = int(d.sessions.get_loc(pd.Timestamp(V3_LONG_ENTRY)))
    series["V3 长窗口"] = fw.simulate(w, flag, d.rets, d.close, s_l)["ret"]
    saved = json.loads((RESEARCH / "voltarget/results.json").read_text())
    checks["V3"] = {"entry_close": str(d.sessions[s_i].date()),
                    "vs_voltarget_monthly": max_abs_diff(monthly(r), saved_monthly("voltarget", "V3")),
                    "e_bar": e_bar, "e_bar_saved": saved["rules"]["V3"]["constant_exposure"]["e_bar"],
                    "long_cagr": cagr_of(series["V3 长窗口"]),
                    "long_cagr_saved": saved["long_window_spliced_report_only"]["V3"]["cagr"]}
    log.say("ETF lines:", json.dumps(checks, default=str))
    return series, refs, prac


def bt4b_line(d: etfs.Data, log: RunLog, checks: dict) -> tuple[pd.Series, dict]:
    """BT4b exactly as studies/bitcoin.py run_sleeve (no band, no trend filter, Bitstamp primary close)."""
    src = bd.load_sources(END)
    p, _ = bd.primary_close(src)
    sessions = d.sessions[d.sessions >= pd.Timestamp("2013-01-01")]
    rets = pd.DataFrame({"QQQ": d.rets["QQQ"].reindex(sessions),
                         "BTC": bd.on_sessions(p, sessions).pct_change(fill_method=None),
                         "CASH": d.rf.reindex(sessions), "ONEQ": d.rets["ONEQ"].reindex(sessions)}, index=sessions)
    prices = pd.DataFrame({"QQQ": d.close["QQQ"].reindex(sessions), "ONEQ": d.close["ONEQ"].reindex(sessions),
                           "BTC": np.nan, "CASH": np.nan}, index=sessions)
    w, flag = bt.sleeve_targets(bt.SPEC["BT4b"], sessions, None)
    fns = {"BTC": lambda v, px, sell, ts: ibkr_crypto_cost(v, BT["btc_half_spread"]),
           "QQQ": lambda v, px, sell, ts: etf_order_cost(v, px, sell, BT["qqq_half_spread"]),
           "ONEQ": lambda v, px, sell, ts: etf_order_cost(v, px, sell, BT["oneq_half_spread"])}
    s_i = sessions.get_loc(pd.Timestamp(BT_ENTRY))
    sim = bt.simulate(w, flag, rets, prices, s_i, fns, monthly_fee=PAXOS_MONTHLY_FEE)
    r = sim["ret"]
    years = (r.index[-1] - r.index[0]).days / 365.25
    prac = {"trades_per_year": sim["orders"] / years,
            "cost_drag_per_year": sim["cost_dollars"] / float(sim["value"].mean()) / years,
            "turnover_one_way_per_year": one_way_turnover(sim["held"], rets)}
    checks["BT4b"] = {"vs_bitcoin_monthly": max_abs_diff(monthly(r), saved_monthly("bitcoin", "BT4b"))}
    log.say("BT4b:", checks["BT4b"])
    return r, prac


def megacap_lines(log: RunLog, checks: dict) -> tuple[dict, dict, dict]:
    nav = pd.read_csv(RESEARCH / "megacap/nav_daily.csv", index_col=0, parse_dates=True)
    summ = pd.read_csv(RESEARCH / "megacap/summary.csv")
    full, oos, prac = {}, {}, {}
    a, b = MEGA_FULL
    for m in MEGA:
        v = nav[m].dropna()
        r = returns_from_nav(v).loc[a:b]
        full[m] = r
        row = summ[(summ["rule"] == m) & (summ["period"] == "Full 2014-2026-08")].iloc[0]
        prac[m] = {"trades_per_year": float(row["orders"]) / float(row["years"]),
                   "turnover_one_way_per_year": float(row["turnover_one_way_per_year"]),
                   "cost_drag_per_year": float(row["cost_drag_per_year"])}
        checks[m] = {"cagr_calendar": calendar_cagr(r, v.index[v.index < pd.Timestamp(a)][-1]),
                     "cagr_saved": float(row["cagr"]), "cagr_252": cagr_of(r)}
    for m, study in MEGA_OOS_NAV.items():
        o = pd.read_csv(CACHE_ROOT / study / "built/nav_daily.csv", index_col=0, parse_dates=True)[m].dropna()
        r = returns_from_nav(o).loc["1999-03-11":"2013-12-31"]
        oos[m] = r
        saved = json.loads((RESEARCH / study / "results.json").read_text())["results"][m]["periods"]
        checks[f"{m} OOS"] = {"cagr_calendar": calendar_cagr(r, pd.Timestamp("1999-03-10")),
                              "cagr_saved": saved["Full 1999-03-10..2013"]["cagr"], "cagr_252": cagr_of(r)}
    log.say("mega-cap:", json.dumps({k: v for k, v in checks.items() if k.startswith("M")}))
    return full, oos, prac


def selective_t_line(log: RunLog, checks: dict) -> tuple[pd.Series, pd.Series, dict]:
    qqq = st.load_qqq_like("QQQ", ST_SOURCES["QQQ"])
    stocks = {sym: st.load_stock(sym, ST_RAW) for sym in st.STOCKS}
    rf = daily_rf(qqq.s.sessions, st.END)
    ref_cache: dict = {}
    outs = {}
    for sym, m in stocks.items():
        b = st.stock_bounds(m, {"full": ST_FULL})["full"]
        outs[sym] = st.run_one(m, *b, "S3-Yb", "stock", "net", rf, ref_cache)
    r = st.basket_mean([o["ret"] for o in outs.values()])
    hm = st.basket_mean([o["hmatch"] for o in outs.values()])
    trips = sum(len(o["sim"]["trips"]) for o in outs.values())
    saved = json.loads((RESEARCH / "selective_t/results.json").read_text())
    f = saved["assets"]["U18"]["configs"]["S3-Yb"]["periods"]["full"]["net"]
    checks["S3-Yb"] = {"cagr": cagr_of(r), "cagr_saved": f["cagr"], "hmatch_cagr": cagr_of(hm),
                       "hmatch_cagr_saved": f["hmatch_cagr"], "trips": trips, "trips_saved": f["trips"]}
    prac = {"trades_per_year": 2 * trips / years_of(r), "cost_drag_per_year": f["cost_drag_per_year"]}
    log.say("S3-Yb:", checks["S3-Yb"])
    return r, hm, prac


def sel_lines(log: RunLog, checks: dict) -> tuple[dict, dict, dict]:
    data = tg.load_all(ST_SOURCES, ST_RAW, TG_PANEL, TG_TOP20)
    ids = list(SEL.values())
    ex = tg.exact_run(data, "QQQ", ids, None, None, tg.CostModel())
    surf = pd.read_csv(RESEARCH / "t_grid/surface_QQQ.csv.gz", usecols=["full_cagr", "full_excess_cagr_hmatch",
                                                                         "full_trips_per_acct_year"])
    series, refs, prac = {}, {}, {}
    for k, (name, i) in enumerate(SEL.items()):
        r = ex["R"][i].dropna()
        hm = tg.hmatch_series(ex, k).reindex(r.index)
        tr = grid_trip_summary(ex, k)
        series[name], refs[name] = r, ("H_match（同平均仓位的 QQQ）", hm)
        size = 0.75 * (0.5 if name == "SEL-A" else 1.0)          # a trade is f x the 75% base (f = 1/2, 1)
        prac[name] = {"trades_per_year": 2 * tr["trips_per_acct_year"],
                      "cost_drag_per_year": tr["trips_per_acct_year"] * tr["cost_per_trip_bp"] / 1e4 * size}
        checks[name] = {"config_id": i, "cagr": cagr_of(r), "cagr_saved": float(surf.loc[i, "full_cagr"]),
                        "excess_vs_hmatch": cagr_of(r) - cagr_of(hm),
                        "excess_vs_hmatch_saved": float(surf.loc[i, "full_excess_cagr_hmatch"]),
                        "trips_per_year": tr["trips_per_acct_year"],
                        "trips_per_year_saved": float(surf.loc[i, "full_trips_per_acct_year"])}
    log.say("SEL:", json.dumps(checks["SEL-A"]), json.dumps(checks["SEL-P"]))
    return series, refs, prac


def smisp_line(log: RunLog, checks: dict) -> tuple[pd.Series, dict]:
    nav = pd.read_csv(RESEARCH / "short_overlay/nav_daily.csv.gz", parse_dates=["date"])
    nav = nav[nav["config"] == SMISP]
    summ = pd.read_csv(RESEARCH / "short_overlay/summary.csv")
    parts, chk, orders, years, cost = [], {}, 0.0, 0.0, 0.0
    for fold in ("H1", "H2"):
        x = nav[nav["fold"] == fold].set_index("date")["nav"]
        r = returns_from_nav(x)
        parts.append(r)
        row = summ[(summ["config"] == SMISP) & (summ["variant"] == "main") & (summ["fold"] == fold)].iloc[0]
        yrs = float(row["years"])
        chk[fold] = {"cagr_calendar": calendar_cagr(r, x.index[0]), "cagr_saved": float(row["cagr"]),
                     "opening_close": str(x.index[0].date()), "first_return": str(r.index[0].date())}
        orders += float(row["orders_per_month"]) * float(row["months"])
        years += yrs
        cost += float(row["trade_cost_pct_yr"]) * yrs
    r = pd.concat(parts)
    checks["S-MISP"] = chk
    prac = {"trades_per_year": orders / years, "cost_drag_per_year": cost / years}
    log.say("S-MISP:", chk)
    return r, prac


def verify(checks: dict) -> list:
    """Stop unless every series reproduces its study's saved output: monthly returns within the CSV rounding (6
    decimals; T01's 2015-2026 piece within 5e-5, since the one-shot account restarted at $10k and the minimum
    commissions weigh differently), CAGRs and counts exactly (SEL against the surface table's 6 digits)."""
    bad = []

    def close(name, a, b, tol, rel=False):
        if not (abs(a - b) <= (tol * abs(b) if rel else tol)):
            bad.append(f"{name}: {a} vs {b}")
    t = checks["T01"]
    close("T01 dev", t["vs_qqq_timing_dev_1999_2014"]["max_abs_diff"], 0.0, 1e-6)
    close("T01 one-shot", t["vs_qqq_timing_oneshot_2015_2026"]["max_abs_diff"], 0.0, 5e-5)
    close("LEV-B3", checks["LEV-B3"]["vs_sector_lev_monthly"]["max_abs_diff"], 0.0, 1e-6)
    close("V3", checks["V3"]["vs_voltarget_monthly"]["max_abs_diff"], 0.0, 1e-6)
    close("V3 e_bar", checks["V3"]["e_bar"], checks["V3"]["e_bar_saved"], 1e-12)
    close("V3 long", checks["V3"]["long_cagr"], checks["V3"]["long_cagr_saved"], 1e-12)
    close("BT4b", checks["BT4b"]["vs_bitcoin_monthly"]["max_abs_diff"], 0.0, 1e-6)
    for m in MEGA:
        for k in (m, f"{m} OOS"):
            close(k, checks[k]["cagr_calendar"], checks[k]["cagr_saved"], 1e-12)
    s3 = checks["S3-Yb"]
    close("S3-Yb", s3["cagr"], s3["cagr_saved"], 1e-12)
    close("S3-Yb H_match", s3["hmatch_cagr"], s3["hmatch_cagr_saved"], 1e-12)
    close("S3-Yb trips", s3["trips"], s3["trips_saved"], 0)
    for k in SEL:
        c = checks[k]
        close(k, c["cagr"], c["cagr_saved"], 1e-5, rel=True)
        close(f"{k} vs H_match", c["excess_vs_hmatch"], c["excess_vs_hmatch_saved"], 1e-5, rel=True)
        close(f"{k} trips", c["trips_per_year"], c["trips_per_year_saved"], 1e-5, rel=True)
    for fold, c in checks["S-MISP"].items():
        close(f"S-MISP {fold}", c["cagr_calendar"], c["cagr_saved"], 1e-12)
    if bad:
        raise SystemExit("series do not reproduce their studies' saved outputs:\n  " + "\n  ".join(bad))
    return bad


def sp_top10_annual() -> dict:
    """The few annual figures the ledger's yearly table allows (not the scorecard: no monthly series)."""
    y = pd.DataFrame(SP_YEARS, index=["sp", "oneq", "qqq"]).T / 100
    full = y.loc[:2025]
    return {"source": "docs/research_ledger_qc_factors.md 3.7.1 (QuantConnect run df141abf…, not re-run)",
            "years": "2003-2025 whole years (2026 = Jan-Jun, left out of the shares)",
            "worst_year": float(full["sp"].min()), "worst_year_which": int(full["sp"].idxmin()),
            "positive_years_share": float((full["sp"] > 0).mean()),
            "years_beating_oneq_share": float((full["sp"] > full["oneq"]).mean()),
            "years_beating_qqq_share": float((full["sp"] > full["qqq"]).mean()),
            "years_lagging_oneq_by_10pp_or_more": [int(k) for k in full.index[(full["sp"] - full["oneq"]) <= -0.10]],
            **SP_LEDGER}


# ======================================================================== scoring

def card(name: str, r: pd.Series, bench: dict, meta: dict, n_boot: int) -> pd.DataFrame:
    c = sc.scorecard(r, freq="D", oneq=bench["ONEQ*"], qqq=bench["QQQ"], rf=bench["rf"], meta=meta, n_boot=n_boot)
    v = c.set_index("key")["value"]
    extra = pd.DataFrame([{"family": "market", "family_zh": sc.FAMILIES["market"], "key": "capture_spread",
                           "value": float(v["up_capture_vs_oneq"] - v["down_capture_vs_oneq"]), "unit": "ratio",
                           "freq": "M", "meaning_zh": "上涨捕获 − 下跌捕获：大于 0 说明涨时跟得多、跌时跟得少",
                           "note": ""}])
    return pd.concat([c, extra], ignore_index=True)


def matched_row(name: str, label: str, r: pd.Series, ref: pd.Series, n_boot: int) -> dict:
    """Excess over the matched comparison (same days): CAGR difference, monthly-excess t, bootstrap interval."""
    j = pd.concat([r, ref], axis=1, join="inner").dropna()
    a, b = j.iloc[:, 0], j.iloc[:, 1]
    ret = sc.return_metrics(a, oneq=b, freq="D")
    conf = sc.confidence_metrics(a, oneq=b, freq="D", n_boot=n_boot)
    return {"candidate": name, "matched": label, "start": str(a.index[0].date()), "end": str(a.index[-1].date()),
            "cagr": cagr_of(a), "matched_cagr": cagr_of(b), "excess_cagr": ret["excess_cagr_vs_oneq"],
            "t": conf["t_vs_oneq"], "p": conf["p_vs_oneq"], "ci_lo": conf["excess_cagr_ci_lo"],
            "ci_hi": conf["excess_cagr_ci_hi"], "boot_share_positive": conf["excess_cagr_boot_share_positive"]}


def angle_ranks(cmp: pd.DataFrame, names: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per angle: each metric's rank (1 = best, ties averaged, missing values unranked) and the average rank.
    Benchmarks are left out of the confidence angle (their t is 0 by construction), and ONEQ out of the two
    "share of periods beating ONEQ" metrics (it cannot beat itself)."""
    rows, avg = [], {}
    for angle, mets in ANGLES.items():
        who = [n for n in names if not (angle == "统计可信度" and n in BENCHMARKS)]
        rk = []
        for key, sign in mets:
            vals = pd.to_numeric(cmp.loc[key, who], errors="coerce") * sign
            if key in SELF_COMPARISONS:
                vals[[n for n in who if n in ("ONEQ", "ONEQ*")]] = np.nan
            r = vals.rank(ascending=False, method="average")
            rk.append(r)
            for n in who:
                rows.append({"angle": angle, "metric": key, "better": "higher" if sign > 0 else "lower",
                             "candidate": n, "value": cmp.loc[key, n], "rank": r[n]})
        avg[angle] = pd.concat(rk, axis=1).mean(axis=1, skipna=True)
    return pd.DataFrame(rows), pd.DataFrame(avg).reindex(names)


# ======================================================================== main

def run(args) -> dict:
    t0 = time.time()
    log = RunLog()
    n_boot = int(args.n_boot)
    d = etfs.load_data(END)
    bench = {"ONEQ*": d.rets["BENCH"].dropna(), "QQQ": d.rets["QQQ"].dropna(), "rf": d.rf}
    checks: dict = {}
    series = {"ONEQ": d.rets["ONEQ"].dropna(), "QQQ": bench["QQQ"]}
    etf, refs, prac = etf_lines(d, log, checks)
    series.update({k: etf[k] for k in ("T01", "LEV-B3", "V3")})
    series["BT4b"], prac["BT4b"] = bt4b_line(d, log, checks)
    mega, mega_oos, p_mega = megacap_lines(log, checks)
    series.update(mega)
    prac.update(p_mega)
    series["S3-Yb"], s3_hm, prac["S3-Yb"] = selective_t_line(log, checks)
    refs["S3-Yb"] = ("H_match（18 个账户同平均仓位的买入持有）", s3_hm)
    sel, sel_refs, p_sel = sel_lines(log, checks)
    series.update(sel)
    refs.update(sel_refs)
    prac.update(p_sel)
    series["S-MISP"], prac["S-MISP"] = smisp_line(log, checks)
    verify(checks)
    log.say("CHECKS OK: every series reproduces its study's saved output")
    extra ={"V3 长窗口": etf["V3 长窗口"], **{f"{m} OOS": r for m, r in mega_oos.items()}, "ONEQ*": bench["ONEQ*"]}
    meta = {k: {**v, **{kk: vv for kk, vv in prac.get(k, {}).items() if kk in
                        ("trades_per_year", "turnover_one_way_per_year", "cost_drag_per_year")}}
            for k, v in META.items()}
    meta["V3 长窗口"] = {**meta["V3"], "n_trials": 8}
    for m in MEGA:
        meta[f"{m} OOS"] = meta[m]
    meta["ONEQ*"] = meta["ONEQ"]
    for k in prac:
        log.say(f"practicality {k}:", json.dumps(prac[k]))

    plan = {"full": {k: r for k, r in {**series, "V3 长窗口": extra["V3 长窗口"],
                                        **{f"{m} OOS": extra[f"{m} OOS"] for m in MEGA}}.items()}}
    for w, (a, b) in WINDOWS.items():
        pool = {**series, **extra} if w == "oos" else series
        plan[w] = {}
        for k, r in pool.items():
            if w == "oos" and k in ("ONEQ",) or (w != "oos" and k.endswith("OOS")):
                continue
            x = cut(r, a, b)
            if x is not None:
                plan[w][k] = x
    OUT.mkdir(parents=True, exist_ok=True)
    long_rows, compare, coverage = [], {}, []
    for w, pool in plan.items():
        cards = {}
        for k, r in pool.items():
            c = card(k, r, bench, meta.get(k, {}), n_boot)
            cards[k] = c
            long_rows.append(c.assign(window=w, candidate=k))
            coverage.append({"window": w, "candidate": k, "start": str(r.index[0].date()),
                             "end": str(r.index[-1].date()), "sessions": int(len(r)), "years": round(years_of(r), 2)})
        cmp = sc.compare(cards)
        cmp.to_csv(OUT / f"compare_{w}.csv", float_format="%.6g")
        head = pd.concat({a: cmp.loc[ks, list(cards)] for a, ks in HEADLINE.items()}, names=["angle", "key"])
        head.to_csv(OUT / f"headline_{w}.csv", float_format="%.6g")
        compare[w] = cmp
        log.say(f"window {w}: {len(cards)} series scored ({time.time() - t0:.0f}s)")
    tall = pd.concat(long_rows, ignore_index=True)[["window", "candidate"] + sc.COLUMNS]
    tall.to_csv(OUT / "scorecard_long.csv", index=False, float_format="%.6g")
    pd.DataFrame(coverage).to_csv(OUT / "coverage.csv", index=False)

    ranks_out = {}
    for w in ("common", "long", "oos"):
        names = list(plan[w])
        detail, avg = angle_ranks(compare[w], names)
        detail.to_csv(OUT / f"angle_detail_{w}.csv", index=False, float_format="%.6g")
        avg.to_csv(OUT / f"angle_ranks_{w}.csv", float_format="%.2f")
        ranks_out[w] = avg.round(2).to_dict()

    matched = [matched_row(k, lab, series[k], ref, n_boot) for k, (lab, ref) in refs.items()]
    for m in MEGA:
        matched.append(matched_row(m, "QQQ（市值加权的纳斯达克 100）", series[m], bench["QQQ"], n_boot))
    matched.append(matched_row("BT4b", "QQQ 单独持有", series["BT4b"], bench["QQQ"], n_boot))
    matched.append(matched_row("S-MISP", "QQQ", series["S-MISP"], bench["QQQ"], n_boot))
    pd.DataFrame(matched).to_csv(OUT / "matched.csv", index=False, float_format="%.6g")

    res = {"end": END, "n_boot": n_boot, "windows": {"full": "each series' own history", **WINDOWS},
           "benchmark": "ONEQ* = ONEQ total return from 2003-10-02, Nasdaq Composite price index before",
           "checks": checks, "practicality_from_runs": prac, "meta": meta, "sp_top10_ledger_only": sp_top10_annual(),
           "angles": {a: [{"metric": k, "better": "higher" if s > 0 else "lower"} for k, s in v]
                      for a, v in ANGLES.items()},
           "average_rank_by_angle": ranks_out, "matched": matched, "runtime_s": round(time.time() - t0, 1)}
    write_json(OUT / "results.json", res, default=json_default)
    write_json(OUT / "checks.json", checks, default=json_default)
    log.say(f"done in {time.time() - t0:.0f}s; outputs in {OUT}")
    log.write(OUT / "run_log.txt")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n-boot", type=int, default=N_BOOT, help="bootstrap resamples per metric (0 skips them)")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
