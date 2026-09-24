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


def test_signal_refresh_fails_fast_after_the_utc_date_rolls_over(
    isolated_refresh: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-10-01T00:00:05Z"))
    with pytest.raises(RuntimeError, match="could never be frozen"):
        r3._refresh_fundamentals_isolated(
            as_of=pd.Timestamp("2026-09-30"),
            universe_path=isolated_refresh["universe"],
            tickers=["AAPL"],
            work=isolated_refresh["work"],
            workers=1,
        )
    assert isolated_refresh["fetched"] == []
    # A rehearsal of a completed session is allowed to run after its date.
    with r3._runtime(rehearsal=True):
        audit = r3._refresh_fundamentals_isolated(
            as_of=pd.Timestamp("2026-09-30"),
            universe_path=isolated_refresh["universe"],
            tickers=["AAPL"],
            work=isolated_refresh["work"],
            workers=1,
        )
    assert audit["sec_unmapped_policy"]["unmapped_tickers"] == []
    assert r3._REFRESH_OPTIONS["enforce_signal_utc_date"] is True


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
    with pytest.raises(RuntimeError, match="refuses late SIGNAL"):
        r3.stage_bundle(
            as_of="2026-09-30", purpose="SIGNAL",
            observed_at=_at("2026-10-01T00:10:00Z"), **kwargs,
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
        observed_at=_at("2026-09-30T20:45:00Z"), **kwargs,
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

    manifest["created_at"] = "2026-10-01T00:00:01+00:00"
    with pytest.raises(RuntimeError, match="staged after"):
        r3._validated_bundle(tmp_path, "SIGNAL")
    manifest["created_at"] = "2026-09-30T21:05:00+00:00"

    manifest["as_of"] = "2026-08-31"
    with pytest.raises(RuntimeError, match="missed or pre-r3"):
        r3._validated_bundle(tmp_path, "SIGNAL")
    manifest["as_of"] = "2026-09-30"

    del manifest["fundamentals_refresh"]["as_of_filter"]
    with pytest.raises(RuntimeError, match="repaired fundamentals refresh"):
        r3._validated_bundle(tmp_path, "SIGNAL")

    manifest["runner_version"] = r2.MODEL_VERSION
    with pytest.raises(RuntimeError, match="not frozen by the v50r3 runner"):
        r3._validated_bundle(tmp_path, "SIGNAL")


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
