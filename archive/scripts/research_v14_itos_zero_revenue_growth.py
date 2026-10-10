#!/usr/bin/env python3
"""Source-lock ITOS's exact TTM profit and zero revenue denominator.

The 2021-Q3 filing proves a positive current TTM profit and a new license-
revenue event.  Its prior comparable TTM revenue is exactly zero, however, so
the frozen revenue-growth percentage is undefined.  This package records that
negative evidence and deliberately emits no candidate fundamentals.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


TICKER = "ITOS"
CIK = 1_808_865
SIGNAL_DATE = "2021-12-31"
LATEST_VALID_AVAILABLE_DATE = "2021-11-10"
FETCHED_AT = "2026-08-29"
OUTPUT_DIR = Path("output/research_only/v14/itos_zero_revenue_growth")
AUDIT_PATH = Path(
    "output/research_only/v14/"
    "checkpoint_20260829_sohu_restated_quarters_recovered_"
    "financial_priorities.csv"
)
EXPECTED_AUDIT_SHA256 = (
    "da109905c70d36898fe8f2689275fc272e69d69a0451f9866de59e6eb8beedca"
)
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

SOURCE_DOCUMENTS = (
    {
        "source_id": "q3_2020",
        "role": "2020 and 2019 nine-month net-loss comparators",
        "form": "10-Q",
        "filed": "2020-11-12",
        "accepted_at": "2020-11-12T07:15:50Z",
        "accession": "0001564590-20-053269",
        "document": "itos-10q_20200930.htm",
        "expected_sha256": (
            "71ea64e1fb1a9c09a4cb76218d33a8edfaef3c79d0349275e71d4e69a6b299d6"
        ),
    },
    {
        "source_id": "fy2020",
        "role": "audited 2020 and 2019 annual net losses and zero revenue",
        "form": "10-K",
        "filed": "2021-03-24",
        "accepted_at": "2021-03-24T17:15:13Z",
        "accession": "0001564590-21-015146",
        "document": "itos-10k_20201231.htm",
        "expected_sha256": (
            "70de615265d7e7790f7f187c476ee27fce6af06a57603b3e46bf841d947c835a"
        ),
    },
    {
        "source_id": "q3_2021",
        "role": "2021-Q3 license revenue and nine-month profit comparators",
        "form": "10-Q",
        "filed": "2021-11-10",
        "accepted_at": "2021-11-10T16:11:29Z",
        "accession": "0001564590-21-056193",
        "document": "itos-10q_20210930.htm",
        "expected_sha256": (
            "4b5654544db0cce12ebb9411d2fd331a18b05ab662a4c1827ba1c95f653f80fe"
        ),
    },
)

SCENARIOS = (
    ("liq2000000-age150-growth", 150),
    ("liq2000000-age365-growth", 365),
    ("liq2000000-age550-growth", 550),
)

FY2019_NET_LOSS = -22_454_000
NINE_MONTH_2019_NET_LOSS = -17_078_000
FY2020_NET_LOSS = -38_033_000
NINE_MONTH_2020_NET_LOSS = -23_129_000
NINE_MONTH_2021_NET_INCOME = 29_649_000
CURRENT_REVENUE_TTM = 104_271_000
PRIOR_REVENUE_TTM = 0

Q4_2019_NET_LOSS = FY2019_NET_LOSS - NINE_MONTH_2019_NET_LOSS
Q4_2020_NET_LOSS = FY2020_NET_LOSS - NINE_MONTH_2020_NET_LOSS
CURRENT_NET_INCOME_TTM = Q4_2020_NET_LOSS + NINE_MONTH_2021_NET_INCOME
PRIOR_NET_INCOME_TTM = Q4_2019_NET_LOSS + NINE_MONTH_2020_NET_LOSS
NET_INCOME_GROWTH = (
    CURRENT_NET_INCOME_TTM - PRIOR_NET_INCOME_TTM
) / abs(PRIOR_NET_INCOME_TTM)

_SOURCE_GUARDS = {
    "q3_2020": (
        r"Net loss\s*\(\s*10,680\s*\)\s*\(\s*6,921\s*\)\s*"
        r"\(\s*23,129\s*\)\s*\(\s*17,078\s*\)",
    ),
    "fy2020": (
        r"Net loss\s*\$?\s*\(\s*38,033\s*\)\s*\$?\s*"
        r"\(\s*22,454\s*\)",
        r"never generated any revenue from product sales",
    ),
    "q3_2021": (
        r"License Revenue\s*\$?\s*104,271\s*\$?\s*[\u2014-]\s*"
        r"\$?\s*104,271\s*\$?\s*[\u2014-]",
        r"Net income \(loss\)\s*69,642\s*\(\s*10,680\s*\)\s*"
        r"29,649\s*\(\s*23,129\s*\)",
    ),
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _url(source: dict) -> str:
    accession = source["accession"].replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/{accession}/"
        f"{source['document']}"
    )


def _download(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _plain_text(raw: bytes) -> str:
    decoded = raw.decode("utf-8", errors="replace")
    decoded = re.sub(r"<script\b.*?</script>", " ", decoded, flags=re.I | re.S)
    decoded = re.sub(r"<style\b.*?</style>", " ", decoded, flags=re.I | re.S)
    decoded = re.sub(r"<[^>]+>", " ", decoded)
    return re.sub(r"\s+", " ", html.unescape(decoded)).strip()


def validate_source_lock() -> None:
    if [source["source_id"] for source in SOURCE_DOCUMENTS] != [
        "q3_2020",
        "fy2020",
        "q3_2021",
    ]:
        raise ValueError("ITOS source set changed")
    for source in SOURCE_DOCUMENTS:
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"invalid ITOS source SHA-256: {source['source_id']}")
        if source["accession"].replace("-", "") not in _url(source):
            raise ValueError(f"ITOS accession URL changed: {source['source_id']}")
        if source["filed"] >= SIGNAL_DATE:
            raise ValueError(f"ITOS source postdates signal: {source['source_id']}")


def verify_sources(payloads: list[bytes]) -> dict:
    """Verify exact SEC bytes and every statement row used in the TTM math."""
    validate_source_lock()
    if len(payloads) != len(SOURCE_DOCUMENTS):
        raise ValueError("ITOS source payload count changed")
    checks = []
    for source, raw in zip(SOURCE_DOCUMENTS, payloads, strict=True):
        actual_sha = _sha256(raw)
        if actual_sha != source["expected_sha256"]:
            raise RuntimeError(
                f"ITOS {source['source_id']} source SHA-256 mismatch: {actual_sha}"
            )
        text = _plain_text(raw)
        for pattern in _SOURCE_GUARDS[source["source_id"]]:
            if re.search(pattern, text, flags=re.I) is None:
                raise RuntimeError(
                    f"ITOS {source['source_id']} accounting guard changed"
                )
        checks.append({
            **source,
            "url": _url(source),
            "sha256": actual_sha,
            "bytes": len(raw),
            "guard_count": len(_SOURCE_GUARDS[source["source_id"]]),
        })
    evidence = ttm_evidence()
    if evidence["net_income"]["current_ttm"] != 14_745_000:
        raise RuntimeError("ITOS current net-income TTM changed")
    if evidence["net_income"]["prior_ttm"] != -28_505_000:
        raise RuntimeError("ITOS prior net-income TTM changed")
    return {"source_checks": checks, **evidence}


def ttm_evidence() -> dict:
    return {
        "units": "USD",
        "scale": 1,
        "net_income": {
            "q4_2019": Q4_2019_NET_LOSS,
            "q4_2020": Q4_2020_NET_LOSS,
            "current_ttm": CURRENT_NET_INCOME_TTM,
            "prior_ttm": PRIOR_NET_INCOME_TTM,
            "growth": NET_INCOME_GROWTH,
            "current_derivation": "FY2020 - 9M2020 + 9M2021",
            "prior_derivation": "FY2019 - 9M2019 + 9M2020",
        },
        "revenue": {
            "current_ttm": CURRENT_REVENUE_TTM,
            "prior_ttm": PRIOR_REVENUE_TTM,
            "growth": None,
            "reason": "growth percentage is undefined when prior TTM is zero",
        },
    }


def resolve_audit_observations() -> pd.DataFrame:
    available = pd.Timestamp(LATEST_VALID_AVAILABLE_DATE)
    rows = []
    for scenario, maximum_age_days in SCENARIOS:
        rows.append({
            "scenario": scenario,
            "ticker": TICKER,
            "signal_date": SIGNAL_DATE,
            "maximum_age_days": maximum_age_days,
            "latest_valid_fiscal_end": "2021-09-30",
            "latest_valid_available_date": LATEST_VALID_AVAILABLE_DATE,
            "financial_age_days": int(
                (pd.Timestamp(SIGNAL_DATE) - available).days
            ),
            "net_income_growth": NET_INCOME_GROWTH,
            "current_revenue_ttm": CURRENT_REVENUE_TTM,
            "prior_revenue_ttm": PRIOR_REVENUE_TTM,
            "revenue_growth": None,
            "resolved": False,
            "decision": "unrecoverable_zero_revenue_denominator",
            "reason": (
                "The exact prior TTM revenue is zero, so the frozen revenue-"
                "growth percentage is undefined even though profit growth is "
                "positive and the filing is timely."
            ),
        })
    return pd.DataFrame(rows)


def rejected_derivations() -> list[dict]:
    return [
        {
            "candidate": "treat 104.271m / 0 as a percentage growth rate",
            "rejected": True,
            "reason": "division by zero; percentage is undefined",
        },
        {
            "candidate": "replace the zero denominator with one dollar",
            "rejected": True,
            "reason": "fabricates a denominator and changes the source facts",
        },
        {
            "candidate": "accept profit growth without revenue growth",
            "rejected": True,
            "reason": "changes the frozen two-factor growth eligibility rule",
        },
    ]


def _validate_audit_binding(path: Path, expected_sha256: str) -> dict:
    actual_sha = _sha256(path.read_bytes())
    if actual_sha != expected_sha256:
        raise RuntimeError(f"ITOS audit binding changed: {actual_sha}")
    priorities = pd.read_csv(path)
    expected = {scenario for scenario, _ in SCENARIOS}
    rows = priorities.loc[
        priorities["ticker"].eq(TICKER)
        & priorities["scenario"].isin(expected)
    ].copy()
    if len(rows) != len(expected) or set(rows["scenario"]) != expected:
        raise RuntimeError("ITOS priority scenarios changed")
    if not rows["missing_signal_count"].eq(1).all():
        raise RuntimeError("ITOS priority missing-signal counts changed")
    if not rows["insufficient_growth_history_signal_count"].eq(1).all():
        raise RuntimeError("ITOS priority classification changed")
    if set(rows["first_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("ITOS first missing signal changed")
    if set(rows["last_missing_signal_date"]) != {SIGNAL_DATE}:
        raise RuntimeError("ITOS last missing signal changed")
    return {
        "path": str(path),
        "sha256": actual_sha,
        "scenario_count": len(rows),
        "missing_observation_count": len(rows),
        "signal_date": SIGNAL_DATE,
        "observed_classification": "insufficient_growth_history",
    }


def build(
    output_dir: Path = OUTPUT_DIR,
    audit_path: Path = AUDIT_PATH,
    expected_audit_sha256: str = EXPECTED_AUDIT_SHA256,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = output_dir / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    payloads = []
    source_outputs = []
    for source in SOURCE_DOCUMENTS:
        path = source_dir / source["document"]
        raw = path.read_bytes() if path.exists() else _download(_url(source))
        if _sha256(raw) != source["expected_sha256"]:
            raise RuntimeError(
                f"ITOS cached source SHA-256 mismatch: {source['source_id']}"
            )
        if not path.exists():
            path.write_bytes(raw)
        payloads.append(raw)
        source_outputs.append({
            **source,
            "url": _url(source),
            "local_path": str(path),
            "sha256": _sha256(raw),
            "bytes": len(raw),
        })

    evidence = verify_sources(payloads)
    audit_binding = _validate_audit_binding(
        Path(audit_path), expected_audit_sha256
    )
    observations = resolve_audit_observations()
    observations_path = output_dir / "unrecoverable_observations.csv"
    observations.to_csv(observations_path, index=False)
    rejected_path = output_dir / "rejected_derivations.json"
    rejected_path.write_text(
        json.dumps(rejected_derivations(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    accepted = pd.DataFrame(columns=OUTPUT_COLUMNS)
    accepted_path = output_dir / "accepted_candidate_facts.csv"
    accepted.to_csv(accepted_path, index=False)

    manifest = {
        "schema_version": 1,
        "research_only": True,
        "ticker": TICKER,
        "cik": CIK,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": False,
        "formal_financials_modified": False,
        "point_in_time_proven": True,
        "negative_evidence_source_locked": True,
        "recovery_classification": "UNRECOVERABLE_ZERO_REVENUE_DENOMINATOR",
        "candidate_rows_created": 0,
        "guardrail": (
            "Do not turn a zero revenue denominator into a percentage, "
            "fabricate a denominator, or waive the frozen revenue-growth gate."
        ),
        "evidence": evidence,
        "audit_binding": audit_binding,
        "outputs": {
            "accepted_candidate_facts": {
                "path": str(accepted_path),
                "sha256": _sha256(accepted_path.read_bytes()),
                "row_count": 0,
            },
            "unrecoverable_observations": {
                "path": str(observations_path),
                "sha256": _sha256(observations_path.read_bytes()),
                "row_count": len(observations),
            },
            "rejected_derivations": {
                "path": str(rejected_path),
                "sha256": _sha256(rejected_path.read_bytes()),
            },
            "sources": source_outputs,
        },
        "fetched_at": FETCHED_AT,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**manifest, "manifest": str(manifest_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--audit-path", type=Path, default=AUDIT_PATH)
    parser.add_argument(
        "--expected-audit-sha256", default=EXPECTED_AUDIT_SHA256
    )
    args = parser.parse_args()
    report = build(
        output_dir=args.output_dir,
        audit_path=args.audit_path,
        expected_audit_sha256=args.expected_audit_sha256,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
