#!/usr/bin/env python3
"""Recover OKTA 2016Q1-2018Q4 legacy-GAAP quarterly revenue and net loss."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup


DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/okta_legacy_quarterly_reports_2016q1_2018q4"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1660134"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

# Values are in thousands of USD. Older quarters are direct entries in the
# quarterly-results table of the 2018 10-K, not annual allocations.
PERIOD_EVIDENCE = {
    "2016-04-30": ("2018-03-12", "10-K", "0001660134-18-000007", "okta-1312018_10k.htm", 31_787, -22_753),
    "2016-07-31": ("2018-03-12", "10-K", "0001660134-18-000007", "okta-1312018_10k.htm", 37_436, -20_601),
    "2016-10-31": ("2018-03-12", "10-K", "0001660134-18-000007", "okta-1312018_10k.htm", 42_283, -21_931),
    "2017-01-31": ("2018-03-12", "10-K", "0001660134-18-000007", "okta-1312018_10k.htm", 48_820, -18_224),
    "2017-04-30": ("2018-03-12", "10-K", "0001660134-18-000007", "okta-1312018_10k.htm", 53_007, -28_901),
    "2017-07-31": ("2018-03-12", "10-K", "0001660134-18-000007", "okta-1312018_10k.htm", 60_995, -27_002),
    "2017-10-31": ("2018-03-12", "10-K", "0001660134-18-000007", "okta-1312018_10k.htm", 68_238, -33_778),
    "2018-01-31": ("2018-03-12", "10-K", "0001660134-18-000007", "okta-1312018_10k.htm", 77_750, -24_678),
    "2018-04-30": ("2018-06-08", "10-Q", "0001660134-18-000013", "okta-4302018_10q.htm", 83_621, -25_962),
    "2018-07-31": ("2018-09-07", "10-Q", "0001660134-18-000020", "okta-7312018_10q.htm", 94_586, -39_207),
    "2018-10-31": ("2018-12-06", "10-Q", "0001660134-18-000023", "okta-10312018_10q.htm", 105_576, -29_517),
    "2019-01-31": ("2019-03-14", "10-K", "0001660134-19-000006", "okta-1312019_10k.htm", 115_471, -30_811),
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
        token = str(cell).strip().replace("$", "").replace(" ", "")
        if token.lower() == "nan" or not re.search(r"\d", token):
            continue
        negative = token.startswith("(") or token.startswith("-")
        digits = re.sub(r"[^\d]", "", token)
        if not digits:
            continue
        value = int(digits)
        value = -value if negative else value
        if not values or value != values[-1]:
            values.append(value)
    return values


def validate_statement(raw: bytes, revenue: int, net_income: int) -> None:
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    if not re.search(r"Okta,? Inc", text, re.I):
        raise ValueError("OKTA issuer identity is not proven")
    if not re.search(r"(?:in thousands|amounts in thousands)", text, re.I):
        raise ValueError("OKTA USD-thousands reporting scale is not proven")
    for table in pd.read_html(io.BytesIO(raw)):
        flattened = " ".join(map(str, table.values.ravel())).lower()
        if "three months" not in flattened:
            continue
        labels = table.iloc[:, 0].astype(str).str.strip()
        revenue_rows = table.loc[labels.eq("Total revenue")]
        net_rows = table.loc[labels.eq("Net loss")]
        if revenue_rows.empty or net_rows.empty:
            continue
        revenues = _accounting_values(revenue_rows.iloc[0])
        losses = _accounting_values(net_rows.iloc[0])
        if revenue in revenues and net_income in losses:
            return
    raise ValueError("OKTA direct quarterly total revenue/net loss is not proven")


def _row(*, fiscal_end: str, available_date: str, metric: str, value: int,
         form: str, accession: str, archive: str, archive_sha256: str) -> dict:
    return {
        "ticker": "OKTA", "fiscal_end": fiscal_end,
        "available_date": available_date, "metric": metric,
        "value": float(value * 1000), "taxonomy": "us-gaap",
        "concept": "Revenues" if metric == "revenue" else "NetIncomeLoss",
        "form": form, "accession": accession, "unit": "USD",
        "source": "sec_okta_legacy_gaap_quarterly_statement",
        "source_archive": archive, "source_archive_sha256": archive_sha256,
        "derivation": "direct_sec_quarterly_statement_or_quarterly_results_table",
    }


def run(*, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    downloaded, rows, sources = {}, [], []
    for fiscal_end, item in PERIOD_EVIDENCE.items():
        filed, form, accession, document, revenue, net_income = item
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
                "filed": filed, "form": form, "accession": accession,
                "document": document, "url": url, "path": str(path),
                "sha256": downloaded[key][1],
            })
        path, sha = downloaded[key]
        validate_statement(path.read_bytes(), revenue, net_income)
        rows.extend([
            _row(fiscal_end=fiscal_end, available_date=filed, metric="revenue",
                 value=revenue, form=form, accession=accession,
                 archive=path.name, archive_sha256=sha),
            _row(fiscal_end=fiscal_end, available_date=filed,
                 metric="net_income", value=net_income, form=form,
                 accession=accession, archive=path.name, archive_sha256=sha),
        ])
    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    if len(facts) != 24 or facts["fiscal_end"].nunique() != 12:
        raise RuntimeError("OKTA recovery is not exactly twelve paired quarters")
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    recovered = [
        {
            "ticker": "OKTA", "fiscal_end": fiscal_end,
            "available_date": item[0], "revenue": float(item[4] * 1000),
            "net_income": float(item[5] * 1000),
        }
        for fiscal_end, item in PERIOD_EVIDENCE.items()
    ]
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "OKTA", "accepted_quarter_count": 12,
        "recovered_quarters": recovered, "filing_sources": sources,
        "outputs": {"quarters": {"path": str(facts_path),
                                   "sha256": _sha256(facts_path)}},
        "guardrail": (
            "Values are legacy-GAAP total revenue and GAAP net loss from direct "
            "three-month SEC statements or explicitly presented quarterly-results "
            "tables. Annual totals are never allocated. Each row uses the actual "
            "filing date, including the conservative 2018-03-12 availability for "
            "the eight quarters presented in the 2018 10-K."
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
