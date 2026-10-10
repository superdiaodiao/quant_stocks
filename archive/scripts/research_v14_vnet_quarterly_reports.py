#!/usr/bin/env python3
"""Recover VNET 2017Q4-2020Q3 from contemporaneous SEC exhibits."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup


DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/vnet_quarterly_reports_2017q4_2020q3"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1508475"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

PERIOD_EVIDENCE = {
    "2017-12-31": ("2018-05-18", "0001193125-18-167099", "d581938dex991.htm", "765,814", "797,569", 765_814_000, 797_569_000),
    "2018-03-31": ("2018-05-18", "0001193125-18-167099", "d581938dex991.htm", "800,765", "34,731", 800_765_000, 34_731_000),
    "2018-06-30": ("2018-08-20", "0001193125-18-252245", "d578837dex991.htm", "828,317", "(95,531)", 828_317_000, -95_531_000),
    "2018-09-30": ("2018-11-23", "0001193125-18-332755", "d657607dex991.htm", "870,068", "(27,894)", 870_068_000, -27_894_000),
    "2018-12-31": ("2019-05-17", "0001193125-19-149871", "d686207dex991.htm", "901,887", "(98,042)", 901_887_000, -98_042_000),
    "2019-03-31": ("2019-05-17", "0001193125-19-149871", "d686207dex991.htm", "871,859", "6,582", 871_859_000, 6_582_000),
    "2019-06-30": ("2019-08-20", "0001193125-19-224751", "d776514dex991.htm", "888,020", "(99,275)", 888_020_000, -99_275_000),
    "2019-09-30": ("2019-11-19", "0001193125-19-295088", "d105252dex991.htm", "980,969", "(66,299)", 980_969_000, -66_299_000),
    "2019-12-31": ("2020-05-15", "0001104659-20-062263", "a20-19887_1ex99d1.htm", "1,048,119", "(22,254)", 1_048_119_000, -22_254_000),
    "2020-03-31": ("2020-05-15", "0001104659-20-062263", "a20-19887_1ex99d1.htm", "1,090,797", "(137,522)", 1_090_797_000, -137_522_000),
    "2020-06-30": ("2020-08-24", "0001104659-20-097989", "a20-28782_4ex99d1.htm", "1,144,061", "(1,648,777)", 1_144_061_000, -1_648_777_000),
    "2020-09-30": ("2020-11-24", "0001104659-20-129059", "a20-37030_1ex99d1.htm", "1,245,794", "99,771", 1_245_794_000, 99_771_000),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_statement(raw: bytes, fiscal_end: str, revenue_text: str,
                       income_text: str) -> None:
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    # SEC table cells often render a closing parenthesis as a separate cell,
    # producing values such as ``(116,837 )`` after text extraction.
    text = re.sub(r"\(([-\d,]+)\s+\)", r"(\1)", text)
    if "21VIANET GROUP, INC." not in text.upper():
        raise ValueError("VNET issuer identity is not proven")
    end = pd.Timestamp(fiscal_end)
    if end.strftime("%B %-d").lower() not in text.lower():
        raise ValueError("VNET requested quarter is not proven")
    if not re.search(r"Renminbi|\bRMB\b", text, re.I):
        raise ValueError("VNET RMB reporting currency is not proven")
    preceding_values = r"(?:\(?-?[\d,]+\)?\s+){0,3}"
    revenue_matches = list(re.finditer(
        rf"(?:Total )?Net revenues\s+{preceding_values}"
        rf"{re.escape(revenue_text)}(?![\d,])",
        text, re.I,
    ))
    if fiscal_end == "2020-06-30" and not revenue_matches:
        h1_revenue = 2_234_858
        q1_revenue = 1_090_797
        h1_income = -1_786_299
        q1_income = -137_522
        requested_revenue = int(revenue_text.replace(",", ""))
        requested_income = -int(income_text.strip("()").replace(",", ""))
        h1_revenue_proven = re.search(
            r"Net revenues(?: Hosting and related services)?\s+"
            r"(?:\(?-?[\d,]+\)?\s+){0,3}"
            r"2,234,858(?![\d,])",
            text, re.I,
        )
        h1_income_proven = re.search(
            r"\bNet loss\s+(?:\(?-?[\d,]+\)?\s+){0,3}"
            r"\(1,786,299\)(?![\d,])",
            text, re.I,
        )
        if (h1_revenue_proven and h1_income_proven
                and h1_revenue - q1_revenue == requested_revenue
                and h1_income - q1_income == requested_income):
            return
    proven = False
    for match in revenue_matches:
        statement = text[match.start():match.start() + 5500]
        if "Cost of revenues" not in statement:
            continue
        if re.search(
            rf"\bNet (?:gain \(loss\)|\(loss\) profit|loss|profit)\s+"
            rf"{preceding_values}{re.escape(income_text)}(?![\d,])",
            statement, re.I,
        ):
            proven = True
            break
    if not revenue_matches:
        raise ValueError("VNET total net revenues value is not proven")
    if not proven:
        raise ValueError("VNET consolidated GAAP net gain/loss is not proven")


def _row(*, fiscal_end: str, available_date: str, metric: str, value: float,
         accession: str, archive: str, archive_sha256: str,
         derivation: str = "direct_contemporaneous_sec_gaap_quarterly_statement") -> dict:
    return {
        "ticker": "VNET", "fiscal_end": fiscal_end,
        "available_date": available_date, "metric": metric,
        "value": float(value), "taxonomy": "us-gaap",
        "concept": ("Revenues" if metric == "revenue" else "ProfitLoss"),
        "form": "6-K", "accession": accession, "unit": "CNY",
        "source": "sec_vnet_contemporaneous_gaap_statement",
        "source_archive": archive, "source_archive_sha256": archive_sha256,
        "derivation": derivation,
    }


def run(*, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    downloaded: dict[tuple[str, str], tuple[Path, str]] = {}
    rows, sources = [], []
    for fiscal_end, item in PERIOD_EVIDENCE.items():
        filed, accession, document, revenue_text, income_text, revenue, income = item
        key = (accession, document)
        compact = accession.replace("-", "")
        url = f"{SEC_BASE}/{compact}/{document}"
        if key not in downloaded:
            with urlopen(Request(url, headers=HEADERS), timeout=90) as response:
                raw = response.read()
            path = raw_dir / f"{accession}_{document}"
            path.write_bytes(raw)
            downloaded[key] = (path, _sha256(path))
            sources.append({"accession": accession, "document": document,
                            "url": url, "path": str(path),
                            "sha256": downloaded[key][1]})
        path, sha = downloaded[key]
        validate_statement(path.read_bytes(), fiscal_end, revenue_text, income_text)
        derivation = (
            "sec_six_month_cumulative_less_independently_proven_sec_q1"
            if fiscal_end == "2020-06-30"
            else "direct_contemporaneous_sec_gaap_quarterly_statement"
        )
        rows.extend([
            _row(fiscal_end=fiscal_end, available_date=filed, metric="revenue",
                 value=revenue, accession=accession, archive=path.name,
                 archive_sha256=sha, derivation=derivation),
            _row(fiscal_end=fiscal_end, available_date=filed, metric="net_income",
                 value=income, accession=accession, archive=path.name,
                 archive_sha256=sha, derivation=derivation),
        ])
    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"]).reset_index(drop=True)
    if len(facts) != 24 or facts["fiscal_end"].nunique() != 12:
        raise RuntimeError("VNET recovery is not exactly twelve paired quarters")
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    paired = facts.pivot_table(index=["fiscal_end", "available_date"],
                               columns="metric", values="value",
                               aggfunc="first").reset_index()
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "VNET", "accepted_quarter_count": 12,
        "recovered_quarters": [
            {"ticker": "VNET", "fiscal_end": str(row.fiscal_end),
             "available_date": str(row.available_date),
             "revenue": float(row.revenue),
             "net_income": float(row.net_income)}
            for row in paired.itertuples(index=False)
        ],
        "filing_sources": sources,
        "outputs": {"quarters": {"path": str(facts_path),
                                  "sha256": _sha256(facts_path)}},
        "guardrail": (
            "Revenue is consolidated GAAP total net revenues in CNY. Net income "
            "is consolidated net gain/loss, not ordinary-shareholder income or "
            "an adjusted measure. 2020Q2 is derived from the precise SEC-filed "
            "2020H1 cumulative statement less the independently proven SEC Q1 "
            "values, rather than rounded release figures. Each comparative "
            "quarter keeps its actual SEC availability."
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
    print(json.dumps({"accepted_quarter_count": result["accepted_quarter_count"],
                      "manifest": result["manifest"],
                      "release_status": result["release_status"]},
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
