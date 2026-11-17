#!/usr/bin/env bash
# Run sue-lt-v1 from this machine when GitHub's runners cannot reach Nasdaq.
#
# From 2026-11-10T22:20Z to 2026-11-12 api.nasdaq.com answered HTTP 403 to
# GitHub's runners for about 35 hours.  A SIGNAL window lasts 11.5 hours and
# a missed month is never backfilled (docs/sue_lt_v1_protocol.md §3), so a
# block like that inside a window costs the month, and the scheduler, the
# window waker and a manual dispatch all run on those same runners.
#
# This script runs the same frozen code from a pinned local copy of
# live/sue-lt-v1, the way the "sue-lt-v1 scheduler" workflow does on a fresh
# runner: sync the branch, ask the frozen code what is due, check that Nasdaq
# has published the session, restore the data release into a clean checkout,
# then `scripts/sue_lt_v1.py run --push --workers 1`.
#
# It is outside the frozen closure (protocol §5): it changes where a run
# starts, never what the run selects, trades or values.  The frozen code
# itself refuses to write off the live branch, with changed code or data
# release, or outside the SIGNAL window.  If GitHub's run and this one both
# finish, the first push wins and the other fails without recording anything.
#
# Exit codes: 0 done, or nothing due; 10 not ready yet (not published, or
# refused here too): retry later; 11 GitHub is running the SIGNAL itself;
# 1 anything else.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
branch="live/sue-lt-v1"
live="${SUE_LT_V1_LIVE:-$(dirname "$repo")/$(basename "$repo")_sue_lt_v1_live}"
workflow="sue_lt_v1_scheduler.yml"
signal_step="Run the due SIGNAL or MARK"
ledger="output/research_only/sue_lt_v1/ledger.jsonl"
NOT_YET=10
GITHUB_BUSY=11

usage() {
    cat <<'EOF'
用法：
  scripts/sue_lt_v1_local_fallback.sh setup
  scripts/sue_lt_v1_local_fallback.sh status
  scripts/sue_lt_v1_local_fallback.sh run [--mark] [--force] [--report]
  scripts/sue_lt_v1_local_fallback.sh rehearse --as-of YYYY-MM-DD

GitHub 的机器被 Nasdaq 拒绝访问时，从本机运行 sue-lt-v1 的冻结代码。

  setup     建立本机副本（默认在本仓库旁边的 <仓库名>_sue_lt_v1_live，可用环境变量
            SUE_LT_V1_LIVE 改）和它自己的 Python 环境，并核对版本与冻结记录一致。
  status    只看不做：冻结代码认为现在该做什么、本机能否从 Nasdaq 取到数据、
            GitHub 那边是否正在跑。
  run       到期的是选股（SIGNAL）就在本机跑并推送账本。
            --mark    到期的是每日估值（MARK）时也在本机跑。默认不跑：估值晚几天
                      不丢收益，等 GitHub 恢复即可。
            --force   GitHub 正在跑选股时也照跑（先推送的一方生效）。
            --report  跑完后在观察 issue 上发报告（以你的 GitHub 账号发）。
  rehearse  对过去的一个月末完整演练一次选股，什么都不记录。

退出码：0 完成或没有到期的事；10 数据还没发布或本机也被拒绝，稍后重试；
        11 GitHub 正在跑；1 其他错误。
EOF
}

say() { printf '[sue-lt-v1 本机备用] %s\n' "$*" >&2; }
die() { say "错误：$*"; exit 1; }

gh_repo() {
    if [[ -n "${GH_REPO:-}" ]]; then
        printf '%s\n' "$GH_REPO"
        return
    fi
    git -C "$repo" remote get-url origin \
        | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##'
}

physical() { (cd "$1" && pwd -P); }

require_checkout() {
    [[ -d "$live" ]] || die "没有本机副本：${live}。先运行：$0 setup"
    local top
    top="$(git -C "$live" rev-parse --show-toplevel 2>/dev/null)" || die "$live 不是 git 工作副本"
    [[ "$(physical "$top")" == "$(physical "$live")" ]] || die "$live 不是工作副本的根目录"
    [[ "$(physical "$live")" != "$(physical "$repo")" ]] || die "本机副本不能是主仓库本身"
    [[ "$(git -C "$live" rev-parse --abbrev-ref HEAD)" == "$branch" ]] || die "$live 不在 $branch 分支上"
}

require_live() {
    require_checkout
    [[ -x "$live/.venv/bin/python" ]] || die "$live/.venv 不存在。先运行：$0 setup"
}

in_live() { (cd "$live" && PYTHONPATH=. "$@"); }

json_field() {
    "$live/.venv/bin/python" - "$1" "$2" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2])
print("" if value is None else value)
PY
}

# (event type, date) of every signal and valuation in a commit's ledger.
ledger_keys() {
    git -C "$live" show "$1:$ledger" | "$live/.venv/bin/python" -c '
import json, sys
for line in sys.stdin:
    if not line.strip():
        continue
    event = json.loads(line)
    payload = event.get("payload", {})
    print(event["event_type"], payload.get("signal_date") or payload.get("as_of") or "")
' | sort
}

rebase_in_progress() {
    (cd "$live" && { [[ -d "$(git rev-parse --git-path rebase-merge)" ]] \
        || [[ -d "$(git rev-parse --git-path rebase-apply)" ]]; })
}

# Bring the local copy to origin's ledger.  Like a fresh runner, only what
# was pushed survives a run that died.
sync_live() {
    local may_push="$1" ahead behind
    git -C "$live" fetch -q origin "$branch" || die "拉取 origin/$branch 失败（GitHub 连不上？）"
    git -C "$live" fetch -q origin master \
        || say "警告：拉取 origin/master 失败，辅助脚本用本机已有的版本"
    if rebase_in_progress; then
        git -C "$live" rebase --abort  # a push that lost to another run
    fi
    git -C "$live" checkout -q -- .
    ahead="$(git -C "$live" rev-list --count "origin/$branch..HEAD")"
    behind="$(git -C "$live" rev-list --count "HEAD..origin/$branch")"
    if (( ahead > 0 && behind > 0 )); then
        # Another run recorded the same events first: its record stands.
        if [[ -z "$(comm -23 <(ledger_keys HEAD) <(ledger_keys "origin/$branch"))" ]]; then
            say "本机有 $ahead 个没推送的提交，但远端账本已经记了同样的事件：以远端为准"
            git -C "$live" reset -q --hard "origin/$branch"
            return
        fi
        die "本机副本与 origin/$branch 分叉（本机多 $ahead 个提交，远端多 $behind 个），账本内容不同。需要人工处理，不要强推。"
    fi
    if (( ahead > 0 )); then
        if [[ "$may_push" == true ]]; then
            say "本机有 $ahead 个还没推送的账本提交，先推送"
            git -C "$live" push -q origin "HEAD:$branch" || die "推送失败"
        else
            say "注意：本机有 $ahead 个还没推送的账本提交（run 会先推送）"
        fi
    fi
    if (( behind > 0 )); then
        git -C "$live" merge -q --ff-only "origin/$branch"
    fi
}

# The installed versions against the freeze record, as tightly as GitHub's
# runners hold them: requirements.txt pins pandas and numpy to the patch, and
# lets exchange_calendars and Python move within their minor series (on
# 2026-11-17 the runners had exchange_calendars 4.13.3 and Python 3.12.16
# against 4.13.2 and 3.12.15 at the freeze).  Every signal file records the
# versions it ran with.
verify_runtime() {
    in_live .venv/bin/python - <<'PY' || die "本机 Python 环境与冻结记录不一致。删掉本机副本里的 .venv 后重新运行 setup。"
import json
import sys

from scripts import sue_lt_v1

frozen = json.loads(sue_lt_v1.PROTOCOL.read_text(encoding="utf-8"))["runtime_environment_at_freeze"]
now = sue_lt_v1.runtime_environment()
series = lambda version: version.rsplit(".", 1)[0]
wrong = {key: {"here": now[key], "frozen": frozen[key]}
         for key in ("pandas", "numpy") if now[key] != frozen[key]}
wrong.update({key: {"here": now[key], "frozen": frozen[key]}
              for key in ("exchange_calendars", "python") if series(now[key]) != series(frozen[key])})
patch = {key: {"here": now[key], "frozen": frozen[key]}
         for key in ("exchange_calendars", "python") if key not in wrong and now[key] != frozen[key]}
print(json.dumps({"here": now, "mismatch": wrong, "patch_differs": patch}, sort_keys=True), file=sys.stderr)
sys.exit(1 if wrong else 0)
PY
}

# Sets action, as_of, gate; writes $out/decision.json.
decide() {
    local code=0
    in_live .venv/bin/python scripts/sue_lt_v1.py check > "$out/decision.json" 2> "$out/check.log" || code=$?
    if (( code != 0 && code != 2 )); then
        cat "$out/check.log" >&2
        die "冻结代码的自检没有通过（退出码 ${code}）"
    fi
    action="$(json_field "$out/decision.json" action)"
    as_of="$(json_field "$out/decision.json" as_of)"
    gate="$(json_field "$out/decision.json" gate_session)"
    gate="${gate:-$as_of}"
    say "冻结代码的判断：$action${as_of:+（${as_of}）}"
    if (( code == 2 )); then
        say "注意：账本显示有月份已经错过（规则不回填）：$(json_field "$out/decision.json" missed)"
    fi
}

window_left() {
    in_live .venv/bin/python - "$1" <<'PY'
import sys
from datetime import datetime, timezone

import pandas as pd

from src.research import prospective_schedule as schedule

opens, closes = schedule.signal_window(pd.Timestamp(sys.argv[1]))
left = (closes - datetime.now(timezone.utc)).total_seconds() / 60
print(f"选股窗口 {opens:%m-%d %H:%M}Z 到 {closes:%m-%d %H:%M}Z，还剩 {left:.0f} 分钟")
PY
}

# 0 published; 10 not yet, or refused; anything else: the check did not run.
probe() {
    local helper="$out/sue_lt_v1_sources_ready.py" code=0
    git -C "$live" show "origin/master:scripts/sue_lt_v1_sources_ready.py" > "$helper" 2>/dev/null \
        || cp "$repo/scripts/sue_lt_v1_sources_ready.py" "$helper"
    in_live .venv/bin/python "$helper" --as-of "$1" > "$out/sources.json" || code=$?
    if (( code == 0 )); then
        say "本机能从 Nasdaq 取到 $1 的收盘数据"
    elif grep -q "HTTP Error 403" "$out/sources.json"; then
        say "Nasdaq 也拒绝了本机的请求（HTTP 403）"
    elif (( code == NOT_YET )); then
        say "Nasdaq 还没有发布 $1 的收盘数据（通常收盘后 4–5 小时）"
    else
        say "警告：发布检查本身没跑成（退出码 ${code}），见 $out/sources.json"
    fi
    return "$code"
}

# Whether a scheduler run on GitHub is inside its SIGNAL/MARK step, and
# whether the latest finished one was refused by Nasdaq.
github_state() {
    "$live/.venv/bin/python" - "$(gh_repo)" "$workflow" "$signal_step" > "$out/github.json" <<'PY' || true
import json
import subprocess
import sys

repo, workflow, step = sys.argv[1:4]


def gh(*args):
    try:
        return subprocess.run(["gh", *args, "--repo", repo], capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return subprocess.CompletedProcess(args, 1, "", f"{type(exc).__name__}: {exc}")


listing = gh("run", "list", "--workflow", workflow, "--limit", "12",
             "--json", "databaseId,status,conclusion,createdAt")
if listing.returncode != 0:
    print(json.dumps({"state": "unknown", "error": listing.stderr.strip()[-300:]}))
    sys.exit(0)
runs = json.loads(listing.stdout or "[]")
active = [run for run in runs if run["status"] != "completed"]
for run in active:
    view = gh("run", "view", str(run["databaseId"]), "--json", "jobs")
    if view.returncode != 0:
        continue
    for job in json.loads(view.stdout).get("jobs", []):
        for item in job.get("steps", []):
            if item.get("name") == step and item.get("status") == "in_progress":
                print(json.dumps({"state": "signal_running", "run": run["databaseId"],
                                  "started": item.get("startedAt")}))
                sys.exit(0)
result = {"state": "idle", "active_runs": [run["databaseId"] for run in active]}
finished = [run for run in runs if run["status"] == "completed"]
if finished:
    latest = finished[0]
    log = gh("run", "view", str(latest["databaseId"]), "--log")
    result["latest_finished"] = {
        "run": latest["databaseId"], "created": latest["createdAt"], "conclusion": latest["conclusion"],
        "nasdaq_refused_403": ("HTTP Error 403" in log.stdout) if log.returncode == 0 else None,
    }
print(json.dumps(result))
PY
    github="$(json_field "$out/github.json" state)"
    github="${github:-unknown}"
    case "$github" in
        signal_running) say "GitHub 正在跑选股或估值：运行 $(json_field "$out/github.json" run)" ;;
        idle) say "GitHub 上没有正在跑的选股。最近一次结束的调度：$(json_field "$out/github.json" latest_finished)" ;;
        *) say "警告：查不到 GitHub 上的运行状态：$(json_field "$out/github.json" error)" ;;
    esac
}

export_sec_contact() {
    if [[ -n "${SEC_USER_AGENT:-}" ]]; then
        return
    fi
    local file="$repo/.env.sec" value=""
    if [[ -f "$file" ]]; then
        value="$(sed -n 's/^[[:space:]]*SEC_USER_AGENT[[:space:]]*=[[:space:]]*//p' "$file" | head -n 1)"
        value="$(printf '%s' "$value" | sed -E "s/[[:space:]]+\$//; s/^[\"']//; s/[\"']\$//")"
    fi
    [[ -n "$value" ]] || die "没有 SEC 要求的联系方式：设置环境变量 SEC_USER_AGENT，或在 $file 里写 SEC_USER_AGENT=..."
    export SEC_USER_AGENT="$value"
}

# A clean checkout plus the pinned data release, as on a fresh runner.
restore() {
    say "清空本机副本里版本库之外的文件，下载并校验数据包（约 256 MB）"
    git -C "$live" clean -q -fdx -e .venv -e .fallback_logs
    (cd "$live" && PATH="$live/.venv/bin:$PATH" scripts/v50r3_restore_data.sh) >&2
}

keep_awake() {
    if command -v caffeinate >/dev/null 2>&1; then
        caffeinate -i "$@"
    else
        "$@"
    fi
}

new_out() {
    out="$live/.fallback_logs/$(date -u +%Y%m%dT%H%M%SZ)-$1"
    mkdir -p "$out"
}

# One process at a time in the live copy: a second run, or a status that
# synced while a run was writing the ledger, could undo its work.
lock="$live/.fallback_logs/lock"

lock_holder() {  # the pid that holds the lock, if it is still alive
    local pid
    pid="$(cat "$lock/pid" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        printf '%s\n' "$pid"
    fi
}

take_lock() {
    mkdir -p "$live/.fallback_logs"
    if ! mkdir "$lock" 2>/dev/null; then
        local holder
        holder="$(lock_holder)"
        [[ -z "$holder" ]] || die "本机副本正在被另一次运行使用（进程 ${holder}）。等它结束再来。"
        rm -rf "$lock"  # left by a run that was killed
        mkdir "$lock"
    fi
    printf '%s\n' "$$" > "$lock/pid"
    trap 'rm -rf "$lock"' EXIT
}

cmd_setup() {
    git -C "$repo" fetch -q origin "$branch" || die "拉取 origin/$branch 失败"
    if [[ ! -d "$live" ]]; then
        if git -C "$repo" show-ref --verify --quiet "refs/heads/$branch"; then
            git -C "$repo" worktree add -q "$live" "$branch"
        else
            git -C "$repo" worktree add -q --track -b "$branch" "$live" "origin/$branch"
        fi
        say "已建立本机副本：$live"
    fi
    require_checkout
    take_lock
    if [[ ! -x "$live/.venv/bin/python" ]]; then
        # An interpreter that stays put: not another project's virtualenv or a
        # tool's cache, which `uv python find` would otherwise prefer.
        local python_bin="${PYTHON:-}"
        if [[ -z "$python_bin" ]] && command -v uv >/dev/null 2>&1; then
            python_bin="$(uv python find --python-preference only-managed 3.12 2>/dev/null || true)"
        fi
        if [[ -z "$python_bin" ]]; then
            python_bin="$(command -v python3.12 || true)"
        fi
        [[ -n "$python_bin" ]] || die "找不到 Python 3.12。用 PYTHON=/path/to/python3.12 指定。"
        "$python_bin" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' \
            || die "$python_bin 不是 Python 3.12"
        "$python_bin" -m venv "$live/.venv"
    fi
    say "安装依赖（与 GitHub 上相同的 requirements.txt）"
    "$live/.venv/bin/python" -m pip install -q --upgrade pip
    "$live/.venv/bin/python" -m pip install -q -r "$live/requirements.txt"
    verify_runtime
    new_out setup
    sync_live false
    decide
    say "本机副本可用。演练：$0 rehearse --as-of <过去的月末交易日>"
}

cmd_status() {
    require_live
    local holder
    holder="$(lock_holder)"
    new_out status
    if [[ -n "$holder" ]]; then
        say "本机正有一次运行在进行（进程 ${holder}）：这次只看，不同步账本"
    else
        take_lock
        sync_live false
    fi
    decide
    github_state
    case "$action" in
        RUN_SIGNAL)
            say "$(window_left "$as_of")"
            probe "$gate" || true
            ;;
        RUN_MARK) probe "$gate" || true ;;
    esac
    cat "$out/decision.json"
}

cmd_run() {
    require_live
    take_lock
    new_out run
    sync_live true
    verify_runtime
    decide
    case "$action" in
        RUN_SIGNAL) say "$(window_left "$as_of")" ;;
        RUN_MARK)
            if [[ "$allow_mark" != true ]]; then
                say "到期的是每日估值，不是选股。估值晚几天不丢收益；要在本机跑，加 --mark。"
                exit 0
            fi
            ;;
        *)
            say "没有到期的事，什么都不做。"
            exit 0
            ;;
    esac
    local published=0
    probe "$gate" || published=$?
    if (( published == NOT_YET )); then
        exit "$NOT_YET"
    fi
    github_state
    if [[ "$github" == signal_running && "$force" != true ]]; then
        say "GitHub 没有被挡住，正在自己跑。本机不重复跑；确实要跑就加 --force。"
        exit "$GITHUB_BUSY"
    fi
    if [[ "$action" == RUN_SIGNAL ]]; then
        restore
        export_sec_contact
    fi
    say "开始运行冻结代码（选股约需 2–3 小时；日志在 ${out}）"
    local code=0 result
    (cd "$live" && PYTHONPATH=. keep_awake .venv/bin/python scripts/sue_lt_v1.py run --push --workers 1) \
        > "$out/run.json" 2> "$out/run.log" || code=$?
    cat "$out/run.json"
    tail -n 40 "$out/run.log" >&2
    if (( code == 0 || code == 2 )); then
        result=done
        say "完成：账本已推送到 origin/${branch}。"
    elif grep -q "names near the pool have no .* close .*; retry later" "$out/run.log"; then
        result=held
        say "有流动性池附近的股票当天没有收盘价，这次没有冻结。它们有收盘价后重试。"
    elif grep -q "retry later" "$out/run.log"; then
        result=not_ready
        say "需要的收盘价还没发布，这次什么都没记。稍后重试。"
    else
        result=failed
        git -C "$live" fetch -q origin "$branch" || true
        if rebase_in_progress; then
            git -C "$live" rebase --abort
        fi
        if [[ "$action" == RUN_SIGNAL ]] \
            && ledger_keys "origin/$branch" | grep -q "^SIGNAL_FROZEN $as_of\$"; then
            say "GitHub 先一步记录了 $as_of 的选股，以它为准；本机这次的结果不记录。"
            git -C "$live" reset -q --hard "origin/$branch"
            exit 0
        fi
        say "运行失败（退出码 ${code}），账本没有变化。完整日志：$out/run.log"
    fi
    if [[ "$report" == true ]]; then
        local helper="$out/sue_lt_v1_notify.py"
        if git -C "$live" show "origin/master:scripts/sue_lt_v1_notify.py" > "$helper" 2>/dev/null; then
            (cd "$live" && GH_REPO="$(gh_repo)" NOTIFY_ISSUE="${NOTIFY_ISSUE:-4}" .venv/bin/python "$helper" \
                --result "$result" --action "$action" --check "$out/decision.json" \
                --run "$out/run.json" --log "$out/run.log") || say "警告：报告没有发出去"
        else
            say "警告：取不到 master 上的通知脚本，报告没有发出去"
        fi
    fi
    case "$result" in
        done) exit 0 ;;
        held|not_ready) exit "$NOT_YET" ;;
        *) exit 1 ;;
    esac
}

cmd_rehearse() {
    [[ -n "$rehearse_as_of" ]] || die "rehearse 需要 --as-of <过去的月末交易日>"
    require_live
    take_lock
    new_out "rehearse-$rehearse_as_of"
    sync_live false
    verify_runtime
    restore
    export_sec_contact
    say "开始演练 $rehearse_as_of 的选股（约 2–3 小时，什么都不记录；日志在 ${out}）"
    local code=0
    (cd "$live" && PYTHONPATH=. keep_awake .venv/bin/python scripts/sue_lt_v1.py rehearse \
        --as-of "$rehearse_as_of" --workers 1) > "$out/rehearsal.json" 2> "$out/rehearsal.log" || code=$?
    if (( code != 0 )); then
        tail -n 40 "$out/rehearsal.log" >&2
        die "演练失败（退出码 ${code}）。完整日志：$out/rehearsal.log"
    fi
    say "演练完成：$out/rehearsal.json"
    "$live/.venv/bin/python" - "$out/rehearsal.json" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1], encoding="utf-8"))
print(json.dumps({key: result.get(key) for key in (
    "signal_date", "elapsed_seconds", "targets", "universe_size", "price_coverage",
    "sue_coverage_of_pool", "runtime_environment")}, indent=2, ensure_ascii=False))
PY
}

main() {
    command="${1:-}"
    [[ -n "$command" ]] || { usage >&2; exit 2; }
    shift
    allow_mark=false
    force=false
    report=false
    rehearse_as_of=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --mark) allow_mark=true; shift ;;
            --force) force=true; shift ;;
            --report) report=true; shift ;;
            --as-of) rehearse_as_of="${2:?--as-of 后必须提供日期}"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) echo "未知参数：${1}" >&2; usage >&2; exit 2 ;;
        esac
    done
    case "$command" in
        setup) cmd_setup ;;
        status) cmd_status ;;
        run) cmd_run ;;
        rehearse) cmd_rehearse ;;
        -h|--help) usage ;;
        *) echo "未知命令：${command}" >&2; usage >&2; exit 2 ;;
    esac
}

# Everything above is parsed before anything runs, and nothing is read after
# this line: a run that takes hours is not disturbed if this file is updated
# under it.
main "$@"; exit
