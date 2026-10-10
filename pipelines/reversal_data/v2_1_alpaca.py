"""Data version 2.1 of the reversal 2012-2026 data: the Alpaca SIP fetch and its entity confirmation (owner decision
2026-10-10, docs/reversal_2012_2026_data_plan.md section 0).

Data only. Nothing here computes signals, strategy or portfolio returns, rankings or spreads. A single-stock daily
total return is computed only to build and check each security's own series, as in step 9.

Targets: every row of the month-2 Tiingo plan (``research_cache/reversal_2012_2026/prefilter/tiingo_month2_plan.csv``,
374 rows) whose needed span reaches 2016-01-04 (Alpaca's free SIP history starts there). Each security is asked under
every ticker it holds in the window (``ticker_intervals.csv``), with ``asof`` set to a date inside that ticker
interval, so Alpaca's symbol mapping reads this company and never today's holder of a reused ticker (rule A1).

Steps (``PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_1_alpaca.py <step>``):
  targets   the request units, from local files only -> ``ALPACA/targets.csv``
  fetch     raw, split-adjusted and fully adjusted daily bars plus the corporate actions of every unit; each body is
            cached before it is parsed, every request is logged in this directory's own ledger
            (``raw_index.csv.gz``, ``quota_ledger.csv``); at most 150 requests a minute
  confirm   offline: the canonical record (S, D, tr) and the entity rules A1-A7 of plan section 0 (2026-10-10);
            accepted series -> ``ALPACA/series/{security_id}.csv.gz``, verdicts -> ``ALPACA/entity.csv`` and
            ``ALPACA/plan_rows.csv`` (one row per plan row)
  proposal  the November Tiingo plan proposal (what Alpaca cannot cover, plus verification samples)
  status    counts

The keys are read from ``.env.alpaca`` inside Python and never printed or logged (the ledger holds the URL only; the
keys travel in headers). Raw bodies and series stay local (never committed: they are vendor prices).
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common

ALPACA = common.V2_1_ALPACA
RAW = ALPACA / "raw"
SERIES = ALPACA / "series"
PLAN = common.V1_CACHE / "prefilter" / "tiingo_month2_plan.csv"
ROOT = Path(__file__).resolve().parents[1]
INPUTS_V2 = ROOT / "output" / "research_only" / "reversal_2012_2026" / "inputs_v2"
CACHE_V2 = common.MAIN_CHECKOUT / "research_cache" / "reversal_2012_2026_v2"
ENV = common.MAIN_CHECKOUT / ".env.alpaca"
BASE = "https://data.alpaca.markets"
ALPACA_START = pd.Timestamp("2016-01-04")
TODAY = pd.Timestamp("2026-10-09")          # the last complete session before the fetch (asof and end cap)
PRICE_END = pd.Timestamp("2026-08-31")      # plan D3
WARMUP_DAYS, AFTER_DAYS, DELIST_AFTER_DAYS = 110, 45, 30
ADJUSTMENTS = ("raw", "split", "all")
CA_TYPES = "forward_split,reverse_split,unit_split,stock_dividend,cash_dividend,spin_off,name_change"
PER_MINUTE = 150

# entity rules (plan section 0, 2026-10-10; written before any series was fetched)
CLASH_MAX_SHARE = 0.10        # A2
DELIST_END_SESSIONS = 10      # A4 (i)
POST_DELIST_SESSIONS = 5      # A4 (ii)
GAP_MAX_SESSIONS = 10         # A5
GAP_JUMP = 0.50               # A5
COVERAGE_MIN = 0.90           # A5
TOL_R = 0.005                 # A6
OVERLAP_MIN = 5               # A6
AGREE_SHARE_MIN = 0.95        # A6 (rule R8's share)
LEVEL_TOL = 0.02              # A6 / A7 (rule R9's level check)
SNAPSHOT_MIN = 3              # A7
SNAPSHOT_SHARE_MIN = 0.80     # A7
SPLIT_IMPLIED_MIN = 0.02      # a raw / split-adjusted ratio change this large is a split
SPLIT_MATCH = 0.02
DIV_IMPLIED_MIN = 0.002        # a dividend read from the bars: at least 0.2% of the prior close ...
DIV_PERSIST = 1e-4            # ... and the adjustment ratio holds its new level the next session (rounding noise reverts)


def log(message: str) -> None:
    print(f"[alpaca] {message}", flush=True)


# ======================================================================== pure helpers (tested)


def split_ratio(value: float, tolerance: float = 0.001) -> float | None:
    """``value`` as a ratio of two whole numbers up to 100, when it is one (as src/io/nasdaq_update._split_ratio)."""
    if not (np.isfinite(value) and value > 0):
        return None
    for denominator in range(1, 101):
        numerator = round(value * denominator)
        if 1 <= numerator <= 100 and abs(numerator / denominator / value - 1) <= tolerance:
            return numerator / denominator
    return None


def rounding_allowance(close: float, prev: float) -> float:
    """Cent rounding's room in a daily return below $1: 0.005 / C_t + 0.005 / C_{t-1} (v1's stored-vote rule)."""
    if not (close > 0 and prev > 0):
        return 0.0
    return 0.005 / close + 0.005 / prev


def filler_flags(close: np.ndarray, volume: np.ndarray, share: float = 0.05, lookback: int = 20,
                 min_history: int = 5) -> np.ndarray:
    """Rule R5 / A3, as the terminal step's ``filler_flags``: True for a row that repeats the previous close on no
    volume or on a volume below ``share`` of the median of the last ``lookback`` real sessions."""
    flags = np.zeros(len(close), dtype=bool)
    recent: list[float] = []
    for i in range(len(close)):
        v = 0.0 if not np.isfinite(volume[i]) else float(volume[i])
        repeat = i > 0 and abs(close[i] - close[i - 1]) <= 1e-6 * max(1.0, abs(close[i - 1]))
        if repeat and (v <= 0 or (len(recent) >= min_history and v < share * float(np.median(recent[-lookback:])))):
            flags[i] = True
        elif v > 0:
            recent.append(v)
    return flags


def interval_mask(sessions: pd.DatetimeIndex, intervals: pd.DataFrame) -> np.ndarray:
    """Sessions inside any of ``intervals`` (start .. end_next_absent, else end; the v2 ``_listing_required`` span)."""
    mask = np.zeros(len(sessions), dtype=bool)
    for r in intervals.itertuples():
        if not (isinstance(r.start, str) and r.start):
            continue
        end = r.end_next_absent if isinstance(r.end_next_absent, str) and r.end_next_absent else r.end
        if not (isinstance(end, str) and end):
            continue
        mask |= (sessions >= pd.Timestamp(r.start)) & (sessions <= pd.Timestamp(end))
    return mask


def bars_frame(body: bytes | None, symbol: str) -> pd.DataFrame:
    """date, close, volume from one /v2/stocks/bars body."""
    if not body:
        return pd.DataFrame(columns=["date", "close", "volume"])
    data = json.loads(body)
    rows = (data.get("bars") or {}).get(symbol) or []
    if not rows:
        return pd.DataFrame(columns=["date", "close", "volume"])
    f = pd.DataFrame({"date": pd.to_datetime([r["t"][:10] for r in rows]),
                      "close": [float(r["c"]) for r in rows], "volume": [float(r.get("v") or 0) for r in rows]})
    return f.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def corporate_actions(body: bytes | None) -> dict[str, list[dict]]:
    if not body:
        return {}
    return (json.loads(body).get("corporate_actions") or {})


def canonical_record(raw: pd.DataFrame, split_adj: pd.DataFrame, all_adj: pd.DataFrame, actions: dict,
                     sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Plan 4.1 canonical rows from Alpaca: date, close_raw, volume_raw, split, div, tr, flags (section 0 (d)).

    S: the split records (forward, reverse, unit, stock dividend: new_rate / old_rate) on their ex-date, checked
    against the change of raw / split-adjusted close; a change with no record that fits a whole-number ratio is used
    (``alpaca_split_from_bars``); a record the bars contradict blanks the day's return (``alpaca_split_mismatch``).
    D: cash-dividend records (``rate`` as paid), else the change of split-adjusted / fully adjusted close times the
    previous raw close (``alpaca_div_from_bars``) when that is at least 0.2% of the prior close and the ratio holds its
    new level the next session (the adjusted closes are rounded, so smaller or reverting changes are noise). A spin-off record blanks the day's return
    (``v21_spinoff_unvalued``). tr = (C x S + D) / C_prev - 1 across consecutive XNAS sessions only."""
    cols = ["date", "close_raw", "volume_raw", "split", "div", "tr", "flags"]
    if raw.empty:
        return pd.DataFrame(columns=cols)
    f = raw.rename(columns={"close": "close_raw", "volume": "volume_raw"}).copy()
    f = f[f["date"].isin(sessions) & (f["close_raw"] > 0)].reset_index(drop=True)
    if f.empty:
        return pd.DataFrame(columns=cols)
    s_adj = split_adj.set_index("date")["close"] if len(split_adj) else pd.Series(dtype=float)
    a_adj = all_adj.set_index("date")["close"] if len(all_adj) else pd.Series(dtype=float)
    f["c_split"] = f["date"].map(s_adj)
    f["c_all"] = f["date"].map(a_adj)
    split_rec: dict[pd.Timestamp, float] = {}
    for kind in ("forward_splits", "reverse_splits", "unit_splits", "stock_dividends"):
        for a in actions.get(kind, []) or []:
            ex = a.get("ex_date")
            new, old = a.get("new_rate"), a.get("old_rate")
            if kind == "stock_dividends" and a.get("rate") is not None:
                new, old = 1.0 + float(a["rate"]), 1.0
            if ex and new and old:
                d = pd.Timestamp(ex)
                split_rec[d] = split_rec.get(d, 1.0) * float(new) / float(old)
    div_rec: dict[pd.Timestamp, float] = {}
    for a in actions.get("cash_dividends", []) or []:
        if a.get("ex_date") and a.get("rate") is not None:
            d = pd.Timestamp(a["ex_date"])
            div_rec[d] = div_rec.get(d, 0.0) + float(a["rate"])
    spin = {pd.Timestamp(a["ex_date"]) for a in (actions.get("spin_offs", []) or []) if a.get("ex_date")}
    pos = pd.Series(np.arange(len(sessions)), index=sessions)
    f["pos"] = f["date"].map(pos).astype(int)
    consecutive = (f["pos"].diff() == 1).to_numpy()
    ratio_s = (f["close_raw"] / f["c_split"]).to_numpy()
    ratio_d = (f["c_split"] / f["c_all"]).to_numpy()
    splits, divs, trs, flags = [], [], [], []
    for i in range(len(f)):
        d = f.at[i, "date"]
        fl = []
        implied = ratio_s[i - 1] / ratio_s[i] if i > 0 and consecutive[i] else np.nan
        S = split_rec.get(d, 1.0)
        if d in split_rec:
            if np.isfinite(implied) and abs(implied / S - 1) > SPLIT_MATCH:
                fl.append("alpaca_split_mismatch")
        elif np.isfinite(implied) and abs(implied - 1) > SPLIT_IMPLIED_MIN:
            r = split_ratio(implied, tolerance=0.01)
            if r is not None:
                S = r
                fl.append("alpaca_split_from_bars")
            else:
                fl.append("alpaca_split_mismatch")
        D = div_rec.get(d, 0.0)
        if d not in div_rec and i > 0 and consecutive[i] and np.isfinite(ratio_d[i]) and np.isfinite(ratio_d[i - 1]):
            # dividend adjustment multiplies earlier closes by (1 - D / C_prev): ratio_d falls by that factor
            q = ratio_d[i] / ratio_d[i - 1]
            holds = i + 1 >= len(f) or not np.isfinite(ratio_d[i + 1]) or abs(ratio_d[i + 1] / ratio_d[i] - 1) <= DIV_PERSIST
            if q < 1 - DIV_IMPLIED_MIN and (1 - q) < 0.5 and holds:
                D = (1 - q) * float(f.at[i - 1, "close_raw"]) * S
                fl.append("alpaca_div_from_bars")
        tr = np.nan
        if i > 0 and consecutive[i]:
            tr = (float(f.at[i, "close_raw"]) * S + D) / float(f.at[i - 1, "close_raw"]) - 1.0
        if d in spin:
            fl.append("v21_spinoff_unvalued")
            tr = np.nan
        if "alpaca_split_mismatch" in fl:
            tr = np.nan
        splits.append(S)
        divs.append(D)
        trs.append(tr)
        flags.append(";".join(fl))
    f["split"], f["div"], f["tr"], f["flags"] = splits, divs, trs, flags
    return f[cols]


def overlap_returns(alp: pd.DataFrame, other: pd.DataFrame) -> dict:
    """A6: Alpaca's tr against a second source's tr on the same dates (both finite), plus the raw level ratio.
    Returns n (return days), agree (count within 0.5% plus cent rounding), level_n, level_median (|ratio - 1|)."""
    out = {"n": 0, "agree": 0, "level_n": 0, "level_median": np.nan}
    if alp.empty or other.empty:
        return out
    a = alp.set_index("date")
    o = other.drop_duplicates("date", keep="first").set_index("date")
    common_days = a.index.intersection(o.index)
    if not len(common_days):
        return out
    lv = (a.loc[common_days, "close_raw"] / o.loc[common_days, "close_raw"] - 1.0).abs()
    lv = lv[np.isfinite(lv)]
    out["level_n"] = int(len(lv))
    out["level_median"] = float(lv.median()) if len(lv) else np.nan
    ta, to = a.loc[common_days, "tr"].astype(float), o.loc[common_days, "tr"].astype(float)
    ok = np.isfinite(ta) & np.isfinite(to)
    days = common_days[ok.to_numpy()]
    if not len(days):
        return out
    prev = a["close_raw"].shift(1).reindex(days)
    allow = np.array([rounding_allowance(c, p) for c, p in zip(a.loc[days, "close_raw"], prev)])
    diff = (ta[ok] - to[ok]).abs().to_numpy()
    out["n"] = int(len(days))
    out["agree"] = int((diff <= TOL_R + allow).sum())
    return out


def snapshot_check(alp: pd.DataFrame, snaps: pd.DataFrame, sessions: pd.DatetimeIndex) -> dict:
    """A7: Nasdaq company-list ``last_sale`` against Alpaca's raw close on ``as_of_session`` or an adjacent session."""
    out = {"n": 0, "hits": 0}
    if alp.empty or snaps.empty:
        return out
    a = alp.set_index("date")["close_raw"]
    lo, hi = a.index.min(), a.index.max()
    for r in snaps.itertuples():
        try:
            day, last = pd.Timestamp(r.as_of_session), float(r.last_sale)
        except (TypeError, ValueError):
            continue
        if not (last > 0) or day < lo or day > hi:
            continue
        k = sessions.searchsorted(day)
        near = [sessions[j] for j in (k - 1, k, k + 1) if 0 <= j < len(sessions)]
        vals = [a[d] for d in near if d in a.index]
        if not vals:
            continue
        out["n"] += 1
        if any(abs(v / last - 1) <= LEVEL_TOL for v in vals):
            out["hits"] += 1
    return out


def continuity(dates: pd.DatetimeIndex, closes: np.ndarray, splits: np.ndarray, listed: pd.DatetimeIndex) -> dict:
    """A5 on the kept rows: coverage of the listed sessions between the first and last kept row, the longest run of
    missing listed sessions, and the largest split-adjusted raw-close jump across a gap of more than one session."""
    out = {"coverage": np.nan, "max_gap": 0, "max_gap_jump": 0.0}
    if not len(dates):
        return out
    span = listed[(listed >= dates.min()) & (listed <= dates.max())]
    have = pd.DatetimeIndex(dates)
    out["coverage"] = float(np.isin(span, have).mean()) if len(span) else np.nan
    pos = span.get_indexer(have)
    ok = pos >= 0
    p = pos[ok]
    c = np.asarray(closes, dtype=float)[ok]
    s = np.asarray(splits, dtype=float)[ok]
    if len(p) > 1:
        gaps = np.diff(p) - 1
        out["max_gap"] = int(gaps.max())
        for k in np.flatnonzero(gaps >= 1):
            jump = abs(c[k + 1] * s[k + 1] / c[k] - 1.0) if c[k] > 0 else 0.0
            out["max_gap_jump"] = max(out["max_gap_jump"], float(jump))
    return out


def judge(facts: dict, amended: bool = True) -> tuple[str, list[str]]:
    """The verdict of plan section 0 (c) from one unit's (or security's) facts: ('accepted' | 'ambiguous' |
    'no_bars', failed rules). Facts keys: kept_rows, clash_share, delist_in_window, delist_end_sessions,
    listing_end_sessions, post_delist_real, transfer_or_successor, coverage, max_gap, max_gap_jump, a6_n, a6_agree,
    a6_level_n, a6_level_median, a7_n, a7_hits.

    ``amended`` (default) applies A4 (i) as amended on 2026-10-10 after the first confirm run (plan section 0 (c)):
    the last real bar may also lie within 10 sessions of the end of the security's own Nasdaq listing in the company
    lists (``listing_end_sessions``), because ``delist_date`` is the Form 25 effective date, which follows the last
    trading day by weeks when Nasdaq suspends trading first (EVLO: last trade 2023-12-11, last listed 2023-11-28 ..
    2023-12-15, Form 25 effective 2024-01-06). ``amended=False`` gives the verdict of the rule as first written."""
    if not facts.get("kept_rows"):
        return "no_bars", []
    failed = []
    if facts.get("clash_share", 0.0) > CLASH_MAX_SHARE:
        failed.append("A2_ticker_clash")
    if facts.get("delist_in_window"):
        end = facts.get("delist_end_sessions")
        near_delist = end is not None and end <= DELIST_END_SESSIONS
        lend = facts.get("listing_end_sessions")
        near_listing_end = amended and lend is not None and lend <= DELIST_END_SESSIONS
        if not (near_delist or near_listing_end):
            failed.append("A4_ends_early")
        if facts.get("post_delist_real", 0) > 0 and not facts.get("transfer_or_successor"):
            failed.append("A4_bars_after_delisting")
    cov = facts.get("coverage")
    if cov is not None and np.isfinite(cov) and cov < COVERAGE_MIN:
        failed.append("A5_coverage")
    if facts.get("max_gap", 0) > GAP_MAX_SESSIONS:
        failed.append("A5_gap")
    if facts.get("max_gap_jump", 0.0) > GAP_JUMP:
        failed.append("A5_gap_jump")
    n, lvl_n, lvl = facts.get("a6_n", 0), facts.get("a6_level_n", 0), facts.get("a6_level_median", np.nan)
    if n >= OVERLAP_MIN and facts.get("a6_agree", 0) / n < AGREE_SHARE_MIN:
        failed.append("A6_returns")
    if lvl_n >= 1 and np.isfinite(lvl) and lvl > LEVEL_TOL:
        failed.append("A6_level")
    if facts.get("a7_n", 0) >= SNAPSHOT_MIN and facts.get("a7_hits", 0) / facts["a7_n"] < SNAPSHOT_SHARE_MIN:
        failed.append("A7_last_sale")
    return ("ambiguous" if failed else "accepted"), failed


def second_source_label(facts: dict) -> str:
    parts = []
    if facts.get("a6_n", 0) >= OVERLAP_MIN:
        parts.append("returns")
    elif facts.get("a6_level_n", 0) >= 1:
        parts.append("levels")
    if facts.get("a7_n", 0) >= SNAPSHOT_MIN:
        parts.append("last_sale")
    return "confirmed_" + "+".join(parts) if parts else "confirmed_dates_only"


# ======================================================================== local inputs


def xnas_sessions() -> pd.DatetimeIndex:
    import exchange_calendars as xcals

    cal = xcals.get_calendar("XNAS", start="2010-01-04", end="2026-12-31")
    return pd.DatetimeIndex(cal.sessions).tz_localize(None).normalize()


def read_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    plan = pd.read_csv(PLAN, dtype=str, keep_default_na=False)
    master = pd.read_csv(INPUTS_V2 / "security_master.csv", dtype=str, keep_default_na=False)
    intervals = pd.read_csv(INPUTS_V2 / "ticker_intervals.csv", dtype=str, keep_default_na=False)
    return plan, master, intervals


def window_of(row: dict, delist: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    start = max(ALPACA_START, pd.Timestamp(row["needed_start"]) - pd.Timedelta(days=WARMUP_DAYS))
    end = pd.Timestamp(row["needed_end"]) + pd.Timedelta(days=AFTER_DAYS)
    if delist:
        end = max(end, pd.Timestamp(delist) + pd.Timedelta(days=DELIST_AFTER_DAYS))
    end = min(end, TODAY)
    if pd.Timestamp(row["needed_end"]) < ALPACA_START or end < start:
        return None
    return start, end


def build_targets() -> pd.DataFrame:
    """One request unit per (security, ticker held in the window): symbol, asof (inside the ticker's interval),
    start, end. Plan rows of one security are merged into one window."""
    plan, master, intervals = read_inputs()
    m = master.set_index("security_id")
    units = []
    for sid, rows in plan.groupby("security_id", sort=False):
        delist = m.at[sid, "delist_date"] if sid in m.index else ""
        wins = [w for w in (window_of(r, delist) for r in rows.to_dict("records")) if w is not None]
        if not wins:
            continue
        start, end = min(w[0] for w in wins), max(w[1] for w in wins)
        iv = intervals[intervals["security_id"] == sid]
        for ticker, g in iv.groupby("ticker"):
            spans = []
            for r in g.itertuples():
                if not r.start:
                    continue
                e = r.end_next_absent or r.end
                if not e:
                    continue
                if pd.Timestamp(r.start) <= end and pd.Timestamp(e) >= start:
                    spans.append((pd.Timestamp(r.start), pd.Timestamp(r.end or e)))
            if not spans:
                continue
            asof = min(max(s[1] for s in spans), TODAY)
            units.append({"security_id": sid, "symbol": ticker, "asof": asof.strftime("%Y-%m-%d"),
                          "start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d"),
                          "plan_rows": len(rows), "groups": " ".join(sorted(set(rows["group"])))})
    out = pd.DataFrame(units)
    common.atomic_write(ALPACA / "targets.csv", out.to_csv(index=False).encode())
    log(f"targets: {len(out)} units, {out['security_id'].nunique() if len(out) else 0} securities")
    return out


# ======================================================================== fetch


def _headers() -> dict:
    return {"APCA-API-KEY-ID": common.read_env_key(ENV, "ALPACA_API_KEY_ID"),
            "APCA-API-SECRET-KEY": common.read_env_key(ENV, "ALPACA_API_SECRET_KEY"), "Accept": "application/json"}


def _safe(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)


def bars_path(u: dict, adjustment: str, page: int = 0) -> Path:
    return RAW / u["security_id"] / (f"{_safe(u['symbol'])}__{adjustment}__{u['start']}_{u['end']}__asof{u['asof']}"
                                     + (f"__p{page}" if page else "") + ".json.gz")


def ca_path(u: dict, page: int = 0) -> Path:
    return RAW / u["security_id"] / (f"{_safe(u['symbol'])}__ca__{u['start']}_{u['end']}"
                                     + (f"__p{page}" if page else "") + ".json.gz")


def _get_pages(url_base: str, params: dict, path_of, headers: dict, limiter, symbol: str) -> list[bytes]:
    out, page, token = [], 0, None
    while True:
        q = dict(params)
        if token:
            q["page_token"] = token
        body = common.cached_get(f"{url_base}?{urlencode(q)}", path_of(page), source="alpaca", headers=headers,
                                 limiter=limiter, symbol=symbol)
        out.append(body)
        token = json.loads(body).get("next_page_token")
        if not token:
            return out
        page += 1


def fetch(units: pd.DataFrame) -> None:
    common.RAW_INDEX, common.QUOTA_LEDGER = ALPACA / "raw_index.csv.gz", ALPACA / "quota_ledger.csv"
    headers = _headers()
    limiter = common.SlidingWindowLimiter({60: PER_MINUTE})

    def one(u: dict) -> str:
        for adj in ADJUSTMENTS:
            params = {"symbols": u["symbol"], "timeframe": "1Day", "start": u["start"], "end": u["end"],
                      "adjustment": adj, "feed": "sip", "limit": 10000, "asof": u["asof"]}
            _get_pages(f"{BASE}/v2/stocks/bars", params, lambda p, a=adj: bars_path(u, a, p), headers, limiter,
                       u["symbol"])
        params = {"symbols": u["symbol"], "types": CA_TYPES, "start": u["start"], "end": u["end"], "limit": 1000}
        _get_pages(f"{BASE}/v1/corporate-actions", params, lambda p: ca_path(u, p), headers, limiter, u["symbol"])
        return "ok"

    records = units.to_dict("records")
    results = common.parallel_map(one, records, workers=4)
    bad = [(r["security_id"], r["symbol"], type(x).__name__) for r, x in zip(records, results) if isinstance(x, Exception)]
    log(f"fetch: {len(records) - len(bad)} units ok, {len(bad)} failed {bad[:10]}")
    (ALPACA / "fetch_failures.json").write_text(json.dumps(bad, indent=1) + "\n")


def _read_body(path: Path) -> bytes | None:
    return gzip.decompress(path.read_bytes()) if path.exists() else None


def unit_bars(u: dict, adjustment: str) -> pd.DataFrame:
    frames, page = [], 0
    while True:
        body = _read_body(bars_path(u, adjustment, page))
        if body is None:
            break
        frames.append(bars_frame(body, u["symbol"]))
        if not json.loads(body).get("next_page_token"):
            break
        page += 1
    frames = [f for f in frames if len(f)]
    if not frames:
        return pd.DataFrame(columns=["date", "close", "volume"])
    return pd.concat(frames).drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def unit_actions(u: dict) -> dict:
    out: dict[str, list] = {}
    page = 0
    while True:
        body = _read_body(ca_path(u, page))
        if body is None:
            break
        for k, v in corporate_actions(body).items():
            out.setdefault(k, []).extend(v or [])
        if not json.loads(body).get("next_page_token"):
            break
        page += 1
    return out


# ======================================================================== confirm


def second_sources(sid: str, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """The v2 canonical rows of ``sid`` (Tiingo, Yahoo, WIKI, archive) with tr only across consecutive sessions, and
    the archived Yahoo capture rows on days the panel lacks: date, close_raw, tr, src."""
    frames = []
    p = CACHE_V2 / "prices" / f"{sid}.csv"
    if p.exists():
        c = pd.read_csv(p, usecols=["date", "close_raw", "tr", "src_primary"])
        c["date"] = pd.to_datetime(c["date"])
        pos = c["date"].map(pd.Series(np.arange(len(sessions)), index=sessions))
        c.loc[~(pos.diff() == 1), "tr"] = np.nan
        frames.append(c.rename(columns={"src_primary": "src"})[["date", "close_raw", "tr", "src"]])
    a = common.V2_FILL / "series" / "archive" / f"{sid}.csv.gz"
    if a.exists():
        from scripts.reversal_data_v2_fill import capture_record
        rows = pd.read_csv(a)
        if len(rows):
            rows = rows[~rows["otc"].astype(str).str.lower().eq("true")]
            rec = capture_record(rows, sessions)
            have = set(frames[0]["date"]) if frames else set()
            rec = rec[~rec["date"].isin(have)]
            frames.append(rec.assign(src="archive_capture")[["date", "close_raw", "tr", "src"]])
    if not frames:
        return pd.DataFrame(columns=["date", "close_raw", "tr", "src"])
    return pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)


def confirm() -> None:
    units = pd.read_csv(ALPACA / "targets.csv", dtype=str, keep_default_na=False)
    plan, master, intervals = read_inputs()
    m = master.set_index("security_id")
    sessions = xnas_sessions()
    snaps_all = pd.read_csv(CACHE_V2 / "prefilter" / "lists.csv.gz", dtype=str, keep_default_na=False)
    snaps_all = snaps_all[snaps_all["security_id"].isin(set(units["security_id"]))]
    SERIES.mkdir(parents=True, exist_ok=True)
    verdicts, unit_rows = [], []
    for sid, us in units.groupby("security_id", sort=False):
        delist = m.at[sid, "delist_date"] if sid in m.index else ""
        delist_ts = pd.Timestamp(delist) if delist else None
        transfer = bool(sid in m.index and (m.at[sid, "transfer_date"] or m.at[sid, "successor_security_id"]))
        parts, facts_all = [], []
        for u in us.to_dict("records"):
            raw, sp, al = unit_bars(u, "raw"), unit_bars(u, "split"), unit_bars(u, "all")
            acts = unit_actions(u)
            f = {"security_id": sid, "symbol": u["symbol"], "asof": u["asof"], "raw_rows": int(len(raw))}
            own = intervals[(intervals["security_id"] == sid) & (intervals["ticker"] == u["symbol"])]
            listed_mask = interval_mask(sessions, own)
            if delist_ts is not None:
                listed_mask &= sessions < delist_ts
            listed = sessions[listed_mask]
            # A3 filler rows on the whole answer (before clipping), so a post-delisting filler run is not a trade
            raw = raw[raw["date"].isin(sessions)].reset_index(drop=True)
            fill = filler_flags(raw["close"].to_numpy(float), raw["volume"].to_numpy(float)) if len(raw) else np.array([], bool)
            real = raw[~fill]
            f["filler_rows"] = int(fill.sum())
            # A4 delisting, on the real bars of the whole answer
            f["delist_in_window"] = bool(delist_ts is not None and pd.Timestamp(u["start"]) <= delist_ts <= pd.Timestamp(u["end"]))
            if f["delist_in_window"]:
                before = real[real["date"] < delist_ts]
                last_expected = sessions[sessions.searchsorted(delist_ts, side="left") - 1]
                f["delist_end_sessions"] = (int(sessions.searchsorted(last_expected) - sessions.searchsorted(before["date"].max()))
                                            if len(before) else None)
                # A4 (i) as amended: the end of this security's own Nasdaq listing in the company lists (the latest
                # interval end on or before the delisting), against the last real bar before the delisting
                nas = intervals[(intervals["security_id"] == sid) & (intervals["exchange"] == "NASDAQ")
                                & intervals["end"].astype(bool)]
                ends_before = pd.to_datetime(nas["end"])
                ends_before = ends_before[ends_before <= delist_ts]
                if len(before) and len(ends_before):
                    e = ends_before.max()
                    last_bar = before["date"].max()
                    f["listing_end_sessions"] = max(0, int(sessions.searchsorted(e) - sessions.searchsorted(last_bar)))
                else:
                    f["listing_end_sessions"] = None
                cut = sessions[min(sessions.searchsorted(delist_ts, side="left") + POST_DELIST_SESSIONS, len(sessions) - 1)]
                f["post_delist_real"] = int((real["date"] > cut).sum())
                f["transfer_or_successor"] = transfer
            # A1 clip to this ticker's own listing
            kept = real[real["date"].isin(listed)]
            # A2 ticker clash with another CIK's interval of the same ticker
            others = intervals[(intervals["ticker"] == u["symbol"]) & (intervals["security_id"] != sid)]
            clash_mask = interval_mask(sessions, others) if len(others) else np.zeros(len(sessions), bool)
            clash_days = set(sessions[clash_mask])
            clash = kept["date"].isin(clash_days)
            f["clash_share"] = float(clash.mean()) if len(kept) else 0.0
            kept = kept[~clash]
            f["kept_rows"] = int(len(kept))
            rec = canonical_record(kept[["date", "close", "volume"]], sp, al, acts, sessions)
            rec["ticker"] = u["symbol"]
            parts.append(rec)
            facts_all.append(f)
        # the security's series: units joined (a date held under two tickers keeps the later asof's row)
        series = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) else \
            pd.DataFrame(columns=["date", "close_raw", "volume_raw", "split", "div", "tr", "flags", "ticker"])
        series = series.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
        if len(series):
            # returns across a junction of two units: recompute across consecutive sessions on the joined series
            pos = series["date"].map(pd.Series(np.arange(len(sessions)), index=sessions))
            prev = series["close_raw"].shift(1)
            recompute = (pos.diff() == 1) & series["tr"].isna() & ~series["flags"].str.contains("unvalued|mismatch")
            series.loc[recompute, "tr"] = (series["close_raw"] * series["split"] + series["div"]) / prev - 1.0
            series.loc[~(pos.diff() == 1), "tr"] = np.nan
        listed_all = sessions[interval_mask(sessions, intervals[intervals["security_id"] == sid])
                              & ((sessions < delist_ts) if delist_ts is not None else True)]
        cont = continuity(pd.DatetimeIndex(series["date"]), series["close_raw"].to_numpy(float),
                          series["split"].to_numpy(float), listed_all)
        a6 = overlap_returns(series, second_sources(sid, sessions))
        a7 = snapshot_check(series, snaps_all[snaps_all["security_id"] == sid], sessions)
        facts = {"security_id": sid, "units": len(facts_all), "symbols": " ".join(f["symbol"] for f in facts_all),
                 "kept_rows": int(len(series)),
                 "raw_rows": int(sum(f["raw_rows"] for f in facts_all)),
                 "filler_rows": int(sum(f["filler_rows"] for f in facts_all)),
                 "clash_share": max([f["clash_share"] for f in facts_all] + [0.0]),
                 "delist_date": delist,
                 "delist_in_window": any(f.get("delist_in_window") for f in facts_all),
                 "transfer_or_successor": transfer,
                 "coverage": cont["coverage"], "max_gap": cont["max_gap"], "max_gap_jump": cont["max_gap_jump"],
                 "a6_n": a6["n"], "a6_agree": a6["agree"], "a6_level_n": a6["level_n"],
                 "a6_level_median": a6["level_median"], "a7_n": a7["n"], "a7_hits": a7["hits"],
                 "first_date": str(series["date"].min().date()) if len(series) else "",
                 "last_date": str(series["date"].max().date()) if len(series) else ""}
        ends = [f.get("delist_end_sessions") for f in facts_all if f.get("delist_in_window")]
        ends_known = [e for e in ends if e is not None]
        facts["delist_end_sessions"] = min(ends_known) if ends_known else None
        lends = [f.get("listing_end_sessions") for f in facts_all if f.get("listing_end_sessions") is not None]
        facts["listing_end_sessions"] = min(lends) if lends else None
        facts["post_delist_real"] = int(sum(f.get("post_delist_real", 0) for f in facts_all))
        verdict, failed = judge(facts)
        facts["verdict"], facts["failed_rules"] = verdict, " ".join(failed)
        v0, f0 = judge(facts, amended=False)
        facts["verdict_as_first_written"], facts["failed_rules_as_first_written"] = v0, " ".join(f0)
        facts["second_source"] = second_source_label(facts) if verdict == "accepted" else ""
        facts["split_flags"] = int(series["flags"].str.contains("split").sum()) if len(series) else 0
        verdicts.append(facts)
        unit_rows += facts_all
        path = SERIES / f"{sid}.csv.gz"
        if verdict == "accepted":
            out = series.assign(date=series["date"].dt.strftime("%Y-%m-%d"))
            common.atomic_write(path, gzip.compress(out.to_csv(index=False, float_format="%.10g").encode(), mtime=0))
        elif path.exists():
            path.unlink()  # a series that no longer passes is not left for the fill to read (rebuilt every run)
    v = pd.DataFrame(verdicts)
    common.atomic_write(ALPACA / "entity.csv", v.to_csv(index=False).encode())
    common.atomic_write(ALPACA / "entity_units.csv", pd.DataFrame(unit_rows).to_csv(index=False).encode())
    plan_rows(v)
    log(f"confirm: {v['verdict'].value_counts().to_dict() if len(v) else {}}")


def plan_rows(v: pd.DataFrame) -> pd.DataFrame:
    """One row per plan row: alpaca_status (filled / ambiguous / no_bars / not_covered), the needed sessions from
    2016-01-04 and how many the accepted series holds, the pre-2016 needed sessions Alpaca cannot cover."""
    plan, master, _ = read_inputs()
    sessions = xnas_sessions()
    vv = v.set_index("security_id") if len(v) else pd.DataFrame()
    rows = []
    for r in plan.to_dict("records"):
        sid = r["security_id"]
        need = sessions[(sessions >= pd.Timestamp(r["needed_start"])) & (sessions <= pd.Timestamp(r["needed_end"]))]
        pre = need[need < ALPACA_START]
        post = need[need >= ALPACA_START]
        status, held, failed, second = "not_covered", 0, "", ""
        if len(post):
            if sid in vv.index:
                status = {"accepted": "filled", "ambiguous": "ambiguous", "no_bars": "no_bars"}[vv.at[sid, "verdict"]]
                failed = vv.at[sid, "failed_rules"]
                second = vv.at[sid, "second_source"]
                path = SERIES / f"{sid}.csv.gz"
                if status == "filled" and path.exists():
                    s = pd.read_csv(path, usecols=["date"])
                    held = int(pd.DatetimeIndex(pd.to_datetime(s["date"])).isin(post).sum())
            else:
                status = "no_bars"
        rows.append({"plan_rank": r["plan_rank"], "security_id": sid, "ticker_for_source": r["ticker_for_source"],
                     "group": r["group"], "needed_start": r["needed_start"], "needed_end": r["needed_end"],
                     "expected_top250_weeks": r["expected_top250_weeks"], "unknown_weeks": r["unknown_weeks"],
                     "alpaca_status": status, "failed_rules": failed, "second_source": second,
                     "need_sessions_pre2016": int(len(pre)), "need_sessions_from2016": int(len(post)),
                     "alpaca_sessions_held": held})
    out = pd.DataFrame(rows)
    common.atomic_write(ALPACA / "plan_rows.csv", out.to_csv(index=False).encode())
    return out


PROPOSAL_BUDGET = 480            # the plan's month-2 symbol budget (``within_480``)
SPLICE_UNTIL = "2016-02-05"      # a pre-2016 Tiingo span runs to here: >= 20 sessions overlap with Alpaca (rule R8)
SAMPLE_SEED = 20261010
SAMPLE_DATES_ONLY, SAMPLE_CONFIRMED = 40, 20


def tiingo_proposal() -> pd.DataFrame:
    """Plan section 0 (2026-10-10), task: the November Tiingo quota goes to what Alpaca cannot cover, plus
    verification samples. Written as a proposal beside the original plan, which is not changed:

      P1 ``ambiguous`` rows (Alpaca's series failed an entity rule): the whole needed span;
      P2 ``no_bars`` rows: the whole needed span;
      P3 needed sessions before 2016-01-04 (Alpaca's free history starts there): ``needed_start`` to 2016-02-05 for a
         filled row (so the Tiingo and Alpaca series overlap by at least 20 sessions, rule R8), the whole span for a
         ``not_covered`` row;
      P4 verification samples of filled rows (seed 20261010): up to 40 accepted on dates only (no second source), then
         up to 20 accepted with a second source; the whole needed span, to measure Alpaca against Tiingo.
    Filled rows outside P3 and P4 leave the plan (``covered_by_alpaca``). Unique symbols are counted against 480."""
    plan, _, _ = read_inputs()
    pr = pd.read_csv(ALPACA / "plan_rows.csv", dtype=str, keep_default_na=False)
    ent = pd.read_csv(ALPACA / "entity.csv", dtype=str, keep_default_na=False).set_index("security_id")
    df = plan.merge(pr[["plan_rank", "alpaca_status", "failed_rules", "second_source", "need_sessions_pre2016",
                        "need_sessions_from2016", "alpaca_sessions_held"]], on="plan_rank", how="left")
    df["proposal_priority"], df["proposal_reason"] = "", ""
    df["proposal_start"], df["proposal_end"] = "", ""
    st = df["alpaca_status"]
    pre = df["need_sessions_pre2016"].astype(int) > 0
    for i in df.index:
        s = st[i]
        if s == "ambiguous":
            df.loc[i, ["proposal_priority", "proposal_reason"]] = ["P1", f"alpaca_ambiguous:{df.at[i, 'failed_rules']}"]
        elif s == "no_bars":
            df.loc[i, ["proposal_priority", "proposal_reason"]] = ["P2", "alpaca_no_bars"]
        elif s == "not_covered":
            df.loc[i, ["proposal_priority", "proposal_reason"]] = ["P3", "pre2016_only"]
        elif s == "filled" and pre[i]:
            df.loc[i, ["proposal_priority", "proposal_reason"]] = ["P3", "pre2016_part"]
            df.loc[i, ["proposal_start", "proposal_end"]] = [df.at[i, "needed_start"],
                                                             min(df.at[i, "needed_end"], SPLICE_UNTIL)]
        if df.at[i, "proposal_priority"] and not df.at[i, "proposal_start"]:
            df.loc[i, ["proposal_start", "proposal_end"]] = [df.at[i, "needed_start"], df.at[i, "needed_end"]]
    rng = np.random.default_rng(SAMPLE_SEED)
    pool = df[(st == "filled") & ~pre]
    dates_only = pool[pool["second_source"] == "confirmed_dates_only"].sort_values("plan_rank", key=lambda x: x.astype(int))
    confirmed = pool[pool["second_source"] != "confirmed_dates_only"].sort_values("plan_rank", key=lambda x: x.astype(int))
    pick = list(rng.choice(dates_only.index, size=min(SAMPLE_DATES_ONLY, len(dates_only)), replace=False)) + \
        list(rng.choice(confirmed.index, size=min(SAMPLE_CONFIRMED, len(confirmed)), replace=False))
    for i in pick:
        df.loc[i, ["proposal_priority", "proposal_start", "proposal_end"]] = ["P4", df.at[i, "needed_start"],
                                                                              df.at[i, "needed_end"]]
        df.loc[i, "proposal_reason"] = "verify_" + ("dates_only" if df.at[i, "second_source"] == "confirmed_dates_only"
                                                    else "confirmed")
    left = df["proposal_priority"] == ""
    df.loc[left, "proposal_priority"], df.loc[left, "proposal_reason"] = "drop", "covered_by_alpaca"
    order = {"P1": 1, "P2": 2, "P3": 3, "P4": 4, "drop": 9}
    df = df.sort_values(["proposal_priority", "plan_rank"], key=lambda x: x.map(order) if x.name == "proposal_priority"
                        else x.astype(int)).reset_index(drop=True)
    keep = df["proposal_priority"] != "drop"
    symbols = (df["security_id"] + "|" + df["ticker_for_source"]).where(keep)
    df["proposal_cum_symbols"] = (~symbols.duplicated() & keep).cumsum().where(keep, 0).astype(int)
    df["proposal_within_480"] = np.where(keep & (df["proposal_cum_symbols"] <= PROPOSAL_BUDGET), "Y",
                                         np.where(keep, "N", ""))
    df["proposal_note"] = ("data v2.1 proposal 2026-10-10 (docs/reversal_2012_2026_data_report_v2_1.md); the original "
                           "plan file is unchanged")
    path = common.MAIN_CHECKOUT / "research_cache" / "reversal_2012_2026_v2_1" / "prefilter" / \
        "tiingo_month2_plan_v2_1_proposal.csv"
    common.atomic_write(path, df.to_csv(index=False).encode())
    log(f"proposal: {df['proposal_priority'].value_counts().to_dict()}, symbols {int(df['proposal_cum_symbols'].max())}"
        f" -> {path}")
    return df


def status() -> None:
    p = ALPACA / "plan_rows.csv"
    if p.exists():
        d = pd.read_csv(p)
        print(d["alpaca_status"].value_counts().to_dict())
        print(pd.crosstab(d["group"], d["alpaca_status"]))
    ledger = ALPACA / "quota_ledger.csv"
    if ledger.exists():
        print("requests logged:", max(0, len(ledger.read_text().splitlines()) - 1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["targets", "fetch", "confirm", "proposal", "status"])
    args = ap.parse_args(argv)
    ALPACA.mkdir(parents=True, exist_ok=True)
    if args.step == "targets":
        build_targets()
    elif args.step == "fetch":
        units = pd.read_csv(ALPACA / "targets.csv", dtype=str, keep_default_na=False) \
            if (ALPACA / "targets.csv").exists() else build_targets()
        fetch(units)
    elif args.step == "confirm":
        confirm()
    elif args.step == "proposal":
        tiingo_proposal()
    else:
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
