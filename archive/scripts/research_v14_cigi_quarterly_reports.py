#!/usr/bin/env python3
"""Recover CIGI 2017-2021 quarters from contemporaneous SEC 6-K exhibits."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup


DEFAULT_COMPANYFACTS = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0000913353.json.gz"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/cigi_quarterly_reports_2017_2021"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/913353"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
QUARTER_FILINGS = {
    "2017-03-31": "0001171843-17-002733",
    "2017-06-30": "0001171843-17-004608",
    "2017-09-30": "0001171843-17-006570",
    "2018-03-31": "0001171843-18-003359",
    "2018-06-30": "0001171843-18-005627",
    "2018-09-30": "0001171843-18-007527",
    "2019-03-31": "0001171843-19-002925",
    "2019-06-30": "0001171843-19-005124",
    "2019-09-30": "0001171843-19-007087",
    "2020-03-31": "0001171843-20-003173",
    "2020-06-30": "0001171843-20-005745",
    "2020-09-30": "0001171843-20-007487",
    "2021-03-31": "0001171843-21-003312",
    "2021-06-30": "0001171843-21-005650",
    "2021-09-30": "0001171843-21-007631",
}
ANNUAL_FILINGS = {
    "2017-12-31": ("2018-02-28", "0001171843-18-001568"),
    "2018-12-31": ("2019-02-22", "0001171843-19-001159"),
    "2019-12-31": ("2020-02-19", "0001171843-20-001077"),
    "2020-12-31": ("2021-02-18", "0001171843-21-001148"),
    "2021-12-31": ("2022-02-17", "0001171843-22-001149"),
}
REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_number(match: re.Match) -> float:
    value = float(match.group(2).replace(",", "")) * 1000.0
    return -value if match.group(1) or match.group(3) else value


def parse_statement(raw: bytes, fiscal_end: str) -> dict:
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    identity = (
        "COLLIERS INTERNATIONAL GROUP INC.",
        "CONSOLIDATED STATEMENTS OF EARNINGS",
        "Unaudited",
        "in thousands of US dollars",
    )
    if not all(value.lower() in text.lower() for value in identity):
        raise ValueError("CIGI identity, statement, or units are not proven")
    end = pd.Timestamp(fiscal_end)
    period_label = end.strftime("%B %-d")
    if f"ended {period_label}".lower() not in text.lower():
        raise ValueError("CIGI exhibit does not prove the requested quarter")
    start = text.upper().find("CONSOLIDATED STATEMENTS OF EARNINGS")
    statement = text[start : start + 5000]
    patterns = {
        "revenue": r"Revenues(?: \(note \d+\))?\s+\$?\s*(\()?\s*([\d,]+)\s*(\))?",
        "net_income": r"Net earnings(?: \(loss(?:es)?\))?\s+\$?\s*(\()?\s*([\d,]+)\s*(\))?",
    }
    values = {}
    for metric, pattern in patterns.items():
        match = re.search(pattern, statement, re.IGNORECASE)
        if match is None:
            raise ValueError(f"CIGI statement does not prove {metric}")
        values[metric] = _parse_number(match)
    return values


def _load_companyfacts(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    payload = wrapper.get("payload", {})
    if int(wrapper.get("cik", 0)) != 913353 or int(
        payload.get("cik", 0)
    ) != 913353:
        raise ValueError("CIGI Company Facts CIK mismatch")
    if "COLLIERS INTERNATIONAL GROUP" not in str(
        payload.get("entityName", "")
    ).upper():
        raise ValueError("CIGI Company Facts issuer mismatch")
    return wrapper


def _annual_fact(
    payload: dict,
    *,
    fiscal_end: str,
    filed: str,
    accession: str,
    concepts: tuple[str, ...],
) -> tuple[str, dict]:
    end = pd.Timestamp(fiscal_end)
    start = pd.Timestamp(year=end.year, month=1, day=1)
    matches = []
    facts = payload["facts"]["us-gaap"]
    for concept in concepts:
        for item in facts.get(concept, {}).get("units", {}).get("USD", []):
            if (
                pd.Timestamp(item.get("start")) == start
                and pd.Timestamp(item.get("end")) == end
                and item.get("filed") == filed
                and item.get("accn") == accession
                and item.get("form") in {"40-F", "40-F/A"}
            ):
                matches.append((concept, item))
    if len(matches) != 1:
        raise ValueError(
            f"CIGI annual fact is not unique for {fiscal_end} {concepts}: "
            f"{len(matches)}"
        )
    return matches[0]


def _row(
    *,
    fiscal_end: str,
    available_date: str,
    metric: str,
    value: float,
    concept: str,
    form: str,
    accession: str,
    source_archive: str,
    source_sha256: str,
    derivation: str,
) -> dict:
    return {
        "ticker": "CIGI",
        "fiscal_end": fiscal_end,
        "available_date": available_date,
        "metric": metric,
        "value": value,
        "taxonomy": "us-gaap",
        "concept": concept,
        "form": form,
        "accession": accession,
        "unit": "USD",
        "source": "sec_6k_cigi_quarterly_report",
        "source_archive": source_archive,
        "source_archive_sha256": source_sha256,
        "derivation": derivation,
    }


def run(
    *,
    companyfacts_path: Path = DEFAULT_COMPANYFACTS,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    wrapper = _load_companyfacts(companyfacts_path)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    direct: dict[int, list[dict]] = {}
    sources = []
    for fiscal_end, accession in QUARTER_FILINGS.items():
        compact = accession.replace("-", "")
        url = f"{SEC_BASE}/{compact}/exh_991.htm"
        with urlopen(Request(url, headers=HEADERS), timeout=90) as response:
            raw = response.read()
        raw_path = raw_dir / f"{accession}_exh_991.htm"
        raw_path.write_bytes(raw)
        values = parse_statement(raw, fiscal_end)
        year = pd.Timestamp(fiscal_end).year
        direct.setdefault(year, []).append(values)
        for metric, value in values.items():
            rows.append(_row(
                fiscal_end=fiscal_end,
                available_date=pd.Timestamp(
                    accession_filing_date(accession)
                ).strftime("%Y-%m-%d"),
                metric=metric,
                value=value,
                concept="Revenues" if metric == "revenue" else "ProfitLoss",
                form="6-K",
                accession=accession,
                source_archive=raw_path.name,
                source_sha256=_sha256(raw_path),
                derivation="direct_three_month_sec_6k_statement",
            ))
        sources.append({
            "fiscal_end": fiscal_end,
            "accession": accession,
            "url": url,
            "path": str(raw_path),
            "sha256": _sha256(raw_path),
        })

    payload = wrapper["payload"]
    for fiscal_end, (filed, accession) in ANNUAL_FILINGS.items():
        year = pd.Timestamp(fiscal_end).year
        if len(direct.get(year, [])) != 3:
            raise RuntimeError(f"CIGI {year} lacks three original interim quarters")
        annual_revenue_concept, annual_revenue = _annual_fact(
            payload,
            fiscal_end=fiscal_end,
            filed=filed,
            accession=accession,
            concepts=REVENUE_CONCEPTS,
        )
        annual_income_concept, annual_income = _annual_fact(
            payload,
            fiscal_end=fiscal_end,
            filed=filed,
            accession=accession,
            concepts=("ProfitLoss",),
        )
        for metric, concept, annual in (
            ("revenue", annual_revenue_concept, annual_revenue),
            ("net_income", annual_income_concept, annual_income),
        ):
            value = float(annual["val"]) - sum(
                quarter[metric] for quarter in direct[year]
            )
            rows.append(_row(
                fiscal_end=fiscal_end,
                available_date=filed,
                metric=metric,
                value=value,
                concept=f"derived_q4:{concept}",
                form="40-F",
                accession=accession,
                source_archive=companyfacts_path.name,
                source_sha256=_sha256(companyfacts_path),
                derivation="original_annual_40f_less_original_q1_q2_q3",
            ))

    facts = pd.DataFrame(rows).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)
    if (
        len(facts) != 40
        or facts["fiscal_end"].nunique() != 20
        or not facts.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    ):
        raise RuntimeError("CIGI recovery is not exactly twenty paired quarters")
    paired = facts.pivot_table(
        index=["fiscal_end", "available_date"],
        columns="metric",
        values="value",
        aggfunc="first",
    ).reset_index()
    recovered = [
        {
            "ticker": "CIGI",
            "fiscal_end": pd.Timestamp(row.fiscal_end).strftime("%Y-%m-%d"),
            "available_date": pd.Timestamp(row.available_date).strftime("%Y-%m-%d"),
            "revenue": float(row.revenue),
            "net_income": float(row.net_income),
        }
        for row in paired.itertuples(index=False)
    ]
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "ticker": "CIGI",
        "accepted_quarter_count": 20,
        "recovered_quarters": recovered,
        "quarterly_sources": sources,
        "companyfacts": {
            "path": str(companyfacts_path),
            "sha256": _sha256(companyfacts_path),
            "source_url": wrapper.get("source_url"),
            "fetched_at": wrapper.get("fetched_at"),
        },
        "outputs": {
            "quarters": {"path": str(facts_path), "sha256": _sha256(facts_path)}
        },
        "guardrail": (
            "Q1-Q3 use the first contemporaneously filed SEC 6-K interim "
            "statement and the first current-quarter column. Net income is "
            "the consistently reported ProfitLoss/Net earnings row. Q4 is "
            "the original 40-F total less those original three quarters. "
            "Later comparative columns and formal financial files are excluded."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def accession_filing_date(accession: str) -> str:
    dates = {
        "0001171843-17-002733": "2017-05-05",
        "0001171843-17-004608": "2017-08-02",
        "0001171843-17-006570": "2017-11-02",
        "0001171843-18-003359": "2018-05-02",
        "0001171843-18-005627": "2018-08-01",
        "0001171843-18-007527": "2018-11-02",
        "0001171843-19-002925": "2019-05-03",
        "0001171843-19-005124": "2019-08-02",
        "0001171843-19-007087": "2019-11-01",
        "0001171843-20-003173": "2020-05-01",
        "0001171843-20-005745": "2020-08-07",
        "0001171843-20-007487": "2020-11-03",
        "0001171843-21-003312": "2021-05-07",
        "0001171843-21-005650": "2021-08-06",
        "0001171843-21-007631": "2021-11-05",
    }
    try:
        return dates[accession]
    except KeyError as error:
        raise ValueError(f"unapproved CIGI accession: {accession}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companyfacts", type=Path, default=DEFAULT_COMPANYFACTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(companyfacts_path=args.companyfacts, output_dir=args.output_dir)
    print(json.dumps({
        "accepted_quarter_count": result["accepted_quarter_count"],
        "manifest": result["manifest"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
