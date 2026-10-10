"""The forward-observation workflow keeps working after the move to quant/observation (docs/architecture.md, phase 2).

.github/workflows/forward_observation.yml runs ``PYTHONPATH=. python scripts/forward_observation.py $args``
unchanged; the script forwards to quant.observation.runner. These checks need no data: the entry point parses its
arguments in a fresh interpreter (where it must set REVERSAL_DATA_VERSION=v2 before quant.data.version loads), and
a dry run on the synthetic fixtures is covered by tests/test_forward_observation.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_workflow_still_calls_the_wrapper():
    wf = (ROOT / ".github/workflows/forward_observation.yml").read_text()
    assert "PYTHONPATH=. python scripts/forward_observation.py $args" in wf
    assert (ROOT / "scripts/forward_observation.py").exists()


def test_entry_point_runs_in_a_fresh_interpreter():
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    env.pop("REVERSAL_DATA_VERSION", None)
    cp = subprocess.run([sys.executable, "scripts/forward_observation.py", "--help"], cwd=ROOT, env=env,
                        capture_output=True, text=True, timeout=300)
    assert cp.returncode == 0, cp.stderr
    assert "--dry-run" in cp.stdout and "--fetch-smisp" in cp.stdout
    probe = ("import scripts.forward_observation as fo, quant.observation.runner as r, quant.data.version as v; "
             "print(fo is r, v.DATA_VERSION)")
    cp = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.split() == ["True", "v2"]
