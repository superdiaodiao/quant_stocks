"""Deterministic fingerprints for research input datasets."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    PROJECT_PATH,
)
from src.research.universe_history import snapshot_directory


CHUNK_SIZE = 1024 * 1024
CAN_SLIM_STRATEGY_CODE_ROOTS = (
    "src/research/can_slim.py",
    "src/research/universe_history.py",
)
CAN_SLIM_DATA_COMPONENTS = (
    "price_data",
    "price_data_provenance",
    "eps",
    "quarterly_fundamentals",
    "nasdaq_index",
    "universe_snapshots",
    "terminal_returns",
    "confirmed_price_adjustments",
    "confirmed_listings",
    "security_identity",
    "reviewed_market_moves",
)


def data_manifest_sha256_from_components(
    components: dict[str, str],
) -> str:
    canonical = json.dumps(
        components,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_data_manifest(fingerprints: dict) -> dict:
    components = {
        name: str(fingerprints[name]["sha256"])
        for name in CAN_SLIM_DATA_COMPONENTS
    }
    return {
        "schema_version": 1,
        "sha256": data_manifest_sha256_from_components(components),
        "components": components,
    }


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(PROJECT_PATH).resolve()).as_posix()
    except ValueError:
        return str(path)


def fingerprint_file(path: str | Path) -> dict:
    """Return a content hash and size for one input file."""
    target = Path(path)
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    result = {
        "path": _display_path(target),
        "sha256": digest.hexdigest(),
        "bytes": target.stat().st_size,
    }
    return result


def fingerprint_tree(path: str | Path, pattern: str = "*.csv") -> dict:
    """Hash a directory from sorted relative paths and per-file content hashes."""
    root = Path(path)
    files = sorted(item for item in root.rglob(pattern) if item.is_file())
    tree_digest = hashlib.sha256()
    total_bytes = 0
    for item in files:
        file_result = fingerprint_file(item)
        relative = item.relative_to(root).as_posix()
        tree_digest.update(relative.encode("utf-8"))
        tree_digest.update(b"\0")
        tree_digest.update(file_result["sha256"].encode("ascii"))
        tree_digest.update(b"\n")
        total_bytes += int(file_result["bytes"])
    return {
        "path": _display_path(root),
        "pattern": pattern,
        "sha256": tree_digest.hexdigest(),
        "file_count": len(files),
        "bytes": total_bytes,
    }


def fingerprint_files(paths: list[str | Path] | tuple[str | Path, ...]) -> dict:
    """Hash an explicit code dependency set without including unrelated files."""
    root = Path(PROJECT_PATH).resolve()
    targets = sorted(
        (Path(path) if Path(path).is_absolute() else root / path).resolve()
        for path in paths
    )
    digest = hashlib.sha256()
    total_bytes = 0
    display_paths = []
    for target in targets:
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"Fingerprint file must be inside project root: {target}"
            ) from exc
        result = fingerprint_file(target)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(result["sha256"].encode("ascii"))
        digest.update(b"\n")
        total_bytes += int(result["bytes"])
        display_paths.append(relative)
    return {
        "path": "explicit_project_files",
        "files": display_paths,
        "sha256": digest.hexdigest(),
        "file_count": len(targets),
        "bytes": total_bytes,
    }


def _project_module_path(module: str, root: Path) -> Path | None:
    if not module.startswith("src"):
        return None
    candidate = root.joinpath(*module.split(".")).with_suffix(".py")
    if candidate.is_file():
        return candidate
    package = root.joinpath(*module.split(".")) / "__init__.py"
    return package if package.is_file() else None


def _project_package_initializers(module: str, root: Path) -> list[Path]:
    parts = module.split(".")
    return [
        initializer
        for depth in range(1, len(parts))
        if (initializer := root.joinpath(
            *parts[:depth], "__init__.py"
        )).is_file()
    ]


def project_python_dependency_closure(
    roots: list[str | Path] | tuple[str | Path, ...],
) -> tuple[str, ...]:
    """Resolve recursive project-local imports from explicit Python roots."""
    project_root = Path(PROJECT_PATH).resolve()
    pending = [
        (Path(path) if Path(path).is_absolute() else project_root / path).resolve()
        for path in roots
    ]
    for root_path in list(pending):
        relative_root = root_path.relative_to(project_root)
        pending.extend(
            initializer
            for parent in relative_root.parents
            if parent != Path(".")
            and (initializer := project_root / parent / "__init__.py").is_file()
        )
    discovered: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in discovered:
            continue
        try:
            relative = path.relative_to(project_root)
        except ValueError as exc:
            raise ValueError(
                f"Strategy dependency must be inside project root: {path}"
            ) from exc
        if not path.is_file():
            raise FileNotFoundError(f"Strategy dependency is missing: {path}")
        discovered.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module:
                    modules.append(node.module)
        for module in modules:
            dependency = _project_module_path(module, project_root)
            if dependency is not None and dependency not in discovered:
                pending.append(dependency)
            pending.extend(
                initializer
                for initializer in _project_package_initializers(
                    module, project_root
                )
                if initializer not in discovered
            )
    return tuple(sorted(path.relative_to(project_root).as_posix() for path in discovered))


def can_slim_input_fingerprints() -> dict:
    """Fingerprint mutable data and code used by the fixed Top 3 replay."""
    nasdaq_data = Path(PROJECT_PATH) / "stocks_list_dir/nasdaq"
    strategy_files = project_python_dependency_closure(
        CAN_SLIM_STRATEGY_CODE_ROOTS
    )
    strategy_code = fingerprint_files(strategy_files)
    strategy_code.update({
        "roots": list(CAN_SLIM_STRATEGY_CODE_ROOTS),
        "dependency_resolution": "recursive_ast_project_imports",
    })
    result = {
        "strategy_code": strategy_code,
        "source_code": fingerprint_tree(
            Path(PROJECT_PATH) / "src", pattern="*.py"
        ),
        "price_data": fingerprint_tree(CLEANED_PRICE_DATA_DIR),
        "price_data_provenance": fingerprint_tree(
            Path(PROJECT_PATH) / "output/data_provenance",
            pattern="*.json",
        ),
        "eps": fingerprint_file(POINT_IN_TIME_EPS_FILE),
        "quarterly_fundamentals": fingerprint_file(
            POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
        ),
        "nasdaq_index": fingerprint_file(NASDAQ_INDEX_FILE),
        "universe_snapshots": fingerprint_tree(snapshot_directory()),
        "terminal_returns": fingerprint_file(
            nasdaq_data / "terminal_returns.csv"
        ),
        "confirmed_price_adjustments": fingerprint_file(
            nasdaq_data / "confirmed_price_adjustments.csv"
        ),
        "confirmed_listings": fingerprint_file(
            nasdaq_data / "confirmed_listings.csv"
        ),
        "security_identity": fingerprint_file(
            nasdaq_data / "security_identity.csv"
        ),
        "reviewed_market_moves": fingerprint_file(
            nasdaq_data / "reviewed_market_moves.csv"
        ),
    }
    result["data_manifest"] = build_data_manifest(result)
    return result
