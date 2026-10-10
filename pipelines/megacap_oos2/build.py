"""Data build for the mega-cap out-of-sample test 1999-2013 without QuantConnect (docs/research_ledger_megacap.md, OOS.2):
the steps that write ``output/research_only/megacap_oos2/`` (moved from scripts/megacap_oos2_data.py, phase 3; the
fetchers, parsers and readers are in quant/data/sources/megacap_oos2.py and are re-exported here, so
``scripts.megacap_oos2_data`` keeps every old name).

  qqq   Nasdaq-100 Trust (QQQ) schedules of investments 1999-2013 from SEC -> qqq_schedule_top45.csv (the candidate
        generator; names are mapped by hand to CIKs in candidates_input.csv).
  sec   SEC submissions index per CIK -> 10-K / 10-Q filed 1998-06 .. 2013-12 -> cover-page facts (sec_cover_facts.csv)
        and the XBRL dei share facts (sec_dei_shares.csv).

SEC requests carry src/io/sec_contact.sec_user_agent() (never printed), at most 7 a second (shared limiter).

Usage:  PYTHONPATH=. .venv/bin/python scripts/megacap_oos2_data.py {qqq|sec} [--dei-only]
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import numpy as np
import pandas as pd

from quant.data.sources import http
from quant.data.sources.megacap_oos2 import *  # noqa: F401,F403  (the old module's names)
from quant.data.sources.megacap_oos2 import (BASE_FORMS, COVER_BYTES, COVER_FACTS, DEI_FACTS, FILINGS_FROM,
                                             FIRST_SIGNAL, HOLDING, LAST_DAY, OUT, QQQ_CIK, QQQ_DOCS, cik_spans,
                                             doc_url, load_candidates, parse_cover_close, parse_cover_shares,
                                             parse_cover_venue, sec_get, submissions, to_text)
from quant.data.sources.megacap_oos2 import _yahoo_csv_span, _tiingo_frame  # noqa: F401  (private names, re-exported)


def qqq_holdings(top: int = 45) -> pd.DataFrame:
    """Every holding line (issuer, shares, value) of each annual schedule; the ``top`` by value per report."""
    rows = []
    for fy, acc, doc in QQQ_DOCS:
        raw = sec_get(doc_url(QQQ_CIK, acc, doc), f"qqq/{acc}_{doc}.gz")
        t = to_text(raw)
        t = re.sub(r"[ \t]+", " ", t)
        # html tables: name / shares / value come on separate lines -> join lines of a row
        t = re.sub(r"\n\s*\n+", "\n", t)
        found = []
        for m in HOLDING.finditer(t):
            name = re.sub(r"[\.\s\*]+$", "", m.group(1)).strip(" .*")
            sh, val = int(m.group(2).replace(",", "")), int(m.group(3).replace(",", ""))
            if val < 1e5 or len(name) < 3 or re.search(r"(?i)total|shares|net assets|investments|cash", name):
                continue
            found.append((fy, name, sh, val))
        if len(found) < 60:   # html layouts: one cell per line
            lines = [x.strip() for x in t.split("\n") if x.strip()]
            for i in range(len(lines) - 2):
                a, b, c = lines[i], lines[i + 1].replace("$", "").strip(), lines[i + 2].replace("$", "").strip()
                if (re.fullmatch(r"[\d,]{3,}", b) and re.fullmatch(r"[\d,]{5,}", c) and re.search(r"[A-Za-z]{3}", a)
                        and not re.search(r"(?i)total|net assets|investments|cash|shares", a)):
                    found.append((fy, a.strip(" .*"), int(b.replace(",", "")), int(c.replace(",", ""))))
        df = pd.DataFrame(found, columns=["fy", "issuer", "shares", "value"]).drop_duplicates(["issuer"])
        df = df.sort_values("value", ascending=False)
        df["rank"] = np.arange(1, len(df) + 1)
        rows.append(df)
        print(f"  QQQ {fy}: {len(df)} holdings parsed; top: {', '.join(df.issuer.head(12))}")
    out = pd.concat(rows, ignore_index=True)
    return out[out["rank"] <= top]


def candidate_filings(cand: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for r in cand.itertuples():
        lo = pd.Timestamp(r.nasdaq_from or FIRST_SIGNAL) - pd.Timedelta(days=430)
        lo = max(lo, pd.Timestamp(FILINGS_FROM))
        hi = min(pd.Timestamp(r.nasdaq_to or LAST_DAY), pd.Timestamp(LAST_DAY))
        for cik, a, b in cik_spans(r.ciks):
            sub = submissions(cik)
            f = sub["filings"]
            f = f[f["form"].isin(BASE_FORMS)].copy()
            f["filingDate"] = pd.to_datetime(f["filingDate"])
            f = f[(f["filingDate"] >= lo) & (f["filingDate"] <= hi)]
            # a CIK that hands over to a successor (Comcast 22301 -> 1166691 in 2002-11; 22301 kept filing as a
            # subsidiary afterwards) is used only inside its span (the first successor filing is allowed 120 days)
            if b:
                f = f[f["filingDate"] <= pd.Timestamp(b) + pd.Timedelta(days=120)]
            if a:
                f = f[f["filingDate"] > pd.Timestamp(a)]
            for x in f.itertuples():
                rows.append({"key": r.key, "cik": cik, "entity": sub["name"], "form": x.form,
                             "filed": x.filingDate.date().isoformat(), "report_date": x.reportDate,
                             "accession": x.accessionNumber, "primary": x.primaryDocument})
    return pd.DataFrame(rows)


def cover_facts(filings: pd.DataFrame) -> pd.DataFrame:
    """Fetch the first COVER_BYTES of every filing and parse its cover. One row per filing."""
    def one(x):
        url = doc_url(x["cik"], x["accession"], x["primary"])
        try:
            raw = sec_get(url, f"covers/{x['cik']}/{x['accession']}.gz", range_bytes=COVER_BYTES)
        except FileNotFoundError:          # the index's primary document is not at that path: the full submission
            url = doc_url(x["cik"], x["accession"], "")
            raw = sec_get(url, f"covers/{x['cik']}/{x['accession']}_full.gz", range_bytes=COVER_BYTES)
        t = to_text(raw)
        sh = parse_cover_shares(t)
        out = {**x, "url": url, "shares": sh["shares"], "asof": sh["asof"].date().isoformat() if sh["asof"] is not None
               else "", "n_numbers": sh["n_numbers"], "multi_class": sh["multi_class"], "sentence": sh["sentence"]}
        if x["form"].startswith("10-K"):
            out.update(parse_cover_venue(t))
            cl = parse_cover_close(t)
            out["cover_close"] = cl["cover_close"]
            out["cover_close_date"] = cl["cover_close_date"].date().isoformat() if cl["cover_close_date"] is not None else ""
        return out
    items = filings.to_dict("records")
    res = http.parallel_map(one, items, workers=14)
    rows = []
    for x, r in zip(items, res):
        rows.append(r if isinstance(r, dict) else {**x, "error": str(r)[:200]})
    return pd.DataFrame(rows)


def companyfacts_dei(cik: int) -> pd.DataFrame:
    """dei EntityCommonStockSharesOutstanding facts (all classes) of one CIK: end (as-of), filed, form, accn, val."""
    try:
        body = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", f"companyfacts/CIK{cik:010d}.json.gz")
    except FileNotFoundError:
        return pd.DataFrame(columns=["cik", "end", "filed", "form", "accn", "val"])
    j = json.loads(body)
    items = j.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    df = pd.DataFrame(items)
    if df.empty:
        return pd.DataFrame(columns=["cik", "end", "filed", "form", "accn", "val"])
    df["cik"] = cik
    return df[["cik", "end", "filed", "form", "accn", "val"]]


def build_sec(args=None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cand = load_candidates()
    fil = candidate_filings(cand)
    print(f"filings to read: {len(fil)} for {fil['key'].nunique()} candidates", flush=True)
    if not (args is not None and getattr(args, "dei_only", False)):
        facts = cover_facts(fil)
        facts.to_csv(COVER_FACTS, index=False)
        print("cover facts written:", len(facts), "errors:", int(facts.get("error", pd.Series(dtype=str)).notna().sum()))
    dei = []
    for r in cand.itertuples():
        for cik, _, _ in cik_spans(r.ciks):
            d = companyfacts_dei(cik)
            print(f"  dei {r.key} {cik}: {len(d)}", flush=True)
            d["key"] = r.key
            dei.append(d)
    dei = pd.concat(dei, ignore_index=True)
    dei = dei[(dei["filed"] >= "2008-06-01") & (dei["filed"] <= LAST_DAY)]
    dei.to_csv(DEI_FACTS, index=False)
    print("dei facts:", len(dei))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=["qqq", "sec"])
    ap.add_argument("--dei-only", action="store_true", help="sec step: only the XBRL dei share facts")
    a = ap.parse_args(argv)
    if a.step == "qqq":
        OUT.mkdir(parents=True, exist_ok=True)
        qqq_holdings(45).to_csv(OUT / "qqq_schedule_top45.csv", index=False)
    if a.step == "sec":
        build_sec(a)


if __name__ == "__main__":
    sys.exit(main())
