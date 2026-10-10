#!/usr/bin/env python3
"""Build source-locked evidence that ADUS's 2020 age-150 gaps are not PIT-recoverable.

The issuer did publish preliminary 2019 and 2020-Q1 figures before the two
signal dates.  It also said that 2017/2018 required re-audit and that prior
period revenue needed revision.  The exact current values therefore cannot be
paired with a reliable same-basis prior period.  The completed 10-K and 10-Q,
including revised comparatives, were not filed until 2020-08-10.  This module
emits no invented quarter or growth fact; it records the rejected derivations.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen
import warnings

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/adus_pit_unrecoverable")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260827_sy_glpg_rlmd_smpl_classified_financial_priorities.csv"
)
EXPECTED_AUDIT_SHA256 = (
    "616ebd6a836bb1f0571ad690fbcd1b0bf56ae06b092041ac406eb976b6243e0e"
)
COMPANYFACTS_CACHE = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/"
    "CIK0001468328.json.gz"
)
TICKER = "ADUS"
CIK = 1_468_328
CURRENCY = "USD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP"
LAST_VALID_FISCAL_END = "2019-09-30"
LAST_VALID_AVAILABLE_DATE = "2019-11-08"
PIT_CUTOFF = "2020-07-31"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}


SOURCE_DOCUMENTS = {
    "10k_2019_03_18_fy2018_original": {
        "role": "pit_source",
        "form": "10-K",
        "filed": "2019-03-18",
        "accession": "0001564590-19-008098",
        "document": "adus-10k_20181231.htm",
        "local_path": "sources/adus-10k_20181231.htm",
        "expected_sha256": (
            "cb6fda1a5eef4ec378473ff8db214fba7d20cd42d99260f4d0c0943366bba08b"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1468328/"
            "000156459019008098/adus-10k_20181231.htm"
        ),
    },
    "10q_2019_11_08_q3_original": {
        "role": "pit_source",
        "form": "10-Q",
        "filed": "2019-11-08",
        "accession": "0001564590-19-042077",
        "document": "adus-10q_20190930.htm",
        "local_path": "sources/adus-10q_20190930.htm",
        "expected_sha256": (
            "8230a179b847512a3c111c72083ea97bf6deb83a0b464ceac41b42bf6fbd88d4"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1468328/"
            "000156459019042077/adus-10q_20190930.htm"
        ),
    },
    "8k_2020_03_17_preliminary_fy2019_ex991": {
        "role": "pit_source",
        "form": "8-K/EX-99.1",
        "filed": "2020-03-17",
        "accession": "0001193125-20-076019",
        "document": "d888790dex991.htm",
        "local_path": "sources/d888790dex991.htm",
        "expected_sha256": (
            "3973cb1ccfe9335da144afaac65e09de1721bb47b1b77bf448f6470a8f330ba6"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1468328/"
            "000119312520076019/d888790dex991.htm"
        ),
    },
    "8k_2020_05_04_preliminary_q1_ex991": {
        "role": "pit_source",
        "form": "8-K/EX-99.1",
        "filed": "2020-05-04",
        "accession": "0001193125-20-132428",
        "document": "d912387dex991.htm",
        "local_path": "sources/d912387dex991.htm",
        "expected_sha256": (
            "7f76d3dbcea4bfb00f6b61532320a37e519d622d3051609f4a58d8bc782db553"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1468328/"
            "000119312520132428/d912387dex991.htm"
        ),
    },
    "10k_2020_08_10_fy2019_revised_later": {
        "role": "later_corroboration_only",
        "form": "10-K",
        "filed": "2020-08-10",
        "accession": "0001564590-20-038909",
        "document": "adus-10k_20191231.htm",
        "local_path": "sources/adus-10k_20191231_filed_20200810.htm",
        "expected_sha256": (
            "8c9f9412d88ab9e6fa45eb6f59f7de89f83feda4d1249fe0c22289bb0529fbdd"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1468328/"
            "000156459020038909/adus-10k_20191231.htm"
        ),
    },
    "10q_2020_08_10_q1_revised_later": {
        "role": "later_corroboration_only",
        "form": "10-Q",
        "filed": "2020-08-10",
        "accession": "0001564590-20-038948",
        "document": "adus-10q_20200331.htm",
        "local_path": "sources/adus-10q_20200331_filed_20200810.htm",
        "expected_sha256": (
            "96ea6ee5e123a3355e54c7337dec8e78083b7f3a64e4eca64bcfef34234e163d"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1468328/"
            "000156459020038948/adus-10q_20200331.htm"
        ),
    },
}


SOURCE_ROW_CHECKS = {
    "10k_2019_03_18_fy2018_original": (
        {
            "metric": "revenue",
            "line_item": "Net service revenues - continuing operations",
            "periods": ("FY2018 original", "FY2017 original", "FY2016"),
            "expected_values": (518_119, 425_994, 400_929),
        },
        {
            "metric": "net_income",
            "line_item": "Net income",
            "periods": ("FY2018 original", "FY2017 original", "FY2016"),
            "expected_values": (17_503, 13_681, 12_160),
        },
    ),
    "10q_2019_11_08_q3_original": (
        {
            "metric": "revenue",
            "line_item": "Net service revenues",
            "periods": ("Q3 2019", "Q3 2018", "9M 2019", "9M 2018"),
            "expected_values": (169_803, 137_716, 458_749, 378_449),
        },
        {
            "metric": "net_income",
            "line_item": "Net income",
            "periods": ("Q3 2019", "Q3 2018", "9M 2019", "9M 2018"),
            "expected_values": (4_867, 3_631, 15_247, 12_835),
        },
    ),
    "8k_2020_03_17_preliminary_fy2019_ex991": (
        {
            "metric": "revenue",
            "line_item": "Net service revenues",
            "periods": ("Q4 2019 preliminary", "FY2019 preliminary"),
            "expected_values": (192_377, 648_791),
        },
        {
            "metric": "net_income",
            "line_item": "Net income",
            "periods": ("Q4 2019 preliminary", "FY2019 preliminary"),
            "expected_values": (10_735, 25_237),
        },
    ),
    "8k_2020_05_04_preliminary_q1_ex991": (
        {
            "metric": "net_income",
            "line_item": "Net income",
            "periods": ("Q1 2020 preliminary",),
            "expected_values": (8_658,),
        },
    ),
    "10k_2020_08_10_fy2019_revised_later": (
        {
            "metric": "revenue",
            "line_item": "Net service revenues - continuing operations",
            "periods": ("FY2019 revised", "FY2018 revised", "FY2017 revised"),
            "expected_values": (648_791, 516_647, 425_994),
        },
        {
            "metric": "net_income",
            "line_item": "Net income",
            "periods": ("FY2019 revised", "FY2018 revised", "FY2017 revised"),
            "expected_values": (25_237, 16_433, 11_953),
        },
    ),
    "10q_2020_08_10_q1_revised_later": (
        {
            "metric": "revenue",
            "line_item": "Net service revenues",
            "periods": ("Q1 2020", "Q1 2019 revised"),
            "expected_values": (190_216, 138_507),
        },
        {
            "metric": "net_income",
            "line_item": "Net income",
            "periods": ("Q1 2020", "Q1 2019 revised"),
            "expected_values": (8_658, 4_296),
        },
    ),
}


SOURCE_TEXT_CHECKS = {
    "8k_2020_03_17_preliminary_fy2019_ex991": (
        "preliminary unaudited financial and operational data",
        "total revenue reduction",
        "approximately $10 million to $12 million",
        "for periods 2009 to 2018",
        "re-audit our financial statements",
        "years ended December 31, 2017 and 2018",
    ),
    "8k_2020_05_04_preliminary_q1_ex991": (
        "preliminary financial results for the first quarter ended March 31, 2020",
        "preliminary estimates that are subject to change and finalization",
        "Net service revenues were $190.2 million",
    ),
    "10q_2020_08_10_q1_revised_later": (
        "Revision of Previously Issued Financial Statements",
        "revised previously issued financial statements",
    ),
}


AUDIT_OBSERVATIONS = (
    ("liq10000000-age150-growth", "2020-05-29", 150),
    ("liq2000000-age150-growth", "2020-05-29", 150),
    ("liq2000000-age150-growth", "2020-07-31", 150),
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _download_source(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalize_text(value: str) -> str:
    return " ".join(
        value.replace("\xa0", " ")
        .replace("–", "-")
        .replace("—", "-")
        .replace("−", "-")
        .split()
    ).casefold()


def _integer_cell(cell_text: str) -> int | None:
    text = "".join(cell_text.replace("\xa0", " ").split())
    match = re.fullmatch(r"\(?-?\d[\d,]*\)?", text)
    if match is None:
        return None
    value = int(re.sub(r"[^0-9]", "", text))
    return -value if text.startswith("(") or text.startswith("-") else value


def _matching_rows(raw: bytes, line_item: str) -> list[tuple[int, ...]]:
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    normalized_label = _normalize_text(line_item)
    matches = []
    for row in soup.find_all("tr"):
        cells = row.find_all(("td", "th"), recursive=False)
        labels = [_normalize_text(cell.get_text(" ", strip=True)) for cell in cells]
        first_label = next((label for label in labels if label), "")
        if first_label != normalized_label:
            continue
        values = tuple(
            value
            for cell in cells
            if (value := _integer_cell(cell.get_text(" ", strip=True))) is not None
        )
        if values:
            matches.append(values)
    return matches


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("ADUS source set changed")
    accessions = set()
    for source_id, source in documents.items():
        if source["accession"] in accessions:
            raise ValueError("duplicate ADUS source accession")
        accessions.add(source["accession"])
        expected_role = (
            "pit_source" if source["filed"] <= PIT_CUTOFF
            else "later_corroboration_only"
        )
        if source["role"] != expected_role:
            raise ValueError(f"ADUS source role violates PIT cutoff: {source_id}")
        if source["accession"].replace("-", "") not in source["url"]:
            raise ValueError(f"ADUS source URL does not lock accession: {source_id}")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"ADUS source URL does not lock document: {source_id}")
        path = Path(source["local_path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe ADUS source local_path: {source_id}")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"invalid ADUS expected SHA-256: {source_id}")
    for source_id in SOURCE_ROW_CHECKS | SOURCE_TEXT_CHECKS:
        if source_id not in documents:
            raise ValueError(f"ADUS verification has no source: {source_id}")


def verify_source_evidence(raw_by_source: dict[str, bytes]) -> dict:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw ADUS source set does not match source lock")
    operands = []
    for source_id, checks in SOURCE_ROW_CHECKS.items():
        raw = raw_by_source[source_id]
        for check in checks:
            expected_values = tuple(check["expected_values"])
            matches = _matching_rows(raw, check["line_item"])
            if expected_values not in matches:
                raise RuntimeError(
                    f"ADUS source row changed for {source_id} "
                    f"{check['line_item']}: expected {expected_values}, got {matches[:5]}"
                )
            if len(check["periods"]) != len(expected_values):
                raise RuntimeError("ADUS period/value mapping length changed")
            for period, value in zip(check["periods"], expected_values, strict=True):
                operands.append({
                    "source_id": source_id,
                    "metric": check["metric"],
                    "line_item": check["line_item"],
                    "period": period,
                    "currency": CURRENCY,
                    "scale": SOURCE_SCALE,
                    "parsed_value": value,
                })
    fragments = []
    for source_id, expected_fragments in SOURCE_TEXT_CHECKS.items():
        soup = BeautifulSoup(raw_by_source[source_id], "lxml")
        document_text = _normalize_text(soup.get_text(" ", strip=True))
        for fragment in expected_fragments:
            if _normalize_text(fragment) not in document_text:
                raise RuntimeError(
                    f"ADUS source disclosure changed for {source_id}: {fragment}"
                )
            fragments.append({"source_id": source_id, "fragment": fragment})
    return {"operands": operands, "text_fragments": fragments}


def prepare_verified_sources(
    output_dir: Path,
) -> tuple[dict[str, dict], dict]:
    validate_source_lock()
    raw_by_source = {}
    provenance = {}
    for source_id, source in SOURCE_DOCUMENTS.items():
        local_path = Path(output_dir) / source["local_path"]
        if local_path.exists():
            raw = local_path.read_bytes()
        else:
            raw = _download_source(source["url"])
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(raw)
        actual_sha = _sha256_bytes(raw)
        if actual_sha != source["expected_sha256"]:
            raise RuntimeError(
                f"ADUS source SHA-256 mismatch for {source_id}: {actual_sha}"
            )
        raw_by_source[source_id] = raw
        provenance[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha,
            "bytes": len(raw),
        }
    return provenance, verify_source_evidence(raw_by_source)


def companyfacts_pit_audit(
    cache_path: Path = COMPANYFACTS_CACHE,
    cutoff: str = PIT_CUTOFF,
) -> dict:
    cache_path = Path(cache_path)
    raw = cache_path.read_bytes()
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        document = json.load(handle)
    payload = document.get("payload", document)
    if int(payload["cik"]) != CIK:
        raise ValueError("Company Facts cache is not ADUS")
    concepts = (
        "NetIncomeLoss",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueServicesNet",
    )
    duration_facts = []
    for concept in concepts:
        concept_data = payload.get("facts", {}).get("us-gaap", {}).get(concept, {})
        for fact in concept_data.get("units", {}).get("USD", []):
            if (
                fact.get("start")
                and fact.get("form") in {"10-K", "10-Q"}
                and fact.get("filed", "9999-99-99") <= cutoff
            ):
                duration_facts.append({"concept": concept, **fact})
    if not duration_facts:
        raise RuntimeError("ADUS Company Facts has no PIT duration facts")
    latest_filed = max(fact["filed"] for fact in duration_facts)
    latest_facts = [fact for fact in duration_facts if fact["filed"] == latest_filed]
    latest_end = max(fact["end"] for fact in latest_facts)
    if latest_filed != LAST_VALID_AVAILABLE_DATE or latest_end != LAST_VALID_FISCAL_END:
        raise RuntimeError("ADUS Company Facts PIT boundary changed")
    accessions = sorted({fact["accn"] for fact in latest_facts})
    return {
        "cache_path": str(cache_path),
        "actual_sha256": _sha256_bytes(raw),
        "cache_fetched_at": document.get("fetched_at"),
        "source_url": document.get("source_url"),
        "cutoff": cutoff,
        "latest_duration_fact_filed": latest_filed,
        "latest_duration_fact_end": latest_end,
        "latest_duration_fact_accessions": accessions,
        "qualifying_duration_fact_count": len(duration_facts),
        "conclusion": "no 10-K/10-Q duration fact after 2019-11-08 by cutoff",
    }


def rejected_derivations() -> list[dict]:
    return [
        {
            "candidate": "FY2019 exact annual growth",
            "current_period": "FY2019 preliminary",
            "available_date": "2020-03-17",
            "current_revenue": 648_791_000,
            "current_net_income": 25_237_000,
            "old_prior_revenue": 518_119_000,
            "old_prior_net_income": 17_503_000,
            "later_revised_prior_revenue": 516_647_000,
            "later_revised_prior_net_income": 16_433_000,
            "later_revision_filed": "2020-08-10",
            "rejected": True,
            "reason": (
                "the 2020-03-17 issuer disclosure made the FY2018 comparator "
                "unreliable pending re-audit; the revised comparator was filed "
                "only after both signal dates"
            ),
        },
        {
            "candidate": "Q1-2020 exact TTM growth",
            "formula": "FY2019 + Q1-2020 - Q1-2019",
            "available_date": "2020-05-04",
            "preliminary_net_income_ttm_using_old_q1": 29_033_000,
            "preliminary_q1_revenue_disclosure": "$190.2 million rounded",
            "old_q1_2019_net_income": 4_862_000,
            "later_revised_q1_2019_net_income": 4_296_000,
            "later_exact_q1_2020_revenue": 190_216_000,
            "later_revision_filed": "2020-08-10",
            "rejected": True,
            "reason": (
                "the press release lacks exact revenue and a reliable same-basis "
                "prior Q1; mixing the old comparator with revised FY2019 would "
                "manufacture a growth package"
            ),
        },
    ]


def strict_quarterly_facts() -> pd.DataFrame:
    """Return the intentionally empty supplement; no strict fact is recoverable."""
    return pd.DataFrame(columns=OUTPUT_COLUMNS)


def resolve_audit_observations(
    observations: Iterable[tuple[str, str, int]] = AUDIT_OBSERVATIONS,
) -> pd.DataFrame:
    last_available = pd.Timestamp(LAST_VALID_AVAILABLE_DATE)
    rows = []
    for scenario, signal_date, maximum_age_days in observations:
        signal = pd.Timestamp(signal_date)
        age = int((signal - last_available).days)
        rows.append({
            "scenario": scenario,
            "signal_date": signal_date,
            "maximum_age_days": maximum_age_days,
            "latest_valid_fiscal_end": LAST_VALID_FISCAL_END,
            "latest_valid_available_date": LAST_VALID_AVAILABLE_DATE,
            "financial_age_days": age,
            "resolved": False,
            "decision": "unrecoverable_reaudit_comparator_not_available",
            "reason": (
                "latest valid quarterly growth snapshot is stale; preliminary "
                "newer figures cannot form a same-basis exact TTM growth bundle"
            ),
        })
    return pd.DataFrame(rows)


def validate_unrecoverable_conclusion() -> None:
    facts = strict_quarterly_facts()
    if not facts.empty:
        raise RuntimeError("ADUS unrecoverable audit must not emit facts")
    rejected = rejected_derivations()
    if not all(item["rejected"] for item in rejected):
        raise RuntimeError("ADUS rejected derivation unexpectedly accepted")
    if any(item["current_net_income"] <= 0 for item in rejected if "current_net_income" in item):
        raise RuntimeError("ADUS preliminary annual profit sign changed")
    observations = resolve_audit_observations()
    if observations["resolved"].any():
        raise RuntimeError("ADUS gap unexpectedly marked recoverable")
    if not observations["financial_age_days"].gt(
        observations["maximum_age_days"]
    ).all():
        raise RuntimeError("ADUS stale-snapshot classification changed")


def validate_audit_binding(path: Path, expected_sha256: str) -> dict:
    path = Path(path)
    actual_sha = _sha256_bytes(path.read_bytes())
    if actual_sha != expected_sha256:
        raise RuntimeError(f"ADUS audit binding changed: {actual_sha}")
    priorities = pd.read_csv(path)
    expected = pd.DataFrame(
        AUDIT_OBSERVATIONS,
        columns=["scenario", "signal_date", "maximum_age_days"],
    )
    expected_counts = (
        expected.groupby("scenario")["signal_date"]
        .agg(["count", "min", "max"])
        .to_dict("index")
    )
    rows = priorities.loc[
        priorities["ticker"].eq(TICKER)
        & priorities["scenario"].isin(expected_counts)
    ]
    if set(rows["scenario"]) != set(expected_counts) or len(rows) != len(
        expected_counts
    ):
        raise RuntimeError("ADUS priority scenarios changed")
    for row in rows.to_dict("records"):
        expected_row = expected_counts[row["scenario"]]
        if int(row["missing_signal_count"]) != int(expected_row["count"]):
            raise RuntimeError("ADUS priority missing-signal count changed")
        if row["first_missing_signal_date"] != expected_row["min"]:
            raise RuntimeError("ADUS first missing signal changed")
        if row["last_missing_signal_date"] != expected_row["max"]:
            raise RuntimeError("ADUS last missing signal changed")
        if int(row["stale_growth_snapshot_signal_count"]) != int(
            expected_row["count"]
        ):
            raise RuntimeError("ADUS priority stale-snapshot class changed")
    return {
        "path": str(path),
        "sha256": actual_sha,
        "scenario_count": len(rows),
        "missing_observation_count": len(AUDIT_OBSERVATIONS),
        "signals": sorted(expected["signal_date"].unique()),
    }


def build(
    output_dir: Path = OUTPUT_DIR,
    companyfacts_cache: Path = COMPANYFACTS_CACHE,
    audit_path: Path = AUDIT_PATH,
    expected_audit_sha256: str = EXPECTED_AUDIT_SHA256,
) -> dict:
    validate_unrecoverable_conclusion()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    provenance, source_evidence = prepare_verified_sources(output_dir)
    companyfacts = companyfacts_pit_audit(companyfacts_cache)
    audit_binding = validate_audit_binding(
        audit_path, expected_audit_sha256
    )
    facts = strict_quarterly_facts()
    observations = resolve_audit_observations()
    rejected = rejected_derivations()

    facts_path = output_dir / "strict_quarterly_facts.csv"
    observation_path = output_dir / "unrecoverable_observations.csv"
    rejected_path = output_dir / "rejected_derivations.json"
    manifest_path = output_dir / "manifest.json"
    facts.to_csv(facts_path, index=False)
    observations.to_csv(observation_path, index=False)
    rejected_path.write_text(
        json.dumps(rejected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = {
        "schema_version": 2,
        "research_only": True,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": False,
        "formal_financials_modified": False,
        "point_in_time_proven": True,
        "negative_evidence_source_locked": True,
        "recovery_classification": (
            "UNRECOVERABLE_REAUDIT_COMPARATOR_NOT_AVAILABLE"
        ),
        "ticker": TICKER,
        "cik": CIK,
        "currency": CURRENCY,
        "source_scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "pit_cutoff": PIT_CUTOFF,
        "accepted_strict_fact_count": len(facts),
        "resolved_audit_observation_count": int(observations["resolved"].sum()),
        "unrecoverable_audit_observation_count": len(observations),
        "unique_signal_dates": sorted(observations["signal_date"].unique()),
        "rejected_derivation_count": len(rejected),
        "source_operand_verification_count": len(source_evidence["operands"]),
        "source_text_verification_count": len(source_evidence["text_fragments"]),
        "companyfacts_pit_audit": companyfacts,
        "audit_binding": audit_binding,
        "sources": provenance,
        "source_operands": source_evidence["operands"],
        "source_text_fragments": source_evidence["text_fragments"],
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256_bytes(facts_path.read_bytes()),
                "row_count": len(facts),
            },
            "unrecoverable_observations": {
                "path": str(observation_path),
                "sha256": _sha256_bytes(observation_path.read_bytes()),
                "row_count": len(observations),
            },
            "rejected_derivations": {
                "path": str(rejected_path),
                "sha256": _sha256_bytes(rejected_path.read_bytes()),
                "row_count": len(rejected),
            },
        },
        "conclusion": (
            "No exact same-basis growth bundle or exact negative TTM was "
            "available by either signal date; no supplement facts emitted."
        ),
    }
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--companyfacts-cache", type=Path, default=COMPANYFACTS_CACHE
    )
    parser.add_argument("--audit-path", type=Path, default=AUDIT_PATH)
    parser.add_argument(
        "--expected-audit-sha256", default=EXPECTED_AUDIT_SHA256
    )
    args = parser.parse_args()
    print(json.dumps(build(
        args.output_dir,
        args.companyfacts_cache,
        args.audit_path,
        args.expected_audit_sha256,
    ), indent=2))


if __name__ == "__main__":
    main()
