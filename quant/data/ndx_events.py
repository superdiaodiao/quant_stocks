"""Nasdaq-100 change events, 2012-2026, for the reconstitution study (docs/research_ledger_ndx_recon.md section 1):
the Wikipedia "Historical components of the Nasdaq-100" change table (a pinned revision), the classification of each
added / removed name, the Nasdaq press releases on GlobeNewswire found through archive.org (announcement date and
time), the final event table with the sessions the rules use, and the price series per event name (data v2 panel,
Yahoo v8, Quandl WIKI).

Moved unchanged from studies/ndx_recon.py in the slimming pass (stages ``events``, ``verify``, ``prices``). The
hand corrections, the event window, the Wikipedia revision, the Yahoo aliases and every folder are arguments
declared by the study. Bodies are cached by quant.data.sources.sec_cache (under research_cache/ndx_recon/raw).
"""
from __future__ import annotations

import gzip
import html
import json
import re
from pathlib import Path

import pandas as pd

from quant.data.sources import daily_series as sp     # Yahoo / WIKI daily series (from research_spinoffs)
from quant.data.sources import sec_cache as sec

SPIN_WORDS = ("spun off", "spin-off", "spin off", "tracking stock", "tracking stocks")


# ============================================================================== Wikipedia event table

def wiki_text(revid: int) -> str:
    """The wikitext of revision ``revid`` of the Wikipedia page (cached)."""
    url = ("https://en.wikipedia.org/w/api.php?action=query&prop=revisions&rvprop=ids|timestamp|content"
           f"&rvslots=main&format=json&revids={revid}")
    data = sec.cached_get(url, "../../ndx_recon/raw/wiki", headers={"User-Agent": "Mozilla/5.0 research"},
                          limiter=sec.WAYBACK_LIMIT, name=f"rev_{revid}")
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


def build_events_raw(text: str, hand: dict, window: tuple) -> pd.DataFrame:
    """One row per added / removed name of the change table ``text`` with an inclusion date inside ``window``,
    classified, with the ``hand`` corrections {(Wikipedia date, added, removed): overrides} applied."""
    w = parse_wiki(text)
    w["inclusion"] = pd.to_datetime(w["wiki_date"], errors="coerce")
    w = w[(w.inclusion >= window[0]) & (w.inclusion <= window[1])].copy()
    rows = []
    for r in w.itertuples():
        h = hand.get((r.wiki_date, r.add, r.rem), {})
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

def _v2_intervals(v2_inputs: Path) -> pd.DataFrame:
    t = pd.read_csv(v2_inputs / "ticker_intervals.csv", dtype=str).fillna("")
    t["start"] = t["start"].replace("", "1900-01-01")
    return t


def candidates_for(ticker: str, day: pd.Timestamp, iv: pd.DataFrame, v2_prices: Path,
                   yahoo_alias: dict) -> list[pd.DataFrame]:
    """Price series candidates of ``ticker`` around ``day``: the v2 panel securities that held the ticker, Yahoo
    (``yahoo_alias``: the symbols holding the security's history today), WIKI."""
    d = day.date().isoformat()
    lo = (day - pd.Timedelta(days=120)).date().isoformat()
    hit = iv[(iv.ticker == ticker) & (iv.start <= d) & ((iv.end >= lo) | (iv.end == ""))]
    sids = list(dict.fromkeys(hit.security_id))
    cands = [sp.series_v2(v2_prices, s) for s in sids]
    for s in yahoo_alias.get(ticker, [ticker]):
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


def build_prices(ev: pd.DataFrame, series_dir: Path, v2_inputs: Path, v2_prices: Path,
                 yahoo_alias: dict) -> pd.DataFrame:
    """QQQ / ONEQ and one series per event name -> ``series_dir`` (local only); returns the coverage table."""
    series_dir.mkdir(parents=True, exist_ok=True)
    for etf in ("QQQ", "ONEQ"):
        s = sp.series_yahoo(etf)
        s.to_csv(series_dir / f"etf_{etf}.csv.gz", index=False)
    iv = _v2_intervals(v2_inputs)
    status = []
    for r in ev.itertuples():
        incl = pd.Timestamp(r.inclusion_date)
        frm = pd.Timestamp(r.announce_date_et) - pd.Timedelta(days=7) if r.announce_date_et else \
            incl - pd.Timedelta(days=21)
        to = min(incl + pd.Timedelta(days=40), pd.Timestamp("2026-10-01"))
        if r.side == "delete" and r.type == "acquisition":
            to = incl - pd.Timedelta(days=3)
        cands = candidates_for(r.ticker, incl, iv, v2_prices, yahoo_alias)
        s = pick_series(cands, frm, to)
        key = r.event_id
        if s is None:
            status.append({"event_id": key, "ticker": r.ticker, "status": "no_prices",
                           "tried": " ".join(c["source"].iloc[0] for c in cands)})
            continue
        s.to_csv(series_dir / f"{key}.csv.gz", index=False)
        status.append({"event_id": key, "ticker": r.ticker, "status": "ok", "source": s["source"].iloc[0],
                       "tried": " ".join(c["source"].iloc[0] for c in cands)})
    return pd.DataFrame(status)


def sessions_for_events() -> pd.DatetimeIndex:
    q = sp.series_yahoo("QQQ")
    return pd.DatetimeIndex(q["date"][q["date"] >= "2011-06-01"])
