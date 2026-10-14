"""Combining the near-miss signals (pre-registered in docs/research_ledger_signal_combo.md, section 0).

Seven components that each came close to passing, all on their frozen rules and engines: a local sales/price top-10
proxy (SP), the S-MISP / S-PROF / S-ML short-loser MN overlays (MISP, PROF, ML), the QQQ RSI2-dip T configuration
SEL-P (RSI2), S3-Yb on 18 stocks (S3Yb) and the B100 S-MISP X10 index exclusion (IDX). Step A (diagnostic): each
component's monthly excess over its own matched base, correlations and t on the 2016-01..2026-08 overlap. Step B
(judged): three fixed-weight combinations (C1 equal risk at a 4% tracking-error target, weights from the other fold;
C2 equal notional 10%; C3 the long-only cash-account subset IDX + RSI2 + S3Yb) simulated as one $10k IBKR account
(every part re-run on its engine at its real dollar size; borrow fees, margin interest, Reg T maintenance check) and
judged against ONEQ on two folds with a family-wide Bonferroni hurdle, plus the signal-off twin. Component runners,
weights, book and fold statistics: quant.strategies.signal_combo.

Usage:
  PYTHONPATH=. .venv/bin/python -m quant study signal_combo --check
  PYTHONPATH=. .venv/bin/python -m quant study signal_combo --register
  PYTHONPATH=. .venv/bin/python -m quant study signal_combo --run
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")      # the panel components are defined on frozen data v2 only

import argparse  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import time  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant.backtest.costs import REBALANCE_BAND  # noqa: E402
from quant.evaluation.criteria import bonferroni_t_student, deflated_sharpe_n  # noqa: E402
from quant.evaluation.metrics import yearly  # noqa: E402
from quant.evaluation.stats import t_of_mean, t_sf  # noqa: E402
from quant.paths import CACHE_ROOT, ROOT  # noqa: E402
from quant.reporting import Preregistration, RunLog, json_default  # noqa: E402
from quant.strategies import megacap as mc  # noqa: E402
from quant.strategies import registry  # noqa: E402
from quant.strategies import selective_t as st  # noqa: E402
from quant.strategies import short_overlay as so  # noqa: E402
from quant.strategies import signal_combo as sc  # noqa: E402
from quant.strategies import t_grid as tg  # noqa: E402
from quant.strategies.signal_combo import Fold, Part  # noqa: E402

# ======================================================================== frozen configuration

OUT = ROOT / "output/research_only/signal_combo"
LEDGER = ROOT / "docs/research_ledger_signal_combo.md"
FROZEN = OUT / "frozen_prereg.json"
PREREG = Preregistration(LEDGER, FROZEN, OUT)
prereg_block, prereg_hash, register = PREREG.block, PREREG.hash, PREREG.register
# inputs (read only): the ML panel cache, its GBRT predictions, the EFFR, the Yahoo charts of calendar / selective_t,
# the v2 panel prices and the megacap v2 top-20 list (t_grid.load_all needs them for its T10 base)
PRED = ROOT / "output/research_only/ml_cross_section/predictions.csv.gz"
ML_CACHE = CACHE_ROOT / "ml_cross_section_v2"
EFFR_CSV = CACHE_ROOT / "leverage_methods/raw/EFFR_nyfed.csv"
T_SOURCES = {"QQQ": CACHE_ROOT / "calendar/raw/chart_QQQ.json", "ONEQ": CACHE_ROOT / "calendar/raw/chart_ONEQ.json"}
T_RAW = CACHE_ROOT / "selective_t/raw"
T_PANEL = CACHE_ROOT / "reversal_2012_2026_v2/prices"
T_TOP20 = ROOT / "output/research_only/megacap_v2/top20_by_month.csv"

ACCOUNT = 10_000.0
FOLDS = (Fold("H1", "2015-12-31", "2016-01-04", "2020-12-31"), Fold("H2", "2020-12-31", "2021-01-04", "2026-08-31"))
COMPONENTS = ("SP", "MISP", "PROF", "ML", "RSI2", "S3Yb", "IDX")
MN_SIGNAL = {"MISP": "S-MISP", "PROF": "S-PROF", "ML": "S-ML"}
MISP = registry.get("S-MISP N10 k20 MN").frozen                # n 10, k 0.20, MN
SELP, SELA = registry.get("SEL-P").frozen, registry.get("SEL-A").frozen
S3 = registry.get("S3-Yb").frozen
SP_COL, SP_N = "sp", 10                                        # fundamentals sp factor, top 10
IDX_SIGNAL, IDX_VARIANT, IDX_TOPN, IDX_LAMBDA = "S-MISP", "X10", 100, 0.5
BAND = REBALANCE_BAND                                          # 0.25
UNIT = {"SP": 1.0, "IDX": 1.0, "MISP": MISP["k"], "PROF": MISP["k"], "ML": MISP["k"], "RSI2": 0.25, "S3Yb": 0.25}
TE_TARGET = 0.04
CAPS = {"SP": 0.5, "IDX": 0.5, "MISP": 0.5, "PROF": 0.5, "ML": 0.5, "RSI2": 0.25, "S3Yb": 0.25}
REGT_CAP = 0.95
C2_H = 0.10
C3_PARTS, C3_H = ("IDX", "RSI2", "S3Yb"), 0.10
COMBOS = ("C1", "C2", "C3")
MARGIN_SPREAD = so.MARGIN_SPREAD                               # IBKR Pro tier 1: BM (EFFR) + 1.5%
FAMILY_TRIALS = {"t_grid": 184_320, "fundamentals": 100, "qc_factors": 40, "short_overlay": 32,
                 "index_exclusion": 24, "selective_t": 18, "ml_cross_section": 8, "signal_combo": len(COMBOS)}
N_FAMILY = sum(FAMILY_TRIALS.values())                         # 184,545
ALPHA = 0.05
DSR_VAR_MULTS = (0.5, 1.0, 2.0)


def load():
    pi = sc.load_panel(ML_CACHE, PRED, EFFR_CSV)
    tdata = tg.load_all(T_SOURCES, T_RAW, T_PANEL, T_TOP20)
    stocks = {sym: st.load_stock(sym, T_RAW) for sym in S3["universe"]}
    return pi, tdata, stocks


def effr_series(pi) -> pd.Series:
    return pd.Series(pi.mk.effr, index=pi.mk.sessions)


# ======================================================================== component runs

class Runs:
    """Every engine run of one fold, cached by (component, dollar size)."""

    def __init__(self, pi, tdata, stocks, fold: Fold):
        self.pi, self.tdata, self.stocks, self.fold = pi, tdata, stocks, fold
        self.index = sc.fold_index(pi.data.sessions, fold)
        self.first_ret = sc.first_return_day(pi.data.sessions, fold)
        self.cache: dict = {}
        self.tgt = {"SP": sc.sp_targets(pi, SP_COL, SP_N),
                    "X10": sc.idx_targets(pi, IDX_SIGNAL, IDX_VARIANT, IDX_TOPN, IDX_LAMBDA),
                    "B100": sc.idx_targets(pi, None, "BASE", IDX_TOPN, IDX_LAMBDA)}

    def get(self, key, size):
        k = (key, round(size, 6))
        if k not in self.cache:
            self.cache[k] = self._run(key, size)
        return self.cache[k]

    def _run(self, key, size):
        pi, f = self.pi, self.fold
        if key in ("SP", "X10", "B100"):
            return sc.stock_sleeve(pi, self.tgt[key], f, size, BAND)
        if key in MN_SIGNAL:                       # size = k (short notional / account), account = $10k
            return sc.mn_overlay(pi, MN_SIGNAL[key], MISP["n"], size, ACCOUNT, f)
        if key in ("RSI2", "SELA"):                # size = account equity
            cfg = SELP if key == "RSI2" else SELA
            return sc.rsi2_run(self.tdata, cfg["config_id"], cfg["reserve_code"], self.first_ret, f.end, size)
        if key == "S3Yb":                          # size = equity per stock account
            return sc.s3_run(self.stocks, S3["config"], self.first_ret, f.end, size)
        raise KeyError(key)

    def oneq_hold(self, account):
        return mc.buy_hold(self.pi.oneq_lvl, self.pi.oneq_px, self.index, mc.ONEQ_HS, account)

    def qqq_hold(self, account):
        return mc.buy_hold(self.pi.qqq_lvl, self.pi.data.qqq_close, self.index, mc.QQQ_HS, account)


def step_a(runs: Runs) -> dict:
    """Monthly excess of every component over its matched base at its frozen size (section 0.4)."""
    m = sc.month_returns
    out = {}
    sp = runs.get("SP", ACCOUNT)["nav"]
    out["SP"] = m(sp) - m(runs.oneq_hold(ACCOUNT))
    for c in MN_SIGNAL:
        r = runs.get(c, MISP["k"])
        out[c] = m(r["nav"]["nav"]) - m(r["qqq_hold"])
    for key, name in (("RSI2", "RSI2"), ("SELA", "SELA")):
        r = runs.get(key, ACCOUNT)
        out[name] = m(r["v_cfg"]) - m(sc.level_of(r["r_hmatch"], r["v_cfg"].index[0]))
    r = runs.get("S3Yb", ACCOUNT)
    first = runs.index[0]
    out["S3Yb"] = m(sc.level_of(r["r_basket"], first)) - m(sc.level_of(r["r_hmatch"], first))
    out["IDX"] = m(runs.get("X10", ACCOUNT)["nav"]) - m(runs.get("B100", ACCOUNT)["nav"])
    return out


def unit_stats(df: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    """Annualised volatility and correlation of the unit-notional monthly streams ``df`` (columns = components)."""
    u = df[list(COMPONENTS)].div(pd.Series(UNIT))[list(COMPONENTS)]
    return u.std(ddof=1) * math.sqrt(12), u.corr()


# ======================================================================== the book of one combination and fold

def weights_for(combo: str, other_fold_units: pd.DataFrame | None) -> tuple[dict, dict]:
    if combo == "C1":
        sig, rho = unit_stats(other_fold_units)
        return sc.equal_risk_weights(sig, rho, TE_TARGET, CAPS, REGT_CAP)
    if combo == "C2":
        h = {c: C2_H for c in COMPONENTS}
        return h, {"regt_initial": sc.regt_initial(h)}
    h = {c: C3_H for c in C3_PARTS}
    return h, {"regt_initial": None}


def book_parts(runs: Runs, h: dict, cash_account: bool) -> tuple[list, float, dict]:
    """The parts of section 0.6 at their dollar sizes. Returns (parts, idle cash, per-part info)."""
    idx, E = runs.index, ACCOUNT
    zero = pd.Series(0.0, index=idx)
    effr = effr_series(runs.pi)
    parts, info = [], {}
    reserve = sum(h.get(p, 0.0) for p in sc.T_PARTS) * E if cash_account else 0.0
    d_oneq = E * (1 - h.get("SP", 0.0) - h.get("IDX", 0.0)) - reserve
    oneq = runs.oneq_hold(d_oneq)
    parts.append(Part("ONEQ", oneq, oneq, oneq, zero))
    years = (idx[-1] - idx[0]).days / 365.25
    for name, key, twin_key in (("SP", "SP", None), ("IDX", "X10", "B100")):
        hh = h.get(name, 0.0)
        if hh <= 0:
            continue
        sim = runs.get(key, hh * E)
        v = sim["nav"]
        tw = runs.oneq_hold(hh * E) if twin_key is None else runs.get(twin_key, hh * E)["nav"]
        parts.append(Part(name, v, tw, v, zero))
        info[name] = {"dollars": hh * E, "trade_cost": float(sim["cost"].sum()), "orders": int(sim["orders"].sum()),
                      "avg_names": float(sim["names"].mean()),
                      "pnl_vs_twin_over_start_equity_pp_yr": float((v.iloc[-1] - tw.reindex(idx).iloc[-1]) / E / years * 100),
                      "pnl_vs_oneq_over_start_equity_pp_yr":
                          float((v.iloc[-1] - runs.oneq_hold(hh * E).iloc[-1]) / E / years * 100)}
    for name in sc.MN_PARTS:
        hh = h.get(name, 0.0)
        if hh <= 0:
            continue
        r = runs.get(name, hh)
        nav, q = r["nav"], r["qqq_hold"]
        ov = nav["nav"] - q
        parts.append(Part(name, ov, zero, (nav["qqq"] - q).clip(lower=0.0), nav["short"]))
        acc = r["res"]["acc"]
        info[name] = {"short_notional": hh * E, "pnl_over_start_equity_pp_yr": float(ov.iloc[-1] / E / years * 100),
                      "trade_cost": acc["commission"] + acc["fees"] + acc["spread"], "borrow": acc["borrow"],
                      "margin_interest": acc["margin_int"], "orders": acc["orders"],
                      "zero_share_skips": acc["zero_share_skips"], "liquidations": acc["liquidations"],
                      "avg_n_short": float(nav["n_short"].mean())}
    for name in sc.T_PARTS:
        hh = h.get(name, 0.0)
        if hh <= 0:
            continue
        if name == "RSI2":
            r = runs.get("RSI2", 4 * hh * E)
            extra = r["x_cfg"] * r["v_cfg"].reindex(r["x_cfg"].index) - r["x_hb"] * r["v_hb"].reindex(r["x_hb"].index)
            extra = pd.concat([pd.Series([0.0], index=[r["v_cfg"].index[0]]), extra])
            n_tr = r["trades"]
        else:
            r = runs.get("S3Yb", 4 * hh * E / len(runs.stocks))
            extra = r["extra"]
            n_tr = r["trips"]
        extra = extra.reindex(idx).fillna(0.0)
        ov = (r["v_cfg"] - r["v_hb"]).reindex(idx)
        fin = zero if cash_account else sc.financing(extra, effr, MARGIN_SPREAD)
        val = ov - fin
        parts.append(Part(name, val, zero, zero if cash_account else extra.clip(lower=0.0), zero))
        info[name] = {"account_equivalent": 4 * hh * E, "reserve_or_max_add": hh * E, "trades": n_tr,
                      "financing": float(fin.iloc[-1]), "pnl_over_start_equity_pp_yr": float(val.iloc[-1] / E / years * 100),
                      "avg_extra_long": float(extra.mean()),
                      **({"stocks_with_zero_q_at_start": r["stocks_with_zero_q_at_start"]} if name == "S3Yb" else {})}
    return parts, reserve, info


# ======================================================================== commands

def check(args):
    """Coverage and dollar sizes only (no returns)."""
    OUT.mkdir(parents=True, exist_ok=True)
    pi, tdata, stocks = load()
    rows = []
    for f in FOLDS:
        runs = Runs(pi, tdata, stocks, f)
        first = runs.first_ret
        sigs = pi.scores[(pi.scores["s"] >= pd.Timestamp(f.signal)) & (pi.scores["s"] < pd.Timestamp(f.end))]
        cov = {c: float(sigs[MN_SIGNAL[c]].notna().mean()) for c in MN_SIGNAL}
        n_sp = sum(1 for s in runs.tgt["SP"] if pd.Timestamp(f.signal) <= s < pd.Timestamp(f.end))
        n_x = sum(1 for s in runs.tgt["X10"] if pd.Timestamp(f.signal) <= s < pd.Timestamp(f.end))
        for combo, h in (("C2", {c: C2_H for c in COMPONENTS}), ("C3", {c: C3_H for c in C3_PARTS})):
            each = 4 * h.get("S3Yb", 0.0) * ACCOUNT / len(stocks)
            zq = 0
            for sym, m in stocks.items():
                s = int(m.s.sessions.searchsorted(first))
                p0 = m.s.close[s - 1]
                b = math.floor(st.BASE_FRAC * (each - st.BUFFER) / (p0 * (1 + st.hs_of(sym, p0))))
                zq += int(b // 3 == 0)
            q0 = tdata["insts"]["QQQ"].prevc[int(tdata["master"].searchsorted(first))]
            k_rsi = 4 * h.get("RSI2", 0.0) * ACCOUNT
            b_rsi = math.floor(0.75 * (k_rsi - tg.BUFFER) / (q0 * 1.0001))
            q_rsi = min(b_rsi, math.floor((0.25 * k_rsi - tg.BUFFER) / (tg.CAP_MULT * q0)))
            rows.append({"fold": f.name, "combo": combo, "first_return_day": str(first.date()),
                         "sp_signals": n_sp, "x10_signals": n_x, **{f"cov_{c}": v for c, v in cov.items()},
                         "mn_short_per_name_usd": h.get("MISP", 0.0) * ACCOUNT / MISP["n"],
                         "sp_per_name_usd": h.get("SP", 0.0) * ACCOUNT / SP_N,
                         "idx_sleeve_usd": h.get("IDX", 0.0) * ACCOUNT,
                         "rsi2_account_equivalent": k_rsi, "rsi2_base_shares": b_rsi, "rsi2_max_add_shares": q_rsi,
                         "s3_equity_per_stock": each, "s3_stocks_with_zero_add_shares": zq,
                         "regt_initial_worst": sc.regt_initial(h) if combo == "C2" else None})
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "check_sizes.csv", index=False)
    pd.set_option("display.width", 250)
    print(df.T.to_string())
    print("family trials", N_FAMILY, FAMILY_TRIALS)


def run(args):
    if not FROZEN.exists():
        raise SystemExit("not registered")
    if json.loads(FROZEN.read_text())["sha256"] != prereg_hash():
        raise SystemExit("pre-registration text changed since --register")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    log = RunLog()
    say = log.say
    pi, tdata, stocks = load()
    say("DATE GUARD:", pi.data.guard["assertion"])
    runs = {f.name: Runs(pi, tdata, stocks, f) for f in FOLDS}

    # ---- Step A (diagnostic)
    streams = {}
    for f in FOLDS:
        a = step_a(runs[f.name])
        streams[f.name] = pd.DataFrame(a)
        say(f"step A {f.name}: {len(streams[f.name])} months ({time.time() - t0:.0f}s)")
    pooled = pd.concat([streams[f.name] for f in FOLDS])
    long_rows = []
    for fname, df in streams.items():
        for per, r in df.iterrows():
            long_rows.append({"fold": fname, "month": str(per), **r.to_dict()})
    pd.DataFrame(long_rows).to_csv(OUT / "step_a_streams.csv", index=False, float_format="%.8g")
    corr_rows = []
    for name, df in (("pooled", pooled), *streams.items()):
        c = df[list(COMPONENTS) + ["SELA"]].corr()
        for i in c.index:
            corr_rows.append({"window": name, "component": i, **c.loc[i].to_dict()})
    pd.DataFrame(corr_rows).to_csv(OUT / "step_a_corr.csv", index=False, float_format="%.4f")
    stat_rows = []
    for comp in list(COMPONENTS) + ["SELA"]:
        r = {"component": comp, "unit_notional": UNIT.get(comp, 0.25)}
        for name, df in (("pooled", pooled), *streams.items()):
            x = df[comp]
            r[f"{name}_mean_ann"] = float(x.mean() * 12)
            r[f"{name}_te_ann"] = float(x.std(ddof=1) * math.sqrt(12))
            r[f"{name}_t"] = t_of_mean(x)
            r[f"{name}_months"] = int(x.notna().sum())
        r["pooled_sr_monthly"] = float(pooled[comp].mean() / pooled[comp].std(ddof=1))
        stat_rows.append(r)
    stats_a = pd.DataFrame(stat_rows)
    stats_a.to_csv(OUT / "step_a_stats.csv", index=False, float_format="%.6g")
    pc = pooled[list(COMPONENTS)].corr()
    off = pc.where(~np.eye(len(pc), dtype=bool)).stack()
    say("STEP A pooled t:", {r["component"]: round(r["pooled_t"], 2) for r in stat_rows})
    say("STEP A mean off-diagonal corr:", round(float(off.mean()), 3), "max", off.idxmax(), round(float(off.max()), 3),
        "min", off.idxmin(), round(float(off.min()), 3))

    # ---- Step B
    other = {"H1": "H2", "H2": "H1"}
    summary, verdict_rows, part_rows, weight_rows, daily, sos_rows, year_rows = [], [], [], [], [], [], []
    ex_pool = {c: {"oneq": [], "twin": [], "sos": [], "qbase": []} for c in COMBOS}
    for combo in COMBOS:
        per_fold = {}
        for f in FOLDS:
            rn = runs[f.name]
            h, winfo = weights_for(combo, streams[other[f.name]] if combo == "C1" else None)
            weight_rows.append({"combo": combo, "fold": f.name, **{f"h_{c}": h.get(c, 0.0) for c in COMPONENTS},
                                **{k: (json.dumps(v, default=json_default) if isinstance(v, (dict, list)) else v)
                                   for k, v in winfo.items()}})
            cash_acct = combo == "C3"
            parts, reserve, pinfo = book_parts(rn, h, cash_acct)
            bk = sc.assemble(rn.index, parts, cash=reserve)
            oneq = rn.oneq_hold(ACCOUNT)
            m, ex, ext = sc.fold_stats(bk["book"], oneq, bk["twin"])
            m["breach_days"] = 0 if cash_acct else int(bk["breach"].sum())
            m["max_long_over_nav"] = float((bk["long"] / bk["book"]).max())
            m["max_short_over_nav"] = float((bk["short"] / bk["book"]).max())
            # diagnostics: the sum of the Step A streams; the QQQ-base book
            sos = sc.month_returns(oneq) + sum(h.get(c, 0.0) * streams[f.name][c] / UNIT[c] for c in COMPONENTS)
            sos_ex = sos - sc.month_returns(oneq)
            base_dollars = ACCOUNT * (1 - h.get("SP", 0.0) - h.get("IDX", 0.0)) - reserve
            qbook = bk["book"] - parts[0].value + rn.qqq_hold(base_dollars)
            qm, qex, _ = sc.fold_stats(qbook, oneq, bk["twin"])
            m["sos_ex_oneq_ann"], m["sos_t"] = float(sos_ex.mean() * 12), t_of_mean(sos_ex)
            m["qqq_base_cagr"], m["qqq_base_t_ex_oneq"] = qm["cagr"], qm["t_ex_oneq"]
            per_fold[f.name] = m
            ex_pool[combo]["oneq"].append(ex)
            ex_pool[combo]["twin"].append(ext)
            ex_pool[combo]["sos"].append(sos_ex)
            ex_pool[combo]["qbase"].append(qex)
            summary.append({"combo": combo, "fold": f.name, **m})
            years = (rn.index[-1] - rn.index[0]).days / 365.25
            prev = bk["book"].shift(1)
            for p in parts[1:]:
                d = (p.value - p.twin).reindex(rn.index).diff()
                pinfo[p.name]["contrib_vs_twin_pct_of_book_yr"] = float((d / prev).sum() / years * 100)
                if p.name == "IDX":
                    o = rn.oneq_hold(h["IDX"] * ACCOUNT)
                    d2 = (p.value - o).reindex(rn.index).diff()
                    pinfo[p.name]["contrib_vs_oneq_pct_of_book_yr"] = float((d2 / prev).sum() / years * 100)
            for pn, pinf in pinfo.items():
                part_rows.append({"combo": combo, "fold": f.name, "part": pn, **pinf})
            dd = pd.DataFrame({"book": bk["book"], "twin": bk["twin"], "oneq": oneq, "qqq_base_book": qbook,
                               "long": bk["long"], "short": bk["short"]})
            dd["combo"], dd["fold"] = combo, f.name
            daily.append(dd.reset_index().rename(columns={"index": "date"}))
            for y, v in yearly(bk["book"].pct_change().dropna()).items():
                year_rows.append({"combo": combo, "fold": f.name, "year": y, "book": v,
                                  "twin": yearly(bk["twin"].pct_change().dropna()).get(y),
                                  "oneq": yearly(oneq.pct_change().dropna()).get(y),
                                  "qqq": yearly(rn.qqq_hold(ACCOUNT).pct_change().dropna()).get(y)})
            sos_rows.append({"combo": combo, "fold": f.name, "sos_ex_oneq_ann": m["sos_ex_oneq_ann"],
                             "sos_t": m["sos_t"], "book_ex_oneq_ann": m["ex_oneq_ann"], "book_t": m["t_ex_oneq"],
                             "book_ex_twin_ann": m["ex_twin_ann"]})
            say(f"{combo} {f.name}: CAGR {m['cagr']:+.2%} ONEQ {m['oneq_cagr']:+.2%} t {m['t_ex_oneq']:+.2f} "
                f"vs twin {m['ex_twin_ann']:+.2%} (t {m['t_ex_twin']:+.2f}) DD {m['max_dd']:.1%} / {m['oneq_max_dd']:.1%} "
                f"A {m['A']} B {m['B']} breaches {m['breach_days']} ({time.time() - t0:.0f}s)")
        exo = pd.concat(ex_pool[combo]["oneq"])
        ext = pd.concat(ex_pool[combo]["twin"])
        df_ = len(exo) - 1
        h_fam = bonferroni_t_student(df_, N_FAMILY, ALPHA)
        h_loc = bonferroni_t_student(df_, len(COMBOS), ALPHA)
        t_o, t_t = t_of_mean(exo), t_of_mean(ext)
        h1, h2 = per_fold["H1"], per_fold["H2"]
        folds_ab = bool(h1["pass"] and h2["pass"])
        twin_pos = bool(h1["ex_twin_ann"] > 0 and h2["ex_twin_ann"] > 0)
        feasible = combo == "C3" or (h1["breach_days"] == 0 and h2["breach_days"] == 0)
        mo, mt = sc.moments(exo), sc.moments(ext)
        var_sr = float(stats_a.set_index("component").loc[list(COMPONENTS), "pooled_sr_monthly"].var(ddof=1))
        dsr = {}
        for lab, mom in (("vs_oneq", mo), ("vs_twin", mt)):
            for n_tr, tag in ((N_FAMILY, "family"), (len(COMBOS), "local")):
                for vm in DSR_VAR_MULTS:
                    d = deflated_sharpe_n(mom["sr"], mom["n"], mom["skew"], mom["kurt"], n_tr, var_sr * vm)
                    dsr[f"dsr_{lab}_{tag}_var{vm:g}"] = d["dsr"]
                    if vm == 1.0:
                        dsr[f"sr0_{lab}_{tag}"] = d["sr0"]
        verdict_rows.append({
            "combo": combo, "H1_A": h1["A"], "H1_B": h1["B"], "H2_A": h2["A"], "H2_B": h2["B"],
            "folds_A_or_B": folds_ab, "t_pooled_vs_oneq": t_o, "p_one_sided_vs_oneq": t_sf(t_o, df_),
            "t_pooled_vs_twin": t_t, "twin_excess_positive_both_folds": twin_pos,
            "hurdle_family": h_fam, "hurdle_local": h_loc, "feasible_no_margin_breach": feasible,
            "pass": bool(folds_ab and t_o >= h_fam and twin_pos and t_t >= h_fam and feasible),
            "local_nominal_pass": bool(folds_ab and t_o >= h_loc and twin_pos and t_t >= h_loc and feasible),
            "sum_of_streams_t_pooled": t_of_mean(pd.concat(ex_pool[combo]["sos"])),
            "qqq_base_t_pooled_vs_oneq": t_of_mean(pd.concat(ex_pool[combo]["qbase"])),
            "sr_monthly_vs_oneq": mo["sr"], "skew_vs_oneq": mo["skew"], "kurt_vs_oneq": mo["kurt"],
            "sr_monthly_vs_twin": mt["sr"], "var_trial_sr": var_sr, **dsr})
        say(f"VERDICT {combo}: pooled t vs ONEQ {t_o:+.2f}, vs twin {t_t:+.2f}; hurdles local {h_loc:.2f} / "
            f"family {h_fam:.2f}; pass {verdict_rows[-1]['pass']} local-nominal {verdict_rows[-1]['local_nominal_pass']}")

    ver = pd.DataFrame(verdict_rows)
    ver.to_csv(OUT / "verdict.csv", index=False)
    pd.DataFrame(summary).to_csv(OUT / "summary.csv", index=False)
    pd.DataFrame(part_rows).to_csv(OUT / "parts.csv", index=False)
    pd.DataFrame(weight_rows).to_csv(OUT / "weights.csv", index=False)
    pd.DataFrame(sos_rows).to_csv(OUT / "sum_of_streams.csv", index=False)
    pd.DataFrame(year_rows).to_csv(OUT / "by_year.csv", index=False)
    pd.concat(daily, ignore_index=True).to_csv(OUT / "book_daily.csv.gz", index=False, float_format="%.4f")
    res = {"prereg_sha256": prereg_hash(), "n_trials_local": len(COMBOS), "n_trials_family": N_FAMILY,
           "family_trials": FAMILY_TRIALS, "verdict": "PASS" if ver["pass"].any() else "FAIL",
           "n_pass": int(ver["pass"].sum()), "n_local_nominal_pass": int(ver["local_nominal_pass"].sum()),
           "step_a_mean_offdiag_corr": float(off.mean()), "guard": pi.data.guard["assertion"],
           "runtime_s": round(time.time() - t0, 1)}
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=json_default) + "\n")
    say(json.dumps(res, default=json_default))
    log.write(OUT / "run_log.txt")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--run", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        check(a)
    elif a.register:
        register()
    else:
        run(a)


if __name__ == "__main__":
    main()
