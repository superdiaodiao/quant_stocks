import pandas as pd

from scripts import research_v35_weekly_volatility_budget_development as v35


def test_candidate_grid_is_small_and_training_only():
    specs = v35.candidate_specs()

    assert len(specs) == 4
    assert {spec["annualized_volatility_target"] for spec in specs} == {
        0.10,
        0.15,
        0.20,
        0.25,
    }
    assert v35.DEVELOPMENT_YEARS == tuple(range(2020, 2026))
    assert v35.FINAL_COMPARISON_YEARS == ()


def test_weekly_schedule_uses_only_prices_before_execution():
    dates = pd.bdate_range("2019-09-02", periods=90)
    close = pd.DataFrame({
        "A": [100.0 * (1.001 ** index) for index in range(len(dates))],
        "B": [100.0 * (1.0005 ** index) for index in range(len(dates))],
    }, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[45], dates[45]],
        "ticker": ["A", "B"],
        "target_weight": [0.5, 0.5],
    })

    schedule = v35.build_weekly_volatility_target_schedule(
        close,
        targets,
        dates[45],
        dates[-1],
        annualized_volatility_target=0.15,
        transaction_cost_bps=50.0,
        lookback_days=40,
        minimum_observations=30,
    )

    assert not schedule.empty
    assert (
        pd.to_datetime(schedule["volatility_cutoff_date"])
        < pd.to_datetime(schedule["effective_date"])
    ).all()
    assert schedule.groupby("effective_date")["target_weight"].sum().le(1.0).all()
    assert set(schedule["base_transaction_cost_bps"]) == {50.0}


def test_future_price_change_cannot_change_first_exposure():
    dates = pd.bdate_range("2019-09-02", periods=90)
    base_close = pd.DataFrame({
        "A": [100.0 * (1.001 ** index) for index in range(len(dates))],
    }, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[45]],
        "ticker": ["A"],
        "target_weight": [1.0],
    })
    changed = base_close.copy()
    changed.loc[dates[46]:, "A"] *= 4.0

    first = v35.build_weekly_volatility_target_schedule(
        base_close,
        targets,
        dates[45],
        dates[-1],
        annualized_volatility_target=0.15,
        transaction_cost_bps=0.0,
        lookback_days=40,
        minimum_observations=30,
    )
    second = v35.build_weekly_volatility_target_schedule(
        changed,
        targets,
        dates[45],
        dates[-1],
        annualized_volatility_target=0.15,
        transaction_cost_bps=0.0,
        lookback_days=40,
        minimum_observations=30,
    )
    first_date = pd.Timestamp(first["effective_date"].min())
    first_weight = first.loc[first["effective_date"].eq(first_date), "target_weight"].sum()
    second_weight = second.loc[second["effective_date"].eq(first_date), "target_weight"].sum()

    assert first_weight == second_weight


def test_protocol_excludes_training_years_from_final_comparison(tmp_path):
    protocol = v35.freeze_protocol(tmp_path / "protocol.json")
    boundary = protocol["evaluation_boundary"]

    assert boundary["training_years_excluded_from_final_comparison"] is True
    assert boundary["final_comparison_years"] == []
    assert boundary["2026_used_for_parameter_selection"] is False
    assert boundary["architecture_isolated_from_2026"] is False
    assert boundary["parameter_isolated_from_2026"] is True
