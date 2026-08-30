import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v42_prospective_v28_observation as v42


def test_protocol_freeze_excludes_training_and_reused_diagnostic_from_score(
    tmp_path: Path,
) -> None:
    protocol_path = tmp_path / "protocol.json"
    ledger_path = tmp_path / "ledger.jsonl"

    result = v42.freeze_protocol(protocol_path, ledger_path)

    assert result["evidence_partition"]["2020_2025"] == {
        "role": "TRAINING_AND_MODEL_SELECTION_ONLY",
        "years": [2020, 2021, 2022, 2023, 2024, 2025],
        "counts_as_official_comparison": False,
        "official_year_wins": 0,
    }
    assert (
        result["evidence_partition"]["2026_01_07"][
            "counts_as_official_comparison"
        ]
        is False
    )
    assert result["contains_index_etf_holdings"] is False
    assert result["evaluation"]["primary_benchmark"] == (
        "NASDAQ_COMPOSITE_PRICE_RETURN"
    )
    assert result["release_status"] == "BLOCKED"
    events = v42.read_ledger(ledger_path)
    assert [event["event_type"] for event in events] == ["PROTOCOL_FROZEN"]
    with pytest.raises(RuntimeError, match="will not be overwritten"):
        v42.freeze_protocol(protocol_path, ledger_path)


def test_ledger_hash_chain_detects_historical_tampering(tmp_path: Path) -> None:
    protocol_path = tmp_path / "protocol.json"
    ledger_path = tmp_path / "ledger.jsonl"
    v42.freeze_protocol(protocol_path, ledger_path)
    protocol_sha = v42._sha256(protocol_path)
    v42.append_event(
        path=ledger_path,
        protocol_sha256=protocol_sha,
        event_type="SIGNAL_FROZEN",
        payload={
            "signal_date": "2026-08-31",
            "signal_sha256": "a" * 64,
        },
        recorded_at="2026-08-31T21:00:00+00:00",
    )
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[1])
    event["payload"]["signal_sha256"] = "b" * 64
    lines[1] = json.dumps(event, sort_keys=True)
    ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="event hash is invalid"):
        v42.read_ledger(ledger_path)


def test_ledger_rejects_valuation_before_execution(tmp_path: Path) -> None:
    protocol_path = tmp_path / "protocol.json"
    ledger_path = tmp_path / "ledger.jsonl"
    v42.freeze_protocol(protocol_path, ledger_path)
    protocol_sha = v42._sha256(protocol_path)
    v42.append_event(
        path=ledger_path,
        protocol_sha256=protocol_sha,
        event_type="SIGNAL_FROZEN",
        payload={
            "signal_date": "2026-08-31",
            "signal_sha256": "a" * 64,
        },
    )

    with pytest.raises(RuntimeError, match="valuation cannot precede"):
        v42.append_event(
            path=ledger_path,
            protocol_sha256=protocol_sha,
            event_type="VALUATION_APPENDED",
            payload={"as_of": "2026-09-01"},
        )


def _synthetic_signal_inputs(*, future_row: bool = False) -> dict:
    dates = pd.bdate_range("2026-01-02", "2026-08-31")
    if future_row:
        dates = dates.append(pd.DatetimeIndex([pd.Timestamp("2026-09-01")]))
    close = pd.DataFrame(
        {"AAA": range(1, len(dates) + 1), "BBB": range(2, len(dates) + 2)},
        index=dates,
        dtype=float,
    )
    return {
        "close": close,
        "nasdaq": pd.Series(range(100, 100 + len(dates)), index=dates, dtype=float),
        "quarterly": pd.DataFrame(
            {
                "available_date": [pd.Timestamp("2026-07-30")],
                "ticker": ["AAA"],
            }
        ),
    }


def test_signal_payload_is_stock_only_and_has_no_execution_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v42, "market_regime_is_on", lambda *_args: True)
    ranking = pd.DataFrame(
        {"momentum_excess_vs_nasdaq": [2.0, 1.0]},
        index=["AAA", "BBB"],
    )
    payload = v42.build_signal_payload(
        signal_date="2026-08-31",
        inputs=_synthetic_signal_inputs(),
        model=v42._selected_model(),
        protocol_sha256="a" * 64,
        bundle_manifest_sha256="b" * 64,
        ranking_function=lambda *_args: ranking,
    )

    assert payload["execution_date"] is None
    assert payload["execution_policy"] == (
        "first common trading-session close after signal"
    )
    assert {row["ticker"] for row in payload["targets"]} == {"AAA", "BBB"}
    assert payload["contains_index_etf_holdings"] is False
    assert sum(row["target_weight"] for row in payload["targets"]) == pytest.approx(
        2 / 5
    )


def test_signal_payload_refuses_future_data_and_etf_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v42, "market_regime_is_on", lambda *_args: True)
    with pytest.raises(RuntimeError, match="future price rows"):
        v42.build_signal_payload(
            signal_date="2026-08-31",
            inputs=_synthetic_signal_inputs(future_row=True),
            model=v42._selected_model(),
            protocol_sha256="a" * 64,
            bundle_manifest_sha256="b" * 64,
            ranking_function=lambda *_args: pd.DataFrame(index=["AAA"]),
        )

    with pytest.raises(RuntimeError, match="forbidden ETFs"):
        v42.build_signal_payload(
            signal_date="2026-08-31",
            inputs=_synthetic_signal_inputs(),
            model=v42._selected_model(),
            protocol_sha256="a" * 64,
            bundle_manifest_sha256="b" * 64,
            ranking_function=lambda *_args: pd.DataFrame(index=["QQQ"]),
        )


def test_period_evaluation_counts_only_complete_prospective_periods() -> None:
    calendar = v42.nasdaq_calendar_for_year(2026)
    dates = pd.DatetimeIndex(
        calendar.sessions_in_range("2026-09-01", "2026-09-30")
    ).tz_localize(None).normalize()
    result = pd.DataFrame(
        {
            "strategy": 0.002,
            "benchmark": 0.001,
        },
        index=dates,
    )

    evaluation = v42._period_evaluation(
        result, first_execution=pd.Timestamp("2026-09-01")
    )

    assert evaluation["complete_month_count"] == 1
    assert evaluation["complete_month_wins"] == 1
    assert evaluation["all_complete_prospective_months_won"] is True
    assert evaluation["official_year_count"] == 0
    assert evaluation["official_year_wins"] == 0
    assert evaluation["training_year_wins_counted"] == 0
    assert evaluation["reused_2026_diagnostic_wins_counted"] == 0


def test_bundle_validation_detects_bound_file_change(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    index = bundle / "nasdaq_index.csv"
    index.write_text("date,close\n2026-08-31,100\n", encoding="utf-8")
    manifest = {
        "purpose": "MARK",
        "as_of": "2026-08-31",
        "readiness_gates": {"ready": True},
        "files": {"nasdaq_index.csv": v42._sha256(index)},
        "price_files": {},
    }
    (bundle / "bundle_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    v42._validated_bundle(bundle, "MARK")
    index.write_text("date,close\n2026-08-31,101\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="binding changed"):
        v42._validated_bundle(bundle, "MARK")


def _write_mark_bundle(
    root: Path,
    *,
    as_of: str,
    revise_date: str | None = None,
) -> Path:
    bundle = root / f"mark_{as_of}"
    prices = bundle / "prices"
    prices.mkdir(parents=True)
    dates = pd.bdate_range("2026-01-02", as_of)
    close = pd.Series(
        [100.0 + position * 0.1 for position in range(len(dates))],
        index=dates,
    )
    if revise_date is not None:
        close.loc[pd.Timestamp(revise_date)] *= 1.10
    pd.DataFrame(
        {"date": dates, "close": close.to_numpy(), "volume": 1_000_000}
    ).to_csv(prices / "aaa.csv", index=False)
    pd.DataFrame(
        {"date": dates, "close": range(10_000, 10_000 + len(dates))}
    ).to_csv(bundle / "nasdaq_index.csv", index=False)
    pd.DataFrame(
        {
            "date": dates,
            "close": range(500, 500 + len(dates)),
            "cash_dividend": 0.0,
        }
    ).to_csv(bundle / "qqq.csv", index=False)
    price_sha = v42._sha256(prices / "aaa.csv")
    manifest = {
        "purpose": "MARK",
        "as_of": as_of,
        "readiness_gates": {"ready": True},
        "files": {
            "nasdaq_index.csv": v42._sha256(bundle / "nasdaq_index.csv"),
            "qqq.csv": v42._sha256(bundle / "qqq.csv"),
        },
        "price_files": {
            "AAA": {
                "file": "prices/aaa.csv",
                "sha256": price_sha,
                "latest_date": as_of,
            }
        },
    }
    (bundle / "bundle_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return bundle


def test_append_mark_binds_next_close_and_refuses_revised_history(
    tmp_path: Path,
) -> None:
    protocol_path = tmp_path / "protocol.json"
    ledger_path = tmp_path / "ledger.jsonl"
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    v42.freeze_protocol(protocol_path, ledger_path)
    protocol_sha = v42._sha256(protocol_path)
    signal_path = signals_dir / "signal_2026-08-31.json"
    signal_path.write_text(
        json.dumps(
            {
                "signal_date": "2026-08-31",
                "targets": [{"ticker": "AAA", "target_weight": 1.0}],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    v42.append_event(
        path=ledger_path,
        protocol_sha256=protocol_sha,
        event_type="SIGNAL_FROZEN",
        payload={
            "signal_date": "2026-08-31",
            "signal_path": str(signal_path),
            "signal_sha256": v42._sha256(signal_path),
        },
    )

    first_bundle = _write_mark_bundle(tmp_path, as_of="2026-09-03")
    first = v42.append_mark(
        bundle=first_bundle,
        protocol_path=protocol_path,
        ledger_path=ledger_path,
        signals_dir=signals_dir,
    )

    assert first["status"] == "APPENDED_PROSPECTIVE_MARK"
    assert first["first_execution_date"] == "2026-09-01"
    assert first["cost_metrics"]["50"]["strategy_nav"] < (
        first["cost_metrics"]["10"]["strategy_nav"]
    )
    events = v42.read_ledger(ledger_path)
    assert [event["event_type"] for event in events] == [
        "PROTOCOL_FROZEN",
        "SIGNAL_FROZEN",
        "EXECUTION_DATE_BOUND",
        "VALUATION_APPENDED",
    ]

    revised_bundle = _write_mark_bundle(
        tmp_path, as_of="2026-09-04", revise_date="2026-09-02"
    )
    with pytest.raises(RuntimeError, match="revises an already frozen"):
        v42.append_mark(
            bundle=revised_bundle,
            protocol_path=protocol_path,
            ledger_path=ledger_path,
            signals_dir=signals_dir,
        )
