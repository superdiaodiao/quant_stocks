from __future__ import annotations

from pathlib import Path
import plistlib
from types import SimpleNamespace

from scripts.research_v6_launchd import install, launchd_status, unload


def _plist(path: Path) -> None:
    path.write_bytes(plistlib.dumps({
        "Label": "com.quant-stocks.v6-shadow",
        "ProgramArguments": ["python", "/tmp/scripts/research_v6_scheduled_run.py"],
        "StartCalendarInterval": [
            {"Weekday": weekday, "Hour": 9, "Minute": 0}
            for weekday in (2, 3, 4, 5, 6)
        ],
    }))


def test_install_is_dry_run_and_does_not_create_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.plist"
    destination = tmp_path / "LaunchAgents/agent.plist"
    _plist(source)
    runner = lambda *args, **kwargs: SimpleNamespace(
        returncode=113, stdout="", stderr="not found"
    )

    result = install(
        source=source,
        destination=destination,
        uid=501,
        runner=runner,
    )

    assert result["status"] == "DRY_RUN"
    assert result["applied"] is False
    assert result["network_requests_started"] is False
    assert not destination.exists()


def test_status_detects_matching_loaded_agent(tmp_path: Path) -> None:
    source = tmp_path / "source.plist"
    destination = tmp_path / "agent.plist"
    _plist(source)
    destination.write_bytes(source.read_bytes())
    runner = lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="loaded", stderr=""
    )

    result = launchd_status(
        source=source,
        destination=destination,
        uid=501,
        runner=runner,
    )

    assert result["state"] == "LOADED"
    assert result["installed_matches_source"] is True


def test_unload_is_dry_run_and_retains_installed_plist(tmp_path: Path) -> None:
    source = tmp_path / "source.plist"
    destination = tmp_path / "agent.plist"
    _plist(source)
    destination.write_bytes(source.read_bytes())
    runner = lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="loaded", stderr=""
    )

    result = unload(
        source=source, destination=destination, uid=501, runner=runner
    )

    assert result["status"] == "DRY_RUN_UNLOAD"
    assert result["applied"] is False
    assert result["installed_plist_will_be_retained"] is True
    assert destination.exists()
