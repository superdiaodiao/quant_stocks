"""The point-in-time Nasdaq market-cap ranking of 1998-2013 (megacap out-of-sample OOS.2, reused by OOS.3): price
panels per candidate stitched from Yahoo / Wayback / CMC / Tiingo sources, SEC cover-page share counts, split units,
terminal values, monthly market-cap ranks and the engine panels; QQQ / ONEQ benchmarks and period statistics.

Extracted unchanged from scripts/research_megacap_oos2.py (``build``, ``market_caps``, ``rank_table``,
``engine_panels``, ``period_stats``, ``load_qqq`` / ``load_oneq``, ``wayback_segment`` and helpers). The raw-source
fetchers are in quant/data/sources/megacap_oos2.py (moved from scripts/megacap_oos2_data.py, phase 3); ``build``
writes its share-count fixes into the OOS.2 output folder, as before.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant.data.calendar import last_session_of_each_month
from quant.data.panel import TERMINAL_D5
from quant.data.sources.yahoo import parse_chart
from quant.evaluation.metrics import max_drawdown, monthly as _monthly
from quant.paths import CACHE_ROOT, output_dir
from quant.data.sources import megacap_oos2 as md

OUT = output_dir("megacap_oos2")                  # build() reads the share overrides and writes the fixes here
LOCAL = md.CACHE / "built"                       # vendor-derived panels (local only)
QQQ_CHART = CACHE_ROOT / "qqq_timing/raw/chart_QQQ.json"
ONEQ_CHART = CACHE_ROOT / "benchmarks/chart_ONEQ.json"
START = "1998-06-01"
FIRST_SIGNAL = "1998-12-31"
END = "2013-12-31"
JUDGE_FROM = "1999-03-10"                        # QQQ's first close
ONEQ_FROM = "2003-10-01"
PERIODS = {"1999-2002": ("1999-03-10", "2002-12-31"), "2003-2007": ("2003-01-01", "2007-12-31"),
           "2008-2013": ("2008-01-01", "2013-12-31"), "Full 1999-03-10..2013": ("1999-03-10", "2013-12-31")}
FULL = "Full 1999-03-10..2013"
FRESH_DAYS = 400
SPLIT_RATIOS = (2.0, 3.0, 1.5, 4.0, 5.0, 4 / 3, 1.25, 0.25, 0.5, 0.2, 0.1, 0.05, 0.125)
SPLIT_TOL = 0.06


def _segment_from_source(src: str, sym: str) -> pd.DataFrame | None:
    """One source's daily frame: date, adj (split-adjusted close to the source's reference date), tr (level),
    close_raw (NaN when unknown), splits list (date, ratio) known to the source, cmc_mcap (CMC only)."""
    if src == "yahoo":
        y = md.yahoo_daily(sym)
        if y is None:
            return None
        y = y.copy()
        r = (y["close_adj"] + y["div_adj"]) / y["close_adj"].shift(1) - 1
        y["tr"] = (1 + r.fillna(0)).cumprod()
        y["adj"] = y["close_adj"]
        y.attrs["splits"] = [(d, s) for d, s in zip(y["date"], y["split"]) if s != 1.0]
        y.attrs["ref"] = pd.Timestamp(md.YAHOO_FETCH_DAY)
        return y[["date", "adj", "tr", "close_raw"]].assign(source=f"yahoo:{sym}")
    if src in ("tiingo", "tiingo_cache"):
        t = md.tiingo_daily(sym) if src == "tiingo" else md.tiingo_cached(sym)
        if t is None:
            return None
        t = t.copy()
        f = np.ones(len(t))
        sp = [(d, x) for d, x in zip(t["date"], t["split"]) if x != 1.0]
        for d, x in sp:
            f[t["date"].to_numpy() < np.datetime64(d)] /= x
        t["adj"] = t["close_raw"] * f
        t["tr"] = _tiingo_tr(t)
        return t[["date", "adj", "tr", "close_raw"]].assign(source=f"{src}:{sym}")
    if src == "wayback":
        raise RuntimeError("wayback segments are built by wayback_segment (needs the need window)")
    if src == "cmc":
        s = md.cmc_series(sym)
        m = s["marketcap"]
        if m.empty:
            return None
        df = pd.DataFrame({"date": m.index, "cmc_mcap": m.to_numpy()})
        df["tr"] = df["cmc_mcap"] / df["cmc_mcap"].iloc[0]
        df["adj"] = np.nan
        df["close_raw"] = np.nan
        df.attrs["splits"] = s["splits"]
        return df.assign(source=f"cmc:{sym}")
    raise ValueError(src)


def _tiingo_tr(t: pd.DataFrame) -> pd.Series:
    """Total-return level from raw close, dividends and split factors (Tiingo's own adjClose identity)."""
    c = t["close_raw"].to_numpy()
    s = t["split"].to_numpy()
    dv = t["div_raw"].to_numpy()
    dv = dv.copy()
    # a large distribution on a day the price did not fall is already in the prior closes (Tiingo's TFCFA
    # 2013-07-01 News Corp spin-off: $3.81 'dividend', close 28.99 -> 29.40): it is not counted twice
    big = np.zeros(len(c), dtype=bool)
    big[1:] = (dv[1:] > 0.05 * c[:-1]) & (c[1:] * s[1:] > 0.97 * c[:-1])
    for i in np.flatnonzero(big):
        print(f"  tiingo distribution ignored on {t['date'].iloc[i].date()}: {dv[i]:.4f} (close {c[i-1]:.2f} -> {c[i]:.2f})")
    dv[big] = 0.0
    r = np.zeros(len(c))
    r[1:] = (c[1:] * s[1:] + dv[1:]) / c[:-1] - 1
    return pd.Series(np.cumprod(1 + r), index=t.index)


def wayback_segment(sym: str, a: pd.Timestamp, b: pd.Timestamp) -> pd.DataFrame | None:
    """Archived Yahoo table.csv: 'Close' is the raw close, 'Adj Close' is adjusted for splits and dividends (checked:
    SUNW 1999-12-07 144.06 -> 1999-12-08 78.44 raw, 36.01 -> 39.22 adjusted; CELG 2014-06-25/26). Splits are read
    from jumps in Close / Adj Close; tr is the Adj Close level. Several captures are chained on raw closes."""
    frames = md.best_wayback(sym, a, b)
    if not frames:
        return None
    parts = []
    for f in sorted(frames, key=lambda x: x["Date"].min()):
        adjcol = next((c for c in f.columns if str(c).lower().startswith("adj")), "Close")
        g = pd.DataFrame({"date": f["Date"], "close_raw": f["Close"].astype(float), "trl": f[adjcol].astype(float)})
        g = g[(g["close_raw"] > 0) & (g["trl"] > 0)]
        if parts:
            g = g[g["date"] > parts[-1]["date"].max()]
        if len(g):
            parts.append(g)
    g = pd.concat(parts, ignore_index=True).sort_values("date").drop_duplicates("date").reset_index(drop=True)
    k = g["close_raw"] / g["trl"]
    jump = (k.shift(1) / k).to_numpy()
    splits = []
    for i in range(1, len(g)):
        q = jump[i]
        if np.isfinite(q) and (q > 1.2 or q < 0.83):
            r = _nearest_ratio(q) or float(q)
            splits.append((g["date"].iloc[i], r))
    r = g["trl"] / g["trl"].shift(1) - 1
    raw_r = g["close_raw"] / g["close_raw"].shift(1) - 1
    r = r.where(np.isfinite(r) & (r.abs() < 0.9), raw_r)          # a junction between captures
    g["tr"] = (1 + r.fillna(0)).cumprod()
    f_adj = np.ones(len(g))
    for d, x in splits:
        f_adj[g["date"].to_numpy() < np.datetime64(d)] /= x
    g["adj"] = g["close_raw"] * f_adj
    out = g[["date", "adj", "tr", "close_raw"]].assign(source=f"wayback:{sym}")
    out.attrs["splits"] = splits
    return out


def name_prices(row, sessions: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    """Daily frame on ``sessions`` for one candidate and its split metadata.

    Columns: adj (split-adjusted close on the primary source's basis; NaN on CMC days), tr (total-return level),
    close_raw (when known), cmc_mcap (CMC days), source. The primary source is the first non-CMC source of the plan
    that answers; a CMC source fills only dates after the primary ends (or after its 'until' date).
    meta: splits = the primary's split events when it has them (Yahoo / Tiingo; F is then computed from them), else
    None (F inferred from the share counts); G = splits between the last share count and the primary's basis date,
    calibrated against any other raw-price source of the plan (1.0 when there is none)."""
    a_need, _ = md.need_window(row)
    plan = md.plan_sources(row.prices)
    primary, meta, others = None, {"splits": None, "G": 1.0, "primary": ""}, []
    for src, sym, until in plan:
        if src == "cmc":
            continue
        seg = wayback_segment(sym, max(a_need, pd.Timestamp(START)), pd.Timestamp(END)) if src == "wayback" else \
            _segment_from_source(src, sym)
        if seg is None or seg.empty:
            continue
        if until:
            seg = seg[seg["date"] <= pd.Timestamp(until)]
        a_cov = max(a_need, pd.Timestamp(START))
        b_cov = min(pd.Timestamp(until) if until else pd.Timestamp(END), pd.Timestamp(row.nasdaq_to or END))
        need_days = int(((sessions >= a_cov) & (sessions <= b_cov)).sum())
        got_days = int(((seg["date"] >= a_cov) & (seg["date"] <= b_cov)).sum())
        if primary is None and need_days and got_days < 0.3 * need_days:
            print(f"  {row.key}: {src}:{sym} covers {got_days}/{need_days} needed sessions -> not used as primary")
            others.append(seg)
            continue
        if primary is None:
            primary = seg
            meta["T_p"] = seg["date"].max()
            meta["primary"] = f"{src}:{sym}"
            if src in ("yahoo", "tiingo", "tiingo_cache"):
                meta["splits"] = _source_splits(src, sym)
            elif src == "wayback":
                meta["splits"] = list(seg.attrs.get("splits", []))
        else:
            others.append(seg)
    segs = [primary] if primary is not None else []
    cursor = primary["date"].max() if primary is not None else pd.Timestamp(START) - pd.Timedelta(days=1)
    for src, sym, until in plan:
        if src != "cmc" or cursor >= pd.Timestamp(END):
            continue
        seg = _segment_from_source(src, sym)
        if seg is None:
            continue
        seg = seg[(seg["date"] > cursor) & (seg["date"] <= pd.Timestamp(until or END))]
        if len(seg):
            segs.append(seg)
            cursor = seg["date"].max()
    if not segs:
        return pd.DataFrame(columns=["adj", "tr", "close_raw", "cmc_mcap", "source"]), meta
    # G: raw / adj of another raw source at the last common in-window sessions (a split after the last share count)
    if meta["splits"] is None and primary is not None:
        for o in others:
            ov = primary.merge(o[["date", "close_raw"]].rename(columns={"close_raw": "raw_o"}), on="date")
            ov = ov[(ov["date"] <= pd.Timestamp(END)) & ov["raw_o"].notna() & (ov["adj"] > 0)].tail(20)
            if len(ov) >= 5:
                meta["G"] = float(np.median(ov["raw_o"] / ov["adj"]))
                meta["G_from"] = o["source"].iloc[0]
                break
    out, level = [], 1.0
    for seg in segs:
        seg = seg.copy()
        seg["tr"] = seg["tr"] / seg["tr"].iloc[0] * level
        level = float(seg["tr"].iloc[-1])
        out.append(seg)
    df = pd.concat(out, ignore_index=True)
    if "cmc_mcap" not in df:
        df["cmc_mcap"] = np.nan
    df = df.set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[(df.index >= pd.Timestamp(START)) & (df.index <= pd.Timestamp(END))]
    return df.reindex(df.index.intersection(sessions)), meta


def _source_splits(src: str, sym: str) -> list:
    if src == "yahoo":
        y = md.yahoo_daily(sym)
        return [(d, x) for d, x in zip(y["date"], y["split"]) if x != 1.0]
    t = md.tiingo_daily(sym) if src == "tiingo" else md.tiingo_cached(sym)
    return [(d, x) for d, x in zip(t["date"], t["split"]) if x != 1.0]


def share_facts(cover: pd.DataFrame, dei: pd.DataFrame, overrides: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per (key, filing): filed, asof, shares, src. XBRL dei (summed over classes of one filing) is used when
    the parsed cover has no count (early XBRL dei omits classes / has x1000 scale errors: CMCSA, GOOG, NWSA 2009-10;
    ETFC, QCOM, ORCL, HBAN, MXIM); hand overrides (key, accession, shares, asof, note) win."""
    c = cover.copy()
    c["asof"] = pd.to_datetime(c["asof"].replace("", np.nan), errors="coerce")
    c["override"] = False
    if overrides is not None and len(overrides):
        for r in overrides.itertuples():
            m = (c["key"] == r.key) & (c["accession"] == r.accession)
            c.loc[m, ["shares", "asof", "override"]] = [float(r.shares), pd.Timestamp(r.asof), True]
    c = c[c["shares"].notna() & (c["shares"] > 0)].copy()
    c["filed"] = pd.to_datetime(c["filed"])
    c["report_date"] = pd.to_datetime(c["report_date"].replace("", np.nan), errors="coerce")
    # a cover without a date: the filing date minus 7 days (covers are dated a few days to weeks before filing)
    c["asof"] = c["asof"].where(c["asof"].notna() & (c["asof"] <= c["filed"]) &
                                (c["asof"] >= c["filed"] - pd.Timedelta(days=200)), c["filed"] - pd.Timedelta(days=7))
    c = c.assign(src=np.where(c["override"], "override", "cover"))[["key", "accession", "form", "filed", "asof",
                                                                       "shares", "src"]]
    rows = [c]
    if dei is not None and len(dei):
        d = dei.copy()
        d["filed"] = pd.to_datetime(d["filed"])
        d["end"] = pd.to_datetime(d["end"])
        d = d[d["form"].isin(md.BASE_FORMS)]
        g = d.groupby(["key", "accn", "end"], as_index=False).agg(filed=("filed", "min"), form=("form", "first"),
                                                                   shares=("val", "sum"))
        # several 'end' dates in one filing: keep the latest
        g = g.sort_values("end").drop_duplicates(["key", "accn"], keep="last")
        g = g.rename(columns={"accn": "accession", "end": "asof"}).assign(src="dei")
        rows.append(g[["key", "accession", "form", "filed", "asof", "shares", "src"]])
    f = pd.concat(rows, ignore_index=True)
    f["pref"] = f["src"].map({"dei": 0, "cover": 1, "override": 2})   # dei only where the cover gave no count (OOS.2a)
    f = f.sort_values(["key", "accession", "pref"]).drop_duplicates(["key", "accession"], keep="last").drop(columns="pref")
    return f.sort_values(["key", "asof", "filed"]).reset_index(drop=True)


def _nearest_ratio(q: float) -> float | None:
    for r in SPLIT_RATIOS:
        if abs(q / r - 1) <= SPLIT_TOL:
            return r
    return None


def split_units(f: pd.DataFrame, prices_splits: dict | None = None, basis_dates: dict | None = None) -> pd.DataFrame:
    """Per key, F = the product of the splits after each share count's as-of date (up to the price reference date):
    from the price source's own split events when it has them (Yahoo, Tiingo), else inferred from jumps in the
    share-count sequence that match a standard split ratio (inferred_split = the ratio found, for review).
    Adds columns F, inferred_split, jump (ratio to the previous count)."""
    out = []
    for key, g in f.groupby("key", sort=False):
        g = g.sort_values(["asof", "filed"]).copy()
        q = (g["shares"] / g["shares"].shift(1)).to_numpy()
        g["jump"] = q
        known = (prices_splits or {}).get(key)
        if known is not None:
            ev = sorted(known)
            g["F"] = [float(np.prod([s for d, s in ev if d > a])) for a in g["asof"]]
            g["inferred_split"] = np.nan
        else:
            inf = np.full(len(g), np.nan)
            for i in range(1, len(g)):
                r = _nearest_ratio(q[i])
                if r is not None and not (0.8 < q[i] < 1.2):
                    inf[i] = r
            g["inferred_split"] = inf
            # F(a_i) = product of the inferred splits at later positions
            fac = np.ones(len(g))
            run = 1.0
            t_basis = (basis_dates or {}).get(key, pd.Timestamp("2100-01-01"))
            asofs = g["asof"].to_numpy()
            for i in range(len(g) - 1, -1, -1):
                fac[i] = run
                if np.isfinite(inf[i]) and asofs[i] <= np.datetime64(t_basis + pd.Timedelta(days=100)):
                    run *= inf[i]           # a split between count i-1 and count i, before the price basis date
            g["F"] = fac
        out.append(g)
    return pd.concat(out, ignore_index=True)


def nasdaq_on(cand: pd.DataFrame, s: pd.Timestamp) -> set:
    keep = set()
    for r in cand.itertuples():
        a = pd.Timestamp(r.nasdaq_from) if r.nasdaq_from else pd.Timestamp("1900-01-01")
        b = pd.Timestamp(r.nasdaq_to) if r.nasdaq_to else pd.Timestamp("2100-01-01")
        if a <= s <= b:
            keep.add(r.key)
    return keep


def signal_sessions(sessions: pd.DatetimeIndex) -> list:
    sig = last_session_of_each_month(sessions[sessions <= pd.Timestamp(END)])
    return [s for s in sig if s >= pd.Timestamp(FIRST_SIGNAL)]


# Data rule OOS.2a: companiesmarketcap.com's daily market cap for Veritas jumps from $31bn to $87bn in the week of
# 2000-03-03 (its 3:2 split) and disagrees with SEC shares x its own month-end raw price; Veritas is therefore ranked
# on SEC shares x CMC month-end close with the splits its 10-Q (filed 1999-11-12) and 10-K (filed 2000-03-30) state.
CMC_PRICE_RANKED = {"VRTS": ("veritas-technologies", [(pd.Timestamp("1999-07-09"), 2.0), (pd.Timestamp("1999-11-22"), 1.5),
                                                      (pd.Timestamp("2000-03-06"), 1.5)])}


def _cmc_price_mcap(key: str, s: pd.Timestamp, facts_key: pd.DataFrame) -> float:
    slug, splits = CMC_PRICE_RANKED[key]
    px = md.cmc_series(slug)["price"]
    px = px[px.index <= s]
    g = facts_key[(facts_key["filed"] < s) & (facts_key["filed"] >= s - pd.Timedelta(days=FRESH_DAYS))]
    if px.empty or g.empty or (s - px.index[-1]).days > 7:
        return np.nan
    x = g.sort_values(["asof", "filed"]).iloc[-1]
    f = float(np.prod([r for d, r in splits if x["asof"] < d <= s]))
    return float(x["shares"]) * f * float(px.iloc[-1])


def market_caps(cand: pd.DataFrame, facts: pd.DataFrame, panel: dict, signals: list) -> pd.DataFrame:
    """Rows (s, key, mcap, src, shares_asof, filed, basis) for every candidate that is Nasdaq-listed at s, has a
    price at s, and a share count filed strictly before s within FRESH_DAYS. CMC days use CMC's own market cap."""
    rows = []
    fk = {k: g.sort_values("filed") for k, g in facts.groupby("key")}
    for s in signals:
        on = nasdaq_on(cand, s)
        for key in on:
            p = panel.get(key)
            if p is None or s not in p.index:
                continue
            px = p.loc[s]
            if not np.isfinite(px["tr"]):
                continue
            if key in CMC_PRICE_RANKED and key in fk:
                v = _cmc_price_mcap(key, s, fk[key])
                if np.isfinite(v):
                    rows.append({"s": s, "key": key, "mcap": v, "src": "sec_x_cmc_close", "asof": pd.NaT,
                                 "filed": pd.NaT, "source": px["source"]})
                continue
            if np.isfinite(px.get("cmc_mcap", np.nan)):
                rows.append({"s": s, "key": key, "mcap": float(px["cmc_mcap"]), "src": "cmc_mcap", "asof": pd.NaT,
                             "filed": pd.NaT, "source": px["source"]})
                continue
            g = fk.get(key)
            if g is None:
                continue
            g = g[(g["filed"] < s) & (g["filed"] >= s - pd.Timedelta(days=FRESH_DAYS))]
            if g.empty or not np.isfinite(px["adj"]):
                continue
            x = g.sort_values(["asof", "filed"]).iloc[-1]            # the latest as-of date among usable filings
            v = float(x["shares"]) * float(x["F"]) * float(px["adj"])
            rows.append({"s": s, "key": key, "mcap": v, "src": f"sec_{x['src']}", "asof": x["asof"], "filed": x["filed"],
                         "source": px["source"]})
    return pd.DataFrame(rows)


def period_stats(nav: pd.Series, bench: pd.Series, a: str, b: str) -> dict | None:
    a_, b_ = pd.Timestamp(a), pd.Timestamp(b)
    before = nav.index[nav.index < a_]
    t0 = before[-1] if len(before) else nav.index[0]
    keep = (nav.index >= t0) & (nav.index <= b_) & bench.reindex(nav.index).notna().to_numpy()
    v, vb = nav[keep], bench.reindex(nav.index)[keep]
    if len(v) < 40:
        return None
    yrs = (v.index[-1] - v.index[0]).days / 365.25
    r, rb = v.pct_change().iloc[1:], vb.pct_change().iloc[1:]
    mx = _monthly(r) - _monthly(rb)
    if a_.day > 7:                                   # a window starting mid-month (1999-03-10): first full month on
        mx = mx.iloc[1:]
    return {"start": str(v.index[0].date()), "end": str(v.index[-1].date()),
            "cagr": (v.iloc[-1] / v.iloc[0]) ** (1 / yrs) - 1, "bench_cagr": (vb.iloc[-1] / vb.iloc[0]) ** (1 / yrs) - 1,
            "max_dd": max_drawdown(v), "bench_max_dd": max_drawdown(vb),
            "vol": float(r.std() * math.sqrt(252)), "bench_vol": float(rb.std() * math.sqrt(252)),
            "months": int(len(mx)), "mean_monthly_excess": float(mx.mean()),
            "t_monthly_excess": float(mx.mean() / mx.std(ddof=1) * math.sqrt(len(mx))) if mx.std() > 0 else np.nan}


def load_qqq() -> pd.DataFrame:
    q = parse_chart(QQQ_CHART, END)
    q["date"] = pd.to_datetime(q["date"])
    return q.set_index("date")


def load_oneq() -> pd.DataFrame:
    q = parse_chart(ONEQ_CHART, END)
    q["date"] = pd.to_datetime(q["date"])
    return q.set_index("date")


def build():
    LOCAL.mkdir(parents=True, exist_ok=True)
    cand = md.load_candidates()
    cal = md.yahoo_daily("MSFT")
    sessions = pd.DatetimeIndex(cal["date"][(cal["date"] >= START) & (cal["date"] <= END)])
    panel, src_rows, metas = {}, [], {}
    for r in cand.itertuples():
        p, meta = name_prices(r, sessions)
        metas[r.key] = meta
        if len(p):
            panel[r.key] = p
            for s_, g in p.groupby("source"):
                src_rows.append({"key": r.key, "source": s_, "first": str(g.index.min().date()),
                                 "last": str(g.index.max().date()), "days": int(len(g)), "G": meta["G"],
                                 "splits_from": "price source" if meta["splits"] is not None else "share counts"})
        else:
            src_rows.append({"key": r.key, "source": "NONE", "days": 0})
    cover = md.within_cik_spans(pd.read_csv(md.COVER_FACTS, dtype={"accession": str}), cand)
    dei = md.within_cik_spans(pd.read_csv(md.DEI_FACTS), cand) if md.DEI_FACTS.exists() else None
    ov_path = OUT / "sec_share_overrides.csv"
    ov = pd.read_csv(ov_path, dtype={"accession": str}) if ov_path.exists() else None
    facts = share_facts(cover, dei, ov)
    facts = split_units(facts, {k: m["splits"] for k, m in metas.items() if m["splits"] is not None},
                        {k: m["T_p"] for k, m in metas.items() if m.get("T_p") is not None})
    g = pd.Series({k: m["G"] for k, m in metas.items()})
    facts["F"] = facts["F"] * facts["key"].map(g).fillna(1.0).where(facts["key"].map(
        lambda k: metas.get(k, {}).get("splits") is None), 1.0)
    facts, fixes = fix_share_outliers(facts)
    fixes.to_csv(OUT / "sec_share_fixes.csv", index=False)
    print(f"share-count fixes: {len(fixes)} ({(fixes['fix'].str.startswith('split') ).sum() if len(fixes) else 0} split-side,"
          f" {(fixes['fix'].str.startswith('dropped')).sum() if len(fixes) else 0} dropped)")
    return cand, sessions, panel, pd.DataFrame(src_rows), facts, metas


# Cash consideration per share for cash deals (SEC merger filings; DELL adds the $0.13 special dividend).
TERMINAL_CASH = {"DELL": 13.75 + 0.13, "SUNW": 9.50, "SEBL": 10.66, "GENZ": 74.00, "PSFT": 26.50, "CHIR": 48.00,
                 "MEDI": 58.00, "BMET": 46.00, "INKT": 1.65, "BEAS": 19.375}
CUT_AFTER_NASDAQ_DAYS = 45      # prices after a name leaves Nasdaq are kept this long (held until the next rebalance)


def factor_at(facts: pd.DataFrame, key: str, t: pd.Timestamp) -> float:
    g = facts[(facts["key"] == key) & (facts["asof"] <= t)]
    return float(g.sort_values("asof")["F"].iloc[-1]) if len(g) else np.nan


def engine_panels(cand: pd.DataFrame, sessions: pd.DatetimeIndex, panel: dict, facts: pd.DataFrame):
    """idx (total-return level, held flat at the terminal value after the last row), close (a per-share price for
    the commission model), last_row (last priced session for names whose series ends before END) and the terminal
    log. A series ends at its last price, at most CUT_AFTER_NASDAQ_DAYS after the name left Nasdaq."""
    idx = pd.DataFrame(index=sessions, dtype=float)
    close = pd.DataFrame(index=sessions, dtype=float)
    last_row, log = {}, []
    for r in cand.itertuples():
        p = panel.get(r.key)
        if p is None or p.empty:
            continue
        stop = pd.Timestamp(END)
        if r.nasdaq_to:
            stop = min(stop, pd.Timestamp(r.nasdaq_to) + pd.Timedelta(days=CUT_AFTER_NASDAQ_DAYS))
        p = p[p.index <= stop]
        if p.empty:
            continue
        tr = p["tr"].reindex(sessions)
        lr = p.index.max()
        raw = p["close_raw"].copy()
        miss = raw.isna() & p["adj"].notna()
        if miss.any():
            fa = np.array([factor_at(facts, r.key, t) for t in p.index[miss]])
            raw[miss] = p.loc[miss, "adj"].to_numpy() * fa
        px = raw.reindex(sessions).ffill().fillna(50.0)
        ratio, how = 1.0, "series runs to the end"
        if lr < pd.Timestamp(END) - pd.Timedelta(days=5):
            last_raw = raw.loc[lr] if lr in raw.index else np.nan
            ev = r.end_event or "series_ends_no_event"
            if r.nasdaq_to and abs((lr - pd.Timestamp(r.nasdaq_to)).days) > 10 and ev != "moved_to_NYSE":
                ev = "series_ends_no_event"          # the price source stops before the corporate event
            if ev == "cash_deal" and r.key in TERMINAL_CASH and np.isfinite(last_raw) and last_raw > 0:
                ratio, how = TERMINAL_CASH[r.key] / last_raw, f"cash {TERMINAL_CASH[r.key]:.3f} per share / last close"
            elif ev == "delisted_no_deal" and not str(p.loc[lr, "source"]).startswith("cmc"):
                ratio, how = 1 + TERMINAL_D5, "no terminal value: D5 -55%"
            elif ev == "delisted_no_deal":
                ratio, how = 1.0, "last OTC close in the CMC path"
            else:
                how = {"stock_deal": "stock deal: last close", "moved_to_NYSE": "left Nasdaq: priced until the cut",
                       "cash_deal": "cash deal: last close (no raw close for the ratio)"}.get(ev, "series ends: last close")
            last_row[r.key] = lr
            after = tr.index > lr
            tr[after] = float(tr.loc[lr]) * ratio
            log.append({"key": r.key, "last_session": str(lr.date()), "event": ev, "terminal_ratio": ratio, "rule": how})
        idx[r.key] = tr.ffill()
        close[r.key] = px
    return idx, close, pd.Series(last_row, dtype="datetime64[ns]"), pd.DataFrame(log)


def rank_table(mcaps: pd.DataFrame, cand: pd.DataFrame, top: int = 20) -> pd.DataFrame:
    name = cand.set_index("key")["name"]
    rows = []
    for s, g in mcaps.groupby("s"):
        g = g.sort_values(["mcap", "key"], ascending=[False, True]).head(top)
        for k, r in enumerate(g.itertuples(), 1):
            rows.append({"s": s.date().isoformat(), "rank": k, "key": r.key, "name": name.get(r.key, r.key),
                         "mcap_bn": round(r.mcap / 1e9, 1), "src": r.src})
    return pd.DataFrame(rows)


SNAPSHOT_SIGNALS = ("1999-12-31", "2000-03-31", "2000-12-29", "2001-12-31", "2002-09-30", "2004-12-31",
                    "2007-12-31", "2009-02-27", "2011-12-30", "2013-06-28", "2013-11-29")


def fix_share_outliers(facts: pd.DataFrame, tol: float = 0.2) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Data rule (OOS.2a): in price-basis units (shares x F) a count that disagrees with both neighbours, while the
    neighbours agree with each other (within 15%), is an outlier. If the ratio is a standard split ratio the count was
    stated on the other side of a split (a cover dated at the period end but counted after the split, e.g. ORCL
    10-Q for 1999-12-31 filed 2000-01-14 already post-split): F is divided by the ratio. Otherwise (a parse error,
    e.g. QCOM 10-K 1998-11 '3,345,652,...') the count is dropped. Isolated pairs are handled the same way.
    Returns the corrected facts and a log of every change."""
    out, log = [], []
    for key, g in facts.groupby("key", sort=False):
        g = g.sort_values(["asof", "filed"]).reset_index(drop=True).copy()
        for width in (1, 2):
            u = (g["shares"] * g["F"]).to_numpy().astype(float).copy()
            n = len(g)
            drop = np.zeros(n, dtype=bool)
            for i in range(n - width + 1):
                block = list(range(i, i + width))
                lo, hi = i - 1, i + width
                edge = False
                if lo >= 0 and hi < n:
                    left, right = u[lo], u[hi]
                elif lo < 0 and hi + 1 < n:
                    left, right, edge = u[hi], u[hi + 1], True
                elif hi >= n and lo - 1 >= 0:
                    left, right, edge = u[lo - 1], u[lo], True
                else:
                    continue
                if abs(np.log(left / right)) > np.log(1.15):
                    continue
                ref = math.sqrt(left * right)
                ratios = [u[j] / ref for j in block]
                if not all(abs(np.log(r)) > np.log(1 + tol) for r in ratios):
                    continue
                if width == 2 and abs(np.log(ratios[0] / ratios[1])) > np.log(1.1):
                    continue
                if edge and not all(_nearest_ratio(r) in (2.0, 3.0, 1.5, 4.0, 0.5) for r in ratios):
                    continue        # at the first / last count only an exact split-side error is corrected
                for j, r in zip(block, ratios):
                    sr = _nearest_ratio(r)
                    if sr is not None:
                        g.loc[j, "F"] = g.loc[j, "F"] / sr
                        u[j] = u[j] / sr
                        how = f"split side: F / {sr:g}"
                    else:
                        drop[j] = True
                        how = "dropped (not a split ratio)"
                    log.append({"key": key, "accession": g.loc[j, "accession"], "form": g.loc[j, "form"],
                                "filed": g.loc[j, "filed"], "asof": g.loc[j, "asof"], "shares": g.loc[j, "shares"],
                                "ratio_to_neighbours": round(float(r), 4), "fix": how, "pass": width})
            g = g[~drop].reset_index(drop=True)
        out.append(g)
    return pd.concat(out, ignore_index=True), pd.DataFrame(log)
