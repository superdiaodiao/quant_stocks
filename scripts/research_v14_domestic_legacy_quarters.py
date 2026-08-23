#!/usr/bin/env python3
"""Recover predeclared ENSG, SAIA, and GOOD legacy PIT quarters."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd


DEFAULT_CACHE_DIR = Path("output/research_only/v14/companyfacts_cache")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/domestic_legacy_quarters_ensg_saia_good"
)

ISSUERS = {
    "ENSG": {
        "cik": 1125376,
        "archive": "CIK0001125376.json.gz",
        "entity_fragment": "ENSIGN GROUP",
    },
    "SAIA": {
        "cik": 1177702,
        "archive": "CIK0001177702.json.gz",
        "entity_fragment": "SAIA",
    },
    "GOOD": {
        "cik": 1234006,
        "archive": "CIK0001234006.json.gz",
        "entity_fragment": "GLADSTONE COMMERCIAL",
    },
}

# Every accepted fact is bound to its original filing version.  In particular,
# ENSG Q1/Q2 must not be replaced by later discontinued-operations comparatives.
DIRECT_QUARTERS = (
    {
        "ticker": "ENSG", "fiscal_end": "2017-03-31",
        "available_date": "2017-05-01", "accession": "0001125376-17-000047",
        "form": "10-Q", "start": "2017-01-01",
        "revenue_concept": "HealthCareOrganizationPatientServiceRevenue",
        "revenue": 441_739_000.0, "net_income": 2_840_000.0,
    },
    {
        "ticker": "ENSG", "fiscal_end": "2017-06-30",
        "available_date": "2017-08-03", "accession": "0001125376-17-000099",
        "form": "10-Q", "start": "2017-04-01",
        "revenue_concept": "HealthCareOrganizationPatientServiceRevenue",
        "revenue": 448_279_000.0, "net_income": 12_217_000.0,
    },
    {
        "ticker": "SAIA", "fiscal_end": "2018-12-31",
        "available_date": "2019-02-25", "accession": "0001564590-19-004105",
        "form": "10-K", "start": "2018-10-01",
        "revenue_concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "revenue": 406_750_000.0, "net_income": 25_380_000.0,
    },
    {
        "ticker": "GOOD", "fiscal_end": "2018-03-31",
        "available_date": "2018-05-01", "accession": "0001234006-18-000007",
        "form": "10-Q", "start": "2018-01-01",
        "revenue_concept": "RealEstateRevenueNet",
        "revenue": 26_353_000.0, "net_income": 4_605_000.0,
    },
    {
        "ticker": "GOOD", "fiscal_end": "2018-06-30",
        "available_date": "2018-07-30", "accession": "0001234006-18-000011",
        "form": "10-Q", "start": "2018-04-01",
        "revenue_concept": "RealEstateRevenueNet",
        "revenue": 26_593_000.0, "net_income": 2_525_000.0,
    },
    {
        "ticker": "GOOD", "fiscal_end": "2018-12-31",
        "available_date": "2019-02-13", "accession": "0001234006-19-000003",
        "form": "10-K", "start": "2018-10-01",
        "revenue_concept": "Revenues",
        "revenue": 27_261_000.0, "net_income": 2_517_000.0,
    },
)

ENSG_Q4 = {
    "ticker": "ENSG", "fiscal_end": "2017-12-31",
    "available_date": "2018-02-08", "accession": "0001125376-18-000028",
    "form": "10-K", "start": "2017-01-01",
    "revenue_concept": "HealthCareOrganizationPatientServiceRevenue",
    "annual_revenue": 1_849_317_000.0, "annual_net_income": 40_475_000.0,
    "revenue": 487_705_000.0, "net_income": 11_206_000.0,
}

ENSG_PRIOR_QUARTERS = (
    ("2017-01-01", "2017-03-31", "0001125376-17-000047", "2017-05-01",
     441_739_000.0, 2_840_000.0),
    ("2017-04-01", "2017-06-30", "0001125376-17-000099", "2017-08-03",
     448_279_000.0, 12_217_000.0),
    ("2017-07-01", "2017-09-30", "0001125376-17-000141", "2017-11-08",
     471_594_000.0, 14_212_000.0),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_payload(path: Path, ticker: str) -> tuple[dict, dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    identity = ISSUERS[ticker]
    payload = wrapper.get("payload") or {}
    if int(wrapper.get("cik", 0)) != identity["cik"]:
        raise ValueError(f"{ticker} wrapper CIK mismatch")
    if int(payload.get("cik", 0)) != identity["cik"]:
        raise ValueError(f"{ticker} payload CIK mismatch")
    if ticker not in {str(value).upper() for value in wrapper.get("symbols", [])}:
        raise ValueError(f"{ticker} wrapper symbol mismatch")
    if identity["entity_fragment"] not in str(payload.get("entityName", "")).upper():
        raise ValueError(f"{ticker} issuer identity mismatch")
    return wrapper, payload


def _unique_fact(
    payload: dict,
    *,
    concept: str,
    start: str,
    end: str,
    accession: str,
    form: str,
    filed: str,
    expected: float,
) -> dict:
    concept_payload = payload["facts"]["us-gaap"][concept]
    if set(concept_payload.get("units") or {}) != {"USD"}:
        raise ValueError(f"{concept} must use only USD")
    matches = [
        fact for fact in concept_payload["units"]["USD"]
        if fact.get("start") == start
        and fact.get("end") == end
        and fact.get("accn") == accession
        and fact.get("form") == form
        and fact.get("filed") == filed
    ]
    if len(matches) != 1:
        raise ValueError(
            f"{concept} {end} original filing fact is not unique: {len(matches)}"
        )
    if float(matches[0]["val"]) != float(expected):
        raise ValueError(f"{concept} {end} differs from predeclared evidence")
    return matches[0]


def _quarter_rows(spec: dict, payload: dict, *, derivation: str) -> list[dict]:
    common = {
        "ticker": spec["ticker"], "fiscal_end": spec["fiscal_end"],
        "available_date": spec["available_date"], "taxonomy": "us-gaap",
        "form": spec["form"], "accession": spec["accession"], "unit": "USD",
        "derivation": derivation,
    }
    return [
        {**common, "metric": "revenue", "value": spec["revenue"],
         "concept": spec["revenue_concept"]},
        {**common, "metric": "net_income", "value": spec["net_income"],
         "concept": "NetIncomeLoss"},
    ]


def strict_quarterly_rows(payloads: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for spec in DIRECT_QUARTERS:
        payload = payloads[spec["ticker"]]
        for concept, value in (
            (spec["revenue_concept"], spec["revenue"]),
            ("NetIncomeLoss", spec["net_income"]),
        ):
            _unique_fact(
                payload, concept=concept, start=spec["start"],
                end=spec["fiscal_end"], accession=spec["accession"],
                form=spec["form"], filed=spec["available_date"], expected=value,
            )
        rows.extend(_quarter_rows(
            spec, payload, derivation="direct_original_pit_sec_quarter_fact"
        ))

    ensign = payloads["ENSG"]
    for concept, expected in (
        (ENSG_Q4["revenue_concept"], ENSG_Q4["annual_revenue"]),
        ("NetIncomeLoss", ENSG_Q4["annual_net_income"]),
    ):
        _unique_fact(
            ensign, concept=concept, start=ENSG_Q4["start"],
            end=ENSG_Q4["fiscal_end"], accession=ENSG_Q4["accession"],
            form=ENSG_Q4["form"], filed=ENSG_Q4["available_date"],
            expected=expected,
        )
    prior_revenue = 0.0
    prior_net_income = 0.0
    for start, end, accession, filed, revenue, net_income in ENSG_PRIOR_QUARTERS:
        _unique_fact(
            ensign, concept=ENSG_Q4["revenue_concept"], start=start, end=end,
            accession=accession, form="10-Q", filed=filed, expected=revenue,
        )
        _unique_fact(
            ensign, concept="NetIncomeLoss", start=start, end=end,
            accession=accession, form="10-Q", filed=filed, expected=net_income,
        )
        prior_revenue += revenue
        prior_net_income += net_income
    if ENSG_Q4["annual_revenue"] - prior_revenue != ENSG_Q4["revenue"]:
        raise ValueError("ENSG Q4 revenue annual difference is not predeclared value")
    if ENSG_Q4["annual_net_income"] - prior_net_income != ENSG_Q4["net_income"]:
        raise ValueError("ENSG Q4 net income annual difference is not predeclared value")
    rows.extend(_quarter_rows(
        ENSG_Q4, ensign,
        derivation="annual_minus_original_pit_direct_q1_q2_q3",
    ))

    result = pd.DataFrame(rows).sort_values(
        ["ticker", "fiscal_end", "metric"]
    ).reset_index(drop=True)
    if len(result) != 14 or result[["ticker", "fiscal_end"]].drop_duplicates().shape[0] != 7:
        raise RuntimeError("domestic legacy recovery must contain exactly seven paired quarters")
    return result


def run(
    *, cache_dir: Path = DEFAULT_CACHE_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    payloads = {}
    wrappers = {}
    archives = {}
    for ticker, identity in ISSUERS.items():
        path = cache_dir / identity["archive"]
        wrapper, payload = _load_payload(path, ticker)
        payloads[ticker] = payload
        wrappers[ticker] = wrapper
        archives[ticker] = path
    facts = strict_quarterly_rows(payloads)
    facts["source"] = "sec_companyfacts_predeclared_original_pit_quarter"
    facts["source_archive"] = facts["ticker"].map(
        {ticker: path.name for ticker, path in archives.items()}
    )
    facts["source_archive_sha256"] = facts["ticker"].map(
        {ticker: _sha256(path) for ticker, path in archives.items()}
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    recovered = []
    for (ticker, fiscal_end, available_date), group in facts.groupby(
        ["ticker", "fiscal_end", "available_date"], sort=True
    ):
        values = group.set_index("metric")["value"]
        recovered.append({
            "ticker": ticker, "fiscal_end": str(fiscal_end),
            "available_date": str(available_date),
            "revenue": float(values["revenue"]),
            "net_income": float(values["net_income"]),
        })
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "accepted_quarter_count": len(recovered),
        "recovered_quarters": recovered,
        "raw_payloads": {
            ticker: {
                "path": str(path), "sha256": _sha256(path),
                "source_url": wrappers[ticker].get("source_url"),
                "fetched_at": wrappers[ticker].get("fetched_at"),
            }
            for ticker, path in archives.items()
        },
        "outputs": {"quarters": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "Only seven predeclared paired USD quarters are restored. ENSG "
            "Q1/Q2 retain their original filing-date values despite later "
            "discontinued-operations comparatives; ENSG Q4 is the audited "
            "annual value less the three original direct PIT quarters. SAIA "
            "Q4 and GOOD Q1/Q2/Q4 are direct facts. No later restatement is "
            "backdated and no concept substitution is allowed."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(cache_dir=args.cache_dir, output_dir=args.output_dir)
    print(json.dumps({
        "accepted_quarter_count": result["accepted_quarter_count"],
        "manifest": result["manifest"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
