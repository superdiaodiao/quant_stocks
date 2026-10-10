#!/usr/bin/env python3
"""Prepare isolated raw Company Facts needed by strict v14 overrides."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path

from src.io.fundamentals_update import (
    parse_and_bind_cached_companyfacts_symbols,
    populate_missing_companyfacts_cache,
    write_companyfacts_cache_manifest,
)


DEFAULT_CACHE = Path("output/research_only/v14/companyfacts_cache")
DEFAULT_REPORT = Path(
    "output/research_only/v14/issuer_companyfacts_prepare.json"
)

DMRC_TRANSITION = {
    "cik": 2119322,
    "predecessor_ciks": [1438231],
    "conformed_name": "Digimarc Corp",
    "source_url": (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&"
        "owner=exclude&output=atom&CIK=DMRC"
    ),
    "evidence": (
        "SEC current ticker map binds DMRC to CIK 2119322; SEC Atom ticker "
        "lookup retains the historical operating issuer CIK 1438231"
    ),
}

MRVL_TRANSITION = {
    "cik": 1835632,
    "predecessor_ciks": [1058057],
    "conformed_name": "Marvell Technology, Inc.",
    "source_url": "https://data.sec.gov/submissions/CIK0001835632.json",
    "evidence": (
        "SEC Company Facts for predecessor CIK 1058057 identifies MARVELL "
        "TECHNOLOGY GROUP LTD and contains the contemporaneously filed "
        "pre-2021 quarterly history; current MRVL Company Facts uses CIK "
        "1835632 and reports that history only in later successor filings"
    ),
}

TTGT_TRANSITION = {
    "cik": 2018064,
    "predecessor_ciks": [1293282],
    "conformed_name": "TechTarget, Inc.",
    "source_url": (
        "https://www.sec.gov/Archives/edgar/data/2018064/"
        "000119312524269931/d913820d8k.htm"
    ),
    "evidence": (
        "SEC submissions identifies current TTGT CIK 2018064 as former Toro "
        "CombineCo and predecessor CIK 1293282 as TechTarget Inc through "
        "2024-11-26; the December 2024 combination retained ticker TTGT while "
        "the predecessor became TechTarget Holdings Inc"
    ),
}

AZPN_TRANSITION = {
    "cik": 1897982,
    "predecessor_ciks": [929940],
    "conformed_name": "Aspen Technology, Inc.",
    "source_url": (
        "https://www.sec.gov/Archives/edgar/data/1897982/"
        "000114036122017665/ny20004077x5_8k.htm"
    ),
    "evidence": (
        "SEC submissions identifies current AspenTech CIK 1897982 as former "
        "Emersub CX and predecessor CIK 929940 as Aspen Technology Inc through "
        "the May 2022 transaction; the historical AZPN filings are under CIK "
        "929940"
    ),
}

RCM_TRANSITION = {
    "cik": 1910851,
    "predecessor_ciks": [1472595],
    "conformed_name": "R1 RCM Inc.",
    "source_url": (
        "https://www.sec.gov/Archives/edgar/data/1472595/"
        "000119312522177795/d368366d8k.htm"
    ),
    "evidence": (
        "SEC submissions identifies predecessor CIK 1472595 as R1 RCM Inc "
        "through 2022-06-14 and successor CIK 1910851 as former Project "
        "Roadrunner Parent; the June 2022 transaction moved the reporting "
        "entity while historical RCM filings remain under CIK 1472595"
    ),
}

IAC_TRANSITION = {
    "cik": 1800227,
    "predecessor_ciks": [891103],
    "conformed_name": "IAC Inc.",
    "source_url": (
        "https://www.sec.gov/Archives/edgar/data/891103/"
        "000110465920080606/tm206790d25_8k.htm"
    ),
    "evidence": (
        "SEC separation 8-K identifies CIK 891103 as Old IAC and CIK "
        "1800227 as IAC Holdings/New IAC; the parties completed the "
        "separation on 2020-06-30, Old IAC became Match Group and New IAC "
        "retained the other IAC businesses under ticker IAC"
    ),
}

UNIT_TRANSITION = {
    "cik": 2020795,
    "predecessor_ciks": [1620280],
    "conformed_name": "Uniti Group Inc.",
    "source_url": (
        "https://www.sec.gov/Archives/edgar/data/2020795/"
        "000095010325009718/dp232461_8k12b.htm"
    ),
    "evidence": (
        "SEC Form 8-K12B states that on 2025-08-01 former Windstream Parent "
        "CIK 2020795 became New Uniti and old Uniti CIK 1620280 survived as "
        "its subsidiary; pre-closing UNIT filings therefore remain under "
        "CIK 1620280"
    ),
}

REQUIRED_PAYLOADS = (
    ("CGC", "CGC", 1737927, "currency_override_CAD"),
    ("AMED", "AMED", 896262, "healthcare_revenue_concept_override"),
    ("AVXL", "AVXL", 1314052, "contract_income_concept_override"),
    ("IGMS", "IGMS", 1496323, "collaborative_revenue_concept_override"),
    ("DMRC", "DMRC", 2119322, "current_parent"),
    (
        "DMRC_PRE_2026",
        "DMRC",
        1438231,
        "historical_operating_issuer",
    ),
    (
        "MRVL_PRE_2021",
        "MRVL",
        1058057,
        "historical_predecessor_issuer",
    ),
    (
        "TTGT_PRE_COMBINATION",
        "TTGT",
        1293282,
        "historical_predecessor_issuer",
    ),
    (
        "AZPN_PRE_2022",
        "AZPN",
        929940,
        "historical_predecessor_issuer",
    ),
    (
        "RCM_PRE_2022",
        "RCM",
        1472595,
        "historical_predecessor_issuer",
    ),
    (
        "IAC_PRE_SEPARATION",
        "IAC",
        891103,
        "historical_predecessor_issuer",
    ),
    (
        "UNIT_PRE_COMBINATION",
        "UNIT",
        1620280,
        "historical_predecessor_issuer",
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def declare_dmrc_transition(cache_dir: Path) -> dict:
    path = Path(cache_dir) / "historical_ticker_ciks.json"
    document = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"format_version": 1, "entries": {}}
    )
    if document.get("format_version") != 1:
        raise RuntimeError(f"Unsupported historical CIK registry {path}")
    entries = dict(document.get("entries") or {})
    existing = entries.get("DMRC")
    if existing is not None and existing != DMRC_TRANSITION:
        if (
            int(existing.get("cik", 0)) != DMRC_TRANSITION["cik"]
            or list(existing.get("predecessor_ciks", []))
            != DMRC_TRANSITION["predecessor_ciks"]
        ):
            raise RuntimeError(
                "Conflicting pre-existing DMRC historical CIK transition"
            )
        merged = {**existing, **DMRC_TRANSITION}
    else:
        merged = dict(DMRC_TRANSITION)
    entries["DMRC"] = merged
    updated = {
        "format_version": 1,
        "entries": {ticker: entries[ticker] for ticker in sorted(entries)},
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "ticker": "DMRC",
        **DMRC_TRANSITION,
    }


def declare_mrvl_transition(cache_dir: Path) -> dict:
    """Declare MRVL's SEC predecessor CIK without changing the formal cache."""
    path = Path(cache_dir) / "historical_ticker_ciks.json"
    document = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"format_version": 1, "entries": {}}
    )
    if document.get("format_version") != 1:
        raise RuntimeError(f"Unsupported historical CIK registry {path}")
    entries = dict(document.get("entries") or {})
    existing = entries.get("MRVL")
    if existing is not None and existing != MRVL_TRANSITION:
        if (
            int(existing.get("cik", 0)) != MRVL_TRANSITION["cik"]
            or list(existing.get("predecessor_ciks", []))
            != MRVL_TRANSITION["predecessor_ciks"]
        ):
            raise RuntimeError(
                "Conflicting pre-existing MRVL historical CIK transition"
            )
        merged = {**existing, **MRVL_TRANSITION}
    else:
        merged = dict(MRVL_TRANSITION)
    entries["MRVL"] = merged
    updated = {
        "format_version": 1,
        "entries": {ticker: entries[ticker] for ticker in sorted(entries)},
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "ticker": "MRVL",
        **MRVL_TRANSITION,
    }


def declare_ttgt_transition(cache_dir: Path) -> dict:
    """Declare TTGT's pre-combination SEC CIK without touching formal data."""
    path = Path(cache_dir) / "historical_ticker_ciks.json"
    document = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"format_version": 1, "entries": {}}
    )
    if document.get("format_version") != 1:
        raise RuntimeError(f"Unsupported historical CIK registry {path}")
    entries = dict(document.get("entries") or {})
    existing = entries.get("TTGT")
    if existing is not None and existing != TTGT_TRANSITION:
        if (
            int(existing.get("cik", 0)) != TTGT_TRANSITION["cik"]
            or list(existing.get("predecessor_ciks", []))
            != TTGT_TRANSITION["predecessor_ciks"]
        ):
            raise RuntimeError(
                "Conflicting pre-existing TTGT historical CIK transition"
            )
        merged = {**existing, **TTGT_TRANSITION}
    else:
        merged = dict(TTGT_TRANSITION)
    entries["TTGT"] = merged
    updated = {
        "format_version": 1,
        "entries": {ticker: entries[ticker] for ticker in sorted(entries)},
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "ticker": "TTGT",
        **TTGT_TRANSITION,
    }


def declare_azpn_transition(cache_dir: Path) -> dict:
    """Declare AZPN's pre-transaction SEC CIK without touching formal data."""
    path = Path(cache_dir) / "historical_ticker_ciks.json"
    document = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"format_version": 1, "entries": {}}
    )
    if document.get("format_version") != 1:
        raise RuntimeError(f"Unsupported historical CIK registry {path}")
    entries = dict(document.get("entries") or {})
    existing = entries.get("AZPN")
    if existing is not None and existing != AZPN_TRANSITION:
        if (
            int(existing.get("cik", 0)) != AZPN_TRANSITION["cik"]
            or list(existing.get("predecessor_ciks", []))
            != AZPN_TRANSITION["predecessor_ciks"]
        ):
            raise RuntimeError(
                "Conflicting pre-existing AZPN historical CIK transition"
            )
        merged = {**existing, **AZPN_TRANSITION}
    else:
        merged = dict(AZPN_TRANSITION)
    entries["AZPN"] = merged
    updated = {
        "format_version": 1,
        "entries": {ticker: entries[ticker] for ticker in sorted(entries)},
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "ticker": "AZPN",
        **AZPN_TRANSITION,
    }


def declare_rcm_transition(cache_dir: Path) -> dict:
    """Declare RCM's pre-transaction SEC CIK without touching formal data."""
    path = Path(cache_dir) / "historical_ticker_ciks.json"
    document = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"format_version": 1, "entries": {}}
    )
    if document.get("format_version") != 1:
        raise RuntimeError(f"Unsupported historical CIK registry {path}")
    entries = dict(document.get("entries") or {})
    existing = entries.get("RCM")
    if existing is not None and existing != RCM_TRANSITION:
        existing_predecessors = list(existing.get("predecessor_ciks", []))
        if (
            int(existing.get("cik", 0)) != RCM_TRANSITION["cik"]
            or (
                existing_predecessors
                and existing_predecessors != RCM_TRANSITION["predecessor_ciks"]
            )
        ):
            raise RuntimeError(
                "Conflicting pre-existing RCM historical CIK transition"
            )
        merged = {**existing, **RCM_TRANSITION}
    else:
        merged = dict(RCM_TRANSITION)
    entries["RCM"] = merged
    updated = {
        "format_version": 1,
        "entries": {ticker: entries[ticker] for ticker in sorted(entries)},
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "ticker": "RCM",
        **RCM_TRANSITION,
    }


def declare_iac_transition(cache_dir: Path) -> dict:
    """Declare IAC's pre-separation SEC CIK without touching formal data."""
    path = Path(cache_dir) / "historical_ticker_ciks.json"
    document = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"format_version": 1, "entries": {}}
    )
    if document.get("format_version") != 1:
        raise RuntimeError(f"Unsupported historical CIK registry {path}")
    entries = dict(document.get("entries") or {})
    existing = entries.get("IAC")
    if existing is not None and existing != IAC_TRANSITION:
        existing_predecessors = list(existing.get("predecessor_ciks", []))
        if (
            int(existing.get("cik", 0)) != IAC_TRANSITION["cik"]
            or (
                existing_predecessors
                and existing_predecessors != IAC_TRANSITION["predecessor_ciks"]
            )
        ):
            raise RuntimeError(
                "Conflicting pre-existing IAC historical CIK transition"
            )
        merged = {**existing, **IAC_TRANSITION}
    else:
        merged = dict(IAC_TRANSITION)
    entries["IAC"] = merged
    updated = {
        "format_version": 1,
        "entries": {ticker: entries[ticker] for ticker in sorted(entries)},
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "ticker": "IAC",
        **IAC_TRANSITION,
    }


def declare_unit_transition(cache_dir: Path) -> dict:
    """Declare UNIT's pre-combination SEC CIK without touching formal data."""
    path = Path(cache_dir) / "historical_ticker_ciks.json"
    document = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"format_version": 1, "entries": {}}
    )
    if document.get("format_version") != 1:
        raise RuntimeError(f"Unsupported historical CIK registry {path}")
    entries = dict(document.get("entries") or {})
    existing = entries.get("UNIT")
    if existing is not None and existing != UNIT_TRANSITION:
        existing_predecessors = list(existing.get("predecessor_ciks", []))
        if (
            int(existing.get("cik", 0)) != UNIT_TRANSITION["cik"]
            or (
                existing_predecessors
                and existing_predecessors != UNIT_TRANSITION["predecessor_ciks"]
            )
        ):
            raise RuntimeError(
                "Conflicting pre-existing UNIT historical CIK transition"
            )
        merged = {**existing, **UNIT_TRANSITION}
    else:
        merged = dict(UNIT_TRANSITION)
    entries["UNIT"] = merged
    updated = {
        "format_version": 1,
        "entries": {ticker: entries[ticker] for ticker in sorted(entries)},
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "ticker": "UNIT",
        **UNIT_TRANSITION,
    }


def prepare_issuer_companyfacts(
    *,
    cache_dir: Path = DEFAULT_CACHE,
    report_path: Path = DEFAULT_REPORT,
    workers: int = 2,
) -> dict:
    cache_dir = Path(cache_dir)
    transitions = [
        declare_dmrc_transition(cache_dir),
        declare_mrvl_transition(cache_dir),
        declare_ttgt_transition(cache_dir),
        declare_azpn_transition(cache_dir),
        declare_rcm_transition(cache_dir),
        declare_iac_transition(cache_dir),
        declare_unit_transition(cache_dir),
    ]
    # The raw-cache refresh verifies the existing manifest before it writes.
    # Bind the newly declared transition first so that verification remains
    # fail-closed rather than temporarily observing an integrity mismatch.
    write_companyfacts_cache_manifest(cache_dir)
    refreshes = []
    for request_symbol, bind_symbol, cik, purpose in REQUIRED_PAYLOADS:
        result = populate_missing_companyfacts_cache(
            date.today(),
            workers=workers,
            tickers=[request_symbol],
            cik_overrides={request_symbol: cik},
            cache_dir=cache_dir,
            refresh_after_days=36500,
        )
        path = cache_dir / f"CIK{cik:010d}.json.gz"
        if not path.exists():
            raise RuntimeError(f"Required issuer payload was not cached: {path}")
        if request_symbol != bind_symbol:
            parse_and_bind_cached_companyfacts_symbols(
                [bind_symbol], cik, cache_dir
            )
            write_companyfacts_cache_manifest(cache_dir)
        refreshes.append({
            "ticker": bind_symbol,
            "request_symbol": request_symbol,
            "cik": cik,
            "purpose": purpose,
            "path": str(path),
            "sha256": _sha256(path),
            "refresh": result,
        })
    manifest = write_companyfacts_cache_manifest(cache_dir)
    report = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_companyfacts_cache_modified": False,
        "cache_dir": str(cache_dir),
        "historical_transitions": transitions,
        "payloads": refreshes,
        "cache_manifest": {
            "path": str(manifest),
            "sha256": _sha256(manifest),
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    report["report"] = str(report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    report = prepare_issuer_companyfacts(
        cache_dir=args.cache_dir,
        report_path=args.report,
        workers=args.workers,
    )
    print(json.dumps({
        "report": report["report"],
        "payload_count": len(report["payloads"]),
        "cache_manifest": report["cache_manifest"],
        "formal_companyfacts_cache_modified": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
