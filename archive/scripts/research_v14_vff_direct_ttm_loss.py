#!/usr/bin/env python3
"""Recover VFF's pre-signal 2020Q3 direct TTM net loss from SEC XBRL."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v14_hone_cik_transition_ttm import (
    NET_INCOME,
    parse_consolidated_usd_facts,
)


TICKER = "VFF"
CIK = 1_584_549
OUTPUT_DIR = Path("output/research_only/v14/vff_direct_ttm_loss")
DEFAULT_AUDIT = Path(
    "output/research_only/v14/checkpoint_20260824_hone_sibn_knsa_hcat_wprt_final.json"
)
FISCAL_END = "2020-09-30"
AVAILABLE_DATE = "2020-11-13"
SIGNAL = "2020-11-30"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

SOURCES = {
    "fy2019": {
        "url": "https://www.sec.gov/Archives/edgar/data/1584549/000119312520094235/vff-20191231.xml",
        "local_path": "sources/vff-20191231.xml",
        "sha256": "835a2149d1c913db18f545da609ee24ea6db4965fe529400919a3cba9daaeaf4",
        "form": "10-K", "filed": "2020-04-01",
        "accession": "0001193125-20-094235",
        "facts": (("2019-01-01", "2019-12-31", 2_325_000),),
    },
    "q3_2020": {
        "url": "https://www.sec.gov/Archives/edgar/data/1584549/000156459020053721/vff-20200930.xml",
        "local_path": "sources/vff-20200930.xml",
        "sha256": "eca5b99f617c884dad207a13ba08eb4b6c6439966675cd4a7cd87a1052648163",
        "form": "10-Q", "filed": AVAILABLE_DATE,
        "accession": "0001564590-20-053721",
        "facts": (
            ("2019-01-01", "2019-09-30", 9_509_000),
            ("2020-01-01", "2020-09-30", 4_591_000),
        ),
    },
}

TTM_NET_INCOME = 2_325_000 - 9_509_000 + 4_591_000

REJECTED_LATER_FILINGS = {
    "0001564590-21-013320": {
        "form": "10-K", "filed": "2021-03-16",
        "reason": "post-signal FY2020 annual filing; excluded",
    }
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _download(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def verify_source(source_id: str, raw: bytes) -> int:
    source = SOURCES[source_id]
    if _sha256(raw) != source["sha256"]:
        raise ValueError(f"VFF {source_id} source SHA256 mismatch")
    parsed = parse_consolidated_usd_facts(raw)
    for start, end, expected in source["facts"]:
        actual = parsed.get((NET_INCOME, start, end))
        if actual != expected:
            raise ValueError(
                f"VFF {source_id} NetIncomeLoss {start}/{end}: "
                f"expected {expected}, got {actual}"
            )
    return len(source["facts"])


def strict_quarterly_facts() -> pd.DataFrame:
    return pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": "net_income_ttm",
        "value": float(TTM_NET_INCOME),
        "taxonomy": "us-gaap",
        "concept": "StrictDirectTTM:NetIncomeLoss:USD",
        "form": "10-K_PLUS_10-Q_DIRECT_TTM",
        "accession": "+".join(source["accession"] for source in SOURCES.values()),
        "fetched_at": "2026-08-24",
    }])


def build(output_dir: Path = OUTPUT_DIR, audit_path: Path = DEFAULT_AUDIT) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    verified = []
    for source_id, source in SOURCES.items():
        path = output_dir / source["local_path"]
        if not path.exists():
            raw = _download(source["url"])
            if _sha256(raw) != source["sha256"]:
                raise ValueError(f"VFF {source_id} downloaded source SHA256 mismatch")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        raw = path.read_bytes()
        verified.append({
            **source, "path": str(path), "verified_fact_count": verify_source(source_id, raw)
        })
    if TTM_NET_INCOME != -2_593_000:
        raise AssertionError("VFF direct TTM arithmetic changed")
    facts_path = output_dir / "strict_quarterly_facts.csv"
    strict_quarterly_facts().to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "ticker": TICKER,
        "cik": CIK,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": False,
        "formal_financials_modified": False,
        "point_in_time_proven": True,
        "recovery_classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "evidence": {
            "formula": "FY2019 - 9M2019 + 9M2020",
            "net_income_ttm_usd": TTM_NET_INCOME,
            "sources": verified,
        },
        "audit_binding": {
            "path": str(audit_path), "sha256": _sha256(audit_path.read_bytes()),
            "missing_observation_count": 1, "signals": [SIGNAL],
            "scenarios": ["liq2000000-age150-growth"],
        },
        "rejected_later_filings": REJECTED_LATER_FILINGS,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path.read_bytes())
            }
        },
        "guardrail": (
            "Only consolidated USD US-GAAP NetIncomeLoss facts from the original "
            "FY2019 and 2020Q3 XBRL instances are used. The post-signal FY2020 "
            "annual filing is excluded."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    args = parser.parse_args()
    result = build(args.output_dir, args.audit)
    print(json.dumps({
        "manifest": result["manifest"],
        "net_income_ttm_usd": result["evidence"]["net_income_ttm_usd"],
        "recovered_observations": 1,
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
