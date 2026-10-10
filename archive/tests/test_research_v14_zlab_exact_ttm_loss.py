from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

import scripts.research_v14_zlab_exact_ttm_loss as zlab_ttm
from scripts.research_v14_zlab_exact_ttm_loss import (
    AUDIT_OBSERVATIONS,
    OPERANDS,
    REJECTED_LATER_FILINGS,
    SOURCE_DOCUMENTS,
    SOURCE_PARSE_SPECS,
    build,
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
    operand_overrides: dict[str, int] | None = None,
) -> bytes:
    overrides = operand_overrides or {}
    spec = SOURCE_PARSE_SPECS[source_id]
    values_by_column = {
        operand["table_column"]: overrides.get(operand_id, operand["value"])
        for operand_id, operand in OPERANDS.items()
        if operand["source_id"] == source_id
    }
    cells = ["<td>Net loss</td>"]
    for column in spec["columns"]:
        value = int(values_by_column.get(column, 0))
        rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
        cells.append(f"<td>{rendered}</td>")
    return (
        "<html><table><tr><td>"
        + " | ".join(spec["context_phrases"])
        + "</td></tr><tr>"
        + "".join(cells)
        + "</tr></table></html>"
    ).encode()


def _install_source_fixtures(
    tmp_path,
    monkeypatch,
    *,
    missing_source: str | None = None,
    operand_overrides: dict[str, int] | None = None,
):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, operand_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw
        if source_id != missing_source:
            local_path = tmp_path / source["local_path"]
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(raw)
    download_calls = []

    def fake_download(url: str) -> bytes:
        download_calls.append(url)
        return downloads[url]

    monkeypatch.setattr(zlab_ttm, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(zlab_ttm, "_download_source", fake_download)
    return sources, download_calls


def test_exact_ttm_math_is_scale_homogeneous_and_never_splits_quarters() -> None:
    evidence = {row["fiscal_end"]: row for row in exact_ttm_evidence()}

    assert evidence["2019-06-30"]["net_income_ttm"] == -180_858_190
    assert evidence["2019-09-30"]["net_income_ttm"] == -211_997_967
    assert evidence["2020-06-30"]["net_income_ttm"] == -240_414_000
    assert evidence["2020-06-30"]["source_scale"] == 1_000
    assert all(row["currency"] == "USD" for row in evidence.values())
    assert all(row["accounting_standard"] == "US-GAAP" for row in evidence.values())

    facts = direct_ttm_facts(fetched_at="2026-08-23")
    assert len(facts) == 3
    assert set(facts["metric"]) == {"net_income_ttm"}
    assert not set(facts["metric"]) & {
        "net_income",
        "revenue",
        "net_income_growth",
        "revenue_growth",
    }


def test_all_nine_dates_and_thirteen_scenario_observations_resolve() -> None:
    assert len(AUDIT_OBSERVATIONS) == 13
    assert len({date for _, date, _ in AUDIT_OBSERVATIONS}) == 9
    resolved = resolve_audit_observations()

    assert len(resolved) == 13
    assert resolved["resolved"].all()
    assert set(resolved["decision"]) == {"known_nonpositive_profit"}
    expected = {
        "2019-09-30": (1, "2019-06-30", 27),
        "2019-10-31": (1, "2019-06-30", 58),
        "2019-11-29": (1, "2019-06-30", 87),
        "2019-12-31": (1, "2019-06-30", 119),
        "2020-01-31": (1, "2019-09-30", 10),
        "2020-02-28": (2, "2019-09-30", 38),
        "2020-09-30": (2, "2020-06-30", 48),
        "2020-11-30": (2, "2020-06-30", 109),
        "2020-12-31": (2, "2020-06-30", 140),
    }
    for signal_date, (count, fiscal_end, financial_age_days) in expected.items():
        rows = resolved.loc[resolved["signal_date"].eq(signal_date)]
        assert len(rows) == count
        assert set(rows["fiscal_end"]) == {fiscal_end}
        assert set(rows["financial_age_days"]) == {financial_age_days}
        assert rows["net_income_ttm"].lt(0).all()


def test_direct_loss_is_pit_limited_and_cannot_create_growth() -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])

    before_h1 = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2019-09-02"), 150
    )
    at_h1 = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2019-09-03"), 150
    )
    at_9m = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-01-21"), 150
    )
    at_2020_h1 = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp("2020-08-13"), 150
    )
    growth = quarterly_growth_snapshot(
        facts, pd.Timestamp("2020-12-31"), 150
    )

    assert before_h1.empty
    assert at_h1.loc["ZLAB", "net_income_ttm"] == -180_858_190
    assert at_9m.loc["ZLAB", "net_income_ttm"] == -211_997_967
    assert at_2020_h1.loc["ZLAB", "net_income_ttm"] == -240_414_000
    assert growth.empty


def test_real_coverage_classifies_all_thirteen_observations(monkeypatch) -> None:
    facts = direct_ttm_facts(fetched_at="2026-08-23")
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    monkeypatch.setattr(
        can_slim_validation,
        "scheduled_signal_dates",
        lambda _index, start, *_args: [pd.Timestamp(start)],
    )
    monkeypatch.setattr(
        can_slim_validation,
        "market_regime_is_on",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        can_slim_validation,
        "build_can_slim_technical_cross_section",
        lambda *_args, **_kwargs: pd.DataFrame(
            {"nonfinancial_candidate": [True]}, index=["ZLAB"]
        ),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "can_slim_nonfinancial_candidate_mask",
        lambda technical, _config: pd.Series(True, index=technical.index),
    )

    for _scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        as_of = pd.Timestamp(signal_date)
        close = pd.DataFrame({"ZLAB": [20.0]}, index=[as_of])
        config = replace(
            can_slim_validation.fixed_top3_config(),
            end=signal_date,
            maximum_financial_age_days=maximum_age_days,
        )
        report = can_slim_validation.technical_candidate_financial_coverage(
            close,
            pd.DataFrame({"ZLAB": [20_000_000.0]}, index=[as_of]),
            pd.Series([100.0], index=[as_of]),
            facts,
            {as_of: {"ZLAB"}},
            config,
            start=signal_date,
            adjusted_close=close,
        )
        row = report["by_signal"][0]
        assert report["missing_financial_observations"] == 0
        assert report["complete"] is True
        assert row["usable_financial_count"] == 0
        assert row["known_nonpositive_profit_count"] == 1


def test_source_lock_rejects_later_domestic_filings_and_mixed_currency() -> None:
    validate_source_lock()
    assert set(REJECTED_LATER_FILINGS) == {
        "0001193125-21-062279",
        "0001193125-21-155749",
    }

    later = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    later["later_10k"] = {
        **next(iter(SOURCE_DOCUMENTS.values())),
        "accession": "0001193125-21-062279",
        "filed": "2021-03-01",
    }
    with pytest.raises(ValueError, match="later filing is forbidden"):
        validate_source_lock(later)

    mixed = {name: dict(source) for name, source in SOURCE_DOCUMENTS.items()}
    mixed["6k_2020_08_13_h1_ex991"]["currency"] = "RMB"
    with pytest.raises(ValueError, match="is not USD"):
        validate_source_lock(mixed)


def test_official_source_paths_and_hashes_are_locked() -> None:
    expected = {
        "6k_2019_03_07_fy2018_ex991": (
            "sources/zlab_2018_fy_ex991.htm",
            "963f0dd0bf72452375726f8830677082d0dcf0511b3bd891f3d978cc2065c264",
        ),
        "6k_2019_09_03_h1_ex991": (
            "sources/zlab_2019_h1_ex991.htm",
            "49356f8166c3843bba86cff297cb88b6497e4e254cc84b4cc831b91ae1efb185",
        ),
        "6k_2020_01_21_9m_r4": (
            "sources/zlab_2019_9m_R4.htm",
            "841aef0f3977cbc5a67b77c004a2cf847ee245766e68e547f7bafbecb0aef32b",
        ),
        "20f_2020_04_29_fy2019_r4": (
            "sources/zlab_2019_fy_R4.htm",
            "410c487a2825af93cda1cf4f2992ba403aa939f6b5f14b0c197b5c2301994747",
        ),
        "6k_2020_08_13_h1_ex991": (
            "sources/zlab_2020_h1_ex991.htm",
            "a6d89b1eaf2c11b82ef6975b9088067db1c4a049c6283963475df280c92d6232",
        ),
    }
    assert {
        source_id: (source["local_path"], source["expected_sha256"])
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected


def test_build_downloads_missing_source_and_records_operand_verification(
    tmp_path,
    monkeypatch,
) -> None:
    sources, download_calls = _install_source_fixtures(
        tmp_path,
        monkeypatch,
        missing_source="20f_2020_04_29_fy2019_r4",
    )
    report = build(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    resolutions = json.loads(
        (tmp_path / "audit_observation_resolution.json").read_text()
    )

    assert report["accepted_exact_ttm_loss_count"] == 3
    assert report["resolved_unique_signal_date_count"] == 9
    assert report["resolved_audit_observation_count"] == 13
    assert download_calls == [sources["20f_2020_04_29_fy2019_r4"]["url"]]
    assert len(manifest["operand_verification"]) == len(OPERANDS)
    assert all(
        item["parsed_value"] == item["expected_value"]
        for item in manifest["operand_verification"]
    )
    assert all(
        source["actual_sha256"] == source["expected_sha256"]
        for source in manifest["source_documents"].values()
    )
    assert manifest["research_only"] is True
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["formal_financials_modified"] is False
    assert manifest["shared_candidate_integrated"] is False
    assert manifest["revenue_assessment"]["direct_growth_emitted"] is False
    assert len(resolutions) == 13
    assert "0001193125-21-062279" not in json.dumps(resolutions)


def test_build_rejects_existing_source_with_wrong_sha(tmp_path, monkeypatch) -> None:
    sources, download_calls = _install_source_fixtures(tmp_path, monkeypatch)
    corrupted = tmp_path / sources["6k_2020_08_13_h1_ex991"]["local_path"]
    corrupted.write_bytes(corrupted.read_bytes() + b"tampered")

    with pytest.raises(RuntimeError, match="source SHA-256 mismatch"):
        build(tmp_path)
    assert download_calls == []


def test_build_rejects_hash_valid_html_when_operand_changes(
    tmp_path,
    monkeypatch,
) -> None:
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        operand_overrides={"h1_2020_net_loss_thousands": -120_000},
    )

    with pytest.raises(
        RuntimeError,
        match="operand h1_2020_net_loss_thousands source changed",
    ):
        build(tmp_path)
