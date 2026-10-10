"""Free SEC data for the sec_alt study (docs/research_ledger_sec_alt.md): the throttled, cached SEC GET, the quarterly
"Insider Transactions Data Sets" (Form 4 open-market trades), XBRL frames of dei:EntityCommonStockSharesOutstanding,
EDGAR 13F-HR information tables (top 10 by value per filing), the issuer -> panel security map and the name
matching used for the hand review of 13F CUSIPs.

Moved unchanged from studies/sec_alt.py. Every cache / raw folder, the quarter lists and the window end are
parameters (the study passes research_cache/sec_alt/...). The SEC User-Agent comes from
quant.data.sources.sec.sec_user_agent (src/io/sec_contact.py) and is never printed; requests are throttled to
``max_per_second`` (3 by default) across the process.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.guards import assert_window
from quant.data.sources.sec import sec_user_agent

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


def fetch_insider_zips(quarters, raw_dir: Path) -> list[Path]:
    paths = []
    for q in quarters:
        dest = raw_dir / f"{q}_form345.zip"
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


def load_insider_trades(cache: Path, quarters, raw_dir: Path, last_filing: str, refresh: bool = False) -> pd.DataFrame:
    """Every quarter's open-market Form 4 trades, cached in ``cache``/processed (filing dates asserted <=
    ``last_filing``)."""
    dest = cache / "processed/insider_open_market_trades.csv.gz"
    if dest.is_file() and not refresh:
        df = pd.read_csv(dest, dtype={"symbol": str})
        for c in ("filing_date", "trans_date"):
            df[c] = pd.to_datetime(df[c], errors="coerce")
        df = df.dropna(subset=["filing_date", "trans_date"])
        assert_window(df["filing_date"], None, last_filing, "Form 4 filing dates")
        return df
    parts = [parse_insider_zip(p) for p in fetch_insider_zips(quarters, raw_dir)]
    df = pd.concat(parts, ignore_index=True).drop_duplicates()
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dest, index=False, compression="gzip")
    return df


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


def load_secmap(panel_ids, inputs: Path) -> SecMap:
    sm = pd.read_csv(inputs / "security_master.csv", dtype=str, usecols=["security_id", "cik"])
    sm = sm[sm["security_id"].isin(set(panel_ids))]
    by = {}
    for r in sm.itertuples():
        if isinstance(r.cik, str) and r.cik.strip():
            by.setdefault(int(r.cik), []).append(r.security_id)
    iv = pd.read_csv(inputs / "ticker_intervals.csv", dtype=str, usecols=["security_id", "ticker", "start", "end"])
    iv["start"], iv["end"] = pd.to_datetime(iv["start"]), pd.to_datetime(iv["end"])
    return SecMap(by_cik={k: sorted(set(v)) for k, v in by.items()}, intervals=iv)


# ======================================================================== shares outstanding

def load_shares(quarters, raw_dir: Path, lag_days: int) -> pd.DataFrame:
    """dei:EntityCommonStockSharesOutstanding from the XBRL frames of ``quarters``; usable ``lag_days`` after the
    period end."""
    rows = []
    for q in quarters:
        url = f"https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/{q}.json"
        try:
            j = json.loads(sec_get(url, raw_dir / f"{q}.json"))
        except urllib.error.HTTPError:
            continue
        for d in j["data"]:
            rows.append((int(d["cik"]), d["end"], float(d["val"])))
    df = pd.DataFrame(rows, columns=["cik", "end", "shares"])
    df["end"] = pd.to_datetime(df["end"])
    df["usable_from"] = df["end"] + pd.Timedelta(days=lag_days)
    return df[df["shares"] > 0].sort_values(["cik", "usable_from"]).reset_index(drop=True)


def shares_asof(shares: pd.DataFrame, cik: int, date: pd.Timestamp) -> float:
    s = shares[(shares["cik"] == cik) & (shares["usable_from"] <= date)]
    return float(s["shares"].iloc[-1]) if len(s) else float("nan")


# ======================================================================== 13F

def manager_filings(ciks: list[str], raw_dir: Path, end: str) -> pd.DataFrame:
    rows = []
    for cik in ciks:
        j = json.loads(sec_get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                               raw_dir / f"submissions_{int(cik)}.json"))
        blocks = [j["filings"]["recent"]]
        for f in j["filings"].get("files", []):
            blocks.append(json.loads(sec_get(f"https://data.sec.gov/submissions/{f['name']}", raw_dir / f["name"])))
        for b in blocks:
            for k in range(len(b["form"])):
                if b["form"][k] == "13F-HR":
                    rows.append({"cik": int(cik), "accession": b["accessionNumber"][k], "filing_date": b["filingDate"][k],
                                 "acceptance": b["acceptanceDateTime"][k], "report_date": b["reportDate"][k]})
    df = pd.DataFrame(rows).drop_duplicates("accession")
    df["acc_date"] = pd.to_datetime(df["acceptance"].str[:10])
    df = df[(df["acc_date"] >= pd.Timestamp("2013-06-01")) & (df["acc_date"] <= pd.Timestamp(end))]
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


def filing_holdings(cik: int, accession: str, raw_dir: Path) -> pd.DataFrame:
    nod = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{nod}"
    idx = json.loads(sec_get(f"{base}/index.json", raw_dir / f"{cik}_{nod}_index.json"))
    names = [i["name"] for i in idx["directory"]["item"]]
    xmls = [n for n in names if n.lower().endswith(".xml") and n.lower() != "primary_doc.xml"]
    for n in xmls:
        df = parse_infotable(sec_get(f"{base}/{n}", raw_dir / f"{cik}_{nod}_{n}"))
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


def load_13f(managers: dict, raw_dir: Path, end: str) -> pd.DataFrame:
    """Rows: manager, accession, acceptance, report_date, rank, cusip, name, title, value, weight_in_top10."""
    rows = []
    for mgr, ciks in managers.items():
        for f in manager_filings(ciks, raw_dir, end).itertuples():
            t = top10(filing_holdings(f.cik, f.accession, raw_dir))
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


def cusip_candidates(cusips: pd.DataFrame, panel_ids, inputs: Path) -> pd.DataFrame:
    """Name-match each 13F CUSIP to panel securities (input to the hand review of the study's CUSIP map)."""
    sm = pd.read_csv(inputs / "security_master.csv", dtype=str)
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
