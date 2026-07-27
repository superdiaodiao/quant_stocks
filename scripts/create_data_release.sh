#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
version="${1:-$(date +%Y-%m-%d)}"
output_dir="${2:-$project_dir/dist}"
archive_name="quant_stocks_data_${version}.tar.zst"
archive_path="$output_dir/$archive_name"
checksum_path="$archive_path.sha256"
manifest_path="$output_dir/quant_stocks_data_${version}.json"
file_list="$(mktemp)"

cleanup() {
    rm -f "$file_list"
}
trap cleanup EXIT

for command_name in tar zstd; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "缺少命令：$command_name" >&2
        exit 1
    fi
done

data_paths=(
    "cleaned_stocks_data"
    "stocks_list_dir"
    "his_data"
)

cd "$project_dir"
for path in "${data_paths[@]}"; do
    if [[ ! -d "$path" ]]; then
        echo "缺少数据目录：$path。请先下载旧数据包或执行数据更新。" >&2
        exit 1
    fi
    find "$path" -type f -print
done | LC_ALL=C sort > "$file_list"

file_count="$(wc -l < "$file_list" | tr -d ' ')"
if [[ "$file_count" -eq 0 ]]; then
    echo "数据包中没有文件，停止生成。" >&2
    exit 1
fi

mkdir -p "$output_dir"
rm -f "$archive_path" "$checksum_path" "$manifest_path"

echo "正在生成 ${archive_name}（共 ${file_count} 个文件）..."
COPYFILE_DISABLE=1 tar -cf - -T "$file_list" \
    | zstd -19 -T0 -f -o "$archive_path"

if command -v sha256sum >/dev/null 2>&1; then
    checksum="$(sha256sum "$archive_path" | awk '{print $1}')"
else
    checksum="$(shasum -a 256 "$archive_path" | awk '{print $1}')"
fi
printf '%s  %s\n' "$checksum" "$archive_name" > "$checksum_path"

archive_bytes="$(wc -c < "$archive_path" | tr -d ' ')"
generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "$manifest_path" <<EOF
{
  "schema_version": 1,
  "data_version": "$version",
  "generated_at_utc": "$generated_at",
  "archive": "$archive_name",
  "sha256": "$checksum",
  "archive_bytes": $archive_bytes,
  "file_count": $file_count,
  "included_paths": [
    "cleaned_stocks_data",
    "stocks_list_dir",
    "his_data"
  ]
}
EOF

echo "数据包：$archive_path"
echo "校验文件：$checksum_path"
echo "清单：$manifest_path"
echo "SHA-256：$checksum"
