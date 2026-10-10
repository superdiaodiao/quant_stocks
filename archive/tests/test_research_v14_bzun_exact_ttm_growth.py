from __future__ import annotations

from copy import deepcopy
import hashlib

import pandas as pd
import pytest

import scripts.research_v14_bzun_exact_ttm_growth as bzun_growth
from scripts.research_v14_bzun_exact_ttm_growth import (
    ACCOUNTING_STANDARD,
    AUDIT_OBSERVATIONS,
    BASELINE_BINDING,
    CURRENCY,
    OPERANDS_RMB_THOUSANDS,
    REJECTED_LATER_FILINGS,
    SNAPSHOT_DEFINITIONS,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    exact_ttm_evidence,
    resolve_audit_observations,
    strict_quarterly_facts,
    validate_exact_package,
    validate_source_lock,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


def _fixture_source_bytes(
    source_id: str,
    value_overrides: dict[tuple[str, str], tuple[int, ...]] | None = None,
) -> bytes:
    overrides = value_overrides or {}
    paragraphs = "".join(
        f"<p>{fragment}</p>" for fragment in SOURCE_TEXT_CHECKS[source_id]
    )
    rows = []
    for check in SOURCE_ROW_CHECKS[source_id]:
        values = overrides.get(
            (source_id, check["metric"]), tuple(check["expected_values"])
        )
        cells = [f"<td>{check['line_item']}</td>"]
        for value in values:
            rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.append(f"<td>{rendered}</td>")
        rows.append("<table><tr>" + "".join(cells) + "</tr></table>")
    return ("<html>" + paragraphs + "".join(rows) + "</html>").encode()


def _install_source_fixtures(
    monkeypatch,
    *,
    value_overrides: dict[tuple[str, str], tuple[int, ...]] | None = None,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads: dict[str, bytes] = {}
    calls: list[str] = []
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, value_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[str(source["url"])] = raw

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return downloads[url]

    monkeypatch.setattr(bzun_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(bzun_growth, "_download_source", fake_download)
    return sources, calls


def _snapshot_facts() -> pd.DataFrame:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    return facts


def _snapshot_map() -> dict[str, dict[str, object]]:
    return {
        snapshot["fiscal_end"]: snapshot
        for snapshot in exact_ttm_evidence()["snapshots"]
    }


def test_three_exact_ttm_identities_and_growth() -> None:
    snapshots = _snapshot_map()
    expected = {
        "2019-03-31": {
            "revenue": (4_265_136, 5_758_599),
            "net_income": (213_192, 288_790),
        },
        "2020-06-30": {
            "revenue": (6_303_679, 7_962_927),
            "net_income": (319_101, 302_236),
        },
        "2020-09-30": {
            "revenue": (6_696_012, 8_288_992),
            "net_income": (328_666, 327_519),
        },
    }
    for fiscal_end, metrics in expected.items():
        snapshot = snapshots[fiscal_end]
        for metric, (prior, current) in metrics.items():
            derived = snapshot["derived"][metric]
            assert derived["prior_ttm_rmb_thousands"] == prior
            assert derived["current_ttm_rmb_thousands"] == current
            assert derived["growth"] == pytest.approx((current - prior) / prior)
    validate_exact_package()


def test_operands_are_exact_rmb_us_gaap_issuer_values() -> None:
    revenue = OPERANDS_RMB_THOUSANDS["revenue"]
    profit = OPERANDS_RMB_THOUSANDS["net_income"]
    assert revenue["fy2017"] == 4_148_808
    assert revenue["fy2018"] == 5_393_037
    assert revenue["fy2019"] == 7_278_192
    assert profit["fy2017"] == 208_866
    assert profit["fy2018"] == 269_712
    assert profit["fy2019"] == 281_297
    evidence = exact_ttm_evidence()
    assert evidence["currency"] == CURRENCY == "RMB"
    assert evidence["accounting_standard"] == ACCOUNTING_STANDARD == "US-GAAP"
    assert "ordinary shareholders" in evidence["metric_mapping"]["net_income"]
    assert "not consolidated" in evidence["metric_mapping"]["net_income"]


def test_strict_facts_are_three_complete_direct_growth_packages() -> None:
    facts = strict_quarterly_facts()
    assert len(facts) == 12
    assert set(facts["fiscal_end"]) == {
        "2019-03-31",
        "2020-06-30",
        "2020-09-30",
    }
    assert set(facts["available_date"]) == {
        "2019-05-29",
        "2020-08-21",
        "2020-11-23",
    }
    assert set(facts["metric"]) == {
        "revenue_ttm",
        "revenue_growth",
        "net_income_ttm",
        "net_income_growth",
    }
    values = facts.set_index(["fiscal_end", "metric"])["value"]
    assert values[("2019-03-31", "revenue_ttm")] == 5_758_599_000
    assert values[("2019-03-31", "net_income_ttm")] == 288_790_000
    assert values[("2020-06-30", "revenue_ttm")] == 7_962_927_000
    assert values[("2020-06-30", "net_income_ttm")] == 302_236_000
    assert values[("2020-09-30", "revenue_ttm")] == 8_288_992_000
    assert values[("2020-09-30", "net_income_ttm")] == 327_519_000


def test_real_snapshot_selector_uses_only_filed_information() -> None:
    facts = _snapshot_facts()
    expected = {
        "2019-07-31": ("2019-03-31", 63, 5_758_599_000, 288_790_000),
        "2020-08-31": ("2020-06-30", 10, 7_962_927_000, 302_236_000),
        "2021-01-29": ("2020-09-30", 67, 8_288_992_000, 327_519_000),
    }
    for signal_date, (fiscal_end, age_days, revenue, profit) in expected.items():
        before = quarterly_growth_snapshot(
            facts, pd.Timestamp(signal_date), maximum_age_days=age_days - 1
        )
        assert "BZUN" not in before.index
        for age_limit in (150, 365, 550):
            snapshot = quarterly_growth_snapshot(
                facts, pd.Timestamp(signal_date), maximum_age_days=age_limit
            )
            assert snapshot.loc["BZUN", "fiscal_end"] == pd.Timestamp(fiscal_end)
            assert snapshot.loc["BZUN", "financial_age_days"] == age_days
            assert snapshot.loc["BZUN", "revenue_ttm"] == revenue
            assert snapshot.loc["BZUN", "net_income_ttm"] == profit


def test_all_18_real_audit_observations_resolve() -> None:
    assert len(AUDIT_OBSERVATIONS) == 18
    assert {row[1] for row in AUDIT_OBSERVATIONS} == {
        "2019-07-31", "2020-08-31", "2021-01-29"
    }
    assert {row[2] for row in AUDIT_OBSERVATIONS} == {150, 365, 550}
    assert {row[0].split("-age")[0] for row in AUDIT_OBSERVATIONS} == {
        "liq2000000", "liq10000000"
    }
    resolution = resolve_audit_observations()
    assert len(resolution) == 18
    assert resolution["resolved"].all()
    assert set(resolution["financial_age_days"]) == {10, 63, 67}
    assert set(resolution["decision"]) == {
        "complete_exact_as_reported_ttm_growth_bundle"
    }
    assert resolution.groupby("signal_date").size().to_dict() == {
        "2019-07-31": 6,
        "2020-08-31": 6,
        "2021-01-29": 6,
    }


def test_every_snapshot_source_was_filed_by_its_available_date() -> None:
    for snapshot in SNAPSHOT_DEFINITIONS:
        assert max(
            SOURCE_DOCUMENTS[source_id]["filed"]
            for source_id in snapshot["source_ids"]
        ) == snapshot["available_date"]
        assert all(
            SOURCE_DOCUMENTS[source_id]["accepted"].startswith(
                SOURCE_DOCUMENTS[source_id]["filed"] + "T"
            )
            for source_id in snapshot["source_ids"]
        )


def test_official_urls_accessions_dates_acceptance_and_full_hashes_are_locked() -> None:
    expected = {
        "20f_2018_04_11_fy2017": (
            "2018-04-11", "2018-04-11T20:37:07.000Z",
            "0001144204-18-020145",
            "a83306d54269d08055fb7002b9494b9965c1c5bf3e8525d943c645e9754d8f61",
        ),
        "6k_2018_05_17_q1": (
            "2018-05-17", "2018-05-17T11:37:34.000Z",
            "0001144204-18-029393",
            "94730e783f8b20ebf238066213620a7d074e49fafad8746de429a91fcee77895",
        ),
        "20f_2019_04_03_fy2018": (
            "2019-04-03", "2019-04-03T18:05:49.000Z",
            "0001144204-19-017964",
            "0e36099ba5fcda04577622586702b7d95d87f6ccf05c958b50a9a18cc2481c15",
        ),
        "6k_2019_05_29_q1": (
            "2019-05-29", "2019-05-29T10:13:28.000Z",
            "0001144204-19-028516",
            "35dbe3f246a25c2536a6ec552f3fd394f8903ca6e42626a06e11faaa76fcb710",
        ),
        "6k_2019_08_21_q2": (
            "2019-08-21", "2019-08-21T10:06:50.000Z",
            "0001144204-19-040869",
            "5bc87acb86eebf93247e334eb16917af164d8788cae296ada6c6e51378d6b7e7",
        ),
        "6k_2019_11_21_q3": (
            "2019-11-21", "2019-11-21T11:02:15.000Z",
            "0001104659-19-066004",
            "44e4e2dbc43f66c525bf5463aecddd7416f98f89b7a4f1c8a12d3c8636e475e9",
        ),
        "20f_2020_04_28_fy2019": (
            "2020-04-28", "2020-04-28T13:30:32.000Z",
            "0001104659-20-052015",
            "15e8caf50e56cea652a92a644217ee75e0d65727cb5e5e8b5ed2165968232b12",
        ),
        "6k_2020_06_02_q1": (
            "2020-06-02", "2020-06-02T10:40:42.000Z",
            "0001104659-20-068642",
            "4ebf1f43bc31615260c784516d99bc0d9f1c2954e195fb4329f9bd1d3cebada7",
        ),
        "6k_2020_08_21_q2": (
            "2020-08-21", "2020-08-21T12:14:09.000Z",
            "0001104659-20-097425",
            "719db84f50d643a6d232986834167536b13f69c6ad4def25112cce25791944ea",
        ),
        "6k_2020_11_23_q3": (
            "2020-11-23", "2020-11-23T11:59:58.000Z",
            "0001104659-20-128179",
            "4d28963feeb919c8e529e6c9ad9b8552514f932c63f7ce8f702a4b5114633b0c",
        ),
    }
    assert {
        source_id: (
            source["filed"], source["accepted"], source["accession"],
            source["expected_sha256"],
        )
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected
    assert all(
        str(source["url"]).startswith(
            "https://www.sec.gov/Archives/edgar/data/1625414/"
        )
        for source in SOURCE_DOCUMENTS.values()
    )
    validate_source_lock()


def test_source_lock_rejects_mixed_basis_fx_and_post_snapshot_source() -> None:
    mixed = deepcopy(SOURCE_DOCUMENTS)
    mixed["6k_2020_11_23_q3"]["currency"] = "USD"
    with pytest.raises(ValueError, match="mixed currency or scale"):
        validate_source_lock(mixed)

    non_gaap = deepcopy(SOURCE_DOCUMENTS)
    non_gaap["6k_2020_11_23_q3"]["accounting_standard"] = "NON-GAAP"
    with pytest.raises(ValueError, match="non-US-GAAP"):
        validate_source_lock(non_gaap)

    late = deepcopy(SOURCE_DOCUMENTS)
    late["6k_2019_05_29_q1"]["filed"] = "2019-08-01"
    late["6k_2019_05_29_q1"]["accepted"] = "2019-08-01T10:13:28.000Z"
    with pytest.raises(ValueError, match="postdates snapshot"):
        validate_source_lock(late)


def test_restatements_adjusted_metrics_and_fx_are_explicitly_rejected() -> None:
    evidence = exact_ttm_evidence()
    assert evidence["accounting_policy_comparability"]["status"] == (
        "EXACT_AS_REPORTED_US_GAAP_RMB_COMPARABLE"
    )
    assert "full retrospective" in (
        evidence["accounting_policy_comparability"]["asc_606"]
    )
    rejected = set(evidence["rejected_measurements"])
    assert "Non-GAAP net income" in rejected
    assert "US$ convenience translations" in rejected
    assert "post-signal filings or later restatements" in rejected
    assert all(item["filed"] > "2021-01-29" for item in REJECTED_LATER_FILINGS.values())


def test_baseline_binding_is_exact() -> None:
    assert BASELINE_BINDING["audit_sha256"] == (
        "a9a3fdc3d78192cef55eb72898b988edd9805a260bf58723d45fc6baaa90d0f5"
    )
    assert BASELINE_BINDING["financial_priorities_sha256"] == (
        "ac0c18c7c24419c26e8e63065d3618b1e165f5d23a98bd6b18cfdac72f95b7e7"
    )
    assert BASELINE_BINDING["quarterly_sha256"] == (
        "532ea8465abb1c20c75f838609c267dd17cd92003981908a8b5e56b8ff7fd293"
    )
    assert BASELINE_BINDING["missing_observation_count"] == 18


def test_build_verifies_sources_and_writes_isolated_outputs(
    tmp_path, monkeypatch
) -> None:
    sources, calls = _install_source_fixtures(monkeypatch)
    report = build(tmp_path)
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    resolution = pd.read_json(tmp_path / "audit_observation_resolution.json")

    assert len(calls) == len(sources) == 10
    assert report["accepted_direct_growth_package_count"] == 3
    assert report["accepted_fact_count"] == 12
    assert report["resolved_audit_observation_count"] == 18
    assert report["expected_audit_observation_count"] == 18
    assert report["source_operand_verification_count"] == 46
    assert report["source_text_verification_count"] == 39
    assert report["shared_candidate_integrated"] is False
    assert report["formal_financials_modified"] is False
    assert report["promotion_eligible"] is False
    assert len(facts) == 12
    assert len(resolution) == 18
    assert resolution["resolved"].all()


def test_build_rejects_operand_drift(tmp_path, monkeypatch) -> None:
    source_id = "6k_2020_11_23_q3"
    _install_source_fixtures(
        monkeypatch,
        value_overrides={(source_id, "revenue"): (1_503_094, 1_829_158)},
    )
    with pytest.raises(RuntimeError, match="source row changed"):
        build(tmp_path)


def test_build_rejects_full_file_sha_drift(tmp_path, monkeypatch) -> None:
    sources, _ = _install_source_fixtures(monkeypatch)
    source_id = "20f_2019_04_03_fy2018"
    source = sources[source_id]
    local_path = tmp_path / str(source["local_path"])
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(_fixture_source_bytes(source_id) + b"drift")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        build(tmp_path)
