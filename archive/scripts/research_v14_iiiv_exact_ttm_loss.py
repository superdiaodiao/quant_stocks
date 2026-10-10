#!/usr/bin/env python3
"""Expose IIIV's exact H1-derived TTM loss as exclusion-only PIT evidence."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


CACHE = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/"
    "CIK0001728688.json.gz"
)
EXPECTED_CACHE_SHA256 = (
    "66d0c7a48e0253afb1d1224a1687ebb26d77e4e2b45ace7c147e68d4450fe72d"
)
OUTPUT_DIR = Path("output/research_only/v14/iiiv_exact_ttm_loss")
TICKER = "IIIV"
CIK = 1_728_688
SOURCE_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0001728688.json"
SIGNAL_DATE = "2019-06-28"
MAXIMUM_AGE_DAYS = 150
SOURCE_FACTS = {
    "fy2018": {
        "start": "2017-10-01",
        "end": "2018-09-30",
        "value": -6_898_000,
        "accession": "0001728688-18-000027",
        "filed": "2018-12-07",
        "form": "10-K",
    },
    "h1_2018": {
        "start": "2017-10-01",
        "end": "2018-03-31",
        "value": -7_168_000,
        "accession": "0001728688-19-000052",
        "filed": "2019-05-14",
        "form": "10-Q",
    },
    "h1_2019": {
        "start": "2018-10-01",
        "end": "2019-03-31",
        "value": -924_000,
        "accession": "0001728688-19-000052",
        "filed": "2019-05-14",
        "form": "10-Q",
    },
}
EXPECTED_TTM = -654_000
EXPECTED_AVAILABLE_AGE_DAYS = 45
EXPECTED_FISCAL_AGE_DAYS = 89
AUDIT_OBSERVATIONS = (
    ("liq10000000-age150-growth", SIGNAL_DATE, MAXIMUM_AGE_DAYS),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_locked_cache(cache_path: Path) -> dict:
    cache_path = Path(cache_path)
    actual = _sha256(cache_path)
    if actual != EXPECTED_CACHE_SHA256:
        raise RuntimeError(f"IIIV Company Facts SHA-256 mismatch: {actual}")
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if int(envelope.get("cik", -1)) != CIK:
        raise ValueError("IIIV cache envelope has the wrong CIK")
    if set(envelope.get("symbols", [])) != {TICKER}:
        raise ValueError("IIIV cache envelope has the wrong symbol binding")
    if envelope.get("source_url") != SOURCE_URL:
        raise ValueError("IIIV cache envelope has the wrong official source URL")
    if int(envelope["payload"].get("cik", -1)) != CIK:
        raise ValueError("IIIV Company Facts payload has the wrong CIK")
    return envelope


def _net_income_units(payload: dict) -> list[dict]:
    return payload["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]


def verify_operands(payload: dict) -> list[dict]:
    facts = _net_income_units(payload)
    verified = []
    for operand_id, expected in SOURCE_FACTS.items():
        matches = [
            fact for fact in facts
            if fact.get("start") == expected["start"]
            and fact.get("end") == expected["end"]
            and int(fact.get("val")) == expected["value"]
            and fact.get("accn") == expected["accession"]
            and fact.get("filed") == expected["filed"]
            and fact.get("form") == expected["form"]
        ]
        if not matches:
            raise RuntimeError(f"IIIV locked operand changed or disappeared: {operand_id}")
        verified.append({"operand_id": operand_id, **expected})
    return verified


def exact_ttm_evidence(payload: dict) -> dict:
    verified = verify_operands(payload)
    values = {row["operand_id"]: int(row["value"]) for row in verified}
    ttm = values["fy2018"] - values["h1_2018"] + values["h1_2019"]
    if ttm != EXPECTED_TTM or ttm >= 0:
        raise RuntimeError(f"IIIV exact TTM loss changed: {ttm}")
    available_date = max(row["filed"] for row in verified)
    if available_date != "2019-05-14":
        raise RuntimeError("IIIV exact TTM availability changed")
    return {
        "ticker": TICKER,
        "evidence_kind": "exact_annual_minus_h1_plus_h1_ttm_loss",
        "fiscal_end": "2019-03-31",
        "available_date": available_date,
        "currency": "USD",
        "accounting_standard": "US-GAAP",
        "net_income_ttm": ttm,
        "formula": "FY2018 - H1_2018 + H1_2019",
        "source_concept": "us-gaap:NetIncomeLoss",
        "source_accessions": list(dict.fromkeys(
            row["accession"] for row in verified
        )),
        "form": "10-K_PLUS_10-Q_H1_CUMULATIVE_TTM",
    }


def direct_ttm_facts(payload: dict, fetched_at: str) -> pd.DataFrame:
    evidence = exact_ttm_evidence(payload)
    return pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": evidence["fiscal_end"],
        "available_date": evidence["available_date"],
        "metric": "net_income_ttm",
        "value": evidence["net_income_ttm"],
        "taxonomy": "us-gaap",
        "concept": "iiiv_exact_ttm:NetIncomeLoss:USD",
        "form": evidence["form"],
        "accession": "+".join(evidence["source_accessions"]),
        "fetched_at": pd.Timestamp(fetched_at).tz_localize(None).normalize(),
    }], columns=OUTPUT_COLUMNS)


def run(cache_path: Path = CACHE, output_dir: Path = OUTPUT_DIR) -> dict:
    envelope = _load_locked_cache(cache_path)
    evidence = exact_ttm_evidence(envelope["payload"])
    facts = direct_ttm_facts(envelope["payload"], envelope["fetched_at"])
    signal = pd.Timestamp(SIGNAL_DATE)
    available_age = int((signal - pd.Timestamp(evidence["available_date"])).days)
    fiscal_age = int((signal - pd.Timestamp(evidence["fiscal_end"])).days)
    if available_age != EXPECTED_AVAILABLE_AGE_DAYS or available_age > MAXIMUM_AGE_DAYS:
        raise RuntimeError("IIIV exact TTM availability is outside the declared window")
    if fiscal_age != EXPECTED_FISCAL_AGE_DAYS or fiscal_age > MAXIMUM_AGE_DAYS:
        raise RuntimeError("IIIV exact TTM fiscal end is outside the declared window")
    resolutions = [{
        "scenario": scenario,
        "signal_date": signal_date,
        "maximum_age_days": maximum_age_days,
        "resolved": True,
        "decision": "known_nonpositive_profit",
        "financial_age_days": available_age,
        "fiscal_age_days": fiscal_age,
        "net_income_ttm": EXPECTED_TTM,
    } for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS]

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_evidence.json"
    resolution_path = output_dir / "audit_observation_resolution.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    resolution_path.write_text(
        json.dumps(resolutions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": TICKER,
        "cik": CIK,
        "source": {
            "path": str(cache_path),
            "sha256": EXPECTED_CACHE_SHA256,
            "url": SOURCE_URL,
            "verified_operands": verify_operands(envelope["payload"]),
        },
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_audit_observation_count": len(resolutions),
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path)
            },
            "exact_ttm_evidence": {
                "path": str(evidence_path), "sha256": _sha256(evidence_path)
            },
            "audit_observation_resolution": {
                "path": str(resolution_path), "sha256": _sha256(resolution_path)
            },
        },
        "guardrail": (
            "Uses only the exact us-gaap NetIncomeLoss operands in the locked "
            "Company Facts payload. Both filing availability and fiscal-end "
            "recency must be within 150 days. It emits only a negative TTM "
            "exclusion state; no revenue, quarter, or growth metric is invented."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-path", type=Path, default=CACHE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = run(args.cache_path, args.output_dir)
    print(json.dumps({
        "manifest": report["manifest"],
        "accepted_exact_ttm_loss_count": report["accepted_exact_ttm_loss_count"],
        "resolved_audit_observation_count": report["resolved_audit_observation_count"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
