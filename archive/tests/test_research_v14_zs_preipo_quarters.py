from pathlib import Path
import pandas as pd
from scripts.research_v14_zs_preipo_quarters import extract_quarters, run

SOURCE=Path("output/data_provenance/zs_preipo/zs_2018_s1.htm")

def test_zs_extracts_ten_direct_quarters():
    rows=extract_quarters(SOURCE)
    assert len(rows)==10
    assert rows[0]=={"fiscal_end":"2015-10-31","revenue":17_132_000.0,"net_income":-6_815_000.0}
    assert rows[-1]=={"fiscal_end":"2018-01-31","revenue":44_976_000.0,"net_income":-6_515_000.0}

def test_zs_run_binds_pit_and_blocked_gates(tmp_path: Path):
    report=run(output_dir=tmp_path)
    assert report["accepted_quarter_count"]==10
    assert report["point_in_time_proven"] is True
    assert report["parameters_frozen"] is False
    assert report["release_status"]=="BLOCKED"
    facts=pd.read_csv(tmp_path/"strict_quarterly_facts.csv")
    assert facts["available_date"].eq("2018-02-16").all()
    assert facts.groupby("fiscal_end")["metric"].nunique().eq(2).all()
