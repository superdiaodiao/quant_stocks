from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v42_prospective_v28_observation as v42
from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50r3_corrected_v47 as r3
from scripts import research_v50r3_rehearsal as rehearsal


def _at(text: str) -> datetime:
    return pd.Timestamp(text).to_pydatetime()


def test_rehearsal_stays_out_of_a_live_month_end_window() -> None:
    assert rehearsal.blocking_window(_at("2026-09-29T21:00:00Z")) is None
    assert rehearsal.blocking_window(_at("2026-09-30T17:29:00Z")) is None
    blocked = rehearsal.blocking_window(_at("2026-09-30T17:30:00Z"))
    assert blocked == {
        "signal_date": "2026-09-30",
        "opens": "2026-09-30T20:30:00+00:00",
        "closes": "2026-10-01T08:00:00+00:00",
    }
    assert rehearsal.blocking_window(_at("2026-10-01T07:59:00Z")) == blocked
    assert rehearsal.blocking_window(_at("2026-10-01T08:00:00Z")) is None
    # A Friday month-end window stays open over the weekend.
    weekend = rehearsal.blocking_window(_at("2026-11-01T12:00:00Z"))
    assert weekend is not None and weekend["signal_date"] == "2026-10-30"


def test_window_fit_requires_room_for_one_retry() -> None:
    fit = rehearsal.window_fit(40 * 60, pd.Timestamp("2026-09-30"))
    assert fit["window_minutes"] == 690.0
    assert fit["attempts_that_fit"] == 17
    assert fit["fits_with_one_retry"] is True
    assert fit["latest_start_for_one_attempt_utc"] == "2026-10-01T07:20:00+00:00"
    slow = rehearsal.window_fit(400 * 60, pd.Timestamp("2026-11-30"))
    assert slow["window_minutes"] == 690.0
    assert slow["fits_with_one_retry"] is False


def test_price_coverage_names_missing_and_stale_files(tmp_path: Path) -> None:
    market = tmp_path / "market"
    (market / "prices").mkdir(parents=True)
    pd.DataFrame({
        "Symbol": ["AAA", "BBB", "CCC", "NA"],
        "Name": [
            "Alpha Inc. Common Stock",
            "Beta Inc. Common Stock",
            "Gamma Inc. Common Stock",
            "Nano Labs Ltd Class A Ordinary Shares",
        ],
    }).to_csv(market / "current_universe.csv", index=False)
    for ticker, last in (("aaa", "2026-09-28"), ("bbb", "2026-09-25"), ("na", "2026-09-28")):
        pd.DataFrame({"date": ["2026-09-24", last], "close": [1.0, 1.0]}).to_csv(
            market / "prices" / f"{ticker}.csv", index=False
        )

    coverage = rehearsal.price_coverage(tmp_path, pd.Timestamp("2026-09-28"))

    assert coverage["universe_ticker_count"] == 4
    assert coverage["missing_price_files"] == ["CCC"]
    assert coverage["without_as_of_close_sample"] == [
        {"ticker": "BBB", "latest_date": "2026-09-25"}
    ]
    assert coverage["all_price_files_present_gate"] is False
    assert rehearsal.price_coverage(tmp_path / "absent", pd.Timestamp("2026-09-28")) == {
        "universe_staged": False
    }


def test_rehearsal_runtime_restores_every_patched_function() -> None:
    before = {
        "month_end": v42._is_month_end_signal,
        "timed": {name: getattr(module, attribute)
                  for name, (module, attribute) in rehearsal.TIMED_FUNCTIONS.items()},
        "refresh": v43._refresh_fundamentals_isolated,
        "model": v43.MODEL_VERSION,
    }
    as_of = pd.Timestamp("2026-09-28")
    with rehearsal._rehearsal_runtime(as_of, {}):
        assert v42._is_month_end_signal(as_of) is True
        assert v42._is_month_end_signal(pd.Timestamp("2026-09-29")) is False
        assert v42._is_month_end_signal(pd.Timestamp("2026-09-30")) is True
        assert v43.MODEL_VERSION == r3.MODEL_VERSION
        assert v43._refresh_fundamentals_isolated is not r3._refresh_fundamentals_isolated
        assert r3._REFRESH_OPTIONS["enforce_signal_window"] is False
    assert v42._is_month_end_signal is before["month_end"]
    assert {name: getattr(module, attribute)
            for name, (module, attribute) in rehearsal.TIMED_FUNCTIONS.items()} == before["timed"]
    assert v43._refresh_fundamentals_isolated is before["refresh"]
    assert v43.MODEL_VERSION == before["model"]
    assert r3._REFRESH_OPTIONS["enforce_signal_window"] is True


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage) -> dict:
    monkeypatch.setattr(v43, "stage_bundle", stage)
    monkeypatch.setattr(rehearsal, "_git_state", lambda: {"head": "c" * 40, "dirty": False})
    return rehearsal.rehearse(
        as_of=pd.Timestamp("2026-09-28"),
        scratch=tmp_path / "scratch",
        workers=2,
        fundamental_workers=1,
        now=_at("2026-09-29T02:00:00Z"),
        lock_path=tmp_path / "staging.lock",
    )


def test_failed_staging_is_reported_without_touching_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = {}
    ledger = r3.REPO_ROOT / r3.LEDGER_PATH
    ledger_before = ledger.read_bytes() if ledger.exists() else None

    def failing_stage(**kwargs):
        seen["kwargs"] = kwargs
        seen["month_end"] = v42._is_month_end_signal(kwargs["as_of"])
        raise RuntimeError("v42 staged bundle is not ready: ['qqq_through_as_of']")

    report = _run(tmp_path, monkeypatch, failing_stage)

    assert report["verdict"] == "FAIL"
    assert report["staging"]["status"] == "FAILED"
    assert "qqq_through_as_of" in report["staging"]["error"]["message"]
    assert report["ledger_written"] is False and report["signal_written"] is False
    assert seen["month_end"] is True
    assert seen["kwargs"]["ledger_path"] == tmp_path / "scratch" / "no_ledger.jsonl"
    assert seen["kwargs"]["bundles_dir"] == tmp_path / "scratch" / "bundles"
    assert report["next_window_fit"]["signal_date"] >= "2026-09-30"
    assert (ledger.read_bytes() if ledger.exists() else None) == ledger_before


def test_successful_rehearsal_reports_selection_and_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "created_at": "2026-09-28T21:10:00+00:00",
        "readiness_gates": {"all_required_price_files_present": True},
        "fundamentals_refresh": {
            "sec_unmapped_policy": {"unmapped_ticker_count": 7},
            "as_of_filter": {"quarterly.csv": {"removed_future_rows": 2}},
            "refresh_elapsed_seconds": {"update_fundamentals": 600.0},
            "failures": [],
        },
    }
    monkeypatch.setattr(
        r3, "_validated_bundle_contents", lambda *_args: (manifest, "a" * 64)
    )
    monkeypatch.setattr(r3, "_build_signal_payload", lambda **_kwargs: {
        "market_regime_on": True,
        "selected_count": 5,
        "targets": [{"ticker": "AAA", "target_weight": 0.2}],
    })
    monkeypatch.setattr(rehearsal, "ranked_pool_corporate_actions", lambda *_args: {
        "ranked_liquid_pool": ["AAA"],
        "unreviewed_split_like_jumps": [{"ticker": "AAA", "date": "2026-08-03"}],
    })

    report = _run(
        tmp_path, monkeypatch,
        lambda **_kwargs: {"status": "FROZEN_ISOLATED_INPUT_BUNDLE"},
    )

    assert report["staging"]["status"] == "FROZEN_ISOLATED_INPUT_BUNDLE"
    assert report["selection"]["diagnostic_targets"][0]["ticker"] == "AAA"
    assert report["created_inside_session_window"] is True
    assert report["fundamentals"]["sec_unmapped_policy"]["unmapped_ticker_count"] == 7
    assert report["verdict"] == "WARN"
    assert any("never reviewed" in warning for warning in report["warnings"])
    assert any("no SEC CIK" in warning for warning in report["warnings"])
    assert report["not_a_prospective_signal"] is True


def test_corporate_action_scan_flags_only_unreviewed_jumps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = pd.bdate_range("2026-01-02", periods=260)
    raw = pd.DataFrame({"AAA": 100.0, "BBB": 50.0, "CCC": 30.0}, index=dates)
    raw.loc[dates[250]:, "AAA"] = 50.0   # unreviewed 2:1 jump in the window
    raw.loc[dates[240]:, "BBB"] = 25.0   # jump already in the frozen table
    raw.loc[dates[5]:, "CCC"] = 15.0     # outside the 199-session window
    as_of = dates[-1]
    inputs = {"raw_close": raw, "nasdaq": pd.Series(1.0, index=dates)}
    validation = pd.DataFrame({
        "ticker": ["BBB"],
        "split_date": [dates[240]],
        "validation_status": ["CONFIRMED_MARKET_MOVE"],
        "confirmed_adjustment_factor": [None],
    })
    monkeypatch.setattr(rehearsal.v42, "_load_signal_inputs", lambda *_args: dict(inputs))
    monkeypatch.setattr(rehearsal, "load_corporate_action_validation", lambda: validation)
    monkeypatch.setattr(
        rehearsal, "corrected_price_views", lambda close, _validation: (close, close)
    )
    monkeypatch.setattr(
        rehearsal, "large_liquid_ranking",
        lambda *_args: pd.DataFrame(index=["AAA", "BBB", "CCC"]),
    )

    scan = rehearsal.ranked_pool_corporate_actions(Path("unused"), as_of)

    assert scan["ranked_liquid_pool"] == ["AAA", "BBB", "CCC"]
    assert [item["ticker"] for item in scan["unreviewed_split_like_jumps"]] == ["AAA"]
    assert scan["unreviewed_split_like_jumps"][0]["matched_factor"] == 0.5
