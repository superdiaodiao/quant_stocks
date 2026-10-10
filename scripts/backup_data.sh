#!/usr/bin/env bash
# Back up one research dataset (or all of them) as a new dated release of the
# PRIVATE repo superdiaodiao/quant_stocks_data, then download it again and
# verify it.
#
#   scripts/backup_data.sh <dataset|all|list>
#
# Each release cache-<dataset>-<YYYY-MM-DD> holds a tar (pax) + zstd archive
# split into parts under 1.9 GiB, <base>.parts.sha256 with each part's
# SHA-256, and <base>.manifest.json with every file's path, size and SHA-256.
# Archive paths are relative to the project root, so restore_data.sh puts
# them back where they came from.
#
# Environment:
#   QUANT_DATA_REPO   target repo (default superdiaodiao/quant_stocks_data; it
#                     must stay private, the script refuses a public one)
#   QUANT_DATA_ROOT   checkout that holds research_cache/ and
#                     cleaned_stocks_data/ (default: the main checkout)
#   BACKUP_DIST       scratch directory for archives (default: a temp dir
#                     outside git), removed after a verified upload
#   BACKUP_DATE       date in the tag (default: today)
#   ZSTD_LEVEL        zstd level (default 19)
#   PART_SIZE         split size for split -b (default 256m; keep it < 1.9g)
#   UPLOAD_JOBS       parallel part uploads (default 12)
#   KEEP_DIST=1       keep the archives after a verified upload
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
repo="${QUANT_DATA_REPO:-superdiaodiao/quant_stocks_data}"
common_dir="$(git -C "$project_dir" rev-parse --path-format=absolute --git-common-dir)"
data_root="${QUANT_DATA_ROOT:-$(cd "$common_dir/.." && pwd)}"
backup_date="${BACKUP_DATE:-$(date +%Y-%m-%d)}"
zstd_level="${ZSTD_LEVEL:-19}"
# Parts must stay under 1.9 GiB (GitHub caps an asset at 2 GiB); smaller parts
# upload in parallel, which is much faster where each connection is slow.
part_size="${PART_SIZE:-256m}"
upload_jobs="${UPLOAD_JOBS:-12}"
helper=(python3 "$project_dir/scripts/data_backup_manifest.py")

# Datasets with a fixed layout. Every other directory under research_cache/
# is its own dataset, named after the directory with "_" written as "-".
# borrow-fees and forward-observation are live, mutable caches: their
# releases are dated snapshots.
special_datasets=(reversal-v1 reversal-v2 reversal-v2-1 reversal-v2-fill reversal-v2-1-alpaca
                  stocks-data research-outputs)
# Directories under research_cache/ that the special datasets already cover,
# or that live in git.
covered_cache_dirs=" reversal_2012_2026 reversal_2012_2026_v2 reversal_2012_2026_v2_1 \
reversal_2012_2026_v2_fill reversal_2012_2026_v2_1_alpaca "

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
}

die() { echo "backup_data: $*" >&2; exit 1; }

for command_name in gh tar zstd split shasum python3 git; do
    command -v "$command_name" >/dev/null 2>&1 || die "missing command: $command_name"
done

cache_root() {
    # research_cache/<dir> lives in the main checkout; a worktree may hold
    # extra caches of its own.
    if [[ -d "$data_root/research_cache/$1" && ! -L "$data_root/research_cache/$1" ]]; then
        echo "$data_root"
    elif [[ -d "$project_dir/research_cache/$1" && ! -L "$project_dir/research_cache/$1" ]]; then
        echo "$project_dir"
    else
        return 1
    fi
}

cache_datasets() {
    local dir name
    for dir in $( { ls -1 "$data_root/research_cache"; ls -1 "$project_dir/research_cache"; } 2>/dev/null | sort -u); do
        cache_root "$dir" >/dev/null || continue
        [[ "$covered_cache_dirs" == *" $dir "* ]] && continue
        name="${dir//_/-}"
        echo "$name"
    done
}

# Print "root<TAB>relative path" lines for a dataset.
dataset_spec() {
    local name="$1" dir root
    local inputs="output/research_only/reversal_2012_2026"
    case "$name" in
        reversal-v1)
            printf '%s\t%s\n' "$data_root" research_cache/reversal_2012_2026
            printf '%s\t%s\n' "$project_dir" "$inputs/inputs/special_distributions.csv" ;;
        reversal-v2)
            printf '%s\t%s\n' "$data_root" research_cache/reversal_2012_2026_v2
            printf '%s\t%s\n' "$project_dir" "$inputs/inputs_v2/special_distributions.csv" ;;
        reversal-v2-1)
            printf '%s\t%s\n' "$data_root" research_cache/reversal_2012_2026_v2_1
            printf '%s\t%s\n' "$project_dir" "$inputs/inputs_v2_1/special_distributions.csv" ;;
        reversal-v2-fill)
            printf '%s\t%s\n' "$data_root" research_cache/reversal_2012_2026_v2_fill ;;
        reversal-v2-1-alpaca)
            printf '%s\t%s\n' "$data_root" research_cache/reversal_2012_2026_v2_1_alpaca ;;
        stocks-data)
            printf '%s\t%s\n' "$data_root" cleaned_stocks_data
            printf '%s\t%s\n' "$data_root" stocks_list_dir ;;
        research-outputs)
            # Research outputs that are not in git (untracked or ignored).
            git -C "$project_dir" ls-files -z --others -- output/research_only \
                | tr '\0' '\n' | grep -v -E '(^|/)(__pycache__|\.DS_Store)(/|$)|\.pyc$' \
                | while IFS= read -r path; do
                    # A worktree may link a file to the main checkout's copy.
                    if [[ -L "$project_dir/$path" ]]; then
                        if [[ -f "$data_root/$path" && ! -L "$data_root/$path" ]]; then
                            printf '%s\t%s\n' "$data_root" "$path"
                        else
                            echo "skipping symlink $path" >&2
                        fi
                    else
                        printf '%s\t%s\n' "$project_dir" "$path"
                    fi
                done ;;
        *)
            dir="${name//-/_}"
            root="$(cache_root "$dir")" || die "unknown dataset: $name"
            printf '%s\t%s\n' "$root" "research_cache/$dir" ;;
    esac
}

check_private_repo() {
    local visibility
    visibility="$(gh repo view "$repo" --json visibility --jq .visibility)" \
        || die "cannot read $repo (gh auth login?)"
    [[ "$visibility" == "PRIVATE" ]] || die "$repo is $visibility; backups go only to a private repo"
}

free_kib() { df -Pk "$1" | awk 'NR==2 {print $4}'; }

backup_one() {
    local name="$1"
    local tag="cache-$name-$backup_date"
    local base="$name-$backup_date"
    local work="$dist/$base"
    if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
        echo "== $tag already exists in $repo; skipped"
        return 0
    fi
    echo "== $tag"
    rm -rf "$work"
    mkdir -p "$work/out"
    local spec="$work/spec.tsv"
    dataset_spec "$name" > "$spec"
    "${helper[@]}" build --dataset "$name" --tag "$tag" \
        --tar-list "$work/tar.list" --manifest "$work/out/$base.manifest.json" < "$spec"
    local raw_kib
    raw_kib="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["total_bytes"]//1024)' \
        "$work/out/$base.manifest.json")"
    # Room for the archive, its downloaded copy and a test extraction.
    if (( $(free_kib "$dist") < raw_kib * 2 + 4 * 1024 * 1024 )); then
        die "not enough free disk in $dist for $name"
    fi

    echo "archiving (zstd -$zstd_level)..."
    local tar_flags=(--format=pax --no-recursion)
    if tar --version 2>/dev/null | grep -q bsdtar; then
        tar_flags+=(--no-xattrs --no-mac-metadata --no-acls --no-fflags)
    fi
    COPYFILE_DISABLE=1 tar "${tar_flags[@]}" -c -f - -T "$work/tar.list" \
        | zstd -q -"$zstd_level" -T0 \
        | split -d -a 3 -b "$part_size" - "$work/out/$base.tar.zst.part-"
    local parts=("$work"/out/"$base".tar.zst.part-*)
    "${helper[@]}" finalize --manifest "$work/out/$base.manifest.json" \
        --archive-name "$base.tar.zst" --sums "$work/out/$base.parts.sha256" "${parts[@]}"

    local summary
    summary="$(python3 - "$work/out/$base.manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
a = m["archive"]
print(f"数据集：{m['dataset']}")
print("来源：" + "、".join(s["path"] for s in m["sources"][:20]) + ("……" if len(m["sources"]) > 20 else ""))
print(f"文件数：{m['file_count']:,}")
print(f"未压缩：{m['total_bytes']:,} 字节（约 {m['total_bytes'] / 2**30:.2f} GiB）")
print(f"压缩后：{a['bytes']:,} 字节（约 {a['bytes'] / 2**30:.2f} GiB），{len(a['parts'])} 个分片")
print(f"整包 SHA-256：{a['sha256']}")
print(f"生成时间（UTC）：{m['created_utc']}")
PY
)"
    cat > "$work/notes.md" <<EOF
quant_stocks 研究数据备份（私有）。

$summary

内容：tar（pax 格式）+ zstd 压缩包，切成小于 1.9 GiB 的分片；\`$base.parts.sha256\` 是每个分片的 SHA-256；\`$base.manifest.json\` 列出包内每个文件的相对路径、大小和 SHA-256。路径相对于项目根目录。

恢复：\`scripts/restore_data.sh $tag [目标目录]\`（需要 gh 登录）。
EOF

    echo "uploading to $repo..."
    gh release create "$tag" --repo "$repo" --title "研究数据备份：$name（$backup_date）" \
        --notes-file "$work/notes.md" --latest=false \
        "$work/out/$base.manifest.json" "$work/out/$base.parts.sha256" >/dev/null
    # Parts go up in parallel: GitHub serves each connection slowly from
    # some networks. xargs exits non-zero if any part fails five times.
    printf '%s\0' "${parts[@]}" | xargs -0 -n 1 -P "$upload_jobs" sh -c '
        for attempt in 1 2 3 4 5; do
            gh release upload "$0" --repo "$1" --clobber "$2" && exit 0
            echo "retrying upload of $2" >&2
            sleep $((attempt * 30))
        done
        exit 1' "$tag" "$repo" || die "upload failed for $tag"

    echo "verifying the uploaded copy..."
    verify_release "$tag" "$work/verify" "$work/out/$base.manifest.json"
    if [[ "${KEEP_DIST:-0}" != 1 ]]; then
        rm -rf "$work"
    fi
    echo "== $tag done"
}

# Download a release, check part hashes, reassemble, test-extract and compare
# every file with the manifest, then compare the manifest with live files.
verify_release() {
    local tag="$1" dir="$2" local_manifest="$3"
    rm -rf "$dir"
    mkdir -p "$dir/dl" "$dir/x"
    "${helper[@]}" download --repo "$repo" --tag "$tag" --dir "$dir/dl"
    local manifest sums
    manifest="$(ls "$dir"/dl/*.manifest.json)"
    sums="$(ls "$dir"/dl/*.parts.sha256)"
    cmp -s "$manifest" "$local_manifest" || die "$tag: downloaded manifest differs from the local one"
    (cd "$dir/dl" && shasum -a 256 -c "$(basename "$sums")")
    local expected actual
    expected="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["archive"]["sha256"])' "$manifest")"
    actual="$(cat "$dir"/dl/*.tar.zst.part-* | shasum -a 256 | awk '{print $1}')"
    [[ "$actual" == "$expected" ]] || die "$tag: reassembled archive SHA-256 mismatch"
    echo "reassembled archive SHA-256 OK: $actual"
    cat "$dir"/dl/*.tar.zst.part-* | zstd -dc | tar -xf - -C "$dir/x"
    "${helper[@]}" verify-tree --manifest "$manifest" --dir "$dir/x"
    if ! "${helper[@]}" verify-live --manifest "$manifest"; then
        echo "note: live files changed after the snapshot (expected only for live caches)"
    fi
}

main() {
    [[ $# -eq 1 ]] || { usage >&2; exit 2; }
    case "$1" in
        -h|--help) usage; exit 0 ;;
        list)
            printf '%s\n' "${special_datasets[@]}"
            cache_datasets
            exit 0 ;;
    esac

    check_private_repo
    dist="${BACKUP_DIST:-$(mktemp -d "${TMPDIR:-/tmp}/quant-data-backup.XXXXXX")}"
    mkdir -p "$dist"
    case "$dist" in
        "$project_dir"/*|"$data_root"/*) die "BACKUP_DIST must be outside the git checkouts" ;;
    esac
    echo "repo: $repo   data root: $data_root   scratch: $dist"

    if [[ "$1" == all ]]; then
        for name in "${special_datasets[@]}" $(cache_datasets); do
            backup_one "$name"
        done
    else
        backup_one "$1"
    fi
    rmdir "$dist" 2>/dev/null || true
}

# Everything runs from main, so bash has read the whole file before it starts.
main "$@"
