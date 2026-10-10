#!/usr/bin/env python3
"""Recover KNSA's pre-signal 2019Q3 direct TTM net loss."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd


TICKER = "KNSA"
CIK = 1_730_430
RAW_PATH = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001730430.json.gz"
)
RAW_SHA256 = "61c8490330c6b9e565e9c50549f01f9ae5cd8b260771ea4882f4697f3707e190"
OUTPUT_DIR = Path("output/research_only/v14/knsa_direct_ttm_loss")
DEFAULT_AUDIT = Path(
    "output/research_only/v14/checkpoint_20260824_hone_sibn_final.json"
)
AVAILABLE_DATE = "2019-11-05"
FISCAL_END = "2019-09-30"
SIGNAL = "2020-02-28"
CONCEPT = "NetIncomeLoss"

OPERANDS = {
    "fy2018": {
        "start": "2018-01-01", "end": "2018-12-31", "value": -103_227_000,
        "filed": "2019-03-12", "form": "10-K", "accn": "0001558370-19-001857",
    },
    "nine_month_2018_original": {
        "start": "2018-01-01", "end": "2018-09-30", "value": -60_647_000,
        "filed": "2018-11-06", "form": "10-Q", "accn": "0001558370-18-008650",
    },
    "nine_month_2018_comparative": {
        "start": "2018-01-01", "end": "2018-09-30", "value": -60_647_000,
        "filed": AVAILABLE_DATE, "form": "10-Q", "accn": "0001558370-19-009978",
    },
    "nine_month_2019": {
        "start": "2019-01-01", "end": "2019-09-30", "value": -130_070_000,
        "filed": AVAILABLE_DATE, "form": "10-Q", "accn": "0001558370-19-009978",
    },
}

TTM_NET_INCOME = (
    OPERANDS["fy2018"]["value"]
    - OPERANDS["nine_month_2018_original"]["value"]
    + OPERANDS["nine_month_2019"]["value"]
)

REJECTED_LATER_FILINGS = {
    "0001558370-20-002081": {
        "form": "10-K",
        "filed": "2020-03-05",
        "reason": "post-signal FY2019 filing; excluded from the direct TTM loss",
    }
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_and_validate(path: Path = RAW_PATH) -> tuple[dict, list[dict]]:
    if _sha256(path) != RAW_SHA256:
        raise ValueError("KNSA Company Facts cache SHA256 mismatch")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    if int(wrapper.get("cik", 0)) != CIK or wrapper.get("symbols") != [TICKER]:
        raise ValueError("KNSA wrapper identity mismatch")
    payload = wrapper.get("payload", {})
    if int(payload.get("cik", 0)) != CIK or "KINIKSA" not in str(
        payload.get("entityName", "")
    ).upper():
        raise ValueError("KNSA payload identity mismatch")
    units = payload["facts"]["us-gaap"][CONCEPT]["units"]
    if set(units) != {"USD"}:
        raise ValueError("KNSA NetIncomeLoss must use only USD")
    verified = []
    for role, expected in OPERANDS.items():
        matches = [
            item for item in units["USD"]
            if all(item.get(key) == expected[key]
                   for key in ("start", "end", "filed", "form", "accn"))
            and int(item["val"]) == expected["value"]
        ]
        if len(matches) != 1:
            raise ValueError(f"KNSA {role} operand is not unique: {len(matches)}")
        verified.append({"role": role, **expected})
    if OPERANDS["nine_month_2018_original"]["value"] != OPERANDS[
        "nine_month_2018_comparative"
    ]["value"]:
        raise ValueError("KNSA 9M2018 comparative mismatch")
    if TTM_NET_INCOME != -172_650_000:
        raise AssertionError("KNSA direct TTM arithmetic changed")
    return wrapper, verified


def strict_quarterly_facts() -> pd.DataFrame:
    accessions = "+".join(dict.fromkeys(
        OPERANDS[role]["accn"] for role in (
            "nine_month_2018_original", "fy2018", "nine_month_2019"
        )
    ))
    return pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": "net_income_ttm",
        "value": float(TTM_NET_INCOME),
        "taxonomy": "us-gaap",
        "concept": "StrictDirectTTM:NetIncomeLoss:USD",
        "form": "10-Q_PLUS_10-K_PLUS_10-Q_DIRECT_TTM",
        "accession": accessions,
        "fetched_at": "2026-08-24",
    }])


def build(
    output_dir: Path = OUTPUT_DIR,
    raw_path: Path = RAW_PATH,
    audit_path: Path = DEFAULT_AUDIT,
) -> dict:
    wrapper, verified = load_and_validate(raw_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts = strict_quarterly_facts()
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    resolution = [{
        "ticker": TICKER,
        "signal_date": SIGNAL,
        "financial_age_days": int(
            (pd.Timestamp(SIGNAL) - pd.Timestamp(AVAILABLE_DATE)).days
        ),
        "classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "net_income_ttm": float(TTM_NET_INCOME),
    }]
    resolution_path = output_dir / "resolved_observations.json"
    resolution_path.write_text(json.dumps(resolution, indent=2) + "\n")
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
            "formula": "FY2018 - 9M2018 + 9M2019",
            "verified_operands": verified,
            "net_income_ttm_usd": TTM_NET_INCOME,
            "original_comparative_match": True,
        },
        "raw_payload": {
            "path": str(raw_path), "sha256": RAW_SHA256,
            "source_url": wrapper.get("source_url"),
            "fetched_at": wrapper.get("fetched_at"),
        },
        "audit_binding": {
            "path": str(audit_path), "sha256": _sha256(audit_path),
            "missing_observation_count": 1, "signals": [SIGNAL],
        },
        "rejected_later_filings": REJECTED_LATER_FILINGS,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path)
            },
            "resolved_observations": {
                "path": str(resolution_path), "sha256": _sha256(resolution_path)
            },
        },
        "guardrail": (
            "Only original USD US-GAAP NetIncomeLoss facts public by 2019-11-05 "
            "are used. The 9M2018 comparative matches the original filing and "
            "the post-signal FY2019 annual report is excluded."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--raw", type=Path, default=RAW_PATH)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    args = parser.parse_args()
    result = build(args.output_dir, args.raw, args.audit)
    print(json.dumps({
        "manifest": result["manifest"],
        "net_income_ttm_usd": result["evidence"]["net_income_ttm_usd"],
        "recovered_observations": 1,
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
