#!/usr/bin/env python3
"""Recover NCTY's exact pre-signal H1-derived TTM growth bundle."""

from __future__ import annotations

import argparse
from decimal import Decimal
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


TICKER = "NCTY"
CIK = 1_296_774
CURRENCY = "RMB"
FISCAL_END = "2020-06-30"
AVAILABLE_DATE = "2020-12-30"
SIGNAL_DATE = "2021-01-29"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/ncty_h1_ttm_growth")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_lgo_exact_annual_ttm"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_lgo_exact_annual_ttm_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "a17dfaa594de056ef08fe10fdd2347788b1a92f4646c2f7ce5c058aa67914379"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_ncty_h1_ttm_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "21307afa31a7c768b42f60dccc4c618bb4974b565754091b3cd40b33113d7f8d"
)

SOURCE_DOCUMENTS = {
    "fy2018_20f": {
        "role": "audited_fy2018_operands",
        "form": "20-F",
        "filed": "2019-04-29",
        "accepted_at": "2019-04-29T20:09:20Z",
        "accession": "0001144204-19-021931",
        "document": "tv517526_20f.htm",
        "expected_sha256": (
            "c2c72834619d8d750f438ae8722d8f22af7e90f839468ad2fd816fb12197df7e"
        ),
    },
    "h1_2018_6k": {
        "role": "unaudited_h1_2018_operands",
        "form": "6-K Exhibit 99.1",
        "filed": "2018-12-27",
        "accepted_at": "2018-12-27T11:10:05Z",
        "accession": "0001193125-18-358508",
        "document": "d665085dex991.htm",
        "expected_sha256": (
            "550681405711c76499c4277b1c69233a38e6014b662dd4e886c3af089fb3fa9f"
        ),
    },
    "h1_2019_6k": {
        "role": "unaudited_h1_2018_comparative_and_h1_2019_operands",
        "form": "6-K Exhibit 99.1",
        "filed": "2019-12-27",
        "accepted_at": "2019-12-27T11:30:00Z",
        "accession": "0001104659-19-076285",
        "document": "tm1927491d1_ex99-1.htm",
        "expected_sha256": (
            "636641effaf50d7963b3701d9d4b25b2778db26f67f8b8e45415bfb7062f72b3"
        ),
    },
    "fy2019_20f": {
        "role": "audited_fy2018_comparative_and_fy2019_operands",
        "form": "20-F",
        "filed": "2020-04-30",
        "accepted_at": "2020-04-30T20:12:27Z",
        "accession": "0001104659-20-054433",
        "document": "tm206461d1_20f.htm",
        "expected_sha256": (
            "097097f19bf544bc35a4f2bf55837a7a6d9c4298b5a379d840bc0f967d359e99"
        ),
    },
    "h1_2020_6k": {
        "role": "unaudited_h1_2019_comparative_and_h1_2020_operands",
        "form": "6-K Exhibit 99.1",
        "filed": AVAILABLE_DATE,
        "accepted_at": "2020-12-30T11:42:52Z",
        "accession": "0001104659-20-140356",
        "document": "tm2039591d1_ex99-1.htm",
        "expected_sha256": (
            "0ba791af1d3cbd677918a91f3b24f5666464b40ea66c42195a43e33d60d6f089"
        ),
    },
}

SOURCE_TEXT_CHECKS = {
    "fy2018_20f": (
        "ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d)",
        "The9 Limited",
        "US$1.00 to RMB6.8755",
    ),
    "h1_2018_6k": (
        "Unaudited Financial Information as of and for the Six Months Ended June 30, 2018",
        "US$1.00 = RMB6.6171",
    ),
    "h1_2019_6k": (
        "As of and For the Six Months Ended June 30, 2019",
        "US$1.00 = RMB6.8650",
    ),
    "fy2019_20f": (
        "ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d)",
        "The9 Limited",
        "US$1.00 to RMB6.9618",
    ),
    "h1_2020_6k": (
        "As of and For the Six Months Ended June 30, 2020",
        "US$1.00 = RMB7.0651",
    ),
}

SOURCE_ROW_CHECKS = {
    "fy2018_20f": (
        {"check_id": "fy2018_revenue", "row_label": "Total net revenues",
         "expected": (56_199_286, 73_148_556, 17_431_858, 2_535_358)},
        {"check_id": "fy2018_net_income", "row_label": "Net loss attributable to The9 Limited",
         "expected": (-593_781_589, -118_165_850, -217_092_926, -31_574_856)},
    ),
    "h1_2018_6k": (
        {"check_id": "h1_2018_revenue", "row_label": "Total net revenues",
         "expected": (15_759_261, 10_460_086, 1_580_766)},
        {"check_id": "h1_2018_net_income", "row_label": "Net loss attributable to The9 Limited",
         "expected": (-113_779_067, -74_994_778, -11_333_483)},
    ),
    "h1_2019_6k": (
        {"check_id": "h1_2019_revenue", "row_label": "Total net revenues",
         "expected": (10_460_086, 251_327, 36_610)},
        {"check_id": "h1_2019_net_income", "row_label": "Net loss attributable to The9 Limited",
         "expected": (-74_994_778, -40_486_287, -5_897_492)},
    ),
    "fy2019_20f": (
        {"check_id": "fy2019_revenue", "row_label": "Total net revenues",
         "expected": (73_148_556, 17_431_858, 341_495, 49_053)},
        {"check_id": "fy2019_net_income", "row_label": "Net loss attributable to The9 Limited",
         "expected": (-118_165_850, -217_092_926, -177_795_168, -25_538_677)},
    ),
    "h1_2020_6k": (
        {"check_id": "h1_2020_revenue", "row_label": "Total net revenues",
         "expected": (251_327, 465_726, 65_919)},
        {"check_id": "h1_2020_net_income", "row_label": "Net (loss) gain attributable to The9 Limited",
         "expected": (-40_486_287, 450_573_532, 63_774_544)},
    ),
}

OPERANDS_RMB = {
    "fy2018": {"revenue": 17_431_858, "net_income": -217_092_926},
    "h1_2018": {"revenue": 10_460_086, "net_income": -74_994_778},
    "h1_2019": {"revenue": 251_327, "net_income": -40_486_287},
    "fy2019": {"revenue": 341_495, "net_income": -177_795_168},
    "h1_2020": {"revenue": 465_726, "net_income": 450_573_532},
}
EXPECTED_TTM_RMB = {
    "prior": {"revenue": 7_223_099, "net_income": -182_584_435},
    "current": {"revenue": 555_894, "net_income": 313_264_651},
}
EXPECTED_GROWTH = {
    "revenue": -0.9230394045547486,
    "net_income": 2.7157248425913195,
}
TARGET_METRICS = frozenset(
    {"revenue_ttm", "net_income_ttm", "revenue_growth", "net_income_growth"}
)
AUDIT_OBSERVATIONS = (("liq2000000-age150-growth", 150),)


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
    raise RuntimeError(f"failed to download locked NCTY source: {url}") from error


def _normalize(value: object) -> str:
    return " ".join(
        str(value).replace("\xa0", " ").replace("\u200b", " ").split()
    )


def _html_text(payload: bytes) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(payload, "lxml")
    return _normalize(soup.get_text(" ", strip=True))


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
    return any(
        tuple(values[index:index + len(expected)]) == expected
        for index in range(len(values) - len(expected) + 1)
    )


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("NCTY raw source set does not match the source lock")
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
                    f"NCTY source row changed for {check['check_id']}"
                )
            verified.append({
                "source_id": source_id,
                "check_id": check["check_id"],
                "row_label": check["row_label"],
                "expected_values": list(expected),
                "match_count": len(matches),
            })
    return verified


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    if set(documents) != set(SOURCE_DOCUMENTS):
        raise ValueError("NCTY source document set changed")
    for source_id, source in documents.items():
        locked = SOURCE_DOCUMENTS[source_id]
        for field in ("form", "accepted_at", "accession", "document"):
            if source[field] != locked[field]:
                raise ValueError(
                    f"NCTY source {source_id} changed locked identity field {field}"
                )
        if source["filed"] > SIGNAL_DATE:
            raise ValueError(f"NCTY source {source_id} violates the PIT cutoff")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"NCTY source {source_id} has an invalid SHA-256")
        url = _source_url(source)
        if f"/data/{CIK}/{source['accession'].replace('-', '')}/" not in url:
            raise ValueError(f"NCTY source {source_id} does not lock CIK/accession")
        if not url.endswith("/" + source["document"]):
            raise ValueError(f"NCTY source {source_id} does not lock document")
    if AVAILABLE_DATE != max(source["filed"] for source in documents.values()):
        raise ValueError("NCTY availability date is not the latest operand filing")


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
                f"NCTY source SHA-256 changed for {source_id}: {actual_sha}"
            )
        text = _html_text(payload).casefold()
        missing = [
            fragment for fragment in SOURCE_TEXT_CHECKS[source_id]
            if _normalize(fragment).casefold() not in text
        ]
        if missing:
            raise RuntimeError(
                f"NCTY source text changed for {source_id}: {missing}"
            )
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


def _growth(current: int, prior: int) -> float:
    if prior == 0:
        raise ValueError("NCTY exact TTM growth denominator cannot be zero")
    return float((Decimal(current) - Decimal(prior)) / abs(Decimal(prior)))


def ttm_evidence() -> dict:
    prior = {
        metric: (
            OPERANDS_RMB["fy2018"][metric]
            - OPERANDS_RMB["h1_2018"][metric]
            + OPERANDS_RMB["h1_2019"][metric]
        )
        for metric in ("revenue", "net_income")
    }
    current = {
        metric: (
            OPERANDS_RMB["fy2019"][metric]
            - OPERANDS_RMB["h1_2019"][metric]
            + OPERANDS_RMB["h1_2020"][metric]
        )
        for metric in ("revenue", "net_income")
    }
    if {"prior": prior, "current": current} != EXPECTED_TTM_RMB:
        raise RuntimeError("NCTY exact TTM operands changed")
    growth = {
        metric: _growth(current[metric], prior[metric])
        for metric in ("revenue", "net_income")
    }
    for metric, expected in EXPECTED_GROWTH.items():
        if abs(growth[metric] - expected) > 1e-15:
            raise RuntimeError(f"NCTY {metric} growth changed: {growth[metric]}")
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "signal_date": SIGNAL_DATE,
        "financial_age_days": int(
            (pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)).days
        ),
        "operands_rmb": OPERANDS_RMB,
        "derived": {
            "prior_ttm_rmb": prior,
            "current_ttm_rmb": current,
            "growth": growth,
        },
        "formulas": {
            "prior_ttm": "FY2018 - H1_2018 + H1_2019",
            "current_ttm": "FY2019 - H1_2019 + H1_2020",
            "growth": "(current_ttm - prior_ttm) / abs(prior_ttm)",
        },
        "metric_mapping": {
            "revenue": "US-GAAP consolidated Total net revenues",
            "net_income": "US-GAAP consolidated Net (loss) gain attributable to The9 Limited",
        },
        "accounting_boundary": {
            "standard": "US-GAAP",
            "presentation_currency": "RMB",
            "same_currency_all_operands": True,
            "usd_convenience_translations_excluded": True,
            "reason_usd_excluded": (
                "Each filing translated only its latest column at a different "
                "period-end RMB/USD rate, so cross-filing USD TTM arithmetic "
                "would be a mixed exchange-rate basis."
            ),
            "consolidated_basis_consistent": True,
            "adjusted_metrics_excluded": True,
            "post_signal_fy2020_20f_excluded": True,
        },
    }


def strict_quarterly_facts() -> pd.DataFrame:
    evidence = ttm_evidence()["derived"]
    values = {
        "revenue_ttm": evidence["current_ttm_rmb"]["revenue"],
        "net_income_ttm": evidence["current_ttm_rmb"]["net_income"],
        "revenue_growth": evidence["growth"]["revenue"],
        "net_income_growth": evidence["growth"]["net_income"],
    }
    accessions = "+".join(dict.fromkeys(
        source["accession"] for source in SOURCE_DOCUMENTS.values()
    ))
    rows = [{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": metric,
        "value": value,
        "taxonomy": "us-gaap",
        "concept": f"ncty_annual_h1_ttm:{metric}:RMB",
        "form": "20-F_PLUS_6-K_H1_CUMULATIVE_TTM",
        "accession": accessions,
        "fetched_at": FETCHED_AT,
    } for metric, value in values.items()]
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if len(facts) != 4 or set(facts["metric"]) != TARGET_METRICS:
        raise RuntimeError("NCTY recovery must contain the four-field TTM bundle")
    return facts


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"NCTY audit binding changed: {actual_sha}")
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
            raise RuntimeError("NCTY remains in the current financial priorities")
        return {
            "path": str(path), "sha256": expected_sha256,
            "remaining_observation_count": 0, "status": "RECOVERED",
        }
    if len(rows) != 1 or set(rows["scenario"]) != {
        "liq2000000-age150-growth"
    }:
        raise RuntimeError("NCTY baseline audit scenario changed")
    expected = {
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 1,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 0,
    }
    for column, value in expected.items():
        if not rows[column].eq(value).all():
            raise RuntimeError(f"NCTY baseline {column} changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("NCTY baseline signal date changed")
    return {
        "path": str(path),
        "sha256": expected_sha256,
        "missing_observation_count": 1,
        "classification": "foreign_h1_sec_exhibit_parser_omission",
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
        "decision": "recovered_exact_us_gaap_annual_h1_ttm_growth_bundle",
        "revenue_ttm_rmb": derived["current_ttm_rmb"]["revenue"],
        "net_income_ttm_rmb": derived["current_ttm_rmb"]["net_income"],
        "revenue_growth": derived["growth"]["revenue"],
        "net_income_growth": derived["growth"]["net_income"],
        "passes_revenue_growth_gate": derived["growth"]["revenue"] > 0,
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
            "remaining_observation_count": 1,
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
            "The four-field bundle uses only the five pre-signal SEC 20-F and "
            "6-K filings and exact FY-minus-H1-plus-H1 arithmetic in RMB. USD "
            "convenience translations, adjusted measures, estimates, and the "
            "post-signal FY2020 20-F are excluded. The recovered revenue growth "
            "is negative, so the package removes a missing-data ambiguity "
            "without making NCTY pass the positive revenue-growth gate."
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
    """Copy-on-write overlay of only NCTY's exact H1-derived TTM bundle."""
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
        raise RuntimeError("NCTY integration requires the quarterly schema")
    if len(incoming) != 4 or not _target_mask(incoming).all():
        raise RuntimeError("NCTY supplement scope is not the four-field TTM bundle")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(untouched) + 4:
        raise RuntimeError("NCTY overlay changed rows outside the bounded key space")

    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("NCTY integration source changed while being read")
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
