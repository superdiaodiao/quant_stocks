"""End-to-end v50r3 MARK chain on a synthetic market (no network)."""

from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50_corrected_v47 as r1
from scripts import research_v50r3_corrected_v47 as r3
from src.research import panel_data
from src.research import prospective_marks as marks
from src.research import prospective_schedule as schedule


def _at(text: str) -> datetime:
    return pd.Timestamp(text).to_pydatetime()


class World:
    """A frozen r3 protocol, its ledger, and a scriptable market source."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.ledger = root / "ledger.jsonl"
        self.signals = root / "signals"
        self.bundles = root / "bundles"
        self.work = root / "work"
        self.lock = root / "staging.lock"
        self.supplement = root / "sourced_event_supplement.csv"
        protocol_path = root / "protocol.json"
        self.protocol = {
            "model": r1._selected_model(),
            "frozen_at": "2026-09-28T00:00:00+00:00",
            "signal_policy": {
                "first_prospective_signal_date": "2026-09-30",
                "missed_signal_dates": ["2026-08-31"],
            },
        }
        root.mkdir(parents=True, exist_ok=True)
        protocol_path.write_text(json.dumps(self.protocol), encoding="utf-8")
        self.protocol_path = protocol_path
        self.protocol_sha = r3._sha256(protocol_path)
        monkeypatch.setattr(
            r3, "_validated_protocol",
            lambda _path=None: (self.protocol, self.protocol_sha),
        )
        v43.append_event(
            path=self.ledger,
            protocol_sha256=self.protocol_sha,
            event_type="PROTOCOL_FROZEN",
            payload={"first_prospective_signal_date": "2026-09-30"},
        )
        validation = root / "validation.csv"
        pd.DataFrame([{
            "ticker": "OTHER",
            "split_date": "2025-06-02",
            "validation_status": "CONFIRMED_MARKET_MOVE",
            "confirmed_action_type": "MARKET_MOVE_NO_ADJUSTMENT",
            "confirmed_action_date": "2025-06-02",
            "confirmed_adjustment_factor": None,
        }]).to_csv(validation, index=False)
        monkeypatch.setattr(r3, "VALIDATION_PATH", validation)
        monkeypatch.setattr(panel_data, "known_non_common_symbols", lambda: set())

        self.sessions = schedule.sessions_between(
            pd.Timestamp("2025-01-02"), pd.Timestamp("2026-12-31")
        )
        steps = pd.Series(range(len(self.sessions)), index=self.sessions)
        self.index = pd.DataFrame({
            "date": self.sessions, "open": None, "high": None, "low": None,
            "close": 20000.0 * 1.0005 ** steps.to_numpy(), "volume": None,
        })
        self.qqq = pd.DataFrame({
            "date": self.sessions, "open": 1.0, "high": 1.0, "low": 1.0,
            "close": 500.0 * 1.0004 ** steps.to_numpy(), "volume": 1000,
            "cash_dividend": 0.0,
        })
        self.stocks: dict[str, pd.DataFrame] = {}
        for position, ticker in enumerate(("QAAA", "QBBB", "QCCC")):
            close = (40.0 + 10 * position) * (1.0 + 0.0008 * (position + 1)) ** (
                steps.to_numpy()
            )
            self.stocks[ticker] = pd.DataFrame({
                "date": self.sessions, "ticker": ticker, "open": close,
                "high": close, "low": close, "close": close, "volume": 100_000,
            })

        monkeypatch.setattr(r3, "_formal_market_bindings", lambda: {"formal": "same"})
        monkeypatch.setattr(r3.v42, "seed_cache", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(r3.v42, "update_all", self._update_all)
        monkeypatch.setattr(r3, "_refresh_index_history", self._refresh_index)
        monkeypatch.setattr(r3.v42, "reconcile_research_index", self._reconcile)
        monkeypatch.setattr(r3.v43, "_trim_qqq", self._trim_qqq)

    # -- the market source; every call returns the source's current history --
    @staticmethod
    def _through(frame: pd.DataFrame, end) -> pd.DataFrame:
        return frame.loc[pd.to_datetime(frame["date"]).le(pd.Timestamp(end))]

    def _refresh_index(self, index_path: Path, end: pd.Timestamp) -> dict:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        self._through(self.index, end).to_csv(index_path, index=False)
        return {"index": "refreshed"}

    def _update_all(self, *, end, workers, tickers, price_dir, index_path) -> dict:
        for ticker in tickers:
            self._through(self.stocks[ticker], end).to_csv(
                Path(price_dir) / f"{ticker.lower()}.csv", index=False
            )
        return self._refresh_index(Path(index_path), pd.Timestamp(end))

    @staticmethod
    def _reconcile(stamp, *, index_path, provenance_path) -> dict:
        Path(provenance_path).write_text('{"records": {}}\n', encoding="utf-8")
        return {"source": "synthetic"}

    def _trim_qqq(self, path: Path, as_of: pd.Timestamp) -> Path:
        self._through(self.qqq, as_of).to_csv(path, index=False)
        path.with_suffix(".provenance.json").write_text("{}\n", encoding="utf-8")
        return path

    # -- ledger and runner helpers --
    def freeze_signal(self, signal_date: str, tickers: list[str]) -> None:
        targets = (
            [{"ticker": ticker, "target_weight": 0.2} for ticker in tickers]
            or [{"ticker": "__CASH__", "target_weight": 0.0}]
        )
        self.signals.mkdir(parents=True, exist_ok=True)
        path = self.signals / f"signal_{signal_date}.json"
        path.write_text(
            json.dumps({"signal_date": signal_date, "targets": targets}),
            encoding="utf-8",
        )
        v43.append_event(
            path=self.ledger,
            protocol_sha256=self.protocol_sha,
            event_type="SIGNAL_FROZEN",
            payload={
                "signal_date": signal_date,
                "signal_path": str(path),
                "signal_sha256": r3._sha256(path),
                "bundle_manifest_sha256": "0" * 64,
                "targets": targets,
            },
        )

    def stage(self, as_of: str) -> dict:
        return r3.stage_bundle(
            as_of=as_of, purpose="MARK", bundles_dir=self.bundles,
            work_dir=self.work, signals_dir=self.signals, ledger_path=self.ledger,
            protocol_path=self.protocol_path, lock_path=self.lock,
            supplement_path=self.supplement,
        )

    def value(self, as_of: str) -> dict:
        return r3.append_mark(
            bundle=self.bundles / f"{as_of}_mark", protocol_path=self.protocol_path,
            ledger_path=self.ledger, signals_dir=self.signals, lock_path=self.lock,
            supplement_path=self.supplement,
        )

    def mark(self, as_of: str) -> dict:
        self.stage(as_of)
        return self.value(as_of)

    def manifest(self, as_of: str) -> dict:
        return json.loads(
            (self.bundles / f"{as_of}_mark" / "bundle_manifest.json").read_text()
        )

    def record(self, **event) -> dict:
        return r3.record_sourced_event(
            protocol_path=self.protocol_path, supplement_path=self.supplement,
            price_dir=self.work / "market" / "prices", lock_path=self.lock, **event,
        )

    def set_close(self, ticker: str, start: str, factor: float) -> None:
        frame = self.stocks[ticker]
        rows = pd.to_datetime(frame["date"]).ge(pd.Timestamp(start))
        for column in ("open", "high", "low", "close"):
            frame.loc[rows, column] *= factor

    def delist(self, ticker: str, after: str) -> None:
        frame = self.stocks[ticker]
        kept = pd.to_datetime(frame["date"]).le(pd.Timestamp(after))
        self.stocks[ticker] = frame.loc[kept]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path / "r3", monkeypatch)


def _nav(result: dict) -> float:
    return result["cost_metrics"]["50"]["strategy_nav"]


def test_mark_chain_survives_revisions_sales_delistings_and_splits(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.freeze_signal("2026-09-30", ["QAAA", "QBBB"])
    first = world.mark("2026-10-02")
    assert first["status"] == "APPENDED_PROSPECTIVE_MARK"
    assert first["first_execution_date"] == "2026-10-01"
    assert first["mark_procedure"] == r3.MARK_PROCEDURE
    assert first["sourced_event_supplement"]["rows"] == 0
    assert world.manifest("2026-10-02")["prefix_carry"]["source"] is None

    # The vendor revises already-valued rows and QQQ is re-downloaded whole:
    # the frozen rows are carried forward, so the earlier mark still verifies.
    for frame in (world.index, world.qqq, world.stocks["QAAA"]):
        frame.loc[pd.to_datetime(frame["date"]).eq("2026-10-01"), "close"] *= 1.001
    world.mark("2026-10-05")
    carry = world.manifest("2026-10-05")["prefix_carry"]
    assert carry["frozen_through"] == "2026-10-02"
    assert set(carry["revisions"]) == {"NASDAQ", "QQQ", "QAAA"}

    # November: QBBB is sold at the 11-02 close, QCCC bought.
    world.freeze_signal("2026-10-30", ["QAAA", "QCCC"])
    world.mark("2026-11-02")
    # A split-like jump and a delisting of QBBB after its sale block nothing.
    world.set_close("QBBB", "2026-11-05", 0.5)
    world.delist("QBBB", "2026-11-10")
    later = world.mark("2026-11-12")
    [ignored] = later["holding_exposure"]["ignored_price_jumps_outside_holdings"]
    assert (ignored["ticker"], ignored["date"]) == ("QBBB", "2026-11-05")
    assert ignored["raw_price_ratio"] == pytest.approx(0.5, abs=0.002)

    # QCCC stops trading while held: fail closed until a sourced terminal return.
    world.delist("QCCC", "2026-11-18")
    with pytest.raises(RuntimeError, match="held positions without a 2026-11-20 close"):
        world.stage("2026-11-20")
    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-11-20T23:00:00Z"))
    recorded = world.record(
        ticker="QCCC", event_type="TERMINAL_RETURN", event_date="2026-11-18",
        terminal_return=0.01, source_url="https://example.com/qccc-merger",
    )
    assert recorded["evidence"]["last_stored_close"] > 0
    terminal = world.mark("2026-11-20")
    assert terminal["holding_exposure"]["sourced_terminal_returns_applied"] == [{
        "ticker": "QCCC",
        "last_price_date": "2026-11-18",
        "holding_window": ["2026-11-02", "2026-11-20"],
    }]
    assert world.manifest("2026-11-20")["mark_exposure"]["sourced_terminal_returns"] == [
        {"ticker": "QCCC", "last_price_date": "2026-11-18"}
    ]

    # A 2-for-1 split in held QAAA stops the valuation until it is sourced.
    world.set_close("QAAA", "2026-11-24", 0.5)
    world.stage("2026-11-24")
    with pytest.raises(RuntimeError, match="QAAA@2026-11-24"):
        world.value("2026-11-24")
    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-11-24T23:00:00Z"))
    with pytest.raises(RuntimeError, match="does not match the adjustment factor"):
        world.record(
            ticker="QAAA", event_type="SPLIT", event_date="2026-11-24",
            adjustment_factor=0.25, source_url="https://example.com/qaaa-split",
        )
    world.record(
        ticker="QAAA", event_type="SPLIT", event_date="2026-11-24",
        adjustment_factor=0.5, source_url="https://example.com/qaaa-split",
    )
    split = world.value("2026-11-24")
    assert split["sourced_event_supplement"]["rows"] == 2
    # Continuous prices: no 10% portfolio loss from the unadjusted split.
    assert _nav(split) > 0.99 * _nav(terminal)

    events = v43.read_ledger(world.ledger)
    assert [event["event_type"] for event in events].count("VALUATION_APPENDED") == 6
    assert all(
        event["payload"]["mark_procedure"] == r3.MARK_PROCEDURE
        for event in events if event["event_type"] == "VALUATION_APPENDED"
    )

    # Rows an earlier mark used can never be rewritten.
    text = world.supplement.read_text()
    world.supplement.write_text(text.replace("qccc-merger", "qccc-deal"))
    world.stage("2026-11-25")
    with pytest.raises(RuntimeError, match="append-only"):
        world.value("2026-11-25")


def test_all_cash_month_is_marked_against_the_benchmark(world: World) -> None:
    world.freeze_signal("2026-09-30", [])
    result = world.mark("2026-10-05")
    assert result["status"] == "APPENDED_PROSPECTIVE_MARK"
    assert result["market_prefix_tickers"] == []
    assert _nav(result) == pytest.approx(1.0)
    assert result["cost_metrics"]["50"]["nasdaq_nav"] > 1.0
    manifest = world.manifest("2026-10-05")
    assert manifest["price_files"] == {}
    assert manifest["mark_exposure"]["closes_required"] == []


def test_catch_up_signals_are_marked_and_listed(world: World) -> None:
    world.freeze_signal("2026-09-30", ["QAAA", "QBBB"])
    # October's month-end window was missed; the 11-03 session caught it up.
    world.freeze_signal("2026-11-03", ["QAAA", "QCCC"])
    result = world.mark("2026-12-01")
    assert result["first_execution_date"] == "2026-10-01"
    assert result["catch_up_signals"] == [{
        "signal_date": "2026-11-03",
        "catch_up_for": "2026-10-30",
        "execution_date": "2026-11-04",
    }]
    months = result["period_evaluation_50bps"]["complete_prospective_months"]
    assert [row["period"] for row in months] == ["2026-10", "2026-11"]
    assert result["complete_months_with_catch_up_rebalance"] == ["2026-11"]
    [event] = [
        event for event in v43.read_ledger(world.ledger)
        if event["event_type"] == "VALUATION_APPENDED"
    ]
    assert event["payload"]["catch_up_signals"] == result["catch_up_signals"]
    assert event["payload"]["complete_months_with_catch_up_rebalance"] == ["2026-11"]
    # QBBB was held until the catch-up executed, then sold.
    exposure = world.manifest("2026-12-01")["mark_exposure"]
    assert exposure["targeted_tickers"] == ["QAAA", "QBBB", "QCCC"]
    assert exposure["closes_required"] == ["QAAA", "QCCC"]


def test_mark_refuses_dates_already_valued_and_sessions_only(world: World) -> None:
    world.freeze_signal("2026-09-30", ["QAAA"])
    world.mark("2026-10-02")
    with pytest.raises(RuntimeError, match="already-valued"):
        world.stage("2026-10-01")
    with pytest.raises(ValueError, match="trading session"):
        world.stage("2026-10-03")
    assert world.value("2026-10-02")["status"] == "ALREADY_VALUED_AND_VERIFIED"


def test_sourced_events_are_checked_against_stored_prices(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.freeze_signal("2026-09-30", ["QAAA"])
    world.mark("2026-10-02")
    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-10-02T23:00:00Z"))
    with pytest.raises(RuntimeError, match="no split-like price jump"):
        world.record(
            ticker="QAAA", event_type="MARKET_MOVE", event_date="2026-10-02",
            source_url="https://example.com/none",
        )
    with pytest.raises(RuntimeError, match="no completed session after 2026-10-02"):
        world.record(
            ticker="QAAA", event_type="TERMINAL_RETURN", event_date="2026-10-02",
            terminal_return=0.0, source_url="https://example.com/none",
        )
    with pytest.raises(RuntimeError, match="last stored close is 2026-10-02"):
        world.record(
            ticker="QAAA", event_type="TERMINAL_RETURN", event_date="2026-10-01",
            terminal_return=0.0, source_url="https://example.com/none",
        )
    # An event the frozen table already adjudicates is never recorded again.
    pd.DataFrame([{
        "ticker": "QAAA",
        "split_date": "2026-10-02",
        "validation_status": "CONFIRMED",
        "confirmed_action_type": "SPLIT",
        "confirmed_action_date": "2026-10-02",
        "confirmed_adjustment_factor": 0.5,
    }]).to_csv(r3.VALIDATION_PATH, index=False)
    with pytest.raises(RuntimeError, match="duplicate the frozen corporate-action"):
        world.record(
            ticker="QAAA", event_type="SPLIT", event_date="2026-10-02",
            adjustment_factor=0.5, source_url="https://example.com/none",
        )
    monkeypatch.setattr(r3, "_utc_now", lambda: _at("2026-09-27T00:00:00Z"))
    with pytest.raises(RuntimeError, match="only after the freeze"):
        world.record(
            ticker="QAAA", event_type="MARKET_MOVE", event_date="2026-10-02",
            source_url="https://example.com/none",
        )
    assert marks.load_supplement(world.supplement).empty
