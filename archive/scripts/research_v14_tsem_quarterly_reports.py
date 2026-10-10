#!/usr/bin/env python3
"""Recover TSEM 2017Q1-2021Q3 from contemporaneous SEC 6-K statements."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import numbers
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup


DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/tsem_quarterly_reports_2017q1_2021q3"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/928876"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

# Values are in thousands of USD and are direct three-month statement values.
PERIOD_EVIDENCE = {
    "2017-03-31": ("2017-05-08", "0001178913-17-001302", "exhibit_99-1.htm", 330_080, 45_509),
    "2017-06-30": ("2017-08-03", "0001178913-17-002247", "exhibit_99-1.htm", 345_059, 50_017),
    "2017-09-30": ("2017-11-07", "0001178913-17-003044", "exhibit_99-1.htm", 354_557, 55_274),
    "2017-12-31": ("2019-02-19", "0001178913-19-000602", "exhibit_99-1.htm", 357_614, 147_211),
    "2018-03-31": ("2018-05-07", "0001178913-18-001465", "exhibit_99-1.htm", 312_710, 26_118),
    "2018-06-30": ("2018-07-31", "0001178913-18-002201", "exhibit_99-1.htm", 335_138, 36_009),
    "2018-09-30": ("2018-10-29", "0001178913-18-002724", "exhibit_99-1.htm", 322_596, 33_646),
    "2018-12-31": ("2019-02-19", "0001178913-19-000602", "exhibit_99-1.htm", 333_590, 38_073),
    "2019-03-31": ("2019-05-15", "0001178913-19-001476", "exhibit_99-1.htm", 310_107, 26_216),
    "2019-06-30": ("2019-07-30", "0001178913-19-001992", "exhibit_99-1.htm", 306_064, 19_722),
    "2019-09-30": ("2019-11-13", "0001178913-19-002701", "exhibit_99-1.htm", 312_122, 22_188),
    "2019-12-31": ("2020-02-18", "0001178913-20-000483", "exhibit_99-1.htm", 305_710, 20_707),
    "2020-03-31": ("2020-05-13", "0001178913-20-001446", "exhibit_99-1.htm", 300_171, 17_020),
    "2020-06-30": ("2020-08-05", "0001178913-20-002273", "exhibit_99-1.htm", 310_090, 21_474),
    "2020-09-30": ("2020-11-12", "0001178913-20-003109", "exhihit_99-1.htm", 310_212, 15_726),
    "2020-12-31": ("2021-02-17", "0001178913-21-000642", "exhibit_99-1.htm", 345_211, 30_058),
    "2021-03-31": ("2021-05-12", "0001178913-21-001705", "exhibit_99-1.htm", 347_214, 30_514),
    "2021-06-30": ("2021-08-09", "0001178913-21-002610", "exhibit_99-1.htm", 362_138, 31_317),
    "2021-09-30": ("2021-11-08", "0001178913-21-003406", "exhibit_99-1.htm", 386_706, 40_367),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _accounting_values(row) -> list[int]:
    values = []
    for cell in list(row)[1:]:
        if isinstance(cell, numbers.Real) and not pd.isna(cell):
            value = int(cell)
            if not values or value != values[-1]:
                values.append(value)
            continue
        token = str(cell).strip().replace("$", "").replace(" ", "")
        if token.lower() == "nan" or not re.search(r"\d", token):
            continue
        negative = token.startswith("(") or token.startswith("-")
        digits = re.sub(r"[^\d]", "", token)
        if not digits:
            continue
        value = -int(digits) if negative else int(digits)
        if not values or value != values[-1]:
            values.append(value)
    return values


def validate_statement(raw: bytes, fiscal_end: str, revenue: int,
                       net_profit: int) -> None:
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    if not re.search(r"Tower Semiconductor", text, re.I):
        raise ValueError("TSEM issuer identity is not proven")
    end = pd.Timestamp(fiscal_end)
    if end.strftime("%B %d, %Y").replace(" 0", " ").lower() not in text.lower():
        raise ValueError("TSEM requested quarter is not proven")
    if not re.search(r"dollars(?: and share count)? in thousands", text, re.I):
        raise ValueError("TSEM USD-thousands reporting scale is not proven")
    if "threemonth" not in re.sub(r"\s+", "", text.lower()):
        raise ValueError("TSEM three-month reporting period is not proven")
    for table in pd.read_html(io.BytesIO(raw)):
        labels = table.iloc[:, 0].astype(str).str.strip().str.upper()
        revenue_rows = table.loc[labels.eq("REVENUES")]
        profit_rows = table.loc[labels.eq("NET PROFIT")]
        if revenue_rows.empty or profit_rows.empty:
            continue
        revenues = _accounting_values(revenue_rows.iloc[0])
        profits = _accounting_values(profit_rows.iloc[0])
        if revenue in revenues and net_profit in profits:
            return
    raise ValueError("TSEM direct quarterly revenues/net profit is not proven")


def _row(*, fiscal_end: str, available_date: str, metric: str, value: int,
         accession: str, archive: str, archive_sha256: str) -> dict:
    return {
        "ticker": "TSEM", "fiscal_end": fiscal_end,
        "available_date": available_date, "metric": metric,
        "value": float(value * 1000), "taxonomy": "us-gaap",
        "concept": "Revenues" if metric == "revenue" else "NetIncomeLoss",
        "form": "6-K", "accession": accession, "unit": "USD",
        "source": "sec_tsem_contemporaneous_quarterly_statement",
        "source_archive": archive, "source_archive_sha256": archive_sha256,
        "derivation": "direct_sec_three_month_statement",
    }


def run(*, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    downloaded, rows, sources = {}, [], []
    for fiscal_end, item in PERIOD_EVIDENCE.items():
        filed, accession, document, revenue, net_profit = item
        key = (accession, document)
        compact = accession.replace("-", "")
        url = f"{SEC_BASE}/{compact}/{document}"
        if key not in downloaded:
            path = raw_dir / f"{accession}_{document}"
            if not path.exists():
                with urlopen(Request(url, headers=HEADERS), timeout=90) as response:
                    path.write_bytes(response.read())
            downloaded[key] = (path, _sha256(path))
            sources.append({
                "filed": filed, "accession": accession,
                "document": document, "url": url, "path": str(path),
                "sha256": downloaded[key][1],
            })
        path, sha = downloaded[key]
        validate_statement(path.read_bytes(), fiscal_end, revenue, net_profit)
        rows.extend([
            _row(fiscal_end=fiscal_end, available_date=filed, metric="revenue",
                 value=revenue, accession=accession, archive=path.name,
                 archive_sha256=sha),
            _row(fiscal_end=fiscal_end, available_date=filed,
                 metric="net_income", value=net_profit, accession=accession,
                 archive=path.name, archive_sha256=sha),
        ])
    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    if len(facts) != 38 or facts["fiscal_end"].nunique() != 19:
        raise RuntimeError("TSEM recovery is not exactly nineteen paired quarters")
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    recovered = [
        {
            "ticker": "TSEM", "fiscal_end": fiscal_end,
            "available_date": item[0], "revenue": float(item[3] * 1000),
            "net_income": float(item[4] * 1000),
        }
        for fiscal_end, item in PERIOD_EVIDENCE.items()
    ]
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "TSEM", "accepted_quarter_count": 19,
        "recovered_quarters": recovered, "filing_sources": sources,
        "outputs": {"quarters": {"path": str(facts_path),
                                   "sha256": _sha256(facts_path)}},
        "guardrail": (
            "Every row is a direct consolidated three-month value from an SEC "
            "6-K: revenues and GAAP net profit, not adjusted profit or profit "
            "attributable to the company. 2017Q4 is taken directly from the "
            "2019-02-19 comparative quarterly table, not allocated from an "
            "annual total. Actual SEC filing dates control PIT availability."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(output_dir=args.output_dir)
    print(json.dumps({
        "accepted_quarter_count": result["accepted_quarter_count"],
        "manifest": result["manifest"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
