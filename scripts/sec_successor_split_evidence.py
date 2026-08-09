"""Cache and SHA-bind SEC filings used to normalize successor split history."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.historicaldata_price_import import _atomic_write_json
from scripts.sec_terminal_filing_evidence import (
    _fetch,
    _filing_text,
    _read_cache,
    _write_cache,
)


def build_evidence(
    filings: list[dict], *, cache_dir: str | Path, output: str | Path, refresh: bool = False
) -> dict:
    cache_dir = Path(cache_dir)
    records = []
    for filing in filings:
        ticker = str(filing["ticker"]).upper()
        accession = str(filing["accession"])
        url = str(filing["source_url"])
        path = cache_dir / f"{ticker.lower()}_{accession}.json.gz"
        if path.exists() and not refresh:
            envelope, payload = _read_cache(path, url)
        else:
            payload = _fetch(url)
            envelope = _write_cache(path, url, payload)
        text = _filing_text(payload)
        phrases = list(filing["required_phrases"])
        lowered = text.lower()
        missing = [phrase for phrase in phrases if phrase.lower() not in lowered]
        if missing:
            raise ValueError(f"{ticker} filing is missing required phrases: {missing}")
        records.append({
            **filing,
            "cache_path": str(path),
            "payload_sha256": envelope["payload_sha256"],
            "payload_size_bytes": len(payload),
            "verified": True,
        })
    evidence = {
        "format_version": 1,
        "research_only": True,
        "records": records,
        "verified": all(record["verified"] for record in records),
    }
    _atomic_write_json(Path(output), evidence)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    filings = json.loads(Path(args.spec).read_text(encoding="utf-8"))["filings"]
    build_evidence(
        filings, cache_dir=args.cache_dir, output=args.output, refresh=args.refresh
    )


if __name__ == "__main__":
    main()
