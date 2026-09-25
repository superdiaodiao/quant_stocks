#!/usr/bin/env bash
# Restore the pinned data release into this checkout for a v50r3 run on a
# fresh machine (a GitHub-hosted runner), as setup_v50r3_live.sh does for a
# local live copy: download and verify the archive, put back every tracked
# file the archive may overwrite, and create the empty SEC Company Facts
# cache that the SIGNAL refresh fills.
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/download_data_release.sh --force
git checkout -- .
PYTHONPATH=. python - <<'PY'
from pathlib import Path

from src.io.fundamentals_update import (
    SEC_COMPANYFACTS_CACHE_DIR,
    write_companyfacts_cache_manifest,
)

cache = Path(SEC_COMPANYFACTS_CACHE_DIR)
if not (cache / "manifest.json").is_file():
    cache.mkdir(parents=True, exist_ok=True)
    write_companyfacts_cache_manifest(cache)
PY
