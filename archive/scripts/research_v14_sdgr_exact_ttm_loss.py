#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM loss evidence for SDGR.

The original 2019 10-K contains consolidated net loss that was omitted from
the parsed historical layer.  Combining that same consolidated concept with
the comparative nine-month periods in the contemporaneous 2020Q3 10-Q gives
an exact negative TTM state.  This supplement is exclusion-only: it emits no
quarter, revenue, or growth observation.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen
import warnings

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/sdgr_exact_ttm_loss")
TICKER = "SDGR"
CIK = 1_490_978
CURRENCY = "USD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP"
PIT_CUTOFF = "2021-02-26"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
BASELINE_BINDING = {
    "candidate": (
        "output/research_only/v14/candidate_fundamentals_v14_epix_hcm_"
        "annual_loss_glpg_futu_yy_town_afmd_bldp_dox_mlco_wb_xp_meoh_"
        "rgld_cgc_pntg_xncr_xpel_ftdr_rpay_zi_step_dkng_imab_qfin_zlab_"
        "ccep_ggal_mtls_town"
    ),
    "audit": (
        "output/research_only/v14/step_dkng_imab_qfin_zlab_ccep_ggal_"
        "mtls_town_audit.json"
    ),
    "baseline_missing_financial_observations": 180,
}

SOURCE_DOCUMENTS = {
    "10k_2020_03_16_fy2019": {
        "form": "10-K",
        "filed": "2020-03-16",
        "accepted": "2020-03-16T16:37:08",
        "accession": "0001564590-20-011182",
        "document": "sdgr-10k_20191231.htm",
        "local_path": "sources/sdgr-10k_20191231.htm",
        "expected_sha256": (
            "8e0d3fdedfe4a71c209717ed773475658fa1e0c428431c80d9312ecbca1abc92"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1490978/"
            "000156459020011182/sdgr-10k_20191231.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "AUDITED",
    },
    "10q_2020_11_12_m9": {
        "form": "10-Q",
        "filed": "2020-11-12",
        "accession": "0001564590-20-053292",
        "document": "sdgr-10q_20200930.htm",
        "local_path": "sources/sdgr-10q_20200930.htm",
        "expected_sha256": (
            "0d4aaa841a27e285b6cd4f36c3fd38081f1723ff5ade022ba182ae0b273b3c98"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1490978/"
            "000156459020053292/sdgr-10q_20200930.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "UNAUDITED",
    },
}
ALLOWED_SOURCE_ACCESSIONS = {
    source["accession"] for source in SOURCE_DOCUMENTS.values()
}
REJECTED_LATER_FILINGS = {
    "0001564590-21-010075": {
        "form": "10-K",
        "filed": "2021-03-04",
        "reason": "filed after the 2021-02-26 audited signal date",
    }
}

OPERANDS_USD_THOUSANDS = {
    "fy2019_consolidated_net_loss": {
        "source_id": "10k_2020_03_16_fy2019",
        "period": "FY2019",
        "table_column": "FY2019",
        "line_item": "Net loss",
        "value": -25_681,
    },
    "m9_2019_consolidated_net_loss": {
        "source_id": "10q_2020_11_12_m9",
        "period": "9M 2019",
        "table_column": "9M_2019",
        "line_item": "Net loss",
        "value": -18_536,
    },
    "m9_2020_consolidated_net_loss": {
        "source_id": "10q_2020_11_12_m9",
        "period": "9M 2020",
        "table_column": "9M_2020",
        "line_item": "Net loss",
        "value": -15_051,
    },
}

SOURCE_PARSE_SPECS = {
    "10k_2020_03_16_fy2019": {
        "context_phrases": (
            "Year Ended December 31",
            "Consolidated Statements of Operations Data",
            "Total revenues",
            "2017",
            "2018",
            "2019",
        ),
        "columns": {
            "FY2017": "FY2017",
            "FY2018": "FY2018",
            "FY2019": "FY2019",
        },
    },
    "10q_2020_11_12_m9": {
        "context_phrases": (
            "Nine Months Ended September 30",
            "Cash flows from operating activities",
            "2020",
            "2019",
        ),
        "columns": {"9M_2020": "9M 2020", "9M_2019": "9M 2019"},
    },
}

TTM_SPEC = {
    "fiscal_end": "2020-09-30",
    "available_date": "2020-11-12",
    "formula": "FY2019 - 9M_2019 + 9M_2020",
    "terms": (
        (1, "fy2019_consolidated_net_loss"),
        (-1, "m9_2019_consolidated_net_loss"),
        (1, "m9_2020_consolidated_net_loss"),
    ),
    "expected_usd_thousands": -22_196,
    "form": "10-K_PLUS_10-Q_9M_CUMULATIVE_TTM",
}

AUDIT_OBSERVATIONS = tuple(
    (f"liq{liquidity}-age{age}-growth", "2021-02-26", age)
    for liquidity in (2_000_000, 10_000_000)
    for age in (150, 365, 550)
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    request = Request(url, headers=SEC_HEADERS)
    with urlopen(request, timeout=60) as response:
        return response.read()


def _normalize_text(value: str) -> str:
    return " ".join(
        value.replace("\xa0", " ").replace("−", "-").split()
    ).casefold()


def _row_numbers(row) -> list[int]:
    text = " ".join(row.stripped_strings).replace("\xa0", " ")
    text = re.sub(r"\s*,\s*", ",", text)
    tokens = re.findall(
        r"\(\s*\d[\d,]*\s*\)|(?<![\w])\d[\d,]*(?![\w])",
        text,
    )
    values = []
    for token in tokens:
        digits = re.sub(r"\D", "", token)
        if digits:
            value = int(digits)
            values.append(-value if "(" in token else value)
    return values


def _parse_source_table(source_id: str, raw: bytes) -> dict[str, int]:
    spec = SOURCE_PARSE_SPECS[source_id]
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    expected_count = len(spec["columns"])
    context = tuple(_normalize_text(item) for item in spec["context_phrases"])
    candidates = []
    for table in soup.find_all("table"):
        table_text = _normalize_text(" ".join(table.stripped_strings))
        if not all(item in table_text for item in context):
            continue
        for row in table.find_all("tr"):
            labels = [
                _normalize_text(" ".join(cell.stripped_strings))
                for cell in row.find_all(("td", "th"))
            ]
            first_label = next((item for item in labels if item), "")
            if first_label != "net loss":
                continue
            values = _row_numbers(row)
            if len(values) == expected_count:
                candidates.append(dict(zip(spec["columns"], values, strict=True)))
    if not candidates:
        raise RuntimeError(f"no unambiguous SDGR Net loss table for {source_id}")
    canonical = json.dumps(candidates[0], sort_keys=True)
    if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
        raise RuntimeError(f"conflicting SDGR Net loss tables for {source_id}")
    return candidates[0]


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in documents.items():
        accession = source["accession"]
        if accession in REJECTED_LATER_FILINGS:
            raise ValueError(f"later filing is forbidden: {accession}")
        if accession not in ALLOWED_SOURCE_ACCESSIONS:
            raise ValueError(f"unapproved source accession: {accession}")
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"source {source_id} was filed after PIT cutoff")
        if source["currency"] != CURRENCY or source["scale"] != SOURCE_SCALE:
            raise ValueError(f"source {source_id} has incompatible units")
        if source["accounting_standard"] != ACCOUNTING_STANDARD:
            raise ValueError(f"source {source_id} is not US-GAAP")
        accession_path = accession.replace("-", "")
        if accession_path not in source["url"]:
            raise ValueError(f"source {source_id} URL does not lock accession")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"source {source_id} URL does not lock document")
        relative_path = Path(source["local_path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"source {source_id} has unsafe local_path")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"source {source_id} has invalid SHA-256")
    for operand_id, operand in OPERANDS_USD_THOUSANDS.items():
        if operand["source_id"] not in documents:
            raise ValueError(f"operand {operand_id} has no locked source")


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw source set does not match the source lock")
    parsed = {
        source_id: _parse_source_table(source_id, raw)
        for source_id, raw in raw_by_source.items()
    }
    verified = []
    for operand_id, operand in OPERANDS_USD_THOUSANDS.items():
        source_id = operand["source_id"]
        column = operand["table_column"]
        if SOURCE_PARSE_SPECS[source_id]["columns"][column] != operand["period"]:
            raise RuntimeError(f"operand {operand_id} period mapping changed")
        parsed_value = parsed[source_id][column]
        if parsed_value != int(operand["value"]):
            raise RuntimeError(
                f"operand {operand_id} source changed: parsed {parsed_value}, "
                f"expected {operand['value']}"
            )
        verified.append({
            "operand_id": operand_id,
            "source_id": source_id,
            "period": operand["period"],
            "line_item": operand["line_item"],
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "expected_value": int(operand["value"]),
            "parsed_value": parsed_value,
        })
    return verified


def prepare_verified_sources(
    output_dir: Path,
) -> tuple[dict[str, bytes], dict[str, dict], list[dict]]:
    validate_source_lock()
    raw_by_source = {}
    provenance = {}
    for source_id, source in SOURCE_DOCUMENTS.items():
        local_path = Path(output_dir) / source["local_path"]
        if local_path.exists():
            raw = local_path.read_bytes()
            downloaded = False
        else:
            raw = _download_source(source["url"])
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(raw)
            downloaded = True
        actual_sha256 = _sha256_bytes(raw)
        if actual_sha256 != source["expected_sha256"]:
            raise RuntimeError(
                f"SDGR source SHA-256 mismatch for {source_id}: {actual_sha256}"
            )
        raw_by_source[source_id] = raw
        provenance[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha256,
            "bytes": len(raw),
            "downloaded": downloaded,
        }
    return raw_by_source, provenance, verify_source_values(raw_by_source)


def _source_ids_for_terms(terms: Iterable[tuple[int, str]]) -> list[str]:
    return list(dict.fromkeys(
        OPERANDS_USD_THOUSANDS[operand_id]["source_id"]
        for _, operand_id in terms
    ))


def exact_ttm_evidence() -> dict:
    validate_source_lock()
    value_thousands = sum(
        coefficient * int(OPERANDS_USD_THOUSANDS[operand_id]["value"])
        for coefficient, operand_id in TTM_SPEC["terms"]
    )
    if value_thousands != TTM_SPEC["expected_usd_thousands"]:
        raise RuntimeError("SDGR exact TTM calculation changed")
    if value_thousands >= 0:
        raise RuntimeError("SDGR direct exact-TTM layer is exclusion-only")
    source_ids = _source_ids_for_terms(TTM_SPEC["terms"])
    sources = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
    if TTM_SPEC["available_date"] != max(source["filed"] for source in sources):
        raise RuntimeError("SDGR exact TTM availability date changed")
    return {
        "ticker": TICKER,
        "evidence_kind": "exact_cumulative_ttm_loss_as_reported",
        "fiscal_end": TTM_SPEC["fiscal_end"],
        "available_date": TTM_SPEC["available_date"],
        "currency": CURRENCY,
        "source_scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "net_income_ttm": value_thousands * SOURCE_SCALE,
        "formula": TTM_SPEC["formula"],
        "operand_ids": [operand_id for _, operand_id in TTM_SPEC["terms"]],
        "source_ids": source_ids,
        "source_accessions": [source["accession"] for source in sources],
        "source_urls": [source["url"] for source in sources],
        "form": TTM_SPEC["form"],
        "profit_scope": "consolidated Net loss, including noncontrolling interest",
    }


def direct_ttm_facts(fetched_at: str | None = None) -> pd.DataFrame:
    if fetched_at is None:
        fetched_at = str(
            pd.Timestamp.now("UTC").tz_localize(None).normalize().date()
        )
    evidence = exact_ttm_evidence()
    return pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": evidence["fiscal_end"],
        "available_date": evidence["available_date"],
        "metric": "net_income_ttm",
        "value": evidence["net_income_ttm"],
        "taxonomy": "us-gaap",
        "concept": "sdgr_exact_ttm:ProfitLoss:USD",
        "form": evidence["form"],
        "accession": "+".join(evidence["source_accessions"]),
        "fetched_at": fetched_at,
    }], columns=OUTPUT_COLUMNS)


def resolve_observation(signal_date: str, maximum_age_days: int) -> dict:
    evidence = exact_ttm_evidence()
    signal = pd.Timestamp(signal_date)
    available = pd.Timestamp(evidence["available_date"])
    age = int((signal - available).days)
    if age < 0 or age > maximum_age_days:
        return {
            "resolved": False,
            "decision": "missing_financial",
            "reason": "no exact TTM loss available within the age limit",
        }
    return {
        "resolved": True,
        "decision": "known_nonpositive_profit",
        "fiscal_end": evidence["fiscal_end"],
        "available_date": evidence["available_date"],
        "financial_age_days": age,
        "net_income_ttm": evidence["net_income_ttm"],
        "currency": CURRENCY,
        "source_accessions": evidence["source_accessions"],
    }


def resolve_audit_observations(
    observations: Iterable[tuple[str, str, int]] = AUDIT_OBSERVATIONS,
) -> pd.DataFrame:
    return pd.DataFrame([{
        "scenario": scenario,
        "signal_date": signal_date,
        "maximum_age_days": maximum_age_days,
        **resolve_observation(signal_date, maximum_age_days),
    } for scenario, signal_date, maximum_age_days in observations])


def build(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _, provenance, source_value_verification = prepare_verified_sources(output_dir)
    evidence = exact_ttm_evidence()
    facts = direct_ttm_facts()
    resolutions = resolve_audit_observations()
    if not resolutions["resolved"].all():
        raise RuntimeError("not every declared SDGR audit observation resolved")

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_evidence.json"
    resolution_path = output_dir / "audit_observation_resolution.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    resolution_path.write_text(
        resolutions.to_json(orient="records", indent=2) + "\n",
        encoding="utf-8",
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
        "shared_candidate_integrated": False,
        "ticker": TICKER,
        "cik": CIK,
        "accounting_standard": ACCOUNTING_STANDARD,
        "reporting_currency": CURRENCY,
        "reporting_profile": "US_DOMESTIC_10-K_10-Q",
        "baseline_binding": BASELINE_BINDING,
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "resolved_audit_observation_count": len(resolutions),
        "source_documents": provenance,
        "source_value_verification": source_value_verification,
        "later_filing_rejections": REJECTED_LATER_FILINGS,
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
            "Uses the consolidated Net loss line consistently across the "
            "original FY2019 10-K and comparative 9M periods in the 2020Q3 "
            "10-Q. It does not mix the attributable-to-stockholders loss, "
            "invent quarters or growth, or use the post-signal 2020 10-K."
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
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = build(args.output_dir)
    print(json.dumps({
        "manifest": report["manifest"],
        "accepted_exact_ttm_loss_count": report["accepted_exact_ttm_loss_count"],
        "resolved_unique_signal_date_count": report["resolved_unique_signal_date_count"],
        "resolved_audit_observation_count": report["resolved_audit_observation_count"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
