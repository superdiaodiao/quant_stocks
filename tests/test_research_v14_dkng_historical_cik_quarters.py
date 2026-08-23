import copy
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.research_v14_dkng_historical_cik_quarters import (
    CURRENT_CIK,
    EXPECTED_QUARTERS,
    HISTORICAL_CIK,
    METRIC_CONCEPTS,
    OUTPUT_COLUMNS,
    REQUIRED_FACTS,
    RESTATEMENT_ACCESSIONS,
    SHELL_ACCESSIONS,
    SOURCES,
    TARGET_FISCAL_ENDS,
    integrate_candidate,
    recover,
)


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


def test_recover_uses_only_original_operating_accessions(tmp_path: Path) -> None:
    raw = tmp_path / "companyfacts.json"
    output = tmp_path / "supplement"
    _write_payload(raw)

    report = recover(raw_path=raw, output_dir=output, fetched_at="2026-08-23")

    assert report["accepted_quarter_count"] == 8
    assert report["signal_coverage"]["2020-09-30"]["recoverable"] is False
    assert report["signal_coverage"]["2021-02-26"]["recoverable"] is True
    assert report["ttm_checks"]["2020"] == {
        "revenue": 614_532_000.0,
        "net_income": -844_270_000.0,
    }
    assert report["ttm_checks"]["2020_net_income_is_negative"] is True
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
        for fiscal_end, group in facts.groupby("fiscal_end")
    }
    assert observed == EXPECTED_QUARTERS
    assert set(facts["accession"]) == {spec["accession"] for spec in SOURCES.values()}
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
        recover(raw_path=raw, output_dir=tmp_path / "current")

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
        recover(raw_path=raw, output_dir=tmp_path / "restated")


def test_integration_replaces_only_exact_dkng_quarter_metric_scope(tmp_path: Path) -> None:
    raw = tmp_path / "companyfacts.json"
    supplement = tmp_path / "supplement"
    _write_payload(raw)
    recover(raw_path=raw, output_dir=supplement, fetched_at="2026-08-23")

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
    assert report["inserted_strict_rows"] == 16
    assert report["formal_financials_modified"] is False
    result = pd.read_csv(candidate / "quarterly.csv")
    assert not result["value"].eq(-999.0).any()
    for expected in original_outside:
        match = result.loc[result["accession"].eq(expected["accession"])]
        assert len(match) == 1
        assert float(match.iloc[0]["value"]) == expected["value"]
    assert (candidate / "annual.csv").read_bytes() == (base / "annual.csv").read_bytes()
