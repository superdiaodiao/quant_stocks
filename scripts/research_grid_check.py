"""POST-HOC illustration: does a fine parameter grid search (like src/opt_params/opt_ma_params.py) pick parameters that
stay good out of sample?

THIS IS NOT NEW EVIDENCE.  Every test period used here was already used for the frozen one-shot tests in
docs/research_ledger_indicators.md, and here every grid point is run on those test periods, so the periods are now
seen through ~1,270 configurations.  The frozen results in output/research_only/indicators/ are not touched.

Grids (all reuse scripts/research_indicators.py: loaders, date guards, IBKR cost model, ONEQ benchmark, engines):
  MA crossover   short 3..10 (step 1) x long 20..60 (step 1), short < long -> 328 points (contains the owner's 5/20)
  Donchian       entry 10..60 (step 1) x exit 5..30 (step 5)               -> 306 points (contains 20/20 = O10)
Settings:
  index  hold ONEQ (signals on ^IXIC OHLC) when in, T-bill ETF when out;  dev 1999-03-10..2014-12-31,
         test 2014-12-31 entry close .. 2026-09-30
  stock  weekly dollar-volume top 300, close >= $10, up to 5 names, $10k IBKR Tiered;  dev 2017-2022,
         test1 2023-01..2026-08-31, test2 judged 2014-01..2016-12 (signals warm up from 2011-06)
Selection: the single best grid point by development Sharpe (the owner's optimiser default) and by development CAGR.
"""
from __future__ import annotations

import argparse
import json
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_indicators as ri  # noqa: E402
from scripts import research_qqq_timing as qt  # noqa: E402

OUT = ROOT / "output/research_only/grid_check"
MA_SHORT, MA_LONG = range(3, 11), range(20, 61)
DC_ENTRY, DC_EXIT = range(10, 61), range(5, 31, 5)
OWNER_REF = {"ma": (5, 20), "donchian": (20, 20)}

INDEX_PERIODS = {"dev": (ri.INDEX_DEV_START, ri.INDEX_DEV_END),
                 "test": (ri.INDEX_TEST_ENTRY, ri.INDEX_TEST_END)}
STOCK_PERIODS = {"dev": "dev", "test1": "test1", "test2": "test2"}   # names of research_livermore.WINDOWS
STOCK_JUDGED_LAST = {"dev": "2022-12-31", "test1": "2026-08-31", "test2": "2016-12-31"}
FutureDataError = ri.FutureDataError


# ======================================================================== grids and guards

def ma_grid() -> list[tuple[int, int]]:
    return [(s, l_) for s, l_ in product(MA_SHORT, MA_LONG) if s < l_]


def donchian_grid() -> list[tuple[int, int]]:
    return list(product(DC_ENTRY, DC_EXIT))


GRIDS = {"ma": ma_grid, "donchian": donchian_grid}
PARAM_NAMES = {"ma": ("short", "long"), "donchian": ("entry", "exit")}


def assert_in_period(dates, first: str, last: str, what: str = "series") -> None:
    """Raise FutureDataError if any date is after ``last``; AssertionError if any is before ``first``."""
    ix = pd.DatetimeIndex(dates)
    if len(ix) == 0:
        raise AssertionError(f"{what}: empty")
    if ix.max() > pd.Timestamp(last):
        raise FutureDataError(f"{what}: {ix.max().date()} is after {last}")
    if ix.min() < pd.Timestamp(first):
        raise AssertionError(f"{what}: {ix.min().date()} is before {first}")


def make_rule(kind: str, a: int, b: int) -> ri.Rule:
    if kind == "ma":
        return ri.Rule(f"MA{a}_{b}", f"ma_{a}_{b}", lambda x, a=a, b=b: ri.r_ma(x, a, b), True, holding=True)
    return ri.Rule(f"DC{a}_{b}", f"donchian_{a}_{b}", lambda x, a=a, b=b: ri.r_donchian(x, a, b), True)


# ======================================================================== index

def index_grid(period: str) -> pd.DataFrame:
    start, end = INDEX_PERIODS[period]
    data = ri.load_index_data(end)                     # every frame truncated at ``end`` and asserted
    for v in data.guard.values():
        if isinstance(v, dict):
            assert pd.Timestamp(v["last"]) <= pd.Timestamp(end)
    bench = ri.index_sim(pd.Series(1.0, index=data.r1["COMP"].index), data, "COMP", start, end)
    first_ret = "1999-03-11" if period == "dev" else ri.INDEX_TEST_FIRST
    assert_in_period(bench["ret"].index, first_ret, end, f"index {period} benchmark")
    bars = data.bars["COMP"]
    rows = []
    for kind, grid in GRIDS.items():
        for a, b in grid():
            r = make_rule(kind, a, b)
            buy, sell = r.fn(bars)
            tgt = ri.state_machine(buy, sell, bars["close"])
            sim = ri.index_sim(tgt, data, "COMP", start, end)
            assert_in_period(sim["ret"].index, first_ret, end, f"index {period} {r.code}")
            m = ri.index_metrics(sim, bench, data.rf)
            rows.append({"setting": "index", "kind": kind, "p1": a, "p2": b, "period": period,
                         "cagr": m["cagr"], "bench_cagr": m["bench_cagr"], "excess_cagr": m["excess_cagr"],
                         "max_dd": m["max_dd"], "bench_max_dd": m["bench_max_dd"], "sharpe": m["sharpe"],
                         "t_monthly_excess": m["t_monthly_excess"], "trades_per_year": m["trades_per_year"],
                         "first": str(sim["ret"].index[0].date()), "last": str(sim["ret"].index[-1].date())})
    print(f"index {period}: {len(rows)} grid points, frames truncated at {end}")
    return pd.DataFrame(rows)


# ======================================================================== stocks

def stock_grid(period: str) -> pd.DataFrame:
    data = ri.lv.load_window(STOCK_PERIODS[period])    # frozen data version 1, truncated and asserted
    print(data.guard["assertion"])
    runner = ri.StockRunner(data)
    judged_from = data.spec["judged_from"]
    rows = []
    for kind, grid in GRIDS.items():
        for a, b in grid():
            r = make_rule(kind, a, b)
            m, res, oneq, qqq = runner.run(r)
            assert_in_period(res["nav"].index, data.spec["perf_start"], STOCK_JUDGED_LAST[period],
                             f"stock {period} {r.code}")
            if judged_from != data.spec["perf_start"]:
                m = ri.stock_sub_metrics(res["nav"], oneq, qqq, judged_from, res["cost"], res["exposure"])
                m["n_buy"] = int(res["counts"]["buy"])
            rows.append({"setting": "stock", "kind": kind, "p1": a, "p2": b, "period": period,
                         "cagr": m["cagr"], "bench_cagr": m["bench_cagr"], "excess_cagr": m["excess_cagr"],
                         "max_dd": m["max_dd"], "bench_max_dd": m["bench_max_dd"], "sharpe": m["sharpe"],
                         "t_monthly_excess": m["t_excess_monthly"], "n_buy": m["n_buy"],
                         "first": m["start"], "last": m["end"]})
    print(f"stock {period}: {len(rows)} grid points, judged {rows[0]['first']}..{rows[0]['last']}")
    return pd.DataFrame(rows)


# ======================================================================== analysis

def spearman(x: pd.Series, y: pd.Series) -> float:
    return float(np.corrcoef(x.rank(), y.rank())[0, 1])


def summarise(allg: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    picks, corr = [], []
    for (setting, kind), g in allg.groupby(["setting", "kind"]):
        dev = g[g["period"] == "dev"].set_index(["p1", "p2"])
        n = len(dev)
        for tp in [p for p in g["period"].unique() if p != "dev"]:
            te = g[g["period"] == tp].set_index(["p1", "p2"]).reindex(dev.index)
            for metric in ("sharpe", "cagr", "excess_cagr"):
                corr.append({"setting": setting, "kind": kind, "test_period": tp, "metric": metric,
                             "spearman_dev_vs_test": spearman(dev[metric], te[metric]), "n_points": n})
            for by in ("sharpe", "cagr", "owner_ref"):
                key = OWNER_REF[kind] if by == "owner_ref" else dev[by].sort_values(ascending=False,
                                                                                     kind="stable").index[0]
                d, t = dev.loc[key], te.loc[key]
                picks.append({
                    "setting": setting, "kind": kind, "test_period": tp, "picked_by": by,
                    "params": f"{key[0]}/{key[1]}",
                    "dev_cagr": d["cagr"], "dev_oneq_cagr": d["bench_cagr"], "dev_excess": d["excess_cagr"],
                    "dev_max_dd": d["max_dd"], "dev_sharpe": d["sharpe"],
                    "dev_rank_sharpe": int((dev["sharpe"] > d["sharpe"]).sum() + 1),
                    "dev_rank_cagr": int((dev["cagr"] > d["cagr"]).sum() + 1),
                    "dev_grid_median_excess": float(dev["excess_cagr"].median()),
                    "test_cagr": t["cagr"], "test_oneq_cagr": t["bench_cagr"], "test_excess": t["excess_cagr"],
                    "test_max_dd": t["max_dd"], "test_oneq_max_dd": t["bench_max_dd"], "test_sharpe": t["sharpe"],
                    "test_t_monthly": t["t_monthly_excess"],
                    "test_rank_sharpe": int((te["sharpe"] > t["sharpe"]).sum() + 1),
                    "test_rank_cagr": int((te["cagr"] > t["cagr"]).sum() + 1),
                    "test_grid_median_excess": float(te["excess_cagr"].median()),
                    "test_grid_best_excess": float(te["excess_cagr"].max()),
                    "test_points_beating_oneq": int((te["excess_cagr"] > 0).sum()), "n_points": n})
    p = pd.DataFrame(picks)
    p["dev_edge_over_median"] = p["dev_excess"] - p["dev_grid_median_excess"]
    p["test_edge_over_median"] = p["test_excess"] - p["test_grid_median_excess"]
    return p, pd.DataFrame(corr)


def write_heatmaps(allg: pd.DataFrame) -> None:
    hm = OUT / "heatmaps"
    hm.mkdir(parents=True, exist_ok=True)
    for (setting, kind, period), g in allg.groupby(["setting", "kind", "period"]):
        r_, c_ = PARAM_NAMES[kind]
        for metric in ("sharpe", "cagr", "excess_cagr"):
            pv = g.pivot(index="p1", columns="p2", values=metric)
            pv.index.name, pv.columns.name = r_, c_
            pv.to_csv(hm / f"{setting}_{kind}_{period}_{metric}.csv", float_format="%.4f")


def main(argv=None):
    p = argparse.ArgumentParser(description="post-hoc grid-search illustration (not new evidence)")
    p.add_argument("--setting", choices=["index", "stock", "both"], default="both")
    a = p.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    frames = []
    if a.setting in ("index", "both"):
        frames += [index_grid(x) for x in INDEX_PERIODS]
    if a.setting in ("stock", "both"):
        frames += [stock_grid(x) for x in STOCK_PERIODS]
    allg = pd.concat(frames, ignore_index=True)
    allg.to_csv(OUT / f"grid_all_{a.setting}.csv", index=False, float_format="%.6f")
    write_heatmaps(allg)
    picks, corr = summarise(allg)
    picks.to_csv(OUT / f"picks_{a.setting}.csv", index=False, float_format="%.6f")
    corr.to_csv(OUT / f"rank_corr_{a.setting}.csv", index=False, float_format="%.4f")
    (OUT / "README.json").write_text(json.dumps({
        "status": "POST-HOC illustration, not new evidence; test periods already used by the frozen one-shots",
        "grid_points": {"ma": len(ma_grid()), "donchian": len(donchian_grid())},
        "index_periods": INDEX_PERIODS, "stock_periods": {k: ri.lv.WINDOWS[v] for k, v in STOCK_PERIODS.items()},
        "stock_sharpe": "daily NAV Sharpe without subtracting the risk-free rate (research_livermore.perf_metrics)",
        "index_sharpe": "daily Sharpe of returns over the risk-free rate (research_qqq_timing.metrics)"}, indent=2))
    pd.set_option("display.width", 250)
    cols = ["setting", "kind", "test_period", "picked_by", "params", "dev_cagr", "dev_excess", "dev_max_dd",
            "dev_sharpe", "test_cagr", "test_oneq_cagr", "test_excess", "test_max_dd", "test_oneq_max_dd",
            "test_rank_sharpe", "test_rank_cagr", "n_points", "test_grid_median_excess"]
    print(picks[cols].to_string(float_format=lambda v: f"{v:+.3f}"))
    print(corr.to_string(float_format=lambda v: f"{v:+.3f}"))


if __name__ == "__main__":
    main()
