#!/usr/bin/env python3
"""Recover source-locked GLPG direct TTM loss and Q3 growth facts."""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
import warnings
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


TICKER = "GLPG"
CIK = 1_421_876
CURRENCY = "EUR"
SOURCE_SCALE = Decimal(1_000_000)
FETCHED_AT = pd.Timestamp("2026-08-27")
OUTPUT_DIR = Path("output/research_only/v14/glpg_direct_ttm_loss")
AUDIT_PATH = Path("output/research_only/v14/checkpoint_20260827_sy.json")
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

SOURCE_DOCUMENTS = {
    "fy2018_xbrl": {
        "accession": "0001558370-19-002655",
        "filed": "2019-03-29",
        "document": "glpg-20181231.xml",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1421876/"
            "000155837019002655/glpg-20181231.xml"
        ),
        "expected_sha256": (
            "811680469fc0e5349d92966f62de54cc7d723650243829a2e4913b7810dae8c4"
        ),
        "local_path": "sources/glpg-20181231.xml",
    },
    "h1_2019_xbrl": {
        "accession": "0001558370-19-006344",
        "filed": "2019-07-25",
        "document": "glpg-20190630.xml",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1421876/"
            "000155837019006344/glpg-20190630.xml"
        ),
        "expected_sha256": (
            "3175de74d5a202740357ee909d506df15159159945f01b64b7dd9d0a116768c3"
        ),
        "local_path": "sources/glpg-20190630.xml",
    },
    "q3_2018_exhibit": {
        "accession": "0001193125-18-309116",
        "filed": "2018-10-26",
        "document": "d644929dex991.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1421876/"
            "000119312518309116/d644929dex991.htm"
        ),
        "expected_sha256": (
            "0e77afd3d23dcaaa5d819b3d92c70bc434be532c7c859d7d47ea90faaced41f5"
        ),
        "local_path": "sources/d644929dex991.htm",
    },
    "q3_2019_exhibit": {
        "accession": "0001193125-19-274853",
        "filed": "2019-10-25",
        "document": "d813638dex991.htm",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1421876/"
            "000119312519274853/d813638dex991.htm"
        ),
        "expected_sha256": (
            "7bf43547c572c5f08cd623442b4092e4e08224fbe017c0f8bae65373ac72f4d0"
        ),
        "local_path": "sources/d813638dex991.htm",
    },
}

ANNUAL_XBRL_EXPECTED = {
    "fy2017": {
        "context": "Duration_1_1_2017_To_12_31_2017",
        "revenue": Decimal(155_918_000),
        "net_income": Decimal(-115_704_000),
    },
    "fy2018": {
        "context": "Duration_1_1_2018_To_12_31_2018",
        "revenue": Decimal(317_845_000),
        "net_income": Decimal(-29_259_000),
    },
}
H1_XBRL_EXPECTED = {
    "h1_2018": {
        "context": "Duration_1_1_2018_To_6_30_2018",
        "net_income": Decimal(-59_056_000),
    },
    "h1_2019": {
        "context": "Duration_1_1_2019_To_6_30_2019",
        "net_income": Decimal(-95_905_000),
    },
}
Q3_EXHIBIT_EXPECTED = {
    "q3_2018_exhibit": {
        "periods": ("m9_2018", "m9_2017"),
        "revenue": (Decimal("205.1"), Decimal("106.4")),
        "net_income": (Decimal("-44.2"), Decimal("-85.9")),
    },
    "q3_2019_exhibit": {
        "periods": ("m9_2019", "m9_2018"),
        "revenue": (Decimal("752.5"), Decimal("205.1")),
        "net_income": (Decimal("265.3"), Decimal("-44.2")),
    },
}

EXPECTED_TTM = {
    "2018-12-31": Decimal(-29_259_000),
    "2019-06-30": Decimal(-66_108_000),
}
EXPECTED_Q3_GROWTH = {
    "revenue": {
        "prior_ttm": Decimal(254_618_000),
        "current_ttm": Decimal(865_245_000),
        "growth": Decimal(610_627_000) / Decimal(254_618_000),
    },
    "net_income": {
        "prior_ttm": Decimal(-74_004_000),
        "current_ttm": Decimal(280_241_000),
        "growth": Decimal(354_245_000) / Decimal(74_004_000),
    },
}
AUDIT_OBSERVATIONS = tuple(
    (f"liq{liquidity}-age150-growth", signal_date, 150)
    for liquidity in (10_000_000, 2_000_000)
    for signal_date in ("2019-12-31", "2020-01-31")
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalize_text(value: str) -> str:
    return " ".join(
        value.replace("\xa0", " ")
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .split()
    ).casefold()


def _decimal_cell(value: str) -> Decimal | None:
    text = "".join(value.replace("\xa0", " ").split()).replace(",", "")
    if re.fullmatch(r"\(?-?\d+(?:\.\d+)?\)?", text) is None:
        return None
    negative = text.startswith(("(", "-"))
    number = Decimal(re.sub(r"[^0-9.]", "", text))
    return -number if negative else number


def _matching_rows(raw: bytes, label: str) -> list[tuple[Decimal, ...]]:
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    normalized_label = _normalize_text(label)
    matches = []
    for row in soup.find_all("tr"):
        cells = row.find_all(("td", "th"), recursive=False)
        labels = [_normalize_text(cell.get_text(" ", strip=True)) for cell in cells]
        first_label = next((item for item in labels if item), "")
        if first_label != normalized_label:
            continue
        values = tuple(
            parsed
            for cell in cells
            if (parsed := _decimal_cell(cell.get_text(" ", strip=True)))
            is not None
        )
        if values:
            matches.append(values)
    return matches


def _extract_xbrl_fact(raw: bytes, context: str, concept: str) -> Decimal:
    root = ET.fromstring(raw)
    values = {
        Decimal(element.text)
        for element in root.iter()
        if element.tag.endswith("}" + concept)
        and element.attrib.get("contextRef") == context
        and element.text is not None
    }
    if len(values) != 1:
        raise RuntimeError(
            f"expected one GLPG {concept} fact for {context}, found {values}"
        )
    return values.pop()


def validate_source_lock(
    documents: dict[str, dict] | None = None,
) -> None:
    sources = SOURCE_DOCUMENTS if documents is None else documents
    if set(sources) != set(SOURCE_DOCUMENTS):
        raise ValueError("GLPG source set changed")
    for source_id, source in sources.items():
        compact_accession = source["accession"].replace("-", "")
        if compact_accession not in source["url"]:
            raise ValueError(f"GLPG accession URL drift: {source_id}")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"GLPG document URL drift: {source_id}")
        local_path = Path(source["local_path"])
        if local_path.is_absolute() or ".." in local_path.parts:
            raise ValueError(f"unsafe GLPG local path: {source_id}")
        if re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]) is None:
            raise ValueError(f"invalid GLPG source SHA-256: {source_id}")
    if sources["q3_2019_exhibit"]["filed"] != "2019-10-25":
        raise ValueError("GLPG Q3 growth availability changed")


def verify_source_evidence(raw_by_source: dict[str, bytes]) -> dict:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("raw GLPG source set does not match source lock")

    annual = {}
    annual_raw = raw_by_source["fy2018_xbrl"]
    for period, expected in ANNUAL_XBRL_EXPECTED.items():
        actual = {
            "revenue": _extract_xbrl_fact(
                annual_raw, expected["context"], "RevenueAndOperatingIncome"
            ),
            "net_income": _extract_xbrl_fact(
                annual_raw, expected["context"], "ProfitLoss"
            ),
        }
        for metric in ("revenue", "net_income"):
            if actual[metric] != expected[metric]:
                raise RuntimeError(
                    f"GLPG annual {period} {metric} changed: {actual[metric]}"
                )
        annual[period] = actual

    h1 = {}
    h1_raw = raw_by_source["h1_2019_xbrl"]
    for period, expected in H1_XBRL_EXPECTED.items():
        actual = _extract_xbrl_fact(
            h1_raw, expected["context"], "ProfitLoss"
        )
        if actual != expected["net_income"]:
            raise RuntimeError(f"GLPG {period} ProfitLoss changed: {actual}")
        h1[period] = {"net_income": actual}

    cumulative = {"revenue": {}, "net_income": {}}
    q3_rows = []
    for source_id, expected in Q3_EXHIBIT_EXPECTED.items():
        raw = raw_by_source[source_id]
        for metric, label in (
            ("revenue", "Revenues"),
            ("net_income", "Net result for the period"),
        ):
            expected_values = expected[metric]
            matches = _matching_rows(raw, label)
            if expected_values not in matches:
                raise RuntimeError(
                    f"GLPG {source_id} {label} changed: {matches[:5]}"
                )
            for period, value in zip(
                expected["periods"], expected_values, strict=True
            ):
                base_value = value * SOURCE_SCALE
                previous = cumulative[metric].get(period)
                if previous is not None and previous != base_value:
                    raise RuntimeError(
                        f"GLPG comparative drift for {metric} {period}"
                    )
                cumulative[metric][period] = base_value
            q3_rows.append({
                "source_id": source_id,
                "metric": metric,
                "line_item": label,
                "periods": list(expected["periods"]),
                "values_eur_millions": [str(value) for value in expected_values],
            })

    return {
        "annual_eur": annual,
        "h1_eur": h1,
        "q3_cumulative_eur": cumulative,
        "q3_source_rows": q3_rows,
    }


def _derive_values(evidence: dict) -> dict:
    annual = evidence["annual_eur"]
    h1 = evidence["h1_eur"]
    cumulative = evidence["q3_cumulative_eur"]
    direct_loss = {
        "2018-12-31": annual["fy2018"]["net_income"],
        "2019-06-30": (
            annual["fy2018"]["net_income"]
            - h1["h1_2018"]["net_income"]
            + h1["h1_2019"]["net_income"]
        ),
    }
    if direct_loss != EXPECTED_TTM:
        raise RuntimeError(f"GLPG direct TTM loss drift: {direct_loss}")

    q3_growth = {}
    for metric in ("revenue", "net_income"):
        prior_ttm = (
            annual["fy2017"][metric]
            - cumulative[metric]["m9_2017"]
            + cumulative[metric]["m9_2018"]
        )
        current_ttm = (
            annual["fy2018"][metric]
            - cumulative[metric]["m9_2018"]
            + cumulative[metric]["m9_2019"]
        )
        growth = (current_ttm - prior_ttm) / abs(prior_ttm)
        q3_growth[metric] = {
            "prior_ttm": prior_ttm,
            "current_ttm": current_ttm,
            "growth": growth,
        }
    if q3_growth != EXPECTED_Q3_GROWTH:
        raise RuntimeError(f"GLPG Q3 growth drift: {q3_growth}")
    return {"direct_loss": direct_loss, "q3_growth": q3_growth}


def strict_quarterly_facts(
    raw_by_source: dict[str, bytes],
) -> tuple[pd.DataFrame, dict]:
    evidence = verify_source_evidence(raw_by_source)
    derived = _derive_values(evidence)
    records = [
        {
            "ticker": TICKER,
            "fiscal_end": "2018-12-31",
            "available_date": "2019-03-29",
            "metric": "net_income_ttm",
            "value": float(derived["direct_loss"]["2018-12-31"]),
            "taxonomy": "ifrs-full",
            "concept": "glpg_exact_ttm:ProfitLoss:EUR",
            "form": "20-F",
            "accession": SOURCE_DOCUMENTS["fy2018_xbrl"]["accession"],
            "fetched_at": FETCHED_AT,
        },
        {
            "ticker": TICKER,
            "fiscal_end": "2019-06-30",
            "available_date": "2019-07-25",
            "metric": "net_income_ttm",
            "value": float(derived["direct_loss"]["2019-06-30"]),
            "taxonomy": "ifrs-full",
            "concept": "glpg_exact_ttm:ProfitLoss:EUR",
            "form": "20-F_PLUS_6-K_H1_CUMULATIVE_TTM",
            "accession": "+".join((
                SOURCE_DOCUMENTS["fy2018_xbrl"]["accession"],
                SOURCE_DOCUMENTS["h1_2019_xbrl"]["accession"],
            )),
            "fetched_at": FETCHED_AT,
        },
    ]
    q3_accession = "+".join(
        SOURCE_DOCUMENTS[source_id]["accession"]
        for source_id in (
            "fy2018_xbrl", "q3_2018_exhibit", "q3_2019_exhibit"
        )
    )
    common = {
        "ticker": TICKER,
        "fiscal_end": "2019-09-30",
        "available_date": "2019-10-25",
        "taxonomy": "ifrs-full",
        "form": "20-F_PLUS_6-K_Q3_CUMULATIVE_TTM",
        "accession": q3_accession,
        "fetched_at": FETCHED_AT,
    }
    for metric in ("revenue", "net_income"):
        values = derived["q3_growth"][metric]
        records.extend((
            {
                **common,
                "metric": f"{metric}_ttm",
                "value": float(values["current_ttm"]),
                "concept": f"glpg_exact_ttm:{metric}:EUR",
            },
            {
                **common,
                "metric": f"{metric}_growth",
                "value": float(values["growth"]),
                "concept": f"glpg_exact_ttm:{metric}:EUR",
            },
        ))
    facts = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    facts = facts.sort_values(["available_date", "metric"]).reset_index(drop=True)
    return facts, {
        **evidence,
        **derived,
        "guardrail": (
            "All operands are reported IFRS EUR values. Direct Q3 growth uses "
            "FY minus the prior nine-month cumulative period plus the current "
            "nine-month cumulative period. No quarter splitting, FX, non-GAAP, "
            "or later filing is used."
        ),
    }


def resolved_audit_observations(evidence: dict) -> list[dict]:
    growth = evidence["q3_growth"]
    results = []
    for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        age = int(
            (pd.Timestamp(signal_date) - pd.Timestamp("2019-10-25")).days
        )
        results.append({
            "scenario": scenario,
            "signal_date": signal_date,
            "maximum_age_days": maximum_age_days,
            "financial_age_days": age,
            "resolved": age <= maximum_age_days,
            "decision": (
                "pass_growth_filters"
                if growth["revenue"]["growth"] >= Decimal("0.10")
                and growth["net_income"]["growth"] >= Decimal("0.25")
                else "fail_growth_filters"
            ),
            "revenue_growth": float(growth["revenue"]["growth"]),
            "net_income_ttm": float(growth["net_income"]["current_ttm"]),
            "net_income_growth": float(growth["net_income"]["growth"]),
        })
    return results


def prepare_verified_sources(
    output_dir: Path,
) -> tuple[dict[str, dict], dict[str, bytes]]:
    validate_source_lock()
    provenance = {}
    raw_by_source = {}
    for source_id, source in SOURCE_DOCUMENTS.items():
        local_path = output_dir / source["local_path"]
        if local_path.exists():
            raw = local_path.read_bytes()
            downloaded = False
        else:
            raw = _download_source(source["url"])
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(raw)
            downloaded = True
        actual_sha = _sha256_bytes(raw)
        if actual_sha != source["expected_sha256"]:
            raise RuntimeError(
                f"GLPG source SHA-256 mismatch for {source_id}: {actual_sha}"
            )
        raw_by_source[source_id] = raw
        provenance[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha,
            "bytes": len(raw),
            "downloaded": downloaded,
        }
    return provenance, raw_by_source


def build(
    output_dir: Path = OUTPUT_DIR,
    audit_path: Path = AUDIT_PATH,
) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    provenance, raw_by_source = prepare_verified_sources(output_dir)
    facts, evidence = strict_quarterly_facts(raw_by_source)
    resolutions = resolved_audit_observations(evidence)

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_evidence.json"
    resolutions_path = output_dir / "resolved_observations.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    resolutions_path.write_text(
        json.dumps(resolutions, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    audit_path = Path(audit_path)
    report = {
        "schema_version": 2,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": TICKER,
        "cik": CIK,
        "currency": CURRENCY,
        "accepted_fact_count": len(facts),
        "resolved_audit_observation_count": len(resolutions),
        "sources": provenance,
        "audit_binding": {
            "path": str(audit_path),
            "sha256": _sha256_path(audit_path),
        },
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256_path(facts_path),
            },
            "exact_ttm_evidence": {
                "path": str(evidence_path),
                "sha256": _sha256_path(evidence_path),
            },
            "resolved_observations": {
                "path": str(resolutions_path),
                "sha256": _sha256_path(resolutions_path),
            },
        },
        "guardrail": evidence["guardrail"],
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
    parser.add_argument("--audit-path", type=Path, default=AUDIT_PATH)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = build(args.output_dir, args.audit_path)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
