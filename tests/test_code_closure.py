from pathlib import Path

import pytest

from src.research.code_closure import (
    closure_differences,
    closure_digest,
    project_import_closure,
)


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_closure_follows_scripts_and_src_imports_and_package_initializers(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "scripts/entry.py", (
        "import json\n"
        "from scripts import helper as h\n"
        "from src.research.policy import RULE\n"
    ))
    _write(tmp_path, "scripts/helper.py", "from scripts.deep import VALUE\n")
    _write(tmp_path, "scripts/deep.py", "VALUE = 1\n")
    _write(tmp_path, "scripts/unused.py", "VALUE = 2\n")
    _write(tmp_path, "src/__init__.py", "")
    _write(tmp_path, "src/research/__init__.py", "")
    _write(tmp_path, "src/research/policy.py", "from .shared import BASE\nRULE = BASE\n")
    _write(tmp_path, "src/research/shared.py", "BASE = 3\n")

    closure = project_import_closure(["scripts/entry.py"], tmp_path)

    assert sorted(closure) == [
        "scripts/deep.py",
        "scripts/entry.py",
        "scripts/helper.py",
        "src/__init__.py",
        "src/research/__init__.py",
        "src/research/policy.py",
        "src/research/shared.py",
    ]


def test_digest_is_order_free_and_detects_any_file_change(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/entry.py", "from scripts import helper\n")
    helper = _write(tmp_path, "scripts/helper.py", "VALUE = 1\n")
    before = project_import_closure(["scripts/entry.py"], tmp_path)
    assert closure_digest(before) == closure_digest(dict(reversed(before.items())))

    helper.write_text("VALUE = 1  # comment\n", encoding="utf-8")
    after = project_import_closure(["scripts/entry.py"], tmp_path)

    assert closure_digest(after) != closure_digest(before)
    assert closure_differences(before, after) == {
        "changed": ["scripts/helper.py"],
        "added": [],
        "removed": [],
    }


def test_missing_root_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="missing"):
        project_import_closure(["scripts/absent.py"], tmp_path)


def test_live_r3_closure_covers_the_modules_r2_left_unbound() -> None:
    from scripts import research_v50r3_corrected_v47 as r3

    files = r3.current_code_closure()["files"]
    for path in (
        "scripts/research_v26_large_liquid_stock_momentum.py",
        "scripts/research_v47_hybrid_entry_portfolio_stop.py",
        "scripts/research_v48_isolated_prospective_v47_observation.py",
        "src/research/data_quality.py",
        "src/research/shadow_evaluation.py",
        "src/strategy/common.py",
        "src/io/fundamentals_update.py",
        "scripts/research_v50r3_scheduled_run.py",
    ):
        assert path in files
