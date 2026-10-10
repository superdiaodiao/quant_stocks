import pandas as pd

from scripts import research_v38_weekly_holding_breadth_development as v38


def test_grid_is_small_and_sma20_is_fixed():
    specs = v38.candidate_specs()

    assert len(specs) == 3
    assert {spec["minimum_healthy_holding_fraction"] for spec in specs} == {
        0.40,
        0.60,
        0.80,
    }
    assert {spec["moving_average_trading_days"] for spec in specs} == {20}


def test_breadth_schedule_uses_only_prior_prices():
    dates = pd.bdate_range("2019-11-01", periods=80)
    close = pd.DataFrame({
        "A": [100.0 + index for index in range(80)],
        "B": [100.0 + index * 0.5 for index in range(80)],
    }, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[30], dates[30]],
        "ticker": ["A", "B"],
        "target_weight": [0.5, 0.5],
    })

    schedule = v38.build_weekly_breadth_target_schedule(
        close,
        targets,
        dates[30],
        dates[-1],
        minimum_healthy_fraction=0.60,
        transaction_cost_bps=50.0,
    )

    assert not schedule.empty
    assert (
        pd.to_datetime(schedule["breadth_cutoff_date"])
        < pd.to_datetime(schedule["effective_date"])
    ).all()
    assert schedule.groupby("effective_date")["target_weight"].sum().le(1.0).all()


def test_future_prices_cannot_change_first_breadth_decision():
    dates = pd.bdate_range("2019-11-01", periods=80)
    close = pd.DataFrame({"A": [100.0 + index for index in range(80)]}, index=dates)
    changed = close.copy()
    changed.loc[dates[31]:, "A"] = 1.0
    targets = pd.DataFrame({
        "effective_date": [dates[30]],
        "ticker": ["A"],
        "target_weight": [1.0],
    })
    kwargs = dict(
        start=dates[30],
        end=dates[-1],
        minimum_healthy_fraction=0.60,
        transaction_cost_bps=0.0,
    )
    first = v38.build_weekly_breadth_target_schedule(close, targets, **kwargs)
    second = v38.build_weekly_breadth_target_schedule(changed, targets, **kwargs)
    first_date = pd.Timestamp(first["effective_date"].min())

    assert bool(first.loc[first["effective_date"].eq(first_date), "breadth_risk_on"].iloc[0]) == bool(
        second.loc[second["effective_date"].eq(first_date), "breadth_risk_on"].iloc[0]
    )


def test_protocol_excludes_training_years_from_final_comparison(tmp_path):
    protocol = v38.freeze_protocol(tmp_path / "protocol.json")
    boundary = protocol["evaluation_boundary"]

    assert boundary["training_years_excluded_from_final_comparison"] is True
    assert boundary["final_comparison_years"] == []
    assert boundary["2026_used_for_parameter_selection"] is False
    assert boundary["architecture_isolated_from_2026"] is False
    assert boundary["parameter_isolated_from_2026"] is True
