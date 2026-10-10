#!/usr/bin/env bash
# Download a data release from the PRIVATE repo superdiaodiao/quant_stocks_data,
# verify it, and extract it.
#
#   scripts/restore_data.sh [--force] [--keep-download] <tag> [dest]
#
# dest defaults to this checkout's root; archive paths are relative to the
# project root (research_cache/..., cleaned_stocks_data/..., output/...).
# The release is checked before anything lands in dest: part SHA-256s, the
# reassembled archive's SHA-256, unsafe paths, and (for cache-* releases)
# every extracted file against the manifest. Files that already exist in dest
# with different content are not overwritten without --force.
#
# Works for the cache-* releases made by scripts/backup_data.sh and for the
# two releases copied from the public repo: data-2026-07-24 (one archive and
# its .sha256) and research-cache-sec-companyfacts-2025-04-14 (restored under
# output/data_provenance/companyfacts_historical_snapshots/).
#
# A private repo needs a logged-in gh (gh auth login) with access to it.
# QUANT_DATA_REPO overrides the repo.
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
repo="${QUANT_DATA_REPO:-superdiaodiao/quant_stocks_data}"
helper=(python3 "$project_dir/scripts/data_backup_manifest.py")
force=false
keep=false

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "restore_data: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) force=true; shift ;;
        --keep-download) keep=true; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) usage >&2; exit 2 ;;
        *) break ;;
    esac
done
[[ $# -ge 1 && $# -le 2 ]] || { usage >&2; exit 2; }
tag="$1"
dest="${2:-$project_dir}"

for command_name in gh tar zstd shasum python3; do
    command -v "$command_name" >/dev/null 2>&1 || die "missing command: $command_name"
done
gh auth status >/dev/null 2>&1 || die "gh is not logged in; run gh auth login (the data repo is private)"

mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"
work="$(mktemp -d "$dest/.restore-data.XXXXXX")"
cleanup() {
    if [[ "$keep" == true ]]; then
        echo "kept download in $work/dl"
        rm -rf "$work/x"
    else
        rm -rf "$work"
    fi
}
trap cleanup EXIT
mkdir -p "$work/dl" "$work/x"

echo "downloading $repo $tag ..."
"${helper[@]}" download --repo "$repo" --tag "$tag" --dir "$work/dl"
cd "$work/dl"

check_paths() {
    # Refuse absolute paths and "..".
    if ! zstd -dc "$@" | tar -tf - | awk '/^\// {bad=1} /(^|\/)\.\.($|\/)/ {bad=1} END {exit bad}'; then
        die "archive has unsafe paths; nothing extracted"
    fi
}

target="$dest"
manifest=""
if compgen -G "*.manifest.json" >/dev/null; then
    manifest="$(ls *.manifest.json)"
    shasum -a 256 -c *.parts.sha256
    expected="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["archive"]["sha256"])' "$manifest")"
    actual="$(cat *.tar.zst.part-* | shasum -a 256 | awk '{print $1}')"
    [[ "$actual" == "$expected" ]] || die "reassembled archive SHA-256 mismatch"
    echo "reassembled archive SHA-256 OK"
    cat *.tar.zst.part-* > "$work/archive.tar.zst"
elif compgen -G "*.tar.zst.sha256" >/dev/null; then
    # data-2026-07-24: one archive and its checksum file.
    shasum -a 256 -c *.tar.zst.sha256
    mv "$(ls *.tar.zst)" "$work/archive.tar.zst"
elif [[ -f sec-companyfacts-2025-04-14.json ]]; then
    # research-cache-sec-companyfacts-2025-04-14: parts listed in its catalog.
    catalog=sec-companyfacts-2025-04-14.json
    python3 - "$catalog" > parts.sha256 <<'PY'
import json, sys
for part in json.load(open(sys.argv[1]))["archive"]["parts"]:
    print(f"{part['sha256']}  {part['name']}")
PY
    shasum -a 256 -c parts.sha256
    expected="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["archive"]["sha256"])' "$catalog")"
    actual="$(cat *.tar.zst.part-* | shasum -a 256 | awk '{print $1}')"
    [[ "$actual" == "$expected" ]] || die "reassembled archive SHA-256 mismatch"
    cat *.tar.zst.part-* > "$work/archive.tar.zst"
    relative="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot"]["relative_restore_path"])' "$catalog")"
    target="$dest/$(dirname "$relative")"
else
    die "$tag has neither a manifest, a .sha256 file nor a known catalog"
fi
rm -f *.tar.zst.part-* 2>/dev/null || true

check_paths "$work/archive.tar.zst"
zstd -dc "$work/archive.tar.zst" | tar -xf - -C "$work/x"
rm -f "$work/archive.tar.zst"
if [[ -n "$manifest" ]]; then
    "${helper[@]}" verify-tree --manifest "$manifest" --dir "$work/x"
fi

# Files in dest that would change.
conflicts="$(cd "$work/x" && find . -type f | while IFS= read -r path; do
    if [[ -e "$target/$path" ]] && ! cmp -s "$path" "$target/$path"; then echo "$path"; fi
done)"
if [[ -n "$conflicts" && "$force" != true ]]; then
    echo "$(printf '%s\n' "$conflicts" | wc -l | tr -d ' ') files in $target differ from the release, for example:" >&2
    printf '%s\n' "$conflicts" | head -10 >&2
    die "nothing changed; rerun with --force to overwrite them"
fi

mkdir -p "$target"
(cd "$work/x" && tar -cf - .) | (cd "$target" && tar -xf -)
echo "restored $tag into $target"
if [[ -f sec-companyfacts-2025-04-14.json && -x "$project_dir/.venv/bin/python" ]]; then
    snapshot="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot"]["id"])' sec-companyfacts-2025-04-14.json)"
    PYTHONPATH="$project_dir" "$project_dir/.venv/bin/python" \
        "$project_dir/scripts/companyfacts_cache_snapshot.py" --verify-snapshot "$target/$snapshot"
fi
