#!/usr/bin/env python3
"""Bridge BCTX's SHA-locked FY2021 IFRS loss into the direct TTM path."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path

import pandas as pd
import requests


OUTPUT_DIR = Path("output/research_only/v14/bctx_annual_ttm_loss")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260824_niu_doyu_bzun_snapshot0715_audit.json"
)
SOURCE = {
    "url": (
        "https://www.sec.gov/Archives/edgar/data/1610820/"
        "000149315221028949/ex99-1.htm"
    ),
    "accession": "0001493152-21-028949",
    "filed": "2021-11-16",
    "form": "40-F/A:EX-99.1",
    "sha256": "8cb360a2233461ec95b639e933545d96ec8146883871f4417992adf651663ce4",
}
NET_LOSS_USD = -428_334


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
        raise ValueError(f"BCTX FY2021 source SHA mismatch: {actual} != {expected_sha256}")
    text = _plain_text(payload)
    required = (
        "Consolidated Statements of Operations and Comprehensive Loss",
        "Expressed in US Dollars",
        "International Financial Reporting Standards",
    )
    missing = [fragment for fragment in required if fragment not in text]
    if missing:
        raise ValueError(f"BCTX audited-statement guards missing: {missing}")
    if re.search(
        r"Loss for the Year\s+\(\s*428,334\s*\)\s+"
        r"\(\s*4,024,536\s*\)\s+\(\s*4,712,789\s*\)",
        text,
    ) is None:
        raise ValueError("BCTX FY2021 IFRS loss-row guard failed")
    return {
        **SOURCE,
        "bytes": len(payload),
        "profit_semantics": "IFRS consolidated Loss for the Year",
        "currency": "USD",
        "scale": 1,
        "net_income_ttm": NET_LOSS_USD,
    }


def build(output_dir: Path = OUTPUT_DIR, audit_path: Path = AUDIT_PATH) -> dict:
    payload = _download(SOURCE["url"])
    evidence = verify_source(payload, SOURCE["sha256"])
    output_dir.mkdir(parents=True, exist_ok=True)
    source_path = output_dir / "source_0001493152-21-028949.html"
    source_path.write_bytes(payload)
    facts = pd.DataFrame([{
        "ticker": "BCTX",
        "fiscal_end": "2021-07-31",
        "available_date": "2021-11-16",
        "metric": "net_income_ttm",
        "value": float(NET_LOSS_USD),
        "taxonomy": "ifrs-full",
        "concept": "StrictAnnualTTM:ProfitLoss:USD",
        "form": SOURCE["form"],
        "accession": SOURCE["accession"],
        "fetched_at": "2026-08-24",
    }])
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    resolution = {
        "ticker": "BCTX",
        "signal_date": "2021-11-30",
        "financial_age_days": 14,
        "classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "net_income_ttm": NET_LOSS_USD,
        "unresolved_pre_filing_signal": "2021-08-31",
        "unresolved_reason": (
            "The FY2021 audited filing was not public until 2021-11-16; no later "
            "statement may be projected backward into the August signal."
        ),
    }
    resolution_path = output_dir / "resolved_observation.json"
    resolution_path.write_text(
        json.dumps(resolution, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "BCTX",
        "cik": 1610820,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": False,
        "formal_financials_modified": False,
        "point_in_time_proven": True,
        "recovery_classification": "PARTIAL_KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "guardrail": (
            "Only the audited IFRS consolidated loss is emitted. The filing is "
            "available to 2021-11-30 and is never projected back to 2021-08-31."
        ),
        "evidence": evidence,
        "audit_binding": {
            "path": str(audit_path),
            "sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
            "baseline_missing_observation_count": 2,
            "recovered_observation_count": 1,
            "unresolved_observation_count": 1,
        },
        "outputs": {
            "source": {"path": str(source_path), "sha256": _sha256(payload)},
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            },
            "resolved_observation": {
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
            "recovered_observation_count"
        ],
        "unresolved_observations": result["audit_binding"][
            "unresolved_observation_count"
        ],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
