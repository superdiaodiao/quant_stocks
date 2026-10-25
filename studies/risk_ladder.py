"""Risk ladder: how much more return each extra step of risk bought, 1999-2026 (pre-registered in
docs/research_ledger_risk_ladder.md, section 0; a decision aid, not a pass / fail test).

Portfolios (``PORTFOLIOS``; $10,000, IBKR Pro costs, month-end rebalance unless noted): ONEQ and QQQ buy-and-hold;
QQQ at 1.2 / 1.3 / 1.5 / 2.0x by margin loan (EFFR + 1.5%) and by QQQ + QLD; QQQ + 5 / 10 / 20% BTC; 1.2 / 1.3x QQQ
+ 10% BTC by margin and by QLD; four QQQ 200-day SMA rules (sector_lev B3 / B4, qqq_timing T12 / T02, traded the
session after a decision). BTC = the bitcoin study's spot series minus a Hong Kong spot-ETF fee, from the
2014-09-30 close (each BTC portfolio holds its no-BTC parent before). Mega-cap M1 / M3 are copied from the megacap
outputs, not recomputed. Engine: quant.strategies.leverage_methods.simulate_batch (margin, forced liquidation at
the day's low); inputs from quant.strategies.risk_ladder.

Metrics: single paths (1999-03-10.., ..2009, 2010.., 2014-10..), 211 rolling 10-year windows, a circular block
bootstrap of monthly returns (24-month blocks, 10,000 draws; 10 / 20 years), +$300 a month contributions,
forced-liquidation thresholds and a 40% maintenance stress, volatility drag and the growth-optimal leverage (its
bootstrap with circular blocks since the post-verification re-run; ``KELLY_CIRCULAR``).

Data (read only): research_cache/sector_lev/raw, research_cache/leverage_methods/raw, research_cache/bitcoin/raw;
output/research_only/megacap{,_oos2,_oos3}/results.json. Every frame truncated at ``END``. Outputs:
output/research_only/risk_ladder/ (returns and metrics only, no price levels).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study risk_ladder [--register-only]
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from quant.data import bitcoin as bd
from quant.data import sector_etfs as etfs
from quant.data.effr import load_effr
from quant.data.guards import assert_dev_dates
from quant.data.sources.yahoo import parse_chart
from quant.evaluation.block_bootstrap import circular_block_index, path_outcomes, pct
from quant.evaluation.contributions import contribution_stats, path_contribution_metrics
from quant.paths import CACHE_ROOT, ROOT, output_dir
from quant.reporting import Preregistration, RunLog, write_json
from quant.strategies import leverage_methods as lm
from quant.strategies import risk_ladder as rl

END = "2026-09-30"
OUT = output_dir("risk_ladder")
LEDGER = ROOT / "docs/research_ledger_risk_ladder.md"
LM_RAW = CACHE_ROOT / "leverage_methods/raw"
MEGACAP_OUT = {k: output_dir(k) / "results.json" for k in ("megacap", "megacap_oos2", "megacap_oos3")}

ENTRY = "1999-03-10"
SPLIT = "2009-12-31"
BTC_FIRST_CLOSE = "2014-09-30"
ONEQ_FIRST = "2003-10-01"
PERIODS = {"full": ENTRY, "1999_2009": ENTRY, "2010_2026": SPLIT, "2014_2026": BTC_FIRST_CLOSE}
ROLL_FIRST, ROLL_LAST, ROLL_MONTHS = "1999-03-31", "2016-09-30", 120

# ---------------------------------------------------------------- frozen parameters (ledger section 0)
# Engine assumptions: identical to studies/leverage_methods.py ``P`` (leverage ledger 0.3; a test asserts equality).
FUT_MAINT = 0.07
P_LEV = lm.Params(
    ibkr_spread=0.015, daycount=360.0, spread_2x=etfs.SPREAD_2X, qqq_er=0.0020, oneq_er=0.0021,
    #                   QQQ   ONEQ  QLDX  TQQQX TBILL BENCH
    maint=np.array([0.25, 0.25, 0.50, 0.75, 0.25, 0.25]),
    half_spread=np.array([1e-4, 2e-4, 2e-4, 2e-4, 1e-4, 2e-4]),
    fut_basis=0.0030, fut_maint=FUT_MAINT, fut_init=FUT_MAINT * 1.1, fut_trade_cost=1e-4, fut_roll_cost=1e-4,
    roll_offset=5, liq_slip=0.005, liq_cushion=1.10, min_order=1.0, band=0.10)

BTC_FEE = 0.015                 # Hong Kong spot-BTC ETF all-in expense, main setting (ledger 0.4)
BTC_HS = 0.0015                 # half-spread of the HK ETF
BTC_MAINT = 1.0                 # no loan value
SENS_BTC = {"fee_1pct": dict(fee=0.010), "fee_2pct": dict(fee=0.020), "hs_50bp": dict(hs=0.0050),
            "ibit_like_ref": dict(fee=0.0025, hs=2e-4, cost="us_etf")}
VALIDATE_CRYPTO = dict(fee=0.0, hs=5e-4, cost="spot_crypto")     # vs bitcoin ledger BT4b
MAINT_STRESS = 0.40
SMA_LEN, SMA_BAND = 200, 0.02
INITIAL, MONTHLY_ADD = 10_000.0, 300.0
BOOT_BLOCK, BOOT_BLOCK_SENS, BOOT_DRAWS, BOOT_SEED = 24, 12, 10_000, 20261026
HORIZONS = {"A": (120, 240), "B": (120,)}
KELLY_SEED, KELLY_BLOCKS, KELLY_DRAWS = 20261008, (252, 504), 1000
# Amendment after the independent verification (ledger "核查后的修正"): leverage_methods.kelly's original draw never
# wraps, so the first / last ~block sessions of each segment are under-sampled (the 2008-09 crash and rebound in
# 1999-2009 at ~1/3 of the average rate with 504-session blocks). Same seed, draws and blocks; circular blocks.
# The first run's non-circular draw is still written next to it (``block_*_first_run_noncircular``).
KELLY_CIRCULAR = True
LOSS_1Y = -0.40                 # the owner's stated tolerance: a 40% one-year drop
DD_LEVELS = (-0.50, -0.75)
GROWTH_GRID = (0.0, 0.5, 1.0, 1.2, 1.3, 1.4, 1.5, 1.75, 2.0, 2.5, 3.0)


@dataclass(frozen=True)
class Spec:
    name: str
    group: str
    label: str
    method: str = ""                # leverage_methods method (constant weights) ...
    L: float = 1.0
    policy: str = "monthly"
    before: dict = field(default_factory=dict)     # ... or switched targets (BTC rows) ...
    after: dict = field(default_factory=dict)
    on: float | None = None                        # ... or an SMA rule (exposure on / off)
    off: float | None = None
    margin: bool = False
    btc: float = 0.0


def _q(e: float) -> dict:
    return rl.exposure_legs(e)


PORTFOLIOS = (
    Spec("ONEQ", "benchmark", "ONEQ buy and hold (Composite price before 2003-10)", method="BENCH_BH", policy="never"),
    Spec("QQQ", "benchmark", "QQQ buy and hold", method="QQQ_BH", policy="never"),
    *(Spec(f"M{L:.1f}", "margin", f"QQQ {L:.1f}x, IBKR margin loan", method="M", L=L, margin=True)
      for L in (1.2, 1.3, 1.5, 2.0)),
    *(Spec(f"Q{L:.1f}", "qld", f"QQQ {L:.1f}x via QQQ + QLD", method="Q2", L=L) for L in (1.2, 1.3, 1.5, 2.0)),
    *(Spec(f"BTC{int(s * 100)}", "btc", f"QQQ + {int(s * 100)}% BTC", before={"QQQ": 1.0},
           after={"QQQ": 1.0 - s, rl.BTC: s}, btc=s) for s in (0.05, 0.10, 0.20)),
    Spec("M1.2+BTC10", "lev_btc", "QQQ 1.2x margin + 10% BTC", before={"QQQ": 1.2}, after={"QQQ": 1.2, rl.BTC: 0.10},
         L=1.2, margin=True, btc=0.10),
    Spec("M1.3+BTC10", "lev_btc", "QQQ 1.3x margin + 10% BTC", before={"QQQ": 1.3}, after={"QQQ": 1.3, rl.BTC: 0.10},
         L=1.3, margin=True, btc=0.10),
    Spec("Q1.2+BTC10", "lev_btc", "QQQ 1.2x via QLD + 10% BTC", before=_q(1.2),
         after={"QQQ": 0.6, "QLDX": 0.3, rl.BTC: 0.10}, L=1.2, btc=0.10),
    Spec("Q1.3+BTC10", "lev_btc", "QQQ 1.3x via QLD + 10% BTC", before=_q(1.3),
         after={"QQQ": 0.5, "QLDX": 0.4, rl.BTC: 0.10}, L=1.3, btc=0.10),
    Spec("SMA-B3", "sma", "SMA200 on 1.5x / off 1.0x (sector_lev B3 = LEV-B3)", on=1.5, off=1.0, policy="never"),
    Spec("SMA-B4", "sma", "SMA200 on 1.25x / off 0.5x (sector_lev B4)", on=1.25, off=0.5, policy="never"),
    Spec("SMA-T12", "sma", "SMA200 on 2x / off 1x (qqq_timing T12_L200_b2)", on=2.0, off=1.0, policy="never"),
    Spec("SMA-T02", "sma", "SMA200 on 2x / off cash (qqq_timing T02_L200_b2)", on=2.0, off=0.0, policy="never"),
)
SPEC = {s.name: s for s in PORTFOLIOS}
BTC_ROWS = tuple(s.name for s in PORTFOLIOS if s.btc > 0)
MARGIN_ROWS = tuple(s.name for s in PORTFOLIOS if s.margin)


# ======================================================================== market and runs

@dataclass
class Setup:
    d: etfs.Data
    mk: lm.Market
    p: lm.Params
    cost: object
    r_btc_spot: pd.Series


def btc_spot_returns(sessions: pd.DatetimeIndex) -> pd.Series:
    p, _ = bd.primary_close(bd.load_sources(END))
    return bd.on_sessions(p, sessions).pct_change(fill_method=None)


def build(d: etfs.Data, mk0: lm.Market, r_spot: pd.Series, fee: float = BTC_FEE, hs: float = BTC_HS,
          cost: str = "hk_etf", maint_qqq: float | None = None) -> Setup:
    r = rl.fund_returns(r_spot, mk0.sessions, fee, BTC_FIRST_CLOSE)
    mk = rl.with_leg(mk0, rl.BTC, r, er_per_session=fee / 252)
    p = rl.params_for(P_LEV, mk, {rl.BTC: BTC_MAINT, **({"QQQ": maint_qqq} if maint_qqq else {})}, {rl.BTC: hs})
    k = mk.legs.index(rl.BTC)
    fn = {"hk_etf": rl.mixed_cost_fn({k: rl.hk_etf_cost}), "spot_crypto": rl.mixed_cost_fn({k: rl.spot_crypto_cost}),
          "us_etf": None}[cost]
    return Setup(d=d, mk=mk, p=p, cost=fn, r_btc_spot=r_spot)


def inputs(spec: Spec, su: Setup) -> dict:
    """Keyword arguments of ``simulate_batch`` for ``spec``."""
    mk = su.mk
    if spec.method:
        return dict(method=spec.method, L=spec.L, policy=spec.policy)
    if spec.on is not None:
        W, flags, _ = rl.sma_targets(mk, su.d.p_qqq, rl.exposure_legs(spec.on), rl.exposure_legs(spec.off), SMA_LEN,
                                     SMA_BAND)
        return dict(method=spec.name, L=spec.on, policy="never", targets=W, extra_flags=flags)
    W, flags = rl.switched_targets(mk, spec.before, spec.after, BTC_FIRST_CLOSE)
    return dict(method=spec.name, L=spec.L, policy=spec.policy, targets=W, extra_flags=flags)


def simulate(spec: Spec, su: Setup, starts, ends, liquidation: bool = True) -> dict:
    kw = inputs(spec, su)
    return lm.simulate_batch(su.mk, kw.pop("method"), kw.pop("L"), kw.pop("policy"), starts, ends, su.p,
                             liquidation, cost_fn=su.cost, **kw)


def single_paths(sessions: pd.DatetimeIndex) -> tuple[list, np.ndarray, np.ndarray]:
    last = len(sessions) - 1
    labels = list(PERIODS)
    starts = np.array([lm.session_index(sessions, PERIODS[k]) for k in labels])
    ends = np.array([lm.session_index(sessions, SPLIT) if k == "1999_2009" else last for k in labels])
    return labels, starts, ends


def path_row(res: dict, b: int, s: pd.DatetimeIndex, bench: dict, qqq: dict) -> dict:
    v = lm.path_values(res, b, s)
    m = lm.path_metrics(v, res, b)
    r = v.pct_change().iloc[1:]
    m["vol"] = float(r.std() * np.sqrt(252))
    for tag, ref in (("oneq", bench), ("qqq", qqq)):
        vb = lm.path_values(ref, b, s)
        yrs = (len(vb) - 1) / 252
        c = (vb.iloc[-1] / vb.iloc[0]) ** (1 / yrs) - 1
        m[f"{tag}_cagr"] = float(c)
        m[f"cagr_minus_{tag}"] = m["cagr"] - float(c)
    return m


def rolling_summary(w: pd.DataFrame, wb: pd.DataFrame, wq: pd.DataFrame, oneq_mask: np.ndarray) -> dict:
    c, cb = w["cagr"].values, wb["cagr"].values
    out = {"n": int(len(w)), "cagr_min": float(c.min()), "cagr_p5": float(np.percentile(c, 5)),
           "cagr_median": float(np.median(c)), "cagr_p95": float(np.percentile(c, 95)),
           "p_beat_oneq": float((c > cb).mean()), "p_beat_qqq": float((c > wq["cagr"].values).mean()),
           "median_minus_oneq": float(np.median(c - cb)), "max_dd_median": float(w["max_dd"].median()),
           "max_dd_worst": float(w["max_dd"].min()), "p_liquidation": float((w["liquidations"] > 0).mean()),
           "p_wipeout": float(w["wiped_out"].mean()), "p_1y_loss_gt_40": float((w["worst_1y"] < LOSS_1Y).mean()),
           "n_oneq_real": int(oneq_mask.sum()), "p_beat_oneq_real": float((c[oneq_mask] > cb[oneq_mask]).mean())}
    for lv in DD_LEVELS:
        out[f"p_dd_gt_{int(-lv * 100)}"] = float((w["max_dd"] <= lv).mean())
    return out


# ======================================================================== bootstrap

def bootstrap_rows(monthly: dict, names: list, sample: str, block: int, horizons: tuple, full: bool) -> list[dict]:
    """Paired circular block bootstrap of the monthly returns ``monthly[name]`` (same draws for all names)."""
    n = len(monthly["ONEQ"])
    rows = []
    for H in horizons:
        rng = np.random.default_rng(BOOT_SEED + 1000 * block + H)
        idx = circular_block_index(n, H, block, BOOT_DRAWS, rng)
        ob = path_outcomes(monthly["ONEQ"].values, idx)
        oc = contribution_stats(monthly["ONEQ"].values[idx], INITIAL, MONTHLY_ADD) if full else None
        for nm in names:
            o = path_outcomes(monthly[nm].values, idx)
            row = {"portfolio": nm, "sample": sample, "months_in_sample": n, "block": block, "horizon_months": H,
                   "draws": BOOT_DRAWS, **{f"cagr_{k}": v for k, v in pct(o["cagr"]).items()},
                   "p_cagr_negative": float((o["cagr"] < 0).mean()),
                   "p_beat_oneq": float((o["cagr"] > ob["cagr"]).mean()),
                   "median_minus_oneq": float(np.median(o["cagr"] - ob["cagr"])),
                   "max_dd_median": float(np.median(o["max_dd"])), "max_dd_p5": float(np.percentile(o["max_dd"], 5)),
                   "p_1y_loss_gt_40": float((o["worst_12m"] < LOSS_1Y).mean()),
                   "multiple_median": float(np.median(o["multiple"]))}
            for lv in DD_LEVELS:
                row[f"p_dd_gt_{int(-lv * 100)}"] = float((o["max_dd"] <= lv).mean())
            if full:
                cs = contribution_stats(monthly[nm].values[idx], INITIAL, MONTHLY_ADD)
                row.update({"contrib_final_over_put_in_median": float(np.median(cs["final_over_contributed"])),
                            "contrib_final_over_put_in_p5": float(np.percentile(cs["final_over_contributed"], 5)),
                            "contrib_p_below_put_in_at_end": float((cs["final_over_contributed"] < 1).mean()),
                            "contrib_irr_median": float(np.nanmedian(cs["irr"])),
                            "contrib_account_dd_median": float(np.median(cs["account_max_dd"])),
                            "contrib_p_account_dd_gt_50": float((cs["account_max_dd"] <= -0.5).mean()),
                            "contrib_worst_vs_put_in_median": float(np.median(cs["worst_vs_contributed"])),
                            "contrib_months_below_put_in_median": float(np.median(cs["months_below_contributed"])),
                            "contrib_twr_dd_median": float(np.median(cs["twr_max_dd"])),
                            "contrib_p_beat_oneq_final": float((cs["final_value"] > oc["final_value"]).mean())})
            rows.append(row)
    return rows


# ======================================================================== growth vs leverage

def growth_curve(mk: lm.Market, s: int, e: int, grid=GROWTH_GRID) -> dict:
    """Realised log growth a year of daily-rebalanced margin QQQ at each L (the kelly model of leverage_methods:
    L > 1 borrows at EFFR + spread, L < 1 earns T-bill; no costs, no liquidation) and the approximation
    r + L (mu - r) - L^2 sigma^2 / 2 with the period's mu, sigma, T-bill and borrowing rates."""
    r = np.nan_to_num(mk.R[s + 1:e + 1, mk.legs.index("QQQ")])
    rf, bo = mk.rf[s + 1:e + 1], mk.borrow[s + 1:e + 1]
    mu, var = r.mean() * 252, r.var() * 252
    rfa, rba = rf.mean() * 252, bo.sum() / (len(bo) / 252)
    out = {}
    for L in grid:
        c = np.where(L >= 1, -(L - 1) * bo, (1 - L) * rf)
        x = 1 + L * r + c
        g = float(np.log(x).mean() * 252) if (x > 0).all() else float("-inf")
        rr = rba if L >= 1 else rfa                  # financing rate of the (L - 1) part
        arith = rr + L * (mu - rr)
        out[f"{L:.2f}"] = {"log_growth": g, "cagr_equiv": float(np.expm1(g)) if np.isfinite(g) else -1.0,
                           "approx_log_growth": float(arith - L ** 2 * var / 2), "arith_return": float(arith),
                           "drag_L2_sigma2_half": float(L ** 2 * var / 2)}
    return {"mu_arith": float(mu), "vol": float(np.sqrt(var)), "tbill": float(rfa), "borrow": float(rba),
            "curve": out}


# ======================================================================== concentration rows (copied)

def concentration_rows() -> list[dict]:
    ins = json.loads(MEGACAP_OUT["megacap"].read_text())
    o2 = json.loads(MEGACAP_OUT["megacap_oos2"].read_text())
    o3 = json.loads(MEGACAP_OUT["megacap_oos3"].read_text())
    rows = []
    for nm, oos in (("M1", o2), ("M3", o3)):
        f = ins["results"][nm]["periods"]["Full 2014-2026-08"]
        rows.append({"portfolio": f"CONC-{nm}", "window": "in-sample 2014-01..2026-08", "start": f["start"],
                     "end": f["end"], "cagr": f["cagr"], "oneq_cagr": f["oneq_cagr"], "qqq_cagr": f["qqq_cagr"],
                     "max_dd": f["max_dd"], "oneq_max_dd": f["oneq_max_dd"], "qqq_max_dd": f["qqq_max_dd"],
                     "t_vs_oneq": f["t_monthly_excess_vs_oneq"], "t_vs_qqq": f["t_monthly_excess_vs_qqq"],
                     "worst_year": f["worst_year"], "worst_year_label": f["worst_year_label"],
                     "source": "output/research_only/megacap/results.json", "flag": "era-dependent"})
        x = oos["results"][nm]["periods"]["Full 1999-03-10..2013"]
        rows.append({"portfolio": f"CONC-{nm}", "window": "out-of-sample 1999-03-10..2013-12", "start": x["start"],
                     "end": x["end"], "cagr": x["cagr"], "qqq_cagr": x["bench_cagr"], "max_dd": x["max_dd"],
                     "qqq_max_dd": x["bench_max_dd"], "t_vs_qqq": x["t_monthly_excess"],
                     "source": f"output/research_only/{'megacap_oos2' if nm == 'M1' else 'megacap_oos3'}/results.json",
                     "flag": "era-dependent"})
        y = oos["results"][nm]["vs_oneq_2003_10_2013"]
        rows.append({"portfolio": f"CONC-{nm}", "window": "out-of-sample 2003-10..2013-12 vs ONEQ", "start": y["start"],
                     "end": y["end"], "cagr": y["cagr"], "oneq_cagr": y["bench_cagr"], "max_dd": y["max_dd"],
                     "oneq_max_dd": y["bench_max_dd"], "t_vs_oneq": y["t_monthly_excess"], "flag": "era-dependent"})
    return rows


# ======================================================================== run

def run(args=None) -> dict:
    log = RunLog()
    pre = Preregistration(LEDGER, OUT / "prereg_frozen.json", OUT)
    if not (OUT / "prereg_frozen.json").exists():
        pre.register()
    frozen = json.loads((OUT / "prereg_frozen.json").read_text())
    if frozen["sha256"] != pre.hash():
        raise SystemExit("section 0 of the ledger changed after registration")
    if getattr(args, "register_only", False):
        return frozen

    d = etfs.load_data(END)
    effr = load_effr(END, LM_RAW)
    tqqq = parse_chart(LM_RAW / "chart_TQQQ.json", END)
    lows = {t: lm.chart_ohlc(etfs.RAW / f"chart_{t}.json", END) for t in ("QQQ", "ONEQ")}
    mk0 = lm.build_market(d, effr, tqqq, lows, P_LEV)
    s = mk0.sessions
    r_spot = btc_spot_returns(s)
    for ix in (effr.index, pd.DatetimeIndex(tqqq["date"]), s, r_spot.dropna().index):
        assert_dev_dates(ix, END)
    su = build(d, mk0, r_spot)
    log.say("DATE GUARD: every frame truncated at", END, mk0.info, "BTC leg from the", BTC_FIRST_CLOSE, "close")
    res = {"end": END, "prereg_sha256": frozen["sha256"], "guard": {**d.guard, **mk0.info},
           "params": {"btc_fee": BTC_FEE, "btc_half_spread": BTC_HS, "btc_maint": BTC_MAINT, "maint_stress": MAINT_STRESS,
                      "sma": [SMA_LEN, SMA_BAND], "initial": INITIAL, "monthly_add": MONTHLY_ADD,
                      "boot": [BOOT_BLOCK, BOOT_BLOCK_SENS, BOOT_DRAWS, BOOT_SEED], "kelly": [KELLY_SEED, KELLY_BLOCKS],
                      "kelly_circular": KELLY_CIRCULAR}}

    labels, sp_st, sp_en = single_paths(s)
    rs, re_ = lm.rolling_windows(s, ROLL_FIRST, ROLL_LAST, ROLL_MONTHS)
    starts, ends = np.r_[sp_st, rs], np.r_[sp_en, re_]
    nl = len(labels)
    oneq_mask = rs >= lm.session_index(s, ONEQ_FIRST)
    bench = simulate(SPEC["ONEQ"], su, starts, ends)
    qqq = simulate(SPEC["QQQ"], su, starts, ends)
    sub = lambda r_: {**r_, "V": r_["V"][:, nl:], "starts": r_["starts"][nl:], "ends": r_["ends"][nl:],
                      "liq": r_["liq"][nl:], "dead": r_["dead"][nl:]}
    wb, wq = rl.window_outcomes(sub(bench), s), rl.window_outcomes(sub(qqq), s)

    rows, roll_rows, monthly_full, monthly_btc, contrib_rows, drag_rows, sims = [], [], {}, {}, [], [], {}
    for spec in PORTFOLIOS:
        r_ = simulate(spec, su, starts, ends)
        sims[spec.name] = r_
        for b, lab in enumerate(labels):
            m = path_row(r_, b, s, bench, qqq)
            note = ""
            if spec.btc > 0:
                note = {"full": "QQQ-only parent before 2014-10", "1999_2009": "= no-BTC parent (BTC did not exist)",
                        "2010_2026": "QQQ-only parent before 2014-10", "2014_2026": "BTC sleeve throughout"}[lab]
            rows.append({"portfolio": spec.name, "group": spec.group, "period": lab, "note": note, **m})
            v = lm.path_values(r_, b, s)
            rm = rl.monthly_returns(v)
            drag_rows.append({"portfolio": spec.name, "period": lab, **rl.drag_row(rm)})
            if lab == "full":
                monthly_full[spec.name] = rm
                contrib_rows.append({"portfolio": spec.name, "entry": "1999-03",
                                     **path_contribution_metrics(rm, INITIAL, MONTHLY_ADD)})
            if lab == "2014_2026":
                monthly_btc[spec.name] = rm
                contrib_rows.append({"portfolio": spec.name, "entry": "2014-10",
                                     **path_contribution_metrics(rm, INITIAL, MONTHLY_ADD)})
        w = rl.window_outcomes(sub(r_), s)
        roll_rows.append({"portfolio": spec.name, "group": spec.group,
                          "n_windows_with_btc": int((rs >= lm.session_index(s, BTC_FIRST_CLOSE)).sum()) if spec.btc else 0,
                          **rolling_summary(w, wb, wq, oneq_mask)})
        f = rows[-nl]
        log.say(f"{spec.name:11s} full CAGR {f['cagr']:+.2%} MDD {f['max_dd']:.1%} | 99-09 {rows[-nl + 1]['cagr']:+.2%} "
                f"10-26 {rows[-nl + 2]['cagr']:+.2%} 14-26 {rows[-1]['cagr']:+.2%} | 10y beat ONEQ "
                f"{roll_rows[-1]['p_beat_oneq']:.0%} liq {f['liquidations']}")

    # ---- validation (report only)
    val = {}
    lmsum = pd.read_csv(output_dir("leverage_methods") / "summary.csv")
    diffs = []
    for spec in PORTFOLIOS:
        if spec.method in ("M", "Q2"):
            for lab, per in (("full", "full"), ("1999_2009", "p1"), ("2010_2026", "p2")):
                ref = lmsum[(lmsum["variant"] == "main") & (lmsum["method"] == spec.method) &
                            (np.isclose(lmsum["L"], spec.L)) & (lmsum["policy"] == "monthly") & (lmsum["period"] == per)]
                mine = [x for x in rows if x["portfolio"] == spec.name and x["period"] == lab][0]
                diffs.append(abs(float(ref["cagr"].iloc[0]) - mine["cagr"]))
    val["leverage_methods_cagr_max_abs_diff"] = float(max(diffs))
    sl = pd.read_csv(output_dir("sector_lev") / "summary.csv")
    for nm, cfg in (("SMA-B3", "B3"), ("SMA-B4", "B4")):
        ref = sl[(sl["config"] == cfg) & (sl["period"] == "full")].iloc[0]
        mine = [x for x in rows if x["portfolio"] == nm and x["period"] == "full"][0]
        val[f"{nm}_vs_sector_lev"] = {"cagr_here": mine["cagr"], "cagr_sector_lev": float(ref["cagr"]),
                                      "max_dd_here": mine["max_dd"], "max_dd_sector_lev": float(ref["max_dd"]),
                                      "start_sector_lev": ref["start"]}
    sv = build(d, mk0, r_spot, **VALIDATE_CRYPTO)
    rv = simulate(SPEC["BTC10"], sv, sp_st, sp_en)
    btcsum = pd.read_csv(output_dir("bitcoin") / "summary.csv")
    bt4b = btcsum[(btcsum["rule"] == "BT4b") & (btcsum["period"] == "full")].iloc[0]
    v14 = path_row(rv, 3, s, bench, qqq)
    val["BTC10_spot_crypto_vs_BT4b"] = {"cagr_here_2014_10": v14["cagr"], "max_dd_here": v14["max_dd"],
                                        "cagr_BT4b": float(bt4b["cagr"]), "max_dd_BT4b": float(bt4b["max_dd"])}
    res["validation"] = val
    log.say("validation", json.dumps(val, default=float))

    # ---- sensitivities (report only)
    sens = []
    for tag, kw in SENS_BTC.items():
        sx = build(d, mk0, r_spot, **kw)
        for nm in BTC_ROWS:
            rx = simulate(SPEC[nm], sx, sp_st, sp_en)
            for b, lab in enumerate(labels):
                if lab in ("full", "2014_2026"):
                    m = path_row(rx, b, s, bench, qqq)
                    sens.append({"variant": tag, "portfolio": nm, "period": lab, "cagr": m["cagr"], "max_dd": m["max_dd"],
                                 "worst_1y": m["worst_1y"], "trade_per_year": m["trade_per_year"],
                                 "liquidations": m["liquidations"]})
    st = build(d, mk0, r_spot, maint_qqq=MAINT_STRESS)
    liq_rows = []
    worst = rl.month_end_to_low(mk0.R[:, 0], mk0.RL[:, 0], s)
    for nm in MARGIN_ROWS:
        spec = SPEC[nm]
        rx = simulate(spec, st, starts, ends)
        wx = rl.window_outcomes(sub(rx), s)
        for b, lab in enumerate(labels):
            m = path_row(rx, b, s, bench, qqq)
            sens.append({"variant": "maint_40pct", "portfolio": nm, "period": lab, "cagr": m["cagr"],
                         "max_dd": m["max_dd"], "worst_1y": m["worst_1y"], "trade_per_year": m["trade_per_year"],
                         "liquidations": m["liquidations"]})
        base = [x for x in rows if x["portfolio"] == nm]
        wbase = rl.window_outcomes(sub(sims[nm]), s)
        liq_rows.append({"portfolio": nm, "L_qqq": spec.L, "btc": spec.btc,
                         "threshold_maint_25": rl.margin_threshold(spec.L, 0.25, spec.btc, BTC_MAINT),
                         "threshold_maint_40": rl.margin_threshold(spec.L, MAINT_STRESS, spec.btc, BTC_MAINT),
                         "worst_month_end_to_low": float(worst.min()), "worst_month": str(worst.idxmin()),
                         **{f"liquidations_{x['period']}_maint25": x["liquidations"] for x in base},
                         **{f"liquidations_{lab}_maint40": int(rx["liq"][b]) for b, lab in enumerate(labels)},
                         "p_liq_10y_maint25": float((wbase["liquidations"] > 0).mean()),
                         "p_liq_10y_maint40": float((wx["liquidations"] > 0).mean())})
    res["month_end_to_low_worst5"] = {k: float(v) for k, v in worst.nsmallest(5).items()}
    log.say("worst month-end -> intramonth low (QQQ):", res["month_end_to_low_worst5"])

    # ---- bootstrap
    no_btc = [sp.name for sp in PORTFOLIOS if sp.btc == 0]
    boot = []
    for block in (BOOT_BLOCK, BOOT_BLOCK_SENS):
        full = block == BOOT_BLOCK
        boot += bootstrap_rows(monthly_full, no_btc, "A_1999_04_2026_09", block, HORIZONS["A"], full)
        boot += bootstrap_rows(monthly_btc, [sp.name for sp in PORTFOLIOS], "B_2014_10_2026_09", block,
                               HORIZONS["B"], full)
    log.say("bootstrap rows", len(boot))

    # ---- growth-optimal leverage and drag
    last = len(s) - 1
    segs = {"full": (lm.session_index(s, ENTRY), last), "1999_2009": (lm.session_index(s, ENTRY), lm.session_index(s, SPLIT)),
            "2010_2026": (lm.session_index(s, SPLIT), last), "2014_2026": (lm.session_index(s, BTC_FIRST_CLOSE), last)}
    kel = {}
    for k, (a, b) in segs.items():
        kel[k] = {"growth": growth_curve(su.mk, a, b)}
        for blk in KELLY_BLOCKS:
            kel[k][f"block_{blk}"] = lm.kelly(su.mk, a, b, KELLY_SEED, n_boot=KELLY_DRAWS, block=blk,
                                              circular=KELLY_CIRCULAR)
            kel[k][f"block_{blk}_first_run_noncircular"] = lm.kelly(su.mk, a, b, KELLY_SEED, n_boot=KELLY_DRAWS,
                                                                    block=blk)["bootstrap"]
        log.say(f"kelly {k}: L* {kel[k]['block_252']['empirical_Lstar']:.2f} formula "
                f"{kel[k]['block_252']['formula_Lstar']:.2f} boot252 p5/p50/p95 "
                f"{kel[k]['block_252']['bootstrap']['p5']:.2f}/{kel[k]['block_252']['bootstrap']['median']:.2f}/"
                f"{kel[k]['block_252']['bootstrap']['p95']:.2f} boot504 "
                f"{kel[k]['block_504']['bootstrap']['p5']:.2f}/{kel[k]['block_504']['bootstrap']['median']:.2f}/"
                f"{kel[k]['block_504']['bootstrap']['p95']:.2f} share<=1 (252/504) "
                f"{kel[k]['block_252']['bootstrap']['share_at_or_below_1']:.0%}/"
                f"{kel[k]['block_504']['bootstrap']['share_at_or_below_1']:.0%} (circular={KELLY_CIRCULAR}; first run "
                f"non-circular {kel[k]['block_252_first_run_noncircular']['share_at_or_below_1']:.0%}/"
                f"{kel[k]['block_504_first_run_noncircular']['share_at_or_below_1']:.0%})")
    res["kelly"] = kel

    # ---- write
    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(roll_rows).to_csv(OUT / "rolling_10y.csv", index=False, float_format="%.6f")
    pd.DataFrame(boot).to_csv(OUT / "bootstrap.csv", index=False, float_format="%.6f")
    pd.DataFrame(contrib_rows).to_csv(OUT / "contributions.csv", index=False, float_format="%.6f")
    pd.DataFrame(drag_rows).to_csv(OUT / "drag.csv", index=False, float_format="%.6f")
    pd.DataFrame(liq_rows).to_csv(OUT / "liquidation.csv", index=False, float_format="%.6f")
    pd.DataFrame(sens).to_csv(OUT / "sensitivities.csv", index=False, float_format="%.6f")
    pd.DataFrame(concentration_rows()).to_csv(OUT / "concentration.csv", index=False, float_format="%.6f")
    pd.DataFrame(monthly_full).to_csv(OUT / "monthly_returns_full.csv", float_format="%.6f")
    pd.DataFrame(monthly_btc).to_csv(OUT / "monthly_returns_2014_10.csv", float_format="%.6f")
    write_json(OUT / "results.json", res, default=float)
    log.write(OUT / "run_log.txt")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--register-only", action="store_true", help="record the SHA-256 of ledger section 0 and stop")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
