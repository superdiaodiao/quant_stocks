"""Report the result of a v50r3 SIGNAL or MARK (a GitHub helper, stdlib only).

The scheduler workflow runs this after each run on live/v50r3, fetched from
master so that the live branch stays pinned.  When the run froze a signal or
appended a mark it reads the run's decision (run.json) and the live
checkout's ledger and signal file, and sends a short summary two ways, each
only when configured:

- as a comment on the issue NOTIFY_ISSUE, with the job token in GH_TOKEN;
  GitHub notifies the issue's subscribers (the comment is public, like the
  ledger itself);
- as an email over SMTP with the repository secrets MAIL_USERNAME and
  MAIL_PASSWORD (a Gmail app password; for another provider also set the
  repository variables MAIL_SERVER and MAIL_PORT).  MAIL_TO defaults to
  MAIL_USERNAME.

The message is not printed to the public run log.  It imports nothing from
the repository and is not part of the r3 code closure.

    python scripts/v50r3_notify.py --decision run.json --root .
    python scripts/v50r3_notify.py --test

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import smtplib
import ssl
import sys
from email.message import EmailMessage
from urllib.request import Request, urlopen

OUTPUT_DIR = Path("output/research_only/v50/corrected_v47_20260924_r3")
LEDGER_PATH = OUTPUT_DIR / "prospective_ledger.jsonl"
SIGNALS_DIR = OUTPUT_DIR / "signals"
CASH = "__CASH__"
FROZEN_STATUSES = {"FROZEN_PROSPECTIVE_SIGNAL", "RECOVERED_AND_FROZEN_PROSPECTIVE_SIGNAL"}
MARK_STATUSES = {"APPENDED_PROSPECTIVE_MARK"}
GITHUB_API = "https://api.github.com"
DEFAULT_SERVER = "smtp.gmail.com"
DEFAULT_PORT = 465
FOOTER = "只是研究记录，不会下单。"


def _events(root: Path) -> list[dict]:
    text = (root / LEDGER_PATH).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _picks(targets: list[dict]) -> str:
    held = [
        row for row in targets
        if row.get("ticker") != CASH and float(row.get("target_weight") or 0) > 0
    ]
    if not held:
        return "全部现金"
    return "、".join(
        f"{row['ticker']} {float(row['target_weight']):.0%}" for row in held
    )


def _signal_message(decision: dict, root: Path) -> tuple[str, list[str]]:
    as_of = decision["as_of"]
    signal = json.loads(
        (root / SIGNALS_DIR / f"signal_{as_of}.json").read_text(encoding="utf-8")
    )
    picks = _picks(signal.get("targets") or [])
    role = (
        f"补跑，补 {decision['catch_up_for']} 的月末信号"
        if decision.get("catch_up_for")
        else "月末信号"
    )
    regime = "开" if signal.get("market_regime_on") else "关，全部现金"
    return f"v50r3 {as_of} 信号：{picks}", [
        f"信号日期：{as_of}（{role}）",
        f"大盘状态：{regime}",
        f"目标持仓：{picks}",
        "执行：下一个交易日收盘按目标权重调仓。",
    ]


def _mark_message(decision: dict, root: Path) -> tuple[str, list[str]]:
    as_of = decision["as_of"]
    events = _events(root)
    payload = [
        event["payload"] for event in events
        if event["event_type"] == "VALUATION_APPENDED"
    ][-1]
    costs = payload["cost_metrics"]
    main = costs["50"]
    nav, nasdaq, qqq = main["strategy_nav"], main["nasdaq_nav"], main["qqq_total_return_nav"]
    signals = [
        event["payload"] for event in events
        if event["event_type"] == "SIGNAL_FROZEN"
        and event["payload"]["signal_date"] < as_of
    ]
    lines = [
        f"估值日期：{as_of}（自 {payload.get('first_execution_date')} 建仓起）",
        f"策略净值：{nav:.4f}（按 50 bps 交易成本；"
        + "，".join(
            f"{bps} bps：{costs[bps]['strategy_nav']:.4f}"
            for bps in ("30", "10") if bps in costs
        )
        + "）",
        f"纳斯达克综合指数：{nasdaq:.4f}；QQQ（含分红）：{qqq:.4f}",
        f"相对纳指：{main['excess_vs_nasdaq_percentage_points']:+.2f} 个百分点",
        f"最大回撤：策略 {main['strategy_maximum_drawdown']:.1%}，"
        f"纳指 {main['nasdaq_maximum_drawdown']:.1%}",
    ]
    if signals:
        lines.append(
            f"当前持仓（{signals[-1]['signal_date']} 的信号）："
            f"{_picks(signals[-1].get('targets') or [])}"
        )
    subject = f"v50r3 {as_of} 估值：净值 {nav:.4f}（纳指 {nasdaq:.4f}，QQQ {qqq:.4f}）"
    return subject, lines


def compose(decision: dict, root: Path, run_url: str | None = None) -> tuple[str, str] | None:
    """The subject and body for a frozen signal or an appended mark, else None."""
    status = decision.get("result_status")
    if decision.get("action") == "RUN_SIGNAL" and status in FROZEN_STATUSES:
        subject, lines = _signal_message(decision, root)
    elif decision.get("action") == "RUN_MARK" and status in MARK_STATUSES:
        subject, lines = _mark_message(decision, root)
    else:
        return None
    commit = (decision.get("git") or {}).get("commit")
    if commit:
        lines.append(f"账本提交：{commit[:12]}")
    if run_url:
        lines.append(f"运行记录：{run_url}")
    lines.extend(["", FOOTER])
    return subject, "\n".join(lines) + "\n"


def send(subject: str, body: str, env: dict | None = None) -> bool:
    """Send over SMTP with the MAIL_* settings; False when they are absent."""
    env = os.environ if env is None else env
    user, password = env.get("MAIL_USERNAME"), env.get("MAIL_PASSWORD")
    if not user or not password:
        return False
    recipients = env.get("MAIL_TO") or user
    server = env.get("MAIL_SERVER") or DEFAULT_SERVER
    port = int(env.get("MAIL_PORT") or DEFAULT_PORT)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = recipients
    message.set_content(body)
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(server, port, context=context, timeout=60) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(server, port, timeout=60) as smtp:
            smtp.starttls(context=context)
            smtp.login(user, password)
            smtp.send_message(message)
    return True


def comment(subject: str, body: str, env: dict | None = None) -> bool:
    """Comment on the NOTIFY_ISSUE issue; False when it is not configured."""
    env = os.environ if env is None else env
    token, repository = env.get("GH_TOKEN"), env.get("GITHUB_REPOSITORY")
    issue = env.get("NOTIFY_ISSUE")
    if not token or not repository or not issue:
        return False
    request = Request(
        f"{GITHUB_API}/repos/{repository}/issues/{int(issue)}/comments",
        data=json.dumps({"body": f"**{subject}**\n\n{body}"}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "v50r3-notify",
        },
    )
    with urlopen(request, timeout=60) as response:
        return 200 <= response.status < 300


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--decision", type=Path, help="the scheduler's run.json")
    parser.add_argument("--root", type=Path, default=Path("."), help="live checkout")
    parser.add_argument("--test", action="store_true", help="send a test notice")
    args = parser.parse_args(argv)
    run_url = os.environ.get("RUN_URL")
    if args.test:
        message = (
            "v50r3 通知测试",
            "收到这条通知，说明 v50r3 的结果通知已经设置好：以后每次冻结信号、"
            f"每次估值都会发一条。\n\n{FOOTER}\n",
        )
    else:
        if args.decision is None or not args.decision.is_file():
            print("no run decision: nothing to report")
            return 0
        decision = json.loads(args.decision.read_text(encoding="utf-8"))
        message = compose(decision, args.root, run_url)
        if message is None:
            print("no frozen signal or appended mark: nothing to report")
            return 0
    delivered, failed = [], []
    for name, deliver in (("issue comment", comment), ("email", send)):
        try:
            if deliver(*message):
                delivered.append(name)
        except (OSError, ValueError, smtplib.SMTPException) as exc:
            failed.append(name)
            print(f"::warning::The result {name} was not sent: {type(exc).__name__}")
    if delivered:
        print(" and ".join(delivered) + " sent")
    elif not failed:
        print("neither NOTIFY_ISSUE nor MAIL_USERNAME and MAIL_PASSWORD are set: nothing sent")
    if failed:
        return 1
    return 1 if args.test and not delivered else 0


if __name__ == "__main__":
    sys.exit(main())
