from __future__ import annotations

from scripts.research_v3_fresh_top3 import MODEL_VERSION, challenger_config


def test_challenger_is_predeclared_fresh_liquid_top3() -> None:
    config = challenger_config()
    assert MODEL_VERSION == "can-slim-v3-fresh-top3-shadow"
    assert config.top_n == 3
    assert config.minimum_median_dollar_volume == 10_000_000.0
    assert config.maximum_financial_age_days == 150
    assert config.selection_mode == "growth"
    assert config.price_channel == "none"
