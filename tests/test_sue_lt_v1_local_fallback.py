"""The guards of scripts/sue_lt_v1_local_fallback.sh, against throwaway repositories (no network)."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sue_lt_v1_local_fallback.sh"
BRANCH = "live/sue-lt-v1"
ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Operator",
    "GIT_AUTHOR_EMAIL": "operator@example.invalid",
    "GIT_COMMITTER_NAME": "Operator",
    "GIT_COMMITTER_EMAIL": "operator@example.invalid",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def _fallback(repo: Path, *args: str, live: Path | None = None) -> subprocess.CompletedProcess:
    env = {key: value for key, value in ENV.items() if key != "SUE_LT_V1_LIVE"}
    if live is not None:
        env["SUE_LT_V1_LIVE"] = str(live)
    return subprocess.run(
        ["bash", str(repo / "scripts" / SCRIPT.name), *args],
        cwd=repo, env=env, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone whose origin has master and the live branch."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "quant"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    _git(tmp_path, "init", "-q", "-b", "master", str(repo))
    (repo / "scripts").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts" / SCRIPT.name)
    _git(repo, "add", "scripts")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "master")
    _git(repo, "push", "-q", "origin", f"master:refs/heads/{BRANCH}")
    _git(repo, "fetch", "-q", "origin")
    return repo


def test_an_unknown_command_is_refused(repo: Path) -> None:
    result = _fallback(repo, "freeze")
    assert result.returncode == 2
    assert "未知命令" in result.stderr


@pytest.mark.parametrize("command", ["status", "run"])
def test_nothing_runs_before_setup(repo: Path, command: str) -> None:
    result = _fallback(repo, command)
    assert result.returncode == 1
    assert f"{repo.name}_sue_lt_v1_live" in result.stderr
    assert "setup" in result.stderr


def test_the_main_checkout_is_never_the_live_copy(repo: Path) -> None:
    # `run` cleans the live copy of everything outside git before a SIGNAL.
    _git(repo, "checkout", "-q", "-b", BRANCH, f"origin/{BRANCH}")
    result = _fallback(repo, "run", live=repo)
    assert result.returncode == 1
    assert "主仓库本身" in result.stderr


def test_a_copy_on_another_branch_is_refused(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _git(repo, "worktree", "add", "-q", "-b", "scratch", str(other))
    result = _fallback(repo, "run", live=other)
    assert result.returncode == 1
    assert BRANCH in result.stderr


def test_a_copy_without_its_environment_is_refused(repo: Path, tmp_path: Path) -> None:
    live = tmp_path / "live"
    _git(repo, "worktree", "add", "-q", "--track", "-b", BRANCH, str(live), f"origin/{BRANCH}")
    (live / "untracked.txt").write_text("kept\n", encoding="utf-8")
    result = _fallback(repo, "run", live=live)
    assert result.returncode == 1
    assert ".venv" in result.stderr
    assert (live / "untracked.txt").is_file()


@pytest.fixture
def live(repo: Path, tmp_path: Path) -> Path:
    """A live copy whose Python environment refuses everything."""
    live = tmp_path / "live"
    _git(repo, "worktree", "add", "-q", "--track", "-b", BRANCH, str(live), f"origin/{BRANCH}")
    python = live / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    python.chmod(0o755)
    return live


def test_a_copy_in_use_is_left_alone(repo: Path, live: Path) -> None:
    lock = live / ".fallback_logs" / "lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = _fallback(repo, "run", live=live)
    assert result.returncode == 1
    assert str(os.getpid()) in result.stderr
    assert (lock / "pid").is_file()


def test_a_lock_left_by_a_killed_run_is_replaced_and_released(repo: Path, live: Path) -> None:
    finished = subprocess.Popen(["true"])
    finished.wait()
    lock = live / ".fallback_logs" / "lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{finished.pid}\n", encoding="utf-8")
    result = _fallback(repo, "rehearse", "--as-of", "2026-10-30", live=live)
    # It got past the lock, to the environment check that this copy fails.
    assert result.returncode == 1
    assert "不一致" in result.stderr
    assert not lock.exists()


def test_a_rehearsal_needs_its_month_end(repo: Path) -> None:
    result = _fallback(repo, "rehearse")
    assert result.returncode == 1
    assert "--as-of" in result.stderr
