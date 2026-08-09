"""Checkpointed batch probe of filing-local SEC XBRL target facts."""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from scripts.sec_inline_xbrl_target_probe import _sha256, probe_targets


USER_AGENT = "quant-stocks-research contact@example.com"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def _load_or_fetch(
    path: Path,
    url: str,
    *,
    refresh: bool,
    fetcher: Callable[[str], bytes],
) -> bytes:
    if path.exists() and not refresh:
        return path.read_bytes()
    payload = fetcher(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return payload


def batch_probe(
    target_manifest: str | Path,
    snapshot_manifest: str | Path,
    cache_dir: str | Path,
    output: str | Path,
    *,
    refresh: bool = False,
    fetcher: Callable[[str], bytes] = _fetch,
) -> dict[str, Any]:
    target_manifest = Path(target_manifest).resolve()
    snapshot_manifest = Path(snapshot_manifest).resolve()
    cache_dir = Path(cache_dir).resolve()
    output = Path(output).resolve()
    target_document = json.loads(target_manifest.read_text(encoding="utf-8"))
    targets = target_document.get("unmatched_target_rows", [])
    snapshot = json.loads(snapshot_manifest.read_text(encoding="utf-8"))
    symbol_to_cik = {
        str(symbol).upper(): int(entry["cik"])
        for entry in snapshot.get("entries", [])
        for symbol in entry.get("symbols", [])
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for target in targets:
        grouped[(target["ticker"].upper(), target["accession"])].append(target)
    report = {
        "format_version": 1,
        "research_only": True,
        "status": "IN_PROGRESS",
        "target_manifest": str(target_manifest),
        "target_manifest_sha256": _sha256(target_manifest),
        "snapshot_manifest": str(snapshot_manifest),
        "snapshot_manifest_sha256": _sha256(snapshot_manifest),
        "requested_filing_count": len(grouped),
        "records": [],
    }
    _atomic_json(output, report)
    for (ticker, accession), filing_targets in sorted(grouped.items()):
        cik = symbol_to_cik.get(ticker)
        record: dict[str, Any] = {
            "ticker": ticker,
            "accession": accession,
            "cik": cik,
            "target_count": len(filing_targets),
        }
        try:
            if cik is None:
                raise ValueError(f"snapshot has no CIK for {ticker}")
            accession_compact = accession.replace("-", "")
            base_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                f"{accession_compact}"
            )
            stem = f"{ticker}_{accession}"
            index_path = cache_dir / f"{stem}_index.json"
            index_url = f"{base_url}/index.json"
            index_payload = _load_or_fetch(
                index_path, index_url, refresh=refresh, fetcher=fetcher
            )
            index = json.loads(index_payload)
            xbrl_names = [
                item["name"] for item in index["directory"]["item"]
                if item["name"].lower().endswith("-xbrl.zip")
            ]
            if len(xbrl_names) != 1:
                raise ValueError(
                    f"expected one filing XBRL ZIP, found {xbrl_names}"
                )
            xbrl_name = xbrl_names[0]
            zip_path = cache_dir / f"{stem}_xbrl.zip"
            zip_url = f"{base_url}/{xbrl_name}"
            _load_or_fetch(
                zip_path, zip_url, refresh=refresh, fetcher=fetcher
            )
            probe_path = cache_dir / f"{stem}_probe.json"
            probe = probe_targets(
                zip_path,
                filing_targets,
                probe_path,
                source_url=zip_url,
                index_path=index_path,
            )
            exact_count = sum(
                row["exact_value_match_count"] for row in probe["targets"]
            )
            record.update({
                "status": "EXACT_MATCH" if exact_count else "NO_EXACT_MATCH",
                "index_url": index_url,
                "index_path": str(index_path),
                "index_sha256": _sha256(index_path),
                "xbrl_url": zip_url,
                "xbrl_path": str(zip_path),
                "xbrl_sha256": _sha256(zip_path),
                "probe_path": str(probe_path),
                "probe_sha256": _sha256(probe_path),
                "exact_match_count": exact_count,
            })
        except Exception as exc:  # pragma: no cover - network dependent
            record.update({"status": "ERROR", "error": repr(exc)})
        report["records"].append(record)
        report["checkpointed_records"] = len(report["records"])
        report["last_checkpoint_accession"] = accession
        _atomic_json(output, report)
        time.sleep(0.2)
    report["status"] = "COMPLETE"
    report["status_counts"] = {
        status: sum(row["status"] == status for row in report["records"])
        for status in sorted({row["status"] for row in report["records"]})
    }
    _atomic_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-manifest", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    report = batch_probe(
        args.target_manifest,
        args.snapshot_manifest,
        args.cache_dir,
        args.output,
        refresh=args.refresh,
    )
    print(json.dumps({
        "filings": len(report["records"]),
        "status_counts": report["status_counts"],
        "research_only": True,
    }, indent=2))


if __name__ == "__main__":
    main()
