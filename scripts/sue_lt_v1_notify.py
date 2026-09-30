#!/usr/bin/env python3
"""Report sue-lt-v1 on its observation issue (outside the frozen closure).

The scheduler and the watchdog fetch this file from master and run it in a
checkout of the live branch, so the wording can improve without touching
the pinned code. It reports:

- a frozen signal, and each run of valuations (account against QQQ in the
  protocol's points, trades, weights, positions retired to cash, and moves
  of 40% or more in one valuation, which may be a split the provider has
  not back-adjusted yet);
- the end of the observation (early stop or end date);
- a failed run, a missed month, a SIGNAL held by names without a close,
  marks that fall behind, a final valuation that is overdue, and a check
  that fails, each once (a hidden key in the comment remembers what was sent).

A run whose session is not published yet stays quiet: a later run retries.
Before a SIGNAL, ``--still-held`` says whether the names that held the
last attempt for that session still have no close (exit 10), so the
scheduler does not download everything again for nothing.

    python sue_lt_v1_notify.py --result done --action RUN_MARK --check decision.json \\
        --run run.json --log run.log
    python sue_lt_v1_notify.py --watchdog --check decision.json
    python sue_lt_v1_notify.py --still-held --as-of 2026-10-30
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

ISSUE_TITLE = "sue-lt-v1 观察记录（自动）"
LEDGER = Path("output/research_only/sue_lt_v1/ledger.jsonl")
BOT_LOGIN = "github-actions[bot]"
MARKER = re.compile(r"<!-- sue-lt-v1-report (\{.*?\}) -->")
# The exception line itself, not the traceback's copy of the source line.
HELD = re.compile(r"^RuntimeError: names near the pool have no (\d{4}-\d{2}-\d{2}) close \(([^)]*)\); "
                  r"retry later$", re.MULTILINE)
STILL_HELD = 10
LARGE_MOVE = 0.40
START_CASH = 10_000.0
MAXIMUM_SESSIONS_BEHIND = 2


# ------------------------------------------------------------------ issue


def _issue_number() -> str:
    if os.environ.get("NOTIFY_ISSUE"):
        return str(int(os.environ["NOTIFY_ISSUE"]))
    listing = subprocess.run(
        ["gh", "issue", "list", "--state", "open", "--search", f'"{ISSUE_TITLE}" in:title',
         "--json", "number,title"], capture_output=True, text=True, check=True).stdout
    for item in json.loads(listing or "[]"):
        if item["title"] == ISSUE_TITLE:
            return str(item["number"])
    raise RuntimeError(f"no open issue titled {ISSUE_TITLE}")


def sent_keys(issue: str) -> set[str]:
    """Keys of the reports already on the issue; only the bot's comments count."""
    listing = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{{owner}}/{{repo}}/issues/{issue}/comments?per_page=100",
         "--jq", ".[] | {login: .user.login, body: .body}"],
        capture_output=True, text=True, check=True).stdout
    keys = set()
    for line in listing.splitlines():
        item = json.loads(line)
        if item.get("login") != BOT_LOGIN:
            continue
        for match in MARKER.finditer(item.get("body") or ""):
            keys.add(json.loads(match.group(1)).get("key"))
    return keys


def post(issue: str, body: str, key: str | None = None) -> None:
    if key is not None:
        body += f"\n\n<!-- sue-lt-v1-report {json.dumps({'key': key})} -->"
    subprocess.run(["gh", "issue", "comment", issue, "--body", body], check=True)


# ------------------------------------------------------------------ reading


def _json(path: str | None) -> dict | None:
    if not path or not Path(path).is_file():
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8") or "null")
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _events(ledger: Path = LEDGER) -> list[dict]:
    if not ledger.is_file():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


def points_behind(nav: float, qqq: float) -> float:
    """The protocol's measure: points of the 10,000-dollar start that the account trails QQQ."""
    return 100.0 * (qqq - nav) / START_CASH


def large_moves(events: list[dict], sessions: list[str]) -> list[str]:
    """Holdings whose value moved LARGE_MOVE or more between consecutive valuations."""
    marks = [e["payload"] for e in events if e["event_type"] == "VALUATION_APPENDED"]
    lines = []
    for before, after in zip(marks, marks[1:]):
        if after["as_of"] not in sessions:
            continue
        old, new = before["book"]["positions"], after["book"]["positions"]
        for ticker in sorted(set(old) & set(new)):
            if old[ticker] > 0 and abs(new[ticker] / old[ticker] - 1) >= LARGE_MOVE:
                lines.append(f"{ticker} {after['as_of']} {new[ticker] / old[ticker] - 1:+.0%}")
    return lines


# ------------------------------------------------------------------ messages


def _link() -> str:
    url = os.environ.get("RUN_URL")
    return f"\n\n[运行记录]({url})" if url else ""


def signal_message(inner: dict) -> str:
    names = "、".join(inner.get("targets", [])) or "（空）"
    text = (f"**{inner.get('signal_date')} 月末选股已冻结**（下一个交易日收盘模拟成交）\n\n"
            f"目标：{names}")
    coverage = inner.get("sue_coverage_of_pool")
    if coverage is not None:
        text += f"\n\n前 100 流动性股票中有 SUE 的比例：{coverage:.0%}"
    behind = inner.get("companyfacts_behind_edgar") or []
    if behind:
        text += ("\n\nSEC 数据接口还没收录已提交的季报、因此本月算不出 SUE 的："
                 + "、".join(behind) + "（按规则不补，只记录）")
    older = inner.get("sue_from_older_quarter") or []
    if older:
        text += "\n\n同样原因、本月仍按上一季 SUE 排名的：" + "、".join(older)
    failures = inner.get("sec_refresh_failures") or {}
    if failures:
        text += "\n\nSEC 请求被拒或超时（当晚已重试两次）、本月没有 SUE 的：" + "、".join(sorted(failures))
    unfetched = inner.get("price_download_failed") or []
    if unfetched:
        text += (f"\n\n价格下载失败或为空、本月不进股票池的 {len(unfetched)} 只："
                 + "、".join(unfetched[:20]) + ("……" if len(unfetched) > 20 else ""))
    return text + _link()


def mark_message(inner: dict, events: list[dict]) -> str:
    sessions = inner.get("sessions") or []
    nav, qqq = inner.get("nav"), inner.get("qqq_nav")
    if not sessions or nav is None or qqq is None:
        return f"估值完成：{', '.join(sessions) or '无新交易日'}{_link()}"
    behind = points_behind(nav, qqq)
    lines = [f"**估值 {sessions[-1]}**：账户 {nav:,.2f} 美元，QQQ 对照 {qqq:,.2f} 美元，"
             f"{'落后' if behind > 0 else '领先'} {abs(behind):.1f} 点（按 1 万美元起始资金计）"]
    if len(sessions) > 1:
        lines.append(f"本次补记 {len(sessions)} 个交易日：{sessions[0]} 至 {sessions[-1]}")
    for trade in inner.get("trades") or []:
        bought = "、".join(trade.get("bought") or []) or "无"
        sold = "、".join(trade.get("sold") or []) or "无"
        lines.append(f"{trade['execution_date']} 执行 {trade['signal_date']} 的名单：买入 {bought}；卖出 {sold}")
    for item in inner.get("retired") or []:
        lines.append(f"⚠️ {item['ticker']} 连续 {item['missing_sessions']} 个开市日没有收盘价，"
                     f"{item['as_of']} 按 {item['value']:,.2f} 美元转为现金。请人工核对（规则 §2）："
                     "被收购的不调整；改了代码的也不调整；停牌后崩盘、退市转场外或破产的，评估时按之后第一个"
                     "场外价格重新计价（查不到按 0），并在这里记下来源。")
    moves = large_moves(events, sessions)
    if moves:
        lines.append("⚠️ 单次估值涨跌 40% 以上，请人工核对（规则 §2）：有公开来源证明是拆股、而数据商当时"
                     "没有回调的，评估时按拆股比例改正那一天的收益：" + "；".join(moves))
    weights = inner.get("weights") or {}
    if weights:
        top = sorted(weights.items(), key=lambda item: -item[1])[:3]
        lines.append("最大持仓：" + "、".join(f"{t} {w:.0%}" for t, w in top))
    skipped = inner.get("skipped_calendar_sessions") or []
    if skipped:
        lines.append("日历上是交易日、但 QQQ 没有收盘价而跳过：" + "、".join(skipped))
    return "\n\n".join(lines) + _link()


def terminal_message(terminal: dict) -> str:
    verdict = "跑赢" if terminal.get("outcome") == "BEAT_QQQ" else "没有跑赢"
    head = (f"**sue-lt-v1 观察期满（最终估值 {terminal['as_of']}）**：人工调整之前{verdict} QQQ"
            f"（落后 {terminal['points_behind_qqq']:.1f} 点，负数为领先）。")
    return (f"{head}\n\n账户 {terminal['nav']:,.2f} 美元，QQQ 对照 {terminal['qqq_nav']:,.2f} 美元。"
            "此后调度器不再选股或估值。按规则 §2、§4 还要核对人工调整（停牌后崩盘、退市转场外、破产的退役持仓，"
            "以及数据商没有回调的拆股），并另算 QQEW 对照。"
            + _link())


def failure_message(action: str, as_of: str, log: str) -> str:
    tail = "\n".join(log.splitlines()[-15:]) or "（没有日志）"
    return (f"**sue-lt-v1 运行没有完成（{action} {as_of}），需要查看**\n\n```\n{tail}\n```\n\n"
            "同一个问题只报告一次；之后的运行会继续重试。" + _link())


def missed_message(day: str) -> str:
    return (f"**{day} 月末选股错过了**：窗口已关闭而没有冻结信号。按规则这个月不补，持仓保持到下一个月末。"
            + _link())


def check_failed_message(log: str) -> str:
    tail = "\n".join(log.splitlines()[-15:])
    return ("**sue-lt-v1 的 check 失败**：冻结的规则、代码或数据版本对不上，或者账本本身无效。"
            "在修好之前不会选股或估值。\n\n" + (f"```\n{tail}\n```" if tail else "") + _link())


def held_message(as_of: str, names: str) -> str:
    return (f"**{as_of} 月末选股在等这些股票的收盘价：{names}**。它们在流动性池附近，前一个交易日有收盘价、"
            "当天却没有（可能整天停牌），或者下载失败。在它们有收盘价之前不会重新下载；窗口关闭前仍没有，"
            "这个月按规则记为错过。" + _link())


def overdue_message() -> str:
    return ("**sue-lt-v1 的最终估值逾期**：最终估值日（2027-10-29）之后已有两个交易日收盘，账本里仍没有终止记录。"
            "请查看调度器的运行。" + _link())


def behind_message(latest: str | None, sessions: int) -> str:
    return (f"**sue-lt-v1 估值落后 {sessions} 个交易日**（最后一次估值：{latest or '尚无'}）。"
            "可能是数据一直没发布、某只持仓一直卡住，或调度器没有运行。" + _link())


# ------------------------------------------------------------------ what to send


def compose(result: str | None, action: str | None, decision: dict | None, run: dict | None,
            log: str, events: list[dict], today: str) -> list[tuple[str, str | None]]:
    """The comments to post, each with its once-only key (None: always post)."""
    out: list[tuple[str, str | None]] = []
    if decision is None:
        out.append((check_failed_message(log), f"check-failed:{today}"))
        return out
    inner = (run or {}).get("result") or {}
    kind = inner.get("action")
    as_of = (run or decision).get("as_of") or ""
    if result == "done" and kind == "SIGNAL_FROZEN":
        out.append((signal_message(inner), None))
    elif result == "done" and kind == "MARKED":
        out.append((mark_message(inner, events), None))
        if inner.get("terminal"):
            out.append((terminal_message(inner["terminal"]), "terminal"))
    elif result == "failed":
        signature = (log.strip().splitlines() or [""])[-1][:120]
        out.append((failure_message(action or "?", as_of, log), f"failed:{action}:{as_of}:{signature}"))
    elif result == "held":
        matches = list(HELD.finditer(log))
        match = matches[-1] if matches else None
        if match:
            out.append((held_message(match.group(1), match.group(2)),
                        f"held:{match.group(1)}:{match.group(2).replace(' ', '')}"))
    if decision.get("terminal") and not any(key == "terminal" for _, key in out):
        out.append((terminal_message(decision["terminal"]), "terminal"))
    for day in decision.get("missed") or []:
        out.append((missed_message(day), f"missed:{day}"))
    return out


def still_held(as_of: str, keys: set[str], has_row) -> list[str]:
    """Names from the held reports for ``as_of`` that still have no close.

    Only a name the provider answered for without that close counts; a
    refused or failed request lets the SIGNAL run.
    """
    held = [key for key in keys if key and key.startswith(f"held:{as_of}:")]
    names = sorted({name for key in held for name in key.split(":", 2)[2].split(",") if name})
    answers = {name: has_row(name) for name in names}
    if any(value is not True and value is not False for value in answers.values()):
        return []
    return [name for name, value in answers.items() if value is False]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result")
    parser.add_argument("--action")
    parser.add_argument("--check")
    parser.add_argument("--run")
    parser.add_argument("--log")
    parser.add_argument("--watchdog", action="store_true")
    parser.add_argument("--still-held", action="store_true")
    parser.add_argument("--as-of")
    args = parser.parse_args()
    if args.still_held:
        # Never hold a SIGNAL back because this check failed.
        try:
            import pandas as pd
            import sue_lt_v1_sources_ready as sources

            session = pd.Timestamp(args.as_of)
            missing = still_held(args.as_of, sent_keys(_issue_number()),
                                 lambda name: sources.has_row(name, session, "stocks"))
        except Exception as exc:
            print(f"could not check held names: {type(exc).__name__}: {exc}")
            return
        print(json.dumps({"still_without_close": missing}))
        if missing:
            raise SystemExit(STILL_HELD)
        return
    log = Path(args.log).read_text(encoding="utf-8", errors="replace") if args.log and Path(args.log).is_file() else ""
    decision = _json(args.check)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if args.watchdog:
        comments = compose(None, None, decision, None, log, [], today)
        behind = (decision or {}).get("sessions_behind")
        if behind is not None and behind > MAXIMUM_SESSIONS_BEHIND:
            latest = decision.get("latest_valuation")
            comments.append((behind_message(latest, behind), f"behind:{latest}"))
        if (decision or {}).get("terminal_overdue"):
            comments.append((overdue_message(), "terminal-overdue"))
    else:
        comments = compose(args.result, args.action, decision, _json(args.run), log, _events(), today)
    if not comments:
        print("nothing to report")
        return
    issue = _issue_number()
    keys = sent_keys(issue) if any(key for _, key in comments) else set()
    for body, key in comments:
        if key is not None and key in keys:
            print(f"already reported: {key}")
            continue
        post(issue, body, key)
        print(f"comment sent{'' if key is None else ': ' + key}")


if __name__ == "__main__":
    main()
