"""Spin-off event list, 2012-2026, from SEC EDGAR only (docs/research_ledger_spinoffs.md section 0.2).

Steps (every SEC body is cached under research_cache/spinoffs/raw/, see quant.data.sources.sec_cache):
1. EDGAR full-text search: every filing of the Form 10-12B family (10-12B, 10-12B/A) filed 2011-01..2026-09.
   A 10-12B registers a class on a national exchange; every exchange-listed spin-off files one (a 10-12G is an
   over-the-counter registration and is out of scope). The index was checked against EDGAR's quarterly form
   indices for 2015Q3, 2017Q1 and 2021Q4 (no filing missing).
2. Per registrant: the submissions record (names, tickers, CERT* listing certifications, 8-A12B, Form 25, 15-12B,
   8-K item lists).
3. Per registrant: the information statement (EX-99.1) of the latest 10-12B filing that carries one (the start of
   the document only). It gives: whether this is a spin-off (a pro rata distribution of the registrant's shares by a
   parent to the parent's holders), the parent's name, the record and distribution dates, the ratio, the symbol and
   the exchange.
4. The distribution date is confirmed from the registrant's own 8-Ks filed within 60 days after the expected date
   ("On <date>, ... completed the spin-off / distribution"); where they disagree the 8-K wins.
5. Parent CIK: EDGAR's entity index looked up by the parent's name (highest-activity match), checked by the parent
   having filed with SEC within a year of the distribution; HAND overrides record what the lookup could not.
6. First regular-way day = first NYSE session after the distribution date (when-issued trading ends on the
   distribution date).
7. Delisting: the first Form 25 / 25-NSE after the spin; its reason (merger, other) from the 8-K items filed in the 30
   days before it (2.01 completion of acquisition, 3.03 / 5.01 change in control => merger).

Output: output/research_only/spinoffs/events.csv (IDs, dates and SEC facts only; no vendor prices).
Moved unchanged from scripts/research_spinoffs_events.py (phase 2); run through studies/spinoffs.py (stage events).
"""
from __future__ import annotations

import html
import re

import pandas as pd

from quant.data.sources import sec_cache as sec
from quant.paths import CACHE_ROOT, output_dir

OUT = output_dir("spinoffs")
EFTS_CSV = sec.CACHE / "efts_10_12b.csv"
FIRST_FILED, LAST_FILED = "2011-01-01", "2026-09-30"
EVENT_START, EVENT_END = "2012-01-01", "2026-09-30"   # first regular-way day inside this window

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE = rf"(?:{MONTHS})\s+\d{{1,2}}\s*,\s*\d{{4}}"
Q = "[\"“”'‘’]"

EXCHANGE_OF_CERT = {"CERTNYS": "NYSE", "CERTNAS": "Nasdaq", "CERTARCA": "NYSE Arca", "CERTAMEX": "NYSE American",
                    "CERTPAC": "NYSE Arca", "CERTBATS": "Cboe BZX", "CERTCBO": "Cboe", "CERTNYSEAMER": "NYSE American"}


# ======================================================================== text helpers

def html_text(raw: bytes) -> str:
    t = raw.decode("utf-8", "ignore")
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ").replace("​", "")
    return no_abbrev_dots(re.sub(r"\s+", " ", t))


def no_abbrev_dots(t: str) -> str:
    """Drop the dots of common abbreviations so that sentence-bounded patterns ([^.]) run over them."""
    t = re.sub(r"\b(Inc|Corp|Co|Ltd|Bros|Mr|Ms|Mrs|Dr|No|St|Jr|Cos|Mfg|Intl)\.", r"\1", t)
    return re.sub(r"\b([A-Z])\.([A-Z])\.(?:([A-Z])\.)?", lambda m: "".join(x for x in m.groups() if x), t)


def parse_date(s: str) -> pd.Timestamp | None:
    try:
        return pd.Timestamp(re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", s.strip())))
    except Exception:
        return None


def _dates_near(text: str, patterns: list[str]) -> list[tuple[pd.Timestamp, str]]:
    out = []
    for p in patterns:
        for m in re.finditer(p, text, re.I):
            d = parse_date(m.group("d"))
            if d is not None:
                out.append((d, text[max(0, m.start() - 80): m.end() + 40]))
    return out


DIST_PATTERNS = [
    rf"(?:on|as of|at [^.]{{0,60}}? on)\s+(?P<d>{DATE})\s*\(?\s*(?:the\s+|such date,? the\s+)?{Q}?\s*distribution date",
    rf"distribution date{Q}?\)?\s*(?:,|is|will be|of|which is|was|is expected to be|, which is expected to be|is currently expected to be)?\s*(?:expected to be\s+)?(?:on\s+)?(?P<d>{DATE})",
    rf"distribut\w+ (?:will|is expected to|are expected to|is scheduled to)\s+(?:be\s+)?(?:made|occur|take place|completed|effective|distributed)[^.]{{0,80}}?\bon\s+(?P<d>{DATE})",
    rf"to be distributed[^.]{{0,120}}?\bon\s+(?P<d>{DATE})",
]
RECORD_PATTERNS = [
    rf"(?:on|as of)\s+(?P<d>{DATE})\s*\(?\s*(?:the\s+)?{Q}?\s*record date",
    rf"record date{Q}?\)?\s*(?:for the distribution)?\s*(?:,|is|will be|of|was)?\s*(?:on\s+)?(?P<d>{DATE})",
    rf"close of business (?:on|as of)\s+(?P<d>{DATE})",
]


def most_common_date(found: list[tuple[pd.Timestamp, str]]) -> tuple[pd.Timestamp | None, str]:
    if not found:
        return None, ""
    s = pd.Series([d for d, _ in found])
    top = s.value_counts()
    best = top.index[0]
    snippet = next(sn for d, sn in found if d == best)
    return best, snippet


def find_distribution_date(text: str):
    return most_common_date(_dates_near(text, DIST_PATTERNS))


def find_record_date(text: str):
    return most_common_date(_dates_near(text, RECORD_PATTERNS))


NAME = r"(?P<p>[A-Z][A-Za-z0-9.,&'’\- ]{1,90}?)"
PARENT_PATTERNS = [
    rf"in connection with the (?:pro rata |special |planned |proposed )?(?:distribution|spin-off|spin off|separation) by {NAME}\s*(?:\(|,|to\b|of\b|\.)",
    rf"distribution by {NAME}\s*\(?{Q}?[A-Za-z ]*{Q}?\)?\s*(?:to|of) (?:its|the) (?:stockholders|shareholders|holders|unitholders)",
    rf"Dear (?:Fellow )?{NAME}\s+(?:Stockholder|Shareholder|Unitholder|Member|Holder)",
    rf"{NAME}\s*(?:\({Q}[^)]{{1,30}}{Q}\))?\s*(?:,\s*)?(?:intends|plans|expects|has announced (?:a plan|its intention)|will|announced (?:a plan|its intention)) to (?:distribute|separate|spin off|spin-off)",
    rf"(?:a |an |the )?(?:wholly[- ]owned |majority[- ]owned )?subsidiary of {NAME}\s*(?:\(|,|\.|that|which)",
]
STOP_WORDS = ("This", "The ", "We ", "Our ", "In ", "On ", "As ", "If ", "Following", "Upon", "After", "Prior", "Each",
              "Holders", "Information", "Stockholders", "Shareholders", "Table", "Page", "You", "Exhibit", "Following")


def clean_name(n: str) -> str:
    n = re.sub(rf"{Q}", "", n).strip(" ,.-")
    n = re.sub(r"\s+", " ", n)
    return n


def find_parent(text: str, own_names: list[str]) -> tuple[str, str]:
    """The parent's name as the information statement writes it, and the matched snippet."""
    own = [re.sub(r"[^a-z]", "", o.lower())[:12] for o in own_names if o]
    votes: dict[str, int] = {}
    snip: dict[str, str] = {}
    for k, p in enumerate(PARENT_PATTERNS):
        for m in list(re.finditer(p, text[:400_000]))[:6]:
            n = clean_name(m.group("p"))
            if len(n) < 3 or n.startswith(STOP_WORDS) or n.lower() in ("parent", "the company", "company"):
                continue
            key = re.sub(r"[^a-z]", "", n.lower())
            if any(key[:12] == o for o in own) or not key:
                continue
            votes[n] = votes.get(n, 0) + (3 if k < 2 else 1)
            snip.setdefault(n, text[max(0, m.start() - 40): m.end() + 40])
    if not votes:
        return "", ""
    best = max(votes, key=lambda n: (votes[n], -len(n)))
    return best, snip[best]


def find_symbols(text: str) -> list[str]:
    """Every symbol written as 'under the symbol "X"', most frequent first."""
    found = [m.group(1).rstrip(".") for m in
             re.finditer(rf"under the (?:ticker |trading )?symbols?\s*{Q}\s*([A-Z][A-Z.\-]{{0,6}})\s*{Q}", text)]
    return list(pd.Series(found).value_counts().index) if found else []


def find_symbol(text: str, exclude: str = "") -> str:
    """The registrant's own symbol: the most frequent 'under the symbol' symbol that is not ``exclude``
    (the parent's ticker)."""
    for s in find_symbols(text):
        if s != exclude:
            return s
    return ""


def find_exchange(text: str) -> str:
    seg = []
    for m in re.finditer(r"(?:to list|listed|listing|apply|applied|approved)[^.]{0,200}", text[:600_000], re.I):
        seg.append(m.group(0))
    s = " ".join(seg)
    hits = {"Nasdaq": len(re.findall(r"nasdaq", s, re.I)),
            "NYSE American": len(re.findall(r"NYSE (?:American|MKT)|American Stock Exchange", s)),
            "NYSE": len(re.findall(r"New York Stock Exchange|\bNYSE\b(?! (?:American|MKT|Arca))", s))}
    best = max(hits, key=hits.get)
    return best if hits[best] else ""


def is_spinoff_text(text: str) -> bool:
    t = text[:600_000].lower()
    has_is = "information statement" in t
    dist = bool(re.search(r"pro rata|spin-off|spin off|spinoff|distribution of (?:all|approximately|\d)", t))
    return has_is and dist


def find_ratio(text: str) -> str:
    m = re.search(r"(?:receive|entitle[sd]? the holder thereof to receive)\s+([^.]{0,40}?)\s+(?:share|common share)s?\s+of[^.]{0,120}?for (?:every|each)\s+([^.]{0,30}?)\s+(?:share|common share)", text)
    return (m.group(1) + " for " + m.group(2)).strip() if m else ""


# ======================================================================== SEC fetch steps

def enumerate_10_12b(refresh: bool = False) -> pd.DataFrame:
    """Every 10-12B / 10-12B/A filing (EDGAR full-text search, by half-year)."""
    if EFTS_CSV.exists() and not refresh:
        return pd.read_csv(EFTS_CSV, dtype=str)
    rows = []
    for y in range(int(FIRST_FILED[:4]), int(LAST_FILED[:4]) + 1):
        for a, b in (("01-01", "06-30"), ("07-01", "12-31")):
            frm = 0
            while True:
                j = sec.efts({"forms": "10-12B", "dateRange": "custom", "startdt": f"{y}-{a}", "enddt": f"{y}-{b}",
                              "from": frm})
                hits, tot = j["hits"]["hits"], j["hits"]["total"]["value"]
                for h in hits:
                    s = h["_source"]
                    rows.append({"id": h["_id"], "adsh": s["adsh"], "ciks": "|".join(s["ciks"]),
                                 "names": "|".join(s["display_names"]), "form": s["form"], "file_type": s["file_type"],
                                 "file_date": s["file_date"], "root": "|".join(s["root_forms"]),
                                 "sic": "|".join(s.get("sics") or []), "inc": "|".join(s.get("inc_states") or []),
                                 "desc": s.get("file_description")})
                frm += 100
                if frm >= tot or not hits:
                    break
    df = pd.DataFrame(rows)
    df = df[(df.file_date >= FIRST_FILED) & (df.file_date <= LAST_FILED)]
    df.to_csv(EFTS_CSV, index=False)
    return df.astype(str)


def filing_documents(cik: int, adsh: str) -> list[tuple[str, str, str]]:
    """(description, href, type) of every document in a filing (its -index.htm page)."""
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/{adsh}-index.htm"
    b = sec.sec_get(url, "idx")
    if b is None:
        return []
    t = b.decode("latin-1")
    pat = (r'<tr[^>]*>\s*<td[^>]*>\d+</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*><a href="([^"]+)"[^>]*>.*?</a>(?:.*?)</td>'
           r'\s*<td[^>]*>(.*?)</td>')
    return [(m.group(1).strip(), m.group(2), m.group(3).strip()) for m in re.finditer(pat, t, re.S)]


def information_statement(cik: int, filings: pd.DataFrame, max_back: int = 4) -> tuple[str, str, str]:
    """(adsh, url, text) of the information statement in the latest 10-12B filing that has an EX-99.1
    (looking back at most ``max_back`` filings), else the latest filing's main document."""
    g = filings.sort_values(["file_date", "adsh"])
    fallback = None
    for adsh in list(g.adsh)[::-1][:max_back]:
        docs = filing_documents(cik, adsh)
        ex = [x for x in docs if x[2].upper().startswith("EX-99.1") or "information statement" in x[0].lower()]
        main = [x for x in docs if x[2].upper().startswith(("10-12B", "S-1", "S-4", "S-11"))]
        if ex:
            url = "https://www.sec.gov" + ex[0][1]
            return adsh, url, html_text(sec.sec_get(url, "docs_head", max_bytes=200_000) or b"")
        if fallback is None and main:
            fallback = (adsh, "https://www.sec.gov" + main[0][1])
    if fallback is None:
        return "", "", ""
    adsh, url = fallback
    return adsh, url, html_text(sec.sec_get(url, "docs_head", max_bytes=200_000) or b"")


COMPLETE_PATTERNS = [
    rf"On (?P<d>{DATE}),?[^.]{{0,300}}?(?:completed|consummated|effected|completion of|distributed)[^.]{{0,250}}?"
    r"(?:spin-off|spin off|spinoff|separation|distribution|split-off)",
    rf"(?:spin-off|spin off|spinoff|separation|distribution)[^.]{{0,200}}?(?:was|became|were) (?:completed|effective|"
    rf"consummated|effected)[^.]{{0,80}}?\bon (?P<d>{DATE})",
    rf"(?:spin-off|spinoff|separation|distribution)[^.]{{0,120}}?effective (?:as of|at)[^.]{{0,60}}?\bon (?P<d>{DATE})",
]


def completion_date_from_8ks(cik: int, filings: pd.DataFrame, around: pd.Timestamp | None,
                             after: pd.Timestamp | None) -> tuple[pd.Timestamp | None, str, str]:
    """The distribution date stated by the registrant's own 8-Ks filed shortly after the spin."""
    f = filings[filings.form.isin(["8-K", "8-K/A"])].copy()
    f["fd"] = pd.to_datetime(f.filingDate)
    if around is not None:
        f = f[(f.fd >= around - pd.Timedelta(days=10)) & (f.fd <= around + pd.Timedelta(days=60))]
    elif after is not None:
        f = f[(f.fd >= after) & (f.fd <= after + pd.Timedelta(days=150))]
    else:
        return None, "", ""
    for r in f.sort_values("fd").head(4).itertuples():
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{r.accessionNumber.replace('-', '')}/"
               f"{r.primaryDocument}")
        b = sec.sec_get(url, "eightk", max_bytes=120_000)
        if not b:
            continue
        t = html_text(b)
        found = _dates_near(t, COMPLETE_PATTERNS)
        found = [(d, s) for d, s in found if d <= r.fd + pd.Timedelta(days=1) and d >= r.fd - pd.Timedelta(days=40)]
        if found:
            d, s = most_common_date(found)
            return d, s, url
    return None, "", ""


def resolve_parent(name: str) -> tuple[int | None, str]:
    """The most active EDGAR entity among the close name matches (score >= 75% of the best)."""
    name = re.sub(r"\b(?:the|its|and)\b", " ", name or "").strip(" ,.")
    hits = entity_lookup(name) if len(name) >= 3 else []
    if not hits:
        return None, ""
    top = max(h[3] for h in hits)
    best = max((h for h in hits if h[3] >= 0.75 * top), key=lambda h: h[2])
    return best[0], best[1]


def entity_lookup(name: str) -> list[tuple[int, str, int, float]]:
    """EDGAR entity index matches for a company name: (cik, entity, activity rank, score)."""
    if not name:
        return []
    j = sec.efts({"keysTyped": name})
    if not j:
        return []
    out = []
    for h in j.get("hits", {}).get("hits", []):
        out.append((int(h["_id"]), h["_source"].get("entity", ""), int(h["_source"].get("rank") or 0),
                    float(h.get("_score") or 0)))
    return out


# ======================================================================== build

PARENT_TICKER = r"\((?:NYSE|NASDAQ|Nasdaq|NYSE American|NYSE MKT|NasdaqGS|Nasdaq Global Select Market)\s*:\s*([A-Z][A-Z.]{0,5})\)"

HAND_CSV = OUT / "hand_review.csv"

# Spin-offs registered on Form S-1 instead of Form 10 (found by EDGAR full-text search of S-1 filings for
# "information statement" + "pro rata distribution" + "regular-way"; each checked to be a pro rata distribution
# of exchange-listed common stock). Reverse Morris Trust deals registered on S-4 are out of scope (ledger 0.2).
SUPPLEMENT_S1 = {896842: "Orchard Supply Hardware Stores", 1173514: "Hyster-Yale Materials Handling",
                 1591588: "A-Mark Precious Metals", 1634117: "Barnes & Noble Education",
                 1637655: "Horizon Global", 1629210: "Paramount Gold Nevada", 1709164: "Hamilton Beach Brands Holding",
                 2057463: "GCI Liberty"}
# Found by the CSD ETF cross-check (archive.org holdings 2024-04 and 2025-03): pro rata spin-offs registered on
# Form S-4 (Vimeo, IAC 2021) and Form S-11 (Millrose, Lennar 2025).
SUPPLEMENT_OTHER = {1837686: ("Vimeo", ["S-4", "S-4/A"]), 2017206: ("Millrose Properties", ["S-11", "S-11/A"])}


def nyse_sessions() -> pd.DatetimeIndex:
    import json
    j = json.loads((CACHE_ROOT / "benchmarks/chart_ONEQ.json")
                   .read_text())["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    return pd.DatetimeIndex(sorted(set(pd.to_datetime(ts.strftime("%Y-%m-%d")))))


def next_session(sessions: pd.DatetimeIndex, d: pd.Timestamp) -> pd.Timestamp | None:
    i = sessions.searchsorted(d, side="right")
    return sessions[i] if i < len(sessions) else None


def first_form(filings: pd.DataFrame, forms, after: str) -> pd.Series | None:
    f = filings[filings.form.isin(forms) & (filings.filingDate >= after)].sort_values("filingDate")
    return f.iloc[0] if len(f) else None


def delisting(filings: pd.DataFrame, after: str) -> dict:
    """First Form 25 after ``after`` and its likely reason from the 8-K items of the 45 days before it."""
    f25 = filings[filings.form.isin(["25-NSE", "25"]) & (filings.filingDate >= after)].sort_values("filingDate")
    periodic = pd.to_datetime(filings.loc[filings.form.isin(["10-Q", "10-K", "10-K/A", "10-KT"]), "filingDate"])
    r = None
    for x in f25.itertuples():   # a Form 25 followed by more periodic reports was for another class or a move
        if not (periodic > pd.Timestamp(x.filingDate) + pd.Timedelta(days=100)).any():
            r = x
            break
    if r is None:
        return {"delist_form25_date": "", "delist_form25_accession": "", "delist_reason": "", "delist_8k_items": ""}
    d = pd.Timestamp(r.filingDate)
    k = filings[filings.form.str.startswith("8-K") & (pd.to_datetime(filings.filingDate) >= d - pd.Timedelta(days=45))
                & (pd.to_datetime(filings.filingDate) <= d + pd.Timedelta(days=10))]
    items = ",".join(str(x) for x in k["items"].fillna(""))
    reason = ("merger" if re.search(r"\b(2\.01|3\.03|5\.01)\b", items) else
              "bankruptcy" if "1.03" in items else "other")
    return {"delist_form25_date": r.filingDate, "delist_form25_accession": r.accessionNumber,
            "delist_reason": reason, "delist_8k_items": items}


def build_events(offline: bool = False) -> pd.DataFrame:
    f10 = enumerate_10_12b()
    f10["cik"] = f10.ciks.str.split("|").str[0].astype(int)
    sessions = nyse_sessions()
    groups = list(f10.groupby("cik"))
    supplement = {c: (n, ["S-1", "S-1/A"]) for c, n in SUPPLEMENT_S1.items()} | SUPPLEMENT_OTHER
    for cik, (name, forms) in supplement.items():   # their registration filings stand in for the Form 10 filings
        f = pd.DataFrame(sec.submissions(cik)["all_filings"])
        f = f[f.form.isin(forms)]
        f = f[f.filingDate <= LAST_FILED]
        g = pd.DataFrame({"adsh": f.accessionNumber, "file_date": f.filingDate, "names": name,
                          "form": f.form, "cik": cik})
        groups.append((cik, g))
    if not offline:   # warm the cache in parallel (the limiter still caps SEC at 5 requests a second)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(lambda cg: _registrant_row(cg[0], cg[1], sessions, False), groups))
    return pd.DataFrame([_registrant_row(cik, g, sessions, True) for cik, g in groups])


def _registrant_row(cik: int, g: pd.DataFrame, sessions: pd.DatetimeIndex, offline: bool) -> dict:
    try:
        return _registrant_row_inner(cik, g, sessions, offline)
    except Exception as exc:  # reported per registrant
        if not offline:
            return {}
        return {"spinco_cik": cik, "build_error": repr(exc)[:300]}


def _registrant_row_inner(cik: int, g: pd.DataFrame, sessions: pd.DatetimeIndex, offline: bool) -> dict:
    if True:
        sub = sec.submissions(cik, offline=offline)
        fil = pd.DataFrame(sub["all_filings"])
        own = [sub.get("name", "")] + [x["name"] for x in sub.get("formerNames") or []]
        own += [re.sub(r"\s*\(.*$", "", n) for n in g.names.str.split("|").str[0]]
        adsh, url, text = information_statement(cik, g)
        hand: dict = {}   # the hand review is applied in finalize(), not in the raw build
        row = {"spinco_cik": cik, "spinco_name_sec": sub.get("name", ""), "form10_first": g.file_date.min(),
               "form10_last": g.file_date.max(), "form10_filings": len(g), "info_statement_url": url,
               "info_statement_adsh": adsh, "sec_tickers_now": "|".join(sub.get("tickers") or []),
               "sec_exchanges_now": "|".join(e or "" for e in sub.get("exchanges") or []),
               "sic": sub.get("sic", ""), "state_of_incorporation": sub.get("stateOfIncorporation", ""),
               "former_names": "|".join(x["name"] for x in sub.get("formerNames") or [])}
        row["is_spinoff_text"] = is_spinoff_text(text)
        row["parent_name_text"], row["parent_snippet"] = find_parent(text, own)
        exp_dist, row["dist_snippet"] = find_distribution_date(text)
        rec, _ = find_record_date(text)
        row["record_date"] = rec.date().isoformat() if rec is not None else ""
        row["dist_date_info_statement"] = exp_dist.date().isoformat() if exp_dist is not None else ""
        pt = [t for t in re.findall(PARENT_TICKER, text[:300_000])]
        row["parent_ticker_text"] = pd.Series(pt).value_counts().index[0] if pt else ""
        row["symbol_text"] = find_symbol(text, exclude=row["parent_ticker_text"])
        row["symbols_all_text"] = "|".join(find_symbols(text))
        row["exchange_text"] = find_exchange(text)
        row["ratio_text"] = find_ratio(text)
        certs = fil[fil.form.astype(str).str.startswith("CERT") & (fil.filingDate >= g.file_date.min())]
        certs = certs.sort_values("filingDate")
        row["cert_form"] = certs.iloc[0].form if len(certs) else ""
        row["cert_date"] = certs.iloc[0].filingDate if len(certs) else ""
        a8 = first_form(fil, ["8-A12B"], g.file_date.min())
        row["form8a_date"] = a8.filingDate if a8 is not None else ""
        rw = first_form(fil, ["RW"], g.file_date.min())
        row["withdrawn_rw_date"] = rw.filingDate if rw is not None else ""
        cert_ts = pd.Timestamp(row["cert_date"]) if row["cert_date"] else None
        k_date, row["dist_8k_snippet"], row["dist_8k_url"] = completion_date_from_8ks(cik, fil, exp_dist, cert_ts)
        row["dist_date_8k"] = k_date.date().isoformat() if k_date is not None else ""
        for k, v in hand.items():
            row[k] = v
        pname = row.get("parent_name_hand") or row["parent_name_text"]
        pc, pe = (int(row["parent_cik_hand"]), "hand") if row.get("parent_cik_hand") else resolve_parent(pname)
        row["parent_cik"], row["parent_entity"] = pc, pe
        dist = (row.get("dist_date_hand") or row["dist_date_8k"] or row["dist_date_info_statement"])
        row["distribution_date"] = dist
        row["distribution_date_source"] = ("hand" if row.get("dist_date_hand") else "8-K" if row["dist_date_8k"]
                                           else "information_statement" if row["dist_date_info_statement"] else "")
        frw = next_session(sessions, pd.Timestamp(dist)) if dist else None
        row["first_regular_way_date"] = frw.date().isoformat() if frw is not None else ""
        row.update(delisting(fil, dist or g.file_date.min()))
        return row


def shares_after(cik: int, dist: str) -> dict:
    """Shares outstanding on the cover of the first 10-Q / 10-K filed after the distribution date whose cover date
    is on or after it (XBRL dei:EntityCommonStockSharesOutstanding from companyfacts; classes reported
    separately on the same cover are summed)."""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json"
    empty = {"shares_outstanding": "", "shares_date": "", "shares_accession": "", "shares_source": ""}
    j = sec.sec_json(url, "xbrl") if dist else None
    if not j:
        return empty
    for tax, tag in (("dei", "EntityCommonStockSharesOutstanding"),
                     ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic")):
        fact = (j.get("facts", {}).get(tax, {}).get(tag) or {}).get("units", {})
        rows = [r for u in fact.values() for r in u]
        rows = [r for r in rows if r.get("filed", "") >= dist and r.get("end", "") >= dist and float(r["val"]) > 1000
                and str(r.get("form", "")).startswith(("10-Q", "10-K"))]
        if not rows:
            continue
        first = min(rows, key=lambda r: (r["filed"], r["accn"]))
        same = [r for r in rows if r["accn"] == first["accn"] and r.get("end") == first.get("end")]
        if tax == "dei":
            total = sum(float(v) for v in {r["val"] for r in same})
        else:   # the shortest period on that report (a quarter)
            total = float(min(same, key=lambda r: (pd.Timestamp(r["end"]) - pd.Timestamp(r.get("start", r["end"])))
                              .days)["val"])
        return {"shares_outstanding": int(total), "shares_date": first.get("end", ""),
                "shares_accession": first["accn"], "shares_source": f"{tax}:{tag}"}
    return empty


# ======================================================================== finalize (hand review applied)

EVENT_COLUMNS = [
    "event_id", "classification", "spinco_cik", "spinco_name", "spinco_symbol", "spinco_tickers_try",
    "parent_cik", "parent_name", "parent_tickers_try", "record_date", "distribution_date", "distribution_date_source",
    "first_regular_way_date", "exchange", "exchange_source", "nasdaq_listed", "registration", "form10_first",
    "form10_last", "info_statement_url", "dist_date_info_statement", "dist_date_8k", "dist_8k_url",
    "date_check", "ratio_text", "cert_form", "cert_date", "delist_form25_date", "delist_form25_accession",
    "delist_reason", "parent_delist_form25_date", "parent_delist_reason", "shares_outstanding", "shares_date",
    "shares_accession", "shares_source", "hand_note"]


def load_hand() -> dict[int, dict]:
    h = pd.read_csv(HAND_CSV, dtype=str).fillna("")
    return {int(r["spinco_cik"]): r.to_dict() for _, r in h.iterrows()}


def exchange_of(row: dict) -> tuple[str, str]:
    cert = str(row.get("cert_form", ""))
    if cert in EXCHANGE_OF_CERT:
        return EXCHANGE_OF_CERT[cert], cert
    if row.get("exchange_text"):
        return row["exchange_text"], "information_statement"
    ex = str(row.get("sec_exchanges_now", "")).split("|")[0]
    return (ex, "sec_submissions_now") if ex else ("", "")


def finalize(raw: pd.DataFrame, hand: dict[int, dict] | None = None) -> pd.DataFrame:
    hand = load_hand() if hand is None else hand
    sessions = nyse_sessions()
    out = []
    for r in raw.fillna("").to_dict("records"):
        if not r.get("spinco_cik"):
            continue
        cik = int(r["spinco_cik"])
        h = hand.get(cik)
        if h is None:
            cls = "excluded:no_information_statement" if str(r.get("is_spinoff_text")) != "True" else "unreviewed"
            h = {}
        else:
            cls = h["classification"]
        row = {k: r.get(k, "") for k in EVENT_COLUMNS if k in r}
        row["classification"] = cls
        row["spinco_cik"] = cik
        row["spinco_name"] = r.get("spinco_name_sec", "")
        row["registration"] = ("S-1" if cik in SUPPLEMENT_S1 else SUPPLEMENT_OTHER[cik][1][0]
                               if cik in SUPPLEMENT_OTHER else "10-12B")
        row["hand_note"] = h.get("note", "")
        sym = h.get("symbol") or r.get("symbol_text", "")
        row["spinco_symbol"] = sym
        now = [x for x in str(r.get("sec_tickers_now", "")).split("|") if x]
        row["spinco_tickers_try"] = "|".join(dict.fromkeys([x for x in now[:1] + [sym] if x]))
        # dates: the hand date when written, else the 8-K, else the information statement
        auto = r.get("dist_date_8k") or r.get("dist_date_info_statement") or ""
        dist = h.get("distribution_date") or auto
        row["distribution_date"] = dist
        row["distribution_date_source"] = ("hand" if h.get("distribution_date") else
                                           "8-K" if r.get("dist_date_8k") else
                                           "information_statement" if r.get("dist_date_info_statement") else "")
        if dist and auto:
            gap = abs((pd.Timestamp(dist) - pd.Timestamp(auto)).days)
            row["date_check"] = "agree" if gap <= 3 else f"differs_{gap}d"
        else:
            row["date_check"] = "no_auto_date" if dist else "no_date"
        if row["distribution_date_source"] == "hand" and row["date_check"] == "agree":
            # the hand date only confirms (or sets the 12:01 a.m. convention on) what SEC text says
            row["distribution_date_source"] = "hand+" + ("8-K" if r.get("dist_date_8k") else "information_statement")
        frw = next_session(sessions, pd.Timestamp(dist)) if dist else None
        row["first_regular_way_date"] = frw.date().isoformat() if frw is not None else ""
        ex, src = (h["exchange"], "hand") if h.get("exchange") else exchange_of(r)
        row["exchange"], row["exchange_source"] = ex, src
        row["nasdaq_listed"] = "Y" if ex == "Nasdaq" else "N"
        # parent
        pc = h.get("parent_cik", "")
        pname = h.get("parent_name") or r.get("parent_name_text", "")
        no_leg = any(x in h.get("note", "") for x in ("not US-listed", "closed-end fund", "no parent leg"))
        if no_leg:
            pc = ""
        if not pc and pname and cls == "spin_off" and not no_leg:
            c, _ = resolve_parent(pname)
            pc = str(c) if c else ""
        row["parent_cik"] = int(float(pc)) if pc else ""
        row["parent_name"] = pname
        row["parent_tickers_try"] = ""
        row["parent_delist_form25_date"] = row["parent_delist_reason"] = ""
        if row["parent_cik"] and cls == "spin_off" and dist:
            ps = sec.submissions(int(row["parent_cik"]))
            if ps:
                row["parent_name"] = ps.get("name", pname)
                pt_text = str(r.get("parent_ticker_text", ""))
                pt_text = pt_text if pt_text and pt_text != sym else ""
                row["parent_tickers_try"] = "|".join(dict.fromkeys(
                    [x for x in (ps.get("tickers") or [])[:1] + [h.get("parent_symbol", ""), pt_text] if x]))
                d = delisting(pd.DataFrame(ps["all_filings"]), dist)
                row["parent_delist_form25_date"], row["parent_delist_reason"] = (d["delist_form25_date"],
                                                                               d["delist_reason"])
        if cls == "spin_off" and dist:
            row.update(shares_after(cik, dist))
        out.append(row)
    ev = pd.DataFrame(out)
    for c in EVENT_COLUMNS:
        if c not in ev:
            ev[c] = ""
    ev = ev[EVENT_COLUMNS].sort_values(["first_regular_way_date", "spinco_cik"])
    inwin = (ev.classification == "spin_off") & (ev.first_regular_way_date >= EVENT_START) & \
            (ev.first_regular_way_date <= EVENT_END)
    late = (ev.first_regular_way_date == "") & (ev.distribution_date > EVENT_END)
    ev.loc[(ev.classification == "spin_off") & ~inwin & ((ev.first_regular_way_date != "") | late),
           "classification"] = "out_of_window"
    ev.loc[(ev.classification == "spin_off") & (ev.first_regular_way_date == ""), "classification"] = "no_date"
    ids = ev.first_regular_way_date.str.replace("-", "") + "_" + ev.spinco_cik.astype(str)
    ev["event_id"] = ids
    return ev
