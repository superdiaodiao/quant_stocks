"""B4 mega-cap lines M1, M2, M3, M6 (docs/research_ledger_megacap.md section 0; checklist section B4).

Nothing of the rule is reimplemented: the month-end candidates (``quant.strategies.megacap.candidates``), the
point-in-time market cap (``quant.data.market_cap.market_caps`` with the 0.2a defaults: shares x price, public float,
company lists, dollar-volume proxy, the 2.5x float and 10x dollar-volume cross-checks; predecessor share counts via
``successor_ciks`` / ``add_predecessor_facts``), the total-return momentum (``momentum_frames``), the targets
(``build_targets``) and the daily engine with the $10k account, 25% band and IBKR costs (``megacap.simulate``,
``buy_hold``) are the research functions.

Fresh monthly data reuse the S-MISP forward pipeline (``quant.observation.smisp``): its signal folder
``<cache>/smisp/s_<date>/`` holds the Nasdaq listing snapshot taken after the signal close, the universe base
(identity and flags of every Nasdaq common stock, frozen v2 identities first), the charts of every eligible name
(Alpaca first, Yahoo fallback, cut at the signal date) and the SEC companyfacts of the U300 names. The mega-cap signal
adds what its rules need and the S-MISP signal does not keep, without changing the S-MISP outputs:

1. the weekly dollar-volume ranks of the universe builder (``smisp.universe_ranks``) and the top 300 of them, which
   include financials (S-MISP drops them later, the mega-cap universe does not). The builder's dollar volume is the
   real close x the raw (unadjusted) volume; the charts hold split-adjusted volume, so the raw volume is rebuilt as
   volume x split-adjusted close / real close (``raw_volume``; B4 only, the S-MISP signal is unchanged);
2. longer charts of those 300 names (``LOOKBACK_DAYS`` = 760 calendar days, Alpaca first, Yahoo fallback;
   ``megacap_charts/``): the public-float cross-check of 0.2a carries the float's measurement date (up to about 22
   months before the signal) to the signal with the total-return index, which the S-MISP charts (460 days) do not
   reach;
3. SEC companyfacts of every candidate CIK (and of a predecessor CIK, ``successor_ciks``), filed strictly before the
   signal (``latest_fact_asof`` asserts it), read one CIK at a time. Facts with the same company, concept, period end
   and filing date (a 10-Q's quarter and year-to-date weighted averages) keep the shortest period (``share_frame``),
   so the choice does not depend on which other companies are read (B4 only; checklist B4.4);
4. the series start of an exchange mover: in the research data a security first listed on Nasdaq inside the window
   (PLTR 2024, WMT 2025) has prices only from about 108 days before its first listed week, so it has no 126-session
   momentum and no float carry for months; the forward charts reach years back. Each security's prices are cut at
   its first Nasdaq listing date less ``MOVER_LEAD_DAYS`` (``first_listed``: the frozen v2 weekly listing, else the
   first forward snapshot that lists it; committed in ``state/forward_observation/megacap/first_listed.csv``);
5. a check of the real close at s: a vendor's split-adjusted bars already contain a split that happens after the
   requested end, without the split event, so the rebuilt real close (and the market cap) would be off by the split
   ratio. Alpaca's raw close at s of every eligible name (``check_real_close``; local cache only) corrects such a
   chart by the snapped ratio (named in the meta).

Each signal is computed once, right after its month-end close, and frozen in
``state/forward_observation/megacap/m_<date>.csv`` (committed: ranks, ratios, ids and weights, no price levels; the
market caps themselves stay in the local cache ``megacap_full.csv``) with ``m_<date>_meta.json``; later runs reuse
it and never recompute it. The daily simulation re-downloads the charts of the target names whose month is not final
yet (``held/``, Alpaca first, Yahoo fallback; a name sold in a month that is final keeps its file); a name whose bars
stop is turned into cash at its last value (no forward terminal values, as the S-MISP line).
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")   # the universe base and the first listings are the frozen v2

import json  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.error import HTTPError  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant.data import market_cap as mcap  # noqa: E402
from quant.data import version as dv  # noqa: E402
from quant.data.guards import assert_window  # noqa: E402
from quant.data.market_cap import value_at  # noqa: E402
from quant.data.panel import make_index  # noqa: E402
from quant.observation import prices as fp  # noqa: E402
from quant.observation import smisp as fs  # noqa: E402
from quant.signals.technical import momentum_frames  # noqa: E402
from quant.strategies import megacap as mc  # noqa: E402
from quant.strategies import registry  # noqa: E402

RULES = tuple(r for r in mc.RULES if r.name in registry.B4_MEGACAP)
FIRST_SIGNAL = registry.B4_FIRST_SIGNAL
STATE = fp.STATE_DIR / "megacap"          # committed: frozen monthly signals (ranks, ratios, weights)
LOOKBACK_DAYS = 760
UNIVERSE_SIZE = 300                       # the weekly top 300 by 50-day dollar volume
KEEP_RANKS = 50                           # rows kept in the committed file (the rules use the top 20)
SIGNAL_COLS = ["s", "security_id", "ticker", "yahoo", "cik", "company", "dv50_rank", "mcap_rank", "mcap_rel",
               "mcap_src", "mom_m6", "mom_m12_1"] + [f"w_{r.name}" for r in RULES]
# exchange movers (fixed 2026-10-24, before any forward data): the research v2 series of a security first listed on
# Nasdaq inside the window starts 106-112 days before its first listed week for the movers of 2023-2026 (median 108.5;
# PLTR 109, WMT 106; 2016-2026: median 98)
MOVER_LEAD_DAYS = 108
FIRST_LISTED_NAME = "first_listed.csv"    # in the state folder (committed: ids, tickers and dates only)
FIRST_LISTED_FROM = "2024-01-01"          # an earlier first listing cannot cut the 760-day window of a 2026-10+ signal
RAW_CLOSE_FILE = "megacap_raw_close.csv"  # Alpaca raw close at s (local cache only: price levels)
REAL_CLOSE_TOL = 0.02                     # chart real close vs the raw close at s: beyond this, a missing split
SPLIT_TOL = 0.005                         # ... corrected only when the ratio is a split ratio n / m or m / n, m <= 4


def signal_path(s, state: Path = STATE) -> Path:
    return Path(state) / f"m_{fs.ydate(s)}.csv"


def load_signal(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"security_id": str, "ticker": str, "yahoo": str, "company": str, "cik": str},
                       parse_dates=["s"])


def _base(sdir: Path) -> pd.DataFrame:
    b = pd.read_csv(sdir / "universe_base.csv", dtype={"security_id": str, "ticker": str, "yahoo": str, "cik": str,
                                                       "multi_class_group": str, "source": str})
    b["eligible"] = b["eligible"].astype(str) == "True"
    return b


def charts_complete(sdir: Path) -> bool:
    """The S-MISP signal folder holds the universe base and every eligible chart (or a no-data record)."""
    if not (sdir / "universe_base.csv").exists():
        return False
    base = _base(sdir)
    ch = sdir / "charts"
    missing = [y for y in base.loc[base["eligible"], "yahoo"] if not (ch / f"{y}.json.gz").exists()]
    return not (set(missing) - fs.no_data_symbols(ch / "fetch_log.csv"))


# ======================================================================== share facts (one CIK at a time)

FACT_COLS = ["cik", "pri", "end", "filed", "val", "start"]


def fact_rows(cik, payload: dict | None, s) -> list:
    """Share-count and public-float rows (``market_cap.facts_from_payload``) of one companyfacts body filed strictly
    before s."""
    if not payload:
        return []
    day = fs.ydate(s)
    return [r for r in mcap.facts_from_payload(int(cik), payload.get("payload", payload)) if str(r[3]) < day]


def share_frame(rows: list) -> pd.DataFrame:
    """One fact per company, concept, period end and filing date: a 10-Q states the quarter's and the year to date's
    weighted average under the same end and filing date (40% of all facts are in such ties). The research's choice
    there followed an unstable sort, so it depended on the other companies in the frame; here the shortest period (the
    latest start) is kept, then the larger value (checklist B4.4; B4 only)."""
    f = pd.DataFrame(rows, columns=FACT_COLS)
    f["start"] = f["start"].fillna("").astype(str)
    f = f.sort_values(["cik", "pri", "end", "filed", "start", "val"], kind="mergesort")
    return f.drop_duplicates(["cik", "pri", "end", "filed"], keep="last").reset_index(drop=True)


def share_facts(payloads: dict, s) -> pd.DataFrame:
    """Share-count and public-float facts filed strictly before s, one per tie (``share_frame``)."""
    rows = []
    for cik, pl in payloads.items():
        rows += fact_rows(cik, pl, s)
    return share_frame(rows)


# ======================================================================== series starts (exchange movers)


def build_first_listed(path: Path, frozen: pd.DataFrame | None = None) -> pd.DataFrame:
    """The frozen base names first listed on or after FIRST_LISTED_FROM with their first week in the frozen v2 weekly
    listing (local v2 cache), written to ``path``."""
    if not dv.IS_V2 or not fs.LISTED.exists():   # (a RuntimeError: the signal waits, the run goes on)
        raise RuntimeError(f"{path.name} is missing and the frozen v2 weekly listing is not readable here")
    wl = pd.read_csv(fs.LISTED, dtype=str, usecols=["week_end", "security_id"])
    first = wl.groupby("security_id")["week_end"].min()
    frozen = fs.frozen_base() if frozen is None else frozen
    tick = dict(zip(frozen["security_id"], frozen["ticker"]))
    first = first[first.index.isin(set(tick)) & (first >= FIRST_LISTED_FROM)]
    out = pd.DataFrame({"security_id": first.index, "ticker": first.index.map(tick), "first_listed": first.to_numpy(),
                        "source": "v2_weekly_listed"})
    out = out.sort_values(["first_listed", "security_id"]).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    return out


def first_listed(state: Path = STATE) -> pd.DataFrame:
    p = Path(state) / FIRST_LISTED_NAME
    if p.exists():
        return pd.read_csv(p, dtype=str)
    return build_first_listed(p)


def update_first_listed(base: pd.DataFrame, s, state: Path = STATE) -> tuple[dict, list]:
    """({security_id: first listed date}, tickers recorded now): an eligible name of a later listing (source 'new',
    not in the frozen base) not yet in the file is recorded at s, the first snapshot that lists it."""
    p = Path(state) / FIRST_LISTED_NAME
    fl = first_listed(state)
    el = base[base["eligible"]]
    src = el["source"] if "source" in el else pd.Series("frozen", index=el.index)
    new = el[(src == "new") & ~el["security_id"].isin(set(fl["security_id"]))]
    if len(new):
        add = pd.DataFrame({"security_id": new["security_id"], "ticker": new["ticker"], "first_listed": fs.ydate(s),
                            "source": "forward_snapshot"})
        fl = pd.concat([fl, add], ignore_index=True).sort_values(["first_listed", "security_id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        fl.to_csv(p, index=False)
    return dict(zip(fl["security_id"], pd.to_datetime(fl["first_listed"]))), sorted(new["ticker"])


def series_starts(base: pd.DataFrame, listed: dict) -> dict:
    """{security_id: first price date counted} = first listing less MOVER_LEAD_DAYS, for the eligible names."""
    return {sid: listed[sid] - pd.Timedelta(days=MOVER_LEAD_DAYS) for sid in base.loc[base["eligible"], "security_id"]
            if sid in listed}


# ======================================================================== real close at s (a split after s)


def check_real_close(base: pd.DataFrame, s, sdir: Path, getters: dict | None = None, sleep=time.sleep) -> str:
    """Alpaca's raw close at s of every eligible name (one bar each, 200 names a request), saved to
    ``sdir/megacap_raw_close.csv`` (local cache: price levels). Returns a note; a missing key or a failed request
    leaves the signal unchecked (named in the meta), it does not stop it."""
    p = Path(sdir) / RAW_CLOSE_FILE
    if p.exists():
        return "checked"
    getters = getters or {}
    headers = getters["alpaca_headers"] if "alpaca_headers" in getters else fp.alpaca_headers()
    if headers is None:
        return "not checked: no Alpaca keys"
    el = base[base["eligible"]]
    tick = dict(zip(el["ticker"], el["security_id"]))
    try:
        bars = fp.alpaca_bars(sorted(tick), fs.ydate(s), fs.ydate(s), "raw", headers, getters.get("alpaca"), sleep)
    except Exception as exc:  # noqa: BLE001 - reported in the meta and the run notes
        code = getattr(exc, "code", "")
        return f"not checked: Alpaca {type(exc).__name__}{' ' + str(code) if code else ''}"
    day = pd.Timestamp(s)
    rows = [(tick[t], t, float(df["c"].iloc[-1])) for t, df in bars.items()
            if t in tick and len(df) and df.index[-1] == day]
    pd.DataFrame(rows, columns=["security_id", "ticker", "raw_close"]).to_csv(p, index=False)
    return "checked"


def load_raw_close(sdir: Path) -> dict | None:
    p = Path(sdir) / RAW_CLOSE_FILE
    if not p.exists():
        return None
    d = pd.read_csv(p, dtype={"security_id": str})
    return dict(zip(d["security_id"], d["raw_close"].astype(float)))


# ======================================================================== frames


def raw_volume(fr: dict) -> dict:
    """The frames with the raw (unadjusted) volume = split-adjusted volume x split-adjusted close / real close, the
    universe builder's dollar volume being real close x raw volume."""
    return {**fr, "volume": fr["volume"] * fr["close_adj"] / fr["close"]}


def split_ratio(x: float) -> float | None:
    """``x`` as a split ratio n / m or m / n with m <= 4 (2, 3, 3/2, 5/4, 10, 1/2, 1/10, ...) within SPLIT_TOL;
    None otherwise (a price difference that no split explains is named, not applied)."""
    for m in (1, 2, 3, 4):
        for y, inv in ((x, False), (1.0 / x, True)):
            n = round(y * m)
            if n >= 1 and n != m and abs(n / m / y - 1) < SPLIT_TOL:
                return m / n if inv else n / m
    return None


def adjust_frames(fr: dict, s, starts: dict | None = None, raw_close: dict | None = None) -> tuple[dict, dict, dict]:
    """(frames, {sid: cut date} of the names with prices before their series start, {sid: (ratio, applied)} of the
    names whose real close at s is off Alpaca's raw close by more than REAL_CLOSE_TOL). A cut name keeps its prices
    from its start, its first return there is empty (the research series start); a split ratio scales the real close
    of the whole window (the total-return index and the split-adjusted columns are unaffected)."""
    if not starts and not raw_close:
        return fr, {}, {}
    s = pd.Timestamp(s)
    close, close_adj, volume, tr = (fr[k].copy() for k in ("close", "close_adj", "volume", "tr"))
    fixed = {}
    if raw_close and s in close.index:
        for sid, raw in raw_close.items():
            if sid not in close.columns or not np.isfinite(close.at[s, sid]) or not raw > 0:
                continue
            x = raw / close.at[s, sid]
            if abs(x - 1) <= REAL_CLOSE_TOL:
                continue
            r = split_ratio(x)
            fixed[sid] = (r if r else round(x, 4), r is not None)
            if r:
                close[sid] *= r
    cut = {}
    for sid, b in (starts or {}).items():
        if sid not in close.columns:
            continue
        before = close.index < pd.Timestamp(b)
        if not close.loc[before, sid].notna().any():
            continue
        cut[sid] = pd.Timestamp(b)
        for df in (close, close_adj, volume, tr):
            df.loc[before, sid] = np.nan
        first = close[sid].first_valid_index()
        if first is not None:
            tr.loc[first, sid] = np.nan
    if not cut and not any(ok for _, ok in fixed.values()):
        return fr, cut, fixed
    out = {**fr, "close": close, "close_adj": close_adj, "volume": volume, "tr": tr, "idx": make_index(tr, close)}
    return out, cut, fixed


def universe(base: pd.DataFrame, chart_dir: Path, s, sessions_full, starts=None, raw_close=None) -> tuple:
    """(ranked eligible names at the universe week of s, the week end, the cuts, the real-close corrections) from the
    S-MISP charts."""
    s = pd.Timestamp(s)
    ses = pd.DatetimeIndex(sessions_full)
    grid = ses[(ses <= s) & (ses >= s - pd.Timedelta(days=fs.LOOKBACK_DAYS))]
    if grid[-1] != s:
        raise ValueError(f"no session on the signal date {s.date()}")
    el = base[base["eligible"]]
    fr = fs.price_frames({r.security_id: Path(chart_dir) / f"{r.yahoo}.json.gz" for r in el.itertuples()}, grid,
                         fs.ydate(s))
    fr, cut, fixed = adjust_frames(fr, s, starts, raw_close)
    w = fs.week_end_on_or_before(s, ses)
    return fs.universe_ranks(base, raw_volume(fr), w), w, cut, fixed


# ======================================================================== the signal


def compute_signal(s, base: pd.DataFrame, chart_dir: Path, long_dir: Path | None, facts_for, sessions_full,
                   lists: pd.DataFrame | None = None, alias: dict | None = None, starts: dict | None = None,
                   raw_close: dict | None = None) -> tuple[pd.DataFrame, dict, dict]:
    """(signal rows, meta, {rule: target list}) at s from local files. ``facts_for(ciks)`` returns {cik: payload}
    (asked one CIK at a time). ``long_dir``: the longer charts of the top-300 names (missing names use ``chart_dir``).
    ``alias`` {predecessor CIK: successor CIK} (``successor_ciks``). ``starts`` {security_id: first price date}
    (``series_starts``), ``raw_close`` {security_id: Alpaca raw close at s} (``check_real_close``). Every price frame
    is cut at s (asserted); every SEC fact used was filed strictly before s."""
    s = pd.Timestamp(s)
    ses = pd.DatetimeIndex(sessions_full)
    ranked, w, _, fixed_u = universe(base, chart_dir, s, ses, starts, raw_close)
    top = ranked[ranked["dv50_rank"] <= UNIVERSE_SIZE].copy()
    grid_l = ses[(ses <= s) & (ses >= s - pd.Timedelta(days=LOOKBACK_DAYS))]
    files, short = {}, []
    for r in top.itertuples():
        p = Path(long_dir) / f"{r.yahoo}.json.gz" if long_dir is not None else None
        if p is None or not p.exists():
            p = chart_dir / f"{r.yahoo}.json.gz"
            short.append(r.ticker)
        files[r.security_id] = p
    fl = fs.price_frames(files, grid_l, fs.ydate(s))
    fl, cut, fixed = adjust_frames(fl, s, starts, raw_close)
    assert_window(fl["close"].dropna(how="all").index, None, fs.ydate(s), "forward mega-cap prices")
    uni = top[["security_id", "ticker", "yahoo", "cik", "multi_class_group", "dv50_rank"]].copy()
    uni["week_end"] = pd.Timestamp(w)
    has = fl["close"].notna()
    last_row = has[::-1].idxmax()
    last_row[~has.any()] = pd.NaT
    cand = mc.candidates(uni, [s], fl["close"], last_row, fs.ydate(w), fs.ydate(s))
    ciks = sorted(int(c) for c in cand["cik"].dropna().unique())
    alias = {int(p): int(q) for p, q in (alias or {}).items() if int(q) in set(ciks)}
    rows, no_cf = [], []
    for c in sorted(set(ciks) | set(alias)):           # one companyfacts body in memory at a time
        pl = facts_for([c]).get(c)
        if not pl and c in set(ciks):
            no_cf.append(c)
        rows += fact_rows(c, pl, s)
        del pl
    facts = mcap.add_predecessor_facts(share_frame(rows), alias)
    lists = mcap.load_company_lists() if lists is None else lists
    lists = lists[lists["as_of"] <= s]
    dv50 = (fl["close_adj"] * fl["volume"]).rolling(50, min_periods=20).median()
    rk = mcap.market_caps(cand[["s", "security_id", "ticker", "cik", "dv50_rank", "universe_week"]], facts, lists,
                          fl["close"], fl["idx"], dv50)
    rk = rk.merge(cand[["security_id", "company", "yahoo"]], on="security_id", how="left")
    mom = momentum_frames(fl["idx"])
    qqq_close = pd.Series(np.nan, index=grid_l)               # (the M5 trend filter only; not an observed rule)
    targets = {r.name: mc.build_targets(r, rk, mom, qqq_close).get(s, []) for r in RULES}
    v = rk[rk["mcap"].notna()].sort_values(["mcap", "security_id"], ascending=[False, True]).reset_index(drop=True)
    v["mcap_rank"] = np.arange(1, len(v) + 1)
    v["mcap_rel"] = v["mcap"] / v["mcap"].iloc[0] if len(v) else np.nan
    for k in ("m6", "m12_1"):
        v[f"mom_{k}"] = value_at(mom[k], v["security_id"], v["s"])
    for name, tgt in targets.items():
        wts = {sid: wt for sid, wt, *_ in tgt}
        v[f"w_{name}"] = v["security_id"].map(wts).fillna(0.0)
    v["cik"] = v["cik"].astype("Int64").astype(str).replace("<NA>", "")
    out = v[v["mcap_rank"] <= KEEP_RANKS][SIGNAL_COLS].reset_index(drop=True)
    tick = {**dict(zip(base["security_id"], base["ticker"])), **dict(zip(v["security_id"], v["ticker"]))}
    top20 = v.head(20)
    fixed_all = {**fixed_u, **fixed}
    meta = {"s": fs.ydate(s), "week_end": fs.ydate(w), "eligible": int(base["eligible"].sum()),
            "ranked": int(ranked["dv50_rank"].notna().sum()), "top300": int(len(top)),
            "candidates": int(len(cand)), "with_mcap": int(len(v)),
            "top300_without_long_chart": sorted(short),
            "mcap_src_top20": top20["mcap_src"].value_counts().to_dict(),
            "top20": list(top20["ticker"]),
            "targets": {n: [tick.get(sid, sid) for sid, *_ in t] for n, t in targets.items()},
            "predecessor_ciks": {str(k): v_ for k, v_ in alias.items()},
            "candidates_without_companyfacts": len(no_cf),
            "series_start_cut": {tick.get(k, k): fs.ydate(b) for k, b in sorted(cut.items())},
            "real_close_split_corrected": {tick.get(k, k): r for k, (r, ok) in sorted(fixed_all.items()) if ok},
            "real_close_unexplained": {tick.get(k, k): r for k, (r, ok) in sorted(fixed_all.items()) if not ok}}
    return out, meta, {"full": v, "targets": targets}


def prepare_signal(s, sessions_full, smisp_base: Path = fs.BASE, state: Path = STATE, fetch: bool = True,
                   getters: dict | None = None, sleep=time.sleep, lists: pd.DataFrame | None = None,
                   use_alpaca: bool = True) -> pd.DataFrame | None:
    """The frozen mega-cap signal of s, computed once from the S-MISP signal folder of the same date (plus the
    real-close check, the longer charts and the candidates' companyfacts it fetches); None while that folder is
    incomplete."""
    out = signal_path(s, state)
    if out.exists():
        return load_signal(out)
    sdir = fs.sdir_of(s, smisp_base)
    if not charts_complete(sdir):
        print(f"mega-cap: the S-MISP inputs of {fs.ydate(s)} are not complete yet (computed with --fetch-smisp)")
        return None
    getters = getters or {}
    base = _base(sdir)
    long_dir = sdir / "megacap_charts"
    listed, recorded = update_first_listed(base, s, state)
    starts = series_starts(base, listed)
    check = check_real_close(base, s, sdir, getters, sleep) if fetch else (
        "checked" if (sdir / RAW_CLOSE_FILE).exists() else "not checked: offline")
    raw_close = load_raw_close(sdir)
    if fetch:
        s_ts = pd.Timestamp(s)
        ranked, *_ = universe(base, sdir / "charts", s_ts, sessions_full, starts, raw_close)
        top = ranked[ranked["dv50_rank"] <= UNIVERSE_SIZE]
        fs.fetch_charts(dict(zip(top["yahoo"], top["ticker"])), long_dir,
                        fs.ydate(s_ts - pd.Timedelta(days=LOOKBACK_DAYS)), fs.ydate(s_ts + pd.Timedelta(days=1)),
                        getters, sleep, use_alpaca)
    pace = {"last": 0.0}

    def facts_for(ciks):
        """{cik: companyfacts body or None}, from the signal folder; with ``fetch`` a missing one is asked from SEC
        (<= 2 requests/s, as ``smisp.fetch_sec_many``); nothing is kept in memory beyond the answer."""
        got = {}
        for c in ciks:
            path = sdir / "companyfacts" / f"CIK{int(c):010d}.json.gz"
            if path.exists() or not fetch:
                got[c] = fp.read_json(path) if path.exists() else None
                continue
            wait = max(0.0, 1.0 / fs.SEC_PER_SECOND - (time.time() - pace["last"]))
            pace["last"] = time.time() + wait
            try:
                got[c] = fs.fetch_json(fs.SEC_FACTS_URL.format(cik=int(c)), path, fs.sec_headers(), getters.get("sec"),
                                       sleep, wait)
            except HTTPError as exc:       # 404 is None (fetch_json); anything else stops the signal
                raise RuntimeError(f"SEC companyfacts CIK {c}: HTTP {exc.code}; stopped (re-run resumes)") from exc
        return got
    try:
        alias = mcap.successor_ciks(base)
    except (OSError, KeyError, ValueError):       # the frozen terminal file is not readable here
        alias = {}
    sig, meta, full = compute_signal(s, base, sdir / "charts", long_dir, facts_for, sessions_full, lists, alias,
                                     starts, raw_close)
    smeta = fs.signal_path(s, fs.STATE).with_name(f"s_{fs.ydate(s)}_meta.json")
    meta["listing_snapshot"] = json.loads(smeta.read_text()).get("listing_snapshot") if smeta.exists() else None
    meta["real_close_check"] = check
    meta["first_listed_recorded"] = recorded
    full["full"].to_csv(sdir / "megacap_full.csv", index=False)       # local copy with the market caps
    write_signal(s, sig, meta, state)
    print(f"mega-cap signal {fs.ydate(s)}: {meta['candidates']} candidates, top 20 {meta['top20']}; targets "
          + "; ".join(f"{k} {' '.join(v)}" for k, v in meta["targets"].items()) + f"; real close {check}")
    return load_signal(out)


def write_signal(s, sig: pd.DataFrame, meta: dict, state: Path = STATE) -> Path:
    """The committed files of one signal: ``m_<date>.csv`` (ranks, ratios, weights, ids) and ``_meta.json``."""
    out = signal_path(s, state)
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = meta.get("computed_utc") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = {**meta, "computed_utc": stamp}
    sig.to_csv(out, index=False, float_format="%.12g")
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, indent=1, default=str) + "\n")
    return out


# ======================================================================== daily simulation (research engine)

def targets_of(signals: dict, rule: str) -> dict:
    """{signal session: [(sid, weight, dv50_rank, nan, mcap_src)]} of one rule from the frozen files (the order of
    ``build_targets``: market-cap rank, M3 by momentum). A signal without a positive weight for the rule is left out:
    the research's ``build_targets`` has no entry there, so the engine keeps holding (no sale to cash)."""
    out = {}
    for s, sc in sorted(signals.items()):
        g = sc[sc[f"w_{rule}"] > 0]
        if g.empty:
            continue
        if rule == "M3":
            g = g.sort_values(["mom_m6", "security_id"], ascending=[False, True])
        else:
            g = g.sort_values("mcap_rank")
        out[pd.Timestamp(s)] = [(r.security_id, float(getattr(r, f"w_{rule}")), float(r.dv50_rank), np.nan,
                                 r.mcap_src) for r in g.itertuples()]
    return out


def held_names(signals: dict) -> dict:
    """{yahoo symbol: ticker} of every name in any observed rule's target list."""
    out = {}
    for sc in signals.values():
        m = (sc[[f"w_{r.name}" for r in RULES]] > 0).any(axis=1)
        out.update(dict(zip(sc.loc[m, "yahoo"], sc.loc[m, "ticker"])))
    return out


def names_to_refresh(signals: dict, held_dir: Path, sessions: pd.DatetimeIndex | None = None) -> dict:
    """{yahoo symbol: ticker} of the target names whose chart can still change a log row: a name of the latest
    signal, a name without a stored chart, and a name sold at a close whose month is not final yet. The month of the
    sale is final once its own month-end signal is frozen and the data hold a later session (the lines then hold a
    session of a later month). A name sold in a final month keeps its stored file (its numbers stay as recorded)."""
    allnames = held_names(signals)
    if sessions is None or not len(sessions):
        return allnames
    ses = pd.DatetimeIndex(sessions)
    sigs = sorted(pd.Timestamp(s) for s in signals)
    last_in = {}
    for s in sigs:
        sc = signals[s]
        m = (sc[[f"w_{r.name}" for r in RULES]] > 0).any(axis=1)
        for y in sc.loc[m, "yahoo"]:
            last_in[y] = s
    out = {}
    for y, t in allnames.items():
        later = [x for x in sigs if x > last_in[y]]
        if not later or not (Path(held_dir) / f"{y}.json.gz").exists():
            out[y] = t
            continue
        sale = ses[ses > later[0]]
        month_end = [x for x in sigs if len(sale) and x.to_period("M") == sale[0].to_period("M")]
        if not (month_end and ses[-1] > month_end[0]):
            out[y] = t                              # not sold yet, or sold in a month that is not final yet
    return out


def fetch_held(signals: dict, held_dir: Path, getters: dict | None = None, sleep=time.sleep,
               use_alpaca: bool = True, sessions: pd.DatetimeIndex | None = None) -> dict:
    """Re-download the charts of the target names that can still change a row (``names_to_refresh``; Alpaca first,
    Yahoo fallback); a failed download keeps the previous file."""
    syms = names_to_refresh(signals, held_dir, sessions)
    tmp = Path(held_dir) / "incoming"
    if tmp.exists():
        for p in tmp.glob("*.json.gz"):
            p.unlink()
    p1 = fs.ydate(pd.Timestamp(FIRST_SIGNAL) - pd.Timedelta(days=40))
    p2 = fs.ydate(pd.Timestamp(datetime.now(timezone.utc).date()) + pd.Timedelta(days=1))
    res = fs.fetch_charts(syms, tmp, p1, p2, getters, sleep, use_alpaca) if syms else {}
    for y in syms:
        p = tmp / f"{y}.json.gz"
        if p.exists():
            p.replace(Path(held_dir) / f"{y}.json.gz")
    return res


def frames_of(signals: dict, held_dir: Path, grid: pd.DatetimeIndex, end: str) -> dict:
    """Total-return index (flat after the last bar), real close and last bar of every target name on ``grid``."""
    ymap = {}
    for sc in signals.values():
        ymap.update(dict(zip(sc["security_id"], sc["yahoo"])))
    names = {sid for sc in signals.values() for sid in sc.loc[(sc[[f"w_{r.name}" for r in RULES]] > 0).any(axis=1),
                                                               "security_id"]}
    files = {sid: Path(held_dir) / f"{ymap[sid]}.json.gz" for sid in sorted(names)}
    fr = fs.price_frames(files, grid, end)
    has = fr["close"].notna()
    last_row = has[::-1].idxmax()
    last_row[~has.any()] = pd.NaT
    last_row[last_row >= grid[-1]] = pd.NaT          # still trading at the last session
    missing = sorted(set(files) - set(fr["close"].columns))
    return {"idx": fr["idx"].ffill(), "close": fr["close"], "last_row": last_row, "missing": missing,
            "files": files}


def run_rule(rule: str, signals: dict, legs, held_dir: Path, end: str, frames: dict | None = None) -> dict:
    """One rule from its first signal through ``end`` with the research engine (``megacap.simulate``), ONEQ and QQQ
    bought at the same close (``megacap.buy_hold``)."""
    tg = targets_of(signals, rule)
    if not tg:
        raise ValueError(f"{rule}: no target list in the frozen signals")
    ses = legs.sessions
    first = min(tg)
    grid = ses[(ses >= first) & (ses <= pd.Timestamp(end))]
    fr = frames or frames_of(signals, held_dir, grid, end)
    own = {x[0] for t in tg.values() for x in t}
    if own & set(fr["missing"]):                       # (a name of another rule only does not stop this one)
        raise RuntimeError(f"no chart for target name(s) {sorted(own & set(fr['missing']))} (run --fetch-smisp)")
    qqq_idx, qqq_close = legs.adj["QQQ"], legs.close["QQQ"]
    sim = mc.simulate(tg, grid, fr["idx"], fr["close"], fr["last_row"], qqq_idx, qqq_close, end=end)
    dates = sim["nav"].index
    oneq = mc.buy_hold(legs.adj["ONEQ"], legs.close["ONEQ"], dates, mc.ONEQ_HS)
    qqq = mc.buy_hold(qqq_idx, qqq_close, dates, mc.QQQ_HS)
    return {"sim": sim, "targets": tg, "nav": sim["nav"], "oneq_nav": oneq, "qqq_nav": qqq, "frames": fr}
