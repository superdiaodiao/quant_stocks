import gzip
import hashlib
import json

import pandas as pd
import pytest

from scripts import research_v14_knsa_direct_ttm_loss as knsa
from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot


def _wrapper() -> dict:
    facts = []
    for expected in knsa.OPERANDS.values():
        facts.append({
            **{key: expected[key] for key in ("start", "end", "filed", "form", "accn")},
            "val": expected["value"],
        })
    return {
        "cik": knsa.CIK, "symbols": [knsa.TICKER],
        "source_url": "fixture", "fetched_at": "2026-08-24",
        "payload": {
            "cik": knsa.CIK, "entityName": "Kiniksa Pharmaceuticals International",
            "facts": {"us-gaap": {knsa.CONCEPT: {"units": {"USD": facts}}}},
        },
    }


def _write(path, wrapper, monkeypatch) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(wrapper, handle)
    monkeypatch.setattr(knsa, "RAW_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())


def test_real_cache_has_exact_original_and_comparative_operands() -> None:
    wrapper, verified = knsa.load_and_validate()
    assert wrapper["symbols"] == ["KNSA"]
    assert len(verified) == 4
    assert knsa.TTM_NET_INCOME == -172_650_000
    assert verified[1]["value"] == verified[2]["value"] == -60_647_000


def test_direct_loss_is_visible_before_the_real_signal() -> None:
    facts = knsa.strict_quarterly_facts()
    facts["fiscal_end"] = pd.to_datetime(facts["fiscal_end"])
    facts["available_date"] = pd.to_datetime(facts["available_date"])
    snapshot = quarterly_profit_ttm_snapshot(
        facts, pd.Timestamp(knsa.SIGNAL), maximum_age_days=150
    )
    assert snapshot.loc["KNSA", "net_income_ttm"] == -172_650_000
    assert snapshot.loc["KNSA", "financial_age_days"] == 115


def test_cache_sha_and_operand_changes_fail_closed(tmp_path, monkeypatch) -> None:
    path = tmp_path / "knsa.json.gz"
    _write(path, _wrapper(), monkeypatch)
    monkeypatch.setattr(knsa, "RAW_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        knsa.load_and_validate(path)

    changed = _wrapper()
    changed["payload"]["facts"]["us-gaap"][knsa.CONCEPT]["units"]["USD"][0][
        "val"
    ] += 1
    _write(path, changed, monkeypatch)
    with pytest.raises(ValueError, match="operand is not unique"):
        knsa.load_and_validate(path)


def test_fixture_build_integrates_without_formal_mutation(tmp_path, monkeypatch) -> None:
    raw = tmp_path / "knsa.json.gz"
    _write(raw, _wrapper(), monkeypatch)
    audit = tmp_path / "audit.json"
    audit.write_text("{}\n")
    supplement = tmp_path / "supplement"
    report = knsa.build(supplement, raw, audit)
    assert report["formal_financials_modified"] is False
    assert report["audit_binding"]["missing_observation_count"] == 1
    assert report["rejected_later_filings"]["0001558370-20-002081"]["filed"] > knsa.SIGNAL

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
