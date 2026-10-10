#!/usr/bin/env python3
"""Recover CLLS's 2020H1 PIT TTM loss from SHA-locked SEC filings."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path

import pandas as pd
import requests


OUTPUT_DIR = Path("output/research_only/v14/clls_direct_ttm_loss")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260824_niu_doyu_snapshot0715_audit.json"
)
SOURCES = (
    {
        "role": "FY2019 audited IFRS consolidated ProfitLoss",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1627281/"
            "000119312520061833/d842992d20f.htm"
        ),
        "accession": "0001193125-20-061833",
        "filed": "2020-03-05",
        "form": "20-F",
        "sha256": "c265b844b3787e24a35f3d42a83fe7ac0c7166ff2f21aa45c766a5d166bc2ee0",
    },
    {
        "role": "2020H1 and comparative 2019H1 IFRS consolidated ProfitLoss",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1627281/"
            "000117184320005556/exh_991.htm"
        ),
        "accession": "0001171843-20-005556",
        "filed": "2020-08-05",
        "form": "6-K:EX-99.1",
        "sha256": "fc43274d3aa60be90a443e3b3e226b48440d133f0d2b454c91fefc614bc9735c",
    },
)
SIGNALS = ("2020-08-31", "2020-09-30", "2020-11-30", "2020-12-31")
FY2019_NET_LOSS_USD_THOUSANDS = -115_212
H1_2019_NET_LOSS_USD_THOUSANDS = -54_461
H1_2020_NET_LOSS_USD_THOUSANDS = -19_290
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
    if len(payloads) != len(SOURCES):
        raise ValueError(f"Expected {len(SOURCES)} SEC payloads, got {len(payloads)}")
    verified = []
    for source, payload in zip(SOURCES, payloads, strict=True):
        actual = _sha256(payload)
        if actual != source["sha256"]:
            raise ValueError(
                f"{source['accession']} SHA mismatch: {actual} != {source['sha256']}"
            )
        verified.append({**source, "bytes": len(payload)})
    annual_text, half_year_text = map(_plain_text, payloads)
    annual_guard = re.search(
        r"Net income \(loss\)\s+\(103,683\s*\)\s+\(88,333\s*\)\s+\(115,212\s*\)",
        annual_text,
    )
    half_year_guard = re.search(
        r"Net income \(loss\)\s+\(54,461\s*\)\s+\(19,290\s*\)",
        half_year_text,
    )
    if annual_guard is None:
        raise ValueError("FY2019 consolidated IFRS net-loss table guard failed")
    if half_year_guard is None:
        raise ValueError("2020H1 comparative consolidated IFRS net-loss guard failed")
    if "Adjusted Net Income (Loss)" not in half_year_text:
        raise ValueError("Non-GAAP exclusion guard is absent")
    if TTM_NET_LOSS_USD_THOUSANDS != -80_041:
        raise AssertionError("CLLS direct TTM arithmetic changed")
    return {
        "sources": verified,
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
    source_dir = output_dir / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_outputs = []
    for source, payload in zip(SOURCES, payloads, strict=True):
        path = source_dir / f"{source['accession']}.html"
        path.write_bytes(payload)
        source_outputs.append({"path": str(path), "sha256": _sha256(payload)})

    facts = pd.DataFrame([{
        "ticker": "CLLS",
        "fiscal_end": "2020-06-30",
        "available_date": "2020-08-05",
        "metric": "net_income_ttm",
        "value": float(TTM_NET_LOSS_USD_THOUSANDS * 1000),
        "taxonomy": "ifrs-full",
        "concept": "StrictDirectTTM:ProfitLoss:USD",
        "form": "20-F_PLUS_6-K_H1_DIRECT_TTM",
        "accession": "0001193125-20-061833+0001171843-20-005556",
        "fetched_at": "2026-08-24",
    }])
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    audit_sha = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    resolutions = [{
        "ticker": "CLLS",
        "signal_date": signal,
        "financial_age_days": int(
            (pd.Timestamp(signal) - pd.Timestamp("2020-08-05")).days
        ),
        "classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "net_income_ttm": float(TTM_NET_LOSS_USD_THOUSANDS * 1000),
    } for signal in SIGNALS]
    resolutions_path = output_dir / "resolved_observations.json"
    resolutions_path.write_text(
        json.dumps(resolutions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "CLLS",
        "cik": 1627281,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": False,
        "formal_financials_modified": False,
        "point_in_time_proven": True,
        "recovery_classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "guardrail": (
            "Only the consolidated IFRS ProfitLoss line is used. The adjusted/non-GAAP "
            "loss and the narrower amount attributable to Cellectis shareholders are excluded."
        ),
        "evidence": evidence,
        "audit_binding": {
            "path": str(audit_path),
            "sha256": audit_sha,
            "missing_observation_count": len(SIGNALS),
            "signals": list(SIGNALS),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            },
            "resolved_observations": {
                "path": str(resolutions_path),
                "sha256": hashlib.sha256(resolutions_path.read_bytes()).hexdigest(),
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
        "classification": result["recovery_classification"],
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
