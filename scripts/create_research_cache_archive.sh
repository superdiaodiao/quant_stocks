#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
snapshot_id="${1:-wayback-20250414-symbols-v2}"
release_tag="${2:-research-cache-sec-companyfacts-2025-04-14}"
output_dir="${3:-$project_dir/dist/research-cache-sec-companyfacts-2025-04-14}"
snapshot_parent="$project_dir/output/data_provenance/companyfacts_historical_snapshots"
snapshot="$snapshot_parent/$snapshot_id"
archive_id="sec-companyfacts-${snapshot_id}"
archive="$output_dir/${archive_id}.tar.zst"
parts_prefix="${archive_id}.tar.zst.part-"
catalog="$project_dir/research_cache/sec-companyfacts-2025-04-14.json"
source_evidence="$project_dir/output/data_provenance/companyfacts_wayback_warc_extraction.json"
variant_audit="$project_dir/output/data_provenance/companyfacts_snapshot_variant_audit_2026-08-10.json"
file_list="$(mktemp)"

cleanup() {
  rm -f "$file_list"
}
trap cleanup EXIT

for command_name in tar zstd split shasum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "missing command: $command_name" >&2
    exit 1
  fi
done
if [[ ! -d "$snapshot" ]]; then
  echo "missing canonical snapshot: $snapshot" >&2
  exit 1
fi
if [[ ! -f "$variant_audit" ]]; then
  echo "missing variant audit: $variant_audit" >&2
  exit 1
fi

mkdir -p "$output_dir" "$(dirname "$catalog")"
(
  cd "$snapshot_parent"
  find "$snapshot_id" -type f -print | LC_ALL=C sort > "$file_list"
)
rm -f "$archive" "$output_dir/$parts_prefix"*

echo "Verifying immutable snapshot before archiving..."
PYTHONPATH="$project_dir" "$project_dir/.venv/bin/python" \
  "$project_dir/scripts/companyfacts_cache_snapshot.py" \
  --verify-snapshot "$snapshot"

echo "Creating compressed archive from $snapshot_id..."
(
  cd "$snapshot_parent"
  COPYFILE_DISABLE=1 tar -cf - -T "$file_list"
) | zstd -19 -T0 -f -o "$archive"

archive_sha256="$(shasum -a 256 "$archive" | awk '{print $1}')"
archive_bytes="$(stat -f '%z' "$archive")"
split -b 1900m -d -a 3 "$archive" "$output_dir/$parts_prefix"

PYTHONPATH="$project_dir" "$project_dir/.venv/bin/python" \
  "$project_dir/scripts/research_cache_catalog.py" create \
  --snapshot "$snapshot" \
  --archive-id "$archive_id" \
  --archive-sha256 "$archive_sha256" \
  --archive-bytes "$archive_bytes" \
  --parts-dir "$output_dir" \
  --parts-prefix "$parts_prefix" \
  --repository "superdiaodiao/quant_stocks" \
  --release-tag "$release_tag" \
  --source-evidence "$source_evidence" \
  --variant-audit "$variant_audit" \
  --output "$catalog"

PYTHONPATH="$project_dir" "$project_dir/.venv/bin/python" \
  "$project_dir/scripts/research_cache_catalog.py" verify-parts \
  --catalog "$catalog" --parts-dir "$output_dir"

rm -f "$archive"
cp "$catalog" "$output_dir/$(basename "$catalog")"
cp "$source_evidence" "$output_dir/$(basename "$source_evidence")"
cp "$variant_audit" "$output_dir/$(basename "$variant_audit")"
echo "Created catalog: $catalog"
echo "Release assets: $output_dir"
