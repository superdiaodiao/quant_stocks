from copy import deepcopy
import gzip
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_adus_pit_unrecoverable as adus_audit
from scripts.research_v14_adus_pit_unrecoverable import (
    AUDIT_OBSERVATIONS,
    COMPANYFACTS_CACHE,
    OUTPUT_COLUMNS,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    companyfacts_pit_audit,
    rejected_derivations,
    resolve_audit_observations,
    strict_quarterly_facts,
    validate_source_lock,
    validate_audit_binding,
    validate_unrecoverable_conclusion,
)


def _fixture_source_bytes(
    source_id: str,
    value_overrides: dict[tuple[str, str], tuple[int, ...]] | None = None,
) -> bytes:
    overrides = value_overrides or {}
    paragraphs = "".join(
        f"<p>{fragment}</p>" for fragment in SOURCE_TEXT_CHECKS.get(source_id, ())
    )
    rows = []
    for check in SOURCE_ROW_CHECKS.get(source_id, ()):
        values = overrides.get(
            (source_id, check["metric"]), tuple(check["expected_values"])
        )
        cells = [f"<td>{check['line_item']}</td>", "<td></td>"]
        for value in values:
            cells.extend(("<td>$</td>", f"<td>{value:,}</td>", "<td></td>"))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return ("<html>" + paragraphs + "<table>" + "".join(rows) + "</table></html>").encode()


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
    download_calls = []

    def fake_download(url: str) -> bytes:
        download_calls.append(url)
        return downloads[url]

    monkeypatch.setattr(adus_audit, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(adus_audit, "_download_source", fake_download)
    return sources, download_calls


def _companyfacts_fixture(tmp_path):
    payload = {
        "cik": 1_468_328,
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {
                                "start": "2019-07-01",
                                "end": "2019-09-30",
                                "val": 4_867_000,
                                "accn": "0001564590-19-042077",
                                "form": "10-Q",
                                "filed": "2019-11-08",
                            },
                            {
                                "start": "2019-01-01",
                                "end": "2019-12-31",
                                "val": 25_237_000,
                                "accn": "0001564590-20-038909",
                                "form": "10-K",
                                "filed": "2020-08-10",
                            },
                        ]
                    }
                },
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {
                                "start": "2019-07-01",
                                "end": "2019-09-30",
                                "val": 169_803_000,
                                "accn": "0001564590-19-042077",
                                "form": "10-Q",
                                "filed": "2019-11-08",
                            }
                        ]
                    }
                },
            }
        },
    }
    document = {
        "cik": 1_468_328,
        "fetched_at": "2026-08-23T00:00:00",
        "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0001468328.json",
        "payload": payload,
    }
    path = tmp_path / "companyfacts.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(document, handle)
    return path


def test_three_age150_observations_remain_strictly_unrecoverable() -> None:
    assert AUDIT_OBSERVATIONS == (
        ("liq10000000-age150-growth", "2020-05-29", 150),
        ("liq2000000-age150-growth", "2020-05-29", 150),
        ("liq2000000-age150-growth", "2020-07-31", 150),
    )
    resolved = resolve_audit_observations()
    assert not resolved["resolved"].any()
    assert resolved["financial_age_days"].tolist() == [203, 203, 266]
    assert set(resolved["latest_valid_fiscal_end"]) == {"2019-09-30"}
    assert set(resolved["latest_valid_available_date"]) == {"2019-11-08"}
    assert set(resolved["decision"]) == {
        "unrecoverable_reaudit_comparator_not_available"
    }


def test_no_quarter_growth_or_loss_fact_is_invented() -> None:
    facts = strict_quarterly_facts()
    assert facts.empty
    assert list(facts.columns) == OUTPUT_COLUMNS
    validate_unrecoverable_conclusion()


def test_rejected_derivations_capture_positive_profit_and_basis_break() -> None:
    annual, q1 = rejected_derivations()
    assert annual["current_net_income"] == 25_237_000
    assert annual["current_net_income"] > 0
    assert annual["old_prior_revenue"] == 518_119_000
    assert annual["later_revised_prior_revenue"] == 516_647_000
    assert annual["old_prior_net_income"] == 17_503_000
    assert annual["later_revised_prior_net_income"] == 16_433_000
    assert q1["preliminary_net_income_ttm_using_old_q1"] == 29_033_000
    assert q1["preliminary_q1_revenue_disclosure"] == "$190.2 million rounded"
    assert all(item["rejected"] for item in (annual, q1))


def test_raw_companyfacts_filters_out_august_filings() -> None:
    result = companyfacts_pit_audit(COMPANYFACTS_CACHE)
    assert result["latest_duration_fact_filed"] == "2019-11-08"
    assert result["latest_duration_fact_end"] == "2019-09-30"
    assert result["latest_duration_fact_accessions"] == [
        "0001564590-19-042077"
    ]
    assert result["source_url"].endswith("CIK0001468328.json")


def test_official_source_paths_dates_and_hashes_are_locked() -> None:
    expected = {
        "10k_2019_03_18_fy2018_original": (
            "2019-03-18",
            "0001564590-19-008098",
            "cb6fda1a5eef4ec378473ff8db214fba7d20cd42d99260f4d0c0943366bba08b",
        ),
        "10q_2019_11_08_q3_original": (
            "2019-11-08",
            "0001564590-19-042077",
            "8230a179b847512a3c111c72083ea97bf6deb83a0b464ceac41b42bf6fbd88d4",
        ),
        "8k_2020_03_17_preliminary_fy2019_ex991": (
            "2020-03-17",
            "0001193125-20-076019",
            "3973cb1ccfe9335da144afaac65e09de1721bb47b1b77bf448f6470a8f330ba6",
        ),
        "8k_2020_05_04_preliminary_q1_ex991": (
            "2020-05-04",
            "0001193125-20-132428",
            "7f76d3dbcea4bfb00f6b61532320a37e519d622d3051609f4a58d8bc782db553",
        ),
        "10k_2020_08_10_fy2019_revised_later": (
            "2020-08-10",
            "0001564590-20-038909",
            "8c9f9412d88ab9e6fa45eb6f59f7de89f83feda4d1249fe0c22289bb0529fbdd",
        ),
        "10q_2020_08_10_q1_revised_later": (
            "2020-08-10",
            "0001564590-20-038948",
            "96ea6ee5e123a3355e54c7337dec8e78083b7f3a64e4eca64bcfef34234e163d",
        ),
    }
    assert {
        source_id: (
            source["filed"], source["accession"], source["expected_sha256"]
        )
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected


def test_source_lock_rejects_later_file_misclassified_as_pit() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["10k_2020_08_10_fy2019_revised_later"]["role"] = "pit_source"
    with pytest.raises(ValueError, match="violates PIT cutoff"):
        validate_source_lock(sources)


def test_build_downloads_missing_source_and_verifies_no_facts(
    tmp_path, monkeypatch
) -> None:
    missing = "8k_2020_05_04_preliminary_q1_ex991"
    sources, download_calls = _install_source_fixtures(
        tmp_path, monkeypatch, missing_source=missing
    )
    companyfacts_path = _companyfacts_fixture(tmp_path)
    audit = tmp_path / "priorities.csv"
    pd.DataFrame([
        {
            "scenario": "liq10000000-age150-growth",
            "ticker": "ADUS",
            "missing_signal_count": 1,
            "first_missing_signal_date": "2020-05-29",
            "last_missing_signal_date": "2020-05-29",
            "stale_growth_snapshot_signal_count": 1,
        },
        {
            "scenario": "liq2000000-age150-growth",
            "ticker": "ADUS",
            "missing_signal_count": 2,
            "first_missing_signal_date": "2020-05-29",
            "last_missing_signal_date": "2020-07-31",
            "stale_growth_snapshot_signal_count": 2,
        },
    ]).to_csv(audit, index=False)
    audit_sha = hashlib.sha256(audit.read_bytes()).hexdigest()
    report = build(tmp_path, companyfacts_path, audit, audit_sha)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    first_manifest_sha = hashlib.sha256(
        (tmp_path / "manifest.json").read_bytes()
    ).hexdigest()
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")

    assert download_calls == [sources[missing]["url"]]
    assert report["accepted_strict_fact_count"] == 0
    assert report["resolved_audit_observation_count"] == 0
    assert report["unrecoverable_audit_observation_count"] == 3
    assert report["source_operand_verification_count"] == 29
    assert report["source_text_verification_count"] == 11
    assert facts.empty
    assert "downloaded" not in manifest["sources"][missing]
    assert manifest["sources"][missing]["actual_sha256"] == (
        sources[missing]["expected_sha256"]
    )
    assert report["negative_evidence_source_locked"] is True
    assert report["formal_financials_modified"] is False
    assert report["release_status"] == "BLOCKED"
    assert report["audit_binding"]["missing_observation_count"] == 3

    build(tmp_path, companyfacts_path, audit, audit_sha)
    assert hashlib.sha256((tmp_path / "manifest.json").read_bytes()).hexdigest() == (
        first_manifest_sha
    )
    assert download_calls == [sources[missing]["url"]]


def test_current_audit_binding_covers_exact_three_observations() -> None:
    binding = validate_audit_binding(
        adus_audit.AUDIT_PATH,
        adus_audit.EXPECTED_AUDIT_SHA256,
    )
    assert binding["scenario_count"] == 2
    assert binding["missing_observation_count"] == 3
    assert binding["signals"] == ["2020-05-29", "2020-07-31"]


def test_build_rejects_operand_drift(tmp_path, monkeypatch) -> None:
    source_id = "10q_2020_08_10_q1_revised_later"
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={(source_id, "net_income"): (8_658, 4_295)},
    )
    with pytest.raises(RuntimeError, match="source row changed"):
        build(tmp_path, _companyfacts_fixture(tmp_path))


def test_build_rejects_sha_drift_before_using_source(tmp_path, monkeypatch) -> None:
    sources, _ = _install_source_fixtures(tmp_path, monkeypatch)
    source_id = "10q_2019_11_08_q3_original"
    sources[source_id]["expected_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        build(tmp_path, _companyfacts_fixture(tmp_path))


def test_companyfacts_fixture_enforces_pit_boundary(tmp_path) -> None:
    result = companyfacts_pit_audit(_companyfacts_fixture(tmp_path))
    assert result["qualifying_duration_fact_count"] == 2
    assert result["latest_duration_fact_filed"] == "2019-11-08"
    assert "no 10-K/10-Q" in result["conclusion"]
