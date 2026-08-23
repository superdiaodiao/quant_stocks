#!/usr/bin/env python3
"""Recover SIBN's pre-signal 2019Q3 direct TTM net loss.

The original 2018Q3 10-Q, FY2018 10-K, and 2019Q3 10-Q are all present in the
SHA-locked SEC Company Facts cache.  One exact FY - 9M comparative + 9M current
calculation classifies the two remaining age-150 observations as known
nonpositive without requiring revenue growth or later filings.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd


TICKER = "SIBN"
CIK = 1_459_839
RAW_PATH = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001459839.json.gz"
)
RAW_SHA256 = "80561b639d05dba71118a7fb72762b8f1bfa69b1c8099fa8452d87f4d0b5c873"
OUTPUT_DIR = Path("output/research_only/v14/sibn_direct_ttm_loss")
DEFAULT_AUDIT = Path(
    "output/research_only/v14/checkpoint_20260824_hone_final.json"
)
AVAILABLE_DATE = "2019-11-12"
FISCAL_END = "2019-09-30"
SIGNALS = ("2020-01-31", "2020-02-28")
CONCEPT = "NetIncomeLoss"

OPERANDS = {
    "fy2018": {
        "start": "2018-01-01", "end": "2018-12-31", "value": -17_453_000,
        "filed": "2019-03-14", "form": "10-K", "accn": "0001459839-19-000011",
    },
    "nine_month_2018_original": {
        "start": "2018-01-01", "end": "2018-09-30", "value": -12_140_000,
        "filed": "2018-11-29", "form": "10-Q", "accn": "0001459839-18-000009",
    },
    "nine_month_2018_comparative": {
        "start": "2018-01-01", "end": "2018-09-30", "value": -12_140_000,
        "filed": AVAILABLE_DATE, "form": "10-Q", "accn": "0001459839-19-000044",
    },
    "nine_month_2019": {
        "start": "2019-01-01", "end": "2019-09-30", "value": -29_305_000,
        "filed": AVAILABLE_DATE, "form": "10-Q", "accn": "0001459839-19-000044",
    },
}

TTM_NET_INCOME = (
    OPERANDS["fy2018"]["value"]
    - OPERANDS["nine_month_2018_original"]["value"]
    + OPERANDS["nine_month_2019"]["value"]
)

REJECTED_LATER_FILINGS = {
    "0001459839-20-000031": {
        "form": "10-K",
        "filed": "2020-03-11",
        "reason": "post-signal FY2019 filing; unnecessary for the exact TTM loss",
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
        raise ValueError("SIBN Company Facts cache SHA256 mismatch")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    if int(wrapper.get("cik", 0)) != CIK or wrapper.get("symbols") != [TICKER]:
        raise ValueError("SIBN wrapper identity mismatch")
    payload = wrapper.get("payload", {})
    if int(payload.get("cik", 0)) != CIK or "SI-BONE" not in str(
        payload.get("entityName", "")
    ).upper():
        raise ValueError("SIBN Company Facts payload identity mismatch")
    facts = payload["facts"]["us-gaap"][CONCEPT]["units"]
    if set(facts) != {"USD"}:
        raise ValueError("SIBN NetIncomeLoss must use only USD units")

    verified = []
    for role, expected in OPERANDS.items():
        matches = [
            item for item in facts["USD"]
            if all(item.get(key) == expected[key]
                   for key in ("start", "end", "filed", "form", "accn"))
            and int(item["val"]) == expected["value"]
        ]
        if len(matches) != 1:
            raise ValueError(f"SIBN {role} operand is not unique: {len(matches)}")
        verified.append({"role": role, **expected})
    if (
        OPERANDS["nine_month_2018_original"]["value"]
        != OPERANDS["nine_month_2018_comparative"]["value"]
    ):
        raise ValueError("SIBN 9M2018 original/comparative mismatch")
    if TTM_NET_INCOME != -34_618_000:
        raise AssertionError("SIBN exact TTM arithmetic changed")
    return wrapper, verified


def strict_quarterly_facts() -> pd.DataFrame:
    return pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": "net_income_ttm",
        "value": float(TTM_NET_INCOME),
        "taxonomy": "us-gaap",
        "concept": "StrictDirectTTM:NetIncomeLoss:USD",
        "form": "10-Q_PLUS_10-K_PLUS_10-Q_DIRECT_TTM",
        "accession": "+".join(dict.fromkeys(
            OPERANDS[role]["accn"] for role in (
                "nine_month_2018_original", "fy2018", "nine_month_2019"
            )
        )),
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
    resolutions = [{
        "ticker": TICKER,
        "signal_date": signal,
        "financial_age_days": int(
            (pd.Timestamp(signal) - pd.Timestamp(AVAILABLE_DATE)).days
        ),
        "classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "net_income_ttm": float(TTM_NET_INCOME),
    } for signal in SIGNALS]
    resolutions_path = output_dir / "resolved_observations.json"
    resolutions_path.write_text(
        json.dumps(resolutions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
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
            "path": str(raw_path),
            "sha256": RAW_SHA256,
            "source_url": wrapper.get("source_url"),
            "fetched_at": wrapper.get("fetched_at"),
        },
        "audit_binding": {
            "path": str(audit_path),
            "sha256": _sha256(audit_path),
            "missing_observation_count": len(SIGNALS),
            "signals": list(SIGNALS),
        },
        "rejected_later_filings": REJECTED_LATER_FILINGS,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path)
            },
            "resolved_observations": {
                "path": str(resolutions_path), "sha256": _sha256(resolutions_path)
            },
        },
        "guardrail": (
            "Only original USD US-GAAP NetIncomeLoss facts public by 2019-11-12 "
            "are used. The identical 9M2018 comparative is verified, and the "
            "post-signal FY2019 10-K is excluded."
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
        "recovered_observations": result["audit_binding"]["missing_observation_count"],
        "release_status": result["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
