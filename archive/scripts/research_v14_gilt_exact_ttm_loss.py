#!/usr/bin/env python3
"""Build source-locked exact negative-TTM evidence for GILT at 2021-01-29.

The 2020Q3 6-K supplies reported nine-month 2020 and 2019 net income (loss).
Combining those cumulative periods with audited FY2019 produces a negative
TTM on one USD/US-GAAP/consolidated basis.  This is exclusion-only evidence:
no quarter, revenue, or growth observation is manufactured.
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


OUTPUT_DIR = Path("output/research_only/v14/gilt_exact_ttm_loss")
TICKER = "GILT"
CIK = 897_322
CURRENCY = "USD"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP"
FISCAL_END = "2020-09-30"
AVAILABLE_DATE = "2020-11-10"
PIT_CUTOFF = "2021-01-29"
FETCHED_AT = "2026-08-23"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}


SOURCE_DOCUMENTS = {
    "6k_2019_11_19_q3_original": {
        "role": "same_basis_comparative_corroboration",
        "form": "6-K",
        "filed": "2019-11-19",
        "accession": "0001178913-19-002787",
        "document": "zk1923673.htm",
        "local_path": "sources/zk1923673.htm",
        "expected_sha256": (
            "1e4118d527d40ed0b6d2c3a30823aa5105d91f38f64b2cda8436e1baf4d998dc"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/897322/"
            "000117891319002787/zk1923673.htm"
        ),
    },
    "20f_2020_03_23_fy2019": {
        "role": "operand_source",
        "form": "20-F",
        "filed": "2020-03-23",
        "accession": "0001178913-20-000895",
        "document": "zk2024178.htm",
        "local_path": "sources/zk2024178.htm",
        "expected_sha256": (
            "6cc2ef1426a5c736c019cfa7735f791b22c7958e3650cd095485df85d47f770c"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/897322/"
            "000117891320000895/zk2024178.htm"
        ),
    },
    "6k_2020_11_10_q3": {
        "role": "latest_operand_source",
        "form": "6-K",
        "filed": AVAILABLE_DATE,
        "accession": "0001178913-20-003069",
        "document": "zk2025113.htm",
        "local_path": "sources/zk2025113.htm",
        "expected_sha256": (
            "caa3f865da85864982ae9c9fd0d3ce4f173f86dd4ea384e4ce1dbb856f49871f"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/897322/"
            "000117891320003069/zk2025113.htm"
        ),
    },
}


SOURCE_ROW_CHECKS = {
    "6k_2019_11_19_q3_original": (
        {
            "metric": "revenue",
            "line_item": "Revenues",
            "periods": ("9M 2019", "9M 2018", "Q3 2019", "Q3 2018"),
            "expected_values": (185_178, 196_662, 63_384, 62_780),
        },
        {
            "metric": "net_income",
            "line_item": "Net income",
            "periods": ("9M 2019", "9M 2018", "Q3 2019", "Q3 2018"),
            "expected_values": (12_517, 13_114, 6_288, 8_652),
        },
    ),
    "20f_2020_03_23_fy2019": (
        {
            "metric": "revenue",
            "line_item": "Total revenues",
            "periods": ("FY2019", "FY2018", "FY2017"),
            "expected_values": (263_492, 266_391, 282_756),
        },
        {
            "metric": "net_income",
            "line_item": "Net income",
            "periods": ("FY2019", "FY2018", "FY2017"),
            "expected_values": (36_538, 18_409, 6_801),
        },
    ),
    "6k_2020_11_10_q3": (
        {
            "metric": "revenue",
            "line_item": "Revenues",
            "periods": ("9M 2020", "9M 2019", "Q3 2020", "Q3 2019"),
            "expected_values": (123_258, 185_178, 37_270, 63_384),
        },
        {
            "metric": "net_income",
            "line_item": "Net income (loss)",
            "periods": ("9M 2020", "9M 2019", "Q3 2020", "Q3 2019"),
            "expected_values": (-27_531, 12_517, -11_551, 6_288),
        },
    ),
}


SOURCE_TEXT_CHECKS = {
    "6k_2019_11_19_q3_original": (
        "Third Quarter 2019 results",
        "U.S. dollars in thousands",
        "Nine months ended",
        "Unaudited",
    ),
    "20f_2020_03_23_fy2019": (
        "For the fiscal year ended December 31, 2019",
        "financial statements have been prepared in accordance with U.S. GAAP",
        "U.S. dollars in thousands",
    ),
    "6k_2020_11_10_q3": (
        "Third Quarter 2020 Results",
        "U.S. dollars in thousands",
        "Nine months ended",
        "Unaudited",
    ),
}


OPERANDS_USD_THOUSANDS = {
    "fy2019_net_income": {
        "source_id": "20f_2020_03_23_fy2019",
        "period": "FY2019",
        "line_item": "Net income",
        "value": 36_538,
    },
    "m9_2019_net_income": {
        "source_id": "6k_2020_11_10_q3",
        "period": "9M 2019",
        "line_item": "Net income (loss)",
        "value": 12_517,
    },
    "m9_2020_net_loss": {
        "source_id": "6k_2020_11_10_q3",
        "period": "9M 2020",
        "line_item": "Net income (loss)",
        "value": -27_531,
    },
}


AUDIT_OBSERVATIONS = tuple(
    (f"liq2000000-age{age}-growth", "2021-01-29", age)
    for age in (150, 365, 550)
)


REJECTED_LATER_FILINGS = {
    "0001178913-21-000586": {
        "form": "6-K",
        "filed": "2021-02-16",
        "description": "Fourth Quarter and Full Year 2020 Results",
        "reason": "filed after the 2021-01-29 signal date",
    },
    "0001178913-21-000937": {
        "form": "20-F",
        "filed": "2021-03-08",
        "fiscal_end": "2020-12-31",
        "reason": "later annual report cannot backfill the signal date",
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
            "https://www.sec.gov/Archives/edgar/data/897322/"
        ):
            raise ValueError(f"{source_id} is not an official SEC archive URL")
        accession_path = str(source["accession"]).replace("-", "")
        if accession_path not in str(source["url"]):
            raise ValueError(f"{source_id} URL does not lock accession")
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


def exact_ttm_evidence() -> dict[str, object]:
    fy2019 = OPERANDS_USD_THOUSANDS["fy2019_net_income"]["value"]
    m9_2019 = OPERANDS_USD_THOUSANDS["m9_2019_net_income"]["value"]
    m9_2020 = OPERANDS_USD_THOUSANDS["m9_2020_net_loss"]["value"]
    net_income_ttm = (fy2019 - m9_2019 + m9_2020) * SOURCE_SCALE
    if net_income_ttm != -3_510_000:
        raise RuntimeError(f"unexpected GILT TTM value: {net_income_ttm}")
    return {
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "currency": CURRENCY,
        "scale": SOURCE_SCALE,
        "accounting_standard": ACCOUNTING_STANDARD,
        "profit_scope": "consolidated Net income (loss)",
        "formula": "FY2019 - 9M_2019 + 9M_2020",
        "operands_usd_thousands": OPERANDS_USD_THOUSANDS,
        "net_income_ttm": net_income_ttm,
        "operand_accessions": [
            "0001178913-20-000895",
            "0001178913-20-003069",
        ],
        "restatement_isolation": (
            "9M2019 net income 12,517 and revenue 185,178 in the original "
            "2019Q3 6-K exactly match the 2020Q3 comparative columns; no amended "
            "or later financial result is used"
        ),
    }


def direct_ttm_facts(fetched_at: str = FETCHED_AT) -> pd.DataFrame:
    evidence = exact_ttm_evidence()
    return pd.DataFrame(
        [
            {
                "ticker": TICKER,
                "fiscal_end": FISCAL_END,
                "available_date": AVAILABLE_DATE,
                "metric": "net_income_ttm",
                "value": evidence["net_income_ttm"],
                "taxonomy": "us-gaap",
                "concept": "gilt_exact_m9_ttm:NetIncomeLoss:USD",
                "form": "20-F_PLUS_6-K_9M_CUMULATIVE_TTM",
                "accession": "+".join(evidence["operand_accessions"]),
                "fetched_at": fetched_at,
            }
        ],
        columns=OUTPUT_COLUMNS,
    )


def resolve_audit_observations() -> pd.DataFrame:
    net_income_ttm = exact_ttm_evidence()["net_income_ttm"]
    return pd.DataFrame(
        [
            {
                "scenario": scenario,
                "signal_date": signal_date,
                "maximum_age_days": maximum_age_days,
                "resolved": True,
                "decision": "known_nonpositive_profit",
                "fiscal_end": FISCAL_END,
                "available_date": AVAILABLE_DATE,
                "financial_age_days": 80,
                "net_income_ttm": net_income_ttm,
                "currency": CURRENCY,
            }
            for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS
        ]
    )


def build(output_dir: Path = OUTPUT_DIR) -> dict[str, object]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sources, verified_rows, verified_fragments = verify_sources(output_dir)
    facts = direct_ttm_facts()
    evidence = exact_ttm_evidence()
    resolution = resolve_audit_observations()

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_loss_evidence.json"
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
        "accepted_exact_ttm_loss_count": 1,
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
            "exact_ttm_loss_evidence": {
                "path": str(evidence_path),
                "sha256": _sha256_path(evidence_path),
            },
            "audit_observation_resolution": {
                "path": str(resolution_path),
                "sha256": _sha256_path(resolution_path),
            },
        },
        "guardrail": (
            "The supplement emits only a reported-period-derived negative "
            "net_income_ttm. It creates no quarter, revenue, growth rate, currency "
            "conversion, or later-filed backfill."
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
