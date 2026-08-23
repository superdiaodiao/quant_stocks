#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM loss evidence for ALLT.

Allot was a U.S.-GAAP foreign private issuer during the audited period. This
supplement preserves its FY2019, 9M 2020, and FY2020 disclosures as distinct
point-in-time versions, emits only direct ``net_income_ttm`` loss states, and
never manufactures quarterly or growth observations.
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


OUTPUT_DIR = Path("output/research_only/v14/allt_exact_ttm_loss")
TICKER = "ALLT"
CIK = 1_365_767
CURRENCY = "USD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP"
PIT_CUTOFF = "2021-02-26"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
BASELINE_BINDING = {
    "quarterly": (
        "output/research_only/v14/candidate_fundamentals_v14_epix_hcm_annual_loss_"
        "glpg_futu_yy_town_afmd_bldp_dox_mlco_wb_xp_meoh_rgld_cgc_pntg_xncr_"
        "xpel_ftdr_rpay_zi_step_dkng_imab_qfin_zlab_ccep_ggal_mtls_town/"
        "quarterly.csv"
    ),
    "audit": "step_dkng_imab_qfin_zlab_ccep_ggal_mtls_town_audit",
    "baseline_reason": "no_raw_pit_financial_facts",
}

SOURCE_DOCUMENTS = {
    "20f_2020_03_26_fy2019_r4": {
        "form": "20-F/R4",
        "filed": "2020-03-26",
        "accession": "0001178913-20-000943",
        "document": "R4.htm",
        "local_path": "sources/allt_2019_20f_R4.htm",
        "expected_sha256": (
            "8e6e06e3f18026b17405b998743ad33f92630cec49fed0ac179b4f5cb2e101b1"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1365767/"
            "000117891320000943/R4.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "AUDITED",
    },
    "6k_2020_11_04_9m_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2020-11-04",
        "accession": "0001178913-20-002985",
        "document": "exhibit_99-1.htm",
        "local_path": "sources/allt_2020_9m_exhibit_99-1.htm",
        "expected_sha256": (
            "c71c2c3e8e51afada99530665cd48eab3d10c277899abad56070e1bb16898f4b"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1365767/"
            "000117891320002985/exhibit_99-1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "UNAUDITED",
    },
    "6k_2021_02_09_fy2020_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2021-02-09",
        "accession": "0001178913-21-000386",
        "document": "exhibit_99-1.htm",
        "local_path": "sources/allt_2020_fy_exhibit_99-1.htm",
        "expected_sha256": (
            "c5ee5cee0f447ee885de525e15a6f78633d228a6f176293e890e8e0a904fdf70"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1365767/"
            "000117891321000386/exhibit_99-1.htm"
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

NON_FINANCIAL_AMENDMENT = {
    "form": "20-F/A",
    "filed": "2020-07-01",
    "accession": "0001178913-20-001927",
    "document": "zk2024617.htm",
    "url": (
        "https://www.sec.gov/Archives/edgar/data/1365767/"
        "000117891320001927/zk2024617.htm"
    ),
    "effect": "AUDITOR_REPORT_DATE_TYPO_ONLY_NO_FINANCIAL_CHANGE",
    "detail": "corrected March 26, 2019 to March 26, 2020",
}

OPERANDS_USD_THOUSANDS = {
    "fy2019_net_loss": {
        "source_id": "20f_2020_03_26_fy2019_r4",
        "period": "FY2019",
        "table_column": "FY2019",
        "line_item": "Net loss",
        "value": -8_659,
    },
    "m9_2019_net_loss": {
        "source_id": "6k_2020_11_04_9m_ex991",
        "period": "9M 2019",
        "table_column": "M9_2019",
        "line_item": "Net Loss",
        "value": -6_977,
    },
    "m9_2020_net_loss": {
        "source_id": "6k_2020_11_04_9m_ex991",
        "period": "9M 2020",
        "table_column": "M9_2020",
        "line_item": "Net Loss",
        "value": -7_667,
    },
    "fy2020_net_loss": {
        "source_id": "6k_2021_02_09_fy2020_ex991",
        "period": "FY2020",
        "table_column": "FY2020",
        "line_item": "Net Loss",
        "value": -9_348,
    },
}

SOURCE_PARSE_SPECS = {
    "20f_2020_03_26_fy2019_r4": {
        "context_phrases": (
            "CONSOLIDATED STATEMENTS OF COMPREHENSIVE LOSS",
            "USD ($)",
            "$ in Thousands",
            "12 Months Ended",
            "Dec. 31, 2019",
            "Dec. 31, 2018",
            "Dec. 31, 2017",
        ),
        "identity_phrases": (
            "CONSOLIDATED STATEMENTS OF COMPREHENSIVE LOSS",
            "12 Months Ended",
        ),
        "columns": {
            "FY2019": "FY2019",
            "FY2018": "FY2018",
            "FY2017": "FY2017",
        },
        "row_labels": {
            "net_income": "Net loss",
        },
    },
    "6k_2020_11_04_9m_ex991": {
        "context_phrases": (
            "Three Months Ended",
            "Nine Months Ended",
            "September 30",
            "2020",
            "2019",
            "Unaudited",
        ),
        "identity_phrases": (
            "Allot Announces Third Quarter 2020 Financial Results",
            "Allot Ltd.",
            "NASDAQ: ALLT",
            "November 4, 2020",
            "U.S. dollars in thousands",
        ),
        "columns": {
            "Q3_2020": "Q3 2020",
            "Q3_2019": "Q3 2019",
            "M9_2020": "9M 2020",
            "M9_2019": "9M 2019",
        },
        "row_labels": {
            "net_income": "Net Loss",
        },
    },
    "6k_2021_02_09_fy2020_ex991": {
        "context_phrases": (
            "Three Months Ended",
            "Year Ended",
            "December 31",
            "2020",
            "2019",
            "Unaudited",
        ),
        "identity_phrases": (
            "Allot Announces Fourth Quarter & Full Year 2020 Financial Results",
            "Allot Ltd.",
            "NASDAQ: ALLT",
            "February 9, 2021",
            "U.S. dollars in thousands",
        ),
        "columns": {
            "Q4_2020": "Q4 2020",
            "Q4_2019": "Q4 2019",
            "FY2020": "FY2020",
            "FY2019": "FY2019",
        },
        "row_labels": {
            "net_income": "Net Loss",
        },
    },
}

TTM_SPECS = {
    "2020-09-30": {
        "available_date": "2020-11-04",
        "formula": "FY2019 - 9M_2019 + 9M_2020",
        "terms": (
            (1, "fy2019_net_loss"),
            (-1, "m9_2019_net_loss"),
            (1, "m9_2020_net_loss"),
        ),
        "expected_usd_thousands": -9_349,
        "form": "20-F_PLUS_6-K_9M_CUMULATIVE_TTM",
    },
    "2020-12-31": {
        "available_date": "2021-02-09",
        "formula": "FY2020_DIRECT",
        "terms": ((1, "fy2020_net_loss"),),
        "expected_usd_thousands": -9_348,
        "form": "6-K_FULL_YEAR_RESULTS",
    },
}

AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", "2021-01-29", 150),
    ("liq2000000-age150-growth", "2021-02-26", 150),
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
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


def _parse_source_tables(source_id: str, raw: bytes) -> dict[str, dict]:
    spec = SOURCE_PARSE_SPECS[source_id]
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    document_text = _normalize_text(" ".join(soup.stripped_strings))
    if any(
        _normalize_text(phrase) not in document_text
        for phrase in spec["identity_phrases"]
    ):
        raise RuntimeError(f"ALLT source identity changed for {source_id}")
    context = tuple(_normalize_text(item) for item in spec["context_phrases"])
    expected_count = len(spec["columns"])
    parsed = {}
    for metric, label in spec["row_labels"].items():
        normalized_label = _normalize_text(label)
        candidates = []
        for table in soup.find_all("table"):
            table_text = _normalize_text(" ".join(table.stripped_strings))
            if not all(item in table_text for item in context):
                continue
            for row in table.find_all("tr"):
                cells = row.find_all(("td", "th"))
                labels = [
                    _normalize_text(" ".join(cell.stripped_strings))
                    for cell in cells
                ]
                first_label = next((item for item in labels if item), "")
                if first_label != normalized_label:
                    continue
                values = _row_numbers(row)
                if len(values) >= expected_count:
                    candidates.append(dict(zip(
                        spec["columns"], values[-expected_count:], strict=True
                    )))
        if not candidates:
            raise RuntimeError(
                f"no unambiguous ALLT {metric} table for {source_id}"
            )
        canonical = json.dumps(candidates[0], sort_keys=True)
        if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
            raise RuntimeError(f"conflicting ALLT {metric} tables for {source_id}")
        parsed[metric] = candidates[0]
    return parsed


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in documents.items():
        accession = source["accession"]
        if accession not in ALLOWED_SOURCE_ACCESSIONS:
            raise ValueError(f"unapproved source accession: {accession}")
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"source {source_id} was filed after PIT cutoff")
        if source["currency"] != CURRENCY or source["scale"] != SOURCE_SCALE:
            raise ValueError(f"source {source_id} has mixed currency or scale")
        if source["accounting_standard"] != ACCOUNTING_STANDARD:
            raise ValueError(f"source {source_id} is not US-GAAP")
        accession_path = accession.replace("-", "")
        if f"/data/{CIK}/{accession_path}/" not in source["url"]:
            raise ValueError(f"source {source_id} URL does not lock CIK/accession")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"source {source_id} URL does not lock document")
        relative_path = Path(source["local_path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"source {source_id} has unsafe local_path")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"source {source_id} has invalid expected SHA-256")
    for item_id, item in OPERANDS_USD_THOUSANDS.items():
        if item["source_id"] not in documents:
            raise ValueError(f"source value {item_id} has no locked source")


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw source set does not match the source lock")
    parsed = {
        source_id: _parse_source_tables(source_id, raw)
        for source_id, raw in raw_by_source.items()
    }
    verified = []
    expected_items = OPERANDS_USD_THOUSANDS
    for item_id, item in expected_items.items():
        source_id = item["source_id"]
        column = item["table_column"]
        source_spec = SOURCE_PARSE_SPECS[source_id]
        if column not in source_spec["columns"]:
            raise RuntimeError(f"source value {item_id} has no parsed period")
        if source_spec["columns"][column] != item["period"]:
            raise RuntimeError(f"source value {item_id} period mapping changed")
        metric = "net_income"
        if _normalize_text(source_spec["row_labels"][metric]) != (
            _normalize_text(item["line_item"])
        ):
            raise RuntimeError(f"source value {item_id} line item mapping changed")
        parsed_value = parsed[source_id][metric][column]
        expected_value = int(item["value"])
        if parsed_value != expected_value:
            raise RuntimeError(
                f"source value {item_id} changed: parsed {parsed_value}, "
                f"expected {expected_value}"
            )
        verified.append({
            "item_id": item_id,
            "source_id": source_id,
            "metric": metric,
            "period": item["period"],
            "table_column": column,
            "line_item": item["line_item"],
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "expected_value": expected_value,
            "parsed_value": parsed_value,
        })
    return verified


def prepare_verified_sources(
    output_dir: Path,
) -> tuple[dict[str, bytes], dict[str, dict], list[dict]]:
    validate_source_lock()
    output_dir = Path(output_dir)
    raw_by_source = {}
    provenance = {}
    for source_id, source in SOURCE_DOCUMENTS.items():
        local_path = output_dir / source["local_path"]
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
                f"ALLT source SHA-256 mismatch for {source_id}: {actual_sha256}"
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


def exact_ttm_evidence() -> list[dict]:
    validate_source_lock()
    rows = []
    for fiscal_end, spec in TTM_SPECS.items():
        value_thousands = sum(
            coefficient * int(OPERANDS_USD_THOUSANDS[operand_id]["value"])
            for coefficient, operand_id in spec["terms"]
        )
        if value_thousands != spec["expected_usd_thousands"]:
            raise RuntimeError(f"ALLT exact TTM changed for {fiscal_end}")
        if value_thousands >= 0:
            raise RuntimeError("ALLT direct exact-TTM layer is exclusion-only")
        source_ids = _source_ids_for_terms(spec["terms"])
        sources = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
        if spec["available_date"] != max(source["filed"] for source in sources):
            raise RuntimeError(f"ALLT availability date changed for {fiscal_end}")
        rows.append({
            "ticker": TICKER,
            "evidence_kind": "exact_cumulative_ttm_loss_as_reported",
            "fiscal_end": fiscal_end,
            "available_date": spec["available_date"],
            "currency": CURRENCY,
            "source_scale": SOURCE_SCALE,
            "accounting_standard": ACCOUNTING_STANDARD,
            "net_income_ttm": value_thousands * SOURCE_SCALE,
            "formula": spec["formula"],
            "operand_ids": [operand_id for _, operand_id in spec["terms"]],
            "source_ids": source_ids,
            "source_accessions": [source["accession"] for source in sources],
            "source_urls": [source["url"] for source in sources],
            "form": spec["form"],
            "profit_scope": (
                "consolidated GAAP NetIncomeLoss; not per-share or non-GAAP"
            ),
        })
    return rows


def direct_ttm_facts(fetched_at: str | None = None) -> pd.DataFrame:
    if fetched_at is None:
        fetched_at = str(
            pd.Timestamp.now("UTC").tz_localize(None).normalize().date()
        )
    rows = [{
        "ticker": TICKER,
        "fiscal_end": evidence["fiscal_end"],
        "available_date": evidence["available_date"],
        "metric": "net_income_ttm",
        "value": evidence["net_income_ttm"],
        "taxonomy": "us-gaap",
        "concept": "allt_exact_ttm:NetIncomeLoss:USD",
        "form": evidence["form"],
        "accession": "+".join(evidence["source_accessions"]),
        "fetched_at": fetched_at,
    } for evidence in exact_ttm_evidence()]
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "available_date"]
    ).reset_index(drop=True)


def resolve_observation(signal_date: str, maximum_age_days: int) -> dict:
    signal = pd.Timestamp(signal_date)
    evidence = pd.DataFrame(exact_ttm_evidence())
    evidence["fiscal_end"] = pd.to_datetime(evidence["fiscal_end"])
    evidence["available_date"] = pd.to_datetime(evidence["available_date"])
    eligible = evidence.loc[
        evidence["available_date"].le(signal)
        & (signal - evidence["available_date"]).dt.days.le(maximum_age_days)
    ].sort_values(["fiscal_end", "available_date"])
    if eligible.empty:
        return {
            "resolved": False,
            "decision": "missing_financial",
            "reason": "no exact TTM loss available within the age limit",
        }
    row = eligible.iloc[-1]
    return {
        "resolved": True,
        "decision": "known_nonpositive_profit",
        "fiscal_end": row["fiscal_end"].strftime("%Y-%m-%d"),
        "available_date": row["available_date"].strftime("%Y-%m-%d"),
        "financial_age_days": int((signal - row["available_date"]).days),
        "net_income_ttm": int(row["net_income_ttm"]),
        "currency": row["currency"],
        "source_accessions": row["source_accessions"],
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
        raise RuntimeError("not every declared ALLT audit observation resolved")

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
        "security": "Allot ordinary shares; consolidated issuer amounts, not ADS/EPS",
        "reporting_profile": "FOREIGN_PRIVATE_ISSUER_20-F_6-K",
        "baseline_binding": BASELINE_BINDING,
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "resolved_audit_observation_count": len(resolutions),
        "source_documents": provenance,
        "source_value_verification": source_value_verification,
        "non_financial_amendment_audit": NON_FINANCIAL_AMENDMENT,
        "revenue_assessment": {
            "direct_growth_emitted": False,
            "reason": (
                "Exact negative consolidated TTM NetIncomeLoss resolves both "
                "eligibility observations. The annual XBRL report also has "
                "product and service revenue dimensions sharing the Total "
                "revenues label, so this exclusion-only supplement does not "
                "parse or emit revenue, quarterly splits, or growth facts."
            ),
        },
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
            "Every operand is an as-reported U.S.-GAAP consolidated amount in "
            "USD thousands. The February 2021 full-year release supersedes the "
            "September-TTM state for the second signal. The layer is not per "
            "share, not non-GAAP, and cannot create quarterly growth."
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
