"""archive.org (Wayback Machine) captures of Yahoo Finance's old pages: URLs and parsers (phase 3).

Moved unchanged from the data-source probe (scripts/data_source_probe.py: the table.csv URL span and the page parsers
without volume) and from the version-2 archive fetcher (scripts/reversal_data_v2_archive.py: the URL facts, the
parsers with volume and ``raw_from_capture``, the canonical record of one capture). Used by the reversal pipeline
(``pipelines/reversal_data/v2_archive.py``, ``source_probe.py``) and the spin-off study. Fetching stays with each
caller (its own cache folder and request ledger); ``capture_url`` is the raw-body address they all use.

Page layouts: an old table.csv file (``ichart.finance.yahoo.com/table.csv?s=T&a..f``, the a/b/c and d/e/f
parameters are the start and end month-1/day/year), the 2004-2015 ``finance.yahoo.com/q/hp`` page (about the latest
66 sessions), and ``finance.yahoo.com/quote/T/history`` (2016-2022 a HistoricalPriceStore JSON, 2023 on an HTML
table, about a year).
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re

import numpy as np
import pandas as pd

CDX = "https://web.archive.org/cdx/search/cdx"


def capture_url(stamp: str, original: str) -> str:
    """The raw body of the capture of ``original`` taken at ``stamp`` (``id_``: as archived, no Wayback banner)."""
    return f"https://web.archive.org/web/{stamp}id_/{original.replace('&amp;', '&')}"


# ======================================================================== the probe's parsers (no volume)


def yahoo_csv_span(url: str) -> tuple[str | None, str | None]:
    """(start, end) of an old Yahoo table.csv URL: a/b/c = start month-1/day/year, d/e/f = end."""
    q = dict(re.findall(r"[?&;](?:amp;)?([a-z])=([^&]*)", url.replace("&amp;", "&")))
    try:
        st = f"{int(q['c']):04d}-{int(q['a']) + 1:02d}-{int(q['b']):02d}" if {"a", "b", "c"} <= set(q) else None
        en = f"{int(q['f']):04d}-{int(q['d']) + 1:02d}-{int(q['e']):02d}" if {"d", "e", "f"} <= set(q) else None
    except ValueError:
        return None, None
    return st, en


def parse_yahoo_hp(text: str) -> tuple[pd.DataFrame, list]:
    """Rows (date, close, adj) and events of an old finance.yahoo.com/q/hp page (2004-2015 layout)."""
    rows = re.findall(r'<td class="yfnc_tabledata1" nowrap align="right">([A-Z][a-z]{2} \d{1,2}, \d{4})</td>'
                      r'((?:<td class="yfnc_tabledata1" align="right">[^<]*</td>){6})', text)
    out = []
    for d, cells in rows:
        v = re.findall(r">([^<]*)</td>", cells)
        try:
            out.append({"date": pd.Timestamp(dt.datetime.strptime(d, "%b %d, %Y")),
                        "close": float(v[3].replace(",", "")), "adj": float(v[5].replace(",", ""))})
        except ValueError:
            continue
    ev = re.findall(r'nowrap align="right">([A-Z][a-z]{2} \d{1,2}, \d{4})</td><td class="yfnc_tabledata1" '
                    r'align="center" colspan="6">([^<]*)</td>', text)
    events = [(pd.Timestamp(dt.datetime.strptime(d, "%b %d, %Y")), e.strip()) for d, e in ev]
    return pd.DataFrame(out, columns=["date", "close", "adj"]), events


def parse_yahoo_history_store(text: str) -> tuple[pd.DataFrame, list]:
    """Rows and events of the 2016-2022 finance.yahoo.com/quote/X/history page (HistoricalPriceStore JSON)."""
    key = '"HistoricalPriceStore":'
    i = text.find(key)
    if i < 0:
        return pd.DataFrame(columns=["date", "close", "adj"]), []
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i + len(key):])
    except ValueError:
        return pd.DataFrame(columns=["date", "close", "adj"]), []
    rows, events = [], []
    for x in obj.get("prices", []):
        day = pd.Timestamp(dt.datetime.fromtimestamp(int(x["date"]), dt.timezone.utc).date())
        if "type" in x:
            events.append((day, f"{x['type']}:{x.get('amount', x.get('splitRatio', ''))}"))
        elif x.get("close") is not None and x.get("adjclose") is not None:
            rows.append({"date": day, "close": float(x["close"]), "adj": float(x["adjclose"])})
    return pd.DataFrame(rows, columns=["date", "close", "adj"]), events


def parse_yahoo_table(text: str) -> tuple[pd.DataFrame, list]:
    """Rows and events of the 2023+ finance.yahoo.com/quote/X/history page (an HTML table: Date, Open, High,
    Low, Close, Adj Close, Volume; cells may wrap values in <span>)."""
    cell = r"<td[^>]*>\s*(?:<span[^>]*>)?\s*([^<]*?)\s*(?:</span>)?\s*</td>\s*"
    rows = re.findall(r"<tr[^>]*>\s*" + cell.replace("([^<]*?)", r"([A-Z][a-z]{2} \d{1,2}, \d{4})") + "(" + cell * 6 + ")",
                      text)
    out, events = [], []
    for d, cells, *_ in rows:
        v = re.findall(r">\s*([^<>]*?)\s*<", cells)
        v = [x for x in v if x.strip()]
        try:
            day = pd.Timestamp(dt.datetime.strptime(d, "%b %d, %Y"))
            out.append({"date": day, "close": float(v[3].replace(",", "")), "adj": float(v[4].replace(",", ""))})
        except (ValueError, IndexError):
            continue
    for d, e in re.findall(r"(?i)<td[^>]*>\s*(?:<span[^>]*>)?([A-Z][a-z]{2} \d{1,2}, \d{4})(?:</span>)?\s*</td>\s*"
                           r"<td[^>]*colspan[^>]*>(.{0,200}?)</td>", text):
        e = re.sub(r"<[^>]+>", " ", e)
        if re.search(r"(?i)dividend|split", e):
            events.append((pd.Timestamp(dt.datetime.strptime(d, "%b %d, %Y")), " ".join(e.split())))
    return pd.DataFrame(out, columns=["date", "close", "adj"]), events


def parse_yahoo_csv(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text))
    return pd.DataFrame({"date": pd.to_datetime(df["Date"]), "close": df["Close"].astype(float),
                         "adj": df["Adj Close"].astype(float)})


# ======================================================================== the v2 fetcher's helpers and parsers (with volume)


def csv_url_facts(original: str) -> dict:
    """Ticker, kind (daily / dividends / other) and a..f span of an old Yahoo table.csv URL."""
    url = original.replace("&amp;", "&")
    q = dict(re.findall(r"[?&;]([a-zA-Z])=([^&]*)", url))
    g = q.get("g", "d").lower()
    kind = {"d": "daily", "v": "dividends"}.get(g, "other")
    start, end = yahoo_csv_span(url)
    return {"ticker": q.get("s", "").upper(), "kind": kind, "start": start, "end": end}


def hp_url_facts(original: str) -> dict:
    """Ticker and whether a q/hp URL is the default (latest) view; custom a..f views give their range, paged
    views (``y`` > 0) are skipped."""
    url = original.replace("&amp;", "&")
    q = dict(re.findall(r"[?&;]([a-zA-Z])=([^&]*)", url))
    tick = re.sub(r"[^A-Z0-9.\-]", "", q.get("s", "").split("+")[0].upper())
    if q.get("y", "0") not in ("", "0"):
        return {"ticker": tick, "usable": False}
    start, end = yahoo_csv_span(url)
    g = q.get("g", "d").lower()
    return {"ticker": tick, "usable": g in ("d", ""), "start": start, "end": end}


def split_ratio(text: str) -> float | None:
    """New shares per old share from a Yahoo split text ('2: 1 Stock Split', 'SPLIT:2:1', '1:16 Stock Splits',
    'SPLIT:3/2')."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)", str(text))
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    return a / b if a > 0 and b > 0 else None


def dividend_amount(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", str(text).replace(",", ""))
    return float(m.group(1)) if m and re.search(r"(?i)div", str(text)) else None


def raw_from_capture(rows: pd.DataFrame, events: list[tuple]) -> pd.DataFrame:
    """The canonical record of one capture: raw close, raw volume, S and D per row.

    A capture's Close is either as traded (the old q/hp pages and table.csv files) or split-adjusted as of the
    capture (quote/T/history). Which one is decided at each in-window split by the capture's own Adj Close: the
    reading whose return on the ex-date is nearer the Adj Close return wins (``mode`` raw / adjusted / none when
    the window holds no split). Raw = Close x the product of the split ratios after the row (adjusted mode),
    volume / that product, dividends x that product."""
    df = rows.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    splits = {}
    divs = {}
    for day, text in events:
        day = pd.Timestamp(day).normalize()
        r = split_ratio(text) if re.search(r"(?i)split", str(text)) else None
        if r and abs(r - 1) > 1e-9:
            splits[day] = splits.get(day, 1.0) * r
        a = dividend_amount(text)
        if a is not None and not re.search(r"(?i)split", str(text)):
            divs[day] = divs.get(day, 0.0) + a
    lo, hi = (df["date"].min(), df["date"].max()) if len(df) else (None, None)
    in_window = {d: r for d, r in splits.items() if lo is not None and lo < d <= hi}
    votes = []
    pos = {d: i for i, d in enumerate(df["date"])}
    for d, r in in_window.items():
        i = pos.get(d)
        if i is None or i == 0:
            continue
        c0, c1 = df.loc[i - 1, "close"], df.loc[i, "close"]
        a0, a1 = df.loc[i - 1, "adj"], df.loc[i, "adj"]
        if not (c0 > 0 and c1 > 0 and a0 > 0 and a1 > 0):
            continue
        truth = a1 / a0 - 1
        votes.append(abs(c1 * r / c0 - 1 - truth) < abs(c1 / c0 - 1 - truth))
    mode = "none" if not votes else ("raw" if sum(votes) * 2 >= len(votes) else "adjusted")
    factor = np.ones(len(df))
    if mode == "adjusted":
        for d, r in in_window.items():
            factor[df["date"].values < np.datetime64(d)] *= r
    out = df.copy()
    out["close_raw"] = df["close"].values * factor
    vol = df["volume"] if "volume" in df else pd.Series(np.nan, index=df.index)
    out["volume_raw"] = vol.astype(float).values / factor
    out["split"] = [splits.get(d, 1.0) for d in df["date"]]
    out["div"] = [divs.get(d, 0.0) * (factor[i] if mode == "adjusted" else 1.0) for i, d in enumerate(df["date"])]
    out["mode"] = mode
    return out


def parse_hp(text: str) -> tuple[pd.DataFrame, list]:
    rows = re.findall(r'<td class="yfnc_tabledata1" nowrap align="right">([A-Z][a-z]{2} \d{1,2}, \d{4})</td>'
                      r'((?:<td class="yfnc_tabledata1" align="right">[^<]*</td>){6})', text)
    out = []
    for d, cells in rows:
        v = re.findall(r">([^<]*)</td>", cells)
        try:
            out.append({"date": pd.Timestamp(dt.datetime.strptime(d, "%b %d, %Y")),
                        "close": float(v[3].replace(",", "")), "volume": float(v[4].replace(",", "") or "nan"),
                        "adj": float(v[5].replace(",", ""))})
        except ValueError:
            continue
    _, events = parse_yahoo_hp(text)
    return pd.DataFrame(out, columns=["date", "close", "volume", "adj"]), events


def parse_qh_store(text: str) -> tuple[pd.DataFrame, list]:
    key = '"HistoricalPriceStore":'
    i = text.find(key)
    if i < 0:
        return pd.DataFrame(columns=["date", "close", "volume", "adj"]), []
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i + len(key):])
    except ValueError:
        return pd.DataFrame(columns=["date", "close", "volume", "adj"]), []
    rows, events = [], []
    for x in obj.get("prices", []):
        day = pd.Timestamp(dt.datetime.fromtimestamp(int(x["date"]), dt.timezone.utc).date())
        if "type" in x:
            t = str(x["type"]).upper()
            if t == "SPLIT":
                events.append((day, f"SPLIT:{x.get('numerator', '')}:{x.get('denominator', '')}"
                               if x.get("numerator") else f"SPLIT:{x.get('splitRatio', '')}"))
            elif t == "DIVIDEND":
                events.append((day, f"DIVIDEND:{x.get('amount', x.get('data', ''))}"))
        elif x.get("close") is not None and x.get("adjclose") is not None:
            rows.append({"date": day, "close": float(x["close"]), "volume": float(x.get("volume") or np.nan),
                         "adj": float(x["adjclose"])})
    return pd.DataFrame(rows, columns=["date", "close", "volume", "adj"]), events


def parse_qh_table(text: str) -> tuple[pd.DataFrame, list]:
    cell = r"<td[^>]*>\s*(?:<span[^>]*>)?\s*([^<]*?)\s*(?:</span>)?\s*</td>\s*"
    rows = re.findall(r"<tr[^>]*>\s*" + cell.replace("([^<]*?)", r"([A-Z][a-z]{2} \d{1,2}, \d{4})") + "(" + cell * 6 + ")",
                      text)
    out = []
    for d, cells, *_ in rows:
        v = [x for x in re.findall(r">\s*([^<>]*?)\s*<", cells) if x.strip()]
        try:
            day = pd.Timestamp(dt.datetime.strptime(d, "%b %d, %Y"))
            vol = v[5].replace(",", "") if len(v) > 5 else ""
            out.append({"date": day, "close": float(v[3].replace(",", "")), "adj": float(v[4].replace(",", "")),
                        "volume": float(vol) if re.fullmatch(r"[\d.]+", vol) else np.nan})
        except (ValueError, IndexError):
            continue
    _, events = parse_yahoo_table(text)
    return pd.DataFrame(out, columns=["date", "close", "volume", "adj"]), events


def parse_csv(text: str) -> pd.DataFrame:
    from io import StringIO
    df = pd.read_csv(StringIO(text))
    return pd.DataFrame({"date": pd.to_datetime(df["Date"]), "close": df["Close"].astype(float),
                         "volume": df["Volume"].astype(float) if "Volume" in df else np.nan,
                         "adj": df["Adj Close"].astype(float)})


def parse_body(kind: str, text: str) -> tuple[pd.DataFrame, list]:
    if kind == "csv":
        if not text.startswith("Date,"):
            return pd.DataFrame(columns=["date", "close", "volume", "adj"]), []
        try:
            return parse_csv(text), []
        except Exception:  # noqa: BLE001
            return pd.DataFrame(columns=["date", "close", "volume", "adj"]), []
    if kind == "div":
        events = []
        for line in text.splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0].strip()):
                try:
                    events.append((pd.Timestamp(parts[0].strip()), f"DIVIDEND:{float(parts[1])}"))
                except ValueError:
                    pass
        return pd.DataFrame(columns=["date", "close", "volume", "adj"]), events
    if kind == "hp":
        return parse_hp(text)
    df, ev = parse_qh_store(text)
    if df.empty:
        df, ev = parse_qh_table(text)
    return df, ev
