"""Build data version 2 of the reversal 2012-2026 data offline, to a fixed point (plan section 0, 2026-10-05).

Every step runs with ``REVERSAL_DATA_VERSION=v2``, so it reads and writes only the version-2 copies
(``research_cache/reversal_2012_2026_v2/`` and ``output/research_only/reversal_2012_2026/inputs_v2/``); version 1 is
never written. No step makes a request (each runs with its offline switch; step 9 refuses the network itself).

One pass, in the order of the version-1 rebuilds:
  archive parse (the fetched captures -> series) -> prefilter -> yahoo -> reconcile (with the v2 fixes, fills and
  third votes) -> terminal -> earnings -> universe -> validate
The archive parse runs in the first pass only (a snapshot of what the fetch has cached); the other steps repeat until
the hashes of the pass's outputs equal the previous pass's (a fixed point), at most
``--max-passes``. Each pass's hashes go to ``CACHE_V2/v2_build/pass_N.json``; the log of each step to
``CACHE_V2/v2_build/logs``.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_build.py               # to a fixed point
    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_build.py --max-passes 1
    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_build.py --from reconcile   # start a pass at a step
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

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
MAIN = Path("/Users/bytedance/code/quant_stocks")
CACHE_V2 = MAIN / "research_cache" / "reversal_2012_2026_v2"
INPUTS_V2 = ROOT / "output" / "research_only" / "reversal_2012_2026" / "inputs_v2"
STEPS = [
    ("archive_parse", ["scripts/reversal_data_v2_archive.py", "parse"]),
    ("prefilter", ["scripts/reversal_data_prefilter.py", "--offline"]),
    ("yahoo", ["scripts/reversal_data_yahoo.py"]),
    ("reconcile", ["scripts/reversal_data_reconcile.py", "--rebuild"]),
    ("terminal", ["scripts/reversal_data_terminal.py", "--offline"]),
    ("earnings", ["scripts/reversal_data_earnings.py", "--offline"]),
    ("universe", ["scripts/reversal_data_universe.py"]),
    ("validate", ["scripts/reversal_data_validate.py"]),
]
# the outputs compared between passes (generated-at stamps inside JSON files are left out of the digest)
WATCH_INPUTS = ["candidate_fetch_list.csv", "unfillable.csv", "split_events.csv", "reviewed_moves.csv",
                "terminal_returns_2012_2026.csv", "exchange_moves.csv", "earnings_events.csv", "sic_history.csv",
                "weekly_universe_summary.csv", "weekly_universe_top300.csv.gz"]
WATCH_CACHE = ["prices/daily_panel.csv.gz", "reconcile/series_ends.csv", "universe/missing_by_security.csv",
               "universe/completeness_by_year.csv", "prefilter/tiingo_month2_plan.csv"]


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


def pass_hashes() -> dict:
    out = {f"inputs_v2/{n}": digest(INPUTS_V2 / n) for n in WATCH_INPUTS}
    out.update({f"cache_v2/{n}": digest(CACHE_V2 / n) for n in WATCH_CACHE})
    vs = INPUTS_V2 / "validation_summary.json"
    if vs.exists():
        v = json.loads(vs.read_text())
        checks = v.get("checks") or {}
        if isinstance(checks, dict):
            out["validate_status"] = {k: (c.get("status") if isinstance(c, dict) else c) for k, c in checks.items()}
        elif isinstance(checks, list):
            out["validate_status"] = {c.get("name"): c.get("status") for c in checks if isinstance(c, dict)}
    return out


def run_step(name: str, argv: list[str], log_dir: Path) -> float:
    env = {**os.environ, "REVERSAL_DATA_VERSION": "v2", "PYTHONPATH": str(ROOT)}
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
    ap.add_argument("--max-passes", type=int, default=3)
    ap.add_argument("--from", dest="start", default="", help="start the first pass at this step")
    args = ap.parse_args(argv)
    if not CACHE_V2.exists() or not INPUTS_V2.exists():
        raise SystemExit("the v2 copies are missing (clone the v1 cache and inputs first)")
    out = CACHE_V2 / "v2_build"
    (out / "logs").mkdir(parents=True, exist_ok=True)
    previous = None
    names = [n for n, _ in STEPS]
    for k in range(1, args.max_passes + 1):
        print(f"pass {k}", flush=True)
        # the archive captures are parsed once, in the first pass: the fetch may still be running, and a fixed point
        # needs the same inputs in every pass
        first = names.index(args.start) if (k == 1 and args.start) else (0 if k == 1 else 1)
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
