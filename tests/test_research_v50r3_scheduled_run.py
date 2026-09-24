from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess

import pandas as pd
import pytest

from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50r3_corrected_v47 as r3
from scripts import research_v50r3_scheduled_run as sched


PROTOCOL = {
    "signal_policy": {
        "first_prospective_signal_date": "2026-09-30",
        "missed_signal_dates": ["2026-08-31"],
    }
}


def _at(text: str) -> datetime:
    return pd.Timestamp(text).to_pydatetime()


@pytest.fixture
def frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A frozen protocol and a one-event ledger in a scratch directory."""
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps(PROTOCOL), encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    v43.append_event(
        path=ledger,
        protocol_sha256=r3._sha256(protocol),
        event_type="PROTOCOL_FROZEN",
        payload={"first_prospective_signal_date": "2026-09-30"},
    )
    monkeypatch.setattr(
        r3, "_validated_protocol", lambda _path=None: (PROTOCOL, r3._sha256(protocol))
    )
    return {
        "protocol_path": protocol,
        "ledger_path": ledger,
        "bundles_dir": tmp_path / "bundles",
        "signals_dir": tmp_path / "signals",
        "lock_path": tmp_path / "staging.lock",
    }


def test_unfrozen_protocol_and_missing_ledger_are_not_an_empty_ledger(
    tmp_path: Path, frozen: dict
) -> None:
    missing = sched.run(
        now=_at("2026-09-30T21:00:00Z"),
        execute=False,
        protocol_path=tmp_path / "absent.json",
    )
    assert missing["action"] == "PROTOCOL_NOT_FROZEN"
    assert sched.exit_code(missing) == sched.NOT_READY_EXIT_CODE

    no_ledger = sched.run(
        now=_at("2026-09-30T21:00:00Z"),
        execute=True,
        protocol_path=frozen["protocol_path"],
        ledger_path=tmp_path / "wrong_directory" / "ledger.jsonl",
    )
    assert no_ledger["action"] == "LEDGER_MISSING"
    assert no_ledger["executed"] is False
    assert sched.exit_code(no_ledger) == sched.NOT_READY_EXIT_CODE


def test_run_stages_then_freezes_under_one_lock(
    frozen: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        r3, "stage_bundle",
        lambda **kwargs: calls.append(("stage", kwargs, dict(r3._HELD_LOCKS)))
        or {"status": "FROZEN_ISOLATED_INPUT_BUNDLE", "recovered_stale_builds": []},
    )
    monkeypatch.setattr(
        r3, "freeze_signal",
        lambda **kwargs: calls.append(("freeze", kwargs, dict(r3._HELD_LOCKS)))
        or {"status": "FROZEN_PROSPECTIVE_SIGNAL", "targets": [{"ticker": "AAA"}]},
    )
    monkeypatch.setattr(
        r3, "append_mark", lambda **_kwargs: pytest.fail("no mark for a due signal")
    )

    checked = sched.run(now=_at("2026-09-30T21:00:00Z"), execute=False, **frozen)
    assert checked["action"] == "RUN_SIGNAL" and checked["executed"] is False
    assert calls == []

    # 10:00 Beijing time the next day: the UTC date has rolled, the window has not.
    executed = sched.run(now=_at("2026-10-01T02:00:00Z"), execute=True, **frozen)
    assert executed["executed"] is True
    assert executed["result_status"] == "FROZEN_PROSPECTIVE_SIGNAL"
    assert executed["targets"] == [{"ticker": "AAA"}]
    assert [name for name, _kwargs, _locks in calls] == ["stage", "freeze"]
    assert calls[0][1]["as_of"] == "2026-09-30"
    assert calls[1][1]["bundle"] == frozen["bundles_dir"] / "2026-09-30_signal"
    lock_key = str(r3.resolve(frozen["lock_path"]))
    assert all(locks.get(lock_key) for _name, _kwargs, locks in calls)
    assert r3._HELD_LOCKS == {}


def test_a_second_process_reports_staging_in_progress(
    frozen: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        r3, "stage_bundle", lambda **_kwargs: pytest.fail("staged while locked")
    )
    frozen["lock_path"].parent.mkdir(parents=True, exist_ok=True)
    with frozen["lock_path"].open("a+") as other:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        decision = sched.run(now=_at("2026-09-30T21:00:00Z"), execute=True, **frozen)
    assert decision["action"] == "RUN_SIGNAL"
    assert decision["action_status"] == "STAGING_IN_PROGRESS"
    assert decision["executed"] is False
    assert sched.exit_code(decision) == 0


def test_missed_window_is_never_staged_and_exits_two(
    frozen: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        r3, "stage_bundle", lambda **_kwargs: pytest.fail("backfill attempted")
    )
    decision = sched.run(now=_at("2026-10-01T08:30:00Z"), execute=True, **frozen)
    assert decision["action"] == "SIGNAL_WINDOW_MISSED"
    assert decision["executed"] is False
    assert sched.exit_code(decision) == sched.MISSED_EXIT_CODE


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_git_record_commits_only_the_ledger_and_signal_and_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", "-b", "master", str(remote))
    _git(tmp_path, "init", "-b", "master", str(repo))
    _git(repo, "config", "user.email", "operator@example.invalid")
    _git(repo, "config", "user.name", "Operator")
    (repo / "README").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "master")
    ledger = repo / "out" / "ledger.jsonl"
    signal = repo / "out" / "signals" / "signal_2026-09-30.json"
    signal.parent.mkdir(parents=True)
    ledger.write_text("{}\n", encoding="utf-8")
    signal.write_text("{}\n", encoding="utf-8")
    # Unrelated staged work must never ride along with the ledger commit.
    (repo / "other.txt").write_text("draft\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    monkeypatch.setattr(r3, "REPO_ROOT", repo)
    monkeypatch.setattr(r3.r1, "REPO_ROOT", repo)

    result = sched.record_in_git(
        [Path("out/ledger.jsonl"), Path("out/signals/signal_2026-09-30.json")],
        message="research: freeze 2026-09-30 v50r3 signal",
        push=True,
    )

    assert "error" not in result
    assert result["committed"] and result["pushed"]
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert sorted(committed) == [
        "out/ledger.jsonl",
        "out/signals/signal_2026-09-30.json",
    ]
    assert "other.txt" in _git(repo, "diff", "--cached", "--name-only")
    assert _git(remote, "rev-parse", "master") == result["commit"]

    again = sched.record_in_git(
        [Path("out/ledger.jsonl")], message="unchanged", push=False
    )
    assert again["committed"] is False and "error" not in again

    _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    failed = sched.record_in_git([Path("out/ledger.jsonl")], message="x", push=True)
    assert failed["error"]
    assert sched.exit_code({"action": "NO_ACTION", "git": failed}) == 1


def test_cli_runs_from_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    seen = {}

    def fake_run(**kwargs):
        seen["cwd"] = Path.cwd()
        seen["kwargs"] = kwargs
        return {"action": "SIGNAL_WINDOW_MISSED"}

    monkeypatch.setattr(sched, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    try:
        code = sched.main(["run", "--push", "--now", "2026-10-01T01:00:00Z"])
    finally:
        os.chdir(tmp_path)
    assert code == sched.MISSED_EXIT_CODE
    assert seen["cwd"] == r3.REPO_ROOT
    assert seen["kwargs"]["commit"] is True and seen["kwargs"]["push"] is True
    assert '"SIGNAL_WINDOW_MISSED"' in capsys.readouterr().out

    code = sched.main(["check"])
    assert seen["kwargs"]["execute"] is False
    assert seen["kwargs"]["commit"] is False and seen["kwargs"]["push"] is False
