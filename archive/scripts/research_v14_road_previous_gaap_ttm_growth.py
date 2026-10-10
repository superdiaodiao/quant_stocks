#!/usr/bin/env python3
"""Recover ROAD's pre-signal TTM growth on one pre-ASC-606 basis."""

from __future__ import annotations

import argparse
from decimal import Decimal
import gzip
import hashlib
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


TICKER = "ROAD"
CIK = 1_718_227
CURRENCY = "USD"
SOURCE_SCALE = 1_000
FISCAL_END = "2019-06-30"
AVAILABLE_DATE = "2019-08-09"
SIGNAL_DATE = "2019-09-30"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/road_previous_gaap_ttm_growth")
SEC_HEADERS = {"User-Agent": "quant-stocks-research contact@example.com"}

COMPANYFACTS_PATH = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001718227.json.gz"
)
COMPANYFACTS_SHA256 = (
    "83bce24fb689a369fcf3382c471d1ee6fd56953d9461bdfeecbfb054ce245936"
)
BASE_CANDIDATE_DIR = Path(
    "output/research_only/v14/"
    "candidate_fundamentals_v14_checkpoint_20260829_stne_q3"
)
BASELINE_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_stne_q3_recovered_financial_priorities.csv"
)
EXPECTED_BASELINE_AUDIT_SHA256 = (
    "a48029b30e5835eb8ae0b240c7526589696d44cf28a64e286222f26be09d25ed"
)
CURRENT_AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_road_previous_gaap_ttm_recovered_financial_priorities.csv"
)
EXPECTED_CURRENT_AUDIT_SHA256 = (
    "7277e53396d47f2a23fe34958cc04075425effdf9aed3b741b918cded8e2cc20"
)

TRANSITION_SOURCE = {
    "role": "m9_2019_previous_gaap_transition_operands",
    "form": "10-Q",
    "filed": AVAILABLE_DATE,
    "accepted_at": "2019-08-09T20:44:45Z",
    "accession": "0001718227-19-000064",
    "document": "road-20190630.htm",
    "expected_sha256": (
        "42d77fae6946831bca6c724182874114ce39ce9f488d82e8a9718a84bd946731"
    ),
}
TRANSITION_TEXT_CHECKS = (
    "using the modified retrospective approach",
    "did not result in a material impact that required recognition of a cumulative adjustment",
    "Without Application of ASC 606",
    "For the Nine Months Ended June 30, 2019",
)
TRANSITION_EXPECTED_USD_THOUSANDS = {
    "revenue": {"as_reported": 545_921, "impact": -1_321, "previous_gaap": 547_242},
    "net_income": {"as_reported": 26_568, "impact": -87, "previous_gaap": 26_655},
}
COMPANYFACTS_COORDINATES = {
    "fy2017": {
        "start": "2016-10-01", "end": "2017-09-30",
        "accession": "0001718227-18-000014", "filed": "2018-12-14",
        "form": "10-K",
    },
    "fy2018": {
        "start": "2017-10-01", "end": "2018-09-30",
        "accession": "0001718227-18-000014", "filed": "2018-12-14",
        "form": "10-K",
    },
    "m9_2017": {
        "start": "2016-10-01", "end": "2017-06-30",
        "accession": "0001193125-18-248680", "filed": "2018-08-14",
        "form": "10-Q",
    },
    "m9_2018": {
        "start": "2017-10-01", "end": "2018-06-30",
        "accession": "0001193125-18-248680", "filed": "2018-08-14",
        "form": "10-Q",
    },
}
EXPECTED_COMPANYFACTS_USD = {
    "fy2017": {"revenue": 568_212_000, "net_income": 26_040_000},
    "fy2018": {"revenue": 680_096_000, "net_income": 50_791_000},
    "m9_2017": {"revenue": 380_585_000, "net_income": 13_773_000},
    "m9_2018": {"revenue": 464_395_000, "net_income": 35_647_000},
}
EXPECTED_TTM_USD = {
    "prior": {"revenue": 652_022_000, "net_income": 47_914_000},
    "current": {"revenue": 762_943_000, "net_income": 41_799_000},
}
EXPECTED_GROWTH = {
    "revenue": 0.1701184929342875,
    "net_income": -0.12762449388487707,
}
TARGET_METRICS = frozenset(
    {"revenue_ttm", "net_income_ttm", "revenue_growth", "net_income_growth"}
)
AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", 150),
    ("liq2000000-age365-growth", 365),
    ("liq2000000-age550-growth", 550),
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _source_url(source: dict = TRANSITION_SOURCE) -> str:
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
    raise RuntimeError(f"failed to download locked ROAD source: {url}") from error


def _normalize(value: object) -> str:
    return " ".join(
        str(value).replace("\xa0", " ").replace("\u200b", " ").split()
    )


def _html_text(payload: bytes) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(payload, "lxml")
    return _normalize(soup.get_text(" ", strip=True))


def validate_source_lock(source: dict | None = None) -> None:
    document = TRANSITION_SOURCE if source is None else source
    for field in ("form", "accepted_at", "accession", "document"):
        if document[field] != TRANSITION_SOURCE[field]:
            raise ValueError(f"ROAD source changed locked identity field {field}")
    if document["filed"] > SIGNAL_DATE:
        raise ValueError("ROAD source violates the PIT cutoff")
    if document["filed"] != AVAILABLE_DATE:
        raise ValueError("ROAD fact availability must equal the 10-Q filing date")
    if not re.fullmatch(r"[0-9a-f]{64}", document["expected_sha256"]):
        raise ValueError("ROAD source has an invalid SHA-256")


def verify_transition_values(payload: bytes) -> dict:
    text = _html_text(payload)
    pattern = re.compile(
        r"For the Nine Months Ended June 30, 2019\s+"
        r"Revenues\s+\$\s+545,921\s+\$\s+\(\s*1,321\s*\)\s+"
        r"\$\s+547,242.{0,500}?"
        r"Net income\s+\$\s+26,568\s+\$\s+\(\s*87\s*\)\s+\$\s+26,655"
    )
    if not pattern.search(text):
        raise RuntimeError("ROAD previous-GAAP transition table changed")
    return {
        "period": "nine_months_ended_2019-06-30",
        "unit": "USD_thousands",
        "values": TRANSITION_EXPECTED_USD_THOUSANDS,
        "identity_check": "as_reported + impact = previous_gaap",
    }


def prepare_verified_transition_source(output_dir: Path) -> tuple[dict, dict]:
    validate_source_lock()
    path = output_dir / "sources" / TRANSITION_SOURCE["document"]
    downloaded = False
    if path.exists():
        payload = path.read_bytes()
    else:
        payload = _download_bytes(_source_url())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        downloaded = True
    actual_sha = _sha256_bytes(payload)
    if actual_sha != TRANSITION_SOURCE["expected_sha256"]:
        raise RuntimeError(f"ROAD source SHA-256 changed: {actual_sha}")
    text = _html_text(payload).casefold()
    missing = [
        fragment for fragment in TRANSITION_TEXT_CHECKS
        if _normalize(fragment).casefold() not in text
    ]
    if missing:
        raise RuntimeError(f"ROAD transition source text changed: {missing}")
    provenance = {
        **TRANSITION_SOURCE,
        "url": _source_url(),
        "local_path": str(path),
        "actual_sha256": actual_sha,
        "bytes": len(payload),
        "downloaded": downloaded,
    }
    return provenance, verify_transition_values(payload)


def _load_companyfacts(path: Path = COMPANYFACTS_PATH) -> tuple[dict, dict]:
    actual_sha = _sha256(path)
    if actual_sha != COMPANYFACTS_SHA256:
        raise RuntimeError(f"ROAD Company Facts SHA-256 changed: {actual_sha}")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    payload = wrapper["payload"]
    if int(payload["cik"]) != CIK:
        raise RuntimeError("ROAD Company Facts CIK changed")
    return wrapper, payload


def _exact_fact(payload: dict, *, concept: str, coordinate: dict) -> int:
    records = payload["facts"]["us-gaap"][concept]["units"][CURRENCY]
    matches = [
        record for record in records
        if record.get("start") == coordinate["start"]
        and record.get("end") == coordinate["end"]
        and record.get("accn") == coordinate["accession"]
        and record.get("filed") == coordinate["filed"]
        and record.get("form") == coordinate["form"]
    ]
    values = {int(record["val"]) for record in matches}
    if len(values) != 1:
        raise RuntimeError(
            f"ROAD exact {concept} fact changed for {coordinate}: {values}"
        )
    return values.pop()


def companyfacts_operands(payload: dict) -> dict:
    concepts = {"revenue": "Revenues", "net_income": "NetIncomeLoss"}
    result = {
        label: {
            metric: _exact_fact(payload, concept=concept, coordinate=coordinate)
            for metric, concept in concepts.items()
        }
        for label, coordinate in COMPANYFACTS_COORDINATES.items()
    }
    if result != EXPECTED_COMPANYFACTS_USD:
        raise RuntimeError(f"ROAD locked Company Facts operands changed: {result}")
    return result


def _growth(current: int, prior: int) -> float:
    if prior == 0:
        raise ValueError("ROAD TTM growth denominator cannot be zero")
    return float((Decimal(current) - Decimal(prior)) / abs(Decimal(prior)))


def ttm_evidence(payload: dict | None = None) -> dict:
    if payload is None:
        _, payload = _load_companyfacts()
    operands = companyfacts_operands(payload)
    m9_2019 = {
        metric: values["previous_gaap"] * SOURCE_SCALE
        for metric, values in TRANSITION_EXPECTED_USD_THOUSANDS.items()
    }
    prior = {
        metric: operands["fy2017"][metric] - operands["m9_2017"][metric]
        + operands["m9_2018"][metric]
        for metric in ("revenue", "net_income")
    }
    current = {
        metric: operands["fy2018"][metric] - operands["m9_2018"][metric]
        + m9_2019[metric]
        for metric in ("revenue", "net_income")
    }
    if {"prior": prior, "current": current} != EXPECTED_TTM_USD:
        raise RuntimeError("ROAD previous-GAAP TTM arithmetic changed")
    growth = {
        metric: _growth(current[metric], prior[metric])
        for metric in ("revenue", "net_income")
    }
    for metric, expected in EXPECTED_GROWTH.items():
        if abs(growth[metric] - expected) > 1e-15:
            raise RuntimeError(f"ROAD {metric} growth changed: {growth[metric]}")
    return {
        "ticker": TICKER,
        "currency": CURRENCY,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "signal_date": SIGNAL_DATE,
        "financial_age_days": int(
            (pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)).days
        ),
        "companyfacts_operands_usd": operands,
        "m9_2019_previous_gaap_usd": m9_2019,
        "derived": {
            "prior_ttm_usd": prior, "current_ttm_usd": current, "growth": growth,
        },
        "formulas": {
            "prior_ttm": "FY2017 - M9_2017 + M9_2018",
            "current_ttm": "FY2018 - M9_2018 + M9_2019_previous_GAAP",
            "growth": "(current_ttm - prior_ttm) / abs(prior_ttm)",
        },
        "accounting_boundary": {
            "homogeneous_previous_gaap_basis": True,
            "asc606_modified_retrospective_as_reported_values_excluded": True,
            "issuer_disclosed_transition_table_only": True,
            "estimates_and_post_signal_filings_excluded": True,
        },
    }


def strict_quarterly_facts(payload: dict | None = None) -> pd.DataFrame:
    evidence = ttm_evidence(payload)["derived"]
    values = {
        "revenue_ttm": evidence["current_ttm_usd"]["revenue"],
        "net_income_ttm": evidence["current_ttm_usd"]["net_income"],
        "revenue_growth": evidence["growth"]["revenue"],
        "net_income_growth": evidence["growth"]["net_income"],
    }
    accessions = "+".join(
        [
            COMPANYFACTS_COORDINATES["fy2017"]["accession"],
            COMPANYFACTS_COORDINATES["m9_2017"]["accession"],
            TRANSITION_SOURCE["accession"],
        ]
    )
    facts = pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": metric,
        "value": value,
        "taxonomy": "us-gaap",
        "concept": f"road_previous_gaap_annual_m9_ttm:{metric}:USD",
        "form": "10-K_PLUS_10-Q_M9_PREVIOUS_GAAP",
        "accession": accessions,
        "fetched_at": FETCHED_AT,
    } for metric, value in values.items()], columns=OUTPUT_COLUMNS)
    if len(facts) != 4 or set(facts["metric"]) != TARGET_METRICS:
        raise RuntimeError("ROAD recovery must contain the four-field TTM bundle")
    return facts


def _audit_rows(path: Path, expected_sha256: str) -> pd.DataFrame:
    actual_sha = _sha256(path)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"ROAD audit binding changed: {actual_sha}")
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
            raise RuntimeError("ROAD remains in the current financial priorities")
        return {
            "path": str(path), "sha256": expected_sha256,
            "remaining_observation_count": 0, "status": "RECOVERED",
        }
    scenarios = {scenario for scenario, _age in AUDIT_OBSERVATIONS}
    if len(rows) != 3 or set(rows["scenario"]) != scenarios:
        raise RuntimeError("ROAD baseline audit scenarios changed")
    expected = {
        "missing_signal_count": 1,
        "no_raw_pit_financial_facts_signal_count": 0,
        "insufficient_growth_history_signal_count": 1,
        "stale_growth_snapshot_signal_count": 0,
    }
    for column, value in expected.items():
        if not rows[column].eq(value).all():
            raise RuntimeError(f"ROAD baseline {column} changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("ROAD baseline signal date changed")
    return {
        "path": str(path), "sha256": expected_sha256,
        "missing_observation_count": 3,
        "classification": "modified_retrospective_transition_table_parser_omission",
    }


def recovered_observations(payload: dict | None = None) -> pd.DataFrame:
    evidence = ttm_evidence(payload)
    growth = evidence["derived"]["growth"]
    return pd.DataFrame([{
        "scenario": scenario,
        "ticker": TICKER,
        "signal_date": SIGNAL_DATE,
        "maximum_age_days": age,
        "resolved": True,
        "decision": "recovered_homogeneous_previous_gaap_annual_m9_ttm",
        "revenue_growth": growth["revenue"],
        "net_income_growth": growth["net_income"],
        "passes_net_income_growth_gate": growth["net_income"] >= 0.15,
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
    source, transition_verification = prepare_verified_transition_source(output_dir)
    wrapper, payload = _load_companyfacts()
    evidence = ttm_evidence(payload)
    facts = strict_quarterly_facts(payload)
    observations = recovered_observations(payload)
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
            "path": str(current_audit_path), "sha256": expected_current_audit_sha256,
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
        "companyfacts_source": {
            "path": str(COMPANYFACTS_PATH), "sha256": COMPANYFACTS_SHA256,
            "source_url": wrapper.get("source_url"),
        },
        "transition_source": source,
        "transition_value_verification": transition_verification,
        "accounting_boundary": evidence["accounting_boundary"],
        "audit_binding": {
            "baseline": baseline, "current": current,
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
                "sha256": _sha256(observations_path), "row_count": len(observations),
            },
        },
        "guardrail": (
            "Uses only pre-signal annual and nine-month operands on the "
            "homogeneous previous-GAAP basis. The 2019 Q3 10-Q expressly used "
            "modified-retrospective ASC 606 adoption and disclosed the exact "
            "without-ASC-606 M9 revenue and net income; those issuer values "
            "replace the incomparable as-reported M9 values. Estimates, "
            "cross-basis arithmetic, and post-signal filings are excluded. "
            "The resulting net-income growth is negative, so ROAD is not "
            "eligible. Formal financial files are unchanged."
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
    base_dir = Path(base_dir)
    supplement_dir = Path(supplement_dir)
    output_dir = Path(output_dir)
    inputs = (
        base_dir / "annual.csv", base_dir / "quarterly.csv",
        base_dir / "manifest.json", supplement_dir / "strict_quarterly_facts.csv",
        supplement_dir / "manifest.json",
    )
    bound = {path: _sha256(path) for path in inputs}
    base = pd.read_csv(inputs[1])
    incoming = pd.read_csv(inputs[3])
    if list(base.columns) != OUTPUT_COLUMNS or list(incoming.columns) != OUTPUT_COLUMNS:
        raise RuntimeError("ROAD integration requires the quarterly schema")
    if len(incoming) != 4 or not _target_mask(incoming).all():
        raise RuntimeError("ROAD supplement scope is not the four-field TTM bundle")
    target = _target_mask(base)
    replaced = base.loc[target].copy()
    untouched = base.loc[~target].copy()
    merged = pd.concat([untouched, incoming], ignore_index=True).sort_values(
        ["ticker", "fiscal_end", "metric", "available_date"]
    ).reset_index(drop=True)
    if len(merged) != len(base) - len(replaced) + len(incoming):
        raise RuntimeError("ROAD integration row count changed unexpectedly")
    output_dir.mkdir(parents=True, exist_ok=True)
    annual_path = output_dir / "annual.csv"
    quarterly_path = output_dir / "quarterly.csv"
    shutil.copyfile(inputs[0], annual_path)
    merged.to_csv(quarterly_path, index=False)
    if {path: _sha256(path) for path in inputs} != bound:
        raise RuntimeError("ROAD integration source changed while being read")
    report = {
        "schema_version": 1, "research_only": True,
        "formal_financials_modified": False, "release_status": "BLOCKED",
        "promotion_eligible": False, "overlay_ticker": TICKER,
        "overlay_fiscal_end": FISCAL_END,
        "overlay_metrics": sorted(TARGET_METRICS),
        "removed_conflicting_rows": len(replaced),
        "inserted_strict_rows": len(incoming),
        "base": {"path": str(base_dir), "sha256": {
            str(path): digest for path, digest in bound.items()
        }},
        "outputs": {
            "annual": str(annual_path), "annual_sha256": _sha256(annual_path),
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
            base_dir=args.base_dir, supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
