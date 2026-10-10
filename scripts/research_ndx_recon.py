"""Nasdaq-100 reconstitution effect, 2012-2026 (docs/research_ledger_ndx_recon.md; rules registered in its section 0).

Stages (each reads what the one before wrote):
  events   Wikipedia "Historical components of the Nasdaq-100" change table (cached revision) + the hand table
           below -> output/research_only/ndx_recon/events_raw.csv (provisional announcement dates)
  verify   Nasdaq press releases on GlobeNewswire, found through archive.org's CDX index day by day around each
           expected announcement and read from archived copies (publication time in UTC -> ET)
           -> press_releases.csv, hand_check.csv; final event list -> events.csv (IDs, dates, sources only)
  prices   price series per event name (data v2 panel, Yahoo v8, Quandl WIKI) -> research_cache/ndx_recon/series/
           (local only); coverage -> price_coverage.csv
  run      event-time abnormal returns, R1 / R2 / R4 on H1 and H2, R3 diagnostic -> output/research_only/ndx_recon/

Usage::

    REVERSAL_DATA_VERSION=v2 PYTHONPATH=. python scripts/research_ndx_recon.py events
    REVERSAL_DATA_VERSION=v2 PYTHONPATH=. python scripts/research_ndx_recon.py verify
    REVERSAL_DATA_VERSION=v2 PYTHONPATH=. python scripts/research_ndx_recon.py prices
    REVERSAL_DATA_VERSION=v2 PYTHONPATH=. python scripts/research_ndx_recon.py run

Vendor prices never leave research_cache/; outputs carry returns, dates, tickers and source URLs only.
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import math
import re
import sys
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import research_spinoffs_sec as sec  # noqa: E402

OUT = ROOT / "output/research_only/ndx_recon"
MAIN = Path("/Users/bytedance/code/quant_stocks")
CACHE = MAIN / "research_cache/ndx_recon"
RAW = CACHE / "raw"
SERIES = CACHE / "series"
V2_PRICES = MAIN / "research_cache/reversal_2012_2026_v2/prices"
V2_INPUTS = ROOT / "output/research_only/reversal_2012_2026/inputs_v2"
WIKI_PRICES = MAIN / "research_cache/reversal_2012_2026/wiki/by_ticker"

WIKI_PAGE = "Historical_components_of_the_Nasdaq-100"
WIKI_REVID = 1378871158          # 2026-10-06T17:02:05Z, the revision read for this study
WINDOW = ("2012-01-01", "2026-09-30")

ACCOUNT = 10_000.0
SLOTS = 10
HOLD_R2 = 20                     # trading days after the effective date
POST_DAYS = 20
HALVES = {"H1": ("2012-01-01", "2018-12-31"), "H2": ("2019-01-01", "2026-09-30")}
ONEQ_HALF_SPREAD = 2e-4
BONFERRONI_T = 2.39              # two-sided 5% over the 3 judged rules (reported only)
RULES = ("R1", "R2", "R4")

# --------------------------------------------------------------------------------------------- hand table
# Corrections to the Wikipedia rows, decided from the press releases / SEC filings before any return was computed
# (ledger 1.2). Keys: (inclusion date as in Wikipedia, added ticker or "", removed ticker or "").
#   inclusion   corrected first day in (out of) the index
#   ann         announcement date (ET) when Wikipedia has none or a wrong one; replaced by a GlobeNewswire date
#               in ``verify`` when one is found
#   add_type / del_type  override of the classification
HAND = {
    ("February 16, 2016", "CSX", "KLAC"): {"inclusion": "2016-02-22",
                                           "note": "release title: CSX joins on February 22, 2016"},
    ("March 18, 2013", "KRFT", "STRZA"): {"ann": "", "note": "Wikipedia cites the 2012 Kraft Foods release"},
    ("November 18, 2013", "MAR", "GOLD"): {"ann": "", "note": "Wikipedia date is the access date"},
    ("July 2, 2015", "KHC", "KRFT"): {"add_type": "continuity", "del_type": "corporate",
                                      "note": "Kraft Foods merged into Kraft Heinz (same index slot)"},
    ("January 15, 2013", "STRZA", "LMCA"): {"add_type": "spin", "del_type": "corporate",
                                            "note": "Liberty Media spun off Starz; the old LMCA became Starz"},
    ("July 27, 2015", "BMRN", ""): {"ann": "", "note": "Wikipedia cites BioMarin's own release"},
    ("October 29, 2013", "VIP", "DELL"): {"ann": "", "note": "Wikipedia cites a news story of the delisting day"},
    ("July 17, 2023", "TTD", "ATVI"): {"ann": "", "note": "Wikipedia cites a news story"},
    ("June 20, 2023", "ON", "RIVN"): {"ann": "", "note": "Wikipedia cites a news story"},
    ("June 24, 2024", "ARM", "SIRI"): {"ann": "2024-06-13", "note": "Nasdaq release dated June 13, 2024"},
    ("November 18, 2024", "APP", "DLTR"): {"ann": "", "note": "Wikipedia cites a news story"},
    ("December 20, 2021", "PANW", "CERN"): {"del_type": "index",
                                            "note": "removed in the annual review (announced 2021-12-10); "
                                                    "the Oracle deal was announced later, on 2021-12-20"},
}

SPIN_WORDS = ("spun off", "spin-off", "spin off", "tracking stock", "tracking stocks")


# ============================================================================== Wikipedia event table

def wiki_text() -> str:
    url = ("https://en.wikipedia.org/w/api.php?action=query&prop=revisions&rvprop=ids|timestamp|content"
           f"&rvslots=main&format=json&revids={WIKI_REVID}")
    data = sec.cached_get(url, "../../ndx_recon/raw/wiki", headers={"User-Agent": "Mozilla/5.0 research"},
                          limiter=sec.WAYBACK_LIMIT, name=f"rev_{WIKI_REVID}")
    page = next(iter(json.loads(data)["query"]["pages"].values()))
    return page["revisions"][0]["slots"]["main"]["*"]


def _refs(cell: str, named: dict) -> list[dict]:
    refs = re.findall(r"<ref[^>/]*?>(.*?)</ref>", cell, re.S)
    refs += [named.get(n.strip(), "") for n in re.findall(r'<ref\s+name="?([^">/]+?)"?\s*/>', cell)]
    out = []
    for x in refs:
        get = lambda k: (re.search(r"\|\s*" + k + r"\s*=\s*([^|}]+)", x) or [None, ""])[1].strip()  # noqa: E731
        out.append({"date": get("date"), "url": get("url"), "title": get("title")})
    return out


def parse_wiki(text: str) -> pd.DataFrame:
    """One row per (date, added, removed) line of the changes table."""
    i = text.find('id="changes"')
    j = text.find("\n|}", i)
    body = text[i:j]
    named = {m.group(1).strip(): m.group(2)
             for m in re.finditer(r'<ref\s+name="?([^">/]+?)"?\s*>(.*?)</ref>', text, re.S)}
    rows = []
    for r in body.split("\n|-\n")[2:]:
        cells = [c[1:] if c.startswith("|") else c for c in r.split("\n|")]
        if len(cells) < 6:
            continue
        reason = "\n|".join(cells[5:])
        refs = _refs(reason, named)
        txt = re.sub(r"<ref.*?(</ref>|/>)", "", reason, flags=re.S)
        txt = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", txt).strip()
        clean = lambda s: re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s).strip()  # noqa: E731
        rows.append({"wiki_date": cells[0].strip(), "add": cells[1].strip(), "add_name": clean(cells[2]),
                     "rem": cells[3].strip(), "rem_name": clean(cells[4]), "reason": " ".join(txt.split()),
                     "ref_date": next((x["date"] for x in refs if x["date"]), ""),
                     "ref_url": next((x["url"] for x in refs if x["url"]), ""),
                     "ref_title": next((x["title"] for x in refs if x["title"]), "")})
    return pd.DataFrame(rows)


def _ref_date(s: str) -> str:
    try:
        return pd.Timestamp(s).date().isoformat()
    except Exception:  # noqa: BLE001
        return ""


def classify_delete(reason: str) -> str:
    r = reason.lower()
    if any(w in r for w in ("acquired", "merged", "merging", "merger", "taken private", "went private",
                            "buyout", "seals")):
        return "acquisition"
    if any(w in r for w in SPIN_WORDS) and "removed" not in r and "minimum" not in r:
        return "corporate"
    return "index"     # annual / quarterly reconstitution, weight rule, listing transfer, REIT, tracking-stock removal


def classify_add(reason: str, wiki_date: str, add: str) -> str:
    r = reason.lower()
    if "class c" in r or "multiple" in r or "multi" in r or "classes" in r:
        return "share_class"
    if "changed its name" in r:
        return "continuity"
    if any(w in r for w in SPIN_WORDS):
        return "spin"
    return "announced"


def kind_of(reason: str) -> str:
    r = reason.lower()
    if "annual index reconstitution" in r or "annual" in r and "reconstitution" in r:
        return "annual"
    if "quarterly index reconstitution" in r:
        return "quarterly"
    return "adhoc"


def build_events_raw(text: str | None = None) -> pd.DataFrame:
    w = parse_wiki(text if text is not None else wiki_text())
    w["inclusion"] = pd.to_datetime(w["wiki_date"], errors="coerce")
    w = w[(w.inclusion >= WINDOW[0]) & (w.inclusion <= WINDOW[1])].copy()
    rows = []
    for r in w.itertuples():
        h = HAND.get((r.wiki_date, r.add, r.rem), {})
        incl = h.get("inclusion", r.inclusion.date().isoformat())
        kind = kind_of(r.reason)
        ann = h.get("ann", _ref_date(r.ref_date))
        if ann and ann >= incl:
            ann = ""        # a "date" on or after the inclusion is a news story / access date, not the release
        for side, tick, name in (("add", r.add, r.add_name), ("delete", r.rem, r.rem_name)):
            if not tick:
                continue
            typ = (h.get("add_type") or classify_add(r.reason, r.wiki_date, tick)) if side == "add" else \
                (h.get("del_type") or classify_delete(r.reason))
            rows.append({"inclusion_date": incl, "kind": kind, "side": side, "ticker": tick, "name": name,
                         "type": typ, "wiki_date": r.wiki_date, "ann_wiki": ann, "ref_url": r.ref_url,
                         "ref_title": r.ref_title, "reason": r.reason, "hand_note": h.get("note", "")})
    ev = pd.DataFrame(rows)
    ev["event_id"] = ("NDX-" + ev.inclusion_date.str.replace("-", "") + "-" + ev.side.str.upper().str[:3] + "-"
                      + ev.ticker)
    return ev.sort_values(["inclusion_date", "side", "ticker"]).reset_index(drop=True)


# ============================================================================== press releases (archive.org)

WAYBACK = sec.Limiter(0.25)      # at most 15 requests a minute


def _wb(url: str, sub: str, name: str) -> bytes | None:
    try:
        data = sec.cached_get(url, f"../../ndx_recon/raw/{sub}", headers={"User-Agent": "Mozilla/5.0"},
                              limiter=WAYBACK, name=name, retries=3)
    except Exception as exc:  # noqa: BLE001
        print(f"  wayback {name}: {type(exc).__name__} {exc}", flush=True)
        return None
    if data and data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def gnw_day(day: pd.Timestamp) -> list[tuple[str, str]]:
    """(release id, original URL) of archived GlobeNewswire releases with 'nasdaq-100' in the URL, published on
    ``day`` (GlobeNewswire URL dates are UTC)."""
    d = day.strftime("%Y/%m/%d")
    url = (f"https://web.archive.org/cdx/search/cdx?url=globenewswire.com/news-release/{d}/&matchType=prefix"
           "&fl=original,timestamp,statuscode&collapse=urlkey&limit=20000")
    body = _wb(url, "cdx", "gnw_" + day.strftime("%Y%m%d")) or b""
    out = {}
    for line in body.decode("utf-8", "ignore").splitlines():
        parts = line.split(" ")
        if len(parts) < 3 or "nasdaq-100" not in parts[0].lower():
            continue
        m = re.search(r"/news-release/\d{4}/\d{2}/\d{2}/(\d+)/", parts[0])
        if not m or parts[2] not in ("200", "-"):
            continue
        u = parts[0].split("&quot;")[0]
        if not u.lower().endswith(".html"):
            continue
        out.setdefault(m.group(1), (u, parts[1]))
    return [(k, v[0], v[1]) for k, v in out.items()]


def read_release(rid: str, url: str, stamp: str) -> dict | None:
    data = _wb(f"https://web.archive.org/web/{stamp}id_/{url}", "gnw", f"gnw_{rid}")
    if not data:
        return None
    t = data.decode("utf-8", "ignore")
    pub = re.search(r'"datePublished"\s*:\s*"([^"]+)"', t) or re.search(r'datetime="([^"]+)"', t)
    head = (re.search(r'"headline"\s*:\s*"([^"]+)"', t) or re.search(r'property="og:title"\s+content="([^"]+)"', t)
            or re.search(r"<title>\s*([^<]+?)\s*</title>", t, re.S))
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S)
    body = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)))
    i = body.find("(GLOBE NEWSWIRE)")
    text = body[max(0, i - 60): i + 4000] if i >= 0 else ""
    if not pub:
        return None
    utc = pd.Timestamp(pub.group(1))
    utc = utc.tz_localize("UTC") if utc.tzinfo is None else utc.tz_convert("UTC")
    et = utc.tz_convert("America/New_York")
    return {"release_id": rid, "url": url, "archive_stamp": stamp, "published_utc": utc.isoformat(),
            "published_et": et.strftime("%Y-%m-%d %H:%M"), "headline": html.unescape(head.group(1)) if head else "",
            "text": text}


def _inclusion_in_headline(h: str) -> str:
    m = re.search(r"(?:beginning|on)\s+([A-Z][a-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})", h, re.I)
    if not m:
        return ""
    s = re.sub(r"(st|nd|rd|th),", ",", m.group(1).replace(".", ""))
    return _ref_date(s)


def announcement_groups(ev: pd.DataFrame) -> pd.DataFrame:
    """One row per announcement: annual / quarterly changes by year, ad-hoc changes by inclusion date."""
    rows = []
    for (kind, incl), g in ev.groupby(["kind", "inclusion_date"]):
        key = f"{kind}-{incl[:4]}" if kind in ("annual", "quarterly") else f"adhoc-{incl}"
        anns = sorted(set(g.ann_wiki) - {""})
        rows.append({"group": key, "kind": kind, "inclusion_date": incl, "ann_wiki": anns[0] if anns else "",
                     "tickers": " ".join(sorted(g.ticker)),
                     "needs_ann": bool(((g.side == "add") & (g.type == "announced")).any())})
    return pd.DataFrame(rows).drop_duplicates("group")


def verify(ev: pd.DataFrame, max_days: int = 14) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Find the Nasdaq release of every announcement group on GlobeNewswire (archive.org)."""
    groups = announcement_groups(ev)
    found, checks, seen = [], [], {}
    for g in groups.itertuples():
        incl = pd.Timestamp(g.inclusion_date)
        if not g.needs_ann:
            continue
        if g.ann_wiki:
            a = pd.Timestamp(g.ann_wiki)
            days = [a + pd.Timedelta(days=k) for k in (0, 1, -1)]
        else:
            # earliest first: the announcement is the first matching release (a later "Update:" re-issue is not)
            days = [incl - pd.Timedelta(days=k) for k in range(max_days, 0, -1)]
        match = None
        for d in days:
            for rid, url, stamp in gnw_day(d):
                if rid not in seen:
                    seen[rid] = read_release(rid, url, stamp)
                rel = seen[rid]
                if rel is None:
                    continue
                h = rel["headline"].lower()
                ok = False
                if g.kind == "annual":
                    ok = "annual" in h and rel["published_et"][:4] == g.inclusion_date[:4]
                elif g.kind == "quarterly":
                    ok = "quarterly" in h
                else:
                    ok = _inclusion_in_headline(rel["headline"]) == g.inclusion_date or \
                        any(t.lower() in rel["text"].lower()[:600] for t in g.tickers.split()) and \
                        ("join" in h or "removed" in h or "added" in h)
                if ok and rel["published_et"][:10] < g.inclusion_date:
                    match = rel
                    break
            if match:
                break
        checks.append({"group": g.group, "kind": g.kind, "inclusion_date": g.inclusion_date, "tickers": g.tickers,
                       "ann_wiki": g.ann_wiki, "gnw_published_et": match["published_et"] if match else "",
                       "gnw_headline": match["headline"] if match else "", "gnw_url": match["url"] if match else "",
                       "archive_stamp": match["archive_stamp"] if match else "",
                       "agree": (match["published_et"][:10] == g.ann_wiki) if (match and g.ann_wiki) else ""})
        print(f"  {g.group:24s} wiki {g.ann_wiki or '-':10s} gnw {checks[-1]['gnw_published_et'] or '-'}",
              flush=True)
    rel = pd.DataFrame([{k: v for k, v in r.items() if k != "text"} for r in seen.values() if r])
    return pd.DataFrame(checks), rel


# ============================================================================== final event table

def finalize(ev: pd.DataFrame, checks: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Announcement date / time / source and the sessions used by the rules:
    ann_session  last session on or before the announcement date (ET); announcements after the close
    buy_session  first session after the announcement (R1 entry close)
    eff_session  effective date = last session before the inclusion date (index funds trade at its close)."""
    grp = announcement_groups(ev).set_index("group")
    chk = checks.set_index("group") if len(checks) else pd.DataFrame()
    key_of = {}
    for g, r in grp.iterrows():
        key_of[(r.kind, r.inclusion_date)] = g
    out = []
    for r in ev.itertuples():
        g = key_of.get((r.kind, r.inclusion_date))
        if g is None:
            g = next(k for (kd, inc), k in key_of.items() if kd == r.kind and inc[:4] == r.inclusion_date[:4])
        c = chk.loc[g] if g in chk.index else None
        ann, t, src, url = "", "", "", ""
        if c is not None and c["gnw_published_et"]:
            ann, t = c["gnw_published_et"][:10], c["gnw_published_et"][11:]
            src, url = "globenewswire (archive.org)", c["gnw_url"]
        elif r.ann_wiki:
            ann, src, url = r.ann_wiki, "wikipedia ref", r.ref_url
        incl = pd.Timestamp(r.inclusion_date)
        ie = int(sessions.searchsorted(incl, side="left")) - 1
        eff = sessions[ie].date().isoformat() if ie >= 0 else ""
        ann_s = buy_s = ""
        if ann:
            a = pd.Timestamp(ann)
            # a release during or before the session counts from that session; after the close, from the next
            ia = int(sessions.searchsorted(a, side="right")) - 1
            if t and t < "09:30" and sessions[ia] == a:
                ia -= 1          # before the open: the reaction starts the same day
            ann_s = sessions[ia].date().isoformat() if ia >= 0 else ""
            buy_s = sessions[ia + 1].date().isoformat() if ia + 1 < len(sessions) else ""
        out.append({"event_id": r.event_id, "group": g, "kind": r.kind, "side": r.side, "ticker": r.ticker,
                    "name": r.name, "type": r.type, "announce_date_et": ann, "announce_time_et": t,
                    "announce_source": src, "announce_url": url, "ann_session": ann_s, "buy_session": buy_s,
                    "inclusion_date": r.inclusion_date, "effective_date": eff,
                    "wiki_date": r.wiki_date, "wiki_ref_url": r.ref_url, "hand_note": r.hand_note})
    return pd.DataFrame(out)


# ============================================================================== prices

def _v2_intervals() -> pd.DataFrame:
    t = pd.read_csv(V2_INPUTS / "ticker_intervals.csv", dtype=str).fillna("")
    t["start"] = t["start"].replace("", "1900-01-01")
    return t


YAHOO_ALIAS = {   # ticker in the index table -> Yahoo symbol(s) holding the same security's history today
    "META": ["META"], "FB": ["META"], "GOOG": ["GOOG"], "VIP": ["VEON"], "TCOM": ["TCOM"], "NLOK": ["GEN"],
    "FI": ["FI", "FISV"], "WTW": ["WTW"], "SIRI": ["SIRI"], "KHC": ["KHC"], "LIN": ["LIN"], "BKNG": ["BKNG"],
    "MSTR": ["MSTR"], "CTSH": ["CTSH"], "LBTYA": ["LBTYA"], "LBTYK": ["LBTYK"], "KFT": ["MDLZ"], "RIMM": ["BB"],
    "CTRP": ["TCOM"], "WLTW": ["WTW"],
}


def series_v2_sid(sid: str) -> pd.DataFrame | None:
    p = V2_PRICES / f"{sid}.csv"
    if not p.exists():
        return None
    v = pd.read_csv(p)
    df = pd.DataFrame({"date": pd.to_datetime(v["date"]), "close_raw": v["close_raw"], "volume_raw": v["volume_raw"],
                       "tr": v["tr"]})
    df["source"] = "v2:" + sid
    return df.dropna(subset=["close_raw"]).sort_values("date")


def candidates_for(ticker: str, day: pd.Timestamp, iv: pd.DataFrame) -> list[pd.DataFrame]:
    from scripts import research_spinoffs as sp
    d = day.date().isoformat()
    lo = (day - pd.Timedelta(days=120)).date().isoformat()
    hit = iv[(iv.ticker == ticker) & (iv.start <= d) & ((iv.end >= lo) | (iv.end == ""))]
    sids = list(dict.fromkeys(hit.security_id))
    cands = [series_v2_sid(s) for s in sids]
    for s in YAHOO_ALIAS.get(ticker, [ticker]):
        cands.append(sp.series_yahoo(s.replace(".", "-")))
    cands.append(sp.series_wiki(ticker))
    return [c for c in cands if c is not None and not c.empty]


def pick_series(cands: list[pd.DataFrame], need_from: pd.Timestamp, need_to: pd.Timestamp) -> pd.DataFrame | None:
    """The first candidate (v2 > Yahoo > WIKI) that has rows on at least 90% of the business days in
    [need_from, need_to] and whose raw close agrees with any other covering candidate within 3% on the first
    common day (else the next one)."""
    need = pd.bdate_range(need_from, need_to)
    good = []
    for c in cands:
        n = int(c["date"].isin(need).sum())
        if n >= 0.85 * len(need):
            good.append(c)
    if not good:
        return None
    return good[0]


def build_prices(ev: pd.DataFrame) -> pd.DataFrame:
    SERIES.mkdir(parents=True, exist_ok=True)
    from scripts import research_spinoffs as sp
    for etf in ("QQQ", "ONEQ"):
        s = sp.series_yahoo(etf)
        s.to_csv(SERIES / f"etf_{etf}.csv.gz", index=False)
    iv = _v2_intervals()
    status = []
    for r in ev.itertuples():
        incl = pd.Timestamp(r.inclusion_date)
        frm = pd.Timestamp(r.announce_date_et) - pd.Timedelta(days=7) if r.announce_date_et else \
            incl - pd.Timedelta(days=21)
        to = min(incl + pd.Timedelta(days=40), pd.Timestamp("2026-10-01"))
        if r.side == "delete" and r.type == "acquisition":
            to = incl - pd.Timedelta(days=3)
        cands = candidates_for(r.ticker, incl, iv)
        s = pick_series(cands, frm, to)
        key = r.event_id
        if s is None:
            status.append({"event_id": key, "ticker": r.ticker, "status": "no_prices",
                           "tried": " ".join(c["source"].iloc[0] for c in cands)})
            continue
        s.to_csv(SERIES / f"{key}.csv.gz", index=False)
        status.append({"event_id": key, "ticker": r.ticker, "status": "ok", "source": s["source"].iloc[0],
                       "tried": " ".join(c["source"].iloc[0] for c in cands)})
    return pd.DataFrame(status)


# ============================================================================== engine

def load_series(key: str) -> pd.DataFrame | None:
    p = SERIES / f"{key}.csv.gz"
    return pd.read_csv(p, parse_dates=["date"]) if p.exists() else None


class Asset:
    def __init__(self, df: pd.DataFrame, sessions: pd.DatetimeIndex):
        s = df.set_index("date")
        s = s[~s.index.duplicated(keep="last")]
        self.px = s["close_raw"].reindex(sessions).to_numpy(float)
        self.ret = s["tr"].reindex(sessions).fillna(0.0).to_numpy(float)
        dv = (s["close_raw"] * s["volume_raw"]).rolling(20, min_periods=1).median()
        self.dv = dv.reindex(sessions).ffill().to_numpy(float)
        rows = np.where(~np.isnan(self.px))[0]
        self.last_i = int(rows[-1]) if len(rows) else -1


def half_spread(dv: float, price: float, mult: float = 1.0) -> float:
    from scripts import research_spinoffs as sp
    return sp.half_spread(dv, price, mult)


def order_cost(shares: float, price: float, sell: bool, hs: float) -> float:
    from scripts import research_reversal_dev as rev
    return rev.order_cost(shares, price, sell, hs)["total"]


def simulate(plan: list[dict], assets: dict, sessions: pd.DatetimeIndex, oneq_ret: np.ndarray,
             oneq_px: np.ndarray, spread_mult: float = 1.0, slots: int = SLOTS) -> dict:
    """Daily engine at the close (ledger 0.4). ``plan``: dicts with ``key`` (event id), ``entry_i``, ``exit_i``,
    ``order``. Each entry buys NAV/slots of the name in whole shares, funded by selling ONEQ; exits at the
    planned close (sells before buys on the same day); up to ``slots`` names; idle money in ONEQ."""
    n = len(sessions)
    by_day: dict[int, list[dict]] = {}
    for c in plan:
        by_day.setdefault(c["entry_i"], []).append(c)
    pos: dict[str, dict] = {}
    cash, oneq_sh, oneq_val = ACCOUNT, 0.0, 0.0
    navs, costs, expo, npos = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n, int)
    trades, skipped_full, skipped_price, traded = [], 0, 0, 0.0

    def oneq_trade(dollars: float, i: int) -> float:
        nonlocal cash, oneq_sh, oneq_val
        p = oneq_px[i]
        sh = math.floor(dollars / p) if dollars > 0 else -math.ceil(-dollars / p)
        if dollars < 0:
            sh = max(sh, -math.floor(oneq_sh + 1e-9))
        if sh == 0:
            return 0.0
        c = order_cost(abs(sh), p, sh < 0, max(ONEQ_HALF_SPREAD * spread_mult, 0.005 / p * spread_mult))
        cash -= sh * p + c
        oneq_sh += sh
        oneq_val = oneq_sh * p
        return c

    for i in range(n):
        day_cost = 0.0
        if i > 0 and oneq_sh:
            gross = oneq_sh * oneq_px[i - 1] * (1.0 + oneq_ret[i])
            oneq_val = oneq_sh * oneq_px[i]
            cash += gross - oneq_val
        for k, p in list(pos.items()):
            a = assets[k]
            if i > p["entry_i"]:
                p["val"] *= 1.0 + a.ret[i]
                p["oneq"] *= 1.0 + oneq_ret[i]
            if i >= p["exit_i"] or i > a.last_i:
                last = min(i, a.last_i)
                price = a.px[last]
                sh = p["sh"]
                c = order_cost(sh, price, True, half_spread(a.dv[last], price, spread_mult))
                cash += p["val"] - c
                day_cost += c
                traded += p["val"]
                ret = (p["val"] - c) / p["inv"] - 1
                trades.append({**p["info"], "exit": sessions[i].date().isoformat(),
                               "exit_kind": "planned" if i >= p["exit_i"] and i <= a.last_i else "data_end",
                               "ret": ret, "oneq_ret": p["oneq"] - 1, "excess": ret - (p["oneq"] - 1),
                               "pnl": p["val"] - c - p["inv"], "days": i - p["entry_i"]})
                del pos[k]
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        for c_ in sorted(by_day.get(i, []), key=lambda c: c["order"]):
            k = c_["key"]
            a = assets.get(k)
            if k in pos:
                continue
            if len(pos) >= slots:
                skipped_full += 1
                continue
            if a is None or not a.px[i] == a.px[i] or c_["exit_i"] <= i:
                skipped_price += 1
                continue
            price = a.px[i]
            sh = math.floor(nav / slots / price)
            if sh < 1:
                skipped_price += 1
                continue
            c = order_cost(sh, price, False, half_spread(a.dv[i], price, spread_mult))
            if cash < sh * price + c:
                day_cost += oneq_trade(-(sh * price + c - cash) * 1.01, i)
            cash -= sh * price + c
            day_cost += c
            traded += sh * price
            pos[k] = {"val": sh * price, "sh": sh, "entry_i": i, "exit_i": c_["exit_i"], "inv": sh * price + c,
                      "oneq": 1.0, "info": {"event_id": k, "rule_leg": c_.get("leg", ""),
                                            "entry": sessions[i].date().isoformat()}}
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        if cash > 0.02 * nav:
            day_cost += oneq_trade(cash - 0.005 * nav, i)
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        navs[i], costs[i] = nav, day_cost
        expo[i] = sum(p["val"] for p in pos.values()) / nav if nav > 0 else 0.0
        npos[i] = len(pos)
    for k, p in pos.items():
        trades.append({**p["info"], "exit": "", "exit_kind": "open", "ret": p["val"] / p["inv"] - 1,
                       "oneq_ret": p["oneq"] - 1, "excess": p["val"] / p["inv"] - p["oneq"],
                       "pnl": p["val"] - p["inv"], "days": n - 1 - p["entry_i"]})
    return {"nav": pd.Series(navs, index=sessions), "cost": pd.Series(costs, index=sessions),
            "exposure": pd.Series(expo, index=sessions), "npos": pd.Series(npos, index=sessions),
            "trades": pd.DataFrame(trades), "traded": traded, "skipped_full": skipped_full,
            "skipped_price": skipped_price}


# ============================================================================== plans and diagnostics

def load_events() -> pd.DataFrame:
    return pd.read_csv(OUT / "events.csv", dtype=str).fillna("")


def sidx(sessions: pd.DatetimeIndex, d: str) -> int | None:
    if not d:
        return None
    i = int(sessions.searchsorted(pd.Timestamp(d), side="left"))
    return i if i < len(sessions) and sessions[i] == pd.Timestamp(d) else None


def r1_eligible(e) -> bool:
    return e.side == "add" and e.type == "announced" and bool(e.buy_session) and bool(e.effective_date)


def r2_eligible(e) -> bool:
    return e.side == "delete" and e.type == "index" and bool(e.effective_date)


def build_plan(ev: pd.DataFrame, sessions: pd.DatetimeIndex, start: str, end: str, legs: tuple) -> list[dict]:
    plan = []
    for e in ev.itertuples():
        if "R1" in legs and r1_eligible(e):
            i, j = sidx(sessions, e.buy_session), sidx(sessions, e.effective_date)
            if i is None or j is None or j <= i:
                continue
            leg = "R1"
        elif "R2" in legs and r2_eligible(e):
            i = sidx(sessions, e.effective_date)
            if i is None:
                continue
            j = i + HOLD_R2
            leg = "R2"
        else:
            continue
        if not (pd.Timestamp(start) <= sessions[i] <= pd.Timestamp(end)):
            continue
        plan.append({"key": e.event_id, "entry_i": i, "exit_i": min(j, len(sessions) - 1), "leg": leg,
                     "order": (sessions[i], leg, e.ticker)})
    return plan


def car_table(ev: pd.DataFrame, sessions: pd.DatetimeIndex, bench: dict, assets: dict) -> pd.DataFrame:
    """Event-time cumulative abnormal returns (sum of daily return minus benchmark return) per event:
    ann  close of the announcement session -> close of the effective date (announcement reaction + run-up)
    r1   close of the buy session (first session after the announcement) -> close of the effective date
    post close of the effective date -> close of +5 / +20 sessions."""
    rows = []
    for e in ev.itertuples():
        a = assets.get(e.event_id)
        if a is None or not e.effective_date:
            continue
        ie = sidx(sessions, e.effective_date)
        ia = sidx(sessions, e.ann_session) if e.ann_session else None
        ib = sidx(sessions, e.buy_session) if e.buy_session else None
        if ie is None:
            continue
        row = {"event_id": e.event_id, "group": e.group, "kind": e.kind, "side": e.side, "type": e.type,
               "ticker": e.ticker, "inclusion_date": e.inclusion_date, "half": "H1" if e.inclusion_date <= "2018-12-31" else "H2"}
        for b in ("QQQ", "ONEQ"):
            br = bench[b]["ret"].to_numpy(float)
            def car(i0, i1):     # returns of sessions i0+1 .. i1
                if i0 is None or i1 is None or i1 <= i0 or i1 > a.last_i:
                    return np.nan
                if np.isnan(a.px[i0]) or np.isnan(a.px[i1]):
                    return np.nan
                return float(np.sum(a.ret[i0 + 1:i1 + 1] - br[i0 + 1:i1 + 1]))
            row[f"ann_{b}"] = car(ia, ie)
            row[f"r1_{b}"] = car(ib, ie)
            row[f"post5_{b}"] = car(ie, ie + 5) if ie + 5 < len(sessions) else np.nan
            row[f"post20_{b}"] = car(ie, ie + POST_DAYS) if ie + POST_DAYS < len(sessions) else np.nan
        row["days_ann_to_eff"] = (ie - ia) if ia is not None else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def _t(x: pd.Series) -> tuple[float, float, int]:
    x = x.dropna()
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else np.nan, np.nan, n)
    return float(x.mean()), float(x.mean() / (x.std(ddof=1) / math.sqrt(n))), n


def car_summary(t: pd.DataFrame) -> pd.DataFrame:
    """Mean CAR, event t-stat and announcement-clustered t-stat (events of one announcement averaged first,
    the registered main statistic) for adds (announced type) and index deletions, by half and pooled."""
    out = []
    sets = {"add": t[(t.side == "add") & (t.type == "announced")],
            "delete": t[(t.side == "delete") & (t.type == "index")]}
    for side, s in sets.items():
        for half in ("all", "H1", "H2"):
            h = s if half == "all" else s[s.half == half]
            for kind in ("all", "annual+quarterly", "adhoc"):
                k = h if kind == "all" else h[h.kind.isin(["annual", "quarterly"])] if kind != "adhoc" else \
                    h[h.kind == "adhoc"]
                for win in ("ann", "r1", "post5", "post20"):
                    for b in ("QQQ", "ONEQ"):
                        col = f"{win}_{b}"
                        m, tt, n = _t(k[col])
                        g = k.groupby("group")[col].mean()
                        mg, tg, ng = _t(g)
                        out.append({"side": side, "half": half, "kind": kind, "window": win, "bench": b,
                                    "n_events": n, "mean_car": m, "t_event": tt, "median_car": float(k[col].median())
                                    if n else np.nan, "pos_share": float((k[col].dropna() > 0).mean()) if n else np.nan,
                                    "n_announcements": ng, "t_clustered": tg})
    return pd.DataFrame(out)


# ============================================================================== run

def bench_frames(sessions: pd.DatetimeIndex) -> dict:
    out = {}
    for etf in ("QQQ", "ONEQ"):
        s = load_series(f"etf_{etf}").set_index("date")
        out[etf] = pd.DataFrame({"px": s["close_raw"].reindex(sessions).ffill(),
                                 "ret": s["tr"].reindex(sessions).fillna(0.0)})
    return out


def metrics(res: dict, bench: dict) -> dict:
    from scripts import research_indicators as ind
    nav = res["nav"]
    oneq = (1 + bench["ONEQ"]["ret"]).cumprod()
    oneq = oneq / oneq.iloc[0] * ACCOUNT
    qqq = (1 + bench["QQQ"]["ret"]).cumprod()
    qqq = qqq / qqq.iloc[0] * ACCOUNT
    m = ind.stock_metrics(nav, oneq, qqq, ACCOUNT, res["cost"], res["traded"], res["exposure"])
    crit = ind.criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_excess_monthly"])
    m["crit_A"], m["crit_B"], m["crit_pass"] = crit["A"], crit["B"], crit["pass"]
    m["excess_cagr"] = m["cagr"] - m["bench_cagr"]
    m["t_ge_bonferroni"] = bool(m["t_excess_monthly"] >= BONFERRONI_T)
    tr = res["trades"]
    m["entries"] = int(len(tr))
    m["skipped_full"], m["skipped_no_price"] = res["skipped_full"], res["skipped_price"]
    m["avg_exposure"] = float(res["exposure"].mean())
    if len(tr):
        mm, tt, nn = _t(tr["excess"])
        m["trade_mean_ret"] = float(tr["ret"].mean())
        m["trade_mean_excess_vs_oneq"] = mm
        m["trade_t_excess"] = tt
        m["trade_median_excess"] = float(tr["excess"].median())
        m["trade_win_vs_oneq"] = float((tr["excess"] > 0).mean())
        m["trade_mean_pnl_usd"] = float(tr["pnl"].mean())
        m["trade_mean_days"] = float(tr["days"].mean())
    return m


def _clean(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_") and k != "by_year"}


def run() -> dict:
    ev = load_events()
    q = load_series("etf_QQQ")
    sessions = pd.DatetimeIndex(q["date"][(q["date"] >= "2011-06-01") & (q["date"] <= "2026-10-02")])
    bench = bench_frames(sessions)
    assets = {}
    for e in ev.itertuples():
        df = load_series(e.event_id)
        if df is not None:
            assets[e.event_id] = Asset(df, sessions)
    # diagnostics: event-time CARs (R3 = adds, post20)
    cars = car_table(ev, sessions, bench, assets)
    cars.to_csv(OUT / "car_events.csv", index=False, float_format="%.6f")
    summ = car_summary(cars)
    summ.to_csv(OUT / "car_summary.csv", index=False, float_format="%.6f")
    results, trades_all = [], []
    legs_of = {"R1": ("R1",), "R2": ("R2",), "R4": ("R1", "R2")}
    for half, (start, end) in HALVES.items():
        seg = sessions[(sessions >= start) & (sessions <= end)]
        bseg = {k: v.loc[seg] for k, v in bench.items()}
        oneq_ret, oneq_px = bseg["ONEQ"]["ret"].to_numpy(float), bseg["ONEQ"]["px"].to_numpy(float)
        seg_assets = {}
        for k, a in assets.items():
            b = Asset.__new__(Asset)
            lo = sessions.get_loc(seg[0])
            hi = sessions.get_loc(seg[-1]) + 1
            b.px, b.ret, b.dv = a.px[lo:hi], a.ret[lo:hi], a.dv[lo:hi]
            b.last_i = min(a.last_i - lo, hi - lo - 1) if a.last_i >= lo else -1
            seg_assets[k] = b
        for rule, legs in legs_of.items():
            for mult in (1.0, 2.0):
                plan = build_plan(ev, seg, start, end, legs)
                res = simulate(plan, seg_assets, seg, oneq_ret, oneq_px, spread_mult=mult)
                m = metrics(res, bseg)
                m.update({"rule": rule, "half": half, "spread_mult": mult, "planned": len(plan)})
                results.append(_clean(m))
                if mult == 1.0:
                    t = res["trades"].copy()
                    t["rule"], t["half"] = rule, half
                    trades_all.append(t)
                    pd.DataFrame({"nav": res["nav"], "oneq": (1 + bseg["ONEQ"]["ret"]).cumprod() * ACCOUNT,
                                  "exposure": res["exposure"]}).to_csv(
                        OUT / f"daily_nav_{rule}_{half}.csv", float_format="%.4f")
    res_df = pd.DataFrame(results)
    res_df.to_csv(OUT / "results.csv", index=False, float_format="%.6f")
    pd.concat(trades_all).to_csv(OUT / "trades.csv", index=False, float_format="%.6f")
    verdict = {}
    for rule in RULES:
        r = res_df[(res_df.rule == rule) & (res_df.spread_mult == 1.0)].set_index("half")
        verdict[rule] = {"H1": bool(r.loc["H1", "crit_pass"]), "H2": bool(r.loc["H2", "crit_pass"]),
                         "pass": bool(r.loc["H1", "crit_pass"] and r.loc["H2", "crit_pass"])}
    summary = {"events": int(len(ev)), "verdict": verdict,
               "r1_eligible": int(sum(r1_eligible(e) for e in ev.itertuples())),
               "r2_eligible": int(sum(r2_eligible(e) for e in ev.itertuples())),
               "with_prices": int(len(assets))}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    cols = ["rule", "half", "spread_mult", "cagr", "bench_cagr", "excess_cagr", "t_excess_monthly", "max_dd",
            "bench_max_dd", "crit_A", "crit_B", "entries", "trade_mean_excess_vs_oneq", "trade_t_excess",
            "avg_exposure"]
    print(res_df[cols].to_string(float_format=lambda x: f"{x:.4f}"))
    return summary


# ============================================================================== CLI

def sessions_for_events() -> pd.DatetimeIndex:
    from scripts import research_spinoffs as sp
    q = sp.series_yahoo("QQQ")
    return pd.DatetimeIndex(q["date"][q["date"] >= "2011-06-01"])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["events", "verify", "prices", "run"])
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.stage == "events":
        ev = build_events_raw()
        ev.to_csv(OUT / "events_raw.csv", index=False)
        print(ev.groupby(["side", "type"]).size())
        print(len(ev), "event rows")
    elif a.stage == "verify":
        ev = pd.read_csv(OUT / "events_raw.csv", dtype=str).fillna("")
        checks, rel = verify(ev)
        checks.to_csv(OUT / "hand_check.csv", index=False)
        rel.to_csv(OUT / "press_releases.csv", index=False)
        fin = finalize(ev, checks, sessions_for_events())
        fin.to_csv(OUT / "events.csv", index=False)
        print(fin.groupby(["side", "type"]).size())
    elif a.stage == "prices":
        cov = build_prices(load_events())
        cov.to_csv(OUT / "price_coverage.csv", index=False)
        print(cov.status.value_counts())
    else:
        run()


if __name__ == "__main__":
    main()
