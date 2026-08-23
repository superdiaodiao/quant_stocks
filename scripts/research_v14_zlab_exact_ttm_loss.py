#!/usr/bin/env python3
"""Build source-locked, research-only exact-TTM loss evidence for ZLAB.

Zai Lab was a foreign private issuer during the audited 2019-2020 signal
period, so its contemporaneous interim financial statements arrived on 6-K
rather than 10-Q.  This supplement retains the reported annual, six-month,
and nine-month periods as operands and emits only direct ``net_income_ttm``
loss states.  It never manufactures a quarter or a growth observation.
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


OUTPUT_DIR = Path("output/research_only/v14/zlab_exact_ttm_loss")
TICKER = "ZLAB"
CIK = 1_704_292
CURRENCY = "USD"
ACCOUNTING_STANDARD = "US-GAAP"
PIT_CUTOFF = "2020-12-31"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

# Every operand must parse from one of these exact official SEC files and the
# downloaded bytes must match the locked digest.  Accession prefixes identify
# filing agents, not a change in Zai Lab's issuer CIK.
SOURCE_DOCUMENTS = {
    "6k_2019_03_07_fy2018_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2019-03-07",
        "accession": "0001564590-19-006625",
        "document": "zlab-ex991_6.htm",
        "local_path": "sources/zlab_2018_fy_ex991.htm",
        "expected_sha256": (
            "963f0dd0bf72452375726f8830677082d0dcf0511b3bd891f3d978cc2065c264"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1704292/"
            "000156459019006625/zlab-ex991_6.htm"
        ),
        "currency": CURRENCY,
        "scale": 1,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2019_09_03_h1_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2019-09-03",
        "accession": "0001564590-19-033837",
        "document": "zlab-ex991_6.htm",
        "local_path": "sources/zlab_2019_h1_ex991.htm",
        "expected_sha256": (
            "49356f8166c3843bba86cff297cb88b6497e4e254cc84b4cc831b91ae1efb185"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1704292/"
            "000156459019033837/zlab-ex991_6.htm"
        ),
        "currency": CURRENCY,
        "scale": 1,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2020_01_21_9m_r4": {
        "form": "6-K/R4",
        "filed": "2020-01-21",
        "accession": "0001564590-20-001532",
        "document": "R4.htm",
        "local_path": "sources/zlab_2019_9m_R4.htm",
        "expected_sha256": (
            "841aef0f3977cbc5a67b77c004a2cf847ee245766e68e547f7bafbecb0aef32b"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1704292/"
            "000156459020001532/R4.htm"
        ),
        "currency": CURRENCY,
        "scale": 1,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "20f_2020_04_29_fy2019_r4": {
        "form": "20-F/R4",
        "filed": "2020-04-29",
        "accession": "0001564590-20-019745",
        "document": "R4.htm",
        "local_path": "sources/zlab_2019_fy_R4.htm",
        "expected_sha256": (
            "410c487a2825af93cda1cf4f2992ba403aa939f6b5f14b0c197b5c2301994747"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1704292/"
            "000156459020019745/R4.htm"
        ),
        "currency": CURRENCY,
        "scale": 1_000,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
    "6k_2020_08_13_h1_ex991": {
        "form": "6-K/EX-99.1",
        "filed": "2020-08-13",
        "accession": "0001564590-20-039799",
        "document": "zlab-ex991_21.htm",
        "local_path": "sources/zlab_2020_h1_ex991.htm",
        "expected_sha256": (
            "a6d89b1eaf2c11b82ef6975b9088067db1c4a049c6283963475df280c92d6232"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1704292/"
            "000156459020039799/zlab-ex991_21.htm"
        ),
        "currency": CURRENCY,
        "scale": 1_000,
        "accounting_standard": ACCOUNTING_STANDARD,
    },
}
ALLOWED_SOURCE_ACCESSIONS = {
    source["accession"] for source in SOURCE_DOCUMENTS.values()
}
REJECTED_LATER_FILINGS = {
    "0001193125-21-062279": {
        "form": "10-K",
        "filed": "2021-03-01",
        "reason": "filed after all audited 2019-2020 signal dates",
    },
    "0001193125-21-155749": {
        "form": "10-Q",
        "filed": "2021-05-10",
        "reason": "first domestic 10-Q was not contemporaneous to the gaps",
    },
}

# Values remain in each cited table's presentation units.  Each TTM formula
# below is scale-homogeneous; exact-dollar and USD-thousand sources are never
# mixed in the same arithmetic expression.
OPERANDS = {
    "fy2018_net_loss": {
        "source_id": "6k_2019_03_07_fy2018_ex991",
        "period": "FY2018",
        "table_column": "FY2018",
        "line_item": "Net loss",
        "value": -139_074_895,
    },
    "h1_2019_net_loss": {
        "source_id": "6k_2019_09_03_h1_ex991",
        "period": "H1 2019",
        "table_column": "H1_2019",
        "line_item": "Net loss",
        "value": -83_273_723,
    },
    "h1_2018_net_loss": {
        "source_id": "6k_2019_09_03_h1_ex991",
        "period": "H1 2018",
        "table_column": "H1_2018",
        "line_item": "Net loss",
        "value": -41_490_428,
    },
    "m9_2019_net_loss": {
        "source_id": "6k_2020_01_21_9m_r4",
        "period": "9M 2019",
        "table_column": "9M_2019",
        "line_item": "Net loss",
        "value": -148_640_670,
    },
    "m9_2018_net_loss": {
        "source_id": "6k_2020_01_21_9m_r4",
        "period": "9M 2018",
        "table_column": "9M_2018",
        "line_item": "Net loss",
        "value": -75_717_598,
    },
    "fy2019_net_loss_thousands": {
        "source_id": "20f_2020_04_29_fy2019_r4",
        "period": "FY2019",
        "table_column": "FY2019",
        "line_item": "Net loss",
        "value": -195_071,
    },
    "h1_2020_net_loss_thousands": {
        "source_id": "6k_2020_08_13_h1_ex991",
        "period": "H1 2020",
        "table_column": "H1_2020",
        "line_item": "Net loss",
        "value": -128_617,
    },
    "h1_2019_net_loss_thousands": {
        "source_id": "6k_2020_08_13_h1_ex991",
        "period": "H1 2019",
        "table_column": "H1_2019",
        "line_item": "Net loss",
        "value": -83_274,
    },
}

SOURCE_PARSE_SPECS = {
    "6k_2019_03_07_fy2018_ex991": {
        "context_phrases": ("Year ended December 31", "2017", "2018"),
        "columns": {"FY2017": "FY2017", "FY2018": "FY2018"},
    },
    "6k_2019_09_03_h1_ex991": {
        "context_phrases": (
            "For the six months ended June 30",
            "2019",
            "2018",
        ),
        "columns": {"H1_2019": "H1 2019", "H1_2018": "H1 2018"},
    },
    "6k_2020_01_21_9m_r4": {
        "context_phrases": (
            "Unaudited Condensed Consolidated Statements of Operations",
            "USD",
            "9 Months Ended",
        ),
        "columns": {"9M_2019": "9M 2019", "9M_2018": "9M 2018"},
    },
    "20f_2020_04_29_fy2019_r4": {
        "context_phrases": (
            "Consolidated Statements of Operations",
            "USD",
            "in Thousands",
            "12 Months Ended",
        ),
        "columns": {
            "FY2019": "FY2019",
            "FY2018": "FY2018",
            "FY2017": "FY2017",
        },
    },
    # The issuer's table presents the comparative 2019 column before 2020.
    "6k_2020_08_13_h1_ex991": {
        "context_phrases": (
            "For the six months ended June 30",
            "2019",
            "2020",
        ),
        "columns": {"H1_2019": "H1 2019", "H1_2020": "H1 2020"},
    },
}

TTM_SPECS = {
    "2019-06-30": {
        "available_date": "2019-09-03",
        "formula": "FY2018 - H1_2018 + H1_2019",
        "terms": (
            (1, "fy2018_net_loss"),
            (-1, "h1_2018_net_loss"),
            (1, "h1_2019_net_loss"),
        ),
        "source_scale": 1,
        "expected_value": -180_858_190,
        "form": "6-K_ANNUAL_PLUS_H1_CUMULATIVE_TTM",
    },
    "2019-09-30": {
        "available_date": "2020-01-21",
        "formula": "FY2018 - 9M_2018 + 9M_2019",
        "terms": (
            (1, "fy2018_net_loss"),
            (-1, "m9_2018_net_loss"),
            (1, "m9_2019_net_loss"),
        ),
        "source_scale": 1,
        "expected_value": -211_997_967,
        "form": "6-K_ANNUAL_PLUS_9M_CUMULATIVE_TTM",
    },
    "2020-06-30": {
        "available_date": "2020-08-13",
        "formula": "FY2019 - H1_2019 + H1_2020",
        "terms": (
            (1, "fy2019_net_loss_thousands"),
            (-1, "h1_2019_net_loss_thousands"),
            (1, "h1_2020_net_loss_thousands"),
        ),
        "source_scale": 1_000,
        "expected_value": -240_414,
        "form": "20-F_PLUS_6-K_H1_CUMULATIVE_TTM",
    },
}

# These dates were established by a read-only delta against
# step_dkng_imab_audit_v2.  There are nine unique missing dates and thirteen
# scenario observations; age-365/550 scenarios already had annual loss state.
AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", "2019-09-30", 150),
    ("liq2000000-age150-growth", "2019-10-31", 150),
    ("liq2000000-age150-growth", "2019-11-29", 150),
    ("liq2000000-age150-growth", "2019-12-31", 150),
    ("liq2000000-age150-growth", "2020-01-31", 150),
    ("liq2000000-age150-growth", "2020-02-28", 150),
    ("liq10000000-age150-growth", "2020-02-28", 150),
    ("liq2000000-age150-growth", "2020-09-30", 150),
    ("liq10000000-age150-growth", "2020-09-30", 150),
    ("liq2000000-age150-growth", "2020-11-30", 150),
    ("liq10000000-age150-growth", "2020-11-30", 150),
    ("liq2000000-age150-growth", "2020-12-31", 150),
    ("liq10000000-age150-growth", "2020-12-31", 150),
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
            cells = row.find_all(("td", "th"))
            labels = [
                _normalize_text(" ".join(cell.stripped_strings))
                for cell in cells
            ]
            first_label = next((item for item in labels if item), "")
            if first_label != "net loss":
                continue
            values = _row_numbers(row)
            if len(values) == expected_count:
                candidates.append(dict(zip(spec["columns"], values, strict=True)))
    if not candidates:
        raise RuntimeError(f"no unambiguous ZLAB Net loss table for {source_id}")
    canonical = json.dumps(candidates[0], sort_keys=True)
    if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
        raise RuntimeError(f"conflicting ZLAB Net loss tables for {source_id}")
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
        if source["currency"] != CURRENCY:
            raise ValueError(f"source {source_id} is not USD")
        if source["accounting_standard"] != ACCOUNTING_STANDARD:
            raise ValueError(f"source {source_id} is not US-GAAP")
        if source["scale"] not in (1, 1_000):
            raise ValueError(f"source {source_id} has unsupported scale")
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
    for operand_id, operand in OPERANDS.items():
        if operand["source_id"] not in documents:
            raise ValueError(f"operand {operand_id} has no locked source")


def verify_operand_sources(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw source set does not match the source lock")
    parsed = {
        source_id: _parse_source_table(source_id, raw)
        for source_id, raw in raw_by_source.items()
    }
    verified = []
    for operand_id, operand in OPERANDS.items():
        source_id = operand["source_id"]
        source = SOURCE_DOCUMENTS[source_id]
        column = operand["table_column"]
        if column not in SOURCE_PARSE_SPECS[source_id]["columns"]:
            raise RuntimeError(f"operand {operand_id} has no parsed period")
        if SOURCE_PARSE_SPECS[source_id]["columns"][column] != operand["period"]:
            raise RuntimeError(f"operand {operand_id} period mapping changed")
        if _normalize_text(operand["line_item"]) != "net loss":
            raise RuntimeError(f"operand {operand_id} line item mapping changed")
        parsed_value = parsed[source_id][column]
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
            "currency": source["currency"],
            "scale": source["scale"],
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
                f"ZLAB source SHA-256 mismatch for {source_id}: {actual_sha256}"
            )
        raw_by_source[source_id] = raw
        provenance[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha256,
            "bytes": len(raw),
            "downloaded": downloaded,
        }
    return raw_by_source, provenance, verify_operand_sources(raw_by_source)


def _source_ids_for_terms(terms: Iterable[tuple[int, str]]) -> list[str]:
    return list(dict.fromkeys(
        OPERANDS[operand_id]["source_id"] for _, operand_id in terms
    ))


def exact_ttm_evidence() -> list[dict]:
    validate_source_lock()
    rows = []
    for fiscal_end, spec in TTM_SPECS.items():
        scales = {
            SOURCE_DOCUMENTS[OPERANDS[operand_id]["source_id"]]["scale"]
            for _, operand_id in spec["terms"]
        }
        if scales != {spec["source_scale"]}:
            raise RuntimeError(f"ZLAB mixed-scale TTM formula for {fiscal_end}")
        source_value = sum(
            coefficient * int(OPERANDS[operand_id]["value"])
            for coefficient, operand_id in spec["terms"]
        )
        if source_value != spec["expected_value"]:
            raise RuntimeError(f"ZLAB exact TTM changed for {fiscal_end}")
        actual_value = source_value * spec["source_scale"]
        if actual_value > 0:
            raise RuntimeError("direct exact-TTM layer is exclusion-only")
        source_ids = _source_ids_for_terms(spec["terms"])
        sources = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
        if spec["available_date"] != max(source["filed"] for source in sources):
            raise RuntimeError(f"ZLAB availability date changed for {fiscal_end}")
        rows.append({
            "ticker": TICKER,
            "evidence_kind": "exact_cumulative_ttm_loss",
            "fiscal_end": fiscal_end,
            "available_date": spec["available_date"],
            "currency": CURRENCY,
            "source_scale": spec["source_scale"],
            "accounting_standard": ACCOUNTING_STANDARD,
            "net_income_ttm": actual_value,
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
    rows = []
    for evidence in exact_ttm_evidence():
        rows.append({
            "ticker": TICKER,
            "fiscal_end": evidence["fiscal_end"],
            "available_date": evidence["available_date"],
            "metric": "net_income_ttm",
            "value": evidence["net_income_ttm"],
            "taxonomy": "us-gaap",
            "concept": "zlab_exact_ttm:NetIncomeLoss:USD",
            "form": evidence["form"],
            "accession": "+".join(evidence["source_accessions"]),
            "fetched_at": fetched_at,
        })
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "available_date"]
    ).reset_index(drop=True)


def resolve_observation(
    signal_date: str,
    maximum_age_days: int,
) -> dict:
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
    rows = []
    for scenario, signal_date, maximum_age_days in observations:
        rows.append({
            "scenario": scenario,
            "signal_date": signal_date,
            "maximum_age_days": maximum_age_days,
            **resolve_observation(signal_date, maximum_age_days),
        })
    return pd.DataFrame(rows)


def build(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _, provenance, operand_verification = prepare_verified_sources(output_dir)
    evidence = exact_ttm_evidence()
    facts = direct_ttm_facts()
    resolutions = resolve_audit_observations()
    if not resolutions["resolved"].all():
        raise RuntimeError("not every declared ZLAB audit observation resolved")

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
        "reporting_profile": "FOREIGN_PRIVATE_ISSUER_20-F_6-K_THROUGH_2020",
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_unique_signal_date_count": resolutions["signal_date"].nunique(),
        "resolved_audit_observation_count": len(resolutions),
        "source_documents": provenance,
        "operand_verification": operand_verification,
        "later_filing_rejections": REJECTED_LATER_FILINGS,
        "revenue_assessment": {
            "direct_growth_emitted": False,
            "reason": (
                "Contemporaneous statements disclose revenue, including zero or "
                "blank 2018 comparatives, but do not prove a complete same-scale "
                "current/prior TTM growth package for every gap. Negative exact "
                "TTM profit is sufficient for candidate exclusion."
            ),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256(facts_path),
            },
            "exact_ttm_evidence": {
                "path": str(evidence_path),
                "sha256": _sha256(evidence_path),
            },
            "audit_observation_resolution": {
                "path": str(resolution_path),
                "sha256": _sha256(resolution_path),
            },
        },
        "guardrail": (
            "Uses only exact annual-minus-prior-cumulative-plus-current-"
            "cumulative loss arithmetic from filings available by each signal. "
            "No quarter, growth metric, later 10-K/10-Q fact, formal financial "
            "edit, or shared-candidate integration is produced."
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
