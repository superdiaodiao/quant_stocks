"""Evaluation of the event and trade-level studies (spin-offs, Nasdaq-100 reconstitution, the "T" trading studies):
metrics of a strategy against its references, trip (round-trip trade) statistics and event-time abnormal returns.

Collected unchanged from the studies in the slimming pass (docs/architecture.md section 12). Each function keeps the
convention its study was registered with (key names, empty-input values, NaN handling, operation order), stated in
its docstring; the variants are distinct conventions, not copies.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant.evaluation.metrics import (
    TRADING_DAYS, cagr_of, core_metrics, max_drawdown, max_drawdown_of_returns, monthly, relative_metrics, t_and_ir,
)
from quant.evaluation.stats import mean_t_n_se


# ======================================================================== strategy vs references (daily returns)

def metrics_vs_refs(r: pd.Series, refs: dict, oneq: pd.Series, rf: pd.Series) -> dict:
    """``core_metrics`` of the daily returns ``r`` plus, per reference ``tag`` in ``refs`` (reindexed on ``r``):
    ``<tag>_cagr``, ``excess_vs_<tag>`` (CAGR difference), ``<tag>_max_dd``, ``t_monthly_excess_vs_<tag>`` /
    ``ir_vs_<tag>`` (``t_and_ir`` of the monthly excess), then ``relative_metrics`` vs ONEQ.
    studies/selective_t.py ``evaluate`` (references hbase / hmatch / h100) and studies/intraday_t.py (h75 / h100)."""
    m = {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
         **core_metrics(r, rf)}
    for tag, ref in refs.items():
        ref = ref.reindex(r.index)
        m[f"{tag}_cagr"] = cagr_of(ref)
        m[f"excess_vs_{tag}"] = m["cagr"] - cagr_of(ref)
        m[f"{tag}_max_dd"] = max_drawdown(pd.concat([pd.Series([1.0]), (1 + ref).cumprod()]))
        t, ir = t_and_ir(monthly(r) - monthly(ref))
        m[f"t_monthly_excess_vs_{tag}"], m[f"ir_vs_{tag}"] = t, ir
    m.update(relative_metrics(r, oneq, rf, "oneq"))
    return m


# ======================================================================== trip statistics

def trip_stats_by_reason(tr: pd.DataFrame, stock_years: float) -> dict:
    """Round trips with columns gross, cost, notional (dollars), reason (target / timeout / end), days, same_day:
    counts per year of ``stock_years``, net win rate, mean gross / net / cost per trip in bp of the notional and in
    dollars, exit-reason shares, mean days, same-day share; NaN statistics without trips (studies/selective_t.py)."""
    if tr.empty:
        return {"trips": 0, "trips_per_year": 0.0, "win_rate_net": float("nan"), "gross_per_trip_bp": float("nan"),
                "net_per_trip_bp": float("nan"), "gross_per_trip_usd": float("nan"), "net_per_trip_usd": float("nan"),
                "cost_per_trip_bp": float("nan"), "share_target": float("nan"), "share_timeout": float("nan"),
                "share_end": float("nan"), "mean_days": float("nan"), "same_day_share": float("nan")}
    net = tr["gross"] - tr["cost"]
    rn = tr["reason"].value_counts(normalize=True)
    return {"trips": int(len(tr)), "trips_per_year": len(tr) / stock_years,
            "win_rate_net": float((net > 0).mean()),
            "gross_per_trip_bp": float((tr["gross"] / tr["notional"]).mean() * 1e4),
            "net_per_trip_bp": float((net / tr["notional"]).mean() * 1e4),
            "gross_per_trip_usd": float(tr["gross"].mean()), "net_per_trip_usd": float(net.mean()),
            "cost_per_trip_bp": float((tr["cost"] / tr["notional"]).mean() * 1e4),
            "share_target": float(rn.get("target", 0.0)), "share_timeout": float(rn.get("timeout", 0.0)),
            "share_end": float(rn.get("end", 0.0)), "mean_days": float(tr["days"].mean()),
            "same_day_share": float(tr["same_day"].astype(bool).mean())}


def trips_by_year_family(tr: pd.DataFrame, asset: str, cfg: str) -> pd.DataFrame | None:
    """Per exit year and signal family: trips, net win rate, mean gross / net bp, share closed at the target, with
    leading asset / config columns; None without trips (studies/selective_t.py)."""
    if tr.empty:
        return None
    tr = tr.copy()
    tr["net"] = tr["gross"] - tr["cost"]
    tr["net_bp"] = tr["net"] / tr["notional"] * 1e4
    tr["gross_bp"] = tr["gross"] / tr["notional"] * 1e4
    g = tr.groupby([tr["exit"].dt.year, "family"])
    out = pd.DataFrame({"trips": g.size(), "win_rate_net": g["net"].apply(lambda x: (x > 0).mean()),
                        "gross_bp_mean": g["gross_bp"].mean(), "net_bp_mean": g["net_bp"].mean(),
                        "share_target": g["reason"].apply(lambda x: (x == "target").mean())}).reset_index()
    out = out.rename(columns={"exit": "year"})
    out.insert(0, "config", cfg)
    out.insert(0, "asset", asset)
    return out


def trip_stats_usd(tr: pd.DataFrame, years: float) -> dict:
    """Same-day round trips with columns gross, cost, notional (dollars): count and count per year of ``years``,
    mean gross / net / cost per trip in dollars, mean gross / net in bp of the notional, net hit rate and dollar
    totals; zeros without trips (studies/intraday_t.py)."""
    if tr.empty:
        return {"trips": 0, "trips_per_year": 0.0, "gross_per_trip_usd": 0.0, "net_per_trip_usd": 0.0,
                "cost_per_trip_usd": 0.0, "gross_per_trip_bp": 0.0, "net_per_trip_bp": 0.0, "hit_rate_net": 0.0,
                "total_gross_usd": 0.0, "total_net_usd": 0.0}
    net = tr["gross"] - tr["cost"]
    return {"trips": int(len(tr)), "trips_per_year": len(tr) / years,
            "gross_per_trip_usd": float(tr["gross"].mean()), "net_per_trip_usd": float(net.mean()),
            "cost_per_trip_usd": float(tr["cost"].mean()),
            "gross_per_trip_bp": float((tr["gross"] / tr["notional"]).mean() * 1e4),
            "net_per_trip_bp": float((net / tr["notional"]).mean() * 1e4),
            "hit_rate_net": float((net > 0).mean()),
            "total_gross_usd": float(tr["gross"].sum()), "total_net_usd": float(net.sum())}


def trips_by_year_usd(trips: pd.DataFrame, base: str, cfg: str) -> pd.DataFrame | None:
    """Per calendar year of the trip date: trips, gross / cost / net dollars and the mean net bp of the notional, with
    leading base / config columns; None without trips (studies/intraday_t.py)."""
    tr = trips.copy()
    if tr.empty:
        return None
    tr["net"] = tr["gross"] - tr["cost"]
    g = tr.groupby(tr["date"].dt.year)
    ty = pd.DataFrame({"trips": g.size(), "gross_usd": g["gross"].sum(),
                       "cost_usd": g["cost"].sum(), "net_usd": g["net"].sum(),
                       "net_bp_mean": g.apply(lambda x: (x["net"] / x["notional"]).mean() * 1e4)})
    ty.insert(0, "config", cfg)
    ty.insert(0, "base", base)
    return ty.reset_index().rename(columns={"date": "year"})


# ======================================================================== the vectorised T grid (accumulators)

def grid_window_stats(sim: dict, ymask: np.ndarray) -> dict:
    """Per-configuration statistics over the years in ``ymask`` from the per-year accumulators of the vectorised T grid
    (quant.strategies.t_grid ``simulate_grid``: ``acc`` per configuration, ``glob`` per basket): daily excess vs
    H_match with the window exposure w (mean, IR, t, daily Sharpe), CAGR, trip statistics per account year, and the
    arithmetic excess vs ONEQ on the ONEQ days (studies/t_grid.py ``wstats``)."""
    A = {k: v[ymask].sum(0) for k, v in sim["acc"].items()}
    G = {k: v[ymask].sum() for k, v in sim["glob"].items()}
    n = G["n"]
    w = A["sX"] / n
    fin = np.maximum(w - 1, 0.0)
    d_mean = (A["sR"] - w * G["sM"]) / n
    mean_e = d_mean + fin * G["sF"] / n
    var_e = np.maximum((A["sR2"] - 2 * w * A["sRM"] + w * w * G["sM2"]) / n - d_mean ** 2, 1e-30)
    sd = np.sqrt(var_e)
    acct_years = G["acct_days"] / TRADING_DAYS
    nT = np.maximum(A["nT"], 1)
    out = {"n": n, "w": w, "mean_e": mean_e, "ann_excess_arith": mean_e * TRADING_DAYS,
           "ir": mean_e / sd * math.sqrt(TRADING_DAYS), "t": mean_e / sd * math.sqrt(n), "sr_daily": mean_e / sd,
           "cagr": np.exp(A["sLog"] * TRADING_DAYS / n) - 1,
           "trips_per_acct_year": A["nT"] / acct_years, "win_rate": np.where(A["nT"] > 0, A["nW"] / nT, np.nan),
           "gross_bp": np.where(A["nT"] > 0, A["sG"] / nT, np.nan), "net_bp": np.where(A["nT"] > 0, A["sNet"] / nT, np.nan),
           "cost_bp": np.where(A["nT"] > 0, A["sC"] / nT, np.nan),
           "share_target": np.where(A["nT"] > 0, A["nTarget"] / nT, np.nan),
           "mean_days": np.where(A["nT"] > 0, A["sDays"] / nT, np.nan),
           "same_day_per_acct_year": A["nSame"] / acct_years, "shortfall": A["nShort"], "ruined": A["nRuin"]}
    if G["nO"] > 0:   # arithmetic excess vs ONEQ on the ONEQ days
        mo = (A["sR_o"] - G["sO"]) / G["nO"]
        vo = np.maximum((A["sR2_o"] - 2 * A["sRO"] + G["sO2"]) / G["nO"] - mo ** 2, 1e-30)
        out["ann_excess_oneq_arith"] = mo * TRADING_DAYS
        out["t_oneq_daily"] = mo / np.sqrt(vo) * math.sqrt(G["nO"])
    return out


def grid_hmatch_cagr(sim: dict, ymask: np.ndarray, w: np.ndarray) -> np.ndarray:
    """CAGR of the basket-level H_match of the grid accumulators ``sim`` (w * M_t - max(w-1, 0) * f_t, daily) for each configuration's w
    (studies/t_grid.py ``geo_hmatch``)."""
    years = sim["sessions"].year.to_numpy()
    sel = np.isin(years, sim["years"][ymask]) & np.isfinite(sim["M"])
    M, f = sim["M"][sel], sim["f"][sel]
    fin = np.maximum(w - 1, 0.0)
    s = np.zeros_like(w)
    for mt, ft in zip(M, f):
        s += np.log(np.maximum(1 + w * mt - fin * ft, 1e-12))
    return np.exp(s * TRADING_DAYS / len(M)) - 1


def grid_trip_summary(ex: dict, k: int) -> dict:
    """Trip statistics of configuration ``k`` of an exact grid re-run ``ex`` (quant.strategies.t_grid ``exact_run``)
    from its accumulators: per account year, net win rate and gross / net / cost bp per trip (NaN without trips),
    exit shares, day trades, shortfalls and ruined accounts (studies/t_grid.py ``trip_summary``)."""
    A = {kk: v[:, k].sum() for kk, v in ex["sim"]["acc"].items()}
    G = {kk: v.sum() for kk, v in ex["sim"]["glob"].items()}
    nT = max(A["nT"], 1)
    return {"trips_per_acct_year": A["nT"] / (G["acct_days"] / TRADING_DAYS), "trips": int(A["nT"]),
            "win_rate_net": A["nW"] / nT if A["nT"] else float("nan"),
            "gross_per_trip_bp": A["sG"] / nT if A["nT"] else float("nan"),
            "net_per_trip_bp": A["sNet"] / nT if A["nT"] else float("nan"),
            "cost_per_trip_bp": A["sC"] / nT if A["nT"] else float("nan"),
            "share_target": A["nTarget"] / nT if A["nT"] else float("nan"),
            "share_timeout": A["nTimeout"] / nT if A["nT"] else float("nan"),
            "mean_days": A["sDays"] / nT if A["nT"] else float("nan"),
            "day_trades_per_acct_year": A["nSame"] / (G["acct_days"] / TRADING_DAYS),
            "shortfall": int(A["nShort"]), "ruined_accounts": int(A["nRuin"])}


def basket_metrics_vs(r: pd.Series, refs: dict, oneq: pd.Series | None, rf: pd.Series,
                      exposure: float | None = None) -> dict:
    """Daily basket returns ``r`` (NaN dropped): CAGR, vol, max drawdown and Sharpe (excess over ``rf``, missing rf
    as 0, NaN without spread), then per reference the CAGR excess, the monthly-excess t / IR and the daily-excess t
    (0 without spread), and ``relative_metrics`` vs ONEQ when it has more than 60 sessions (studies/t_grid.py
    ``metrics_vs``; unlike ``metrics_vs_refs`` no ``core_metrics`` and no reference drawdowns)."""
    r = r.dropna()
    v0 = pd.concat([pd.Series([1.0]), (1 + r).cumprod().reset_index(drop=True)])
    ex = r - rf.reindex(r.index).fillna(0)
    m = {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)), "cagr": cagr_of(r),
         "vol": float(r.std() * math.sqrt(TRADING_DAYS)), "max_dd": max_drawdown(v0),
         "sharpe": float(ex.mean() / ex.std() * math.sqrt(TRADING_DAYS)) if ex.std() > 0 else float("nan")}
    if exposure is not None:
        m["exposure"] = exposure
    for tag, ref in refs.items():
        ref = ref.reindex(r.index)
        m[f"{tag}_cagr"] = cagr_of(ref)
        m[f"excess_vs_{tag}"] = m["cagr"] - cagr_of(ref)
        t, ir = t_and_ir(monthly(r) - monthly(ref))
        m[f"t_monthly_excess_vs_{tag}"], m[f"ir_vs_{tag}"] = t, ir
        d = r - ref
        m[f"t_daily_excess_vs_{tag}"] = float(d.mean() / d.std() * math.sqrt(len(d))) if d.std() > 0 else 0.0
    if oneq is not None and len(oneq) > 60:
        m.update(relative_metrics(r, oneq, rf, "oneq"))
    return m


# ======================================================================== weekly overlay (holding-period rows)

def weekly_overlay_metrics(w: pd.DataFrame, periods: float = 52.0) -> dict:
    """Holding-period rows of an overlay on a base (columns excess_net / excess_gross / excess_ideal, qqq, the
    long / short legs, cost parts, orders, traded value, gross book): annualised (x ``periods``) mean excess,
    geometric excess of base + overlay over the base, tracking error, IR and t (NaN without spread), drawdown of
    the overlay returns (``max_drawdown_of_returns``), hit rate, cost drag and turnover, skew / kurtosis and the
    per-period Sharpe; {} for no rows (studies/reversal_dev.py ``metrics``)."""
    if w.empty:
        return {}
    ex = w["excess_net"]
    n = len(ex)
    mean, sd = ex.mean(), ex.std(ddof=1)
    strat = (1 + w["qqq"] + ex).prod()
    bench = (1 + w["qqq"]).prod()
    years = n / periods
    gross_ann = w["excess_gross"].mean() * periods
    out = {
        "weeks": n,
        "excess_net_ann": mean * periods,
        "excess_net_geo_ann": (strat / bench) ** (1 / years) - 1,
        "tracking_error": sd * math.sqrt(periods),
        "ir": mean / sd * math.sqrt(periods) if sd > 0 else float("nan"),
        "t_stat": mean / sd * math.sqrt(n) if sd > 0 else float("nan"),
        "max_dd_overlay": max_drawdown_of_returns(ex),
        "hit_rate": float((ex > 0).mean()),
        "excess_gross_ann": gross_ann,
        "excess_ideal_ann": w["excess_ideal"].mean() * periods,
        "t_ideal": w["excess_ideal"].mean() / w["excess_ideal"].std(ddof=1) * math.sqrt(n),
        "cost_drag_ann": gross_ann - mean * periods,
        "cost_per_year_usd_at_start": w["cost"].mean() * periods,
        "commission_share_of_cost": w["commission"].sum() / w["cost"].sum() if w["cost"].sum() else float("nan"),
        "orders_per_week": w["orders"].mean(),
        "partial_orders_per_week": w["partial_orders"].mean(),
        "turnover_traded_over_book": (w["traded_value"] / w["gross_book"]).mean(),
        "long_net_ann": w["long_net"].mean() * periods, "short_net_ann": w["short_net"].mean() * periods,
        "long_gross_ann": w["long_gross"].mean() * periods, "short_gross_ann": w["short_gross"].mean() * periods,
        "long_leg_raw_ann": w["long_leg_ret"].mean() * periods, "short_leg_raw_ann": w["short_leg_ret"].mean() * periods,
        "qqq_ann": w["qqq"].mean() * periods,
        "strategy_cagr": strat ** (1 / years) - 1, "qqq_cagr": bench ** (1 / years) - 1,
        "skew": float(ex.skew()), "kurt": float(ex.kurt() + 3),
        "sr_weekly": mean / sd if sd > 0 else float("nan"),
    }
    return out


def excess_by_year(w: pd.DataFrame) -> dict:
    """Sum of the net excess per calendar year of the entry, rounded to 4 places (studies/reversal_dev.py)."""
    y = w.assign(year=pd.to_datetime(w["entry"]).dt.year)
    return {int(k): round(float(g["excess_net"].sum()), 4) for k, g in y.groupby("year")}


# ======================================================================== event-time abnormal returns

def car_summary(t: pd.DataFrame) -> pd.DataFrame:
    """Event-time CAR table ``t`` (quant.strategies.ndx_recon ``car_table``) -> mean CAR, event t-stat
    (``mean_t_n_se``) and announcement-clustered t-stat (events of one announcement averaged first,
    the registered main statistic) for adds (announced type) and index deletions, by half and pooled."""
    out = []
    sets = {"add": t[(t.side == "add") & (t.type == "announced")],
            "delete": t[(t.side == "delete") & (t.type == "index")]}
    for side, s in sets.items():
        for half in ("all", "H1", "H2"):
            h = s if half == "all" else s[s.half == half]
            for kind in ("all", "annual+quarterly", "adhoc"):
                k = h if kind == "all" else h[h.kind.isin(["annual", "quarterly"])] if kind != "adhoc" else \
                    h[h.kind == "adhoc"]
                for win in ("ann", "r1", "post5", "post20"):
                    for b in ("QQQ", "ONEQ"):
                        col = f"{win}_{b}"
                        m, tt, n = mean_t_n_se(k[col])
                        g = k.groupby("group")[col].mean()
                        mg, tg, ng = mean_t_n_se(g)
                        out.append({"side": side, "half": half, "kind": kind, "window": win, "bench": b,
                                    "n_events": n, "mean_car": m, "t_event": tt, "median_car": float(k[col].median())
                                    if n else np.nan, "pos_share": float((k[col].dropna() > 0).mean()) if n else np.nan,
                                    "n_announcements": ng, "t_clustered": tg})
    return pd.DataFrame(out)


def bhar_summary(t: pd.DataFrame) -> pd.DataFrame:
    """Event-time BHAR table ``t`` (quant.strategies.spinoffs ``bhar_table``) -> per group (all, each half, Nasdaq
    or not), who, entry day and horizon, vs ONEQ and vs the size ETF: n, mean, median, naive t (mean / sd(ddof=1) *
    sqrt(n), NaN without spread; groups with fewer than 2 values left out), share positive, mean raw return."""
    rows = []
    groups = [("all", t)] + [(f"half={h}", g) for h, g in t.groupby("half")] + \
             [(f"nasdaq={n}", g) for n, g in t.groupby("nasdaq")]
    for name, g in groups:
        for (who, day, h), x in g.groupby(["who", "entry_day", "months"]):
            for col in ("bhar_oneq", "bhar_size"):
                v = x[col].dropna()
                if len(v) < 2:
                    continue
                rows.append({"group": name, "who": who, "entry_day": day, "months": h, "vs": col, "n": len(v),
                             "mean": v.mean(), "median": v.median(),
                             "t_naive": v.mean() / v.std(ddof=1) * math.sqrt(len(v)) if v.std() > 0 else np.nan,
                             "share_positive": float((v > 0).mean()), "mean_raw_ret": x["ret"].mean()})
    return pd.DataFrame(rows)
