"""Free-source survey pilot for a future data v2 of the reversal 2012-2026 data (docs/data_sources_survey.md).

Data only: no signal, strategy return or cross-stock return aggregate is computed. The frozen v1 inputs
(output/research_only/reversal_2012_2026/inputs/) and the v1 cache are only read, never written: this module points
the shared request ledger of scripts/reversal_data_common at its own cache before any request is made.

Steps (each cached on disk and re-runnable; raw vendor bodies stay under research_cache/data_source_probe/):
  gaplist     the pilot gap list (~70 securities) from the v1 gap files, ranked by estimated top-250 name-weeks,
              plus control securities with known-good v1 data for the agreement test.
  wayback     archive.org captures of Yahoo's old table.csv files and of its history pages (q/hp, quote/.../history).
  cmc         companiesmarketcap.com daily market cap (search -> defunct slug -> page).
  quantquote  the free QuantQuote S&P 500 daily archive (1998-2013), as archived on archive.org.
  eastmoney   push2his.eastmoney.com daily klines (markets 105/106/107).
  nasdaq      api.nasdaq.com historical (rolling ten years).
  yahooq      Yahoo v8 chart for the OTC "Q" tickers of D5 names (post-delisting price).
  report      coverage of each gap name's need window and return agreement with v1 on known-good days.

Usage:  PYTHONPATH=. /Users/bytedance/code/quant_stocks/.venv/bin/python scripts/data_source_probe.py <step>
Outputs that may go to git (counts, shares, IDs; no vendor price level): output/research_only/data_source_probe/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import html
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import reversal_data_common as common  # noqa: E402

MAIN = common.MAIN_CHECKOUT
V1_INPUTS = ROOT / "output/research_only/reversal_2012_2026/inputs"
V1_CACHE = MAIN / "research_cache/reversal_2012_2026"
CACHE = MAIN / "research_cache/data_source_probe"
RAW = CACHE / "raw"
OUT = ROOT / "output/research_only/data_source_probe"
# Own request ledger: the v1 raw_index / quota_ledger are never appended to by this probe.
common.RAW_INDEX = CACHE / "raw_index.csv.gz"
common.QUOTA_LEDGER = CACHE / "quota_ledger.csv"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120 Safari/537.36"}
WAYBACK_LIMITER = common.SlidingWindowLimiter({2: 1, 60: 15})  # archive.org refused connections at 30 a minute
CMC_LIMITER = common.SlidingWindowLimiter({3: 1})
EM_LIMITER = common.SlidingWindowLimiter({1: 1})
NASDAQ_LIMITER = common.SlidingWindowLimiter({2: 1})
YAHOO_LIMITER = common.SlidingWindowLimiter({2: 1})
AGREE_TOL = 0.005

# ======================================================================== pure helpers (tested)


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


def pick_spaced(stamps: list[str], min_gap_days: int, limit: int) -> list[str]:
    """Greedy pick of capture timestamps (YYYYMMDDhhmmss), latest first, at least ``min_gap_days`` apart."""
    out: list[pd.Timestamp] = []
    keep = []
    for s in sorted(set(stamps), reverse=True):
        t = pd.Timestamp(s[:8])
        if all(abs((t - u).days) >= min_gap_days for u in out):
            out.append(t)
            keep.append(s)
        if len(keep) >= limit:
            break
    return keep


def daily_returns(df: pd.DataFrame, col: str) -> pd.Series:
    """Return of ``col`` between consecutive rows (dates sorted, duplicates dropped, non-positive levels removed)."""
    s = df.drop_duplicates("date", keep="last").set_index("date")[col].astype(float).sort_index()
    s = s[s > 0]
    return (s / s.shift(1) - 1).dropna()


def agreement(new: pd.Series, ref: pd.Series, tol: float = AGREE_TOL) -> tuple[int, int]:
    """(overlap days, days with |new - ref| <= tol); both series indexed by date, the return into that date.
    Only days whose previous row is also the previous session in both series should be passed in."""
    j = pd.concat([new.rename("n"), ref.rename("r")], axis=1, join="inner").dropna()
    return len(j), int((np.abs(j["n"] - j["r"]) <= tol).sum())


def coverage(dates: pd.Series | pd.Index, sessions: pd.DatetimeIndex) -> float:
    if len(sessions) == 0:
        return float("nan")
    return float(pd.DatetimeIndex(pd.to_datetime(pd.Series(dates))).normalize().isin(sessions).sum()
                 / len(sessions)) if len(dates) else 0.0


# ======================================================================== gap list


def v1_sessions() -> pd.DatetimeIndex:
    p = CACHE / "sessions.csv"
    if not p.exists():
        d = pd.read_csv(V1_CACHE / "prices/daily_panel.csv.gz", usecols=["date"])["date"].value_counts()
        pd.Series(sorted(d[d >= 500].index)).to_csv(p, index=False, header=["date"])
    return pd.DatetimeIndex(pd.to_datetime(pd.read_csv(p)["date"]))


CONTROLS = (  # (ticker, security_id): v1 securities with multi-vendor days in 2012-2020, for the agreement test
    ("ALTR", "768251"), ("BRCM", "1054374"), ("DTV", "1465112"), ("LLTC", "791907"), ("XLNX", "743988"),
    ("CELG", "816284"), ("ESRX", "1532063"), ("MSFT", "789019"), ("INTC", "50863"), ("CSCO", "858877"),
    ("VIAB", "1339947.B"), ("MXIM", "743316"),
)
D5_PICK = ("NESR", "ASTI", "BBBY", "SIVB", "GTAT", "DNDN", "UPL", "PDLI", "EXXI", "SGYP")
NAMED = ("XPER", "SRGA", "TSRA", "QVCGB")


def build_gaplist() -> pd.DataFrame:
    m = pd.read_csv(V1_CACHE / "universe/missing_by_security.csv", dtype=str)
    m["est"] = m["est_top250_weeks"].astype(float)
    t = pd.read_csv(V1_INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    rows = []
    unf = m[m["missing_reason"] == "unfillable"].sort_values("est", ascending=False).head(42)
    for r in unf.itertuples():
        rows.append((r.security_id, r.ticker, r.name, "unfillable", r.first_missing, r.last_missing, r.est, r.delist_date))
    for tk in NAMED:
        r = m[m["ticker"] == tk].iloc[0]
        rows.append((r.security_id, tk, r["name"], "named_gap", r.first_missing, r.last_missing, r.est, r.delist_date))
    pend = m[m["missing_reason"] == "tiingo_pending"].sort_values("est", ascending=False).head(6)
    for r in pend.itertuples():
        rows.append((r.security_id, r.ticker, r.name, "tiingo_pending", r.first_missing, r.last_missing, r.est,
                     r.delist_date))
    d5 = t[t["status"] == "awaiting_d5"].set_index("ticker")
    for tk in D5_PICK:
        r = d5.loc[tk]
        a = r["last_price_date"] if isinstance(r["last_price_date"], str) else (
            (pd.Timestamp(r["delist_date"]) - pd.Timedelta(days=60)).strftime("%Y-%m-%d"))
        b = (pd.Timestamp(r["delist_date"]) + pd.Timedelta(days=30)).strftime("%Y-%m-%d")
        rows.append((r["security_id"], tk, r["name"], "d5_terminal", a, b, np.nan, r["delist_date"]))
    # securities with the most unresolved two-source disagreements (2011-2018): a third vote is the need
    p = pd.read_csv(V1_CACHE / "prices/daily_panel.csv.gz", usecols=["security_id", "date", "flags"], dtype=str)
    u = p[p["flags"].fillna("").str.contains("disagree_unresolved")]
    top = u.groupby("security_id").agg(n=("date", "size"), a=("date", "min"), b=("date", "max"))
    top = top.sort_values("n", ascending=False).head(8)
    sm = pd.read_csv(V1_INPUTS / "security_master.csv", dtype=str).set_index("security_id")
    for sid, r in top.iterrows():
        tk = sm.loc[sid, "first_ticker"] if sid in sm.index else sid
        nm = sm.loc[sid, "name"] if sid in sm.index else ""
        rows.append((sid, tk, nm, "unresolved_days", r.a, r.b, float(r.n), ""))
    g = pd.DataFrame(rows, columns=["security_id", "ticker", "name", "category", "need_start", "need_end",
                                    "est_top250_weeks", "delist_date"]).drop_duplicates("security_id")
    for tk, sid in CONTROLS:
        g.loc[len(g)] = [sid, tk, "", "control", "2012-01-01", "2020-12-31", np.nan, ""]
    OUT.mkdir(parents=True, exist_ok=True)
    g.to_csv(OUT / "gap_list.csv", index=False)
    print(g.groupby("category").size().to_string())
    return g


def gaplist() -> pd.DataFrame:
    return pd.read_csv(OUT / "gap_list.csv", dtype=str)


# ======================================================================== archive.org: old Yahoo CSV and history pages

CSV_HOSTS = ("ichart.finance.yahoo.com/table.csv", "real-chart.finance.yahoo.com/table.csv")  # the 2010-2017 hosts


def cdx(url: str, cache: Path, symbol: str, extra: str = "") -> list[list[str]]:
    q = ("https://web.archive.org/cdx/search/cdx?url=" + url + "&matchType=prefix&filter=statuscode:200"
         + extra + "&fl=timestamp,original,length&limit=5000")
    try:
        body = common.cached_get(q, cache, source="wayback_cdx", limiter=WAYBACK_LIMITER, timeout=240, symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        print(f"  cdx {symbol} {url}: {type(exc).__name__}")
        return []
    return [ln.split(" ") for ln in body.decode("utf-8", "replace").splitlines() if ln.count(" ") == 2]


def wayback_body(symbol: str, stamp: str, original: str, kind: str) -> str | None:
    url = f"https://web.archive.org/web/{stamp}id_/{original.replace('&amp;', '&')}"
    cache = RAW / f"wayback_{kind}" / f"{symbol}__{stamp}.gz"
    try:
        data = common.cached_get(url, cache, source="wayback", limiter=WAYBACK_LIMITER, timeout=240, symbol=symbol)
        if data[:2] == b"\x1f\x8b":        # archive.org sometimes returns the capture still gzip-encoded
            data = gzip.decompress(data)
        return data.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"  wayback {symbol} {stamp}: {type(exc).__name__}")
        return None


def probe_wayback(g: pd.DataFrame, workers: int = 2) -> None:
    """Three names at a time (archive.org answers a history page in about a minute); the shared limiter keeps
    the request rate at or under 30 a minute."""
    common.parallel_map(_wayback_one, list(g.itertuples()), workers=workers)


def _wayback_one(r, max_hp: int = 8, max_qh: int = 5) -> None:
    if True:
        sym = r.ticker
        a, b = pd.Timestamp(r.need_start), pd.Timestamp(r.need_end)
        frames, events, used = [], [], []
        # 1. old table.csv files
        for host in CSV_HOSTS:
            for ts, orig, ln in cdx(host, RAW / "cdx" / f"csv_{sym}__{host.split('/')[0]}.txt", sym,
                                    f"&filter=original:.*s={sym}.*"):
                if not re.search(rf"[?&;]s={re.escape(sym)}(&|$)", orig.replace("&amp;", "&")):
                    continue
                st, en = yahoo_csv_span(orig)
                end = pd.Timestamp(en) if en else pd.Timestamp(ts[:8])
                if int(ln) > 2000 and end >= a and (st is None or pd.Timestamp(st) <= b):
                    used.append(("csv", ts, orig, end))
        used.sort(key=lambda x: x[3], reverse=True)
        for kind, ts, orig, _ in used[:2]:
            body = wayback_body(sym, ts, orig, "csv")
            if body and body.startswith("Date,"):
                try:
                    frames.append(parse_yahoo_csv(body).assign(kind="csv"))
                except Exception:  # noqa: BLE001
                    pass
        # 2. history pages (q/hp to 2015; quote/X/history from 2016): captures inside the need window + 1 year
        pages = []
        syms = [sym] + ([sym + "Q"] if r.category == "d5_terminal" else [])   # OTC ticker after a bankruptcy
        for pat, kind, cs in [(f"finance.yahoo.com/q/hp?s={x}", "hp", x) for x in syms] + \
                [(f"finance.yahoo.com/quote/{x}/history", "qh", x) for x in syms]:
            for ts, orig, ln in cdx(pat, RAW / "cdx" / f"{kind}_{cs}.txt", cs):
                t = pd.Timestamp(ts[:8])
                if a <= t <= b + pd.Timedelta(days=365) and int(ln) > 5000:
                    if kind == "hp" and not re.search(rf"[?&]s={cs}($|&|\+)", orig, re.I):
                        continue
                    if kind == "hp" and re.search(r"[&?](a|b|c|d|e|f|g|z|y)=", orig):
                        continue  # paged / custom-range views: rarely archived, skip for the pilot
                    pages.append((ts, orig, kind))
        chosen = set(pick_spaced([p[0] for p in pages if p[2] == "hp"], 80, max_hp)
                     + pick_spaced([p[0] for p in pages if p[2] == "qh"], 300, max_qh))
        for ts, orig, kind in pages:
            if ts not in chosen:
                continue
            chosen.discard(ts)
            body = wayback_body(sym, ts, orig, kind)
            if not body:
                continue
            df, ev = (parse_yahoo_hp(body) if kind == "hp" else parse_yahoo_history_store(body))
            if kind == "qh" and df.empty:
                df, ev = parse_yahoo_table(body)
            if len(df):
                frames.append(df.assign(kind=kind, capture=ts))
                events += ev
        out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "close", "adj", "kind"])
        common.atomic_write(CACHE / "series/wayback" / f"{r.security_id}.csv.gz",
                            gzip.compress(out.to_csv(index=False).encode(), mtime=0))
        pd.DataFrame(events, columns=["date", "event"]).to_csv(CACHE / "series/wayback" / f"{r.security_id}_events.csv",
                                                               index=False)
        print(f"{sym}: csv captures {len(used)}, pages {len(pages)}, rows {len(out)}, events {len(events)}", flush=True)


# ======================================================================== companiesmarketcap.com


def cmc_search(query: str) -> list[dict]:
    cache = RAW / "cmc" / f"search__{re.sub(r'[^A-Za-z0-9]+', '_', query)}.json"
    p = Path(cache)
    if p.exists():
        return json.loads(p.read_text() or "[]")
    from urllib.request import Request, urlopen
    CMC_LIMITER.wait()
    req = Request("https://companiesmarketcap.com/search.do", data=urlencode({"query": query}).encode(),
                  headers={**UA, "Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=60) as resp:
        data = resp.read()
    common.log_request("companiesmarketcap", "https://companiesmarketcap.com/search.do", 200, data, p, query)
    common.atomic_write(p, data)
    return json.loads(data or b"[]")


def cmc_pick(results: list[dict], ticker: str, delist_year: int | None) -> dict | None:
    """The search result for ``ticker``. A delisted name takes only '<T>.defunct.<year>' within a year of its
    delisting (the bare '<T>' is today's holder of a reused ticker); a listed name takes only the bare '<T>'."""
    best, score = None, None
    for x in results:
        ident = str(x.get("identifier", ""))
        m = re.fullmatch(rf"{re.escape(ticker)}(?:\.defunct\.(\d{{4}}))?", ident, re.I)
        if not m or x.get("type") != "stock":
            continue
        yr = int(m.group(1)) if m.group(1) else None
        if delist_year:
            if yr is None or abs(yr - delist_year) > 1:
                continue
            s = abs(yr - delist_year)
        else:
            if yr is not None:
                continue
            s = 0
        if score is None or s < score:
            best, score = x, s
    return best


CMC_SUFFIX = r"(?i)\b(inc|corp|corporation|ltd|limited|holdings?|co|company|plc|llc|group|the|n\.?v|s\.?a|de)\b\.?"


def cmc_slugs(name: str) -> list[str]:
    """Slug guesses from a company name: 'BMC SOFTWARE INC' -> ['bmc-software', 'bmc']."""
    base = re.sub(r"/[A-Z]{2}/?$", "", str(name))
    base = re.sub(CMC_SUFFIX, " ", base)
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", base.lower().replace("&", " and ")).split() if w]
    out = []
    for k in (len(words), 2, 1):
        if 0 < k <= len(words):
            slug = "-".join(words[:k])
            if slug not in out:
                out.append(slug)
    return out


def cmc_guess(ticker: str, name: str, delist_year: int | None) -> dict | None:
    """A guessed slug is accepted only when the page title carries '(TICKER)' and, for a delisted name, its
    daily series ends within a year of the delisting."""
    for slug in cmc_slugs(name)[:3]:
        try:
            h = common.cached_get(f"https://companiesmarketcap.com/{slug}/marketcap/", RAW / "cmc" / f"{slug}__mcap.html.gz",
                                  source="companiesmarketcap", limiter=CMC_LIMITER, headers=UA, symbol=slug).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001  (404 cached as a marker)
            continue
        title = " ".join(re.findall(r"<title>(.*?)</title>", h, re.S))
        if f"({ticker.upper()})" not in title.upper():
            continue
        m = re.search(r"data = (\[.*?\]);", h, re.S)
        d = json.loads(m.group(1)) if m else []
        if not d:
            continue
        last = pd.Timestamp(d[-1]["d"], unit="s")
        if delist_year and abs(last.year - delist_year) > 1:
            continue
        return {"url": slug, "identifier": f"guess:{slug}"}
    return None


def probe_cmc(g: pd.DataFrame) -> None:
    rows = []
    for r in g.itertuples():
        yr = int(str(r.delist_date)[:4]) if isinstance(r.delist_date, str) and r.delist_date[:4].isdigit() else None
        if yr is None and r.category != "control" and pd.Timestamp(r.need_end) < pd.Timestamp("2026-06-01"):
            yr = pd.Timestamp(r.need_end).year      # renamed / moved names: the gap ends when the old line ends
        if r.category == "control" and r.ticker in ("ALTR", "BRCM", "DTV", "LLTC", "CELG", "ESRX", "VIAB", "MXIM",
                                                    "XLNX"):
            yr = {"ALTR": 2015, "BRCM": 2016, "DTV": 2015, "LLTC": 2017, "CELG": 2019, "ESRX": 2018, "VIAB": 2019,
                  "MXIM": 2021, "XLNX": 2022}[r.ticker]
        hit = None
        for q in (r.ticker, re.sub(r"(?i)\b(inc|corp|corporation|ltd|holdings?|co|plc|llc|group|the)\b\.?,?", "",
                                   str(r.name)).strip(" ,.") if isinstance(r.name, str) else ""):
            if not q:
                continue
            try:
                hit = cmc_pick(cmc_search(q), r.ticker, yr)
            except Exception as exc:  # noqa: BLE001
                print(f"  cmc search {q}: {type(exc).__name__}")
            if hit:
                break
        if not hit:
            hit = cmc_guess(r.ticker, r.name, yr)
        if not hit:
            rows.append({"security_id": r.security_id, "ticker": r.ticker, "slug": "", "identifier": ""})
            print(f"{r.ticker}: no cmc match", flush=True)
            continue
        slug = hit["url"]
        try:
            h = common.cached_get(f"https://companiesmarketcap.com/{slug}/marketcap/", RAW / "cmc" / f"{slug}__mcap.html.gz",
                                  source="companiesmarketcap", limiter=CMC_LIMITER, headers=UA, symbol=slug).decode("utf-8", "replace")
            sp = common.cached_get(f"https://companiesmarketcap.com/{slug}/stock-splits/", RAW / "cmc" / f"{slug}__splits.html.gz",
                                   source="companiesmarketcap", limiter=CMC_LIMITER, headers=UA, symbol=slug).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            print(f"  cmc {slug}: {type(exc).__name__}")
            continue
        m = re.search(r"data = (\[.*?\]);", h, re.S)
        d = json.loads(m.group(1)) if m else []
        s = pd.DataFrame({"date": [pd.Timestamp(x["d"], unit="s").normalize() for x in d],
                          "mcap": [float(x["m"]) for x in d]})
        splits = re.findall(r"<td[^>]*>(\d{4}-\d{2}-\d{2})</td>\s*<td[^>]*>\s*([\d\.]+)\s*(?:for|:|-)\s*([\d\.]+)", sp)
        common.atomic_write(CACHE / "series/cmc" / f"{r.security_id}.csv.gz",
                            gzip.compress(s.to_csv(index=False).encode(), mtime=0))
        rows.append({"security_id": r.security_id, "ticker": r.ticker, "slug": slug, "identifier": hit["identifier"],
                     "n_splits_listed": len(splits)})
        print(f"{r.ticker}: {hit['identifier']} rows {len(s)}", flush=True)
    pd.DataFrame(rows).to_csv(CACHE / "series/cmc/_matches.csv", index=False)


# ======================================================================== QuantQuote free S&P 500 daily (archive.org copy)

QQ_ZIP = RAW / "quantquote/quantquote_daily_sp500_83986.zip"
QQ_URL = "https://web.archive.org/web/20150602033648id_/http://quantquote.com/files/quantquote_daily_sp500_83986.zip"


def probe_quantquote(g: pd.DataFrame) -> None:
    if not QQ_ZIP.exists():
        common.cached_get(QQ_URL, QQ_ZIP, source="wayback", limiter=WAYBACK_LIMITER, timeout=1800, symbol="quantquote")
    z = zipfile.ZipFile(QQ_ZIP)
    names = {Path(n).name.lower(): n for n in z.namelist()}
    (CACHE / "series/quantquote").mkdir(parents=True, exist_ok=True)
    for r in g.itertuples():
        key = f"table_{r.ticker.lower()}.csv"
        if key not in names:
            continue
        df = pd.read_csv(z.open(names[key]), header=None,
                         names=["date", "time", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
        df[["date", "close"]].to_csv(CACHE / "series/quantquote" / f"{r.security_id}.csv.gz", index=False)
        print(f"{r.ticker}: {len(df)} rows {df.date.min().date()}..{df.date.max().date()}", flush=True)


# ======================================================================== Eastmoney, Nasdaq, Yahoo Q tickers


def probe_eastmoney(g: pd.DataFrame) -> None:
    for r in g.itertuples():
        for mk in ("105", "106", "107"):
            params = {"secid": f"{mk}.{r.ticker}", "fields1": "f1,f2,f3,f4,f5,f6",
                      "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": "101", "fqt": "0", "end": "20500000",
                      "lmt": "1000000"}
            url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urlencode(params)
            try:
                body = common.cached_get(url, RAW / "eastmoney" / f"{r.ticker}_{mk}.json.gz", source="eastmoney",
                                         limiter=EM_LIMITER, headers={**UA, "Referer": "https://quote.eastmoney.com/"},
                                         symbol=r.ticker)
            except Exception as exc:  # noqa: BLE001
                print(f"  em {r.ticker} {mk}: {type(exc).__name__}")
                continue
            data = (json.loads(body) or {}).get("data")
            if data and data.get("klines"):
                k = [x.split(",") for x in data["klines"]]
                df = pd.DataFrame({"date": pd.to_datetime([x[0] for x in k]), "close": [float(x[2]) for x in k],
                                   "vendor_name": data.get("name")})
                (CACHE / "series/eastmoney").mkdir(parents=True, exist_ok=True)
                df.to_csv(CACHE / "series/eastmoney" / f"{r.security_id}.csv.gz", index=False)
                print(f"{r.ticker} {mk}: {data.get('name')} {len(df)} rows {df.date.min().date()}..{df.date.max().date()}",
                      flush=True)
                break


def probe_nasdaq(g: pd.DataFrame) -> None:
    (CACHE / "series/nasdaq").mkdir(parents=True, exist_ok=True)
    for r in g.itertuples():
        b = pd.Timestamp(r.need_end)
        if b < pd.Timestamp("2016-10-01"):
            continue  # the API serves a rolling ten years only
        a = max(pd.Timestamp(r.need_start), pd.Timestamp("2016-10-06"))
        url = (f"https://api.nasdaq.com/api/quote/{r.ticker}/historical?assetclass=stocks&fromdate={a.date()}"
               f"&todate={b.date()}&limit=9999")
        try:
            body = common.cached_get(url, RAW / "nasdaq" / f"{r.ticker}_{a.date()}_{b.date()}.json.gz", source="nasdaq",
                                     limiter=NASDAQ_LIMITER, headers={**UA, "Accept": "application/json"},
                                     symbol=r.ticker)
        except Exception as exc:  # noqa: BLE001
            print(f"  nasdaq {r.ticker}: {type(exc).__name__}")
            continue
        j = json.loads(body)
        rows = (((j.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
        if rows:
            df = pd.DataFrame({"date": pd.to_datetime([x["date"] for x in rows]),
                               "close": [float(str(x["close"]).replace("$", "").replace(",", "")) for x in rows]})
            df.to_csv(CACHE / "series/nasdaq" / f"{r.security_id}.csv.gz", index=False)
        print(f"{r.ticker}: {len(rows)} rows", flush=True)


def probe_yahooq(g: pd.DataFrame) -> None:
    (CACHE / "series/yahooq").mkdir(parents=True, exist_ok=True)
    for r in g[g["category"] == "d5_terminal"].itertuples():
        a = int(pd.Timestamp(r.need_start).timestamp())
        b = int((pd.Timestamp(r.delist_date) + pd.Timedelta(days=60)).timestamp())
        for tk in (r.ticker + "Q", r.ticker + "F", r.ticker):
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{tk}?period1={a}&period2={b}&interval=1d"
                   "&events=div%2Csplits")
            try:  # the same request headers as data v1 (Yahoo answers 429 to a full browser User-Agent string)
                body = common.cached_get(url, RAW / "yahooq" / f"{tk}_{a}_{b}.json.gz", source="yahoo",
                                         limiter=YAHOO_LIMITER, headers={"User-Agent": "Mozilla/5.0",
                                                                         "Accept": "application/json"}, symbol=tk)
            except Exception as exc:  # noqa: BLE001
                print(f"  yahoo {tk}: {type(exc).__name__}")
                continue
            res = ((json.loads(body).get("chart") or {}).get("result") or [None])[0]
            ts = (res or {}).get("timestamp") or []
            if ts:
                q = res["indicators"]["quote"][0]
                df = pd.DataFrame({"date": pd.to_datetime(ts, unit="s").normalize(), "close": q["close"],
                                   "volume": q["volume"]}).dropna(subset=["close"])
                after = df[df["date"] > pd.Timestamp(r.delist_date) - pd.Timedelta(days=45)]
                df.assign(ticker=tk).to_csv(CACHE / "series/yahooq" / f"{r.security_id}_{tk}.csv.gz", index=False)
                print(f"{r.ticker} via {tk}: {len(df)} rows, {len(after)} near/after delisting "
                      f"({res['meta'].get('exchangeName')})", flush=True)
            else:
                print(f"{r.ticker} via {tk}: none", flush=True)


# ======================================================================== SEC: what the old equity got in a D5 bankruptcy

EQUITY = r"(?:existing|old|prepetition)?\s*(?:common stock|equity interests?|interests in (?:the )?(?:company|debtors?)|shares)"
ZERO = re.compile(r"(?is)" + EQUITY + r".{0,300}?(?:will|shall)\s+(?:not\s+receive|receive\s+no)\s+(?:any\s+)?"
                  r"(?:distribution|recovery|property|consideration)"
                  r"|(?:cancel+ed|extinguished|discharged)[^.]{0,200}?(?:without|and\s+receive\s+no)\s+(?:any\s+)?"
                  r"(?:distribution|recovery|consideration)"
                  r"|will\s+not\s+receive\s+or\s+retain\s+any\s+property"
                  r"|(?<!Intercompany )(?:Existing |Old |Parent )?(?:Equity )?Interests[^.]{0,200}?(?:cancel+ed|extinguished)"
                  r"[^.]{0,200}?(?:will|shall)\s+not\s+receive\s+any\s+(?:distribution|recovery)"
                  r"|receive\s+no\s+recovery")
POSITIVE = re.compile(r"(?is)holders\s+of\s+" + EQUITY + r"[^.]{0,200}?(?:will|shall)\s+receive\s+"
                      r"(?!no\b)(?:their\s+pro\s+rata|\$\s?[\d.]+|[\d,.]+\s+shares|warrants|rights|a\s+pro\s+rata|cash)")


def classify_plan_text(text: str) -> dict:
    """Counts of 'old equity gets nothing' and 'old equity receives something' statements (for hand review)."""
    t = re.sub(r"\s+", " ", text)
    z, pz = ZERO.findall(t), POSITIVE.findall(t)
    return {"zero": len(z), "positive": len(pz)}


def probe_sec_d5(_g: pd.DataFrame) -> None:
    t = pd.read_csv(V1_INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    d5 = t[t["status"] == "awaiting_d5"]
    rows = []
    for r in d5.itertuples():
        cik = int(str(r.cik).split(".")[0])
        dd = pd.Timestamp(r.delist_date)
        q = {"q": '"plan of reorganization"', "ciks": f"{cik:010d}", "forms": "8-K", "dateRange": "custom",
             "startdt": (dd - pd.Timedelta(days=365)).date().isoformat(),
             "enddt": (dd + pd.Timedelta(days=730)).date().isoformat()}
        url = "https://efts.sec.gov/LATEST/search-index?" + urlencode(q)
        try:
            hits = json.loads(common.cached_get(url, RAW / "sec" / f"efts_{cik}.json.gz", source="sec_efts",
                                                headers=common.sec_headers(), limiter=common.SEC_LIMITER,
                                                symbol=r.ticker))["hits"]["hits"]
        except Exception as exc:  # noqa: BLE001
            print(f"  efts {r.ticker}: {type(exc).__name__}")
            continue
        zero = pos = 0
        acc_zero = acc_pos = ""
        items = set()
        for h in hits[:10]:
            acc, doc = h["_id"].split(":", 1)
            items |= set(h["_source"].get("items") or [])
            durl = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
            try:
                body = common.cached_get(durl, RAW / "sec" / f"{cik}_{acc}_{doc}.gz", source="sec_docs",
                                         headers=common.sec_headers(), limiter=common.SEC_LIMITER, symbol=r.ticker)
            except Exception:  # noqa: BLE001
                continue
            c = classify_plan_text(html.unescape(re.sub(r"<[^>]+>", " ", body.decode("latin-1"))))
            if c["zero"] and not acc_zero:
                acc_zero = acc
            if c["positive"] and not acc_pos:
                acc_pos = acc
            zero += c["zero"]
            pos += c["positive"]
        rows.append({"ticker": r.ticker, "security_id": r.security_id, "cik": cik, "delist_date": r.delist_date,
                     "efts_hits": len(hits), "items_seen": ";".join(sorted(items)), "zero_statements": zero,
                     "positive_statements": pos, "first_zero_accession": acc_zero, "first_positive_accession": acc_pos})
        print(f"{r.ticker}: hits {len(hits)} zero {zero} positive {pos}", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT / "sec_d5_plan_evidence.csv", index=False)


# ======================================================================== report: coverage and agreement


def v1_series(security_id: str) -> pd.DataFrame | None:
    p = V1_CACHE / "prices" / f"{security_id}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, usecols=["date", "close_raw", "split_factor", "div_cash", "tr", "n_sources", "flags"])
    df["date"] = pd.to_datetime(df["date"])
    df["pr"] = df["close_raw"] * df["split_factor"] / df["close_raw"].shift(1) - 1
    good = (df["n_sources"].fillna(0) >= 2) & ~df["flags"].fillna("").str.contains("disagree|review|glitch")
    df["good"] = good
    return df


ENTITY_CUT: dict[str, pd.Timestamp] = {}   # security_id -> delisting date (filled by build_report)


def load_source(source: str, sid: str) -> pd.DataFrame | None:
    """A probed series. For a delisted gap name, a page that shows more than 5 sessions after the delisting is
    dropped whole (by then the ticker may belong to someone else); stray rows after the delisting are cut."""
    p = CACHE / "series" / source / f"{sid}.csv.gz"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    cut = ENTITY_CUT.get(sid)
    if cut is not None and source == "wayback" and "capture" in df:
        late = (df["date"] > cut + pd.Timedelta(days=5))
        n_late = late.groupby(df["capture"].fillna(-1)).transform("sum")
        df = df[(n_late <= 5) & ~late]
    return df if len(df) else None


SOURCE_COLS = {  # (source, column for returns, v1 column to compare: tr = total return, pr = price return)
    "wayback": ("adj", "tr"), "cmc": ("mcap", "pr"), "quantquote": ("close", "tr"), "eastmoney": ("close", "pr"),
    "nasdaq": ("close", "pr"),
}


def consecutive_returns(df: pd.DataFrame, col: str, sessions: pd.DatetimeIndex) -> pd.Series:
    """Returns only where the source's previous row is the previous session (no gap-spanning returns)."""
    s = df.drop_duplicates("date", keep="last").set_index("date")[col].astype(float).sort_index()
    s = s[(s > 0) & s.index.isin(sessions)]
    pos = sessions.get_indexer(s.index)
    r = s / s.shift(1) - 1
    ok = pd.Series(pos, index=s.index).diff() == 1
    return r[ok]


def build_report() -> None:
    g = gaplist()
    sessions = v1_sessions()
    ENTITY_CUT.update({r.security_id: pd.Timestamp(r.delist_date) for r in g.itertuples()
                       if r.category not in ("control", "d5_terminal") and isinstance(r.delist_date, str)})
    cov_rows, agr_rows = [], []
    for r in g.itertuples():
        a, b = pd.Timestamp(r.need_start), pd.Timestamp(r.need_end)
        need = sessions[(sessions >= a) & (sessions <= b)]
        v1 = v1_series(r.security_id)
        for src, (col, ref) in SOURCE_COLS.items():
            df = load_source(src, r.security_id)
            if df is None:
                cov_rows.append({"security_id": r.security_id, "ticker": r.ticker, "category": r.category,
                                 "source": src, "rows": 0, "need_sessions": len(need), "need_coverage": 0.0})
                continue
            if src == "wayback" and "kind" in df:
                df = df.sort_values("kind").drop_duplicates("date", keep="first")
            cov_rows.append({"security_id": r.security_id, "ticker": r.ticker, "category": r.category, "source": src,
                             "rows": len(df), "first": df["date"].min().date(), "last": df["date"].max().date(),
                             "need_sessions": len(need),
                             "need_coverage": round(coverage(df["date"][(df["date"] >= a) & (df["date"] <= b)], need), 4)})
            if v1 is not None and len(v1):
                rn = consecutive_returns(df, col, sessions)
                vv = v1.set_index("date")
                ok_ref = vv.loc[vv["good"], ref].dropna()
                n, k = agreement(rn, ok_ref)
                one = vv.loc[vv["n_sources"].fillna(0) == 1, ref].dropna()
                n1, k1 = agreement(rn, one)
                unres = vv.loc[vv["flags"].fillna("").str.contains("disagree_unresolved"), ref].dropna()
                n2, _ = agreement(rn, unres)
                agr_rows.append({"security_id": r.security_id, "ticker": r.ticker, "category": r.category,
                                 "source": src, "overlap_good_days": n, "agree_0p5pct": k,
                                 "share": round(k / n, 5) if n else np.nan, "single_source_days_covered": n1,
                                 "single_source_days_agree": k1, "unresolved_days_covered": n2})
    cov = pd.DataFrame(cov_rows)
    agr = pd.DataFrame(agr_rows)
    # new source against new source on the gap names (the second-source test where v1 has nothing)
    cross = []
    for r in g[g["category"] != "control"].itertuples():
        a, b = pd.Timestamp(r.need_start), pd.Timestamp(r.need_end)
        ser = {}
        for src, (col, ref) in SOURCE_COLS.items():
            df = load_source(src, r.security_id)
            if df is not None:
                if src == "wayback" and "kind" in df:
                    df = df.sort_values("kind").drop_duplicates("date", keep="first")
                rr = consecutive_returns(df, col, sessions)
                ser[src] = (rr[(rr.index >= a) & (rr.index <= b)], ref)
        names = sorted(ser)
        for i, s1 in enumerate(names):
            for s2 in names[i + 1:]:
                if ser[s1][1] != ser[s2][1] and "cmc" not in (s1, s2):
                    continue
                n, k = agreement(ser[s1][0], ser[s2][0])
                if n:
                    cross.append({"security_id": r.security_id, "ticker": r.ticker, "category": r.category,
                                  "pair": f"{s1}-{s2}", "days": n, "agree_0p5pct": k})
    pd.DataFrame(cross).to_csv(OUT / "cross_source_agreement_gap_names.csv", index=False)
    # D5: is there any close after the last Nasdaq session (an OTC price) within 30 days of the delisting?
    t = pd.read_csv(V1_INPUTS / "terminal_returns_2012_2026.csv", dtype=str).set_index("security_id")
    d5 = []
    for r in g[g["category"] == "d5_terminal"].itertuples():
        last = t.loc[r.security_id, "last_price_date"] if r.security_id in t.index else None
        start = pd.Timestamp(last) if isinstance(last, str) else pd.Timestamp(r.delist_date) - pd.Timedelta(days=60)
        end = pd.Timestamp(r.delist_date) + pd.Timedelta(days=30)
        found = {}
        for src in ("wayback", "yahooq"):
            if src == "yahooq":
                fs = sorted((CACHE / "series/yahooq").glob(f"{r.security_id}_*.csv.gz"))
                df = pd.concat([pd.read_csv(f) for f in fs]) if fs else None
            else:
                df = load_source(src, r.security_id)
            if df is None or df.empty:
                continue
            dd = pd.to_datetime(df["date"])
            after = dd[(dd > start) & (dd <= end)]
            if len(after):
                found[src] = (after.min().date(), len(after))
        d5.append({"security_id": r.security_id, "ticker": r.ticker, "last_nasdaq_price": last,
                   "delist_date": r.delist_date, **{f"{k}_first_after": v[0] for k, v in found.items()},
                   **{f"{k}_rows_after": v[1] for k, v in found.items()}})
    pd.DataFrame(d5).to_csv(OUT / "d5_post_delisting_prices.csv", index=False)
    # union of the sources that passed the identity rules (archive.org, companiesmarketcap, QuantQuote)
    uni = []
    for r in g[~g["category"].isin(["control", "d5_terminal"])].itertuples():
        a, b = pd.Timestamp(r.need_start), pd.Timestamp(r.need_end)
        need = sessions[(sessions >= a) & (sessions <= b)]
        got = {}
        for src in ("wayback", "cmc", "quantquote"):
            df = load_source(src, r.security_id)
            if df is not None:
                got[src] = set(pd.DatetimeIndex(df["date"]).normalize()) & set(need)
        u = set().union(*got.values()) if got else set()
        two = {d for d in u if sum(d in v for v in got.values()) >= 2}
        wk = pd.Series(need, index=need).groupby(need.to_period("W-FRI"))
        weeks_full = sum(all(d in u for d in v) for _, v in wk)
        est = float(r.est_top250_weeks) if r.est_top250_weeks == r.est_top250_weeks and r.est_top250_weeks else 0.0
        uni.append({"security_id": r.security_id, "ticker": r.ticker, "category": r.category,
                    "need_sessions": len(need), "union_coverage": round(len(u) / max(len(need), 1), 4),
                    "two_source_coverage": round(len(two) / max(len(need), 1), 4),
                    "est_top250_weeks": est, "est_weeks_x_union": round(est * len(u) / max(len(need), 1), 2),
                    "weeks": len(wk), "weeks_all_sessions": weeks_full,
                    "est_weeks_x_full_weeks": round(est * weeks_full / max(len(wk), 1), 2)})
    pd.DataFrame(uni).to_csv(OUT / "union_coverage_gap_names.csv", index=False)
    cov.to_csv(OUT / "coverage_by_name_source.csv", index=False)
    agr.to_csv(OUT / "agreement_by_name_source.csv", index=False)
    piv = cov.pivot_table(index=["security_id", "ticker", "category"], columns="source", values="need_coverage",
                          aggfunc="max").reset_index()
    piv.to_csv(OUT / "coverage_matrix.csv", index=False)
    summ = {}
    for src in SOURCE_COLS:
        c = cov[(cov["source"] == src) & (cov["category"] != "control")]
        s = agr[agr["source"] == src]
        summ[src] = {
            "gap_names_full_ge_0p95": int((c["need_coverage"] >= 0.95).sum()),
            "gap_names_partial_0p2_0p95": int(((c["need_coverage"] >= 0.2) & (c["need_coverage"] < 0.95)).sum()),
            "gap_names_any_row_in_window": int((c["need_coverage"] > 0).sum()),
            "agreement_days": int(s["overlap_good_days"].sum()), "agreement_within_0p5pct": int(s["agree_0p5pct"].sum()),
            "agreement_share": round(float(s["agree_0p5pct"].sum() / max(s["overlap_good_days"].sum(), 1)), 5),
            "single_source_days_covered": int(s["single_source_days_covered"].sum()),
            "unresolved_days_covered": int(s["unresolved_days_covered"].sum()),
        }
        by_cat = c.groupby("category")["need_coverage"].apply(lambda x: int((x >= 0.95).sum())).to_dict()
        summ[src]["full_by_category"] = by_cat
    (OUT / "summary.json").write_text(json.dumps({"built_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                                  "gap_names": int((g["category"] != "control").sum()),
                                                  "by_source": summ, "no_returns_aggregated": True,
                                                  "note": "coverage shares and agreement counts only; no vendor price "
                                                          "level and no cross-stock return aggregate"}, indent=2,
                                                 default=str) + "\n")
    print(json.dumps(summ, indent=1, default=str))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["gaplist", "wayback", "cmc", "quantquote", "eastmoney", "nasdaq", "yahooq", "sec_d5",
                                       "report"])
    ap.add_argument("--only", default="", help="comma-separated tickers")
    args = ap.parse_args(argv)
    if args.step == "gaplist":
        build_gaplist()
        return
    g = gaplist()
    if args.only:
        g = g[g["ticker"].isin(args.only.split(","))]
    {"wayback": probe_wayback, "cmc": probe_cmc, "quantquote": probe_quantquote, "eastmoney": probe_eastmoney,
     "nasdaq": probe_nasdaq, "yahooq": probe_yahooq, "sec_d5": probe_sec_d5, "report": lambda _g: build_report()}[args.step](g)


if __name__ == "__main__":
    main()
