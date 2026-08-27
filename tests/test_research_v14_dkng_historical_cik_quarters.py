import copy
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.research_v14_dkng_historical_cik_quarters import (
    CURRENT_CIK,
    AUDIT_OBSERVATIONS,
    DIRECT_TTM_METRICS,
    EXPECTED_QUARTERS,
    EXPECTED_SIGNAL_TTM,
    HISTORICAL_CIK,
    METRIC_CONCEPTS,
    OUTPUT_COLUMNS,
    REQUIRED_FACTS,
    RESTATEMENT_ACCESSIONS,
    S4A_SOURCE,
    SHELL_ACCESSIONS,
    SOURCES,
    TARGET_FISCAL_ENDS,
    integrate_candidate,
    recover,
    verify_s4a_actual_annuals,
)
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def _payload() -> dict:
    payload = {
        "cik": HISTORICAL_CIK,
        "entityName": "DraftKings Inc.",
        "facts": {"us-gaap": {
            concept: {"units": {"USD": []}}
            for concept in METRIC_CONCEPTS.values()
        }},
    }
    for requirement in REQUIRED_FACTS.values():
        source = SOURCES[requirement["source"]]
        for metric, concept in METRIC_CONCEPTS.items():
            payload["facts"]["us-gaap"][concept]["units"]["USD"].append({
                "start": requirement["start"],
                "end": requirement["end"],
                "val": requirement[metric],
                "accn": source["accession"],
                "form": source["form"],
                "filed": source["filed"],
            })

    # Same historical CIK also contains these forbidden alternatives.  They
    # deliberately conflict with the Old DraftKings operating facts.
    net_income = payload["facts"]["us-gaap"][METRIC_CONCEPTS["net_income"]]["units"]["USD"]
    net_income.extend([
        {
            "start": "2020-01-01", "end": "2020-03-31", "val": 479_411.0,
            "accn": sorted(SHELL_ACCESSIONS)[1], "form": "10-Q", "filed": "2020-05-15",
        },
        {
            "start": "2020-01-01", "end": "2020-12-31", "val": -1_231_835_000.0,
            "accn": next(iter(RESTATEMENT_ACCESSIONS)), "form": "10-K/A", "filed": "2021-05-03",
        },
    ])
    payload["facts"]["us-gaap"]["BusinessAcquisitionsProFormaRevenue"] = {
        "units": {"USD": [{
            "start": "2020-01-01", "end": "2020-12-31", "val": 643_502_000.0,
            "accn": SOURCES["2020_fy"]["accession"], "form": "10-K", "filed": "2021-02-26",
        }]}
    }
    return payload


def _write_payload(path: Path, payload: dict | None = None) -> None:
    path.write_text(json.dumps(_payload() if payload is None else payload))


def _s4a_payload() -> bytes:
    return (
        "<html>SELECTED HISTORICAL CONSOLIDATED FINANCIAL INFORMATION OF "
        "DRAFTKINGS derived from the audited historical consolidated financial "
        "statements of DraftKings prior to and without giving pro forma effect "
        "to the impact of the Business Combination For the year ended December "
        "31 Statement of Operations Data Revenue 323,410 $ 226,277 $ 191,844 "
        "Net Loss $ (142,734) $ (76,220) $ (75,556)</html>"
    ).encode()


def _write_audit(path: Path, include_dkng: bool = True) -> str:
    rows = []
    if include_dkng:
        for scenario, _ in AUDIT_OBSERVATIONS:
            rows.append({
                "scenario": scenario,
                "ticker": "DKNG",
                "missing_signal_count": 1,
                "first_missing_signal_date": "2020-09-30",
                "last_missing_signal_date": "2020-09-30",
                "no_raw_pit_financial_facts_signal_count": 0,
                "insufficient_growth_history_signal_count": 1,
                "stale_growth_snapshot_signal_count": 0,
            })
    else:
        rows.append({
            "scenario": "liq2000000-age150-growth",
            "ticker": "OTHER",
            "missing_signal_count": 1,
            "first_missing_signal_date": "2020-09-30",
            "last_missing_signal_date": "2020-09-30",
            "no_raw_pit_financial_facts_signal_count": 1,
            "insufficient_growth_history_signal_count": 0,
            "stale_growth_snapshot_signal_count": 0,
        })
    pd.DataFrame(rows).to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recover(
    raw: Path,
    output: Path,
    *,
    fetched_at: str = "2026-08-23",
    current_audit_missing: bool = True,
):
    s4a = raw.parent / "s4a.html"
    s4a.write_bytes(_s4a_payload())
    baseline = raw.parent / "baseline.csv"
    baseline_sha = _write_audit(baseline)
    current = raw.parent / "current.csv"
    current_sha = _write_audit(current, include_dkng=current_audit_missing)
    return recover(
        raw_path=raw,
        output_dir=output,
        fetched_at=fetched_at,
        s4a_path=s4a,
        expected_raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
        expected_s4a_sha256=hashlib.sha256(s4a.read_bytes()).hexdigest(),
        baseline_audit_path=baseline,
        expected_baseline_audit_sha256=baseline_sha,
        audit_path=current,
        expected_audit_sha256=current_sha,
    )


def test_recover_uses_only_original_operating_accessions(tmp_path: Path) -> None:
    raw = tmp_path / "companyfacts.json"
    output = tmp_path / "supplement"
    _write_payload(raw)

    report = _recover(raw, output)

    assert report["accepted_quarter_count"] == 8
    assert report["accepted_fact_count"] == 18
    assert report["signal_coverage"]["2020-09-30"]["recoverable"] is True
    assert report["signal_coverage"]["2020-09-30"]["classification"] == (
        "KNOWN_NONPOSITIVE_DIRECT_TTM_PROFIT"
    )
    assert report["signal_coverage"]["2021-02-26"]["recoverable"] is True
    assert report["ttm_checks"]["2020"] == {
        "revenue": 614_532_000.0,
        "net_income": -844_270_000.0,
    }
    assert report["ttm_checks"]["2020_net_income_is_negative"] is True
    assert report["ttm_checks"]["signal_2020q2"] == EXPECTED_SIGNAL_TTM
    assert report["period_identity_checks"][2020]["9m"] == {
        "revenue": 292_309_000.0,
        "net_income": -577_870_000.0,
    }
    assert all(source["filed_on_or_before_signal"] for source in report["sources"])
    assert report["exclusions"]["pro_forma_concepts_used"] is False
    assert report["exclusions"]["current_cik_used"] is False

    facts = pd.read_csv(output / "strict_quarterly_facts.csv")
    observed = {
        fiscal_end: group.set_index("metric")["value"].to_dict()
        for fiscal_end, group in facts.loc[
            facts["metric"].isin(METRIC_CONCEPTS)
        ].groupby("fiscal_end")
    }
    assert observed == EXPECTED_QUARTERS
    assert set(facts.loc[
        facts["metric"].isin(METRIC_CONCEPTS), "accession"
    ]) == {spec["accession"] for spec in SOURCES.values()}
    direct = facts.loc[facts["metric"].isin(DIRECT_TTM_METRICS)]
    assert direct.set_index("metric")["value"].to_dict() == {
        "revenue_ttm": 357_401_000.0,
        "net_income_ttm": -315_184_000.0,
    }
    assert not set(facts["accession"]) & SHELL_ACCESSIONS
    assert not set(facts["accession"]) & RESTATEMENT_ACCESSIONS
    assert pd.to_datetime(facts["available_date"]).max() == pd.Timestamp("2021-02-26")
    assert not facts["concept"].str.contains("proforma", case=False).any()


def test_recover_rejects_current_cik_and_changed_original_fact(tmp_path: Path) -> None:
    raw = tmp_path / "companyfacts.json"
    current = _payload()
    current["cik"] = CURRENT_CIK
    _write_payload(raw, current)
    with pytest.raises(RuntimeError, match="historical CIK"):
        _recover(raw, tmp_path / "current")

    changed = _payload()
    facts = changed["facts"]["us-gaap"][METRIC_CONCEPTS["net_income"]]["units"]["USD"]
    original = next(
        fact for fact in facts
        if fact["accn"] == SOURCES["2020_fy"]["accession"]
        and fact["start"] == "2020-01-01"
        and fact["end"] == "2020-12-31"
    )
    original["val"] = -1_231_835_000.0
    _write_payload(raw, changed)
    with pytest.raises(RuntimeError, match="original fact changed"):
        _recover(raw, tmp_path / "restated")


def test_integration_replaces_only_exact_dkng_quarter_metric_scope(tmp_path: Path) -> None:
    raw = tmp_path / "companyfacts.json"
    supplement = tmp_path / "supplement"
    _write_payload(raw)
    _recover(raw, supplement)

    base = tmp_path / "base"
    base.mkdir()
    pd.DataFrame({"ticker": ["KEEP"], "value": [7]}).to_csv(
        base / "annual.csv", index=False
    )
    rows = [
        {
            "ticker": "DKNG", "fiscal_end": fiscal_end,
            "available_date": "2026-01-01", "metric": metric, "value": -999.0,
            "taxonomy": "bad", "concept": "conflict", "form": "10-K/A",
            "accession": "later", "fetched_at": "2026-01-01",
        }
        for fiscal_end in TARGET_FISCAL_ENDS for metric in METRIC_CONCEPTS
    ]
    outside = [
        {
            "ticker": "DKNG", "fiscal_end": "2018-06-30",
            "available_date": "2020-01-01", "metric": "revenue", "value": 11.0,
            "taxonomy": "keep", "concept": "keep", "form": "10-Q",
            "accession": "keep-dkng", "fetched_at": "2020-01-01",
        },
        {
            "ticker": "OTHER", "fiscal_end": "2020-12-31",
            "available_date": "2021-01-01", "metric": "net_income", "value": 12.0,
            "taxonomy": "keep", "concept": "keep", "form": "10-K",
            "accession": "keep-other", "fetched_at": "2021-01-01",
        },
        {
            "ticker": "DKNG", "fiscal_end": "2020-12-31",
            "available_date": "2021-02-26", "metric": "assets", "value": 13.0,
            "taxonomy": "keep", "concept": "keep", "form": "10-K",
            "accession": "keep-metric", "fetched_at": "2021-02-26",
        },
    ]
    pd.DataFrame(rows + outside, columns=OUTPUT_COLUMNS).to_csv(
        base / "quarterly.csv", index=False
    )
    (base / "manifest.json").write_text("{}\n")

    original_outside = copy.deepcopy(outside)
    candidate = tmp_path / "candidate"
    report = integrate_candidate(
        base_dir=base, supplement_dir=supplement, output_dir=candidate
    )

    assert report["removed_conflicting_rows"] == 16
    assert report["inserted_strict_rows"] == 18
    assert report["formal_financials_modified"] is False
    result = pd.read_csv(candidate / "quarterly.csv")
    assert not result["value"].eq(-999.0).any()
    for expected in original_outside:
        match = result.loc[result["accession"].eq(expected["accession"])]
        assert len(match) == 1
        assert float(match.iloc[0]["value"]) == expected["value"]
    assert (candidate / "annual.csv").read_bytes() == (base / "annual.csv").read_bytes()


def test_s4a_source_lock_accepts_only_audited_old_draftkings_actuals():
    payload = _s4a_payload()
    evidence = verify_s4a_actual_annuals(
        payload, hashlib.sha256(payload).hexdigest()
    )
    assert evidence["basis"] == (
        "Old DraftKings audited historical actuals, not pro forma"
    )
    assert evidence["annual_actuals"][2019] == {
        "revenue": 323_410_000.0,
        "net_income": -142_734_000.0,
    }
    with pytest.raises(RuntimeError, match="source SHA changed"):
        verify_s4a_actual_annuals(payload + b" changed", evidence["sha256"])


def test_direct_ttm_eliminates_missing_signal_as_known_nonpositive(tmp_path: Path):
    raw = tmp_path / "companyfacts.json"
    output = tmp_path / "supplement"
    _write_payload(raw)
    report = _recover(raw, output, current_audit_missing=False)
    assert report["audit_binding"]["current"] == {
        "path": str(tmp_path / "current.csv"),
        "sha256": hashlib.sha256(
            (tmp_path / "current.csv").read_bytes()
        ).hexdigest(),
        "remaining_observation_count": 0,
        "status": "RECOVERED_KNOWN_NONPOSITIVE_DIRECT_TTM",
    }

    facts = pd.read_csv(output / "strict_quarterly_facts.csv")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    snapshot = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-09-30"), maximum_age_days=150
    )
    assert snapshot.loc["DKNG", "net_income_ttm"] == -315_184_000.0
    assert snapshot.loc["DKNG", "financial_age_days"] == 47
