import pandas as pd
import pytest

from src.research.can_slim_validation import (
    write_can_slim_validation_outputs,
)
from src.research.validation_artifacts import (
    verify_validation_artifact_manifest,
)


def _validation():
    return (
        pd.DataFrame({"strategy": [0.0]}),
        pd.DataFrame({"strategy": [0.0]}),
        pd.DataFrame({"cost_bps": [10.0]}),
        pd.DataFrame({"ticker": ["ABC"]}),
        pd.DataFrame({"ticker": ["ABC"]}),
        {"release_status": "BLOCKED"},
        {"missing_financial_priorities": []},
    )


def test_validation_artifact_manifest_verifies_complete_generation(
    tmp_path,
):
    write_can_slim_validation_outputs(_validation(), tmp_path)

    result = verify_validation_artifact_manifest(tmp_path)

    assert result["verified"]
    assert result["artifact_count"] == 8


def test_validation_artifact_manifest_rejects_one_tampered_file(
    tmp_path,
):
    write_can_slim_validation_outputs(_validation(), tmp_path)
    ledger = tmp_path / "can_slim_fixed_top3_trade_ledger.csv"
    ledger.write_text(
        ledger.read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="integrity mismatch"):
        verify_validation_artifact_manifest(tmp_path)
