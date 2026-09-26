"""Report each v50r3 SIGNAL and MARK run (a GitHub helper).

The scheduler workflow runs this around each SIGNAL or MARK on live/v50r3,
fetched from master so that the live branch stays pinned.  It reads the run's
decision (run.json, or the check's decision.json when the run died before
printing one), its log (run.log) and the live checkout's ledger and signal
file, and reports:

- a frozen signal (the targets) or an appended mark (the NAV);
- a run held by stocks without a close (a halt), a missed month, or a
  failure, with what to do about it.  GitHub notifies nobody of a failed run
  that the window waker or a retry started: those runs are the bot's.  Each
  such report carries a hidden key and is not repeated within a week, except
  that a SIGNAL held by stocks without a close is reported again when its one
  retry is held the same way.

A SIGNAL held by stocks without a close fails the same way until those closes
are published, and each run takes about two and a half hours.  Before a
SIGNAL the scheduler asks --still-blocked: the latest such report for the
session names the stocks, and their closes are checked on Nasdaq with the
live checkout's scripts/v50r3_sources_ready.py.  The SIGNAL waits (exit 10)
while a close is still missing, for good once its retry was held the same
way or when a momentum-start close is missing (it is never published later).
A check that fails, or crashes, lets the SIGNAL run.

It sends each report two ways, each only when configured:

- as a comment on the issue NOTIFY_ISSUE, with the job token in GH_TOKEN;
  GitHub notifies the issue's subscribers (the comment is public, like the
  ledger itself);
- as an email over SMTP with the repository secrets MAIL_USERNAME and
  MAIL_PASSWORD (a Gmail app password; for another provider also set the
  repository variables MAIL_SERVER and MAIL_PORT).  MAIL_TO defaults to
  MAIL_USERNAME.

The message is not printed to the public run log.  Only --still-blocked
imports from the repository (the live checkout, on PYTHONPATH); this is not
part of the r3 code closure.

    python scripts/v50r3_notify.py --result done --decision run.json \\
        --check decision.json --log run.log --root .
    python scripts/v50r3_notify.py --blocked-by run.log
    PYTHONPATH=. python scripts/v50r3_notify.py --still-blocked --as-of 2026-09-30
    python scripts/v50r3_notify.py --test

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
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
RESULTS = ("done", "not_ready", "blocked", "missed", "failed")

# Reports of held, missed and failed runs carry a hidden key.
BOT_LOGIN = "github-actions[bot]"
MARKER = re.compile(r"<!-- v50r3-report (\{.*\}) -->")
REPORT_DAYS = 7
# A held SIGNAL is reported, retried once its closes are published, and
# reported again if the retry is held the same way; then it waits for good.
HELD_SIGNAL_REPORTS = 2
# More stocks than this without a close: the session is not published yet.
MOST_HELD_STOCKS = 10
# --still-blocked's answer while the SIGNAL waits; a crash (1) or a usage
# error (2) never holds it back.
HELD_EXIT = 10

# The scheduler workflow's own reading of run.log.
NOT_READY = re.compile(
    r"is not ready|Retry once the closes are published|official index market is not closed"
)
NEEDS_EVENT = re.compile(r"split_like_moves_explained|Unresolved corporate action")
# The r3 runner's refusals (scripts/research_v50r3_corrected_v47.py).
SIGNAL_REFUSAL = re.compile(r"v50r3 SIGNAL bundle is not ready: \[([^\]]*)\]; (\{.*\})\s*$")
MARK_REFUSAL = re.compile(
    r"v50r3 MARK bundle is not ready: \[([^\]]*)\];.*held positions without a "
    r"\d{4}-\d{2}-\d{2} close \[(.*?)\]\. Retry"
)
HELD_WITHOUT_CLOSE = re.compile(
    r"held positions have no close after their last stored date inside a "
    r"holding window: (.*?)\. Retry"
)
AS_OF_GATE = "pool_candidates_priced_at_as_of"
START_GATE = "pool_candidates_have_momentum_start_close"
GATE_NAME = re.compile(r"'(\w+)'")
TICKER_FIELD = re.compile(r"'ticker': '([^']+)'")
LAST_CLOSE = re.compile(r"([A-Z0-9.^-]+) \(last close (\d{4}-\d{2}-\d{2})\)")
EVENT_REFERENCE = re.compile(r"([A-Z0-9.^-]+)@(\d{4}-\d{2}-\d{2})")
ERROR_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit)): (.*)$")
TOKEN_LIKE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")
RECORD_EVENT = "Actions → v50r3 record sourced event"
RETRY_ONCE = (
    "调度器等这些收盘价出现后再跑一次，不会一遍遍空转重试；重跑还缺就不再跑这个日期。"
)
RETRIED = "这是重跑后的结果：还是缺同样的收盘价，这个日期不再重跑。"
TEST_SUBJECT = "v50r3 通知测试"
TEST_BODY = (
    "收到这条通知，说明 v50r3 的结果通知已经设置好：以后每次冻结信号、"
    f"每次估值都会发一条。\n\n{FOOTER}\n"
)


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


def _role(decision: dict) -> str:
    if decision.get("catch_up_for"):
        return f"补跑，补 {decision['catch_up_for']} 的月末信号"
    return "月末信号"


def _signal_message(decision: dict, root: Path) -> tuple[str, list[str]]:
    as_of = decision["as_of"]
    signal = json.loads(
        (root / SIGNALS_DIR / f"signal_{as_of}.json").read_text(encoding="utf-8")
    )
    picks = _picks(signal.get("targets") or [])
    regime = "开" if signal.get("market_regime_on") else "关，全部现金"
    return f"v50r3 {as_of} 信号：{picks}", [
        f"信号日期：{as_of}（{_role(decision)}）",
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


def _finish(lines: list[str], run_url: str | None) -> str:
    if run_url:
        lines.append(f"运行记录：{run_url}")
    lines.extend(["", FOOTER])
    return "\n".join(lines) + "\n"


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
    return subject, _finish(lines, run_url)


def blocked_by(log: str) -> dict | None:
    """The stocks without a close that held a not-ready run, when it names them.

    None when the run was not held that way: the session is not published yet
    (a benchmark, or more than MOST_HELD_STOCKS stocks), or a move needs a
    sourced event.
    """
    if not NOT_READY.search(log) or NEEDS_EVENT.search(log):
        return None
    for line in reversed(log.splitlines()):
        refusal = SIGNAL_REFUSAL.search(line)
        if refusal:
            gates = set(GATE_NAME.findall(refusal.group(1)))
            if not gates or gates - {AS_OF_GATE, START_GATE}:
                return None
            try:
                details = json.loads(refusal.group(2))
            except ValueError:
                return None
            as_of = sorted(details.get("candidates_without_as_of_close") or [])
            start = sorted(details.get("candidates_without_momentum_start_close") or [])
            if not (as_of or start) or len(as_of) > MOST_HELD_STOCKS:
                return None
            return {
                "purpose": "SIGNAL",
                "without_as_of_close": as_of,
                "without_start_close": start,
            }
        refusal = MARK_REFUSAL.search(line)
        if refusal:
            if set(GATE_NAME.findall(refusal.group(1))) != {"held_positions_priced_at_as_of"}:
                return None
            tickers = sorted(set(TICKER_FIELD.findall(refusal.group(2))))
            if not tickers:
                return None
            return {"purpose": "MARK", "without_as_of_close": tickers}
        refusal = HELD_WITHOUT_CLOSE.search(line)
        if refusal:
            closes = dict(LAST_CLOSE.findall(refusal.group(1)))
            if not closes:
                return None
            return {
                "purpose": "MARK",
                "without_as_of_close": sorted(closes),
                "last_closes": closes,
            }
    return None


def _redact(text: str, env: dict) -> str:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MAIL_PASSWORD"):
        secret = env.get(name)
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    return TOKEN_LIKE.sub("***", text)


def _error_line(log: str) -> str | None:
    matches = [line for line in log.splitlines() if ERROR_LINE.match(line.strip())]
    if not matches:
        return None
    line = matches[-1].strip()
    return line if len(line) <= 300 else line[:297] + "..."


def _beijing(moment: str | None) -> str | None:
    if not moment:
        return None
    try:
        stamp = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return f"{stamp.astimezone(timezone(timedelta(hours=8))):%m-%d %H:%M}"


def _window_line(decision: dict) -> str | None:
    window = decision.get("catch_up_window_utc") or decision.get("signal_window_utc") or {}
    closes = _beijing(window.get("closes"))
    if closes is None:
        return None
    return (
        f"窗口在北京时间 {closes} 关闭；到时还没冻结，就算错过这个月，"
        "之后的交易日会在各自的窗口自动补跑。"
    )


def _missed_problem(decision: dict) -> dict:
    missed = decision.get("missed_signal_dates") or []
    due = decision.get("catch_up_for") or decision.get("due_signal_date") or (
        missed[-1] if missed else None
    )
    lines = [
        f"{due} 的信号窗口已经关闭，没有冻结信号，这个月还欠着。",
        "之后的交易日会在各自的窗口自动补跑，直到下一个月末；补上后会另发一条信号通知。",
    ]
    upcoming = decision.get("next_catch_up_window_utc") or {}
    opens = _beijing(upcoming.get("opens"))
    if opens:
        lines.append(f"下一个补跑窗口北京时间 {opens} 打开。")
    return {
        "subject": f"v50r3 {due} 月末信号错过了",
        "lines": lines,
        "marker": {"key": f"missed:{due}", "kind": "missed"},
    }


def _held_problem(decision: dict, held: dict) -> dict:
    as_of = decision.get("as_of")
    missing = held["without_as_of_close"]
    if held["purpose"] == "SIGNAL":
        start = held["without_start_close"]
        stocks = sorted(set(missing) | set(start))
        lines = [f"信号日期：{as_of}（{_role(decision)}）"]
        if missing:
            lines.append(
                f"{as_of} 的数据已经发布，但候选股 {'、'.join(missing)} 没有 {as_of} "
                "的收盘价，多半是停牌。"
            )
        if start:
            lines.append(
                f"候选股 {'、'.join(start)} 缺动量起点那天的收盘价（多半当天停牌），"
                "以后也不会补上，这个日期的信号不会再通过。"
            )
        lines.append("按冻结的规则，候选股缺收盘价时信号不能冻结。")
        if not start:
            lines.append(RETRY_ONCE)
        window = _window_line(decision)
        if window:
            lines.append(window)
        lines.append("不需要操作。")
        return {
            "subject": f"v50r3 {as_of} 信号暂缓：{'、'.join(stocks)} 缺收盘价",
            "lines": lines,
            "marker": {
                "key": f"held:SIGNAL:{as_of}:{','.join(stocks)}",
                "kind": "held",
                "purpose": "SIGNAL",
                "as_of": as_of,
                "without_as_of_close": missing,
                "without_start_close": start,
            },
        }
    last = held.get("last_closes") or {}
    described = "、".join(
        f"{ticker}（最后收盘 {last[ticker]}）" if ticker in last else ticker
        for ticker in missing
    )
    return {
        "subject": f"v50r3 {as_of} 估值暂缓：持仓 {'、'.join(missing)} 缺收盘价",
        "lines": [
            f"估值日期：{as_of}",
            (
                f"持仓 {described} 没有 {as_of} 的收盘价，多半是停牌。停牌结束后估值会"
                "自动继续，不需要操作。"
            ),
            "只有它退市或被收购时才要补录终值收益：" + RECORD_EVENT
            + "，类型选 TERMINAL_RETURN，填公开来源链接和终值收益（归零填 -1）。",
        ],
        "marker": {"key": f"held:MARK:{','.join(missing)}", "kind": "held", "purpose": "MARK"},
    }


def _moves(log: str) -> list[dict]:
    for line in reversed(log.splitlines()):
        refusal = SIGNAL_REFUSAL.search(line)
        if refusal:
            try:
                details = json.loads(refusal.group(2))
            except ValueError:
                break
            return details.get("unexplained_split_like_moves") or []
        if "Unresolved corporate action" in line:
            return [
                {"ticker": ticker, "session": session}
                for ticker, session in EVENT_REFERENCE.findall(line)
            ]
    return []


def _failed_problem(decision: dict, action: str, log: str | None, env: dict) -> dict:
    as_of = decision.get("as_of") or "?"
    purpose = "SIGNAL" if action == "RUN_SIGNAL" else "MARK"
    word = "信号" if purpose == "SIGNAL" else "估值"
    # A SIGNAL date is reported once; a MARK's date moves on every session.
    scope = f"{purpose}:{as_of}" if purpose == "SIGNAL" else purpose
    window = _window_line(decision) if purpose == "SIGNAL" else None
    if log and NEEDS_EVENT.search(log):
        moves = _moves(log)
        stocks = sorted({move["ticker"] for move in moves})
        lines = []
        for move in moves:
            ratio = move.get("raw_price_ratio")
            lines.append(
                f"{move['ticker']} 在 {move['session']} 的收盘价"
                + (f"是前一个收盘价的 {float(ratio):.4g} 倍" if ratio is not None else "跳变")
                + "，像拆股或合股，但事件表里没有来源。"
            )
        if not moves:
            lines.append("有像拆股或合股的价格跳变，但事件表里没有来源（股票和日期见运行记录）。")
        lines.append(
            "补录后调度器下一次运行会重试：" + RECORD_EVENT
            + "，拆股选 SPLIT 并填价格因子（二拆一填 0.5，十合一填 10），"
            "真实的大涨大跌选 MARKET_MOVE，都要附公开来源链接。"
        )
        if window:
            lines.append(window)
        return {
            "subject": f"v50r3 {as_of} {word}失败："
            + ("、".join(stocks) if stocks else "有股票") + " 疑似拆股，需要补录",
            "lines": lines,
            "marker": {"key": f"event:{scope}:{','.join(stocks)}", "kind": "failed"},
        }
    push_error = (decision.get("git") or {}).get("error")
    if push_error:
        first = _redact(push_error.strip().splitlines()[0][:300], env)
        return {
            "subject": f"v50r3 {as_of} {word}没有推送",
            "lines": [
                f"{word}在 runner 上完成了，但推送 live/v50r3 失败：{first}",
                "账本里还没有这次结果，调度器下一次运行会重做。",
            ] + ([window] if window else []),
            "marker": {"key": f"push:{scope}", "kind": "failed"},
        }
    error = _error_line(_redact(log, env)) if log else None
    if error is None:
        detail = "运行没有完成，也没有留下错误信息（可能是恢复数据等更早的步骤失败、超时或被取消）。"
        signature = "no-error-line"
    else:
        detail = f"错误：{error}"
        match = ERROR_LINE.match(error)
        head = re.sub(r"\d", "#", match.group(2) if match else error)[:60]
        signature = f"{match.group(1) if match else 'error'}:{head}"
    return {
        "subject": f"v50r3 {as_of} {word}失败",
        "lines": [
            detail,
            "调度器下一次运行会重试；同样的错误一周内只通知一次。详情看运行记录。",
        ] + ([window] if window else []),
        "marker": {"key": f"failed:{scope}:{signature}", "kind": "failed"},
    }


def compose_problem(
    result: str,
    action: str,
    decision: dict,
    log: str | None,
    env: dict | None = None,
) -> dict | None:
    """The report of a held, missed or failed run (subject, lines, marker)."""
    env = os.environ if env is None else env
    if result in ("done", "not_ready"):
        return None
    if result == "missed":
        return _missed_problem(decision)
    if result == "blocked":
        held = blocked_by(log or "")
        if held is not None:
            return _held_problem(decision, held)
    return _failed_problem(decision, action, log, env)


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


def _issue_url(env: dict) -> str | None:
    repository, issue = env.get("GITHUB_REPOSITORY"), env.get("NOTIFY_ISSUE")
    if not env.get("GH_TOKEN") or not repository or not issue:
        return None
    return f"{GITHUB_API}/repos/{repository}/issues/{int(issue)}/comments"


def _headers(env: dict) -> dict:
    return {
        "Authorization": f"Bearer {env['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "v50r3-notify",
    }


def comment(subject: str, body: str, env: dict | None = None, marker: dict | None = None) -> bool:
    """Comment on the NOTIFY_ISSUE issue; False when it is not configured."""
    env = os.environ if env is None else env
    url = _issue_url(env)
    if url is None:
        return False
    text = f"**{subject}**\n\n{body}"
    if marker is not None:
        text += f"\n<!-- v50r3-report {json.dumps(marker, sort_keys=True)} -->\n"
    request = Request(
        url,
        data=json.dumps({"body": text}).encode("utf-8"),
        method="POST",
        headers=_headers(env),
    )
    with urlopen(request, timeout=60) as response:
        return 200 <= response.status < 300


def reports(env: dict | None = None, now: datetime | None = None) -> list[dict]:
    """The hidden keys of this week's reports on the issue, oldest first.

    Only the job token's own comments count: anyone can comment on the issue.
    """
    env = os.environ if env is None else env
    url = _issue_url(env)
    if url is None:
        return []
    since = (now or datetime.now(timezone.utc)) - timedelta(days=REPORT_DAYS)
    found: list[dict] = []
    for page in range(1, 11):
        request = Request(
            f"{url}?since={since:%Y-%m-%dT%H:%M:%SZ}&per_page=100&page={page}",
            headers=_headers(env),
        )
        with urlopen(request, timeout=60) as response:
            comments = json.loads(response.read().decode("utf-8"))
        for item in comments:
            if (item.get("user") or {}).get("login") != BOT_LOGIN:
                continue
            match = MARKER.search(item.get("body") or "")
            if match is None:
                continue
            try:
                marker = json.loads(match.group(1))
            except ValueError:
                continue
            if isinstance(marker, dict) and marker.get("key"):
                found.append({**marker, "created_at": item.get("created_at")})
        if len(comments) < 100:
            break
    return found


def _has_close(ticker: str, session: str) -> bool | str:
    """Nasdaq's answer for one stock's close, from the live checkout's probe."""
    import pandas as pd

    from scripts import v50r3_sources_ready as sources

    return sources._has_row(ticker, pd.Timestamp(session).normalize(), "stocks")


def still_blocked(as_of: str, env: dict | None = None, has_close=None) -> dict:
    """Whether the session's SIGNAL still waits for the closes it was held by."""
    has_close = has_close or _has_close
    held = [
        report for report in reports(env)
        if report.get("kind") == "held"
        and report.get("purpose") == "SIGNAL"
        and report.get("as_of") == as_of
    ]
    if not held:
        return {"blocked": False, "reason": "no held SIGNAL was reported for this session"}
    latest = held[-1]
    result = {"held_by": latest["key"]}
    if sum(report["key"] == latest["key"] for report in held) >= HELD_SIGNAL_REPORTS:
        return {**result, "blocked": True, "reason": "its retry was held the same way"}
    if latest.get("without_start_close"):
        return {
            **result, "blocked": True,
            "reason": "a momentum-start close is missing and is never published later",
        }
    checks = {ticker: has_close(ticker, as_of) for ticker in latest.get("without_as_of_close") or []}
    missing = sorted(ticker for ticker, found in checks.items() if found is False)
    return {
        **result,
        "blocked": bool(missing),
        "reason": "closes still missing" if missing else "the closes are published or unknown",
        "checks": {ticker: found for ticker, found in checks.items()},
    }


def _read_json(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _deliver(subject: str, body: str, marker: dict | None, env: dict) -> tuple[list, list]:
    delivered, failed = [], []
    for name, deliver in (
        ("issue comment", lambda: comment(subject, body, env, marker)),
        ("email", lambda: send(subject, body, env)),
    ):
        try:
            if deliver():
                delivered.append(name)
        except (OSError, ValueError, smtplib.SMTPException) as exc:
            failed.append(name)
            print(f"::warning::The result {name} was not sent: {type(exc).__name__}")
    return delivered, failed


def _report_problem(problem: dict, run_url: str | None, env: dict) -> tuple[list, list, bool]:
    """Deliver a held, missed or failed run's report unless it was sent this week."""
    marker = problem["marker"]
    try:
        earlier = [report for report in reports(env) if report["key"] == marker["key"]]
    except (OSError, ValueError) as exc:
        # Better twice than never.
        print(f"::warning::Earlier reports could not be read: {type(exc).__name__}")
        earlier = []
    allowed = HELD_SIGNAL_REPORTS if marker.get("purpose") == "SIGNAL" else 1
    if len(earlier) >= allowed:
        print(f"this {marker['kind']} run was already reported this week: nothing sent")
        return [], [], True
    lines = list(problem["lines"])
    if earlier:
        lines = [line for line in lines if line != RETRY_ONCE]
        lines.insert(1, RETRIED)
    delivered, failed = _deliver(problem["subject"], _finish(lines, run_url), marker, env)
    return delivered, failed, False


def _read_text(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--result", choices=RESULTS, help="the scheduler's reading of the run")
    parser.add_argument("--action", help="RUN_SIGNAL or RUN_MARK, from the check")
    parser.add_argument("--decision", type=Path, help="the scheduler's run.json")
    parser.add_argument("--check", type=Path, help="the check's decision.json")
    parser.add_argument("--log", type=Path, help="the run's log (run.log)")
    parser.add_argument("--root", type=Path, default=Path("."), help="live checkout")
    parser.add_argument("--blocked-by", type=Path, metavar="LOG",
                        help="print 'blocked' when named stocks without a close held the run")
    parser.add_argument("--still-blocked", action="store_true",
                        help=f"exit {HELD_EXIT} while the --as-of SIGNAL still waits for its closes")
    parser.add_argument("--as-of", help="the SIGNAL session for --still-blocked")
    parser.add_argument("--test", action="store_true", help="send a test notice")
    args = parser.parse_args(argv)
    env = os.environ
    run_url = env.get("RUN_URL")
    if args.blocked_by is not None:
        print("blocked" if blocked_by(_read_text(args.blocked_by) or "") else "not blocked")
        return 0
    if args.still_blocked:
        if not args.as_of:
            parser.error("--still-blocked needs --as-of")
        try:
            answer = still_blocked(args.as_of, env)
        except Exception as exc:  # a failed check never holds a SIGNAL back
            answer = {"blocked": False, "error": type(exc).__name__}
        print(json.dumps(answer, indent=2, sort_keys=True, default=str))
        return HELD_EXIT if answer["blocked"] else 0
    notices: list[tuple[str, str]] = []
    problem = None
    if args.test:
        notices.append((TEST_SUBJECT, TEST_BODY))
    else:
        decision = _read_json(args.decision)
        if args.result is None and not decision:
            print("no run decision: nothing to report")
            return 0
        result = args.result or "done"
        if result in ("done", "missed") and decision:
            success = compose(decision, args.root, run_url)
            if success is not None:
                notices.append(success)
        action = args.action or decision.get("action") or ""
        problem = compose_problem(
            result, action, decision or _read_json(args.check), _read_text(args.log), env
        )
        if not notices and problem is None:
            print("no frozen signal, appended mark, held or failed run: nothing to report")
            return 0
    delivered, failed, repeated = [], [], False
    for subject, body in notices:
        sent, refused = _deliver(subject, body, None, env)
        delivered += sent
        failed += refused
    if problem is not None:
        sent, refused, repeated = _report_problem(problem, run_url, env)
        delivered += sent
        failed += refused
    if delivered:
        print(" and ".join(dict.fromkeys(delivered)) + " sent")
    elif not failed and not repeated:
        print("neither NOTIFY_ISSUE nor MAIL_USERNAME and MAIL_PASSWORD are set: nothing sent")
    if failed:
        return 1
    return 1 if args.test and not delivered else 0


if __name__ == "__main__":
    sys.exit(main())
