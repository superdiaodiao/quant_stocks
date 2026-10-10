"""The monthly U300 feature panel of the machine-learning study (13 price features, the 40 point-in-time fundamental
factors of quant.strategies.fundamentals, FF12 industry dummies), defined on frozen data version 2.

Extracted unchanged from scripts/research_ml_cross_section.py (``build_panel``, ``price_features``,
``qqq_total_returns`` and their helpers); also used by research_short_overlay, research_index_exclusion and the
S-MISP forward signal (quant.observation.smisp). The panel is cached in research_cache/ml_cross_section_v2.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.data import market_cap as mcap
from quant.data import version as dv
from quant.data.calendar import last_session_of_each_month
from quant.paths import CACHE_ROOT
from quant.strategies import canslim as cs
from quant.strategies import fundamentals as rf
from quant.strategies import indicators as ind


def require_v2():
    if not dv.IS_V2:
        raise SystemExit("research_ml_cross_section is pre-registered on frozen data v2: set REVERSAL_DATA_VERSION=v2")


MCACHE = CACHE_ROOT / "ml_cross_section_v2"
PANEL_CACHE = MCACHE / "panel_v1.pkl"
PRICE_START, PERF_START, END = "2011-06-01", "2016-01-01", "2026-08-31"
FIRST_SIGNAL = "2012-01-31"
PRICE_FEATURES = ("r_1w", "r_1m", "mom_3m", "mom_6_1", "mom_12_1", "vol_1m", "vol_3m", "max_ret_1m", "beta_1y",
                  "log_dv50", "dist_52wh", "sma50_ratio", "sma200_ratio")
FUND_FEATURES = rf.FACTORS
CONT_FEATURES = PRICE_FEATURES + FUND_FEATURES
FF12_RANGES = {
    "NoDur": [(100, 999), (2000, 2399), (2700, 2749), (2770, 2799), (3100, 3199), (3940, 3989)],
    "Durbl": [(2500, 2519), (2590, 2599), (3630, 3659), (3710, 3711), (3714, 3714), (3716, 3716), (3750, 3751),
              (3792, 3792), (3900, 3939), (3990, 3999)],
    "Manuf": [(2520, 2589), (2600, 2699), (2750, 2769), (3000, 3099), (3200, 3569), (3580, 3629), (3700, 3709),
              (3712, 3713), (3715, 3715), (3717, 3749), (3752, 3791), (3793, 3799), (3830, 3839), (3860, 3899)],
    "Enrgy": [(1200, 1399), (2900, 2999)],
    "Chems": [(2800, 2829), (2840, 2899)],
    "BusEq": [(3570, 3579), (3660, 3692), (3694, 3699), (3810, 3829), (7370, 7379)],
    "Telcm": [(4800, 4899)],
    "Utils": [(4900, 4949)],
    "Shops": [(5000, 5999), (7200, 7299), (7600, 7699)],
    "Hlth": [(2830, 2839), (3693, 3693), (3840, 3859), (8000, 8099)],
    "Money": [(6000, 6999)],
}


def ff12(sic) -> str | None:
    """Fama-French 12-industry label of a SIC code (None when the SIC is unknown)."""
    try:
        v = float(sic)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    v = int(v)
    for k, rngs in FF12_RANGES.items():
        if any(a <= v <= b for a, b in rngs):
            return k
    return "Other"


def price_feature_frames(idx: pd.DataFrame, close_adj: pd.DataFrame, vol_adj: pd.DataFrame,
                         mkt_ret: pd.Series) -> dict:
    """Session x security frames of the 13 price features. Every value at session t uses rows <= t only
    (shifts and trailing rolling windows)."""
    r = idx.pct_change(fill_method=None)
    out = {"r_1w": idx / idx.shift(5) - 1, "r_1m": idx / idx.shift(21) - 1, "mom_3m": idx / idx.shift(63) - 1,
           "mom_6_1": idx.shift(21) / idx.shift(126) - 1, "mom_12_1": idx.shift(21) / idx.shift(252) - 1,
           "vol_1m": r.rolling(21, min_periods=15).std(), "vol_3m": r.rolling(63, min_periods=40).std(),
           "max_ret_1m": r.rolling(21, min_periods=15).max()}
    q = pd.DataFrame(np.repeat(mkt_ret.reindex(r.index).to_numpy()[:, None], r.shape[1], axis=1),
                     index=r.index, columns=r.columns).where(r.notna())
    rr = r.where(q.notna())
    w = dict(window=252, min_periods=126)
    cov = (rr * q).rolling(**w).mean() - rr.rolling(**w).mean() * q.rolling(**w).mean()
    var = (q * q).rolling(**w).mean() - q.rolling(**w).mean() ** 2
    out["beta_1y"] = cov / var.where(var > 0)
    dvol = (close_adj * vol_adj).rolling(50, min_periods=20).median()
    out["log_dv50"] = np.log(dvol.where(dvol > 0))
    hi = close_adj.rolling(252, min_periods=126).max()
    out["dist_52wh"] = close_adj / hi - 1
    out["sma50_ratio"] = close_adj / close_adj.rolling(50, min_periods=50).mean() - 1
    out["sma200_ratio"] = close_adj / close_adj.rolling(200, min_periods=200).mean() - 1
    return {k: v.replace([np.inf, -np.inf], np.nan) for k, v in out.items()}


def sample_at(frames: dict, sids, dates) -> pd.DataFrame:
    """Values of each frame at (last session <= date, sid)."""
    return pd.DataFrame({k: mcap.value_at(f, sids, dates) for k, f in frames.items()})


def price_features(idx, close_adj, vol_adj, mkt_ret, sids, dates) -> pd.DataFrame:
    return sample_at(price_feature_frames(idx, close_adj, vol_adj, mkt_ret), sids, dates)[list(PRICE_FEATURES)]


def u300_candidates(data, sigs: list) -> pd.DataFrame:
    """research_fundamentals.candidate_rows restricted to the U300 rows (latest top-300 week <= s)."""
    uni = data.universe
    u3 = uni[uni["dv50_rank"].notna() & uni["security_id"].isin(data.close.columns)].copy()
    weeks = np.array(sorted(u3["week_end"].unique()), dtype="datetime64[ns]")
    by = {w: g for w, g in u3.groupby("week_end")}
    rows = []
    for s in sigs:
        k = weeks.searchsorted(np.datetime64(s), side="right") - 1
        if k < 0:
            continue
        g = by[pd.Timestamp(weeks[k])][["security_id", "ticker", "cik", "dv50_rank", "sic", "multi_class_group"]]
        rows.append(g.assign(in300=True, s=s, universe_week=pd.Timestamp(weeks[k])))
    c = pd.concat(rows, ignore_index=True)
    facts = pd.read_csv(rf.SECFACTS, dtype=str, usecols=["security_id", "cik"]).set_index("security_id")["cik"]
    c["cik"] = c["cik"].fillna(c["security_id"].map(facts)).fillna(c["security_id"].str.split(".").str[0])
    c["cik"] = pd.to_numeric(c["cik"], errors="coerce").astype("Int64")
    px = mcap.value_at(data.close, c["security_id"], c["s"])
    lr = pd.to_datetime(c["security_id"].map(data.last_row))
    c["close_s"] = px
    c = c[np.isfinite(px) & (lr.isna() | (lr >= c["s"])).to_numpy()].copy()
    cs.assert_window(c["universe_week"].dropna(), "2012-01-01", END, "universe weeks used")
    c["sic"] = pd.to_numeric(c["sic"], errors="coerce")
    miss = c["sic"].isna() & c["cik"].notna()
    smap = rf.sic_map(c.loc[miss, "cik"].dropna().astype(int).unique())
    c.loc[miss, "sic"] = c.loc[miss, "cik"].map(lambda x: smap.get(int(x), np.nan) if pd.notna(x) else np.nan)
    return c.reset_index(drop=True)


def qqq_total_returns(end: str) -> pd.Series:
    q = pd.read_csv(dv.CACHE / "factors/qqq_joined.csv", dtype={"date": str})
    q = cs.truncate(q, "date", PRICE_START, end).sort_values("date")
    r = (q["close"] + q["dividend"].fillna(0)) / q["close"].shift(1) - 1
    return pd.Series(r.to_numpy(float), index=pd.DatetimeIndex(pd.to_datetime(q["date"])))


def build_panel(refresh: bool = False):
    """Load data v2 and build the U300 monthly panel: fundamentals (point in time), market cap, price features,
    FF12 label, next-month total return. Returns (data, sigs, panel, oneq_lvl, oneq_px)."""
    require_v2()
    rf.PERF_START = PERF_START          # performance index starts 2016 (features use the full history)
    rf.FCACHE = MCACHE                   # derived caches of this study only (fundamentals_v2 cache untouched)
    MCACHE.mkdir(parents=True, exist_ok=True)
    data = rf.load_all()
    print("DATE GUARD:", data.guard["assertion"], flush=True)
    end = data.spec["effective_end"]
    sigs = [s for s in last_session_of_each_month(data.sessions) if s >= pd.Timestamp(FIRST_SIGNAL)]
    oneq_lvl, oneq_px = ind.oneq_on_sessions(data.sessions, PRICE_START, end)
    if PANEL_CACHE.exists() and not refresh:
        panel = pd.read_pickle(PANEL_CACHE)
        if sorted(panel["s"].unique()) == sorted(sigs):
            return data, sigs, panel, oneq_lvl, oneq_px
    cand = u300_candidates(data, sigs)
    ciks = sorted(cand["cik"].dropna().astype(int).unique())
    alias = mcap.successor_ciks(data.universe)
    sh = mcap.extract_share_facts(sorted(set(ciks) | set(alias)), cache=MCACHE / "sec_share_facts.csv.gz")
    sh = mcap.add_predecessor_facts(sh, alias)
    lists = mcap.load_company_lists()
    dv50 = (data.close_adj * data.vol_adj).rolling(50, min_periods=20).median()
    # fundamentals data rule 1.1a: shares x price is not replaced by the public float
    capped = mcap.market_caps(cand[["s", "security_id", "ticker", "cik", "dv50_rank", "universe_week"]], sh, lists,
                              data.close, data.sig_idx, dv50, shares_vs_float=np.inf)
    cand["mcap"] = capped["mcap"].to_numpy()
    cand["mcap_src"] = capped["mcap_src"].to_numpy()
    cand.loc[cand["mcap_src"].astype(str).str.startswith("dollar_volume"), "mcap"] = np.nan
    cand["dv50"] = mcap.value_at(dv50, cand["security_id"], cand["s"])
    cand = rf.reject_small_mcaps(cand)
    annual, eps = rf.extract_all(ciks)
    annual = annual[annual["filed"].astype(str) <= end]
    eps = eps[eps["avail"].astype(str) <= end]
    states = rf.build_states(annual)
    states = states[states["filed"].astype(str) <= end]
    cs.assert_window(states["filed"], None, end, "fundamental filings used")
    rows = rf.universes(cand)["U300"]
    m = rf.asof_states(rows, states, eps)
    m = rf.market_factors(m)
    m = m.sort_values(["s", "security_id"]).reset_index(drop=True)
    m = pd.concat([m, rf.composites(m)], axis=1)
    # ---- price features (only the securities that ever enter the panel)
    ids = sorted(m["security_id"].unique())
    pf = price_features(data.sig_idx[ids], data.close_adj[ids], data.vol_adj[ids], qqq_total_returns(end),
                        m["security_id"], m["s"])
    for f in PRICE_FEATURES:
        m[f] = pf[f].to_numpy()
    # ---- next-month total return (label; terminal values included through the index)
    nxt = dict(zip(sigs[:-1], sigs[1:]))
    m["s_next"] = m["s"].map(nxt)
    i0 = mcap.value_at(data.sig_idx, m["security_id"], m["s"])
    i1 = mcap.value_at(data.sig_idx, m["security_id"], m["s_next"].fillna(m["s"]))
    m["fwd_ret"] = np.where(m["s_next"].notna(), i1 / i0 - 1, np.nan)
    m["ff12"] = m["sic"].map(ff12)
    keep = ["s", "s_next", "security_id", "ticker", "cik", "sic", "ff12", "dv50_rank", "mcap", "mcap_src",
            "fwd_ret", "filed", "fy0_end", "eps_avail"] + list(CONT_FEATURES) + list(rf.COMPOSITES)
    panel = m[keep].copy()
    panel.to_pickle(PANEL_CACHE)
    return data, sigs, panel, oneq_lvl, oneq_px
