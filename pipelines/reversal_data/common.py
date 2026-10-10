"""Shared plumbing for the reversal 2012-2026 data acquisition (docs/reversal_2012_2026_data_plan.md).

Data only: nothing here computes signals or returns. The data version and its paths come from
``quant.data.version`` (the one place the versions are declared); the cached, logged, rate-limited GET from
``quant.data.sources.http`` and the SEC rules from ``quant.data.sources.sec`` (moved there unchanged, phase 3). This
module binds them to the pipeline's request ledger: ``RAW_INDEX`` / ``QUOTA_LEDGER`` are looked up at every call, so a
step that points the ledger at its own cache (the probe, the v2 archive fetcher) still works. Every request is
cached on disk before it is parsed; keys are read from the .env files inside Python and never printed or put in a
logged URL.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time  # noqa: F401  (tests patch common.time.sleep)

from quant.data import version as _version
from quant.data.sources import http
from quant.data.sources import sec as _sec
from quant.data.sources.http import (  # noqa: F401  (the pipeline's names for the shared plumbing)
    SECRET_PARAMS, SlidingWindowLimiter, atomic_write, parallel_map, read_env_key, redact, sha256_bytes, sha256_file)

# the main checkout, or the folder ``REVERSAL_DATA_MAIN_CHECKOUT`` names (testing only: a scratch copy laid out like
# it, so an offline rebuild can be checked without writing the real caches)
MAIN_CHECKOUT = _version.DATA_MAIN
# Data version (plan section 0): v1 is frozen and read only. ``REVERSAL_DATA_VERSION=v2`` points every step at the
# version-2 copies instead (a copy-on-write clone of the v1 cache and of the v1 inputs, built 2026-10-05), so a v2
# rebuild never writes a v1 file.
# ``REVERSAL_DATA_VERSION=v2.1`` (2026-10-10) is version 2 plus the Alpaca SIP fill: its own copies again (a clone of
# the v2 cache and inputs), so neither v1 nor v2 is written.
DATA_VERSION = _version.DATA_VERSION
VERSIONS = _version.VERSIONS
# True from version 2 on: the v2 fixes, archive fills and third votes apply (v2.1 adds the Alpaca fill on top)
V2_PLUS = _version.V2_PLUS
V2_1 = _version.IS_V2_1
V1_CACHE = _version.CACHE_ROOT / _version.CACHE_NAME["v1"]
V1_INPUTS = _version.INPUTS_PARENT / _version.INPUTS_NAME["v1"]
CACHE = _version.CACHE
INPUTS = _version.INPUTS_PARENT / _version.INPUTS_NAME[DATA_VERSION]   # relative to the working directory
# the v2 fill sources (archive.org Yahoo captures, companiesmarketcap, QuantQuote, QuantConnect), local only; read
# only under v2.1
V2_FILL = _version.CACHE_ROOT / "reversal_2012_2026_v2_fill"
# the v2.1 Alpaca SIP fill (raw bodies, parsed series, entity verdicts, request ledger), local only
V2_1_ALPACA = _version.CACHE_ROOT / "reversal_2012_2026_v2_1_alpaca"
RAW = CACHE / "raw"
RAW_INDEX = _version.RAW_INDEX
QUOTA_LEDGER = _version.QUOTA_LEDGER
SEC_PER_SECOND = _sec.SEC_PER_SECOND
SEC_USER_AGENT = _sec.user_agent()
SEC_LIMITER = _sec.SEC_LIMITER


def ledger() -> http.Ledger:
    """The request ledger in force now (a step may have pointed ``RAW_INDEX`` / ``QUOTA_LEDGER`` elsewhere)."""
    return http.Ledger(RAW_INDEX, QUOTA_LEDGER)


def log_request(source: str, url: str, status: int | str, data: bytes | None, cache_path: Path | None,
                symbol: str = "") -> None:
    http.log_request(ledger(), source, url, status, data, cache_path, symbol)


def quota_used(source: str, month: str | None = None, unique_symbols: bool = False) -> int:
    """Requests (or unique symbols) logged for ``source`` in ``month`` (YYYY-MM, default now)."""
    return http.quota_used(ledger(), source, month, unique_symbols)


def cached_get(url: str, cache_path: str | Path, *, source: str, headers: dict | None = None,
               limiter: SlidingWindowLimiter | None = None, symbol: str = "", timeout: float = 60,
               retries: int = 3, refetch: bool = False) -> bytes:
    """``quant.data.sources.http.cached_get`` logged to the pipeline's ledger (see there)."""
    return http.cached_get(url, cache_path, source=source, ledger=ledger(), headers=headers, limiter=limiter,
                           symbol=symbol, timeout=timeout, retries=retries, refetch=refetch)


def sec_headers() -> dict:
    return _sec.sec_headers(SEC_USER_AGENT)


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
