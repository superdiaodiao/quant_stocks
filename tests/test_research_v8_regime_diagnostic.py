import pandas as pd
import pytest

from scripts.research_v8_regime_diagnostic import future_compounded, regime_summary


def test_future_compounded_excludes_window_start_session():
    returns = pd.Series([0.50, 0.10, 0.20, -0.10])
    result = future_compounded(returns, 2)
    assert result.iloc[0] == pytest.approx(1.10 * 1.20 - 1)
    assert result.iloc[1] == pytest.approx(1.20 * 0.90 - 1)
    assert pd.isna(result.iloc[2])


def test_regime_summary_separates_boolean_states():
    outcome = pd.Series([0.1, -0.1, 0.2, -0.2])
    regime = pd.Series([True, True, False, False])
    result = regime_summary(outcome, regime)
    assert result["true"]["positive_fraction"] == pytest.approx(0.5)
    assert result["false"]["positive_fraction"] == pytest.approx(0.5)
