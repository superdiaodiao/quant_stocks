#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
metadata_file="$project_dir/data_release/latest.json"
force=false
requested_tag=""
local_archive=""

usage() {
    cat <<'EOF'
用法：
  scripts/download_data_release.sh [--tag TAG] [--archive FILE] [--force]

参数：
  --tag TAG  下载指定的数据 Release；默认使用 data_release/latest.json。
  --archive FILE
             使用已经下载的数据包，仍执行 SHA-256 和路径安全检查。
  --force    允许覆盖本地已有的数据目录。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)
            requested_tag="${2:?--tag 后必须提供 TAG}"
            shift 2
            ;;
        --force)
            force=true
            shift
            ;;
        --archive)
            local_archive="${2:?--archive 后必须提供文件路径}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

for command_name in tar zstd; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "缺少命令：$command_name" >&2
        exit 1
    fi
done

if [[ ! -f "$metadata_file" ]]; then
    echo "缺少 $metadata_file" >&2
    exit 1
fi

read_json_field() {
    local field="$1"
    python3 - "$metadata_file" "$field" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle)[sys.argv[2]]
print(value)
PY
}

repo="$(read_json_field repository)"
default_tag="$(read_json_field tag)"
archive_name="$(read_json_field archive)"
expected_sha256="$(read_json_field sha256)"
tag="${requested_tag:-$default_tag}"

if [[ -n "$requested_tag" && "$requested_tag" != "$default_tag" ]]; then
    echo "指定其他 tag 时，校验值仍必须来自对应 Release 的 JSON 清单。" >&2
    echo "请下载该 Release 后手工校验，或更新 data_release/latest.json。" >&2
    exit 2
fi

for path in cleaned_stocks_data stocks_list_dir his_data; do
    if [[ -d "$project_dir/$path" ]] && find "$project_dir/$path" -type f -print -quit | grep -q .; then
        if [[ "$force" != true ]]; then
            echo "本地 $path 已包含数据。若确认覆盖，请增加 --force。" >&2
            exit 1
        fi
    fi
done

temporary_dir="$(mktemp -d)"
cleanup() {
    rm -rf "$temporary_dir"
}
trap cleanup EXIT
archive_path="$temporary_dir/$archive_name"

if [[ -n "$local_archive" ]]; then
    if [[ ! -f "$local_archive" ]]; then
        echo "本地数据包不存在：$local_archive" >&2
        exit 1
    fi
    cp "$local_archive" "$archive_path"
else
    echo "正在下载 GitHub Release：$repo / $tag"
    if command -v gh >/dev/null 2>&1; then
        gh release download "$tag" \
            --repo "$repo" \
            --pattern "$archive_name" \
            --dir "$temporary_dir"
    else
        url="https://github.com/$repo/releases/download/$tag/$archive_name"
        curl --fail --location --retry 3 --output "$archive_path" "$url"
    fi
fi

if command -v sha256sum >/dev/null 2>&1; then
    actual_sha256="$(sha256sum "$archive_path" | awk '{print $1}')"
else
    actual_sha256="$(shasum -a 256 "$archive_path" | awk '{print $1}')"
fi
if [[ "$actual_sha256" != "$expected_sha256" ]]; then
    echo "SHA-256 校验失败。" >&2
    echo "期望：$expected_sha256" >&2
    echo "实际：$actual_sha256" >&2
    exit 1
fi

echo "校验通过，正在检查压缩包路径安全性..."
if zstd -dc "$archive_path" | tar -tf - | awk '
    /^\// { bad=1 }
    /(^|\/)\.\.($|\/)/ { bad=1 }
    END { exit bad ? 1 : 0 }
'; then
    :
else
    echo "压缩包包含不安全路径，拒绝解压。" >&2
    exit 1
fi

echo "正在解压到 $project_dir"
if [[ "$force" == true ]]; then
    for path in cleaned_stocks_data stocks_list_dir his_data; do
        rm -rf "$project_dir/$path"
    done
fi
zstd -dc "$archive_path" | tar -xf - -C "$project_dir"
echo "数据恢复完成。建议继续运行："
echo "  PYTHONPATH=. .venv/bin/python -m src.research.data_audit --as-of YYYY-MM-DD"
