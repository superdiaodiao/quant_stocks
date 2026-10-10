"""Cached, logged, rate-limited GETs shared by every data fetcher (moved unchanged from scripts/reversal_data_common.py,
phase 3; docs/architecture.md).

Every request is cached on disk before it is parsed, so a stopped run resumes without fetching anything twice, and
every request is logged to a request ledger (URL with any key redacted, status, bytes, sha256) and counted against its
source's quota. Which ledger is the caller's choice (``Ledger``): the reversal pipeline's is the versioned cache's
``raw_index.csv.gz`` / ``quota_ledger.csv`` (``quant.data.version.RAW_INDEX`` / ``QUOTA_LEDGER``). Keys are read from
the .env files inside Python and never printed or put in a logged URL.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SECRET_PARAMS = re.compile(
    r"(api_key|token|apikey|X-Amz-Credential|X-Amz-Signature|X-Amz-Security-Token|Signature|AWSAccessKeyId)=[^&]+",
    re.IGNORECASE)
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Ledger:
    """Where requests are logged: the gzip request index and the plain quota ledger."""
    raw_index: Path
    quota: Path


def read_env_key(path: str | Path, name: str) -> str:
    """The value of ``name`` in a KEY=VALUE file; never printed."""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    raise KeyError(f"{name} is not set in {path}")


def redact(url: str) -> str:
    """``url`` with keys and presigned-URL credentials removed (a presigned link's whole query)."""
    if "amazonaws.com" in url and "?" in url:
        return url.split("?", 1)[0] + "?REDACTED"
    return SECRET_PARAMS.sub(lambda m: f"{m.group(1)}=REDACTED", url)


class SlidingWindowLimiter:
    """Blocks until a request fits every window, e.g. {1: 7} or {3600: 45, 86400: 900}."""

    def __init__(self, windows: dict[float, int]):
        self.windows = dict(windows)
        self.stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                longest = max(self.windows)
                while self.stamps and now - self.stamps[0] > longest:
                    self.stamps.popleft()
                delays = []
                for seconds, limit in self.windows.items():
                    recent = [s for s in self.stamps if now - s <= seconds]
                    if len(recent) >= limit:
                        delays.append(seconds - (now - recent[0]) + 0.01)
                if not delays:
                    self.stamps.append(now)
                    return
                time.sleep(max(delays))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: str | Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def append_line(path: Path, header: str, line: str, compressed: bool) -> None:
    """Append one line; a lock file keeps concurrent processes from interleaving
    their gzip members (which corrupted the index once, on 2026-10-01)."""
    import fcntl

    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                new = not path.exists()
                opener = gzip.open if compressed else open
                with opener(path, "at", encoding="utf-8") as handle:
                    if new:
                        handle.write(header + "\n")
                    handle.write(line + "\n")
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def csv_field(value) -> str:
    text = "" if value is None else str(value)
    return '"' + text.replace('"', '""') + '"' if any(c in text for c in ',"\n') else text


def log_request(ledger: Ledger, source: str, url: str, status: int | str, data: bytes | None,
                cache_path: Path | None, symbol: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    append_line(ledger.raw_index, "fetched_utc,source,url_redacted,http_status,bytes,sha256,cache_path",
                ",".join(csv_field(v) for v in (
                    now, source, redact(url), status, len(data) if data else 0,
                    sha256_bytes(data) if data else "", cache_path or "")), compressed=True)
    append_line(ledger.quota, "fetched_utc,source,month,symbol,status",
                ",".join(csv_field(v) for v in (now, source, now[:7], symbol, status)), compressed=False)


def quota_used(ledger: Ledger, source: str, month: str | None = None, unique_symbols: bool = False) -> int:
    """Requests (or unique symbols) logged for ``source`` in ``month`` (YYYY-MM, default now)."""
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    if not ledger.quota.exists():
        return 0
    seen, count = set(), 0
    for line in ledger.quota.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 5 and parts[1] == source and parts[2] == month:
            count += 1
            seen.add(parts[3])
    return len(seen) if unique_symbols else count


def cached_get(url: str, cache_path: str | Path, *, source: str, ledger: Ledger, headers: dict | None = None,
               limiter: SlidingWindowLimiter | None = None, symbol: str = "", timeout: float = 60,
               retries: int = 3, refetch: bool = False) -> bytes:
    """The response body for ``url``, from ``cache_path`` when it exists, else fetched once and cached.

    Bodies are cached gzip-compressed when the path ends in .gz. A 404 is
    cached as an empty marker so it is not asked again; other HTTP errors
    raise after ``retries`` attempts and are not cached.
    """
    cache_path = Path(cache_path)
    marker = cache_path.with_name(cache_path.name + ".404")
    if not refetch:
        if cache_path.exists():
            data = cache_path.read_bytes()
            return gzip.decompress(data) if cache_path.suffix == ".gz" else data
        if marker.exists():
            raise FileNotFoundError(f"404 (cached): {redact(url)}")
    error = None
    for attempt in range(retries):
        if limiter is not None:
            limiter.wait()
        try:
            request = Request(url, headers=headers or {})
            with urlopen(request, timeout=timeout) as response:
                data = response.read()
                status = response.status
            log_request(ledger, source, url, status, data, cache_path, symbol)
            atomic_write(cache_path, gzip.compress(data, mtime=0) if cache_path.suffix == ".gz" else data)
            return data
        except HTTPError as exc:
            log_request(ledger, source, url, exc.code, None, None, symbol)
            if exc.code == 404:
                atomic_write(marker, b"")
                raise FileNotFoundError(f"404: {redact(url)}") from None
            if exc.code in (401, 403, 429):
                raise  # a refusal or a quota stop: the caller decides
            error = exc
        except Exception as exc:  # network trouble: try again after a pause
            log_request(ledger, source, url, type(exc).__name__, None, None, symbol)
            error = exc
        time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"{redact(url)}: {error}")


def parallel_map(function, items, workers: int = 8) -> list:
    """``function`` over ``items`` on a thread pool, results in input order.

    For slow-answering sources (SEC answers in about a second): the shared
    limiter still caps the request rate, so more workers only fill it.
    Exceptions are returned in place of results, not raised.
    """
    from concurrent.futures import ThreadPoolExecutor

    def safe(item):
        try:
            return function(item)
        except Exception as exc:  # reported per item
            return exc

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(safe, items))
