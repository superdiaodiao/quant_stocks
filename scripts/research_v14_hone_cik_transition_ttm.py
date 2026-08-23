#!/usr/bin/env python3
"""Recover HONE 2019Q3 TTM growth across its 2019 SEC CIK transition.

HarborOne's second-step conversion created a new registrant.  The old CIK owns
the 2017/2018 annual and 2018Q3 facts while the new CIK owns 2019Q3.  This
research-only package binds the three original XBRL instances and derives one
strict, pre-signal bank-revenue/net-income TTM growth bundle.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


TICKER = "HONE"
OLD_CIK = 1_668_224
NEW_CIK = 1_769_617
FISCAL_END = "2019-09-30"
AVAILABLE_DATE = "2019-11-07"
FETCHED_AT = "2026-08-24"
OUTPUT_DIR = Path("output/research_only/v14/hone_cik_transition_ttm")
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

NET_INTEREST = "InterestIncomeExpenseNet"
NONINTEREST = "NoninterestIncome"
NET_INCOME = "NetIncomeLoss"

SOURCE_DOCUMENTS = {
    "old_q3_2018": {
        "cik": OLD_CIK,
        "form": "10-Q",
        "filed": "2018-11-09",
        "accession": "0001558370-18-009116",
        "document": "hone-20180930.xml",
        "local_path": "sources/hone-20180930.xml",
        "expected_sha256": "3906272a70dc5af384c0d7466b38f418a48b04720c17ae2778ea6be39878557a",
        "url": "https://www.sec.gov/Archives/edgar/data/1668224/000155837018009116/hone-20180930.xml",
    },
    "old_fy2018": {
        "cik": OLD_CIK,
        "form": "10-K",
        "filed": "2019-03-11",
        "accession": "0001558370-19-001744",
        "document": "hone-20181231.xml",
        "local_path": "sources/hone-20181231.xml",
        "expected_sha256": "b4b1e3e4b206dbde0ada35cb95d8be7e7ceaf9850534e8fb21e147c37798311f",
        "url": "https://www.sec.gov/Archives/edgar/data/1668224/000155837019001744/hone-20181231.xml",
    },
    "new_q3_2019": {
        "cik": NEW_CIK,
        "form": "10-Q",
        "filed": AVAILABLE_DATE,
        "accession": "0001558370-19-010298",
        "document": "hone-20190930.xml",
        "local_path": "sources/hone-20190930.xml",
        "expected_sha256": "bb38c8fbb0b37f196634531c772f5a078826816f74da22cb5066316738927da4",
        "url": "https://www.sec.gov/Archives/edgar/data/1769617/000155837019010298/hone-20190930.xml",
    },
}

# Each tuple is (start, end, exact USD value).  The new filing's 2018
# comparative is deliberately checked against the old filing before bridging.
SOURCE_EXPECTED_FACTS = {
    "old_q3_2018": {
        NET_INTEREST: (
            ("2017-01-01", "2017-09-30", 54_916_000),
            ("2018-01-01", "2018-09-30", 62_140_000),
        ),
        NONINTEREST: (
            ("2017-01-01", "2017-09-30", 40_380_000),
            ("2018-01-01", "2018-09-30", 37_546_000),
        ),
        NET_INCOME: (
            ("2017-01-01", "2017-09-30", 8_786_000),
            ("2018-01-01", "2018-09-30", 11_283_000),
        ),
    },
    "old_fy2018": {
        NET_INTEREST: (
            ("2017-01-01", "2017-12-31", 74_348_000),
            ("2018-01-01", "2018-12-31", 88_930_000),
        ),
        NONINTEREST: (
            ("2017-01-01", "2017-12-31", 54_534_000),
            ("2018-01-01", "2018-12-31", 49_198_000),
        ),
        NET_INCOME: (
            ("2017-01-01", "2017-12-31", 10_379_000),
            ("2018-01-01", "2018-12-31", 11_394_000),
        ),
    },
    "new_q3_2019": {
        NET_INTEREST: (
            ("2018-01-01", "2018-09-30", 62_140_000),
            ("2019-01-01", "2019-09-30", 80_728_000),
        ),
        NONINTEREST: (
            ("2018-01-01", "2018-09-30", 37_546_000),
            ("2019-01-01", "2019-09-30", 42_833_000),
        ),
        NET_INCOME: (
            ("2018-01-01", "2018-09-30", 11_283_000),
            ("2019-01-01", "2019-09-30", 13_961_000),
        ),
    },
}

AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", "2019-12-31", 54),
    ("liq2000000-age150-growth", "2020-01-31", 85),
)

REJECTED_LATER_FILINGS = {
    "0001558370-20-002624": {
        "form": "10-K",
        "filed": "2020-03-13",
        "reason": "post-signal annual filing; excluded from both HONE observations",
    }
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _download(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _duration_contexts(root: ET.Element) -> dict[str, tuple[str, str, bool]]:
    contexts = {}
    for node in root.iter():
        if _local_name(node.tag) != "context":
            continue
        start = end = None
        dimensional = False
        for child in node.iter():
            name = _local_name(child.tag)
            if name == "startDate":
                start = (child.text or "").strip()
            elif name == "endDate":
                end = (child.text or "").strip()
            elif name in {"segment", "explicitMember", "typedMember"}:
                dimensional = True
        if start and end:
            contexts[str(node.attrib["id"])] = (start, end, dimensional)
    return contexts


def parse_consolidated_usd_facts(raw: bytes) -> dict[tuple[str, str, str], int]:
    root = ET.fromstring(raw)
    contexts = _duration_contexts(root)
    usd_units = set()
    for node in root.iter():
        if _local_name(node.tag) != "unit":
            continue
        measures = [
            (child.text or "").strip().upper()
            for child in node.iter()
            if _local_name(child.tag) == "measure"
        ]
        if measures == ["ISO4217:USD"]:
            usd_units.add(str(node.attrib["id"]))
    found: dict[tuple[str, str, str], set[int]] = {}
    for node in root.iter():
        concept = _local_name(node.tag)
        if concept not in {NET_INTEREST, NONINTEREST, NET_INCOME}:
            continue
        context = contexts.get(str(node.attrib.get("contextRef", "")))
        if context is None or context[2] or node.attrib.get("unitRef") not in usd_units:
            continue
        value = int(Decimal((node.text or "").replace(",", "").strip()))
        key = (concept, context[0], context[1])
        found.setdefault(key, set()).add(value)
    ambiguous = {key: values for key, values in found.items() if len(values) != 1}
    if ambiguous:
        raise ValueError(f"ambiguous consolidated HONE facts: {ambiguous}")
    return {key: next(iter(values)) for key, values in found.items()}


def validate_source_bytes(source_id: str, raw: bytes) -> int:
    expected_hash = SOURCE_DOCUMENTS[source_id]["expected_sha256"]
    if _sha256(raw) != expected_hash:
        raise ValueError(f"HONE {source_id} source SHA256 mismatch")
    parsed = parse_consolidated_usd_facts(raw)
    checked = 0
    for concept, facts in SOURCE_EXPECTED_FACTS[source_id].items():
        for start, end, value in facts:
            actual = parsed.get((concept, start, end))
            if actual != value:
                raise ValueError(
                    f"HONE {source_id} {concept} {start}/{end}: "
                    f"expected {value}, got {actual}"
                )
            checked += 1
    return checked


def exact_ttm_evidence() -> dict:
    old_q3 = SOURCE_EXPECTED_FACTS["old_q3_2018"]
    annual = SOURCE_EXPECTED_FACTS["old_fy2018"]
    new_q3 = SOURCE_EXPECTED_FACTS["new_q3_2019"]

    def value(source: dict, concept: str, start: str) -> int:
        return next(item[2] for item in source[concept] if item[0] == start)

    # Confirm that the new registrant reproduced the old registrant's 9M2018
    # comparatives exactly for every accepted concept.
    matches = {}
    for concept in (NET_INTEREST, NONINTEREST, NET_INCOME):
        old = value(old_q3, concept, "2018-01-01")
        new = value(new_q3, concept, "2018-01-01")
        if old != new:
            raise ValueError(f"HONE CIK bridge comparative mismatch for {concept}")
        matches[concept] = old

    def ttm(concepts: tuple[str, ...], year: int) -> int:
        annual_start = f"{year - 1}-01-01"
        prior_9m_start = f"{year - 1}-01-01"
        current_9m_start = f"{year}-01-01"
        current_source = old_q3 if year == 2018 else new_q3
        return sum(
            value(annual, concept, annual_start)
            - value(old_q3, concept, prior_9m_start)
            + value(current_source, concept, current_9m_start)
            for concept in concepts
        )

    prior_revenue = ttm((NET_INTEREST, NONINTEREST), 2018)
    current_revenue = ttm((NET_INTEREST, NONINTEREST), 2019)
    prior_income = ttm((NET_INCOME,), 2018)
    current_income = ttm((NET_INCOME,), 2019)
    return {
        "prior_revenue_ttm_usd": prior_revenue,
        "current_revenue_ttm_usd": current_revenue,
        "revenue_growth": (current_revenue - prior_revenue) / abs(prior_revenue),
        "prior_net_income_ttm_usd": prior_income,
        "current_net_income_ttm_usd": current_income,
        "net_income_growth": (current_income - prior_income) / abs(prior_income),
        "comparative_matches": matches,
        "derivation": "FY - nine_month_prior_comparable + nine_month_current",
        "bank_revenue": f"{NET_INTEREST}+{NONINTEREST}",
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = exact_ttm_evidence()
    accession = "+".join(
        SOURCE_DOCUMENTS[source_id]["accession"]
        for source_id in ("old_q3_2018", "old_fy2018", "new_q3_2019")
    )
    values = {
        "revenue_ttm": evidence["current_revenue_ttm_usd"],
        "revenue_growth": evidence["revenue_growth"],
        "net_income_ttm": evidence["current_net_income_ttm_usd"],
        "net_income_growth": evidence["net_income_growth"],
    }
    rows = [{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": metric,
        "value": value,
        "taxonomy": "us-gaap",
        "concept": f"derived_cik_transition_exact_ttm:{metric}:USD",
        "form": "10-Q+10-K+10-Q",
        "accession": accession,
        "fetched_at": FETCHED_AT,
    } for metric, value in values.items()]
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def build(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    verified = []
    for source_id, source in SOURCE_DOCUMENTS.items():
        path = output_dir / source["local_path"]
        if not path.exists():
            raw = _download(str(source["url"]))
            if _sha256(raw) != source["expected_sha256"]:
                raise ValueError(f"HONE {source_id} downloaded source SHA256 mismatch")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        raw = path.read_bytes()
        checked = validate_source_bytes(source_id, raw)
        verified.append({
            **source,
            "path": str(path),
            "actual_sha256": _sha256(raw),
            "verified_fact_count": checked,
        })

    facts = strict_quarterly_facts()
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "ticker": TICKER,
        "old_cik": OLD_CIK,
        "new_cik": NEW_CIK,
        "accepted_direct_growth_package_count": 1,
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": len(AUDIT_OBSERVATIONS),
        "audit_observations": [
            {"scenario": scenario, "signal_date": date, "financial_age_days": age}
            for scenario, date, age in AUDIT_OBSERVATIONS
        ],
        "exact_ttm_evidence": exact_ttm_evidence(),
        "sources": verified,
        "rejected_later_filings": REJECTED_LATER_FILINGS,
        "outputs": {
            "facts": {"path": str(facts_path), "sha256": _sha256(facts_path.read_bytes())}
        },
        "guardrail": (
            "The bridge is allowed only because all three 9M2018 consolidated "
            "comparatives match exactly across the old and new registrants. "
            "No post-signal annual filing, FX conversion, or adjusted metric is used."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    result = build(args.output_dir)
    print(json.dumps({
        "manifest": result["manifest"],
        "accepted_fact_count": result["accepted_fact_count"],
        "resolved_audit_observation_count": result["resolved_audit_observation_count"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
