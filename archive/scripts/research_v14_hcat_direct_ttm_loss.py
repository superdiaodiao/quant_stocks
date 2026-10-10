#!/usr/bin/env python3
"""Recover HCAT's pre-signal 2020Q3 direct TTM net loss from SEC XBRL."""

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


TICKER = "HCAT"
CIK = 1_636_422
OUTPUT_DIR = Path("output/research_only/v14/hcat_direct_ttm_loss")
DEFAULT_AUDIT = Path(
    "output/research_only/v14/checkpoint_20260824_hone_sibn_knsa_final.json"
)
FISCAL_END = "2020-09-30"
AVAILABLE_DATE = "2020-11-10"
SIGNAL = "2021-01-29"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

SOURCES = {
    "fy2019": {
        "url": "https://www.sec.gov/Archives/edgar/data/1636422/000163642220000016/hcat-20191231x10k_htm.xml",
        "local_path": "sources/hcat-20191231x10k_htm.xml",
        "sha256": "9a71fe457c405c1a9e7e4df874ead6e38814c38094424f71195365a42786a7a8",
        "form": "10-K", "filed": "2020-02-28",
        "accession": "0001636422-20-000016",
        "facts": (("2019-01-01", "2019-12-31", -60_096_000),),
    },
    "q3_2020": {
        "url": "https://www.sec.gov/Archives/edgar/data/1636422/000163642220000084/hcat-20200930_htm.xml",
        "local_path": "sources/hcat-20200930_htm.xml",
        "sha256": "aacac0dde78096daf03ee6259b1107e0e369a93b5d450991eb738d5ec8e85a5c",
        "form": "10-Q", "filed": AVAILABLE_DATE,
        "accession": "0001636422-20-000084",
        "facts": (
            ("2019-01-01", "2019-09-30", -45_830_000),
            ("2020-01-01", "2020-09-30", -71_999_000),
        ),
    },
}

TTM_NET_INCOME = -60_096_000 - (-45_830_000) + (-71_999_000)

REJECTED_LATER_FILINGS = {
    "0001636422-21-000026": {
        "form": "10-K", "filed": "2021-02-25",
        "reason": "post-signal FY2020 annual report; excluded",
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
        raise ValueError(f"HCAT {source_id} source SHA256 mismatch")
    parsed = parse_consolidated_usd_facts(raw)
    for start, end, expected in source["facts"]:
        actual = parsed.get((NET_INCOME, start, end))
        if actual != expected:
            raise ValueError(
                f"HCAT {source_id} NetIncomeLoss {start}/{end}: "
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
                raise ValueError(f"HCAT {source_id} downloaded source SHA256 mismatch")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        raw = path.read_bytes()
        verified.append({
            **source, "path": str(path), "verified_fact_count": verify_source(source_id, raw)
        })
    if TTM_NET_INCOME != -86_265_000:
        raise AssertionError("HCAT direct TTM arithmetic changed")
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
        },
        "rejected_later_filings": REJECTED_LATER_FILINGS,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path.read_bytes())
            }
        },
        "guardrail": (
            "Only consolidated USD US-GAAP NetIncomeLoss values in the original "
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
