"""scripts/setup_v50r3_live.sh against throwaway repositories (no network)."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_v50r3_live.sh"
BOUND = "stocks_list_dir/nasdaq/reviewed_market_moves.csv"
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


def _setup(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(repo / "scripts" / "setup_v50r3_live.sh"), *args],
        cwd=repo, env=ENV, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone whose data directories are ignored except one bound file."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "quant"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    _git(tmp_path, "init", "-q", "-b", "master", str(repo))
    (repo / "scripts").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts" / "setup_v50r3_live.sh")
    (repo / ".gitignore").write_text(
        "cleaned_stocks_data/\nstocks_list_dir/\n", encoding="utf-8"
    )
    (repo / BOUND).parent.mkdir(parents=True)
    (repo / BOUND).write_text("ticker,date\nAAA,2026-07-14\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "scripts")
    _git(repo, "add", "-f", BOUND)
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "master")
    # Local data, including a stale copy of the bound file the data would
    # otherwise overwrite in the live copy.
    (repo / "cleaned_stocks_data" / "price").mkdir(parents=True)
    (repo / "cleaned_stocks_data" / "price" / "aaa.csv").write_text("date,close\n")
    (repo / "stocks_list_dir" / "nasdaq" / "nasdaq_300M.csv").write_text("Symbol\n")
    (repo / BOUND).write_text("ticker,date\n", encoding="utf-8")
    return repo


def test_setup_creates_a_pinned_worktree_with_its_own_data(
    repo: Path, tmp_path: Path
) -> None:
    live = tmp_path / "live"
    result = _setup(repo, "--path", str(live), "--data", "copy", "--no-venv")
    assert result.returncode == 0, result.stderr
    assert _git(live, "rev-parse", "--abbrev-ref", "HEAD") == "live/v50r3"
    assert _git(live, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")
    assert (live / "cleaned_stocks_data" / "price" / "aaa.csv").is_file()
    assert (live / "stocks_list_dir" / "nasdaq" / "nasdaq_300M.csv").is_file()
    # The protocol-bound file keeps the branch's version, not the local copy.
    assert (live / BOUND).read_text() == "ticker,date\nAAA,2026-07-14\n"
    assert _git(live, "status", "--porcelain") == ""
    assert "git push -u origin live/v50r3" in result.stdout
    # The main checkout is untouched and still on master.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "master"
    assert (repo / BOUND).read_text() == "ticker,date\n"

    again = _setup(repo, "--path", str(tmp_path / "second"), "--no-venv")
    assert again.returncode == 1
    assert "已有本地分支 live/v50r3" in again.stderr
    taken = _setup(repo, "--path", str(live), "--no-venv")
    assert taken.returncode == 1 and "目标已存在" in taken.stderr


def test_setup_tracks_an_existing_remote_live_branch(
    repo: Path, tmp_path: Path
) -> None:
    first = tmp_path / "live"
    assert _setup(repo, "--path", str(first), "--data", "skip", "--no-venv").returncode == 0
    (first / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    _git(first, "add", "ledger.jsonl")
    _git(first, "commit", "-q", "-m", "freeze")
    _git(first, "push", "-q", "-u", "origin", "live/v50r3")

    # A second machine: a fresh clone picks up the pushed live branch.
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    (other / "scripts").mkdir(exist_ok=True)
    shutil.copy2(SCRIPT, other / "scripts" / "setup_v50r3_live.sh")
    moved = tmp_path / "moved"
    result = _setup(other, "--path", str(moved), "--data", "skip", "--no-venv")
    assert result.returncode == 0, result.stderr
    assert "远端已有 live/v50r3" in result.stdout
    assert (moved / "ledger.jsonl").is_file()
    assert _git(moved, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/live/v50r3"


def test_setup_rejects_unknown_options(repo: Path) -> None:
    result = _setup(repo, "--data", "sometimes")
    assert result.returncode == 2
    assert "--data 只能是" in result.stderr
