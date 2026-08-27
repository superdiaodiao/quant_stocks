from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

import scripts.research_v14_sy_exact_ttm_growth as sy
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


def _fixture_source_bytes(source_id: str) -> bytes:
    paragraphs = "".join(
        f"<p>{fragment}</p>"
        for fragment in sy.SOURCE_TEXT_CHECKS[source_id]
    )
    rows = []
    for check in sy.SOURCE_ROW_CHECKS[source_id]:
        cells = [f"<td>{check['line_item']}</td>"]
        for value in check["expected_values"]:
            cell = f"({abs(value):,})" if value < 0 else f"{value:,}"
            cells.append(f"<td>{cell}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<html>" + paragraphs + "<table>" + "".join(rows) + "</table></html>"
    ).encode()


def _fixture_sources() -> dict[str, bytes]:
    return {
        source_id: _fixture_source_bytes(source_id)
        for source_id in sy.SOURCE_DOCUMENTS
    }


def _install_source_fixtures(tmp_path, monkeypatch):
    raw_by_source = _fixture_sources()
    sources = deepcopy(sy.SOURCE_DOCUMENTS)
    downloads = {}
    for source_id, source in sources.items():
        raw = raw_by_source[source_id]
        source["expected_sha256"] = hashlib.sha256(raw).hexdigest()
        downloads[source["url"]] = raw
    download_calls = []

    def fake_download(url: str) -> bytes:
        download_calls.append(url)
        return downloads[url]

    monkeypatch.setattr(sy, "SOURCE_DOCUMENTS", sources)
    monkeypatch.setattr(sy, "_download_source", fake_download)
    audit_path = tmp_path / "audit.json"
    audit_path.write_text("{}\n", encoding="utf-8")
    return raw_by_source, sources, download_calls, audit_path


def _facts_and_evidence() -> tuple[pd.DataFrame, dict]:
    facts, evidence = sy.strict_quarterly_facts(_fixture_sources())
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    return facts, evidence


def test_source_lock_binds_five_original_sec_documents() -> None:
    sy.validate_source_lock()
    assert {
        source_id: (
            source["filed"],
            source["accession"],
            source["document"],
            source["expected_sha256"],
        )
        for source_id, source in sy.SOURCE_DOCUMENTS.items()
    } == {
        "fy2019_20f": (
            "2020-04-27",
            "0001104659-20-051203",
            "a20-1178_120f.htm",
            "5b765322954ca2033035be4cfe04d23e063819702db194ae41585c5defef8108",
        ),
        "q1_2019_6k": (
            "2019-05-30",
            "0001104659-19-032746",
            "a19-10672_1ex99d1.htm",
            "11cf87f74994ff4b08c6e70e9a320f4d8463fe4463146a6496be344f3b78023b",
        ),
        "q1_2020_6k": (
            "2020-05-18",
            "0001104659-20-062991",
            "a20-20059_1ex99d1.htm",
            "6c537806c5814f4d5303680117c814dc0742a06772e664f0af3f48ad4460e75a",
        ),
        "q3_2019_6k": (
            "2019-12-05",
            "0001104659-19-070083",
            "a19-24570_1ex99d1.htm",
            "266a04498440eed85ed06a407eadb4a9018513580acd1176cb8ad1ed3f4408d5",
        ),
        "q3_2020_6k": (
            "2020-11-25",
            "0001104659-20-129215",
            "a20-37117_1ex99d1.htm",
            "d60bfbc6fe94704ad86799e0d888b7e07357c99f2e9a0fef2c7dc8f00d6e3563",
        ),
    }


def test_exact_ttm_arithmetic_uses_reported_rmb_gaap_operands() -> None:
    facts, evidence = _facts_and_evidence()
    q1 = evidence["derived_bundles"]["q1_2020"]
    m9 = evidence["derived_bundles"]["m9_2020"]

    assert q1["revenue"]["prior_ttm_rmb_thousands"] == 640_266
    assert q1["revenue"]["current_ttm_rmb_thousands"] == 1_128_138
    assert q1["net_income"]["prior_ttm_rmb_thousands"] == 60_175
    assert q1["net_income"]["current_ttm_rmb_thousands"] == 94_936
    assert q1["revenue"]["growth"] == pytest.approx(0.7619833006906505)
    assert q1["net_income"]["growth"] == pytest.approx(0.5776651433319485)

    assert m9["revenue"]["prior_ttm_rmb_thousands"] == 976_476
    assert m9["revenue"]["current_ttm_rmb_thousands"] == 1_228_527
    assert m9["net_income"]["prior_ttm_rmb_thousands"] == 147_592
    assert m9["net_income"]["current_ttm_rmb_thousands"] == 37_105
    assert m9["revenue"]["growth"] == pytest.approx(0.2581230875105993)
    assert m9["net_income"]["growth"] == pytest.approx(-0.7485974849585343)
    assert len(facts) == 8
    assert set(facts["taxonomy"]) == {"us-gaap"}
    assert facts["concept"].str.endswith(":RMB").all()


def test_facts_resolve_both_signal_dates_without_crossing_pit() -> None:
    facts, _ = _facts_and_evidence()
    before = quarterly_growth_snapshot(
        facts, pd.Timestamp("2020-04-30"), maximum_age_days=550
    )
    assert "SY" not in before.index

    q1 = quarterly_growth_snapshot(
        facts, pd.Timestamp("2020-07-31"), maximum_age_days=150
    )
    assert q1.loc["SY", "revenue_growth"] > 0.10
    assert q1.loc["SY", "net_income_growth"] > 0.25
    assert q1.loc["SY", "net_income_ttm"] == 94_936_000.0

    m9 = quarterly_growth_snapshot(
        facts, pd.Timestamp("2021-02-26"), maximum_age_days=150
    )
    assert m9.loc["SY", "revenue_growth"] > 0.10
    assert m9.loc["SY", "net_income_growth"] < 0.25
    assert m9.loc["SY", "net_income_ttm"] == 37_105_000.0


def test_six_audit_observations_are_financially_resolved() -> None:
    _, evidence = _facts_and_evidence()
    resolutions = sy.resolved_audit_observations(evidence)
    assert len(resolutions) == 6
    assert {row["signal_date"] for row in resolutions} == {
        "2020-07-31",
        "2021-02-26",
    }
    assert all(row["resolved"] for row in resolutions)
    assert {
        row["decision"] for row in resolutions if row["signal_date"] == "2020-07-31"
    } == {"pass_growth_filters"}
    assert {
        row["decision"] for row in resolutions if row["signal_date"] == "2021-02-26"
    } == {"fail_growth_filters"}


def test_build_downloads_and_binds_all_sources(tmp_path, monkeypatch) -> None:
    _, sources, download_calls, audit_path = _install_source_fixtures(
        tmp_path, monkeypatch
    )
    report = sy.build(tmp_path / "out", audit_path)
    facts = pd.read_csv(tmp_path / "out" / "strict_quarterly_facts.csv")
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())

    assert download_calls == [source["url"] for source in sources.values()]
    assert report["accepted_direct_growth_package_count"] == 2
    assert report["accepted_fact_count"] == 8
    assert report["resolved_audit_observation_count"] == 6
    assert report["formal_financials_modified"] is False
    assert report["release_status"] == "BLOCKED"
    assert set(facts["metric"]) == {
        "revenue_ttm",
        "revenue_growth",
        "net_income_ttm",
        "net_income_growth",
    }
    assert manifest["audit_binding"]["missing_observation_count"] == 6


def test_build_rejects_source_sha_drift(tmp_path, monkeypatch) -> None:
    _, sources, _, audit_path = _install_source_fixtures(tmp_path, monkeypatch)
    sources["q3_2020_6k"]["expected_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="source SHA-256 mismatch"):
        sy.build(tmp_path / "out", audit_path)
