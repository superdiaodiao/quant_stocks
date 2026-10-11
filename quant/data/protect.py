"""Protection of the frozen data versions (docs/architecture.md section 13).

A data version with a ``FROZEN_UTC`` (``quant.data.version``; today v1, v2 and v2.1) must never change again. Three
guards, besides the pipeline's own refusal to write a frozen version without ``--allow-frozen-rebuild``:

- **manifests** (``state/data_manifests/<version>.json.gz``, committed): the path, size and sha256 of every file of the
  version's cache (``research_cache/<cache>``) and of its committed inputs (``output/research_only/
  reversal_2012_2026/<inputs>``, the files git tracks), plus optional untracked inputs the backups also carry
  (``special_distributions.csv``: checked when present). Paths, sizes and hashes only, no vendor content.
- **verify**: ``python -m quant data verify [--version v1|v2|v2.1|all]`` compares the live cache and the inputs of this
  checkout with the manifest and exits non-zero on any changed, missing or unexpected file (``--quick``: sizes only).
- **lock / unlock**: ``python -m quant data lock`` removes the write bits of every file and folder of the frozen
  caches (so a stray write fails with PermissionError instead of silently changing a file); ``unlock`` gives the
  owner's write bit back (for a deliberate rebuild, after which the manifests are regenerated on purpose).

``python -m quant data manifest --version V`` writes a manifest from the current files (refuses to overwrite one
without ``--force``); ``python -m quant data quota`` prints the Tiingo account's unique symbols this month across
every ledger (``quant.data.sources.tiingo``). Folder names Finder or Python leave behind (``.DS_Store``,
``__pycache__``, ``._*``) are skipped, as in the backups (scripts/data_backup_manifest.py).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from quant.data import version as dv
from quant.paths import ROOT

MANIFEST_DIR = ROOT / "state/data_manifests"
SCHEMA = 1
SKIP_NAMES = {".DS_Store", "__pycache__", ".pytest_cache", ".ruff_cache", ".ipynb_checkpoints"}
SKIP_SUFFIXES = (".pyc", ".pyo")
# untracked inputs a build writes next to the committed ones (git-ignored; the backups carry them): checked when present
OPTIONAL_INPUTS = ("special_distributions.csv",)
WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def skipped(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES) or name.startswith("._")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def scan(root: Path) -> dict[str, os.stat_result]:
    """Every file under ``root`` (relative POSIX path -> lstat), skipping ``SKIP_NAMES``; a symlink is reported as a
    file (its own lstat), so it shows up as changed or unexpected rather than being followed."""
    out: dict[str, os.stat_result] = {}
    if not root.is_dir():
        return out
    stack = [root]
    while stack:
        folder = stack.pop()
        with os.scandir(folder) as entries:
            for e in entries:
                if skipped(e.name):
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                else:
                    out[Path(e.path).relative_to(root).as_posix()] = e.stat(follow_symlinks=False)
    return out


def hash_files(root: Path, rels, workers: int = 8) -> dict[str, str]:
    rels = sorted(rels)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(rels, pool.map(lambda r: sha256(root / r), rels)))


def manifest_path(version: str, directory: Path | None = None) -> Path:
    return (MANIFEST_DIR if directory is None else Path(directory)) / f"{version.replace('.', '_')}.json.gz"


def tracked_files(checkout: Path, rel_dir: Path) -> list[str]:
    """The files git tracks under ``rel_dir`` of ``checkout`` (relative to ``rel_dir``)."""
    out = subprocess.run(["git", "-C", str(checkout), "ls-files", "-z", "--", str(rel_dir)], check=True,
                         capture_output=True).stdout.decode()
    prefix = rel_dir.as_posix().rstrip("/") + "/"
    return sorted(p[len(prefix):] for p in out.split("\0") if p.startswith(prefix))


# ======================================================================== manifests

def build_manifest(version: str, inputs_root: Path | None = None, cache: Path | None = None,
                   tracked: list[str] | None = None) -> dict:
    """The manifest of ``version`` from the current files: the cache folder (all files) and the committed inputs of
    the checkout ``inputs_root`` (default: this one; ``tracked``: their names, default the files git tracks there),
    plus the optional untracked inputs found there."""
    inputs_root = ROOT if inputs_root is None else Path(inputs_root)
    cache = dv.cache_dir(version) if cache is None else Path(cache)
    rel_inputs = dv.INPUTS_PARENT / dv.INPUTS_NAME[version]
    inputs = inputs_root / rel_inputs
    if not cache.is_dir():
        raise SystemExit(f"{version}: no cache folder at {cache}")
    cstat = scan(cache)
    chash = hash_files(cache, cstat)
    tracked = sorted(tracked) if tracked is not None else tracked_files(inputs_root, rel_inputs)
    missing = [r for r in tracked if not (inputs / r).is_file()]
    if missing:
        raise SystemExit(f"{version}: committed inputs missing in {inputs}: {missing[:5]}")
    ihash = hash_files(inputs, tracked)
    optional = [r for r in OPTIONAL_INPUTS if r not in tracked and (inputs / r).is_file()]
    ohash = hash_files(inputs, optional)

    def rows(root, hashes):
        return [[r, (root / r).lstat().st_size, h] for r, h in sorted(hashes.items())]

    cache_rows = rows(cache, chash)
    return {
        "schema": SCHEMA, "version": version, "frozen_utc": dv.FROZEN_UTC.get(version),
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cache": {"root": f"research_cache/{dv.CACHE_NAME[version]}", "file_count": len(cache_rows),
                  "total_bytes": sum(r[1] for r in cache_rows), "files": cache_rows},
        "inputs": {"root": rel_inputs.as_posix(), "files": rows(inputs, ihash)},
        "optional_inputs": {"root": rel_inputs.as_posix(), "files": rows(inputs, ohash)},
    }


def write_manifest(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(manifest, indent=0, separators=(",", ":")) + "\n").encode()
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
        gz.write(data)
    os.replace(tmp, path)


def read_manifest(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


# ======================================================================== verify

def compare(root: Path, rows: list, quick: bool = False, optional: bool = False,
            expected_extra: set | None = None) -> dict:
    """Files of ``root`` against manifest ``rows`` ([path, size, sha256]): changed, missing and unexpected paths.
    ``optional``: a missing file is not a problem. ``expected_extra``: paths on disk that belong to another part of
    the manifest (not unexpected here)."""
    want = {r[0]: (r[1], r[2]) for r in rows}
    have = scan(root)
    missing = sorted(p for p in want if p not in have)
    unexpected = sorted(p for p in have if p not in want and p not in (expected_extra or set()))
    common = sorted(p for p in want if p in have)
    changed = [p for p in common if have[p].st_size != want[p][0] or stat.S_ISLNK(have[p].st_mode)]
    if not quick:
        rest = [p for p in common if p not in set(changed)]
        hashes = hash_files(root, rest)
        changed += [p for p in rest if hashes[p] != want[p][1]]
    writable = sum(1 for p in common if have[p].st_mode & WRITE_BITS)
    return {"checked": len(common), "changed": sorted(changed), "missing": [] if optional else missing,
            "absent_optional": missing if optional else [], "unexpected": unexpected, "writable_files": writable}


def verify_version(version: str, directory: Path | None = None, inputs_root: Path | None = None,
                   cache: Path | None = None, quick: bool = False) -> dict:
    path = manifest_path(version, directory)
    if not path.is_file():
        return {"version": version, "ok": False, "error": f"no manifest {path}"}
    m = read_manifest(path)
    inputs_root = ROOT if inputs_root is None else Path(inputs_root)
    cache = dv.cache_dir(version) if cache is None else Path(cache)
    inputs = inputs_root / m["inputs"]["root"]
    out = {"version": version, "manifest": str(path), "cache_dir": str(cache), "inputs_dir": str(inputs)}
    if not cache.is_dir():
        out.update(ok=False, error=f"no cache folder at {cache}")
        return out
    out["cache"] = compare(cache, m["cache"]["files"], quick)
    optional_names = {r[0] for r in m["optional_inputs"]["files"]} | set(OPTIONAL_INPUTS)
    out["inputs"] = compare(inputs, m["inputs"]["files"], quick, expected_extra=optional_names)
    out["optional_inputs"] = compare(inputs, m["optional_inputs"]["files"], quick, optional=True,
                                     expected_extra={r[0] for r in m["inputs"]["files"]} | optional_names)
    out["optional_inputs"]["unexpected"] = []     # reported by the committed-inputs check
    problems = sum(len(out[k][c]) for k in ("cache", "inputs", "optional_inputs")
                   for c in ("changed", "missing", "unexpected"))
    out["ok"] = problems == 0
    out["locked"] = out["cache"]["writable_files"] == 0
    return out


def print_report(r: dict, limit: int = 20) -> None:
    v = r["version"]
    if "error" in r:
        print(f"{v}: FAILED: {r['error']}")
        return
    print(f"{v}: {'OK' if r['ok'] else 'FAILED'} ({r['manifest']})")
    for part in ("cache", "inputs", "optional_inputs"):
        c = r[part]
        where = r["cache_dir"] if part == "cache" else r["inputs_dir"]
        line = (f"  {part}: {c['checked']} files checked in {where}; changed {len(c['changed'])}, missing "
                f"{len(c['missing'])}, unexpected {len(c['unexpected'])}")
        if c["absent_optional"]:
            line += f", absent (optional, untracked) {len(c['absent_optional'])}: {' '.join(c['absent_optional'])}"
        print(line)
        for kind in ("changed", "missing", "unexpected"):
            for p in c[kind][:limit]:
                print(f"    {kind}: {p}")
            if len(c[kind]) > limit:
                print(f"    ... {len(c[kind]) - limit} more {kind}")
    print(f"  cache {'locked (read-only)' if r['locked'] else 'not locked'}: "
          f"{r['cache']['writable_files']} writable files")


# ======================================================================== lock / unlock

def set_writable(root: Path, writable: bool) -> int:
    """Remove (``writable=False``) every write bit, or give the owner's write bit back, on every file and folder
    under ``root`` (symlinks are left alone). Returns the number of entries changed."""
    changed = 0
    for current, dirs, files in os.walk(root):
        for name in dirs + files + ([""] if current == str(root) else []):
            p = os.path.join(current, name) if name else current
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                continue
            mode = stat.S_IMODE(st.st_mode)
            new = (mode | stat.S_IWUSR) if writable else (mode & ~WRITE_BITS)
            if new != mode:
                os.chmod(p, new)
                changed += 1
    return changed


def chosen(version: str) -> list[str]:
    frozen = dv.frozen_versions()
    if version == "all":
        return frozen
    if version not in dv.VERSIONS:
        raise SystemExit(f"unknown version {version!r}: one of {', '.join(dv.VERSIONS)} or all")
    return [version]


# ======================================================================== command line

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m quant data", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    vh = "v1, v2, v2.1 or all (default: all frozen versions)"
    p = sub.add_parser("verify", help="check the frozen caches and inputs against the committed manifests")
    p.add_argument("--version", default="all", help=vh)
    p.add_argument("--quick", action="store_true", help="sizes only, no hashing")
    p.add_argument("--manifest-dir", type=Path, default=None, help=f"default {MANIFEST_DIR}")
    p.add_argument("--inputs-root", type=Path, default=None, help="checkout whose inputs are checked (default: this)")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    p = sub.add_parser("manifest", help="write a version's manifest from the current files (deliberate)")
    p.add_argument("--version", required=True, help=vh)
    p.add_argument("--manifest-dir", type=Path, default=None)
    p.add_argument("--inputs-root", type=Path, default=None, help="checkout whose committed inputs are hashed")
    p.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    for name, text in (("lock", "make the frozen version caches read-only"),
                       ("unlock", "make the frozen version caches writable again (owner write bit)")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--version", default="all", help=vh)
    sub.add_parser("quota", help="the Tiingo account's unique symbols this month, across every ledger")
    a = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    if a.cmd == "quota":
        from quant.data.sources import tiingo
        month = tiingo.this_month()
        ledgers = tiingo.account_ledgers()
        total = tiingo.month_symbols(month, ledgers)
        for path in ledgers:
            print(f"{len(tiingo.ledger_symbols(path, month)):4d}  {path}")
        print(f"{len(total):4d}  unique symbols in {month} across {len(ledgers)} ledgers "
              f"(the free tier allows {tiingo.MONTHLY_SYMBOLS}; local callers stop at 480)")
        return 0
    if a.cmd == "manifest":
        rc = 0
        for v in chosen(a.version):
            path = manifest_path(v, a.manifest_dir)
            if path.exists() and not a.force:
                print(f"{v}: {path} exists (the frozen reference); --force overwrites it")
                rc = 1
                continue
            m = build_manifest(v, a.inputs_root)
            write_manifest(m, path)
            print(f"{v}: {path}: {m['cache']['file_count']} cache files ({m['cache']['total_bytes']:,} bytes), "
                  f"{len(m['inputs']['files'])} committed inputs, {len(m['optional_inputs']['files'])} optional")
        return rc
    if a.cmd in ("lock", "unlock"):
        for v in chosen(a.version):
            root = dv.cache_dir(v)
            if not root.is_dir():
                print(f"{v}: no cache folder at {root}")
                continue
            n = set_writable(root, writable=a.cmd == "unlock")
            print(f"{v}: {root} {'locked (read-only)' if a.cmd == 'lock' else 'unlocked (writable)'}: "
                  f"{n} entries changed")
        return 0
    results = [verify_version(v, a.manifest_dir, a.inputs_root, quick=a.quick) for v in chosen(a.version)]
    if a.json:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            print_report(r)
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
