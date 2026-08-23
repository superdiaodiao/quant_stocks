from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import (
    EXPECTED_ANNUAL,
    EXPECTED_Q1,
    EXPECTED_Q4,
    extract_s1_q1,
    integrate_candidate,
    run,
)


SOURCE = Path("output/data_provenance/adpt_preipo/adpt_2019_s1.htm")


def test_adpt_extracts_two_direct_s1_q1_comparatives():
    assert extract_s1_q1(SOURCE) == EXPECTED_Q1


def test_adpt_run_closes_two_years_and_preserves_pit(tmp_path: Path):
    report = run(output_dir=tmp_path)
    assert report["accepted_quarter_count"] == 8
    assert report["annual_identity_checks"] == EXPECTED_ANNUAL
    assert report["q4_residual"] == EXPECTED_Q4
    assert report["point_in_time_proven"] is True
    assert report["parameters_frozen"] is False
    assert report["release_status"] == "BLOCKED"

    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert len(facts) == 16
    assert facts.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    q1 = facts[facts["fiscal_end"].str.endswith("03-31")]
    q4 = facts[facts["fiscal_end"].str.endswith("12-31")]
    assert q1["available_date"].eq("2019-05-30").all()
    assert q4["available_date"].eq("2020-02-26").all()


def test_adpt_candidate_integration_binds_sources(tmp_path: Path):
    supplement = tmp_path / "supplement"
    run(output_dir=supplement)
    base = tmp_path / "base"
    base.mkdir()
    pd.DataFrame({"ticker": ["ZZZ"], "value": [1]}).to_csv(
        base / "annual.csv", index=False
    )
    columns = pd.read_csv(supplement / "strict_quarterly_facts.csv").columns
    pd.DataFrame(columns=columns).to_csv(base / "quarterly.csv", index=False)
    (base / "manifest.json").write_text("{}\n")
    output = tmp_path / "candidate"
    report = integrate_candidate(
        base_dir=base, supplement_dir=supplement, output_dir=output
    )
    assert report["inserted_identity_rows"] == 16
    assert report["formal_financials_modified"] is False
    assert report["release_status"] == "BLOCKED"
    assert pd.read_csv(output / "quarterly.csv").shape[0] == 16
