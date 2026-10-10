#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM growth evidence for AFYA.

Afya was an IFRS foreign private issuer and reported in Brazilian reais.  The
2020 half-year growth bundle is derived only from complete annual and
cumulative six-month periods; the 2020 annual bundle comes directly from one
audited 20-F table.  No cumulative period is split into a quarter.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen
import warnings

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/afya_exact_ttm_growth")
TICKER = "AFYA"
CIK = 1_771_007
CURRENCY = "BRL"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "IFRS-IASB"
PIT_CUTOFF = "2021-06-30"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

SOURCE_DOCUMENTS = {
    "6k_2019_08_29_h1_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2019-08-29",
        "accession": "0000950103-19-011488",
        "document": "dp111957_ex9901.htm",
        "local_path": "sources/dp111957_ex9901.htm",
        "expected_sha256": (
            "3510df2c517766407d8fda1b6fbfe852aaaeced4bb8ea22a7cd315c9ed650a66"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1771007/"
            "000095010319011488/dp111957_ex9901.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "UNAUDITED",
    },
    "20f_2020_04_20_fy2019": {
        "form": "20-F",
        "filed": "2020-04-20",
        "accession": "0001292814-20-001352",
        "document": "afyaform20f_2019.htm",
        "local_path": "sources/afyaform20f_2019.htm",
        "expected_sha256": (
            "9885ef40f70f5b50ef5e5711439b8b8a1efc50e48f0af1c4c78fe5269e31e144"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1771007/"
            "000129281420001352/afyaform20f_2019.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "AUDITED",
    },
    "6k_2020_08_27_h1_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2020-08-27",
        "accession": "0001292814-20-003307",
        "document": "ex99-1.htm",
        "local_path": "sources/ex99-1_h1_2020.htm",
        "expected_sha256": (
            "5c5cedf907d1e179f3a603dd35b879adeaf38943ed67d10f6641e67fbd9f960f"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1771007/"
            "000129281420003307/ex99-1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "UNAUDITED",
    },
    "20f_2021_04_30_fy2020": {
        "form": "20-F",
        "filed": "2021-04-30",
        "accession": "0001292814-21-001953",
        "document": "afyaform20f_2020.htm",
        "local_path": "sources/afyaform20f_2020.htm",
        "expected_sha256": (
            "cb1e16d272c0ac6cdb476dfbe6c56a2a3f30f2b410f25ab73d4bcbcc429555d4"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1771007/"
            "000129281421001953/afyaform20f_2020.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "audit_status": "AUDITED",
    },
}
ALLOWED_SOURCE_ACCESSIONS = {
    source["accession"] for source in SOURCE_DOCUMENTS.values()
}
REJECTED_LATER_FILINGS = {
    "0001292814-21-003519": {
        "form": "6-K",
        "filed": "2021-08-26",
        "document": "afyafs2q21_6k.htm",
        "reason": "filed after every audited AFYA signal date",
    }
}

SOURCE_VALUES_BRL_THOUSANDS = {
    "fy2019_original_net_income": {
        "source_id": "20f_2020_04_20_fy2019",
        "period": "FY2019",
        "table_column": "FY2019_BRL",
        "metric": "net_income",
        "line_item": "Net income",
        "value": 172_762,
    },
    "fy2018_original_net_income": {
        "source_id": "20f_2020_04_20_fy2019",
        "period": "FY2018",
        "table_column": "FY2018_BRL",
        "metric": "net_income",
        "line_item": "Net income",
        "value": 94_734,
    },
    "fy2019_original_revenue": {
        "source_id": "20f_2020_04_20_fy2019",
        "period": "FY2019",
        "table_column": "FY2019_BRL",
        "metric": "revenue",
        "line_item": "Net revenue",
        "value": 750_630,
    },
    "fy2018_original_revenue": {
        "source_id": "20f_2020_04_20_fy2019",
        "period": "FY2018",
        "table_column": "FY2018_BRL",
        "metric": "revenue",
        "line_item": "Net revenue",
        "value": 333_935,
    },
    "h1_2019_original_net_income": {
        "source_id": "6k_2019_08_29_h1_ex991",
        "period": "H1 2019",
        "table_column": "H1_2019_BRL",
        "metric": "net_income",
        "line_item": "Net income",
        "value": 70_802,
    },
    "h1_2018_net_income": {
        "source_id": "6k_2019_08_29_h1_ex991",
        "period": "H1 2018",
        "table_column": "H1_2018_BRL",
        "metric": "net_income",
        "line_item": "Net income",
        "value": 41_448,
    },
    "h1_2019_original_revenue": {
        "source_id": "6k_2019_08_29_h1_ex991",
        "period": "H1 2019",
        "table_column": "H1_2019_BRL",
        "metric": "revenue",
        "line_item": "Net revenue",
        "value": 323_071,
    },
    "h1_2018_revenue": {
        "source_id": "6k_2019_08_29_h1_ex991",
        "period": "H1 2018",
        "table_column": "H1_2018_BRL",
        "metric": "revenue",
        "line_item": "Net revenue",
        "value": 136_555,
    },
    "h1_2020_net_income": {
        "source_id": "6k_2020_08_27_h1_ex991",
        "period": "H1 2020",
        "table_column": "H1_2020_BRL",
        "metric": "net_income",
        "line_item": "Net income",
        "value": 167_556,
    },
    "h1_2019_comparative_net_income": {
        "source_id": "6k_2020_08_27_h1_ex991",
        "period": "H1 2019",
        "table_column": "H1_2019_BRL",
        "metric": "net_income",
        "line_item": "Net income",
        "value": 70_802,
    },
    "h1_2020_revenue": {
        "source_id": "6k_2020_08_27_h1_ex991",
        "period": "H1 2020",
        "table_column": "H1_2020_BRL",
        "metric": "revenue",
        "line_item": "Net revenue",
        "value": 546_515,
    },
    "h1_2019_comparative_revenue": {
        "source_id": "6k_2020_08_27_h1_ex991",
        "period": "H1 2019",
        "table_column": "H1_2019_BRL",
        "metric": "revenue",
        "line_item": "Net revenue",
        "value": 323_071,
    },
    "fy2020_net_income": {
        "source_id": "20f_2021_04_30_fy2020",
        "period": "FY2020",
        "table_column": "FY2020_BRL",
        "metric": "net_income",
        "line_item": "Net income",
        "value": 307_987,
    },
    "fy2019_comparative_net_income": {
        "source_id": "20f_2021_04_30_fy2020",
        "period": "FY2019",
        "table_column": "FY2019_BRL",
        "metric": "net_income",
        "line_item": "Net income",
        "value": 172_762,
    },
    "fy2020_revenue": {
        "source_id": "20f_2021_04_30_fy2020",
        "period": "FY2020",
        "table_column": "FY2020_BRL",
        "metric": "revenue",
        "line_item": "Net revenue",
        "value": 1_201_191,
    },
    "fy2019_comparative_revenue": {
        "source_id": "20f_2021_04_30_fy2020",
        "period": "FY2019",
        "table_column": "FY2019_BRL",
        "metric": "revenue",
        "line_item": "Net revenue",
        "value": 750_630,
    },
}

SOURCE_PARSE_SPECS = {
    "6k_2019_08_29_h1_ex991": {
        "context_phrases": (
            "Three-month period ended",
            "Six-month period ended",
            "June 30, 2019",
            "June 30, 2018",
            "unaudited",
        ),
        "columns": {
            "Q2_2019_BRL": "Q2 2019",
            "Q2_2018_BRL": "Q2 2018",
            "H1_2019_BRL": "H1 2019",
            "H1_2018_BRL": "H1 2018",
        },
        "row_labels": {"net_income": "Net income", "revenue": "Net revenue"},
    },
    "20f_2020_04_20_fy2019": {
        "context_phrases": (
            "Notes",
            "2019",
            "2018",
            "2017",
        ),
        "document_context_phrases": (
            "For the years ended December 31, 2019, 2018 and 2017",
            "In thousands of Brazilian reais",
        ),
        "columns": {
            "FY2019_BRL": "FY2019",
            "FY2018_BRL": "FY2018",
            "FY2017_BRL": "FY2017",
        },
        "row_labels": {"net_income": "Net income", "revenue": "Net revenue"},
    },
    "6k_2020_08_27_h1_ex991": {
        "context_phrases": (
            "Three-month period ended",
            "Six-month period ended",
            "June 30, 2020",
            "June 30, 2019",
            "unaudited",
        ),
        "columns": {
            "Q2_2020_BRL": "Q2 2020",
            "Q2_2019_BRL": "Q2 2019",
            "H1_2020_BRL": "H1 2020",
            "H1_2019_BRL": "H1 2019",
        },
        "row_labels": {"net_income": "Net income", "revenue": "Net revenue"},
    },
    "20f_2021_04_30_fy2020": {
        "context_phrases": (
            "Notes",
            "2020",
            "2019",
            "2018",
        ),
        "document_context_phrases": (
            "For the years ended December 31, 2020, 2019 and 2018",
            "In thousands of Brazilian reais",
        ),
        "columns": {
            "FY2020_BRL": "FY2020",
            "FY2019_BRL": "FY2019",
            "FY2018_BRL": "FY2018",
        },
        "row_labels": {"net_income": "Net income", "revenue": "Net revenue"},
    },
}

GROWTH_SPECS = {
    "2020-06-30": {
        "available_date": "2020-08-27",
        "current_net_income_formula": "FY2019 - H1_2019 + H1_2020",
        "current_net_income_terms": (
            (1, "fy2019_original_net_income"),
            (-1, "h1_2019_comparative_net_income"),
            (1, "h1_2020_net_income"),
        ),
        "prior_net_income_formula": "FY2018 - H1_2018 + H1_2019",
        "prior_net_income_terms": (
            (1, "fy2018_original_net_income"),
            (-1, "h1_2018_net_income"),
            (1, "h1_2019_original_net_income"),
        ),
        "current_revenue_formula": "FY2019 - H1_2019 + H1_2020",
        "current_revenue_terms": (
            (1, "fy2019_original_revenue"),
            (-1, "h1_2019_comparative_revenue"),
            (1, "h1_2020_revenue"),
        ),
        "prior_revenue_formula": "FY2018 - H1_2018 + H1_2019",
        "prior_revenue_terms": (
            (1, "fy2018_original_revenue"),
            (-1, "h1_2018_revenue"),
            (1, "h1_2019_original_revenue"),
        ),
        "expected": {
            "net_income_ttm": 269_516,
            "prior_net_income_ttm": 124_088,
            "revenue_ttm": 974_074,
            "prior_revenue_ttm": 520_451,
        },
        "form": "20-F_PLUS_6-K_H1_EXACT_TTM_GROWTH",
    },
    "2020-12-31": {
        "available_date": "2021-04-30",
        "current_net_income_formula": "FY2020",
        "current_net_income_terms": ((1, "fy2020_net_income"),),
        "prior_net_income_formula": "FY2019 comparative",
        "prior_net_income_terms": ((1, "fy2019_comparative_net_income"),),
        "current_revenue_formula": "FY2020",
        "current_revenue_terms": ((1, "fy2020_revenue"),),
        "prior_revenue_formula": "FY2019 comparative",
        "prior_revenue_terms": ((1, "fy2019_comparative_revenue"),),
        "expected": {
            "net_income_ttm": 307_987,
            "prior_net_income_ttm": 172_762,
            "revenue_ttm": 1_201_191,
            "prior_revenue_ttm": 750_630,
        },
        "form": "20-F_EXACT_ANNUAL_TTM_GROWTH",
    },
}

AUDIT_OBSERVATIONS = tuple(
    (
        f"liq2000000-age{maximum_age_days}-growth",
        signal_date,
        maximum_age_days,
    )
    for maximum_age_days in (150, 365, 550)
    for signal_date in ("2020-09-30", "2020-11-30", "2021-06-30")
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalize_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").replace("−", "-").split()).casefold()


def _row_numbers(row) -> list[int]:
    text = " ".join(row.stripped_strings).replace("\xa0", " ")
    text = re.sub(r"\s*,\s*", ",", text)
    tokens = re.findall(
        r"\(\s*\d[\d,]*\s*\)|(?<![\w])\d[\d,]*(?![\w])", text
    )
    values = []
    for token in tokens:
        digits = re.sub(r"\D", "", token)
        if digits:
            value = int(digits)
            values.append(-value if "(" in token else value)
    return values


def _parse_source_table(source_id: str, raw: bytes) -> dict[str, dict]:
    spec = SOURCE_PARSE_SPECS[source_id]
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    context = tuple(_normalize_text(item) for item in spec["context_phrases"])
    document_text = _normalize_text(" ".join(soup.stripped_strings))
    document_context = tuple(
        _normalize_text(item)
        for item in spec.get("document_context_phrases", ())
    )
    if not all(item in document_text for item in document_context):
        raise RuntimeError(f"AFYA document context changed for {source_id}")
    expected_count = len(spec["columns"])
    candidates = []
    for table in soup.find_all("table"):
        table_text = _normalize_text(" ".join(table.stripped_strings))
        if not all(item in table_text for item in context):
            continue
        parsed_rows = {}
        for metric, label in spec["row_labels"].items():
            normalized_label = _normalize_text(label)
            matches = []
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
                    matches.append(dict(zip(
                        spec["columns"], values[-expected_count:], strict=True
                    )))
            if len(matches) != 1:
                break
            parsed_rows[metric] = matches[0]
        if set(parsed_rows) == set(spec["row_labels"]):
            candidates.append(parsed_rows)
    if not candidates:
        raise RuntimeError(f"no unambiguous AFYA source table for {source_id}")
    canonical = json.dumps(candidates[0], sort_keys=True)
    if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
        raise RuntimeError(f"conflicting AFYA source tables for {source_id}")
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
            raise ValueError(f"source {source_id} has mixed currency or scale")
        if source["accounting_standard"] != ACCOUNTING_STANDARD:
            raise ValueError(f"source {source_id} is not IFRS-IASB")
        if accession.replace("-", "") not in source["url"]:
            raise ValueError(f"source {source_id} URL does not lock accession")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"source {source_id} URL does not lock document")
        relative_path = Path(source["local_path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"source {source_id} has unsafe local_path")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"source {source_id} has invalid expected SHA-256")
    for value_id, value in SOURCE_VALUES_BRL_THOUSANDS.items():
        if value["source_id"] not in documents:
            raise ValueError(f"source value {value_id} has no locked source")


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw source set does not match the source lock")
    parsed = {
        source_id: _parse_source_table(source_id, raw)
        for source_id, raw in raw_by_source.items()
    }
    verified = []
    for value_id, value in SOURCE_VALUES_BRL_THOUSANDS.items():
        source_id = value["source_id"]
        column = value["table_column"]
        spec = SOURCE_PARSE_SPECS[source_id]
        if column not in spec["columns"]:
            raise RuntimeError(f"source value {value_id} has no parsed period")
        if spec["columns"][column] != value["period"]:
            raise RuntimeError(f"source value {value_id} period mapping changed")
        if _normalize_text(spec["row_labels"][value["metric"]]) != (
            _normalize_text(value["line_item"])
        ):
            raise RuntimeError(f"source value {value_id} line item mapping changed")
        parsed_value = parsed[source_id][value["metric"]][column]
        expected_value = int(value["value"])
        if parsed_value != expected_value:
            raise RuntimeError(
                f"source value {value_id} changed: parsed {parsed_value}, "
                f"expected {expected_value}"
            )
        verified.append({
            "value_id": value_id,
            "source_id": source_id,
            "metric": value["metric"],
            "period": value["period"],
            "table_column": column,
            "line_item": value["line_item"],
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
                f"AFYA source SHA-256 mismatch for {source_id}: {actual_sha256}"
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


def _value(value_id: str) -> int:
    return int(SOURCE_VALUES_BRL_THOUSANDS[value_id]["value"])


def _sum_terms(terms: Iterable[tuple[int, str]]) -> int:
    return sum(coefficient * _value(value_id) for coefficient, value_id in terms)


def _growth(current: int, prior: int) -> float:
    if prior == 0:
        raise ValueError("exact TTM growth denominator cannot be zero")
    return float((Decimal(current) - Decimal(prior)) / abs(Decimal(prior)))


def _source_ids(spec: dict) -> list[str]:
    term_fields = (
        "current_net_income_terms",
        "prior_net_income_terms",
        "current_revenue_terms",
        "prior_revenue_terms",
    )
    return list(dict.fromkeys(
        SOURCE_VALUES_BRL_THOUSANDS[value_id]["source_id"]
        for field in term_fields
        for _, value_id in spec[field]
    ))


def validate_comparative_consistency() -> None:
    pairs = (
        ("h1_2019_original_net_income", "h1_2019_comparative_net_income"),
        ("h1_2019_original_revenue", "h1_2019_comparative_revenue"),
        ("fy2019_original_net_income", "fy2019_comparative_net_income"),
        ("fy2019_original_revenue", "fy2019_comparative_revenue"),
    )
    for original, comparative in pairs:
        if _value(original) != _value(comparative):
            raise RuntimeError(
                f"AFYA later comparative changed without isolation: {original}"
            )


def exact_ttm_growth_evidence() -> list[dict]:
    validate_source_lock()
    validate_comparative_consistency()
    rows = []
    for fiscal_end, spec in GROWTH_SPECS.items():
        values = {
            "net_income_ttm": _sum_terms(spec["current_net_income_terms"]),
            "prior_net_income_ttm": _sum_terms(spec["prior_net_income_terms"]),
            "revenue_ttm": _sum_terms(spec["current_revenue_terms"]),
            "prior_revenue_ttm": _sum_terms(spec["prior_revenue_terms"]),
        }
        if values != spec["expected"]:
            raise RuntimeError(f"AFYA exact TTM values changed for {fiscal_end}")
        source_ids = _source_ids(spec)
        sources = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
        if spec["available_date"] != max(source["filed"] for source in sources):
            raise RuntimeError(f"AFYA availability date changed for {fiscal_end}")
        rows.append({
            "ticker": TICKER,
            "evidence_kind": "exact_complete_ttm_growth",
            "fiscal_end": fiscal_end,
            "available_date": spec["available_date"],
            "currency": CURRENCY,
            "source_scale": SOURCE_SCALE,
            "accounting_standard": ACCOUNTING_STANDARD,
            **{name: value * SOURCE_SCALE for name, value in values.items()},
            "net_income_growth": _growth(
                values["net_income_ttm"], values["prior_net_income_ttm"]
            ),
            "revenue_growth": _growth(
                values["revenue_ttm"], values["prior_revenue_ttm"]
            ),
            "formulas": {
                name: spec[name]
                for name in (
                    "current_net_income_formula",
                    "prior_net_income_formula",
                    "current_revenue_formula",
                    "prior_revenue_formula",
                )
            },
            "source_ids": source_ids,
            "source_accessions": [source["accession"] for source in sources],
            "source_urls": [source["url"] for source in sources],
            "form": spec["form"],
        })
    return rows


def direct_growth_facts(fetched_at: str | None = None) -> pd.DataFrame:
    if fetched_at is None:
        fetched_at = str(
            pd.Timestamp.now("UTC").tz_localize(None).normalize().date()
        )
    rows = []
    for evidence in exact_ttm_growth_evidence():
        metadata = {
            "ticker": TICKER,
            "fiscal_end": evidence["fiscal_end"],
            "available_date": evidence["available_date"],
            "taxonomy": "ifrs-full",
            "form": evidence["form"],
            "accession": "+".join(evidence["source_accessions"]),
            "fetched_at": fetched_at,
        }
        for metric in (
            "net_income_ttm",
            "net_income_growth",
            "revenue_ttm",
            "revenue_growth",
        ):
            rows.append({
                **metadata,
                "metric": metric,
                "value": evidence[metric],
                "concept": f"afya_exact_ttm:{metric}:BRL",
            })
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    ).reset_index(drop=True)


def resolve_observation(signal_date: str, maximum_age_days: int) -> dict:
    signal = pd.Timestamp(signal_date)
    evidence = pd.DataFrame(exact_ttm_growth_evidence())
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
            "reason": "no complete exact TTM growth bundle within the age limit",
        }
    row = eligible.iloc[-1]
    return {
        "resolved": True,
        "decision": "usable_exact_ttm_growth",
        "fiscal_end": row["fiscal_end"].strftime("%Y-%m-%d"),
        "available_date": row["available_date"].strftime("%Y-%m-%d"),
        "financial_age_days": int((signal - row["available_date"]).days),
        "net_income_ttm": int(row["net_income_ttm"]),
        "net_income_growth": float(row["net_income_growth"]),
        "revenue_ttm": int(row["revenue_ttm"]),
        "revenue_growth": float(row["revenue_growth"]),
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
    _, provenance, source_verification = prepare_verified_sources(output_dir)
    evidence = exact_ttm_growth_evidence()
    facts = direct_growth_facts()
    resolutions = resolve_audit_observations()
    if not resolutions["resolved"].all():
        raise RuntimeError("not every declared AFYA audit observation resolved")

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_growth_evidence.json"
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
        "accepted_direct_growth_fact_count": len(facts),
        "accepted_exact_growth_bundle_count": len(evidence),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "resolved_audit_observation_count": len(resolutions),
        "source_documents": provenance,
        "source_value_verification": source_verification,
        "comparative_restatement_check": {
            "h1_2019_unchanged_through_2020_h1_filing": True,
            "fy2019_unchanged_through_2020_annual_filing": True,
            "later_values_backfilled": False,
        },
        "later_filing_rejections": REJECTED_LATER_FILINGS,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path)
            },
            "exact_ttm_growth_evidence": {
                "path": str(evidence_path), "sha256": _sha256(evidence_path)
            },
            "audit_observation_resolution": {
                "path": str(resolution_path), "sha256": _sha256(resolution_path)
            },
        },
        "guardrail": (
            "Uses complete same-currency IFRS annual and cumulative operands. "
            "It emits only two four-metric direct growth bundles, never splits "
            "a cumulative period, and does not use post-cutoff filings."
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
