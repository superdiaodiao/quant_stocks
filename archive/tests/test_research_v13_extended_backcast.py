import pandas as pd

from scripts.research_v13_extended_backcast import _snapshot
from src.research.can_slim import CanSlimConfig


def test_fallback_snapshot_is_explicitly_labelled_and_rank_weighted():
    configs = [CanSlimConfig(top_n=5), CanSlimConfig(top_n=10)]
    result = _snapshot(
        pd.Timestamp("2019-01-01"), pd.Timestamp("2019-12-31"),
        pd.Timestamp("2018-12-31"), [0], configs,
        "pre_training_fixed_core_fallback",
    )
    assert result["selection_reason"] == "pre_training_fixed_core_fallback"
    assert result["configs"][0].ensemble_weight == 1.0
