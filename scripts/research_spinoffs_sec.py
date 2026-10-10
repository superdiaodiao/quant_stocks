"""Moved to quant/data/sources/sec_cache.py (docs/architecture.md, phase 2).

This wrapper keeps the commands written in docs/research_ledger_spinoffs.md working
(``PYTHONPATH=. .venv/bin/python scripts/research_spinoffs_sec.py``) and makes ``import scripts.research_spinoffs_sec`` return
the moved module itself, so attribute reads and writes by other scripts and tests reach the real code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quant.data.sources import sec_cache as _study  # noqa: E402

if __name__ == "__main__":
    raise SystemExit("this is a library module (quant.data.sources.sec_cache), not a command")
else:
    sys.modules[__name__] = _study
