from pathlib import Path

import pandas as pd

from scripts.research_v14_apa_cik_transition import (
    extract_total_revenue,
    run,
)


ROOT = Path("output/data_provenance/apa_cik_transition")


def test_apa_extracts_three_contemporaneous_total_revenues() -> None:
    expected = {
        "apa-20210331.htm": 1_871_000_000.0,
        "apa-20210630.htm": 1_756_000_000.0,
        "apa-20210930.htm": 2_059_000_000.0,
    }
    assert {
        name: extract_total_revenue(ROOT / name) for name in expected
    } == expected


def test_apa_run_enforces_cik_boundary_pit_and_blocked_gates(
    tmp_path: Path,
) -> None:
    report = run(output_dir=tmp_path)
    assert report["predecessor_cik"] == 6769
    assert report["successor_cik"] == 1841666
    assert report["successor_revenue_rows"] == 3
    assert report["successor_paired_quarters"] == 3
    assert report["point_in_time_proven"] is True
    assert report["parameters_frozen"] is False
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    successor = facts.loc[pd.to_datetime(facts["fiscal_end"]).ge("2021-01-01")]
    assert successor.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    assert successor.loc[successor["metric"].eq("revenue"), "value"].gt(0).all()
    legacy = facts.loc[pd.to_datetime(facts["fiscal_end"]).le("2020-12-31")]
    assert legacy["concept"].str.startswith(
        "research_predecessor_cik_6769:"
    ).all()
