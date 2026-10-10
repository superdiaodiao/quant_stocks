import gzip
import json

import pandas as pd
import pytest

from scripts.research_v14_qfin_annual_growth import (
    SOURCE_PATH,
    SOURCE_SHA256,
    build,
    qfin_direct_growth_facts,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


def _actual_facts() -> pd.DataFrame:
    with gzip.open(SOURCE_PATH, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    facts = qfin_direct_growth_facts(
        envelope["payload"], str(envelope["fetched_at"])[:10]
    )
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    return facts


def test_qfin_source_locked_growth_values_are_exact() -> None:
    facts = _actual_facts()
    values = facts.pivot(index="fiscal_end", columns="metric", values="value")
    fy2019 = values.loc[pd.Timestamp("2019-12-31")]
    assert fy2019["revenue_ttm"] == 9_219_847_000
    assert fy2019["net_income_ttm"] == 2_501_304_000
    assert fy2019["revenue_growth"] == pytest.approx(
        (9_219_847_000 - 4_447_018_000) / 4_447_018_000
    )
    assert fy2019["net_income_growth"] == pytest.approx(
        (2_501_304_000 - 1_193_311_000) / 1_193_311_000
    )
    fy2020 = values.loc[pd.Timestamp("2020-12-31")]
    assert fy2020["revenue_ttm"] == 13_563_954_000
    assert fy2020["net_income_ttm"] == 3_495_709_000
    assert set(facts["metric"]) == {
        "net_income_ttm", "net_income_growth", "revenue_ttm", "revenue_growth"
    }


def test_qfin_direct_packages_respect_point_in_time_age() -> None:
    facts = _actual_facts()
    stale = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-01-29"), maximum_age_days=150
    )
    assert "QFIN" not in stale.index
    january = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-01-29"), maximum_age_days=365
    )
    assert january.loc["QFIN", "fiscal_end"] == pd.Timestamp("2019-12-31")
    june = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-06-30"), maximum_age_days=150
    )
    assert june.loc["QFIN", "fiscal_end"] == pd.Timestamp("2020-12-31")


def test_qfin_build_binds_source_sha(tmp_path) -> None:
    report = build(output_dir=tmp_path / "qfin")
    assert report["source"]["sha256"] == SOURCE_SHA256
    assert report["accepted_direct_growth_package_count"] == 2
    assert report["accepted_fact_count"] == 8
    assert report["formal_financials_modified"] is False
    assert report["release_status"] == "BLOCKED"


def test_qfin_rejects_tampered_source(tmp_path) -> None:
    tampered = tmp_path / "CIK0001741530.json.gz"
    tampered.write_bytes(SOURCE_PATH.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="SHA mismatch"):
        build(source_path=tampered, output_dir=tmp_path / "out")
