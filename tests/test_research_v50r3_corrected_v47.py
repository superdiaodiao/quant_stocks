from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess

import pandas as pd
import pytest

from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50_corrected_v47 as r1
from scripts import research_v50r2_corrected_v47 as r2
from scripts import research_v50r3_corrected_v47 as r3
from src.io import fundamentals_update


SIGNAL_POLICY = {
    "first_prospective_signal_date": "2026-09-30",
    "missed_signal_dates": ["2026-08-31"],
}


def _at(text: str) -> datetime:
    return pd.Timestamp(text).to_pydatetime()


def _fact(ticker: str, available: str, value: float) -> dict:
    return {
        "ticker": ticker,
        "fiscal_end": "2026-06-30",
        "available_date": available,
        "metric": "revenue",
        "value": value,
        "taxonomy": "us-gaap",
        "concept": "Revenues",
        "form": "10-Q",
        "accession": f"0000000000-26-{int(value):06d}",
        "fetched_at": "2026-09-30T21:00:00",
    }


def test_r3_reuses_the_frozen_model_and_leaves_r1_r2_untouched() -> None:
    assert r3._selected_model() == r1._selected_model() == r2._selected_model()
    assert r3.MODEL_VERSION not in {r1.MODEL_VERSION, r2.MODEL_VERSION}
    assert r3.SUPERSEDED_MODEL_VERSION == r2.MODEL_VERSION
    assert r3.OUTPUT_DIR not in {r1.OUTPUT_DIR, r2.OUTPUT_DIR}
    assert r3.EARLIEST_PROSPECTIVE_SIGNAL_DATE == pd.Timestamp("2026-09-30")
    assert r3.PRIOR_MISSED_SIGNAL_DATES == (pd.Timestamp("2026-08-31"),)
    r2_protocol = json.loads(
        (r3.REPO_ROOT / r2.PROTOCOL_PATH).read_text(encoding="utf-8")
    )
    for name in ("runner", "r1_runner", "v42_calculation_core", "v43_runtime_core"):
        binding = r2_protocol["input_bindings"][name]
        assert r3._sha256(binding["path"]) == binding["sha256"], name
    spec = r3.runtime_repair_specification()
    assert spec["new_threshold_search"] is False
    assert spec["invented_cik_allowed"] is False
    assert spec["missed_signal_backfill_allowed"] is False
    assert spec["missed_signal_window"].startswith("one_catch_up_as_of")


@pytest.fixture
def isolated_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the real SEC refresher with the network replaced."""
    universe = tmp_path / "current_universe.csv"
    pd.DataFrame({
        "Symbol": ["AAPL", "HIFS", "NA"],
        "Name": [
            "Apple Inc. Common Stock",
            "Hingham Institution for Savings Common Stock",
            "Nano Labs Ltd Class A Ordinary Shares",
        ],
    }).to_csv(universe, index=False)
    work = tmp_path / "work"
    fetched: list[list[str]] = []
    map_calls: list[int] = []

    def fake_map() -> dict[str, int]:
        map_calls.append(1)
        # HIFS files with the FDIC and has no SEC CIK.
        return {"AAPL": 320193, "NA": 1937441}

    def fake_fetch(symbols, cik, retries=3, cache_dir=None, offline_cache=False):
        fetched.append(list(symbols))
        results = {}
        for symbol in symbols:
            rows = [_fact(symbol, "2026-09-30", 100.0)]
            if symbol == "AAPL":
                # Accepted after the EDGAR cutoff: dated the next business day.
                rows.append(_fact(symbol, "2026-10-01", 101.0))
            frame = pd.DataFrame(rows, columns=fundamentals_update.OUTPUT_COLUMNS)
            results[symbol] = (frame.copy(), frame.copy())
        return results

    def fake_clone(source: Path, target: Path) -> dict:
        target.mkdir(parents=True, exist_ok=True)
        return {"status": "HARDLINK_CLONED_AND_VERIFIED", "target": str(target)}

    monkeypatch.setattr(fundamentals_update, "fetch_sec_ticker_map", fake_map)
    monkeypatch.setattr(
        fundamentals_update, "fetch_sec_fundamentals_for_symbols", fake_fetch
    )
    monkeypatch.setattr(v43, "_hardlink_clone_cache", fake_clone)
    monkeypatch.setattr(
        r3.v42, "_initialize_fundamental_work",
        lambda path: Path(path).mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(v43, "_formal_financial_bindings", lambda: {"x": "1"})
    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-09-30T21:00:00Z"))
    # One unmapped name in a three-name universe; real universes are ~3,500.
    monkeypatch.setattr(r3, "MAXIMUM_UNMAPPED_FRACTION", 0.5)
    return {
        "universe": universe,
        "work": work,
        "fetched": fetched,
        "map_calls": map_calls,
    }


def test_signal_refresh_keeps_unmapped_names_instead_of_aborting(
    isolated_refresh: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    tickers = ["AAPL", "HIFS", "NA"]
    legacy = isolated_refresh["work"].parent / "legacy"
    legacy.mkdir()
    for name, value in {
        "NASDAQ_300M_STOCK_LIST_FILE": isolated_refresh["universe"],
        "FUNDAMENTALS_REFRESH_STATE_FILE": legacy / "state.json",
        "FUNDAMENTALS_COVERAGE_FILE": legacy / "coverage.json",
        "QUARTERLY_FUNDAMENTALS_COVERAGE_FILE": legacy / "quarterly_coverage.json",
    }.items():
        monkeypatch.setattr(fundamentals_update, name, str(value))
    # The inherited v43 refresh passes every universe name explicitly and the
    # SEC refresher aborts on HIFS; this is the defect r3 repairs.
    with pytest.raises(ValueError, match="No SEC CIK mapping"):
        fundamentals_update.update_fundamentals(
            as_of=pd.Timestamp("2026-09-30").date(),
            workers=1,
            refresh_after_days=0,
            output=legacy / "legacy.csv",
            quarterly_output=legacy / "legacy_q.csv",
            force=True,
            tickers=tickers,
            cache_dir=legacy / "cache",
        )

    audit = r3._refresh_fundamentals_isolated(
        as_of=pd.Timestamp("2026-09-30"),
        universe_path=isolated_refresh["universe"],
        tickers=tickers,
        work=isolated_refresh["work"],
        workers=1,
    )

    policy = audit["sec_unmapped_policy"]
    assert policy["classification"] == r3.UNMAPPED_CLASSIFICATION
    assert policy["unmapped_tickers"] == ["HIFS"]
    assert policy["mapped_ticker_count"] == 2
    assert policy["invented_cik_count"] == 0
    assert policy["kept_in_signal_universe"] is True
    assert sorted(sum(isolated_refresh["fetched"], [])) == ["AAPL", "NA"]
    assert len(isolated_refresh["map_calls"]) == 2  # legacy call + one pinned r3 map
    assert audit["as_of"] == "2026-09-30"
    assert audit["parsed_outputs_written"] is True
    quarterly = pd.read_csv(
        isolated_refresh["work"] / "quarterly.csv", keep_default_na=False
    )
    assert pd.to_datetime(quarterly["available_date"]).le("2026-09-30").all()
    assert "NA" in set(quarterly["ticker"])
    trimmed = audit["as_of_filter"]["quarterly.csv"]
    assert trimmed["removed_future_rows"] == 1
    assert trimmed["removed_tickers"] == ["AAPL"]
    assert set(audit["refresh_elapsed_seconds"]) >= {
        "sec_ticker_map", "update_fundamentals", "as_of_filter"
    }
    json.dumps(audit, default=str)


def test_signal_refresh_refuses_a_broken_sec_map(
    isolated_refresh: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        fundamentals_update, "fetch_sec_ticker_map", lambda: {"AAPL": 320193}
    )
    tickers = ["AAPL"] + [f"X{index:03d}" for index in range(30)]
    with pytest.raises(RuntimeError, match="ticker map looks incomplete"):
        r3._refresh_fundamentals_isolated(
            as_of=pd.Timestamp("2026-09-30"),
            universe_path=isolated_refresh["universe"],
            tickers=tickers,
            work=isolated_refresh["work"],
            workers=1,
        )
    monkeypatch.setattr(fundamentals_update, "fetch_sec_ticker_map", lambda: {})
    with pytest.raises(RuntimeError, match="resolved none"):
        r3._refresh_fundamentals_isolated(
            as_of=pd.Timestamp("2026-09-30"),
            universe_path=isolated_refresh["universe"],
            tickers=["AAPL"],
            work=isolated_refresh["work"],
            workers=1,
        )


def test_signal_refresh_fails_fast_once_the_signal_window_closes(
    isolated_refresh: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refresh() -> dict:
        return r3._refresh_fundamentals_isolated(
            as_of=pd.Timestamp("2026-09-30"),
            universe_path=isolated_refresh["universe"],
            tickers=["AAPL"],
            work=isolated_refresh["work"],
            workers=1,
        )

    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-10-01T08:00:05Z"))
    with pytest.raises(RuntimeError, match="could never be frozen"):
        refresh()
    assert isolated_refresh["fetched"] == []
    # A rehearsal of a completed session is allowed to run after its window.
    with r3._runtime(rehearsal=True):
        audit = refresh()
    assert audit["sec_unmapped_policy"]["unmapped_tickers"] == []
    assert r3._REFRESH_OPTIONS["enforce_signal_window"] is True

    # The UTC date has rolled over, but pre-market has not opened yet.
    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-10-01T07:30:00Z"))
    assert refresh()["refresh_started_at"] == "2026-10-01T07:30:00+00:00"


def test_sec_ticker_map_fetch_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def flaky() -> dict[str, int]:
        calls.append(1)
        if len(calls) < 3:
            raise OSError("temporary SEC failure")
        return {"aapl": 320193}

    monkeypatch.setattr(fundamentals_update, "fetch_sec_ticker_map", flaky)
    monkeypatch.setattr(r3.time, "sleep", lambda _seconds: None)
    assert r3._fetch_sec_ticker_map() == {"AAPL": 320193}
    assert len(calls) == 3


def test_future_row_filter_keeps_every_other_byte(tmp_path: Path) -> None:
    columns = fundamentals_update.OUTPUT_COLUMNS
    rows = [
        _fact("NA", "2026-08-14", 5.0),
        _fact("AAPL", "2026-07-31", 1.25),
    ]
    path = tmp_path / "quarterly.csv"
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)
    original = path.read_bytes()

    summary = r3._trim_future_available_rows(tmp_path, pd.Timestamp("2026-09-30"))
    assert summary["quarterly.csv"]["removed_future_rows"] == 0
    assert path.read_bytes() == original
    assert summary["fundamentals.csv"] == {"present": False}

    pd.DataFrame(
        rows + [_fact("AAPL", "2026-10-01", 9.0)], columns=columns
    ).to_csv(path, index=False)
    summary = r3._trim_future_available_rows(tmp_path, pd.Timestamp("2026-09-30"))
    assert summary["quarterly.csv"]["removed_future_rows"] == 1
    assert path.read_bytes() == original


def test_stale_builds_are_quarantined_and_the_lock_is_exclusive(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    suffix = "2026-09-30_signal"
    stale = r3.stale_build_paths(work, suffix)
    for path in stale:
        path.mkdir(parents=True)
        (path / "marker").write_text("x", encoding="utf-8")

    moved = r3.quarantine_stale_builds(work, suffix)

    assert len(moved) == 3
    assert not any(path.exists() for path in stale)
    assert all(Path(item["to"]).joinpath("marker").is_file() for item in moved)
    assert r3.quarantine_stale_builds(work, suffix) == []

    lock = tmp_path / "staging.lock"
    with r3.staging_lock(lock):
        with r3.staging_lock(lock):  # re-entrant inside one run
            pass
    with lock.open("a+") as other:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(r3.StagingInProgress):
            with r3.staging_lock(lock):
                pass


def test_unverifiable_isolated_cache_is_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "work" / "fundamentals" / "companyfacts_cache"
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}", encoding="utf-8")
    calls = []

    def clone(source: Path, destination: Path) -> dict:
        calls.append(destination.exists())
        if destination.exists():
            raise RuntimeError("Company Facts cache manifest inventory mismatch")
        destination.mkdir(parents=True)
        return {"status": "HARDLINK_CLONED_AND_VERIFIED"}

    monkeypatch.setattr(v43, "_hardlink_clone_cache", clone)
    result = r3._clone_isolated_cache(tmp_path / "source", target)

    assert calls == [True, False]
    assert result["status"] == "HARDLINK_CLONED_AND_VERIFIED"
    moved = result["replaced_unverifiable_isolated_cache"]
    assert Path(moved[0]["to"]).joinpath("manifest.json").is_file()
    assert "failed_attempts" in moved[0]["to"]


def test_stage_bundle_validates_dates_then_recovers_and_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        r3, "_validated_protocol",
        lambda _path=None: ({"signal_policy": SIGNAL_POLICY}, "a" * 64),
    )
    kwargs = {
        "bundles_dir": tmp_path / "bundles",
        "work_dir": tmp_path / "work",
        "signals_dir": tmp_path / "signals",
        "ledger_path": tmp_path / "ledger.jsonl",
        "lock_path": tmp_path / "staging.lock",
    }
    with pytest.raises(RuntimeError, match="never backfilled"):
        r3.stage_bundle(
            as_of="2026-08-31", purpose="SIGNAL",
            observed_at=_at("2026-08-31T22:00:00Z"), **kwargs,
        )
    for outside in ("2026-09-30T20:10:00Z", "2026-10-01T08:10:00Z"):
        with pytest.raises(RuntimeError, match="only inside its window"):
            r3.stage_bundle(
                as_of="2026-09-30", purpose="SIGNAL",
                observed_at=_at(outside), **kwargs,
            )

    stale = r3.stale_build_paths(kwargs["work_dir"], "2026-09-30_signal")[0]
    stale.mkdir(parents=True)
    seen = {}

    def fake_stage(**stage_kwargs):
        seen["stale_present"] = stale.exists()
        seen["model"] = v43.MODEL_VERSION
        seen["refresh"] = v43._refresh_fundamentals_isolated
        seen["kwargs"] = stage_kwargs
        return {"status": "FROZEN_ISOLATED_INPUT_BUNDLE"}

    monkeypatch.setattr(v43, "stage_bundle", fake_stage)
    result = r3.stage_bundle(
        as_of="2026-09-30", purpose="SIGNAL",
        observed_at=_at("2026-10-01T02:00:00Z"), **kwargs,
    )

    assert seen["stale_present"] is False
    assert seen["model"] == r3.MODEL_VERSION
    assert seen["refresh"] is r3._refresh_fundamentals_isolated
    assert seen["kwargs"]["as_of"] == pd.Timestamp("2026-09-30")
    assert len(result["recovered_stale_builds"]) == 1
    assert v43.MODEL_VERSION != r3.MODEL_VERSION


def test_signal_bundle_validator_requires_repaired_refresh_and_timeliness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "runner_version": r3.MODEL_VERSION,
        "purpose": "SIGNAL",
        "as_of": "2026-09-30",
        "created_at": "2026-09-30T21:05:00+00:00",
        "fundamentals_refresh": {
            "sec_unmapped_policy": {
                "classification": r3.UNMAPPED_CLASSIFICATION,
                "invented_cik_count": 0,
            },
            "as_of_filter": {},
        },
    }
    monkeypatch.setattr(r1, "V43_VALIDATED_BUNDLE", lambda *_args: (manifest, "a" * 64))
    validated, _sha = r3._validated_bundle(tmp_path, "SIGNAL")
    assert validated is manifest

    manifest["created_at"] = "2026-10-01T07:59:59+00:00"
    assert r3._validated_bundle(tmp_path, "SIGNAL")[0] is manifest
    for outside in ("2026-09-30T20:29:59+00:00", "2026-10-01T08:00:00+00:00"):
        manifest["created_at"] = outside
        with pytest.raises(RuntimeError, match="not staged inside its SIGNAL window"):
            r3._validated_bundle(tmp_path, "SIGNAL")
    manifest["created_at"] = "2026-09-30T21:05:00+00:00"

    manifest["as_of"] = "2026-08-31"
    with pytest.raises(RuntimeError, match="missed or pre-r3"):
        r3._validated_bundle(tmp_path, "SIGNAL")
    # A catch-up bundle is judged by its own session's window.
    manifest["as_of"] = "2026-10-01"
    manifest["created_at"] = "2026-10-02T02:00:00+00:00"
    assert r3._validated_bundle(tmp_path, "SIGNAL")[0] is manifest
    manifest["created_at"] = "2026-10-02T08:00:00+00:00"
    with pytest.raises(RuntimeError, match="not staged inside its SIGNAL window"):
        r3._validated_bundle(tmp_path, "SIGNAL")
    manifest["as_of"] = "2026-09-30"
    manifest["created_at"] = "2026-09-30T21:05:00+00:00"

    del manifest["fundamentals_refresh"]["as_of_filter"]
    with pytest.raises(RuntimeError, match="repaired fundamentals refresh"):
        r3._validated_bundle(tmp_path, "SIGNAL")

    manifest["runner_version"] = r2.MODEL_VERSION
    with pytest.raises(RuntimeError, match="not frozen by the v50r3 runner"):
        r3._validated_bundle(tmp_path, "SIGNAL")


def test_signal_frozen_event_is_refused_once_the_window_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "bundle_manifest.json").write_text(
        json.dumps({"as_of": "2026-09-30"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        r3, "_validated_protocol",
        lambda _path=None: ({"signal_policy": SIGNAL_POLICY}, "a" * 64),
    )
    appended = []

    def fake_append(**kwargs) -> None:
        appended.append(kwargs)

    monkeypatch.setattr(v43, "append_event", fake_append)

    def fake_freeze(**_kwargs):
        v43.append_event(
            path="ledger", protocol_sha256="a" * 64,
            event_type="SIGNAL_FROZEN", payload={},
        )
        return {"status": "FROZEN_PROSPECTIVE_SIGNAL"}

    monkeypatch.setattr(v43, "freeze_signal", fake_freeze)
    kwargs = {"bundle": bundle, "lock_path": tmp_path / "staging.lock"}

    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-10-01T07:59:59Z"))
    assert r3.freeze_signal(**kwargs)["status"] == "FROZEN_PROSPECTIVE_SIGNAL"
    assert appended[-1]["recorded_at"] == "2026-10-01T07:59:59+00:00"

    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-10-01T08:00:00Z"))
    with pytest.raises(RuntimeError, match="window closed"):
        r3.freeze_signal(**kwargs)
    assert len(appended) == 1
    assert v43.append_event is fake_append

    # Only the SIGNAL_FROZEN event is gated: marks keep flowing.
    with r3._signal_deadline(pd.Timestamp("2026-09-30")):
        v43.append_event(
            path="ledger", protocol_sha256="a" * 64,
            event_type="VALUATION_APPENDED", payload={},
        )
    assert appended[-1]["event_type"] == "VALUATION_APPENDED"
    assert "recorded_at" not in appended[-1]


def _ledger_with(path: Path, *signal_dates: str) -> list[dict]:
    for date in ("PROTOCOL", *signal_dates):
        v43.append_event(
            path=path,
            protocol_sha256="a" * 64,
            event_type="PROTOCOL_FROZEN" if date == "PROTOCOL" else "SIGNAL_FROZEN",
            payload={} if date == "PROTOCOL" else {"signal_date": date},
        )
    return v43.read_ledger(path)


def test_signal_role_allows_one_catch_up_per_missed_month_end(tmp_path: Path) -> None:
    first = pd.Timestamp("2026-09-30")
    assert r3.signal_role("2026-09-30", [], first) == {
        "signal_role": "REGULAR",
        "signal_window_utc": {
            "opens": "2026-09-30T20:30:00+00:00",
            "closes": "2026-10-01T08:00:00+00:00",
        },
    }
    assert r3.signal_role("2026-10-02", [], first) == {
        "signal_role": "CATCH_UP",
        "catch_up_for": "2026-09-30",
        "missed_signal_window_utc": {
            "opens": "2026-09-30T20:30:00+00:00",
            "closes": "2026-10-01T08:00:00+00:00",
        },
        # Friday's own window runs over the weekend.
        "signal_window_utc": {
            "opens": "2026-10-02T20:30:00+00:00",
            "closes": "2026-10-05T08:00:00+00:00",
        },
    }
    caught_up = _ledger_with(tmp_path / "caught_up.jsonl", "2026-10-02")
    # Re-verifying the frozen catch-up is allowed; a second catch-up is not.
    assert r3.signal_role("2026-10-02", caught_up, first)["signal_role"] == "CATCH_UP"
    with pytest.raises(RuntimeError, match="already frozen"):
        r3.signal_role("2026-10-05", caught_up, first)
    on_time = _ledger_with(tmp_path / "on_time.jsonl", "2026-09-30")
    with pytest.raises(RuntimeError, match="already frozen"):
        r3.signal_role("2026-10-01", on_time, first)
    # The next month end is its own regular signal again.
    assert r3.signal_role("2026-10-30", caught_up, first)["signal_role"] == "REGULAR"
    with pytest.raises(RuntimeError, match="before its first date"):
        r3.signal_role("2026-09-29", [], first)
    with pytest.raises(ValueError, match="not a Nasdaq session"):
        r3.signal_role("2026-10-03", [], first)
    assert r3.catch_up_signals(caught_up) == [{
        "signal_date": "2026-10-02",
        "catch_up_for": "2026-09-30",
        "execution_date": None,
    }]
    assert r3.catch_up_signals(on_time) == []


def test_stage_bundle_stages_a_catch_up_only_inside_its_own_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        r3, "_validated_protocol",
        lambda _path=None: ({"signal_policy": SIGNAL_POLICY}, "a" * 64),
    )
    kwargs = {
        "bundles_dir": tmp_path / "bundles",
        "work_dir": tmp_path / "work",
        "signals_dir": tmp_path / "signals",
        "ledger_path": tmp_path / "ledger.jsonl",
        "lock_path": tmp_path / "staging.lock",
    }
    seen = {}

    def fake_stage(**stage_kwargs):
        # The inherited month-end check accepts exactly the catch-up session.
        seen["catch_up_session"] = r3.v42._is_month_end_signal(
            pd.Timestamp("2026-10-01")
        )
        seen["other_session"] = r3.v42._is_month_end_signal(pd.Timestamp("2026-10-02"))
        seen["as_of"] = stage_kwargs["as_of"]
        return {"status": "FROZEN_ISOLATED_INPUT_BUNDLE"}

    monkeypatch.setattr(v43, "stage_bundle", fake_stage)
    with pytest.raises(RuntimeError, match="only inside its window"):
        r3.stage_bundle(
            as_of="2026-10-01", purpose="SIGNAL",
            observed_at=_at("2026-10-02T08:00:00Z"), **kwargs,
        )
    result = r3.stage_bundle(
        as_of="2026-10-01", purpose="SIGNAL",
        observed_at=_at("2026-10-02T02:00:00Z"), **kwargs,
    )
    assert seen == {
        "catch_up_session": True,
        "other_session": False,
        "as_of": pd.Timestamp("2026-10-01"),
    }
    assert r3.v42._is_month_end_signal(pd.Timestamp("2026-10-01")) is False
    assert (result["signal_role"], result["catch_up_for"]) == ("CATCH_UP", "2026-09-30")

    _ledger_with(kwargs["ledger_path"], "2026-10-01")
    with pytest.raises(RuntimeError, match="already frozen"):
        r3.stage_bundle(
            as_of="2026-10-02", purpose="SIGNAL",
            observed_at=_at("2026-10-03T12:00:00Z"), **kwargs,
        )


def test_a_catch_up_freeze_records_its_role_before_its_window_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "bundle_manifest.json").write_text(
        json.dumps({"as_of": "2026-10-01"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        r3, "_validated_protocol",
        lambda _path=None: ({"signal_policy": SIGNAL_POLICY}, "a" * 64),
    )
    covered = tmp_path / "covered.jsonl"
    _ledger_with(covered, "2026-09-30")
    appended = []
    monkeypatch.setattr(v43, "append_event", lambda **kwargs: appended.append(kwargs))
    seen = {}

    def fake_freeze(**_kwargs):
        seen["catch_up_session"] = r3.v42._is_month_end_signal(
            pd.Timestamp("2026-10-01")
        )
        v43.append_event(
            path="ledger", protocol_sha256="a" * 64,
            event_type="SIGNAL_FROZEN", payload={"signal_date": "2026-10-01"},
        )
        return {"status": "FROZEN_PROSPECTIVE_SIGNAL"}

    monkeypatch.setattr(v43, "freeze_signal", fake_freeze)
    kwargs = {
        "bundle": bundle,
        "lock_path": tmp_path / "staging.lock",
        "ledger_path": tmp_path / "ledger.jsonl",
    }
    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-10-02T07:59:59Z"))
    result = r3.freeze_signal(**kwargs)
    assert seen["catch_up_session"] is True
    assert r3.v42._is_month_end_signal(pd.Timestamp("2026-10-01")) is False
    assert (result["signal_role"], result["catch_up_for"]) == ("CATCH_UP", "2026-09-30")
    event = appended[-1]
    assert event["recorded_at"] == "2026-10-02T07:59:59+00:00"
    assert event["payload"]["signal_date"] == "2026-10-01"
    assert event["payload"]["signal_role"] == "CATCH_UP"
    assert event["payload"]["catch_up_for"] == "2026-09-30"
    assert event["payload"]["missed_signal_window_utc"]["closes"] == (
        "2026-10-01T08:00:00+00:00"
    )
    assert event["payload"]["signal_window_utc"]["closes"] == (
        "2026-10-02T08:00:00+00:00"
    )

    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-10-02T08:00:00Z"))
    with pytest.raises(RuntimeError, match="window closed"):
        r3.freeze_signal(**kwargs)
    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-10-02T02:00:00Z"))
    with pytest.raises(RuntimeError, match="already frozen"):
        r3.freeze_signal(**{**kwargs, "ledger_path": covered})
    assert len(appended) == 1


def _freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: str) -> dict:
    manifest = {
        "development_status": "PASS",
        "research_forward_observation_ready": True,
        "gates": {"all": True},
        "positive_training_years_50bps": 6,
    }
    events = []
    monkeypatch.setattr(r1, "_development_manifest", lambda: manifest)
    monkeypatch.setattr(r1, "_zero_signal_v48_events", lambda: [{}])
    monkeypatch.setattr(r2, "_zero_signal_v50r1_events", lambda: [{}])
    monkeypatch.setattr(r3, "_zero_signal_v50r2_events", lambda: [{}])
    monkeypatch.setattr(
        r3, "_file_binding", lambda path: {"path": str(path), "sha256": "b" * 64}
    )
    monkeypatch.setattr(r3, "_git_head", lambda: "c" * 40)
    monkeypatch.setattr(r3, "current_branch", lambda: r3.LIVE_BRANCH)
    monkeypatch.setattr(
        r3, "current_code_closure",
        lambda: {"roots": ["x"], "file_count": 1, "files": {"x": "d" * 64},
                 "sha256": "e" * 64},
    )
    monkeypatch.setattr(v43, "append_event", lambda **kwargs: events.append(kwargs))
    directory = tmp_path / now.replace(":", "")
    result = r3.freeze_protocol(
        directory / "protocol.json", directory / "ledger.jsonl", now=_at(now)
    )
    result["_events"] = events
    return result


def test_freeze_binds_closure_supersedes_r2_and_dates_the_first_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _freeze(tmp_path, monkeypatch, "2026-09-28T12:00:00Z")
    assert result["model_version"] == r3.MODEL_VERSION
    assert result["model"] == r1._selected_model()
    assert result["supersedes"]["model_version"] == r2.MODEL_VERSION
    assert result["supersedes"]["v50r2_signal_count"] == 0
    assert result["runtime_repair"] == r3.runtime_repair_specification()
    assert result["code_closure"]["sha256"] == "e" * 64
    assert result["signal_policy"]["first_prospective_signal_date"] == "2026-09-30"
    assert result["signal_policy"]["missed_signal_dates"] == ["2026-08-31"]
    assert result["signal_policy"]["first_signal_window_utc"] == {
        "opens": "2026-09-30T20:30:00+00:00",
        "closes": "2026-10-01T08:00:00+00:00",
    }
    assert result["signal_policy"]["signal_frozen_before_window_closes"] is True
    catch_up = result["signal_policy"]["missed_window_catch_up"]
    assert catch_up["allowed"] is True
    assert catch_up["catch_up_signals_per_missed_month_end"] == 1
    assert catch_up["missed_month_end_date_backfilled"] is False
    assert result["signal_policy"]["missed_signal_backfill_allowed"] is False
    assert "catch_up_signals" in result["evaluation"]
    assert result["mark_policy"]["procedure"] == r3.MARK_PROCEDURE
    assert result["code_branch"] == r3.LIVE_BRANCH
    assert result["operations"]["watchdog_reads"] == r3.LIVE_BRANCH
    assert result["mark_policy"]["post_freeze_sourced_events"] == (
        r3.SUPPLEMENT_PATH.as_posix()
    )
    assert result["release_status"] == "BLOCKED"
    assert result["promotion_eligible"] is False
    assert {"runner", "scheduler", "r2_runner", "v50r2_protocol", "v50r2_ledger"} <= set(
        result["input_bindings"]
    )
    event = result["_events"][0]
    assert event["event_type"] == "PROTOCOL_FROZEN"
    assert event["payload"]["code_closure_sha256"] == "e" * 64

    late = _freeze(tmp_path, monkeypatch, "2026-09-30T21:00:00Z")
    assert late["signal_policy"]["first_prospective_signal_date"] == "2026-10-30"
    assert late["signal_policy"]["missed_signal_dates"] == ["2026-08-31", "2026-09-30"]

    with pytest.raises(RuntimeError, match="will not be overwritten"):
        r3.freeze_protocol(
            tmp_path / "2026-09-28T120000Z" / "protocol.json",
            tmp_path / "2026-09-28T120000Z" / "ledger.jsonl",
            now=_at("2026-09-28T12:00:00Z"),
        )


def test_protocol_validation_rejects_code_closure_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closure = {"roots": ["x"], "file_count": 1, "files": {"a.py": "0" * 64},
               "sha256": "f" * 64}
    protocol = {
        "model_version": r3.MODEL_VERSION,
        "model": r3._selected_model(),
        "runtime_repair": r3.runtime_repair_specification(),
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "input_bindings": {},
        "code_closure": closure,
        "signal_policy": SIGNAL_POLICY,
    }
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol), encoding="utf-8")
    monkeypatch.setattr(r3, "current_code_closure", lambda: dict(closure))
    validated, _sha = r3._validated_protocol(path)
    assert validated["code_closure"] == closure

    drifted = {**closure, "files": {"a.py": "1" * 64}, "sha256": "9" * 64}
    monkeypatch.setattr(r3, "current_code_closure", lambda: drifted)
    with pytest.raises(RuntimeError, match=r"closure changed.*a\.py"):
        r3._validated_protocol(path)


def test_runtime_binds_r3_and_restores_the_inherited_modules() -> None:
    names = (
        "MODEL_VERSION", "_validated_protocol", "_validated_bundle",
        "_build_signal_payload", "_refresh_fundamentals_isolated",
    )
    before = {name: getattr(v43, name) for name in names}
    json_before = (v43.json, v43.v42.json)
    with r3._runtime():
        assert v43.MODEL_VERSION == r3.MODEL_VERSION
        assert v43._refresh_fundamentals_isolated is r3._refresh_fundamentals_isolated
        assert isinstance(v43.json, r2._JsonScalarProxy)
    assert {name: getattr(v43, name) for name in names} == before
    assert (v43.json, v43.v42.json) == json_before


def test_frozen_r3_protocol_is_tracked_hash_bound_and_verifiable() -> None:
    protocol_path = r3.REPO_ROOT / r3.PROTOCOL_PATH
    if not protocol_path.exists():
        pytest.skip("v50r3 protocol has not been frozen yet")
    tracked = set(subprocess.run(
        ["git", "ls-files"], cwd=r3.REPO_ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.splitlines())
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    for name, binding in protocol["input_bindings"].items():
        relative = binding["path"]
        assert not Path(relative).is_absolute(), f"{name} is checkout-absolute"
        assert relative in tracked, f"clean checkout is missing {relative}"
        payload = (r3.REPO_ROOT / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == binding["sha256"], name
    for relative in protocol["code_closure"]["files"]:
        assert relative in tracked, f"closure file {relative} is untracked"
    validated, protocol_sha = r3._validated_protocol(protocol_path)
    assert validated["supersedes"]["model_version"] == r2.MODEL_VERSION
    supersession = json.loads(
        (r3.REPO_ROOT / r3.V50R2_SUPERSESSION_PATH).read_text(encoding="utf-8")
    )
    assert supersession["successor_protocol"]["sha256"] == protocol_sha
    # Later SIGNAL/MARK events are expected; the chain must still bind r3.
    events = v43.read_ledger(r3.REPO_ROOT / r3.LEDGER_PATH)
    assert events[0]["event_type"] == "PROTOCOL_FROZEN"
    assert all(event["protocol_sha256"] == protocol_sha for event in events)
    first, _missed = r3.signal_dates(validated)
    assert all(
        pd.Timestamp(event["payload"]["signal_date"]) >= first
        for event in events
        if event["event_type"] == "SIGNAL_FROZEN"
    )


def _repo_on(tmp_path: Path, branch: str) -> Path:
    repo = tmp_path / "copy"
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=o@example.invalid",
         "-c", "user.name=O", "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    return repo


def test_only_a_live_branch_checkout_may_write_r3_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_on(tmp_path, "master")
    monkeypatch.setattr(r3, "REPO_ROOT", repo)
    assert r3.current_branch() == "master"
    with pytest.raises(SystemExit, match="only from a live/v50r3 checkout"):
        r3.require_live_checkout()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True)
    assert r3.current_branch() is None
    with pytest.raises(SystemExit, match="a detached HEAD"):
        r3.require_live_checkout()
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", r3.LIVE_BRANCH], check=True
    )
    assert r3.current_branch() == r3.LIVE_BRANCH
    r3.require_live_checkout()


def test_cli_writes_refuse_other_branches_but_status_reads_anywhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.chdir(r3.REPO_ROOT)  # main() switches to the repository root
    monkeypatch.setattr(r3, "current_branch", lambda: "master")
    monkeypatch.setattr(
        r3, "freeze_protocol", lambda: pytest.fail("froze outside the live copy")
    )
    for command in (
        ["freeze-protocol"],
        ["stage-bundle", "--as-of", "2026-09-30", "--purpose", "SIGNAL"],
        ["record-sourced-event", "--ticker", "A", "--type", "MARKET_MOVE",
         "--date", "2026-10-01", "--source-url", "https://example.com"],
    ):
        with pytest.raises(SystemExit, match="live/v50r3"):
            r3.main(command)
    if not (r3.REPO_ROOT / r3.PROTOCOL_PATH).is_file():
        assert r3.main(["status"]) == 3
        assert '"live_branch": "live/v50r3"' in capsys.readouterr().out

    monkeypatch.setattr(r3, "current_branch", lambda: r3.LIVE_BRANCH)
    monkeypatch.setattr(r3, "freeze_protocol", lambda: {"status": "FROZEN"})
    assert r3.main(["freeze-protocol"]) == 0


# -- live SIGNAL inputs and readiness ----------------------------------------

def _frozen_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "validation.csv"
    pd.DataFrame([{
        "ticker": "AAA", "split_date": "2026-07-21", "validation_status": "CONFIRMED",
        "confirmed_action_type": "SPLIT", "confirmed_action_date": "2026-07-21",
        "confirmed_adjustment_factor": 0.5,
    }]).to_csv(path, index=False)
    monkeypatch.setattr(r3, "VALIDATION_PATH", path)


def _provider_rows(*rows: tuple[str, str, float]) -> pd.DataFrame:
    return pd.DataFrame([{
        "ticker": ticker, "split_date": pd.Timestamp(session),
        "raw_price_ratio": factor, "matched_factor": factor,
        "validation_status": "CONFIRMED",
        "confirmed_action_type": "PROVIDER_ADJUSTMENT_DISCONTINUITY",
        "confirmed_action_date": pd.Timestamp(session),
        "confirmed_adjustment_factor": factor,
        "primary_source": "nasdaq_history_overlap", "overlap_sessions": 19,
        "overlap_first": "2026-06-22", "overlap_last": "2026-07-20",
        "ratio_spread": 0.0, "recorded_at": "2026-09-30T21:00:00+00:00",
    } for ticker, session, factor in rows])


def test_live_validation_adds_provider_rescalings_without_overriding_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _frozen_table(tmp_path, monkeypatch)
    provider = _provider_rows(
        ("AAA", "2026-07-21", 0.5), ("BBB", "2026-10-20", 2 / 3),
        ("CCC", "2026-10-14", 0.5),
    )

    validation, audit = r3._live_validation(
        provider=provider, ignore_provider_through=pd.Timestamp("2026-10-14")
    )

    assert sorted(zip(validation["ticker"], validation["split_date"].dt.strftime("%m-%d"))) == [
        ("AAA", "07-21"), ("BBB", "10-20"),
    ]
    assert [row["ticker"] for row in audit["provider_adjustments_applied"]] == ["BBB"]
    assert {row["ticker"]: row["reason"] for row in audit["provider_adjustments_ignored"]} == {
        "AAA": "an adjudicated event covers it",
        "CCC": "measured after its session was valued",
    }


def test_identity_breaks_keep_only_the_security_now_using_the_ticker() -> None:
    dates = pd.bdate_range("2024-01-01", "2026-09-30")
    raw = pd.DataFrame(index=dates, columns=["SPCX", "HALT", "PLAIN"], dtype=float)
    raw.loc[:"2025-04-10", "SPCX"] = 23.0
    raw.loc["2026-06-12":, "SPCX"] = 160.0
    # A long halt that resumes near its old price is the same security.
    raw.loc[:"2025-07-09", "HALT"] = 150.0
    raw.loc["2026-04-17":, "HALT"] = 170.0
    raw["PLAIN"] = 50.0

    assert r3.identity_breaks(raw) == {"SPCX": pd.Timestamp("2026-06-12")}


def test_signal_inputs_drop_a_reused_ticker_until_it_has_its_own_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _frozen_table(tmp_path, monkeypatch)
    dates = pd.bdate_range("2024-08-01", "2026-09-30")
    raw = pd.DataFrame({"SPCX": 23.0, "PLAIN": 50.0}, index=dates)
    raw.loc["2025-04-11":"2026-06-11", "SPCX"] = float("nan")
    raw.loc["2026-06-12":, "SPCX"] = 160.0
    monkeypatch.setattr(r3, "V42_LOAD_SIGNAL_INPUTS", lambda *_args: {
        "raw_close": raw.copy(), "dollar_volume": raw * 1e6,
    })

    inputs = r3._signal_inputs(tmp_path, dates[-1], r3._live_validation()[0])

    assert inputs["identity_breaks"] == {"SPCX": "2026-06-12"}
    assert inputs["raw_close"]["SPCX"].isna().all()
    assert inputs["dollar_volume"]["SPCX"].isna().all()
    assert inputs["raw_close"]["PLAIN"].notna().all()


def test_the_signal_payload_selects_from_the_live_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # r1's own payload builder runs with its loader routed to the live inputs,
    # which must still read through v42's loader (the routed one recursed).
    _frozen_table(tmp_path, monkeypatch)
    dates = pd.bdate_range("2026-06-01", "2026-09-30")
    raw = pd.DataFrame({"SPLT": 100.0, "PLAIN": 50.0}, index=dates)
    raw.loc["2026-09-01":, "SPLT"] = 25.0
    monkeypatch.setattr(r3, "V42_LOAD_SIGNAL_INPUTS", lambda *_args: {
        "raw_close": raw.copy(), "dollar_volume": raw * 1e6,
    })
    _provider_rows(("SPLT", "2026-09-01", 0.25)).to_csv(
        tmp_path / r3.PROVIDER_ADJUSTMENTS_NAME, index=False
    )
    selected: dict = {}

    def build_signal_payload(**kwargs):
        selected.update(kwargs["inputs"])
        return {"targets": []}

    monkeypatch.setattr(r3.r1.v42, "build_signal_payload", build_signal_payload)
    loader = r3.r1.v42._load_signal_inputs

    payload = r3._build_signal_payload(
        signal_date=dates[-1],
        bundle=tmp_path,
        protocol={"model": {}, "code_closure": {"sha256": "c" * 64}},
        protocol_sha="0" * 64,
        manifest_sha="1" * 64,
    )

    # The provider's rescaling reaches the selector's continuous prices.
    assert selected["close"]["SPLT"].eq(25.0).all()
    assert selected["raw_close"]["SPLT"].iloc[0] == 100.0
    assert payload["live_price_events"]["provider_adjustments_applied"] == [
        {"ticker": "SPLT", "session": "2026-09-01", "factor": 0.25}
    ]
    assert payload["model_version"] == r3.MODEL_VERSION
    assert r3.r1.v42._load_signal_inputs is loader


def _readiness_inputs(dates, raw, liquidity):
    return {
        "raw_close": raw,
        "close": raw,
        "dollar_volume": pd.DataFrame(liquidity, index=dates),
        "nasdaq": pd.Series(range(1, len(dates) + 1), index=dates, dtype=float),
        "universe": lambda _as_of: set(raw.columns),
    }


@pytest.fixture
def readiness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _frozen_table(tmp_path, monkeypatch)
    dates = pd.bdate_range("2025-09-01", periods=260)
    raw = pd.DataFrame(100.0, index=dates, columns=["POOL", "NEAR", "SMALL"])
    liquidity = {"POOL": 1e9, "NEAR": 9e8, "SMALL": 1e7}
    state = {"dates": dates, "raw": raw, "liquidity": liquidity, "bundle": tmp_path}
    monkeypatch.setattr(
        r3, "_signal_inputs",
        lambda _bundle, _stamp, validation: _readiness_inputs(
            dates, state["raw"], state["liquidity"]
        ),
    )
    monkeypatch.setattr(
        r3.corrected_stock_policy, "large_liquid_ranking",
        lambda *_args: pd.DataFrame({"median_dollar_volume_50d": [1e9]}, index=["POOL"]),
    )
    monkeypatch.setattr(r3, "_selected_model", lambda: {"selector_specification": {
        "liquid_pool_size": 1, "lookback_sessions": 63,
    }})
    return state


def test_signal_readiness_passes_complete_candidates(readiness) -> None:
    gates, details = r3._signal_readiness(readiness["bundle"], readiness["dates"][-1])
    assert all(gates.values())
    # A stock far less liquid than the pool cannot enter it and is not checked.
    assert details["candidate_count"] == 2


def test_signal_readiness_refuses_a_candidate_without_its_as_of_close(readiness) -> None:
    readiness["raw"].loc[readiness["dates"][-1], "NEAR"] = float("nan")
    readiness["raw"].loc[readiness["dates"][-1], "SMALL"] = float("nan")

    gates, details = r3._signal_readiness(readiness["bundle"], readiness["dates"][-1])

    assert gates["pool_candidates_priced_at_as_of"] is False
    assert details["candidates_without_as_of_close"] == ["NEAR"]


def test_signal_readiness_refuses_a_hole_at_the_momentum_start(readiness) -> None:
    start = readiness["dates"][-1 - 63]
    readiness["raw"].loc[start, "NEAR"] = float("nan")

    gates, details = r3._signal_readiness(readiness["bundle"], readiness["dates"][-1])

    assert gates["pool_candidates_have_momentum_start_close"] is False
    assert details["candidates_without_momentum_start_close"] == ["NEAR"]


def test_signal_readiness_needs_every_split_like_move_explained(readiness) -> None:
    session = readiness["dates"][-20]
    readiness["raw"].loc[session:, "NEAR"] = 52.0

    gates, details = r3._signal_readiness(readiness["bundle"], readiness["dates"][-1])
    assert gates["pool_candidates_split_like_moves_explained"] is False
    assert [row["ticker"] for row in details["unexplained_split_like_moves"]] == ["NEAR"]

    # The provider's own rescaling, recorded by the price update, explains it.
    _provider_rows(("NEAR", f"{session:%Y-%m-%d}", 0.52)).to_csv(
        readiness["bundle"] / r3.PROVIDER_ADJUSTMENTS_NAME, index=False
    )
    gates, _details = r3._signal_readiness(readiness["bundle"], readiness["dates"][-1])
    assert all(gates.values())


def test_signal_readiness_does_not_gate_a_cash_month(readiness) -> None:
    readiness["raw"].loc[readiness["dates"][-1], "NEAR"] = float("nan")
    dates = readiness["dates"]
    falling = pd.Series(range(len(dates), 0, -1), index=dates, dtype=float)

    def inputs(_bundle, _stamp, _validation):
        frame = _readiness_inputs(dates, readiness["raw"], readiness["liquidity"])
        frame["nasdaq"] = falling
        return frame

    r3_signal_inputs = r3._signal_inputs
    try:
        r3._signal_inputs = inputs
        gates, details = r3._signal_readiness(readiness["bundle"], dates[-1])
    finally:
        r3._signal_inputs = r3_signal_inputs
    assert all(gates.values()) and details["market_regime_on"] is False


def test_a_signal_build_that_fails_its_gates_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = tmp_path / "work" / "bundle_builds" / "2026-09-30_signal"

    def v42_stage(**_kwargs):
        build.mkdir(parents=True)
        (build / "bundle_manifest.json").write_text(json.dumps({
            "files": {}, "readiness_gates": {"required_prices_exact_at_least_98pct": True},
        }), encoding="utf-8")
        return {"bundle": str(build)}

    monkeypatch.setattr(r3, "V42_STAGE_BUNDLE", v42_stage)
    monkeypatch.setattr(r3, "_signal_readiness", lambda *_args: (
        {"pool_candidates_priced_at_as_of": False},
        {"candidates_without_as_of_close": ["OKTA"]},
    ))
    with pytest.raises(RuntimeError, match="OKTA"):
        r3._stage_v42_bundle(
            as_of="2026-09-30", purpose="SIGNAL", work_dir=tmp_path / "work",
        )
    assert not build.exists()


def test_the_runtime_uses_the_latest_four_quarter_profit_rule() -> None:
    original = r3.v24._profitable_symbols
    with r3._runtime():
        assert r3.v24._profitable_symbols is r3._live_profitable_symbols
        assert r3.v42.stage_bundle is r3._stage_v42_bundle
    assert r3.v24._profitable_symbols is original
    assert r3.v42.stage_bundle is r3.V42_STAGE_BUNDLE
