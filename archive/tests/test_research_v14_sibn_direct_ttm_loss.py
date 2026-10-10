import gzip
import hashlib
import json

import pandas as pd
import pytest

from scripts import research_v14_sibn_direct_ttm_loss as sibn
from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def _wrapper() -> dict:
    facts = []
    for expected in sibn.OPERANDS.values():
        facts.append({
            key: expected[key]
            for key in ("start", "end", "val", "filed", "form", "accn")
            if key in expected
        })
        facts[-1]["val"] = expected["value"]
    return {
        "cik": sibn.CIK,
        "symbols": [sibn.TICKER],
        "source_url": "fixture",
        "fetched_at": "2026-08-24",
        "payload": {
            "cik": sibn.CIK,
            "entityName": "SI-BONE, INC.",
            "facts": {"us-gaap": {sibn.CONCEPT: {"units": {"USD": facts}}}},
        },
    }


def _write_fixture(path, monkeypatch):
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(_wrapper(), handle)
    monkeypatch.setattr(sibn, "RAW_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())


def test_real_sha_locked_cache_has_four_exact_operands() -> None:
    wrapper, verified = sibn.load_and_validate()
    assert wrapper["symbols"] == ["SIBN"]
    assert len(verified) == 4
    assert sibn.TTM_NET_INCOME == -34_618_000
    assert verified[1]["value"] == verified[2]["value"] == -12_140_000


def test_direct_ttm_loss_is_consumed_as_known_nonpositive() -> None:
    facts = sibn.strict_quarterly_facts()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    assert len(facts) == 1
    assert facts.loc[0, "metric"] == "net_income_ttm"
    for signal in sibn.SIGNALS:
        snapshot = quarterly_profit_ttm_snapshot(
            facts, pd.Timestamp(signal), maximum_age_days=150
        )
        assert snapshot.loc["SIBN", "net_income_ttm"] == -34_618_000
        assert 0 <= snapshot.loc["SIBN", "financial_age_days"] <= 150


def test_changed_cache_sha_is_rejected(tmp_path, monkeypatch) -> None:
    path = tmp_path / "sibn.json.gz"
    _write_fixture(path, monkeypatch)
    monkeypatch.setattr(sibn, "RAW_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        sibn.load_and_validate(path)


def test_operand_change_is_rejected_even_with_matching_archive_sha(
    tmp_path, monkeypatch
) -> None:
    wrapper = _wrapper()
    wrapper["payload"]["facts"]["us-gaap"][sibn.CONCEPT]["units"]["USD"][0][
        "val"
    ] += 1
    path = tmp_path / "sibn.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(wrapper, handle)
    monkeypatch.setattr(sibn, "RAW_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match="operand is not unique"):
        sibn.load_and_validate(path)


def test_fixture_build_and_generic_integration(tmp_path, monkeypatch) -> None:
    raw = tmp_path / "sibn.json.gz"
    _write_fixture(raw, monkeypatch)
    audit = tmp_path / "audit.json"
    audit.write_text("{}\n")
    supplement = tmp_path / "supplement"
    report = sibn.build(supplement, raw, audit)
    assert report["formal_financials_modified"] is False
    assert report["audit_binding"]["missing_observation_count"] == 2
    assert report["rejected_later_filings"]["0001459839-20-000031"]["filed"] > max(
        sibn.SIGNALS
    )

    facts = pd.read_csv(supplement / "strict_quarterly_facts.csv")
    base = tmp_path / "base"
    base.mkdir()
    facts.iloc[0:0].to_csv(base / "quarterly.csv", index=False)
    (base / "annual.csv").write_text("ticker,value\n")
    (base / "manifest.json").write_text("{}\n")
    integrated = integrate_candidate(
        base_dir=base, supplement_dir=supplement, output_dir=tmp_path / "candidate"
    )
    assert integrated["inserted_identity_rows"] == 1
