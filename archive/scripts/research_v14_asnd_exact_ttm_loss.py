#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM loss evidence for ASND.

Ascendis Pharma was an IFRS foreign private issuer whose Nasdaq security was
an ADS.  This supplement uses only consolidated issuer-level EUR amounts that
were public before the 2019-03-29 signal.  It emits one direct negative TTM
profit state for exclusion and cannot manufacture a quarter or growth fact.
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


OUTPUT_DIR = Path("output/research_only/v14/asnd_exact_ttm_loss")
TICKER = "ASND"
CIK = 1_612_042
CURRENCY = "EUR"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "IFRS-IASB"
PIT_CUTOFF = "2019-03-29"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
BASELINE_BINDING = {
    "quarterly": (
        "output/research_only/v14/"
        "candidate_fundamentals_v14_batch_afya_legn_sdgr/quarterly.csv"
    ),
    "quarterly_sha256": (
        "3332d40899309a99b8740415eaf275e35262111104ce634fc8bf2612b9fb172a"
    ),
    "audit": "output/research_only/v14/batch_afya_legn_sdgr_audit.json",
    "audit_sha256": (
        "ea94b239992e914e5efaa77e0d585949a1e3e8aa3d669ad62bd19af5a8474a5a"
    ),
    "baseline_reason": "insufficient_growth_history",
}

SOURCE_DOCUMENTS = {
    "20f_2018_03_28_fy2017": {
        "form": "20-F",
        "filed": "2018-03-28",
        "accession": "0001193125-18-099514",
        "document": "d445343d20f.htm",
        "local_path": "sources/asnd_2017_20f.htm",
        "expected_sha256": (
            "c05e3b53b1a67f92b3a03ccbfd09022b70915dc261b36c2709b5534ae8f65752"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1612042/"
            "000119312518099514/d445343d20f.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "AUDITED",
    },
    "6k_2018_11_28_9m_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2018-11-28",
        "accession": "0001193125-18-336571",
        "document": "d602356dex991.htm",
        "local_path": "sources/asnd_2018_9m_exhibit_99-1.htm",
        "expected_sha256": (
            "518b47a9d81ffeb2438d501c9acce115f92a41471ac552da618e4afcfb7c9abd"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1612042/"
            "000119312518336571/d602356dex991.htm"
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

POST_SIGNAL_CORROBORATION = {
    "form": "20-F",
    "filed": "2019-04-03",
    "accession": "0001193125-19-096759",
    "document": "d654369d20f.htm",
    "url": (
        "https://www.sec.gov/Archives/edgar/data/1612042/"
        "000119312519096759/d654369d20f.htm"
    ),
    "sha256": (
        "bbbce530a139d6631d29ead5d96c883422da72bc3363ba59e2acedcd9dc10059"
    ),
    "effect": "CORROBORATES_NO_RESTATEMENT_BUT_EXCLUDED_AFTER_SIGNAL",
    "fy2017_profit_attributable_eur_thousands": -123_897,
    "fy2018_profit_attributable_eur_thousands": -130_097,
    "ifrs_9_effect": "NO_IMPACT_ON_CONSOLIDATED_FINANCIAL_STATEMENTS",
    "ifrs_15_effect": "NO_IMPACT_ON_CONSOLIDATED_FINANCIAL_STATEMENTS",
}

OPERANDS_EUR_THOUSANDS = {
    "fy2017_profit_attributable": {
        "source_id": "20f_2018_03_28_fy2017",
        "period": "FY2017",
        "table_column": "FY2017",
        "line_item": "Profit/(loss) for the year attributable to owners of the Company",
        "value": -123_897,
    },
    "m9_2017_profit_attributable": {
        "source_id": "6k_2018_11_28_9m_ex991",
        "period": "9M 2017",
        "table_column": "M9_2017",
        "line_item": (
            "Profit / (loss) for the period attributable to owners of the Company"
        ),
        "value": -89_722,
    },
    "m9_2018_profit_attributable": {
        "source_id": "6k_2018_11_28_9m_ex991",
        "period": "9M 2018",
        "table_column": "M9_2018",
        "line_item": (
            "Profit / (loss) for the period attributable to owners of the Company"
        ),
        "value": -98_119,
    },
}

SOURCE_PARSE_SPECS = {
    "20f_2018_03_28_fy2017": {
        "context_phrases": (
            "Notes",
            "2017",
            "2016",
            "2015",
            "(EUR’000)",
            "Revenue",
            "Other comprehensive income/(loss)",
        ),
        "identity_phrases": (
            "Ascendis Pharma A/S",
            "The NASDAQ Global Select Market under the symbol ASND",
            "The Company’s Board of Directors approved these consolidated financial statements on March 28, 2018",
            "International Financial Reporting Standards",
        ),
        "columns": {
            "FY2017": "FY2017",
            "FY2016": "FY2016",
            "FY2015": "FY2015",
        },
        "row_labels": {
            "net_income": "Net profit/(loss) for the year",
            "profit_attributable": (
                "Profit/(loss) for the year attributable to owners of the Company"
            ),
        },
    },
    "6k_2018_11_28_9m_ex991": {
        "context_phrases": (
            "Three Months Ended September 30",
            "Nine Months Ended September 30",
            "2018",
            "2017",
            "(EUR’000)",
            "Revenue",
        ),
        "identity_phrases": (
            "Ascendis Pharma A/S",
            "Unaudited Condensed Consolidated Interim Financial Statements",
            "for the Three and Nine Months Ended September 30, 2018 and 2017",
            "International Financial Reporting Standards",
        ),
        "columns": {
            "Q3_2018": "Q3 2018",
            "Q3_2017": "Q3 2017",
            "M9_2018": "9M 2018",
            "M9_2017": "9M 2017",
        },
        "row_labels": {
            "net_income": "Net profit / (loss) for the period",
            "profit_attributable": (
                "Profit / (loss) for the period attributable to owners of the Company"
            ),
        },
    },
}

TTM_SPEC = {
    "fiscal_end": "2018-09-30",
    "available_date": "2018-11-28",
    "formula": "FY2017 - 9M_2017 + 9M_2018",
    "terms": (
        (1, "fy2017_profit_attributable"),
        (-1, "m9_2017_profit_attributable"),
        (1, "m9_2018_profit_attributable"),
    ),
    "expected_eur_thousands": -132_294,
    "form": "20-F_PLUS_6-K_9M_CUMULATIVE_TTM",
}

AUDIT_OBSERVATIONS = (
    ("liq10000000-age150-growth", "2019-03-29", 150),
    ("liq10000000-age365-growth", "2019-03-29", 365),
    ("liq10000000-age550-growth", "2019-03-29", 550),
    ("liq2000000-age150-growth", "2019-03-29", 150),
    ("liq2000000-age365-growth", "2019-03-29", 365),
    ("liq2000000-age550-growth", "2019-03-29", 550),
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
        raise RuntimeError(f"ASND source identity changed for {source_id}")
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
                f"no unambiguous ASND {metric} table for {source_id}"
            )
        canonical = json.dumps(candidates[0], sort_keys=True)
        if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
            raise RuntimeError(f"conflicting ASND {metric} tables for {source_id}")
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
    for item_id, item in OPERANDS_EUR_THOUSANDS.items():
        if item["source_id"] not in documents:
            raise ValueError(f"source value {item_id} has no locked source")
    if POST_SIGNAL_CORROBORATION["filed"] <= PIT_CUTOFF:
        raise ValueError("post-signal corroboration is not actually post-signal")
    if POST_SIGNAL_CORROBORATION["accession"] in ALLOWED_SOURCE_ACCESSIONS:
        raise ValueError("post-signal filing cannot be an allowed TTM operand")


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw source set does not match the source lock")
    parsed = {
        source_id: _parse_source_tables(source_id, raw)
        for source_id, raw in raw_by_source.items()
    }
    verified = []
    for item_id, item in OPERANDS_EUR_THOUSANDS.items():
        source_id = item["source_id"]
        column = item["table_column"]
        source_spec = SOURCE_PARSE_SPECS[source_id]
        if source_spec["columns"].get(column) != item["period"]:
            raise RuntimeError(f"source value {item_id} period mapping changed")
        if _normalize_text(source_spec["row_labels"]["profit_attributable"]) != (
            _normalize_text(item["line_item"])
        ):
            raise RuntimeError(f"source value {item_id} line item mapping changed")
        parsed_value = parsed[source_id]["profit_attributable"][column]
        consolidated_value = parsed[source_id]["net_income"][column]
        expected_value = int(item["value"])
        if parsed_value != expected_value:
            raise RuntimeError(
                f"source value {item_id} changed: parsed {parsed_value}, "
                f"expected {expected_value}"
            )
        if consolidated_value != parsed_value:
            raise RuntimeError(
                f"ASND noncontrolling-interest attribution mismatch for {item_id}"
            )
        verified.append({
            "item_id": item_id,
            "source_id": source_id,
            "metric": "profit_attributable_to_owners",
            "period": item["period"],
            "table_column": column,
            "line_item": item["line_item"],
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "expected_value": expected_value,
            "parsed_value": parsed_value,
            "consolidated_net_profit_loss_value": consolidated_value,
            "attribution_parity": True,
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
                f"ASND source SHA-256 mismatch for {source_id}: {actual_sha256}"
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


def exact_ttm_evidence() -> list[dict]:
    validate_source_lock()
    value_thousands = sum(
        coefficient * int(OPERANDS_EUR_THOUSANDS[operand_id]["value"])
        for coefficient, operand_id in TTM_SPEC["terms"]
    )
    if value_thousands != TTM_SPEC["expected_eur_thousands"]:
        raise RuntimeError("ASND exact TTM changed")
    if value_thousands >= 0:
        raise RuntimeError("ASND direct exact-TTM layer is exclusion-only")
    source_ids = list(dict.fromkeys(
        OPERANDS_EUR_THOUSANDS[operand_id]["source_id"]
        for _, operand_id in TTM_SPEC["terms"]
    ))
    sources = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
    if TTM_SPEC["available_date"] != max(source["filed"] for source in sources):
        raise RuntimeError("ASND availability date changed")
    return [{
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
        "profit_scope": (
            "consolidated IFRS profit/loss attributable to owners of the Company; "
            "issuer EUR amount, not ADS or per-share"
        ),
    }]


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
        "concept": (
            "asnd_exact_ttm:ProfitLossAttributableToOwnersOfParent:EUR"
        ),
        "form": evidence["form"],
        "accession": "+".join(evidence["source_accessions"]),
        "fetched_at": fetched_at,
    } for evidence in exact_ttm_evidence()]
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


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
        raise RuntimeError("not every declared ASND audit observation resolved")

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
        "security": (
            "Nasdaq-listed ADSs representing Ascendis Pharma ordinary shares; "
            "consolidated issuer EUR amounts, not ADS/EPS"
        ),
        "reporting_profile": "FOREIGN_PRIVATE_ISSUER_20-F_6-K",
        "baseline_binding": BASELINE_BINDING,
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "resolved_audit_observation_count": len(resolutions),
        "source_documents": provenance,
        "source_value_verification": source_value_verification,
        "post_signal_corroboration_excluded": POST_SIGNAL_CORROBORATION,
        "revenue_assessment": {
            "direct_growth_emitted": False,
            "reason": (
                "Exact negative consolidated attributable TTM profit resolves "
                "eligibility before any revenue-growth decision. This "
                "exclusion-only supplement does not parse or emit revenue, "
                "quarterly splits, EPS, or growth facts."
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
            "Every operand is an as-reported consolidated IFRS amount "
            "attributable to owners in EUR thousands. The latest operand was "
            "filed 2018-11-28, 121 days before the signal. The 2019-04-03 "
            "20-F is corroboration only and is never an operand. The layer is "
            "not per ADS, not per share, and cannot create quarterly growth."
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
