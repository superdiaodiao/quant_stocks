"""Moved to studies/sector_lev.py (docs/architecture.md, phase 2).

This wrapper keeps the commands written in docs/research_ledger_sector_lev.md working
(``PYTHONPATH=. .venv/bin/python scripts/research_sector_lev.py``) and makes ``import scripts.research_sector_lev`` return
the moved module itself, so attribute reads and writes by other scripts and tests reach the real code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studies import sector_lev as _study  # noqa: E402

if __name__ == "__main__":
    _study.main()
else:
    sys.modules[__name__] = _study
