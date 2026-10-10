"""Moved to pipelines/megacap_oos2/build.py (docs/architecture.md, phase 3).

Running this file runs that module as a script; importing it returns that module itself, so the commands in
docs/reversal_* and the ledgers keep working."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if __name__ == "__main__":
    runpy.run_module("pipelines.megacap_oos2.build", run_name="__main__", alter_sys=True)
else:
    from pipelines.megacap_oos2 import build as _module  # noqa: E402

    sys.modules[__name__] = _module
