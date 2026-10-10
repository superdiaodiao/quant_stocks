from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_niu_exact_ttm_growth as niu_growth
from scripts.research_v14_niu_exact_ttm_growth import (
    ACCOUNTING_STANDARD,
    ADJUSTED_OR_NON_GAAP_LABELS,
    AUDIT_OBSERVATIONS,
    COMPARATIVE_MATCHES,
    OUTPUT_COLUMNS,
    PACKAGE_METADATA,
    REJECTED_LATER_FILINGS,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    exact_ttm_evidence,
    rejected_evidence,
    resolve_audit_observations,
    strict_quarterly_facts,
    validate_candidate_operand,
    validate_exact_packages,
    validate_source_lock,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


EXPECTED_TTMS = {
    "2020-03-31": (1_660_178_080, 1_954_009_909, -275_162_267, 151_726_635),
    "2020-06-30": (1_806_427_307, 2_068_438_740, 28_805_957, 157_572_089),
}


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
            (source_id, check["metric"]), tuple(check["expected_row_values"])
        )
        cells = [f"<td>{check['line_item']}</td>", "<td></td>"]
        for value in values:
            rendered = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.extend(("<td></td>", f"<td>{rendered}</td>"))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<html>" + paragraphs + "<table>" + "".join(rows) + "</table></html>"
    ).encode()


def _install_source_fixtures(
    tmp_path,
    monkeypatch,
    *,
    missing_source: str | None = None,
    value_overrides: dict[tuple[str, str], tuple[int, ...]] | None = None,
):
    sources = deepcopy(SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = _fixture_source_bytes(source_id, value_overrides)
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw
        if source_id != missing_source:
            local_path = tmp_path / source["local_path"]
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(raw)
    calls = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return downloads[url]

    monkeypatch.setattr(niu_growth, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(niu_growth, "_download_source", fake_download)
    return sources, calls


def _snapshot_facts() -> pd.DataFrame:
    facts = strict_quarterly_facts().copy()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    return facts


def test_two_exact_cny_ttm_packages_and_growth_arithmetic() -> None:
    packages = exact_ttm_evidence()["packages"]
    assert set(packages) == set(EXPECTED_TTMS)
    for fiscal_end, (prior_revenue, revenue, prior_profit, profit) in EXPECTED_TTMS.items():
        derived = packages[fiscal_end]["derived"]
        assert derived["revenue"]["prior_ttm_cny"] == prior_revenue
        assert derived["revenue"]["current_ttm_cny"] == revenue
        assert derived["revenue"]["growth"] == pytest.approx(
            (revenue - prior_revenue) / abs(prior_revenue)
        )
        assert derived["net_income"]["prior_ttm_cny"] == prior_profit
        assert derived["net_income"]["current_ttm_cny"] == profit
        assert derived["net_income"]["growth"] == pytest.approx(
            (profit - prior_profit) / abs(prior_profit)
        )
        assert packages[fiscal_end]["currency"] == "CNY"
        assert packages[fiscal_end]["source_currency_label"] == "RMB"
        assert packages[fiscal_end]["accounting_standard"] == ACCOUNTING_STANDARD
    validate_exact_packages()


def test_strict_facts_are_two_complete_direct_growth_packages() -> None:
    facts = strict_quarterly_facts()
    assert list(facts.columns) == OUTPUT_COLUMNS
    assert len(facts) == 8
    assert set(facts["metric"]) == {
        "net_income_ttm", "net_income_growth", "revenue_ttm", "revenue_growth"
    }
    assert set(facts.groupby(["fiscal_end", "available_date"])["metric"].nunique()) == {4}
    assert facts["concept"].str.endswith(":CNY").all()
    assert not facts["concept"].str.contains("adjusted|non-gaap", case=False).any()
    for fiscal_end, (_, revenue, _, profit) in EXPECTED_TTMS.items():
        values = facts.loc[facts["fiscal_end"].eq(fiscal_end)].set_index("metric")["value"]
        assert values["revenue_ttm"] == revenue
        assert values["net_income_ttm"] == profit


def test_real_snapshot_dates_select_latest_visible_package() -> None:
    facts = _snapshot_facts()
    assert "NIU" not in quarterly_growth_snapshot(
        facts, pd.Timestamp("2020-05-18"), maximum_age_days=365
    ).index
    expected = {
        "2020-06-30": ("2020-03-31", 42),
        "2020-07-31": ("2020-03-31", 73),
        "2020-08-31": ("2020-06-30", 13),
    }
    for signal_date, (fiscal_end, age) in expected.items():
        for maximum_age_days in (150, 365):
            snapshot = quarterly_growth_snapshot(
                facts, pd.Timestamp(signal_date), maximum_age_days=maximum_age_days
            )
            assert snapshot.loc["NIU", "fiscal_end"] == pd.Timestamp(fiscal_end)
            assert snapshot.loc["NIU", "financial_age_days"] == age


def test_all_10_real_audit_observations_resolve() -> None:
    assert len(AUDIT_OBSERVATIONS) == 10
    resolution = resolve_audit_observations()
    assert len(resolution) == 10
    assert resolution["resolved"].all()
    assert resolution["signal_date"].nunique() == 3
    assert (resolution["signal_date"] == "2020-06-30").sum() == 2
    assert (resolution["signal_date"] == "2020-07-31").sum() == 4
    assert (resolution["signal_date"] == "2020-08-31").sum() == 4
    assert set(resolution["decision"]) == {
        "complete_exact_cny_cumulative_ttm_growth_bundle"
    }


def test_all_14_usd_translation_values_are_explicitly_rejected() -> None:
    rejected = rejected_evidence()
    values = rejected["excluded_usd_translation_values"]
    assert rejected["excluded_usd_translation_value_count"] == 14
    assert len(values) == 14
    for value in values:
        with pytest.raises(ValueError, match="USD convenience translation"):
            validate_candidate_operand(
                value=value,
                currency="USD",
                metric_label="Revenues",
                filed="2020-05-19",
            )


def test_adjusted_and_non_gaap_metrics_are_explicitly_rejected() -> None:
    assert len(ADJUSTED_OR_NON_GAAP_LABELS) == 3
    for label in ADJUSTED_OR_NON_GAAP_LABELS:
        with pytest.raises(ValueError, match="adjusted/non-GAAP"):
            validate_candidate_operand(
                value=1,
                currency="CNY",
                metric_label=label,
                filed="2020-05-19",
            )


def test_later_filings_are_rejected_and_never_enter_accessions() -> None:
    assert REJECTED_LATER_FILINGS["0001104659-20-128730"]["filed"] == "2020-11-24"
    assert REJECTED_LATER_FILINGS["0001104659-21-048371"]["filed"] == "2021-04-09"
    with pytest.raises(ValueError, match="later filing"):
        validate_candidate_operand(
            value=1,
            currency="CNY",
            metric_label="Net income",
            filed="2020-11-24",
        )
    accessions = "+".join(strict_quarterly_facts()["accession"])
    assert "0001104659-20-128730" not in accessions
    assert "0001104659-21-048371" not in accessions


def test_source_dates_accessions_and_full_hashes_are_locked() -> None:
    assert len(SOURCE_DOCUMENTS) == 5
    expected = {
        "20f_fy2019": ("2020-04-24", "2020-04-24T10:48:03Z", "0001104659-20-050585", "3cb326a8c9779bfda64834d5514235c9100841ebb4ded3e31a53adfcadecbded"),
        "6k_q1_2019": ("2019-05-14", "2019-05-14T10:08:20Z", "0001104659-19-029094", "a3e5034eafffc537d86b570c28e1a681d6375e58c5cf1d66463346da6b68c35c"),
        "6k_q2_2019": ("2019-08-23", "2019-08-23T12:21:01Z", "0001104659-19-047035", "f60eab42fffc08c16308366de854fe8d66463d15e258995f5cbb37bf28bd36b2"),
        "6k_q1_2020": ("2020-05-19", "2020-05-19T10:30:04Z", "0001104659-20-063446", "d420733a484841ea4c6979e7045b114f7160173def381f25a7a5a24e2d5552f7"),
        "6k_q2_2020": ("2020-08-18", "2020-08-18T11:02:03Z", "0001104659-20-096304", "3c43a45bbf7c67b47c6614d69738622c0863c70b0ccb0206d425fa887297bec8"),
    }
    assert {
        source_id: (
            source["filed"], source["accepted_at"], source["accession"],
            source["expected_sha256"],
        )
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected
    validate_source_lock()


def test_package_dates_and_original_comparatives_are_pit_consistent() -> None:
    for package in PACKAGE_METADATA.values():
        source_dates = [SOURCE_DOCUMENTS[s]["filed"] for s in package["source_ids"]]
        assert max(source_dates) == package["available_date"]
    evidence = exact_ttm_evidence()
    assert len(COMPARATIVE_MATCHES) == 6
    assert all(item["matched"] for item in evidence["comparative_matches"])
    assert "14 US$" in evidence["currency_isolation"]


def test_build_downloads_missing_source_and_emits_rejection_evidence(
    tmp_path, monkeypatch
) -> None:
    missing = "6k_q2_2020"
    sources, calls = _install_source_fixtures(
        tmp_path, monkeypatch, missing_source=missing
    )
    report = build(tmp_path)
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    rejected = json.loads((tmp_path / "rejected_evidence.json").read_text())
    unrecoverable = json.loads((tmp_path / "unrecoverable_observations.json").read_text())
    assert calls == [sources[missing]["url"]]
    assert report["accepted_direct_growth_package_count"] == 2
    assert report["accepted_fact_count"] == 8
    assert report["resolved_audit_observation_count"] == 10
    assert report["unrecoverable_observation_count"] == 0
    assert report["source_operand_verification_count"] == 30
    assert report["excluded_usd_translation_value_count"] == 14
    assert report["source_text_verification_count"] == 19
    assert len(facts) == 8
    assert rejected["excluded_usd_translation_value_count"] == 14
    assert unrecoverable == []


def test_build_rejects_operand_drift(tmp_path, monkeypatch) -> None:
    source_id = "6k_q2_2020"
    original = list(SOURCE_ROW_CHECKS[source_id][0]["expected_row_values"])
    original[4] -= 1
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={(source_id, "revenue"): tuple(original)},
    )
    with pytest.raises(RuntimeError, match="source row changed"):
        build(tmp_path)


def test_build_rejects_sha_drift(tmp_path, monkeypatch) -> None:
    sources, _ = _install_source_fixtures(tmp_path, monkeypatch)
    source_id = "20f_fy2019"
    path = tmp_path / sources[source_id]["local_path"]
    path.write_bytes(path.read_bytes() + b"drift")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        build(tmp_path)
