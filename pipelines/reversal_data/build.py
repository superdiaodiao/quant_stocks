"""Build one data version of the reversal 2012-2026 data offline, to a fixed point (plan section 0).

One runner for every version (it replaces scripts/reversal_data_v2_build.py, 2026-10-05, and
scripts/reversal_data_v2_1_build.py, 2026-10-10; those commands still work and call this with ``--version v2`` /
``--version v2.1``). Every step runs as ``python -m pipelines.reversal_data.<step>`` with
``REVERSAL_DATA_VERSION=<version>``, so it reads and writes only that version's copies
(``research_cache/<cache>/`` and ``output/research_only/reversal_2012_2026/<inputs>/``, see ``quant.data.version``).
No step makes a request (each runs with its offline switch; validate refuses the network itself).

Steps of one pass, in the order of the version-1 rebuilds:
  [v2 only, first pass only: archive parse (the fetched captures -> series)] -> prefilter -> yahoo ->
  reconcile -> terminal -> earnings -> universe -> validate
- ``v2``: reconcile applies the v2 fixes, archive fills and third votes. The archive parse runs in the first pass
  only (a snapshot of what the fetch has cached); the other steps repeat until a fixed point.
- ``v2.1``: v2 plus the Alpaca SIP fill. The archive captures are not re-parsed (the v2 fill's parsed series are read
  as they are); the Alpaca series are those ``reversal_data_v2_1_alpaca.py confirm`` accepted (run it first).
- ``v1``: frozen (plan section 0). Rebuilding it rewrites the frozen v1 files, so it needs ``--allow-v1``; it is
  meant for scratch copies (``REVERSAL_DATA_MAIN_CHECKOUT``), e.g. to show that a code change leaves v1 unchanged.

The steps repeat until the hashes of the pass's outputs equal the previous pass's (a fixed point), at most
``--max-passes``. Each pass's hashes go to ``<cache>/<build dir>/pass_N.json`` (``v2_build``, ``v2_1_build``,
``v1_build``); the log of each step to ``<cache>/<build dir>/logs``.

Usage::

    PYTHONPATH=. .venv/bin/python -m pipelines.reversal_data.build --version v2.1          # to a fixed point
    PYTHONPATH=. .venv/bin/python -m pipelines.reversal_data.build --version v2 --max-passes 1
    PYTHONPATH=. .venv/bin/python -m pipelines.reversal_data.build --version v2 --from reconcile   # start a pass at a step
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from quant.data import version as dv

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
STEPS = [
    ("archive_parse", ["v2_archive", "parse"]),
    ("prefilter", ["prefilter", "--offline"]),
    ("yahoo", ["yahoo"]),
    ("reconcile", ["reconcile", "--rebuild"]),
    ("terminal", ["terminal", "--offline"]),
    ("earnings", ["earnings", "--offline"]),
    ("universe", ["universe"]),
    ("validate", ["validate"]),
]
# per version: the steps of a pass, whether the first step runs in the first pass only, the default pass limit, the
# build folder and what the copies are cloned from
PLANS = {
    "v1": {"steps": [s for s in STEPS if s[0] != "archive_parse"], "first_pass_only": False, "max_passes": 3,
           "dir": "v1_build", "missing": "the v1 cache and inputs are missing"},
    "v2": {"steps": STEPS, "first_pass_only": True, "max_passes": 3, "dir": "v2_build",
           "missing": "the v2 copies are missing (clone the v1 cache and inputs first)"},
    "v2.1": {"steps": [s for s in STEPS if s[0] != "archive_parse"], "first_pass_only": False, "max_passes": 4,
             "dir": "v2_1_build", "missing": "the v2.1 copies are missing (clone the v2 cache and inputs first)"},
}
# the outputs compared between passes (generated-at stamps inside JSON files are left out of the digest)
WATCH_INPUTS = ["candidate_fetch_list.csv", "unfillable.csv", "split_events.csv", "reviewed_moves.csv",
                "terminal_returns_2012_2026.csv", "exchange_moves.csv", "earnings_events.csv", "sic_history.csv",
                "weekly_universe_summary.csv", "weekly_universe_top300.csv.gz"]
WATCH_CACHE = ["prices/daily_panel.csv.gz", "reconcile/series_ends.csv", "universe/missing_by_security.csv",
               "universe/completeness_by_year.csv", "prefilter/tiingo_month2_plan.csv"]


def cache_dir(version: str) -> Path:
    return dv.CACHE_ROOT / dv.CACHE_NAME[version]


def inputs_dir(version: str) -> Path:
    return ROOT / dv.INPUTS_PARENT / dv.INPUTS_NAME[version]


def digest(path: Path) -> str:
    if not path.exists():
        return "missing"
    h = hashlib.sha256()
    if path.suffix == ".gz":
        import gzip
        with gzip.open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    else:
        h.update(path.read_bytes())
    return h.hexdigest()


def pass_hashes(version: str) -> dict:
    tag = dv.INPUTS_NAME[version][len("inputs"):]   # "", "_v2", "_v2_1"
    inputs, cache = inputs_dir(version), cache_dir(version)
    out = {f"inputs{tag}/{n}": digest(inputs / n) for n in WATCH_INPUTS}
    out.update({f"cache{tag}/{n}": digest(cache / n) for n in WATCH_CACHE})
    vs = inputs / "validation_summary.json"
    if vs.exists():
        v = json.loads(vs.read_text())
        checks = v.get("checks") or {}
        if isinstance(checks, dict):
            out["validate_status"] = {k: (c.get("status") if isinstance(c, dict) else c) for k, c in checks.items()}
        elif isinstance(checks, list):
            out["validate_status"] = {c.get("name"): c.get("status") for c in checks if isinstance(c, dict)}
    return out


def run_step(version: str, name: str, argv: list[str], log_dir: Path) -> float:
    env = {**os.environ, "REVERSAL_DATA_VERSION": version, "PYTHONPATH": str(ROOT)}
    started = time.time()
    log = log_dir / f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}_{name}.log"
    module, *args = argv
    with log.open("w") as handle:
        code = subprocess.run([PY, "-m", f"pipelines.reversal_data.{module}"] + args, cwd=ROOT, env=env,
                              stdout=handle, stderr=subprocess.STDOUT).returncode
    took = time.time() - started
    print(f"  {name}: exit {code} in {took / 60:.1f} min ({log.name})", flush=True)
    if code != 0 and name != "validate":  # validate exits non-zero when checks fail; that is a result, not an error
        raise SystemExit(f"step {name} failed with exit {code}; see {log}")
    return took


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build one data version offline, to a fixed point.")
    ap.add_argument("--version", required=True, choices=dv.VERSIONS)
    ap.add_argument("--max-passes", type=int, default=None, help="default 3 (v1, v2) or 4 (v2.1)")
    ap.add_argument("--from", dest="start", default="", help="start the first pass at this step")
    ap.add_argument("--allow-v1", action="store_true", help="v1 is frozen: rebuild it only on purpose")
    args = ap.parse_args(argv)
    version = args.version
    plan = PLANS[version]
    if version == "v1" and not args.allow_v1:
        raise SystemExit("v1 is frozen (plan section 0): pass --allow-v1 to rebuild it (meant for scratch copies)")
    cache, inputs = cache_dir(version), inputs_dir(version)
    if not cache.exists() or not inputs.exists():
        raise SystemExit(plan["missing"])
    steps = plan["steps"]
    max_passes = args.max_passes if args.max_passes is not None else plan["max_passes"]
    out = cache / plan["dir"]
    (out / "logs").mkdir(parents=True, exist_ok=True)
    previous = None
    names = [n for n, _ in steps]
    for k in range(1, max_passes + 1):
        print(f"pass {k}", flush=True)
        # v2: the archive captures are parsed once, in the first pass: the fetch may still be running, and a fixed
        # point needs the same inputs in every pass
        if k == 1:
            first = names.index(args.start) if args.start else 0
        else:
            first = 1 if plan["first_pass_only"] else 0
        timings = {}
        for name, step in steps[first:]:
            timings[name] = round(run_step(version, name, step, out / "logs"), 1)
        hashes = pass_hashes(version)
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
