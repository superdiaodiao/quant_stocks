"""Price sources for scripts/forward_observation.py and scripts/forward_smisp.py (checklist sections B0 and B3).

Every source is turned into the Yahoo v8 chart body the frozen loaders already read (``research_calendar.parse_ohlc``,
``research_intraday_t.split_events``, ``research_selective_t.ohlc_frame``): split-adjusted open / high / low / close
and volume, ``adjclose`` (split and dividend adjusted), dividend events (split-adjusted amounts at the ex-date) and
split events. So no loader or simulator changes with the source.

- **Alpaca** (S-MISP line, first choice): ``GET https://data.alpaca.markets/v2/stocks/bars`` with ``feed=sip``,
  ``timeframe=1Day``, many symbols per request, at most 200 requests a minute. Each window is read three times:
  ``adjustment=raw`` (real close and volume), ``adjustment=split`` (split-adjusted OHLCV; the split ratios are the
  steps of raw / split) and ``adjustment=all`` (split and dividend adjusted: the total-return ``adjclose``). Dividend
  events are not needed by the S-MISP code (it reads the real close, the volume and the total return) and are left
  empty.
- **Tiingo** (B1 / B2 lines, first choice from 2026-11; the free plan allows 500 unique symbols a month and October's
  quota was used up): ``GET https://api.tiingo.com/tiingo/daily/<sym>/prices`` (raw OHLCV, ``adjClose``,
  ``divCash``, ``splitFactor``).
- **Yahoo** v8 chart (fallback for both; the format itself).

Keys are read from the environment (GitHub secrets) or from ``.env.alpaca`` / ``.env.tiingo`` in the main checkout or
any parent folder; they are never printed or logged.

The small I/O helpers shared by both runners (HTTP GET, one Yahoo chart request, atomic writes, gzip / plain JSON
reads, CSV request logs) are here too.

History state (B1 / B2): RSI2's expanding percentiles need QQQ from 1999, which Alpaca does not have and which should
not change with the vendor. ``state/forward_observation/price_history_returns.csv.gz`` (committed) keeps the history
through the 2026-10-09 decision close as ratios only (close / previous close, open / high / low over the same close,
adjclose / previous adjclose, dividend / close, split ratio): no price levels. Each run fetches only a recent tail,
rebuilds the levels backwards from the tail's close on the last state day, checks the overlapping days, and appends
the new days.
"""
from __future__ import annotations

import gzip
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quant.paths import ROOT
from quant.strategies import selective_t as st

NY = ZoneInfo("America/New_York")
ALPACA_BARS = "https://data.alpaca.markets/v2/stocks/bars"
ALPACA_PER_MINUTE = 200
ALPACA_SYMBOLS_PER_CALL = 200
TIINGO_DAILY = "https://api.tiingo.com/tiingo/daily/{sym}/prices"
TIINGO_FROM_MONTH = "2026-11"         # first month with Tiingo quota for the forward runner
STATE_DIR = ROOT / "state/forward_observation"
HISTORY_STATE = STATE_DIR / "price_history_returns.csv.gz"
HISTORY_COLS = ["sym", "date", "c_r", "o_r", "h_r", "l_r", "a_r", "d_r", "split"]
TAIL_DAYS = 30                        # calendar days of overlap fetched before the last state day
OVERLAP_TOL = 2e-4                    # max |close-ratio difference| on overlapping days before a warning


# ======================================================================== keys

def secret(name: str, filename: str) -> str | None:
    """Environment first (GitHub Actions secrets), then ``filename`` in this checkout or a parent folder."""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    for folder in [ROOT, *ROOT.parents]:
        p = folder / filename
        if p.is_file():
            for line in p.read_text().splitlines():
                k, _, val = line.partition("=")
                if k.strip() == name and val.strip():
                    return val.strip().strip('"').strip("'")
    return None


def alpaca_headers() -> dict | None:
    k, s = secret("ALPACA_API_KEY_ID", ".env.alpaca"), secret("ALPACA_API_SECRET_KEY", ".env.alpaca")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s, "Accept": "application/json"} if k and s else None


def tiingo_key() -> str | None:
    return secret("TIINGO_API_KEY", ".env.tiingo")


def http_get(url: str, headers: dict, timeout: int = 60) -> bytes:
    """GET ``url``; a gzip-encoded body (SEC with Accept-Encoding: gzip) is decompressed."""
    with urlopen(Request(url, headers=headers), timeout=timeout) as resp:
        data = resp.read()
        return gzip.decompress(data) if resp.headers.get("Content-Encoding") == "gzip" else data


def yahoo_get(sym: str, period1: str, period2: str, getter=None) -> bytes:
    """One Yahoo v8 daily chart (``research_selective_t.chart_url`` / ``HEADERS``). ``getter(url)`` for tests. The
    callers keep the research politeness: at most one request every 2 s, stop at ``st.STOP_CODES``."""
    url = st.chart_url(sym, period1, period2)
    return getter(url) if getter is not None else http_get(url, st.HEADERS, 30)


# ======================================================================== files

def write_atomic(path: Path, data: bytes, compress: bool = False) -> None:
    """Write via a temporary file (gzip with mtime 0 when ``compress``), so a broken run leaves no partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(gzip.compress(data, mtime=0) if compress else data)
    tmp.replace(path)


def read_json(path: Path):
    """A JSON file, gzip-compressed when its name ends in .gz."""
    data = path.read_bytes()
    return json.loads(gzip.decompress(data) if path.suffix == ".gz" else data)


def is_daily_chart(payload: dict) -> bool:
    """A Yahoo-format body with a daily result."""
    r = (payload.get("chart") or {}).get("result") or [None]
    return bool(r[0]) and (r[0].get("meta") or {}).get("dataGranularity") == "1d"


def log_line(path: Path, header: str, line: str = "") -> None:
    """Append ``line`` to a CSV log, writing ``header`` first when the file is new (no line: header only)."""
    if not path.exists():
        path.write_text(header + "\n")
    if line:
        with path.open("a") as fh:
            fh.write(line + "\n")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ======================================================================== the common format

def ny_ts(dates) -> list:
    """Unix seconds of 09:30 New York on each date (as Yahoo stamps daily bars)."""
    return [int(pd.Timestamp(str(pd.Timestamp(d).date()) + " 09:30", tz=NY).timestamp()) for d in dates]


def chart_payload(sym: str, df: pd.DataFrame, dividends: dict | None = None, splits: dict | None = None) -> dict:
    """Yahoo v8 body from a frame indexed by date with open / high / low / close / volume (split-adjusted) and
    adjclose; ``dividends`` {date: split-adjusted amount}, ``splits`` {date: ratio}."""
    df = df.sort_index()
    ts = ny_ts(df.index)
    pos = {pd.Timestamp(d): t for d, t in zip(df.index, ts)}

    def lst(col):
        return [None if not np.isfinite(x) else float(x) for x in df[col].to_numpy(float)]

    ev = {"dividends": {}, "splits": {}}
    for d, a in (dividends or {}).items():
        t = pos.get(pd.Timestamp(d))
        if t is not None and a:
            ev["dividends"][str(t)] = {"date": t, "amount": float(a)}
    for d, r in (splits or {}).items():
        t = pos.get(pd.Timestamp(d))
        if t is not None and r and r != 1:
            ev["splits"][str(t)] = {"date": t, "numerator": float(r), "denominator": 1.0}
    return {"chart": {"result": [{"timestamp": ts, "meta": {"dataGranularity": "1d", "symbol": sym},
                                  "indicators": {"quote": [{k: lst(k) for k in ("open", "high", "low", "close",
                                                                                 "volume")}],
                                                 "adjclose": [{"adjclose": lst("adjclose")}]},
                                  "events": ev}], "error": None}}


def payload_frame(payload: dict) -> tuple[pd.DataFrame, dict, dict]:
    """Inverse of ``chart_payload``: (frame by date, dividends, splits)."""
    j = payload["chart"]["result"][0]
    dates = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert(NY).normalize().tz_localize(None)
    q = j["indicators"]["quote"][0]
    adj = (j["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    df = pd.DataFrame({k: pd.to_numeric(pd.Series(q.get(k) or [np.nan] * len(dates)), errors="coerce").to_numpy(float)
                       for k in ("open", "high", "low", "close", "volume")}, index=dates)
    df["adjclose"] = pd.to_numeric(pd.Series(adj), errors="coerce").to_numpy(float)
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])

    def day(v):
        return pd.Timestamp(pd.to_datetime(v["date"], unit="s", utc=True).tz_convert(NY).date())
    ev = j.get("events") or {}
    divs = {day(v): float(v["amount"]) for v in (ev.get("dividends") or {}).values()}
    spl = {day(v): float(v["numerator"]) / float(v["denominator"]) for v in (ev.get("splits") or {}).values()}
    return df, divs, spl


# ======================================================================== Alpaca

def alpaca_bars(symbols, start: str, end: str, adjustment: str, headers: dict, getter=None,
                sleep=time.sleep) -> dict:
    """{symbol: DataFrame(date -> o, h, l, c, v)} for daily SIP bars (paginated, <= 200 requests a minute)."""
    get = getter or http_get
    out: dict = {}
    syms = list(dict.fromkeys(symbols))
    gap = 60.0 / ALPACA_PER_MINUTE
    last = None
    for i in range(0, len(syms), ALPACA_SYMBOLS_PER_CALL):
        chunk = syms[i: i + ALPACA_SYMBOLS_PER_CALL]
        token = None
        while True:
            q = {"symbols": ",".join(chunk), "timeframe": "1Day", "feed": "sip", "start": start, "end": end,
                 "adjustment": adjustment, "limit": 10000}
            if token:
                q["page_token"] = token
            if last is not None and time.time() - last < gap:
                sleep(gap - (time.time() - last))
            last = time.time()
            j = json.loads(get(ALPACA_BARS + "?" + urlencode(q), headers))
            for sym, bars in (j.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            token = j.get("next_page_token")
            if not token:
                break
    frames = {}
    for sym, bars in out.items():
        df = pd.DataFrame(bars)
        # daily bars are stamped at midnight New York (04:00 or 05:00 UTC): the UTC date of stamp + 6 h is the day
        d = (pd.to_datetime(df["t"], utc=True) + pd.Timedelta(hours=6)).dt.normalize().dt.tz_localize(None)
        frames[sym] = pd.DataFrame({"o": df["o"].astype(float).to_numpy(), "h": df["h"].astype(float).to_numpy(),
                                    "l": df["l"].astype(float).to_numpy(), "c": df["c"].astype(float).to_numpy(),
                                    "v": df["v"].astype(float).to_numpy()}, index=pd.DatetimeIndex(d)) \
            .sort_index().loc[lambda x: ~x.index.duplicated(keep="last")]
    return frames


def snap_ratio(x: float) -> float:
    """A split ratio read from rounded prices, snapped to n / m (m <= 20, n <= 40) when within 0.2%."""
    best = min(((abs(n / m - x) / x, n / m) for m in range(1, 21) for n in range(1, 41)), key=lambda z: z[0])
    return float(best[1]) if best[0] < 0.002 else float(round(x, 4))


def alpaca_charts(symbols, start: str, end: str, headers: dict | None = None, getter=None,
                  sleep=time.sleep) -> dict:
    """{symbol: Yahoo-format body} from Alpaca raw / split / all bars. Split ratio on day d = F(d-1) / F(d) with
    F = raw close / split-adjusted close (a step of more than 2%, snapped to a simple fraction)."""
    headers = headers or alpaca_headers()
    if headers is None:
        raise RuntimeError("no Alpaca keys (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY)")
    raw = alpaca_bars(symbols, start, end, "raw", headers, getter, sleep)
    spl = alpaca_bars(symbols, start, end, "split", headers, getter, sleep)
    alla = alpaca_bars(symbols, start, end, "all", headers, getter, sleep)
    out = {}
    for sym in symbols:
        if sym not in spl or sym not in raw or sym not in alla:
            continue
        s, r, a = spl[sym], raw[sym].reindex(spl[sym].index), alla[sym].reindex(spl[sym].index)
        f = (r["c"] / s["c"])
        f = f.where(np.isfinite(f)).ffill().bfill()
        ratio = f.shift(1) / f
        splits = {d: snap_ratio(x) for d, x in ratio.items() if np.isfinite(x) and abs(x - 1) > 0.02}
        df = pd.DataFrame({"open": s["o"], "high": s["h"], "low": s["l"], "close": s["c"], "volume": s["v"],
                           "adjclose": a["c"]})
        out[sym] = chart_payload(sym, df, {}, splits)
    return out


# ======================================================================== Tiingo

def tiingo_chart(sym: str, start: str, end: str, key: str | None = None, getter=None) -> dict:
    """Yahoo-format body from Tiingo daily prices (raw OHLCV; split-adjusted here with the splitFactor column)."""
    key = key or tiingo_key()
    if not key:
        raise RuntimeError("no Tiingo key (TIINGO_API_KEY)")
    url = TIINGO_DAILY.format(sym=sym.lower()) + "?" + urlencode({"startDate": start, "endDate": end,
                                                                  "format": "json", "token": key})
    rows = json.loads((getter or http_get)(url, {"Accept": "application/json"}))
    if not rows:
        raise ValueError(f"Tiingo: no rows for {sym}")
    return tiingo_rows_to_payload(sym, rows)


def tiingo_rows_to_payload(sym: str, rows: list) -> dict:
    df = pd.DataFrame(rows)
    d = pd.DatetimeIndex(pd.to_datetime(df["date"].str[:10]))
    sf = pd.Series(df["splitFactor"].astype(float).to_numpy(), index=d)
    # F(t) = product of split factors with ex-date after t: split-adjusted = raw / F
    after = sf[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    out = pd.DataFrame({k: df[k].astype(float).to_numpy() / after.to_numpy() for k in ("open", "high", "low", "close")},
                       index=d)
    out["volume"] = df["volume"].astype(float).to_numpy() * after.to_numpy()
    out["adjclose"] = df["adjClose"].astype(float).to_numpy()
    div = pd.Series(df["divCash"].astype(float).to_numpy() / after.to_numpy(), index=d)
    return chart_payload(sym, out, {k: v for k, v in div.items() if v > 0},
                         {k: v for k, v in sf.items() if v != 1})


# ======================================================================== returns-only history state

def history_rows(sym: str, payload: dict, end: str) -> pd.DataFrame:
    """Ratios (no levels) of one chart through ``end`` for the committed history state."""
    df, divs, spl = payload_frame(payload)
    df = df[df.index <= pd.Timestamp(end)]
    c = df["close"]
    out = pd.DataFrame({"sym": sym, "date": df.index.strftime("%Y-%m-%d"),
                        "c_r": (c / c.shift(1)).fillna(1.0).to_numpy(),
                        "o_r": (df["open"] / c).to_numpy(), "h_r": (df["high"] / c).to_numpy(),
                        "l_r": (df["low"] / c).to_numpy(),
                        "a_r": (df["adjclose"] / df["adjclose"].shift(1)).fillna(1.0).to_numpy(),
                        "d_r": [divs.get(d, 0.0) / x for d, x in zip(df.index, c)],
                        "split": [spl.get(d, 1.0) for d in df.index]})
    return out


def write_history_state(payloads: dict, end: str, path: Path = HISTORY_STATE, starts: dict | None = None) -> Path:
    """``starts`` {symbol: first date kept} (the U18 stocks need only a short history: SMA20; checked identical)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    for s, p in payloads.items():
        h = history_rows(s, p, end)
        if starts and s in starts:
            h = h[h["date"] >= starts[s]].copy()
            h.iloc[0, h.columns.get_loc("c_r")] = 1.0      # the first kept day has no previous close
            h.iloc[0, h.columns.get_loc("a_r")] = 1.0
        parts.append(h)
    rows = pd.concat(parts, ignore_index=True)
    rows.to_csv(path, index=False, compression="gzip", float_format="%.15g")
    return path


def load_history_state(path: Path = HISTORY_STATE) -> pd.DataFrame | None:
    return pd.read_csv(path, dtype={"sym": str, "date": str}) if path.exists() else None


def merge_history(sym: str, state: pd.DataFrame, tail: dict) -> tuple[dict, dict]:
    """History from the state (levels rebuilt backwards from the tail's close on the last state day) followed by
    the tail's later days. Returns (Yahoo-format body, check)."""
    h = state[state["sym"] == sym].sort_values("date")
    if h.empty:
        return tail, {"history": "none"}
    tdf, tdiv, tspl = payload_frame(tail)
    last = pd.Timestamp(h["date"].iloc[-1])
    if last not in tdf.index:
        raise ValueError(f"{sym}: the fetched tail does not hold the last state day {last.date()}")
    dates = pd.DatetimeIndex(pd.to_datetime(h["date"]))
    c_r, a_r = h["c_r"].to_numpy(float), h["a_r"].to_numpy(float)
    n = len(h)
    c, a = np.empty(n), np.empty(n)
    c[-1], a[-1] = tdf.at[last, "close"], tdf.at[last, "adjclose"]
    for k in range(n - 1, 0, -1):
        c[k - 1] = c[k] / c_r[k]
        a[k - 1] = a[k] / a_r[k]
    hist = pd.DataFrame({"open": h["o_r"].to_numpy(float) * c, "high": h["h_r"].to_numpy(float) * c,
                         "low": h["l_r"].to_numpy(float) * c, "close": c, "volume": np.nan, "adjclose": a},
                        index=dates)
    divs = {d: r * x for d, r, x in zip(dates, h["d_r"].to_numpy(float), c) if r > 0}
    spl = {d: r for d, r in zip(dates, h["split"].to_numpy(float)) if r != 1}
    # overlap check: the tail's close ratios on the days both hold
    common = tdf.index[(tdf.index > dates[0]) & (tdf.index <= last)]
    tr_ = (tdf["close"] / tdf["close"].shift(1)).reindex(common)
    st_ = pd.Series(c_r, index=dates).reindex(common)
    diff = (tr_ - st_).abs().dropna()
    new = tdf[tdf.index > last]
    out = pd.concat([hist, new])
    divs.update({d: v for d, v in tdiv.items() if d > last})
    spl.update({d: v for d, v in tspl.items() if d > last})
    check = {"history_to": str(last.date()), "overlap_days": int(len(diff)),
             "overlap_max_abs_close_ratio_diff": float(diff.max()) if len(diff) else None,
             "new_days": int(len(new))}
    check["overlap_ok"] = bool(len(diff) == 0 or diff.max() <= OVERLAP_TOL)
    return chart_payload(sym, out, divs, spl), check


def save_payload(payload: dict, path: Path, compress: bool | None = None) -> None:
    """A Yahoo-format body as JSON (gzip when ``compress``, by default when the name ends in .gz)."""
    write_atomic(path, json.dumps(payload).encode(), path.suffix == ".gz" if compress is None else compress)
