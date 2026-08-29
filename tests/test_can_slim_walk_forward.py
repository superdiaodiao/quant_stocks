import pandas as pd

from src.research.can_slim_walk_forward import (
    _training_score,
    annual_parameter_snapshot_periods,
    candidate_configs,
    configs_from_snapshots,
    core_fallback_ids,
    select_stable_ensemble,
)


def test_candidate_grid_is_predeclared_and_unique():
    configs = candidate_configs()
    keys = {(c.top_n, c.minimum_median_dollar_volume) for c in configs}
    assert len(configs) == len(keys) == 6
    assert {c.top_n for c in configs} == {3, 5, 10}
    assert all(c.maximum_position_weight == 1 / c.top_n for c in configs)


def test_adaptive_channel_grid_only_adds_predeclared_recovery_variants():
    configs = candidate_configs(adaptive_channel=True)
    keys = {
        (c.top_n, c.minimum_median_dollar_volume, c.price_channel)
        for c in configs
    }
    assert len(configs) == len(keys) == 12
    assert {c.price_channel for c in configs} == {"none", "keltner"}
    assert {
        (c.price_channel, c.selection_mode) for c in configs
    } == {("none", "growth"), ("keltner", "recovery")}


def test_candidate_grid_carries_the_requested_signal_frequency():
    configs = candidate_configs("weekly")
    assert {config.signal_frequency for config in configs} == {"weekly"}
    assert {c.minimum_eps_growth for c in configs} == {0.25}


def test_freshness_grid_is_predeclared_and_expands_without_duplicates():
    configs = candidate_configs(maximum_financial_age_days=(150, 365, 550))
    keys = {
        (c.top_n, c.minimum_median_dollar_volume, c.maximum_financial_age_days)
        for c in configs
    }
    assert len(configs) == len(keys) == 18
    assert {c.maximum_financial_age_days for c in configs} == {150, 365, 550}
    fallback = core_fallback_ids(configs)
    assert len(fallback) == 1
    assert configs[fallback[0]].maximum_financial_age_days == 550


def test_training_score_prefers_worst_year_then_median():
    annual = pd.DataFrame({"excess_vs_nasdaq": [0.2, -0.1, 0.3]}, index=[2021, 2022, 2023])
    assert _training_score(annual, [2021, 2022, 2023]) == (-0.1, 0.2)


def test_grouped_selection_prevents_duplicate_model_family_votes():
    dates = pd.date_range("2019-01-31", "2021-12-31", freq="ME")
    base = pd.DataFrame({
        "strategy": [0.02] * len(dates), "benchmark": [0.01] * len(dates)
    }, index=dates)
    weaker = pd.DataFrame({
        "strategy": [0.005] * len(dates), "benchmark": [0.01] * len(dates)
    }, index=dates)
    results = {0: base, 1: weaker, 2: base.copy(), 3: weaker.copy()}

    selected, ranking = select_stable_ensemble(
        results, 2022, ensemble_size=2,
        candidate_groups={0: "a", 1: "a", 2: "b", 3: "b"},
    )

    assert set(selected) == {0, 2}
    assert ranking.loc[ranking["variant_selected"], "config_id"].tolist() == [0, 2]


def test_dominated_extra_variant_cannot_change_other_group_winners():
    dates = pd.date_range("2019-01-31", "2021-12-31", freq="ME")
    strong = pd.DataFrame({
        "strategy": [0.02] * len(dates), "benchmark": [0.01] * len(dates)
    }, index=dates)
    medium = pd.DataFrame({
        "strategy": [0.015] * len(dates), "benchmark": [0.01] * len(dates)
    }, index=dates)
    weak = pd.DataFrame({
        "strategy": [0.005] * len(dates), "benchmark": [0.01] * len(dates)
    }, index=dates)
    base_results = {0: strong, 1: weak, 2: medium, 3: weak}
    base_groups = {0: "a", 1: "a", 2: "b", 3: "b"}
    expanded_results = {**base_results, 4: weak.copy()}
    expanded_groups = {**base_groups, 4: "a"}

    _, base_ranking = select_stable_ensemble(
        base_results, 2022, candidate_groups=base_groups
    )
    _, expanded_ranking = select_stable_ensemble(
        expanded_results, 2022, candidate_groups=expanded_groups
    )

    assert set(base_ranking.loc[base_ranking["variant_selected"], "config_id"]) == {
        0, 2
    }
    assert set(
        expanded_ranking.loc[expanded_ranking["variant_selected"], "config_id"]
    ) == {0, 2}


def test_exact_selection_ties_use_config_id_not_mapping_order():
    dates = pd.date_range("2019-01-31", "2021-12-31", freq="ME")
    tied = pd.DataFrame({
        "strategy": [0.02] * len(dates),
        "benchmark": [0.01] * len(dates),
    }, index=dates)
    results = {9: tied, 7: tied.copy(), 3: tied.copy()}

    selected, ranking = select_stable_ensemble(
        results, 2022, ensemble_size=2
    )
    grouped_selected, grouped_ranking = select_stable_ensemble(
        results,
        2022,
        ensemble_size=2,
        candidate_groups={9: "a", 7: "b", 3: "a"},
    )

    assert selected == [3, 7]
    assert ranking["config_id"].tolist() == [3, 7, 9]
    assert grouped_selected == [3, 7]
    assert grouped_ranking.loc[
        grouped_ranking["variant_selected"], "config_id"
    ].tolist() == [3, 7]


def test_annual_parameter_periods_only_train_through_prior_day():
    periods = annual_parameter_snapshot_periods("2022-01-01", "2023-07-17")

    assert periods == [
        (
            pd.Timestamp("2022-01-01"),
            pd.Timestamp("2022-12-31"),
            pd.Timestamp("2021-12-31"),
        ),
        (
            pd.Timestamp("2023-01-01"),
            pd.Timestamp("2023-07-17"),
            pd.Timestamp("2022-12-31"),
        ),
    ]


def test_snapshot_resolver_changes_only_after_effective_date():
    old = [object()]
    new = [object()]
    snapshots = [
        {"effective_start": pd.Timestamp("2025-01-01"), "configs": old},
        {"effective_start": pd.Timestamp("2025-04-01"), "configs": new},
    ]

    assert configs_from_snapshots(snapshots, "2025-03-31") is old
    assert configs_from_snapshots(snapshots, "2025-04-01") is new


def test_time_cutoff_selection_excludes_future_results():
    dates = pd.date_range("2019-01-31", "2022-03-31", freq="ME")
    result = pd.DataFrame({
        "strategy": [0.02] * len(dates),
        "benchmark": [0.01] * len(dates),
    }, index=dates)
    future = pd.DataFrame({
        "strategy": [-0.90],
        "benchmark": [0.00],
    }, index=[pd.Timestamp("2022-04-30")])

    _, before = select_stable_ensemble(
        {0: pd.concat([result, future])}, train_end="2022-03-31"
    )
    _, without_future = select_stable_ensemble(
        {0: result}, train_end="2022-03-31"
    )

    assert before.loc[0, "rolling_quality"] == without_future.loc[
        0, "rolling_quality"
    ]


def test_selection_accepts_an_explicit_earlier_expanding_start():
    dates = pd.date_range("2018-01-31", "2020-12-31", freq="ME")
    result = pd.DataFrame({
        "strategy": [0.02] * len(dates),
        "benchmark": [0.01] * len(dates),
    }, index=dates)

    selected, ranking = select_stable_ensemble(
        {0: result}, 2021, expanding_start="2018-01-01"
    )

    assert selected == [0]
    assert ranking.loc[0, "rolling_months"] == 36


def test_selection_uses_fixed_core_when_every_recent_quality_is_negative():
    dates = pd.date_range("2019-01-31", "2021-12-31", freq="ME")
    candidates = {
        0: pd.DataFrame(
            {"strategy": 0.01, "benchmark": 0.02}, index=dates
        ),
        1: pd.DataFrame(
            {"strategy": 0.00, "benchmark": 0.02}, index=dates
        ),
    }

    selected, ranking = select_stable_ensemble(
        candidates,
        train_end="2021-12-31",
        no_evidence_fallback_ids=[1],
    )

    assert selected == [1]
    assert ranking["selection_reason"].unique().tolist() == [
        "no_positive_rolling_evidence"
    ]
