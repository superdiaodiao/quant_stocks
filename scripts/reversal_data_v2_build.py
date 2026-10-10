"""Replaced by pipelines/reversal_data/build.py, the one runner for every data version (docs/architecture.md,
phase 3). Running this file runs ``build --version v2`` with the same options (--max-passes, --from); importing
it returns the runner module."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipelines.reversal_data import build as _build  # noqa: E402

if __name__ == "__main__":
    sys.exit(_build.main(["--version", "v2", *sys.argv[1:]]))
else:
    sys.modules[__name__] = _build
