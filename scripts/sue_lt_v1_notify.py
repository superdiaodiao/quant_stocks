#!/usr/bin/env python3
"""Post a sue-lt-v1 run result on its observation issue (outside the frozen closure).

The scheduler fetches this file from master, so the wording can improve
without touching the pinned live branch.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

ISSUE_TITLE = "sue-lt-v1 观察记录（自动）"


def _issue_number() -> str:
    listing = subprocess.run(
        ["gh", "issue", "list", "--state", "open", "--search", f'"{ISSUE_TITLE}" in:title',
         "--json", "number,title"], capture_output=True, text=True, check=True).stdout
    for item in json.loads(listing or "[]"):
        if item["title"] == ISSUE_TITLE:
            return str(item["number"])
    raise RuntimeError(f"no open issue titled {ISSUE_TITLE}")


def _body(result: str, run: dict, log: str) -> str:
    link = os.environ.get("RUN_URL", "")
    inner = run.get("result") or {}
    action = inner.get("action")
    if result == "done" and action == "SIGNAL_FROZEN":
        names = "、".join(inner.get("targets", []))
        return f"**{inner.get('signal_date')} 月末选股已冻结**（下一个交易日收盘模拟成交）\n\n目标：{names}\n\n[运行记录]({link})"
    if result == "done" and action == "MARKED":
        nav, qqq = inner.get("nav"), inner.get("qqq_nav")
        sessions = inner.get("sessions") or []
        if nav is None or qqq is None:
            return f"估值完成：{', '.join(sessions)}\n\n[运行记录]({link})"
        diff = (nav / qqq - 1) * 100
        return (f"**估值 {sessions[-1] if sessions else ''}**：账户 {nav:,.2f} 美元，"
                f"QQQ 对照 {qqq:,.2f} 美元（相对 {diff:+.2f}%）\n\n[运行记录]({link})")
    if result == "not_ready":
        return ""  # a later run retries; stay quiet
    tail = "\n".join(log.splitlines()[-15:])
    return f"**运行没有完成，需要查看**\n\n```\n{tail}\n```\n\n[运行记录]({link})"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    try:
        run = json.loads(Path(args.run).read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        run = {}
    log = Path(args.log).read_text(encoding="utf-8", errors="replace") if Path(args.log).is_file() else ""
    body = _body(args.result, run, log)
    if body:
        subprocess.run(["gh", "issue", "comment", _issue_number(), "--body", body], check=True)
        print("comment sent")


if __name__ == "__main__":
    main()
