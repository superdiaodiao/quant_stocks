#!/usr/bin/env python3
"""Build source-locked evidence for MOMO's unrecoverable 2019-11-29 gaps.

Momo changed its reporting currency from USD to RMB effective in 2018Q4.  The
2019Q3 release and 2018 20-F support an exact, positive current TTM in RMB, but
the pre-change 2018Q3 release reports 9M2017 only in USD.  The 2018 20-F recasts
annual comparatives into RMB but does not disclose a recast RMB 9M2017 value.
Consequently an exact same-currency prior TTM and TTM growth bundle cannot be
constructed as of 2019-11-29.  This module emits no invented quarter, currency
conversion, growth rate, or negative-profit fact.
"""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/momo_pit_unrecoverable")
TICKER = "MOMO"
CIK = 1_610_601
PIT_CUTOFF = "2019-11-29"
LATEST_PIT_FILING_DATE = "2019-11-26"
LATEST_FISCAL_END = "2019-09-30"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}


SOURCE_DOCUMENTS = {
    "20f_2018_04_26_fy2017_usd": {
        "role": "pre_change_usd_basis",
        "form": "20-F",
        "filed": "2018-04-26",
        "accession": "0001193125-18-133102",
        "document": "d504595d20f.htm",
        "local_path": "sources/d504595d20f.htm",
        "expected_sha256": (
            "def42a4533bbb49860346ac25f28000aed598a40bf183e0b9162a43ac1dcbcb5"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1610601/"
            "000119312518133102/d504595d20f.htm"
        ),
        "accounting_standard": "US-GAAP",
        "currency": "USD",
        "scale": 1_000,
    },
    "6k_2018_12_06_q3_usd_ex991": {
        "role": "pre_change_usd_basis",
        "form": "6-K/EX-99.1",
        "filed": "2018-12-06",
        "accession": "0001193125-18-343009",
        "document": "d626983dex991.htm",
        "local_path": "sources/d626983dex991.htm",
        "expected_sha256": (
            "427ba04fc9bfd0abc47d41c0fa4da822918fa0023aaa467c53715e3d6e0ac122"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1610601/"
            "000119312518343009/d626983dex991.htm"
        ),
        "accounting_standard": "US-GAAP unaudited",
        "currency": "USD",
        "scale": 1_000,
    },
    "20f_2019_04_26_fy2018_rmb_recast": {
        "role": "post_change_rmb_basis",
        "form": "20-F",
        "filed": "2019-04-26",
        "accession": "0001193125-19-120962",
        "document": "d682290d20f.htm",
        "local_path": "sources/d682290d20f.htm",
        "expected_sha256": (
            "8901bc6fe4779c9d98fc7b88d7567fcb1b7d39440e3a02669b3e75c85d274b59"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1610601/"
            "000119312519120962/d682290d20f.htm"
        ),
        "accounting_standard": "US-GAAP",
        "currency": "RMB",
        "scale": 1_000,
    },
    "6k_2019_11_26_q3_rmb_ex991": {
        "role": "latest_pit_rmb_basis",
        "form": "6-K/EX-99.1",
        "filed": "2019-11-26",
        "accession": "0001193125-19-300514",
        "document": "d828376dex991.htm",
        "local_path": "sources/d828376dex991.htm",
        "expected_sha256": (
            "426a38c0153aef97270e89e8da85e3cecc3333a383d002151affb2be3fc6274e"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1610601/"
            "000119312519300514/d828376dex991.htm"
        ),
        "accounting_standard": "US-GAAP unaudited",
        "currency": "RMB",
        "scale": 1_000,
    },
}


SOURCE_ROW_CHECKS = {
    "20f_2018_04_26_fy2017_usd": (
        {
            "metric": "revenue",
            "line_item": "Net revenues",
            "periods": ("FY2015", "FY2016", "FY2017"),
            "expected_values": (133_988, 553_098, 1_318_271),
        },
        {
            "metric": "net_income_attributable",
            "line_item": "Net income attributable to Momo Inc.",
            "periods": ("FY2015", "FY2016", "FY2017"),
            "expected_values": (13_697, 145_250, 318_566),
        },
    ),
    "6k_2018_12_06_q3_usd_ex991": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": ("Q3 2017", "Q3 2018", "9M 2017", "9M 2018"),
            "expected_values": (354_453, 535_970, 931_915, 1_465_369),
        },
        {
            "metric": "net_income_attributable",
            "line_item": "Net income attributable to Momo Inc.",
            "periods": ("Q3 2017", "Q3 2018", "9M 2017", "9M 2018"),
            "expected_values": (79_089, 85_156, 221_059, 332_819),
        },
    ),
    "20f_2019_04_26_fy2018_rmb_recast": (
        {
            "metric": "revenue",
            "line_item": "Net revenues",
            "periods": ("FY2016 RMB", "FY2017 RMB recast", "FY2018 RMB"),
            "expected_values": (3_707_358, 8_886_390, 13_408_421),
        },
        {
            "metric": "net_income_attributable",
            "line_item": "Net income attributable to Momo Inc.",
            "periods": ("FY2016 RMB", "FY2017 RMB recast", "FY2018 RMB"),
            "expected_values": (978_969, 2_148_098, 2_815_775),
        },
    ),
    "6k_2019_11_26_q3_rmb_ex991": (
        {
            "metric": "revenue",
            "line_item": "Total net revenues",
            "periods": (
                "Q3 2018 RMB",
                "Q3 2019 RMB",
                "Q3 2019 USD convenience",
                "9M 2018 RMB",
                "9M 2019 RMB",
                "9M 2019 USD convenience",
            ),
            "expected_values": (
                3_647_597,
                4_451_642,
                622_808,
                9_564_504,
                12_327_191,
                1_724_637,
            ),
        },
        {
            "metric": "net_income_attributable",
            "line_item": "Net income attributable to Momo Inc.",
            "periods": (
                "Q3 2018 RMB",
                "Q3 2019 RMB",
                "Q3 2019 USD convenience",
                "9M 2018 RMB",
                "9M 2019 RMB",
                "9M 2019 USD convenience",
            ),
            "expected_values": (
                579_539,
                893_897,
                125_062,
                2_154_938,
                1_914_991,
                267_916,
            ),
        },
    ),
}


SOURCE_TEXT_CHECKS = {
    "20f_2018_04_26_fy2017_usd": (
        "Our reporting and functional currency is U.S. dollar",
    ),
    "6k_2018_12_06_q3_usd_ex991": (
        "US dollars in thousands, except per share data",
    ),
    "20f_2019_04_26_fy2018_rmb_recast": (
        "changed our reporting currency from U.S. dollar to RMB",
        "financial statements prior to the current period have been recast",
        "Current period amounts in this annual report are translated into U.S. dollars for the convenience of the readers",
    ),
    "6k_2019_11_26_q3_rmb_ex991": (
        "translations of certain Renminbi amounts into U.S. dollars",
        "solely for the convenience of readers",
        "RMB7.1477 to US$1.00",
    ),
}


AUDIT_OBSERVATIONS = tuple(
    (f"liq{liquidity}-age{age}-growth", "2019-11-29", age)
    for liquidity in (2_000_000, 10_000_000)
    for age in (150, 365, 550)
)


REJECTED_LATER_FILINGS = {
    "0001193125-20-079851": {
        "form": "6-K",
        "filed": "2020-03-20",
        "reason": "Q4/FY2019 results were filed after the 2019-11-29 signal date",
    },
    "0001193125-20-123373": {
        "form": "20-F",
        "filed": "2020-04-28",
        "reason": "FY2019 annual report was filed after the signal date",
    },
}


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _download_source(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalize_text(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split())


def _numeric_cells(row: Iterable[object]) -> list[int]:
    values: list[int] = []
    for cell in row:
        text = _normalize_text(cell)
        if not text or text.lower() == "nan" or text in {"$", "RMB", "US$", ")"}:
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
        matches: list[list[int]] = []
        for table in tables:
            for _, row in table.iterrows():
                if _normalize_text(row.iloc[0]) != check["line_item"]:
                    continue
                numeric = _numeric_cells(row.iloc[1:])
                expected = list(check["expected_values"])
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
            }
        )
    return verified


def validate_source_lock(sources: dict[str, dict[str, object]] = SOURCE_DOCUMENTS) -> None:
    for source_id, source in sources.items():
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"{source_id} violates PIT cutoff {PIT_CUTOFF}")
        if not str(source["url"]).startswith(
            "https://www.sec.gov/Archives/edgar/data/1610601/"
        ):
            raise ValueError(f"{source_id} is not an official SEC archive URL")
        if not re.fullmatch(r"[0-9a-f]{64}", str(source["expected_sha256"])):
            raise ValueError(f"{source_id} lacks a valid SHA-256 lock")


def verify_sources(output_dir: Path) -> tuple[dict[str, dict[str, object]], list[dict[str, object]], list[dict[str, str]]]:
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
            re.sub(r"<[^>]+>", " ", raw.decode("utf-8", errors="replace"))
        )
        for fragment in SOURCE_TEXT_CHECKS.get(source_id, ()):
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
        }
    return manifest_sources, verified_rows, verified_fragments


def rejected_derivations() -> dict[str, object]:
    current_revenue = 13_408_421 - 9_564_504 + 12_327_191
    current_profit = 2_815_775 - 2_154_938 + 1_914_991
    prior_revenue = 1_318_271 - 931_915 + 1_465_369
    prior_profit = 318_566 - 221_059 + 332_819
    return {
        "current_ttm_rmb_thousands": {
            "fiscal_end": "2019-09-30",
            "available_date": "2019-11-26",
            "currency": "RMB",
            "revenue_ttm": current_revenue,
            "net_income_attributable_ttm": current_profit,
            "formula": "FY2018_RMB - 9M2018_RMB + 9M2019_RMB",
            "source_accessions": [
                "0001193125-19-120962",
                "0001193125-19-300514",
            ],
        },
        "prior_ttm_usd_thousands": {
            "fiscal_end": "2018-09-30",
            "available_date": "2018-12-06",
            "currency": "USD",
            "revenue_ttm": prior_revenue,
            "net_income_attributable_ttm": prior_profit,
            "formula": "FY2017_USD - 9M2017_USD + 9M2018_USD",
            "source_accessions": [
                "0001193125-18-133102",
                "0001193125-18-343009",
            ],
        },
        "rejection": {
            "rejected": True,
            "reason_code": "REPORTING_CURRENCY_CHANGE_MISSING_RECAST_INTERIM",
            "reason": (
                "current and prior TTM values are on RMB and USD reporting bases; "
                "the official PIT sources do not disclose recast RMB 9M2017 or a "
                "complete same-basis bridge, so exact TTM growth is unavailable"
            ),
            "positive_current_profit": current_profit > 0,
            "negative_ttm_exclusion_available": False,
            "forbidden_shortcuts": [
                "divide RMB current TTM by USD prior TTM",
                "translate the prior TTM with a spot or annual-average rate",
                "treat annual growth as TTM growth",
                "use the 2020-03-20 or 2020-04-28 later filings",
            ],
        },
    }


def validate_unrecoverable_conclusion() -> None:
    evidence = rejected_derivations()
    current = evidence["current_ttm_rmb_thousands"]
    prior = evidence["prior_ttm_usd_thousands"]
    rejection = evidence["rejection"]
    if current["currency"] == prior["currency"]:
        raise RuntimeError("currency-basis break unexpectedly disappeared")
    if current["net_income_attributable_ttm"] <= 0:
        raise RuntimeError("current TTM is no longer positive")
    if not rejection["rejected"] or rejection["negative_ttm_exclusion_available"]:
        raise RuntimeError("unrecoverable conclusion changed")


def strict_quarterly_facts() -> pd.DataFrame:
    validate_unrecoverable_conclusion()
    return pd.DataFrame(columns=OUTPUT_COLUMNS)


def resolve_audit_observations() -> pd.DataFrame:
    rows = []
    for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        rows.append(
            {
                "scenario": scenario,
                "signal_date": signal_date,
                "maximum_age_days": maximum_age_days,
                "resolved": False,
                "decision": "unrecoverable_currency_basis_break",
                "latest_fiscal_end": LATEST_FISCAL_END,
                "latest_available_date": LATEST_PIT_FILING_DATE,
                "financial_age_days": 3,
                "reason_code": "REPORTING_CURRENCY_CHANGE_MISSING_RECAST_INTERIM",
            }
        )
    return pd.DataFrame(rows)


def build(output_dir: Path = OUTPUT_DIR) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    sources, verified_rows, verified_fragments = verify_sources(output_dir)
    facts = strict_quarterly_facts()
    resolution = resolve_audit_observations()
    derivations = rejected_derivations()

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "rejected_derivations.json"
    resolution_path = output_dir / "audit_observation_resolution.json"
    manifest_path = output_dir / "manifest.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(json.dumps(derivations, indent=2) + "\n")
    resolution_path.write_text(
        json.dumps(resolution.to_dict(orient="records"), indent=2) + "\n"
    )
    manifest = {
        "research_only": True,
        "ticker": TICKER,
        "cik": CIK,
        "pit_cutoff": PIT_CUTOFF,
        "latest_pit_filing_date": LATEST_PIT_FILING_DATE,
        "accepted_strict_fact_count": len(facts),
        "resolved_audit_observation_count": int(resolution["resolved"].sum()),
        "unrecoverable_audit_observation_count": int((~resolution["resolved"]).sum()),
        "unrecoverable_unique_signal_date_count": int(resolution["signal_date"].nunique()),
        "source_operand_verification_count": sum(len(x["values"]) for x in verified_rows),
        "source_text_verification_count": len(verified_fragments),
        "point_in_time_proven": True,
        "promotion_eligible": False,
        "release_status": "BLOCKED",
        "conclusion": (
            "no complete same-currency TTM growth bundle and no exact negative TTM "
            "were available by 2019-11-29"
        ),
        "sources": sources,
        "source_value_verification": {
            "rows": verified_rows,
            "fragments": verified_fragments,
        },
        "rejected_later_filings": REJECTED_LATER_FILINGS,
        "outputs": {
            "strict_quarterly_facts": str(facts_path),
            "rejected_derivations": str(evidence_path),
            "audit_observation_resolution": str(resolution_path),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(build(args.output_dir), indent=2))


if __name__ == "__main__":
    main()
