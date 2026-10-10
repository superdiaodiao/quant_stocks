#!/usr/bin/env python3
"""Recover CGEN's 2020H1 direct TTM loss from two SHA-locked SEC reports."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path

import pandas as pd
import requests


OUTPUT_DIR = Path("output/research_only/v14/cgen_direct_ttm_loss")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260824_niu_doyu_bzun_clls_bctx_nesr_snapshot0715_audit.json"
)
SOURCES = (
    {
        "url": "https://www.sec.gov/Archives/edgar/data/1119774/000117891320000625/R4.htm",
        "accession": "0001178913-20-000625",
        "filed": "2020-02-24",
        "form": "20-F:XBRL-STATEMENT",
        "sha256": "d71e8bfedf78a2c16add8488db9f4bb421d8555ac75b084ff1d4797cb00295f9",
    },
    {
        "url": "https://www.sec.gov/Archives/edgar/data/1119774/000117891320002175/R4.htm",
        "accession": "0001178913-20-002175",
        "filed": "2020-07-30",
        "form": "6-K:XBRL-STATEMENT",
        "sha256": "b7a10858478398e71ed2f4d92984cd11d624b67d0db7fb370dfbf5dc923a2f0c",
    },
)
FY2019_NET_LOSS_USD_THOUSANDS = -27_337
H1_2019_NET_LOSS_USD_THOUSANDS = -14_385
H1_2020_NET_LOSS_USD_THOUSANDS = -13_374
TTM_NET_LOSS_USD_THOUSANDS = (
    FY2019_NET_LOSS_USD_THOUSANDS
    - H1_2019_NET_LOSS_USD_THOUSANDS
    + H1_2020_NET_LOSS_USD_THOUSANDS
)


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


def verify_sources(payloads: list[bytes]) -> dict:
    if len(payloads) != 2:
        raise ValueError(f"Expected two CGEN SEC statements, got {len(payloads)}")
    for source, payload in zip(SOURCES, payloads, strict=True):
        actual = _sha256(payload)
        if actual != source["sha256"]:
            raise ValueError(
                f"{source['accession']} SHA mismatch: {actual} != {source['sha256']}"
            )
    annual, half = map(_plain_text, payloads)
    if "CONSOLIDATED STATEMENTS OF COMPREHENSIVE LOSS" not in annual:
        raise ValueError("CGEN annual statement-title guard failed")
    if re.search(
        r"Net loss\s+\$\s*\(27,337\)\s+\$\s*\(22,599\)\s+\$\s*\(37,066\)",
        annual,
    ) is None:
        raise ValueError("CGEN FY2019 annual-loss guard failed")
    if "CONSOLIDATED STATEMENTS OF COMPREHENSIVE LOSS (Unaudited)" not in half:
        raise ValueError("CGEN H1 statement-title guard failed")
    if re.search(r"Net loss\s+\$\s*13,374\s+\$\s*14,385", half) is None:
        raise ValueError("CGEN H1 2020/2019 comparative-loss guard failed")
    if TTM_NET_LOSS_USD_THOUSANDS != -26_326:
        raise AssertionError("CGEN direct TTM arithmetic changed")
    return {
        "sources": [{**source, "bytes": len(payload)} for source, payload in zip(
            SOURCES, payloads, strict=True
        )],
        "currency": "USD",
        "scale": 1000,
        "accounting_standard": "US-GAAP",
        "operands_usd_thousands": {
            "fy2019": FY2019_NET_LOSS_USD_THOUSANDS,
            "h1_2019": H1_2019_NET_LOSS_USD_THOUSANDS,
            "h1_2020": H1_2020_NET_LOSS_USD_THOUSANDS,
        },
        "formula": "FY2019 - H1_2019 + H1_2020",
        "net_income_ttm_usd_thousands": TTM_NET_LOSS_USD_THOUSANDS,
    }


def build(output_dir: Path = OUTPUT_DIR, audit_path: Path = AUDIT_PATH) -> dict:
    payloads = [_download(source["url"]) for source in SOURCES]
    evidence = verify_sources(payloads)
    output_dir.mkdir(parents=True, exist_ok=True)
    source_outputs = []
    for source, payload in zip(SOURCES, payloads, strict=True):
        path = output_dir / f"source_{source['accession']}.html"
        path.write_bytes(payload)
        source_outputs.append({"path": str(path), "sha256": _sha256(payload)})
    facts = pd.DataFrame([{
        "ticker": "CGEN",
        "fiscal_end": "2020-06-30",
        "available_date": "2020-07-30",
        "metric": "net_income_ttm",
        "value": float(TTM_NET_LOSS_USD_THOUSANDS * 1000),
        "taxonomy": "us-gaap",
        "concept": "StrictDirectTTM:NetIncomeLoss:USD",
        "form": "20-F_PLUS_6-K_H1_DIRECT_TTM",
        "accession": "0001178913-20-000625+0001178913-20-002175",
        "fetched_at": "2026-08-24",
    }])
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    manifest = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "CGEN",
        "cik": 1119774,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": False,
        "formal_financials_modified": False,
        "point_in_time_proven": True,
        "recovery_classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "guardrail": (
            "Only SEC-rendered US-GAAP net-loss statements are used. The exact "
            "FY-minus-comparative-H1-plus-current-H1 identity is available before "
            "the 2020-08-31 signal; later filings and estimates are excluded."
        ),
        "evidence": evidence,
        "audit_binding": {
            "path": str(audit_path),
            "sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
            "missing_observation_count": 1,
            "signal": "2020-08-31",
            "financial_age_days": 32,
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            },
            "sources": source_outputs,
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
        "net_income_ttm_usd_thousands": result["evidence"][
            "net_income_ttm_usd_thousands"
        ],
        "recovered_observations": result["audit_binding"][
            "missing_observation_count"
        ],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
