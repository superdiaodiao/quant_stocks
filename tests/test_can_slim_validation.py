from src.research.can_slim_validation import fixed_top3_config


def test_frozen_top3_policy_has_one_unambiguous_position_rule():
    config = fixed_top3_config()

    assert config.top_n == 3
    assert config.maximum_position_weight == 1 / 3
    assert config.minimum_median_dollar_volume == 10_000_000
    assert config.signal_frequency == "monthly"
    assert config.use_quarterly_fundamentals is True
    assert config.price_channel == "none"
