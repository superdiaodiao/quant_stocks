"""Two signals from free SEC data (pre-registered in docs/research_ledger_sec_alt.md, section 0, before any result).

(I) Insider buying (Form 4, SEC "Insider Transactions Data Sets"):
    I1  opportunistic officer / director open-market purchase (code P) >= $50k in one Form 4 -> buy at the close of
        the session after the FILING date, hold 63 sessions, at most 10 names, NAV / 10 each.
    I2  cluster buys: >= 3 distinct opportunistic officers / directors buying within a 30-day trade-date window.
    I3  I1 restricted to market cap < $5B (XBRL frames dei:EntityCommonStockSharesOutstanding x raw close).
    "Routine" insiders (Cohen-Malloy-Pomorski): an open-market trade in the same calendar month in each of the three
    previous calendar years (filings known before 1 January) -> their purchases are dropped.
(II) 13F cloning (EDGAR 13F-HR information tables):
    F1  Berkshire Hathaway top 10 by reported value, priced names equal weight, re-weighted at the close of the
        session after each filing's acceptance date.
    F2  union of the top 10 of Akre Capital, Gardner Russo & Quinn, Ruane Cunniff & Goldfarb (chosen before results).

Prices: the frozen Nasdaq panel via scripts/research_livermore.load_window (total-return index, terminal returns,
successor stitching); names outside the panel are skipped and counted. Costs: IBKR Pro Tiered for the actual dollar
size (scripts/research_reversal_dev.order_cost / half_spread). Benchmark ONEQ total return (QQQ reported).
Account: $10,000 cash at the close of 2013-12-31, last session 2026-08-31; halves 2014-2019 and 2020-2026-08.

Raw SEC files are cached locally under research_cache/sec_alt/ (never in git). The SEC User-Agent comes from
src/io/sec_contact.py and is never printed; requests are throttled to <= 3 per second.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_canslim_dev as cs  # noqa: E402  (date guard helpers)
from scripts import research_livermore as lv  # noqa: E402  (panel window loader)
from scripts import research_qqq_timing as qt  # noqa: E402  (Yahoo chart parser, RF)
from scripts import research_reversal_dev as rev  # noqa: E402  (IBKR cost model, half spread)
from src.io.sec_contact import sec_user_agent  # noqa: E402

NORM = NormalDist()
MAIN = Path("/Users/bytedance/code/quant_stocks")
CACHE = MAIN / "research_cache/sec_alt"
RAW_INSIDER = CACHE / "raw/insider"
RAW_FRAMES = CACHE / "raw/frames"
RAW_13F = CACHE / "raw/13f"
BENCH_DIR = MAIN / "research_cache/benchmarks"
INPUTS = cs.INPUTS
from scripts import study_data_version as dv  # noqa: E402  (REVERSAL_DATA_VERSION; docs/robustness_data_v2.md)
OUT = dv.versioned(ROOT / "output/research_only/sec_alt")
LEDGER = ROOT / "docs/research_ledger_sec_alt.md"

START = "2013-12-31"          # $10,000 cash at this close; first return 2014-01-02
END = "2026-08-31"            # last panel session
HALVES = (("2014-2019", "2014-01-01", "2019-12-31"), ("2020-2026-08", "2020-01-01", END))
ACCOUNT = 10_000.0
INSIDER_QUARTERS = [f"{y}q{q}" for y in range(2011, 2027) for q in range(1, 5) if (y, q) <= (2026, 2)]
FRAME_QUARTERS = [f"CY{y}Q{q}I" for y in range(2012, 2027) for q in range(1, 5) if (2012, 4) <= (y, q) <= (2026, 2)]
SHARES_LAG_DAYS = 45
MIN_PURCHASE = 50_000.0
CLUSTER_N, CLUSTER_DAYS = 3, 30
HOLD_SESSIONS = 63
K = 10
MCAP_MAX = 5e9
REBAL_BAND = 0.01
BERKSHIRE = {"Berkshire Hathaway": ["1067983"]}
F2_MANAGERS = {"Akre Capital": ["1112520"], "Gardner Russo & Quinn": ["860643"],
               "Ruane Cunniff & Goldfarb": ["728014", "1720792"]}
BAD_TITLE = re.compile(r"prefer|note|debenture|warrant|\bunits?\b|\bright", re.I)
N_TRIALS = 5


# ======================================================================== SEC access (throttled, cached)

_last_request = [0.0]


def sec_get(url: str, dest: Path | None = None, max_per_second: float = 3.0, retries: int = 4) -> bytes:
    """GET ``url`` with the SEC contact User-Agent; cached at ``dest`` when given. Never prints the User-Agent."""
    if dest is not None and dest.is_file() and dest.stat().st_size > 0:
        return dest.read_bytes()
    for attempt in range(retries):
        wait = 1.0 / max_per_second - (time.monotonic() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": sec_user_agent(), "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"failed after {retries} tries: {url}")
    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
    return data


def insider_zip_url(quarter: str) -> list[str]:
    base = "https://www.sec.gov/files/{}/data/insider-transactions-data-sets/{}_form345.zip"
    return [base.format(d, quarter) for d in ("structureddata", "datastandardsinnovation")]


def fetch_insider_zips(quarters=INSIDER_QUARTERS) -> list[Path]:
    paths = []
    for q in quarters:
        dest = RAW_INSIDER / f"{q}_form345.zip"
        if not dest.is_file():
            for url in insider_zip_url(q):
                try:
                    sec_get(url, dest)
                    break
                except urllib.error.HTTPError:
                    continue
            else:
                raise RuntimeError(f"insider data set {q} not found")
        paths.append(dest)
    return paths


# ======================================================================== Form 4 parsing

def _read_tsv(z: zipfile.ZipFile, name: str, cols: list[str]) -> pd.DataFrame:
    with z.open(name) as f:
        return pd.read_csv(f, sep="\t", usecols=cols, dtype=str, quoting=3, on_bad_lines="skip",
                           encoding="latin-1", keep_default_na=False)


def parse_insider_zip(path: Path) -> pd.DataFrame:
    """Open-market trades (codes P and S) on original Form 4s, one row per (transaction, reporting owner)."""
    with zipfile.ZipFile(path) as z:
        sub = _read_tsv(z, "SUBMISSION.tsv", ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK",
                                               "ISSUERTRADINGSYMBOL"])
        own = _read_tsv(z, "REPORTINGOWNER.tsv", ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNER_RELATIONSHIP"])
        tr = _read_tsv(z, "NONDERIV_TRANS.tsv", ["ACCESSION_NUMBER", "SECURITY_TITLE", "TRANS_DATE", "TRANS_CODE",
                                                 "TRANS_SHARES", "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"])
    sub = sub[sub["DOCUMENT_TYPE"] == "4"]
    tr = tr[tr["TRANS_CODE"].isin(["P", "S"])]
    df = tr.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER")
    out = pd.DataFrame({
        "accession": df["ACCESSION_NUMBER"],
        "filing_date": pd.to_datetime(df["FILING_DATE"], format="%d-%b-%Y", errors="coerce"),
        "issuer_cik": pd.to_numeric(df["ISSUERCIK"], errors="coerce"),
        "symbol": df["ISSUERTRADINGSYMBOL"].str.strip().str.upper(),
        "owner_cik": pd.to_numeric(df["RPTOWNERCIK"], errors="coerce"),
        "relationship": df["RPTOWNER_RELATIONSHIP"],
        "title": df["SECURITY_TITLE"],
        "trans_date": pd.to_datetime(df["TRANS_DATE"], format="%d-%b-%Y", errors="coerce"),
        "code": df["TRANS_CODE"],
        "ad": df["TRANS_ACQUIRED_DISP_CD"],
        "shares": pd.to_numeric(df["TRANS_SHARES"], errors="coerce"),
        "price": pd.to_numeric(df["TRANS_PRICEPERSHARE"], errors="coerce"),
    })
    return out.dropna(subset=["filing_date", "issuer_cik", "owner_cik", "trans_date"])


def load_insider_trades(refresh: bool = False) -> pd.DataFrame:
    dest = CACHE / "processed/insider_open_market_trades.csv.gz"
    if dest.is_file() and not refresh:
        df = pd.read_csv(dest, dtype={"symbol": str})
        for c in ("filing_date", "trans_date"):
            df[c] = pd.to_datetime(df[c], errors="coerce")
        df = df.dropna(subset=["filing_date", "trans_date"])
        cs.assert_window(df["filing_date"], None, "2026-06-30", "Form 4 filing dates")
        return df
    parts = [parse_insider_zip(p) for p in fetch_insider_zips()]
    df = pd.concat(parts, ignore_index=True).drop_duplicates()
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dest, index=False, compression="gzip")
    return df


def routine_owners(trades: pd.DataFrame) -> set:
    """{(owner_cik, year)}: owners with an open-market trade in the same calendar month in each of the three previous
    calendar years, counting only filings made before 1 January of ``year`` (Cohen-Malloy-Pomorski)."""
    t = trades[["owner_cik", "trans_date", "filing_date"]].copy()
    t["ty"], t["tm"] = t["trans_date"].dt.year, t["trans_date"].dt.month
    out = set()
    years = trades["trans_date"].dt.year
    years = years[years.between(2000, 2026)]
    for year in range(int(years.min()) + 3, int(years.max()) + 2):
        known = t[(t["filing_date"] < pd.Timestamp(f"{year}-01-01")) & t["ty"].between(year - 3, year - 1)]
        if known.empty:
            continue
        g = known.drop_duplicates(["owner_cik", "ty", "tm"]).groupby(["owner_cik", "tm"])["ty"].nunique()
        out |= {(int(o), year) for o in g[g == 3].index.get_level_values(0).unique()}
    return out


def qualified_purchases(trades: pd.DataFrame, routine: set) -> pd.DataFrame:
    """One row per (Form 4, officer / director owner): opportunistic open-market purchase totals."""
    p = trades[(trades["code"] == "P") & (trades["ad"] == "A") & (trades["price"] > 0) & (trades["shares"] > 0)
               & (trades["trans_date"] <= trades["filing_date"])
               & trades["relationship"].fillna("").str.contains("Director|Officer")
               & ~trades["title"].fillna("").str.contains(BAD_TITLE)].copy()
    p["value"] = p["shares"] * p["price"]
    g = p.groupby(["accession", "owner_cik"]).agg(
        filing_date=("filing_date", "first"), issuer_cik=("issuer_cik", "first"), symbol=("symbol", "first"),
        trans_date=("trans_date", "max"), value=("value", "sum")).reset_index()
    key = list(zip(g["owner_cik"].astype(int), g["trans_date"].dt.year))
    g["routine"] = [k in routine for k in key]
    return g


# ======================================================================== issuer -> panel security

@dataclass
class SecMap:
    by_cik: dict          # cik -> [security_id]
    intervals: pd.DataFrame

    def lookup(self, cik: int, symbol: str, date: pd.Timestamp) -> tuple[str | None, str]:
        sids = self.by_cik.get(int(cik), [])
        if not sids:
            return None, "cik_not_in_panel"
        if len(sids) == 1:
            return sids[0], "unique_cik"
        iv = self.intervals[self.intervals["security_id"].isin(sids) & (self.intervals["ticker"] == symbol)]
        if iv.empty:
            return None, "multi_class_no_ticker_match"
        live = iv[(iv["start"] <= date) & (iv["end"] >= date)]
        pick = (live if not live.empty else iv)["security_id"].unique()
        return (pick[0], "ticker_match") if len(pick) == 1 else (None, "multi_class_ambiguous")


def load_secmap(panel_ids) -> SecMap:
    sm = pd.read_csv(INPUTS / "security_master.csv", dtype=str, usecols=["security_id", "cik"])
    sm = sm[sm["security_id"].isin(set(panel_ids))]
    by = {}
    for r in sm.itertuples():
        if isinstance(r.cik, str) and r.cik.strip():
            by.setdefault(int(r.cik), []).append(r.security_id)
    iv = pd.read_csv(INPUTS / "ticker_intervals.csv", dtype=str, usecols=["security_id", "ticker", "start", "end"])
    iv["start"], iv["end"] = pd.to_datetime(iv["start"]), pd.to_datetime(iv["end"])
    return SecMap(by_cik={k: sorted(set(v)) for k, v in by.items()}, intervals=iv)


# ======================================================================== shares outstanding (I3)

def load_shares(refresh: bool = False) -> pd.DataFrame:
    rows = []
    for q in FRAME_QUARTERS:
        url = f"https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/{q}.json"
        try:
            j = json.loads(sec_get(url, RAW_FRAMES / f"{q}.json"))
        except urllib.error.HTTPError:
            continue
        for d in j["data"]:
            rows.append((int(d["cik"]), d["end"], float(d["val"])))
    df = pd.DataFrame(rows, columns=["cik", "end", "shares"])
    df["end"] = pd.to_datetime(df["end"])
    df["usable_from"] = df["end"] + pd.Timedelta(days=SHARES_LAG_DAYS)
    return df[df["shares"] > 0].sort_values(["cik", "usable_from"]).reset_index(drop=True)


def shares_asof(shares: pd.DataFrame, cik: int, date: pd.Timestamp) -> float:
    s = shares[(shares["cik"] == cik) & (shares["usable_from"] <= date)]
    return float(s["shares"].iloc[-1]) if len(s) else float("nan")


# ======================================================================== insider signals

def cluster_signals(q: pd.DataFrame) -> pd.DataFrame:
    """I2: per issuer, a Form 4 that brings the count of distinct qualifying insiders whose purchase dates fit in one
    30-day window to >= 3 (window total >= $50k), using only Form 4s filed by that day; then 30 days of quiet."""
    out = []
    for cik, g in q.sort_values(["filing_date", "accession"]).groupby("issuer_cik"):
        g = g.reset_index(drop=True)
        quiet_until = pd.Timestamp.min
        for i, r in g.iterrows():
            if r["filing_date"] <= quiet_until:
                continue
            known = g[(g["filing_date"] <= r["filing_date"])
                      & ((g["trans_date"] - r["trans_date"]).abs() <= pd.Timedelta(days=CLUSTER_DAYS))]
            best = None
            for a in sorted(set(known["trans_date"])):
                if not (a <= r["trans_date"] <= a + pd.Timedelta(days=CLUSTER_DAYS)):
                    continue
                w = known[(known["trans_date"] >= a) & (known["trans_date"] <= a + pd.Timedelta(days=CLUSTER_DAYS))]
                n, v = w["owner_cik"].nunique(), w["value"].sum()
                if n >= CLUSTER_N and v >= MIN_PURCHASE and (best is None or v > best[1]):
                    best = (n, v)
            if best is not None:
                out.append({"filing_date": r["filing_date"], "issuer_cik": cik, "symbol": r["symbol"],
                            "value": best[1], "n_insiders": best[0], "accession": r["accession"]})
                quiet_until = r["filing_date"] + pd.Timedelta(days=CLUSTER_DAYS)
    return pd.DataFrame(out, columns=["filing_date", "issuer_cik", "symbol", "value", "n_insiders", "accession"])


def entry_session(sessions: pd.DatetimeIndex, filing_date: pd.Timestamp) -> pd.Timestamp | None:
    i = sessions.searchsorted(filing_date, side="right")
    return sessions[i] if i < len(sessions) else None


def attach_entries(sig: pd.DataFrame, secmap: SecMap, sessions: pd.DatetimeIndex, close: pd.DataFrame,
                   last_row: pd.Series) -> tuple[pd.DataFrame, dict]:
    """Map each signal to a panel security and an entry session; count every reason a signal is skipped."""
    counts = {"signals": int(len(sig))}
    rows = []
    for r in sig.itertuples():
        sid, how = secmap.lookup(r.issuer_cik, r.symbol if isinstance(r.symbol, str) else "", r.filing_date)
        if sid is None:
            counts[how] = counts.get(how, 0) + 1
            continue
        e = entry_session(sessions, r.filing_date)
        if e is None or e > pd.Timestamp(END) or e <= pd.Timestamp(START):
            counts["entry_outside_window"] = counts.get("entry_outside_window", 0) + 1
            continue
        if sid not in close.columns or not np.isfinite(close.at[e, sid]) or (pd.notna(last_row.get(sid))
                                                                               and last_row[sid] < e):
            counts["no_panel_price_on_entry"] = counts.get("no_panel_price_on_entry", 0) + 1
            continue
        rows.append({**r._asdict(), "security_id": sid, "entry": e, "map": how})
    df = pd.DataFrame(rows)
    if len(df):
        df = df.drop(columns=["Index"]).sort_values(["entry", "value"], ascending=[True, False])
        before = len(df)
        df = df.drop_duplicates(["entry", "security_id"])
        counts["same_day_duplicates"] = before - len(df)
    counts["priced_signals"] = int(len(df))
    return df, counts


# ======================================================================== 13F

def manager_filings(ciks: list[str]) -> pd.DataFrame:
    rows = []
    for cik in ciks:
        j = json.loads(sec_get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                               RAW_13F / f"submissions_{int(cik)}.json"))
        blocks = [j["filings"]["recent"]]
        for f in j["filings"].get("files", []):
            blocks.append(json.loads(sec_get(f"https://data.sec.gov/submissions/{f['name']}", RAW_13F / f["name"])))
        for b in blocks:
            for k in range(len(b["form"])):
                if b["form"][k] == "13F-HR":
                    rows.append({"cik": int(cik), "accession": b["accessionNumber"][k], "filing_date": b["filingDate"][k],
                                 "acceptance": b["acceptanceDateTime"][k], "report_date": b["reportDate"][k]})
    df = pd.DataFrame(rows).drop_duplicates("accession")
    df["acc_date"] = pd.to_datetime(df["acceptance"].str[:10])
    df = df[(df["acc_date"] >= pd.Timestamp("2013-06-01")) & (df["acc_date"] <= pd.Timestamp(END))]
    # one filing per report period (Ruane's two entities overlap in 2018): the first accepted
    df = df.sort_values("acceptance").drop_duplicates("report_date", keep="first")
    return df.sort_values("acceptance").reset_index(drop=True)


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_infotable(xml: bytes) -> pd.DataFrame:
    root = ET.fromstring(xml)
    rows = []
    for it in root.iter():
        if _strip_ns(it.tag) != "infoTable":
            continue
        d = {}
        for c in it.iter():
            t = _strip_ns(c.tag)
            if t in ("nameOfIssuer", "titleOfClass", "cusip", "value", "putCall", "sshPrnamtType", "sshPrnamt"):
                d[t] = (c.text or "").strip()
        rows.append(d)
    df = pd.DataFrame(rows)
    for c in ("putCall", "sshPrnamtType"):
        if c not in df:
            df[c] = ""
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def filing_holdings(cik: int, accession: str) -> pd.DataFrame:
    nod = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{nod}"
    idx = json.loads(sec_get(f"{base}/index.json", RAW_13F / f"{cik}_{nod}_index.json"))
    names = [i["name"] for i in idx["directory"]["item"]]
    xmls = [n for n in names if n.lower().endswith(".xml") and n.lower() != "primary_doc.xml"]
    for n in xmls:
        df = parse_infotable(sec_get(f"{base}/{n}", RAW_13F / f"{cik}_{nod}_{n}"))
        if len(df):
            return df
    raise ValueError(f"no information table in {cik} {accession}: {names}")


def top10(h: pd.DataFrame) -> pd.DataFrame:
    h = h[(h["putCall"].fillna("") == "") & (h["sshPrnamtType"].fillna("SH").str.upper() != "PRN")].copy()
    h["cusip"] = h["cusip"].str.upper()
    h = h[h["cusip"].str.fullmatch(r"[0-9A-Z]{9}") & (h["cusip"] != "000000000")]
    g = h.groupby("cusip").agg(value=("value", "sum"), name=("nameOfIssuer", "first"),
                               title=("titleOfClass", "first")).reset_index()
    g = g.sort_values(["value", "cusip"], ascending=[False, True]).head(10).reset_index(drop=True)
    g["rank"] = np.arange(1, len(g) + 1)
    g["weight_in_top10"] = g["value"] / g["value"].sum()
    return g


def load_13f(managers: dict) -> pd.DataFrame:
    """Rows: manager, accession, acceptance, report_date, rank, cusip, name, title, value, weight_in_top10."""
    rows = []
    for mgr, ciks in managers.items():
        for f in manager_filings(ciks).itertuples():
            t = top10(filing_holdings(f.cik, f.accession))
            t.insert(0, "manager", mgr)
            t.insert(1, "accession", f.accession)
            t.insert(2, "acceptance", f.acceptance)
            t.insert(3, "report_date", f.report_date)
            rows.append(t)
    return pd.concat(rows, ignore_index=True)


_SUFFIX = re.compile(r"\b(INC|CORP|CORPORATION|CO|COMPANY|LTD|PLC|LLC|LP|L P|HLDGS|HOLDINGS|HLDG|GROUP|GRP|NEW|DEL|"
                     r"COM|CL|CLASS|A|B|C|THE|N V|NV|SA|AG|SE|INCORPORATED|LIMITED|TR|TRUST|INTL|INTERNATIONAL)\b")


def norm_name(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", str(s).upper().replace("&", " AND "))
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def cusip_candidates(cusips: pd.DataFrame, panel_ids) -> pd.DataFrame:
    """Name-match each 13F CUSIP to panel securities (input to the hand review in CUSIP_REVIEW)."""
    sm = pd.read_csv(INPUTS / "security_master.csv", dtype=str)
    sm = sm[sm["security_id"].isin(set(panel_ids))]
    names = {}
    for r in sm.itertuples():
        alln = [r.name] + [re.sub(r"\s*\(.*?\)\s*$", "", x) for x in str(r.former_names or "").split(";") if x.strip()]
        for n in alln:
            if isinstance(n, str) and n.strip() and n != "nan":
                names.setdefault(norm_name(n), set()).add(r.security_id)
    tick = sm.set_index("security_id")[["first_ticker", "name", "share_class", "tickers_observed"]]
    rows = []
    for r in cusips.itertuples():
        hit = sorted(names.get(norm_name(r.name), set()))
        rows.append({"cusip": r.cusip, "name_13f": r.name, "title_13f": r.title,
                     "candidates": " | ".join(f"{s}:{tick.at[s, 'tickers_observed']}:{tick.at[s, 'share_class']}"
                                              for s in hit)})
    return pd.DataFrame(rows)


# Hand review of the name matches (identity and share class only; done before any return was computed).
# cusip -> panel security_id; CUSIPs not listed here are treated as "not in the panel".
# Checked against security_master (name, share class, Nasdaq listing); the panel prices a name only on sessions it
# has a close (e.g. Roper is in the panel only after its 2023 move to Nasdaq). Everything else in the 13F top tens
# (NYSE names such as WFC, KO, AXP, MA, V; foreign ordinaries / ADRs; BIDU, ICLR, SCHW not in the panel) is unpriced.
CUSIP_REVIEW: dict[str, str] = {
    "037833100": "320193",            # Apple
    "500754106": "1637459",           # Kraft Heinz
    "25490A309": "1465112",           # DIRECTV
    "16119P108": "1091667",           # Charter Communications class A
    "00507V109": "718877",            # Activision Blizzard
    "02079K305": "1652044.A",         # Alphabet class A
    "02079K107": "1652044.C",         # Alphabet class C
    "38259P508": "1288776.A",         # Google Inc class A (pre-2015; continues as 1652044.A)
    "256746108": "935703",            # Dollar Tree
    "778296103": "745732",            # Ross Stores
    "67103H107": "898173",            # O'Reilly Automotive
    "87236Y108": "1173431",           # TD Ameritrade
    "25470F302": "1437107.A",         # Discovery series A (later Warner Bros. Discovery)
    "78388J106": "1034054",           # SBA Communications class A (pre-2017 CUSIP)
    "78410G104": "1034054",           # SBA Communications class A (REIT CUSIP)
    "776696106": "882835",            # Roper (panel prices it from 2023-04 only)
    "92345Y106": "1442145",           # Verisk class A
    "G3075P101": "1363829",           # Enstar
    "00724F101": "796343",            # Adobe
    "22160N109": "1057352",           # CoStar
    "009066101": "1559720",           # Airbnb class A
    "217204106": "900075",            # Copart
    "64110L106": "1065280",           # Netflix
    "311900104": "815556",            # Fastenal
    "45168D104": "874716",            # IDEXX
    "24906P109": "818479",            # Dentsply Sirona
    "023135106": "1018724",           # Amazon
    "30303M102": "1326801",           # Facebook / Meta class A
    "225310101": "885550",            # Credit Acceptance
    "595112103": "723125",            # Micron
    "531229854": "1560385.T-FWONK",   # Liberty Media series C Formula One (pre-2023 CUSIP)
    "531229755": "1560385.T-FWONK",   # Liberty Media series C Liberty Formula One (post-2023 CUSIP)
}


# ======================================================================== portfolio engines

def rank_lookup(universe: pd.DataFrame):
    weeks = np.array(sorted(universe["week_end"].unique()), dtype="datetime64[ns]")
    by = {pd.Timestamp(w): g.set_index("security_id")["dv50_rank"].to_dict() for w, g in universe.groupby("week_end")}

    def f(d: pd.Timestamp, sid: str) -> float:
        i = weeks.searchsorted(np.datetime64(d), side="right") - 1
        return by.get(pd.Timestamp(weeks[i]), {}).get(sid, np.nan) if i >= 0 else np.nan
    return f


def _cost(value: float, price: float, sell: bool, rank: float) -> float:
    if value <= 0 or not np.isfinite(price) or price <= 0:
        return 0.0
    return rev.order_cost(value / price, price, sell=sell, hs=rev.half_spread(rank, price))["total"]


def run_insider(entries: pd.DataFrame, data, rank_of, k: int = K, hold: int = HOLD_SESSIONS) -> dict:
    """Event portfolio: buy at the entry close (NAV / k, cash permitting), sell at the close ``hold`` sessions later."""
    perf = data.sessions[(data.sessions >= pd.Timestamp(START)) & (data.sessions <= pd.Timestamp(END))]
    col = {s: i for i, s in enumerate(data.perf_idx.columns)}
    I = data.perf_idx.reindex(perf).to_numpy()
    P = data.close.reindex(perf).to_numpy()
    lr = data.last_row
    by_day = {d: g for d, g in entries.groupby("entry")} if len(entries) else {}
    cash = ACCOUNT
    pos = {}                      # sid -> [units, entry_i, entry_idx, entry_value]
    nav, cost, expo, npos, tv = (np.zeros(len(perf)) for _ in range(5))
    traded, trades, skipped_full, skipped_cash, skipped_held = 0.0, [], 0, 0, 0
    for i, d in enumerate(perf):
        c_day = 0.0
        for sid in [s for s in pos if pd.notna(lr.get(s)) and d > lr[s]]:
            u, ei, eidx, ev = pos.pop(sid)
            v = u * I[i, col[sid]]
            cash += v
            trades.append({"sid": sid, "entry": perf[ei], "exit": d, "reason": "delisted", "ret": I[i, col[sid]] / eidx - 1,
                           "pnl": v - ev})
        for sid in [s for s, p in pos.items() if i - p[1] >= hold]:
            u, ei, eidx, ev = pos.pop(sid)
            c = col[sid]
            v = u * I[i, c]
            k_ = _cost(v, P[i, c], True, rank_of(d, sid))
            cash += v - k_
            c_day += k_
            traded += v
            tv[i] += v
            trades.append({"sid": sid, "entry": perf[ei], "exit": d, "reason": "held_63", "ret": I[i, c] / eidx - 1,
                           "pnl": v - k_ - ev})
        if d in by_day:
            stock = sum(p[0] * I[i, col[s]] for s, p in pos.items())
            size = (cash + stock) / k
            for r in by_day[d].itertuples():
                sid = r.security_id
                if sid in pos:
                    skipped_held += 1
                    continue
                if len(pos) >= k:
                    skipped_full += 1
                    continue
                c = col.get(sid)
                if c is None or not np.isfinite(I[i, c]) or not np.isfinite(P[i, c]):
                    continue
                amt = min(size, cash)
                if amt < 0.2 * size or amt < 100:
                    skipped_cash += 1
                    continue
                k_ = _cost(amt, P[i, c], False, rank_of(d, sid))
                pos[sid] = [(amt - k_) / I[i, c], i, I[i, c], amt]
                cash -= amt
                c_day += k_
                traded += amt
                tv[i] += amt
        stock = sum(p[0] * I[i, col[s]] for s, p in pos.items())
        nav[i], cost[i] = cash + stock, c_day
        expo[i], npos[i] = (stock / nav[i] if nav[i] > 0 else 0.0), len(pos)
    open_ = [{"sid": s, "entry": perf[p[1]], "exit": pd.NaT, "reason": "open_at_end",
              "ret": I[-1, col[s]] / p[2] - 1, "pnl": p[0] * I[-1, col[s]] - p[3]} for s, p in pos.items()]
    return {"nav": pd.Series(nav, index=perf), "cost": pd.Series(cost, index=perf), "exposure": pd.Series(expo, index=perf),
            "n_positions": pd.Series(npos, index=perf), "traded": traded, "traded_day": pd.Series(tv, index=perf), "trades": pd.DataFrame(trades + open_),
            "skipped_full": skipped_full, "skipped_cash": skipped_cash, "skipped_already_held": skipped_held}


def run_targets(targets: dict, data, rank_of, band: float = REBAL_BAND) -> dict:
    """Equal-weight target portfolio. ``targets``: trade session -> list of security ids (may be empty = cash).
    At each target session: sell names that left, then move every target name to NAV / n (skip moves < band x NAV)."""
    perf = data.sessions[(data.sessions >= pd.Timestamp(START)) & (data.sessions <= pd.Timestamp(END))]
    col = {s: i for i, s in enumerate(data.perf_idx.columns)}
    I = data.perf_idx.reindex(perf).to_numpy()
    P = data.close.reindex(perf).to_numpy()
    lr = data.last_row
    cash = ACCOUNT
    pos = {}                      # sid -> [units, entry_i, cost_basis]
    nav, cost, expo, npos, tv = (np.zeros(len(perf)) for _ in range(5))
    traded, trades, n_rebal = 0.0, [], 0

    def close_spell(sid, i, d, v_out, reason):
        u, ei, basis = pos.pop(sid)
        trades.append({"sid": sid, "entry": perf[ei], "exit": d, "reason": reason, "ret": v_out / basis - 1 if basis else
                       np.nan, "pnl": v_out - basis})

    for i, d in enumerate(perf):
        c_day = 0.0
        for sid in [s for s in pos if pd.notna(lr.get(s)) and d > lr[s]]:
            v = pos[sid][0] * I[i, col[sid]]
            cash += v
            close_spell(sid, i, d, v, "delisted")
        if d in targets:
            n_rebal += 1
            tgt = [s for s in dict.fromkeys(targets[d]) if s in col and np.isfinite(I[i, col[s]])
                   and np.isfinite(P[i, col[s]]) and not (pd.notna(lr.get(s)) and lr[s] < d)]
            for sid in [s for s in pos if s not in tgt]:
                c = col[sid]
                v = pos[sid][0] * I[i, c]
                k_ = _cost(v, P[i, c], True, rank_of(d, sid))
                cash += v - k_
                c_day += k_
                traded += v
                tv[i] += v
                close_spell(sid, i, d, v - k_, "left_target")
            total = cash + sum(p[0] * I[i, col[s]] for s, p in pos.items())
            want = total / len(tgt) if tgt else 0.0
            deltas = {s: want - (pos[s][0] * I[i, col[s]] if s in pos else 0.0) for s in tgt}
            for sid, dv in sorted(deltas.items(), key=lambda x: x[1]):       # sells first
                c = col[sid]
                if abs(dv) < band * total and sid in pos:
                    continue
                if dv < 0:
                    v = -dv
                    k_ = _cost(v, P[i, c], True, rank_of(d, sid))
                    frac = v / (pos[sid][0] * I[i, c])
                    pos[sid][2] *= (1 - frac)
                    pos[sid][0] -= v / I[i, c]
                    cash += v - k_
                else:
                    v = min(dv, cash)
                    if v < 100:
                        continue
                    k_ = _cost(v, P[i, c], False, rank_of(d, sid))
                    if sid in pos:
                        pos[sid][0] += (v - k_) / I[i, c]
                        pos[sid][2] += v
                    else:
                        pos[sid] = [(v - k_) / I[i, c], i, v]
                    cash -= v
                c_day += k_
                traded += v
                tv[i] += v
        stock = sum(p[0] * I[i, col[s]] for s, p in pos.items())
        nav[i], cost[i] = cash + stock, c_day
        expo[i], npos[i] = (stock / nav[i] if nav[i] > 0 else 0.0), len(pos)
    for s in list(pos):
        close_spell(s, len(perf) - 1, pd.NaT, pos[s][0] * I[-1, col[s]], "open_at_end")
    return {"nav": pd.Series(nav, index=perf), "cost": pd.Series(cost, index=perf), "exposure": pd.Series(expo, index=perf),
            "n_positions": pd.Series(npos, index=perf), "traded": traded, "traded_day": pd.Series(tv, index=perf), "trades": pd.DataFrame(trades),
            "n_rebalances": n_rebal}


def benchmark_nav(ticker: str, sessions: pd.DatetimeIndex) -> pd.Series:
    df = qt.parse_chart(BENCH_DIR / f"chart_{ticker}.json", END)
    adj = pd.Series(df["adjclose"].to_numpy(float), index=pd.DatetimeIndex(df["date"])).reindex(sessions).ffill()
    px = pd.Series(df["close"].to_numpy(float), index=pd.DatetimeIndex(df["date"])).reindex(sessions).ffill()
    hs = 2e-4 if ticker == "ONEQ" else 1e-4
    k = rev.order_cost(ACCOUNT / px.iloc[0], px.iloc[0], sell=False, hs=hs)["total"]
    return (ACCOUNT - k) * adj / adj.iloc[0]


# ======================================================================== metrics

def _longest_dd(v: pd.Series) -> int:
    under = (v / v.cummax() - 1) < 0
    best = run = 0
    for u in under.to_numpy():
        run = run + 1 if u else 0
        best = max(best, run)
    return int(best)


def period_metrics(nav: pd.Series, oneq: pd.Series, qqq: pd.Series, rf: pd.Series, start: str, end: str,
                   res: dict | None = None) -> dict:
    """Metrics for sessions in [start, end]; the base is the last close before ``start`` (or the account start)."""
    def seg(s):
        s0 = s[s.index < pd.Timestamp(start)].iloc[-1:]
        return pd.concat([s0, s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]])
    v, b, q = seg(nav), seg(oneq), seg(qqq)
    r, rb, rq = v.pct_change().iloc[1:], b.pct_change().iloc[1:], q.pct_change().iloc[1:]
    f = rf.reindex(r.index).fillna(0.0)
    yrs = len(r) / 252.0
    cagr = (v.iloc[-1] / v.iloc[0]) ** (1 / yrs) - 1
    bc = (b.iloc[-1] / b.iloc[0]) ** (1 / yrs) - 1
    qc = (q.iloc[-1] / q.iloc[0]) ** (1 / yrs) - 1
    dd, bdd, qdd = qt.max_drawdown(v), qt.max_drawdown(b), qt.max_drawdown(q)
    ex = r - f
    down = ex[ex < 0]
    a = r - rb
    m, mb = qt.monthly(r), qt.monthly(rb)
    mx = m - mb
    y, yb = qt.yearly(r), qt.yearly(rb)
    beta = float(np.cov(r, rb, ddof=1)[0, 1] / rb.var(ddof=1))
    alpha = float(((r - f) - beta * (rb - f)).mean() * 252)
    out = {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
           "cagr": cagr, "oneq_cagr": bc, "qqq_cagr": qc, "excess_vs_oneq": cagr - bc, "excess_vs_qqq": cagr - qc,
           "vol": float(r.std(ddof=1) * math.sqrt(252)), "max_dd": dd, "oneq_max_dd": bdd, "qqq_max_dd": qdd,
           "dd_shallower_than_oneq_pp": (abs(bdd) - abs(dd)) * 100,
           "longest_dd_sessions": _longest_dd(v), "oneq_longest_dd_sessions": _longest_dd(b),
           "sharpe": float(ex.mean() / ex.std(ddof=1) * math.sqrt(252)) if ex.std() > 0 else float("nan"),
           "sortino": float(ex.mean() * 252 / (math.sqrt((down ** 2).sum() / len(ex)) * math.sqrt(252)))
           if len(down) else float("nan"),
           "calmar": cagr / abs(dd) if dd < 0 else float("nan"),
           "ir": float(a.mean() / a.std(ddof=1) * math.sqrt(252)) if a.std() > 0 else float("nan"),
           "beta_vs_oneq": beta, "alpha_vs_oneq_ann": alpha,
           "months": int(len(mx)), "monthly_excess_mean": float(mx.mean()),
           "t_monthly_excess_vs_oneq": float(mx.mean() / mx.std(ddof=1) * math.sqrt(len(mx))) if mx.std() > 0 else 0.0,
           "years_beating_oneq": f"{int((y > yb).sum())}/{len(y)}", "share_years_beating_oneq": float((y > yb).mean()),
           "worst_year": float(y.min()), "worst_year_label": int(y.idxmin()), "worst_month": float(m.min()),
           "worst_month_label": str(m.idxmin())}
    if res is not None:
        # the segment whose base is the START close also owns the trades made at that close
        lo = pd.Timestamp(START) if v.index[0] == pd.Timestamp(START) else pd.Timestamp(start)
        mask = (res["nav"].index >= lo) & (res["nav"].index <= pd.Timestamp(end))
        t = res["trades"]
        if len(t):
            t = t[(t["entry"] >= lo) & (t["entry"] <= pd.Timestamp(end))]
        gains, losses = t["pnl"][t["pnl"] > 0].sum() if len(t) else 0, -t["pnl"][t["pnl"] < 0].sum() if len(t) else 0
        navm = res["nav"][mask]
        prev = res["nav"].shift(1).fillna(ACCOUNT)[mask]
        out.update({"trades": int(len(t)), "win_rate": float((t["ret"] > 0).mean()) if len(t) else float("nan"),
                    "profit_factor": float(gains / losses) if losses > 0 else float("nan"),
                    "avg_trade_ret": float(t["ret"].mean()) if len(t) else float("nan"),
                    "cost_drag_per_year": float((res["cost"][mask] / prev).sum() / yrs),
                    "avg_exposure": float(res["exposure"][mask].mean()),
                    "avg_positions": float(res["n_positions"][mask].mean()),
                    "time_in_cash_share": float((res["exposure"][mask] < 0.05).mean())})
        # turnover: one-way traded value / 2 / average NAV per year, on this segment
        out["turnover_per_year"] = float(res["traded_day"][mask].sum()
                                         / 2 / navm.mean() / yrs)
    return out


def judge(halves: list[dict], full: dict) -> dict:
    a = all(h["cagr"] > h["oneq_cagr"] for h in halves) and full["t_monthly_excess_vs_oneq"] >= 2.0
    b = all(h["dd_shallower_than_oneq_pp"] >= 10.0 and h["cagr"] >= h["oneq_cagr"] - 0.03 for h in halves)
    return {"A": bool(a), "B": bool(b), "pass": bool(a or b)}


# ======================================================================== assembly

lv.WINDOWS["sec_alt"] = {"perf_start": START, "perf_end": END, "price_start": "2013-01-01",
                         "universe_start": "2013-01-01", "judged_from": "2014-01-01"}


def load_prices():
    data = lv.load_window("sec_alt")
    assert data.guard["effective_end"] == END, data.guard["effective_end"]
    cs.assert_window(data.sessions, "2013-01-01", END, "panel sessions")
    return data


def insider_entries(data, secmap: SecMap, shares: pd.DataFrame | None = None) -> tuple[dict, dict, pd.DataFrame]:
    trades = load_insider_trades()
    routine = routine_owners(trades)
    q = qualified_purchases(trades, routine)
    q = q[q["filing_date"] >= pd.Timestamp("2013-12-01")]
    stats = {"form4_open_market_rows": int(len(trades)), "qualified_purchase_filings_2014on": int(len(q)),
             "routine_dropped": int(q["routine"].sum())}
    opp = q[~q["routine"]]
    sig1 = opp[opp["value"] >= MIN_PURCHASE][["filing_date", "issuer_cik", "symbol", "value", "accession", "owner_cik"]]
    sig2 = cluster_signals(opp)
    out, counts = {}, {}
    out["I1"], counts["I1"] = attach_entries(sig1, secmap, data.sessions, data.close, data.last_row)
    out["I2"], counts["I2"] = attach_entries(sig2, secmap, data.sessions, data.close, data.last_row)
    if shares is None:
        shares = load_shares()
    e = out["I1"].copy()
    caps = []
    for r in e.itertuples():
        sh = shares_asof(shares, int(r.issuer_cik), r.filing_date)
        px_day = data.sessions[data.sessions <= r.filing_date][-1]
        caps.append(sh * data.close.at[px_day, r.security_id])
    e["mcap"] = caps
    counts["I3"] = {"from_I1_priced": int(len(e)), "no_shares_data": int(e["mcap"].isna().sum()),
                    "mcap_ge_5b": int((e["mcap"] >= MCAP_MAX).sum())}
    out["I3"] = e[e["mcap"] < MCAP_MAX]
    counts["I3"]["priced_signals"] = int(len(out["I3"]))
    return out, {"filters": stats, **counts}, q


def f13_targets(top: pd.DataFrame, sessions: pd.DatetimeIndex, cmap: dict) -> tuple[dict, pd.DataFrame]:
    """Trade session -> target list (union of each manager's latest top 10, priced names only)."""
    top = top.copy()
    top["security_id"] = top["cusip"].map(cmap)
    latest, targets, cov = {}, {}, []
    start = pd.Timestamp(START)
    for acc, g in top.sort_values(["acceptance", "rank"]).groupby("accession", sort=False):
        mgr = g["manager"].iloc[0]
        latest[mgr] = g
        acc_date = pd.Timestamp(g["acceptance"].iloc[0][:10])
        trade = entry_session(sessions, acc_date)
        if trade is None or trade > pd.Timestamp(END):
            continue
        trade = max(trade, start)
        names = []
        for m in sorted(latest):
            names += [s for s in latest[m]["security_id"] if isinstance(s, str)]
        targets[trade] = list(dict.fromkeys(names))
        cov.append({"manager": mgr, "accession": acc, "acceptance": g["acceptance"].iloc[0],
                    "report_date": g["report_date"].iloc[0], "trade_session": str(trade.date()),
                    "top10_priced_n": int(g["security_id"].notna().sum()),
                    "top10_value_share_priced": float(g.loc[g["security_id"].notna(), "weight_in_top10"].sum()),
                    "priced": " ".join(g.loc[g["security_id"].notna(), "name"]),
                    "target_n": len(targets[trade])})
    # sessions before START collapse onto START: keep only the last pre-start target
    return targets, pd.DataFrame(cov)


def fmt_pct(x):
    return f"{x * 100:+.1f}%" if isinstance(x, float) and np.isfinite(x) else str(x)


def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    data = load_prices()
    print("DATE GUARD:", data.guard["assertion"])
    sessions = data.sessions
    perf = sessions[(sessions >= pd.Timestamp(START)) & (sessions <= pd.Timestamp(END))]
    rank_of = rank_lookup(data.universe)
    rf = qt.fill_rf(qt.load_kf_rf(END), qt.load_dtb3(END), perf)
    oneq, qqq = benchmark_nav("ONEQ", perf), benchmark_nav("QQQ", perf)
    secmap = load_secmap(data.perf_idx.columns)

    missing = []
    entries, counts, q = insider_entries(data, secmap)
    res = {k: run_insider(entries[k], data, rank_of) for k in ("I1", "I2", "I3")}
    for k in res:
        counts[k] = {**counts[k], "skipped_book_full": res[k]["skipped_full"],
                     "skipped_already_held": res[k]["skipped_already_held"], "skipped_no_cash": res[k]["skipped_cash"]}
        entries[k].to_csv(OUT / f"signals_{k}.csv", index=False)

    cmap = dict(CUSIP_REVIEW)
    # a reviewed security that never made the weekly top-300 universe after 2013 is not loaded -> unpriced
    missing = sorted({v for v in cmap.values() if v not in data.perf_idx.columns})
    cmap = {c: v for c, v in cmap.items() if v in data.perf_idx.columns}
    f1top, f2top = load_13f(BERKSHIRE), load_13f(F2_MANAGERS)
    u = pd.concat([f1top, f2top]).drop_duplicates("cusip")[["cusip", "name", "title"]]
    u.assign(security_id=u["cusip"].map(cmap)).sort_values("cusip").to_csv(OUT / "cusip_map.csv", index=False)
    t1, cov1 = f13_targets(f1top, sessions, cmap)
    t2, cov2 = f13_targets(f2top, sessions, cmap)
    res["F1"], res["F2"] = run_targets(t1, data, rank_of), run_targets(t2, data, rank_of)
    for name, top, cov in (("F1", f1top, cov1), ("F2", f2top, cov2)):
        top.assign(security_id=top["cusip"].map(cmap)).to_csv(OUT / f"top10_{name}.csv", index=False)
        cov.to_csv(OUT / f"coverage_{name}.csv", index=False)
        counts[name] = {"filings": int(len(cov)), "avg_top10_priced_n": float(cov["top10_priced_n"].mean()),
                        "avg_top10_value_share_priced": float(cov["top10_value_share_priced"].mean()),
                        "filings_with_zero_priced": int((cov["top10_priced_n"] == 0).sum()),
                        "rebalances": res[name]["n_rebalances"]}

    rows, verdict, navs = [], {}, {"ONEQ": oneq, "QQQ": qqq}
    for k, r in res.items():
        full = period_metrics(r["nav"], oneq, qqq, rf, "2014-01-01", END, r)
        hs = [period_metrics(r["nav"], oneq, qqq, rf, a, b, r) for _, a, b in HALVES]
        verdict[k] = judge(hs, full)
        for lab, m in [("full", full)] + [(h[0], x) for h, x in zip(HALVES, hs)]:
            rows.append({"config": k, "segment": lab, **m})
        navs[k] = r["nav"]
        r["trades"].to_csv(OUT / f"trades_{k}.csv", index=False)
    metrics = pd.DataFrame(rows)
    metrics.to_csv(OUT / "metrics.csv", index=False)
    pd.DataFrame(navs).to_csv(OUT / "daily_nav.csv")
    summary = {"pre_registration": str(LEDGER.relative_to(ROOT)), "trials": N_TRIALS,
               "reviewed_cusip_targets_not_loaded": missing,
               "bonferroni_t_one_sided_5pct": NORM.inv_cdf(1 - 0.05 / N_TRIALS), "window": [START, END],
               "halves": HALVES, "verdict": verdict, "coverage_and_counts": counts,
               "terminal_events_in_panel": int(len(data.terminal_events)), "date_guard": data.guard["assertion"]}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    show = ["cagr", "oneq_cagr", "qqq_cagr", "max_dd", "oneq_max_dd", "t_monthly_excess_vs_oneq", "trades"]
    for k in res:
        print(k, verdict[k])
        print(metrics[metrics["config"] == k].set_index("segment")[show].to_string())
    print(json.dumps(counts, indent=1, default=str))
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fetch", action="store_true", help="download / parse the SEC raw files only")
    ap.add_argument("--cusip-candidates", action="store_true", help="write the name-match table for the hand review")
    args = ap.parse_args(argv)
    if args.fetch:
        print("Form 4 rows:", len(load_insider_trades()))
        print("shares rows:", len(load_shares()))
        for mg in (BERKSHIRE, F2_MANAGERS):
            print("13F top-10 rows:", len(load_13f(mg)))
        return
    if args.cusip_candidates:
        data = load_prices()
        top = pd.concat([load_13f(BERKSHIRE), load_13f(F2_MANAGERS)])
        u = top.drop_duplicates("cusip")[["cusip", "name", "title"]]
        c = cusip_candidates(u, data.perf_idx.columns)
        OUT.mkdir(parents=True, exist_ok=True)
        c.to_csv(OUT / "cusip_candidates.csv", index=False)
        print(c.to_string())
        return
    run(args)


if __name__ == "__main__":
    main()
