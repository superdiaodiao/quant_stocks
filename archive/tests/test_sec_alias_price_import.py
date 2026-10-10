import json

import pandas as pd

from scripts import sec_alias_price_import as importer


def _probe_row():
    return {
        "ticker": "OLD",
        "status": "ok",
        "search_url": "https://sec.example/search",
        "search_payload_sha256": "sec-search-sha",
        "matches": [{"cik": "0000000123", "accession": "accession"}],
        "issuers": [{
            "cik": "0000000123",
            "current_tickers": ["NEW"],
            "submission_url": "https://sec.example/submissions",
            "submission_payload_sha256": "sec-submission-sha",
        }],
    }


def test_candidates_require_one_cik_and_one_replacement_ticker():
    same = _probe_row()
    same["ticker"] = "NEW"
    ambiguous = _probe_row()
    ambiguous["issuers"][0]["current_tickers"] = ["NEW", "OTHER"]

    candidates = importer._candidates({"results": [_probe_row(), same, ambiguous]})

    assert [(row["historical_ticker"], row["successor_ticker"], row["cik"])
            for row in candidates] == [("OLD", "NEW", "0000000123")]

    expanded = importer._candidates(
        {"results": [ambiguous]}, allow_multiple_successors=True
    )
    assert [row["successor_ticker"] for row in expanded] == ["NEW", "OTHER"]
    assert {row["successor_candidate_count"] for row in expanded} == {2}


def test_import_aliases_appends_only_validated_tail_through_membership_end(
    tmp_path, monkeypatch
):
    dates = pd.bdate_range("2025-01-02", periods=35)
    source = pd.DataFrame({
        "date": dates,
        "ticker": "NEW",
        "open": range(100, 135),
        "high": range(101, 136),
        "low": range(99, 134),
        "close": range(100, 135),
        "volume": range(1000, 1035),
    })
    local = source.iloc[:30].copy()
    local["ticker"] = "OLD"
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    local.to_csv(price_dir / "old.csv", index=False)
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(json.dumps({"results": [_probe_row()]}), encoding="utf-8")
    membership_end = dates[32].strftime("%Y-%m-%d")
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({
        "missing_price_while_listed_histories": [{
            "ticker": "OLD", "last_membership_date": membership_end
        }]
    }), encoding="utf-8")
    monkeypatch.setattr(
        importer, "_load_or_fetch",
        lambda *_args, **_kwargs: (b"payload", tmp_path / "cached.json.gz"),
    )
    monkeypatch.setattr(
        importer, "_parse_yahoo",
        lambda _payload, _ticker: (
            source,
            {"instrument_type": "EQUITY", "symbol": "NEW"},
        ),
    )

    report = importer.import_aliases(
        probe_path=probe_path,
        audit_path=audit_path,
        price_dir=price_dir,
        cache_dir=tmp_path / "cache",
        output=tmp_path / "report.json",
        end="2025-12-31",
        apply=True,
    )

    record = report["records"][0]
    persisted = pd.read_csv(price_dir / "old.csv")
    assert record["status"] == "UPDATED"
    assert record["rows_missing"] == 3
    assert len(persisted) == 33
    assert persisted.iloc[-1]["date"] == membership_end
    assert set(persisted["ticker"]) == {"OLD"}


def test_import_aliases_terminal_tail_uses_analysis_end(tmp_path, monkeypatch):
    dates = pd.bdate_range("2025-01-02", periods=35)
    source = pd.DataFrame({
        "date": dates,
        "ticker": "NEW",
        "open": range(100, 135),
        "high": range(101, 136),
        "low": range(99, 134),
        "close": range(100, 135),
        "volume": range(1000, 1035),
    })
    local = source.iloc[:30].copy()
    local["ticker"] = "OLD"
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    local.to_csv(price_dir / "old.csv", index=False)
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(json.dumps({"results": [_probe_row()]}), encoding="utf-8")
    analysis_end = dates[-1].strftime("%Y-%m-%d")
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({
        "unresolved_terminal_return_histories": [{
            "ticker": "OLD",
            "last_price_date": dates[29].strftime("%Y-%m-%d"),
            "last_membership_date": dates[20].strftime("%Y-%m-%d"),
        }]
    }), encoding="utf-8")
    monkeypatch.setattr(
        importer, "_load_or_fetch",
        lambda *_args, **_kwargs: (b"payload", tmp_path / "cached.json.gz"),
    )
    monkeypatch.setattr(
        importer, "_parse_yahoo",
        lambda _payload, _ticker: (
            source,
            {"instrument_type": "EQUITY", "symbol": "NEW"},
        ),
    )

    report = importer.import_aliases(
        probe_path=probe_path,
        audit_path=audit_path,
        price_dir=price_dir,
        cache_dir=tmp_path / "cache",
        output=tmp_path / "report.json",
        end=analysis_end,
        apply=True,
        terminal_tail=True,
    )

    record = report["records"][0]
    persisted = pd.read_csv(price_dir / "old.csv")
    assert report["terminal_tail"] is True
    assert record["status"] == "UPDATED"
    assert record["rows_missing"] == 5
    assert record["tail_end_date"] == analysis_end
    assert persisted.iloc[-1]["date"] == analysis_end
