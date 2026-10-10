"""Shared plumbing for the reversal 2012-2026 data acquisition (docs/reversal_2012_2026_data_plan.md).

Data only: nothing here computes signals or returns. Every request is
cached on disk before it is parsed, so a stopped run resumes without
fetching anything twice; every request is logged (URL with any key
redacted, status, bytes, sha256) and counted against its source's quota.
Keys are read from the .env files inside Python and never printed or put
in a logged URL.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.io.sec_contact import sec_user_agent

# ``REVERSAL_DATA_MAIN_CHECKOUT`` (testing only) points every cache path at another folder laid out like the main
# checkout (a scratch copy), so an offline rebuild can be checked without writing the real caches. Unset = unchanged.
MAIN_CHECKOUT = Path(os.environ.get("REVERSAL_DATA_MAIN_CHECKOUT") or "/Users/bytedance/code/quant_stocks")
# Data version (plan section 0): v1 is frozen and read only. ``REVERSAL_DATA_VERSION=v2`` points every step at the
# version-2 copies instead (a copy-on-write clone of the v1 cache and of the v1 inputs, built 2026-10-05), so a v2
# rebuild never writes a v1 file.
# ``REVERSAL_DATA_VERSION=v2.1`` (2026-10-10) is version 2 plus the Alpaca SIP fill: its own copies again (a clone of
# the v2 cache and inputs), so neither v1 nor v2 is written.
DATA_VERSION = os.environ.get("REVERSAL_DATA_VERSION", "v1").strip().lower() or "v1"
VERSIONS = ("v1", "v2", "v2.1")
if DATA_VERSION not in VERSIONS:
    raise ValueError(f"REVERSAL_DATA_VERSION must be v1, v2 or v2.1, not {DATA_VERSION!r}")
# True from version 2 on: the v2 fixes, archive fills and third votes apply (v2.1 adds the Alpaca fill on top)
V2_PLUS = DATA_VERSION in ("v2", "v2.1")
V2_1 = DATA_VERSION == "v2.1"
V1_CACHE = MAIN_CHECKOUT / "research_cache" / "reversal_2012_2026"
V1_INPUTS = Path("output/research_only/reversal_2012_2026/inputs")
_CACHE_NAME = {"v1": "reversal_2012_2026", "v2": "reversal_2012_2026_v2", "v2.1": "reversal_2012_2026_v2_1"}
_INPUTS_NAME = {"v1": "inputs", "v2": "inputs_v2", "v2.1": "inputs_v2_1"}
CACHE = MAIN_CHECKOUT / "research_cache" / _CACHE_NAME[DATA_VERSION]
INPUTS = Path("output/research_only/reversal_2012_2026") / _INPUTS_NAME[DATA_VERSION]
# the v2 fill sources (archive.org Yahoo captures, companiesmarketcap, QuantQuote, QuantConnect), local only; read
# only under v2.1
V2_FILL = MAIN_CHECKOUT / "research_cache" / "reversal_2012_2026_v2_fill"
# the v2.1 Alpaca SIP fill (raw bodies, parsed series, entity verdicts, request ledger), local only
V2_1_ALPACA = MAIN_CHECKOUT / "research_cache" / "reversal_2012_2026_v2_1_alpaca"
RAW = CACHE / "raw"
RAW_INDEX = CACHE / "raw_index.csv.gz"
QUOTA_LEDGER = CACHE / "quota_ledger.csv"
# SEC asks for at most ten requests a second; stay well under it.
SEC_PER_SECOND = 7
SEC_USER_AGENT = sec_user_agent()
SECRET_PARAMS = re.compile(
    r"(api_key|token|apikey|X-Amz-Credential|X-Amz-Signature|X-Amz-Security-Token|Signature|AWSAccessKeyId)=[^&]+",
    re.IGNORECASE)
_LOCK = threading.Lock()


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


def _append_line(path: Path, header: str, line: str, compressed: bool) -> None:
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


def _csv_field(value) -> str:
    text = "" if value is None else str(value)
    return '"' + text.replace('"', '""') + '"' if any(c in text for c in ',"\n') else text


def log_request(source: str, url: str, status: int | str, data: bytes | None, cache_path: Path | None,
                symbol: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _append_line(RAW_INDEX, "fetched_utc,source,url_redacted,http_status,bytes,sha256,cache_path",
                 ",".join(_csv_field(v) for v in (
                     now, source, redact(url), status, len(data) if data else 0,
                     sha256_bytes(data) if data else "", cache_path or "")), compressed=True)
    _append_line(QUOTA_LEDGER, "fetched_utc,source,month,symbol,status",
                 ",".join(_csv_field(v) for v in (now, source, now[:7], symbol, status)), compressed=False)


def quota_used(source: str, month: str | None = None, unique_symbols: bool = False) -> int:
    """Requests (or unique symbols) logged for ``source`` in ``month`` (YYYY-MM, default now)."""
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    if not QUOTA_LEDGER.exists():
        return 0
    seen, count = set(), 0
    for line in QUOTA_LEDGER.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 5 and parts[1] == source and parts[2] == month:
            count += 1
            seen.add(parts[3])
    return len(seen) if unique_symbols else count


def cached_get(url: str, cache_path: str | Path, *, source: str, headers: dict | None = None,
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
            log_request(source, url, status, data, cache_path, symbol)
            atomic_write(cache_path, gzip.compress(data, mtime=0) if cache_path.suffix == ".gz" else data)
            return data
        except HTTPError as exc:
            log_request(source, url, exc.code, None, None, symbol)
            if exc.code == 404:
                atomic_write(marker, b"")
                raise FileNotFoundError(f"404: {redact(url)}") from None
            if exc.code in (401, 403, 429):
                raise  # a refusal or a quota stop: the caller decides
            error = exc
        except Exception as exc:  # network trouble: try again after a pause
            log_request(source, url, type(exc).__name__, None, None, symbol)
            error = exc
        time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"{redact(url)}: {error}")


def sec_headers() -> dict:
    return {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "identity"}


SEC_LIMITER = SlidingWindowLimiter({1: SEC_PER_SECOND})


def update_manifest(entries: dict[str, dict]) -> dict:
    """Merge ``entries`` (relative path -> facts) into the inputs manifest, recording each file's sha256."""
    path = INPUTS / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"files": {}, "sources": {}}
    for relative, facts in entries.items():
        file_path = Path(relative) if Path(relative).is_absolute() else INPUTS / relative
        manifest["files"][str(relative)] = {**facts, "sha256": sha256_file(file_path)}
    manifest["updated_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    atomic_write(path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return manifest


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
