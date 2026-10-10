import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd

import scripts.research_v14_nasdaq_price_backfill as backfill_module
from scripts.research_v14_nasdaq_price_backfill import _same_price_values


def _prices(ticker: str) -> pd.DataFrame:
    dates = pd.bdate_range("2022-11-01", "2023-02-28")
    close = pd.Series(range(len(dates)), dtype=float) / 100 + 10
    return pd.DataFrame(
        {
            "date": dates,
            "ticker": ticker,
            "open": close - 0.05,
            "high": close + 0.10,
            "low": close - 0.10,
            "close": close,
            "volume": 1000.0 + close.index,
        }
    )


def _write_priority(path: Path) -> None:
    pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "provider_ticker": "AAA",
                "first_missing_signal_date": "2022-12-15",
                "remediation_scope": (
                    "BACKFILL_PRICE_HEAD_PLUS_PIT_FINANCIAL"
                ),
                "priority_rank": 2,
                "recovery_priority_rank": 1,
            }
        ]
    ).to_csv(path, index=False)


def test_same_price_values_accepts_matching_missing_volume() -> None:
    left = _prices("AAA")
    left.loc[left.index[3], "volume"] = float("nan")
    right = left.copy()
    assert _same_price_values(left, right)


def test_backfill_materializes_only_validated_overlay_file(
    tmp_path: Path, monkeypatch
) -> None:
    formal = tmp_path / "formal"
    overlay = tmp_path / "overlay"
    snapshots = tmp_path / "snapshots"
    formal.mkdir()
    complete = _prices("AAA")
    local = complete.loc[complete["date"].ge("2023-01-02")]
    local.to_csv(formal / "aaa.csv", index=False, date_format="%Y-%m-%d")
    _prices("BBB").to_csv(
        formal / "bbb.csv", index=False, date_format="%Y-%m-%d"
    )
    priority = tmp_path / "priority.csv"
    state = tmp_path / "state.json"
    _write_priority(priority)
    formal_sha = hashlib.sha256((formal / "aaa.csv").read_bytes()).hexdigest()
    calls = []

    def fetch(ticker, start, end, retries=3):
        calls.append((ticker, start, end, retries))
        return complete.drop(columns="ticker").copy()

    monkeypatch.setattr(backfill_module, "fetch_history", fetch)
    result = backfill_module.backfill_batch(
        priority_path=priority,
        formal_price_dir=formal,
        overlay_dir=overlay,
        snapshot_dir=snapshots,
        state_path=state,
        start=date(2022, 11, 1),
        end=date(2023, 2, 28),
        limit=1,
        delay_seconds=0,
        apply=True,
    )

    assert result["result_status_counts"] == {"IMPORTED": 1}
    assert len(calls) == 1
    assert hashlib.sha256((formal / "aaa.csv").read_bytes()).hexdigest() == formal_sha
    assert not (overlay / "aaa.csv").is_symlink()
    assert (overlay / "bbb.csv").is_symlink()
    persisted = pd.read_csv(overlay / "aaa.csv", parse_dates=["date"])
    assert len(persisted) == len(complete)
    assert persisted["date"].min() == complete["date"].min()
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["records"]["AAA"]["cross_validation"]["sessions"] >= 20
    assert payload["records"]["AAA"]["formal_price_sha256_before"] == formal_sha
    assert payload["records"]["AAA"]["formal_price_sha256_after"] == formal_sha
    assert payload["records"]["AAA"]["source_url"].endswith(
        "assetclass=stocks&fromdate=2022-11-01&todate=2023-02-28&limit=5000"
    )
    assert payload["records"]["AAA"]["response_frame_sha256"]

    monkeypatch.setattr(
        backfill_module,
        "fetch_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    replay = backfill_module.backfill_batch(
        priority_path=priority,
        formal_price_dir=formal,
        overlay_dir=overlay,
        snapshot_dir=snapshots,
        state_path=state,
        start=date(2022, 11, 1),
        end=date(2023, 2, 28),
        limit=1,
        delay_seconds=0,
        apply=True,
    )
    assert replay["selected_tickers"] == []


def test_backfill_rejects_contradictory_overlap_without_materializing(
    tmp_path: Path, monkeypatch
) -> None:
    formal = tmp_path / "formal"
    formal.mkdir()
    complete = _prices("AAA")
    local = complete.loc[complete["date"].ge("2023-01-02")]
    local.to_csv(formal / "aaa.csv", index=False, date_format="%Y-%m-%d")
    priority = tmp_path / "priority.csv"
    _write_priority(priority)
    contradictory = complete.drop(columns="ticker").copy()
    contradictory[["open", "high", "low", "close"]] *= 2
    monkeypatch.setattr(
        backfill_module,
        "fetch_history",
        lambda *_args, **_kwargs: contradictory.copy(),
    )

    result = backfill_module.backfill_batch(
        priority_path=priority,
        formal_price_dir=formal,
        overlay_dir=tmp_path / "overlay",
        snapshot_dir=tmp_path / "snapshots",
        state_path=tmp_path / "state.json",
        start=date(2022, 11, 1),
        end=date(2023, 2, 28),
        limit=1,
        delay_seconds=0,
        apply=True,
    )

    assert result["result_status_counts"] == {"REJECT_CROSS_VALIDATION": 1}
    assert (tmp_path / "overlay" / "aaa.csv").is_symlink()


def test_backfill_accepts_reciprocal_constant_split_scale(
    tmp_path: Path, monkeypatch
) -> None:
    formal = tmp_path / "formal"
    formal.mkdir()
    complete = _prices("AAA")
    complete.loc[complete["date"].ge("2023-01-02")].to_csv(
        formal / "aaa.csv", index=False, date_format="%Y-%m-%d"
    )
    priority = tmp_path / "priority.csv"
    _write_priority(priority)
    differently_adjusted = complete.drop(columns="ticker").copy()
    differently_adjusted[["open", "high", "low", "close"]] *= 10
    differently_adjusted["volume"] /= 10
    monkeypatch.setattr(
        backfill_module,
        "fetch_history",
        lambda *_args, **_kwargs: differently_adjusted.copy(),
    )

    result = backfill_module.backfill_batch(
        priority_path=priority,
        formal_price_dir=formal,
        overlay_dir=tmp_path / "overlay",
        snapshot_dir=tmp_path / "snapshots",
        state_path=tmp_path / "state.json",
        start=date(2022, 11, 1),
        end=date(2023, 2, 28),
        limit=1,
        delay_seconds=0,
        apply=True,
    )

    record = result["records"][0]
    assert record["status"] == "IMPORTED"
    assert record["raw_cross_validation"]["passed"] is False
    assert record["cross_validation"]["passed"] is True
    assert record["scale_normalization"]["price_factor"] == 0.1
    assert record["scale_normalization"]["volume_factor"] == 10.0


def test_backfill_can_fill_validated_internal_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    formal = tmp_path / "formal"
    formal.mkdir()
    complete = _prices("AAA")
    missing_date = complete.iloc[30]["date"]
    complete.loc[complete["date"].ne(missing_date)].to_csv(
        formal / "aaa.csv", index=False, date_format="%Y-%m-%d"
    )
    priority = tmp_path / "priority.csv"
    _write_priority(priority)
    frame = pd.read_csv(priority)
    frame["remediation_scope"] = (
        "FILL_INTERNAL_PRICE_GAPS_PLUS_PIT_FINANCIAL"
    )
    frame.to_csv(priority, index=False)
    formal_sha = hashlib.sha256((formal / "aaa.csv").read_bytes()).hexdigest()
    monkeypatch.setattr(
        backfill_module,
        "fetch_history",
        lambda *_args, **_kwargs: complete.drop(columns="ticker").copy(),
    )

    result = backfill_module.backfill_batch(
        priority_path=priority,
        formal_price_dir=formal,
        overlay_dir=tmp_path / "overlay",
        snapshot_dir=tmp_path / "snapshots",
        state_path=tmp_path / "state.json",
        start=date(2022, 11, 1),
        end=date(2023, 2, 28),
        limit=1,
        delay_seconds=0,
        apply=True,
        remediation_scope="FILL_INTERNAL_PRICE_GAPS_PLUS_PIT_FINANCIAL",
    )

    assert result["result_status_counts"] == {"IMPORTED": 1}
    persisted = pd.read_csv(tmp_path / "overlay" / "aaa.csv", parse_dates=["date"])
    assert missing_date in set(persisted["date"])
    assert hashlib.sha256((formal / "aaa.csv").read_bytes()).hexdigest() == formal_sha


def test_backfill_recovers_an_applied_but_uncheckpointed_overlay(
    tmp_path: Path, monkeypatch
) -> None:
    formal = tmp_path / "formal"
    formal.mkdir()
    complete = _prices("AAA")
    complete.loc[complete["date"].ge("2023-01-02")].to_csv(
        formal / "aaa.csv", index=False, date_format="%Y-%m-%d"
    )
    priority = tmp_path / "priority.csv"
    state = tmp_path / "state.json"
    _write_priority(priority)
    monkeypatch.setattr(
        backfill_module,
        "fetch_history",
        lambda *_args, **_kwargs: complete.drop(columns="ticker").copy(),
    )
    kwargs = {
        "priority_path": priority,
        "formal_price_dir": formal,
        "overlay_dir": tmp_path / "overlay",
        "snapshot_dir": tmp_path / "snapshots",
        "state_path": state,
        "start": date(2022, 11, 1),
        "end": date(2023, 2, 28),
        "limit": 1,
        "delay_seconds": 0,
        "apply": True,
    }
    first = backfill_module.backfill_batch(**kwargs)
    assert first["records"][0]["repair_rows_added"] > 0
    state.unlink()

    recovered = backfill_module.backfill_batch(**kwargs)

    assert recovered["records"][0]["status"] == "IMPORTED"
    assert recovered["records"][0]["repair_rows_added"] == 0
    assert recovered["records"][0]["checkpoint_recovered"] is True
