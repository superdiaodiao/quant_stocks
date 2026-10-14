"""Free public daily BTC-USD candles: Coinbase Exchange, Kraken, Bitstamp and Yahoo (BTC-USD), plus the US spot
bitcoin ETFs (IBIT, FBTC) through Yahoo's chart API. No key is involved anywhere.

Day convention (docs/research_ledger_bitcoin.md 0.2): every source labels a daily candle by its UTC start
(00:00 UTC); the candle of day D runs 00:00-24:00 UTC and its close is the price at 00:00 UTC of D+1
(20:00 New York in summer, 19:00 in winter). All parsers return the UTC date of the candle start as ``date``
(YYYY-MM-DD) and are truncated at ``end`` at once (``quant.data.guards.truncate_dev``).

Request side: URL builders and polite fetchers that go through ``quant.data.sources.http.cached_get`` (raw body
cached before parsing, every request logged in the study's own ledger ``research_cache/bitcoin/``, never in a
frozen version cache). Raw vendor data stays local and is never committed.

- Coinbase Exchange ``/products/BTC-USD/candles``: at most 300 candles a request, rows
  ``[time, low, high, open, close, volume]``, newest first; history from 2015-07-20.
- Kraken ``/0/public/OHLC``: only the most recent 720 candles whatever ``since`` says, rows
  ``[time, open, high, low, close, vwap, volume, count]``; the last row is the running (incomplete) day.
- Bitstamp ``/api/v2/ohlc/btcusd/``: at most 1000 candles a request, ``start`` in epoch seconds; from 2011-08.
- Yahoo ``v8/finance/chart/BTC-USD``: CoinMarketCap-derived, from 2014-09-17; timestamps at 00:00 UTC.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd

from quant.data.guards import truncate_dev
from quant.data.sources.http import Ledger, SlidingWindowLimiter, cached_get
from quant.data.sources.yahoo import HEADERS as YAHOO_HEADERS, chart_url

COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
KRAKEN = "https://api.kraken.com/0/public/OHLC"
BITSTAMP = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
COINBASE_MAX = 300
BITSTAMP_MAX = 1000
DAY = 86400
HEADERS = {"User-Agent": "quant-stocks-research/1.0", "Accept": "application/json"}
COLUMNS = ["date", "open", "high", "low", "close", "volume"]
SOURCES = ("bitstamp", "coinbase", "yahoo", "kraken")
FIRST = {"coinbase": "2015-07-20", "bitstamp": "2011-08-18", "yahoo": "2014-09-17"}


def _epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _utc_date(seconds) -> pd.Series:
    return pd.to_datetime(pd.Series(seconds, dtype="int64"), unit="s", utc=True).dt.strftime("%Y-%m-%d")


def _frame(df: pd.DataFrame, end: str, what: str) -> pd.DataFrame:
    df = df[COLUMNS].copy()
    for c in COLUMNS[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date")
    if df["date"].duplicated().any():
        # overlapping request windows return the same candle twice; they must agree
        g = df.groupby("date")["close"].nunique()
        if (g > 1).any():
            raise ValueError(f"{what}: conflicting duplicate candles on {list(g[g > 1].index[:5])}")
        df = df.drop_duplicates("date", keep="last")
    return truncate_dev(df, "date", end).reset_index(drop=True)


# ======================================================================== parsers (raw body -> frame)

def parse_coinbase(bodies: list[bytes], end: str) -> pd.DataFrame:
    rows = []
    for b in bodies:
        rows.extend(json.loads(b))
    df = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
    df["date"] = _utc_date(df["t"]).values
    return _frame(df, end, "coinbase")


def parse_kraken(body: bytes, end: str, today_utc: str | None = None) -> pd.DataFrame:
    """Kraken's last row is the running day: rows dated ``today_utc`` (the fetch day) or later are dropped."""
    res = json.loads(body)
    if res.get("error"):
        raise ValueError(f"kraken: {res['error']}")
    key = [k for k in res["result"] if k != "last"][0]
    df = pd.DataFrame(res["result"][key], columns=["t", "open", "high", "low", "close", "vwap", "volume", "count"])
    df["date"] = _utc_date(df["t"]).values
    if today_utc is not None:
        df = df[df["date"] < today_utc]
    return _frame(df, end, "kraken")


def parse_bitstamp(bodies: list[bytes], end: str) -> pd.DataFrame:
    rows = []
    for b in bodies:
        rows.extend(json.loads(b)["data"]["ohlc"])
    df = pd.DataFrame(rows)
    df["date"] = _utc_date(df["timestamp"].astype("int64")).values
    return _frame(df, end, "bitstamp")


def parse_yahoo_crypto(body: bytes, end: str) -> pd.DataFrame:
    """Yahoo chart JSON of a crypto pair (exchange timezone UTC): dates are the UTC candle start, unlike
    ``quant.data.sources.yahoo.parse_chart`` which uses New York dates (right for US-listed ETFs only)."""
    j = json.loads(body)["chart"]["result"][0]
    if j["meta"].get("exchangeTimezoneName") != "UTC":
        raise ValueError("expected a UTC-timestamped crypto chart")
    q = j["indicators"]["quote"][0]
    df = pd.DataFrame({"t": j["timestamp"], "open": q["open"], "high": q["high"], "low": q["low"],
                       "close": q["close"], "volume": q["volume"]})
    if (pd.Series(j["timestamp"]) % DAY != 0).any():
        # Yahoo stamps the running day with the latest trade time; any other off-midnight row is unexpected
        off = df[pd.Series(j["timestamp"]) % DAY != 0]
        if len(off) > 1 or off.index[0] != len(df) - 1:
            raise ValueError("yahoo crypto: candles not stamped at 00:00 UTC")
        df = df.iloc[:-1]
    df["date"] = _utc_date(df["t"]).values
    return _frame(df, end, "yahoo")


# ======================================================================== requests

def coinbase_url(start: str, stop: str) -> str:
    """Daily candles for [start, stop] (ISO dates, both candle starts; at most 300 days)."""
    return COINBASE + "?" + urlencode({"granularity": DAY, "start": f"{start}T00:00:00Z", "end": f"{stop}T00:00:00Z"})


def bitstamp_url(start_epoch: int, limit: int = BITSTAMP_MAX) -> str:
    return BITSTAMP + "?" + urlencode({"step": DAY, "limit": limit, "start": start_epoch})


def kraken_url() -> str:
    return KRAKEN + "?" + urlencode({"pair": "XBTUSD", "interval": 1440})


def ledger(cache: Path) -> Ledger:
    return Ledger(cache / "raw_index.csv.gz", cache / "quota_ledger.csv")


def _days(a: str, b: str) -> list[str]:
    d0, d1 = datetime.strptime(a, "%Y-%m-%d"), datetime.strptime(b, "%Y-%m-%d")
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days + 1)]


def fetch_coinbase(cache: Path, last: str, first: str = FIRST["coinbase"]) -> list[bytes]:
    """Every daily candle from ``first`` to ``last`` (candle starts), 300 a request, about 2 requests a second."""
    lim, led, out = SlidingWindowLimiter({1: 2}), ledger(cache), []
    days = _days(first, last)
    for i in range(0, len(days), COINBASE_MAX):
        a, b = days[i], days[min(i + COINBASE_MAX - 1, len(days) - 1)]
        out.append(cached_get(coinbase_url(a, b), cache / f"raw/coinbase/{a}_{b}.json", source="coinbase",
                              ledger=led, headers=HEADERS, limiter=lim))
    return out


def fetch_bitstamp(cache: Path, last: str, first: str = FIRST["bitstamp"]) -> list[bytes]:
    lim, led, out = SlidingWindowLimiter({1: 1}), ledger(cache), []
    t, stop = _epoch(first), _epoch(last)
    while t <= stop:
        n = min(BITSTAMP_MAX, (stop - t) // DAY + 1)
        a = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        out.append(cached_get(bitstamp_url(t, n), cache / f"raw/bitstamp/{a}_{n}.json", source="bitstamp",
                              ledger=led, headers=HEADERS, limiter=lim))
        t += n * DAY
    return out


def fetch_kraken(cache: Path, fetch_day: str) -> bytes:
    """Kraken's last 720 candles, cached under the fetch day (the response depends on when it is asked)."""
    return cached_get(kraken_url(), cache / f"raw/kraken/ohlc_1440_fetched_{fetch_day}.json", source="kraken",
                      ledger=ledger(cache), headers=HEADERS)


def fetch_yahoo(cache: Path, symbol: str, period1: str, period2: str) -> bytes:
    """Yahoo v8 chart JSON for [period1, period2) (BTC-USD, IBIT, FBTC ...)."""
    return cached_get(chart_url(symbol, period1, period2), cache / f"raw/yahoo/chart_{symbol}_{period1}_{period2}.json",
                      source="yahoo", ledger=ledger(cache), headers=YAHOO_HEADERS, symbol=symbol)
