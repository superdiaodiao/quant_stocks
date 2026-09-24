"""Project-local Python import closure and its content digest.

A frozen runtime is only as frozen as the code it imports.  These helpers
resolve the transitive project-local imports (``src.*`` and ``scripts.*``) of
explicit root files, so a protocol can bind every file that participates in a
run instead of a hand-picked subset.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path


PROJECT_PACKAGES = ("src", "scripts")


def _module_file(module: str, root: Path) -> Path | None:
    parts = module.split(".")
    if not parts or parts[0] not in PROJECT_PACKAGES:
        return None
    candidate = root.joinpath(*parts).with_suffix(".py")
    if candidate.is_file():
        return candidate
    package = root.joinpath(*parts, "__init__.py")
    return package if package.is_file() else None


def _package_initializers(parts: tuple[str, ...], root: Path) -> list[Path]:
    """Return the ``__init__.py`` files executed when importing ``parts``."""
    return [
        initializer
        for depth in range(1, len(parts))
        if (initializer := root.joinpath(*parts[:depth], "__init__.py")).is_file()
    ]


def _imported_modules(path: Path, relative: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = relative.with_suffix("").parts[:-1]
    if relative.name == "__init__.py":
        package = relative.parent.parts
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package[: len(package) - node.level + 1]
                base = ".".join([*anchor, *(node.module or "").split(".")])
                base = base.strip(".")
            else:
                base = node.module or ""
            if not base:
                continue
            modules.add(base)
            # ``from scripts import research_vNN as vNN`` imports submodules.
            modules.update(
                f"{base}.{alias.name}" for alias in node.names if alias.name != "*"
            )
    return modules


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_import_closure(
    roots: list[str | Path] | tuple[str | Path, ...],
    root_dir: str | Path,
) -> dict[str, str]:
    """Map every project file reachable from ``roots`` to its SHA-256."""
    root_dir = Path(root_dir).resolve()
    pending = [
        (Path(item) if Path(item).is_absolute() else root_dir / item).resolve()
        for item in roots
    ]
    discovered: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in discovered:
            continue
        try:
            relative = path.relative_to(root_dir)
        except ValueError as exc:
            raise ValueError(f"closure file is outside the project: {path}") from exc
        if not path.is_file():
            raise FileNotFoundError(f"closure file is missing: {relative}")
        discovered.add(path)
        pending.extend(
            item
            for item in _package_initializers(relative.parts, root_dir)
            if item not in discovered
        )
        for module in _imported_modules(path, relative):
            target = _module_file(module, root_dir)
            if target is not None and target not in discovered:
                pending.append(target)
            if module.split(".")[0] in PROJECT_PACKAGES:
                pending.extend(
                    item
                    for item in _package_initializers(
                        tuple(module.split(".")), root_dir
                    )
                    if item not in discovered
                )
    return {
        path.relative_to(root_dir).as_posix(): _file_sha256(path)
        for path in sorted(discovered)
    }


def closure_digest(files: dict[str, str]) -> str:
    """Content-address a closure independently of dictionary ordering."""
    lines = "".join(f"{path}\t{digest}\n" for path, digest in sorted(files.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def closure_differences(
    expected: dict[str, str], actual: dict[str, str]
) -> dict[str, list[str]]:
    return {
        "changed": sorted(
            path
            for path in expected.keys() & actual.keys()
            if expected[path] != actual[path]
        ),
        "added": sorted(actual.keys() - expected.keys()),
        "removed": sorted(expected.keys() - actual.keys()),
    }
