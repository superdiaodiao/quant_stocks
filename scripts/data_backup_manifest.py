#!/usr/bin/env python3
"""File lists, manifests and checks for scripts/backup_data.sh and restore_data.sh.

build     read "root<TAB>relative path" lines on stdin, walk them, and write a
          bsdtar -T list plus a draft manifest (path, size, sha256 per file)
finalize  add the archive and part hashes to the draft manifest
verify-tree   an extracted tree must hold exactly the manifest's files
verify-live   compare the manifest with the live files it was made from
download  download a release's assets with parallel HTTP range requests
          (GitHub serves each connection slowly from some networks); every
          asset is checked against the SHA-256 digest GitHub reports
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SKIP_NAMES = {".DS_Store", "__pycache__", ".pytest_cache", ".ruff_cache", ".ipynb_checkpoints"}
SKIP_SUFFIXES = (".pyc", ".pyo")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def skipped(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES) or name.startswith("._")


def walk(root: Path, relative: str):
    start = root / relative
    if start.is_symlink():
        raise SystemExit(f"refusing symlink in a dataset: {start}")
    if start.is_file():
        yield relative
        return
    if not start.is_dir():
        raise SystemExit(f"missing dataset path: {start}")
    for current, dirs, files in os.walk(start):
        dirs[:] = sorted(d for d in dirs if not skipped(d))
        for name in sorted(files):
            if skipped(name):
                continue
            path = Path(current) / name
            if path.is_symlink():
                raise SystemExit(f"refusing symlink in a dataset: {path}")
            yield str(path.relative_to(root))


def build(args) -> None:
    entries: dict[str, dict] = {}
    roots: dict[str, str] = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        root_text, relative = line.split("\t", 1)
        root = Path(root_text).resolve()
        roots[relative] = str(root)
        for rel in walk(root, relative):
            if rel in entries:
                raise SystemExit(f"path listed twice: {rel}")
            path = root / rel
            entries[rel] = {"path": rel, "size": path.stat().st_size, "sha256": sha256(path),
                            "_root": str(root)}
    if not entries:
        raise SystemExit("dataset is empty")
    ordered = sorted(entries.values(), key=lambda e: (e["_root"], e["path"]))
    with open(args.tar_list, "w", encoding="utf-8") as handle:
        current = None
        for entry in ordered:
            if entry["_root"] != current:
                current = entry["_root"]
                handle.write(f"-C\n{current}\n")
            handle.write(entry["path"] + "\n")
    files = [{k: v for k, v in e.items() if k != "_root"} for e in sorted(ordered, key=lambda e: e["path"])]
    manifest = {
        "schema_version": 1,
        "dataset": args.dataset,
        "tag": args.tag,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [{"path": rel, "root": root} for rel, root in sorted(roots.items())],
        "file_count": len(files),
        "total_bytes": sum(f["size"] for f in files),
        "files": files,
    }
    Path(args.manifest).write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(files)} files, {manifest['total_bytes']} bytes")


def finalize(args) -> None:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    parts = [Path(p) for p in args.parts]
    archive = hashlib.sha256()
    for part in parts:
        with open(part, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                archive.update(block)
    manifest["archive"] = {
        "name": args.archive_name,
        "format": "tar (pax) + zstd, split",
        "sha256": archive.hexdigest(),
        "bytes": sum(p.stat().st_size for p in parts),
        "parts": [{"name": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)} for p in parts],
        "restore": f"scripts/restore_data.sh {manifest['tag']} <dest>",
    }
    # Keep the file list last so the summary is readable at the top.
    files = manifest.pop("files")
    manifest["files"] = files
    Path(args.manifest).write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    with open(args.sums, "w", encoding="utf-8") as handle:
        for part in manifest["archive"]["parts"]:
            handle.write(f"{part['sha256']}  {part['name']}\n")


def verify_tree(args) -> None:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    root = Path(args.dir)
    expected = {f["path"]: f for f in manifest["files"]}
    found = set()
    for current, _dirs, files in os.walk(root):
        for name in files:
            found.add(str((Path(current) / name).relative_to(root)))
    bad = []
    for rel in sorted(set(expected) - found):
        bad.append(f"missing {rel}")
    for rel in sorted(found - set(expected)):
        bad.append(f"extra {rel}")
    for rel in sorted(set(expected) & found):
        path = root / rel
        entry = expected[rel]
        if path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
            bad.append(f"changed {rel}")
    if bad:
        print("\n".join(bad[:50]))
        raise SystemExit(f"verify-tree FAILED: {len(bad)} problems")
    print(f"verify-tree OK: {len(expected)} files match the manifest")


def verify_live(args) -> None:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    sources = manifest["sources"]
    listed = {f["path"]: f for f in manifest["files"]}
    live: dict[str, Path] = {}
    for source in sources:
        root = Path(source["root"])
        if not (root / source["path"]).exists():
            continue
        for rel in walk(root, source["path"]):
            live[rel] = root / rel
    changed = [r for r in sorted(set(listed) & set(live))
               if live[r].stat().st_size != listed[r]["size"] or sha256(live[r]) != listed[r]["sha256"]]
    missing = sorted(set(listed) - set(live))
    added = sorted(set(live) - set(listed))
    print(f"verify-live: {len(listed)} in manifest; changed {len(changed)}, "
          f"gone {len(missing)}, new since backup {len(added)}")
    for label, items in (("changed", changed), ("gone", missing), ("new", added)):
        for rel in items[:20]:
            print(f"  {label} {rel}")
    if changed or missing or added:
        raise SystemExit(1)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - keep the Location
        return None


def _gh_json(path: str):
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


class _Asset:
    """One release asset; the signed download URL is renewed when it expires."""

    def __init__(self, api_url: str, token: str):
        self.api_url = api_url
        self.token = token
        self.lock = threading.Lock()
        self.url = None
        self.fetched = 0.0

    def signed_url(self, renew: bool = False) -> str:
        with self.lock:
            if renew or self.url is None or time.time() - self.fetched > 240:
                request = urllib.request.Request(self.api_url, headers={
                    "Accept": "application/octet-stream",
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent": "quant-stocks-data-backup"})
                opener = urllib.request.build_opener(_NoRedirect)
                try:
                    opener.open(request, timeout=60)
                    raise RuntimeError("asset request was not redirected")
                except urllib.error.HTTPError as error:
                    if error.code not in (301, 302, 303, 307, 308):
                        raise
                    self.url = error.headers["Location"]
                self.fetched = time.time()
            return self.url


def _fetch_range(asset: _Asset, target: Path, start: int, end: int) -> None:
    for attempt in range(8):
        try:
            request = urllib.request.Request(asset.signed_url(renew=attempt > 0), headers={
                "Range": f"bytes={start}-{end}", "User-Agent": "quant-stocks-data-backup"})
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
            if len(data) != end - start + 1:
                raise IOError(f"short read {len(data)} of {end - start + 1}")
            with open(target, "r+b") as handle:
                handle.seek(start)
                handle.write(data)
            return
        except Exception as error:  # retried; the final hash check is the guard
            if attempt == 7:
                raise RuntimeError(f"range {start}-{end} of {target.name} failed: {error}") from None
            time.sleep(2 + attempt * 5)


def download(args) -> None:
    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    release = _gh_json(f"repos/{args.repo}/releases/tags/{args.tag}")
    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    assets = [a for a in release["assets"] if not args.pattern or any(
        Path(a["name"]).match(p) for p in args.pattern)]
    if not assets:
        raise SystemExit(f"no assets in {args.repo} {args.tag}")
    chunk = args.chunk_mib << 20
    jobs = []
    for meta in assets:
        target = out / meta["name"]
        with open(target, "wb") as handle:
            handle.truncate(meta["size"])
        asset = _Asset(meta["url"], token)
        for start in range(0, meta["size"], chunk):
            jobs.append((asset, target, start, min(start + chunk, meta["size"]) - 1))
    total = sum(a["size"] for a in assets)
    began = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in [pool.submit(_fetch_range, *job) for job in jobs]:
            future.result()
    seconds = max(time.time() - began, 1e-6)
    bad = []
    for meta in assets:
        digest = (meta.get("digest") or "").removeprefix("sha256:")
        actual = sha256(out / meta["name"])
        if digest and digest != actual:
            bad.append(meta["name"])
        print(f"{actual}  {meta['name']}  ({meta['size']} bytes; GitHub digest "
              f"{'matches' if digest == actual else ('MISSING' if not digest else 'DIFFERS')})")
    print(f"downloaded {total} bytes in {seconds:.0f}s ({total / seconds / 1e6:.2f} MB/s)")
    if bad:
        raise SystemExit("downloaded assets differ from GitHub's digest: " + ", ".join(bad))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build")
    p.add_argument("--dataset", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--tar-list", required=True)
    p.add_argument("--manifest", required=True)
    p.set_defaults(func=build)
    p = sub.add_parser("finalize")
    p.add_argument("--manifest", required=True)
    p.add_argument("--archive-name", required=True)
    p.add_argument("--sums", required=True)
    p.add_argument("parts", nargs="+")
    p.set_defaults(func=finalize)
    p = sub.add_parser("verify-tree")
    p.add_argument("--manifest", required=True)
    p.add_argument("--dir", required=True)
    p.set_defaults(func=verify_tree)
    p = sub.add_parser("verify-live")
    p.add_argument("--manifest", required=True)
    p.set_defaults(func=verify_live)
    p = sub.add_parser("download")
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--pattern", action="append", help="glob on the asset name; repeatable")
    p.add_argument("--workers", type=int, default=int(os.environ.get("QUANT_DOWNLOAD_WORKERS", "48")))
    p.add_argument("--chunk-mib", type=int, default=8)
    p.set_defaults(func=download)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
