from scripts.research_v14_candidate_path_audit import (
    representative_configs,
    scenario_id,
)


def test_representative_configs_remove_only_top_n_duplicates() -> None:
    configs = representative_configs("2019-01-01", "2021-12-31")
    keys = {
        (
            config.minimum_median_dollar_volume,
            config.maximum_financial_age_days,
            config.selection_mode,
        )
        for config in configs
    }
    assert len(configs) == len(keys) == 6
    assert {config.top_n for config in configs} == {3}
    assert all(config.start == "2019-01-01" for config in configs)
    assert all(config.end == "2021-12-31" for config in configs)


def test_scenario_id_binds_data_relevant_settings() -> None:
    config = representative_configs("2019-01-01", "2021-12-31")[0]
    identifier = scenario_id(config)
    assert f"age{config.maximum_financial_age_days}" in identifier
    assert f"liq{int(config.minimum_median_dollar_volume)}" in identifier
