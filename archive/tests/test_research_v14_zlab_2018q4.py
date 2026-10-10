from pathlib import Path

import pandas as pd

from scripts.research_v14_zlab_2018q4 import recover, run


def test_zlab_2018q4_uses_annual_minus_nine_month_comparator() -> None:
    assert recover() == {
        "revenue": 129_452.0,
        "net_income": -63_357_297.0,
    }


def test_zlab_2018q4_is_late_pit_research_only(tmp_path: Path) -> None:
    report = run(output_dir=tmp_path)
    assert report["point_in_time_proven"] is True
    assert report["parameters_frozen"] is False
    assert report["policy_status"] == "RESEARCH_PRETRAINING_ONLY_UNFROZEN"
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    assert report["accepted_quarter_count"] == 1
    assert all(len(source["sha256"]) == 64 for source in report["source_bindings"])
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert set(facts["metric"]) == {"revenue", "net_income"}
    assert set(facts["available_date"]) == {"2020-01-21"}
    assert set(facts["fiscal_end"]) == {"2018-12-31"}
