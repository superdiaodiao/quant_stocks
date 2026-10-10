#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM loss evidence for MTLS.

Materialise was an IFRS foreign private issuer during the audited period and
reported interim results on Form 6-K.  This supplement preserves six- and
nine-month periods as cumulative operands and emits only direct
``net_income_ttm`` loss states.  It does not manufacture quarters, emit an
incomplete growth bundle, or backfill the later FY2019 restatement.
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


OUTPUT_DIR = Path("output/research_only/v14/mtls_exact_ttm_loss")
TICKER = "MTLS"
CIK = 1_091_223
CURRENCY = "EUR"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "IFRS-IASB"
PIT_CUTOFF = "2021-01-29"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

SOURCE_DOCUMENTS = {
    "20f_2020_04_30_fy2019": {
        "form": "20-F",
        "filed": "2020-04-30",
        "accession": "0001193125-20-128829",
        "document": "d873114d20f.htm",
        "local_path": "sources/d873114d20f.htm",
        "expected_sha256": (
            "e046562973b151059499b9e59778ce73de40045bc36aa9ab9e078f2d5135bb40"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1091223/"
            "000119312520128829/d873114d20f.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "AUDITED",
    },
    "6k_2020_07_30_h1": {
        "form": "6-K",
        "filed": "2020-07-30",
        "accession": "0001193125-20-204178",
        "document": "d39097d6k.htm",
        "local_path": "sources/d39097d6k.htm",
        "expected_sha256": (
            "e37b3aed07449c1a132ec265e0b45de728ecf21c38ce9b5730dd2a4c2efdbdd2"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1091223/"
            "000119312520204178/d39097d6k.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "UNAUDITED",
    },
    "6k_2020_10_29_9m": {
        "form": "6-K",
        "filed": "2020-10-29",
        "accession": "0001193125-20-280594",
        "document": "d71400d6k.htm",
        "local_path": "sources/d71400d6k.htm",
        "expected_sha256": (
            "a2ce6c23b737c7be1e5c6a98beccee67e4d6e2055dafd61fec4c79f0736d2468"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1091223/"
            "000119312520280594/d71400d6k.htm"
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

# The March 2021 results retrospectively changed FY2019 net profit from
# EUR1,724k to EUR1,644k after finalizing the Engimplan purchase accounting.
# Both later documents are evidence of a restatement, never PIT operands.
REJECTED_LATER_RESTATEMENTS = {
    "0001193125-21-074859": {
        "form": "6-K",
        "filed": "2021-03-09",
        "document": "d106849d6k.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1091223/"
            "000119312521074859/d106849d6k.htm"
        ),
        "original_fy2019_net_profit_eur_thousands": 1_724,
        "restated_fy2019_net_profit_eur_thousands": 1_644,
        "reason": "filed after all audited signal dates",
    },
    "0001193125-21-145001": {
        "form": "20-F",
        "filed": "2021-04-30",
        "document": "d171871d20f.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1091223/"
            "000119312521145001/d171871d20f.htm"
        ),
        "original_fy2019_net_profit_eur_thousands": 1_724,
        "restated_fy2019_net_profit_eur_thousands": 1_644,
        "reason": "filed after all audited signal dates",
    },
}

# A November 2020 6-K/A is within the PIT window but explicitly corrected only
# Exhibit 3.1 (articles of association) and did not amend financial disclosure.
NON_FINANCIAL_AMENDMENT = {
    "form": "6-K/A",
    "filed": "2020-11-10",
    "accession": "0001193125-20-290140",
    "document": "d860427d6ka.htm",
    "url": (
        "https://www.sec.gov/Archives/edgar/data/1091223/"
        "000119312520290140/d860427d6ka.htm"
    ),
    "effect": "NO_FINANCIAL_UPDATE_EXHIBIT_3_1_ONLY",
}

OPERANDS_EUR_THOUSANDS = {
    "fy2019_net_profit": {
        "source_id": "20f_2020_04_30_fy2019",
        "period": "FY2019",
        "table_column": "FY2019_EUR",
        "metric": "net_income",
        "line_item": "Net profit (loss) for the year",
        "value": 1_724,
    },
    "h1_2019_net_loss": {
        "source_id": "6k_2020_07_30_h1",
        "period": "H1 2019",
        "table_column": "H1_2019_EUR",
        "metric": "net_income",
        "line_item": "Net profit (loss) for the period",
        "value": -602,
    },
    "h1_2020_net_loss": {
        "source_id": "6k_2020_07_30_h1",
        "period": "H1 2020",
        "table_column": "H1_2020_EUR",
        "metric": "net_income",
        "line_item": "Net profit (loss) for the period",
        "value": -4_786,
    },
    "m9_2019_net_profit": {
        "source_id": "6k_2020_10_29_9m",
        "period": "9M 2019",
        "table_column": "9M_2019_EUR",
        "metric": "net_income",
        "line_item": "Net profit (loss) for the period",
        "value": 397,
    },
    "m9_2020_net_loss": {
        "source_id": "6k_2020_10_29_9m",
        "period": "9M 2020",
        "table_column": "9M_2020_EUR",
        "metric": "net_income",
        "line_item": "Net profit (loss) for the period",
        "value": -5_152,
    },
}

REVENUE_DISCLOSURES_EUR_THOUSANDS = {
    "fy2019_revenue": {
        "source_id": "20f_2020_04_30_fy2019",
        "period": "FY2019",
        "table_column": "FY2019_EUR",
        "metric": "revenue",
        "line_item": "Revenue",
        "value": 196_679,
    },
    "h1_2019_revenue": {
        "source_id": "6k_2020_07_30_h1",
        "period": "H1 2019",
        "table_column": "H1_2019_EUR",
        "metric": "revenue",
        "line_item": "Revenue",
        "value": 95_519,
    },
    "h1_2020_revenue": {
        "source_id": "6k_2020_07_30_h1",
        "period": "H1 2020",
        "table_column": "H1_2020_EUR",
        "metric": "revenue",
        "line_item": "Revenue",
        "value": 84_362,
    },
    "m9_2019_revenue": {
        "source_id": "6k_2020_10_29_9m",
        "period": "9M 2019",
        "table_column": "9M_2019_EUR",
        "metric": "revenue",
        "line_item": "Revenue",
        "value": 145_968,
    },
    "m9_2020_revenue": {
        "source_id": "6k_2020_10_29_9m",
        "period": "9M 2020",
        "table_column": "9M_2020_EUR",
        "metric": "revenue",
        "line_item": "Revenue",
        "value": 125_148,
    },
}

SOURCE_PARSE_SPECS = {
    "20f_2020_04_30_fy2019": {
        "context_phrases": (
            "For the year ended December 31",
            "in 000€, except per share data",
            "Notes",
            "2019",
            "2018",
            "2017",
        ),
        "columns": {
            "FY2019_EUR": "FY2019",
            "FY2018_EUR": "FY2018",
            "FY2017_EUR": "FY2017",
        },
        "row_labels": {
            "net_income": "Net profit (loss) for the year",
            "revenue": "Revenue",
        },
    },
    "6k_2020_07_30_h1": {
        "context_phrases": (
            "For the three months ended June 30",
            "For the six months ended June 30",
            "In 000",
            "U.S.$",
            "€",
        ),
        "columns": {
            "Q2_2020_USD": "Q2 2020 USD translation",
            "Q2_2020_EUR": "Q2 2020",
            "Q2_2019_EUR": "Q2 2019",
            "H1_2020_EUR": "H1 2020",
            "H1_2019_EUR": "H1 2019",
        },
        "row_labels": {
            "net_income": "Net profit (loss) for the period",
            "revenue": "Revenue",
        },
    },
    "6k_2020_10_29_9m": {
        "context_phrases": (
            "For the three months ended September 30",
            "For the nine months ended September 30",
            "In 000",
            "U.S.$",
            "€",
        ),
        "columns": {
            "Q3_2020_USD": "Q3 2020 USD translation",
            "Q3_2020_EUR": "Q3 2020",
            "Q3_2019_EUR": "Q3 2019",
            "9M_2020_EUR": "9M 2020",
            "9M_2019_EUR": "9M 2019",
        },
        "row_labels": {
            "net_income": "Net profit (loss) for the period",
            "revenue": "Revenue",
        },
    },
}

TTM_SPECS = {
    "2020-06-30": {
        "available_date": "2020-07-30",
        "formula": "FY2019 - H1_2019 + H1_2020",
        "terms": (
            (1, "fy2019_net_profit"),
            (-1, "h1_2019_net_loss"),
            (1, "h1_2020_net_loss"),
        ),
        "expected_eur_thousands": -2_460,
        "form": "20-F_PLUS_6-K_H1_CUMULATIVE_TTM",
    },
    "2020-09-30": {
        "available_date": "2020-10-29",
        "formula": "FY2019 - 9M_2019 + 9M_2020",
        "terms": (
            (1, "fy2019_net_profit"),
            (-1, "m9_2019_net_profit"),
            (1, "m9_2020_net_loss"),
        ),
        "expected_eur_thousands": -3_825,
        "form": "20-F_PLUS_6-K_9M_CUMULATIVE_TTM",
    },
}

# Read-only delta against step_dkng_imab_qfin_audit: three unique dates and
# twelve scenario observations.
AUDIT_OBSERVATIONS = tuple(
    (
        f"liq2000000-age{maximum_age_days}-growth",
        signal_date,
        maximum_age_days,
    )
    for maximum_age_days in (150, 365, 550)
    for signal_date in ("2020-08-31", "2020-12-31", "2021-01-29")
) + tuple(
    (
        f"liq10000000-age{maximum_age_days}-growth",
        "2021-01-29",
        maximum_age_days,
    )
    for maximum_age_days in (150, 365, 550)
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    with urlopen(
        Request(url, headers=SEC_HEADERS), timeout=120
    ) as response:
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
                f"no unambiguous MTLS {metric} table for {source_id}"
            )
        canonical = json.dumps(candidates[0], sort_keys=True)
        if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
            raise RuntimeError(f"conflicting MTLS {metric} tables for {source_id}")
        parsed[metric] = candidates[0]
    return parsed


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in documents.items():
        accession = source["accession"]
        if accession in REJECTED_LATER_RESTATEMENTS:
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
        if accession_path not in source["url"]:
            raise ValueError(f"source {source_id} URL does not lock accession")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"source {source_id} URL does not lock document")
        relative_path = Path(source["local_path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"source {source_id} has unsafe local_path")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"source {source_id} has invalid expected SHA-256")
    for item_id, item in {
        **OPERANDS_EUR_THOUSANDS,
        **REVENUE_DISCLOSURES_EUR_THOUSANDS,
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
        **OPERANDS_EUR_THOUSANDS,
        **REVENUE_DISCLOSURES_EUR_THOUSANDS,
    }
    for item_id, item in expected_items.items():
        source_id = item["source_id"]
        column = item["table_column"]
        source_spec = SOURCE_PARSE_SPECS[source_id]
        if column not in source_spec["columns"]:
            raise RuntimeError(f"source value {item_id} has no parsed period")
        if source_spec["columns"][column] != item["period"]:
            raise RuntimeError(f"source value {item_id} period mapping changed")
        if _normalize_text(source_spec["row_labels"][item["metric"]]) != (
            _normalize_text(item["line_item"])
        ):
            raise RuntimeError(f"source value {item_id} line item mapping changed")
        parsed_value = parsed[source_id][item["metric"]][column]
        expected_value = int(item["value"])
        if parsed_value != expected_value:
            raise RuntimeError(
                f"source value {item_id} changed: parsed {parsed_value}, "
                f"expected {expected_value}"
            )
        verified.append({
            "item_id": item_id,
            "source_id": source_id,
            "metric": item["metric"],
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
                f"MTLS source SHA-256 mismatch for {source_id}: {actual_sha256}"
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
        OPERANDS_EUR_THOUSANDS[operand_id]["source_id"]
        for _, operand_id in terms
    ))


def exact_ttm_evidence() -> list[dict]:
    validate_source_lock()
    rows = []
    for fiscal_end, spec in TTM_SPECS.items():
        value_thousands = sum(
            coefficient * int(OPERANDS_EUR_THOUSANDS[operand_id]["value"])
            for coefficient, operand_id in spec["terms"]
        )
        if value_thousands != spec["expected_eur_thousands"]:
            raise RuntimeError(f"MTLS exact TTM changed for {fiscal_end}")
        if value_thousands > 0:
            raise RuntimeError("direct exact-TTM layer is exclusion-only")
        source_ids = _source_ids_for_terms(spec["terms"])
        sources = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
        if spec["available_date"] != max(source["filed"] for source in sources):
            raise RuntimeError(f"MTLS availability date changed for {fiscal_end}")
        rows.append({
            "ticker": TICKER,
            "evidence_kind": "exact_cumulative_ttm_loss",
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
        "taxonomy": "ifrs-full",
        "concept": "mtls_exact_ttm:ProfitLoss:EUR",
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
        raise RuntimeError("not every declared MTLS audit observation resolved")

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
        "reporting_profile": "FOREIGN_PRIVATE_ISSUER_20-F_6-K",
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "resolved_audit_observation_count": len(resolutions),
        "source_documents": provenance,
        "source_value_verification": source_value_verification,
        "later_restatement_isolation": REJECTED_LATER_RESTATEMENTS,
        "non_financial_amendment_audit": NON_FINANCIAL_AMENDMENT,
        "revenue_assessment": {
            "verified_disclosures_eur_thousands": {
                item_id: int(item["value"])
                for item_id, item in REVENUE_DISCLOSURES_EUR_THOUSANDS.items()
            },
            "direct_growth_emitted": False,
            "reason": (
                "Selected PIT sources prove current TTM revenue but do not "
                "contain the older H1/9M 2018 cumulative operands needed for "
                "a complete prior-TTM growth package. Exact negative TTM "
                "profit already resolves candidate eligibility."
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
            "Uses only original-as-filed IFRS annual-minus-prior-cumulative-"
            "plus-current-cumulative loss arithmetic. It emits no quarter or "
            "growth metric and rejects the March/April 2021 FY2019 restatement."
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
    print(json.dumps(build(args.output_dir), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
