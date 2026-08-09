"""Resumable parallel HTTP Range downloader for immutable Wayback captures.

The Wayback replay endpoint is used instead of reconstructing a MegaWARC
record.  Every part is written atomically and the final file is assembled only
when all byte ranges have the expected lengths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen


DEFAULT_URL = (
    "https://web.archive.org/web/20250414082852id_/"
    "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
)
DEFAULT_SIZE = 1_281_479_761


def _part_path(root: Path, index: int) -> Path:
    return root / f"part-{index:06d}"


def _download_part(url: str, root: Path, index: int, start: int, end: int, retries: int) -> tuple[int, int]:
    target = _part_path(root, index)
    expected = end - start + 1
    if target.exists() and target.stat().st_size == expected:
        return index, expected
    partial = root / f".{target.name}.partial"
    for attempt in range(retries + 1):
        try:
            current = partial.stat().st_size if partial.exists() else 0
            if current > expected:
                partial.unlink()
                current = 0
            request_start = start + current
            request = Request(
                url,
                headers={
                    "Accept-Encoding": "identity",
                    "Range": f"bytes={request_start}-{end}",
                    "User-Agent": "quant_stocks-wayback-repro/1.0",
                },
            )
            with urlopen(request, timeout=180) as response:
                # A server returning 200 to a resumed request would duplicate
                # bytes; restart that part from scratch instead.
                if current and getattr(response, "status", 206) == 200:
                    partial.unlink(missing_ok=True)
                    continue
                with open(partial, "ab") as handle:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        handle.write(block)
            if os.path.getsize(partial) != expected:
                raise RuntimeError(f"range {start}-{end} returned {os.path.getsize(partial)} bytes")
            os.replace(partial, target)
            return index, expected
        except Exception:
            if attempt >= retries:
                raise
    raise AssertionError("unreachable")


def download(
    url: str,
    output: Path,
    size: int,
    chunk_size: int,
    workers: int,
    retries: int,
    base_offset: int = 0,
) -> dict:
    output = output.resolve()
    parts = output.with_name(output.name + ".parts")
    parts.mkdir(parents=True, exist_ok=True)
    ranges = [(start, min(size - 1, start + chunk_size - 1)) for start in range(0, size, chunk_size)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _download_part,
                url,
                parts,
                index,
                base_offset + start,
                base_offset + end,
                retries,
            ): index
            for index, (start, end) in enumerate(ranges)
        }
        for future in as_completed(futures):
            index, count = future.result()
            print(json.dumps({"part": index, "bytes": count}, sort_keys=True), flush=True)
    temporary = output.with_name(output.name + ".assembling")
    with open(temporary, "wb") as handle:
        for index, (start, end) in enumerate(ranges):
            part = _part_path(parts, index)
            if part.stat().st_size != end - start + 1:
                raise RuntimeError(f"invalid part {part}")
            with open(part, "rb") as source:
                while block := source.read(1024 * 1024):
                    handle.write(block)
    if temporary.stat().st_size != size:
        raise RuntimeError(f"assembled size {temporary.stat().st_size} != {size}")
    os.replace(temporary, output)
    digest_builder = hashlib.sha256()
    with open(output, "rb") as handle:
        while block := handle.read(1024 * 1024):
            digest_builder.update(block)
    digest = digest_builder.hexdigest()
    return {
        "url": url,
        "base_offset": base_offset,
        "bytes": size,
        "sha256": digest,
        "chunk_size": chunk_size,
        "parts": len(ranges),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--base-offset", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--retries", type=int, default=4)
    args = parser.parse_args()
    print(
        json.dumps(
            download(
                args.url,
                args.output,
                args.size,
                args.chunk_size,
                args.workers,
                args.retries,
                args.base_offset,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
