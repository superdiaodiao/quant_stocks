from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_momo_pit_unrecoverable as momo_audit
from scripts.research_v14_momo_pit_unrecoverable import (
    AUDIT_OBSERVATIONS,
    OUTPUT_COLUMNS,
    REJECTED_LATER_FILINGS,
    SOURCE_DOCUMENTS,
    SOURCE_ROW_CHECKS,
    SOURCE_TEXT_CHECKS,
    build,
    rejected_derivations,
    resolve_audit_observations,
    strict_quarterly_facts,
    validate_source_lock,
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
    for check in SOURCE_ROW_CHECKS[source_id]:
        values = overrides.get(
            (source_id, check["metric"]), tuple(check["expected_values"])
        )
        cells = [f"<td>{check['line_item']}</td>", "<td></td>"]
        for value in values:
            cells.extend(("<td>RMB</td>", f"<td>{value:,}</td>", "<td></td>"))
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

    monkeypatch.setattr(momo_audit, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(momo_audit, "_download_source", fake_download)
    return sources, download_calls


def test_all_six_observations_remain_strictly_unrecoverable() -> None:
    assert len(AUDIT_OBSERVATIONS) == 6
    assert {row[1] for row in AUDIT_OBSERVATIONS} == {"2019-11-29"}
    assert {row[2] for row in AUDIT_OBSERVATIONS} == {150, 365, 550}
    assert {row[0].split("-")[0] for row in AUDIT_OBSERVATIONS} == {
        "liq2000000",
        "liq10000000",
    }
    resolution = resolve_audit_observations()
    assert not resolution["resolved"].any()
    assert set(resolution["decision"]) == {"unrecoverable_currency_basis_break"}
    assert set(resolution["financial_age_days"]) == {3}


def test_current_and_prior_ttm_are_exact_but_cross_currency() -> None:
    evidence = rejected_derivations()
    current = evidence["current_ttm_rmb_thousands"]
    prior = evidence["prior_ttm_usd_thousands"]
    assert current["revenue_ttm"] == 16_171_108
    assert current["net_income_attributable_ttm"] == 2_575_828
    assert current["currency"] == "RMB"
    assert current["available_date"] == "2019-11-26"
    assert prior["revenue_ttm"] == 1_851_725
    assert prior["net_income_attributable_ttm"] == 430_326
    assert prior["currency"] == "USD"
    assert evidence["rejection"]["positive_current_profit"] is True
    assert evidence["rejection"]["negative_ttm_exclusion_available"] is False


def test_no_quarter_growth_or_negative_loss_fact_is_invented() -> None:
    facts = strict_quarterly_facts()
    assert facts.empty
    assert list(facts.columns) == OUTPUT_COLUMNS
    validate_unrecoverable_conclusion()


def test_official_source_paths_dates_currencies_and_hashes_are_locked() -> None:
    expected = {
        "20f_2018_04_26_fy2017_usd": (
            "2018-04-26",
            "0001193125-18-133102",
            "USD",
            "def42a4533bbb49860346ac25f28000aed598a40bf183e0b9162a43ac1dcbcb5",
        ),
        "6k_2018_12_06_q3_usd_ex991": (
            "2018-12-06",
            "0001193125-18-343009",
            "USD",
            "427ba04fc9bfd0abc47d41c0fa4da822918fa0023aaa467c53715e3d6e0ac122",
        ),
        "20f_2019_04_26_fy2018_rmb_recast": (
            "2019-04-26",
            "0001193125-19-120962",
            "RMB",
            "8901bc6fe4779c9d98fc7b88d7567fcb1b7d39440e3a02669b3e75c85d274b59",
        ),
        "6k_2019_11_26_q3_rmb_ex991": (
            "2019-11-26",
            "0001193125-19-300514",
            "RMB",
            "426a38c0153aef97270e89e8da85e3cecc3333a383d002151affb2be3fc6274e",
        ),
    }
    assert {
        source_id: (
            source["filed"],
            source["accession"],
            source["currency"],
            source["expected_sha256"],
        )
        for source_id, source in SOURCE_DOCUMENTS.items()
    } == expected
    validate_source_lock()


def test_source_lock_rejects_post_signal_operand_source() -> None:
    sources = deepcopy(SOURCE_DOCUMENTS)
    sources["6k_2019_11_26_q3_rmb_ex991"]["filed"] = "2019-12-01"
    with pytest.raises(ValueError, match="violates PIT cutoff"):
        validate_source_lock(sources)


def test_later_results_are_explicitly_rejected() -> None:
    assert REJECTED_LATER_FILINGS == {
        "0001193125-20-079851": {
            "form": "6-K",
            "filed": "2020-03-20",
            "reason": "Q4/FY2019 results were filed after the 2019-11-29 signal date",
        },
        "0001193125-20-123373": {
            "form": "20-F",
            "filed": "2020-04-28",
            "reason": "FY2019 annual report was filed after the signal date",
        },
    }


def test_build_downloads_missing_source_and_emits_no_facts(
    tmp_path, monkeypatch
) -> None:
    missing = "6k_2019_11_26_q3_rmb_ex991"
    sources, download_calls = _install_source_fixtures(
        tmp_path, monkeypatch, missing_source=missing
    )
    report = build(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")

    assert download_calls == [sources[missing]["url"]]
    assert report["accepted_strict_fact_count"] == 0
    assert report["resolved_audit_observation_count"] == 0
    assert report["unrecoverable_audit_observation_count"] == 6
    assert report["source_operand_verification_count"] == 32
    assert report["source_text_verification_count"] == 8
    assert facts.empty
    assert manifest["sources"][missing]["downloaded"] is True
    assert manifest["sources"][missing]["actual_sha256"] == (
        sources[missing]["expected_sha256"]
    )


def test_build_rejects_operand_drift(tmp_path, monkeypatch) -> None:
    source_id = "6k_2019_11_26_q3_rmb_ex991"
    _install_source_fixtures(
        tmp_path,
        monkeypatch,
        value_overrides={
            (source_id, "net_income_attributable"): (
                579_539,
                893_897,
                125_062,
                2_154_938,
                1_914_990,
                267_916,
            )
        },
    )
    with pytest.raises(RuntimeError, match="source row changed"):
        build(tmp_path)


def test_build_rejects_sha_drift_before_using_source(tmp_path, monkeypatch) -> None:
    sources, _ = _install_source_fixtures(tmp_path, monkeypatch)
    source_id = "20f_2019_04_26_fy2018_rmb_recast"
    path = tmp_path / sources[source_id]["local_path"]
    path.write_bytes(path.read_bytes() + b"drift")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        build(tmp_path)
