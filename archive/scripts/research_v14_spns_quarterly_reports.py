#!/usr/bin/env python3
"""Recover SPNS 2017Q1-2021Q3 from contemporaneous SEC 6-K statements."""

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
    "output/research_only/v14/spns_quarterly_reports_2017q1_2021q3"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/885740"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

# Values are direct three-month GAAP values in thousands of USD.  Later
# releases also show six-/nine-/twelve-month columns; the first accounting
# value is the requested current quarter.
PERIOD_EVIDENCE = {
    "2017-03-31": (
        "2017-05-15", "0001144204-17-027010",
        "v467002_ex99-1.htm", 56_534, -2_242,
    ),
    "2017-06-30": (
        "2017-08-07", "0001144204-17-040677",
        "v472541_ex99-1.htm", 69_049, -3_586,
    ),
    "2017-09-30": (
        "2017-11-09", "0001144204-17-057404",
        "tv479064_ex99-1.htm", 72_011, 2_951,
    ),
    "2017-12-31": (
        "2018-03-08", "0001144204-18-013490",
        "tv488052_ex99-1.htm", 71_600, 3_433,
    ),
    "2018-03-31": (
        "2018-05-07", "0001144204-18-025504",
        "tv493218_ex99-1.htm", 70_995, 2_828,
    ),
    "2018-06-30": (
        "2018-08-07", "0001213900-18-010322",
        "f6k0818ex99-1_sapiensinter.htm", 72_164, 2_129,
    ),
    "2018-09-30": (
        "2018-11-07", "0001213900-18-015014",
        "f6k110718ex99-1_sapiensinter.htm", 73_237, 5_211,
    ),
    "2018-12-31": (
        "2019-02-26", "0001213900-19-003120",
        "f6k022519cex99-1_sapiens.htm", 73_311, 3_832,
    ),
    "2019-03-31": (
        "2019-05-06", "0001213900-19-007840",
        "f6k050619ex99-1_sapiens.htm", 76_787, 5_176,
    ),
    "2019-06-30": (
        "2019-08-05", "0001213900-19-014589",
        "f6k080619ex99-1_sapiensinter.htm", 79_529, 6_866,
    ),
    "2019-09-30": (
        "2019-11-04", "0001213900-19-021891",
        "f6k110419ex99-1_sapiens.htm", 82_643, 7_505,
    ),
    "2019-12-31": (
        "2020-02-24", "0001213900-20-004561",
        "f6k022420ex99-1_sapiens.htm", 86_715, 6_944,
    ),
    "2020-03-31": (
        "2020-05-14", "0001213900-20-012086",
        "ea121706ex99-1_sapiens.htm", 90_534, 6_890,
    ),
    "2020-06-30": (
        "2020-08-04", "0001213900-20-020014",
        "ea124920ex99-1_sapiens.htm", 93_063, 9_330,
    ),
    "2020-09-30": (
        "2020-11-05", "0001213900-20-035145",
        "ea129334ex99-1_sapiensinter.htm", 97_645, 9_535,
    ),
    "2020-12-31": (
        "2021-02-25", "0001213900-21-011481",
        "ea136384ex99-1_sapiensinter.htm", 101_661, 8_402,
    ),
    "2021-03-31": (
        "2021-05-04", "0001213900-21-024397",
        "ea140292ex99-1_sapiensinter.htm", 109_592, 9_902,
    ),
    "2021-06-30": (
        "2021-08-04", "0001213900-21-040202",
        "ea145166ex99-1_sapiens.htm", 114_406, 10_417,
    ),
    "2021-09-30": (
        "2021-11-03", "0001213900-21-056324",
        "ea149839ex99-1_sapiensinter.htm", 117_812, 13_366,
    ),
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


def _statement_heading(table) -> str:
    for node in table.find_all_previous(["p", "div"], limit=12):
        text = " ".join(node.get_text(" ").split())
        normalized = text.upper()
        if "STATEMENT" in normalized and "INCOME" in normalized:
            return text
    return ""


def validate_statement(
    raw: bytes,
    fiscal_end: str,
    revenue: int,
    net_income: int,
) -> None:
    soup = BeautifulSoup(raw, "html.parser")
    text = " ".join(soup.get_text(" ").split())
    if not re.search(r"Sapiens International Corporation", text, re.I):
        raise ValueError("SPNS issuer identity is not proven")
    end = pd.Timestamp(fiscal_end)
    period = end.strftime("%B %d, %Y").replace(" 0", " ")
    if period.lower() not in text.lower():
        raise ValueError("SPNS requested quarter is not proven")
    if not re.search(r"U\.S\. dollars in thousands", text, re.I):
        raise ValueError("SPNS USD-thousands reporting scale is not proven")
    if "threemonthsended" not in re.sub(r"\s+", "", text.lower()):
        raise ValueError("SPNS direct three-month reporting period is not proven")

    for table in soup.find_all("table"):
        heading = _statement_heading(table).upper()
        if (
            "CONSOLIDATED" not in heading
            or "INCOME" not in heading
            or "NON-GAAP" in heading
        ):
            continue
        frames = pd.read_html(io.StringIO(str(table)))
        if not frames:
            continue
        frame = frames[0]
        labels = frame.iloc[:, 0].astype(str).str.strip()
        revenue_rows = frame.loc[
            labels.str.fullmatch(r"Revenues?", case=False, na=False)
        ]
        income_rows = frame.loc[
            labels.str.fullmatch(
                r"Net income(?: \(loss\))?", case=False, na=False
            )
        ]
        if revenue_rows.empty or income_rows.empty:
            continue
        revenues = _accounting_values(revenue_rows.iloc[0])
        incomes = _accounting_values(income_rows.iloc[0])
        if revenues and incomes and revenues[0] == revenue and incomes[0] == net_income:
            return
    raise ValueError("SPNS direct consolidated GAAP revenue/net income is not proven")


def _row(
    *,
    fiscal_end: str,
    available_date: str,
    metric: str,
    value: int,
    accession: str,
    archive: str,
    archive_sha256: str,
) -> dict:
    return {
        "ticker": "SPNS",
        "fiscal_end": fiscal_end,
        "available_date": available_date,
        "metric": metric,
        "value": float(value * 1000),
        "taxonomy": "us-gaap",
        "concept": "Revenues" if metric == "revenue" else "NetIncomeLoss",
        "form": "6-K",
        "accession": accession,
        "unit": "USD",
        "source": "sec_spns_contemporaneous_quarterly_statement",
        "source_archive": archive,
        "source_archive_sha256": archive_sha256,
        "derivation": "direct_sec_three_month_gaap_statement",
    }


def run(*, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    rows, sources = [], []
    for fiscal_end, item in PERIOD_EVIDENCE.items():
        filed, accession, document, revenue, net_income = item
        compact = accession.replace("-", "")
        url = f"{SEC_BASE}/{compact}/{document}"
        path = raw_dir / f"{accession}_{document}"
        if not path.exists():
            with urlopen(Request(url, headers=HEADERS), timeout=90) as response:
                path.write_bytes(response.read())
        sha = _sha256(path)
        validate_statement(path.read_bytes(), fiscal_end, revenue, net_income)
        sources.append({
            "fiscal_end": fiscal_end,
            "filed": filed,
            "accession": accession,
            "document": document,
            "url": url,
            "path": str(path),
            "sha256": sha,
        })
        rows.extend([
            _row(
                fiscal_end=fiscal_end,
                available_date=filed,
                metric="revenue",
                value=revenue,
                accession=accession,
                archive=path.name,
                archive_sha256=sha,
            ),
            _row(
                fiscal_end=fiscal_end,
                available_date=filed,
                metric="net_income",
                value=net_income,
                accession=accession,
                archive=path.name,
                archive_sha256=sha,
            ),
        ])

    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    if len(facts) != 38 or facts["fiscal_end"].nunique() != 19:
        raise RuntimeError("SPNS recovery is not exactly nineteen paired quarters")
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "SPNS",
        "accepted_quarter_count": 19,
        "recovered_quarters": [
            {
                "ticker": "SPNS",
                "fiscal_end": fiscal_end,
                "available_date": item[0],
                "revenue": float(item[3] * 1000),
                "net_income": float(item[4] * 1000),
            }
            for fiscal_end, item in PERIOD_EVIDENCE.items()
        ],
        "filing_sources": sources,
        "outputs": {
            "quarters": {"path": str(facts_path), "sha256": _sha256(facts_path)}
        },
        "guardrail": (
            "Every row is the first/current-period value in an SEC 6-K "
            "condensed consolidated GAAP statement of income. Non-GAAP "
            "statements, net income attributable to shareholders, summary "
            "rounded values, and year-to-date values are rejected. Actual "
            "SEC filing dates control point-in-time availability."
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
