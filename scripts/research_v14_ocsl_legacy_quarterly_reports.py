#!/usr/bin/env python3
"""Recover OCSL/FSC 2017Q1-2020Q3 from contemporaneous SEC statements."""

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
    "output/research_only/v14/ocsl_legacy_quarterly_reports_2017q1_2020q3"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1414932"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

# Values are in thousands of USD in the filed statements.
PERIOD_EVIDENCE = {
    "2017-03-31": ("2017-05-10", "10-Q", "0001414932-17-000022", "fsc-033117x10xq.htm", 45_555, 8_801),
    "2017-06-30": ("2017-08-09", "10-Q", "0001414932-17-000026", "fsc-063017x10xq.htm", 44_917, -6_057),
    "2017-09-30": ("2017-11-29", "10-K", "0001414932-17-000033", "ocsl-093017x10xk.htm", 35_732, -125_471),
    "2017-12-31": ("2018-02-08", "10-Q", "0001414932-18-000004", "ocsl-123117x10xq.htm", 33_876, -30_441),
    "2018-03-31": ("2018-05-08", "10-Q", "0001414932-18-000007", "ocsl-033118x10xq.htm", 34_779, 19_620),
    "2018-06-30": ("2018-08-08", "10-Q", "0001414932-18-000011", "ocsl-063018x10xq.htm", 31_847, 24_252),
    "2018-09-30": ("2018-11-29", "10-K", "0001414932-18-000015", "ocsl-093018x10xk.htm", 38_220, 33_331),
    "2018-12-31": ("2019-02-07", "10-Q", "0001414932-19-000004", "ocsl-123118x10xq.htm", 38_276, 27_718),
    "2019-03-31": ("2019-05-08", "10-Q", "0001414932-19-000007", "ocsl-033119x10xq.htm", 38_244, 64_485),
    "2019-06-30": ("2019-08-07", "10-Q", "0001414932-19-000012", "ocsl-063019x10xq.htm", 36_669, 19_986),
    "2019-09-30": ("2019-11-20", "10-K", "0001414932-19-000017", "ocsl-093019x10xk.htm", 34_513, 13_971),
    "2019-12-31": ("2020-02-06", "10-Q", "0001414932-20-000004", "ocsl-123119x10xq.htm", 30_960, 13_843),
    "2020-03-31": ("2020-05-07", "10-Q", "0001414932-20-000008", "ocsl-033120x10xq.htm", 34_171, -165_467),
    "2020-06-30": ("2020-08-07", "10-Q", "0001414932-20-000013", "ocsl-063020x10xq.htm", 34_403, 120_231),
    "2020-09-30": ("2020-11-19", "10-K", "0001414932-20-000019", "ocsl-09302020x10xk.htm", 43_599, 70_617),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_accounting_value(cells) -> int | None:
    for cell in list(cells)[1:]:
        token = str(cell).strip().replace("$", "").replace(" ", "")
        if token.lower() == "nan" or not re.search(r"\d", token):
            continue
        negative = token.startswith("(") or token.startswith("-")
        digits = re.sub(r"[^\d]", "", token)
        if digits:
            value = int(digits)
            return -value if negative else value
    return None


def extract_statement_values(raw: bytes, fiscal_end: str) -> tuple[int, int]:
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    if not re.search(r"(?:Oaktree Specialty Lending|Fifth Street Finance)", text, re.I):
        raise ValueError("OCSL/FSC issuer identity is not proven")
    end = pd.Timestamp(fiscal_end)
    if end.strftime("%B %-d, %Y").lower() not in text.lower():
        raise ValueError("OCSL requested quarter is not proven")
    if not re.search(r"(?:dollars )?in thousands", text, re.I):
        raise ValueError("OCSL USD-thousands reporting scale is not proven")
    for table in pd.read_html(io.BytesIO(raw)):
        flattened = " ".join(map(str, table.values.ravel()))
        if "three months" not in flattened.lower():
            continue
        rows = table.iloc[:, 0].astype(str)
        revenue_rows = table.loc[rows.eq("Total investment income")]
        net_rows = table.loc[
            rows.str.contains(
                r"Net (?:increase|decrease).*net assets resulting from operations",
                case=False, regex=True, na=False,
            )
        ]
        if revenue_rows.empty or net_rows.empty:
            continue
        revenue = _first_accounting_value(revenue_rows.iloc[0])
        net_income = _first_accounting_value(net_rows.iloc[0])
        if revenue is not None and net_income is not None:
            return revenue, net_income
    raise ValueError("OCSL consolidated investment-company statement is not proven")


def _row(*, fiscal_end: str, available_date: str, metric: str, value: int,
         form: str, accession: str, archive: str, archive_sha256: str) -> dict:
    return {
        "ticker": "OCSL", "fiscal_end": fiscal_end,
        "available_date": available_date, "metric": metric,
        "value": float(value * 1000), "taxonomy": "us-gaap",
        "concept": (
            "GrossInvestmentIncomeOperating"
            if metric == "revenue" else "NetIncomeLoss"
        ),
        "form": form, "accession": accession, "unit": "USD",
        "source": "sec_ocsl_fsc_contemporaneous_statement",
        "source_archive": archive, "source_archive_sha256": archive_sha256,
        "derivation": "direct_sec_three_month_investment_company_statement",
    }


def run(*, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    rows, sources = [], []
    for fiscal_end, item in PERIOD_EVIDENCE.items():
        filed, form, accession, document, revenue, net_income = item
        compact = accession.replace("-", "")
        url = f"{SEC_BASE}/{compact}/{document}"
        path = raw_dir / f"{accession}_{document}"
        if not path.exists():
            with urlopen(Request(url, headers=HEADERS), timeout=90) as response:
                path.write_bytes(response.read())
        sha = _sha256(path)
        observed = extract_statement_values(path.read_bytes(), fiscal_end)
        if observed != (revenue, net_income):
            raise ValueError(
                f"OCSL {fiscal_end} statement mismatch: {observed} != "
                f"{(revenue, net_income)}"
            )
        sources.append({
            "fiscal_end": fiscal_end, "filed": filed, "form": form,
            "accession": accession, "document": document, "url": url,
            "path": str(path), "sha256": sha,
        })
        rows.extend([
            _row(fiscal_end=fiscal_end, available_date=filed, metric="revenue",
                 value=revenue, form=form, accession=accession,
                 archive=path.name, archive_sha256=sha),
            _row(fiscal_end=fiscal_end, available_date=filed,
                 metric="net_income", value=net_income, form=form,
                 accession=accession, archive=path.name, archive_sha256=sha),
        ])
    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    if len(facts) != 30 or facts["fiscal_end"].nunique() != 15:
        raise RuntimeError("OCSL recovery is not exactly fifteen paired quarters")
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "OCSL", "accepted_quarter_count": 15,
        "predecessor_identity": "Fifth Street Finance Corp (same CIK 1414932)",
        "recovered_quarters": [
            {
                "ticker": "OCSL", "fiscal_end": fiscal_end,
                "available_date": item[0],
                "revenue": float(item[4] * 1000),
                "net_income": float(item[5] * 1000),
            }
            for fiscal_end, item in PERIOD_EVIDENCE.items()
        ],
        "filing_sources": sources,
        "outputs": {"quarters": {"path": str(facts_path),
                                   "sha256": _sha256(facts_path)}},
        "guardrail": (
            "Revenue is total investment income and net income is the net "
            "increase/decrease in net assets resulting from operations. Net "
            "investment income alone is not substituted for GAAP net income. "
            "Every quarter is read directly from a contemporaneous three-month "
            "SEC statement; no annual or future-period allocation is used."
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
