#!/usr/bin/env python3
"""Recover EVGN's 2020Q3 direct TTM loss from a SHA-locked SEC 6-K."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path

import pandas as pd
import requests


OUTPUT_DIR = Path("output/research_only/v14/evgn_direct_ttm_loss")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260824_niu_doyu_bzun_clls_bctx_nesr_snapshot0715_audit.json"
)
SOURCE = {
    "url": (
        "https://www.sec.gov/Archives/edgar/data/1574565/"
        "000117891320003196/exhibit_99-1.htm"
    ),
    "accession": "0001178913-20-003196",
    "filed": "2020-11-18",
    "form": "6-K:EX-99.1",
    "sha256": "fc5aaae2a5d83f531ec1218dae1c0b00312de46feab35c621aa6314b99509b0c",
}
FY2019_LOSS_USD_THOUSANDS = -19_115
NINE_MONTH_2019_LOSS_USD_THOUSANDS = -12_423
NINE_MONTH_2020_LOSS_USD_THOUSANDS = -17_443
TTM_LOSS_USD_THOUSANDS = (
    FY2019_LOSS_USD_THOUSANDS
    - NINE_MONTH_2019_LOSS_USD_THOUSANDS
    + NINE_MONTH_2020_LOSS_USD_THOUSANDS
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


def verify_source(payload: bytes, expected_sha256: str | None = None) -> dict:
    expected_sha256 = expected_sha256 or SOURCE["sha256"]
    actual = _sha256(payload)
    if actual != expected_sha256:
        raise ValueError(f"EVGN Q3 source SHA mismatch: {actual} != {expected_sha256}")
    text = _plain_text(payload)
    required = (
        "CONDENSED CONSOLIDATED INTERIM STATEMENTS OF PROFIT OR LOSS",
        "U.S. dollars in thousands",
        "Nine months ended September 30",
        "Unaudited Audited",
    )
    missing = [fragment for fragment in required if fragment not in text]
    if missing:
        raise ValueError(f"EVGN statement guards missing: {missing}")
    if re.search(
        r"Loss\s+\$\s*\(17,443\s*\)\s+\$\s*\(12,423\s*\)\s+"
        r"\$\s*\(5,411\s*\)\s+\$\s*\(4,528\s*\)\s+\$\s*\(19,115\s*\)",
        text,
    ) is None:
        raise ValueError("EVGN 9M/FY comparative loss-row guard failed")
    if TTM_LOSS_USD_THOUSANDS != -24_135:
        raise AssertionError("EVGN direct TTM arithmetic changed")
    return {
        **SOURCE,
        "bytes": len(payload),
        "currency": "USD",
        "scale": 1000,
        "accounting_standard": "IFRS",
        "operands_usd_thousands": {
            "fy2019": FY2019_LOSS_USD_THOUSANDS,
            "nine_month_2019": NINE_MONTH_2019_LOSS_USD_THOUSANDS,
            "nine_month_2020": NINE_MONTH_2020_LOSS_USD_THOUSANDS,
        },
        "formula": "FY2019 - 9M2019 + 9M2020",
        "net_income_ttm_usd_thousands": TTM_LOSS_USD_THOUSANDS,
    }


def build(output_dir: Path = OUTPUT_DIR, audit_path: Path = AUDIT_PATH) -> dict:
    payload = _download(SOURCE["url"])
    evidence = verify_source(payload, SOURCE["sha256"])
    output_dir.mkdir(parents=True, exist_ok=True)
    source_path = output_dir / "source_0001178913-20-003196.html"
    source_path.write_bytes(payload)
    facts = pd.DataFrame([{
        "ticker": "EVGN",
        "fiscal_end": "2020-09-30",
        "available_date": SOURCE["filed"],
        "metric": "net_income_ttm",
        "value": float(TTM_LOSS_USD_THOUSANDS * 1000),
        "taxonomy": "ifrs-full",
        "concept": "StrictDirectTTM:ProfitLoss:USD",
        "form": "6-K_9M_DIRECT_TTM",
        "accession": SOURCE["accession"],
        "fetched_at": "2026-08-24",
    }])
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    manifest = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "EVGN",
        "cik": 1574565,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": False,
        "formal_financials_modified": False,
        "point_in_time_proven": True,
        "recovery_classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "guardrail": (
            "The single pre-signal 6-K contains audited FY2019 and unaudited "
            "comparable 9M2019/9M2020 IFRS losses in USD. The exact TTM identity "
            "uses the consolidated Loss line, not attributable or adjusted values."
        ),
        "evidence": evidence,
        "audit_binding": {
            "path": str(audit_path),
            "sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
            "missing_observation_count": 1,
            "signal": "2021-01-29",
            "financial_age_days": 72,
        },
        "outputs": {
            "source": {"path": str(source_path), "sha256": _sha256(payload)},
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
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
