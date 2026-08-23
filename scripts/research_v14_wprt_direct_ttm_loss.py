#!/usr/bin/env python3
"""Recover WPRT's pre-signal 2020Q3 direct TTM net loss."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v14_hone_cik_transition_ttm import (
    NET_INCOME,
    parse_consolidated_usd_facts,
)


TICKER = "WPRT"
CIK = 1_370_416
OUTPUT_DIR = Path("output/research_only/v14/wprt_direct_ttm_loss")
DEFAULT_AUDIT = Path(
    "output/research_only/v14/checkpoint_20260824_hone_sibn_knsa_hcat_final.json"
)
FISCAL_END = "2020-09-30"
AVAILABLE_DATE = "2020-11-09"
SIGNAL = "2020-11-30"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

SOURCES = {
    "fy2019": {
        "url": "https://www.sec.gov/Archives/edgar/data/1370416/000137041620000003/wprt-20191231.xml",
        "local_path": "sources/wprt-20191231.xml",
        "sha256": "c3ca0c75694ca71fca0c25f4be41e1881d0dafd7fec6ed6d623d75ec5c2e3e99",
        "form": "40-F", "filed": "2020-03-17",
        "accession": "0001370416-20-000003",
    },
    "q3_2020": {
        "url": "https://www.sec.gov/Archives/edgar/data/1370416/000137041620000038/wprt-09302020xex992.htm",
        "local_path": "sources/wprt-09302020xex992.htm",
        "sha256": "bc49b6f1aa82a29b987778d2d2271365f105d3284c240d8c4c291bc8eb0196a5",
        "form": "6-K:EX-99.2", "filed": AVAILABLE_DATE,
        "accession": "0001370416-20-000038",
    },
    "q3_2020_release": {
        "url": "https://www.sec.gov/Archives/edgar/data/1370416/000137041620000038/wprt-09302020xex991.htm",
        "local_path": "sources/wprt-09302020xex991.htm",
        "sha256": "2430c042af43c4d45d9d367c61e91664140d1233589108f8f0a5126e469de525",
        "form": "6-K:EX-99.1", "filed": AVAILABLE_DATE,
        "accession": "0001370416-20-000038",
    },
}

FY2019_NET_INCOME = 41_000
NINE_MONTH_2019_NET_LOSS = -610_000
NINE_MONTH_2020_NET_LOSS = -11_477_000
TTM_NET_INCOME = (
    FY2019_NET_INCOME - NINE_MONTH_2019_NET_LOSS + NINE_MONTH_2020_NET_LOSS
)

REJECTED_NON_GAAP_LABELS = (
    "Adjusted EBITDA",
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _download(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _plain_text(raw: bytes) -> str:
    value = raw.decode("utf-8", errors="replace")
    value = re.sub(r"<script\b.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def verify_sources(payloads: dict[str, bytes]) -> dict:
    if set(payloads) != set(SOURCES):
        raise ValueError("WPRT source set mismatch")
    verified = []
    for source_id, source in SOURCES.items():
        raw = payloads[source_id]
        if _sha256(raw) != source["sha256"]:
            raise ValueError(f"WPRT {source_id} source SHA256 mismatch")
        verified.append({**source, "bytes": len(raw)})

    annual = parse_consolidated_usd_facts(payloads["fy2019"])
    annual_value = annual.get((NET_INCOME, "2019-01-01", "2019-12-31"))
    if annual_value != FY2019_NET_INCOME:
        raise ValueError(f"WPRT FY2019 NetIncomeLoss changed: {annual_value}")

    interim_text = _plain_text(payloads["q3_2020"])
    statement = re.search(
        r"Net income \(loss\) for the period\s+822\s+4,987\s+"
        r"\(11,477\)\s+\(610\)",
        interim_text,
    )
    if statement is None:
        raise ValueError("WPRT 2020Q3 consolidated income-statement guard failed")
    release_text = _plain_text(payloads["q3_2020_release"])
    if not all(label in release_text for label in REJECTED_NON_GAAP_LABELS):
        raise ValueError("WPRT non-GAAP exclusion labels changed")
    if TTM_NET_INCOME != -10_826_000:
        raise AssertionError("WPRT direct TTM arithmetic changed")
    return {
        "sources": verified,
        "operands_usd": {
            "fy2019": FY2019_NET_INCOME,
            "nine_month_2019": NINE_MONTH_2019_NET_LOSS,
            "nine_month_2020": NINE_MONTH_2020_NET_LOSS,
        },
        "formula": "FY2019 - 9M2019 + 9M2020",
        "net_income_ttm_usd": TTM_NET_INCOME,
    }


def strict_quarterly_facts() -> pd.DataFrame:
    return pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": "net_income_ttm",
        "value": float(TTM_NET_INCOME),
        "taxonomy": "us-gaap",
        "concept": "StrictDirectTTM:NetIncomeLoss:USD",
        "form": "40-F_PLUS_6-K_Q3_DIRECT_TTM",
        "accession": "+".join(dict.fromkeys(
            source["accession"] for source in SOURCES.values()
        )),
        "fetched_at": "2026-08-24",
    }])


def build(output_dir: Path = OUTPUT_DIR, audit_path: Path = DEFAULT_AUDIT) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = {}
    for source_id, source in SOURCES.items():
        path = output_dir / source["local_path"]
        if not path.exists():
            raw = _download(source["url"])
            if _sha256(raw) != source["sha256"]:
                raise ValueError(f"WPRT {source_id} downloaded source SHA256 mismatch")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        payloads[source_id] = path.read_bytes()
    evidence = verify_sources(payloads)
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
        "evidence": evidence,
        "audit_binding": {
            "path": str(audit_path), "sha256": _sha256(audit_path.read_bytes()),
            "missing_observation_count": 3, "signals": [SIGNAL],
            "scenarios": [
                f"liq2000000-age{age}-growth" for age in (150, 365, 550)
            ],
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path.read_bytes())
            }
        },
        "guardrail": (
            "Uses only consolidated US-GAAP NetIncomeLoss from the original "
            "FY2019 XBRL and the original 2020Q3 interim income statement. "
            "Adjusted EBITDA and adjusted net income are explicitly excluded."
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
        "recovered_observations": result["audit_binding"]["missing_observation_count"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
