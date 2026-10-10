import gzip
import json

import pandas as pd
import pytest

from scripts.research_v14_ccep_h1_exact_growth import (
    SOURCE_PATH,
    SOURCE_SHA256,
    build,
    ccep_direct_growth_facts,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


def _facts() -> tuple[pd.DataFrame, dict]:
    with gzip.open(SOURCE_PATH, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    facts, evidence = ccep_direct_growth_facts(
        envelope["payload"], str(envelope["fetched_at"])[:10]
    )
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    return facts, evidence


def test_ccep_exact_h1_ttm_arithmetic() -> None:
    facts, evidence = _facts()
    derived = evidence["derived"]
    assert derived["revenue"]["prior_ttm"] == 11_052_000_000
    assert derived["revenue"]["current_ttm"] == 11_687_000_000
    assert derived["net_income"]["prior_ttm"] == 708_000_000
    assert derived["net_income"]["current_ttm"] == 616_000_000
    assert derived["revenue"]["growth"] == pytest.approx(635 / 11_052)
    assert derived["net_income"]["growth"] == pytest.approx(-92 / 708)
    assert len(facts) == 4


def test_ccep_package_is_not_visible_before_h1_filing() -> None:
    facts, _ = _facts()
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-08-31"), maximum_age_days=550
    )
    assert "CCEP" not in before.index
    after = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-09-30"), maximum_age_days=150
    )
    assert after.loc["CCEP", "fiscal_end"] == pd.Timestamp("2021-07-02")
    assert after.loc["CCEP", "net_income_growth"] < 0


def test_ccep_build_binds_companyfacts(tmp_path) -> None:
    report = build(output_dir=tmp_path / "ccep")
    assert report["source"]["sha256"] == SOURCE_SHA256
    assert report["accepted_direct_growth_package_count"] == 1
    assert report["formal_financials_modified"] is False
    assert report["release_status"] == "BLOCKED"


def test_ccep_rejects_tampered_source(tmp_path) -> None:
    tampered = tmp_path / "CIK0001650107.json.gz"
    tampered.write_bytes(SOURCE_PATH.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="SHA mismatch"):
        build(source_path=tampered, output_dir=tmp_path / "out")
