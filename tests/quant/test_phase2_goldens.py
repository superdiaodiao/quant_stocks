"""Golden regression for the phase-2 studies: the migrated studies write the same output files as the
pre-migration scripts (master 22ff8cf15).

tests/golden/phase2_studies_sha256.json has two parts:

- ``inprocess``: jobs re-run here. Each imports ``scripts.research_<study>`` (before the migration the script
  itself, after it the forwarding wrapper, i.e. the studies module), points its output folder attributes at a
  temporary folder, calls ``main(argv)`` and hashes every file written there. The golden hashes were produced by
  this same test on the pre-migration checkout (``QUANT_GOLDEN_WRITE=<file>``). ``slow`` jobs (> 60 s) run only
  with ``QUANT_GOLDEN_SLOW=1``.
- ``harness``: jobs that cannot be redirected in-process (one-shot tests that refuse a second run, data-version-2
  studies, studies that read their own earlier outputs, runs of several minutes). Their pre-migration hashes were
  recorded by running the old and the new command in two copies of the checkout with the same inputs; they are
  listed for the record and checked by hand (docs/architecture.md section 9).

Hashes: ``.gz`` files are hashed after decompression (gzip headers carry a timestamp); JSON files that record a
run time are hashed with the timing keys removed; everything else byte for byte. Needs the local research cache;
skipped without it or under data version 2.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib
import inspect
import json
import os
import time
from pathlib import Path

import pytest

from quant.data import version
from quant.paths import CACHE_ROOT

GOLDEN_FILE = Path(__file__).resolve().parents[1] / "golden/phase2_studies_sha256.json"
TIMING_KEYS = {"runtime_s", "elapsed_s", "seconds", "runtime_seconds", "elapsed"}

# job -> (script module, argv, attributes pointed at the temporary folder, slow)
JOBS = {
    "qqq_timing_dev": ("research_qqq_timing", [], ["OUT"], False),
    "qqq_timing_oneshot": ("research_qqq_timing", ["--mode", "oneshot", "--test-end", "2026-09-30"], ["OUT_ONESHOT"],
                           False),
    "dow_theory_dev": ("research_dow_theory", [], ["OUT"], False),
    "dow_theory_oneshot": ("research_dow_theory", ["--mode", "oneshot", "--test-end", "2026-09-30"], ["OUT"], False),
    "calendar": ("research_calendar", [], ["OUT"], False),
    "sector_lev": ("research_sector_lev", [], ["OUT"], False),
    "voltarget": ("research_voltarget", [], ["OUT"], False),
    "intraday_t": ("research_intraday_t", [], ["OUT"], False),
    "grid_check_index": ("research_grid_check", ["--setting", "index"], ["OUT"], False),
    "livermore_dev": ("research_livermore", [], ["OUT"], False),
    "indicators_dev": ("research_indicators", ["--dev", "both"], ["OUT"], False),
    "mean_reversion": ("research_mean_reversion", [], ["OUT"], False),
    "earnings_events": ("research_earnings_events", [], ["OUT"], False),
    "stops": ("research_stops", ["--skip-dev-check"], ["OUT"], False),
    "benchmark_composite": ("research_benchmark_composite", [], ["OUT"], False),
    "canslim_dev": ("research_canslim_dev", [], ["OUT"], True),
    "sec_alt": ("research_sec_alt", [], ["OUT"], True),
    "leverage_methods": ("research_leverage_methods", [], ["OUT"], True),
    "reversal_dev": ("research_reversal_dev", [], ["OUT"], True),
}


def strip_timing(x):
    if isinstance(x, dict):
        return {k: strip_timing(v) for k, v in x.items() if k not in TIMING_KEYS}
    if isinstance(x, list):
        return [strip_timing(v) for v in x]
    return x


def file_hash(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    elif path.suffix == ".json":
        try:
            obj = json.loads(data)
        except ValueError:
            obj = None
        if obj is not None and strip_timing(obj) != obj:
            data = json.dumps(strip_timing(obj), sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()


def run_job(name: str, tmp_path: Path, monkeypatch) -> dict:
    script, argv, attrs, _ = JOBS[name]
    mod = importlib.import_module(f"scripts.{script}")
    for a in attrs:
        monkeypatch.setattr(mod, a, tmp_path / a)
    if inspect.signature(mod.main).parameters:
        mod.main(list(argv))
    else:                                     # pre-migration research_benchmark_composite.main() took none
        mod.main()
    return {str(p.relative_to(tmp_path)): file_hash(p) for p in sorted(tmp_path.rglob("*")) if p.is_file()}


def golden() -> dict:
    return json.loads(GOLDEN_FILE.read_text()) if GOLDEN_FILE.exists() else {"inprocess": {}, "harness": {}}


@pytest.mark.parametrize("name", sorted(JOBS))
def test_study_output_matches_the_pre_migration_golden(name, tmp_path, monkeypatch):
    if not (CACHE_ROOT / "calendar/raw/chart_QQQ.json").exists() or not (version.CACHE / "prices").exists():
        pytest.skip("research cache not available")
    if version.IS_V2:
        pytest.skip("goldens are data version v1")
    if JOBS[name][3] and not os.environ.get("QUANT_GOLDEN_SLOW") and not os.environ.get("QUANT_GOLDEN_WRITE"):
        pytest.skip("slow golden (set QUANT_GOLDEN_SLOW=1)")
    t0 = time.time()
    try:
        got = run_job(name, tmp_path, monkeypatch)
    except FileNotFoundError as exc:          # benchmark_composite re-scores other studies' saved outputs
        if "output/research_only" in str(exc):
            pytest.skip(f"upstream study outputs not in this checkout: {exc.filename}")
        raise
    out = os.environ.get("QUANT_GOLDEN_WRITE")
    if out:                                   # producing the golden on the pre-migration checkout
        p = Path(out)
        doc = json.loads(p.read_text()) if p.exists() else {}
        doc[name] = {"argv": JOBS[name][1], "seconds": round(time.time() - t0, 1), "files": got}
        p.write_text(json.dumps(doc, indent=1, sort_keys=True))
        return
    want = golden()["inprocess"][name]["files"]
    assert sorted(got) == sorted(want)
    assert [f for f in got if got[f] != want[f]] == []


def test_golden_file_lists_every_harness_job():
    g = golden()
    assert set(g["inprocess"]) == set(JOBS)
    assert len(g["harness"]) >= 25
    for job, v in g["harness"].items():
        assert v["files"] and v["argv"] is not None, job
