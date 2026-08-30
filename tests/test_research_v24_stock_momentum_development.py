import pandas as pd
import pytest

from scripts import research_v24_stock_momentum_development as v24


def _inputs(*, rising_market=True):
    dates = pd.bdate_range("2018-01-02", "2019-02-15")
    market = range(1000, 1000 + len(dates))
    if not rising_market:
        market = range(1000 + len(dates), 1000, -1)
    return {
        "close": pd.DataFrame({"A": 100.0, "B": 100.0}, index=dates),
        "nasdaq": pd.Series(market, index=dates, dtype=float),
    }


def _spec(*, frequency="weekly", top_n=1, buffer=2, quality="technical"):
    return {
        "lookback_sessions": 63,
        "skip_recent_sessions": 0,
        "signal_frequency": frequency,
        "top_n": top_n,
        "rank_buffer_multiple": buffer,
        "quality_mode": quality,
    }


def _ranking(tickers):
    return pd.DataFrame({
        "momentum_excess_vs_nasdaq": range(len(tickers), 0, -1),
    }, index=tickers)


def test_grid_covers_monthly_and_buffered_weekly_stock_candidates():
    specs = v24.candidate_specs()

    assert len(specs) == 24
    assert {spec["signal_frequency"] for spec in specs} == {"monthly", "weekly"}
    assert {
        spec["rank_buffer_multiple"]
        for spec in specs if spec["signal_frequency"] == "weekly"
    } == {2}
    assert {spec["quality_mode"] for spec in specs} == {
        "technical",
        "profitable",
    }
    assert v24.OBSERVATION_START == "2026-01-01"


def test_real_technical_ranking_uses_only_liquid_common_equity_leaders():
    dates = pd.bdate_range("2018-01-02", periods=300)
    close = pd.DataFrame({
        "A": pd.Series(range(100, 400), index=dates, dtype=float),
        "B": pd.Series(range(100, 400), index=dates, dtype=float) * 0.6,
        "ILLIQUID": pd.Series(range(100, 400), index=dates, dtype=float),
    })
    inputs = {
        "close": close,
        "dollar_volume": pd.DataFrame({
            "A": 20_000_000.0,
            "B": 20_000_000.0,
            "ILLIQUID": 1_000_000.0,
        }, index=dates),
        "nasdaq": pd.Series(range(1000, 1300), index=dates, dtype=float),
        "universe": lambda _date: {"A", "B", "ILLIQUID"},
        "technical_cache": {},
    }

    ranking = v24._technical_ranking(dates[-1], _spec(), inputs)

    assert list(ranking.index) == ["A", "B"]
    assert ranking["momentum_excess_vs_nasdaq"].is_monotonic_decreasing


def test_weekly_rank_buffer_keeps_existing_leader(monkeypatch):
    calls = 0

    def fake_ranking(_signal_date, _specification, _loaded):
        nonlocal calls
        calls += 1
        return _ranking(["A", "B"] if calls == 1 else ["B", "A"])

    monkeypatch.setattr(v24, "_technical_ranking", fake_ranking)

    targets = v24.generate_target_schedule(_spec(), _inputs())

    active = targets.loc[targets["ticker"].ne("__CASH__")]
    assert len(active) > 1
    assert set(active["ticker"]) == {"A"}
    assert set(active["target_weight"]) == {1.0}


def test_market_risk_off_emits_cash(monkeypatch):
    monkeypatch.setattr(
        v24,
        "_technical_ranking",
        lambda *_args, **_kwargs: _ranking(["A", "B"]),
    )

    targets = v24.generate_target_schedule(_spec(), _inputs(rising_market=False))

    assert set(targets["ticker"]) == {"__CASH__"}
    assert set(targets["target_weight"]) == {0.0}


def test_profitable_filter_is_applied_without_changing_rank_order(monkeypatch):
    monkeypatch.setattr(
        v24,
        "_technical_ranking",
        lambda *_args, **_kwargs: _ranking(["A", "B"]),
    )
    monkeypatch.setattr(
        v24,
        "_profitable_symbols",
        lambda *_args, **_kwargs: {"B"},
    )

    targets = v24.generate_target_schedule(
        _spec(frequency="monthly", buffer=1, quality="profitable"),
        _inputs(),
    )

    assert set(targets.loc[targets["ticker"].ne("__CASH__"), "ticker"]) == {"B"}


def test_forbidden_etf_target_is_rejected(monkeypatch):
    monkeypatch.setattr(
        v24,
        "_technical_ranking",
        lambda *_args, **_kwargs: _ranking(["QQQ"]),
    )

    with pytest.raises(RuntimeError, match="forbidden ETFs"):
        v24.generate_target_schedule(_spec(), _inputs())
