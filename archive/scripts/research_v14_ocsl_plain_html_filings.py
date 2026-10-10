#!/usr/bin/env python3
"""Recover OCSL 2020Q4-2021Q3 from contemporaneous plain-HTML SEC filings."""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

import pandas as pd


CIK = 1_414_932
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/ocsl_plain_html_2020q4_2021q3"
)
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

FILINGS = {
    "2020Q4": {
        "accession": "0001414932-21-000004",
        "document": "ocsl-12312020x10xq.htm",
        "filed": "2021-02-04",
        "heading": "Three months ended December 31, 2020",
        "prior_heading": "Three months ended December 31, 2019",
        "expected_prior": (30_960_000.0, 13_843_000.0),
    },
    "2021Q1": {
        "accession": "0001414932-21-000008",
        "document": "ocsl-03312021x10xq.htm",
        "filed": "2021-05-06",
        "heading": "Three months ended March 31, 2021",
        "prior_heading": "Three months ended March 31, 2020",
        "expected_prior": (34_171_000.0, -165_467_000.0),
    },
    "2021Q2": {
        "accession": "0001414932-21-000015",
        "document": "ocsl-06302021x10xq.htm",
        "filed": "2021-08-05",
        "heading": "Three months ended June 30, 2021",
        "prior_heading": "Three months ended June 30, 2020",
        "expected_prior": (34_403_000.0, 120_231_000.0),
        "nine_month_heading": "Nine months ended June 30, 2021",
    },
    "2021FY": {
        "accession": "0001414932-21-000020",
        "document": "ocsl-09302021x10xk.htm",
        "filed": "2021-11-16",
        "heading": "Year ended September 30, 2021",
        "prior_heading": "Year ended September 30, 2020",
        "expected_prior": (143_133_000.0, 39_224_000.0),
    },
}

PERIODS = (
    ("2020Q4", "2020-12-31"),
    ("2021Q1", "2021-03-31"),
    ("2021Q2", "2021-06-30"),
)

REVENUE_LABEL = "Total investment income"
PROFIT_LABEL = "Net increase (decrease) in net assets resulting from operations"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_url(item: dict) -> str:
    accession = item["accession"].replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
        f"{accession}/{item['document']}"
    )


def _fetch(item: dict) -> bytes:
    request = Request(_source_url(item), headers=SEC_HEADERS)
    with urlopen(request, timeout=120) as response:
        return response.read()


def _number(value) -> float | None:
    text = str(value).strip()
    if not text or text.lower() == "nan" or text in {"$", "—", "-"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    result = float(cleaned) * 1000.0
    return -result if negative else result


def _statement_table(raw: bytes) -> pd.DataFrame:
    matches = []
    for table in pd.read_html(BytesIO(raw)):
        values = table.astype(str)
        flattened = set(values.to_numpy().ravel())
        if REVENUE_LABEL in flattened and PROFIT_LABEL in flattened:
            if "Base management fee" in flattened:
                matches.append(table)
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one OCSL statement-of-operations table, found {len(matches)}"
        )
    return matches[0]


def _row_value(table: pd.DataFrame, label: str, heading: str) -> float:
    text = table.astype(str)
    heading_columns = [
        column for column in table.columns
        if text[column].eq(heading).any()
    ]
    if not heading_columns:
        raise RuntimeError(f"missing statement heading {heading!r}")
    row_mask = text.apply(lambda column: column.eq(label)).any(axis=1)
    if int(row_mask.sum()) != 1:
        raise RuntimeError(f"expected one statement row {label!r}")
    values = {
        number
        for value in table.loc[row_mask, heading_columns].iloc[0]
        if (number := _number(value)) is not None
    }
    if len(values) != 1:
        raise RuntimeError(
            f"expected one value for {label!r} / {heading!r}, found {sorted(values)}"
        )
    return values.pop()


def _pair(raw: bytes, heading: str) -> tuple[float, float]:
    table = _statement_table(raw)
    return (
        _row_value(table, REVENUE_LABEL, heading),
        _row_value(table, PROFIT_LABEL, heading),
    )


def recover(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = {key: _fetch(item) for key, item in FILINGS.items()}
    direct = {}
    sources = []
    for key, item in FILINGS.items():
        current = _pair(raw[key], item["heading"])
        prior = _pair(raw[key], item["prior_heading"])
        if prior != item["expected_prior"]:
            raise RuntimeError(
                f"{key} prior-period comparison mismatch: "
                f"expected {item['expected_prior']}, found {prior}"
            )
        direct[key] = current
        sources.append({
            "period": key,
            "accession": item["accession"],
            "document": item["document"],
            "filed": item["filed"],
            "url": _source_url(item),
            "sha256": _sha256_bytes(raw[key]),
            "bytes": len(raw[key]),
        })

    nine_month = _pair(raw["2021Q2"], FILINGS["2021Q2"]["nine_month_heading"])
    annual = direct["2021FY"]
    q3 = tuple(annual[index] - nine_month[index] for index in range(2))
    expected_q3 = (63_800_000.0, 36_561_000.0)
    if q3 != expected_q3:
        raise RuntimeError(f"OCSL 2021Q3 derivation mismatch: {q3}")

    quarter_values = {
        **{fiscal_end: direct[key] for key, fiscal_end in PERIODS},
        "2021-09-30": q3,
    }
    if tuple(map(sum, zip(*quarter_values.values()))) != annual:
        raise RuntimeError("OCSL 2021 quarterly values do not close to FY")

    facts = []
    for key, fiscal_end in PERIODS:
        item = FILINGS[key]
        for metric, value, concept in (
            ("revenue", direct[key][0], "GrossInvestmentIncomeOperating"),
            ("net_income", direct[key][1], "NetIncomeLoss"),
        ):
            facts.append({
                "ticker": "OCSL", "fiscal_end": fiscal_end,
                "available_date": item["filed"], "metric": metric,
                "value": value, "taxonomy": "sec-plain-html",
                "concept": f"plain_html_statement:{concept}",
                "form": "10-Q", "accession": item["accession"],
                "fetched_at": pd.Timestamp.utcnow().tz_localize(None).normalize(),
            })
    annual_item = FILINGS["2021FY"]
    for metric, value, concept in (
        ("revenue", q3[0], "GrossInvestmentIncomeOperating"),
        ("net_income", q3[1], "NetIncomeLoss"),
    ):
        facts.append({
            "ticker": "OCSL", "fiscal_end": "2021-09-30",
            "available_date": annual_item["filed"], "metric": metric,
            "value": value, "taxonomy": "sec-plain-html",
            "concept": f"plain_html_derived_q4:{concept}",
            "form": "10-K", "accession": annual_item["accession"],
            "fetched_at": pd.Timestamp.utcnow().tz_localize(None).normalize(),
        })
    frame = pd.DataFrame(facts).sort_values(["fiscal_end", "metric"])
    facts_path = output_dir / "strict_quarterly_facts.csv"
    frame.to_csv(facts_path, index=False)
    recovered_quarters = []
    for fiscal_end, group in frame.groupby("fiscal_end", sort=True):
        values = group.set_index("metric")["value"].to_dict()
        recovered_quarters.append({
            "ticker": "OCSL",
            "fiscal_end": str(fiscal_end),
            "available_date": str(group["available_date"].iloc[0]),
            "revenue": float(values["revenue"]),
            "net_income": float(values["net_income"]),
        })
    manifest = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": "OCSL",
        "cik": CIK,
        "accepted_quarter_count": 4,
        "accepted_fact_count": 8,
        "point_in_time_proven": True,
        "recovered_quarters": recovered_quarters,
        "sources": sources,
        "validation": {
            "direct_quarters": 3,
            "derived_q4_formula": "2021FY minus 2021 nine months",
            "nine_month_values": {
                "revenue": nine_month[0], "net_income": nine_month[1]
            },
            "annual_values": {
                "revenue": annual[0], "net_income": annual[1]
            },
            "quarterly_closure_difference": {
                "revenue": 0.0, "net_income": 0.0
            },
            "unit": "USD",
            "source_scale": "USD thousands",
        },
        "outputs": {
            "facts": str(facts_path),
            "facts_sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        },
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {"manifest": str(manifest_path), **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    report = recover(args.output_dir)
    print(json.dumps({
        "manifest": report["manifest"],
        "accepted_quarter_count": report["accepted_quarter_count"],
        "release_status": report["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
