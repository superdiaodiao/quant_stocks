"""The data-version switch of the stock-level studies (docs/robustness_data_v2.md)."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _paths(version: str | None) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "REVERSAL_DATA_VERSION"}
    if version is not None:
        env["REVERSAL_DATA_VERSION"] = version
    code = ("from scripts import research_canslim_dev as cs, research_livermore as lv, research_oneil as on\n"
            "for p in (cs.INPUTS, cs.CACHE, cs.OUT, cs.EPS_CACHE, lv.OUT, lv.FROZEN, on.EPS_CACHE): print(p)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**env, "PYTHONPATH": str(ROOT)},
                         capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_default_is_v1_and_unchanged():
    inputs, cache, out, eps, lv_out, frozen, oneil_eps = _paths(None)
    assert inputs.endswith("output/research_only/reversal_2012_2026/inputs")
    assert cache.endswith("research_cache/reversal_2012_2026")
    assert out.endswith("output/research_only/canslim_dev_2017_2022")
    assert eps.endswith("research_cache/canslim_dev")
    assert lv_out.endswith("output/research_only/livermore")
    assert frozen.endswith("output/research_only/livermore/frozen_rule.json")
    assert oneil_eps.endswith("research_cache/oneil/eps_states_filed.csv.gz")
    assert _paths("v1") == _paths(None)


def test_v2_reads_v2_writes_v2_and_keeps_v1_frozen_rules():
    inputs, cache, out, eps, lv_out, frozen, oneil_eps = _paths("v2")
    assert inputs.endswith("output/research_only/reversal_2012_2026/inputs_v2")
    assert cache.endswith("research_cache/reversal_2012_2026_v2")
    assert out.endswith("output/research_only/canslim_dev_2017_2022_v2")
    assert eps.endswith("research_cache/canslim_dev_v2")
    assert lv_out.endswith("output/research_only/livermore_v2")
    assert frozen.endswith("output/research_only/livermore/frozen_rule.json")   # frozen rules never versioned
    assert oneil_eps.endswith("research_cache/oneil/eps_states_filed_v2.csv.gz")


def test_versioned_suffix_and_bad_value(monkeypatch):
    from scripts import study_data_version as dv
    monkeypatch.setattr(dv, "IS_V2", True)
    assert dv.versioned(Path("/a/b/x.csv.gz")) == Path("/a/b/x_v2.csv.gz")
    assert dv.versioned(Path("/a/b/dir")) == Path("/a/b/dir_v2")
    monkeypatch.setattr(dv, "IS_V2", False)
    assert dv.versioned(Path("/a/b/x.csv.gz")) == Path("/a/b/x.csv.gz")
    monkeypatch.setenv("REVERSAL_DATA_VERSION", "v3")
    with pytest.raises(ValueError):
        importlib.reload(dv)
    monkeypatch.delenv("REVERSAL_DATA_VERSION")
    importlib.reload(dv)
