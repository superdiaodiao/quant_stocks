#!/usr/bin/env python3
"""Build a source-locked, research-only exact annual-TTM package for OPRA.

Opera's 2021-02-25 6-K attached an unaudited FY2020 results release one day
before the three 2021-02-26 audit observations.  The release reports complete
FY2020 and FY2019 revenue and net income in USD thousands under IFRS, so it can
support an exact direct-TTM growth package without inventing fiscal quarters.

The later 2021-06-11 audited 20-F is downloaded only to prove and isolate the
subsequent FY2020 revisions.  Its revised values never enter the PIT facts.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup
from datetime import date
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/opra_exact_annual_growth")
TICKER = "OPRA"
CIK = 1_737_450
CURRENCY = "USD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "IFRS as issued by IASB"
FISCAL_END = "2020-12-31"
AVAILABLE_DATE = "2021-02-25"
PIT_CUTOFF = "2021-02-26"
FETCHED_AT = "2026-08-23"
OPERAND_ACCESSION = "0001437749-21-004012"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}


SOURCE_DOCUMENTS = {
    "6k_2021_02_25_cover": {
        "role": "availability_proof",
        "eligible_as_operand": True,
        "form": "6-K",
        "filed": AVAILABLE_DATE,
        "accepted_at": "2021-02-25T12:09:46Z",
        "accession": OPERAND_ACCESSION,
        "document": "opra20210224_6k.htm",
        "local_path": "sources/opra20210224_6k.htm",
        "expected_sha256": (
            "5e46ef6f1cdece3d27a1cd274e8bf495fc22d093c654448f178ea332e6db0499"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1737450/"
            "000143774921004012/opra20210224_6k.htm"
        ),
    },
    "6k_2021_02_25_exhibit_99_1": {
        "role": "operand_source",
        "eligible_as_operand": True,
        "form": "6-K Exhibit 99.1",
        "filed": AVAILABLE_DATE,
        "accepted_at": "2021-02-25T12:09:46Z",
        "accession": OPERAND_ACCESSION,
        "document": "ex_229746.htm",
        "local_path": "sources/ex_229746.htm",
        "expected_sha256": (
            "b2eab71d97a727596d84e496f151d096ff2379cf735807f5edd30e3759ce5d8b"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1737450/"
            "000143774921004012/ex_229746.htm"
        ),
    },
    "20f_2021_06_11_later_revision_reference": {
        "role": "rejected_later_revision_reference",
        "eligible_as_operand": False,
        "form": "20-F",
        "filed": "2021-06-11",
        "accepted_at": "2021-06-10T21:53:05Z",
        "accession": "0001437749-21-014514",
        "document": "opra20201231_20f.htm",
        "local_path": "sources/opra20201231_20f.htm",
        "expected_sha256": (
            "32dbddba2dc2ea06737196d918598568c210b9b1fe195840d53020431c5b149c"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1737450/"
            "000143774921014514/opra20201231_20f.htm"
        ),
    },
}


SOURCE_ROW_CHECKS = {
    "6k_2021_02_25_exhibit_99_1": (
        {
            "metric": "revenue",
            "line_item": "Revenue",
            "periods": ("Q4 2019", "Q4 2020", "FY2019", "FY2020"),
            "expected_values": (48_763, 50_446, 177_078, 165_274),
        },
        {
            "metric": "net_income",
            "line_item": "Net income (loss)",
            "periods": ("Q4 2019", "Q4 2020", "FY2019", "FY2020"),
            "expected_values": (21_973, 25_404, 57_899, 176_052),
        },
    ),
    "20f_2021_06_11_later_revision_reference": (
        {
            "metric": "revenue",
            "line_item": "Revenue",
            "periods": ("FY2018", "FY2019", "FY2020 revised"),
            "expected_values": (161_334, 177_078, 165_056),
        },
        {
            "metric": "net_income",
            "line_item": "Net income",
            "periods": ("FY2018", "FY2019", "FY2020 revised"),
            "expected_values": (35_160, 57_899, 179_174),
        },
    ),
}


SOURCE_TEXT_CHECKS = {
    "6k_2021_02_25_cover": (
        "On February 25, 2021, the registrant announced its unaudited financial results",
        "filed herewith as Exhibit 99.1",
    ),
    "6k_2021_02_25_exhibit_99_1": (
        "Opera Limited announces fourth quarter 2020 financial results",
        "announced its unaudited consolidated financial results",
        "Twelve Months Ended December 31",
        "US$ thousands, except for margins and per ADS amounts",
        "prepared and presented based on IFRS",
    ),
    "20f_2021_06_11_later_revision_reference": (
        "For the fiscal year ended December 31, 2020",
        "International Financial Reporting Standards as issued by the International Accounting Standards Board",
    ),
}


OPERANDS_USD_THOUSANDS = {
    "revenue": {"prior_fy2019": 177_078, "current_fy2020": 165_274},
    "net_income": {"prior_fy2019": 57_899, "current_fy2020": 176_052},
}


LATER_AUDITED_VALUES_USD_THOUSANDS = {
    "revenue": {"prior_fy2019": 177_078, "current_fy2020": 165_056},
    "net_income": {"prior_fy2019": 57_899, "current_fy2020": 179_174},
}


AUDIT_OBSERVATIONS = tuple(
    (f"liq2000000-age{age}-growth", "2021-02-26", age)
    for age in (150, 365, 550)
)


REJECTED_LATER_FILINGS = {
    "0001437749-21-014514": {
        "form": "20-F",
        "filed": "2021-06-11",
        "fiscal_end": FISCAL_END,
        "reason": (
            "audited FY2020 values were filed after the signal; used only to "
            "document later revisions and never as PIT operands"
        ),
    },
    "0001437749-21-015737": {
        "form": "20-F/A",
        "filed": "2021-06-28",
        "fiscal_end": FISCAL_END,
        "reason": "filed after the signal and excluded from all PIT operands",
    },
}


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalize_text(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split())


def _numeric_cells(row: Iterable[object]) -> list[int]:
    values: list[int] = []
    for cell in row:
        text = _normalize_text(cell)
        if not text or text.lower() == "nan" or text in {"$", ")"}:
            continue
        negative = text.startswith("(") or text.endswith(")")
        cleaned = re.sub(r"[^0-9.-]", "", text)
        if not cleaned or cleaned in {"-", "."}:
            continue
        try:
            number = float(cleaned)
        except ValueError:
            continue
        if not number.is_integer():
            continue
        value = int(number)
        values.append(-abs(value) if negative else value)
    return values


def _contains_subsequence(values: list[int], expected: list[int]) -> bool:
    width = len(expected)
    return any(values[i : i + width] == expected for i in range(len(values) - width + 1))


def _parse_checked_rows(raw: bytes, source_id: str) -> list[dict[str, object]]:
    checks = SOURCE_ROW_CHECKS.get(source_id, ())
    if not checks:
        return []
    tables = pd.read_html(BytesIO(raw))
    verified: list[dict[str, object]] = []
    for check in checks:
        expected = list(check["expected_values"])
        matched = False
        for table in tables:
            for _, row in table.iterrows():
                if _normalize_text(row.iloc[0]).lower() != str(
                    check["line_item"]
                ).lower():
                    continue
                if _contains_subsequence(_numeric_cells(row.iloc[1:]), expected):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            raise RuntimeError(
                f"source row changed: {source_id} {check['line_item']} "
                f"expected {check['expected_values']}"
            )
        verified.append(
            {
                "source_id": source_id,
                "metric": check["metric"],
                "line_item": check["line_item"],
                "periods": list(check["periods"]),
                "values": expected,
                "currency": CURRENCY,
                "scale": SOURCE_SCALE,
                "eligible_as_operand": bool(
                    SOURCE_DOCUMENTS[source_id]["eligible_as_operand"]
                ),
            }
        )
    return verified


def validate_source_lock(
    sources: dict[str, dict[str, object]] | None = None,
) -> None:
    sources = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in sources.items():
        eligible = bool(source["eligible_as_operand"])
        if eligible and source["filed"] > PIT_CUTOFF:
            raise ValueError(f"{source_id} violates PIT cutoff {PIT_CUTOFF}")
        if not eligible and source["filed"] <= PIT_CUTOFF:
            raise ValueError(f"{source_id} is not a later revision reference")
        if not str(source["url"]).startswith(
            "https://www.sec.gov/Archives/edgar/data/1737450/"
        ):
            raise ValueError(f"{source_id} is not an official SEC archive URL")
        if not re.fullmatch(r"[0-9a-f]{64}", str(source["expected_sha256"])):
            raise ValueError(f"{source_id} lacks a valid SHA-256 lock")


def verify_sources(
    output_dir: Path,
) -> tuple[dict[str, dict[str, object]], list[dict[str, object]], list[dict[str, str]]]:
    validate_source_lock()
    manifest_sources: dict[str, dict[str, object]] = {}
    verified_rows: list[dict[str, object]] = []
    verified_fragments: list[dict[str, str]] = []
    for source_id, source in SOURCE_DOCUMENTS.items():
        local_path = output_dir / str(source["local_path"])
        downloaded = False
        if not local_path.exists():
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(_download_source(str(source["url"])))
            downloaded = True
        raw = local_path.read_bytes()
        actual_sha = _sha256_bytes(raw)
        if actual_sha != source["expected_sha256"]:
            raise RuntimeError(
                f"SHA-256 mismatch for {source_id}: {actual_sha} != "
                f"{source['expected_sha256']}"
            )
        verified_rows.extend(_parse_checked_rows(raw, source_id))
        normalized = _normalize_text(
            BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
        )
        for fragment in SOURCE_TEXT_CHECKS[source_id]:
            if _normalize_text(fragment).lower() not in normalized.lower():
                raise RuntimeError(f"source text changed: {source_id} {fragment}")
            verified_fragments.append({"source_id": source_id, "fragment": fragment})
        manifest_sources[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha,
            "bytes": len(raw),
            "downloaded": downloaded,
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "accounting_standard": ACCOUNTING_STANDARD,
        }
    return manifest_sources, verified_rows, verified_fragments


def _growth(current: int, prior: int) -> float:
    if prior == 0:
        raise ValueError("cannot calculate growth from a zero prior TTM")
    return (current - prior) / abs(prior)


def exact_ttm_evidence() -> dict[str, object]:
    derived: dict[str, dict[str, object]] = {}
    later_revision: dict[str, dict[str, object]] = {}
    for metric, operands in OPERANDS_USD_THOUSANDS.items():
        prior = operands["prior_fy2019"]
        current = operands["current_fy2020"]
        later_current = LATER_AUDITED_VALUES_USD_THOUSANDS[metric]["current_fy2020"]
        derived[metric] = {
            "prior_ttm_usd_thousands": prior,
            "current_ttm_usd_thousands": current,
            "growth": _growth(current, prior),
            "formula": "FY2020 / FY2019 exact full-year comparison",
        }
        later_revision[metric] = {
            "prior_fy2019_usd_thousands": (
                LATER_AUDITED_VALUES_USD_THOUSANDS[metric]["prior_fy2019"]
            ),
            "current_fy2020_usd_thousands": later_current,
            "change_from_pit_operand_usd_thousands": later_current - current,
            "eligible_as_operand": False,
        }
    return {
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "accepted_at": "2021-02-25T12:09:46Z",
        "signal_date": PIT_CUTOFF,
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "source_status": "unaudited consolidated FY2020 results filed on Form 6-K",
        "metric_mapping": {
            "revenue": "Revenue / ifrs-full:Revenue",
            "net_income": "Net income (loss) / ifrs-full:ProfitLoss",
        },
        "operands_usd_thousands": OPERANDS_USD_THOUSANDS,
        "derived": derived,
        "operand_accession": OPERAND_ACCESSION,
        "later_audited_revision": {
            "accession": "0001437749-21-014514",
            "filed": "2021-06-11",
            "used_in_pit_facts": False,
            "values": later_revision,
        },
        "restatement_isolation": (
            "The later audited 20-F retained both FY2019 comparatives but revised "
            "FY2020 revenue from 165274 to 165056 and net income from 176052 to "
            "179174 (USD thousands). Those later values are verification-only and "
            "are not backfilled into the 2021-02-26 snapshot."
        ),
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = exact_ttm_evidence()
    concepts = {"revenue": "Revenue", "net_income": "ProfitLoss"}
    records = []
    for metric, values in evidence["derived"].items():
        for output_metric, value in (
            (f"{metric}_ttm", values["current_ttm_usd_thousands"] * SOURCE_SCALE),
            (f"{metric}_growth", values["growth"]),
        ):
            records.append(
                {
                    "ticker": TICKER,
                    "fiscal_end": FISCAL_END,
                    "available_date": AVAILABLE_DATE,
                    "metric": output_metric,
                    "value": value,
                    "taxonomy": "ifrs-full",
                    "concept": (
                        f"opra_direct_annual_ttm:{concepts[metric]}:{CURRENCY}"
                    ),
                    "form": "6-K_DIRECT_ANNUAL_TTM",
                    "accession": OPERAND_ACCESSION,
                    "fetched_at": FETCHED_AT,
                }
            )
    return (
        pd.DataFrame(records, columns=OUTPUT_COLUMNS)
        .sort_values("metric")
        .reset_index(drop=True)
    )


def resolve_audit_observations() -> pd.DataFrame:
    evidence = exact_ttm_evidence()["derived"]
    age_days = (
        date.fromisoformat(PIT_CUTOFF) - date.fromisoformat(AVAILABLE_DATE)
    ).days
    rows = []
    for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        rows.append(
            {
                "scenario": scenario,
                "signal_date": signal_date,
                "maximum_age_days": maximum_age_days,
                "resolved": age_days <= maximum_age_days,
                "decision": "complete_exact_direct_annual_ttm_growth_bundle",
                "fiscal_end": FISCAL_END,
                "available_date": AVAILABLE_DATE,
                "financial_age_days": age_days,
                "revenue_ttm": (
                    evidence["revenue"]["current_ttm_usd_thousands"] * SOURCE_SCALE
                ),
                "revenue_growth": evidence["revenue"]["growth"],
                "net_income_ttm": (
                    evidence["net_income"]["current_ttm_usd_thousands"]
                    * SOURCE_SCALE
                ),
                "net_income_growth": evidence["net_income"]["growth"],
                "currency": CURRENCY,
            }
        )
    return pd.DataFrame(rows)


def validate_exact_package() -> None:
    evidence = exact_ttm_evidence()
    facts = strict_quarterly_facts()
    if evidence["derived"]["revenue"]["current_ttm_usd_thousands"] != 165_274:
        raise RuntimeError("OPRA PIT revenue TTM changed")
    if evidence["derived"]["net_income"]["current_ttm_usd_thousands"] != 176_052:
        raise RuntimeError("OPRA PIT net-income TTM changed")
    if set(facts["metric"]) != {
        "revenue_ttm",
        "revenue_growth",
        "net_income_ttm",
        "net_income_growth",
    }:
        raise RuntimeError("OPRA direct annual growth package is incomplete")
    if set(facts["accession"]) != {OPERAND_ACCESSION}:
        raise RuntimeError("a later filing entered the OPRA PIT facts")


def build(output_dir: Path = OUTPUT_DIR) -> dict[str, object]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sources, verified_rows, verified_fragments = verify_sources(output_dir)
    validate_exact_package()
    facts = strict_quarterly_facts()
    evidence = exact_ttm_evidence()
    resolution = resolve_audit_observations()

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_evidence.json"
    resolution_path = output_dir / "audit_observation_resolution.json"
    manifest_path = output_dir / "manifest.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")
    resolution_path.write_text(
        json.dumps(resolution.to_dict(orient="records"), indent=2) + "\n"
    )
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": TICKER,
        "cik": CIK,
        "pit_cutoff": PIT_CUTOFF,
        "currency": CURRENCY,
        "accounting_standard": ACCOUNTING_STANDARD,
        "accepted_direct_growth_package_count": 1,
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": int(resolution["resolved"].sum()),
        "resolved_unique_signal_date_count": int(resolution["signal_date"].nunique()),
        "source_operand_verification_count": sum(
            len(row["values"])
            for row in verified_rows
            if row["eligible_as_operand"]
        ),
        "later_revision_value_verification_count": sum(
            len(row["values"])
            for row in verified_rows
            if not row["eligible_as_operand"]
        ),
        "source_text_verification_count": len(verified_fragments),
        "sources": sources,
        "source_value_verification": {
            "rows": verified_rows,
            "fragments": verified_fragments,
        },
        "rejected_later_filings": REJECTED_LATER_FILINGS,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256_path(facts_path),
            },
            "exact_ttm_evidence": {
                "path": str(evidence_path),
                "sha256": _sha256_path(evidence_path),
            },
            "audit_observation_resolution": {
                "path": str(resolution_path),
                "sha256": _sha256_path(resolution_path),
            },
        },
        "guardrail": (
            "Only complete FY2020/FY2019 USD-thousand IFRS amounts in the "
            "2021-02-25 6-K exhibit enter the direct TTM package. The later audited "
            "20-F revisions are verified and explicitly excluded; no quarter, "
            "currency conversion, later value, or formal financial fact is made up."
        ),
    }
    manifest_path.write_text(json.dumps(report, indent=2) + "\n")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(build(args.output_dir), indent=2))


if __name__ == "__main__":
    main()
