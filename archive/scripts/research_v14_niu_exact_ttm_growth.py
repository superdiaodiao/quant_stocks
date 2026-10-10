#!/usr/bin/env python3
"""Build source-locked, research-only exact TTM growth packages for NIU.

The two packages use only reported RMB/CNY U.S.-GAAP amounts from NIU's
FY2019 20-F and contemporaneous Q1/Q2 6-K exhibits.  The exhibits also display
US-dollar convenience translations and non-GAAP measures; both are explicitly
recorded as rejected evidence and never enter the TTM arithmetic.
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


OUTPUT_DIR = Path("output/research_only/v14/niu_exact_ttm_growth")
TICKER = "NIU"
CIK = 1_744_781
CURRENCY = "CNY"
SOURCE_CURRENCY_LABEL = "RMB"
SOURCE_SCALE = 1
ACCOUNTING_STANDARD = "US-GAAP"
PIT_CUTOFF = "2020-08-31"
FETCHED_AT = "2026-08-24"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}


SOURCE_DOCUMENTS = {
    "20f_fy2019": {
        "form": "20-F",
        "filed": "2020-04-24",
        "accepted_at": "2020-04-24T10:48:03Z",
        "accession": "0001104659-20-050585",
        "document": "a19-28358_120f.htm",
        "local_path": "sources/a19-28358_120f.htm",
        "expected_sha256": "3cb326a8c9779bfda64834d5514235c9100841ebb4ded3e31a53adfcadecbded",
        "url": "https://www.sec.gov/Archives/edgar/data/1744781/000110465920050585/a19-28358_120f.htm",
    },
    "6k_q1_2019": {
        "form": "6-K Exhibit 99.1",
        "filed": "2019-05-14",
        "accepted_at": "2019-05-14T10:08:20Z",
        "accession": "0001104659-19-029094",
        "document": "a19-9864_1ex99d1.htm",
        "local_path": "sources/a19-9864_1ex99d1.htm",
        "expected_sha256": "a3e5034eafffc537d86b570c28e1a681d6375e58c5cf1d66463346da6b68c35c",
        "url": "https://www.sec.gov/Archives/edgar/data/1744781/000110465919029094/a19-9864_1ex99d1.htm",
    },
    "6k_q2_2019": {
        "form": "6-K Exhibit 99.1",
        "filed": "2019-08-23",
        "accepted_at": "2019-08-23T12:21:01Z",
        "accession": "0001104659-19-047035",
        "document": "a19-17693_1ex99d1.htm",
        "local_path": "sources/a19-17693_1ex99d1.htm",
        "expected_sha256": "f60eab42fffc08c16308366de854fe8d66463d15e258995f5cbb37bf28bd36b2",
        "url": "https://www.sec.gov/Archives/edgar/data/1744781/000110465919047035/a19-17693_1ex99d1.htm",
    },
    "6k_q1_2020": {
        "form": "6-K Exhibit 99.1",
        "filed": "2020-05-19",
        "accepted_at": "2020-05-19T10:30:04Z",
        "accession": "0001104659-20-063446",
        "document": "a20-20058_1ex99d1.htm",
        "local_path": "sources/a20-20058_1ex99d1.htm",
        "expected_sha256": "d420733a484841ea4c6979e7045b114f7160173def381f25a7a5a24e2d5552f7",
        "url": "https://www.sec.gov/Archives/edgar/data/1744781/000110465920063446/a20-20058_1ex99d1.htm",
    },
    "6k_q2_2020": {
        "form": "6-K Exhibit 99.1",
        "filed": "2020-08-18",
        "accepted_at": "2020-08-18T11:02:03Z",
        "accession": "0001104659-20-096304",
        "document": "a20-28772_1ex99d1.htm",
        "local_path": "sources/a20-28772_1ex99d1.htm",
        "expected_sha256": "3c43a45bbf7c67b47c6614d69738622c0863c70b0ccb0206d425fa887297bec8",
        "url": "https://www.sec.gov/Archives/edgar/data/1744781/000110465920096304/a20-28772_1ex99d1.htm",
    },
}


SOURCE_ROW_CHECKS = {
    "20f_fy2019": (
        {
            "metric": "revenue",
            "line_item": "Revenues",
            "periods": ("FY2017 RMB", "FY2018 RMB", "FY2019 RMB"),
            "operand_values": (769_368_001, 1_477_781_304, 2_076_289_101),
            "excluded_usd_translation_values": (298_240_268,),
            "expected_row_values": (769_368_001, 1_477_781_304, 2_076_289_101, 298_240_268),
        },
        {
            "metric": "net_income",
            "line_item": "Net (loss) / income",
            "periods": ("FY2017 RMB", "FY2018 RMB", "FY2019 RMB"),
            "operand_values": (-184_662_871, -349_027_476, 190_084_771),
            "excluded_usd_translation_values": (27_303_970,),
            "expected_row_values": (-184_662_871, -349_027_476, 190_084_771, 27_303_970),
        },
    ),
    "6k_q1_2019": (
        {
            "metric": "revenue",
            "line_item": "Revenues",
            "periods": ("Q1 2018 RMB", "Q1 2019 RMB"),
            "operand_values": (172_822_924, 355_219_700),
            "excluded_usd_translation_values": (52_929_387,),
            "expected_row_values": (172_822_924, 355_219_700, 52_929_387),
        },
        {
            "metric": "net_income",
            "line_item": "Net (loss)/income",
            "periods": ("Q1 2018 RMB", "Q1 2019 RMB"),
            "operand_values": (-61_882_999, 11_982_210),
            "excluded_usd_translation_values": (1_785_405,),
            "expected_row_values": (-61_882_999, 11_982_210, 1_785_405),
        },
    ),
    "6k_q2_2019": (
        {
            "metric": "revenue",
            "line_item": "Revenues",
            "periods": ("Q2 2018 RMB", "Q2 2019 RMB", "H1 2018 RMB", "H1 2019 RMB"),
            "operand_values": (384_256_352, 530_505_579, 557_079_276, 885_725_279),
            "excluded_usd_translation_values": (77_276_851, 129_020_434),
            "expected_row_values": (384_256_352, 530_505_579, 77_276_851, 557_079_276, 885_725_279, 129_020_434),
        },
        {
            "metric": "net_income",
            "line_item": "Net (loss)/income",
            "periods": ("Q2 2018 RMB", "Q2 2019 RMB", "H1 2018 RMB", "H1 2019 RMB"),
            "operand_values": (-252_986_829, 50_981_395, -314_869_828, 62_963_605),
            "excluded_usd_translation_values": (7_426_278, 9_171_684),
            "expected_row_values": (-252_986_829, 50_981_395, 7_426_278, -314_869_828, 62_963_605, 9_171_684),
        },
    ),
    "6k_q1_2020": (
        {
            "metric": "revenue",
            "line_item": "Revenues",
            "periods": ("Q1 2019 RMB", "Q1 2020 RMB"),
            "operand_values": (355_219_700, 232_940_508),
            "excluded_usd_translation_values": (32_897_484,),
            "expected_row_values": (355_219_700, 232_940_508, 32_897_484),
        },
        {
            "metric": "net_income",
            "line_item": "Net income/(loss)",
            "periods": ("Q1 2019 RMB", "Q1 2020 RMB"),
            "operand_values": (11_982_210, -26_375_926),
            "excluded_usd_translation_values": (-3_724_993,),
            "expected_row_values": (11_982_210, -26_375_926, -3_724_993),
        },
    ),
    "6k_q2_2020": (
        {
            "metric": "revenue",
            "line_item": "Revenues",
            "periods": ("Q2 2019 RMB", "Q2 2020 RMB", "H1 2019 RMB", "H1 2020 RMB"),
            "operand_values": (530_505_579, 644_934_410, 885_725_279, 877_874_918),
            "excluded_usd_translation_values": (91_284_541, 124_255_130),
            "expected_row_values": (530_505_579, 644_934_410, 91_284_541, 885_725_279, 877_874_918, 124_255_130),
        },
        {
            "metric": "net_income",
            "line_item": "Net income",
            "periods": ("Q2 2019 RMB", "Q2 2020 RMB", "H1 2019 RMB", "H1 2020 RMB"),
            "operand_values": (50_981_395, 56_826_849, 62_963_605, 30_450_923),
            "excluded_usd_translation_values": (8_043_318, 4_310_048),
            "expected_row_values": (50_981_395, 56_826_849, 8_043_318, 62_963_605, 30_450_923, 4_310_048),
        },
    ),
}


SOURCE_TEXT_CHECKS = {
    "20f_fy2019": (
        "U.S. GAAP",
        "For the Year Ended December 31",
        "US$ (Note 2(d))",
    ),
    **{
        source_id: (
            "UNAUDITED CONDENSED CONSOLIDATED",
            "RMB",
            "US$",
            "Non-GAAP",
        )
        for source_id in SOURCE_DOCUMENTS
        if source_id.startswith("6k_")
    },
}


PACKAGE_METADATA = {
    "2020-03-31": {
        "available_date": "2020-05-19",
        "period": "Q1",
        "source_ids": ("20f_fy2019", "6k_q1_2019", "6k_q1_2020"),
    },
    "2020-06-30": {
        "available_date": "2020-08-18",
        "period": "H1",
        "source_ids": ("20f_fy2019", "6k_q2_2019", "6k_q2_2020"),
    },
}


OPERANDS_CNY = {
    "2020-03-31": {
        "revenue": (2_076_289_101, 355_219_700, 232_940_508, 1_477_781_304, 172_822_924, 355_219_700),
        "net_income": (190_084_771, 11_982_210, -26_375_926, -349_027_476, -61_882_999, 11_982_210),
    },
    "2020-06-30": {
        "revenue": (2_076_289_101, 885_725_279, 877_874_918, 1_477_781_304, 557_079_276, 885_725_279),
        "net_income": (190_084_771, 62_963_605, 30_450_923, -349_027_476, -314_869_828, 62_963_605),
    },
}


AUDIT_OBSERVATIONS = tuple(
    (f"liq2000000-age{age}-growth", signal_date, age)
    for age in (150, 365)
    for signal_date in ("2020-06-30", "2020-07-31", "2020-08-31")
) + tuple(
    (f"liq10000000-age{age}-growth", signal_date, age)
    for age in (150, 365)
    for signal_date in ("2020-07-31", "2020-08-31")
)


COMPARATIVE_MATCHES = (
    ("Q1 2019 revenue", "6k_q1_2019", "6k_q1_2020", 355_219_700),
    ("Q1 2019 net income", "6k_q1_2019", "6k_q1_2020", 11_982_210),
    ("Q2 2019 revenue", "6k_q2_2019", "6k_q2_2020", 530_505_579),
    ("Q2 2019 net income", "6k_q2_2019", "6k_q2_2020", 50_981_395),
    ("H1 2019 revenue", "6k_q2_2019", "6k_q2_2020", 885_725_279),
    ("H1 2019 net income", "6k_q2_2019", "6k_q2_2020", 62_963_605),
)


ADJUSTED_OR_NON_GAAP_LABELS = (
    "Adjusted net income",
    "Non-GAAP net income",
    "Adjusted EBITDA",
)


REJECTED_LATER_FILINGS = {
    "0001104659-20-128730": {
        "form": "6-K",
        "filed": "2020-11-24",
        "reason": "Q3 results filed after every audited signal date",
    },
    "0001104659-21-048371": {
        "form": "20-F",
        "filed": "2021-04-09",
        "reason": "later FY2020 annual report; never used to revise PIT operands",
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
    return " ".join(str(value).replace("\xa0", " ").replace("\u200b", " ").split())


def _numeric_cells(row: Iterable[object]) -> list[int]:
    values = []
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
    return any(values[i:i + width] == expected for i in range(len(values) - width + 1))


def validate_candidate_operand(
    *, value: int | float, currency: str, metric_label: str, filed: str
) -> None:
    del value
    if currency.upper() != CURRENCY:
        raise ValueError("USD convenience translation is not an eligible CNY operand")
    lowered = metric_label.lower()
    if "adjusted" in lowered or "non-gaap" in lowered:
        raise ValueError("adjusted/non-GAAP metric is not an eligible GAAP operand")
    if filed > PIT_CUTOFF:
        raise ValueError(f"later filing violates PIT cutoff {PIT_CUTOFF}")


def _parse_checked_rows(raw: bytes, source_id: str) -> list[dict[str, object]]:
    tables = pd.read_html(BytesIO(raw))
    verified = []
    for check in SOURCE_ROW_CHECKS[source_id]:
        expected = list(check["expected_row_values"])
        matched = False
        for table in tables:
            for _, row in table.iterrows():
                if _normalize_text(row.iloc[0]).lower() != str(check["line_item"]).lower():
                    continue
                if _contains_subsequence(_numeric_cells(row.iloc[1:]), expected):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            raise RuntimeError(
                f"source row changed: {source_id} {check['line_item']} "
                f"expected {check['expected_row_values']}"
            )
        for value in check["operand_values"]:
            validate_candidate_operand(
                value=value,
                currency=CURRENCY,
                metric_label=str(check["line_item"]),
                filed=str(SOURCE_DOCUMENTS[source_id]["filed"]),
            )
        verified.append({
            "source_id": source_id,
            "metric": check["metric"],
            "line_item": check["line_item"],
            "periods": list(check["periods"]),
            "operand_values": list(check["operand_values"]),
            "currency": CURRENCY,
            "source_currency_label": SOURCE_CURRENCY_LABEL,
            "scale": SOURCE_SCALE,
            "excluded_usd_translation_values": list(check["excluded_usd_translation_values"]),
        })
    return verified


def validate_source_lock(sources: dict[str, dict[str, object]] | None = None) -> None:
    sources = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in sources.items():
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"{source_id} violates PIT cutoff {PIT_CUTOFF}")
        if not str(source["url"]).startswith("https://www.sec.gov/Archives/edgar/data/1744781/"):
            raise ValueError(f"{source_id} is not an official SEC archive URL")
        if not re.fullmatch(r"[0-9a-f]{64}", str(source["expected_sha256"])):
            raise ValueError(f"{source_id} lacks a valid SHA-256 lock")
    for fiscal_end, package in PACKAGE_METADATA.items():
        source_dates = [str(sources[source_id]["filed"]) for source_id in package["source_ids"]]
        if max(source_dates) != package["available_date"]:
            raise ValueError(f"{fiscal_end} available_date is not its latest source filing")


def verify_sources(output_dir: Path) -> tuple[dict, list[dict], list[dict]]:
    validate_source_lock()
    manifest_sources, verified_rows, verified_fragments = {}, [], []
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
            "source_currency_label": SOURCE_CURRENCY_LABEL,
            "scale": SOURCE_SCALE,
            "accounting_standard": ACCOUNTING_STANDARD,
        }
    return manifest_sources, verified_rows, verified_fragments


def _growth(current: int, prior: int) -> float:
    if prior == 0:
        raise ValueError("cannot calculate growth from a zero prior TTM")
    return (current - prior) / abs(prior)


def exact_ttm_evidence() -> dict[str, object]:
    packages = {}
    for fiscal_end, metrics in OPERANDS_CNY.items():
        derived = {}
        for metric, operands in metrics.items():
            current_annual, current_subtract, current_add, prior_annual, prior_subtract, prior_add = operands
            current_ttm = current_annual - current_subtract + current_add
            prior_ttm = prior_annual - prior_subtract + prior_add
            derived[metric] = {
                "prior_ttm_cny": prior_ttm,
                "current_ttm_cny": current_ttm,
                "growth": _growth(current_ttm, prior_ttm),
                "current_formula": "current annual - same-period prior YTD + current YTD",
                "prior_formula": "prior annual - same-period older YTD + prior YTD",
            }
        metadata = PACKAGE_METADATA[fiscal_end]
        packages[fiscal_end] = {
            "available_date": metadata["available_date"],
            "cumulative_period": metadata["period"],
            "currency": CURRENCY,
            "source_currency_label": SOURCE_CURRENCY_LABEL,
            "scale": SOURCE_SCALE,
            "accounting_standard": ACCOUNTING_STANDARD,
            "source_ids": list(metadata["source_ids"]),
            "source_accessions": [SOURCE_DOCUMENTS[s]["accession"] for s in metadata["source_ids"]],
            "operands_cny": metrics,
            "derived": derived,
        }
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "accounting_standard": ACCOUNTING_STANDARD,
        "packages": packages,
        "comparative_matches": [
            {"label": label, "original_source": original, "later_comparative_source": later, "value_cny": value, "matched": True}
            for label, original, later, value in COMPARATIVE_MATCHES
        ],
        "currency_isolation": (
            "Only RMB columns (represented as ISO CNY) are operands. All 14 US$ "
            "convenience-translation cells are source-verified but rejected."
        ),
        "metric_isolation": (
            "Only consolidated U.S.-GAAP Revenues and Net income/(loss) are used; "
            "adjusted and non-GAAP measures are rejected."
        ),
        "restatement_isolation": (
            "All Q1/H1 2019 comparatives match their original 2019 releases. "
            "Each package is dated to its latest operand filing; Q3 2020 and later "
            "annual filings are rejected rather than backfilled."
        ),
    }


def strict_quarterly_facts() -> pd.DataFrame:
    records = []
    concepts = {"revenue": "RevenueFromContractWithCustomerExcludingAssessedTax", "net_income": "NetIncomeLoss"}
    for fiscal_end, package in exact_ttm_evidence()["packages"].items():
        accession = "+".join(package["source_accessions"])
        for metric, values in package["derived"].items():
            for output_metric, value in (
                (f"{metric}_ttm", values["current_ttm_cny"]),
                (f"{metric}_growth", values["growth"]),
            ):
                records.append({
                    "ticker": TICKER,
                    "fiscal_end": fiscal_end,
                    "available_date": package["available_date"],
                    "metric": output_metric,
                    "value": value,
                    "taxonomy": "us-gaap",
                    "concept": f"niu_exact_cumulative_ttm:{concepts[metric]}:{CURRENCY}",
                    "form": "20-F_PLUS_6-K_CUMULATIVE_TTM",
                    "accession": accession,
                    "fetched_at": FETCHED_AT,
                })
    return pd.DataFrame(records, columns=OUTPUT_COLUMNS).sort_values(["fiscal_end", "metric"]).reset_index(drop=True)


def _package_for_signal(signal_date: str) -> tuple[str, dict[str, object]]:
    eligible = [
        (fiscal_end, package)
        for fiscal_end, package in exact_ttm_evidence()["packages"].items()
        if package["available_date"] <= signal_date
    ]
    if not eligible:
        raise ValueError(f"no NIU package available for {signal_date}")
    return max(eligible, key=lambda item: (item[1]["available_date"], item[0]))


def resolve_audit_observations() -> pd.DataFrame:
    rows = []
    for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        fiscal_end, package = _package_for_signal(signal_date)
        age_days = (
            date.fromisoformat(signal_date)
            - date.fromisoformat(package["available_date"])
        ).days
        derived = package["derived"]
        rows.append({
            "scenario": scenario,
            "signal_date": signal_date,
            "maximum_age_days": maximum_age_days,
            "resolved": age_days <= maximum_age_days,
            "decision": "complete_exact_cny_cumulative_ttm_growth_bundle",
            "fiscal_end": fiscal_end,
            "available_date": package["available_date"],
            "financial_age_days": age_days,
            "revenue_ttm": derived["revenue"]["current_ttm_cny"],
            "revenue_growth": derived["revenue"]["growth"],
            "net_income_ttm": derived["net_income"]["current_ttm_cny"],
            "net_income_growth": derived["net_income"]["growth"],
            "currency": CURRENCY,
        })
    return pd.DataFrame(rows)


def validate_exact_packages() -> None:
    expected = {
        "2020-03-31": (1_660_178_080, 1_954_009_909, -275_162_267, 151_726_635),
        "2020-06-30": (1_806_427_307, 2_068_438_740, 28_805_957, 157_572_089),
    }
    packages = exact_ttm_evidence()["packages"]
    for fiscal_end, values in expected.items():
        revenue = packages[fiscal_end]["derived"]["revenue"]
        profit = packages[fiscal_end]["derived"]["net_income"]
        actual = (
            revenue["prior_ttm_cny"], revenue["current_ttm_cny"],
            profit["prior_ttm_cny"], profit["current_ttm_cny"],
        )
        if actual != values:
            raise RuntimeError(f"NIU exact TTM package changed for {fiscal_end}: {actual}")
    facts = strict_quarterly_facts()
    if len(facts) != 8 or set(facts.groupby(["fiscal_end", "available_date"])["metric"].nunique()) != {4}:
        raise RuntimeError("NIU direct TTM growth package is incomplete")


def rejected_evidence() -> dict[str, object]:
    usd_values = [
        value
        for checks in SOURCE_ROW_CHECKS.values()
        for check in checks
        for value in check["excluded_usd_translation_values"]
    ]
    return {
        "excluded_usd_translation_value_count": len(usd_values),
        "excluded_usd_translation_values": usd_values,
        "adjusted_or_non_gaap_labels": list(ADJUSTED_OR_NON_GAAP_LABELS),
        "later_filings": REJECTED_LATER_FILINGS,
    }


def build(output_dir: Path = OUTPUT_DIR) -> dict[str, object]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sources, verified_rows, verified_fragments = verify_sources(output_dir)
    validate_exact_packages()
    facts = strict_quarterly_facts()
    evidence = exact_ttm_evidence()
    resolution = resolve_audit_observations()
    rejected = rejected_evidence()
    unrecoverable: list[dict[str, object]] = []

    paths = {
        "strict_quarterly_facts": output_dir / "strict_quarterly_facts.csv",
        "exact_ttm_evidence": output_dir / "exact_ttm_evidence.json",
        "audit_observation_resolution": output_dir / "audit_observation_resolution.json",
        "rejected_evidence": output_dir / "rejected_evidence.json",
        "unrecoverable_observations": output_dir / "unrecoverable_observations.json",
    }
    facts.to_csv(paths["strict_quarterly_facts"], index=False)
    paths["exact_ttm_evidence"].write_text(json.dumps(evidence, indent=2) + "\n")
    paths["audit_observation_resolution"].write_text(json.dumps(resolution.to_dict(orient="records"), indent=2) + "\n")
    paths["rejected_evidence"].write_text(json.dumps(rejected, indent=2) + "\n")
    paths["unrecoverable_observations"].write_text(json.dumps(unrecoverable, indent=2) + "\n")
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
        "source_currency_label": SOURCE_CURRENCY_LABEL,
        "accounting_standard": ACCOUNTING_STANDARD,
        "accepted_direct_growth_package_count": len(PACKAGE_METADATA),
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": int(resolution["resolved"].sum()),
        "resolved_unique_signal_date_count": int(resolution["signal_date"].nunique()),
        "unrecoverable_observation_count": len(unrecoverable),
        "source_operand_verification_count": sum(len(row["operand_values"]) for row in verified_rows),
        "excluded_usd_translation_value_count": rejected["excluded_usd_translation_value_count"],
        "source_text_verification_count": len(verified_fragments),
        "comparative_match_count": len(COMPARATIVE_MATCHES),
        "sources": sources,
        "source_value_verification": {"rows": verified_rows, "fragments": verified_fragments},
        "rejected_evidence": rejected,
        "outputs": {name: {"path": str(path), "sha256": _sha256_path(path)} for name, path in paths.items()},
        "guardrail": (
            "Every operand is a reported whole-RMB/CNY U.S.-GAAP annual or "
            "cumulative value available before its served signal. Fourteen US$ "
            "convenience translations, adjusted/non-GAAP measures, later filings, "
            "estimates, FX conversions, and manufactured quarters are rejected."
        ),
    }
    manifest_path = output_dir / "manifest.json"
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
