#!/usr/bin/env python3
"""Expose MESO's pre-signal FY2020 loss as exact annual TTM evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup

from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/meso_exact_annual_loss")
ACCESSION = "0001564590-20-041869"
INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/data/1345099/"
    "000156459020041869/0001564590-20-041869-index.html"
)
EXHIBIT_URL = (
    "https://www.sec.gov/Archives/edgar/data/1345099/"
    "000156459020041869/meso-ex991_6.htm"
)
INDEX_SHA256 = "d0797d2eb2c47ca987246704806b766b4d4c98b5eeaa25cb9039c8495e44627b"
EXHIBIT_SHA256 = "bfe5b31a5f26bf6923bdec7ee3a9f88c487f7f83b074b616e5e9c0b0f99f35a3"
EDGAR_ACCEPTED = "2020-08-28 20:48:24"
AVAILABLE_DATE = "2020-08-28"
SEC_FILING_DATE = "2020-08-31"
SIGNAL_DATE = "2020-08-31"
FISCAL_END = "2020-06-30"
EXPECTED_NET_LOSS = -77_940_000
PRIOR_NET_LOSS = -89_799_000
MAXIMUM_AGE_DAYS = 150
AUDIT_OBSERVATIONS = (("liq2000000-age150-growth", SIGNAL_DATE, MAXIMUM_AGE_DAYS),)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _download(url: str) -> bytes:
    request = Request(
        url, headers={"User-Agent": "quant-stocks-research research@example.com"}
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=120) as response:
                return response.read()
        except Exception as exc:  # SEC occasionally closes an otherwise valid request.
            last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    raise RuntimeError(f"MESO SEC download failed after three attempts: {url}") from last_error


def _normalized_text(raw: bytes) -> str:
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def verify_sources(index_raw: bytes, exhibit_raw: bytes) -> dict:
    actual_index = _sha256_bytes(index_raw)
    actual_exhibit = _sha256_bytes(exhibit_raw)
    if actual_index != INDEX_SHA256:
        raise RuntimeError(f"MESO filing index SHA-256 mismatch: {actual_index}")
    if actual_exhibit != EXHIBIT_SHA256:
        raise RuntimeError(f"MESO exhibit SHA-256 mismatch: {actual_exhibit}")
    index_text = _normalized_text(index_raw)
    exhibit_text = _normalized_text(exhibit_raw)
    for fragment in (EDGAR_ACCEPTED, "Filing Date 2020-08-31", "EX-99.1"):
        if fragment.lower() not in index_text.lower():
            raise RuntimeError(f"MESO EDGAR timing fragment changed: {fragment}")
    exhibit_fragments = (
        "13% reduction in loss after tax",
        "US$77.9 million for FY2020 compared with US$89.8 million for FY2019",
        "Loss attributable to the owners of Mesoblast Limited",
        "77,940",
        "89,799",
        "in U.S. dollars, in thousands",
    )
    for fragment in exhibit_fragments:
        if fragment.lower() not in exhibit_text.lower():
            raise RuntimeError(f"MESO loss evidence fragment changed: {fragment}")
    return {
        "accession": ACCESSION,
        "form": "6-K/EX-99.1",
        "edgar_accepted": EDGAR_ACCEPTED,
        "available_date": AVAILABLE_DATE,
        "sec_filing_date": SEC_FILING_DATE,
        "index_url": INDEX_URL,
        "index_sha256": actual_index,
        "exhibit_url": EXHIBIT_URL,
        "exhibit_sha256": actual_exhibit,
        "verified_exhibit_fragments": list(exhibit_fragments),
    }


def exact_loss_evidence() -> dict:
    if EXPECTED_NET_LOSS >= 0 or PRIOR_NET_LOSS >= 0:
        raise RuntimeError("MESO annual loss sign changed")
    return {
        "ticker": "MESO",
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "currency": "USD",
        "scale": 1_000,
        "accounting_standard": "IFRS",
        "source_line_item": "Loss attributable to the owners of Mesoblast Limited",
        "net_income_ttm": EXPECTED_NET_LOSS,
        "prior_net_income_ttm": PRIOR_NET_LOSS,
        "decision": "known_nonpositive_profit",
    }


def direct_ttm_facts(fetched_at: str) -> pd.DataFrame:
    evidence = exact_loss_evidence()
    return pd.DataFrame([{
        "ticker": "MESO",
        "fiscal_end": FISCAL_END,
        "available_date": AVAILABLE_DATE,
        "metric": "net_income_ttm",
        "value": EXPECTED_NET_LOSS,
        "taxonomy": "ifrs-full",
        "concept": "meso_6k_exact_annual:ProfitLossAttributableToOwnersOfParent:USD",
        "form": "6-K_EX-99.1_EXACT_ANNUAL",
        "accession": ACCESSION,
        "fetched_at": pd.Timestamp(fetched_at).tz_localize(None).normalize(),
    }], columns=OUTPUT_COLUMNS)


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    sources_dir = output_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    index_path = sources_dir / "0001564590-20-041869-index.html"
    exhibit_path = sources_dir / "meso-ex991_6.htm"
    if not index_path.exists():
        raw = _download(INDEX_URL)
        if _sha256_bytes(raw) != INDEX_SHA256:
            raise RuntimeError("MESO downloaded filing index failed its SHA lock")
        index_path.write_bytes(raw)
    if not exhibit_path.exists():
        raw = _download(EXHIBIT_URL)
        if _sha256_bytes(raw) != EXHIBIT_SHA256:
            raise RuntimeError("MESO downloaded exhibit failed its SHA lock")
        exhibit_path.write_bytes(raw)
    source = verify_sources(index_path.read_bytes(), exhibit_path.read_bytes())
    evidence = exact_loss_evidence()
    facts = direct_ttm_facts(
        pd.Timestamp.now("UTC").tz_localize(None).normalize().isoformat()
    )
    age = int((pd.Timestamp(SIGNAL_DATE) - pd.Timestamp(AVAILABLE_DATE)).days)
    if age != 3 or age > MAXIMUM_AGE_DAYS:
        raise RuntimeError("MESO exact annual loss is outside the signal window")
    resolutions = [{
        "scenario": scenario,
        "signal_date": signal,
        "maximum_age_days": maximum_age,
        "financial_age_days": age,
        "resolved": True,
        "decision": evidence["decision"],
    } for scenario, signal, maximum_age in AUDIT_OBSERVATIONS]

    facts_path = output_dir / "strict_quarterly_facts.csv"
    evidence_path = output_dir / "exact_loss_evidence.json"
    resolutions_path = output_dir / "audit_observation_resolution.json"
    facts.to_csv(facts_path, index=False)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    resolutions_path.write_text(json.dumps(resolutions, indent=2, sort_keys=True) + "\n")
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": "MESO",
        "source": {
            **source,
            "index_path": str(index_path),
            "exhibit_path": str(exhibit_path),
        },
        "accepted_exact_ttm_loss_count": len(facts),
        "resolved_audit_observation_count": len(resolutions),
        "outputs": {
            "strict_quarterly_facts": {"path": str(facts_path), "sha256": _sha256_path(facts_path)},
            "exact_loss_evidence": {"path": str(evidence_path), "sha256": _sha256_path(evidence_path)},
            "audit_observation_resolution": {"path": str(resolutions_path), "sha256": _sha256_path(resolutions_path)},
        },
        "guardrail": (
            "The SEC index proves the exhibit was accepted Friday 2020-08-28 at "
            "20:48:24 ET, before the Monday 2020-08-31 signal. Only the exact IFRS "
            "annual loss is emitted; no revenue, quarter, growth, EPS, or later filing "
            "is manufactured."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = run(args.output_dir)
    print(json.dumps({
        "manifest": report["manifest"],
        "accepted_exact_ttm_loss_count": report["accepted_exact_ttm_loss_count"],
        "resolved_audit_observation_count": report["resolved_audit_observation_count"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
