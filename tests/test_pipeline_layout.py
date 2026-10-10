"""Phase 3 (docs/architecture.md): the data pipeline lives in pipelines/, vendor code in quant/data/sources/, the data
version in quant.data.version; the old scripts/ paths are forwarding wrappers. No data, no network."""
import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STEPS = ("common earnings factors form25 listings prefilter reconcile review security_master terminal tiingo universe "
         "v2_1_alpaca v2_1_fill v2_1_report v2_archive v2_fill v2_report validate wiki yahoo").split()
WRAPPERS = {**{f"reversal_data_{s}": f"pipelines.reversal_data.{s}" for s in STEPS},
            "data_source_probe": "pipelines.reversal_data.source_probe",
            "robustness_data_v2_compare": "pipelines.reversal_data.robustness_v2_compare",
            "megacap_oos2_data": "pipelines.megacap_oos2.build",
            "reversal_data_v2_build": "pipelines.reversal_data.build",
            "reversal_data_v2_1_build": "pipelines.reversal_data.build",
            "study_data_version": "quant.data.version"}


def _run(args, **env):
    full = {**{k: v for k, v in os.environ.items() if k != "REVERSAL_DATA_VERSION"}, "PYTHONPATH": str(ROOT), **env}
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=full, capture_output=True, text=True)


@pytest.mark.parametrize("old,new", sorted(WRAPPERS.items()))
def test_importing_an_old_path_returns_the_new_module(old, new):
    assert importlib.import_module(f"scripts.{old}") is importlib.import_module(new)


@pytest.mark.parametrize("old", ["reversal_data_validate", "reversal_data_prefilter", "reversal_data_v2_archive",
                                 "data_source_probe", "megacap_oos2_data"])
def test_running_an_old_path_runs_the_step(old):
    out = _run([f"scripts/{old}.py", "--help"])
    assert out.returncode == 0, out.stderr
    assert "usage:" in out.stdout


@pytest.mark.parametrize("old,version", [("reversal_data_v2_build", "v2"), ("reversal_data_v2_1_build", "v2.1")])
def test_the_old_build_commands_call_the_runner_with_their_version(old, version, monkeypatch):
    from pipelines.reversal_data import build
    seen = {}
    monkeypatch.setattr(build, "main", lambda argv=None: seen.setdefault("argv", argv) and 0)
    monkeypatch.setattr(sys, "argv", [f"scripts/{old}.py", "--max-passes", "1"])
    import runpy
    with pytest.raises(SystemExit):
        runpy.run_path(str(ROOT / "scripts" / f"{old}.py"), run_name="__main__")
    assert seen["argv"] == ["--version", version, "--max-passes", "1"]


def test_the_runner_plans_each_version_and_refuses_v1_without_allow():
    from pipelines.reversal_data import build
    assert [s for s, _ in build.PLANS["v2"]["steps"]][0] == "archive_parse" and build.PLANS["v2"]["first_pass_only"]
    assert "archive_parse" not in [s for s, _ in build.PLANS["v2.1"]["steps"]]
    assert [s for s, _ in build.PLANS["v1"]["steps"]] == [s for s, _ in build.PLANS["v2.1"]["steps"]]
    assert build.PLANS["v2"]["dir"] == "v2_build" and build.PLANS["v2.1"]["dir"] == "v2_1_build"
    out = _run(["-m", "pipelines.reversal_data.build", "--version", "v1"])
    assert out.returncode != 0 and "--allow-v1" in out.stderr


def test_pass_hash_keys_keep_the_old_names(tmp_path, monkeypatch):
    from pipelines.reversal_data import build
    monkeypatch.setattr(build, "cache_dir", lambda v: tmp_path / "c")
    monkeypatch.setattr(build, "inputs_dir", lambda v: tmp_path / "i")
    assert "inputs_v2_1/unfillable.csv" in build.pass_hashes("v2.1")
    assert "cache_v2/prices/daily_panel.csv.gz" in build.pass_hashes("v2")
    assert "inputs/unfillable.csv" in build.pass_hashes("v1")


def test_quant_data_version_declares_v2_1():
    code = ("from quant.data import version as v; from pipelines.reversal_data import common as c; "
            "print(v.DATA_VERSION, v.CACHE.name, v.INPUTS.name, v.IS_V2, v.IS_V2_1, v.versioned('/a/x.csv').name, "
            "c.CACHE == v.CACHE, c.INPUTS.name)")
    assert _run(["-c", code], REVERSAL_DATA_VERSION="v2.1").stdout.split() == [
        "v2.1", "reversal_2012_2026_v2_1", "inputs_v2_1", "False", "True", "x_v2_1.csv", "True", "inputs_v2_1"]
    assert _run(["-c", code], REVERSAL_DATA_VERSION="v2").stdout.split()[:6] == [
        "v2", "reversal_2012_2026_v2", "inputs_v2", "True", "False", "x_v2.csv"]
    assert _run(["-c", code]).stdout.split()[:6] == [
        "v1", "reversal_2012_2026", "inputs", "False", "False", "x.csv"]


def test_a_scratch_main_checkout_moves_every_versioned_cache(tmp_path):
    code = "from pipelines.reversal_data import common as c; print(c.CACHE); print(c.V2_FILL); print(c.RAW_INDEX)"
    out = _run(["-c", code], REVERSAL_DATA_MAIN_CHECKOUT=str(tmp_path)).stdout.split()
    assert all(line.startswith(str(tmp_path / "research_cache")) for line in out), out


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
    return names


def test_quant_and_studies_import_no_pipeline_and_no_data_script():
    for path in sorted((ROOT / "quant").rglob("*.py")) + sorted((ROOT / "studies").rglob("*.py")):
        for name in _imports(path):
            assert not name.startswith("pipelines"), (path, name)
            assert not any(s in name for s in ("reversal_data_", "megacap_oos2_data", "data_source_probe",
                                               "study_data_version")), (path, name)


def test_the_borrow_fee_recorder_stays_self_contained():
    # .github/workflows/borrow_fees.yml checks out only this file and runs it with a bare python3
    names = _imports(ROOT / "scripts" / "record_borrow_fees.py")
    assert all(n.split(".")[0] in sys.stdlib_module_names or n == "__future__" or n.startswith("__future__.")
               for n in names), names
    assert "scripts/record_borrow_fees.py" in (ROOT / ".github/workflows/borrow_fees.yml").read_text()
