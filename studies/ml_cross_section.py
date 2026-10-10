"""Machine-learning cross-sectional return prediction, Gu-Kelly-Xiu (2020) style
(pre-registered in docs/research_ledger_ml_cross_section.md, section 0).

Monthly features at each month end (13 price features, the 40 point-in-time fundamental factors of
quant.strategies.fundamentals, FF12 industry dummies), cross-sectionally rank-normalised in the U300 universe; target = the
next-month total-return rank. Three models (elastic net, histogram gradient-boosted trees, a small MLP ensemble),
implemented in numpy (scikit-learn / LightGBM are not installed in .venv). Expanding-window walk-forward: train on
2012..Y-1 (last 12 months = validation for tuning), predict year Y = 2016..2026, retrain yearly. Long-only Top10 /
Top20 equal weight with a top-2N buffer, $10k IBKR Tiered real-trading simulation (quant.strategies.megacap.simulate),
judged against ONEQ total return. Frozen data version 2 only. Panel: quant.strategies.ml_features; models,
walk-forward and diagnostics: quant.strategies.ml_models.

Usage:
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_ml_cross_section.py --check
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_ml_cross_section.py --smoke
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_ml_cross_section.py --register
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_ml_cross_section.py --run
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")      # the study is defined on frozen data v2 only

import argparse  # noqa: E402
import json  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant.evaluation.criteria import bonferroni_t_student  # noqa: E402
from quant.evaluation.metrics import yearly  # noqa: E402
from quant.evaluation.periods import nav_window_metrics  # noqa: E402
from quant.evaluation.stats import t_sf  # noqa: E402
from quant.paths import CACHE_ROOT, ROOT  # noqa: E402
from quant.reporting import Preregistration, json_rounded  # noqa: E402
from quant.strategies import megacap as mc  # noqa: E402
from quant.strategies import ml_models as mm  # noqa: E402
from quant.strategies.ml_features import CONT_FEATURES, FUND_FEATURES, PRICE_FEATURES, build_panel  # noqa: E402

# ======================================================================== frozen configuration

OUT = ROOT / "output/research_only/ml_cross_section"
LEDGER = ROOT / "docs/research_ledger_ml_cross_section.md"
FROZEN = OUT / "frozen_prereg.json"
PREREG = Preregistration(LEDGER, FROZEN, OUT)
prereg_block, prereg_hash, register = PREREG.block, PREREG.hash, PREREG.register
ML_CACHE = CACHE_ROOT / "ml_cross_section_v2"          # the feature panel and its derived data

TEST_YEARS = tuple(range(2016, 2027))
VAL_MONTHS = 12
SEED = 20261009
PERIODS = {"P1 2016-2020": ("2016-01-01", "2020-12-31"),
           "P2 2021-2026-08": ("2021-01-01", "2026-08-31"),
           "Full 2016-2026-08": ("2016-01-01", "2026-08-31")}
JUDGED = ("P1 2016-2020", "P2 2021-2026-08")
FULL = "Full 2016-2026-08"
N_TRIALS = 8
ALPHA_ONE_SIDED = 0.05
BUFFER_MULT = 2
TOP100 = 100

FF12_IN = ("NoDur", "Durbl", "Manuf", "Enrgy", "Chems", "BusEq", "Telcm", "Utils", "Shops", "Hlth", "Other")
DUMMIES = tuple(f"ff12_{k}" for k in FF12_IN)
FEATURES = CONT_FEATURES + DUMMIES
assert len(PRICE_FEATURES) == 13 and len(FUND_FEATURES) == 40 and len(FEATURES) == 64
FEATURE_GROUPS = ["price"] * 13 + ["fundamental"] * 40 + ["industry"] * 11

MODELS = ("ENET", "GBRT", "MLP")
ENET_ALPHAS = (1e-2, 1e-3, 1e-4, 1e-5)          # strongest first: ties keep the stronger penalty
ENET_RHOS = (0.9, 0.5, 0.1)
GB = {"bins": 32, "depth": 3, "lr": 0.05, "min_leaf": 100, "lam": 1.0, "subsample": 0.5, "max_trees": 500,
      "check_every": 10}
MLP_CFG = {"hidden": (32, 16), "lr": 1e-3, "batch": 512, "l2": 1e-4, "seeds": 5, "max_epochs": 30}
CFG = mm.ModelConfig(seed=SEED, val_months=VAL_MONTHS, enet_alphas=ENET_ALPHAS, enet_rhos=ENET_RHOS, gb=GB,
                     mlp=MLP_CFG, cont_features=CONT_FEATURES, ff12_in=FF12_IN, dummies=DUMMIES, models=MODELS)
assert CFG.features == FEATURES


def criteria(per: dict, n_trials: int = N_TRIALS) -> dict:
    h = [per[p] for p in JUDGED]
    full = per[FULL]
    p = t_sf(full["t_monthly_excess_vs_oneq"], full["months"] - 1)
    a = all(x["cagr"] > x["oneq_cagr"] for x in h) and p < ALPHA_ONE_SIDED / n_trials
    b = all(x["dd_shallower_than_oneq_pp"] >= 10.0 and x["cagr"] >= x["oneq_cagr"] - 0.03 for x in h)
    return {"p_one_sided": p, "bonferroni_t": bonferroni_t_student(full["months"] - 1, n_trials, ALPHA_ONE_SIDED),
            "A_cagr_above_oneq_both_and_bonferroni_t": bool(a),
            "B_dd_10pp_shallower_and_cagr_within_3pp_both": bool(b), "pass": bool(a or b)}


# ======================================================================== portfolio configurations

def configs(pred: pd.DataFrame, panel: pd.DataFrame) -> list:
    """(name, kind, targets, ONEQ-leg flag). kind = 'trial' (8, section 0.7) or 'reference'."""
    out = []
    for mdl in MODELS:
        sc = pred[["s", "security_id", "dv50_rank", "mcap"]].assign(score=pred[mdl])
        for n in (10, 20):
            out.append((f"{mdl}_Top{n}", "trial", mm.buffered_targets(sc, n, BUFFER_MULT), False))
    g = pred[["s", "security_id", "dv50_rank", "mcap"]].assign(score=pred["GBRT"])
    out.append(("V100_GBRT_Top10", "trial", mm.buffered_targets(mm.top_by_mcap(g, TOP100), 10, BUFFER_MULT), False))
    out.append(("BLEND_50ONEQ_GBRT_Top10", "trial",
                mm.buffered_targets(g, 10, BUFFER_MULT, weight=0.05, extra=[("QQQ", 0.5, np.nan, np.nan, "oneq")]),
                True))
    test_sigs = set(pred["s"].unique())
    p = panel[panel["s"].isin(test_sigs)]
    for comp in ("C_profitability", "C_ALL"):
        sc = p[["s", "security_id", "dv50_rank", "mcap"]].assign(score=p[comp])
        for n in (10, 20):
            out.append((f"REF_{comp}_Top{n}", "reference", mm.buffered_targets(sc, n, BUFFER_MULT), False))
    out.append(("REF_EW_U300", "reference", mm.equal_weight_targets(p), False))
    out.append(("REF_EW_Top100mcap", "reference", mm.equal_weight_targets(mm.top_by_mcap(p, TOP100)), False))
    return out


def simulate_all(cfgs: list, data, oneq_lvl, oneq_px, log=print) -> tuple[list, list, dict, dict, list]:
    summary, by_year, navs, results, holds = [], [], {}, {}, []
    for name, kind, tg, oneq_leg in cfgs:
        if oneq_leg:
            # the 'QQQ' slot carries ONEQ in BLEND (and its half-spread)
            sim = mc.simulate(tg, data.sessions, data.perf_idx, data.close, data.last_row, oneq_lvl, oneq_px,
                              qqq_hs=mc.ONEQ_HS)
        else:
            sim = mc.simulate(tg, data.sessions, data.perf_idx, data.close, data.last_row, data.qqq_perf_idx,
                              data.qqq_close)
        dates = sim["nav"].index
        oneq = mc.buy_hold(oneq_lvl, oneq_px, dates, mc.ONEQ_HS)
        qqq = mc.buy_hold(data.qqq_perf_idx, data.qqq_close, dates, mc.QQQ_HS)
        per = {pn: nav_window_metrics(sim["nav"], oneq, qqq, a, b, sim["cost"], sim["traded"], sim["orders"])
               for pn, (a, b) in PERIODS.items()}
        for pn, m in per.items():
            summary.append({"config": name, "kind": kind, "period": pn, **m})
        crit = criteria(per)
        results[name] = {"kind": kind, "criteria": crit, "start": str(sim["start"].date()),
                         "avg_names_held": float(sim["names"].mean()),
                         "t_full": per[FULL]["t_monthly_excess_vs_oneq"], "months": per[FULL]["months"]}
        navs[name] = sim["nav"]
        if "ONEQ" not in navs:
            navs["ONEQ"], navs["QQQ"] = oneq, qqq
            for pn, (a, b) in PERIODS.items():
                summary.append({"config": "ONEQ buy-hold", "kind": "benchmark", "period": pn,
                                **nav_window_metrics(oneq, oneq, qqq, a, b)})
                summary.append({"config": "QQQ buy-hold", "kind": "benchmark", "period": pn,
                                **nav_window_metrics(qqq, oneq, qqq, a, b)})
        yo, yq = yearly(oneq.pct_change().dropna()), yearly(qqq.pct_change().dropna())
        for yy, v in yearly(sim["nav"].pct_change().dropna()).items():
            by_year.append({"config": name, "year": yy, "strategy": v, "oneq": yo.get(yy), "qqq": yq.get(yy)})
        if kind == "trial":
            for s, lst in tg.items():
                for sid, w, *_ in lst:
                    holds.append({"config": name, "signal": s.date().isoformat(), "security_id": sid, "weight": w})
        log(f"{name:28s} P1 {per[JUDGED[0]]['cagr']:+.1%} (ONEQ {per[JUDGED[0]]['oneq_cagr']:+.1%}) "
            f"P2 {per[JUDGED[1]]['cagr']:+.1%} (ONEQ {per[JUDGED[1]]['oneq_cagr']:+.1%}) "
            f"t {per[FULL]['t_monthly_excess_vs_oneq']:+.2f} DD {per[FULL]['max_dd']:.0%} pass {crit['pass']}")
    return summary, by_year, navs, results, holds


# ======================================================================== commands

def coverage(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for y, g in panel.groupby(panel["s"].dt.year):
        r = {"year": int(y), "signals": int(g["s"].nunique()), "avg_names": round(len(g) / g["s"].nunique(), 1),
             "label_share": round(float(g["fwd_ret"].notna().mean()), 3),
             "ff12_known": round(float(g["ff12"].notna().mean()), 3)}
        for f in CONT_FEATURES:
            r[f] = round(float(g[f].notna().mean()), 3)
        rows.append(r)
    return pd.DataFrame(rows)


def check(args):
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, *_ = build_panel(ML_CACHE, refresh=args.refresh)
    guard = mm.assert_point_in_time(panel)
    cov = coverage(panel)
    cov.to_csv(OUT / "check_coverage.csv", index=False)
    X = mm.feature_matrix(panel, CFG)
    rng = X[list(CONT_FEATURES)].agg(["min", "max", "mean"]).T
    assert rng["min"].min() >= -0.5 - 1e-12 and rng["max"].max() <= 0.5 + 1e-12
    pd.set_option("display.width", 250)
    print(json.dumps(guard, indent=1))
    print(cov[["year", "signals", "avg_names", "label_share", "ff12_known", "r_1m", "mom_12_1", "beta_1y",
               "sma200_ratio", "ep", "roe", "q_eps_g", "size", "rd_sales"]].to_string())
    print(panel["ff12"].value_counts(dropna=False).to_string())
    for Y in (2016, 2026):
        sp = mm.split_for_year(sigs, Y, VAL_MONTHS)
        print(Y, "s0", sp["s0"].date(), "train", sp["train"][0].date(), "..", sp["train"][-1].date(), "val",
              sp["val"][0].date(), "..", sp["val"][-1].date(), "test", sp["test"][0].date(), "..",
              sp["test"][-1].date())


def smoke(args):
    out = OUT / "smoke"
    out.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, oneq_lvl, oneq_px = build_panel(ML_CACHE)
    mm.assert_point_in_time(panel)
    years = tuple(int(y) for y in args.years.split(",")) if args.years else (2016, 2017)
    pred, hyper, imp = mm.walk_forward(panel, sigs, CFG, years, shuffle_target=True)
    mm.ic_table(pred, MODELS).to_csv(out / "ic_shuffled.csv", index=False)
    mm.importance_table(imp, CFG, FEATURE_GROUPS)
    mm.decile_table(pred, MODELS)
    cfgs = configs(pred, panel)
    simulate_all(cfgs[:1] + cfgs[7:8], data, oneq_lvl, oneq_px, log=lambda *_: None)
    pd.DataFrame(hyper).to_csv(out / "hyper_shuffled.csv", index=False)
    print("SMOKE OK (labels shuffled within month; results carry no information and are not reported)")


def run(args):
    if not FROZEN.exists():
        raise SystemExit("run --register first")
    if json.loads(FROZEN.read_text())["sha256"] != prereg_hash():
        raise SystemExit("pre-registration text changed since --register")
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, oneq_lvl, oneq_px = build_panel(ML_CACHE)
    guard = mm.assert_point_in_time(panel)
    pred, hyper, imp = mm.walk_forward(panel, sigs, CFG, TEST_YEARS)
    pred.drop(columns=["fwd_ret"]).to_csv(OUT / "predictions.csv.gz", index=False, compression="gzip")
    pd.DataFrame([{k: v for k, v in h.items()} for h in hyper]).to_csv(OUT / "hyperparameters.csv", index=False)
    ic = mm.ic_table(pred, MODELS)
    ic.to_csv(OUT / "ic_by_year.csv", index=False)
    dec = mm.decile_table(pred, MODELS)
    dec.to_csv(OUT / "deciles.csv", index=False)
    impt = mm.importance_table(imp, CFG, FEATURE_GROUPS)
    impt.to_csv(OUT / "feature_importance.csv", index=False)
    cfgs = configs(pred, panel)
    summary, by_year, navs, results, holds = simulate_all(cfgs, data, oneq_lvl, oneq_px)
    pd.DataFrame(summary).to_csv(OUT / "summary.csv", index=False)
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    pd.DataFrame(holds).to_csv(OUT / "holdings_by_signal.csv.gz", index=False, compression="gzip")
    pd.DataFrame(navs).to_csv(OUT / "nav_daily.csv.gz", compression="gzip")
    trials = {k: v for k, v in results.items() if v["kind"] == "trial"}
    verdict = any(v["criteria"]["pass"] for v in trials.values())
    out = {"prereg_sha256": prereg_hash(), "guard": data.guard["assertion"], "point_in_time": guard,
           "n_trials": N_TRIALS,
           "bonferroni_t": bonferroni_t_student(results[next(iter(trials))]["months"] - 1, N_TRIALS, ALPHA_ONE_SIDED),
           "nominal_t_ge_2": int(sum(v["t_full"] >= 2 for v in trials.values())),
           "results": results, "verdict_pass": verdict, "first_signal": str(sigs[0].date()),
           "last_signal": str(sigs[-1].date()), "panel_rows": int(len(panel)),
           "terminal_events": int(len(data.terminal_events)),
           "ic_all": ic[ic["test_year"] == "all"].set_index("model")[["mean_ic", "t"]].to_dict("index")}
    (OUT / "results.json").write_text(json.dumps(out, indent=1, default=json_rounded) + "\n")
    print(ic[ic["test_year"] == "all"].to_string())
    print(dec[["model", "D1_mean_monthly", "D10_mean_monthly", "D10_minus_D1_mean", "D10_minus_D1_t"]].to_string())
    print("VERDICT:", "PASS" if verdict else "FAIL")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--smoke", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--run", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="rebuild the cached panel (--check)")
    ap.add_argument("--years", default="", help="--smoke only: comma-separated test years")
    a = ap.parse_args(argv)
    if a.register:
        register()
    elif a.check:
        check(a)
    elif a.smoke:
        smoke(a)
    else:
        run(a)


if __name__ == "__main__":
    main()
