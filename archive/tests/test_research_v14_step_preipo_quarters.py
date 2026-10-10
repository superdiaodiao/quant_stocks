from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from scripts.research_v14_step_preipo_quarters import (
    COMPANYFACTS_CACHE,
    EXPECTED,
    EXPECTED_S1_OPERANDS,
    EXPECTED_TTM,
    REJECTED_NET_INCOME_CONCEPT,
    S1_PATH,
    TARGET_FISCAL_ENDS,
    annual_identity_checks,
    ensure_s1,
    extract_s1_operands,
    integrate_candidate,
    load_companyfacts,
    recover_quarters,
    run,
    snapshot_checks,
)


def test_step_s1_sha_predecessor_boundary_and_q1_operands() -> None:
    source = ensure_s1(S1_PATH)
    assert extract_s1_operands(source) == EXPECTED_S1_OPERANDS


def test_step_companyfacts_selects_revenues_and_profitloss_only() -> None:
    facts, annual = recover_quarters(
        load_companyfacts(COMPANYFACTS_CACHE),
        extract_s1_operands(ensure_s1(S1_PATH)),
    )
    recovered = {
        str(pd.Timestamp(end).date()): group.set_index("metric")["value"].to_dict()
        for end, group in facts.groupby(pd.to_datetime(facts["fiscal_end"]))
    }
    assert recovered == EXPECTED
    assert len(facts) == 16
    assert facts.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    assert not facts["concept"].str.contains(REJECTED_NET_INCOME_CONCEPT).any()
    assert set(
        facts.loc[facts["metric"].eq("net_income"), "concept"]
    ) == {"ProfitLoss", "derived_q4:ProfitLoss"}
    assert annual == {
        "2020-03-31": {"revenue": 446_611_000.0, "net_income": 144_785_000.0},
        "2021-03-31": {"revenue": 787_716_000.0, "net_income": 314_593_000.0},
    }


def test_step_never_falls_back_to_parent_net_income() -> None:
    envelope = deepcopy(load_companyfacts(COMPANYFACTS_CACHE))
    del envelope["payload"]["facts"]["us-gaap"]["ProfitLoss"]
    with pytest.raises(RuntimeError, match="fallback is forbidden"):
        recover_quarters(envelope, EXPECTED_S1_OPERANDS)


def test_step_run_closes_annuals_and_validates_six_pit_snapshots(
    tmp_path: Path,
) -> None:
    report = run(output_dir=tmp_path)
    assert report["accepted_quarter_count"] == 8
    assert report["fact_count"] == 16
    assert report["point_in_time_proven"] is True
    assert report["formal_financials_modified"] is False
    assert report["release_status"] == "BLOCKED"
    assert report["accepted_net_income_concept"] == "ProfitLoss"
    assert report["rejected_net_income_concept"] == "NetIncomeLoss"
    assert len(report["annual_identity_checks"]) == 2
    assert all(
        check["difference"] == {"revenue": 0.0, "net_income": 0.0}
        for check in report["annual_identity_checks"]
    )
    assert len(report["snapshot_checks"]) == 6
    assert {
        check["financial_age_days"] for check in report["snapshot_checks"]
    } == {49, 78}
    assert all(
        check["growth_available_date"] == "2021-08-12"
        for check in report["snapshot_checks"]
    )
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert set(facts["fiscal_end"]) == set(TARGET_FISCAL_ENDS)
    assert pd.to_datetime(facts["available_date"]).le(
        pd.Timestamp("2021-09-30")
    ).all()


def test_step_snapshot_guard_rejects_post_signal_fact() -> None:
    facts, _ = recover_quarters(
        load_companyfacts(COMPANYFACTS_CACHE),
        EXPECTED_S1_OPERANDS,
    )
    facts.loc[
        facts["fiscal_end"].astype(str).eq("2021-06-30"), "available_date"
    ] = "2021-11-12"
    with pytest.raises(RuntimeError, match="post-signal"):
        snapshot_checks(facts)


def test_step_candidate_integration_removes_only_target_conflicts(
    tmp_path: Path,
) -> None:
    supplement = tmp_path / "supplement"
    run(output_dir=supplement)
    strict = pd.read_csv(supplement / "strict_quarterly_facts.csv")

    base = tmp_path / "base"
    base.mkdir()
    pd.DataFrame({"ticker": ["KEEP"], "value": [1]}).to_csv(
        base / "annual.csv", index=False
    )
    conflicts = strict.copy()
    conflicts["value"] = -999.0
    conflicts["concept"] = conflicts["metric"].map({
        "revenue": "Revenues",
        "net_income": REJECTED_NET_INCOME_CONCEPT,
    })
    unaffected = strict.iloc[[0]].copy()
    unaffected["ticker"] = "KEEP"
    step_outside = strict.iloc[[0]].copy()
    step_outside["fiscal_end"] = "2019-06-30"
    quarterly = pd.concat(
        [conflicts, unaffected, step_outside], ignore_index=True
    )
    quarterly.to_csv(base / "quarterly.csv", index=False)
    (base / "manifest.json").write_text("{}\n", encoding="utf-8")

    output = tmp_path / "candidate"
    report = integrate_candidate(
        base_dir=base,
        supplement_dir=supplement,
        output_dir=output,
    )
    assert report["removed_conflict_rows"] == 16
    assert report["inserted_strict_rows"] == 16
    assert report["formal_financials_modified"] is False
    assert report["release_status"] == "BLOCKED"

    merged = pd.read_csv(output / "quarterly.csv")
    target = merged.loc[
        merged["ticker"].eq("STEP")
        & merged["fiscal_end"].isin(TARGET_FISCAL_ENDS)
        & merged["metric"].isin({"revenue", "net_income"})
    ]
    assert len(target) == 16
    assert not target["value"].eq(-999.0).any()
    assert len(merged.loc[merged["ticker"].eq("KEEP")]) == 1
    assert len(
        merged.loc[
            merged["ticker"].eq("STEP")
            & merged["fiscal_end"].eq("2019-06-30")
        ]
    ) == 1


def test_step_exact_ttm_values_are_stable() -> None:
    facts, _ = recover_quarters(
        load_companyfacts(COMPANYFACTS_CACHE),
        EXPECTED_S1_OPERANDS,
    )
    checks = snapshot_checks(facts)
    assert all(
        check["revenue_ttm"] == EXPECTED_TTM["revenue_ttm"]
        and check["net_income_ttm"] == EXPECTED_TTM["net_income_ttm"]
        and check["revenue_growth"] == EXPECTED_TTM["revenue_growth"]
        and check["net_income_growth"] == EXPECTED_TTM["net_income_growth"]
        for check in checks
    )
