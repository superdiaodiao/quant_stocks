#!/usr/bin/env python3
"""Recover NESR 2020/2019 direct annual TTM growth from its PIT 20-F."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path

import pandas as pd
import requests


OUTPUT_DIR = Path("output/research_only/v14/nesr_annual_growth")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260824_niu_doyu_bzun_clls_snapshot0715_audit.json"
)
SOURCE = {
    "url": (
        "https://www.sec.gov/Archives/edgar/data/1698514/"
        "000149315221006706/form20-f.htm"
    ),
    "accession": "0001493152-21-006706",
    "filed": "2021-03-24",
    "form": "20-F",
    "sha256": "eccfb7d490d23c6cd0ae9140ff8d11d525701d332249c371dde368babe75c7e4",
}
CURRENT_REVENUE_USD_THOUSANDS = 834_146
PRIOR_REVENUE_USD_THOUSANDS = 658_385
CURRENT_NET_INCOME_USD_THOUSANDS = 50_087
PRIOR_NET_INCOME_USD_THOUSANDS = 39_364
REVENUE_GROWTH = (
    CURRENT_REVENUE_USD_THOUSANDS - PRIOR_REVENUE_USD_THOUSANDS
) / abs(PRIOR_REVENUE_USD_THOUSANDS)
NET_INCOME_GROWTH = (
    CURRENT_NET_INCOME_USD_THOUSANDS - PRIOR_NET_INCOME_USD_THOUSANDS
) / abs(PRIOR_NET_INCOME_USD_THOUSANDS)
SIGNALS = ("2021-03-31", "2021-04-30", "2021-06-30")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _download(url: str) -> bytes:
    response = requests.get(
        url,
        headers={"User-Agent": "quant_stocks research contact@example.com"},
        timeout=120,
    )
    response.raise_for_status()
    return response.content


def _plain_text(payload: bytes) -> str:
    decoded = payload.decode("utf-8", errors="replace")
    decoded = re.sub(r"<script\b.*?</script>", " ", decoded, flags=re.I | re.S)
    decoded = re.sub(r"<style\b.*?</style>", " ", decoded, flags=re.I | re.S)
    decoded = re.sub(r"<[^>]+>", " ", decoded)
    return re.sub(r"\s+", " ", html.unescape(decoded)).strip()


def verify_source(payload: bytes, expected_sha256: str | None = None) -> dict:
    expected_sha256 = expected_sha256 or SOURCE["sha256"]
    actual = _sha256(payload)
    if actual != expected_sha256:
        raise ValueError(f"NESR 20-F SHA mismatch: {actual} != {expected_sha256}")
    text = _plain_text(payload)
    required = (
        "National Energy Services Reunited Corp. and Subsidiaries",
        "CONSOLIDATED STATEMENTS OF OPERATIONS",
        "In US$ thousands",
        "Successor (NESR)",
    )
    missing = [fragment for fragment in required if fragment not in text]
    if missing:
        raise ValueError(f"NESR audited-statement guards missing: {missing}")
    if re.search(r"Revenues\s+\$\s*834,146\s+\$\s*658,385", text) is None:
        raise ValueError("NESR 2020/2019 revenue-row guard failed")
    if re.search(
        r"Net income(?:\s*/\s*\(loss\))?\s+50,087\s+39,364", text
    ) is None:
        raise ValueError("NESR 2020/2019 net-income-row guard failed")
    return {
        **SOURCE,
        "bytes": len(payload),
        "currency": "USD",
        "scale": 1000,
        "accounting_standard": "US-GAAP",
        "period_comparability": (
            "Full calendar years 2020 and 2019, both Successor NESR periods"
        ),
        "operands_usd_thousands": {
            "current_revenue": CURRENT_REVENUE_USD_THOUSANDS,
            "prior_revenue": PRIOR_REVENUE_USD_THOUSANDS,
            "current_net_income": CURRENT_NET_INCOME_USD_THOUSANDS,
            "prior_net_income": PRIOR_NET_INCOME_USD_THOUSANDS,
        },
        "revenue_growth": REVENUE_GROWTH,
        "net_income_growth": NET_INCOME_GROWTH,
    }


def build(output_dir: Path = OUTPUT_DIR, audit_path: Path = AUDIT_PATH) -> dict:
    payload = _download(SOURCE["url"])
    evidence = verify_source(payload, SOURCE["sha256"])
    output_dir.mkdir(parents=True, exist_ok=True)
    source_path = output_dir / "source_0001493152-21-006706.html"
    source_path.write_bytes(payload)
    common = {
        "ticker": "NESR",
        "fiscal_end": "2020-12-31",
        "available_date": SOURCE["filed"],
        "taxonomy": "us-gaap",
        "form": SOURCE["form"],
        "accession": SOURCE["accession"],
        "fetched_at": "2026-08-24",
    }
    facts = pd.DataFrame([
        {**common, "metric": "revenue_ttm", "value": float(
            CURRENT_REVENUE_USD_THOUSANDS * 1000
        ), "concept": "StrictAnnualDirectGrowth:Revenues:USD"},
        {**common, "metric": "revenue_growth", "value": REVENUE_GROWTH,
         "concept": "StrictAnnualDirectGrowth:Revenues:USD"},
        {**common, "metric": "net_income_ttm", "value": float(
            CURRENT_NET_INCOME_USD_THOUSANDS * 1000
        ), "concept": "StrictAnnualDirectGrowth:NetIncomeLoss:USD"},
        {**common, "metric": "net_income_growth", "value": NET_INCOME_GROWTH,
         "concept": "StrictAnnualDirectGrowth:NetIncomeLoss:USD"},
    ])
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    resolutions = [{
        "ticker": "NESR",
        "signal_date": signal,
        "financial_age_days": int(
            (pd.Timestamp(signal) - pd.Timestamp(SOURCE["filed"])).days
        ),
        "classification": "PASS_DIRECT_TTM_GROWTH_AND_POSITIVE_NET_INCOME",
        "revenue_growth": REVENUE_GROWTH,
        "net_income_growth": NET_INCOME_GROWTH,
    } for signal in SIGNALS]
    resolution_path = output_dir / "resolved_observations.json"
    resolution_path.write_text(
        json.dumps(resolutions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "NESR",
        "cik": 1698514,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": False,
        "formal_financials_modified": False,
        "point_in_time_proven": True,
        "recovery_classification": "DIRECT_ANNUAL_TTM_GROWTH",
        "guardrail": (
            "The source compares complete 2020 and 2019 Successor NESR calendar "
            "years in USD under US-GAAP. Predecessor and partial 2018 periods, "
            "adjusted measures, and later filings are excluded."
        ),
        "evidence": evidence,
        "audit_binding": {
            "path": str(audit_path),
            "sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
            "missing_observation_count": len(SIGNALS),
            "signals": list(SIGNALS),
        },
        "outputs": {
            "source": {"path": str(source_path), "sha256": _sha256(payload)},
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            },
            "resolved_observations": {
                "path": str(resolution_path),
                "sha256": hashlib.sha256(resolution_path.read_bytes()).hexdigest(),
            },
        },
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest["manifest"] = str(manifest_path)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--audit-path", type=Path, default=AUDIT_PATH)
    args = parser.parse_args()
    result = build(output_dir=args.output_dir, audit_path=args.audit_path)
    print(json.dumps({
        "manifest": result["manifest"],
        "ticker": result["ticker"],
        "recovered_observations": result["audit_binding"][
            "missing_observation_count"
        ],
        "revenue_growth": result["evidence"]["revenue_growth"],
        "net_income_growth": result["evidence"]["net_income_growth"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
