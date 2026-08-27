from copy import deepcopy
import hashlib

import pandas as pd
import pytest

from scripts import research_v14_rlmd_transition_ttm_loss as rlmd
from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def _xml(facts: list[tuple[str, str, int]]) -> bytes:
    contexts = []
    values = []
    for index, (start, end, value) in enumerate(facts):
        context_id = f"c{index}"
        contexts.append(
            f'<xbrli:context id="{context_id}"><xbrli:entity>'
            '<xbrli:identifier scheme="test">1553643</xbrli:identifier>'
            "</xbrli:entity><xbrli:period>"
            f"<xbrli:startDate>{start}</xbrli:startDate>"
            f"<xbrli:endDate>{end}</xbrli:endDate>"
            "</xbrli:period></xbrli:context>"
        )
        values.append(
            f'<us-gaap:NetIncomeLoss contextRef="{context_id}" '
            f'unitRef="USD" decimals="0">{value}</us-gaap:NetIncomeLoss>'
        )
    return (
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:us-gaap="http://fasb.org/us-gaap/2020-01-31">'
        + "".join(contexts)
        + '<xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure>'
        "</xbrli:unit>"
        + "".join(values)
        + "</xbrli:xbrl>"
    ).encode()


def _payloads() -> list[bytes]:
    return [
        _xml([("2018-07-01", "2019-06-30", -17_318_060)]),
        _xml([
            ("2018-07-01", "2019-06-30", -17_318_060),
            ("2018-07-01", "2018-12-31", -10_509_403),
            ("2019-07-01", "2019-12-31", -8_196_542),
        ]),
        _xml([
            ("2019-01-01", "2019-03-31", -2_686_065),
            ("2020-01-01", "2020-03-31", -10_673_316),
        ]),
    ]


def _patched_sources(payloads: list[bytes]) -> tuple[dict, ...]:
    return tuple(
        {**deepcopy(source), "expected_sha256": hashlib.sha256(raw).hexdigest()}
        for source, raw in zip(rlmd.SOURCES, payloads, strict=True)
    )


def test_official_sources_and_pit_dates_are_locked() -> None:
    assert [source["accession"] for source in rlmd.SOURCES] == [
        "0001213900-19-018787",
        "0001213900-20-007501",
        "0001213900-20-012693",
    ]
    assert [source["filed"] for source in rlmd.SOURCES] == [
        "2019-09-24",
        "2020-03-26",
        "2020-05-15",
    ]
    assert [source["expected_sha256"] for source in rlmd.SOURCES] == [
        "e85d4093b273d7a9d0b91d9ef8dfd48947e7bbb9aa123156da2f1939048ac5e7",
        "b30cdee8d40ad4f40c544f39bc3878622561b2dea9a92db76a4a7abc60398642",
        "ddcda68310513f00584edca3415502dc73ab1f00bd2da609436973a7e12d5633",
    ]
    assert max(source["filed"] for source in rlmd.SOURCES) < min(rlmd.SIGNALS)


def test_transition_bridge_recovers_exact_negative_ttm(monkeypatch) -> None:
    payloads = _payloads()
    monkeypatch.setattr(rlmd, "SOURCES", _patched_sources(payloads))
    evidence = rlmd.verify_sources(payloads)
    assert evidence["operands_usd"] == rlmd.EXPECTED_OPERANDS
    assert evidence["cross_source_fy2019_match"] is True
    assert evidence["calendar_2019_net_income_usd"] == -15_005_199
    assert evidence["net_income_ttm_usd"] == -22_992_450


def test_transition_bridge_rejects_cross_source_fy_drift(monkeypatch) -> None:
    payloads = _payloads()
    payloads[1] = _xml([
        ("2018-07-01", "2019-06-30", -17_318_061),
        ("2018-07-01", "2018-12-31", -10_509_403),
        ("2019-07-01", "2019-12-31", -8_196_542),
    ])
    monkeypatch.setattr(rlmd, "SOURCES", _patched_sources(payloads))
    with pytest.raises(RuntimeError, match="does not match across"):
        rlmd.verify_sources(payloads)


def test_transition_bridge_rejects_source_sha_drift(monkeypatch) -> None:
    monkeypatch.setattr(
        rlmd,
        "SOURCES",
        tuple({**source, "expected_sha256": "0" * 64} for source in rlmd.SOURCES),
    )
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        rlmd.verify_sources(_payloads())


def test_exact_ttm_loss_is_fresh_at_both_missing_signals() -> None:
    facts = pd.DataFrame([{
        "ticker": "RLMD",
        "fiscal_end": pd.Timestamp("2020-03-31"),
        "available_date": pd.Timestamp("2020-05-15"),
        "metric": "net_income_ttm",
        "value": -22_992_450.0,
    }])
    ages = []
    for signal in rlmd.SIGNALS:
        snapshot = quarterly_profit_ttm_snapshot(
            facts, pd.Timestamp(signal), maximum_age_days=150
        )
        assert snapshot.loc["RLMD", "net_income_ttm"] == -22_992_450.0
        ages.append(int(snapshot.loc["RLMD", "financial_age_days"]))
    assert ages == [14, 46]


def test_build_and_candidate_integration_insert_one_identity(
    tmp_path, monkeypatch
) -> None:
    payloads = _payloads()
    sources = _patched_sources(payloads)
    monkeypatch.setattr(rlmd, "SOURCES", sources)
    monkeypatch.setattr(
        rlmd,
        "_download",
        lambda url: payloads[[source["url"] for source in sources].index(url)],
    )
    audit = tmp_path / "audit.json"
    audit.write_text("{}\n", encoding="utf-8")
    audit_sha = hashlib.sha256(audit.read_bytes()).hexdigest()
    supplement = tmp_path / "supplement"
    report = rlmd.build(supplement, audit, audit_sha)
    facts = pd.read_csv(supplement / "strict_quarterly_facts.csv")
    assert report["formal_financials_modified"] is False
    assert report["release_status"] == "BLOCKED"
    assert facts.loc[0, "metric"] == "net_income_ttm"
    assert facts.loc[0, "value"] == -22_992_450.0

    base = tmp_path / "base"
    base.mkdir()
    facts.iloc[0:0].to_csv(base / "quarterly.csv", index=False)
    (base / "annual.csv").write_text("ticker,value\n", encoding="utf-8")
    (base / "manifest.json").write_text("{}\n", encoding="utf-8")
    integrated = integrate_candidate(
        base_dir=base,
        supplement_dir=supplement,
        output_dir=tmp_path / "candidate",
    )
    assert integrated["inserted_identity_rows"] == 1
