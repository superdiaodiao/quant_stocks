"""Total-return history before the OOS.2 panel start (1998-06-01) for the 252-session momentum look-back of the
megacap out-of-sample test OOS.3 (docs/research_ledger_megacap.md, section OOS.3, registered addition 1).

Each name's history comes from the source it uses on 1998-06-01 in OOS.2 (Yahoo v8 chart, Tiingo, the OOS.2 Wayback
capture or companiesmarketcap.com market caps), is chained to the OOS.2 total-return level at the level's first date
(the OOS.2 level itself is never changed) and is used for momentum only.

Moved unchanged from studies/megacap_oos3.py (``_yahoo_pre``, ``_tiingo_pre``, ``pre_history``, ``chain_pre``,
``extended_index`` and the extension loop of ``run``). The cache folder and the history window are parameters:
``local`` (the study's local cache, e.g. research_cache/megacap_oos3), ``pre_from`` / ``pre_to`` (first / last day
asked for) and ``url_to`` (the exclusive end day of the Yahoo request).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import megacap_history as o2     # the OOS.2 data build (Wayback segments, Tiingo levels)
from quant.data import version as dv
from quant.data.sources import http
from quant.data.sources import megacap_oos2 as md      # the OOS.2 sources (cached_get on the pipeline ledger)


def yahoo_pre(symbol: str, local: Path, pre_from: str, pre_to: str, url_to: str) -> pd.Series | None:
    p1, p2 = int(pd.Timestamp(pre_from).timestamp()), int(pd.Timestamp(url_to).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={p1}&period2={p2}"
           "&interval=1d&events=div%2Csplits&includeAdjustedClose=true")
    try:
        body = md.cached_get(url, local / "raw/yahoo" / f"{symbol}__{pre_from}_{pre_to}.json.gz", source="yahoo",
                                 headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                                 limiter=md.YAHOO_LIMITER, symbol=symbol, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"  yahoo pre {symbol}: {exc}")
        return None
    res = json.loads(body).get("chart", {}).get("result")
    if not res or not res[0].get("timestamp"):
        return None
    r = res[0]
    ts = pd.to_datetime(r["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    df = pd.DataFrame({"date": ts, "c": r["indicators"]["quote"][0]["close"]}).dropna().drop_duplicates("date", keep="last")
    divs = {}
    for v in r.get("events", {}).get("dividends", {}).values():
        d = pd.Timestamp(v["date"], unit="s", tz="UTC").tz_convert("America/New_York").normalize().tz_localize(None)
        divs[d] = divs.get(d, 0.0) + float(v["amount"])
    df["dv"] = df["date"].map(divs).fillna(0.0)
    ret = (df["c"] + df["dv"]) / df["c"].shift(1) - 1          # split-adjusted close + dividend (as md.yahoo_daily)
    return pd.Series((1 + ret.fillna(0)).cumprod().to_numpy(), index=pd.DatetimeIndex(df["date"]))


def tiingo_pre(symbol: str, local: Path, pre_from: str, pre_to: str) -> pd.Series | None:
    if symbol.upper() not in md.tiingo_month_symbols():
        print(f"  tiingo pre {symbol}: not counted this month -> not asked")
        return None
    url = f"https://api.tiingo.com/tiingo/daily/{symbol.lower()}/prices?startDate={pre_from}&endDate={pre_to}"
    key = http.read_env_key(dv.DATA_MAIN / ".env.tiingo", "TIINGO_API_KEY")
    try:
        body = md.cached_get(url, local / "raw/tiingo" / f"{symbol}__{pre_from}_{pre_to}.json.gz", source="tiingo",
                                 symbol=symbol.upper(), limiter=md.TIINGO_LIMITER, timeout=60,
                                 headers={"Authorization": f"Token {key}", "Content-Type": "application/json"})
    except Exception as exc:  # noqa: BLE001
        print(f"  tiingo pre {symbol}: {type(exc).__name__}")
        return None
    t = md._tiingo_frame(json.loads(body))
    if t is None:
        return None
    return pd.Series(o2._tiingo_tr(t).to_numpy(), index=pd.DatetimeIndex(t["date"]))


def pre_history(row, panel_source: str, local: Path, pre_from: str, pre_to: str,
                url_to: str) -> tuple[pd.Series | None, str]:
    """Total-return level before (and overlapping) 1998-06-01 from the source this name uses on 1998-06-01 in OOS.2."""
    src, _, sym = panel_source.partition(":")
    if src == "yahoo":
        return yahoo_pre(sym, local, pre_from, pre_to, url_to), panel_source
    if src in ("tiingo", "tiingo_cache"):
        return tiingo_pre(sym, local, pre_from, pre_to), f"tiingo:{sym}"
    if src == "wayback":
        a_need, _ = md.need_window(row)
        seg = o2.wayback_segment(sym, max(a_need, pd.Timestamp(o2.START)), pd.Timestamp(o2.END))   # OOS.2's capture
        if seg is None:
            return None, panel_source
        return pd.Series(seg["tr"].to_numpy(), index=pd.DatetimeIndex(seg["date"])), panel_source
    if src == "cmc":
        m = md.cmc_series(sym)["marketcap"]
        return (m / m.iloc[0]) if len(m) else None, panel_source
    return None, panel_source


def chain_pre(pre: pd.Series, own: pd.Series, cal_pre: pd.DatetimeIndex, pre_to: str) -> tuple[pd.Series, dict]:
    """Prepend ``pre`` to ``own`` (the OOS.2 total-return level) at own's first date; own is not changed.
    Returns the levels on cal_pre (dates before own's first date) and an overlap check of daily returns up to
    ``pre_to``."""
    anchor = own.first_valid_index()
    pre = pre[~pre.index.duplicated()].sort_index()
    if anchor not in pre.index or not np.isfinite(pre.loc[anchor]) or pre.loc[anchor] <= 0:
        return pd.Series(dtype=float), {"anchor": str(anchor.date()), "ok": False}
    scaled = pre * (own.loc[anchor] / pre.loc[anchor])
    ov = pd.DataFrame({"a": scaled, "b": own}).dropna()
    ov = ov[(ov.index >= anchor) & (ov.index <= pd.Timestamp(pre_to))].pct_change().dropna()
    diff = float((ov["a"] - ov["b"]).abs().max()) if len(ov) else np.nan
    out = scaled[scaled.index < anchor].reindex(cal_pre).ffill()
    first = scaled.index.min()
    out[out.index < first] = np.nan
    return out, {"anchor": str(anchor.date()), "ok": True, "overlap_days": int(len(ov)), "overlap_max_abs_ret_diff": diff,
                 "pre_first": str(first.date())}


def extended_index(idx: pd.DataFrame, extend: dict, cal_pre: pd.DatetimeIndex) -> pd.DataFrame:
    """idx on cal_pre + idx.index; columns in ``extend`` get their prepended history, others NaN before."""
    top = pd.DataFrame(np.nan, index=cal_pre, columns=idx.columns)
    for k, s in extend.items():
        top[k] = s.reindex(cal_pre).to_numpy()
    return pd.concat([top, idx]).sort_index()


def lookback_extensions(keys: list, idx: pd.DataFrame, panel: dict, cand: pd.DataFrame,
                        sessions: pd.DatetimeIndex, cal_pre: pd.DatetimeIndex, local: Path, pre_from: str,
                        pre_to: str, url_to: str) -> tuple[dict, pd.DataFrame]:
    """For each key in ``keys``: the chained pre-start history (``extend``: key -> levels on ``cal_pre``) and one log
    row (extended, IPO after the panel start, no history, or could not chain)."""
    crow = {r.key: r for r in cand.itertuples()}
    extend, ext_log = {}, []
    for k in keys:
        own = idx[k]
        first_src = str(panel[k]["source"].iloc[0])
        if own.first_valid_index() > sessions[0]:
            ext_log.append({"key": k, "source": first_src, "status": "series starts after 1998-06-01 (IPO): not extended",
                            "own_first": str(own.first_valid_index().date())})
            continue
        pre, label = pre_history(crow[k], first_src, local, pre_from, pre_to, url_to)
        if pre is None or pre.empty:
            ext_log.append({"key": k, "source": label, "status": "no pre-1998-06 history"})
            continue
        s_, chk = chain_pre(pre, own, cal_pre, pre_to)
        if not chk["ok"] or s_.notna().sum() == 0:
            ext_log.append({"key": k, "source": label, "status": "could not chain", **chk})
            continue
        extend[k] = s_
        ext_log.append({"key": k, "source": label, "status": "extended", **chk,
                        "pre_sessions": int(s_.notna().sum())})
    return extend, pd.DataFrame(ext_log)
