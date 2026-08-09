import gzip
import io
import json

from pathlib import Path

import pandas as pd
import pytest

from src.io import fundamentals_update
from src.io.fundamentals_update import (
    audit_fundamentals_coverage,
    parse_companyfacts_annual,
    parse_companyfacts_quarterly,
)
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot


def _fact(val, start=None, end="2025-12-31", filed="2026-02-15", form="10-K", fp="FY"):
    row = {"val": val, "end": end, "filed": filed, "form": form, "fp": fp, "accn": "x"}
    if start:
        row["start"] = start
    return row


def _cache_path(cache_dir, cik=123):
    matches = list(cache_dir.glob(f"CIK{cik:010d}.json*"))
    assert len(matches) == 1
    return matches[0]


def _read_cache_envelope(cache_dir, cik=123):
    path = _cache_path(cache_dir, cik)
    if path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_cache_envelope(cache_dir, envelope, cik=123):
    path = _cache_path(cache_dir, cik)
    serialized = json.dumps(envelope).encode()
    if path.name.endswith(".gz"):
        path.write_bytes(gzip.compress(serialized, mtime=0))
    else:
        path.write_bytes(serialized)


def test_sec_request_slot_throttles_all_workers_through_shared_clock(
    monkeypatch,
):
    clock = iter([4.0, 5.0])
    sleeps = []
    monkeypatch.setattr(
        fundamentals_update, "_SEC_NEXT_REQUEST_AT", 5.0
    )
    monkeypatch.setattr(
        fundamentals_update.time,
        "monotonic",
        lambda: next(clock),
    )
    monkeypatch.setattr(
        fundamentals_update.time, "sleep", sleeps.append
    )

    fundamentals_update._wait_for_sec_request_slot()

    assert sleeps == [1.0]
    assert fundamentals_update._SEC_NEXT_REQUEST_AT == 5.125


def test_companyfacts_parser_keeps_annual_filing_dates_and_rejects_quarters():
    payload = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            _fact(100, "2025-01-01"),
            _fact(30, "2025-01-01", end="2025-03-31", filed="2025-05-01", form="10-Q", fp="Q1"),
        ]}},
        "NetIncomeLoss": {"units": {"USD": [_fact(10, "2025-01-01")]}},
        "GrossProfit": {"units": {"USD": [_fact(40, "2025-01-01")]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [_fact(15, "2025-01-01")]}},
        "Assets": {"units": {"USD": [_fact(200)]}},
        "StockholdersEquity": {"units": {"USD": [_fact(80)]}},
    }}}
    frame = parse_companyfacts_annual("abc", payload, fetched_at="2026-07-18")
    assert set(frame["metric"]) == {
        "revenue", "net_income", "gross_profit", "operating_cash_flow", "assets", "equity"
    }
    assert set(frame["available_date"]) == {pd.Timestamp("2026-02-15")}
    assert (frame["ticker"] == "ABC").all()


def test_new_revenue_concept_extends_old_concept_history():
    old = _fact(80, "2023-01-01", end="2023-12-31", filed="2024-02-01")
    recent = _fact(100, "2025-01-01", end="2025-12-31", filed="2026-02-01")
    payload = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [old]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [recent]}},
    }}}
    revenue = parse_companyfacts_annual("ABC", payload)
    assert revenue["value"].tolist() == [80.0, 100.0]
    assert revenue.iloc[-1]["concept"] == "RevenueFromContractWithCustomerExcludingAssessedTax"


def test_quarterly_parser_accepts_revenue_including_assessed_tax():
    revenue = _fact(
        533_800_000,
        "2025-06-29",
        end="2025-09-27",
        filed="2025-11-05",
        form="10-Q",
        fp="Q1",
    )
    income = _fact(
        4_200_000,
        "2025-06-29",
        end="2025-09-27",
        filed="2025-11-05",
        form="10-Q",
        fp="Q1",
    )
    payload = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerIncludingAssessedTax": {
            "units": {"USD": [revenue]}
        },
        "NetIncomeLoss": {"units": {"USD": [income]}},
    }}}

    frame = parse_companyfacts_quarterly("LITE", payload)

    quarter = frame.set_index("metric")
    assert quarter.loc["revenue", "value"] == 533_800_000
    assert (
        quarter.loc["revenue", "concept"]
        == "RevenueFromContractWithCustomerIncludingAssessedTax"
    )


def test_companyfacts_parser_accepts_total_utility_operating_revenue():
    annual_revenue = _fact(
        12_000_000_000,
        "2025-01-01",
        end="2025-12-31",
        filed="2026-02-20",
    )
    quarterly_revenue = _fact(
        3_100_000_000,
        "2025-01-01",
        end="2025-03-31",
        filed="2025-04-25",
        form="10-Q",
        fp="Q1",
    )
    payload = {"facts": {"us-gaap": {
        "RegulatedAndUnregulatedOperatingRevenue": {
            "units": {"USD": [annual_revenue, quarterly_revenue]}
        },
    }}}

    annual = parse_companyfacts_annual("UTIL", payload)
    quarterly = parse_companyfacts_quarterly("UTIL", payload)

    assert annual.loc[annual["metric"].eq("revenue"), "value"].tolist() == [
        12_000_000_000
    ]
    revenue = quarterly.loc[quarterly["metric"].eq("revenue")].iloc[0]
    assert revenue["value"] == 3_100_000_000
    assert revenue["concept"] == "RegulatedAndUnregulatedOperatingRevenue"


def test_companyfacts_raw_cache_supports_offline_reparse_and_historical_symbol(
    tmp_path, monkeypatch
):
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2025-01-01",
                end="2025-03-31",
                filed="2025-05-01",
                form="10-Q",
                fp="Q1",
            )
        ]}},
    }}}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(
        fundamentals_update,
        "urlopen",
        lambda *_args, **_kwargs: Response(json.dumps(payload).encode()),
    )
    fundamentals_update.fetch_sec_fundamentals(
        "ACTIVE", 123, retries=1, cache_dir=tmp_path
    )

    cache_path = _cache_path(tmp_path)
    envelope = _read_cache_envelope(tmp_path)
    assert envelope["cik"] == 123
    assert envelope["symbols"] == ["ACTIVE"]
    assert envelope["payload"] == payload
    assert cache_path.name.endswith(".json.gz")
    assert not list(tmp_path.glob("*.tmp"))

    monkeypatch.setattr(
        fundamentals_update,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("offline reparse contacted SEC"),
    )
    _, quarterly = fundamentals_update.fetch_sec_fundamentals(
        "HISTORICAL",
        123,
        retries=1,
        cache_dir=tmp_path,
        offline_cache=True,
    )
    assert set(quarterly["ticker"]) == {"HISTORICAL"}


def test_companyfacts_compressed_cache_is_deterministic(tmp_path):
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2025-01-01",
                end="2025-03-31",
                filed="2025-05-01",
                form="10-Q",
                fp="Q1",
            )
        ]}},
    }}}
    fetched_at = pd.Timestamp("2026-07-30T00:00:00")

    fundamentals_update._write_companyfacts_cache(
        "ABC", 123, payload, fetched_at, tmp_path
    )
    path = _cache_path(tmp_path)
    first = path.read_bytes()
    fundamentals_update._write_companyfacts_cache(
        "ABC", 123, payload, fetched_at, tmp_path
    )

    assert path.name.endswith(".json.gz")
    assert path.read_bytes() == first


def test_companyfacts_legacy_json_cache_remains_readable(tmp_path):
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2025-01-01",
                end="2025-03-31",
                filed="2025-05-01",
                form="10-Q",
                fp="Q1",
            )
        ]}},
    }}}
    legacy = tmp_path / "CIK0000000123.json"
    legacy.write_text(json.dumps({
        "cik": 123,
        "symbols": ["ABC"],
        "fetched_at": "2026-07-30T00:00:00",
        "source_url": "https://data.sec.gov/example",
        "payload": payload,
    }), encoding="utf-8")
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)

    annual, quarterly = fundamentals_update.fetch_sec_fundamentals(
        "ABC",
        123,
        retries=1,
        cache_dir=tmp_path,
        offline_cache=True,
    )

    assert annual.empty
    assert set(quarterly["ticker"]) == {"ABC"}
    assert fundamentals_update.verify_companyfacts_cache_manifest(
        tmp_path
    )["verified"]


def test_manifest_checkpoint_rehashes_only_changed_payloads(tmp_path, monkeypatch):
    fundamentals_update._write_companyfacts_cache(
        ["AAA"], 1900011, {"facts": {}}, pd.Timestamp("2026-08-01"), tmp_path
    )
    fundamentals_update._write_companyfacts_cache(
        ["BBB"], 1900012, {"facts": {}}, pd.Timestamp("2026-08-01"), tmp_path
    )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)
    fundamentals_update._write_companyfacts_cache(
        ["AAA"], 1900011, {"facts": {"changed": True}},
        pd.Timestamp("2026-08-02"), tmp_path
    )
    calls = []
    original = fundamentals_update._file_sha256

    def record(path):
        calls.append(Path(path).name)
        return original(path)

    monkeypatch.setattr(fundamentals_update, "_file_sha256", record)
    fundamentals_update.write_companyfacts_cache_manifest(
        tmp_path, changed_payload_paths={"CIK0001900011.json.gz"}
    )
    assert calls == ["CIK0001900011.json.gz"]

    # Reusing an unmarked entry is only a checkpoint optimization; the next
    # full integrity audit must still detect an external mutation.
    payload_path = tmp_path / "CIK0001900012.json.gz"
    payload_path.write_bytes(payload_path.read_bytes() + b"tamper")
    with pytest.raises(RuntimeError, match="integrity mismatch"):
        fundamentals_update.verify_companyfacts_cache_manifest(tmp_path)


def test_companyfacts_cache_coverage_audit_is_verified_and_read_only(
    tmp_path,
):
    payload = {"facts": {"us-gaap": {}}}
    fundamentals_update._write_companyfacts_cache(
        "ABC",
        123,
        payload,
        pd.Timestamp("2026-07-30T00:00:00"),
        tmp_path,
    )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)
    before = {
        path.name: path.read_bytes()
        for path in tmp_path.iterdir()
        if path.is_file()
    }

    result = fundamentals_update.audit_companyfacts_cache_coverage(
        tmp_path, {"ABC", "MISSING"}
    )

    assert result["manifest_verified"]
    assert result["cached_ciks"] == 1
    assert result["required_symbol_count"] == 2
    assert result["cached_required_symbol_count"] == 1
    assert result["missing_cache_symbols"] == ["MISSING"]
    assert result["cache_symbol_coverage"] == 0.5
    assert result["compressed_cache_file_count"] == 1
    assert result["payload_profile_counts"] == {"NO_FACTS": 1}
    assert {
        path.name: path.read_bytes()
        for path in tmp_path.iterdir()
        if path.is_file()
    } == before


def test_companyfacts_cache_coverage_preflight_skips_payload_profiles(
    tmp_path, monkeypatch
):
    fundamentals_update._write_companyfacts_cache(
        "ABC",
        123,
        {"facts": {"us-gaap": {}}},
        pd.Timestamp("2026-07-30T00:00:00"),
        tmp_path,
    )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)

    def fail_if_profiled(_cache_dir):
        raise AssertionError("preflight decoded payload profiles")

    monkeypatch.setattr(
        fundamentals_update,
        "audit_cached_companyfacts_payload_profiles",
        fail_if_profiled,
    )

    result = fundamentals_update.audit_companyfacts_cache_coverage(
        tmp_path,
        {"ABC"},
        include_payload_profiles=False,
    )

    assert result["manifest_verified"]
    assert result["cache_symbol_coverage"] == 1.0
    assert not result["payload_profiles_included"]
    assert "payload_profile_counts" not in result


def test_targeted_payload_profiles_decode_only_requested_distinct_ciks(
    tmp_path, monkeypatch
):
    for symbols, cik in ((["ABC", "ABC.A"], 123), (["XYZ"], 456)):
        fundamentals_update._write_companyfacts_cache(
            symbols,
            cik,
            {"facts": {"us-gaap": {}}},
            pd.Timestamp("2026-07-30T00:00:00"),
            tmp_path,
        )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)
    real_read = fundamentals_update._read_companyfacts_cache_envelope
    read_paths = []

    def record_read(path):
        read_paths.append(path.name)
        return real_read(path)

    monkeypatch.setattr(
        fundamentals_update,
        "_read_companyfacts_cache_envelope",
        record_read,
    )

    profiles = (
        fundamentals_update.cached_companyfacts_symbol_payload_profiles(
            tmp_path,
            {"ABC", "ABC.A", "MISSING"},
        )
    )

    assert set(profiles) == {"ABC", "ABC.A"}
    assert profiles["ABC"]["cik"] == 123
    assert profiles["ABC.A"]["cik"] == 123
    assert read_paths == ["CIK0000000123.json.gz"]


def test_companyfacts_payload_profile_distinguishes_foreign_periodic():
    foreign = {
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "EUR": [{"form": "20-F"}],
                    }
                }
            }
        }
    }
    domestic = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [{"form": "10-Q"}],
                    }
                }
            }
        }
    }

    foreign_result = fundamentals_update.classify_companyfacts_payload(
        foreign
    )
    domestic_result = fundamentals_update.classify_companyfacts_payload(
        domestic
    )

    assert foreign_result["profile"] == "FOREIGN_PERIODIC_NO_10Q"
    assert foreign_result["forms"] == ["20-F"]
    assert foreign_result["units"] == ["EUR"]
    assert domestic_result["profile"] == "US_GAAP_WITH_10Q"
    assert domestic_result["has_supported_revenue_source"]
    assert domestic_result["direct_revenue_concepts"] == ["Revenues"]


def test_sec_ticker_map_snapshot_is_content_addressed_and_manifested(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        fundamentals_update,
        "fetch_sec_ticker_map",
        lambda: {"xyz": 456, "ABC": 123},
    )

    first_mapping, first = (
        fundamentals_update.fetch_sec_ticker_map_snapshot(tmp_path)
    )
    second_mapping, second = (
        fundamentals_update.fetch_sec_ticker_map_snapshot(tmp_path)
    )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)
    verification = fundamentals_update.verify_companyfacts_cache_manifest(
        tmp_path
    )

    assert first_mapping == second_mapping == {"ABC": 123, "XYZ": 456}
    assert first["path"] == second["path"]
    assert first["mapping_sha256"] == second["mapping_sha256"]
    assert first["symbol_count"] == 2
    assert verification["ticker_map_entry_count"] == 1
    with gzip.open(first["path"], "rt", encoding="utf-8") as handle:
        assert json.load(handle) == {"ABC": 123, "XYZ": 456}


def test_companyfacts_batch_fetch_downloads_each_cik_once(tmp_path, monkeypatch):
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2025-01-01",
                end="2025-03-31",
                filed="2025-05-01",
                form="10-Q",
                fp="Q1",
            )
        ]}},
    }}}
    requests = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request, **_kwargs):
        requests.append(request.full_url)
        return Response(json.dumps(payload).encode())

    monkeypatch.setattr(fundamentals_update, "urlopen", fake_urlopen)

    results = fundamentals_update.fetch_sec_fundamentals_for_symbols(
        ["ABC", "ABC.A"], 123, retries=1, cache_dir=tmp_path
    )

    assert len(requests) == 1
    assert set(results) == {"ABC", "ABC.A"}
    assert set(results["ABC"][1]["ticker"]) == {"ABC"}
    assert set(results["ABC.A"][1]["ticker"]) == {"ABC.A"}
    envelope = _read_cache_envelope(tmp_path)
    assert envelope["symbols"] == ["ABC", "ABC.A"]
    assert fundamentals_update._group_tickers_by_cik(
        ["ABC", "ABC.A", "XYZ"],
        {"ABC": 123, "ABC.A": 123, "XYZ": 456},
    ) == {123: ["ABC", "ABC.A"], 456: ["XYZ"]}


def test_cached_cik_alias_is_parsed_and_bound_without_network(tmp_path):
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2025-01-01",
                end="2025-03-31",
                filed="2025-05-01",
                form="10-Q",
                fp="Q1",
            )
        ]}},
    }}}
    fundamentals_update._write_companyfacts_cache(
        "ABC",
        123,
        payload,
        pd.Timestamp("2026-07-30T00:00:00"),
        tmp_path,
    )

    results = (
        fundamentals_update.parse_and_bind_cached_companyfacts_symbols(
            ["ABC.A"], 123, tmp_path
        )
    )

    assert set(results) == {"ABC.A"}
    assert not results["ABC.A"][1].empty
    assert _read_cache_envelope(tmp_path)["symbols"] == ["ABC", "ABC.A"]
    assert fundamentals_update.cached_companyfacts_ciks(tmp_path) == {123}


def test_online_update_rejects_tampered_cache_before_rebaseline(tmp_path):
    fundamentals_update._write_companyfacts_cache(
        "ABC",
        123,
        {"facts": {"us-gaap": {}}},
        pd.Timestamp("2026-07-30T00:00:00"),
        tmp_path,
    )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)
    envelope = _read_cache_envelope(tmp_path)
    envelope["symbols"].append("TAMPERED")
    _write_cache_envelope(tmp_path, envelope)

    with pytest.raises(RuntimeError, match="integrity mismatch"):
        fundamentals_update._update_fundamentals_unlocked(
            as_of=pd.Timestamp("2026-07-31").date(),
            cache_dir=tmp_path,
            output=tmp_path / "annual.csv",
            quarterly_output=tmp_path / "quarterly.csv",
        )


def test_online_update_rejects_unmanifested_existing_cache(tmp_path):
    fundamentals_update._write_companyfacts_cache(
        "ABC",
        123,
        {"facts": {"us-gaap": {}}},
        pd.Timestamp("2026-07-30T00:00:00"),
        tmp_path,
    )

    with pytest.raises(RuntimeError, match="has no manifest"):
        fundamentals_update._update_fundamentals_unlocked(
            as_of=pd.Timestamp("2026-07-31").date(),
            cache_dir=tmp_path,
            output=tmp_path / "annual.csv",
            quarterly_output=tmp_path / "quarterly.csv",
        )


def test_refresh_limit_counts_unique_ciks_and_keeps_aliases_together():
    selected = fundamentals_update.limit_refresh_tickers_by_cik(
        ["ABC", "XYZ", "ABC.A", "LATER"],
        {"ABC": 123, "ABC.A": 123, "XYZ": 456, "LATER": 789},
        limit=1,
    )

    assert selected == ["ABC", "ABC.A"]
    assert fundamentals_update.limit_refresh_tickers_by_cik(
        ["ABC", "XYZ"], {"ABC": 123, "XYZ": 456}, limit=None
    ) == ["ABC", "XYZ"]
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        fundamentals_update.limit_refresh_tickers_by_cik(
            ["ABC"], {"ABC": 123}, limit=0
        )


def test_cache_refresh_expands_selected_cik_to_uncached_cooldown_aliases():
    expanded = fundamentals_update.expand_selected_cik_aliases(
        ["ABC"],
        ["ABC", "ABC.A", "OTHER", "CACHED.A"],
        {"ABC": 123, "ABC.A": 123, "CACHED.A": 123, "OTHER": 456},
        excluded_symbols={"CACHED.A"},
    )

    assert expanded == ["ABC", "ABC.A"]


def test_cache_missing_refresh_selection_skips_cached_and_respects_cooldown():
    state = {
        "ABC": {"cache_last_attempt": "2026-07-30"},
        "XYZ": {"cache_last_attempt": "2026-07-30"},
        "MISSING": {
            "cache_last_attempt": "2025-01-01",
            "cache_status": "companyfacts_not_available",
            "cache_failure_reason": "HTTP Error 404: Not Found",
        },
    }
    selected = fundamentals_update.select_fundamentals_refresh_tickers(
        ["ABC", "XYZ", "NEW", "MISSING"],
        {"ABC": 123, "XYZ": 456, "NEW": 789, "MISSING": 999},
        state,
        pd.Timestamp("2026-07-30").date(),
        refresh_after_days=30,
        cache_missing_only=True,
        cached_symbols={"ABC"},
    )

    assert selected == ["NEW"]
    forced = fundamentals_update.select_fundamentals_refresh_tickers(
        ["ABC", "XYZ", "NEW", "MISSING"],
        {"ABC": 123, "XYZ": 456, "NEW": 789, "MISSING": 999},
        state,
        pd.Timestamp("2026-07-30").date(),
        refresh_after_days=30,
        force=True,
        cache_missing_only=True,
        cached_symbols={"ABC"},
    )
    assert forced == ["XYZ", "NEW", "MISSING"]


def test_raw_cache_only_populates_payload_without_parsed_output_io(
    tmp_path,
    monkeypatch,
):
    universe_path = tmp_path / "universe.csv"
    pd.DataFrame({
        "Symbol": ["AAA"],
        "Name": ["AAA Common Stock"],
    }).to_csv(universe_path, index=False)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(
        fundamentals_update,
        "NASDAQ_300M_STOCK_LIST_FILE",
        str(universe_path),
    )
    monkeypatch.setattr(
        fundamentals_update,
        "investable_common_equities",
        lambda frame: frame,
    )
    monkeypatch.setattr(
        fundamentals_update,
        "fetch_sec_ticker_map_snapshot",
        lambda _cache_dir: ({
            "AAA": 1,
        }, {
            "path": "ticker-map-snapshot",
            "mapping_sha256": "map-sha",
        }),
    )
    monkeypatch.setattr(
        fundamentals_update,
        "write_fundamentals_pair",
        lambda *_args, **_kwargs: pytest.fail(
            "raw-cache-only wrote parsed outputs"
        ),
    )
    monkeypatch.setattr(
        fundamentals_update,
        "audit_fundamentals_coverage",
        lambda *_args, **_kwargs: pytest.fail(
            "raw-cache-only read parsed outputs"
        ),
    )

    def fake_fetch(symbols, cik, _retries, target, _offline):
        fundamentals_update._write_companyfacts_cache(
            symbols,
            cik,
            {"facts": {}},
            pd.Timestamp("2026-07-31"),
            target,
        )
        empty = pd.DataFrame(columns=fundamentals_update.OUTPUT_COLUMNS)
        return {symbol: (empty, empty) for symbol in symbols}

    monkeypatch.setattr(
        fundamentals_update,
        "fetch_sec_fundamentals_for_symbols",
        fake_fetch,
    )

    result = fundamentals_update.populate_missing_companyfacts_cache(
        pd.Timestamp("2026-07-31").date(),
        workers=1,
        limit=1,
        cache_dir=cache_dir,
    )

    assert result["mode"] == "raw_companyfacts_cache_missing_only"
    assert result["requested_ciks"] == 1
    assert result["cache_symbol_coverage"] == 1.0
    assert result["formal_outputs_read"] is False
    assert result["formal_outputs_written"] is False
    assert result["parsed_outputs_written"] is False
    assert fundamentals_update.verify_companyfacts_cache_manifest(
        cache_dir
    )["verified"] is True


def test_raw_cache_only_fetches_explicit_cik_override_missing_from_binding(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    universe_path = tmp_path / "universe.csv"
    pd.DataFrame({
        "Symbol": ["CLBK"],
        "Name": ["Columbia Financial, Inc."],
    }).to_csv(universe_path, index=False)
    monkeypatch.setattr(
        fundamentals_update, "NASDAQ_300M_STOCK_LIST_FILE", universe_path
    )
    monkeypatch.setattr(
        fundamentals_update,
        "fetch_sec_ticker_map",
        lambda: {"CLBK": 2115119},
    )
    legacy_payload = {"facts": {"us-gaap": {}}}
    fundamentals_update._write_companyfacts_cache(
        "CLBK",
        2115119,
        legacy_payload,
        pd.Timestamp("2026-08-01"),
        cache_dir,
    )
    historical_path = fundamentals_update.historical_ticker_cik_path(
        cache_dir
    )
    historical_path.write_text(json.dumps({
        "format_version": 1,
        "entries": {
            "CLBK": {
                "cik": 2115119,
                "predecessor_ciks": [1723596],
                "source_url": "https://example.test/clbk-transition",
            }
        },
    }), encoding="utf-8")
    fundamentals_update.fetch_sec_ticker_map_snapshot(cache_dir)
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)

    requests = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request, **_kwargs):
        requests.append(request.full_url)
        return Response(json.dumps(legacy_payload).encode())

    monkeypatch.setattr(fundamentals_update, "urlopen", fake_urlopen)

    result = fundamentals_update.populate_missing_companyfacts_cache(
        pd.Timestamp("2026-08-02").date(),
        workers=1,
        force=True,
        tickers=["CLBK"],
        cik_overrides={"CLBK": 1723596},
        cache_dir=cache_dir,
    )

    assert any("CIK0001723596.json" in url for url in requests)
    assert 1723596 in fundamentals_update.cached_companyfacts_ciks(cache_dir)
    assert result["formal_outputs_read"] is False
    assert result["formal_outputs_written"] is False
    assert result["parsed_outputs_written"] is False


def test_raw_cache_only_checkpoints_partial_success_before_interrupt(
    tmp_path,
    monkeypatch,
):
    tickers = [f"TICKER{index}" for index in range(1, 7)]
    universe_path = tmp_path / "universe.csv"
    pd.DataFrame({
        "Symbol": tickers,
        "Name": [f"{ticker} Common Stock" for ticker in tickers],
    }).to_csv(universe_path, index=False)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(
        fundamentals_update,
        "NASDAQ_300M_STOCK_LIST_FILE",
        str(universe_path),
    )
    monkeypatch.setattr(
        fundamentals_update,
        "investable_common_equities",
        lambda frame: frame,
    )
    monkeypatch.setattr(
        fundamentals_update,
        "fetch_sec_ticker_map_snapshot",
        lambda _cache_dir: (
            {ticker: index for index, ticker in enumerate(tickers, start=1)},
            {
                "path": "ticker-map-snapshot",
                "mapping_sha256": "map-sha",
            },
        ),
    )

    def fake_fetch(symbols, cik, _retries, target, _offline):
        if cik == 6:
            raise KeyboardInterrupt("simulated network interruption")
        fundamentals_update._write_companyfacts_cache(
            symbols,
            cik,
            {"facts": {}},
            pd.Timestamp("2026-07-31"),
            target,
        )
        empty = pd.DataFrame(columns=fundamentals_update.OUTPUT_COLUMNS)
        return {symbol: (empty, empty) for symbol in symbols}

    monkeypatch.setattr(
        fundamentals_update,
        "fetch_sec_fundamentals_for_symbols",
        fake_fetch,
    )

    with pytest.raises(
        KeyboardInterrupt, match="simulated network interruption"
    ):
        fundamentals_update.populate_missing_companyfacts_cache(
            pd.Timestamp("2026-07-31").date(),
            workers=1,
            limit=6,
            cache_dir=cache_dir,
        )

    verification = fundamentals_update.verify_companyfacts_cache_manifest(
        cache_dir
    )
    state = json.loads(
        fundamentals_update.raw_cache_refresh_state_path(
            cache_dir
        ).read_text(encoding="utf-8")
    )
    assert verification["verified"] is True
    assert verification["entry_count"] == 5
    assert {
        ticker for ticker, entry in state.items()
        if entry["cache_status"] == "raw_cached"
    } == set(tickers[:5])


def test_raw_cache_checkpoint_reconciles_stale_failed_alias(tmp_path):
    fundamentals_update._write_companyfacts_cache(
        ["LILA", "LILAP"],
        1712184,
        {"facts": {}},
        pd.Timestamp("2026-08-01T13:31:59"),
        tmp_path,
    )
    state = {
        "LILAP": {
            "cache_last_attempt": "2026-08-01",
            "cache_status": "fetch_failed",
            "cache_failure_reason": "temporary request failure",
        }
    }

    fundamentals_update._checkpoint_raw_cache_refresh(tmp_path, state)

    persisted = json.loads(
        fundamentals_update.raw_cache_refresh_state_path(tmp_path).read_text(
            encoding="utf-8"
        )
    )
    verification = fundamentals_update.verify_companyfacts_cache_manifest(tmp_path)
    assert persisted["LILAP"]["cache_status"] == "raw_cached"
    assert persisted["LILAP"]["cache_failure_reason"] is None
    assert verification["verified"] is True


def test_raw_cache_checkpoint_recovers_manifest_gap_after_interruption(
    tmp_path, monkeypatch
):
    fundamentals_update._write_companyfacts_cache(
        ["GAP"],
        1900001,
        {"facts": {}},
        pd.Timestamp("2026-08-01T13:31:59"),
        tmp_path,
    )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)
    state = {
        "GAP": {
            "cache_last_attempt": "2026-08-02",
            "cache_status": "raw_cached",
            "cache_failure_reason": None,
        }
    }
    original = fundamentals_update.write_companyfacts_cache_manifest

    def fail_once(_cache_dir, **_kwargs):
        raise OSError("simulated manifest interruption")

    monkeypatch.setattr(
        fundamentals_update,
        "write_companyfacts_cache_manifest",
        fail_once,
    )
    with pytest.raises(OSError, match="simulated manifest interruption"):
        fundamentals_update._checkpoint_raw_cache_refresh(tmp_path, state)
    pending = tmp_path / fundamentals_update.RAW_CACHE_CHECKPOINT_PENDING_NAME
    assert pending.exists()

    monkeypatch.setattr(
        fundamentals_update,
        "write_companyfacts_cache_manifest",
        original,
    )
    fundamentals_update._recover_pending_raw_cache_checkpoint(tmp_path)
    verification = fundamentals_update.verify_companyfacts_cache_manifest(
        tmp_path
    )
    assert verification["verified"] is True
    assert not pending.exists()


def test_raw_cache_refresh_journal_recovers_payload_after_hard_interrupt(
    tmp_path,
):
    fundamentals_update._write_companyfacts_cache(
        ["BASE"], 1900002, {"facts": {}}, pd.Timestamp("2026-08-01"), tmp_path
    )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)
    fundamentals_update._begin_raw_cache_refresh(tmp_path)
    fundamentals_update._write_companyfacts_cache(
        ["NEW"], 1900003, {"facts": {}}, pd.Timestamp("2026-08-02"), tmp_path
    )

    with pytest.raises(RuntimeError, match="inventory mismatch"):
        fundamentals_update.verify_companyfacts_cache_manifest(tmp_path)
    assert (
        tmp_path / fundamentals_update.RAW_CACHE_CHECKPOINT_PENDING_NAME
    ).exists()
    fundamentals_update._recover_pending_raw_cache_checkpoint(tmp_path)
    verification = fundamentals_update.verify_companyfacts_cache_manifest(
        tmp_path
    )
    state = json.loads(
        fundamentals_update.raw_cache_refresh_state_path(tmp_path).read_text(
            encoding="utf-8"
        )
    )
    assert verification["verified"] is True
    assert verification["entry_count"] == 2
    assert state["BASE"]["cache_status"] == "raw_cached"
    assert state["NEW"]["cache_status"] == "raw_cached"
    assert not (
        tmp_path / fundamentals_update.RAW_CACHE_CHECKPOINT_PENDING_NAME
    ).exists()


def test_mid_refresh_checkpoint_keeps_journal_until_final_batch(tmp_path):
    fundamentals_update._write_companyfacts_cache(
        ["BASE"], 1900014, {"facts": {}}, pd.Timestamp("2026-08-01"), tmp_path
    )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)
    fundamentals_update._begin_raw_cache_refresh(tmp_path)
    fundamentals_update._write_companyfacts_cache(
        ["FIRST"], 1900015, {"facts": {}}, pd.Timestamp("2026-08-02"), tmp_path
    )
    fundamentals_update._checkpoint_raw_cache_refresh(
        tmp_path,
        {"FIRST": {"cache_status": "raw_cached"}},
        {"CIK0001900015.json.gz"},
        keep_refresh_journal=True,
    )
    pending = tmp_path / fundamentals_update.RAW_CACHE_CHECKPOINT_PENDING_NAME
    assert pending.exists()
    fundamentals_update._write_companyfacts_cache(
        ["SECOND"], 1900016, {"facts": {}}, pd.Timestamp("2026-08-02"), tmp_path
    )
    with pytest.raises(RuntimeError, match="inventory mismatch"):
        fundamentals_update.verify_companyfacts_cache_manifest(tmp_path)

    fundamentals_update._recover_pending_raw_cache_checkpoint(tmp_path)
    verification = fundamentals_update.verify_companyfacts_cache_manifest(tmp_path)
    assert verification["verified"] is True
    assert verification["entry_count"] == 3
    assert not pending.exists()


def test_cache_audit_separates_manifest_bound_unavailable_symbols(
    tmp_path,
):
    fundamentals_update._write_companyfacts_cache(
        ["AAA"],
        1,
        {"facts": {}},
        pd.Timestamp("2026-07-31"),
        tmp_path,
    )
    state_path = fundamentals_update.raw_cache_refresh_state_path(
        tmp_path
    )
    state_path.write_text(json.dumps({
        "BBB": {
            "cache_status": "not_in_sec_ticker_map",
            "cache_ticker_map_sha256": "official-map-sha",
        },
        "CCC": {
            "cache_status": "companyfacts_not_available",
            "cache_failure_reason": "HTTP Error 404: Not Found",
        },
        "DDD": {
            "cache_status": "fetch_failed",
            "cache_failure_reason": "timeout",
        },
    }), encoding="utf-8")
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)

    result = fundamentals_update.audit_companyfacts_cache_coverage(
        tmp_path,
        ["AAA", "BBB", "CCC", "DDD"],
        include_payload_profiles=False,
    )

    assert result["cached_required_symbol_count"] == 1
    assert result["known_unavailable_required_symbols"] == [
        "BBB",
        "CCC",
    ]
    assert result["unresolved_cache_symbols"] == ["DDD"]
    assert result["cache_resolution_coverage"] == 0.75

    state_path.write_text("{}", encoding="utf-8")
    with pytest.raises(
        RuntimeError,
        match="raw-cache state integrity mismatch",
    ):
        fundamentals_update.verify_companyfacts_cache_manifest(
            tmp_path
        )


def test_refresh_priority_orders_matches_and_preserves_unlisted_universe():
    ordered, matched = fundamentals_update.prioritize_refresh_tickers(
        ["KEEP1", "HIGH2", "HIGH1", "KEEP2"],
        ["HIGH1", "OUTSIDE", "HIGH2"],
    )

    assert ordered == ["HIGH1", "HIGH2", "KEEP1", "KEEP2"]
    assert matched == 2

    selected = fundamentals_update.select_fundamentals_refresh_tickers(
        ordered,
        {ticker: index for index, ticker in enumerate(ordered, start=1)},
        {
            "HIGH1": {"cache_last_attempt": "2026-07-30"},
            "HIGH2": {"cache_last_attempt": "2026-07-30"},
        },
        pd.Timestamp("2026-07-30").date(),
        refresh_after_days=30,
        cache_missing_only=True,
        cached_symbols={"HIGH1"},
    )

    assert selected == ["KEEP1", "KEEP2"]


def test_cache_repair_includes_historical_priority_symbols():
    requested = fundamentals_update.build_requested_refresh_universe(
        ["CURRENT1", "CURRENT2"],
        explicit_tickers=None,
        priority_tickers=["HISTORICAL", "CURRENT2"],
        cache_missing_only=True,
    )

    assert requested == ["HISTORICAL", "CURRENT2", "CURRENT1"]
    assert fundamentals_update.build_requested_refresh_universe(
        ["CURRENT"],
        explicit_tickers=["EXPLICIT"],
        priority_tickers=["HISTORICAL"],
        cache_missing_only=True,
    ) == ["EXPLICIT"]


def test_historical_ticker_cik_resolution_is_cached(
    tmp_path, monkeypatch
):
    xml = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <company-info>
        <cik>0001598110</cik>
        <conformed-name>CyberArk Software Ltd.</conformed-name>
      </company-info>
    </feed>"""
    requests = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, timeout))
        return Response(xml)

    monkeypatch.setattr(fundamentals_update, "urlopen", fake_urlopen)
    monkeypatch.setattr(fundamentals_update.time, "sleep", lambda _value: None)

    result = fundamentals_update.resolve_historical_ticker_ciks(
        ["cybr"], tmp_path
    )
    repeated = fundamentals_update.resolve_historical_ticker_ciks(
        ["CYBR"], tmp_path
    )

    assert result["resolved"] == {"CYBR": 1598110}
    assert repeated["resolved"] == {"CYBR": 1598110}
    assert len(requests) == 1
    assert fundamentals_update.load_historical_ticker_ciks(
        tmp_path
    ) == {"CYBR": 1598110}


def test_companyfacts_manifest_detects_historical_ticker_cik_tampering(
    tmp_path,
):
    historical_path = fundamentals_update.historical_ticker_cik_path(
        tmp_path
    )
    historical_path.write_text(
        json.dumps({"CYBR": 1598110}), encoding="utf-8"
    )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)

    manifest = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )
    historical_entry = manifest["historical_ticker_cik_entry"]
    assert historical_entry["path"] == historical_path.name
    assert historical_entry["bytes"] == historical_path.stat().st_size
    assert len(historical_entry["sha256"]) == 64
    assert fundamentals_update.verify_companyfacts_cache_manifest(
        tmp_path
    )["verified"]

    historical_path.write_text(
        json.dumps({"CYBR": 1}), encoding="utf-8"
    )

    with pytest.raises(
        RuntimeError, match="historical ticker CIK integrity mismatch"
    ):
        fundamentals_update.verify_companyfacts_cache_manifest(tmp_path)


def test_refresh_priority_file_uses_rank_and_records_fingerprint(tmp_path):
    path = tmp_path / "priorities.csv"
    path.write_text(
        "ticker,priority_rank\nlow,20\nHIGH,1\nmiddle,10\n",
        encoding="utf-8",
    )

    result = fundamentals_update.load_refresh_priority_file(path)

    assert result["tickers"] == ["HIGH", "MIDDLE", "LOW"]
    assert result["ticker_count"] == 3
    assert result["ordering"] == "priority_rank_then_file_order"
    assert len(result["sha256"]) == 64


def test_refresh_priority_file_prefers_cache_specific_rank(tmp_path):
    path = tmp_path / "priorities.csv"
    path.write_text(
        "ticker,priority_rank,cache_refresh_priority_rank\n"
        "FOREIGN,1,3\nPARTIAL,2,1\nMISSING,3,2\n",
        encoding="utf-8",
    )

    result = fundamentals_update.load_refresh_priority_file(path)

    assert result["tickers"] == ["PARTIAL", "MISSING", "FOREIGN"]
    assert (
        result["ordering"]
        == "cache_refresh_priority_rank_then_file_order"
    )


def test_refresh_priority_file_filters_to_fetch_queue(tmp_path):
    path = tmp_path / "priorities.csv"
    path.write_text(
        "ticker,priority_rank,fetch_priority_rank\n"
        "FOREIGN,1,\nFETCH_SECOND,2,2\nFETCH_FIRST,3,1\nREPARSE,4,\n",
        encoding="utf-8",
    )

    result = fundamentals_update.load_refresh_priority_file(path)

    assert result["tickers"] == ["FETCH_FIRST", "FETCH_SECOND"]
    assert result["ticker_count"] == 2
    assert result["ordering"] == "fetch_priority_rank_then_file_order"


def test_reparse_priority_file_filters_and_orders_queue(tmp_path):
    path = tmp_path / "priorities.csv"
    path.write_text(
        "ticker,priority_rank,reparse_priority_rank\n"
        "FETCH,1,\nREPARSE_SECOND,2,2\nREPARSE_FIRST,3,1\n",
        encoding="utf-8",
    )

    result = fundamentals_update.load_reparse_priority_file(path)

    assert result["tickers"] == ["REPARSE_FIRST", "REPARSE_SECOND"]
    assert result["ticker_count"] == 2
    assert (
        result["ordering"]
        == "reparse_priority_rank_then_file_order"
    )


def test_refresh_priority_file_rejects_duplicate_tickers(tmp_path):
    path = tmp_path / "priorities.csv"
    path.write_text(
        "ticker,priority_rank\nabc,1\nABC,2\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate tickers: ABC"):
        fundamentals_update.load_refresh_priority_file(path)


def test_cache_refresh_backlog_distinguishes_limit_from_cooldown():
    deferred, cooldown = fundamentals_update.classify_cache_refresh_backlog(
        [
            "REQUESTED",
            "ALIAS_REQUESTED",
            "DEFER1",
            "DEFER2",
            "COOLDOWN",
            "CACHED",
        ],
        ["REQUESTED", "DEFER1", "DEFER2"],
        ["REQUESTED", "ALIAS_REQUESTED"],
        {"CACHED"},
    )

    assert deferred == ["DEFER1", "DEFER2"]
    assert cooldown == ["COOLDOWN"]


def test_update_fundamentals_rejects_nonpositive_limit_before_io():
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        fundamentals_update._update_fundamentals_unlocked(
            as_of=pd.Timestamp("2026-07-31").date(),
            limit=0,
        )


def test_global_unmapped_universe_is_independent_of_targeted_refresh():
    universe = ["ABC", "TARGET", "UNMAPPED"]
    cik_map = {"ABC": 123, "TARGET": 456}
    targeted = ["TARGET"]

    global_unmapped = fundamentals_update.unmapped_fundamentals_tickers(
        universe, cik_map
    )
    explicit_unknown = fundamentals_update.unmapped_fundamentals_tickers(
        targeted, cik_map
    )

    assert global_unmapped == ["UNMAPPED"]
    assert explicit_unknown == []


def test_successful_refresh_replaces_stale_ticker_history():
    existing = pd.DataFrame([
        {
            "ticker": ticker,
            "fiscal_end": "2020-03-31",
            "available_date": "2020-05-01",
            "metric": "net_income",
            "value": value,
            "taxonomy": "us-gaap",
            "concept": "Old",
            "form": "10-Q",
            "accession": f"old-{ticker}",
            "fetched_at": "2020-05-01",
        }
        for ticker, value in (("ABC", 1), ("KEEP", 2))
    ])
    incoming = pd.DataFrame([{
        "ticker": "ABC",
        "fiscal_end": "2025-03-31",
        "available_date": "2025-05-01",
        "metric": "net_income",
        "value": 10,
        "taxonomy": "us-gaap",
        "concept": "NetIncomeLoss",
        "form": "10-Q",
        "accession": "new-ABC",
        "fetched_at": "2025-05-01",
    }])

    replaced = fundamentals_update.replace_ticker_fundamentals(
        existing, incoming, {"ABC"}
    )

    assert set(replaced["ticker"]) == {"ABC", "KEEP"}
    assert replaced.loc[replaced["ticker"].eq("ABC"), "value"].tolist() == [10]
    assert replaced.loc[replaced["ticker"].eq("KEEP"), "value"].tolist() == [2]


def test_cache_repair_integration_preserves_existing_ticker_history():
    existing = pd.DataFrame([
        {
            "ticker": ticker,
            "fiscal_end": "2020-03-31",
            "available_date": "2020-05-01",
            "metric": "net_income",
            "value": value,
            "taxonomy": "us-gaap",
            "concept": "Old",
            "form": "10-Q",
            "accession": f"old-{ticker}",
            "fetched_at": "2020-05-01",
        }
        for ticker, value in (("ABC", 1), ("KEEP", 2))
    ])
    incoming = pd.DataFrame([{
        "ticker": "ABC",
        "fiscal_end": "2025-03-31",
        "available_date": "2025-05-01",
        "metric": "net_income",
        "value": 10,
        "taxonomy": "us-gaap",
        "concept": "NetIncomeLoss",
        "form": "10-Q",
        "accession": "new-ABC",
        "fetched_at": "2025-05-01",
    }])

    merged = fundamentals_update.integrate_refreshed_fundamentals(
        existing,
        incoming,
        {"ABC"},
        non_destructive=True,
    )

    assert merged.loc[merged["ticker"].eq("ABC"), "value"].tolist() == [1, 10]
    assert merged.loc[merged["ticker"].eq("KEEP"), "value"].tolist() == [2]


def test_cache_repair_integration_replaces_covered_period_only():
    existing = pd.DataFrame([
        {
            "ticker": "ABC",
            "fiscal_end": fiscal_end,
            "available_date": available_date,
            "metric": "net_income",
            "value": value,
            "taxonomy": "legacy",
            "concept": "LegacyIncome",
            "form": "legacy",
            "accession": accession,
            "fetched_at": available_date,
        }
        for fiscal_end, available_date, value, accession in (
            ("2024-03-31", "2024-06-01", 1, "old-covered"),
            ("2023-03-31", "2023-06-01", 2, "old-uncovered"),
        )
    ])
    incoming = pd.DataFrame([{
        "ticker": "ABC",
        "fiscal_end": "2024-03-31",
        "available_date": "2024-05-01",
        "metric": "net_income",
        "value": 10,
        "taxonomy": "us-gaap",
        "concept": "NetIncomeLoss",
        "form": "10-Q",
        "accession": "new-covered",
        "fetched_at": "2024-05-01",
    }])

    merged = fundamentals_update.integrate_refreshed_fundamentals(
        existing,
        incoming,
        {"ABC"},
        non_destructive=True,
    )

    assert set(merged["accession"]) == {"old-uncovered", "new-covered"}
    assert "old-covered" not in set(merged["accession"])


def test_restore_uncovered_periods_does_not_downgrade_covered_period():
    current = pd.DataFrame([
        {
            "ticker": "ABC",
            "fiscal_end": "2024-03-31",
            "available_date": "2024-05-01",
            "metric": "net_income",
            "value": 10,
            "taxonomy": "us-gaap",
            "concept": "NetIncomeLoss",
            "form": "10-Q",
            "accession": "current",
            "fetched_at": "2024-05-01",
        }
    ])
    fallback = pd.DataFrame([
        {
            **current.iloc[0].to_dict(),
            "available_date": "2024-06-01",
            "value": 1,
            "accession": "covered-fallback",
        },
        {
            **current.iloc[0].to_dict(),
            "fiscal_end": "2023-03-31",
            "available_date": "2023-06-01",
            "value": 2,
            "accession": "uncovered-fallback",
        },
    ])

    merged, restored = (
        fundamentals_update.restore_uncovered_fundamental_periods(
            current, fallback
        )
    )

    assert restored["accession"].tolist() == ["uncovered-fallback"]
    assert set(merged["accession"]) == {"current", "uncovered-fallback"}


def test_fundamentals_pair_write_rolls_back_first_file_if_second_fails(
    tmp_path, monkeypatch
):
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    annual_output.write_text("old annual\n", encoding="utf-8")
    quarterly_output.write_text("old quarterly\n", encoding="utf-8")
    annual_before = annual_output.read_bytes()
    quarterly_before = quarterly_output.read_bytes()
    real_replace = fundamentals_update.os.replace
    failure_injected = False

    def fail_second_staged_replace(source, destination):
        nonlocal failure_injected
        if (
            destination == quarterly_output
            and str(source).endswith(".tmp")
            and not failure_injected
        ):
            failure_injected = True
            raise OSError("injected quarterly replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(
        fundamentals_update.os, "replace", fail_second_staged_replace
    )

    with pytest.raises(OSError, match="injected quarterly"):
        fundamentals_update.write_fundamentals_pair(
            pd.DataFrame({"ticker": ["NEW"]}),
            annual_output,
            pd.DataFrame({"ticker": ["NEW"]}),
            quarterly_output,
        )

    assert annual_output.read_bytes() == annual_before
    assert quarterly_output.read_bytes() == quarterly_before
    assert not list(tmp_path.glob(".*.tmp"))
    assert not list(tmp_path.glob(".*.bak.*"))


@pytest.mark.parametrize(
    "refresh_option",
    [
        ["--offline-cache"],
        ["--force"],
        ["--ticker-cik", "ABC=123"],
        ["--workers", "2"],
        ["--refresh-after-days", "7"],
    ],
)
def test_reparse_cli_rejects_silently_ignored_refresh_options(
    monkeypatch, capsys, refresh_option
):
    monkeypatch.setattr(
        "sys.argv",
        [
            "fundamentals_update.py",
            "--reparse-cache",
            "incremental",
            "--tickers",
            "ABC",
            *refresh_option,
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        fundamentals_update.main()

    assert (
        "--reparse-cache cannot be combined with online-refresh options"
        in capsys.readouterr().err
    )


def test_incremental_reparse_uses_manifest_index_and_reads_only_target_cik(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    for ticker, cik in (("ABC", 123), ("XYZ", 456)):
        fundamentals_update._write_companyfacts_cache(
            ticker,
            cik,
            {"facts": {"us-gaap": {}}},
            pd.Timestamp("2026-07-30T00:00:00"),
            cache_dir,
        )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)
    real_read = fundamentals_update._read_companyfacts_cache_envelope
    real_sha256 = fundamentals_update._file_sha256
    read_paths = []
    hashed_payload_paths = []

    def record_read(path):
        read_paths.append(path.name)
        return real_read(path)

    def record_sha256(path):
        if path.name.startswith("CIK"):
            hashed_payload_paths.append(path.name)
        return real_sha256(path)

    monkeypatch.setattr(
        fundamentals_update,
        "_read_companyfacts_cache_envelope",
        record_read,
    )
    monkeypatch.setattr(
        fundamentals_update,
        "_file_sha256",
        record_sha256,
    )

    result = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        tickers=["ABC"],
    )

    assert result["requested_tickers"] == ["ABC"]
    assert read_paths == ["CIK0000000123.json.gz"]
    assert set(hashed_payload_paths) == {"CIK0000000123.json.gz"}
    assert result["cache_manifest_verification_scope"] == "selected_payloads"
    assert result["cache_manifest_verified_payload_count"] == 1


def test_incremental_reparse_with_no_parsed_facts_skips_formal_csv_io(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    annual_output.write_text("keep annual bytes\n", encoding="utf-8")
    quarterly_output.write_text("keep quarterly bytes\n", encoding="utf-8")
    annual_before = annual_output.read_bytes()
    quarterly_before = quarterly_output.read_bytes()
    fundamentals_update._write_companyfacts_cache(
        "EMPTY",
        123,
        {"facts": {"us-gaap": {}}},
        pd.Timestamp("2026-07-30T00:00:00"),
        cache_dir,
    )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)

    real_read_csv = fundamentals_update.pd.read_csv

    def reject_formal_csv_reads(path, *args, **kwargs):
        if str(path) in {str(annual_output), str(quarterly_output)}:
            pytest.fail("empty incremental reparse read a formal CSV")
        return real_read_csv(path, *args, **kwargs)

    monkeypatch.setattr(
        fundamentals_update.pd,
        "read_csv",
        reject_formal_csv_reads,
    )
    result = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        tickers=["EMPTY"],
    )

    assert result["parsed_outputs_written"] is False
    assert result["annual_incoming_rows"] == 0
    assert result["quarterly_incoming_rows"] == 0
    assert result["annual_rows"] is None
    assert result["quarterly_rows"] is None
    assert annual_output.read_bytes() == annual_before
    assert quarterly_output.read_bytes() == quarterly_before
    assert result["reparse_state_ticker_count"] == 1


def test_incremental_reparse_does_not_touch_empty_output_side(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    quarterly_output.write_text("keep quarterly bytes\n", encoding="utf-8")
    quarterly_before = quarterly_output.read_bytes()
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2025-01-01",
                end="2025-12-31",
                filed="2026-02-15",
                form="10-K",
                fp="FY",
            )
        ]}},
    }}}
    fundamentals_update._write_companyfacts_cache(
        "ANNUAL",
        123,
        payload,
        pd.Timestamp("2026-02-15T00:00:00"),
        cache_dir,
    )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)
    real_read_csv = fundamentals_update.pd.read_csv

    def reject_quarterly_read(path, *args, **kwargs):
        if str(path) == str(quarterly_output):
            pytest.fail("annual-only incremental reparse read quarterly CSV")
        return real_read_csv(path, *args, **kwargs)

    monkeypatch.setattr(
        fundamentals_update.pd,
        "read_csv",
        reject_quarterly_read,
    )
    result = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        tickers=["ANNUAL"],
    )

    assert result["annual_incoming_rows"] == 1
    assert result["quarterly_incoming_rows"] == 0
    assert result["annual_output_written"] is True
    assert result["quarterly_output_written"] is False
    assert annual_output.exists()
    assert quarterly_output.read_bytes() == quarterly_before


def test_companyfacts_cache_reparse_rebuilds_outputs_from_scratch(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2025-01-01",
                end="2025-03-31",
                filed="2025-05-01",
                form="10-Q",
                fp="Q1",
            )
        ]}},
    }}}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(
        fundamentals_update,
        "urlopen",
        lambda *_args, **_kwargs: Response(json.dumps(payload).encode()),
    )
    fundamentals_update.fetch_sec_fundamentals(
        "ABC", 123, retries=1, cache_dir=cache_dir
    )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)
    pd.DataFrame([{
        "ticker": "STALE",
        "fiscal_end": "2020-03-31",
        "available_date": "2020-05-01",
        "metric": "net_income",
        "value": 1,
        "taxonomy": "us-gaap",
        "concept": "Old",
        "form": "10-Q",
        "accession": "old",
        "fetched_at": "2020-05-01",
    }]).to_csv(quarterly_output, index=False)

    result = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        required_symbols={"ABC"},
    )

    rebuilt = pd.read_csv(quarterly_output)
    assert set(rebuilt["ticker"]) == {"ABC"}
    assert result["cached_ciks"] == 1
    assert result["required_symbol_count"] == 1
    assert result["cache_symbol_coverage"] == 1.0
    assert result["cache_manifest_verification_scope"] == "full"
    assert result["cache_manifest_verified_payload_count"] == 1
    assert fundamentals_update.cached_companyfacts_symbols(cache_dir) == {
        "ABC"
    }
    assert fundamentals_update.cached_companyfacts_cik_map(cache_dir) == {
        "ABC": 123
    }
    assert result["reparse_state_ticker_count"] == 1
    changed, unchanged = (
        fundamentals_update.select_changed_companyfacts_reparse_tickers(
            ["ABC"], cache_dir
        )
    )
    assert changed == []
    assert unchanged == ["ABC"]
    manifest = json.loads(
        (cache_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["entry_count"] == 1
    assert len(manifest["entries"][0]["sha256"]) == 64


def test_companyfacts_full_reparse_is_byte_deterministic(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    annual_one = tmp_path / "annual-one.csv"
    quarterly_one = tmp_path / "quarterly-one.csv"
    annual_two = tmp_path / "annual-two.csv"
    quarterly_two = tmp_path / "quarterly-two.csv"
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2025-01-01",
                end="2025-03-31",
                filed="2025-05-01",
                form="10-Q",
                fp="Q1",
            )
        ]}},
    }}}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(
        fundamentals_update,
        "urlopen",
        lambda *_args, **_kwargs: Response(json.dumps(payload).encode()),
    )
    fundamentals_update.fetch_sec_fundamentals(
        "ABC", 123, retries=1, cache_dir=cache_dir
    )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)

    fundamentals_update.reparse_companyfacts_cache(
        cache_dir, annual_one, quarterly_one, required_symbols={"ABC"}
    )
    fundamentals_update.reparse_companyfacts_cache(
        cache_dir, annual_two, quarterly_two, required_symbols={"ABC"}
    )

    assert fundamentals_update._file_sha256(annual_one) == (
        fundamentals_update._file_sha256(annual_two)
    )
    assert fundamentals_update._file_sha256(quarterly_one) == (
        fundamentals_update._file_sha256(quarterly_two)
    )


def test_companyfacts_full_reparse_rejects_partial_cache_before_writes(
    tmp_path,
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    annual_output.write_text("old annual\n", encoding="utf-8")
    quarterly_output.write_text("old quarterly\n", encoding="utf-8")
    annual_before = annual_output.read_bytes()
    quarterly_before = quarterly_output.read_bytes()
    fundamentals_update._write_companyfacts_cache(
        "ABC",
        123,
        {"facts": {"us-gaap": {}}},
        pd.Timestamp("2026-07-30T00:00:00"),
        cache_dir,
    )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)

    with pytest.raises(RuntimeError, match=r"incomplete.*1 missing.*MISSING"):
        fundamentals_update.reparse_companyfacts_cache(
            cache_dir,
            annual_output,
            quarterly_output,
            required_symbols={"ABC", "MISSING"},
        )

    assert annual_output.read_bytes() == annual_before
    assert quarterly_output.read_bytes() == quarterly_before
    assert not fundamentals_update.companyfacts_reparse_state_path(
        cache_dir
    ).exists()


def test_companyfacts_full_reparse_dry_run_compares_without_writes(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    universe_output = tmp_path / "universe.csv"
    formal_row = {
        "ticker": "ABC",
        "fiscal_end": "2020-03-31",
        "available_date": "2020-05-01",
        "metric": "net_income",
        "value": 1,
        "taxonomy": "us-gaap",
        "concept": "Old",
        "form": "10-Q",
        "accession": "old",
        "fetched_at": "2020-05-01",
    }
    pd.DataFrame([formal_row]).to_csv(annual_output, index=False)
    pd.DataFrame([formal_row]).to_csv(quarterly_output, index=False)
    annual_before = annual_output.read_bytes()
    quarterly_before = quarterly_output.read_bytes()
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2025-01-01",
                end="2025-03-31",
                filed="2025-05-01",
                form="10-Q",
                fp="Q1",
            )
        ]}},
    }}}
    fundamentals_update._write_companyfacts_cache(
        "ABC", 123, payload, pd.Timestamp("2026-07-30"), cache_dir
    )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)
    manifest_before = (cache_dir / "manifest.json").read_bytes()
    reparse_state_path = fundamentals_update.companyfacts_reparse_state_path(
        cache_dir
    )
    reparse_state_path.write_text(
        json.dumps({"format_version": 1, "tickers": {"ABC": {}}}),
        encoding="utf-8",
    )
    reparse_state_before = reparse_state_path.read_bytes()
    pd.DataFrame([{
        "Symbol": "ABC",
        "Name": "ABC Common Stock",
    }]).to_csv(universe_output, index=False)
    monkeypatch.setattr(
        fundamentals_update,
        "NASDAQ_300M_STOCK_LIST_FILE",
        universe_output,
    )

    result = fundamentals_update.dry_run_companyfacts_full_reparse(
        cache_dir,
        annual_output,
        quarterly_output,
        required_symbols={"ABC"},
    )

    assert result["mode"] == "offline_cache_full_rebuild_dry_run"
    assert result["dry_run"] is True
    assert result["required_symbol_count"] == 1
    assert result["unresolved_cache_symbol_count"] == 0
    assert result["formal_outputs_read"] is True
    assert result["formal_outputs_written"] is False
    assert result["annual_output_written"] is False
    assert result["quarterly_output_written"] is False
    assert result["parsed_outputs_written"] is False
    assert result["temporary_outputs_removed"] is True
    assert result["formal_outputs_unchanged"] is True
    assert result["formal_content_match"] is False
    assert result["formal_rebuild_gate"] == "BLOCKED_FORMAL_CONTENT_MISMATCH"
    assert result["annual_comparison"]["formal_rows"] == 1
    assert result["annual_comparison"]["rebuilt_rows"] == 0
    assert result["annual_comparison"]["exact_byte_match"] is False
    assert annual_output.read_bytes() == annual_before
    assert quarterly_output.read_bytes() == quarterly_before
    assert (cache_dir / "manifest.json").read_bytes() == manifest_before
    assert reparse_state_path.read_bytes() == reparse_state_before


def test_full_reparse_comparison_ignores_fetch_time_but_reports_content_drift(
    tmp_path,
):
    formal_output = tmp_path / "formal.csv"
    rebuilt_output = tmp_path / "rebuilt.csv"
    row = {
        "ticker": "ABC",
        "fiscal_end": "2024-03-31",
        "available_date": "2024-05-01",
        "metric": "net_income",
        "value": 100,
        "taxonomy": "us-gaap",
        "concept": "NetIncomeLoss",
        "form": "10-Q",
        "accession": "0000000000-24-000001",
        "fetched_at": "2024-05-01",
    }
    pd.DataFrame([row]).to_csv(formal_output, index=False)
    fetched_at_only = {**row, "fetched_at": "2026-08-02"}
    pd.DataFrame([fetched_at_only]).to_csv(rebuilt_output, index=False)

    timestamp_only = fundamentals_update._full_reparse_output_comparison(
        formal_output,
        rebuilt_output,
    )

    assert timestamp_only["exact_byte_match"] is False
    assert timestamp_only["content_comparison_excluded_columns"] == [
        "fetched_at"
    ]
    assert timestamp_only["content_difference_ticker_count"] == 0
    assert timestamp_only["formal_content_match"] is True
    assert "content_difference_tickers" not in timestamp_only

    content_changed = {**fetched_at_only, "value": 101}
    pd.DataFrame([content_changed]).to_csv(rebuilt_output, index=False)
    comparison = fundamentals_update._full_reparse_output_comparison(
        formal_output,
        rebuilt_output,
        include_ticker_deltas=True,
    )

    assert comparison["content_difference_ticker_count"] == 1
    assert comparison["formal_content_match"] is False
    assert comparison["formal_only_content_row_count"] == 1
    assert comparison["rebuilt_only_content_row_count"] == 1
    assert comparison["content_difference_tickers"] == [{
        "ticker": "ABC",
        "formal_rows": 1,
        "rebuilt_rows": 1,
        "formal_only_content_rows": 1,
        "rebuilt_only_content_rows": 1,
    }]


def test_full_reparse_dry_run_cli_uses_safe_entry_point(monkeypatch, capsys):
    captured = {}

    def dry_run(**kwargs):
        captured.update(kwargs)
        return {"mode": "offline_cache_full_rebuild_dry_run"}

    monkeypatch.setattr(
        fundamentals_update,
        "dry_run_companyfacts_full_reparse",
        dry_run,
    )
    monkeypatch.setattr(
        fundamentals_update,
        "load_companyfacts_full_rebuild_inputs",
        lambda _snapshot, _scope: {
            "cache_dir": Path("immutable-snapshot"),
            "required_symbols": ["ABC"],
            "cache_manifest_sha256": "a" * 64,
            "rebuild_recipe_bound": True,
            "rebuild_recipe_sha256": "c" * 64,
            "snapshot_id": "manifest-test",
            "scope": {
                "scope_path": "scope.json",
                "required_symbols_sha256": "b" * 64,
            },
        },
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "fundamentals_update.py",
            "--reparse-cache",
            "full",
            "--dry-run",
            "--cache-snapshot",
            "immutable-snapshot",
            "--full-rebuild-scope",
            "scope.json",
        ],
    )

    fundamentals_update.main()

    assert captured["cache_dir"] == Path("immutable-snapshot")
    assert captured["required_symbols"] == ["ABC"]
    assert captured["expected_cache_manifest_sha256"] == "a" * 64
    assert captured["expected_rebuild_recipe_sha256"] == "c" * 64
    assert json.loads(capsys.readouterr().out)["mode"] == (
        "offline_cache_full_rebuild_dry_run"
    )


def test_online_raw_refresh_cli_defaults_to_25_cik_batch(monkeypatch, capsys):
    captured = {}

    def refresh(*args, **kwargs):
        captured.update(kwargs)
        return {"mode": "raw_companyfacts_cache_missing_only"}

    monkeypatch.setattr(
        fundamentals_update,
        "populate_missing_companyfacts_cache",
        refresh,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "fundamentals_update.py",
            "--cache-missing-only",
            "--raw-cache-only",
        ],
    )

    fundamentals_update.main()

    assert captured["limit"] == 25
    assert json.loads(capsys.readouterr().out)["mode"] == (
        "raw_companyfacts_cache_missing_only"
    )


def test_offline_cache_cli_does_not_use_online_batch_default(monkeypatch, capsys):
    captured = {}

    def refresh(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return {"mode": "offline_cache"}

    monkeypatch.setattr(fundamentals_update, "update_fundamentals", refresh)
    monkeypatch.setattr(
        "sys.argv",
        ["fundamentals_update.py", "--offline-cache"],
    )

    fundamentals_update.main()

    assert captured["args"][2] is None
    assert json.loads(capsys.readouterr().out)["mode"] == "offline_cache"


def test_full_reparse_dry_run_rejects_parser_recipe_mismatch_before_cache_read(
    tmp_path,
):
    with pytest.raises(ValueError, match="parser recipe does not match"):
        fundamentals_update.dry_run_companyfacts_full_reparse(
            tmp_path / "cache",
            tmp_path / "annual.csv",
            tmp_path / "quarterly.csv",
            required_symbols={"ABC"},
            expected_cache_manifest_sha256="a" * 64,
            expected_rebuild_recipe_sha256="0" * 64,
        )


def test_full_reparse_rejects_unpaired_snapshot_and_recipe_provenance(tmp_path):
    with pytest.raises(ValueError, match="requires both cache manifest"):
        fundamentals_update.dry_run_companyfacts_full_reparse(
            tmp_path / "cache",
            tmp_path / "annual.csv",
            tmp_path / "quarterly.csv",
            required_symbols={"ABC"},
            expected_cache_manifest_sha256="a" * 64,
        )
    with pytest.raises(ValueError, match="requires both cache manifest"):
        fundamentals_update.reparse_companyfacts_cache(
            tmp_path / "cache",
            tmp_path / "annual.csv",
            tmp_path / "quarterly.csv",
            required_symbols={"ABC"},
            expected_rebuild_recipe_sha256="b" * 64,
        )


def test_full_reparse_cli_rejects_legacy_unbound_scope(monkeypatch, capsys):
    monkeypatch.setattr(
        fundamentals_update,
        "load_companyfacts_full_rebuild_inputs",
        lambda _snapshot, _scope: {
            "cache_dir": Path("immutable-snapshot"),
            "required_symbols": ["ABC"],
            "cache_manifest_sha256": "a" * 64,
            "rebuild_recipe_bound": False,
            "rebuild_recipe_sha256": None,
            "snapshot_id": "manifest-test",
            "scope": {
                "scope_path": "scope.json",
                "required_symbols_sha256": "b" * 64,
            },
        },
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "fundamentals_update.py",
            "--reparse-cache",
            "full",
            "--dry-run",
            "--cache-snapshot",
            "immutable-snapshot",
            "--full-rebuild-scope",
            "legacy-scope.json",
        ],
    )

    with pytest.raises(SystemExit):
        fundamentals_update.main()

    assert "parser-recipe-bound" in capsys.readouterr().err


def test_reparse_dry_run_cli_requires_full_mode(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "fundamentals_update.py",
            "--reparse-cache",
            "incremental",
            "--tickers",
            "ABC",
            "--dry-run",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        fundamentals_update.main()

    assert "--dry-run requires --reparse-cache full" in capsys.readouterr().err


def test_full_reparse_cli_requires_immutable_snapshot_and_scope(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "fundamentals_update.py",
            "--reparse-cache",
            "full",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        fundamentals_update.main()

    assert "requires both --cache-snapshot and --full-rebuild-scope" in (
        capsys.readouterr().err
    )


def test_full_reparse_api_requires_explicit_immutable_ticker_scope(tmp_path):
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"

    with pytest.raises(ValueError, match="explicit immutable required_symbols scope"):
        fundamentals_update.reparse_companyfacts_cache(
            tmp_path / "cache",
            annual_output,
            quarterly_output,
        )
    with pytest.raises(ValueError, match="explicit immutable required_symbols scope"):
        fundamentals_update.dry_run_companyfacts_full_reparse(
            tmp_path / "cache",
            annual_output,
            quarterly_output,
        )

    assert not annual_output.exists()
    assert not quarterly_output.exists()


def test_full_reparse_replaces_outputs_from_only_the_declared_scope(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    payload = {"facts": {"us-gaap": {}}}
    for ticker, cik in (("ABC", 123), ("OUTSIDE", 456)):
        fundamentals_update._write_companyfacts_cache(
            ticker,
            cik,
            payload,
            pd.Timestamp("2026-07-30T00:00:00"),
            cache_dir,
        )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)

    def parsed(symbol, metric):
        return pd.DataFrame([{
            "ticker": symbol,
            "fiscal_end": "2024-03-31",
            "available_date": "2024-05-01",
            "metric": metric,
            "value": 1,
            "taxonomy": "us-gaap",
            "concept": "Example",
            "form": "10-Q",
            "accession": "0000000000-24-000001",
            "fetched_at": "2026-07-30",
        }])

    monkeypatch.setattr(
        fundamentals_update,
        "parse_companyfacts_annual",
        lambda symbol, _payload, _fetched_at: parsed(symbol, "annual"),
    )
    monkeypatch.setattr(
        fundamentals_update,
        "parse_registered_companyfacts_quarterly",
        lambda symbol, _cik, _payload, _fetched_at: (
            parsed(symbol, "quarterly"),
            False,
        ),
    )
    pd.DataFrame([{
        "ticker": "OLD",
        "fiscal_end": "2020-03-31",
        "available_date": "2020-05-01",
        "metric": "old",
        "value": 1,
        "taxonomy": "us-gaap",
        "concept": "Old",
        "form": "10-Q",
        "accession": "old",
        "fetched_at": "2020-05-01",
    }]).to_csv(annual_output, index=False)
    pd.read_csv(annual_output).to_csv(quarterly_output, index=False)

    result = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        required_symbols={"ABC"},
    )

    assert result["mode"] == "offline_cache_full_rebuild"
    assert result["merge_policy"] == "replace_complete_outputs"
    assert result["parsed_scope_symbol_count"] == 1
    assert set(pd.read_csv(annual_output)["ticker"]) == {"ABC"}
    assert set(pd.read_csv(quarterly_output)["ticker"]) == {"ABC"}
    state = fundamentals_update.load_companyfacts_reparse_state(cache_dir)
    assert set(state["tickers"]) == {"ABC"}


def test_explicit_historical_cik_chain_reparses_all_sourced_payloads(
    tmp_path,
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    old_payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2024-01-01",
                end="2024-03-31",
                filed="2024-05-01",
                form="10-Q",
                fp="Q1",
            )
        ]}},
    }}}
    new_payload = {"facts": {"us-gaap": {}}}
    fundamentals_update._write_companyfacts_cache(
        "ABC", 111, old_payload, pd.Timestamp("2026-07-30"), cache_dir
    )
    fundamentals_update._write_companyfacts_cache(
        "ABC", 222, new_payload, pd.Timestamp("2026-07-30"), cache_dir
    )
    historical_path = fundamentals_update.historical_ticker_cik_path(
        cache_dir
    )
    historical_path.write_text(json.dumps({
        "format_version": 1,
        "entries": {
            "ABC": {
                "cik": 222,
                "predecessor_ciks": [111],
                "source_url": "https://www.sec.gov/example-transition",
            }
        },
    }), encoding="utf-8")
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)

    assert fundamentals_update.cached_companyfacts_cik_map(cache_dir) == {
        "ABC": 222
    }
    assert fundamentals_update.cached_companyfacts_cik_chains_for_symbols(
        ["ABC"], cache_dir
    ) == {"ABC": (222, 111)}
    profile = fundamentals_update.cached_companyfacts_symbol_payload_profiles(
        cache_dir, {"ABC"}
    )["ABC"]
    assert profile["cik"] == 222
    assert profile["profile_source_cik"] == 111
    assert profile["profile"] == "US_GAAP_WITH_10Q"
    assert profile["cik_chain"] == [222, 111]

    result = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        tickers=["ABC"],
    )

    rebuilt = pd.read_csv(quarterly_output)
    assert result["cached_ciks"] == 2
    assert set(rebuilt["fiscal_end"]) == {"2024-03-31"}
    fingerprint = fundamentals_update.companyfacts_reparse_fingerprint(
        "ABC", cache_dir
    )
    assert fingerprint["cik_chain"] == [222, 111]
    assert len(fingerprint["payloads"]) == 2

    full_annual = tmp_path / "full_annual.csv"
    full_quarterly = tmp_path / "full_quarterly.csv"
    full = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        full_annual,
        full_quarterly,
        required_symbols={"ABC"},
    )

    assert full["mode"] == "offline_cache_full_rebuild"
    assert full["cached_ciks"] == 2
    assert set(pd.read_csv(full_quarterly)["fiscal_end"]) == {"2024-03-31"}


def test_undeclared_historical_cik_collision_is_rejected(tmp_path):
    for cik in (111, 222):
        fundamentals_update._write_companyfacts_cache(
            "ABC",
            cik,
            {"facts": {"us-gaap": {}}},
            pd.Timestamp("2026-07-30"),
            tmp_path,
        )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)

    with pytest.raises(RuntimeError, match="Multiple cached CIKs"):
        fundamentals_update.cached_companyfacts_cik_map(tmp_path)


def test_companyfacts_full_reparse_allows_only_manifest_bound_unavailable(
    tmp_path,
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    fundamentals_update._write_companyfacts_cache(
        "ABC",
        123,
        {"facts": {"us-gaap": {}}},
        pd.Timestamp("2026-07-30T00:00:00"),
        cache_dir,
    )
    state_path = fundamentals_update.raw_cache_refresh_state_path(
        cache_dir
    )
    state_path.write_text(json.dumps({
        "OFFICIAL404": {
            "cache_status": "companyfacts_not_available",
            "cache_failure_reason": "HTTP Error 404: Not Found",
        },
        "TIMEOUT": {
            "cache_status": "fetch_failed",
            "cache_failure_reason": "timed out",
        },
    }), encoding="utf-8")
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)

    with pytest.raises(RuntimeError, match=r"1 missing.*TIMEOUT"):
        fundamentals_update.reparse_companyfacts_cache(
            cache_dir,
            annual_output,
            quarterly_output,
            required_symbols={"ABC", "OFFICIAL404", "TIMEOUT"},
        )

    result = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        required_symbols={"ABC", "OFFICIAL404"},
    )

    assert result["mode"] == "offline_cache_full_rebuild"
    assert result["known_unavailable_required_symbols"] == ["OFFICIAL404"]
    assert result["unresolved_cache_symbol_count"] == 0


def test_companyfacts_full_reparse_requires_existing_historical_tickers(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    universe_output = tmp_path / "universe.csv"
    existing = pd.DataFrame([{
        "ticker": "HISTORICAL",
        "fiscal_end": "2020-03-31",
        "available_date": "2020-05-01",
        "metric": "net_income",
        "value": 1,
        "taxonomy": "us-gaap",
        "concept": "Old",
        "form": "10-Q",
        "accession": "old",
        "fetched_at": "2020-05-01",
    }])
    existing.to_csv(annual_output, index=False)
    existing.to_csv(quarterly_output, index=False)
    annual_before = annual_output.read_bytes()
    quarterly_before = quarterly_output.read_bytes()
    pd.DataFrame([{
        "Symbol": "ABC",
        "Name": "ABC Common Stock",
    }]).to_csv(universe_output, index=False)
    monkeypatch.setattr(
        fundamentals_update,
        "NASDAQ_300M_STOCK_LIST_FILE",
        universe_output,
    )
    fundamentals_update._write_companyfacts_cache(
        "ABC",
        123,
        {"facts": {"us-gaap": {}}},
        pd.Timestamp("2026-07-30T00:00:00"),
        cache_dir,
    )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)

    with pytest.raises(
        RuntimeError, match=r"incomplete.*1 missing.*HISTORICAL"
    ):
        fundamentals_update.reparse_companyfacts_cache(
            cache_dir,
            annual_output,
            quarterly_output,
            required_symbols={"ABC", "HISTORICAL"},
        )

    assert annual_output.read_bytes() == annual_before
    assert quarterly_output.read_bytes() == quarterly_before


def test_companyfacts_cache_incremental_reparse_non_destructively_upserts(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2025-01-01",
                end="2025-03-31",
                filed="2025-05-01",
                form="10-Q",
                fp="Q1",
            )
        ]}},
    }}}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(
        fundamentals_update,
        "urlopen",
        lambda *_args, **_kwargs: Response(json.dumps(payload).encode()),
    )
    fundamentals_update.fetch_sec_fundamentals(
        "ABC", 123, retries=1, cache_dir=cache_dir
    )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)
    existing = pd.DataFrame([
        {
            "ticker": ticker,
            "fiscal_end": "2020-03-31",
            "available_date": "2020-05-01",
            "metric": "net_income",
            "value": value,
            "taxonomy": "us-gaap",
            "concept": "Old",
            "form": "10-Q",
            "accession": f"old-{ticker}",
            "fetched_at": "2020-05-01",
        }
        for ticker, value in (("ABC", 1), ("KEEP", 2))
    ])
    existing.to_csv(annual_output, index=False)
    existing.to_csv(quarterly_output, index=False)

    result = fundamentals_update.reparse_companyfacts_cache(
        cache_dir, annual_output, quarterly_output, tickers=["abc"]
    )

    rebuilt = pd.read_csv(quarterly_output)
    assert set(rebuilt["ticker"]) == {"ABC", "KEEP"}
    assert rebuilt.loc[rebuilt["ticker"].eq("ABC"), "value"].tolist() == [
        1.0,
        10.0,
    ]
    assert rebuilt.loc[rebuilt["ticker"].eq("KEEP"), "value"].tolist() == [2.0]
    assert result["mode"] == "offline_cache_incremental_rebuild"
    assert result["merge_policy"] == "non_destructive_upsert"
    assert result["requested_tickers"] == ["ABC"]

    annual_before = annual_output.read_bytes()
    quarterly_before = quarterly_output.read_bytes()
    repeated = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        tickers=["abc"],
        skip_unchanged=True,
    )

    assert repeated["mode"] == "offline_cache_incremental_noop"
    assert repeated["unchanged_tickers"] == ["ABC"]
    assert not repeated["parsed_outputs_written"]
    assert annual_output.read_bytes() == annual_before
    assert quarterly_output.read_bytes() == quarterly_before


def test_companyfacts_reparse_fingerprint_changes_with_payload(tmp_path):
    fundamentals_update._write_companyfacts_cache(
        "ABC",
        123,
        {"facts": {"us-gaap": {}}},
        pd.Timestamp("2026-07-30T00:00:00"),
        tmp_path,
    )
    fundamentals_update.write_companyfacts_cache_manifest(tmp_path)

    changed, unchanged = (
        fundamentals_update.select_changed_companyfacts_reparse_tickers(
            ["ABC"], tmp_path
        )
    )
    assert changed == ["ABC"]
    assert unchanged == []
    fundamentals_update.record_companyfacts_reparse_state(
        ["ABC"], tmp_path
    )
    changed, unchanged = (
        fundamentals_update.select_changed_companyfacts_reparse_tickers(
            ["ABC"], tmp_path
        )
    )
    assert changed == []
    assert unchanged == ["ABC"]

    fundamentals_update._write_companyfacts_cache(
        "ABC",
        123,
        {"facts": {"us-gaap": {"Changed": {}}}},
        pd.Timestamp("2026-07-31T00:00:00"),
        tmp_path,
    )
    changed, unchanged = (
        fundamentals_update.select_changed_companyfacts_reparse_tickers(
            ["ABC"], tmp_path
        )
    )
    assert changed == ["ABC"]
    assert unchanged == []


def test_companyfacts_reparse_fingerprint_scopes_foreign_registry_by_ticker(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    registry = tmp_path / "validated.csv"
    for ticker, cik in (("ABC", 123), ("SAFE", 456)):
        fundamentals_update._write_companyfacts_cache(
            ticker,
            cik,
            {"facts": {"us-gaap": {}}},
            pd.Timestamp("2026-07-30T00:00:00"),
            cache_dir,
        )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)
    pd.DataFrame([{
        "ticker": "SAFE",
        "cik": 456,
        "currency": "EUR",
        "validation_rule": "strict_v1",
    }]).to_csv(registry, index=False)
    monkeypatch.setattr(
        fundamentals_update,
        "VALIDATED_FOREIGN_QUARTERLY_FILE",
        registry,
    )
    fundamentals_update.record_companyfacts_reparse_state(
        ["ABC", "SAFE"], cache_dir
    )

    updated = pd.read_csv(registry)
    updated.loc[0, "validation_rule"] = "strict_v2"
    updated.to_csv(registry, index=False)

    changed, unchanged = (
        fundamentals_update.select_changed_companyfacts_reparse_tickers(
            ["ABC", "SAFE"], cache_dir
        )
    )
    assert changed == ["SAFE"]
    assert unchanged == ["ABC"]


def test_companyfacts_reparse_fingerprint_scopes_foreign_parser_by_ticker(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    registry = tmp_path / "validated.csv"
    payload = {"facts": {"us-gaap": {}}}
    fundamentals_update._write_companyfacts_cache(
        "DOMESTIC",
        123,
        payload,
        pd.Timestamp("2026-07-30T00:00:00Z"),
        cache_dir,
    )
    fundamentals_update._write_companyfacts_cache(
        "FOREIGN",
        456,
        payload,
        pd.Timestamp("2026-07-30T00:00:00Z"),
        cache_dir,
    )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)
    pd.DataFrame([
        {
            "ticker": "FOREIGN",
            "cik": 456,
            "currency": "USD",
            "validation_rule": "test",
        }
    ]).to_csv(registry, index=False)
    monkeypatch.setattr(
        fundamentals_update,
        "VALIDATED_FOREIGN_QUARTERLY_FILE",
        registry,
    )

    before = fundamentals_update._companyfacts_reparse_fingerprints(
        ["DOMESTIC", "FOREIGN"], cache_dir
    )
    changed_recipe = json.loads(json.dumps(
        fundamentals_update.companyfacts_full_rebuild_recipe()
    ))
    changed_recipe["foreign_quarterly_parser"]["sha256"] = "f" * 64
    monkeypatch.setattr(
        fundamentals_update,
        "companyfacts_full_rebuild_recipe",
        lambda: changed_recipe,
    )

    after = fundamentals_update._companyfacts_reparse_fingerprints(
        ["DOMESTIC", "FOREIGN"], cache_dir
    )

    assert before["DOMESTIC"]["fingerprint"] == after["DOMESTIC"]["fingerprint"]
    assert before["FOREIGN"]["fingerprint"] != after["FOREIGN"]["fingerprint"]
    assert after["DOMESTIC"]["foreign_parser_sha256"] is None
    assert after["FOREIGN"]["foreign_parser_sha256"] == "f" * 64


def test_incremental_reparse_reports_resumable_batch_progress(tmp_path):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    tickers = ["AAA", "BBB", "CCC"]
    for offset, ticker in enumerate(tickers, start=1):
        fundamentals_update._write_companyfacts_cache(
            ticker,
            offset,
            {"facts": {"us-gaap": {}}},
            pd.Timestamp("2026-07-30T00:00:00"),
            cache_dir,
        )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)
    fundamentals_update.record_companyfacts_reparse_state(
        ["AAA"], cache_dir
    )

    first = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        tickers=tickers,
        skip_unchanged=True,
        limit=1,
    )

    assert first["requested_tickers"] == ["BBB"]
    assert first["candidate_ticker_count"] == 3
    assert first["changed_ticker_count"] == 2
    assert first["selected_ticker_count"] == 1
    assert first["unchanged_ticker_count"] == 1
    assert first["deferred_changed_ticker_count"] == 1
    assert first["next_deferred_ticker"] == "CCC"
    assert first["batch_complete"] is False

    second = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        tickers=tickers,
        skip_unchanged=True,
        limit=1,
    )

    assert second["requested_tickers"] == ["CCC"]
    assert second["changed_ticker_count"] == 1
    assert second["selected_ticker_count"] == 1
    assert second["unchanged_ticker_count"] == 2
    assert second["deferred_changed_ticker_count"] == 0
    assert second["next_deferred_ticker"] is None
    assert second["batch_complete"] is True


def test_companyfacts_cache_reparse_rejects_manifest_mismatch_before_writing(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _fact(
                10,
                "2025-01-01",
                end="2025-03-31",
                filed="2025-05-01",
                form="10-Q",
                fp="Q1",
            )
        ]}},
    }}}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(
        fundamentals_update,
        "urlopen",
        lambda *_args, **_kwargs: Response(json.dumps(payload).encode()),
    )
    fundamentals_update.fetch_sec_fundamentals(
        "ABC", 123, retries=1, cache_dir=cache_dir
    )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)
    original = pd.DataFrame([{
        "ticker": "KEEP",
        "fiscal_end": "2020-03-31",
        "available_date": "2020-05-01",
        "metric": "net_income",
        "value": 2,
        "taxonomy": "us-gaap",
        "concept": "Old",
        "form": "10-Q",
        "accession": "old-keep",
        "fetched_at": "2020-05-01",
    }])
    original.to_csv(annual_output, index=False)
    original.to_csv(quarterly_output, index=False)
    annual_before = annual_output.read_bytes()
    quarterly_before = quarterly_output.read_bytes()
    envelope = _read_cache_envelope(cache_dir)
    envelope["symbols"].append("TAMPERED")
    _write_cache_envelope(cache_dir, envelope)

    with pytest.raises(RuntimeError, match="integrity mismatch"):
        fundamentals_update.reparse_companyfacts_cache(
            cache_dir,
            annual_output,
            quarterly_output,
            tickers=["ABC"],
        )

    assert annual_output.read_bytes() == annual_before
    assert quarterly_output.read_bytes() == quarterly_before


def test_incremental_reparse_scopes_hashes_but_full_detects_other_tampering(
    tmp_path,
):
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    for ticker, cik in (("ABC", 123), ("XYZ", 456)):
        fundamentals_update._write_companyfacts_cache(
            ticker,
            cik,
            {"facts": {"us-gaap": {}}},
            pd.Timestamp("2026-07-30T00:00:00"),
            cache_dir,
        )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)
    xyz_envelope = _read_cache_envelope(cache_dir, cik=456)
    xyz_envelope["symbols"].append("TAMPERED")
    _write_cache_envelope(cache_dir, xyz_envelope, cik=456)

    incremental = fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        tickers=["ABC"],
    )

    assert incremental["cache_manifest_verification_scope"] == (
        "selected_payloads"
    )
    with pytest.raises(RuntimeError, match="CIK0000000456"):
        fundamentals_update.reparse_companyfacts_cache(
            cache_dir,
            annual_output,
            quarterly_output,
            required_symbols={"ABC", "XYZ"},
        )


def test_companyfacts_cache_lock_rejects_overlapping_writer(tmp_path):
    with fundamentals_update.companyfacts_cache_lock(tmp_path):
        with pytest.raises(RuntimeError, match="Timed out waiting"):
            with fundamentals_update.companyfacts_cache_lock(
                tmp_path, timeout_seconds=0.01
            ):
                pytest.fail("overlapping writer acquired the cache lock")


def test_companyfacts_cache_incremental_reparse_missing_ticker_keeps_outputs(
    tmp_path,
):
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    original = pd.DataFrame([{
        "ticker": "KEEP",
        "fiscal_end": "2020-03-31",
        "available_date": "2020-05-01",
        "metric": "net_income",
        "value": 2,
        "taxonomy": "us-gaap",
        "concept": "Old",
        "form": "10-Q",
        "accession": "old-keep",
        "fetched_at": "2020-05-01",
    }])
    original.to_csv(annual_output, index=False)
    original.to_csv(quarterly_output, index=False)
    annual_before = annual_output.read_bytes()
    quarterly_before = quarterly_output.read_bytes()

    with pytest.raises(RuntimeError, match="MISSING"):
        fundamentals_update.reparse_companyfacts_cache(
            tmp_path / "cache",
            annual_output,
            quarterly_output,
            tickers=["MISSING"],
        )

    assert annual_output.read_bytes() == annual_before
    assert quarterly_output.read_bytes() == quarterly_before


def _validated_foreign_payload():
    revenue = []
    income = []
    for year in (2023, 2024):
        ends = [
            f"{year}-03-31",
            f"{year}-06-30",
            f"{year}-09-30",
            f"{year}-12-31",
        ]
        cumulative = [10, 30, 60, 100]
        for end, value in zip(ends, cumulative):
            form = "20-F" if end.endswith("12-31") else "6-K"
            filed = (
                f"{year + 1}-03-01"
                if form == "20-F"
                else (
                    pd.Timestamp(end) + pd.Timedelta(days=30)
                ).strftime("%Y-%m-%d")
            )
            kwargs = {
                "end": end,
                "filed": filed,
                "form": form,
                "fp": "FY" if form == "20-F" else "Q2",
            }
            revenue.append(_fact(value, f"{year}-01-01", **kwargs))
            income.append(_fact(value / 10, f"{year}-01-01", **kwargs))
    return {
        "facts": {
            "ifrs-full": {
                "Revenue": {"units": {"EUR": revenue}},
                "ProfitLoss": {"units": {"EUR": income}},
            }
        }
    }


def test_validated_foreign_quarterly_parser_accepts_strict_history():
    frame = fundamentals_update.parse_validated_foreign_quarterly(
        "SAFE", 123, _validated_foreign_payload(), "2026-07-31"
    )

    assert set(frame["metric"]) == {"revenue", "net_income"}
    assert len(frame) == 16
    assert frame["concept"].str.startswith("foreign_").all()


def test_validated_foreign_quarters_require_incremental_reparse(tmp_path):
    with pytest.raises(
        ValueError, match="require incremental tickers"
    ):
        fundamentals_update.reparse_companyfacts_cache(
            tmp_path / "cache",
            tmp_path / "annual.csv",
            tmp_path / "quarterly.csv",
            tickers=None,
            required_symbols=set(),
            include_validated_foreign_quarters=True,
        )


def test_registered_foreign_parser_is_automatic(
    tmp_path, monkeypatch
):
    registry = tmp_path / "validated.csv"
    pd.DataFrame([{
        "ticker": "SAFE",
        "cik": 123,
        "currency": "EUR",
        "validation_rule": "test_v1",
    }]).to_csv(registry, index=False)
    monkeypatch.setattr(
        fundamentals_update,
        "VALIDATED_FOREIGN_QUARTERLY_FILE",
        registry,
    )

    frame, used_registry = (
        fundamentals_update.parse_registered_companyfacts_quarterly(
            "SAFE", 123, _validated_foreign_payload(), "2026-07-31"
        )
    )

    assert used_registry is True
    assert len(frame) == 16
    assert frame["concept"].str.startswith("foreign_").all()


def test_registered_foreign_parser_rejects_cik_change(
    tmp_path, monkeypatch
):
    registry = tmp_path / "validated.csv"
    pd.DataFrame([{
        "ticker": "SAFE",
        "cik": 999,
        "currency": "EUR",
        "validation_rule": "test_v1",
    }]).to_csv(registry, index=False)
    monkeypatch.setattr(
        fundamentals_update,
        "VALIDATED_FOREIGN_QUARTERLY_FILE",
        registry,
    )

    with pytest.raises(RuntimeError, match="registry CIK 999"):
        fundamentals_update.parse_registered_companyfacts_quarterly(
            "SAFE", 123, _validated_foreign_payload(), "2026-07-31"
        )


def test_merge_fundamentals_has_canonical_tie_order():
    rows = pd.DataFrame([
        {
            "ticker": "ABC",
            "fiscal_end": "2025-03-31",
            "available_date": "2025-05-01",
            "metric": "revenue",
            "value": 2,
            "taxonomy": "us-gaap",
            "concept": "Revenue",
            "form": "10-Q",
            "accession": accession,
            "fetched_at": "2026-07-31",
        }
        for accession in ("z-accession", "a-accession")
    ])

    first = fundamentals_update.merge_fundamentals(
        pd.DataFrame(columns=rows.columns), rows
    )
    second = fundamentals_update.merge_fundamentals(
        pd.DataFrame(columns=rows.columns), rows.iloc[::-1]
    )

    pd.testing.assert_frame_equal(first, second)
    assert first["accession"].tolist() == ["a-accession", "z-accession"]


def test_companyfacts_offline_cache_rejects_corrupt_envelope(tmp_path):
    (tmp_path / "CIK0000000123.json").write_text(
        '{"cik":123,"payload":',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="cache unavailable or invalid"):
        fundamentals_update.fetch_sec_fundamentals(
            "ABC",
            123,
            retries=1,
            cache_dir=tmp_path,
            offline_cache=True,
        )


def test_quarterly_parser_accepts_bank_revenue_net_of_interest_expense():
    bank_revenue = _fact(
        250_000_000,
        "2025-01-01",
        end="2025-03-31",
        filed="2025-05-01",
        form="10-Q",
        fp="Q1",
    )
    payload = {"facts": {"us-gaap": {
        "RevenuesNetOfInterestExpense": {
            "units": {"USD": [bank_revenue]}
        },
    }}}

    frame = parse_companyfacts_quarterly("BANK", payload)

    revenue = frame.loc[frame["metric"].eq("revenue")].iloc[0]
    assert revenue["value"] == 250_000_000
    assert revenue["concept"] == "RevenuesNetOfInterestExpense"
    assert revenue["available_date"] == pd.Timestamp("2025-05-01")


def test_quarterly_parser_accepts_bdc_gross_investment_income():
    investment_income = _fact(
        250_000_000,
        "2025-01-01",
        end="2025-03-31",
        filed="2025-05-01",
        form="10-Q",
        fp="Q1",
    )
    payload = {"facts": {"us-gaap": {
        "GrossInvestmentIncomeOperating": {
            "units": {"USD": [investment_income]}
        },
    }}}

    frame = parse_companyfacts_quarterly("BDC", payload)

    revenue = frame.loc[frame["metric"].eq("revenue")].iloc[0]
    assert revenue["value"] == 250_000_000
    assert revenue["concept"] == "GrossInvestmentIncomeOperating"


def test_quarterly_parser_derives_bank_revenue_from_same_filing_components():
    net_interest = _fact(
        180_000_000,
        "2025-01-01",
        end="2025-03-31",
        filed="2025-05-01",
        form="10-Q",
        fp="Q1",
    )
    noninterest = {
        **net_interest,
        "val": 70_000_000,
    }
    payload = {"facts": {"us-gaap": {
        "InterestIncomeExpenseNet": {
            "units": {"USD": [net_interest]}
        },
        "NoninterestIncome": {
            "units": {"USD": [noninterest]}
        },
    }}}

    frame = parse_companyfacts_quarterly("BANK", payload)

    revenue = frame.loc[frame["metric"].eq("revenue")].iloc[0]
    assert revenue["value"] == 250_000_000
    assert revenue["concept"] == (
        "derived_bank_revenue:"
        "InterestIncomeExpenseNet+NoninterestIncome"
    )
    assert revenue["available_date"] == pd.Timestamp("2025-05-01")


def test_cache_reparse_retains_derived_bank_revenue(tmp_path):
    annual_net = _fact(
        700_000_000,
        "2024-01-01",
        end="2024-12-31",
        filed="2025-02-20",
        form="10-K",
        fp="FY",
    )
    annual_noninterest = {**annual_net, "val": 300_000_000}
    quarterly_net = _fact(
        180_000_000,
        "2025-01-01",
        end="2025-03-31",
        filed="2025-05-01",
        form="10-Q",
        fp="Q1",
    )
    quarterly_noninterest = {**quarterly_net, "val": 70_000_000}
    payload = {"facts": {"us-gaap": {
        "InterestIncomeExpenseNet": {"units": {"USD": [annual_net, quarterly_net]}},
        "NoninterestIncome": {
            "units": {"USD": [annual_noninterest, quarterly_noninterest]}
        },
    }}}
    cache_dir = tmp_path / "cache"
    annual_output = tmp_path / "annual.csv"
    quarterly_output = tmp_path / "quarterly.csv"
    fundamentals_update._write_companyfacts_cache(
        "BANK", 123, payload, pd.Timestamp("2026-08-09T00:00:00"), cache_dir
    )
    fundamentals_update.write_companyfacts_cache_manifest(cache_dir)

    fundamentals_update.reparse_companyfacts_cache(
        cache_dir,
        annual_output,
        quarterly_output,
        tickers=["BANK"],
    )

    annual = pd.read_csv(annual_output)
    quarterly = pd.read_csv(quarterly_output)
    annual_revenue = annual.loc[annual["metric"].eq("revenue")].iloc[0]
    quarterly_revenue = quarterly.loc[quarterly["metric"].eq("revenue")].iloc[0]
    assert annual_revenue["value"] == 1_000_000_000
    assert quarterly_revenue["value"] == 250_000_000
    assert annual_revenue["concept"].startswith("derived_bank_revenue:")
    assert quarterly_revenue["concept"].startswith("derived_bank_revenue:")


def test_quarterly_parser_prefers_reported_total_over_derived_bank_revenue():
    total = _fact(
        260_000_000,
        "2025-01-01",
        end="2025-03-31",
        filed="2025-05-01",
        form="10-Q",
        fp="Q1",
    )
    net_interest = {**total, "val": 180_000_000}
    noninterest = {**total, "val": 70_000_000}
    payload = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [total]}},
        "InterestIncomeExpenseNet": {
            "units": {"USD": [net_interest]}
        },
        "NoninterestIncome": {
            "units": {"USD": [noninterest]}
        },
    }}}

    frame = parse_companyfacts_quarterly("BANK", payload)

    revenue = frame.loc[frame["metric"].eq("revenue")].iloc[0]
    assert revenue["value"] == 260_000_000
    assert revenue["concept"] == "Revenues"


def test_quarterly_parser_uses_common_stockholder_income_as_fallback():
    common_income = _fact(
        42_000_000,
        "2025-01-01",
        end="2025-03-31",
        filed="2025-05-01",
        form="10-Q",
        fp="Q1",
    )
    payload = {"facts": {"us-gaap": {
        "NetIncomeLossAvailableToCommonStockholdersBasic": {
            "units": {"USD": [common_income]}
        },
    }}}

    frame = parse_companyfacts_quarterly("BANK", payload)

    income = frame.loc[frame["metric"].eq("net_income")].iloc[0]
    assert income["value"] == 42_000_000
    assert (
        income["concept"]
        == "NetIncomeLossAvailableToCommonStockholdersBasic"
    )


def test_quarterly_parser_prefers_company_income_over_common_fallback():
    company_income = _fact(
        50_000_000,
        "2025-01-01",
        end="2025-03-31",
        filed="2025-05-01",
        form="10-Q",
        fp="Q1",
    )
    common_income = {**company_income, "val": 42_000_000}
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [company_income]}},
        "NetIncomeLossAvailableToCommonStockholdersBasic": {
            "units": {"USD": [common_income]}
        },
    }}}

    frame = parse_companyfacts_quarterly("BANK", payload)

    income = frame.loc[frame["metric"].eq("net_income")].iloc[0]
    assert income["value"] == 50_000_000
    assert income["concept"] == "NetIncomeLoss"


def test_fundamentals_coverage_requires_all_core_metrics_to_be_fresh():
    metrics = ["net_income", "assets", "equity", "operating_cash_flow"]
    frame = pd.DataFrame([
        {"ticker": ticker, "metric": metric, "available_date": pd.Timestamp(available)}
        for ticker, available, selected in (
            ("A", "2026-02-15", metrics),
            ("B", "2026-02-15", metrics[:-1]),
            ("C", "2024-01-01", metrics),
        )
        for metric in selected
    ])
    audit = audit_fundamentals_coverage(frame, ["A", "B", "C"], pd.Timestamp("2026-07-17").date())
    assert audit["fresh_complete_tickers"] == 1
    assert audit["fresh_complete_coverage"] == 1 / 3


def test_quarterly_parser_keeps_filing_dates_and_derives_fourth_quarter():
    def quarter(value, start, end, filed, frame, accn):
        return {
            "val": value, "start": start, "end": end, "filed": filed,
            "form": "10-Q", "fp": frame[-2:], "frame": frame, "accn": accn,
        }

    revenue_quarters = [
        quarter(20, "2025-01-01", "2025-03-31", "2025-05-01", "CY2025Q1", "q1"),
        quarter(25, "2025-04-01", "2025-06-30", "2025-08-01", "CY2025Q2", "q2"),
        quarter(30, "2025-07-01", "2025-09-30", "2025-11-01", "CY2025Q3", "q3"),
        _fact(110, "2025-01-01", end="2025-12-31", filed="2026-02-15"),
    ]
    revenue_quarters[0]["frame"] = None
    revenue_quarters.append({
        **revenue_quarters[0], "filed": "2026-05-01", "fy": 2026,
        "accn": "q1-comparative", "frame": "CY2025Q1",
    })
    income_quarters = [
        quarter(2, "2025-01-01", "2025-03-31", "2025-05-01", "CY2025Q1", "iq1"),
        quarter(3, "2025-04-01", "2025-06-30", "2025-08-01", "CY2025Q2", "iq2"),
        quarter(4, "2025-07-01", "2025-09-30", "2025-11-01", "CY2025Q3", "iq3"),
        _fact(15, "2025-01-01", end="2025-12-31", filed="2026-02-15"),
    ]
    # Company Facts can expose one reported quarter with adjacent 52/53-week
    # fiscal-end coordinates.  It must still count as one Q3 operand.
    income_quarters.insert(3, {
        **income_quarters[2], "end": "2025-10-01",
    })
    payload = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": revenue_quarters}},
        "NetIncomeLoss": {"units": {"USD": income_quarters}},
    }}}
    frame = parse_companyfacts_quarterly("abc", payload, fetched_at="2026-07-18")
    q1 = frame.loc[
        (frame["fiscal_end"] == pd.Timestamp("2025-03-31"))
        & (frame["metric"] == "revenue")
    ]
    assert q1["available_date"].min() == pd.Timestamp("2025-05-01")
    q4 = frame.loc[frame["fiscal_end"] == pd.Timestamp("2025-12-31")].set_index("metric")
    assert q4.loc["revenue", "value"] == 35
    assert q4.loc["net_income", "value"] == 6
    assert set(q4["available_date"]) == {pd.Timestamp("2026-02-15")}


def test_quarterly_parser_derives_q2_and_q3_from_cumulative_ytd():
    q1 = _fact(
        2,
        "2025-01-01",
        end="2025-03-31",
        filed="2025-05-01",
        form="10-Q",
        fp="Q1",
    )
    q1["accn"] = "q1"
    q2_ytd = _fact(
        5,
        "2025-01-01",
        end="2025-06-30",
        filed="2025-08-01",
        form="10-Q",
        fp="Q2",
    )
    q2_ytd["accn"] = "q2"
    q3_ytd = _fact(
        9,
        "2025-01-01",
        end="2025-09-30",
        filed="2025-11-01",
        form="10-Q",
        fp="Q3",
    )
    q3_ytd["accn"] = "q3"
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {
            "units": {"USD": [q1, q2_ytd, q3_ytd]}
        },
    }}}

    frame = parse_companyfacts_quarterly("ABC", payload)
    income = frame.loc[frame["metric"].eq("net_income")].set_index(
        "fiscal_end"
    )

    assert income.loc[pd.Timestamp("2025-03-31"), "value"] == 2
    assert income.loc[pd.Timestamp("2025-06-30"), "value"] == 3
    assert income.loc[pd.Timestamp("2025-09-30"), "value"] == 4
    assert income.loc[
        pd.Timestamp("2025-06-30"), "available_date"
    ] == pd.Timestamp("2025-08-01")
    assert income.loc[
        pd.Timestamp("2025-09-30"), "concept"
    ] == "derived_ytd:NetIncomeLoss"


def test_quarterly_parser_rejects_q1_filing_mistagged_as_prior_q3_ytd():
    prior_h1 = _fact(
        153_747_000,
        "2024-01-01",
        end="2024-06-30",
        filed="2024-08-06",
        form="10-Q",
        fp="Q2",
    )
    prior_h1["accn"] = "q2"
    malformed_prior_q3 = _fact(
        107_979_000,
        "2024-01-01",
        end="2024-09-30",
        filed="2025-05-08",
        form="10-Q",
        fp="Q1",
    )
    malformed_prior_q3["accn"] = "q1-current-filing"
    current_q1 = _fact(
        107_979_000,
        "2025-01-01",
        end="2025-03-31",
        filed="2025-05-08",
        form="10-Q",
        fp="Q1",
    )
    current_q1["accn"] = "q1-current-filing"
    payload = {"facts": {"us-gaap": {
        "Revenues": {
            "units": {"USD": [prior_h1, malformed_prior_q3, current_q1]}
        },
    }}}

    frame = parse_companyfacts_quarterly("DAVE", payload)

    malformed = frame.loc[
        frame["fiscal_end"].eq(pd.Timestamp("2024-09-30"))
        & frame["concept"].eq("derived_ytd:Revenues")
    ]
    assert malformed.empty
    assert not frame["value"].eq(-45_768_000).any()


def test_quarterly_parser_accepts_comparative_quarters_embedded_in_10k():
    fact = _fact(
        2_460_000,
        "2011-01-01",
        end="2011-03-31",
        filed="2013-03-15",
        form="10-K",
        fp="FY",
    )
    fact["frame"] = "CY2011Q1"
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [fact]}}
    }}}

    frame = parse_companyfacts_quarterly("ACNB", payload)

    assert len(frame) == 1
    assert frame.iloc[0]["fiscal_end"] == pd.Timestamp("2011-03-31")
    assert frame.iloc[0]["value"] == 2_460_000


def test_quarterly_parser_accepts_quarter_length_10q_facts_mislabeled_fy():
    comparative = _fact(
        29_669_000,
        "2020-01-01",
        end="2020-03-31",
        filed="2021-05-11",
        form="10-Q",
        fp="FY",
    )
    comparative["frame"] = "CY2020Q1"
    current = _fact(
        57_006_000,
        "2021-01-01",
        end="2021-03-31",
        filed="2021-05-11",
        form="10-Q",
        fp="FY",
    )
    payload = {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [comparative, current]}}
    }}}

    frame = parse_companyfacts_quarterly("BSY", payload)
    income = frame.loc[frame["metric"].eq("net_income")].set_index(
        "fiscal_end"
    )

    assert income.loc[pd.Timestamp("2020-03-31"), "value"] == 29_669_000
    assert income.loc[pd.Timestamp("2021-03-31"), "value"] == 57_006_000


def test_quarterly_growth_snapshot_uses_only_available_complete_years():
    ends = pd.date_range("2024-03-31", periods=8, freq="QE")
    rows = []
    for index, end in enumerate(ends):
        for metric, value in (("revenue", 100 + 10 * index), ("net_income", 10 + index)):
            rows.append({
                "ticker": "ABC", "fiscal_end": end,
                "available_date": end + pd.Timedelta(days=40),
                "metric": metric, "value": value,
            })
    frame = pd.DataFrame(rows)
    early = quarterly_growth_snapshot(frame, pd.Timestamp("2025-11-01"))
    assert early.empty
    snapshot = quarterly_growth_snapshot(frame, pd.Timestamp("2026-05-20"))
    assert snapshot.loc["ABC", "revenue_growth"] > 0
    assert snapshot.loc["ABC", "net_income_growth"] > 0


def test_quarterly_growth_snapshot_returns_empty_when_metric_is_missing():
    ends = pd.date_range("2020-03-31", periods=8, freq="QE")
    frame = pd.DataFrame([
        {
            "ticker": "BSGM",
            "fiscal_end": end,
            "available_date": end + pd.Timedelta(days=40),
            "metric": "net_income",
            "value": 10 + index,
        }
        for index, end in enumerate(ends)
    ])

    snapshot = quarterly_growth_snapshot(frame, pd.Timestamp("2022-12-31"))

    assert snapshot.empty
