from __future__ import annotations

import pytest
import pandas as pd

from src.research.can_slim_data_audit import _config_ids_as_of, _holding_calendar


def test_config_ids_as_of_accepts_legacy_list() -> None:
    assert _config_ids_as_of("[2, 1]", "2025-06-30") == [2, 1]


def test_config_ids_as_of_uses_latest_effective_snapshot() -> None:
    value = '{"2025-01-01": [6], "2025-07-01": [2, 1]}'
    assert _config_ids_as_of(value, "2025-06-30") == [6]
    assert _config_ids_as_of(value, "2025-07-31") == [2, 1]


def test_config_ids_as_of_rejects_missing_effective_snapshot() -> None:
    with pytest.raises(ValueError, match="no configuration effective"):
        _config_ids_as_of('{"2025-07-01": [2]}', "2025-06-30")


def test_holding_calendar_uses_benchmark_sessions_and_excludes_rebalance() -> None:
    benchmark = pd.to_datetime(
        ["2026-06-18", "2026-06-22", "2026-06-23"]
    )
    result = _holding_calendar(benchmark, "2026-06-18", "2026-06-23")
    assert result.tolist() == list(pd.to_datetime(["2026-06-18", "2026-06-22"]))
