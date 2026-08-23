#!/usr/bin/env python3
"""Build a source-locked, research-only exact-TTM growth package for GAIN.

Gladstone Investment has a March fiscal year-end.  The 2019-11-04 fiscal-Q2
10-Q supplies six-month cumulative values and comparatives, so current and
prior TTM values can be derived from complete reported periods without making
up fiscal quarters.  Total investment income is the top line for this BDC and
the net-income measure is net increase in net assets resulting from operations.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/gain_exact_ttm_growth")
TICKER = "GAIN"
CIK = 1_321_741
CURRENCY = "USD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP / ASC 946"
FISCAL_END = "2019-09-30"
AVAILABLE_DATE = "2019-11-04"
PIT_CUTOFF = "2019-12-31"
FETCHED_AT = "2026-08-23"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}


SOURCE_DOCUMENTS = {
    "10k_2018_05_15_fy2018_corroboration": {
        "role": "same_basis_corroboration",
        "form": "10-K",
        "filed": "2018-05-15",
        "accession": "0001193125-18-164066",
        "document": "d588089d10k.htm",
        "local_path": "sources/d588089d10k.htm",
        "expected_sha256": (
            "8a84eb856a87b19696e003dfa0c7b814e35258c01d265199fdf4fbdc9beb7807"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1321741/"
            "000119312518164066/d588089d10k.htm"
        ),
    },
    "10q_2018_11_05_q2fy2019": {
        "role": "operand_source",
        "form": "10-Q",
        "filed": "2018-11-05",
        "accession": "0001193125-18-318357",
        "document": "d649122d10q.htm",
        "local_path": "sources/d649122d10q.htm",
        "expected_sha256": (
            "3c02638efd0076903404f83c69a0390bd7c2d32a3946b366c6cc2fb07aefd9d1"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1321741/"
            "000119312518318357/d649122d10q.htm"
        ),
    },
    "10k_2019_05_13_fy2019": {
        "role": "operand_source",
        "form": "10-K",
        "filed": "2019-05-13",
        "accession": "0001193125-19-145332",
        "document": "d741282d10k.htm",
        "local_path": "sources/d741282d10k.htm",
        "expected_sha256": (
            "3789285de4d84ebdf6131829b99ddc951c6995763b142c23eef07db4aa1ca777"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1321741/"
            "000119312519145332/d741282d10k.htm"
        ),
    },
    "10q_2019_11_04_q2fy2020": {
        "role": "latest_operand_source",
        "form": "10-Q",
        "filed": AVAILABLE_DATE,
        "accession": "0001193125-19-283342",
        "document": "d796659d10q.htm",
        "local_path": "sources/d796659d10q.htm",
        "expected_sha256": (
            "53587f610d58a5401165a46350dcba8f0b290ab9ef56fcd415cb91f7a84271f9"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1321741/"
            "000119312519283342/d796659d10q.htm"
        ),
    },
}


SOURCE_ROW_CHECKS = {
    "10k_2018_05_15_fy2018_corroboration": (
        {
            "metric": "revenue",
            "line_item": "Total investment income",
            "periods": ("FY2018", "FY2017", "FY2016"),
            "expected_values": (58_355, 51_875, 50_955),
        },
        {
            "metric": "net_income",
            "line_item": "NET INCREASE IN NET ASSETS RESULTING FROM OPERATIONS",
            "periods": ("FY2018", "FY2017", "FY2016"),
            "expected_values": (60_687, 44_763, 24_854),
        },
    ),
    "10q_2018_11_05_q2fy2019": (
        {
            "metric": "revenue",
            "line_item": "Total investment income",
            "periods": ("FQ2 2019", "FQ2 2018", "H1 FY2019", "H1 FY2018"),
            "expected_values": (13_091, 13_132, 28_595, 26_752),
        },
        {
            "metric": "net_income",
            "line_item": "NET INCREASE IN NET ASSETS RESULTING FROM OPERATIONS",
            "periods": ("FQ2 2019", "FQ2 2018", "H1 FY2019", "H1 FY2018"),
            "expected_values": (30_364, 13_556, 62_691, 21_697),
        },
    ),
    "10k_2019_05_13_fy2019": (
        {
            "metric": "revenue",
            "line_item": "Total investment income",
            "periods": ("FY2019", "FY2018", "FY2017", "FY2016", "FY2015"),
            "expected_values": (59_663, 58_355, 51_875, 50_955, 41_643),
        },
        {
            "metric": "net_income",
            "line_item": "Net increase in net assets resulting from operations",
            "periods": ("FY2019", "FY2018", "FY2017", "FY2016", "FY2015"),
            "expected_values": (81_590, 60_687, 44_763, 24_854, 50_214),
        },
    ),
    "10q_2019_11_04_q2fy2020": (
        {
            "metric": "revenue",
            "line_item": "Total investment income",
            "periods": ("FQ2 2020", "FQ2 2019", "H1 FY2020", "H1 FY2019"),
            "expected_values": (16_636, 13_091, 33_946, 28_595),
        },
        {
            "metric": "net_income",
            "line_item": "NET INCREASE IN NET ASSETS RESULTING FROM OPERATIONS",
            "periods": ("FQ2 2020", "FQ2 2019", "H1 FY2020", "H1 FY2019"),
            "expected_values": (11_004, 30_364, 17_050, 62_691),
        },
    ),
}


SOURCE_TEXT_CHECKS = {
    "10k_2018_05_15_fy2018_corroboration": (
        "DOLLAR AMOUNTS IN THOUSANDS",
    ),
    "10q_2018_11_05_q2fy2019": (
        "For the quarterly period ended September 30, 2018",
        "three and six months ended September 30, 2018 and 2017",
        "UNAUDITED",
    ),
    "10k_2019_05_13_fy2019": (
        "business development company",
        "Accounting Standards Codification",
        "Topic 946",
        "DOLLAR AMOUNTS IN THOUSANDS",
    ),
    "10q_2019_11_04_q2fy2020": (
        "For the quarterly period ended September 30, 2019",
        "three and six months ended September 30, 2019 and 2018",
        "UNAUDITED",
    ),
}


OPERANDS_USD_THOUSANDS = {
    "revenue": {
        "fy2018": 58_355,
        "h1_fy2018": 26_752,
        "h1_fy2019": 28_595,
        "fy2019": 59_663,
        "h1_fy2020": 33_946,
    },
    "net_income": {
        "fy2018": 60_687,
        "h1_fy2018": 21_697,
        "h1_fy2019": 62_691,
        "fy2019": 81_590,
        "h1_fy2020": 17_050,
    },
}


AUDIT_OBSERVATIONS = tuple(
    (f"liq2000000-age{age}-growth", "2019-12-31", age)
    for age in (150, 365, 550)
)


REJECTED_LATER_FILINGS = {
    "0001193125-20-024218": {
        "form": "10-Q",
        "filed": "2020-02-04",
        "fiscal_end": "2019-12-31",
        "reason": "filed after the 2019-12-31 signal date",
    },
    "0001193125-20-140388": {
        "form": "10-K",
        "filed": "2020-05-12",
        "fiscal_end": "2020-03-31",
        "reason": "filed after the signal date and not used to revise PIT operands",
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


def _parse_checked_rows(raw: bytes, source_id: str) -> list[dict[str, object]]:
    tables = pd.read_html(BytesIO(raw))
    verified: list[dict[str, object]] = []
    for check in SOURCE_ROW_CHECKS[source_id]:
        expected = list(check["expected_values"])
        matches = []
        for table in tables:
            for _, row in table.iterrows():
                if _normalize_text(row.iloc[0]).lower() != str(
                    check["line_item"]
                ).lower():
                    continue
                numeric = _numeric_cells(row.iloc[1:])
                if len(numeric) >= len(expected) and numeric[: len(expected)] == expected:
                    matches.append(expected)
        if not matches:
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
                "values": matches[0],
                "currency": CURRENCY,
                "scale": SOURCE_SCALE,
            }
        )
    return verified


def validate_source_lock(
    sources: dict[str, dict[str, object]] | None = None,
) -> None:
    sources = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in sources.items():
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"{source_id} violates PIT cutoff {PIT_CUTOFF}")
        if not str(source["url"]).startswith(
            "https://www.sec.gov/Archives/edgar/data/1321741/"
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
            verified_fragments.append(
                {"source_id": source_id, "fragment": fragment}
            )
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
    for metric, operands in OPERANDS_USD_THOUSANDS.items():
        prior_ttm = (
            operands["fy2018"]
            - operands["h1_fy2018"]
            + operands["h1_fy2019"]
        )
        current_ttm = (
            operands["fy2019"]
            - operands["h1_fy2019"]
            + operands["h1_fy2020"]
        )
        derived[metric] = {
            "prior_ttm_usd_thousands": prior_ttm,
            "current_ttm_usd_thousands": current_ttm,
            "growth": _growth(current_ttm, prior_ttm),
            "prior_formula": "FY2018 - H1_FY2018 + H1_FY2019",
            "current_formula": "FY2019 - H1_FY2019 + H1_FY2020",
        }
    return {
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "fiscal_calendar": "March 31 year-end; H1 ends September 30",
        "metric_mapping": {
            "revenue": "Total investment income / us-gaap:GrossInvestmentIncomeOperating",
            "net_income": (
                "Net increase in net assets resulting from operations / "
                "us-gaap:NetIncomeLoss"
            ),
        },
        "operands_usd_thousands": OPERANDS_USD_THOUSANDS,
        "derived": derived,
        "operand_accessions": [
            "0001193125-18-318357",
            "0001193125-19-145332",
            "0001193125-19-283342",
        ],
        "restatement_isolation": (
            "FY2018 values in the 2019 10-K match the 2018 10-K, and H1 FY2019 "
            "values in the 2019Q2 10-Q match the original 2018Q2 10-Q; no amended "
            "filing is used"
        ),
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = exact_ttm_evidence()
    accession = "+".join(evidence["operand_accessions"])
    records = []
    concepts = {
        "revenue": "GrossInvestmentIncomeOperating",
        "net_income": "NetIncomeLoss",
    }
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
                    "taxonomy": "us-gaap",
                    "concept": (
                        f"gain_exact_h1_ttm:{concepts[metric]}:{CURRENCY}"
                    ),
                    "form": "10-K_PLUS_10-Q_H1_CUMULATIVE_TTM",
                    "accession": accession,
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
    rows = []
    for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        rows.append(
            {
                "scenario": scenario,
                "signal_date": signal_date,
                "maximum_age_days": maximum_age_days,
                "resolved": True,
                "decision": "complete_exact_ttm_growth_bundle",
                "fiscal_end": FISCAL_END,
                "available_date": AVAILABLE_DATE,
                "financial_age_days": 57,
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
    evidence = exact_ttm_evidence()["derived"]
    facts = strict_quarterly_facts()
    if evidence["revenue"]["current_ttm_usd_thousands"] != 65_014:
        raise RuntimeError("GAIN current revenue TTM changed")
    if evidence["net_income"]["current_ttm_usd_thousands"] != 35_949:
        raise RuntimeError("GAIN current net-income TTM changed")
    if set(facts["metric"]) != {
        "revenue_ttm",
        "revenue_growth",
        "net_income_ttm",
        "net_income_growth",
    }:
        raise RuntimeError("GAIN direct growth package is incomplete")


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
            len(row["values"]) for row in verified_rows
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
            "All operands are reported USD-thousand US-GAAP/ASC-946 annual or "
            "six-month cumulative amounts available by 2019-11-04. No fiscal "
            "quarter, currency conversion, later filing, or formal financial fact "
            "is manufactured."
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
