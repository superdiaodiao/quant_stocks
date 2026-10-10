#!/usr/bin/env python3
"""Recover LGO's exact audited pre-signal annual TTM growth bundle."""

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


TICKER = "LGO"
CIK = 1_400_438
CURRENCY = "USD"
SOURCE_SCALE = 1_000
FISCAL_END = "2020-12-31"
AVAILABLE_DATE = "2021-04-14"
SIGNAL_DATE = "2021-06-30"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/lgo_exact_annual_growth")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_lake_annual_m9_ttm"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_lake_annual_m9_ttm_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "7c2dc66fca501199aeed55a6d30618439d30f90dc036d2c0d579462787b78246"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_lgo_exact_annual_ttm_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "a17dfaa594de056ef08fe10fdd2347788b1a92f4646c2f7ce5c058aa67914379"
)

OPERAND_ACCESSION = "0001104659-21-049967"
SOURCE_DOCUMENTS = {
    "40fr12b_cover": {
        "role": "sec_availability_and_incorporation_proof",
        "form": "40-F Registration Statement",
        "filed": AVAILABLE_DATE,
        "accepted_at": "2021-04-14T07:43:04Z",
        "accession": OPERAND_ACCESSION,
        "document": "a21-6618_140fr12b.htm",
        "expected_sha256": (
            "7ce53d7e05dd7e6817106442a2aabbfc421c4cfdee5bafdacf6d13b8f27229a3"
        ),
    },
    "40fr12b_exhibit_99_69": {
        "role": "audited_fy2019_fy2020_operands",
        "form": "40-F Exhibit 99.69",
        "filed": AVAILABLE_DATE,
        "accepted_at": "2021-04-14T07:43:04Z",
        "accession": OPERAND_ACCESSION,
        "document": "a21-6618_1ex99d69.htm",
        "expected_sha256": (
            "2346f0f961ba79aed963fc535221e43177c4233d84b08bff0b92d7b099c55e5d"
        ),
    },
}

SOURCE_TEXT_CHECKS = {
    "40fr12b_cover": (
        "REGISTRATION STATEMENT PURSUANT TO SECTION 12",
        "LARGO RESOURCES LTD.",
        "99.69 Annual Consolidated Financial Statements",
        "Annual Consolidated Financial Statements for the years ended December 31, 2019 and 2020",
    ),
    "40fr12b_exhibit_99_69": (
        "ANNUAL CONSOLIDATED FINANCIAL STATEMENTS FOR THE YEARS ENDED DECEMBER 31, 2020 AND 2019",
        "U.S. dollars",
        "International Financial Reporting Standards as issued by the International Accounting Standards Board",
        "Prior period comparative information has been restated in U.S. dollars",
    ),
}

SOURCE_ROW_CHECKS = {
    "40fr12b_exhibit_99_69": (
        {
            "check_id": "annual_revenues",
            "row_label": "Revenues",
            "expected": (119_987, 105_107),
        },
        {
            "check_id": "annual_profit_loss",
            "row_label": "Net income (loss)",
            "expected": (6_761, -26_970),
        },
    ),
}

OPERANDS_USD_THOUSANDS = {
    "fy2019": {"revenue": 105_107, "net_income": -26_970},
    "fy2020": {"revenue": 119_987, "net_income": 6_761},
}
EXPECTED_GROWTH = {
    "revenue": 0.14157001912336953,
    "net_income": 1.2506859473489063,
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
    raise RuntimeError(f"failed to download locked LGO source: {url}") from error


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
        raise ValueError("LGO raw source set does not match the source lock")
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
                    f"LGO source row changed for {check['check_id']}"
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
        raise ValueError("LGO source document set changed")
    for source_id, source in documents.items():
        locked = SOURCE_DOCUMENTS[source_id]
        for field in ("form", "accepted_at", "accession", "document"):
            if source[field] != locked[field]:
                raise ValueError(
                    f"LGO source {source_id} changed locked identity field {field}"
                )
        if source["filed"] > SIGNAL_DATE:
            raise ValueError(f"LGO source {source_id} violates the PIT cutoff")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"LGO source {source_id} has an invalid SHA-256")
        url = _source_url(source)
        if f"/data/{CIK}/{source['accession'].replace('-', '')}/" not in url:
            raise ValueError(f"LGO source {source_id} does not lock CIK/accession")
        if not url.endswith("/" + source["document"]):
            raise ValueError(f"LGO source {source_id} does not lock document")
    if AVAILABLE_DATE != max(source["filed"] for source in documents.values()):
        raise ValueError("LGO availability date is not the latest operand filing")


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
                f"LGO source SHA-256 changed for {source_id}: {actual_sha}"
            )
        text = _normalize(_html_soup(payload).get_text(" ", strip=True)).casefold()
        missing = [
            fragment for fragment in SOURCE_TEXT_CHECKS[source_id]
            if _normalize(fragment).casefold() not in text
        ]
        if missing:
            raise RuntimeError(f"LGO source text changed for {source_id}: {missing}")
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
    current = OPERANDS_USD_THOUSANDS["fy2020"]
    previous = OPERANDS_USD_THOUSANDS["fy2019"]
    growth = {
        metric: (current[metric] - previous[metric]) / abs(previous[metric])
        for metric in ("revenue", "net_income")
    }
    for metric, expected in EXPECTED_GROWTH.items():
        if abs(growth[metric] - expected) > 1e-12:
            raise RuntimeError(f"LGO {metric} growth changed: {growth[metric]}")
    if previous["revenue"] == 0 or previous["net_income"] == 0:
        raise RuntimeError("LGO growth denominator must remain nonzero")
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
        "operands_usd_thousands": OPERANDS_USD_THOUSANDS,
        "derived": {
            "previous_ttm_usd_thousands": previous,
            "current_ttm_usd_thousands": current,
            "growth": growth,
        },
        "metric_mapping": {
            "revenue": "IFRS consolidated Revenues",
            "net_income": "IFRS consolidated Net income (loss)",
            "growth_formula": "(current_ttm - previous_ttm) / abs(previous_ttm)",
        },
        "accounting_boundary": {
            "standard": "IFRS as issued by IASB",
            "presentation_currency": "USD",
            "fy2019_comparatives_represented_in_usd": True,
            "functional_currency_change_is_prospective": True,
            "consolidated_basis_consistent": True,
            "adjusted_metrics_excluded": True,
            "later_40f_amendments_excluded": True,
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
        "taxonomy": "ifrs-full",
        "concept": f"lgo_direct_annual_ttm:{metric}",
        "form": "40-FR12B_DIRECT_ANNUAL_TTM",
        "accession": OPERAND_ACCESSION,
        "fetched_at": FETCHED_AT,
    } for metric, value in values.items()]
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if len(facts) != 4 or set(facts["metric"]) != TARGET_METRICS:
        raise RuntimeError("LGO recovery must contain the four-field TTM bundle")
    return facts


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"LGO audit binding changed: {actual_sha}")
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
            raise RuntimeError("LGO remains in the current financial priorities")
        return {
            "path": str(path),
            "sha256": expected_sha256,
            "remaining_observation_count": 0,
            "status": "RECOVERED",
        }
    scenarios = {scenario for scenario, _age in AUDIT_OBSERVATIONS}
    if len(rows) != 3 or set(rows["scenario"]) != scenarios:
        raise RuntimeError("LGO baseline audit scenarios changed")
    expected = {
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 1,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 0,
    }
    for column, value in expected.items():
        if not rows[column].eq(value).all():
            raise RuntimeError(f"LGO baseline {column} changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("LGO baseline signal date changed")
    return {
        "path": str(path),
        "sha256": expected_sha256,
        "missing_observation_count": 3,
        "classification": "pre_listing_40fr12b_exhibit_parser_omission",
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
        "decision": "recovered_exact_ifrs_direct_annual_ttm_growth_bundle",
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
            "The four-field bundle uses only audited FY2020/FY2019 IFRS "
            "amounts incorporated into the 2021-04-14 Form 40-F registration "
            "statement. Both years are presented in USD and the FY2019 "
            "comparatives were explicitly restated for presentation currency. "
            "Adjusted metrics, later 40-F amendments, and estimates are "
            "excluded. Formal financial files remain unchanged."
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
    """Copy-on-write overlay of only LGO's audited FY2020 direct TTM bundle."""
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
        raise RuntimeError("LGO integration requires the quarterly schema")
    if len(incoming) != 4 or not _target_mask(incoming).all():
        raise RuntimeError("LGO supplement scope is not the four-field TTM bundle")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(untouched) + 4:
        raise RuntimeError("LGO overlay changed rows outside the bounded key space")

    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("LGO integration source changed while being read")
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
