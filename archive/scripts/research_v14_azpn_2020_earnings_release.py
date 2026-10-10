#!/usr/bin/env python3
"""Recover AZPN's FY2020 exact growth from its pre-signal SEC 8-K exhibit."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/azpn_2020_earnings_release")
SOURCE_URL = (
    "https://www.sec.gov/Archives/edgar/data/929940/"
    "000092994020000036/a081220208-kxexhibit991.htm"
)
SOURCE_SHA256 = "c8c898b74692c24adcfede7f0dd6a24a06112fbf00b6189c272e9ac62d189063"
ACCESSION = "0000929940-20-000036"
AVAILABLE_DATE = "2020-08-12"
FISCAL_END = "2020-06-30"
SIGNAL_DATE = "2020-11-30"
MAXIMUM_AGE_DAYS = 150
CURRENT_REVENUE = 590_181_000
PRIOR_REVENUE = 598_345_000
CURRENT_NET_INCOME = 225_708_000
PRIOR_NET_INCOME = 262_734_000
SOURCE_FRAGMENTS = (
    "Total revenue 199,331 195,769 590,181 598,345",
    "Net income $ 97,628 $ 103,865 $ 225,708 $ 262,734",
    "fiscal year ended June 30, 2020",
    "unaudited",
)
AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", SIGNAL_DATE, MAXIMUM_AGE_DAYS),
    ("liq10000000-age150-growth", SIGNAL_DATE, MAXIMUM_AGE_DAYS),
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source() -> bytes:
    request = Request(
        SOURCE_URL,
        headers={"User-Agent": "quant-stocks-research research@example.com"},
    )
    with urlopen(request, timeout=120) as response:
        return response.read()


def _normalized_source_text(raw: bytes) -> str:
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def verify_source(raw: bytes) -> dict:
    actual = _sha256_bytes(raw)
    if actual != SOURCE_SHA256:
        raise RuntimeError(f"AZPN SEC exhibit SHA-256 mismatch: {actual}")
    normalized = _normalized_source_text(raw)
    missing = [fragment for fragment in SOURCE_FRAGMENTS if fragment.lower() not in normalized.lower()]
    if missing:
        raise RuntimeError(f"AZPN SEC exhibit fragments changed: {missing}")
    if "Non-GAAP net income" not in normalized:
        raise RuntimeError("AZPN non-GAAP label guard disappeared")
    return {
        "url": SOURCE_URL,
        "sha256": actual,
        "accession": ACCESSION,
        "form": "8-K/EX-99.1",
        "filed": AVAILABLE_DATE,
        "verified_fragments": list(SOURCE_FRAGMENTS),
        "guardrail": "Accepted values are the GAAP total revenue and net income rows, not non-GAAP rows.",
    }


def exact_growth_evidence() -> dict:
    revenue_growth = CURRENT_REVENUE / PRIOR_REVENUE - 1.0
    net_income_growth = CURRENT_NET_INCOME / PRIOR_NET_INCOME - 1.0
    if revenue_growth >= 0 or net_income_growth >= 0:
        raise RuntimeError("AZPN FY2020 exact growth sign changed")
    return {
        "ticker": "AZPN",
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "currency": "USD",
        "scale": 1_000,
        "accounting_standard": "US-GAAP",
        "current_revenue_ttm": CURRENT_REVENUE,
        "prior_revenue_ttm": PRIOR_REVENUE,
        "revenue_growth": revenue_growth,
        "current_net_income_ttm": CURRENT_NET_INCOME,
        "prior_net_income_ttm": PRIOR_NET_INCOME,
        "net_income_growth": net_income_growth,
        "decision": "FAIL_REVENUE_AND_NET_INCOME_GROWTH",
    }


def direct_growth_facts(fetched_at: str) -> pd.DataFrame:
    evidence = exact_growth_evidence()
    metrics = {
        "revenue_ttm": evidence["current_revenue_ttm"],
        "revenue_growth": evidence["revenue_growth"],
        "net_income_ttm": evidence["current_net_income_ttm"],
        "net_income_growth": evidence["net_income_growth"],
    }
    return pd.DataFrame([{
        "ticker": "AZPN",
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": metric,
        "value": value,
        "taxonomy": "us-gaap",
        "concept": f"azpn_8k_exact_annual:{metric}:USD",
        "form": "8-K_EX-99.1_EXACT_ANNUAL",
        "accession": ACCESSION,
        "fetched_at": pd.Timestamp(fetched_at).tz_localize(None).normalize(),
    } for metric, value in metrics.items()], columns=OUTPUT_COLUMNS)


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    source_path = output_dir / "sources" / "a081220208-kxexhibit991.htm"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path.exists():
        raw = source_path.read_bytes()
    else:
        raw = _download_source()
        if _sha256_bytes(raw) != SOURCE_SHA256:
            raise RuntimeError("AZPN downloaded SEC exhibit failed its SHA lock")
        source_path.write_bytes(raw)
    source = verify_source(raw)
    evidence = exact_growth_evidence()
    facts = direct_growth_facts(
        pd.Timestamp.now("UTC").tz_localize(None).normalize().isoformat()
    )
    age = int((pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)).days)
    if age != 110 or age > MAXIMUM_AGE_DAYS:
        raise RuntimeError("AZPN exact annual bundle is outside the age150 window")
    resolutions = [{
        "scenario": scenario,
        "signal_date": signal,
        "maximum_age_days": maximum_age,
        "financial_age_days": age,
        "resolved": True,
        "decision": evidence["decision"],
    } for scenario, signal, maximum_age in AUDIT_OBSERVATIONS]

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_growth_evidence.json"
    resolutions_path = output_dir / "audit_observation_resolution.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    resolutions_path.write_text(json.dumps(resolutions, indent=2, sort_keys=True) + "\n")
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": "AZPN",
        "source": {**source, "local_path": str(source_path)},
        "accepted_direct_growth_fact_count": len(facts),
        "resolved_audit_observation_count": len(resolutions),
        "outputs": {
            "strict_quarterly_facts": {"path": str(facts_path), "sha256": _sha256_path(facts_path)},
            "exact_growth_evidence": {"path": str(evidence_path), "sha256": _sha256_path(evidence_path)},
            "audit_observation_resolution": {"path": str(resolutions_path), "sha256": _sha256_path(resolutions_path)},
        },
        "guardrail": (
            "Uses the exact GAAP annual comparison in the pre-signal SEC 8-K exhibit. "
            "The delayed 2020 10-K and 2021Q1 10-Q were not used; non-GAAP rows and "
            "per-share measures were excluded."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = run(args.output_dir)
    print(json.dumps({
        "manifest": report["manifest"],
        "accepted_direct_growth_fact_count": report["accepted_direct_growth_fact_count"],
        "resolved_audit_observation_count": report["resolved_audit_observation_count"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
