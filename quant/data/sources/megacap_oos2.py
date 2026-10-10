"""Sources of the mega-cap out-of-sample test 1999-2013 without QuantConnect (docs/research_ledger_megacap.md, OOS.2).

Data only: nothing here computes a strategy return. Moved from scripts/megacap_oos2_data.py (phase 3): the
fetchers, parsers and readers live here; the steps that write the outputs (``qqq``, ``sec``) are in
pipelines/megacap_oos2/build.py (``scripts/megacap_oos2_data.py`` still runs them). Steps (each cached,
re-runnable):
  candidates  Nasdaq-100 Trust (QQQ) schedules of investments 1999-2013 from SEC (top 45 by value in any report)
              -> the candidate generator; names are mapped by hand to CIKs in CANDIDATES below (plus additions).
  filings     SEC submissions index per CIK -> 10-K / 10-Q (and 10-K405, 10-KT) filed 1998-06 .. 2013-12.
  covers      the first ~120 kB of each filing (HTTP Range) -> cover-page shares outstanding (+ as-of date), the
              section 12(b)/12(g) registration text, and the "closing price of $X on <date>" statement.
  prices      Yahoo v8 charts (raw close restored from split-adjusted close) -> Tiingo cache -> companiesmarketcap.com
              daily market-cap path (delisted names with no other source).

SEC requests carry src/io/sec_contact.sec_user_agent() (never printed), at most 7 a second (shared limiter).
Raw bodies stay under research_cache/megacap_oos2/ (git-ignored); outputs that go to git are SEC facts, IDs, ranks.

Usage:  PYTHONPATH=. .venv/bin/python scripts/megacap_oos2_data.py {qqq|sec} [--dei-only]   (the build)
        (qqq: Nasdaq-100 Trust schedules -> qqq_schedule_top45.csv, the candidate generator; sec: filings, covers,
        dei. Prices are fetched and assembled, from cache when present, by scripts/research_megacap_oos2.py.)
"""
from __future__ import annotations

import html
import json
import re

import numpy as np
import pandas as pd

from quant.data import version as dv
from quant.data.sources import http, sec
from quant.paths import CACHE_ROOT, OUTPUT_ROOT

CACHE = CACHE_ROOT / "megacap_oos2"
SEC_RAW = CACHE / "sec"
OUT = OUTPUT_ROOT / "megacap_oos2"

SOURCE = "sec_megacap_oos2"
QQQ_CIK = 1067839
FIRST_SIGNAL = "1998-12-31"
LAST_DAY = "2013-12-31"
FILINGS_FROM = "1997-10-01"        # a 10-K filed up to 400 days before the first signal can still be used
SHARE_FORMS = ("10-K", "10-K405", "10-Q", "10-KT", "10-K/A", "10-Q/A", "10-KT/A", "10-K405/A")
COVER_BYTES = 100_000


# ======================================================================== SEC plumbing

def cached_get(url: str, cache_path, **kwargs) -> bytes:
    """A cached GET logged to the reversal pipeline's request ledger of the current data version (as
    reversal_data_common.cached_get did for this module)."""
    return http.cached_get(url, cache_path, ledger=http.Ledger(dv.RAW_INDEX, dv.QUOTA_LEDGER), **kwargs)


def sec_get(url: str, cache_name: str, range_bytes: int | None = None) -> bytes:
    headers = sec.sec_headers()
    if range_bytes:
        headers = {**headers, "Range": f"bytes=0-{range_bytes - 1}"}
    return cached_get(url, SEC_RAW / cache_name, source=SOURCE, headers=headers,
                      limiter=sec.SEC_LIMITER, timeout=60)


def submissions(cik: int) -> dict:
    """The submissions index with older pages merged (form, filingDate, reportDate, accessionNumber, primaryDocument)."""
    main = json.loads(sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
                              f"submissions/CIK{cik:010d}.json.gz"))
    cols = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument")
    rec = {k: list(main["filings"]["recent"].get(k, [])) for k in cols}
    for f in main["filings"].get("files", []):
        page = json.loads(sec_get(f"https://data.sec.gov/submissions/{f['name']}", f"submissions/{f['name']}.gz"))
        for k in cols:
            rec[k] += list(page.get(k, []))
    return {"name": main.get("name"), "former": main.get("formerNames", []), "tickers": main.get("tickers", []),
            "exchanges": main.get("exchanges", []), "state": main.get("stateOfIncorporation"),
            "filings": pd.DataFrame(rec)}


def doc_url(cik: int, accession: str, primary: str) -> str:
    acc = accession.replace("-", "")
    if primary:
        return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{primary}"
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{accession}.txt"


def to_text(raw: bytes) -> str:
    """HTML or SGML text -> plain text with single spaces (tags removed, entities decoded)."""
    t = raw.decode("latin-1", errors="replace")
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|th|li|h\d)>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ")
    return t


# ======================================================================== QQQ schedules of investments (candidate generator)

QQQ_DOCS = (  # (fiscal year end of the schedule, accession, primary document); 485BPOS / N-30B-2 with the annual report
    ("1999-09-30", "0000912057-00-030669", "a485bpos.txt"),
    ("2000-09-30", "0000912057-01-003392", "a2036273z485bpos.txt"),
    ("2001-09-30", "0000912057-02-003384", "a2068197z485bpos.txt"),
    ("2002-09-30", "0001206774-03-000033", "d11781.txt"),
    ("2003-09-30", "0001206774-04-000023", "d13831.txt"),
    ("2004-09-30", "0001206774-05-000077", "d16120_s-6.txt"),
    ("2005-09-30", "0001047469-06-005571", "a2167148zn-30b_2.txt"),
    ("2006-09-30", "0001104659-07-003177", "a06-24326_3n30b2.htm"),
    ("2007-09-30", "0001104659-08-012645", "a08-3893_3n30b2.htm"),
    ("2008-09-30", "0001104659-09-005352", "a08-28674_2n30b2.htm"),
    ("2009-09-30", "0001104659-10-001821", "a09-31668_2n30b2.htm"),
    ("2010-09-30", "0001104659-10-064790", "a10-18275_1n30b2.htm"),
    ("2011-09-30", "0001104659-12-003105", "a11-28102_1n30b2.htm"),
    ("2012-09-30", "0001104659-13-005075", "a12-24340_1n30b2.htm"),
    ("2013-09-30", "0001104659-14-003107", "a13-21957_1n30b2.htm"),
)
HOLDING = re.compile(r"^\s*([A-Z0-9][^\n]*?[A-Za-z\)\.\*][^\n]*?)[\s\.]{2,}\$?\s*([\d,]{3,})\s+\$?\s*([\d,]{4,})\s*$", re.M)


# ======================================================================== Wayback copies of Yahoo's old table.csv

WAYBACK_HOSTS = ("ichart.finance.yahoo.com/table.csv", "real-chart.finance.yahoo.com/table.csv",
                 "ichart.yahoo.com/table.csv", "table.finance.yahoo.com/table.csv")
WAYBACK_LIMITER = http.SlidingWindowLimiter({1: 1, 60: 30})


def _yahoo_csv_span(url: str) -> tuple[str | None, str | None]:
    """(start, end) dates encoded in an old Yahoo table.csv URL (a/b/c = start month-1/day/year, d/e/f = end)."""
    q = dict(re.findall(r"[?&;](?:amp;)?([a-z])=([^&]*)", url.replace("&amp;", "&")))
    try:
        st = f"{int(q['c']):04d}-{int(q['a']) + 1:02d}-{int(q['b']):02d}" if {"a", "b", "c"} <= set(q) else None
        en = f"{int(q['f']):04d}-{int(q['d']) + 1:02d}-{int(q['e']):02d}" if {"d", "e", "f"} <= set(q) else None
    except ValueError:
        return None, None
    return st, en


def wayback_captures(symbol: str) -> pd.DataFrame:
    """CDX rows (timestamp, original, length) of archived Yahoo daily CSVs for ``symbol`` (status 200)."""
    rows = []
    for host in WAYBACK_HOSTS:
        url = ("https://web.archive.org/cdx/search/cdx?url=" + host + "&matchType=prefix&filter=statuscode:200"
               f"&filter=original:.*s={symbol}.*&fl=timestamp,original,length&limit=2000")
        cache = CACHE / "raw/wayback_cdx" / f"{symbol}__{host.split('/')[0]}.txt"
        try:
            body = cached_get(url, cache, source="wayback", limiter=WAYBACK_LIMITER, timeout=240, symbol=symbol)
        except Exception as exc:  # noqa: BLE001  (an unanswered CDX query is recorded as no captures)
            print(f"  cdx {symbol} {host}: {exc}")
            continue
        for line in body.decode().splitlines():
            parts = line.split(" ")
            if len(parts) == 3 and re.search(rf"[?&;]s={re.escape(symbol)}(&|$)", parts[1].replace("&amp;", "&")):
                st, en = _yahoo_csv_span(parts[1])
                g = re.search(r"[?&;](?:amp;)?g=([a-z])", parts[1].replace("&amp;", "&"))
                rows.append({"symbol": symbol, "timestamp": parts[0], "original": parts[1], "length": int(parts[2]),
                             "start": st, "end": en, "g": g.group(1) if g else "d"})
    return pd.DataFrame(rows, columns=["symbol", "timestamp", "original", "length", "start", "end", "g"])


def wayback_csv(symbol: str, timestamp: str, original: str) -> pd.DataFrame:
    """The archived Yahoo CSV body (Date, Open, High, Low, Close, Volume, Adj Close), oldest first."""
    url = f"https://web.archive.org/web/{timestamp}id_/{original}"
    cache = CACHE / "raw/wayback_yahoo" / f"{symbol}__{timestamp}.csv.gz"
    body = cached_get(url, cache, source="wayback", limiter=WAYBACK_LIMITER, timeout=240, symbol=symbol)
    text = body.decode("latin-1")
    if not text.startswith("Date,"):
        raise ValueError(f"{symbol} {timestamp}: not a Yahoo CSV")
    from io import StringIO
    df = pd.read_csv(StringIO(text))
    df["Date"] = pd.to_datetime(df["Date"])
    return df.sort_values("Date").reset_index(drop=True)


# ======================================================================== cover pages: shares outstanding, venue, closing price

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
MON_ABBR = "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
DATE_RE = re.compile(rf"\b((?:{MONTHS}|(?:{MON_ABBR})\.?)\s+\d{{1,2}}\s*,?\s*(?:19|20)\d\d)\b|\b(\d{{1,2}}/\d{{1,2}}/(?:19|20)?\d\d)\b",
                     re.I)
NUM_RE = re.compile(r"(?<![\$\d,\.])(\d{1,3}(?:,\d{3}){2,})(?![\d,]*\.\d)")


def parse_date(text: str):
    m = DATE_RE.search(text)
    if not m:
        return None
    raw = re.sub(r"\bSept\b", "Sep", (m.group(1) or m.group(2)).replace(".", ""))
    try:
        return pd.Timestamp(pd.to_datetime(re.sub(r"\s+", " ", raw)))
    except (ValueError, TypeError):
        return None


def cover_block(text: str, limit: int = 25_000) -> str:
    """Plain-text cover: from the start to 'PART I' / 'Table of Contents' / 'INDEX' (searched after the first 1.5 kB)."""
    t = re.sub(r"[ \t\r\f\v]+", " ", text)
    t = re.sub(r"\n\s*", "\n", t)
    # skip the SGML header of a full submission
    k = t.find("</SEC-HEADER>")
    if k >= 0:
        t = t[k:]
    stop = [m.start() for m in re.finditer(r"(?i)\bPART\s+I\b(?!I)|table of contents|\bINDEX\b|\bFORM 10-Q\s+INDEX",
                                           t[1500:limit])]
    return t[: (stop[0] + 1500) if stop else limit]


def parse_cover_shares(text: str) -> dict:
    """Shares outstanding stated on the cover (summed over classes when several are listed), its as-of date and the
    sentence it came from. Numbers preceded by '$' (market values, par values) are ignored."""
    cov = cover_block(text)
    flat = re.sub(r"\s+", " ", cov)
    # sentences / table rows mentioning outstanding shares
    flat = re.sub(r"[_=\-]{3,}|\[\s*[xX]?\s*\]|/\s*[xX]?\s*/", " | ", flat)
    pieces = [x for x in re.split(r"(?<=[\.;])\s+(?=[A-Z])|\s\|\s", flat) if x.strip()]
    best = None
    for i, p in enumerate(pieces):
        if not re.search(r"(?i)outstanding", p) or not re.search(r"(?i)share|common stock|class", p):
            continue
        window = p if NUM_RE.search(p) else " ".join(pieces[i:i + 2])
        if re.search(r"(?i)aggregate market value|held by non-?affiliates", window) and not re.search(
                r"(?i)(were|was|had)\s+(?:\S+\s+){0,30}outstanding|outstanding\s+(?:as of|at|on)", window):
            continue
        nums = []
        for m in NUM_RE.finditer(window):
            pre = window[max(0, m.start() - 3): m.start()]
            if "$" in pre:
                continue
            v = int(m.group(1).replace(",", ""))
            if v >= 1_000_000:
                nums.append((m.start(), v))
        for m in re.finditer(r"(?<![\$\d,\.])(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(million|billion)\b", window, re.I):
            if "$" in window[max(0, m.start() - 3): m.start()]:
                continue
            v = float(m.group(1).replace(",", "")) * (1e6 if m.group(2).lower() == "million" else 1e9)
            if v >= 1_000_000:
                nums.append((m.start(), int(round(v))))
        nums.sort()
        if not nums:
            continue
        if re.search(r"(?i)preferred", window) and re.search(r"(?i)respectively", window) and len(nums) >= 2:
            nums = nums[:-1]          # '... class A, class B and preferred stock was X, Y and Z, respectively'
        multi = len(re.findall(r"(?i)\bclass\b|\bseries\b", window)) >= 2 and len(nums) >= 2
        vals = [v for _, v in nums]
        total = sum(dict.fromkeys(vals)) if multi else vals[0]
        cand = {"shares": float(total), "n_numbers": len(nums), "multi_class": bool(multi),
                "asof": parse_date(window), "sentence": window[:400]}
        if best is None:
            best = cand
    return best or {"shares": np.nan, "n_numbers": 0, "multi_class": False, "asof": None, "sentence": ""}


def parse_cover_venue(text: str) -> dict:
    """Section 12(b) registration text on a 10-K cover -> 'NYSE', 'NASDAQ', 'AMEX', 'none_12b' (12(b): None) or ''."""
    flat = re.sub(r"\s+", " ", cover_block(text, 30_000))
    m = re.search(r"(?i)section\s*12\s*\(\s*b\s*\)(.{0,700}?)section\s*12\s*\(\s*g\s*\)", flat)
    seg = m.group(1) if m else ""
    gseg = flat[m.end(): m.end() + 400] if m else ""
    venue = ""
    if re.search(r"(?i)new york stock exchange|\bNYSE\b", seg):
        venue = "NYSE"
    elif re.search(r"(?i)nasdaq", seg):
        venue = "NASDAQ"
    elif re.search(r"(?i)american stock exchange", seg):
        venue = "AMEX"
    elif m and re.search(r"(?i)\bnone\b", seg):
        venue = "none_12b"
    nasdaq_word = bool(re.search(r"(?i)nasdaq", flat))
    return {"venue_12b": venue, "nasdaq_on_cover": nasdaq_word, "venue_text": (seg + " || " + gseg)[:300]}


def parse_cover_close(text: str) -> dict:
    """'... based on the closing (sale) price of $X ... on <date>' on a 10-K cover -> (price, date)."""
    flat = re.sub(r"\s+", " ", cover_block(text, 30_000))
    m = re.search(r"(?i)(?:closing|last)(?:\s+reported)?(?:\s+sale)?(?:\s+sales)?\s+price[^\$]{0,120}?\$\s?(\d{1,4}(?:\.\d+)?)"
                  r"(?![\d,])(.{0,160})", flat)
    if not m:
        return {"cover_close": np.nan, "cover_close_date": None}
    ctx = flat[max(0, m.start() - 200): m.end()]
    return {"cover_close": float(m.group(1).replace(",", "")), "cover_close_date": parse_date(m.group(2)) or parse_date(ctx)}


# ======================================================================== Yahoo v8 (live symbols)

YAHOO_CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1=896659200&period2={p2}"
               "&interval=1d&events=div%2Csplits&includeAdjustedClose=true")
YAHOO_LIMITER = http.SlidingWindowLimiter({2: 1})
YAHOO_FETCH_DAY = "2026-10-05"


def yahoo_chart(symbol: str) -> dict | None:
    p2 = int(pd.Timestamp(YAHOO_FETCH_DAY).timestamp())
    cache = CACHE / "raw/yahoo" / f"{symbol}__{YAHOO_FETCH_DAY}.json.gz"
    try:
        body = cached_get(YAHOO_CHART.format(symbol=symbol, p2=p2), cache, source="yahoo",
                                 headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                                 limiter=YAHOO_LIMITER, symbol=symbol, timeout=30)
    except (FileNotFoundError, Exception) as exc:  # noqa: BLE001
        print(f"  yahoo {symbol}: {exc}")
        return None
    res = json.loads(body).get("chart", {}).get("result")
    return res[0] if res else None


def yahoo_daily(symbol: str) -> pd.DataFrame | None:
    """date, close_adj (split-adjusted as served), close_raw (splits after the date restored), div_adj, split.
    Yahoo serves close adjusted for every split up to the fetch day: close_raw = close x product of later split ratios."""
    r = yahoo_chart(symbol)
    if r is None or not r.get("timestamp"):
        return None
    ts = pd.to_datetime(r["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    q = r["indicators"]["quote"][0]
    df = pd.DataFrame({"date": ts, "close_adj": q["close"]}).dropna().drop_duplicates("date", keep="last")
    ev = r.get("events", {})
    divs = {}
    for v in ev.get("dividends", {}).values():
        d = pd.Timestamp(v["date"], unit="s", tz="UTC").tz_convert("America/New_York").normalize().tz_localize(None)
        divs[d] = divs.get(d, 0.0) + float(v["amount"])
    splits = []
    for v in ev.get("splits", {}).values():
        d = pd.Timestamp(v["date"], unit="s", tz="UTC").tz_convert("America/New_York").normalize().tz_localize(None)
        splits.append((d, float(v["numerator"]) / float(v["denominator"])))
    df["div_adj"] = df["date"].map(divs).fillna(0.0)
    df["split"] = df["date"].map(dict(splits)).fillna(1.0)
    f = np.ones(len(df))
    for d, ratio in splits:
        f[df["date"].to_numpy() < np.datetime64(d)] *= ratio
    df["close_raw"] = df["close_adj"] * f
    df["source"] = "yahoo"
    return df.reset_index(drop=True)


# ======================================================================== candidates, filings and cover facts

CANDIDATES_IN = OUT / "candidates_input.csv"
COVER_FACTS = OUT / "sec_cover_facts.csv"
DEI_FACTS = OUT / "sec_dei_shares.csv"
BASE_FORMS = ("10-K", "10-K405", "10-Q", "10-KT", "10-QT")


def load_candidates() -> pd.DataFrame:
    c = pd.read_csv(CANDIDATES_IN, dtype=str, keep_default_na=False)
    return c


def cik_spans(text: str) -> list[tuple[int, str | None, str | None]]:
    """'777676..2005-01-31;1341439' -> [(777676, None, '2005-01-31'), (1341439, '2005-01-31', None)]."""
    out, prev_end = [], None
    for part in text.split(";"):
        cik, _, end = part.partition("..")
        out.append((int(cik), prev_end, end or None))
        prev_end = end or None
    return out


def plan_sources(text: str) -> list[tuple[str, str, str | None]]:
    """'wayback:SUNW..2007-03-01;cmc:sun-microsystems' -> [('wayback','SUNW','2007-03-01'), ('cmc','sun-microsystems',None)]."""
    out = []
    for part in [p for p in text.split(";") if p]:
        src, _, rest = part.partition(":")
        sym, _, until = rest.partition("..")
        out.append((src, sym, until or None))
    return out


def need_window(r) -> tuple[pd.Timestamp, pd.Timestamp]:
    a = pd.Timestamp(r.nasdaq_from) if r.nasdaq_from else pd.Timestamp("1998-06-01")
    b = pd.Timestamp(r.nasdaq_to) if r.nasdaq_to else pd.Timestamp(LAST_DAY)
    return max(a, pd.Timestamp("1998-06-01")), min(b, pd.Timestamp(LAST_DAY))


def best_wayback(symbol: str, need_a: pd.Timestamp, need_b: pd.Timestamp) -> list[pd.DataFrame]:
    """Download the archived daily CSVs that cover [need_a, need_b] best (the latest-ending capture that starts by
    need_a, plus, when it ends early, the capture that reaches furthest past it)."""
    caps = wayback_captures(symbol)
    caps = caps[(caps["g"] == "d") & caps["start"].notna() & caps["end"].notna() & (caps["length"] > 2000)].copy()
    if caps.empty:
        caps = wayback_captures(symbol)
        caps = caps[caps["length"] > 2000].copy()       # captures without span parameters (whole history)
        caps["start"], caps["end"] = "1900-01-01", caps["timestamp"].str[:8].apply(lambda x: f"{x[:4]}-{x[4:6]}-{x[6:]}")
    if caps.empty:
        return []
    caps["start"], caps["end"] = pd.to_datetime(caps["start"], errors="coerce"), pd.to_datetime(caps["end"], errors="coerce")
    caps = caps.dropna(subset=["start", "end"])
    got = []
    early = caps[caps["start"] <= need_a + pd.Timedelta(days=10)].sort_values(["end", "length"], ascending=False)
    pick = early.iloc[0] if len(early) else caps.sort_values("start").iloc[0]
    for row in [pick]:
        try:
            got.append(wayback_csv(symbol, row["timestamp"], row["original"]))
        except Exception as exc:  # noqa: BLE001
            print(f"  wayback {symbol} {row['timestamp']}: {exc}")
    last = max((g["Date"].max() for g in got), default=pd.Timestamp("1900-01-01"))
    if last < need_b - pd.Timedelta(days=10):
        later = caps[caps["end"] > last + pd.Timedelta(days=10)].sort_values(["end", "length"], ascending=False)
        for _, row in later.head(3).iterrows():
            try:
                got.append(wayback_csv(symbol, row["timestamp"], row["original"]))
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  wayback {symbol} {row['timestamp']}: {exc}")
    return got


# ======================================================================== companiesmarketcap.com (delisted names with no other daily source)

CMC_LIMITER = http.SlidingWindowLimiter({3: 1})
CMC_PAGES = {"marketcap": "marketcap", "price": "stock-price-history", "splits": "stock-splits"}


def cmc_page(slug: str, page: str) -> str:
    url = f"https://companiesmarketcap.com/{slug}/{CMC_PAGES[page]}/"
    cache = CACHE / "raw/cmc" / f"{slug}__{page}.html.gz"
    body = cached_get(url, cache, source="companiesmarketcap", limiter=CMC_LIMITER, timeout=60, symbol=slug,
                             headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"})
    return body.decode("utf-8", errors="replace")


def cmc_series(slug: str) -> dict:
    """Daily market cap (USD) and monthly split-adjusted close embedded in the pages ('data = [...]')."""
    out = {}
    for page, key in (("marketcap", "m"), ("price", "v")):
        h = cmc_page(slug, page)
        m = re.search(r"data = (\[.*?\]);", h, re.S)
        if not m:
            out[page] = pd.Series(dtype=float)
            continue
        d = json.loads(m.group(1))
        s = pd.Series({pd.Timestamp(x["d"], unit="s").normalize(): float(x[key]) for x in d}).sort_index()
        out[page] = s * (1e5 if page == "marketcap" else 1.0)        # market cap is served in units of $100k
    h = cmc_page(slug, "splits")
    rows = re.findall(r"<td[^>]*>(\d{4}-\d{2}-\d{2})</td>\s*<td[^>]*>\s*([\d\.]+)\s*(?:for|:|-)\s*([\d\.]+)", h)
    out["splits"] = [(pd.Timestamp(a), float(b) / float(c)) for a, b, c in rows]
    return out


# ======================================================================== Tiingo (only symbols already counted this month: no new unique symbol)

TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{t}/prices?startDate=1998-06-01&endDate=2013-12-31"
TIINGO_LIMITER = http.SlidingWindowLimiter({80: 1, 3600: 40})
TIINGO_DELISTED = CACHE_ROOT / "tiingo_delisted"


def tiingo_month_symbols() -> set:
    month = pd.Timestamp.now(tz="UTC").strftime("%Y-%m")
    q = pd.read_csv(dv.QUOTA_LEDGER, dtype=str, keep_default_na=False)
    return set(q.loc[(q["source"] == "tiingo") & (q["month"] == month), "symbol"].str.upper())


def tiingo_daily(symbol: str) -> pd.DataFrame | None:
    cache = CACHE / "raw/tiingo" / f"{symbol}__1998-06-01_2013-12-31.json.gz"
    if not cache.exists() and symbol.upper() not in tiingo_month_symbols():
        print(f"  tiingo {symbol}: not yet counted this month -> not asked (free-tier unique-symbol budget)")
        return None
    key = http.read_env_key(dv.DATA_MAIN / ".env.tiingo", "TIINGO_API_KEY")
    try:
        body = cached_get(TIINGO_URL.format(t=symbol.lower()), cache, source="tiingo", symbol=symbol.upper(),
                                 headers={"Authorization": f"Token {key}", "Content-Type": "application/json"},
                                 limiter=TIINGO_LIMITER, timeout=60)
    except Exception as exc:  # noqa: BLE001
        print(f"  tiingo {symbol}: {type(exc).__name__}")
        return None
    return _tiingo_frame(json.loads(body))


def tiingo_cached(name: str) -> pd.DataFrame | None:
    p = TIINGO_DELISTED / f"{name}.json"
    if not p.exists():
        return None
    return _tiingo_frame(json.loads(p.read_text())["prices"])


def _tiingo_frame(rows: list) -> pd.DataFrame | None:
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"].str[:10])
    out = pd.DataFrame({"date": df["date"], "close_raw": df["close"].astype(float), "adj": df["adjClose"].astype(float),
                        "split": df["splitFactor"].astype(float), "div_raw": df["divCash"].astype(float)})
    out["source"] = "tiingo"
    return out.sort_values("date").reset_index(drop=True)


CMC_SLUGS = {"MSFT": "microsoft", "INTC": "intel", "CSCO": "cisco", "ORCL": "oracle", "AAPL": "apple", "AMGN": "amgen",
             "QCOM": "qualcomm", "AMZN": "amazon", "GOOG": "alphabet-google", "CMCSA": "comcast",
             "AMAT": "applied-materials", "EBAY": "ebay", "GILD": "gilead-sciences", "COST": "costco",
             "SBUX": "starbucks", "BIIB": "biogen", "TXN": "texas-instruments", "MDLZ": "mondelez",
             "FB": "meta-platforms", "ADP": "automatic-data-processing", "YHOO": "yahoo", "JDSU": "viavi-solutions",
             "JNPR": "juniper-networks", "NTAP": "netapp", "MXIM": "maxim-integrated", "XLNX": "xilinx",
             "LLTC": "linear-technology-corp", "PAYX": "paychex", "NXTL": "nextel-communications", "WCOM": "worldcom",
             "SUNW": "sun-microsystems", "FITB": "fifth-third-bank", "NTRS": "northern-trust", "KRFT": "kraft-foods-group",
             "VRTS": "veritas-technologies", "QWST": "qwest-communications-international", "GBLX": "global-crossing"}


def cmc_year_end_caps(slug: str) -> dict:
    """{year: market cap in $} from the year table of a companiesmarketcap.com market-cap page."""
    h = cmc_page(slug, "marketcap")
    out = {}
    for y, v, unit in re.findall(r"<td>(\d{4})</td><td[^>]*>\$\s?([\d\.,]+)\s*([TBM])", h):
        out[int(y)] = float(v.replace(",", "")) * {"T": 1e12, "B": 1e9, "M": 1e6}[unit]
    return out


def within_cik_spans(df: pd.DataFrame, cand: pd.DataFrame, date_col: str = "filed") -> pd.DataFrame:
    """Rows (key, cik, filed) whose filing date lies inside that CIK's span for the candidate (120 days of grace
    after the end, as in candidate_filings)."""
    spans = {(r.key, cik): (a, b) for r in cand.itertuples() for cik, a, b in cik_spans(r.ciks)}
    d = pd.to_datetime(df[date_col])
    keep = []
    for k, c, t in zip(df["key"], df["cik"].astype(int), d):
        a, b = spans.get((k, c), (None, None)) if (k, c) in spans else ("x", "x")
        if a == "x":
            keep.append(False)
            continue
        ok = (not a or t > pd.Timestamp(a)) and (not b or t <= pd.Timestamp(b) + pd.Timedelta(days=120))
        keep.append(bool(ok))
    return df[np.array(keep, dtype=bool)]
