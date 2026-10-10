"""Un-migrated scripts use the pilot studies as libraries (``mc.X``, ``st.X``, ``rr.X``). These tests pin the parts of
that surface that are not exercised by the golden runs: temporary overrides of module constants must still reach
the quant code (research_fundamentals, research_ml_cross_section and forward_smisp set ``mc.SHARES_VS_FLOAT``;
research_ml_cross_section sets ``mc.QQQ_HS``)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.data import market_cap
from studies import megacap as mc
from studies import regime as rr
from studies import selective_t as st


def test_shares_vs_float_override_reaches_market_caps(monkeypatch):
    seen = {}
    monkeypatch.setattr(market_cap, "market_caps", lambda *a, **k: seen.update(k) or "ok")
    monkeypatch.setattr(mc, "SHARES_VS_FLOAT", np.inf)
    assert mc.market_caps(None, None, None, None, None, None) == "ok"
    assert seen["shares_vs_float"] == np.inf


def test_qqq_half_spread_override_reaches_the_engine(monkeypatch):
    seen = {}
    monkeypatch.setattr(mc, "simulate_index_units", lambda *a, **k: seen.update(k) or "ok")
    monkeypatch.setattr(mc, "QQQ_HS", mc.ONEQ_HS)
    assert mc.simulate({1: []}, None, None, None, None, None, None) == "ok"
    assert seen["qqq_hs"] == mc.ONEQ_HS and seen["band"] == 0.25 and seen["account"] == 10_000.0


def test_library_names_other_scripts_read_are_present():
    for name in ("ACCOUNT", "FULL", "ONEQ_HS", "PERIODS", "QQQ_HS", "RULES", "Rule", "SHARES_VS_FLOAT",
                 "_facts_from_payload", "_monthly", "_value_at", "_yearly", "add_predecessor_facts", "build_targets",
                 "buy_hold", "candidates", "criteria", "extract_share_facts", "latest_fact_asof", "load_company_lists",
                 "longest_drawdown_days", "market_caps", "max_drawdown", "momentum_frames", "signal_sessions",
                 "simulate", "successor_ciks", "window_metrics"):
        assert hasattr(mc, name), name
    for name in ("END", "FETCH_START", "Fam", "HALF_SPREAD_BPS", "HEADERS", "MAIN", "Market", "ONEQ_FIRST_RETURN",
                 "ROOT", "S2_SLIP", "SECONDS_PER_REQUEST", "START_EQUITY", "STOCKS", "STOCK_HALF_SPREAD_BPS",
                 "STOP_CODES", "Spec", "basket_mean", "bh_reject", "chart_url", "fams_of", "load_qqq_like",
                 "load_stock", "market_from_frame", "match_returns", "ohlc_frame", "panel_check", "raw_path",
                 "rsi_wilder", "run_one", "simulate", "variant_spec", "hs_of", "exit_limit", "Cycle"):
        assert hasattr(st, name), name
    for name in ("Data", "END", "RULES", "buy_hold", "entry_index", "evaluate", "halves", "inverse_vol_weights",
                 "load_data", "month_end_mask", "monthly_only", "order_cost", "period_metrics", "simulate",
                 "sma_state", "target_weights", "trailing_rf", "two_state", "vol_state"):
        assert hasattr(rr, name), name


def test_paths_are_unchanged_by_the_move():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    assert rr.OUT == root / "output/research_only/regime"
    assert st.OUT == root / "output/research_only/selective_t" and st.ROOT == root
    assert mc.OUT.parent == root / "output/research_only"
    assert str(st.RAW) == "/Users/bytedance/code/quant_stocks/research_cache/selective_t/raw"
    assert str(rr.RAW) == "/Users/bytedance/code/quant_stocks/research_cache/regime/raw"
    assert mc.LISTS.name == "lists.csv.gz" and mc.SHARES_CACHE.name.startswith("sec_share_facts")
    assert isinstance(rr.HALF_SPREAD, dict) and pd.Timestamp(st.END) == pd.Timestamp(rr.END)
