#!/usr/bin/env python3
"""Recover point-in-time STNE 2018-2020Q2 IFRS quarters from SEC filings."""

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
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001745431.json.gz"
)
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/stne_quarterly_reports_2018_2020q2"
)
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1745431"

# Values are in BRL.  Each value is re-proved against the downloaded filing;
# this declaration prevents a broad/ambiguous table parser from silently
# selecting adjusted income or one of Stone's component revenue lines.
PERIOD_EVIDENCE = {
    "2017-09-30": {
        "filed": "2018-11-26", "form": "6-K",
        "accession": "0000950103-18-013702", "document": "dp98655_6k.htm",
        "scale": 1_000_000, "revenue_text": "187.1", "income_text": "(14.8)",
        "revenue": 187_100_000, "net_income": -14_800_000,
        "derivation": "first_sec_quarterly_comparative_ifrs_statement",
    },
    "2018-03-31": {
        "filed": "2019-05-13", "form": "6-K",
        "accession": "0000950103-19-006304", "document": "dp106624_6k.htm",
        "scale": 1000, "revenue_text": "288,028", "income_text": "24,691",
        "revenue": 288_028_000, "net_income": 24_691_000,
        "derivation": "first_sec_quarterly_comparative_ifrs_statement",
    },
    "2018-06-30": {
        "filed": "2019-08-14", "form": "6-K",
        "accession": "0000950103-19-010842", "document": "dp111172_6k-fs.htm",
        "scale": 1000, "revenue_text": "347,700", "income_text": "63,023",
        "revenue": 347_700_000, "net_income": 63_023_000,
        "derivation": "first_sec_quarterly_comparative_ifrs_statement",
    },
    "2018-09-30": {
        "filed": "2018-11-26", "form": "6-K",
        "accession": "0000950103-18-013702", "document": "dp98655_6k.htm",
        "scale": 1_000_000, "revenue_text": "414.1", "income_text": "90.4",
        "revenue": 414_100_000, "net_income": 90_400_000,
        "derivation": "contemporaneous_sec_quarterly_ifrs_statement",
    },
    "2019-03-31": {
        "filed": "2019-05-13", "form": "6-K",
        "accession": "0000950103-19-006304", "document": "dp106624_6k.htm",
        "scale": 1000, "revenue_text": "535,773", "income_text": "177,036",
        "revenue": 535_773_000, "net_income": 177_036_000,
        "derivation": "contemporaneous_sec_quarterly_ifrs_statement",
    },
    "2019-06-30": {
        "filed": "2019-08-14", "form": "6-K",
        "accession": "0000950103-19-010842", "document": "dp111172_6k-fs.htm",
        "scale": 1000, "revenue_text": "586,192", "income_text": "171,853",
        "revenue": 586_192_000, "net_income": 171_853_000,
        "derivation": "contemporaneous_sec_quarterly_ifrs_statement",
    },
    "2019-09-30": {
        "filed": "2019-11-21", "form": "6-K",
        "accession": "0001193125-19-297597", "document": "d835493dex991.htm",
        "scale": 1_000_000, "revenue_text": "671.1", "income_text": "191.3",
        "revenue": 671_100_000, "net_income": 191_300_000,
        "derivation": "contemporaneous_sec_quarterly_ifrs_statement",
    },
    "2020-03-31": {
        "filed": "2020-05-26", "form": "6-K",
        "accession": "0000950103-20-010119", "document": "dp128643_6k.htm",
        "scale": 1000, "revenue_text": "716,756", "income_text": "158,619",
        "revenue": 716_756_000, "net_income": 158_619_000,
        "derivation": "contemporaneous_sec_quarterly_ifrs_statement",
    },
    "2020-06-30": {
        "filed": "2020-08-11", "form": "6-K",
        "accession": "0000950103-20-015657", "document": "dp134141_ex9901.htm",
        "scale": 1_000_000, "revenue_text": "667.4", "income_text": "123.6",
        "revenue": 667_400_000, "net_income": 123_600_000,
        "derivation": "contemporaneous_sec_quarterly_ifrs_statement",
    },
}
ANNUAL_FACTS = {
    "2018-12-31": {
        "filed": "2019-05-29", "accession": "0000950103-19-006867",
        "form": "20-F/A",
    },
    "2019-12-31": {
        "filed": "2020-04-29", "accession": "0000950103-20-008435",
        "form": "20-F",
    },
}
Q4_2017_EVIDENCE = {
    "filed": "2019-05-29", "accession": "0000950103-19-006867",
    "form": "20-F/A", "nine_month_revenue": 518_800_000,
    "nine_month_net_income": -90_700_000,
    "interim_accession": "0000950103-18-013702",
    "interim_document": "dp98655_6k.htm",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_statement(raw: bytes, fiscal_end: str, evidence: dict) -> None:
    """Reject issuer, period, currency, IFRS-line, and value mismatches."""
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    if "STONECO LTD." not in text.upper():
        raise ValueError("STNE issuer identity is not proven")
    end = pd.Timestamp(fiscal_end)
    if end.strftime("%B %-d").lower() not in text.lower():
        raise ValueError("STNE filing does not prove requested fiscal period")
    if not re.search(r"Brazilian Reais|R\$\s*(?:millions)?", text, re.I):
        raise ValueError("STNE BRL reporting currency is not proven")
    revenue_pattern = rf"Total revenue and income.{{0,35}}{re.escape(str(evidence['revenue_text']))}"
    income_pattern = rf"(?<!Adjusted )\bNet income(?: \(loss\))?(?: for the period| was)?.{{0,35}}{re.escape(str(evidence['income_text']))}"
    if not re.search(revenue_pattern, text, re.I):
        raise ValueError("STNE total revenue and income value is not proven")
    if not re.search(income_pattern, text, re.I):
        raise ValueError("STNE IFRS net income value is not proven")
    if int(evidence["scale"]) not in {1000, 1_000_000}:
        raise ValueError("STNE source scale is unsupported")


def _load_companyfacts(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    payload = wrapper.get("payload", {})
    if int(wrapper.get("cik", 0)) != 1745431 or int(payload.get("cik", 0)) != 1745431:
        raise ValueError("STNE Company Facts CIK mismatch")
    if str(payload.get("entityName", "")).upper() != "STONECO LTD.":
        raise ValueError("STNE Company Facts issuer mismatch")
    return wrapper


def _annual_fact(payload: dict, concept: str, fiscal_end: str, evidence: dict) -> dict:
    year = pd.Timestamp(fiscal_end).year
    matches = [
        fact
        for fact in payload["facts"]["ifrs-full"][concept]["units"]["BRL"]
        if fact.get("start") == f"{year}-01-01"
        and fact.get("end") == fiscal_end
        and fact.get("filed") == evidence["filed"]
        and fact.get("accn") == evidence["accession"]
        and fact.get("form") == evidence["form"]
    ]
    if len(matches) != 1:
        raise ValueError(f"STNE annual {concept} fact is not unique for {fiscal_end}")
    return matches[0]


def _row(*, fiscal_end: str, available_date: str, metric: str, value: float,
         concept: str, form: str, accession: str, archive: str,
         archive_sha256: str, derivation: str) -> dict:
    return {
        "ticker": "STNE", "fiscal_end": fiscal_end,
        "available_date": available_date, "metric": metric,
        "value": float(value), "taxonomy": "ifrs-full", "concept": concept,
        "form": form, "accession": accession, "unit": "BRL",
        "source": "sec_stne_contemporaneous_ifrs_statement",
        "source_archive": archive, "source_archive_sha256": archive_sha256,
        "derivation": derivation,
    }


def run(*, companyfacts_path: Path = DEFAULT_COMPANYFACTS,
        output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    wrapper = _load_companyfacts(companyfacts_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    rows, sources = [], []
    by_year: dict[int, list[dict]] = {}
    downloaded: dict[tuple[str, str], tuple[Path, str]] = {}
    for fiscal_end, evidence in PERIOD_EVIDENCE.items():
        key = (str(evidence["accession"]), str(evidence["document"]))
        compact = key[0].replace("-", "")
        url = f"{SEC_BASE}/{compact}/{key[1]}"
        if key not in downloaded:
            with urlopen(Request(url, headers=HEADERS), timeout=90) as response:
                raw = response.read()
            path = raw_dir / f"{key[0]}_{key[1]}"
            path.write_bytes(raw)
            downloaded[key] = (path, _sha256(path))
            sources.append({"accession": key[0], "document": key[1],
                            "url": url, "path": str(path),
                            "sha256": downloaded[key][1]})
        path, sha = downloaded[key]
        raw = path.read_bytes()
        validate_statement(raw, fiscal_end, evidence)
        values = {"revenue": evidence["revenue"],
                  "net_income": evidence["net_income"]}
        by_year.setdefault(pd.Timestamp(fiscal_end).year, []).append(values)
        for metric, value in values.items():
            rows.append(_row(
                fiscal_end=fiscal_end, available_date=str(evidence["filed"]),
                metric=metric, value=float(value),
                concept=("RevenueAndOperatingIncome" if metric == "revenue"
                         else "ProfitLoss"), form=str(evidence["form"]),
                accession=str(evidence["accession"]), archive=path.name,
                archive_sha256=sha, derivation=str(evidence["derivation"]),
            ))

    payload = wrapper["payload"]
    annual_2017 = {
        metric: _annual_fact(payload, concept, "2017-12-31", Q4_2017_EVIDENCE)
        for metric, concept in (("revenue", "RevenueAndOperatingIncome"),
                                ("net_income", "ProfitLoss"))
    }
    interim_path, interim_sha = downloaded[(
        Q4_2017_EVIDENCE["interim_accession"],
        Q4_2017_EVIDENCE["interim_document"],
    )]
    for metric, concept in (("revenue", "RevenueAndOperatingIncome"),
                            ("net_income", "ProfitLoss")):
        nine_month = float(Q4_2017_EVIDENCE[f"nine_month_{metric}"])
        value = float(annual_2017[metric]["val"]) - nine_month
        rows.append(_row(
            fiscal_end="2017-12-31",
            available_date=str(Q4_2017_EVIDENCE["filed"]), metric=metric,
            value=value, concept=f"derived_q4:{concept}",
            form=str(Q4_2017_EVIDENCE["form"]),
            accession=str(Q4_2017_EVIDENCE["accession"]),
            archive=f"{companyfacts_path.name}+{interim_path.name}",
            archive_sha256=(f"{_sha256(companyfacts_path)}+{interim_sha}"),
            derivation=(
                "first_matching_annual_ifrs_fact_less_original_rounded_"
                "nine_month_sec_statement"
            ),
        ))
    for fiscal_end, evidence in ANNUAL_FACTS.items():
        year = pd.Timestamp(fiscal_end).year
        if len(by_year.get(year, [])) != 3:
            raise RuntimeError(f"STNE {year} lacks three proven interim quarters")
        for metric, concept in (("revenue", "RevenueAndOperatingIncome"),
                                ("net_income", "ProfitLoss")):
            annual = _annual_fact(payload, concept, fiscal_end, evidence)
            value = float(annual["val"]) - sum(q[metric] for q in by_year[year])
            rows.append(_row(
                fiscal_end=fiscal_end, available_date=str(evidence["filed"]),
                metric=metric, value=value, concept=f"derived_q4:{concept}",
                form=str(evidence["form"]), accession=str(evidence["accession"]),
                archive=companyfacts_path.name,
                archive_sha256=_sha256(companyfacts_path),
                derivation="first_matching_annual_ifrs_fact_less_proven_q1_q2_q3",
            ))

    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"]).reset_index(drop=True)
    if len(facts) != 24 or facts["fiscal_end"].nunique() != 12:
        raise RuntimeError("STNE recovery is not exactly twelve paired quarters")
    if not facts.groupby("fiscal_end")["metric"].nunique().eq(2).all():
        raise RuntimeError("STNE recovery has an unpaired quarter")
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    paired = facts.pivot_table(index=["fiscal_end", "available_date"],
                               columns="metric", values="value",
                               aggfunc="first").reset_index()
    recovered = [{"ticker": "STNE", "fiscal_end": str(row.fiscal_end),
                  "available_date": str(row.available_date),
                  "revenue": float(row.revenue),
                  "net_income": float(row.net_income)}
                 for row in paired.itertuples(index=False)]
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "STNE", "accepted_quarter_count": 12,
        "recovered_quarters": recovered, "filing_sources": sources,
        "companyfacts": {"path": str(companyfacts_path),
                         "sha256": _sha256(companyfacts_path),
                         "source_url": wrapper.get("source_url"),
                         "fetched_at": wrapper.get("fetched_at")},
        "outputs": {"quarters": {"path": str(facts_path),
                                  "sha256": _sha256(facts_path)}},
        "guardrail": (
            "Revenue means IFRS Total revenue and income in BRL, not a component "
            "line. Net income is IFRS net income for the period, not adjusted "
            "income or parent-only income. Comparative quarters retain their "
            "first SEC availability date. Q4 is derived only from the first "
            "matching annual filing; 2017Q4 retains the original interim "
            "statement's disclosed R$0.1m precision. No later-filed 2020Q3 "
            "value is backdated."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companyfacts", type=Path, default=DEFAULT_COMPANYFACTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(companyfacts_path=args.companyfacts, output_dir=args.output_dir)
    print(json.dumps({"accepted_quarter_count": result["accepted_quarter_count"],
                      "manifest": result["manifest"],
                      "release_status": result["release_status"]},
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
