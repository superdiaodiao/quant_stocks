"""The phase-2 library surface: what the studies and the forward observation take from quant.strategies / quant.data
instead of from other studies, the dependency rule, the legacy wrappers and the frozen strategy registry."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import market_cap
from quant.strategies import megacap, registry
from quant.strategies import selective_t as st

ROOT = Path(__file__).resolve().parents[2]


def test_market_caps_shares_vs_float_is_an_explicit_argument():
    """fundamentals / ml_cross_section / the S-MISP signal used to override research_megacap.SHARES_VS_FLOAT for one
    call; they now pass shares_vs_float=np.inf, and the default stays the megacap data rule (2.5)."""
    import inspect
    assert market_cap.SHARES_VS_FLOAT == 2.5
    assert inspect.signature(market_cap.market_caps).parameters["shares_vs_float"].default == 2.5
    for f in ("studies/fundamentals.py", "quant/strategies/ml_features.py", "quant/observation/smisp.py"):
        text = (ROOT / f).read_text()
        assert "shares_vs_float=np.inf" in text and "SHARES_VS_FLOAT =" not in text, f


def test_megacap_engine_binding_passes_the_half_spread(monkeypatch):
    """ml_cross_section's BLEND used to set research_megacap.QQQ_HS = ONEQ_HS around simulate; it passes qqq_hs."""
    seen = {}
    monkeypatch.setattr(megacap, "simulate_index_units", lambda *a, **k: seen.update(k) or "ok")
    assert megacap.simulate({1: []}, None, None, None, None, None, None) == "ok"
    assert seen["qqq_hs"] == megacap.QQQ_HS == 1e-4 and seen["band"] == 0.25 and seen["account"] == 10_000.0
    megacap.simulate({1: []}, None, None, None, None, None, None, qqq_hs=megacap.ONEQ_HS)
    assert seen["qqq_hs"] == megacap.ONEQ_HS == 2e-4


def test_no_study_imports_another_study_or_a_research_script():
    """docs/architecture.md section 3: studies import quant (and data-build scripts), never another study."""
    names = {p.stem for p in (ROOT / "studies").glob("*.py")}
    for path in list((ROOT / "studies").glob("*.py")) + list((ROOT / "quant").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for m in mods:
                assert not m.startswith("scripts.research_"), (path, m)
                assert not (m == "scripts" and isinstance(node, ast.ImportFrom)
                            and any(a.name.startswith("research_") for a in node.names)), (path, m)
                if path.parent.name == "studies":
                    assert not (m.startswith("studies.") and m.split(".")[1] in names), (path, m)
                elif path.name != "cli.py":
                    assert not m.startswith("studies"), (path, m)


def test_legacy_wrappers_alias_the_moved_modules():
    pairs = {"research_qqq_timing": "studies.qqq_timing", "research_livermore": "studies.livermore",
             "research_t_grid": "studies.t_grid", "research_spinoffs_sec": "quant.data.sources.sec_cache",
             "research_spinoffs_events": "quant.data.spinoff_events", "stop_rules": "quant.backtest.stop_rules",
             "forward_observation": "quant.observation.runner", "forward_smisp": "quant.observation.smisp",
             "forward_prices": "quant.observation.prices"}
    for old, new in pairs.items():
        assert importlib.import_module(f"scripts.{old}") is importlib.import_module(new), old
    wrappers = [p for p in (ROOT / "scripts").glob("research_*.py") if not p.name.startswith("research_v")]
    for p in wrappers:
        text = p.read_text()
        if "Moved to" in text:
            assert len(text.splitlines()) < 20, p


def test_registry_declares_the_observed_lines_once():
    r = registry
    assert [s.name for s in r.observed()] == ["S3-Yb", "SEL-A", "SEL-P", "S-MISP N10 k20 MN", "S/P top-10"]
    assert r.get("SEL-A").frozen["config_id"] == 29876 and r.get("SEL-P").frozen["config_id"] == 29916
    assert r.get("S-MISP N10 k20 MN").frozen == {"signal": "S-MISP", "n": 10, "k": 0.20, "mode": "MN",
                                                  "first_signal": "2026-10-30"}
    assert st.STOCKS is r.U18 and len(r.U18) == 18
    assert [x.name for x in megacap.RULES] == ["M1", "M2", "M3", "M4", "M5", "M6"]
    assert megacap.RULES[4] == megacap.Rule("M5", 20, 5, "m6", trend=True)
    assert megacap.RULES[5] == megacap.Rule("M6", 10, 10, capw=True)
    from quant.observation import runner, smisp
    assert runner.B1_CONFIG == "S3-Yb" and runner.B2_IDS == {"SEL-A": 29876, "SEL-P": 29916}
    assert runner.START_CLOSE == "2026-10-12" and runner.B2_RESERVE_CODE == 2 and runner.STOCKS == r.U18
    assert (smisp.CFG.signal, smisp.CFG.n, smisp.CFG.k, smisp.CFG.mode) == ("S-MISP", 10, 0.20, "MN")
    assert smisp.FIRST_SIGNAL == "2026-10-30"
    assert (ROOT / r.get("S/P top-10").frozen["algorithm"]).exists()


def test_paths_are_unchanged_by_the_moves():
    from quant.observation import prices, runner
    from studies import regime as rr
    from studies import selective_t as sts
    assert runner.LOG == ROOT / "docs/forward_observation_log.md"
    assert prices.STATE_DIR == ROOT / "state/forward_observation"
    assert sts.OUT == ROOT / "output/research_only/selective_t" and st.ROOT == ROOT
    assert str(st.RAW) == "/Users/bytedance/code/quant_stocks/research_cache/selective_t/raw"
    assert str(rr.RAW) == "/Users/bytedance/code/quant_stocks/research_cache/regime/raw"
    from quant.strategies import t_grid
    assert t_grid.TOP20 == ROOT / "output/research_only/megacap_v2/top20_by_month.csv"
    from quant.data import spinoff_events
    assert spinoff_events.OUT == ROOT / "output/research_only/spinoffs"
    assert isinstance(rr.HALF_SPREAD, dict) and pd.Timestamp(st.END) == pd.Timestamp(rr.END)
