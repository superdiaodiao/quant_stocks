import pandas as pd
import pytest

from scripts import research_v26_large_liquid_stock_momentum as v26


def _spec(*, pool_size=25, top_n=3):
    return {
        "lookback_sessions": 63,
        "skip_recent_sessions": 0,
        "liquid_pool_size": pool_size,
        "top_n": top_n,
        "signal_frequency": "monthly",
        "rank_buffer_multiple": 1,
        "quality_mode": "profitable",
    }


def test_candidate_grid_is_monthly_large_liquid_common_stock_only():
    specs = v26.candidate_specs()

    assert len(specs) == 18
    assert {spec["liquid_pool_size"] for spec in specs} == {25, 50, 100}
    assert {spec["top_n"] for spec in specs} == {3, 5}
    assert {spec["signal_frequency"] for spec in specs} == {"monthly"}
    assert {spec["quality_mode"] for spec in specs} == {"profitable"}
    assert "QQQ" in v26.FORBIDDEN_ETFS


def test_liquid_pool_is_formed_before_momentum_ranking(monkeypatch):
    technical = pd.DataFrame({
        "median_dollar_volume_50d": [100.0, 90.0, 80.0],
        "momentum_excess_vs_nasdaq": [0.1, 0.3, 0.2],
    }, index=["A", "B", "C"])
    monkeypatch.setattr(
        v26.v24,
        "_technical_ranking",
        lambda *_args, **_kwargs: technical,
    )
    monkeypatch.setattr(
        v26.v24,
        "_profitable_symbols",
        lambda *_args, **_kwargs: {"A", "B", "C"},
    )
    inputs = {"large_liquid_cache": {}}

    actual = v26._large_liquid_ranking(
        pd.Timestamp("2024-01-31"), _spec(pool_size=2), inputs
    )

    assert list(actual.index) == ["B", "A"]
    assert "C" not in actual.index


def test_unprofitable_name_is_removed_before_liquid_pool(monkeypatch):
    technical = pd.DataFrame({
        "median_dollar_volume_50d": [100.0, 90.0],
        "momentum_excess_vs_nasdaq": [0.4, 0.3],
    }, index=["LOSS", "PROFIT"])
    monkeypatch.setattr(
        v26.v24,
        "_technical_ranking",
        lambda *_args, **_kwargs: technical,
    )
    monkeypatch.setattr(
        v26.v24,
        "_profitable_symbols",
        lambda *_args, **_kwargs: {"PROFIT"},
    )

    actual = v26._large_liquid_ranking(
        pd.Timestamp("2024-01-31"),
        _spec(pool_size=25),
        {"large_liquid_cache": {}},
    )

    assert list(actual.index) == ["PROFIT"]


def test_forbidden_etf_target_is_rejected(monkeypatch):
    dates = pd.bdate_range("2018-01-02", "2019-02-15")
    inputs = {
        "close": pd.DataFrame({"QQQ": 100.0}, index=dates),
        "nasdaq": pd.Series(range(1000, 1000 + len(dates)), index=dates),
    }
    monkeypatch.setattr(
        v26,
        "_large_liquid_ranking",
        lambda *_args, **_kwargs: pd.DataFrame(index=["QQQ"]),
    )

    with pytest.raises(RuntimeError, match="forbidden ETFs"):
        v26.generate_target_schedule(_spec(top_n=1), inputs)
