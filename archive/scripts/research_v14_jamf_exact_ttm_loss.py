#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM loss evidence for JAMF.

JAMF's 2021Q2 10-Q reports a revised FY2020 net loss and revised comparative
six-month net loss beside the current six-month net loss.  Those three
cumulative/annual operands produce one exact negative TTM state without
inventing quarters or growth.  The older 2020 10-K value is retained only to
prove that the correction is isolated and is never mixed into the formula.
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


OUTPUT_DIR = Path("output/research_only/v14/jamf_exact_ttm_loss")
COMPANYFACTS_CACHE = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/"
    "CIK0001721947.json.gz"
)
TICKER = "JAMF"
CIK = 1_721_947
CURRENCY = "USD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP"
PIT_CUTOFF = "2021-10-29"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}


SOURCE_DOCUMENTS = {
    "10k_2021_03_04_fy2020_original": {
        "role": "superseded_audit_source",
        "form": "10-K",
        "filed": "2021-03-04",
        "accession": "0001558370-21-002391",
        "document": "jamf-20201231x10k.htm",
        "local_path": "sources/jamf-20201231x10k.htm",
        "expected_sha256": (
            "5091a4384cbe9aa8d46cea598d35466cedddc4237646ceb6f516cd67efe50371"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1721947/"
            "000155837021002391/jamf-20201231x10k.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "10q_2021_08_20_q2_revised": {
        "role": "operand_source",
        "form": "10-Q",
        "filed": "2021-08-20",
        "accession": "0001628280-21-017415",
        "document": "jamf-20210630.htm",
        "local_path": "sources/jamf-20210630.htm",
        "expected_sha256": (
            "5183cd37a5f4032279ae5812c416081f20aa4c600ff5506a641e2319f1a39ad7"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1721947/"
            "000162828021017415/jamf-20210630.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "8k_2021_08_27_correction_confirmation": {
        "role": "correction_confirmation",
        "form": "8-K",
        "filed": "2021-08-27",
        "accession": "0001628280-21-017726",
        "document": "jamf-20210827.htm",
        "local_path": "sources/jamf-20210827.htm",
        "expected_sha256": (
            "e6e1b5ac4c8c0fb4e0f2505f14d9557981d3b0b232eb563eec9080fc48bdf924"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1721947/"
            "000162828021017726/jamf-20210827.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
}
ALLOWED_SOURCE_ACCESSIONS = {
    source["accession"] for source in SOURCE_DOCUMENTS.values()
}
REJECTED_LATER_FILINGS = {
    "0001628280-21-023008": {
        "form": "10-Q",
        "filed": "2021-11-12",
        "reason": "filed after the 2021-10-29 signal date",
    },
    "0001628280-22-004530": {
        "form": "10-K",
        "filed": "2022-03-01",
        "reason": "later annual report cannot backfill either signal date",
    },
}
REVIEWED_PREIPO_FILINGS = {
    "0001047469-20-004160": {
        "form": "S-1/A",
        "filed": "2020-07-20",
        "document": "a2242092zs-1a.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1721947/"
            "000104746920004160/a2242092zs-1a.htm"
        ),
        "conclusion": (
            "contains FY2018/FY2019 and Q1-2019/Q1-2020 comparatives, but is "
            "superseded by the correction and is unnecessary for the exact "
            "negative TTM derived wholly from the 2021Q2 10-Q"
        ),
    }
}


SOURCE_PARSE_SPECS = {
    "original_fy2020_statement": {
        "source_id": "10k_2021_03_04_fy2020_original",
        "context_phrases": (
            "2020",
            "2019",
            "2018",
            "Total revenue",
            "Net loss",
        ),
        "columns": {
            "FY2020_original": "FY2020 original",
            "FY2019_original": "FY2019 original",
            "FY2018_original": "FY2018 original",
        },
        "minimum_absolute_value": 100,
    },
    "q2_statement_revised": {
        "source_id": "10q_2021_08_20_q2_revised",
        "context_phrases": (
            "Three Months Ended June 30",
            "Six Months Ended June 30",
            "As Revised",
            "Total revenue",
            "Net loss",
        ),
        "columns": {
            "Q2_2021": "Q2 2021",
            "Q2_2020_revised": "Q2 2020 revised",
            "H1_2021": "H1 2021",
            "H1_2020_revised": "H1 2020 revised",
        },
        "minimum_absolute_value": 100,
    },
    "fy2020_revision_bridge": {
        "source_id": "10q_2021_08_20_q2_revised",
        "context_phrases": (
            "Year Ended December 31, 2020",
            "As Previously Reported",
            "Adjustments",
            "Commissions",
            "Other",
            "As Revised",
        ),
        "columns": {
            "FY2020_original": "FY2020 original",
            "commission_adjustment": "commission adjustment",
            "other_adjustment": "other adjustment",
            "FY2020_revised": "FY2020 revised",
        },
        "minimum_absolute_value": 100,
    },
    "h1_2020_revision_bridge": {
        "source_id": "10q_2021_08_20_q2_revised",
        "context_phrases": (
            "Six Months Ended June 30, 2020",
            "As Previously Reported",
            "Adjustments",
            "Commissions",
            "Other",
            "As Revised",
        ),
        "columns": {
            "H1_2020_original": "H1 2020 original",
            "commission_adjustment": "commission adjustment",
            "other_adjustment": "other adjustment",
            "H1_2020_revised": "H1 2020 revised",
        },
        "minimum_absolute_value": 100,
    },
}


EXPECTED_PARSED_TABLES = {
    "original_fy2020_statement": {
        "FY2020_original": -22_771,
        "FY2019_original": -32_600,
        "FY2018_original": -36_256,
    },
    "q2_statement_revised": {
        "Q2_2021": -16_467,
        "Q2_2020_revised": -834,
        "H1_2021": -21_056,
        "H1_2020_revised": -10_330,
    },
    "fy2020_revision_bridge": {
        "FY2020_original": -22_771,
        "commission_adjustment": -1_878,
        "other_adjustment": 567,
        "FY2020_revised": -24_082,
    },
    "h1_2020_revision_bridge": {
        "H1_2020_original": -8_713,
        "commission_adjustment": -923,
        "other_adjustment": -694,
        "H1_2020_revised": -10_330,
    },
}


SOURCE_TEXT_CHECKS = {
    "10q_2021_08_20_q2_revised": (
        "Revision of previously issued consolidated financial statements",
        "commissions that were incorrectly capitalized in prior periods",
        "should have been expensed as incurred in accordance with GAAP",
    ),
    "8k_2021_08_27_correction_confirmation": (
        "to reflect the correction of immaterial errors",
        "as previously disclosed in the Quarterly Report on Form 10-Q",
        "filed with the SEC on August 20, 2021",
        "revised previously issued consolidated financial statements",
    ),
}


OPERANDS_USD_THOUSANDS = {
    "fy2020_revised_net_loss": {
        "source_id": "10q_2021_08_20_q2_revised",
        "parse_id": "fy2020_revision_bridge",
        "period": "FY2020 revised",
        "table_column": "FY2020_revised",
        "line_item": "Net loss",
        "value": -24_082,
    },
    "h1_2020_revised_net_loss": {
        "source_id": "10q_2021_08_20_q2_revised",
        "parse_id": "q2_statement_revised",
        "period": "H1 2020 revised",
        "table_column": "H1_2020_revised",
        "line_item": "Net loss",
        "value": -10_330,
    },
    "h1_2021_net_loss": {
        "source_id": "10q_2021_08_20_q2_revised",
        "parse_id": "q2_statement_revised",
        "period": "H1 2021",
        "table_column": "H1_2021",
        "line_item": "Net loss",
        "value": -21_056,
    },
}


TTM_SPEC = {
    "fiscal_end": "2021-06-30",
    "available_date": "2021-08-20",
    "formula": "FY2020_revised - H1_2020_revised + H1_2021",
    "terms": (
        (1, "fy2020_revised_net_loss"),
        (-1, "h1_2020_revised_net_loss"),
        (1, "h1_2021_net_loss"),
    ),
    "expected_usd_thousands": -34_808,
    "form": "10-Q_REVISED_ANNUAL_PLUS_H1_CUMULATIVE_TTM",
}


AUDIT_OBSERVATIONS = tuple(
    (f"liq{liquidity}-age150-growth", signal_date, 150)
    for liquidity in (2_000_000, 10_000_000)
    for signal_date in ("2021-08-31", "2021-10-29")
)


COMPANYFACTS_OPERANDS = (
    {
        "period": "FY2020 revised",
        "start": "2020-01-01",
        "end": "2020-12-31",
        "value": -24_082_000,
    },
    {
        "period": "H1 2020 revised",
        "start": "2020-01-01",
        "end": "2020-06-30",
        "value": -10_330_000,
    },
    {
        "period": "H1 2021",
        "start": "2021-01-01",
        "end": "2021-06-30",
        "value": -21_056_000,
    },
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


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
        r"\(\s*\d[\d,]*\s*\)|(?<![\w.])\d[\d,]*(?![\w.])", text
    )
    values = []
    for token in tokens:
        digits = re.sub(r"\D", "", token)
        if digits:
            value = int(digits)
            values.append(-value if "(" in token else value)
    return values


def _parse_source_table(parse_id: str, raw: bytes) -> dict[str, int]:
    spec = SOURCE_PARSE_SPECS[parse_id]
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    context = tuple(_normalize_text(item) for item in spec["context_phrases"])
    expected_count = len(spec["columns"])
    minimum = int(spec["minimum_absolute_value"])
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
            if len(values) != expected_count:
                continue
            if any(abs(value) < minimum for value in values):
                continue
            candidates.append(dict(zip(spec["columns"], values, strict=True)))
    if not candidates:
        raise RuntimeError(f"no JAMF Net loss table for {parse_id}")
    canonical = json.dumps(candidates[0], sort_keys=True)
    if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
        raise RuntimeError(f"conflicting JAMF Net loss tables for {parse_id}")
    return candidates[0]


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("JAMF source set changed")
    for source_id, source in documents.items():
        accession = source["accession"]
        if accession in REJECTED_LATER_FILINGS:
            raise ValueError(f"later filing is forbidden: {accession}")
        if accession not in ALLOWED_SOURCE_ACCESSIONS:
            raise ValueError(f"unapproved JAMF accession: {accession}")
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"source {source_id} was filed after PIT cutoff")
        if source["currency"] != CURRENCY or source["scale"] != SOURCE_SCALE:
            raise ValueError(f"source {source_id} has incompatible units")
        if source["accounting_standard"] != ACCOUNTING_STANDARD:
            raise ValueError(f"source {source_id} is not US-GAAP")
        if accession.replace("-", "") not in source["url"]:
            raise ValueError(f"source {source_id} URL does not lock accession")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"source {source_id} URL does not lock document")
        path = Path(source["local_path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"source {source_id} has unsafe local_path")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"source {source_id} has invalid SHA-256")
    for operand_id, operand in OPERANDS_USD_THOUSANDS.items():
        if operand["source_id"] not in documents:
            raise ValueError(f"operand {operand_id} has no locked source")


def verify_source_values(raw_by_source: dict[str, bytes]) -> dict:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw JAMF source set does not match source lock")
    parsed = {
        parse_id: _parse_source_table(
            parse_id, raw_by_source[spec["source_id"]]
        )
        for parse_id, spec in SOURCE_PARSE_SPECS.items()
    }
    for parse_id, expected in EXPECTED_PARSED_TABLES.items():
        if parsed[parse_id] != expected:
            raise RuntimeError(
                f"JAMF parsed table changed for {parse_id}: {parsed[parse_id]}"
            )
    operands = []
    for operand_id, operand in OPERANDS_USD_THOUSANDS.items():
        parse_id = operand["parse_id"]
        column = operand["table_column"]
        if SOURCE_PARSE_SPECS[parse_id]["columns"][column] != operand["period"]:
            raise RuntimeError(f"JAMF operand period changed: {operand_id}")
        parsed_value = parsed[parse_id][column]
        if parsed_value != int(operand["value"]):
            raise RuntimeError(
                f"JAMF operand changed for {operand_id}: {parsed_value}"
            )
        operands.append({
            "operand_id": operand_id,
            "source_id": operand["source_id"],
            "parse_id": parse_id,
            "period": operand["period"],
            "line_item": operand["line_item"],
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "parsed_value": parsed_value,
        })
    fragments = []
    for source_id, expected_fragments in SOURCE_TEXT_CHECKS.items():
        soup = BeautifulSoup(raw_by_source[source_id], "lxml")
        document_text = _normalize_text(soup.get_text(" ", strip=True))
        for fragment in expected_fragments:
            if _normalize_text(fragment) not in document_text:
                raise RuntimeError(
                    f"JAMF disclosure changed for {source_id}: {fragment}"
                )
            fragments.append({"source_id": source_id, "fragment": fragment})
    return {"parsed_tables": parsed, "operands": operands, "fragments": fragments}


def prepare_verified_sources(output_dir: Path) -> tuple[dict, dict]:
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
        actual_sha = _sha256_bytes(raw)
        if actual_sha != source["expected_sha256"]:
            raise RuntimeError(
                f"JAMF source SHA-256 mismatch for {source_id}: {actual_sha}"
            )
        raw_by_source[source_id] = raw
        provenance[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha,
            "bytes": len(raw),
            "downloaded": downloaded,
        }
    return provenance, verify_source_values(raw_by_source)


def companyfacts_operand_audit(cache_path: Path = COMPANYFACTS_CACHE) -> dict:
    cache_path = Path(cache_path)
    raw = cache_path.read_bytes()
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        document = json.load(handle)
    payload = document.get("payload", document)
    if int(payload["cik"]) != CIK:
        raise ValueError("Company Facts cache is not JAMF")
    facts = (
        payload.get("facts", {})
        .get("us-gaap", {})
        .get("NetIncomeLoss", {})
        .get("units", {})
        .get("USD", [])
    )
    matches = []
    for expected in COMPANYFACTS_OPERANDS:
        candidates = [
            fact for fact in facts
            if fact.get("start") == expected["start"]
            and fact.get("end") == expected["end"]
            and int(fact.get("val")) == expected["value"]
            and fact.get("accn") == "0001628280-21-017415"
            and fact.get("filed") == "2021-08-20"
            and fact.get("form") == "10-Q"
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                f"JAMF Company Facts operand changed: {expected['period']}"
            )
        matches.append({**expected, "accession": candidates[0]["accn"]})
    return {
        "cache_path": str(cache_path),
        "actual_sha256": _sha256_bytes(raw),
        "cache_fetched_at": document.get("fetched_at"),
        "source_url": document.get("source_url"),
        "matched_operands": matches,
    }


def exact_ttm_evidence() -> dict:
    validate_source_lock()
    value_thousands = sum(
        coefficient * int(OPERANDS_USD_THOUSANDS[operand_id]["value"])
        for coefficient, operand_id in TTM_SPEC["terms"]
    )
    if value_thousands != TTM_SPEC["expected_usd_thousands"]:
        raise RuntimeError("JAMF exact TTM calculation changed")
    if value_thousands >= 0:
        raise RuntimeError("JAMF evidence is exclusion-only and must be negative")
    source = SOURCE_DOCUMENTS["10q_2021_08_20_q2_revised"]
    if source["filed"] != TTM_SPEC["available_date"]:
        raise RuntimeError("JAMF exact TTM availability date changed")
    return {
        "ticker": TICKER,
        "evidence_kind": "exact_revised_annual_plus_cumulative_ttm_loss",
        "fiscal_end": TTM_SPEC["fiscal_end"],
        "available_date": TTM_SPEC["available_date"],
        "currency": CURRENCY,
        "source_scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "net_income_ttm": value_thousands * SOURCE_SCALE,
        "formula": TTM_SPEC["formula"],
        "operand_ids": [operand_id for _, operand_id in TTM_SPEC["terms"]],
        "source_accession": source["accession"],
        "source_url": source["url"],
        "form": TTM_SPEC["form"],
        "restatement_isolation": (
            "all formula operands use the revised basis first disclosed in the "
            "same 2021Q2 10-Q; original 10-K/H1 values are audit-only"
        ),
    }


def direct_ttm_facts(fetched_at: str | None = None) -> pd.DataFrame:
    evidence = exact_ttm_evidence()
    if fetched_at is None:
        fetched_at = str(
            pd.Timestamp.now("UTC").tz_localize(None).normalize().date()
        )
    return pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": evidence["fiscal_end"],
        "available_date": evidence["available_date"],
        "metric": "net_income_ttm",
        "value": evidence["net_income_ttm"],
        "taxonomy": "us-gaap",
        "concept": "jamf_exact_ttm:NetIncomeLoss:USD:revised_basis",
        "form": evidence["form"],
        "accession": evidence["source_accession"],
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
            "reason": "exact negative TTM was not within the PIT age limit",
        }
    return {
        "resolved": True,
        "decision": "known_nonpositive_profit",
        "fiscal_end": evidence["fiscal_end"],
        "available_date": evidence["available_date"],
        "financial_age_days": age,
        "net_income_ttm": evidence["net_income_ttm"],
        "currency": CURRENCY,
        "source_accession": evidence["source_accession"],
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


def build(
    output_dir: Path = OUTPUT_DIR,
    companyfacts_cache: Path = COMPANYFACTS_CACHE,
) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    provenance, source_verification = prepare_verified_sources(output_dir)
    companyfacts = companyfacts_operand_audit(companyfacts_cache)
    evidence = exact_ttm_evidence()
    facts = direct_ttm_facts()
    resolutions = resolve_audit_observations()
    if not resolutions["resolved"].all():
        raise RuntimeError("not every declared JAMF audit observation resolved")

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_loss_evidence.json"
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
        "research_only": True,
        "ticker": TICKER,
        "cik": CIK,
        "pit_cutoff": PIT_CUTOFF,
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_audit_observation_count": int(resolutions["resolved"].sum()),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "source_value_verification": source_verification,
        "companyfacts_operand_audit": companyfacts,
        "sources": provenance,
        "reviewed_preipo_filings": REVIEWED_PREIPO_FILINGS,
        "rejected_later_filings": REJECTED_LATER_FILINGS,
        "outputs": {
            "strict_quarterly_facts": str(facts_path),
            "exact_ttm_loss_evidence": str(evidence_path),
            "audit_observation_resolution": str(resolution_path),
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--companyfacts-cache", type=Path, default=COMPANYFACTS_CACHE
    )
    args = parser.parse_args()
    print(json.dumps(build(args.output_dir, args.companyfacts_cache), indent=2))


if __name__ == "__main__":
    main()
