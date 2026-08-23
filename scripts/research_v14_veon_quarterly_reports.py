#!/usr/bin/env python3
"""Recover VEON 2017Q1-2021Q3 from contemporaneous SEC 6-K IFRS reports."""

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
    "output/research_only/v14/veon_quarterly_reports_2017q1_2021q3"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1468091"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

# Direct three-month consolidated IFRS values in millions of USD. Q2/Q3 SEC
# reports also contain year-to-date columns; the values below are taken only
# from their explicitly labelled three-month columns.
PERIOD_EVIDENCE = {
    "2017-03-31": ("2017-05-11", "0001193125-17-166747", "d378002d6k.htm", 2_281, -11),
    "2017-06-30": ("2017-08-03", "0001193125-17-247029", "d432655d6k.htm", 2_417, -258),
    "2017-09-30": ("2017-11-09", "0001193125-17-338261", "d490903d6k.htm", 2_456, 151),
    "2017-12-31": ("2018-02-22", "0001193125-18-054098", "d510687d6k.htm", 2_320, -378),
    "2018-03-31": ("2018-05-14", "0001193125-18-161706", "d571249d6k.htm", 2_250, -82),
    "2018-06-30": ("2018-08-02", "0001193125-18-235809", "d668402d6k.htm", 2_270, -138),
    "2018-09-30": ("2018-11-08", "0001193125-18-321825", "d607851d6k.htm", 2_317, 561),
    "2018-12-31": ("2019-02-25", "0001468091-19-000015", "exhibit991earningsreleas.htm", 2_249, 33),
    "2019-03-31": ("2019-05-02", "0001468091-19-000028", "a6-kq1mdafinal.htm", 2_124, 530),
    "2019-06-30": ("2019-08-01", "0001468091-19-000050", "veon6-kq22019.htm", 2_261, 75),
    "2019-09-30": ("2019-11-04", "0001468091-19-000073", "veon6-kq32019.htm", 2_224, 31),
    "2019-12-31": ("2020-02-14", "0001468091-20-000008", "veon4q19erfinal.htm", 2_254, 48),
    "2020-03-31": ("2020-05-07", "0001468091-20-000031", "veon6-kq12020.htm", 2_097, 120),
    "2020-06-30": ("2020-08-06", "0001468091-20-000046", "vip-20200630.htm", 1_892, 175),
    "2020-09-30": ("2020-10-29", "0001468091-20-000060", "veon6-kq32020.htm", 1_993, -644),
    "2020-12-31": ("2021-02-18", "0001468091-21-000009", "a4q20earningsrelease_fin.htm", 1_998, 35),
    "2021-03-31": ("2021-04-29", "0001468091-21-000029", "veonltd6-kq12021.htm", 1_989, 138),
    "2021-06-30": ("2021-08-30", "0001468091-21-000048", "vip-20210630.htm", 2_065, 127),
    "2021-09-30": ("2021-10-28", "0001468091-21-000062", "vip-20210930.htm", 2_005, 195),
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
        else:
            token = str(cell).strip().replace("$", "").replace("−", "-")
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


def validate_statement(raw: bytes, fiscal_end: str, revenue: int, profit: int) -> None:
    soup = BeautifulSoup(raw, "html.parser")
    text = " ".join(soup.get_text(" ").split())
    if not re.search(r"(?:VEON|VimpelCom)", text, re.I):
        raise ValueError("VEON issuer identity is not proven")
    if not re.search(r"(?:millions of U\.S\. dollars|USD millions?)", text, re.I):
        raise ValueError("VEON USD-millions reporting scale is not proven")
    end = pd.Timestamp(fiscal_end)
    period = end.strftime("%B %d, %Y").replace(" 0", " ")
    normalized = re.sub(r"\s+", " ", text.replace("\xa0", " "))
    quarter_label = f"4Q{str(end.year)[-2:]}"
    if period.lower() not in normalized.lower() and not (
        end.month == 12 and quarter_label.lower() in normalized.lower()
    ):
        raise ValueError("VEON requested quarter is not proven")
    if end.month == 12:
        revenue_token = f"{revenue:,}"
        profit_token = (
            rf"\(\s*{abs(profit):,}\s*\)" if profit < 0 else f"{profit:,}"
        )
        if (
            re.search(r"UNAUDITED CONSOLIDATED STATEMENT OF INCOME", normalized, re.I)
            and re.search(
                rf"Total (?:operating )?revenues?\s+{re.escape(revenue_token)}\b",
                normalized,
                re.I,
            )
            and re.search(
                rf"(?:\(Loss\)\s*/\s*Profit|Profit\s*/\s*\(Loss\)) "
                rf"for the period\s+{profit_token}",
                normalized,
                re.I,
            )
        ):
            return

    for table in soup.find_all("table"):
        table_text = " ".join(table.get_text(" ").split())
        direct_pattern = rf"three[- ]months?|{re.escape(quarter_label)}"
        table_has_direct_header = bool(re.search(
            direct_pattern, table_text.replace("\xa0", " "), re.I
        ))
        is_first_quarter = end.month == 3
        if not table_has_direct_header and not is_first_quarter:
            continue
        try:
            frames = pd.read_html(io.StringIO(str(table)), flavor="lxml")
        except (ValueError, ImportError):
            continue
        if not frames:
            continue
        frame = frames[0]
        direct_columns = [
            position
            for position in range(frame.shape[1])
            if re.search(
                direct_pattern,
                " ".join(map(str, frame.iloc[:4, position].tolist())).replace(
                    "\xa0", " "
                ),
                re.I,
            )
        ]
        if not direct_columns:
            if is_first_quarter:
                direct_columns = list(range(frame.shape[1]))
            else:
                continue
        labels = frame.iloc[:, 0].map(str).str.replace(
            r"\s+", " ", regex=True
        ).str.strip()
        revenue_rows = frame.loc[
            labels.str.fullmatch(
                r"Total (?:operating )?revenues?", case=False, na=False
            )
        ]
        profit_rows = frame.loc[
            labels.str.fullmatch(
                r"(?:\(Loss\) / profit|Profit / \(loss\)|Loss / profit|Net income/\(loss\))"
                r"(?: for the period)?",
                case=False,
                na=False,
            )
        ]
        if revenue_rows.empty or profit_rows.empty:
            continue
        revenues = _accounting_values(
            ["revenue", *revenue_rows.iloc[0].iloc[direct_columns].tolist()]
        )
        profits = _accounting_values(
            ["profit", *profit_rows.iloc[0].iloc[direct_columns].tolist()]
        )
        if revenue in revenues and profit in profits:
            return
    raise ValueError("VEON direct three-month consolidated revenue/profit is not proven")


def _row(*, fiscal_end: str, available_date: str, metric: str, value: int,
         accession: str, archive: str, archive_sha256: str) -> dict:
    return {
        "ticker": "VEON", "fiscal_end": fiscal_end,
        "available_date": available_date, "metric": metric,
        "value": float(value * 1_000_000), "taxonomy": "ifrs-full",
        "concept": "Revenue" if metric == "revenue" else "ProfitLoss",
        "form": "6-K", "accession": accession, "unit": "USD",
        "source": "sec_veon_contemporaneous_quarterly_statement",
        "source_archive": archive, "source_archive_sha256": archive_sha256,
        "derivation": "direct_sec_three_month_ifrs_statement",
    }


def run(*, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    rows, sources = [], []
    for fiscal_end, item in PERIOD_EVIDENCE.items():
        filed, accession, document, revenue, profit = item
        url = f"{SEC_BASE}/{accession.replace('-', '')}/{document}"
        path = raw_dir / f"{accession}_{document}"
        if not path.exists():
            with urlopen(Request(url, headers=HEADERS), timeout=120) as response:
                path.write_bytes(response.read())
        sha = _sha256(path)
        validate_statement(path.read_bytes(), fiscal_end, revenue, profit)
        sources.append({"fiscal_end": fiscal_end, "filed": filed,
                        "accession": accession, "document": document,
                        "url": url, "path": str(path), "sha256": sha})
        rows.extend([
            _row(fiscal_end=fiscal_end, available_date=filed, metric="revenue",
                 value=revenue, accession=accession, archive=path.name,
                 archive_sha256=sha),
            _row(fiscal_end=fiscal_end, available_date=filed, metric="net_income",
                 value=profit, accession=accession, archive=path.name,
                 archive_sha256=sha),
        ])
    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    if len(facts) != 38 or facts["fiscal_end"].nunique() != 19:
        raise RuntimeError("VEON recovery is not exactly nineteen paired quarters")
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "VEON", "accepted_quarter_count": 19,
        "recovered_quarters": [
            {"ticker": "VEON", "fiscal_end": end, "available_date": item[0],
             "revenue": float(item[3] * 1_000_000),
             "net_income": float(item[4] * 1_000_000)}
            for end, item in PERIOD_EVIDENCE.items()
        ],
        "filing_sources": sources,
        "outputs": {"quarters": {"path": str(facts_path),
                                   "sha256": _sha256(facts_path)}},
        "guardrail": (
            "Only explicitly labelled three-month consolidated IFRS values "
            "from contemporaneous SEC 6-K reports are accepted. Cumulative "
            "periods are not differenced; attributable, adjusted, segment, "
            "and annual values are rejected."
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
