#!/usr/bin/env python3
"""Recover BCTX's pre-signal IFRS TTM facts without inventing 0/0 growth."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path

import pandas as pd
import requests

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


TICKER = "BCTX"
CIK = 1_610_820
SIGNAL_DATE = "2021-08-31"
FETCHED_AT = "2026-08-28"
OUTPUT_DIR = Path("output/research_only/v14/bctx_annual_ttm_loss")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260828_bctx_apr2021_classified_financial_priorities.csv"
)
EXPECTED_AUDIT_SHA256 = (
    "616ebd6a836bb1f0571ad690fbcd1b0bf56ae06b092041ac406eb976b6243e0e"
)

SOURCES = (
    {
        "source_id": "fy2020_audited_cad",
        "role": "FY2020 and FY2019 audited IFRS loss",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1610820/"
            "000149315221004840/form424b4.htm"
        ),
        "accession": "0001493152-21-004840",
        "filed": "2021-02-25",
        "form": "424B4",
        "local_name": "source_0001493152-21-004840_form424b4.html",
        "expected_sha256": (
            "5a88354932a2d04ec08fb4e6c72a5e14a0294c298588c3a7ae549075a32b93f7"
        ),
    },
    {
        "source_id": "interim2020_cad",
        "role": "2020 and 2019 nine-month IFRS loss comparators",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1610820/"
            "000149315220017990/formf1a.htm"
        ),
        "accession": "0001493152-20-017990",
        "filed": "2020-09-18",
        "form": "F-1/A",
        "local_name": "source_0001493152-20-017990_formf1a.html",
        "expected_sha256": (
            "b7436ce060e23878fd44fed2cb1505f0d24edd4be25b663c3ef98fc73d579f13"
        ),
    },
    {
        "source_id": "interim2021_cad",
        "role": "2021 and 2020 nine-month IFRS profit-loss comparators",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1610820/"
            "000149315221015716/ex99-1.htm"
        ),
        "accession": "0001493152-21-015716",
        "filed": "2021-06-30",
        "form": "6-K:EX-99.1",
        "local_name": "source_0001493152-21-015716_ex99-1.html",
        "expected_sha256": (
            "5407b85dd8a6568cbfaea6bde63bc9729adec70c28c38780e68915a6ddfcca52"
        ),
    },
    {
        "source_id": "fy2021_audited_usd_late",
        "role": "FY2021 audited IFRS loss after the August signal",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1610820/"
            "000149315221028949/ex99-1.htm"
        ),
        "accession": "0001493152-21-028949",
        "filed": "2021-11-16",
        "form": "40-F/A:EX-99.1",
        "local_name": "source_0001493152-21-028949_ex99-1.html",
        "expected_sha256": (
            "8cb360a2233461ec95b639e933545d96ec8146883871f4417992adf651663ce4"
        ),
    },
)
SOURCE = SOURCES[-1]

FY2020_LOSS_CAD = -4_944_221
FY2019_LOSS_CAD = -5_789_662
NINE_MONTH_2020_LOSS_CAD = -4_248_670
NINE_MONTH_2019_LOSS_CAD = -4_094_995
NINE_MONTH_2021_PROFIT_CAD = 2_816_193
CURRENT_NET_INCOME_TTM_CAD = (
    FY2020_LOSS_CAD - NINE_MONTH_2020_LOSS_CAD + NINE_MONTH_2021_PROFIT_CAD
)
PRIOR_NET_INCOME_TTM_CAD = (
    FY2019_LOSS_CAD - NINE_MONTH_2019_LOSS_CAD + NINE_MONTH_2020_LOSS_CAD
)
NET_INCOME_GROWTH = (
    CURRENT_NET_INCOME_TTM_CAD - PRIOR_NET_INCOME_TTM_CAD
) / abs(PRIOR_NET_INCOME_TTM_CAD)
NET_LOSS_USD = -428_334

AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", 150),
    ("liq2000000-age365-growth", 365),
    ("liq2000000-age550-growth", 550),
)

_SOURCE_GUARDS = {
    "fy2020_audited_cad": {
        "fragments": (
            "International Financial Reporting Standards as issued by the "
            "International Accounting Standards Board",
            "expressed in Canadian dollars",
        ),
        "pattern": (
            r"Loss For The Period\s+\(\s*517,601\s*\)\s+"
            r"\(\s*4,944,221\s*\)\s+\(\s*5,789,662\s*\)\s+"
            r"\(\s*5,412,663\s*\)"
        ),
    },
    "interim2020_cad": {
        "fragments": (
            "Condensed Interim Consolidated Statements of Operations and "
            "Comprehensive Loss",
            "For the Three and Nine Months Ended April 30, 2020 and 2019",
            "Expressed in Canadian Dollars",
        ),
        "pattern": (
            r"Loss For The Period\s+\(\s*700,649\s*\)\s+"
            r"\(\s*1,522,646\s*\)\s+\(\s*4,248,670\s*\)\s+"
            r"\(\s*4,094,995\s*\)"
        ),
    },
    "interim2021_cad": {
        "fragments": (
            "Interim Consolidated Statements of Operations and Comprehensive "
            "Profit (Loss)",
            "For the Three and Nine Months Ended April 30, 2021 and 2020",
            "Expressed in Canadian Dollars",
            "As the Company has no revenues",
        ),
        "pattern": (
            r"Profit \(Loss\) For The Period\s+3,623,642\s+"
            r"\(\s*700,649\s*\)\s+2,816,193\s+"
            r"\(\s*4,248,670\s*\)"
        ),
    },
    "fy2021_audited_usd_late": {
        "fragments": (
            "Consolidated Statements of Operations and Comprehensive Loss",
            "Expressed in US Dollars",
            "International Financial Reporting Standards",
        ),
        "pattern": (
            r"Loss for the Year\s+\(\s*428,334\s*\)\s+"
            r"\(\s*4,024,536\s*\)\s+\(\s*4,712,789\s*\)"
        ),
    },
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _path_sha256(path: Path) -> str:
    return _sha256(Path(path).read_bytes())


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


def verify_sources(
    payloads: dict[str, bytes],
    expected_sha256s: dict[str, str] | None = None,
) -> list[dict]:
    """Verify exact SEC bytes and each accounting row used in the derivation."""
    source_ids = {source["source_id"] for source in SOURCES}
    if set(payloads) != source_ids:
        raise ValueError(f"BCTX source set changed: {sorted(payloads)}")
    expected_sha256s = expected_sha256s or {
        source["source_id"]: source["expected_sha256"] for source in SOURCES
    }
    evidence = []
    for source in SOURCES:
        source_id = source["source_id"]
        payload = payloads[source_id]
        actual = _sha256(payload)
        expected = expected_sha256s[source_id]
        if actual != expected:
            raise ValueError(
                f"BCTX {source_id} source SHA mismatch: {actual} != {expected}"
            )
        text = _plain_text(payload)
        guard = _SOURCE_GUARDS[source_id]
        missing = [fragment for fragment in guard["fragments"] if fragment not in text]
        if missing:
            raise ValueError(f"BCTX {source_id} statement guards missing: {missing}")
        if re.search(guard["pattern"], text, flags=re.I) is None:
            raise ValueError(f"BCTX {source_id} accounting-row guard failed")
        evidence.append({**source, "sha256": actual, "bytes": len(payload)})
    return evidence


def verify_source(payload: bytes, expected_sha256: str | None = None) -> dict:
    """Backward-compatible verifier for the late FY2021 audited source."""
    expected = expected_sha256 or SOURCE["expected_sha256"]
    text = _plain_text(payload)
    actual = _sha256(payload)
    if actual != expected:
        raise ValueError(f"BCTX FY2021 source SHA mismatch: {actual} != {expected}")
    guard = _SOURCE_GUARDS[SOURCE["source_id"]]
    missing = [fragment for fragment in guard["fragments"] if fragment not in text]
    if missing:
        raise ValueError(f"BCTX audited-statement guards missing: {missing}")
    if re.search(guard["pattern"], text, flags=re.I) is None:
        raise ValueError("BCTX FY2021 IFRS loss-row guard failed")
    return {
        **SOURCE,
        "sha256": actual,
        "bytes": len(payload),
        "profit_semantics": "IFRS consolidated Loss for the Year",
        "currency": "USD",
        "scale": 1,
        "net_income_ttm": NET_LOSS_USD,
    }


def _direct_facts() -> pd.DataFrame:
    pre_signal = next(
        source for source in SOURCES if source["source_id"] == "interim2021_cad"
    )
    rows = [
        {
            "ticker": TICKER,
            "fiscal_end": "2021-04-30",
            "available_date": pre_signal["filed"],
            "metric": "net_income_ttm",
            "value": float(CURRENT_NET_INCOME_TTM_CAD),
            "taxonomy": "BCTX_IFRS_DIRECT_TTM_CAD",
            "concept": "derived_fy2020_minus_9m2020_plus_9m2021:net_income_ttm",
            "form": pre_signal["form"],
            "accession": pre_signal["accession"],
            "fetched_at": FETCHED_AT,
        },
        {
            "ticker": TICKER,
            "fiscal_end": "2021-04-30",
            "available_date": pre_signal["filed"],
            "metric": "net_income_growth",
            "value": float(NET_INCOME_GROWTH),
            "taxonomy": "BCTX_IFRS_DIRECT_TTM_CAD",
            "concept": "direct_ttm_growth_vs_2020-04-30:net_income",
            "form": pre_signal["form"],
            "accession": pre_signal["accession"],
            "fetched_at": FETCHED_AT,
        },
        {
            "ticker": TICKER,
            "fiscal_end": "2021-04-30",
            "available_date": pre_signal["filed"],
            "metric": "revenue_ttm",
            "value": 0.0,
            "taxonomy": "BCTX_IFRS_DIRECT_TTM_CAD",
            "concept": "strict_zero_revenue_ttm:no_revenue_operations_statement",
            "form": pre_signal["form"],
            "accession": pre_signal["accession"],
            "fetched_at": FETCHED_AT,
        },
        {
            "ticker": TICKER,
            "fiscal_end": "2021-07-31",
            "available_date": SOURCE["filed"],
            "metric": "net_income_ttm",
            "value": float(NET_LOSS_USD),
            "taxonomy": "ifrs-full",
            "concept": "StrictAnnualTTM:ProfitLoss:USD",
            "form": SOURCE["form"],
            "accession": SOURCE["accession"],
            "fetched_at": FETCHED_AT,
        },
    ]
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def ttm_derivation() -> dict:
    return {
        "currency": "CAD",
        "scale": 1,
        "fiscal_end": "2021-04-30",
        "available_date": "2021-06-30",
        "current_net_income_ttm": {
            "formula": "FY2020 - 9M2020 + 9M2021",
            "operands": [
                FY2020_LOSS_CAD,
                NINE_MONTH_2020_LOSS_CAD,
                NINE_MONTH_2021_PROFIT_CAD,
            ],
            "value": CURRENT_NET_INCOME_TTM_CAD,
        },
        "prior_net_income_ttm": {
            "formula": "FY2019 - 9M2019 + 9M2020",
            "operands": [
                FY2019_LOSS_CAD,
                NINE_MONTH_2019_LOSS_CAD,
                NINE_MONTH_2020_LOSS_CAD,
            ],
            "value": PRIOR_NET_INCOME_TTM_CAD,
        },
        "net_income_growth": {
            "formula": "(current - prior) / abs(prior)",
            "value": NET_INCOME_GROWTH,
        },
        "revenue": {
            "current_ttm": 0,
            "prior_ttm": 0,
            "growth": None,
            "reason": "0/0 comparison is undefined",
        },
    }


def _validate_audit_binding(path: Path, expected_sha256: str) -> dict:
    actual_sha = _path_sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"BCTX audit binding changed: {actual_sha}")
    priorities = pd.read_csv(path)
    scenarios = {scenario for scenario, _ in AUDIT_OBSERVATIONS}
    rows = priorities.loc[
        priorities["ticker"].eq(TICKER)
        & priorities["scenario"].isin(scenarios)
    ].copy()
    if set(rows["scenario"]) != scenarios or len(rows) != len(scenarios):
        raise RuntimeError("BCTX priority scenarios changed")
    if not rows["missing_signal_count"].eq(1).all():
        raise RuntimeError("BCTX missing-signal count changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE} or set(
        rows["last_missing_signal_date"]
    ) != {SIGNAL_DATE}:
        raise RuntimeError("BCTX missing signal date changed")
    reason_columns = (
        "no_raw_pit_financial_facts_signal_count",
        "insufficient_growth_history_signal_count",
        "stale_growth_snapshot_signal_count",
    )
    reason_counts = rows[list(reason_columns)].sum(axis=1)
    if not reason_counts.eq(1).all():
        raise RuntimeError("BCTX audit must have exactly one missing-data reason")
    classifications = {
        column.removesuffix("_signal_count")
        for column in reason_columns
        if rows[column].eq(1).all()
    }
    if len(classifications) != 1:
        raise RuntimeError("BCTX audit classifications differ by scenario")
    return {
        "path": str(path),
        "sha256": actual_sha,
        "scenario_count": len(rows),
        "missing_observation_count": len(rows),
        "signal_date": SIGNAL_DATE,
        "observed_classification": classifications.pop(),
    }


def _unrecoverable_observations() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "scenario": scenario,
            "ticker": TICKER,
            "signal_date": SIGNAL_DATE,
            "maximum_age_days": maximum_age_days,
            "resolved": False,
            "decision": "unrecoverable_zero_revenue_growth_denominator",
            "reason": (
                "Exact current and prior revenue TTM are both zero; the frozen "
                "growth formula has a zero denominator, so revenue growth is "
                "undefined and the four-field growth bundle cannot be completed."
            ),
        }
        for scenario, maximum_age_days in AUDIT_OBSERVATIONS
    ])


def build(
    output_dir: Path = OUTPUT_DIR,
    audit_path: Path = AUDIT_PATH,
    expected_audit_sha256: str = EXPECTED_AUDIT_SHA256,
) -> dict:
    output_dir = Path(output_dir)
    audit_path = Path(audit_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = output_dir / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    payloads = {}
    source_paths = {}
    for source in SOURCES:
        path = source_dir / source["local_name"]
        payload = path.read_bytes() if path.exists() else _download(source["url"])
        payloads[source["source_id"]] = payload
        source_paths[source["source_id"]] = path
    evidence = verify_sources(payloads)
    for source in SOURCES:
        source_id = source["source_id"]
        path = source_paths[source_id]
        if not path.exists():
            path.write_bytes(payloads[source_id])

    facts = _direct_facts()
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    derivation = ttm_derivation()
    derivation_path = output_dir / "ttm_derivation.json"
    derivation_path.write_text(
        json.dumps(derivation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    observations = _unrecoverable_observations()
    observations_path = output_dir / "unrecoverable_observations.csv"
    observations.to_csv(observations_path, index=False)
    resolution = {
        "ticker": TICKER,
        "signal_date": "2021-11-30",
        "financial_age_days": 14,
        "classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "net_income_ttm": NET_LOSS_USD,
        "currency": "USD",
    }
    resolution_path = output_dir / "resolved_observation.json"
    resolution_path.write_text(
        json.dumps(resolution, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    audit_binding = _validate_audit_binding(audit_path, expected_audit_sha256)
    source_outputs = [
        {**item, "local_path": str(source_paths[item["source_id"]])}
        for item in evidence
    ]
    manifest = {
        "schema_version": 2,
        "research_only": True,
        "ticker": TICKER,
        "cik": CIK,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": False,
        "formal_financials_modified": False,
        "shared_candidate_integrated": False,
        "point_in_time_proven": True,
        "source_locked": True,
        "recovery_classification": "PARTIAL_EXACT_TTM_ZERO_REVENUE_DENOMINATOR",
        "accepted_pre_signal_direct_fact_count": 3,
        "unrecoverable_observation_count": len(observations),
        "guardrail": (
            "Emit exact CAD net-income TTM, net-income growth, and zero revenue "
            "TTM known by 2021-06-30. Never emit revenue_growth=0 because both "
            "the current and prior revenue TTM are zero and 0/0 is undefined. "
            "Never backdate the 2021-11-16 USD annual filing."
        ),
        "derivation": derivation,
        "audit_binding": audit_binding,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _path_sha256(facts_path),
                "row_count": len(facts),
            },
            "ttm_derivation": {
                "path": str(derivation_path),
                "sha256": _path_sha256(derivation_path),
            },
            "unrecoverable_observations": {
                "path": str(observations_path),
                "sha256": _path_sha256(observations_path),
                "row_count": len(observations),
            },
            "resolved_observation": {
                "path": str(resolution_path),
                "sha256": _path_sha256(resolution_path),
            },
            "sources": source_outputs,
        },
        "fetched_at": FETCHED_AT,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {**manifest, "manifest": str(manifest_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--audit-path", type=Path, default=AUDIT_PATH)
    parser.add_argument(
        "--expected-audit-sha256", default=EXPECTED_AUDIT_SHA256
    )
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = build(
        output_dir=args.output_dir,
        audit_path=args.audit_path,
        expected_audit_sha256=args.expected_audit_sha256,
    )
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
