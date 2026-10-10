#!/usr/bin/env python3
"""Recover LAKE's exact pre-signal annual-plus-M9 TTM growth bundle."""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import time
from urllib.request import Request, urlopen
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


TICKER = "LAKE"
CIK = 798_081
CURRENCY = "USD"
SOURCE_SCALE = 1_000
FISCAL_END = "2019-10-31"
AVAILABLE_DATE = "2019-12-09"
SIGNAL_DATE = "2020-02-28"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/lake_annual_m9_ttm_growth")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_hrzn_exact_annual_ttm"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_hrzn_exact_annual_ttm_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "09bf613a6440992149a4c083547b1ad5764e6aa03f8adbc3ba523da8a17014f1"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_lake_annual_m9_ttm_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "7c2dc66fca501199aeed55a6d30618439d30f90dc036d2c0d579462787b78246"
)

SOURCE_DOCUMENTS = {
    "fy2019_10k": {
        "role": "audited_fy2018_fy2019_operands",
        "form": "10-K",
        "filed": "2019-04-16",
        "accepted_at": "2019-04-16T16:03:05Z",
        "accession": "0001654954-19-004479",
        "document": "lakeland10-kv17toedgar.htm",
        "expected_sha256": (
            "618e8634f710351a49ee51d8bc17aebc9191b6c4a68a31bf72b288a780429910"
        ),
    },
    "q3_2018_10q": {
        "role": "m9_2017_m9_2018_operands",
        "form": "10-Q",
        "filed": "2018-12-17",
        "accepted_at": "2018-12-17T16:02:27Z",
        "accession": "0001654954-18-014004",
        "document": "lake_10q.htm",
        "expected_sha256": (
            "ba7fc2840b75d25106b5e23dc58c4216776702a19106186166938957ce660729"
        ),
    },
    "q3_2019_10q": {
        "role": "m9_2018_m9_2019_operands",
        "form": "10-Q",
        "filed": AVAILABLE_DATE,
        "accepted_at": "2019-12-09T16:05:43Z",
        "accession": "0001654954-19-013729",
        "document": "lake_10q.htm",
        "expected_sha256": (
            "7f669f272da232c53bb0ee98918aafea7a4c2643920f02a77b855c8ced80c7a3"
        ),
    },
}

SOURCE_TEXT_CHECKS = {
    "fy2019_10k": (
        "LAKELAND INDUSTRIES, INC.",
        "CONSOLIDATED STATEMENTS OF OPERATIONS",
        "January 31, 2019 and 2018",
        "Net sales",
    ),
    "q3_2018_10q": (
        "LAKELAND INDUSTRIES, INC.",
        "CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS",
        "Nine Months Ended October 31",
        "UNAUDITED",
    ),
    "q3_2019_10q": (
        "LAKELAND INDUSTRIES, INC.",
        "CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS",
        "Nine Months Ended October 31",
        "UNAUDITED",
    ),
}

SOURCE_ROW_CHECKS = {
    "fy2019_10k": (
        {
            "check_id": "annual_net_sales",
            "row_label": "Net sales",
            "expected": (99_011, 95_987),
        },
        {
            "check_id": "annual_net_income",
            "row_label": "Net income",
            "expected": (1_459, 440),
        },
    ),
    "q3_2018_10q": (
        {
            "check_id": "q3_2018_net_sales",
            "row_label": "Net sales",
            "expected": (24_009, 23_960, 73_970, 70_831),
        },
        {
            "check_id": "q3_2018_net_income",
            "row_label": "Net income",
            "expected": (501, 1_806, 3_386, 5_358),
        },
    ),
    "q3_2019_10q": (
        {
            "check_id": "q3_2019_net_sales",
            "row_label": "Net sales",
            "expected": (27_464, 24_009, 79_620, 73_970),
        },
        {
            "check_id": "q3_2019_net_income",
            "row_label": "Net income",
            "expected": (1_146, 501, 2_076, 3_386),
        },
    ),
}

OPERANDS_USD_THOUSANDS = {
    "fy2018": {"revenue": 95_987, "net_income": 440},
    "fy2019": {"revenue": 99_011, "net_income": 1_459},
    "m9_2017": {"revenue": 70_831, "net_income": 5_358},
    "m9_2018": {"revenue": 73_970, "net_income": 3_386},
    "m9_2019": {"revenue": 79_620, "net_income": 2_076},
}
EXPECTED_TTM_USD_THOUSANDS = {
    "previous": {"revenue": 99_126, "net_income": -1_532},
    "current": {"revenue": 104_661, "net_income": 149},
}
EXPECTED_GROWTH = {
    "revenue": 0.05583802433266751,
    "net_income": 1.097258485639687,
}
TARGET_METRICS = frozenset(
    {"revenue_ttm", "net_income_ttm", "revenue_growth", "net_income_growth"}
)
AUDIT_OBSERVATIONS = tuple(
    (f"liq2000000-age{age}-growth", age) for age in (150, 365, 550)
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _source_url(source: dict) -> str:
    accession = source["accession"].replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
        f"{accession}/{source['document']}"
    )


def _download_bytes(url: str) -> bytes:
    error: Exception | None = None
    for attempt in range(5):
        try:
            with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
                return response.read()
        except OSError as exc:  # pragma: no cover - network retry
            error = exc
            if attempt < 4:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to download locked LAKE source: {url}") from error


def _normalize(value: object) -> str:
    return " ".join(
        str(value)
        .replace("\xa0", " ")
        .replace("\u200b", " ")
        .replace("\u2009", " ")
        .split()
    )


def _html_soup(payload: bytes) -> BeautifulSoup:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        return BeautifulSoup(payload, "lxml")


def _numeric_cells(values) -> list[int]:
    numbers = []
    for value in values:
        text = _normalize(value)
        if not text or text.casefold() == "nan" or text in {"$", ")", "-", "—"}:
            continue
        negative = text.startswith("(") or text.endswith(")")
        cleaned = re.sub(r"[^0-9.-]", "", text)
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
            continue
        number = float(cleaned)
        if not number.is_integer():
            continue
        integer = int(number)
        numbers.append(-abs(integer) if negative else integer)
    return numbers


def _contains_subsequence(values: list[int], expected: tuple[int, ...]) -> bool:
    width = len(expected)
    return any(
        tuple(values[index:index + width]) == expected
        for index in range(len(values) - width + 1)
    )


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("LAKE raw source set does not match the source lock")
    verified = []
    for source_id, checks in SOURCE_ROW_CHECKS.items():
        tables = pd.read_html(BytesIO(raw_by_source[source_id]))
        for check in checks:
            expected = tuple(check["expected"])
            matches = []
            for table in tables:
                for _, row in table.iterrows():
                    if _normalize(row.iloc[0]).casefold() != _normalize(
                        check["row_label"]
                    ).casefold():
                        continue
                    values = _numeric_cells(row.iloc[1:])
                    if _contains_subsequence(values, expected):
                        matches.append(tuple(values))
            if not matches:
                raise RuntimeError(
                    f"LAKE source row changed for {check['check_id']}"
                )
            verified.append({
                "source_id": source_id,
                "check_id": check["check_id"],
                "row_label": check["row_label"],
                "expected_values_usd_thousands": list(expected),
                "match_count": len(matches),
            })
    return verified


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("LAKE source document set changed")
    for source_id, source in documents.items():
        locked = SOURCE_DOCUMENTS[source_id]
        for field in ("form", "accepted_at", "accession", "document"):
            if source[field] != locked[field]:
                raise ValueError(
                    f"LAKE source {source_id} changed locked identity field {field}"
                )
        if source["filed"] > SIGNAL_DATE:
            raise ValueError(f"LAKE source {source_id} violates the PIT cutoff")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"LAKE source {source_id} has an invalid SHA-256")
        url = _source_url(source)
        if f"/data/{CIK}/{source['accession'].replace('-', '')}/" not in url:
            raise ValueError(f"LAKE source {source_id} does not lock CIK/accession")
        if not url.endswith("/" + source["document"]):
            raise ValueError(f"LAKE source {source_id} does not lock document")
    if AVAILABLE_DATE != max(source["filed"] for source in documents.values()):
        raise ValueError("LAKE availability date is not the latest operand filing")


def prepare_verified_sources(output_dir: Path) -> tuple[dict, list[dict]]:
    validate_source_lock()
    provenance = {}
    raw_by_source = {}
    for source_id, source in SOURCE_DOCUMENTS.items():
        path = output_dir / "sources" / f"{source_id}_{source['document']}"
        downloaded = False
        if path.exists():
            payload = path.read_bytes()
        else:
            payload = _download_bytes(_source_url(source))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            downloaded = True
        actual_sha = _sha256_bytes(payload)
        if actual_sha != source["expected_sha256"]:
            raise RuntimeError(
                f"LAKE source SHA-256 changed for {source_id}: {actual_sha}"
            )
        text = _normalize(_html_soup(payload).get_text(" ", strip=True)).casefold()
        missing = [
            fragment for fragment in SOURCE_TEXT_CHECKS[source_id]
            if _normalize(fragment).casefold() not in text
        ]
        if missing:
            raise RuntimeError(f"LAKE source text changed for {source_id}: {missing}")
        raw_by_source[source_id] = payload
        provenance[source_id] = {
            **source,
            "url": _source_url(source),
            "local_path": str(path),
            "actual_sha256": actual_sha,
            "bytes": len(payload),
            "downloaded": downloaded,
        }
    return provenance, verify_source_values(raw_by_source)


def ttm_evidence() -> dict:
    operands = OPERANDS_USD_THOUSANDS
    previous = {
        metric: operands["fy2018"][metric] - operands["m9_2017"][metric]
        + operands["m9_2018"][metric]
        for metric in ("revenue", "net_income")
    }
    current = {
        metric: operands["fy2019"][metric] - operands["m9_2018"][metric]
        + operands["m9_2019"][metric]
        for metric in ("revenue", "net_income")
    }
    growth = {
        metric: (current[metric] - previous[metric]) / abs(previous[metric])
        for metric in ("revenue", "net_income")
    }
    if previous != EXPECTED_TTM_USD_THOUSANDS["previous"]:
        raise RuntimeError(f"LAKE previous TTM changed: {previous}")
    if current != EXPECTED_TTM_USD_THOUSANDS["current"]:
        raise RuntimeError(f"LAKE current TTM changed: {current}")
    for metric, expected in EXPECTED_GROWTH.items():
        if abs(growth[metric] - expected) > 1e-12:
            raise RuntimeError(f"LAKE {metric} growth changed: {growth[metric]}")
    if previous["revenue"] == 0 or previous["net_income"] == 0:
        raise RuntimeError("LAKE growth denominator must remain nonzero")
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "source_scale": SOURCE_SCALE,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "signal_date": SIGNAL_DATE,
        "financial_age_days": (
            pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)
        ).days,
        "operands_usd_thousands": operands,
        "derived": {
            "previous_ttm_usd_thousands": previous,
            "current_ttm_usd_thousands": current,
            "growth": growth,
        },
        "metric_mapping": {
            "revenue": "consolidated Net sales",
            "net_income": "consolidated Net income",
            "growth_formula": "(current_ttm - previous_ttm) / abs(previous_ttm)",
        },
        "accounting_boundary": {
            "standard": "US-GAAP",
            "currency_and_scale_consistent": True,
            "consolidated_basis_consistent": True,
            "discontinued_operations_excluded": True,
            "adjusted_metrics_excluded": True,
            "later_fy2020_10k_excluded": True,
        },
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = ttm_evidence()["derived"]
    values = {
        "revenue_ttm": evidence["current_ttm_usd_thousands"]["revenue"]
        * SOURCE_SCALE,
        "net_income_ttm": evidence["current_ttm_usd_thousands"]["net_income"]
        * SOURCE_SCALE,
        "revenue_growth": evidence["growth"]["revenue"],
        "net_income_growth": evidence["growth"]["net_income"],
    }
    rows = [{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": metric,
        "value": value,
        "taxonomy": "us-gaap",
        "concept": f"lake_annual_m9_ttm:{metric}",
        "form": "10-K+10-Q_DERIVED_TTM",
        "accession": SOURCE_DOCUMENTS["q3_2019_10q"]["accession"],
        "fetched_at": FETCHED_AT,
    } for metric, value in values.items()]
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if len(facts) != 4 or set(facts["metric"]) != TARGET_METRICS:
        raise RuntimeError("LAKE recovery must contain the four-field TTM bundle")
    return facts


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"LAKE audit binding changed: {actual_sha}")
    frame = pd.read_csv(path)
    scenarios = {scenario for scenario, _age in AUDIT_OBSERVATIONS}
    return frame.loc[
        frame["ticker"].eq(TICKER) & frame["scenario"].isin(scenarios)
    ].copy()


def validate_audit_binding(
    path: Path, expected_sha256: str, *, expect_recovered: bool
) -> dict:
    rows = _audit_rows(path, expected_sha256)
    if expect_recovered:
        if not rows.empty:
            raise RuntimeError("LAKE remains in the current financial priorities")
        return {
            "path": str(path),
            "sha256": expected_sha256,
            "remaining_observation_count": 0,
            "status": "RECOVERED",
        }
    scenarios = {scenario for scenario, _age in AUDIT_OBSERVATIONS}
    if len(rows) != 3 or set(rows["scenario"]) != scenarios:
        raise RuntimeError("LAKE baseline audit scenarios changed")
    expected = {
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 0,
        "insufficient_growth_history_signal_count": 1,
        "stale_growth_snapshot_signal_count": 0,
    }
    for column, value in expected.items():
        if not rows[column].eq(value).all():
            raise RuntimeError(f"LAKE baseline {column} changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("LAKE baseline signal date changed")
    return {
        "path": str(path),
        "sha256": expected_sha256,
        "missing_observation_count": 3,
        "classification": "pre_signal_10k_net_sales_parser_omission",
    }


def recovered_observations() -> pd.DataFrame:
    evidence = ttm_evidence()
    derived = evidence["derived"]
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": TICKER,
        "signal_date": SIGNAL_DATE,
        "maximum_age_days": age,
        "resolved": True,
        "decision": "recovered_exact_us_gaap_annual_m9_ttm_growth_bundle",
        "revenue_ttm_usd": (
            derived["current_ttm_usd_thousands"]["revenue"] * SOURCE_SCALE
        ),
        "net_income_ttm_usd": (
            derived["current_ttm_usd_thousands"]["net_income"] * SOURCE_SCALE
        ),
        "revenue_growth": derived["growth"]["revenue"],
        "net_income_growth": derived["growth"]["net_income"],
        "financial_age_days": evidence["financial_age_days"],
        "available_date": AVAILABLE_DATE,
    } for scenario, age in AUDIT_OBSERVATIONS])


def build(
    output_dir: Path = OUTPUT_DIR,
    *,
    baseline_audit_path: Path = BASELINE_AUDIT_PATH,
    expected_baseline_audit_sha256: str = EXPECTED_BASELINE_AUDIT_SHA256,
    current_audit_path: Path = CURRENT_AUDIT_PATH,
    expected_current_audit_sha256: str = EXPECTED_CURRENT_AUDIT_SHA256,
) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sources, source_verification = prepare_verified_sources(output_dir)
    evidence = ttm_evidence()
    facts = strict_quarterly_facts()
    observations = recovered_observations()
    baseline = validate_audit_binding(
        Path(baseline_audit_path), expected_baseline_audit_sha256,
        expect_recovered=False,
    )
    current_is_baseline = (
        Path(current_audit_path) == Path(baseline_audit_path)
        and expected_current_audit_sha256 == expected_baseline_audit_sha256
    )
    current = validate_audit_binding(
        Path(current_audit_path), expected_current_audit_sha256,
        expect_recovered=not current_is_baseline,
    )
    if current_is_baseline:
        current = {
            "path": str(current_audit_path),
            "sha256": expected_current_audit_sha256,
            "remaining_observation_count": 3,
            "status": "PENDING_CANDIDATE_INTEGRATION",
        }

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "ttm_evidence.json"
    observations_path = output_dir / "recovered_observations.csv"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    observations.to_csv(observations_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "shared_candidate_integrated": not current_is_baseline,
        "ticker": TICKER,
        "cik": CIK,
        "currency": CURRENCY,
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": len(observations),
        "source_documents": sources,
        "source_value_verification": source_verification,
        "metric_mapping": evidence["metric_mapping"],
        "accounting_boundary": evidence["accounting_boundary"],
        "audit_binding": {
            "baseline": baseline,
            "current": current,
            "recovered_observation_count": len(observations),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path),
            },
            "ttm_evidence": {
                "path": str(evidence_path), "sha256": _sha256(evidence_path),
            },
            "recovered_observations": {
                "path": str(observations_path),
                "sha256": _sha256(observations_path),
                "row_count": len(observations),
            },
        },
        "guardrail": (
            "The four-field bundle uses audited FY2018/FY2019 and exact "
            "comparative M9 2017/M9 2018/M9 2019 consolidated USD-thousand "
            "amounts public by 2019-12-09. The later FY2020 10-K, discontinued "
            "operations, adjusted values, and estimates are excluded. Formal "
            "financial files remain unchanged."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def _target_mask(frame: pd.DataFrame) -> pd.Series:
    fiscal_end = pd.to_datetime(frame["fiscal_end"], errors="raise").dt.strftime(
        "%Y-%m-%d"
    )
    return (
        frame["ticker"].eq(TICKER)
        & fiscal_end.eq(FISCAL_END)
        & frame["metric"].isin(TARGET_METRICS)
    )


def integrate_candidate(
    *, base_dir: Path, supplement_dir: Path = OUTPUT_DIR, output_dir: Path
) -> dict:
    """Copy-on-write overlay of only LAKE's 2019Q3 exact TTM bundle."""
    base_dir = Path(base_dir)
    supplement_dir = Path(supplement_dir)
    output_dir = Path(output_dir)
    inputs = (
        base_dir / "annual.csv",
        base_dir / "quarterly.csv",
        base_dir / "manifest.json",
        supplement_dir / "strict_quarterly_facts.csv",
        supplement_dir / "manifest.json",
    )
    bound = {path: _sha256(path) for path in inputs}
    base = pd.read_csv(inputs[1])
    incoming = pd.read_csv(inputs[3])
    if list(base.columns) != OUTPUT_COLUMNS or list(incoming.columns) != OUTPUT_COLUMNS:
        raise RuntimeError("LAKE integration requires the quarterly schema")
    if len(incoming) != 4 or not _target_mask(incoming).all():
        raise RuntimeError("LAKE supplement scope is not the four-field TTM bundle")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(untouched) + 4:
        raise RuntimeError("LAKE overlay changed rows outside the bounded key space")

    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("LAKE integration source changed while being read")
    report = {
        "schema_version": 1,
        "research_only": True,
        "formal_financials_modified": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "overlay_ticker": TICKER,
        "overlay_fiscal_end": FISCAL_END,
        "overlay_metrics": sorted(TARGET_METRICS),
        "removed_conflicting_rows": len(replaced),
        "inserted_strict_rows": len(incoming),
        "base": {
            "path": str(base_dir),
            "sha256": {str(path): digest for path, digest in bound.items()},
        },
        "outputs": {
            "annual": str(annual_path),
            "annual_sha256": _sha256(annual_path),
            "quarterly": str(quarterly_path),
            "quarterly_sha256": _sha256(quarterly_path),
            "quarterly_rows": len(merged),
        },
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
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = build(args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
