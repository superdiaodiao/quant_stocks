#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM loss evidence for LEGN.

Legend Biotech was an IFRS foreign private issuer during the audited period.
This supplement combines its contemporaneous FY2020 Form 20-F and H1 2021
Form 6-K cumulative periods, emits only a direct ``net_income_ttm`` loss, and
never manufactures quarters or backfills the issuer's later restatement.
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


OUTPUT_DIR = Path("output/research_only/v14/legn_exact_ttm_loss")
TICKER = "LEGN"
CIK = 1_801_198
CURRENCY = "USD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "IFRS-IASB"
PIT_CUTOFF = "2021-11-30"
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
    "20f_2021_04_02_fy2020_r2": {
        "form": "20-F/R2",
        "filed": "2021-04-02",
        "accession": "0001564590-21-017439",
        "document": "R2.htm",
        "local_path": "sources/legn_2020_20f_R2.htm",
        "expected_sha256": (
            "d7924dbe20cd4f1dd6bca76344cfb8d27370ef9ca4ec1ff5e242e47d3ec72dfb"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1801198/"
            "000156459021017439/R2.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "AUDITED",
    },
    "6k_2021_08_23_h1_r2": {
        "form": "6-K/R2",
        "filed": "2021-08-23",
        "accession": "0001564590-21-045342",
        "document": "R2.htm",
        "local_path": "sources/legn_2021_h1_6k_R2.htm",
        "expected_sha256": (
            "05d0bb172d32fbd9b0aa159078a00a410560f205d0e2c9b6d6769be0f98b6f59"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1801198/"
            "000156459021045342/R2.htm"
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

# Filed after every target signal. The issuer disclosed that its prior
# collaboration-license revenue accounting was materially incorrect. These
# values are important audit evidence but are forbidden as PIT operands.
REJECTED_LATER_RESTATEMENT = {
    "form": "20-F/A",
    "filed": "2023-02-17",
    "accession": "0001564590-23-002041",
    "document": "legn-20fa_20211231.htm",
    "url": (
        "https://www.sec.gov/Archives/edgar/data/1801198/"
        "000156459023002041/legn-20fa_20211231.htm"
    ),
    "announced": "2022-10-19",
    "affected_topic": (
        "Janssen cilta-cel commercial-license valuation and collaboration "
        "revenue recognition"
    ),
    "original_fy2020_profit_loss_usd_thousands": -303_477,
    "restated_fy2020_profit_loss_usd_thousands": -266_373,
    "rule": "later restatement is not knowable at the 2021 signal dates",
}

OPERANDS_USD_THOUSANDS = {
    "fy2020_profit_loss": {
        "source_id": "20f_2021_04_02_fy2020_r2",
        "period": "FY2020",
        "table_column": "FY2020",
        "line_item": "LOSS FOR THE YEAR",
        "value": -303_477,
    },
    "h1_2020_profit_loss": {
        "source_id": "6k_2021_08_23_h1_r2",
        "period": "H1 2020",
        "table_column": "H1_2020",
        "line_item": "LOSS FOR THE PERIOD",
        "value": -179_104,
    },
    "h1_2021_profit_loss": {
        "source_id": "6k_2021_08_23_h1_r2",
        "period": "H1 2021",
        "table_column": "H1_2021",
        "line_item": "LOSS FOR THE PERIOD",
        "value": -172_483,
    },
}

# Revenue is verified because the later restatement concerned collaboration
# revenue. It is retained as audit context only and never emitted as a fact.
REVENUE_DISCLOSURES_USD_THOUSANDS = {
    "fy2020_revenue": {
        "source_id": "20f_2021_04_02_fy2020_r2",
        "period": "FY2020",
        "table_column": "FY2020",
        "line_item": "REVENUE",
        "value": 75_676,
    },
    "h1_2020_revenue": {
        "source_id": "6k_2021_08_23_h1_r2",
        "period": "H1 2020",
        "table_column": "H1_2020",
        "line_item": "REVENUE",
        "value": 23_146,
    },
    "h1_2021_revenue": {
        "source_id": "6k_2021_08_23_h1_r2",
        "period": "H1 2021",
        "table_column": "H1_2021",
        "line_item": "REVENUE",
        "value": 33_915,
    },
}

SOURCE_PARSE_SPECS = {
    "20f_2021_04_02_fy2020_r2": {
        "context_phrases": (
            "CONSOLIDATED STATEMENTS OF PROFIT OR LOSS AND OTHER COMPREHENSIVE INCOME",
            "USD ($)",
            "$ in Thousands",
            "12 Months Ended",
            "Dec. 31, 2020",
            "Dec. 31, 2019",
            "Dec. 31, 2018",
        ),
        "columns": {
            "FY2020": "FY2020",
            "FY2019": "FY2019",
            "FY2018": "FY2018",
        },
        "row_labels": {
            "net_income": "LOSS FOR THE YEAR",
            "revenue": "REVENUE",
        },
    },
    "6k_2021_08_23_h1_r2": {
        "context_phrases": (
            "UNAUDITED INTERIM CONDENSED CONSOLIDATED STATEMENTS OF PROFIT OR LOSS",
            "USD ($)",
            "$ in Thousands",
            "6 Months Ended",
            "Jun. 30, 2021",
            "Jun. 30, 2020",
        ),
        "columns": {
            "H1_2021": "H1 2021",
            "H1_2020": "H1 2020",
        },
        "row_labels": {
            "net_income": "LOSS FOR THE PERIOD",
            "revenue": "REVENUE",
        },
    },
}

TTM_SPEC = {
    "fiscal_end": "2021-06-30",
    "available_date": "2021-08-23",
    "formula": "FY2020 - H1_2020 + H1_2021",
    "terms": (
        (1, "fy2020_profit_loss"),
        (-1, "h1_2020_profit_loss"),
        (1, "h1_2021_profit_loss"),
    ),
    "expected_usd_thousands": -296_856,
    "form": "20-F_PLUS_6-K_H1_CUMULATIVE_TTM",
}

AUDIT_OBSERVATIONS = tuple(
    (
        f"liq{liquidity}-age150-growth",
        signal_date,
        150,
    )
    for liquidity in (10_000_000, 2_000_000)
    for signal_date in ("2021-09-30", "2021-10-29", "2021-11-30")
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
                f"no unambiguous LEGN {metric} table for {source_id}"
            )
        canonical = json.dumps(candidates[0], sort_keys=True)
        if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
            raise RuntimeError(f"conflicting LEGN {metric} tables for {source_id}")
        parsed[metric] = candidates[0]
    return parsed


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in documents.items():
        accession = source["accession"]
        if accession == REJECTED_LATER_RESTATEMENT["accession"]:
            raise ValueError(f"later restatement is forbidden: {accession}")
        if accession not in ALLOWED_SOURCE_ACCESSIONS:
            raise ValueError(f"unapproved source accession: {accession}")
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"source {source_id} was filed after PIT cutoff")
        if source["currency"] != CURRENCY or source["scale"] != SOURCE_SCALE:
            raise ValueError(f"source {source_id} has mixed currency or scale")
        if source["accounting_standard"] != ACCOUNTING_STANDARD:
            raise ValueError(f"source {source_id} is not IFRS-IASB")
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
    for item_id, item in {
        **OPERANDS_USD_THOUSANDS,
        **REVENUE_DISCLOSURES_USD_THOUSANDS,
    }.items():
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
    expected_items = {
        **OPERANDS_USD_THOUSANDS,
        **REVENUE_DISCLOSURES_USD_THOUSANDS,
    }
    for item_id, item in expected_items.items():
        source_id = item["source_id"]
        column = item["table_column"]
        source_spec = SOURCE_PARSE_SPECS[source_id]
        if column not in source_spec["columns"]:
            raise RuntimeError(f"source value {item_id} has no parsed period")
        if source_spec["columns"][column] != item["period"]:
            raise RuntimeError(f"source value {item_id} period mapping changed")
        metric = "net_income" if item_id in OPERANDS_USD_THOUSANDS else "revenue"
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
                f"LEGN source SHA-256 mismatch for {source_id}: {actual_sha256}"
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
        raise RuntimeError("LEGN exact TTM calculation changed")
    if value_thousands >= 0:
        raise RuntimeError("LEGN direct exact-TTM layer is exclusion-only")
    source_ids = _source_ids_for_terms(TTM_SPEC["terms"])
    sources = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
    if TTM_SPEC["available_date"] != max(source["filed"] for source in sources):
        raise RuntimeError("LEGN exact TTM availability date changed")
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
        "profit_scope": "consolidated ProfitLoss; not per-ADS or attributable EPS",
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
        "taxonomy": "ifrs-full",
        "concept": "legn_exact_ttm:ProfitLoss:USD",
        "form": evidence["form"],
        "accession": "+".join(evidence["source_accessions"]),
        "fetched_at": fetched_at,
    }], columns=OUTPUT_COLUMNS)


def resolve_observation(signal_date: str, maximum_age_days: int) -> dict:
    signal = pd.Timestamp(signal_date)
    evidence = exact_ttm_evidence()
    available_date = pd.Timestamp(evidence["available_date"])
    age = int((signal - available_date).days)
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
        "currency": evidence["currency"],
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
        raise RuntimeError("not every declared LEGN audit observation resolved")

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
    revenue_ttm = (
        REVENUE_DISCLOSURES_USD_THOUSANDS["fy2020_revenue"]["value"]
        - REVENUE_DISCLOSURES_USD_THOUSANDS["h1_2020_revenue"]["value"]
        + REVENUE_DISCLOSURES_USD_THOUSANDS["h1_2021_revenue"]["value"]
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
        "security": "Legend Biotech ADS; consolidated issuer amounts, not per-ADS",
        "reporting_profile": "FOREIGN_PRIVATE_ISSUER_20-F_6-K",
        "baseline_binding": BASELINE_BINDING,
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "resolved_audit_observation_count": len(resolutions),
        "source_documents": provenance,
        "source_value_verification": source_value_verification,
        "later_restatement_isolation": REJECTED_LATER_RESTATEMENT,
        "revenue_assessment": {
            "as_reported_ttm_revenue_usd_thousands": revenue_ttm,
            "formula": "FY2020 - H1_2020 + H1_2021",
            "direct_growth_emitted": False,
            "reason": (
                "Collaboration revenue was later restated. Exact negative "
                "consolidated TTM ProfitLoss already resolves eligibility, so "
                "this supplement does not emit revenue or growth facts."
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
            "The direct loss uses the contemporaneous IFRS consolidated "
            "ProfitLoss line in USD thousands. It is not per ADS, does not use "
            "the attributable-loss/EPS row, and does not backfill the 2023 "
            "collaboration-revenue restatement. It is exclusion-only and "
            "cannot create a quarterly growth observation."
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
