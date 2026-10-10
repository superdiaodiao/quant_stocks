"""Build ticker-transition candidates from SHA-bound cached SEC submissions.

This avoids repeating a full-text search when the historical ticker already has
an independently resolved CIK in a submission-triage report.  The exact cached
SEC submissions envelope is replayed and its canonical payload SHA and current
ticker list must still match the triage record.  The output is compatible with
the SEC alias price importers and never changes prices by itself.
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.historicaldata_price_import import _atomic_write_json, _sha256
from scripts.sec_submission_triage import _payload_sha256
from src.conf import PROJECT_PATH


DEFAULT_AUDIT = Path(PROJECT_PATH) / "output/historical_data_audit.json"
DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_cached_submission_transition_probe.json"
)


def build_probe(
    triage_path: str | Path,
    *,
    audit_path: str | Path = DEFAULT_AUDIT,
    output: str | Path = DEFAULT_OUTPUT,
) -> dict:
    triage_path, audit_path, output = (
        Path(triage_path), Path(audit_path), Path(output)
    )
    triage = json.loads(triage_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    unresolved = {
        str(row.get("ticker", "")).upper().strip()
        for row in audit.get("unresolved_terminal_return_histories") or []
        if row.get("ticker")
    }
    results = []
    for row in triage.get("records") or []:
        ticker = str(row.get("ticker", "")).upper().strip()
        current_tickers = sorted({
            str(value).upper().strip()
            for value in row.get("current_sec_tickers") or []
            if str(value).strip()
        })
        if (
            ticker not in unresolved
            or not current_tickers
            or current_tickers == [ticker]
        ):
            continue
        cache_path = Path(row["cache_path"])
        if not cache_path.is_absolute():
            cache_path = Path(PROJECT_PATH) / cache_path
        with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
            envelope = json.load(handle)
        payload = envelope.get("payload")
        cik = int(row["cik"])
        if (
            envelope.get("format_version") != 1
            or int(envelope.get("cik", -1)) != cik
            or not isinstance(payload, dict)
            or envelope.get("payload_sha256") != _payload_sha256(payload)
            or envelope.get("payload_sha256") != row.get("cache_payload_sha256")
        ):
            raise ValueError(f"{ticker}: cached SEC submissions evidence mismatch")
        payload_tickers = sorted({
            str(value).upper().strip()
            for value in payload.get("tickers") or []
            if str(value).strip()
        })
        if payload_tickers != current_tickers:
            raise ValueError(
                f"{ticker}: triage/current SEC ticker mismatch: "
                f"{current_tickers} != {payload_tickers}"
            )
        padded_cik = f"{cik:010d}"
        results.append({
            "ticker": ticker,
            "status": "ok",
            "search_url": envelope["source_url"],
            "search_query": ticker,
            "search_payload_sha256": envelope["payload_sha256"],
            "fallback_searches": [],
            "matches": [{
                "cik": padded_cik,
                "display_name": payload.get("name"),
                "match_method": "cached_sec_submission_triage_unique_cik",
            }],
            "issuers": [{
                "cik": padded_cik,
                "issuer_name": payload.get("name"),
                "current_tickers": payload_tickers,
                "current_exchanges": payload.get("exchanges") or [],
                "former_names": payload.get("formerNames") or [],
                "submission_url": envelope["source_url"],
                "submission_payload_sha256": envelope["payload_sha256"],
                "submission_cache_path": str(cache_path),
                "submission_cache_file_sha256": _sha256(cache_path),
            }],
        })
    report = {
        "schema_version": 1,
        "research_only": True,
        "status": "COMPLETE",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "evidence_method": "SHA_BOUND_CACHED_SEC_SUBMISSIONS",
        "triage_path": str(triage_path),
        "triage_sha256": _sha256(triage_path),
        "audit_path": str(audit_path),
        "audit_sha256": _sha256(audit_path),
        "requested_tickers": sorted(unresolved),
        "completed_ticker_count": len(results),
        "results": sorted(results, key=lambda item: item["ticker"]),
    }
    _atomic_write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triage", required=True)
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    report = build_probe(args.triage, audit_path=args.audit, output=args.output)
    print(json.dumps({"candidate_issuers": report["completed_ticker_count"]}))


if __name__ == "__main__":
    main()
