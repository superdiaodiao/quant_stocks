"""Build data version 2.1 (v2 + the Alpaca SIP fill) of the reversal 2012-2026 data offline, to a fixed point
(plan section 0, 2026-10-10), as reversal_data_v2_build.py builds v2.

Every step runs with ``REVERSAL_DATA_VERSION=v2.1``, so it reads and writes only the v2.1 copies
(``research_cache/reversal_2012_2026_v2_1/`` and ``output/research_only/reversal_2012_2026/inputs_v2_1/``, clones of
the v2 ones); v1 and v2 are never written. No step makes a request. The archive captures are not re-parsed (the v2
fill's parsed series are read as they are); the Alpaca series are those ``reversal_data_v2_1_alpaca.py confirm``
accepted (run it first).

One pass: prefilter -> yahoo -> reconcile (v2 fixes, Alpaca rows, archive fills, third votes) -> terminal ->
earnings -> universe -> validate, repeated until the watched hashes equal the previous pass's (at most
``--max-passes``). Hashes and logs: ``CACHE_V2_1/v2_1_build/``.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_1_build.py
    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_1_build.py --from reconcile
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from scripts.reversal_data_v2_build import WATCH_CACHE, WATCH_INPUTS, digest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
MAIN = Path("/Users/bytedance/code/quant_stocks")
CACHE_V2_1 = MAIN / "research_cache" / "reversal_2012_2026_v2_1"
INPUTS_V2_1 = ROOT / "output" / "research_only" / "reversal_2012_2026" / "inputs_v2_1"
STEPS = [
    ("prefilter", ["scripts/reversal_data_prefilter.py", "--offline"]),
    ("yahoo", ["scripts/reversal_data_yahoo.py"]),
    ("reconcile", ["scripts/reversal_data_reconcile.py", "--rebuild"]),
    ("terminal", ["scripts/reversal_data_terminal.py", "--offline"]),
    ("earnings", ["scripts/reversal_data_earnings.py", "--offline"]),
    ("universe", ["scripts/reversal_data_universe.py"]),
    ("validate", ["scripts/reversal_data_validate.py"]),
]


def pass_hashes() -> dict:
    out = {f"inputs_v2_1/{n}": digest(INPUTS_V2_1 / n) for n in WATCH_INPUTS}
    out.update({f"cache_v2_1/{n}": digest(CACHE_V2_1 / n) for n in WATCH_CACHE})
    vs = INPUTS_V2_1 / "validation_summary.json"
    if vs.exists():
        v = json.loads(vs.read_text())
        checks = v.get("checks") or {}
        if isinstance(checks, dict):
            out["validate_status"] = {k: (c.get("status") if isinstance(c, dict) else c) for k, c in checks.items()}
        elif isinstance(checks, list):
            out["validate_status"] = {c.get("name"): c.get("status") for c in checks if isinstance(c, dict)}
    return out


def run_step(name: str, argv: list[str], log_dir: Path) -> float:
    env = {**os.environ, "REVERSAL_DATA_VERSION": "v2.1", "PYTHONPATH": str(ROOT)}
    started = time.time()
    log = log_dir / f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}_{name}.log"
    with log.open("w") as handle:
        code = subprocess.run([PY] + argv, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT).returncode
    took = time.time() - started
    print(f"  {name}: exit {code} in {took / 60:.1f} min ({log.name})", flush=True)
    if code != 0 and name != "validate":  # validate exits non-zero when checks fail; that is a result, not an error
        raise SystemExit(f"step {name} failed with exit {code}; see {log}")
    return took


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-passes", type=int, default=4)
    ap.add_argument("--from", dest="start", default="", help="start the first pass at this step")
    args = ap.parse_args(argv)
    if not CACHE_V2_1.exists() or not INPUTS_V2_1.exists():
        raise SystemExit("the v2.1 copies are missing (clone the v2 cache and inputs first)")
    out = CACHE_V2_1 / "v2_1_build"
    (out / "logs").mkdir(parents=True, exist_ok=True)
    previous = None
    names = [n for n, _ in STEPS]
    for k in range(1, args.max_passes + 1):
        print(f"pass {k}", flush=True)
        first = names.index(args.start) if (k == 1 and args.start) else 0
        timings = {}
        for name, step in STEPS[first:]:
            timings[name] = round(run_step(name, step, out / "logs"), 1)
        hashes = pass_hashes()
        record = {"pass": k, "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                  "timings_s": timings, "hashes": hashes}
        changed = sorted(key for key in hashes if previous is None or previous.get(key) != hashes[key])
        record["changed_vs_previous_pass"] = changed if previous is not None else "first pass"
        (out / f"pass_{k}.json").write_text(json.dumps(record, indent=1) + "\n")
        print(f"  changed vs previous pass: {record['changed_vs_previous_pass']}", flush=True)
        if previous is not None and not changed:
            print(f"fixed point after pass {k}", flush=True)
            (out / "fixed_point.json").write_text(json.dumps({"pass": k, **record}, indent=1) + "\n")
            return 0
        previous = hashes
    print("no fixed point within the pass limit", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
