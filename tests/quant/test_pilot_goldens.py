"""Golden regression: the migrated pilot studies write byte-identical output files.

tests/golden/pilot_studies_sha256.json holds the SHA-256 of every file the pre-migration
scripts/research_<name>.py wrote (master dc99a861f). Each test re-runs studies/<name>.py into a temporary folder
and compares. Needs the local research cache (about 50 s in total); skipped without it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from quant.data import version
from quant.paths import CACHE_ROOT

GOLDEN = json.loads((Path(__file__).resolve().parents[1] / "golden/pilot_studies_sha256.json").read_text())["studies"]
NEEDS = {"regime": CACHE_ROOT / "regime/raw/chart_QQQ.json",
         "megacap": version.CACHE / "prices/daily_panel.csv.gz",
         "selective_t": CACHE_ROOT / "selective_t/raw/AAPL.json.gz"}
ARGS = {"regime": argparse.Namespace(), "megacap": argparse.Namespace(check_ranks=False),
        "selective_t": argparse.Namespace(fetch=False)}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_study_output_is_byte_identical(name, tmp_path, monkeypatch):
    if not NEEDS[name].exists() or not (CACHE_ROOT / "calendar/raw/chart_QQQ.json").exists():
        pytest.skip("research cache not available")
    if version.IS_V2:
        pytest.skip("goldens are data version v1")
    study = __import__(f"studies.{name}", fromlist=["run"])
    monkeypatch.setattr(study, "OUT", tmp_path)
    study.run(ARGS[name])
    got = {p.name: sha256(p) for p in sorted(tmp_path.iterdir())}
    assert sorted(got) == sorted(GOLDEN[name])
    assert [f for f in got if got[f] != GOLDEN[name][f]] == []


def test_legacy_wrappers_are_the_study_modules():
    """``scripts.research_<name>`` is the studies module object (ledger commands, mc.X / st.X library users)."""
    import scripts.research_megacap as mc
    import scripts.research_regime as rr
    import scripts.research_selective_t as st
    from studies import megacap, regime, selective_t
    assert rr is regime and mc is megacap and st is selective_t
