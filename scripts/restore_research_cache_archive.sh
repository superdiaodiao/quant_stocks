#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
catalog="${1:-$project_dir/research_cache/sec-companyfacts-2025-04-14.json}"
download_dir="${2:-$project_dir/dist/research-cache-restore}"
restore_parent="${3:-}"

if [[ -z "$restore_parent" ]]; then
  echo "usage: $0 [catalog] [parts-dir] <restore-parent>" >&2
  echo "restore-parent is required so an existing research cache is never overwritten implicitly" >&2
  exit 2
fi

for command_name in gh jq zstd tar shasum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "missing command: $command_name" >&2
    exit 1
  fi
done
if [[ ! -f "$catalog" ]]; then
  echo "missing catalog: $catalog" >&2
  exit 1
fi

repository="$(jq -r '.repository' "$catalog")"
release_tag="$(jq -r '.release_tag' "$catalog")"
snapshot_id="$(jq -r '.snapshot.id' "$catalog")"
restore_target="$restore_parent/$snapshot_id"
if [[ -e "$restore_target" ]]; then
  echo "refusing to overwrite existing restore target: $restore_target" >&2
  exit 1
fi

mkdir -p "$download_dir" "$restore_parent"
while IFS= read -r asset; do
  if [[ ! -f "$download_dir/$asset" ]]; then
    gh release download "$release_tag" --repo "$repository" \
      --pattern "$asset" --dir "$download_dir"
  fi
done < <(jq -r '.archive.parts[].name' "$catalog")

PYTHONPATH="$project_dir" "$project_dir/.venv/bin/python" \
  "$project_dir/scripts/research_cache_catalog.py" verify-parts \
  --catalog "$catalog" --parts-dir "$download_dir"

assembled="$(mktemp "$download_dir/${snapshot_id}.XXXXXX.tar.zst")"
cleanup() {
  rm -f "$assembled"
}
trap cleanup EXIT

: > "$assembled"
while IFS= read -r asset; do
  command cat "$download_dir/$asset" >> "$assembled"
done < <(jq -r '.archive.parts[].name' "$catalog")

expected_archive_sha="$(jq -r '.archive.sha256' "$catalog")"
actual_archive_sha="$(shasum -a 256 "$assembled" | awk '{print $1}')"
if [[ "$actual_archive_sha" != "$expected_archive_sha" ]]; then
  echo "assembled archive SHA-256 mismatch" >&2
  exit 1
fi

zstd -dc "$assembled" | tar -xf - -C "$restore_parent"
PYTHONPATH="$project_dir" "$project_dir/.venv/bin/python" \
  "$project_dir/scripts/companyfacts_cache_snapshot.py" \
  --verify-snapshot "$restore_target"

echo "Restored and verified: $restore_target"
