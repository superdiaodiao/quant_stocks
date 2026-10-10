#!/usr/bin/env python3
"""Build source-locked exact-TTM loss evidence for iQIYI (IQ).

The supplement consistently uses issuer-attributable net loss in RMB.  It
emits a H1-derived TTM loss for the October 2020 signal and the directly
reported FY2020 loss for the February 2021 signal.  It cannot create revenue
or growth eligibility.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.request import Request, urlopen
import warnings

import pandas as pd

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/iq_exact_ttm_loss")
TICKER = "IQ"
CIK = 1_722_608
CURRENCY = "CNY"
SOURCE_SCALE = 1_000
ACCOUNTING_STANDARD = "US-GAAP"
PIT_CUTOFF = "2021-02-26"
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

SOURCE_DOCUMENTS = {
    "20f_2020_03_12_fy2019": {
        "form": "20-F",
        "filed": "2020-03-12",
        "accession": "0001564590-20-010259",
        "document": "iq-20f_20191231.htm",
        "local_path": "sources/iq-20f_20191231.htm",
        "expected_sha256": (
            "bbd0ab3e4654d5119f8fdce40dbf2d2a147a231ee13aa2e73393449674587b5f"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1722608/"
            "000156459020010259/iq-20f_20191231.htm"
        ),
    },
    "6k_2020_08_14_q2_full_submission": {
        "form": "6-K/EX-99.1",
        "filed": "2020-08-14",
        "accession": "0001193125-20-219640",
        "document": "0001193125-20-219640.txt",
        "local_path": "sources/0001193125-20-219640.txt",
        "expected_sha256": (
            "b0b6c3a3790c32260b551a047120c117c1d54358f0f1a465dcbd0ce98607f4fc"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1722608/"
            "000119312520219640/0001193125-20-219640.txt"
        ),
    },
    "6k_2021_02_19_fy2020_full_submission": {
        "form": "6-K/EX-99.1",
        "filed": "2021-02-19",
        "accession": "0001193125-21-047947",
        "document": "0001193125-21-047947.txt",
        "local_path": "sources/0001193125-21-047947.txt",
        "expected_sha256": (
            "15e30e005c49f9f2446377e3557887da79f226eac8e3e2ac680db2d17d629890"
        ),
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1722608/"
            "000119312521047947/0001193125-21-047947.txt"
        ),
    },
}
ALLOWED_SOURCE_ACCESSIONS = {
    source["accession"] for source in SOURCE_DOCUMENTS.values()
}

SOURCE_PARSE_SPECS = {
    "20f_2020_03_12_fy2019": {
        "columns": {
            "FY2017": "FY2017",
            "FY2018": "FY2018",
            "FY2019": "FY2019",
            "FY2019_USD": "FY2019 USD translation",
        },
    },
    "6k_2020_08_14_q2_full_submission": {
        "columns": {
            "Q2_2019": "Q2 2019",
            "Q2_2020": "Q2 2020",
            "Q2_2020_USD": "Q2 2020 USD translation",
            "H1_2019": "H1 2019",
            "H1_2020": "H1 2020",
        },
    },
    "6k_2021_02_19_fy2020_full_submission": {
        "columns": {
            "Q4_2019": "Q4 2019",
            "Q4_2020": "Q4 2020",
            "Q4_2020_USD": "Q4 2020 USD translation",
            "FY2019": "FY2019",
            "FY2020": "FY2020",
        },
    },
}

OPERANDS_CNY_THOUSANDS = {
    "fy2019_attributable_net_loss": {
        "source_id": "20f_2020_03_12_fy2019",
        "period": "FY2019",
        "table_column": "FY2019",
        "value": -10_323_329,
    },
    "h1_2019_attributable_net_loss": {
        "source_id": "6k_2020_08_14_q2_full_submission",
        "period": "H1 2019",
        "table_column": "H1_2019",
        "value": -4_141_421,
    },
    "h1_2020_attributable_net_loss": {
        "source_id": "6k_2020_08_14_q2_full_submission",
        "period": "H1 2020",
        "table_column": "H1_2020",
        "value": -4_316_459,
    },
    "fy2020_attributable_net_loss": {
        "source_id": "6k_2021_02_19_fy2020_full_submission",
        "period": "FY2020",
        "table_column": "FY2020",
        "value": -7_038_361,
    },
}

TTM_SPECS = {
    "2020-06-30": {
        "available_date": "2020-08-14",
        "formula": "FY2019 - H1_2019 + H1_2020",
        "terms": (
            (1, "fy2019_attributable_net_loss"),
            (-1, "h1_2019_attributable_net_loss"),
            (1, "h1_2020_attributable_net_loss"),
        ),
        "expected_cny_thousands": -10_498_367,
        "form": "20-F_PLUS_6-K_H1_CUMULATIVE_TTM",
    },
    "2020-12-31": {
        "available_date": "2021-02-19",
        "formula": "direct FY2020 issuer-attributable net loss",
        "terms": ((1, "fy2020_attributable_net_loss"),),
        "expected_cny_thousands": -7_038_361,
        "form": "6-K_EX-99.1_DIRECT_ANNUAL_TTM",
    },
}

AUDIT_OBSERVATIONS = tuple(
    (f"liq{liquidity}-age150-growth", signal_date, 150)
    for liquidity in (2_000_000, 10_000_000)
    for signal_date in ("2020-10-30", "2021-02-26")
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download_source(url: str) -> bytes:
    with urlopen(Request(url, headers=SEC_HEADERS), timeout=120) as response:
        return response.read()


def _normalize_text(value: str) -> str:
    return " ".join(
        value.replace("\xa0", " ").replace("−", "-").split()
    ).casefold()


def _row_numbers(row) -> list[int]:
    text = " ".join(row.stripped_strings).replace("\xa0", " ")
    text = re.sub(r"\s*,\s*", ",", text)
    tokens = re.findall(
        r"\(\s*\d[\d,]*\s*\)|(?<![\w])\d[\d,]*(?![\w])",
        text,
    )
    values = []
    for token in tokens:
        digits = re.sub(r"\D", "", token)
        if digits:
            value = int(digits)
            values.append(-value if "(" in token else value)
    return values


def _parse_source_table(source_id: str, raw: bytes) -> dict[str, int]:
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    columns = SOURCE_PARSE_SPECS[source_id]["columns"]
    candidates = []
    for row in soup.find_all("tr"):
        labels = [
            _normalize_text(" ".join(cell.stripped_strings))
            for cell in row.find_all(("td", "th"))
        ]
        first_label = next((item for item in labels if item), "")
        if first_label != "net loss attributable to iqiyi, inc.":
            continue
        values = _row_numbers(row)
        if len(values) == len(columns):
            candidates.append(dict(zip(columns, values, strict=True)))
    if not candidates:
        raise RuntimeError(f"no unambiguous IQ net-loss table for {source_id}")
    canonical = json.dumps(candidates[0], sort_keys=True)
    if any(json.dumps(item, sort_keys=True) != canonical for item in candidates):
        raise RuntimeError(f"conflicting IQ net-loss tables for {source_id}")
    return candidates[0]


def validate_source_lock(sources: dict[str, dict] | None = None) -> None:
    documents = SOURCE_DOCUMENTS if sources is None else sources
    for source_id, source in documents.items():
        accession = source["accession"]
        if accession not in ALLOWED_SOURCE_ACCESSIONS:
            raise ValueError(f"unapproved IQ source accession: {accession}")
        if source["filed"] > PIT_CUTOFF:
            raise ValueError(f"IQ source {source_id} is post-cutoff")
        accession_path = accession.replace("-", "")
        if accession_path not in source["url"]:
            raise ValueError(f"IQ source {source_id} URL does not lock accession")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError(f"IQ source {source_id} URL does not lock document")
        relative_path = Path(source["local_path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"IQ source {source_id} has unsafe local_path")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"IQ source {source_id} has invalid SHA-256")
    for operand_id, operand in OPERANDS_CNY_THOUSANDS.items():
        if operand["source_id"] not in documents:
            raise ValueError(f"IQ operand {operand_id} has no source")


def verify_source_values(raw_by_source: dict[str, bytes]) -> list[dict]:
    if set(raw_by_source) != set(SOURCE_DOCUMENTS):
        raise ValueError("IQ raw source set does not match the source lock")
    parsed = {
        source_id: _parse_source_table(source_id, raw)
        for source_id, raw in raw_by_source.items()
    }
    verified = []
    for operand_id, operand in OPERANDS_CNY_THOUSANDS.items():
        source_id = operand["source_id"]
        column = operand["table_column"]
        value = parsed[source_id][column]
        if value != int(operand["value"]):
            raise RuntimeError(
                f"IQ operand {operand_id} changed: parsed {value}, "
                f"expected {operand['value']}"
            )
        verified.append({
            "operand_id": operand_id,
            "source_id": source_id,
            "period": operand["period"],
            "table_column": column,
            "expected_value": int(operand["value"]),
            "parsed_value": value,
            "currency": CURRENCY,
            "scale": SOURCE_SCALE,
            "profit_scope": "attributable to iQIYI, Inc.",
        })
    return verified


def prepare_verified_sources(
    output_dir: Path,
) -> tuple[dict[str, bytes], dict[str, dict], list[dict]]:
    validate_source_lock()
    raw_by_source = {}
    provenance = {}
    for source_id, source in SOURCE_DOCUMENTS.items():
        local_path = Path(output_dir) / source["local_path"]
        if local_path.exists():
            raw = local_path.read_bytes()
            downloaded = False
        else:
            raw = _download_source(source["url"])
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(raw)
            downloaded = True
        actual_sha256 = _sha256_bytes(raw)
        if actual_sha256 != source["expected_sha256"]:
            raise RuntimeError(
                f"IQ source SHA-256 mismatch for {source_id}: {actual_sha256}"
            )
        raw_by_source[source_id] = raw
        provenance[source_id] = {
            **source,
            "local_path": str(local_path),
            "actual_sha256": actual_sha256,
            "bytes": len(raw),
            "downloaded": downloaded,
        }
    return raw_by_source, provenance, verify_source_values(raw_by_source)


def exact_ttm_evidence() -> list[dict]:
    validate_source_lock()
    rows = []
    for fiscal_end, spec in TTM_SPECS.items():
        value = sum(
            coefficient * int(OPERANDS_CNY_THOUSANDS[operand_id]["value"])
            for coefficient, operand_id in spec["terms"]
        )
        if value != spec["expected_cny_thousands"] or value >= 0:
            raise RuntimeError(f"IQ exact TTM changed for {fiscal_end}: {value}")
        source_ids = list(dict.fromkeys(
            OPERANDS_CNY_THOUSANDS[operand_id]["source_id"]
            for _, operand_id in spec["terms"]
        ))
        sources = [SOURCE_DOCUMENTS[source_id] for source_id in source_ids]
        if spec["available_date"] != max(source["filed"] for source in sources):
            raise RuntimeError(f"IQ availability changed for {fiscal_end}")
        rows.append({
            "ticker": TICKER,
            "evidence_kind": "exact_issuer_attributable_ttm_loss",
            "fiscal_end": fiscal_end,
            "available_date": spec["available_date"],
            "currency": CURRENCY,
            "source_scale": SOURCE_SCALE,
            "accounting_standard": ACCOUNTING_STANDARD,
            "net_income_ttm": value * SOURCE_SCALE,
            "formula": spec["formula"],
            "operand_ids": [operand_id for _, operand_id in spec["terms"]],
            "source_accessions": [source["accession"] for source in sources],
            "source_urls": [source["url"] for source in sources],
            "form": spec["form"],
            "profit_scope": "Net loss attributable to iQIYI, Inc.; not per ADS",
        })
    return rows


def direct_ttm_facts(fetched_at: str | None = None) -> pd.DataFrame:
    if fetched_at is None:
        fetched_at = str(
            pd.Timestamp.now("UTC").tz_localize(None).normalize().date()
        )
    return pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": row["fiscal_end"],
        "available_date": row["available_date"],
        "metric": "net_income_ttm",
        "value": row["net_income_ttm"],
        "taxonomy": "us-gaap",
        "concept": "iq_exact_ttm:NetIncomeLossAttributableToIQIYI:CNY",
        "form": row["form"],
        "accession": "+".join(row["source_accessions"]),
        "fetched_at": fetched_at,
    } for row in exact_ttm_evidence()], columns=OUTPUT_COLUMNS)


def resolve_observation(signal_date: str, maximum_age_days: int) -> dict:
    signal = pd.Timestamp(signal_date)
    evidence = pd.DataFrame(exact_ttm_evidence())
    evidence["available_date"] = pd.to_datetime(evidence["available_date"])
    eligible = evidence.loc[
        evidence["available_date"].le(signal)
        & (signal - evidence["available_date"]).dt.days.le(maximum_age_days)
    ].sort_values("available_date")
    if eligible.empty:
        return {"resolved": False, "decision": "missing_financial"}
    row = eligible.iloc[-1]
    return {
        "resolved": True,
        "decision": "known_nonpositive_profit",
        "fiscal_end": row["fiscal_end"],
        "available_date": row["available_date"].strftime("%Y-%m-%d"),
        "financial_age_days": int((signal - row["available_date"]).days),
        "net_income_ttm": int(row["net_income_ttm"]),
    }


def resolve_audit_observations(
    observations: Iterable[tuple[str, str, int]] = AUDIT_OBSERVATIONS,
) -> pd.DataFrame:
    return pd.DataFrame([{
        "scenario": scenario,
        "signal_date": signal_date,
        "maximum_age_days": maximum_age_days,
        **resolve_observation(signal_date, maximum_age_days),
    } for scenario, signal_date, maximum_age_days in observations])


def build(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _, provenance, source_value_verification = prepare_verified_sources(output_dir)
    evidence = exact_ttm_evidence()
    facts = direct_ttm_facts()
    resolutions = resolve_audit_observations()
    if not resolutions["resolved"].all():
        raise RuntimeError("not every declared IQ observation resolved")

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_ttm_evidence.json"
    resolution_path = output_dir / "audit_observation_resolution.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    resolution_path.write_text(
        resolutions.to_json(orient="records", indent=2) + "\n",
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": TICKER,
        "cik": CIK,
        "reporting_currency": CURRENCY,
        "accounting_standard": ACCOUNTING_STANDARD,
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_audit_observation_count": len(resolutions),
        "source_documents": provenance,
        "source_value_verification": source_value_verification,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path), "sha256": _sha256(facts_path)
            },
            "exact_ttm_evidence": {
                "path": str(evidence_path), "sha256": _sha256(evidence_path)
            },
            "audit_observation_resolution": {
                "path": str(resolution_path), "sha256": _sha256(resolution_path)
            },
        },
        "guardrail": (
            "Uses issuer-attributable RMB net loss consistently and never "
            "mixes consolidated loss, USD convenience translations, ADS EPS, "
            "or post-signal filings. It emits no quarter, revenue, or growth."
        ),
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
    args = parser.parse_args()
    report = build(args.output_dir)
    print(json.dumps({
        "manifest": report["manifest"],
        "accepted_exact_ttm_loss_count": report["accepted_exact_ttm_loss_count"],
        "resolved_audit_observation_count": report["resolved_audit_observation_count"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
