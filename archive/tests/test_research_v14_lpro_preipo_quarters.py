from pathlib import Path

import pandas as pd

from scripts.research_v14_lpro_preipo_quarters import extract_s1_values, run


SOURCE = Path("output/data_provenance/lpro_preipo/lpro_2020_s1.htm")


def test_lpro_extracts_s1_q1_and_annual_values() -> None:
    assert extract_s1_values(SOURCE) == {
        "annual": {"revenue": 92_847_000.0, "net_income": 62_544_000.0},
        "q1": {"revenue": 19_484_000.0, "net_income": 12_904_000.0},
    }


def test_lpro_run_closes_2019_and_preserves_pit(tmp_path: Path) -> None:
    report = run(output_dir=tmp_path)
    assert report["accepted_quarter_count"] == 4
    assert report["point_in_time_proven"] is True
    assert report["parameters_frozen"] is False
    assert report["release_status"] == "BLOCKED"
    assert report["q4_residual"] == {
        "revenue": 26_076_000.0, "net_income": 17_440_000.0
    }
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    q4 = facts.loc[facts["fiscal_end"].eq("2019-12-31")]
    assert q4["available_date"].eq("2020-11-13").all()
