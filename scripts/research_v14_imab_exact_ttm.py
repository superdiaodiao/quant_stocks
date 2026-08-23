#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM evidence for IMAB.

This supplement deliberately keeps cumulative six- and nine-month facts as
cumulative operands.  It emits direct ``net_income_ttm`` loss observations and
one exact annual-over-annual TTM growth observation; it never manufactures a
quarter from a cumulative period.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from copy import deepcopy
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


OUTPUT_DIR = Path("output/research_only/v14/imab_exact_ttm")
TICKER = "IMAB"
CIK = 1_778_016
CURRENCY = "RMB"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP"
PIT_CUTOFF = "2021-09-30"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

# These are the only documents allowed to contribute an operand.  In
# particular, the 2022 20-F is deliberately outside the source lock.
SOURCE_DOCUMENTS = {
    "f1_2020_12_01": {
        "form": "F-1",
        "filed": "2020-12-01",
        "accession": "0001193125-20-307060",
        "document": "d98473df1.htm",
        "local_path": "sources/d98473df1.htm",
        "expected_sha256": (
            "3c26c1b35c37bc15688234b154d26997e0a77c95d1b664f5c48badc8d87c32fe"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1778016/"
            "000119312520307060/d98473df1.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2021_02_05_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2021-02-05",
        "accession": "0001193125-21-030570",
        "document": "d123013dex991.htm",
        "local_path": "sources/d123013dex991.htm",
        "expected_sha256": (
            "2f1bbf588618a814923c21c59c867828f38c9e6ef01ede98996562a35acfeef2"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1778016/"
            "000119312521030570/d123013dex991.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2021_02_05_ex992": {
        "form": "6-K/EX-99.2",
        "filed": "2021-02-05",
        "accession": "0001193125-21-030570",
        "document": "d123013dex992.htm",
        "local_path": "sources/d123013dex992.htm",
        "expected_sha256": (
            "b1245a105808c99b06eeff98ac99aa81fe41be12af21e29e787c9594b83d0729"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1778016/"
            "000119312521030570/d123013dex992.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "20f_2021_04_28": {
        "form": "20-F",
        "filed": "2021-04-28",
        "accession": "0001193125-21-135440",
        "document": "d10811d20f.htm",
        "local_path": "sources/d10811d20f.htm",
        "expected_sha256": (
            "e4485327a9d8a1e4225ea092bb790b71c054cefd5d49fc1fb888e8bc13be8a9d"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1778016/"
            "000119312521135440/d10811d20f.htm"
        ),
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2021_08_31_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2021-08-31",
        "accession": "0001193125-21-261222",
        "document": "d212638dex991.htm",
        "local_path": "sources/d212638dex991.htm",
        "expected_sha256": (
            "77526e3223c0e03069da4814677fc5055edd782a627c04df3822c543adc830dc"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1778016/"
            "000119312521261222/d212638dex991.htm"
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
    "0001193125-22-133550": {
        "form": "20-F",
        "filed": "2022-04-29",
        "reason": "filed after every audited 2021 signal date",
    }
}

# Values are transcribed in the presentation unit used by the cited tables:
# RMB thousands.  Profit is consistently the issuer-attributable line, not an
# ordinary-shareholder or ADS per-share numerator.
OPERANDS_RMB_THOUSANDS = {
    "f1_fy2019_net_income": {
        "source_id": "f1_2020_12_01",
        "period": "FY2019",
        "table_column": "FY2019_RMB",
        "metric": "net_income_attributable_to_imab",
        "line_item": "Net loss attributable to I-Mab",
        "value": -1_451_950,
    },
    "f1_h1_2019_net_income": {
        "source_id": "f1_2020_12_01",
        "period": "H1 2019",
        "table_column": "H1_2019_RMB",
        "metric": "net_income_attributable_to_imab",
        "line_item": "Net loss attributable to I-Mab",
        "value": -857_337,
    },
    "f1_h1_2020_net_income": {
        "source_id": "f1_2020_12_01",
        "period": "H1 2020",
        "table_column": "H1_2020_RMB",
        "metric": "net_income_attributable_to_imab",
        "line_item": "Net loss attributable to I-Mab",
        "value": -582_853,
    },
    "6k_fy2019_net_income": {
        "source_id": "6k_2021_02_05_ex992",
        "period": "FY2019",
        "table_column": "FY2019_RMB",
        "metric": "net_income_attributable_to_imab",
        "line_item": "Net loss attributable to I-Mab",
        "value": -1_451_950,
    },
    "6k_9m_2019_net_income": {
        "source_id": "6k_2021_02_05_ex991",
        "period": "9M 2019",
        "table_column": "9M_2019_RMB",
        "metric": "net_income_attributable_to_imab",
        "line_item": "Net loss attributable to I-MAB",
        "value": -1_103_380,
    },
    "6k_9m_2020_net_income": {
        "source_id": "6k_2021_02_05_ex991",
        "period": "9M 2020",
        "table_column": "9M_2020_RMB",
        "metric": "net_income_attributable_to_imab",
        "line_item": "Net loss attributable to I-MAB",
        "value": -570_635,
    },
    "20f_fy2019_net_income": {
        "source_id": "20f_2021_04_28",
        "period": "FY2019",
        "table_column": "FY2019_RMB",
        "metric": "net_income_attributable_to_imab",
        "line_item": "Net income (loss) attributable to I-MAB",
        "value": -1_451_950,
    },
    "20f_fy2020_net_income": {
        "source_id": "20f_2021_04_28",
        "period": "FY2020",
        "table_column": "FY2020_RMB",
        "metric": "net_income_attributable_to_imab",
        "line_item": "Net income (loss) attributable to I-MAB",
        "value": 470_915,
    },
    "20f_fy2019_revenue": {
        "source_id": "20f_2021_04_28",
        "period": "FY2019",
        "table_column": "FY2019_RMB",
        "metric": "revenue",
        "line_item": "Licensing and collaboration revenue",
        "value": 30_000,
    },
    "20f_fy2020_revenue": {
        "source_id": "20f_2021_04_28",
        "period": "FY2020",
        "table_column": "FY2020_RMB",
        "metric": "revenue",
        "line_item": "Licensing and collaboration revenue",
        "value": 1_542_668,
    },
    "6k_h1_2020_net_income": {
        "source_id": "6k_2021_08_31_ex991",
        "period": "H1 2020",
        "table_column": "H1_2020_RMB",
        "metric": "net_income_attributable_to_imab",
        "line_item": "Net loss attributable to I-MAB",
        "value": -582_853,
    },
    "6k_h1_2021_net_income": {
        "source_id": "6k_2021_08_31_ex991",
        "period": "H1 2021",
        "table_column": "H1_2021_RMB",
        "metric": "net_income_attributable_to_imab",
        "line_item": "Net loss attributable to I-MAB",
        "value": -1_076_481,
    },
}

# Column order is the order of numeric cells in the named source table row.
# USD convenience translations are retained in the shape so an RMB operand
# cannot silently slide into the wrong column.
SOURCE_PARSE_SPECS = {
    "f1_2020_12_01": {
        "context_phrases": (
            "For the Year Ended December 31",
            "For the Six Months Ended June 30",
            "in thousands",
        ),
        "columns": {
            "FY2017_RMB": "FY2017",
            "FY2018_RMB": "FY2018",
            "FY2019_RMB": "FY2019",
            "FY2019_USD": "FY2019 USD translation",
            "H1_2019_RMB": "H1 2019",
            "H1_2020_RMB": "H1 2020",
            "H1_2020_USD": "H1 2020 USD translation",
        },
        "row_labels": {
            "net_income_attributable_to_imab": (
                "Net loss attributable to I-Mab",
            ),
        },
    },
    "6k_2021_02_05_ex991": {
        "context_phrases": (
            "Nine Months Ended September 30",
            "2019",
            "2020",
            "RMB",
        ),
        "columns": {
            "9M_2019_RMB": "9M 2019",
            "9M_2020_RMB": "9M 2020",
            "9M_2020_USD": "9M 2020 USD translation",
        },
        "row_labels": {
            "net_income_attributable_to_imab": (
                "Net loss attributable to I-MAB",
            ),
        },
    },
    "6k_2021_02_05_ex992": {
        "context_phrases": (
            "For the Year Ended December 31",
            "For the Nine Months Ended September 30",
            "in thousands",
        ),
        "columns": {
            "FY2017_RMB": "FY2017",
            "FY2018_RMB": "FY2018",
            "FY2019_RMB": "FY2019",
            "FY2019_USD": "FY2019 USD translation",
            "9M_2019_RMB": "9M 2019",
            "9M_2020_RMB": "9M 2020",
            "9M_2020_USD": "9M 2020 USD translation",
        },
        "row_labels": {
            "net_income_attributable_to_imab": (
                "Net loss attributable to I-Mab",
            ),
        },
    },
    "20f_2021_04_28": {
        "context_phrases": (
            "For the Year Ended December 31",
            "2019",
            "2020",
            "in thousands",
        ),
        "columns": {
            "FY2017_RMB": "FY2017",
            "FY2018_RMB": "FY2018",
            "FY2019_RMB": "FY2019",
            "FY2020_RMB": "FY2020",
            "FY2020_USD": "FY2020 USD translation",
        },
        "row_labels": {
            "net_income_attributable_to_imab": (
                "Net income (loss) attributable to I-Mab",
            ),
            "revenue": ("Licensing and collaboration revenue",),
        },
    },
    "6k_2021_08_31_ex991": {
        "context_phrases": (
            "For the six months ended June 30",
            "2020",
            "2021",
            "RMB",
        ),
        "columns": {
            "H1_2020_RMB": "H1 2020",
            "H1_2021_RMB": "H1 2021",
            "H1_2021_USD": "H1 2021 USD translation",
        },
        "row_labels": {
            "net_income_attributable_to_imab": (
                "Net loss attributable to I-MAB",
            ),
        },
    },
}

TTM_SPECS = {
    "2020-06-30": {
        "available_date": "2020-12-01",
        "formula": "FY2019 - H1_2019 + H1_2020",
        "terms": (
            (1, "f1_fy2019_net_income"),
            (-1, "f1_h1_2019_net_income"),
            (1, "f1_h1_2020_net_income"),
        ),
        "expected_rmb_thousands": -1_177_466,
        "form": "F-1_CUMULATIVE_TTM",
    },
    "2020-09-30": {
        "available_date": "2021-02-05",
        "formula": "FY2019 - 9M_2019 + 9M_2020",
        "terms": (
            (1, "6k_fy2019_net_income"),
            (-1, "6k_9m_2019_net_income"),
            (1, "6k_9m_2020_net_income"),
        ),
        "expected_rmb_thousands": -919_205,
        "form": "6-K_9M_CUMULATIVE_TTM",
    },
    "2021-06-30": {
        "available_date": "2021-08-31",
        "formula": "FY2020 - H1_2020 + H1_2021",
        "terms": (
            (1, "20f_fy2020_net_income"),
            (-1, "6k_h1_2020_net_income"),
            (1, "6k_h1_2021_net_income"),
        ),
        "expected_rmb_thousands": -22_713,
        "form": "20-F_PLUS_6-K_H1_CUMULATIVE_TTM",
    },
}

GROWTH_SPEC = {
    "fiscal_end": "2020-12-31",
    "prior_fiscal_end": "2019-12-31",
    "available_date": "2021-04-28",
    "current_net_income": "20f_fy2020_net_income",
    "prior_net_income": "20f_fy2019_net_income",
    "current_revenue": "20f_fy2020_revenue",
    "prior_revenue": "20f_fy2019_revenue",
    "source_id": "20f_2021_04_28",
}

AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", "2021-01-29", 150),
    ("liq2000000-age150-growth", "2021-02-26", 150),
    ("liq10000000-age150-growth", "2021-02-26", 150),
    ("liq2000000-age150-growth", "2021-05-28", 150),
    ("liq2000000-age365-growth", "2021-05-28", 365),
    ("liq10000000-age150-growth", "2021-05-28", 150),
    ("liq10000000-age365-growth", "2021-05-28", 365),
    ("liq2000000-age150-growth", "2021-06-30", 150),
    ("liq2000000-age365-growth", "2021-06-30", 365),
    ("liq10000000-age150-growth", "2021-06-30", 150),
    ("liq10000000-age365-growth", "2021-06-30", 365),
    ("liq2000000-age150-growth", "2021-07-30", 150),
    ("liq2000000-age365-growth", "2021-07-30", 365),
    ("liq10000000-age150-growth", "2021-07-30", 150),
    ("liq10000000-age365-growth", "2021-07-30", 365),
    ("liq2000000-age150-growth", "2021-09-30", 150),
    ("liq2000000-age365-growth", "2021-09-30", 365),
    ("liq10000000-age150-growth", "2021-09-30", 150),
    ("liq10000000-age365-growth", "2021-09-30", 365),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


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
        if not digits:
            continue
        value = int(digits)
        values.append(-value if "(" in token else value)
    return values


def _parse_source_table(source_id: str, raw: bytes) -> dict[str, dict]:
    spec = SOURCE_PARSE_SPECS[source_id]
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    candidates = []
    expected_count = len(spec["columns"])
    normalized_context = tuple(
        _normalize_text(phrase) for phrase in spec["context_phrases"]
    )
    for table in soup.find_all("table"):
        table_text = _normalize_text(" ".join(table.stripped_strings))
        if not all(phrase in table_text for phrase in normalized_context):
            continue
        parsed_rows = {}
        for metric, labels in spec["row_labels"].items():
            normalized_labels = tuple(_normalize_text(label) for label in labels)
            matching_values = []
            for row in table.find_all("tr"):
                row_text = _normalize_text(" ".join(row.stripped_strings))
                if not row_text.startswith(normalized_labels):
                    continue
                values = _row_numbers(row)
                if len(values) == expected_count:
                    matching_values.append(tuple(values))
            unique_values = set(matching_values)
            if len(unique_values) != 1:
                break
            parsed_rows[metric] = dict(zip(
                spec["columns"], unique_values.pop(), strict=True
            ))
        if set(parsed_rows) == set(spec["row_labels"]):
            candidates.append(parsed_rows)
    if not candidates:
        raise RuntimeError(
            f"no unambiguous source table found for IMAB {source_id}"
        )
    canonical = json.dumps(candidates[0], sort_keys=True)
    if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
        raise RuntimeError(f"conflicting source tables found for {source_id}")
    return candidates[0]


def verify_operand_sources(raw_by_source: dict[str, bytes]) -> list[dict]:
    """Parse every declared operand back from its locked official table."""
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw source set does not match the source lock")
    parsed = {
        source_id: _parse_source_table(source_id, raw)
        for source_id, raw in raw_by_source.items()
    }
    verified = []
    for operand_id, operand in OPERANDS_RMB_THOUSANDS.items():
        source_id = operand["source_id"]
        source_spec = SOURCE_PARSE_SPECS[source_id]
        metric = operand["metric"]
        column = operand["table_column"]
        if metric not in source_spec["row_labels"]:
            raise RuntimeError(f"operand {operand_id} has no parsed line item")
        if column not in source_spec["columns"]:
            raise RuntimeError(f"operand {operand_id} has no parsed period")
        if source_spec["columns"][column] != operand["period"]:
            raise RuntimeError(f"operand {operand_id} period mapping changed")
        normalized_line_item = _normalize_text(operand["line_item"])
        normalized_labels = {
            _normalize_text(label)
            for label in source_spec["row_labels"][metric]
        }
        if normalized_line_item not in normalized_labels:
            raise RuntimeError(f"operand {operand_id} line item mapping changed")
        parsed_value = parsed[source_id][metric][column]
        expected_value = int(operand["value"])
        if parsed_value != expected_value:
            raise RuntimeError(
                f"operand {operand_id} source changed: "
                f"parsed {parsed_value}, expected {expected_value}"
            )
        verified.append({
            "operand_id": operand_id,
            "source_id": source_id,
            "period": operand["period"],
            "table_column": column,
            "line_item": operand["line_item"],
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "expected_value": expected_value,
            "parsed_value": parsed_value,
        })
    if {item["operand_id"] for item in verified} != set(
        OPERANDS_RMB_THOUSANDS
    ):
        raise RuntimeError("operand verification coverage is incomplete")
    return verified


def prepare_verified_sources(
    output_dir: Path,
) -> tuple[dict[str, bytes], dict[str, dict], list[dict]]:
    """Download missing official files, verify hashes, and parse operands."""
    output_dir = Path(output_dir)
    raw_by_source = {}
    provenance = {}
    for source_id, source in SOURCE_DOCUMENTS.items():
        relative_path = Path(source["local_path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"unsafe source local_path: {relative_path}")
        local_path = output_dir / relative_path
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
                f"IMAB source SHA-256 mismatch for {source_id}: "
                f"{actual_sha256}"
            )
        raw_by_source[source_id] = raw
        provenance[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha256,
            "bytes": len(raw),
            "downloaded": downloaded,
        }
    operand_verification = verify_operand_sources(raw_by_source)
    return raw_by_source, provenance, operand_verification


def _operand_value(operand_id: str) -> int:
    return int(OPERANDS_RMB_THOUSANDS[operand_id]["value"])


def _source_ids_for_terms(terms: Iterable[tuple[int, str]]) -> list[str]:
    return list(dict.fromkeys(
        OPERANDS_RMB_THOUSANDS[operand_id]["source_id"]
        for _, operand_id in terms
    ))


def _growth(current: int, prior: int) -> float:
    if prior == 0:
        raise ValueError("exact TTM growth denominator cannot be zero")
    return float(
        (Decimal(current) - Decimal(prior)) / abs(Decimal(prior))
    )


def validate_source_lock(
    sources: dict[str, dict] | None = None,
) -> None:
    """Reject mixed currency, unapproved accessions, and hindsight filings."""
    documents = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in documents.items():
        accession = source["accession"]
        if accession in REJECTED_LATER_FILINGS:
            raise ValueError(f"later filing is forbidden: {accession}")
        if accession not in ALLOWED_SOURCE_ACCESSIONS:
            raise ValueError(f"unapproved source accession: {accession}")
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(
                f"source {source_id} was filed after PIT cutoff: "
                f"{source['filed']}"
            )
        if source["currency"] != CURRENCY or source["scale"] != SOURCE_SCALE:
            raise ValueError(f"source {source_id} has mixed currency or scale")
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
            raise ValueError(f"source {source_id} has invalid expected SHA-256")

    for operand_id, operand in OPERANDS_RMB_THOUSANDS.items():
        if operand["source_id"] not in documents:
            raise ValueError(f"operand {operand_id} has no locked source")


def exact_ttm_evidence() -> list[dict]:
    """Return three exact nonpositive TTM observations in actual RMB units."""
    validate_source_lock()
    rows = []
    for fiscal_end, spec in TTM_SPECS.items():
        value_thousands = sum(
            coefficient * _operand_value(operand_id)
            for coefficient, operand_id in spec["terms"]
        )
        if value_thousands != spec["expected_rmb_thousands"]:
            raise RuntimeError(
                f"IMAB exact TTM changed for {fiscal_end}: {value_thousands}"
            )
        if value_thousands > 0:
            raise RuntimeError("direct exact-TTM layer is exclusion-only")
        source_ids = _source_ids_for_terms(spec["terms"])
        source_docs = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
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
            "source_accessions": list(dict.fromkeys(
                source["accession"] for source in source_docs
            )),
            "source_urls": [source["url"] for source in source_docs],
            "form": spec["form"],
        })
    return rows


def direct_ttm_facts(fetched_at: str | None = None) -> pd.DataFrame:
    """Expose exact profit states in the existing direct-TTM fact structure.

    The positive FY2020 state is necessary even though it cannot create growth
    eligibility by itself: without it, the older 2020-09-30 loss would remain
    the latest direct state during May-July 2021.
    """
    if fetched_at is None:
        fetched_at = str(
            pd.Timestamp.now("UTC").tz_localize(None).normalize().date()
        )
    annual_growth = exact_ttm_growth_evidence()
    evidence_rows = exact_ttm_evidence() + [{
        "fiscal_end": annual_growth["fiscal_end"],
        "available_date": annual_growth["available_date"],
        "net_income_ttm": annual_growth["net_income_ttm"],
        "form": annual_growth["form"],
        "source_accessions": annual_growth["source_accessions"],
    }]
    rows = []
    for evidence in evidence_rows:
        rows.append({
            "ticker": TICKER,
            "fiscal_end": evidence["fiscal_end"],
            "available_date": evidence["available_date"],
            "metric": "net_income_ttm",
            "value": evidence["net_income_ttm"],
            "taxonomy": "us-gaap",
            "concept": (
                "imab_exact_ttm:NetIncomeLossAttributableToIMAB:RMB"
            ),
            "form": evidence["form"],
            "accession": "+".join(evidence["source_accessions"]),
            "fetched_at": fetched_at,
        })
    annual_fact_metadata = {
        "ticker": TICKER,
        "fiscal_end": annual_growth["fiscal_end"],
        "available_date": annual_growth["available_date"],
        "taxonomy": "us-gaap",
        "form": annual_growth["form"],
        "accession": "+".join(annual_growth["source_accessions"]),
        "fetched_at": fetched_at,
    }
    for metric, value, concept in (
        (
            "net_income_growth",
            annual_growth["net_income_growth"],
            "imab_exact_ttm_growth:NetIncomeLossAttributableToIMAB:RMB",
        ),
        (
            "revenue_ttm",
            annual_growth["revenue_ttm"],
            "imab_exact_ttm:Revenue:RMB",
        ),
        (
            "revenue_growth",
            annual_growth["revenue_growth"],
            "imab_exact_ttm_growth:Revenue:RMB",
        ),
    ):
        rows.append({
            **annual_fact_metadata,
            "metric": metric,
            "value": value,
            "concept": concept,
        })
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "available_date"]
    ).reset_index(drop=True)


def exact_ttm_growth_evidence() -> dict:
    """Return exact FY2020-over-FY2019 TTM growth from one RMB 20-F table."""
    validate_source_lock()
    current_net_income = _operand_value(GROWTH_SPEC["current_net_income"])
    prior_net_income = _operand_value(GROWTH_SPEC["prior_net_income"])
    current_revenue = _operand_value(GROWTH_SPEC["current_revenue"])
    prior_revenue = _operand_value(GROWTH_SPEC["prior_revenue"])
    source = SOURCE_DOCUMENTS[GROWTH_SPEC["source_id"]]
    return {
        "ticker": TICKER,
        "evidence_kind": "exact_complete_fiscal_year_ttm_growth",
        "fiscal_end": GROWTH_SPEC["fiscal_end"],
        "prior_fiscal_end": GROWTH_SPEC["prior_fiscal_end"],
        "available_date": GROWTH_SPEC["available_date"],
        "currency": CURRENCY,
        "source_scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "net_income_ttm": current_net_income * SOURCE_SCALE,
        "prior_net_income_ttm": prior_net_income * SOURCE_SCALE,
        "net_income_growth": _growth(current_net_income, prior_net_income),
        "revenue_ttm": current_revenue * SOURCE_SCALE,
        "prior_revenue_ttm": prior_revenue * SOURCE_SCALE,
        "revenue_growth": _growth(current_revenue, prior_revenue),
        "source_ids": [GROWTH_SPEC["source_id"]],
        "source_accessions": [source["accession"]],
        "source_urls": [source["url"]],
        "form": source["form"],
    }


def resolve_observation(
    signal_date: str | pd.Timestamp,
    maximum_age_days: int,
) -> dict:
    """Choose the newest exact fiscal-period evidence known by a signal date."""
    as_of = pd.Timestamp(signal_date)
    candidates = exact_ttm_evidence() + [exact_ttm_growth_evidence()]
    eligible = []
    for evidence in candidates:
        available_date = pd.Timestamp(evidence["available_date"])
        age_days = (as_of - available_date).days
        if 0 <= age_days <= maximum_age_days:
            eligible.append((
                pd.Timestamp(evidence["fiscal_end"]),
                available_date,
                evidence,
                age_days,
            ))
    if not eligible:
        return {
            "ticker": TICKER,
            "signal_date": as_of.strftime("%Y-%m-%d"),
            "maximum_age_days": int(maximum_age_days),
            "resolved": False,
            "decision": "no_exact_pit_evidence",
        }

    _, _, selected, age_days = max(
        eligible, key=lambda item: (item[0], item[1])
    )
    result = deepcopy(selected)
    result.update({
        "signal_date": as_of.strftime("%Y-%m-%d"),
        "maximum_age_days": int(maximum_age_days),
        "financial_age_days": age_days,
        "resolved": True,
        "decision": (
            "known_nonpositive_profit"
            if selected["net_income_ttm"] <= 0
            else "usable_exact_ttm_growth"
        ),
    })
    return result


def resolve_audit_observations(
    observations: Iterable[tuple[str, str, int]] = AUDIT_OBSERVATIONS,
) -> pd.DataFrame:
    """Resolve the 19 IMAB gaps independently of the shared audit entrypoint."""
    rows = []
    for scenario, signal_date, maximum_age_days in observations:
        row = resolve_observation(signal_date, maximum_age_days)
        row["scenario"] = scenario
        rows.append(row)
    return pd.DataFrame(rows)


def build(output_dir: Path = OUTPUT_DIR) -> dict:
    """Write only research-only supplemental evidence and a blocked manifest."""
    validate_source_lock()
    _, source_provenance, operand_verification = prepare_verified_sources(
        output_dir
    )
    facts = direct_ttm_facts()
    growth = exact_ttm_growth_evidence()
    resolutions = resolve_audit_observations()
    if len(resolutions) != 19 or not resolutions["resolved"].all():
        raise RuntimeError("IMAB evidence did not resolve all 19 audited gaps")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    growth_path = output_dir / "exact_ttm_growth_evidence.json"
    resolution_path = output_dir / "audit_observation_resolution.json"
    facts.to_csv(facts_path, index=False)
    growth_path.write_text(
        json.dumps(growth, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    resolution_path.write_text(
        json.dumps(
            resolutions.to_dict("records"), indent=2, sort_keys=True
        ) + "\n",
        encoding="utf-8",
    )

    counts = resolutions["decision"].value_counts().to_dict()
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": TICKER,
        "cik": CIK,
        "currency": CURRENCY,
        "accounting_standard": ACCOUNTING_STANDARD,
        "accepted_exact_ttm_fact_count": len(facts),
        "accepted_exact_ttm_loss_count": int(
            facts.loc[facts["metric"].eq("net_income_ttm"), "value"].le(0).sum()
        ),
        "accepted_exact_annual_positive_state_count": int(
            facts.loc[facts["metric"].eq("net_income_ttm"), "value"].gt(0).sum()
        ),
        "accepted_direct_growth_metric_count": int(
            facts.loc[
                facts["fiscal_end"].eq(GROWTH_SPEC["fiscal_end"])
            ].shape[0]
        ),
        "accepted_exact_growth_count": 1,
        "resolved_audit_observation_count": len(resolutions),
        "resolution_counts": counts,
        "source_documents": source_provenance,
        "operand_verification": operand_verification,
        "rejected_later_filings": REJECTED_LATER_FILINGS,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256(facts_path),
            },
            "exact_ttm_growth_evidence": {
                "path": str(growth_path),
                "sha256": _sha256(growth_path),
            },
            "audit_observation_resolution": {
                "path": str(resolution_path),
                "sha256": _sha256(resolution_path),
            },
        },
        "shared_audit_compatibility": {
            "direct_ttm_loss": "compatible_with_quarterly_profit_ttm_snapshot",
            "exact_ttm_growth": "compatible_with_quarterly_growth_snapshot",
        },
        "guardrail": (
            "Uses only exact annual or FY-minus-prior-cumulative-plus-current-"
            "cumulative arithmetic in RMB under US-GAAP. It creates no Q1/Q2 "
            "facts, no synthetic eight-quarter history, and newer H1-derived "
            "negative TTM evidence supersedes older annual positive growth."
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
