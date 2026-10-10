"""Moved to quant/observation/prices.py (docs/architecture.md, phase 2).

This wrapper keeps the commands written in docs/forward_observation_checklist.md working
(``PYTHONPATH=. .venv/bin/python scripts/forward_prices.py``) and makes ``import scripts.forward_prices`` return
the moved module itself, so attribute reads and writes by other scripts and tests reach the real code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quant.observation import prices as _study  # noqa: E402

if __name__ == "__main__":
    raise SystemExit("library module of the forward observation; run scripts/forward_observation.py")
else:
    sys.modules[__name__] = _study
