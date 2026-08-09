import hashlib

from src.research import data_fingerprint
from src.research.data_fingerprint import (
    build_data_manifest,
    data_manifest_sha256_from_components,
    fingerprint_file,
    fingerprint_files,
    fingerprint_tree,
)


def test_file_fingerprint_is_content_based(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")

    result = fingerprint_file(path)

    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["bytes"] == len(path.read_bytes())


def test_data_manifest_is_order_independent_and_tamper_evident():
    fingerprints = {
        name: {"sha256": f"{index:064x}"}
        for index, name in enumerate(
            data_fingerprint.CAN_SLIM_DATA_COMPONENTS,
            start=1,
        )
    }

    manifest = build_data_manifest(fingerprints)
    reversed_components = dict(
        reversed(list(manifest["components"].items()))
    )

    assert data_manifest_sha256_from_components(
        reversed_components
    ) == manifest["sha256"]
    reversed_components["eps"] = "f" * 64
    assert data_manifest_sha256_from_components(
        reversed_components
    ) != manifest["sha256"]


def test_tree_fingerprint_is_stable_and_tracks_relative_paths(tmp_path):
    left = tmp_path / "left.csv"
    nested = tmp_path / "nested"
    nested.mkdir()
    right = nested / "right.csv"
    left.write_text("left", encoding="utf-8")
    right.write_text("right", encoding="utf-8")

    before = fingerprint_tree(tmp_path)
    after = fingerprint_tree(tmp_path)

    assert before == after
    assert before["file_count"] == 2
    right.rename(nested / "renamed.csv")
    assert fingerprint_tree(tmp_path)["sha256"] != before["sha256"]


def test_explicit_file_fingerprint_ignores_unrelated_code(
    tmp_path, monkeypatch
):
    strategy = tmp_path / "strategy.py"
    unrelated = tmp_path / "downloader.py"
    strategy.write_text("POLICY = 1\n", encoding="utf-8")
    unrelated.write_text("SOURCE = 1\n", encoding="utf-8")
    monkeypatch.setattr(data_fingerprint, "PROJECT_PATH", tmp_path)

    strategy_before = fingerprint_files(["strategy.py"])
    source_before = fingerprint_tree(tmp_path, pattern="*.py")
    unrelated.write_text("SOURCE = 2\n", encoding="utf-8")

    assert fingerprint_files(["strategy.py"]) == strategy_before
    assert fingerprint_tree(tmp_path, pattern="*.py") != source_before


def test_can_slim_strategy_code_set_excludes_data_downloaders():
    files = set(data_fingerprint.project_python_dependency_closure(
        data_fingerprint.CAN_SLIM_STRATEGY_CODE_ROOTS
    ))

    assert "src/research/can_slim.py" in files
    assert "src/strategy/common.py" in files
    assert "src/io/terminal_returns.py" in files
    assert "src/io/fundamentals_update.py" not in files
    assert "src/research/can_slim_validation.py" not in files


def test_project_dependency_closure_recurses_without_importing(
    tmp_path, monkeypatch
):
    package = tmp_path / "src"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "root.py").write_text(
        "from src.middle import VALUE\n",
        encoding="utf-8",
    )
    (package / "middle.py").write_text(
        "from src.leaf import VALUE\n",
        encoding="utf-8",
    )
    (package / "leaf.py").write_text(
        "raise RuntimeError('must not import')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(data_fingerprint, "PROJECT_PATH", tmp_path)

    assert data_fingerprint.project_python_dependency_closure(
        ["src/root.py"]
    ) == (
        "src/__init__.py",
        "src/leaf.py",
        "src/middle.py",
        "src/root.py",
    )
