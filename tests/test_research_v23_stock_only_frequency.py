import pandas as pd
import pytest

from scripts import research_v23_stock_only_frequency as v23


def _summary(excesses, *, drawdown_lag=0.05, turnover=5.0):
    annual = [
        {
            "year": year,
            "strategy": 0.10 + excess,
            "benchmark": 0.09,
            "qqq": 0.10,
            "excess_vs_qqq": excess,
            "excess_vs_nasdaq": 0.01 + excess,
        }
        for year, excess in zip(v23.DEVELOPMENT_YEARS, excesses, strict=True)
    ]
    costs = {}
    for cost in v23.COSTS:
        periods = {}
        for end in range(1, len(v23.DEVELOPMENT_YEARS) + 1):
            years = v23.DEVELOPMENT_YEARS[:end]
            rows = annual[:end]
            strategy = 1.0
            qqq = 1.0
            for row in rows:
                strategy *= 1.0 + row["strategy"]
                qqq *= 1.0 + row["qqq"]
            periods[v23._period_key(years)] = {
                "compounded_excess_vs_qqq": strategy - qqq,
                "drawdown_lag_vs_qqq": drawdown_lag,
                "turnover": turnover * end / len(v23.DEVELOPMENT_YEARS),
            }
        costs[str(cost)] = {
            "annual": annual,
            "periods": periods,
        }
    return {"costs": costs}


def test_candidate_grid_compares_monthly_and_weekly_without_etfs():
    specs = v23.candidate_specs()

    assert len(specs) == 12
    assert {spec["signal_frequency"] for spec in specs} == {"monthly", "weekly"}
    assert {spec["top_n"] for spec in specs} == {3, 5, 10}
    for spec in specs:
        config = v23._candidate_config(spec)
        assert config.maximum_position_weight == 1.0 / spec["top_n"]
        assert config.signal_frequency == spec["signal_frequency"]
    assert "QQQ" in v23.FORBIDDEN_ETFS
    assert v23.OBSERVATION_START == "2026-01-01"


def test_candidate_selection_requires_qqq_gates_and_uses_fixed_order():
    rejected = _summary([0.02, 0.02, -0.02, -0.02, -0.02, -0.02, -0.02])
    eligible_weaker = _summary([0.01, 0.01, 0.01, 0.01, 0.01, -0.01, -0.01])
    eligible_stronger = _summary([0.02, 0.02, 0.02, 0.02, 0.02, -0.01, -0.01])

    selected, ranking = v23.select_candidate({
        "rejected": rejected,
        "monthly": eligible_weaker,
        "weekly": eligible_stronger,
    })

    assert selected == "weekly"
    assert ranking[0]["candidate"] == "weekly"
    assert ranking[0]["eligible"] is True
    assert next(row for row in ranking if row["candidate"] == "rejected")[
        "eligible"
    ] is False


def test_prefix_selection_does_not_use_later_drawdown():
    summary = _summary([0.02] * 7)
    summary["costs"]["50"]["periods"]["2019-2021"][
        "drawdown_lag_vs_qqq"
    ] = 0.01
    summary["costs"]["50"]["periods"]["2019-2025"][
        "drawdown_lag_vs_qqq"
    ] = 0.50

    early = v23._selection_row("candidate", summary, (2019, 2020, 2021))
    full = v23._selection_row("candidate", summary, v23.DEVELOPMENT_YEARS)

    assert early["eligible"] is True
    assert early["drawdown_lag_vs_qqq_50bps"] == 0.01
    assert full["eligible"] is False
    assert full["drawdown_lag_vs_qqq_50bps"] == 0.50


def test_qqq_first_development_session_keeps_prior_session_return():
    dates = pd.to_datetime(["2019-01-02", "2019-01-03"])
    result = pd.DataFrame({
        "strategy": [0.0, 0.0],
        "benchmark": [0.0, 0.0],
        "turnover": [0.0, 0.0],
    }, index=dates)
    nasdaq = pd.Series([100.0, 101.0], index=dates)
    qqq_dates = pd.to_datetime(["2018-12-31", *dates.strftime("%Y-%m-%d")])
    qqq = pd.DataFrame({
        "close": [100.0, 110.0, 121.0],
        "cash_dividend": [0.0, 0.0, 0.0],
    }, index=qqq_dates)

    actual = v23._canonicalize_result(result, nasdaq, qqq)

    assert actual.loc[dates[0], "qqq"] == pytest.approx(0.10)
    assert actual.loc[dates[1], "qqq"] == pytest.approx(0.10)
