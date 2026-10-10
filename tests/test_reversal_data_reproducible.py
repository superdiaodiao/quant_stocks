"""An offline rebuild of a frozen data version must reproduce its committed files (docs/architecture.md 11.7)."""
from __future__ import annotations

import inspect
import re

import pytest

from pipelines.reversal_data import common, reconcile, security_master, terminal
from quant.data import version


def test_every_version_has_a_stamp_date():
    assert set(version.STAMP_DATE) == set(version.VERSIONS)
    for day in version.STAMP_DATE.values():
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
    assert common.STAMP_DATE == version.stamp_date() == version.STAMP_DATE[version.DATA_VERSION]


def test_committed_stamps_are_the_version_build_days():
    # the build days the committed v1 / v2 / v2.1 tables carry (terminal verified_at, mechanical R1 moves)
    assert version.STAMP_DATE == {"v1": "2026-10-03", "v2": "2026-10-05", "v2.1": "2026-10-10"}


@pytest.mark.parametrize("func", [terminal.build, reconcile.build_move_queue])
def test_row_stamps_do_not_read_the_clock(func):
    source = inspect.getsource(func)
    assert "datetime.now" not in source and "STAMP_DATE" in source


def test_security_master_stops_without_the_local_files(tmp_path, monkeypatch):
    monkeypatch.setattr(security_master, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(security_master, "PRICE_DIR", tmp_path / "price")
    with pytest.raises(SystemExit, match="main checkout"):
        security_master.require_local_files()
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "price").mkdir()
    for name in ("snapshots/nasdaq_listed_2020-01-02.csv", "snapshots/nasdaq_300M_2020-01-02.csv", "price/aapl.csv"):
        (tmp_path / name).write_text("x\n")
    security_master.require_local_files()


def test_prefilter_budget_reads_the_ledger_only_up_to_the_freeze(tmp_path, monkeypatch):
    from pipelines.reversal_data import prefilter
    ledger = tmp_path / "quota_ledger.csv"
    ledger.write_text("fetched_utc,source,month,symbol,status\n"
                      "2026-10-01T01:00:00+00:00,tiingo,2026-10,AAPL,200\n"
                      "2026-10-01T02:00:00+00:00,wayback,2026-10,,200\n"
                      "2026-10-20T01:00:00+00:00,tiingo,2026-10,MSFT,200\n")   # after every freeze
    monkeypatch.setattr(common, "QUOTA_LEDGER", ledger)
    for v in version.VERSIONS:
        monkeypatch.setattr(common, "DATA_VERSION", v)
        assert prefilter.tiingo_used("2026-10") == 1 and prefilter.ledger_symbols("2026-10") == {"AAPL"}
    monkeypatch.setitem(version.FROZEN_UTC, "v1", None)   # a version not yet frozen reads the whole ledger
    monkeypatch.setattr(common, "DATA_VERSION", "v1")
    assert prefilter.tiingo_used("2026-10") == 2 and prefilter.ledger_symbols("2026-10") == {"AAPL", "MSFT"}
