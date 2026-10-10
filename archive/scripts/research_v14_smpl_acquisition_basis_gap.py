#!/usr/bin/env python3
"""Source-lock SMPL's unrecoverable 2019 acquisition-basis growth gaps.

The two affected signals have a timely fiscal-2019 Q1 filing and therefore a
valid current TTM.  The prior TTM, however, necessarily spans the July 2017
Atkins business combination: its early quarters are explicitly tagged as the
Predecessor while later quarters belong to the Successor.  The only full-year
alternative is unaudited pro-forma information.  Neither is an admissible
point-in-time growth denominator, so this package records negative evidence
and deliberately emits no candidate fundamentals.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


TICKER = "SMPL"
CIK = 1_702_744
OUTPUT_DIR = Path("output/research_only/v14/smpl_acquisition_basis_gap")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260827_sy_glpg_rlmd_financial_priorities.csv"
)
EXPECTED_AUDIT_SHA256 = (
    "616ebd6a836bb1f0571ad690fbcd1b0bf56ae06b092041ac406eb976b6243e0e"
)
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
FETCHED_AT = "2026-08-27"

SOURCE_DOCUMENTS = (
    {
        "source_id": "fy2017_split",
        "form": "10-K",
        "filed": "2017-11-09",
        "accession": "0001702744-17-000027",
        "document": "atk-20170826.xml",
        "expected_sha256": (
            "7063e309e74628fd7ff13064bfa4bfb80255fd3a4dbf81efc16b73f0f88b932f"
        ),
    },
    {
        "source_id": "fy2018_successor",
        "form": "10-K",
        "filed": "2018-10-24",
        "accession": "0001702744-18-000064",
        "document": "atk-20180825.xml",
        "expected_sha256": (
            "1424bc39e933651ea4e4decc5759311465029fd095951f795abab3b591191a2e"
        ),
    },
    {
        "source_id": "q1_2019_successor",
        "form": "10-Q",
        "filed": "2019-01-03",
        "accession": "0001702744-19-000004",
        "document": "atk-20181124.xml",
        "expected_sha256": (
            "882fad65d685e93c0fd9b5147a2b7ea6ad38624db0d11d6270c0c1dbd768b89d"
        ),
    },
)

PREDECESSOR = ("us-gaap:PredecessorMember",)
ACQUISITION = ("atk:AcquisitionofAtkinsMember",)
SUCCESSOR: tuple[str, ...] = ()


def _fact(
    concept: str,
    start: str,
    end: str,
    members: tuple[str, ...],
    value: int,
) -> dict:
    return {
        "concept": concept,
        "start": start,
        "end": end,
        "members": members,
        "value": value,
    }


EXPECTED_FACTS = {
    "fy2017_split": (
        _fact("SalesRevenueNet", "2016-08-28", "2017-07-06", PREDECESSOR, 339_837_000),
        _fact("NetIncomeLoss", "2016-08-28", "2017-07-06", PREDECESSOR, -2_485_000),
        _fact("SalesRevenueNet", "2017-07-07", "2017-08-26", SUCCESSOR, 56_334_000),
        _fact("NetIncomeLoss", "2017-07-07", "2017-08-26", SUCCESSOR, 450_000),
        _fact("BusinessAcquisitionsProFormaRevenue", "2016-08-28", "2017-08-26", ACQUISITION, 396_171_000),
        _fact("BusinessAcquisitionsProFormaNetIncomeLoss", "2016-08-28", "2017-08-26", ACQUISITION, 28_701_000),
    ),
    "fy2018_successor": (
        _fact("Revenues", "2016-08-28", "2017-07-06", PREDECESSOR, 339_837_000),
        _fact("NetIncomeLoss", "2016-08-28", "2017-07-06", PREDECESSOR, -2_485_000),
        _fact("Revenues", "2017-07-07", "2017-08-26", SUCCESSOR, 56_334_000),
        _fact("NetIncomeLoss", "2017-07-07", "2017-08-26", SUCCESSOR, 450_000),
        _fact("Revenues", "2016-08-28", "2016-11-26", PREDECESSOR, 99_803_000),
        _fact("NetIncomeLoss", "2016-08-28", "2016-11-26", PREDECESSOR, 6_787_000),
        _fact("Revenues", "2017-08-27", "2017-11-25", SUCCESSOR, 106_587_000),
        _fact("NetIncomeLoss", "2017-08-27", "2017-11-25", SUCCESSOR, 10_218_000),
        _fact("Revenues", "2017-08-27", "2018-08-25", SUCCESSOR, 431_429_000),
        _fact("NetIncomeLoss", "2017-08-27", "2018-08-25", SUCCESSOR, 70_455_000),
        _fact("BusinessAcquisitionsProFormaRevenue", "2016-08-28", "2017-08-26", ACQUISITION, 396_171_000),
        _fact("BusinessAcquisitionsProFormaNetIncomeLoss", "2016-08-28", "2017-08-26", ACQUISITION, 28_857_000),
    ),
    "q1_2019_successor": (
        _fact("RevenueFromContractWithCustomerExcludingAssessedTax", "2017-08-27", "2017-11-25", SUCCESSOR, 106_587_000),
        _fact("NetIncomeLoss", "2017-08-27", "2017-11-25", SUCCESSOR, 10_218_000),
        _fact("RevenueFromContractWithCustomerExcludingAssessedTax", "2018-08-26", "2018-11-24", SUCCESSOR, 120_931_000),
        _fact("NetIncomeLoss", "2018-08-26", "2018-11-24", SUCCESSOR, 15_257_000),
    ),
}
ACCEPTED_CONCEPTS = {
    fact["concept"]
    for facts in EXPECTED_FACTS.values()
    for fact in facts
}

SCENARIOS = (
    ("liq2000000-age150-growth", 150),
    ("liq2000000-age365-growth", 365),
    ("liq2000000-age550-growth", 550),
)
SIGNALS = ("2019-02-28", "2019-03-29")
LATEST_VALID_AVAILABLE_DATE = "2019-01-03"


def _url(source: dict) -> str:
    accession = source["accession"].replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/{accession}/"
        f"{source['document']}"
    )


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _download(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_consolidated_usd_facts(
    raw: bytes,
) -> dict[tuple[str, str, str, tuple[str, ...]], int]:
    """Parse exact consolidated/predecessor/pro-forma duration facts."""
    root = ET.fromstring(raw)
    contexts: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for node in root.iter():
        if _local_name(node.tag) != "context":
            continue
        start = end = None
        members = []
        for child in node.iter():
            name = _local_name(child.tag)
            if name == "startDate":
                start = (child.text or "").strip()
            elif name == "endDate":
                end = (child.text or "").strip()
            elif name in {"explicitMember", "typedMember"}:
                members.append((child.text or "").strip())
        if start and end:
            contexts[str(node.attrib["id"])] = (
                start,
                end,
                tuple(sorted(members)),
            )

    usd_units = set()
    for node in root.iter():
        if _local_name(node.tag) != "unit":
            continue
        measures = [
            (child.text or "").strip().casefold()
            for child in node.iter()
            if _local_name(child.tag) == "measure"
        ]
        if measures == ["iso4217:usd"]:
            usd_units.add(str(node.attrib["id"]))

    found: dict[tuple[str, str, str, tuple[str, ...]], set[int]] = {}
    for node in root.iter():
        concept = _local_name(node.tag)
        if concept not in ACCEPTED_CONCEPTS:
            continue
        context = contexts.get(str(node.attrib.get("contextRef", "")))
        if context is None or node.attrib.get("unitRef") not in usd_units:
            continue
        text = (node.text or "").replace(",", "").strip()
        if not text:
            continue
        try:
            value = int(Decimal(text))
        except (ValueError, ArithmeticError):
            continue
        key = (concept, *context)
        found.setdefault(key, set()).add(value)
    ambiguous = {key: values for key, values in found.items() if len(values) != 1}
    if ambiguous:
        raise ValueError(f"ambiguous SMPL consolidated facts: {ambiguous}")
    return {key: next(iter(values)) for key, values in found.items()}


def validate_source_lock() -> None:
    if set(EXPECTED_FACTS) != {source["source_id"] for source in SOURCE_DOCUMENTS}:
        raise ValueError("SMPL source/fact set changed")
    for source in SOURCE_DOCUMENTS:
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"invalid SMPL source SHA-256: {source['source_id']}")
        if source["accession"].replace("-", "") not in _url(source):
            raise ValueError(f"SMPL source accession URL changed: {source['source_id']}")
        if source["filed"] > LATEST_VALID_AVAILABLE_DATE:
            raise ValueError(f"post-cutoff SMPL source: {source['source_id']}")


def exact_basis_evidence() -> dict:
    current_revenue_ttm = 431_429_000 - 106_587_000 + 120_931_000
    current_net_income_ttm = 70_455_000 - 10_218_000 + 15_257_000

    combined_actual_fy2017_revenue = 339_837_000 + 56_334_000
    combined_actual_fy2017_net_income = -2_485_000 + 450_000
    cross_basis_prior_revenue_ttm = (
        combined_actual_fy2017_revenue - 99_803_000 + 106_587_000
    )
    cross_basis_prior_net_income_ttm = (
        combined_actual_fy2017_net_income - 6_787_000 + 10_218_000
    )
    pro_forma_prior_net_income_ttm = 28_857_000 - 6_787_000 + 10_218_000

    return {
        "basis_boundary": {
            "predecessor_end": "2017-07-06",
            "successor_start": "2017-07-07",
            "xbrl_member": "us-gaap:PredecessorMember",
        },
        "current_successor_ttm": {
            "fiscal_end": "2018-11-24",
            "available_date": LATEST_VALID_AVAILABLE_DATE,
            "revenue_usd": current_revenue_ttm,
            "net_income_usd": current_net_income_ttm,
            "basis": "successor_only",
            "derivation": "FY2018 - Q1FY2018 + Q1FY2019",
        },
        "rejected_cross_basis_prior_ttm": {
            "fiscal_end": "2017-11-25",
            "revenue_usd": cross_basis_prior_revenue_ttm,
            "net_income_usd": cross_basis_prior_net_income_ttm,
            "basis": "predecessor_plus_successor",
            "would_produce_revenue_growth": (
                current_revenue_ttm - cross_basis_prior_revenue_ttm
            ) / abs(cross_basis_prior_revenue_ttm),
            "would_produce_net_income_growth": (
                current_net_income_ttm - cross_basis_prior_net_income_ttm
            ) / abs(cross_basis_prior_net_income_ttm),
        },
        "rejected_pro_forma_prior_ttm": {
            "fiscal_end": "2017-11-25",
            "net_income_usd": pro_forma_prior_net_income_ttm,
            "basis": "unaudited_pro_forma_annual_plus_mixed_actual_quarters",
            "fy2017_net_income_revised_between_filings_usd": {
                "2017_10k": 28_701_000,
                "2018_10k": 28_857_000,
            },
        },
    }


def verify_sources(payloads: list[bytes]) -> dict:
    validate_source_lock()
    if len(payloads) != len(SOURCE_DOCUMENTS):
        raise ValueError("SMPL source payload count changed")
    checks = []
    for source, raw in zip(SOURCE_DOCUMENTS, payloads, strict=True):
        actual_sha = _sha256(raw)
        if actual_sha != source["expected_sha256"]:
            raise RuntimeError(
                f"SMPL {source['source_id']} source SHA-256 mismatch: {actual_sha}"
            )
        parsed = parse_consolidated_usd_facts(raw)
        for expected in EXPECTED_FACTS[source["source_id"]]:
            key = (
                expected["concept"],
                expected["start"],
                expected["end"],
                tuple(sorted(expected["members"])),
            )
            actual = parsed.get(key)
            if actual != expected["value"]:
                raise RuntimeError(
                    f"SMPL {source['source_id']} fact changed: {key} "
                    f"expected {expected['value']}, got {actual}"
                )
        checks.append({
            "source_id": source["source_id"],
            "checked_fact_count": len(EXPECTED_FACTS[source["source_id"]]),
            "sha256": actual_sha,
        })
    evidence = exact_basis_evidence()
    if evidence["current_successor_ttm"]["revenue_usd"] != 445_773_000:
        raise RuntimeError("SMPL current successor revenue TTM changed")
    if evidence["current_successor_ttm"]["net_income_usd"] != 75_494_000:
        raise RuntimeError("SMPL current successor net-income TTM changed")
    return {"source_checks": checks, **evidence}


def resolve_audit_observations() -> pd.DataFrame:
    rows = []
    available = pd.Timestamp(LATEST_VALID_AVAILABLE_DATE)
    for scenario, maximum_age_days in SCENARIOS:
        for signal in SIGNALS:
            rows.append({
                "scenario": scenario,
                "ticker": TICKER,
                "signal_date": signal,
                "maximum_age_days": maximum_age_days,
                "latest_valid_fiscal_end": "2018-11-24",
                "latest_valid_available_date": LATEST_VALID_AVAILABLE_DATE,
                "financial_age_days": int((pd.Timestamp(signal) - available).days),
                "resolved": False,
                "decision": "unrecoverable_predecessor_successor_basis_split",
                "reason": (
                    "The current TTM is successor-only, but its prior-year TTM "
                    "necessarily mixes explicitly tagged predecessor and "
                    "successor periods; the only alternative is unaudited "
                    "pro-forma acquisition information."
                ),
            })
    return pd.DataFrame(rows)


def rejected_derivations() -> list[dict]:
    evidence = exact_basis_evidence()
    return [
        {
            "candidate": "combine predecessor and successor actual periods",
            "rejected": True,
            "reason": "cross-basis TTM growth denominator",
            "values": evidence["rejected_cross_basis_prior_ttm"],
        },
        {
            "candidate": "use business-acquisition pro-forma full year",
            "rejected": True,
            "reason": (
                "unaudited pro-forma values are excluded and no homogeneous "
                "pro-forma quarterly bridge exists"
            ),
            "values": evidence["rejected_pro_forma_prior_ttm"],
        },
        {
            "candidate": "substitute Q1 year-over-year growth for TTM growth",
            "rejected": True,
            "reason": "changes the frozen TTM feature definition",
            "q1_revenue_growth": (120_931_000 - 106_587_000) / 106_587_000,
            "q1_net_income_growth": (15_257_000 - 10_218_000) / 10_218_000,
        },
        {
            "candidate": "use the fiscal-Q2 filing dated 2019-04-04",
            "rejected": True,
            "reason": "postdates both 2019-02-28 and 2019-03-29 signals",
            "accession": "0001702744-19-000015",
        },
    ]


def _validate_audit_binding(path: Path, expected_sha256: str) -> dict:
    actual_sha = _sha256(path.read_bytes())
    if actual_sha != expected_sha256:
        raise RuntimeError(f"SMPL audit binding changed: {actual_sha}")
    priorities = pd.read_csv(path)
    expected = {scenario for scenario, _ in SCENARIOS}
    rows = priorities.loc[
        priorities["ticker"].eq(TICKER)
        & priorities["scenario"].isin(expected)
    ].copy()
    if set(rows["scenario"]) != expected or len(rows) != len(expected):
        raise RuntimeError("SMPL priority scenarios changed")
    if not rows["missing_signal_count"].eq(len(SIGNALS)).all():
        raise RuntimeError("SMPL priority missing-signal counts changed")
    if not rows["insufficient_growth_history_signal_count"].eq(len(SIGNALS)).all():
        raise RuntimeError("SMPL priority classification changed")
    if set(rows["first_missing_signal_date"]) != {SIGNALS[0]}:
        raise RuntimeError("SMPL first missing signal changed")
    if set(rows["last_missing_signal_date"]) != {SIGNALS[-1]}:
        raise RuntimeError("SMPL last missing signal changed")
    return {
        "path": str(path),
        "sha256": actual_sha,
        "scenario_count": len(rows),
        "missing_observation_count": len(rows) * len(SIGNALS),
        "signals": list(SIGNALS),
    }


def build(
    output_dir: Path = OUTPUT_DIR,
    audit_path: Path = AUDIT_PATH,
    expected_audit_sha256: str = EXPECTED_AUDIT_SHA256,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = []
    source_outputs = []
    source_dir = output_dir / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    for source in SOURCE_DOCUMENTS:
        path = source_dir / source["document"]
        raw = path.read_bytes() if path.exists() else _download(_url(source))
        if _sha256(raw) != source["expected_sha256"]:
            raise RuntimeError(
                f"SMPL cached source SHA-256 mismatch: {source['source_id']}"
            )
        if not path.exists():
            path.write_bytes(raw)
        payloads.append(raw)
        source_outputs.append({
            **source,
            "url": _url(source),
            "local_path": str(path),
            "sha256": _sha256(raw),
            "bytes": len(raw),
        })

    evidence = verify_sources(payloads)
    audit_binding = _validate_audit_binding(
        Path(audit_path), expected_audit_sha256
    )
    observations = resolve_audit_observations()
    observations_path = output_dir / "unrecoverable_observations.csv"
    observations.to_csv(observations_path, index=False)
    rejected_path = output_dir / "rejected_derivations.json"
    rejected_path.write_text(
        json.dumps(rejected_derivations(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    accepted = pd.DataFrame(columns=OUTPUT_COLUMNS)
    accepted_path = output_dir / "accepted_candidate_facts.csv"
    accepted.to_csv(accepted_path, index=False)

    manifest = {
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
        "negative_evidence_source_locked": True,
        "recovery_classification": (
            "UNRECOVERABLE_PREDECESSOR_SUCCESSOR_BASIS_SPLIT"
        ),
        "candidate_rows_created": 0,
        "guardrail": (
            "Do not combine predecessor and successor actual periods, use "
            "unaudited acquisition pro-forma values, substitute quarterly "
            "growth for TTM growth, or backdate the 2019-04-04 filing."
        ),
        "evidence": evidence,
        "audit_binding": audit_binding,
        "outputs": {
            "accepted_candidate_facts": {
                "path": str(accepted_path),
                "sha256": _sha256(accepted_path.read_bytes()),
                "row_count": 0,
            },
            "unrecoverable_observations": {
                "path": str(observations_path),
                "sha256": _sha256(observations_path.read_bytes()),
                "row_count": len(observations),
            },
            "rejected_derivations": {
                "path": str(rejected_path),
                "sha256": _sha256(rejected_path.read_bytes()),
            },
            "sources": source_outputs,
        },
        "fetched_at": FETCHED_AT,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**manifest, "manifest": str(manifest_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--audit-path", type=Path, default=AUDIT_PATH)
    parser.add_argument(
        "--expected-audit-sha256", default=EXPECTED_AUDIT_SHA256
    )
    args = parser.parse_args()
    report = build(
        output_dir=args.output_dir,
        audit_path=args.audit_path,
        expected_audit_sha256=args.expected_audit_sha256,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
