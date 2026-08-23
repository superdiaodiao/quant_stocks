from pathlib import Path

import pandas as pd
import pytest

from scripts.research_v14_allk_exact_ttm_loss import (
    CACHE,
    EXPECTED_TTM,
    _load_locked_cache,
    direct_ttm_facts,
    exact_ttm_evidence,
    run,
    verify_operands,
)
from src.financial.quarterly_fundamentals import (
    quarterly_growth_snapshot,
    quarterly_profit_ttm_snapshot,
)


def test_locked_companyfacts_operands_produce_exact_negative_ttm() -> None:
    envelope = _load_locked_cache(CACHE)
    operands = verify_operands(envelope["payload"])
    evidence = exact_ttm_evidence(envelope["payload"])

    assert len(operands) == 3
    assert evidence["net_income_ttm"] == EXPECTED_TTM == -64_701_000
    assert evidence["available_date"] == "2019-08-05"
    assert evidence["formula"] == "FY2018 - H1_2018 + H1_2019"


def test_exact_loss_resolves_age150_without_inventing_growth() -> None:
    envelope = _load_locked_cache(CACHE)
    facts = direct_ttm_facts(envelope["payload"], envelope["fetched_at"])
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2019-08-04"), 150
    )
    at_signal = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2019-09-30"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2019-09-30"), 150
    )

    assert before.empty
    assert at_signal.loc["ALLK", "net_income_ttm"] == EXPECTED_TTM
    assert growth.empty
    assert set(facts["metric"]) == {"net_income_ttm"}


def test_build_is_blocked_research_only_and_resolves_both_scenarios(tmp_path) -> None:
    report = run(output_dir=tmp_path)

    assert report["point_in_time_proven"] is True
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    assert report["formal_financials_modified"] is False
    assert report["resolved_audit_observation_count"] == 2
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert len(facts) == 1


def test_locked_cache_rejects_tampering(tmp_path: Path) -> None:
    tampered = tmp_path / "CIK0001564824.json.gz"
    tampered.write_bytes(CACHE.read_bytes() + b"tampered")

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        run(cache_path=tampered, output_dir=tmp_path / "out")
