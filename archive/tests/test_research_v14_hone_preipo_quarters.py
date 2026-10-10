from pathlib import Path

import pandas as pd

from scripts.research_v14_hone_preipo_quarters import (
    _annual_identity_checks,
    extract_quarters,
    run,
)


SOURCE = Path("output/data_provenance/hone_preipo/hone_2019_s1.htm")


def test_hone_extracts_eight_direct_bank_quarters() -> None:
    rows = extract_quarters(SOURCE)
    assert len(rows) == 8
    assert rows[0] == {
        "ticker": "HONE",
        "fiscal_end": "2017-03-31",
        "revenue": 28_886_000.0,
        "net_income": 2_735_000.0,
        "fiscal_quarter": 1,
    }
    assert rows[-1] == {
        "ticker": "HONE",
        "fiscal_end": "2018-12-31",
        "revenue": 38_442_000.0,
        "net_income": 111_000.0,
        "fiscal_quarter": 4,
    }


def test_hone_quarters_close_exactly_to_audited_years() -> None:
    checks = _annual_identity_checks(extract_quarters(SOURCE))
    assert checks[0]["difference"] == {"revenue": 0.0, "net_income": 0.0}
    assert checks[1]["difference"] == {"revenue": 0.0, "net_income": 0.0}


def test_hone_run_binds_s1_sha_pit_and_blocked_gates(tmp_path: Path) -> None:
    report = run(output_dir=tmp_path)
    assert report["accepted_quarter_count"] == 8
    assert report["fact_count"] == 16
    assert report["point_in_time_proven"] is True
    assert report["parameters_frozen"] is False
    assert report["policy_status"] == "RESEARCH_PRETRAINING_ONLY_UNFROZEN"
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    assert len(report["filing_sources"][0]["sha256"]) == 64
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert facts["available_date"].eq("2019-03-11").all()
    assert facts.groupby("fiscal_end")["metric"].nunique().eq(2).all()
