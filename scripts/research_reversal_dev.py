"""Moved to studies/reversal_dev.py (docs/architecture.md, phase 2).

This wrapper keeps the commands written in docs/research_ledger_reversal_dev.md working
(``PYTHONPATH=. .venv/bin/python scripts/research_reversal_dev.py``) and makes ``import scripts.research_reversal_dev`` return
the moved module itself, so attribute reads and writes by other scripts and tests reach the real code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studies import reversal_dev as _study  # noqa: E402

if __name__ == "__main__":
    _study.main()
else:
    sys.modules[__name__] = _study
