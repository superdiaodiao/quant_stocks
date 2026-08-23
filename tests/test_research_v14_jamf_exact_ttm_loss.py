from copy import deepcopy
from dataclasses import replace
import gzip
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_jamf_exact_ttm_loss as jamf_ttm
from scripts.research_v14_jamf_exact_ttm_loss import (
    AUDIT_OBSERVATIONS,
    COMPANYFACTS_CACHE,
    EXPECTED_PARSED_TABLES,
    OPERANDS_USD_THOUSANDS,
    REJECTED_LATER_FILINGS,
    REVIEWED_PREIPO_FILINGS,
    SOURCE_DOCUMENTS,
    SOURCE_PARSE_SPECS,
    SOURCE_TEXT_CHECKS,
    build,
    companyfacts_operand_audit,
    direct_ttm_facts,
    exact_ttm_evidence,
    resolve_audit_observations,
    validate_source_lock,
)
from src.financial.quarterly_fundamentals import (
    quarterly_growth_snapshot,
    quarterly_profit_ttm_snapshot,
)
from src.research import can_slim_validation


def _fixture_source_bytes(
    source_id: str,
    overrides: dict[str, dict[str, int]] | None = None,
) -> bytes:
    overrides = overrides or {}
    paragraphs = "".join(
        f"<p>{fragment}</p>" for fragment in SOURCE_TEXT_CHECKS.get(source_id, ())
    )
    tables = []
    for parse_id, spec in SOURCE_PARSE_SPECS.items():
        if spec["source_id"] != source_id:
            continue
        values = overrides.get(parse_id, EXPECTED_PARSED_TABLES[parse_id])
        cells = ["<td>Net loss</td>"]
        for column in spec["columns"]:
            value = int(values[column])
            rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.append(f"<td>{rendered}</td>")
        tables.append(
            "<table><tr><td>"
            + " | ".join(spec["context_phrases"])
            + "</td></tr><tr>"
            + "".join(cells)
            + "</tr></table>"
        )
    return ("<html>" + paragraphs + "".join(tables) + "</html>").encode()


def _install_source_fixtures(
    tmp_path,
    monkeypatch,
    *,
    missing_source: str | None = None,
    overrides: dict[str, dict[str, int]] | None = None,
):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw
        if source_id != missing_source:
            path = tmp_path / source["local_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    download_calls = []

    def fake_download(url: str) -> bytes:
        download_calls.append(url)
        return downloads[url]

    monkeypatch.setattr(jamf_ttm, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(jamf_ttm, "_download_source", fake_download)
    return sources, download_calls


def _companyfacts_fixture(tmp_path):
    facts = []
    for operand in jamf_ttm.COMPANYFACTS_OPERANDS:
        facts.append({
            "start": operand["start"],
            "end": operand["end"],
            "val": operand["value"],
            "accn": "0001628280-21-017415",
            "form": "10-Q",
            "filed": "2021-08-20",
        })
    document = {
        "cik": 1_721_947,
        "fetched_at": "2026-08-23T00:00:00",
        "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0001721947.json",
        "payload": {
            "cik": 1_721_947,
            "facts": {
                "us-gaap": {"NetIncomeLoss": {"units": {"USD": facts}}}
            },
        },
    }
    path = tmp_path / "companyfacts.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(document, handle)
    return path


def test_exact_ttm_uses_only_same_filing_revised_operands() -> None:
    evidence = exact_ttm_evidence()

    assert evidence["net_income_ttm"] == -34_808_000
    assert evidence["formula"] == (
        "FY2020_revised - H1_2020_revised + H1_2021"
    )
    assert evidence["available_date"] == "2021-08-20"
    assert evidence["source_accession"] == "0001628280-21-017415"
    assert set(evidence["operand_ids"]) == set(OPERANDS_USD_THOUSANDS)

    facts = direct_ttm_facts(fetched_at="2026-08-23")
    assert len(facts) == 1
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert not set(facts["metric"]) & {
        "net_income", "revenue", "net_income_growth", "revenue_growth"
    }


def test_revision_bridges_are_isolated_and_reconcile() -> None:
    annual = EXPECTED_PARSED_TABLES["fy2020_revision_bridge"]
    h1 = EXPECTED_PARSED_TABLES["h1_2020_revision_bridge"]
    statement = EXPECTED_PARSED_TABLES["q2_statement_revised"]

    assert annual["FY2020_original"] == -22_771
    assert annual["FY2020_original"] + annual["commission_adjustment"] + annual[
        "other_adjustment"
    ] == annual["FY2020_revised"] == -24_082
    assert h1["H1_2020_original"] + h1["commission_adjustment"] + h1[
        "other_adjustment"
    ] == h1["H1_2020_revised"] == -10_330
    assert statement["H1_2020_revised"] == h1["H1_2020_revised"]
    assert EXPECTED_PARSED_TABLES["original_fy2020_statement"][
        "FY2020_original"
    ] == annual["FY2020_original"]


def test_all_four_observations_resolve_as_known_nonpositive_profit() -> None:
    assert len(AUDIT_OBSERVATIONS) == 4
    resolved = resolve_audit_observations()

    assert resolved["resolved"].all()
    assert set(resolved["decision"]) == {"known_nonpositive_profit"}
    assert resolved.groupby("signal_date").size().to_dict() == {
        "2021-08-31": 2,
        "2021-10-29": 2,
    }
    assert set(resolved.loc[
        resolved["signal_date"].eq("2021-08-31"), "financial_age_days"
    ]) == {11}
    assert set(resolved.loc[
        resolved["signal_date"].eq("2021-10-29"), "financial_age_days"
    ]) == {70}
    assert set(resolved["net_income_ttm"]) == {-34_808_000}


def test_direct_loss_is_pit_limited_and_cannot_create_growth() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-08-19"), 550
    )
    at_filing = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-08-20"), 150
    )
    at_last_signal = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2021-10-29"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-10-29"), 550
    )

    assert before.empty
    assert at_filing.loc["JAMF", "net_income_ttm"] == -34_808_000
    assert at_last_signal.loc["JAMF", "net_income_ttm"] == -34_808_000
    assert growth.empty


def test_real_coverage_classifies_all_four_observations(monkeypatch) -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    monkeypatch.setattr(
        can_slim_validation,
        "scheduled_signal_dates",
        lambda _index, start, *_args: [pd.Timestamp(start)],
    )
    monkeypatch.setattr(
        can_slim_validation, "market_regime_is_on", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        can_slim_validation,
        "build_can_slim_technical_cross_section",
        lambda *_a, **_k: pd.DataFrame(
            {"nonfinancial_candidate": [True]}, index=["JAMF"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    for _scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        as_of = pd.Timestamp(signal_date)
        close = pd.DataFrame({"JAMF": [20.0]}, index=[as_of])
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end=signal_date,
            maximum_financial_age_days=maximum_age_days,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            pd.DataFrame({"JAMF": [20_000_000.0]}, index=[as_of]),
            pd.Series([100.0], index=[as_of]),
            facts,
            {as_of: {"JAMF"}},
            config,
            start=signal_date,
            adjusted_close=close,
        )
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        assert row["usable_financial_count"] == 0
        assert row["known_nonpositive_profit_count"] == 1


def test_raw_companyfacts_matches_all_three_operands() -> None:
    result = companyfacts_operand_audit(COMPANYFACTS_CACHE)
    assert len(result["matched_operands"]) == 3
    assert {item["value"] for item in result["matched_operands"]} == {
        -24_082_000,
        -10_330_000,
        -21_056_000,
    }
    assert {item["accession"] for item in result["matched_operands"]} == {
        "0001628280-21-017415"
    }


def test_official_source_paths_dates_and_hashes_are_locked() -> None:
    assert {
        source_id: (source["filed"], source["accession"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == {
        "10k_2021_03_04_fy2020_original": (
            "2021-03-04",
            "0001558370-21-002391",
            "5091a4384cbe9aa8d46cea598d35466cedddc4237646ceb6f516cd67efe50371",
        ),
        "10q_2021_08_20_q2_revised": (
            "2021-08-20",
            "0001628280-21-017415",
            "5183cd37a5f4032279ae5812c416081f20aa4c600ff5506a641e2319f1a39ad7",
        ),
        "8k_2021_08_27_correction_confirmation": (
            "2021-08-27",
            "0001628280-21-017726",
            "e6e1b5ac4c8c0fb4e0f2505f14d9557981d3b0b232eb563eec9080fc48bdf924",
        ),
    }


def test_build_downloads_missing_source_and_verifies_tables(
    tmp_path, monkeypatch
) -> None:
    missing = "10q_2021_08_20_q2_revised"
    sources, download_calls = _install_source_fixtures(
        tmp_path, monkeypatch, missing_source=missing
    )
    report = build(tmp_path, _companyfacts_fixture(tmp_path))
    manifest = json.loads((tmp_path / "manifest.json").read_text())

    assert download_calls == [sources[missing]["url"]]
    assert report["accepted_exact_ttm_loss_count"] == 1
    assert report["resolved_audit_observation_count"] == 4
    assert report["resolved_unique_signal_date_count"] == 2
    assert len(report["source_value_verification"]["parsed_tables"]) == 4
    assert len(report["source_value_verification"]["operands"]) == 3
    assert len(report["source_value_verification"]["fragments"]) == 7
    assert manifest["sources"][missing]["actual_sha256"] == (
        sources[missing]["expected_sha256"]
    )


def test_build_rejects_source_value_drift(tmp_path, monkeypatch) -> None:
    changed = deepcopy(EXPECTED_PARSED_TABLES["q2_statement_revised"])
    changed["H1_2021"] = -21_055
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        overrides={"q2_statement_revised": changed},
    )
    with pytest.raises(RuntimeError, match="parsed table changed"):
        build(tmp_path, _companyfacts_fixture(tmp_path))


def test_source_lock_rejects_mixed_currency_and_later_filings() -> None:
    mixed = deepcopy(SOURCE_DOCUMENTS)
    mixed["10q_2021_08_20_q2_revised"]["currency"] = "EUR"
    with pytest.raises(ValueError, match="incompatible units"):
        validate_source_lock(mixed)

    validate_source_lock()
    assert REJECTED_LATER_FILINGS["0001628280-21-023008"]["filed"] == (
        "2021-11-12"
    )
    assert REVIEWED_PREIPO_FILINGS["0001047469-20-004160"]["filed"] == (
        "2020-07-20"
    )
