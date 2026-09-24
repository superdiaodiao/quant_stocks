#!/usr/bin/env bash
# Create the pinned live copy that runs the v50r3 prospective observation.
#
# The r3 protocol hash-binds every file its runner and scheduler import, and
# the runner refuses to write from any branch but live/v50r3.  This script
# makes a separate git worktree on that branch, gives it its own data and
# virtualenv, and checks that r3 loads there.  Master can then keep changing:
# the live copy only ever receives the freeze commit and the scheduler's
# ledger commits.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
branch="live/v50r3"
target="$(dirname "$repo")/$(basename "$repo")_live"
start="HEAD"
data="auto"
make_venv=true
python_bin="${PYTHON:-python3}"

usage() {
    cat <<'EOF'
用法：
  scripts/setup_v50r3_live.sh [--path DIR] [--from COMMIT]
                              [--data auto|copy|download|skip] [--no-venv]

在 DIR（默认：本仓库旁边的 <仓库名>_live）建立 live/v50r3 分支的独立工作副本。

  --from COMMIT  新分支的起点（默认 HEAD）。远端已有 live/v50r3 时忽略此参数，
                 改为跟踪远端分支（换机器时就是这种用法）。
  --data MODE    auto：本仓库有数据目录就复制，否则下载数据 Release（默认）；
                 copy / download / skip：强制复制、下载或跳过。
  --no-venv      不创建 .venv，也不做最后的自检。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --path) target="${2:?--path 后必须提供目录}"; shift 2 ;;
        --from) start="${2:?--from 后必须提供提交}"; shift 2 ;;
        --data) data="${2:?--data 后必须提供模式}"; shift 2 ;;
        --no-venv) make_venv=false; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
    esac
done
case "$data" in
    auto|copy|download|skip) ;;
    *) echo "--data 只能是 auto、copy、download 或 skip" >&2; exit 2 ;;
esac

if [[ -e "$target" ]]; then
    echo "目标已存在：$target" >&2
    exit 1
fi
if git -C "$repo" show-ref --verify --quiet "refs/heads/$branch"; then
    echo "本仓库已有本地分支 $branch。用 git worktree list 找到它的工作副本；" >&2
    echo "确实要重建时，先 git worktree remove 那个副本并 git branch -D $branch。" >&2
    exit 1
fi

if ! git -C "$repo" fetch --quiet origin 2>/dev/null; then
    echo "提醒：无法连接 origin，只按本地记录判断远端是否已有 $branch。" >&2
fi
tracking=false
if git -C "$repo" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
    tracking=true
    echo "远端已有 $branch，建立跟踪它的工作副本。"
    git -C "$repo" worktree add --track -b "$branch" "$target" "origin/$branch"
else
    if ! git -C "$repo" rev-parse --verify --quiet "$start^{commit}" >/dev/null; then
        echo "找不到提交：$start" >&2
        exit 1
    fi
    if [[ -n "$(git -C "$repo" status --porcelain --untracked-files=no)" ]]; then
        echo "提醒：本仓库有未提交的改动，独立副本从 $start 建立，不包含它们。" >&2
    fi
    git -C "$repo" worktree add -b "$branch" "$target" "$start"
fi

if [[ "$data" == auto ]]; then
    if [[ -d "$repo/cleaned_stocks_data" ]]; then data=copy; else data=download; fi
fi
case "$data" in
    copy)
        echo "复制数据目录..."
        for dir in cleaned_stocks_data stocks_list_dir his_data; do
            if [[ -d "$repo/$dir" ]]; then
                mkdir -p "$target/$dir"
                cp -a "$repo/$dir/." "$target/$dir/"
            fi
        done
        ;;
    download)
        (cd "$target" && scripts/download_data_release.sh --force)
        ;;
    skip) ;;
esac
# Data directories are git-ignored except a few files the protocol binds
# (such as the reviewed market moves); put every tracked file back to the
# branch's version after copying or unpacking data over them.
git -C "$target" checkout -- .

if [[ "$make_venv" == true ]]; then
    echo "创建 .venv 并安装依赖..."
    "$python_bin" -m venv "$target/.venv"
    "$target/.venv/bin/python" -m pip install --quiet --upgrade pip
    "$target/.venv/bin/python" -m pip install --quiet -r "$target/requirements.txt"
    if [[ "$data" != skip ]]; then
        # The data release has no SEC Company Facts cache; the SIGNAL refresh
        # re-downloads every payload, so an empty fingerprinted cache suffices.
        (cd "$target" && PYTHONPATH=. .venv/bin/python - <<'PY'
from pathlib import Path

from src.io.fundamentals_update import (
    SEC_COMPANYFACTS_CACHE_DIR,
    write_companyfacts_cache_manifest,
)

cache = Path(SEC_COMPANYFACTS_CACHE_DIR)
if not (cache / "manifest.json").is_file():
    cache.mkdir(parents=True, exist_ok=True)
    write_companyfacts_cache_manifest(cache)
    print(f"已建立空的 SEC Company Facts 缓存：{cache}")
PY
        )
    fi
    set +e
    (cd "$target" && PYTHONPATH=. .venv/bin/python \
        scripts/research_v50r3_corrected_v47.py status >/dev/null)
    code=$?
    set -e
    case "$code" in
        0) echo "自检通过：r3 协议已冻结，代码闭包与账本校验一致。" ;;
        3) echo "自检通过：代码就绪，r3 尚未冻结。" ;;
        *) echo "自检失败：r3 status 退出码 $code。" >&2; exit 1 ;;
    esac
fi

echo
echo "独立副本：$target（分支 $branch）"
if [[ "$tracking" == true ]]; then
    echo "它跟踪远端的 $branch。按 README 把定时任务指向这个目录即可。"
else
    cat <<EOF
演练和数据源探测都通过后，在副本里冻结并推送：
  cd "$target"
  PYTHONPATH=. .venv/bin/python scripts/research_v50r3_corrected_v47.py freeze-protocol
  PYTHONPATH=. .venv/bin/python scripts/research_v50r3_corrected_v47.py write-v50r2-supersession
  git add output/research_only/v50/corrected_v47_20260924_r3 \\
    output/research_only/v50/corrected_v47_20260905_r2/superseded_by_v50r3.json
  git commit -m "research: freeze v50r3 prospective protocol and supersede r2"
  git push -u origin $branch
然后按 README 配置定时任务，并在 GitHub 上保护 $branch 分支。
EOF
fi
