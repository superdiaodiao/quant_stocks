import numpy as np
import pandas as pd
import pytest

from scripts.research_v8_short_horizon_robustness import (
    moving_block_bootstrap,
    relative_returns,
    rolling_summary,
)


def test_relative_returns_are_self_financing():
    strategy = pd.Series([0.10, -0.05])
    benchmark = pd.Series([0.05, -0.02])
    result = relative_returns(strategy, benchmark)
    assert result.iloc[0] == pytest.approx(1.10 / 1.05 - 1)
    assert result.iloc[1] == pytest.approx(0.95 / 0.98 - 1)


def test_rolling_summary_compounds_exact_windows():
    relative = pd.Series([0.10, 0.0, -0.10])
    result = rolling_summary(relative, 2)
    assert result["overlapping_windows"] == 2
    assert result["maximum_relative_return"] == pytest.approx(0.10)
    assert result["minimum_relative_return"] == pytest.approx(-0.10)
    assert result["positive_fraction"] == pytest.approx(0.5)


def test_moving_block_bootstrap_is_seeded_and_positive_for_positive_path():
    relative = pd.Series(np.full(80, 0.001))
    first = moving_block_bootstrap(relative, 65, samples=100, seed=7)
    second = moving_block_bootstrap(relative, 65, samples=100, seed=7)
    assert first == second
    assert first["positive_probability"] == 1.0
    assert first["quantile_05"] > 0
