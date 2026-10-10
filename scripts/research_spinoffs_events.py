"""Moved to quant/data/spinoff_events.py (docs/architecture.md section 5). Running this file runs that module;
importing it returns that module itself, so the ledger commands and every caller keep working."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quant.data import spinoff_events as _study  # noqa: E402

if __name__ == "__main__":
    raise SystemExit("run the event build with: scripts/research_spinoffs.py events")
else:
    sys.modules[__name__] = _study
