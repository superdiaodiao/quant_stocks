import json

import pandas as pd

from scripts import sec_otc_alias_price_import as importer


def test_otc_alias_import_requires_overlap_and_caps_membership(tmp_path, monkeypatch):
    dates = pd.bdate_range("2025-01-02", periods=35)
    source = pd.DataFrame({
        "date": dates,
        "ticker": "NEWF",
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
    stooq_report = tmp_path / "stooq.json"
    stooq_report.write_text(json.dumps({"records": [{
        "historical_ticker": "OLD",
        "successor_ticker": "NEWF",
        "cik": "0000000123",
        "status": "SOURCE_MISSING",
    }]}), encoding="utf-8")
    membership_end = dates[32].strftime("%Y-%m-%d")
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({
        "missing_price_while_listed_histories": [{
            "ticker": "OLD", "last_membership_date": membership_end
        }]
    }), encoding="utf-8")
    monkeypatch.setattr(
        importer, "_load_or_fetch",
        lambda *_args, **_kwargs: (b"payload", str(tmp_path / "cache.json.gz")),
    )
    monkeypatch.setattr(
        importer, "_parse_edgar", lambda _payload, _ticker: (source, "Old Corp")
    )

    report = importer.import_aliases(
        stooq_report_path=stooq_report,
        audit_path=audit,
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


def test_otc_terminal_tail_accepts_unique_cik_contiguous_rename(tmp_path, monkeypatch):
    local = pd.DataFrame({
        "date": pd.to_datetime(["2025-01-02", "2025-01-03"]),
        "ticker": "OLD", "open": 10, "high": 11, "low": 9,
        "close": 10, "volume": 100,
    })
    source = pd.DataFrame({
        "date": pd.to_datetime(["2025-01-06", "2025-01-07"]),
        "ticker": "OLDF", "open": 10, "high": 11, "low": 9,
        "close": 10, "volume": 100,
    })
    price_dir = tmp_path / "prices"; price_dir.mkdir()
    local.to_csv(price_dir / "old.csv", index=False)
    stooq_report = tmp_path / "stooq.json"
    stooq_report.write_text(json.dumps({"records": [{
        "historical_ticker": "OLD", "successor_ticker": "OLDF",
        "cik": "0000000123", "status": "SOURCE_MISSING",
        "sec_issuers": [{"cik": "0000000123"}],
    }]}))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"unresolved_terminal_return_histories": [{
        "ticker": "OLD", "last_price_date": "2025-01-03"
    }]}))
    monkeypatch.setattr(
        importer, "_load_or_fetch",
        lambda *_args, **_kwargs: (b"payload", str(tmp_path / "cache.json.gz")),
    )
    monkeypatch.setattr(
        importer, "_parse_edgar", lambda _payload, _ticker: (source, "Old Corp")
    )
    report = importer.import_aliases(
        stooq_report_path=stooq_report, audit_path=audit,
        price_dir=price_dir, cache_dir=tmp_path / "cache",
        output=tmp_path / "report.json", end="2025-12-31",
        terminal_tail=True, apply=True,
    )
    assert report["records"][0]["status"] == "UPDATED"
    assert report["records"][0]["rows_missing"] == 2


def test_otc_terminal_tail_accepts_recent_stable_split_scale(tmp_path, monkeypatch):
    dates = pd.bdate_range("2025-01-02", periods=55)
    source = pd.DataFrame({
        "date": dates,
        "ticker": "OLDF",
        "open": range(100, 155),
        "high": range(101, 156),
        "low": range(99, 154),
        "close": range(100, 155),
        "volume": range(1000, 1055),
    })
    local = source.iloc[:50].copy()
    local["ticker"] = "OLD"
    for field in ("open", "high", "low", "close"):
        local[field] = local[field].astype(float)
        local.loc[local.index >= 20, field] *= 0.5
    local.loc[local.index >= 20, "volume"] *= 2
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    local.to_csv(price_dir / "old.csv", index=False)
    stooq_report = tmp_path / "stooq.json"
    stooq_report.write_text(json.dumps({"records": [{
        "historical_ticker": "OLD",
        "successor_ticker": "OLDF",
        "cik": "0000000123",
        "status": "SOURCE_MISSING",
        "sec_issuers": [{"cik": "0000000123"}],
    }]}))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"unresolved_terminal_return_histories": [{
        "ticker": "OLD",
        "last_price_date": dates[49].strftime("%Y-%m-%d"),
    }]}))
    monkeypatch.setattr(
        importer,
        "_load_or_fetch",
        lambda *_args, **_kwargs: (b"payload", str(tmp_path / "cache.json.gz")),
    )
    monkeypatch.setattr(
        importer, "_parse_edgar", lambda _payload, _ticker: (source, "Old Corp")
    )
    expected_last_close = float(source.iloc[-1]["close"]) * 0.5

    report = importer.import_aliases(
        stooq_report_path=stooq_report,
        audit_path=audit,
        price_dir=price_dir,
        cache_dir=tmp_path / "cache",
        output=tmp_path / "report.json",
        end=dates[-1].strftime("%Y-%m-%d"),
        terminal_tail=True,
        apply=True,
    )

    record = report["records"][0]
    persisted = pd.read_csv(price_dir / "old.csv")
    assert record["status"] == "UPDATED"
    assert record["rows_missing"] == 5
    assert record["cross_validation"]["validation_scope"] == (
        "recent_stable_overlap_tail"
    )
    assert record["cross_validation"]["sessions"] == 30
    assert float(persisted.iloc[-1]["close"]) == expected_last_close


def test_otc_terminal_tail_accepts_10_session_exact_tail_with_unique_sec_cik(
    tmp_path, monkeypatch
):
    dates = pd.bdate_range("2025-01-02", periods=35)
    source = pd.DataFrame({
        "date": dates,
        "ticker": "OLDF",
        "open": range(100, 135),
        "high": range(101, 136),
        "low": range(99, 134),
        "close": range(100, 135),
        "volume": range(1000, 1035),
    })
    local = source.iloc[:30].copy()
    local["ticker"] = "OLD"
    for field in ("open", "high", "low", "close"):
        local[field] = local[field].astype(float)
        local.loc[local.index < 18, field] *= 0.5
    local.loc[local.index < 18, "volume"] *= 2
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    local.to_csv(price_dir / "old.csv", index=False)
    stooq_report = tmp_path / "stooq.json"
    stooq_report.write_text(json.dumps({"records": [{
        "historical_ticker": "OLD",
        "successor_ticker": "OLDF",
        "cik": "0000000123",
        "status": "SOURCE_MISSING",
        "sec_matches": [{"cik": "0000000123"}],
        "sec_issuers": [{"cik": "0000000123"}],
    }]}))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"unresolved_terminal_return_histories": [{
        "ticker": "OLD",
        "last_price_date": dates[29].strftime("%Y-%m-%d"),
    }]}))
    monkeypatch.setattr(
        importer,
        "_load_or_fetch",
        lambda *_args, **_kwargs: (b"payload", str(tmp_path / "cache.json.gz")),
    )
    monkeypatch.setattr(
        importer, "_parse_edgar", lambda _payload, _ticker: (source, "Old Corp")
    )

    report = importer.import_aliases(
        stooq_report_path=stooq_report,
        audit_path=audit,
        price_dir=price_dir,
        cache_dir=tmp_path / "cache",
        output=tmp_path / "report.json",
        end=dates[-1].strftime("%Y-%m-%d"),
        terminal_tail=True,
        apply=True,
    )

    record = report["records"][0]
    assert record["status"] == "UPDATED"
    assert record["rows_missing"] == 5
    assert record["cross_validation"]["validation_scope"] == (
        "exact_recent_tail_plus_sec_unique_cik"
    )
    assert record["cross_validation"]["sessions"] == 12


def test_otc_terminal_tail_rejects_9_session_tail_without_cross_source(
    tmp_path, monkeypatch
):
    dates = pd.bdate_range("2025-01-02", periods=35)
    source = pd.DataFrame({
        "date": dates,
        "ticker": "OLDF",
        "open": range(100, 135),
        "high": range(101, 136),
        "low": range(99, 134),
        "close": range(100, 135),
        "volume": range(1000, 1035),
    })
    local = source.iloc[:30].copy()
    local["ticker"] = "OLD"
    for field in ("open", "high", "low", "close"):
        local[field] = local[field].astype(float)
        local.loc[local.index < 21, field] *= 0.5
    local.loc[local.index < 21, "volume"] *= 2
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    local.to_csv(price_dir / "old.csv", index=False)
    stooq_report = tmp_path / "stooq.json"
    stooq_report.write_text(json.dumps({"records": [{
        "historical_ticker": "OLD",
        "successor_ticker": "OLDF",
        "cik": "0000000123",
        "status": "SOURCE_MISSING",
        "sec_matches": [{"cik": "0000000123"}],
        "sec_issuers": [{"cik": "0000000123"}],
    }]}))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"unresolved_terminal_return_histories": [{
        "ticker": "OLD",
        "last_price_date": dates[29].strftime("%Y-%m-%d"),
    }]}))
    monkeypatch.setattr(
        importer,
        "_load_or_fetch",
        lambda *_args, **_kwargs: (b"payload", str(tmp_path / "cache.json.gz")),
    )
    monkeypatch.setattr(
        importer, "_parse_edgar", lambda _payload, _ticker: (source, "Old Corp")
    )

    report = importer.import_aliases(
        stooq_report_path=stooq_report,
        audit_path=audit,
        price_dir=price_dir,
        cache_dir=tmp_path / "cache",
        output=tmp_path / "report.json",
        end=dates[-1].strftime("%Y-%m-%d"),
        terminal_tail=True,
        apply=False,
    )

    record = report["records"][0]
    assert record["status"] == "REJECT_CROSS_VALIDATION"
    assert record["sec_exact_tail_validation"]["reason"] == (
        "fewer_than_10_exact_tail_sessions"
    )

    monkeypatch.setattr(
        importer,
        "_fixed_mirror_sec_cross_validation",
        lambda **_kwargs: {"passed": True, "mirror_commit": "a" * 40},
    )
    accepted = importer.import_aliases(
        stooq_report_path=stooq_report,
        audit_path=audit,
        price_dir=price_dir,
        cache_dir=tmp_path / "cache",
        output=tmp_path / "accepted.json",
        end=dates[-1].strftime("%Y-%m-%d"),
        terminal_tail=True,
        fixed_mirror_provenance=tmp_path / "mirror.json",
        sec_transition_probe=tmp_path / "sec.json",
        apply=True,
    )["records"][0]
    assert accepted["status"] == "UPDATED"
    assert accepted["cross_validation"]["validation_scope"] == (
        "exact_short_tail_plus_fixed_git_mirror_plus_sec_identity"
    )
    assert accepted["cross_validation"]["sessions"] == 9


def test_otc_terminal_tail_can_replace_one_exact_carried_boundary(
    tmp_path, monkeypatch
):
    dates = pd.bdate_range("2025-01-02", periods=35)
    source = pd.DataFrame({
        "date": dates,
        "ticker": "OLDQ",
        "open": range(100, 135),
        "high": range(101, 136),
        "low": range(99, 134),
        "close": range(100, 135),
        "volume": range(1000, 1035),
    })
    local = source.iloc[:30].copy()
    local["ticker"] = "OLD"
    for field in ("open", "high", "low", "close", "volume"):
        local.loc[local.index[-1], field] = local.iloc[-2][field]
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    local.to_csv(price_dir / "old.csv", index=False)
    stooq_report = tmp_path / "stooq.json"
    stooq_report.write_text(json.dumps({"records": [{
        "historical_ticker": "OLD",
        "successor_ticker": "OLDQ",
        "cik": "0000000123",
        "status": "SOURCE_MISSING",
        "sec_matches": [{"cik": "0000000123"}],
        "sec_issuers": [{"cik": "0000000123"}],
    }]}))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"unresolved_terminal_return_histories": [{
        "ticker": "OLD",
        "last_price_date": dates[29].strftime("%Y-%m-%d"),
    }]}))
    monkeypatch.setattr(
        importer,
        "_load_or_fetch",
        lambda *_args, **_kwargs: (b"payload", str(tmp_path / "cache.json.gz")),
    )
    monkeypatch.setattr(
        importer, "_parse_edgar", lambda _payload, _ticker: (source, "Old Corp")
    )

    report = importer.import_aliases(
        stooq_report_path=stooq_report,
        audit_path=audit,
        price_dir=price_dir,
        cache_dir=tmp_path / "cache",
        output=tmp_path / "report.json",
        end=dates[-1].strftime("%Y-%m-%d"),
        terminal_tail=True,
        replace_carried_terminal_row=True,
        apply=True,
    )

    record = report["records"][0]
    persisted = pd.read_csv(price_dir / "old.csv")
    boundary_date = dates[29].strftime("%Y-%m-%d")
    boundary = persisted.loc[persisted["date"].eq(boundary_date)].iloc[0]
    assert record["status"] == "UPDATED"
    assert record["rows_missing"] == 6
    assert record["rows_replaced"] == 1
    assert record["cross_validation"]["validation_scope"] == (
        "replace_single_carried_terminal_row"
    )
    assert record["cross_validation"]["prior_stable_tail_validation"][
        "sessions"
    ] == 29
    assert float(boundary["close"]) == float(source.iloc[29]["close"])
    assert len(persisted) == 35
