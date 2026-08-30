import pandas as pd

from scripts import research_v31_recovery_speed_stock_momentum as v31


def test_grid_changes_only_predeclared_recovery_dimensions():
    specs = v31.candidate_specs()

    assert len(specs) == 30
    assert {spec["lookback_sessions"] for spec in specs} == {21, 42, 63}
    assert {spec["market_ma_days"] for spec in specs} == {0, 50, 100, 150, 200}
    assert {spec["top_n"] for spec in specs} == {5, 10}
    assert {spec["liquid_pool_size"] for spec in specs} == {25}
    assert {spec["signal_frequency"] for spec in specs} == {"monthly"}
    assert {spec["quality_mode"] for spec in specs} == {"profitable"}
    assert "QQQ" in v31.FORBIDDEN_ETFS


def test_always_on_and_moving_average_market_rules(monkeypatch):
    index = pd.Series([1.0], index=[pd.Timestamp("2024-01-31")])

    assert v31._market_regime_on(pd.Timestamp("2024-01-31"), index, 0)
    monkeypatch.setattr(v31, "market_regime_is_on", lambda *_args: False)
    assert not v31._market_regime_on(pd.Timestamp("2024-01-31"), index, 50)


def test_gap_policy_covers_prestart_and_four_2019_signals():
    assert v31.GAP_SIGNAL_DATES == tuple(pd.to_datetime([
        "2018-12-31",
        "2019-04-30",
        "2019-05-31",
        "2019-08-30",
        "2019-09-30",
    ]))


def _summary(excess_by_year):
    years = tuple(sorted(excess_by_year))
    annual = [
        {
            "year": year,
            "excess_vs_nasdaq": excess_by_year[year],
            "excess_vs_qqq": excess_by_year[year],
        }
        for year in years
    ]
    periods = {
        f"{min(years)}-{max(years)}": {
            "compounded_excess_vs_nasdaq": sum(excess_by_year.values()),
            "drawdown_lag_vs_qqq": 0.0,
            "turnover": 1.0,
        }
    }
    return {
        "costs": {
            str(cost): {"annual": annual, "periods": periods}
            for cost in v31.COSTS
        }
    }


def test_selection_requires_every_year_to_beat_nasdaq():
    years = v31.DEVELOPMENT_YEARS
    all_win = _summary({year: 0.01 for year in years})
    one_loss = _summary({year: (-0.01 if year == 2019 else 0.01) for year in years})

    selected, ranking = v31.select_candidate({"all_win": all_win, "one_loss": one_loss})

    assert selected == "all_win"
    assert ranking[0]["required_wins"] == len(years)
    assert ranking[0]["wins_vs_nasdaq_50bps"] == len(years)
    assert next(row for row in ranking if row["candidate"] == "one_loss")[
        "eligible"
    ] is False
