"""Moved to quant/backtest/stop_rules.py (docs/architecture.md section 5). Running this file runs that module;
importing it returns that module itself, so the ledger commands and every caller keep working."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quant.backtest import stop_rules as _study  # noqa: E402

if __name__ == "__main__":
    raise SystemExit("stop_rules is a library (quant.backtest.stop_rules), not a command")
else:
    sys.modules[__name__] = _study
