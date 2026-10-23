"""B4 forward data (docs/forward_observation_checklist.md section B4): BTC, the T-bill rate and the ETF legs.

- **ETF legs** (LEV-B3, V3, BT4b): QQQ and ONEQ are the B1 / B2 files of the runner (Tiingo from 2026-11, Yahoo
  fallback, history from the committed returns-only state: QQQ from 1999 for the SMA200 and V3's expanding
  volatility); QLD and IBIT are fetched by the same ``runner.fetch`` into the same folder (``ETFS``), from
  2026-08-01 only (``runner.FETCH_START``: no line or check reads them earlier; a short series saves Tiingo bytes).
  ``load_legs``
  reads them with the research loader ``quant.data.sources.yahoo.parse_chart`` (cut at the as-of date at once and
  asserted) and builds the legs of ``quant.data.sector_etfs.build_legs`` for the forward window, where every fund
  exists: QLDX = real QLD, BENCH = ONEQ total return, CASH = T-bill ETF (RF - 0.10% a year). The calendar is the QQQ
  sessions (research: SPY's; every other file must be a subset).
- **BTC** (BT4b): the free public sources of the bitcoin study (``quant.data.sources.crypto``: URL builders and
  parsers), asked fresh each run (no request cache: the latest candle changes every day). Bitstamp is the primary
  series, Coinbase / Yahoo / Kraken fill a zero-volume Bitstamp day (``quant.data.bitcoin.primary_close``) and are the
  second vendor of the health checks. UTC day D's close = the candle starting 00:00 UTC on D; a candle of the fetch day
  (or later) is still running and is dropped. The series ends at Bitstamp's last complete day.
- **T-bill** (V3's cash leg): FRED DTB3 (no key); the research rate is Ken French's daily RF with DTB3 / 252 after the
  French file ends, and every forward session is after it (``quant.data.rates.fill_rf``).

Raw bodies stay in the local cache (``cache_dir()`` = ``$FORWARD_OBS_CACHE``/b4, else
research_cache/forward_observation/b4), never in Git.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import bitcoin as bd
from quant.data.guards import assert_dev_dates
from quant.data.rates import TRADING_DAYS, fill_rf, load_dtb3
from quant.data.sources import crypto
from quant.data.sources.yahoo import HEADERS as YAHOO_HEADERS, chart_url, parse_chart
from quant.data.synthetic import CASH_ETF_FEE, NOMINAL_PRICE
from quant.observation import prices as fp
from quant.observation import smisp as fs

ETFS = ("QLD", "IBIT")               # fetched with the B1 / B2 series (runner.fetch), same folder
LEG_SYMBOLS = ("QQQ", "ONEQ") + ETFS
BTC_FROM = "2026-09-01"              # first UTC day asked from the BTC sources (BT4b trades from 2026-11-02)
DTB3_FROM = "2026-01-01"
DTB3_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3&cosd={a}"
BTC_SOURCES = ("bitstamp", "coinbase", "yahoo", "kraken")


def cache_dir() -> Path:
    return fs.cache_root() / "b4"


# ======================================================================== BTC (fresh each run)

def _get(url: str, headers: dict, getter=None) -> bytes:
    return (getter or fp.http_get)(url, headers)


def _check_body(src: str, data: bytes) -> None:
    j = json.loads(data)
    ok = {"bitstamp": lambda: isinstance(j["data"]["ohlc"], list),
          "coinbase": lambda: isinstance(j, list),
          "kraken": lambda: not j.get("error") and isinstance(j["result"], dict),
          "yahoo": lambda: bool(j["chart"]["result"][0]["timestamp"])}[src]
    try:
        good = ok()
    except (KeyError, TypeError, IndexError):
        good = False
    if not good:
        raise ValueError(f"{src}: unexpected body")


def btc_urls(start: str, today: str) -> dict:
    """{source: [urls]} for every UTC day from ``start`` to ``today`` (today's candle is running; it is asked and
    dropped when parsed). Bitstamp 1000 and Coinbase 300 candles a request, as the study's fetchers."""
    t, stop = crypto._epoch(start), crypto._epoch(today)
    bs = []
    while t <= stop:
        n = min(crypto.BITSTAMP_MAX, (stop - t) // crypto.DAY + 1)
        bs.append(crypto.bitstamp_url(t, n))
        t += n * crypto.DAY
    days = crypto._days(start, today)
    cb = [crypto.coinbase_url(days[i], days[min(i + crypto.COINBASE_MAX - 1, len(days) - 1)])
          for i in range(0, len(days), crypto.COINBASE_MAX)]
    tomorrow = (pd.Timestamp(today) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    return {"bitstamp": bs, "coinbase": cb, "kraken": [crypto.kraken_url()],
            "yahoo": [chart_url("BTC-USD", start, tomorrow)]}


def fetch_btc(base: Path | None = None, start: str = BTC_FROM, getter=None, sleep=time.sleep,
              now: datetime | None = None, pacer=None) -> dict:
    """Every source asked once (all its pages); a source's files are replaced only when all its pages arrived, so a
    failure keeps the previous complete copy. Returns {source: 'ok (n)' or the error}; ``btc/fetched.json`` records
    each source's fetch time (its candles of that UTC day or later are dropped as running). ``pacer``
    (``prices.YahooPacer``): Yahoo is asked at the run's shared pace, and not at all once Yahoo refused the run."""
    d = Path(base or cache_dir()) / "btc"
    d.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    meta_p = d / "fetched.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    out = {}
    for src, urls in btc_urls(start, today).items():
        headers = YAHOO_HEADERS if src == "yahoo" else crypto.HEADERS
        if src == "yahoo" and pacer is not None and pacer.stopped:
            out[src] = f"skipped (Yahoo stopped: {pacer.stopped})"
            continue
        bodies, err = [], None
        for k, url in enumerate(urls):
            if src == "yahoo" and pacer is not None:
                pacer.wait()
            elif k or src != "bitstamp":
                sleep(0.6 if src != "yahoo" else 2.0)      # Coinbase 2 a second, Bitstamp 1, Yahoo one per 2 s
            try:
                data = _get(url, headers, getter)
                _check_body(src, data)
            except Exception as exc:  # noqa: BLE001 - a source problem is reported; the previous copy stays
                err = f"{type(exc).__name__}{' ' + str(getattr(exc, 'code', '')) if getattr(exc, 'code', '') else ''}"
                if src == "yahoo" and pacer is not None and getattr(exc, "code", None) in (401, 403, 429):
                    pacer.stop(f"HTTP {exc.code} at BTC-USD")
                break
            bodies.append(data)
        stamp = fp.utc_stamp()
        fp.log_line(d / "fetch_log.csv", "fetched_utc,source,status,pages,bytes",
                    f"{stamp},{src},{err or 'ok'},{len(bodies)},{sum(len(b) for b in bodies)}")
        if err:
            out[src] = f"error {err}"
            continue
        for p in d.glob(f"{src}_*.json"):
            p.unlink()
        for k, b in enumerate(bodies):
            fp.write_atomic(d / f"{src}_{k:02d}.json", b)
        meta[src] = {"fetched_utc": now.isoformat(timespec="seconds"), "pages": len(bodies)}
        out[src] = f"ok ({len(bodies)})"
    meta_p.write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return out


def _parse(src: str, bodies: list, end: str, day: str) -> pd.DataFrame:
    if src == "bitstamp":
        df = crypto.parse_bitstamp(bodies, end)
    elif src == "coinbase":
        df = crypto.parse_coinbase(bodies, end)
    elif src == "kraken":
        df = crypto.parse_kraken(bodies[0], end, today_utc=day)
    else:
        df = crypto.parse_yahoo_crypto(bodies[0], end)
    return df[df["date"] < day].reset_index(drop=True)      # the fetch day's candle (or later) is still running


@dataclass
class Btc:
    close: pd.Series                 # primary close on every UTC day (Bitstamp, zero-volume days filled)
    info: dict
    sources: bd.Sources
    fetched: dict = field(default_factory=dict)


def load_btc(base: Path | None = None, end: str | None = None) -> Btc | None:
    """The primary BTC close per UTC day through ``end`` (asserted), from Bitstamp's first to its last complete day;
    None without a Bitstamp copy."""
    d = Path(base or cache_dir()) / "btc"
    meta_p = d / "fetched.json"
    if not meta_p.exists():
        return None
    meta = json.loads(meta_p.read_text())
    end = end or "2100-01-01"
    frames = {}
    for src in BTC_SOURCES:
        files = sorted(d.glob(f"{src}_*.json"))
        if not files or src not in meta:
            continue
        day = meta[src]["fetched_utc"][:10]
        try:
            frames[src] = _parse(src, [p.read_bytes() for p in files], end, day)
        except Exception:  # noqa: BLE001 - an unreadable check source is left out (the health check reports it)
            if src == "bitstamp":
                raise
    if "bitstamp" not in frames or frames["bitstamp"].empty:
        return None
    first, last = (pd.Timestamp(frames["bitstamp"]["date"].min()), pd.Timestamp(frames["bitstamp"]["date"].max()))
    close = pd.DataFrame({k: v.set_index("date")["close"] for k, v in frames.items()})
    volume = pd.DataFrame({k: v.set_index("date")["volume"] for k, v in frames.items()})
    close.index = volume.index = pd.DatetimeIndex(close.index)
    close, volume = close.sort_index(), volume.sort_index()
    keep = (close.index >= first) & (close.index <= last)    # the primary's own range: fill only inside it
    src = bd.Sources(frames=frames, close=close[keep], volume=volume[keep])
    p, info = bd.primary_close(src)
    assert_dev_dates(p.index, end)
    return Btc(close=p, info=info, sources=src, fetched={k: v["fetched_utc"] for k, v in meta.items()})


# ======================================================================== T-bill (FRED DTB3)

IRX_SYMBOL = "^IRX"                  # CBOE 13-week T-bill discount rate (percent), DTB3's fallback (checklist B4)


def _irx_as_dtb3_csv(data: bytes, start: str, end: str) -> bytes:
    """Yahoo ^IRX chart JSON -> a FRED-format DTB3 CSV body (observation_date, DTB3 in percent)."""
    j = json.loads(data)["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York").strftime("%Y-%m-%d")
    close = j["indicators"]["quote"][0]["close"]
    rows = [f"{d},{c:.2f}" for d, c in zip(ts, close) if c is not None and start <= d <= end]
    if len(rows) < 5:
        raise RuntimeError("^IRX: too few rows")
    return ("observation_date,DTB3\n" + "\n".join(rows) + "\n").encode()


def fetch_dtb3(base: Path | None = None, getter=None, start: str = DTB3_FROM, pacer=None) -> str:
    """FRED DTB3 into ``DTB3.csv``; FRED often times out from GitHub runners, so after it fails the same file is
    written from Yahoo ^IRX (the 13-week bill discount rate, same basis; only V3's cash leg uses it). Returns the
    source used ("fred" or "irx"); raises when both fail (the previous file stays)."""
    p = Path(base or cache_dir()) / "DTB3.csv"
    try:
        data = _get(DTB3_URL.format(a=start), {"User-Agent": "Mozilla/5.0"}, getter)
        text = data.decode()
        if "DTB3" not in text.splitlines()[0] or len(text.splitlines()) < 5:
            raise RuntimeError("DTB3 CSV: unexpected content")
        fp.write_atomic(p, data)
        return "fred"
    except Exception as fred_exc:  # noqa: BLE001 - fall back to ^IRX
        if pacer is not None and pacer.stopped:
            raise RuntimeError(f"FRED {type(fred_exc).__name__}; ^IRX skipped (Yahoo stopped)") from fred_exc
        today = datetime.now(timezone.utc).date()
        if pacer is not None:
            pacer.wait()
        body = _get(chart_url(IRX_SYMBOL, start, str(today + timedelta(days=1))), YAHOO_HEADERS, getter)
        fp.write_atomic(p, _irx_as_dtb3_csv(body, start, str(today)))
        return "irx"


def load_rf(base: Path | None, sessions: pd.DatetimeIndex, end: str) -> pd.Series | None:
    """Daily T-bill on ``sessions``: DTB3 / 252, carried forward (``fill_rf`` after the French file, which ends
    before every forward session); None without a DTB3 file."""
    p = Path(base or cache_dir()) / "DTB3.csv"
    if not p.exists():
        return None
    dtb3 = load_dtb3(end, p)
    french_ended = pd.Series([0.0], index=pd.DatetimeIndex([pd.Timestamp("1990-01-02")]))
    return fill_rf(french_ended, dtb3, sessions)


# ======================================================================== ETF legs

@dataclass
class Legs:
    sessions: pd.DatetimeIndex
    adj: pd.DataFrame                # adjusted closes (NaN before a fund's first bar)
    close: pd.DataFrame              # split-adjusted closes (share counts in the cost model)
    rets: pd.DataFrame               # daily returns of QQQ, QLDX, CASH, BENCH (= ONEQ), ONEQ, IBIT
    p_qqq: pd.Series                 # QQQ adjusted close (the SMA200 signal; a constant multiple of research p_qqq)
    rf: pd.Series | None
    have: dict                       # symbol -> (first, last) date of its file


def load_legs(raw_dir: Path, end: str, rf_base: Path | None = None, syms=LEG_SYMBOLS) -> Legs:
    raw = {s: parse_chart(Path(raw_dir) / f"{s}.json", end) for s in syms if (Path(raw_dir) / f"{s}.json").exists()}
    if "QQQ" not in raw or "ONEQ" not in raw:
        raise SystemExit(f"missing raw data for QQQ / ONEQ in {raw_dir}; run with --fetch")
    sessions = pd.DatetimeIndex(raw["QQQ"]["date"])
    adj, close = {}, {}
    for s, df in raw.items():
        ix = pd.DatetimeIndex(df["date"])
        extra = ix.difference(sessions)
        if len(extra) and s == "ONEQ":       # every line holds ONEQ; a stray QLD / IBIT bar: b4_data_integrity
            raise ValueError(f"{s} has sessions not in the QQQ calendar: {list(extra[:5])}")
        adj[s] = pd.Series(df["adjclose"].to_numpy(float), index=ix).reindex(sessions)
        close[s] = pd.Series(df["close"].to_numpy(float), index=ix).reindex(sessions)
        assert_dev_dates(df["date"], end)
    for s in syms:
        adj.setdefault(s, pd.Series(np.nan, index=sessions))
        close.setdefault(s, pd.Series(np.nan, index=sessions))
    adj, close = pd.DataFrame(adj)[list(syms)], pd.DataFrame(close)[list(syms)]
    assert_dev_dates(sessions, end)
    rf = load_rf(rf_base, sessions, end)
    rets = adj.pct_change(fill_method=None)
    rets["QLDX"], close["QLDX"] = rets["QLD"], close["QLD"]          # real QLD (lists 2006; no synthetic leg)
    rets["BENCH"], close["BENCH"] = rets["ONEQ"], close["ONEQ"]      # real ONEQ (lists 2003; no index splice)
    rets["CASH"] = (rf if rf is not None else pd.Series(0.0, index=sessions)) - CASH_ETF_FEE / TRADING_DAYS
    close["CASH"] = NOMINAL_PRICE
    have = {s: (str(pd.Timestamp(raw[s]["date"].iloc[0]).date()), str(pd.Timestamp(raw[s]["date"].iloc[-1]).date()))
            for s in raw}
    return Legs(sessions=sessions, adj=adj, close=close, rets=rets, p_qqq=adj["QQQ"], rf=rf, have=have)


def utc_day_complete(day, now: datetime | None = None) -> bool:
    """A UTC day's BTC candle is complete from 00:00 UTC of the next day."""
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    now = now.tz_convert("UTC") if now.tzinfo else now.tz_localize("UTC")
    return now >= pd.Timestamp(day, tz="UTC") + timedelta(days=1)
