#!/usr/bin/env python3
"""Recover RLMD's 2020-Q1 TTM loss across its fiscal-year transition."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/rlmd_transition_ttm_loss")
AUDIT_PATH = Path(
    "output/research_only/v14/checkpoint_20260827_sy_glpg.json"
)
EXPECTED_AUDIT_SHA256 = (
    "5eece029f400db82c29b3ddd310572a24ad49fdd9ba420d3033b96f1f5d33cf5"
)
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
TICKER = "RLMD"
CIK = 1_553_643
SIGNALS = ("2020-05-29", "2020-06-30")

SOURCES = (
    {
        "source_id": "fy2019_june_10k",
        "role": "audited fiscal year ended 2019-06-30",
        "form": "10-K",
        "filed": "2019-09-24",
        "accession": "0001213900-19-018787",
        "document": "rlmd-20190630.xml",
        "local_path": "sources/rlmd-20190630.xml",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1553643/"
            "000121390019018787/rlmd-20190630.xml"
        ),
        "expected_sha256": (
            "e85d4093b273d7a9d0b91d9ef8dfd48947e7bbb9aa123156da2f1939048ac5e7"
        ),
    },
    {
        "source_id": "six_month_transition_2019_10kt",
        "role": "audited six-month transition and 2018 comparative",
        "form": "10-KT",
        "filed": "2020-03-26",
        "accession": "0001213900-20-007501",
        "document": "rlmd-20191231.xml",
        "local_path": "sources/rlmd-20191231.xml",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1553643/"
            "000121390020007501/rlmd-20191231.xml"
        ),
        "expected_sha256": (
            "b30cdee8d40ad4f40c544f39bc3878622561b2dea9a92db76a4a7abc60398642"
        ),
    },
    {
        "source_id": "q1_2020_10q",
        "role": "2020-Q1 and 2019-Q1 comparative",
        "form": "10-Q",
        "filed": "2020-05-15",
        "accession": "0001213900-20-012693",
        "document": "rlmd-20200331.xml",
        "local_path": "sources/rlmd-20200331.xml",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1553643/"
            "000121390020012693/rlmd-20200331.xml"
        ),
        "expected_sha256": (
            "ddcda68310513f00584edca3415502dc73ab1f00bd2da609436973a7e12d5633"
        ),
    },
)

EXPECTED_OPERANDS = {
    "fy2019_june": -17_318_060,
    "h2_2018": -10_509_403,
    "h2_2019": -8_196_542,
    "q1_2019": -2_686_065,
    "q1_2020": -10_673_316,
}
EXPECTED_CALENDAR_2019 = -15_005_199
EXPECTED_TTM_2020_Q1 = -22_992_450


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _download(url: str) -> bytes:
    for attempt in range(5):
        try:
            with urlopen(
                Request(url, headers=SEC_HEADERS), timeout=120
            ) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError, OSError):
            if attempt == 4:
                raise
            time.sleep(1.0 + attempt * 2.0)
    raise AssertionError("unreachable RLMD download retry state")


def _context_periods(root: ET.Element) -> dict[str, tuple[str, str]]:
    periods = {}
    for element in root.iter():
        if not element.tag.endswith("context"):
            continue
        context_id = element.attrib.get("id")
        start = end = None
        for child in element.iter():
            if child.tag.endswith("startDate"):
                start = child.text
            elif child.tag.endswith("endDate"):
                end = child.text
        if context_id and start and end:
            periods[context_id] = (start, end)
    return periods


def _net_income_by_period(raw: bytes) -> dict[tuple[str, str], int]:
    root = ET.fromstring(raw)
    periods = _context_periods(root)
    values: dict[tuple[str, str], set[int]] = {}
    for element in root.iter():
        if not element.tag.endswith("NetIncomeLoss"):
            continue
        if element.attrib.get("unitRef") != "USD":
            continue
        period = periods.get(element.attrib.get("contextRef", ""))
        if period is None or element.text is None:
            continue
        values.setdefault(period, set()).add(int(element.text))
    ambiguous = {period: found for period, found in values.items() if len(found) != 1}
    if ambiguous:
        raise RuntimeError(f"ambiguous RLMD NetIncomeLoss facts: {ambiguous}")
    return {period: found.pop() for period, found in values.items()}


def verify_sources(payloads: list[bytes]) -> dict:
    if len(payloads) != len(SOURCES):
        raise ValueError(f"expected {len(SOURCES)} RLMD sources, got {len(payloads)}")
    facts_by_source = {}
    provenance = []
    for source, raw in zip(SOURCES, payloads, strict=True):
        actual_sha = _sha256(raw)
        if actual_sha != source["expected_sha256"]:
            raise RuntimeError(
                f"RLMD source SHA-256 mismatch for {source['source_id']}: "
                f"{actual_sha}"
            )
        facts_by_source[source["source_id"]] = _net_income_by_period(raw)
        provenance.append({**source, "actual_sha256": actual_sha, "bytes": len(raw)})

    fy_period = ("2018-07-01", "2019-06-30")
    operands = {
        "fy2019_june": facts_by_source["fy2019_june_10k"].get(fy_period),
        "h2_2018": facts_by_source["six_month_transition_2019_10kt"].get(
            ("2018-07-01", "2018-12-31")
        ),
        "h2_2019": facts_by_source["six_month_transition_2019_10kt"].get(
            ("2019-07-01", "2019-12-31")
        ),
        "q1_2019": facts_by_source["q1_2020_10q"].get(
            ("2019-01-01", "2019-03-31")
        ),
        "q1_2020": facts_by_source["q1_2020_10q"].get(
            ("2020-01-01", "2020-03-31")
        ),
    }
    transition_fy = facts_by_source["six_month_transition_2019_10kt"].get(
        fy_period
    )
    if operands != EXPECTED_OPERANDS:
        raise RuntimeError(f"RLMD source operands changed: {operands}")
    if transition_fy != operands["fy2019_june"]:
        raise RuntimeError("RLMD FY2019 value does not match across 10-K and 10-KT")

    calendar_2019 = (
        operands["fy2019_june"] - operands["h2_2018"] + operands["h2_2019"]
    )
    ttm_2020_q1 = calendar_2019 - operands["q1_2019"] + operands["q1_2020"]
    if calendar_2019 != EXPECTED_CALENDAR_2019:
        raise RuntimeError(f"RLMD calendar-2019 bridge changed: {calendar_2019}")
    if ttm_2020_q1 != EXPECTED_TTM_2020_Q1:
        raise RuntimeError(f"RLMD 2020-Q1 TTM changed: {ttm_2020_q1}")
    return {
        "sources": provenance,
        "operands_usd": operands,
        "cross_source_fy2019_match": True,
        "calendar_2019_formula": "FY2019_June - H2_2018 + H2_2019",
        "calendar_2019_net_income_usd": calendar_2019,
        "ttm_2020_q1_formula": "Calendar_2019 - Q1_2019 + Q1_2020",
        "net_income_ttm_usd": ttm_2020_q1,
    }


def _load_sources(output_dir: Path) -> tuple[list[bytes], list[dict]]:
    payloads = []
    outputs = []
    for source in SOURCES:
        relative = Path(source["local_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe RLMD source local_path")
        path = output_dir / relative
        if path.exists():
            raw = path.read_bytes()
        else:
            raw = _download(source["url"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        payloads.append(raw)
        outputs.append({
            "source_id": source["source_id"],
            "path": str(path),
            "sha256": _sha256(raw),
        })
    return payloads, outputs


def build(
    output_dir: Path = OUTPUT_DIR,
    audit_path: Path = AUDIT_PATH,
    expected_audit_sha256: str = EXPECTED_AUDIT_SHA256,
) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads, source_outputs = _load_sources(output_dir)
    evidence = verify_sources(payloads)

    audit_path = Path(audit_path)
    audit_sha = _sha256(audit_path.read_bytes())
    if audit_sha != expected_audit_sha256:
        raise RuntimeError(f"RLMD audit binding changed: {audit_sha}")

    facts = pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": "2020-03-31",
        "available_date": "2020-05-15",
        "metric": "net_income_ttm",
        "value": float(EXPECTED_TTM_2020_Q1),
        "taxonomy": "us-gaap",
        "concept": "StrictDirectTTM:NetIncomeLoss:USD",
        "form": "10-K_PLUS_10-KT_PLUS_10-Q_Q1_DIRECT_TTM",
        "accession": "+".join(source["accession"] for source in SOURCES),
        "fetched_at": "2026-08-27",
    }], columns=OUTPUT_COLUMNS)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)

    resolutions = [{
        "ticker": TICKER,
        "signal_date": signal,
        "financial_age_days": int(
            (pd.Timestamp(signal) - pd.Timestamp("2020-05-15")).days
        ),
        "classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "net_income_ttm": float(EXPECTED_TTM_2020_Q1),
    } for signal in SIGNALS]
    resolutions_path = output_dir / "resolved_observations.json"
    resolutions_path.write_text(
        json.dumps(resolutions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

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
        "recovery_classification": "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT",
        "guardrail": (
            "Only exact US-GAAP NetIncomeLoss facts in USD are bridged across "
            "the June-to-December fiscal transition. No revenue, adjusted loss, "
            "quarter split, or post-signal filing is used."
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
                "sha256": _sha256(facts_path.read_bytes()),
            },
            "resolved_observations": {
                "path": str(resolutions_path),
                "sha256": _sha256(resolutions_path.read_bytes()),
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
