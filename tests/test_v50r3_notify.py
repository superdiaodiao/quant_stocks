from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import v50r3_notify as notify


def _targets(*tickers: str) -> list[dict]:
    return [{"ticker": ticker, "target_weight": 0.2} for ticker in tickers]


def _signal(root: Path, date: str, targets: list[dict], regime: bool = True) -> None:
    path = root / notify.SIGNALS_DIR / f"signal_{date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "signal_date": date, "targets": targets, "market_regime_on": regime,
    }), encoding="utf-8")


def _metrics(nav: float) -> dict:
    return {
        "strategy_nav": nav, "nasdaq_nav": 1.004, "qqq_total_return_nav": 1.005,
        "excess_vs_nasdaq_percentage_points": (nav - 1.004) * 100,
        "strategy_maximum_drawdown": -0.013, "nasdaq_maximum_drawdown": 0.0,
    }


def _ledger(root: Path, *events: dict) -> None:
    path = root / notify.LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )


def test_a_frozen_signal_reports_its_targets(tmp_path: Path) -> None:
    _signal(tmp_path, "2026-09-30", _targets("PLTR", "OKTA", "WDAY", "CRWD", "MDB"))
    decision = {
        "action": "RUN_SIGNAL", "result_status": "FROZEN_PROSPECTIVE_SIGNAL",
        "as_of": "2026-09-30", "git": {"commit": "0123456789abcdef"},
    }

    subject, body = notify.compose(decision, tmp_path, "https://example.invalid/run/1")

    assert subject == "v50r3 2026-09-30 信号：PLTR 20%、OKTA 20%、WDAY 20%、CRWD 20%、MDB 20%"
    assert "信号日期：2026-09-30（月末信号）" in body
    assert "大盘状态：开" in body
    assert "账本提交：0123456789ab" in body
    assert "运行记录：https://example.invalid/run/1" in body
    assert body.rstrip().endswith(notify.FOOTER)


def test_a_cash_catch_up_signal_says_so(tmp_path: Path) -> None:
    _signal(
        tmp_path, "2026-10-01",
        [{"ticker": "__CASH__", "target_weight": 0.0}], regime=False,
    )
    subject, body = notify.compose({
        "action": "RUN_SIGNAL", "result_status": "RECOVERED_AND_FROZEN_PROSPECTIVE_SIGNAL",
        "as_of": "2026-10-01", "catch_up_for": "2026-09-30",
    }, tmp_path)
    assert subject == "v50r3 2026-10-01 信号：全部现金"
    assert "补跑，补 2026-09-30 的月末信号" in body
    assert "大盘状态：关，全部现金" in body


def test_a_mark_reports_the_nav_against_its_benchmarks(tmp_path: Path) -> None:
    _ledger(
        tmp_path,
        {"event_type": "PROTOCOL_FROZEN", "payload": {}},
        {"event_type": "SIGNAL_FROZEN", "payload": {
            "signal_date": "2026-09-30", "targets": _targets("PLTR", "OKTA"),
        }},
        {"event_type": "VALUATION_APPENDED", "payload": {
            "as_of": "2026-10-01", "first_execution_date": "2026-10-01",
            "cost_metrics": {
                "10": _metrics(0.998), "30": _metrics(0.997), "50": _metrics(0.995),
            },
        }},
    )

    subject, body = notify.compose(
        {"action": "RUN_MARK", "result_status": "APPENDED_PROSPECTIVE_MARK",
         "as_of": "2026-10-01"},
        tmp_path,
    )

    assert subject == "v50r3 2026-10-01 估值：净值 0.9950（纳指 1.0040，QQQ 1.0050）"
    assert "策略净值：0.9950（按 50 bps 交易成本；30 bps：0.9970，10 bps：0.9980）" in body
    assert "相对纳指：-0.90 个百分点" in body
    assert "当前持仓（2026-09-30 的信号）：PLTR 20%、OKTA 20%" in body


@pytest.mark.parametrize("decision", [
    {"action": "WAIT_FOR_FIRST_SIGNAL_DATE"},
    {"action": "RUN_SIGNAL", "result_status": None, "as_of": "2026-09-30"},
    {"action": "RUN_SIGNAL", "result_status": "ALREADY_FROZEN_AND_VERIFIED",
     "as_of": "2026-09-30"},
    {"action": "RUN_MARK", "result_status": "ALREADY_STAGED", "as_of": "2026-10-01"},
])
def test_nothing_else_is_reported(tmp_path: Path, decision: dict) -> None:
    assert notify.compose(decision, tmp_path) is None


class _FakeSmtp:
    sent: list = []

    def __init__(self, server, port, context=None, timeout=None):
        self.server, self.port = server, port

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def login(self, user, password):
        self.user = user

    def send_message(self, message):
        _FakeSmtp.sent.append((self.server, self.port, self.user, message))


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> list:
    _FakeSmtp.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _FakeSmtp)
    return _FakeSmtp.sent


def test_send_uses_the_secrets_and_defaults_to_gmail(smtp: list) -> None:
    assert notify.send("subject", "body", {}) is False
    assert notify.send("subject", "body", {
        "MAIL_USERNAME": "me@example.invalid", "MAIL_PASSWORD": "app-password",
    }) is True
    ((server, port, user, message),) = smtp
    assert (server, port, user) == ("smtp.gmail.com", 465, "me@example.invalid")
    assert message["To"] == "me@example.invalid" and message["Subject"] == "subject"


def test_the_cli_reports_a_run_and_tests_the_settings(
    tmp_path: Path, smtp: list, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    for name in ("GH_TOKEN", "NOTIFY_ISSUE", "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_TO"):
        monkeypatch.delenv(name, raising=False)
    _signal(tmp_path, "2026-09-30", _targets("PLTR"))
    decision = tmp_path / "run.json"
    decision.write_text(json.dumps({
        "action": "RUN_SIGNAL", "result_status": "FROZEN_PROSPECTIVE_SIGNAL",
        "as_of": "2026-09-30",
    }), encoding="utf-8")
    argv = ["--decision", str(decision), "--root", str(tmp_path)]

    # Without the secrets a run is not an error; a test says what is missing.
    assert notify.main(argv) == 0
    assert notify.main(["--test"]) == 1
    assert not smtp

    monkeypatch.setenv("MAIL_USERNAME", "me@example.invalid")
    monkeypatch.setenv("MAIL_PASSWORD", "app-password")
    assert notify.main(argv) == 0
    assert notify.main(["--test"]) == 0
    assert [message["Subject"] for *_rest, message in smtp] == [
        "v50r3 2026-09-30 信号：PLTR 20%", "v50r3 通知测试",
    ]
    # The public run log never shows the message or the address.
    log = capsys.readouterr().out
    assert "PLTR" not in log and "example.invalid" not in log


class _Response:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_comment_posts_to_the_notify_issue_with_the_job_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []
    monkeypatch.setattr(
        notify, "urlopen", lambda request, timeout: requests.append(request) or _Response()
    )
    assert notify.comment("subject", "body", {"GH_TOKEN": "token"}) is False
    assert notify.comment("subject", "body", {
        "GH_TOKEN": "token", "GITHUB_REPOSITORY": "owner/repo", "NOTIFY_ISSUE": "2",
    }) is True
    (request,) = requests
    assert request.full_url == "https://api.github.com/repos/owner/repo/issues/2/comments"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer token"
    assert json.loads(request.data) == {"body": "**subject**\n\nbody"}


def test_the_cli_comments_on_the_issue_without_any_email_settings(
    tmp_path: Path, smtp: list, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    requests = []
    monkeypatch.setattr(
        notify, "urlopen", lambda request, timeout: requests.append(request) or _Response()
    )
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("NOTIFY_ISSUE", "2")
    monkeypatch.delenv("MAIL_USERNAME", raising=False)
    monkeypatch.delenv("MAIL_PASSWORD", raising=False)
    _signal(tmp_path, "2026-09-30", _targets("PLTR"))
    decision = tmp_path / "run.json"
    decision.write_text(json.dumps({
        "action": "RUN_SIGNAL", "result_status": "FROZEN_PROSPECTIVE_SIGNAL",
        "as_of": "2026-09-30",
    }), encoding="utf-8")

    assert notify.main(["--decision", str(decision), "--root", str(tmp_path)]) == 0
    assert notify.main(["--test"]) == 0

    assert not smtp and len(requests) == 2
    assert json.loads(requests[0].data)["body"].startswith(
        "**v50r3 2026-09-30 信号：PLTR 20%**"
    )
    log = capsys.readouterr().out
    assert "issue comment sent" in log and "PLTR" not in log


def test_a_refused_comment_is_reported_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    def refuse(_request, timeout):
        raise OSError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(notify, "urlopen", refuse)
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("NOTIFY_ISSUE", "2")
    assert notify.main(["--test"]) == 1
    assert "The result issue comment was not sent: OSError" in capsys.readouterr().out


# The r3 runner's refusals, built as scripts/research_v50r3_corrected_v47.py
# builds them (the test below checks that its formats stay these).
def _signal_refusal(failed: list[str], **details) -> str:
    details = {
        "candidates_without_as_of_close": [],
        "candidates_without_momentum_start_close": [],
        "market_regime_on": True,
        "price_events": [],
        "unexplained_split_like_moves": [],
        **details,
    }
    return (
        "Traceback (most recent call last):\n"
        '  File "scripts/research_v50r3_scheduled_run.py", line 295, in <module>\n'
        "RuntimeError: "
        + f"v50r3 SIGNAL bundle is not ready: {failed}; "
        + json.dumps(details, sort_keys=True)
        + "\n"
    )


def _mark_refusal(failed: list[str], unpriced: list[dict], missing=()) -> str:
    stamp_text = "2026-10-02"
    return (
        "RuntimeError: "
        f"v50r3 MARK bundle is not ready: {failed}; missing price files "
        f"{list(missing)}; held positions without a {stamp_text} close "
        f"{unpriced}. Retry once the closes are published.  A halt "
        "resumes by itself: wait for it.\n"
    )


def test_the_r3_refusals_keep_the_formats_read_here() -> None:
    source = (Path(__file__).parents[1] / "scripts/research_v50r3_corrected_v47.py").read_text(
        encoding="utf-8"
    )
    for piece in (
        (
            'f"v50r3 SIGNAL bundle is not ready: {failed}; "\n'
            "            + json.dumps(details, sort_keys=True, default=_json_default)"
        ),
        (
            'f"v50r3 MARK bundle is not ready: {failed}; missing price files "\n'
            '                f"{missing}; held positions without a {stamp_text} close "\n'
            '                f"{unpriced}. Retry once the closes are published.'
        ),
        (
            '"held positions have no close after their last stored date inside a "\n'
            '            f"holding window: {details}. Retry once the closes are published; a "'
        ),
        'f"{row[\'ticker\']} (last close {row[\'last_price_date\']})"',
        '"pool_candidates_priced_at_as_of": not missing_as_of,',
        '"pool_candidates_have_momentum_start_close": not missing_start,',
    ):
        assert piece in source, piece


def test_a_signal_held_by_stocks_without_a_close_names_them() -> None:
    log = _signal_refusal(
        ["pool_candidates_priced_at_as_of"], candidates_without_as_of_close=["XYZ", "ABC"]
    )
    assert notify.blocked_by(log) == {
        "purpose": "SIGNAL",
        "without_as_of_close": ["ABC", "XYZ"],
        "without_start_close": [],
    }
    log = _signal_refusal(
        ["pool_candidates_have_momentum_start_close"],
        candidates_without_momentum_start_close=["OLD"],
    )
    assert notify.blocked_by(log)["without_start_close"] == ["OLD"]


def test_an_unpublished_session_or_a_split_like_move_holds_nothing() -> None:
    many = [f"T{number}" for number in range(notify.MOST_HELD_STOCKS + 1)]
    for log in (
        _signal_refusal(["pool_candidates_priced_at_as_of"], candidates_without_as_of_close=many),
        _signal_refusal(
            ["pool_candidates_priced_at_as_of", "pool_candidates_split_like_moves_explained"],
            candidates_without_as_of_close=["XYZ"],
        ),
        "ValueError: COMP official index market is not closed\n",
        "RuntimeError: QQQ public history is not ready through bundle as-of\n",
        _mark_refusal(
            ["held_positions_priced_at_as_of", "nasdaq_through_as_of"],
            [{"ticker": "XYZ", "latest_date": "2026-10-01"}],
        ),
        "RuntimeError: something else broke\n",
    ):
        assert notify.blocked_by(log) is None, log


def test_a_mark_held_by_positions_without_a_close_names_them() -> None:
    log = _mark_refusal(
        ["held_positions_priced_at_as_of"],
        [{"ticker": "XYZ", "latest_date": "2026-10-01"}, {"ticker": "ABC", "latest_date": None}],
    )
    assert notify.blocked_by(log) == {"purpose": "MARK", "without_as_of_close": ["ABC", "XYZ"]}
    log = (
        "RuntimeError: held positions have no close after their last stored date inside "
        "a holding window: XYZ (last close 2026-09-29), ABC (last close 2026-09-30). Retry "
        "once the closes are published; a halt resumes by itself.\n"
    )
    assert notify.blocked_by(log) == {
        "purpose": "MARK",
        "without_as_of_close": ["ABC", "XYZ"],
        "last_closes": {"XYZ": "2026-09-29", "ABC": "2026-09-30"},
    }


SIGNAL_CHECK = {
    "action": "RUN_SIGNAL", "as_of": "2026-09-30", "due_signal_date": "2026-09-30",
    "signal_window_utc": {"opens": "2026-09-30T20:30:00+00:00",
                          "closes": "2026-10-01T08:00:00+00:00"},
}


def test_a_held_signal_is_explained_with_its_window() -> None:
    log = _signal_refusal(
        ["pool_candidates_priced_at_as_of"], candidates_without_as_of_close=["XYZ"]
    )
    problem = notify.compose_problem("blocked", "RUN_SIGNAL", SIGNAL_CHECK, log, {})
    assert problem["subject"] == "v50r3 2026-09-30 信号暂缓：XYZ 缺收盘价"
    text = "\n".join(problem["lines"])
    assert "候选股 XYZ 没有 2026-09-30 的收盘价，多半是停牌" in text
    assert notify.RETRY_ONCE in problem["lines"]
    assert "窗口在北京时间 10-01 16:00 关闭" in text
    assert problem["marker"] == {
        "key": "held:SIGNAL:2026-09-30:XYZ", "kind": "held", "purpose": "SIGNAL",
        "as_of": "2026-09-30", "without_as_of_close": ["XYZ"], "without_start_close": [],
    }
    # A missing momentum-start close is never published later: no retry.
    log = _signal_refusal(
        ["pool_candidates_have_momentum_start_close"],
        candidates_without_momentum_start_close=["OLD"],
    )
    problem = notify.compose_problem("blocked", "RUN_SIGNAL", SIGNAL_CHECK, log, {})
    assert notify.RETRY_ONCE not in problem["lines"]
    assert "这个日期的信号不会再通过" in "\n".join(problem["lines"])


def test_a_held_mark_says_when_a_terminal_return_is_needed() -> None:
    log = (
        "RuntimeError: held positions have no close after their last stored date inside "
        "a holding window: XYZ (last close 2026-09-29). Retry once the closes are "
        "published; a halt resumes by itself.\n"
    )
    problem = notify.compose_problem(
        "blocked", "RUN_MARK", {"action": "RUN_MARK", "as_of": "2026-10-02"}, log, {}
    )
    assert problem["subject"] == "v50r3 2026-10-02 估值暂缓：持仓 XYZ 缺收盘价"
    text = "\n".join(problem["lines"])
    assert "XYZ（最后收盘 2026-09-29）" in text and "TERMINAL_RETURN" in text
    # A halt can last days: one report per stock, whatever the date.
    assert problem["marker"]["key"] == "held:MARK:XYZ"


def test_failures_say_what_to_do() -> None:
    log = _signal_refusal(
        ["pool_candidates_split_like_moves_explained"],
        unexplained_split_like_moves=[{
            "ticker": "XYZ", "session": "2026-09-15", "raw_price_ratio": 0.5,
            "reason": "whole_factor",
        }],
    )
    problem = notify.compose_problem("failed", "RUN_SIGNAL", SIGNAL_CHECK, log, {})
    assert problem["subject"] == "v50r3 2026-09-30 信号失败：XYZ 疑似拆股，需要补录"
    text = "\n".join(problem["lines"])
    assert "XYZ 在 2026-09-15 的收盘价是前一个收盘价的 0.5 倍" in text
    assert "SPLIT" in text and "record sourced event" in text and "10-01 16:00" in text
    assert problem["marker"]["key"] == "event:SIGNAL:2026-09-30:XYZ"

    mark = {"action": "RUN_MARK", "as_of": "2026-10-05"}
    log = (
        "RuntimeError: Unresolved corporate action affects a strategy target: "
        "XYZ@2026-10-02, ABC@2026-10-05\n"
    )
    problem = notify.compose_problem("failed", "RUN_MARK", mark, log, {})
    assert problem["subject"] == "v50r3 2026-10-05 估值失败：ABC、XYZ 疑似拆股，需要补录"
    assert problem["marker"]["key"] == "event:MARK:ABC,XYZ"

    pushed = {**SIGNAL_CHECK, "git": {"error": "! [rejected] HEAD -> live/v50r3 (fetch first)"}}
    problem = notify.compose_problem("failed", "RUN_SIGNAL", pushed, "", {})
    assert problem["subject"] == "v50r3 2026-09-30 信号没有推送"
    assert "推送 live/v50r3 失败：! [rejected]" in problem["lines"][0]

    token = "ghs_" + "a" * 36
    log = f"Traceback ...\nRuntimeError: SEC ticker map unavailable: HTTP 503 for {token}\n"
    problem = notify.compose_problem(
        "failed", "RUN_SIGNAL", SIGNAL_CHECK, log, {"GH_TOKEN": token}
    )
    assert problem["lines"][0] == "错误：RuntimeError: SEC ticker map unavailable: HTTP 503 for ***"
    assert problem["marker"]["key"] == (
        "failed:SIGNAL:2026-09-30:RuntimeError:SEC ticker map unavailable: HTTP ### for ***"
    )
    problem = notify.compose_problem("failed", "RUN_MARK", mark, None, {})
    assert problem["subject"] == "v50r3 2026-10-05 估值失败"
    assert problem["marker"]["key"] == "failed:MARK:no-error-line"


def test_a_missed_month_and_a_ready_run_are_reported_as_such() -> None:
    decision = {
        "action": "RUN_MARK", "as_of": "2026-10-05", "due_signal_date": "2026-09-30",
        "signal_window_missed": True,
        "next_catch_up_window_utc": {"as_of": "2026-10-06",
                                     "opens": "2026-10-06T20:30:00+00:00"},
    }
    problem = notify.compose_problem("missed", "RUN_MARK", decision, "", {})
    assert problem["subject"] == "v50r3 2026-09-30 月末信号错过了"
    assert "下一个补跑窗口北京时间 10-07 04:30 打开。" in problem["lines"]
    assert problem["marker"] == {"key": "missed:2026-09-30", "kind": "missed"}
    for result in ("done", "not_ready"):
        assert notify.compose_problem(result, "RUN_SIGNAL", SIGNAL_CHECK, "", {}) is None


class _GitHub:
    """The issue comments API, as the job token sees it."""

    def __init__(self, comments=()):
        self.comments = [
            {"user": {"login": login}, "body": body, "created_at": f"2026-09-30T2{i}:00:00Z"}
            for i, (login, body) in enumerate(comments)
        ]
        self.posted: list[str] = []
        self.refuse = False

    def __call__(self, request, timeout):
        if self.refuse:
            raise OSError("HTTP Error 502: Bad Gateway")
        if request.get_method() == "POST":
            body = json.loads(request.data)["body"]
            self.posted.append(body)
            self.comments.append({
                "user": {"login": notify.BOT_LOGIN}, "body": body,
                "created_at": "2026-10-01T03:00:00Z",
            })
            return _Response()
        assert "since=" in request.full_url and request.get_method() == "GET"
        return _Listing(self.comments)


class _Listing(_Response):
    status = 200

    def __init__(self, comments):
        self.payload = json.dumps(comments).encode("utf-8")

    def read(self):
        return self.payload


ENV = {"GH_TOKEN": "token", "GITHUB_REPOSITORY": "owner/repo", "NOTIFY_ISSUE": "2"}


def _marker(**fields) -> str:
    return f"text\n<!-- v50r3-report {json.dumps(fields, sort_keys=True)} -->\n"


def test_only_the_job_tokens_reports_count(monkeypatch: pytest.MonkeyPatch) -> None:
    github = _GitHub([
        (notify.BOT_LOGIN, _marker(key="missed:2026-09-30", kind="missed")),
        ("someone", _marker(key="held:SIGNAL:2026-09-30:XYZ", kind="held",
                            purpose="SIGNAL", as_of="2026-09-30")),
        (notify.BOT_LOGIN, "**v50r3 2026-10-01 估值** without a key"),
    ])
    monkeypatch.setattr(notify, "urlopen", github)
    assert [report["key"] for report in notify.reports(ENV)] == ["missed:2026-09-30"]


def test_a_problem_is_reported_once_and_a_held_signal_once_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github = _GitHub()
    monkeypatch.setattr(notify, "urlopen", github)
    log = _signal_refusal(
        ["pool_candidates_priced_at_as_of"], candidates_without_as_of_close=["XYZ"]
    )
    held = notify.compose_problem("blocked", "RUN_SIGNAL", SIGNAL_CHECK, log, {})
    missed = notify.compose_problem("missed", "RUN_MARK", {"due_signal_date": "2026-09-30"}, "", {})
    for _attempt in range(3):
        notify._report_problem(held, None, ENV)
        notify._report_problem(missed, None, ENV)
    assert len(github.posted) == 3
    first, missed_body, retry = github.posted
    assert first.startswith("**v50r3 2026-09-30 信号暂缓：XYZ 缺收盘价**")
    assert notify.RETRY_ONCE in first and notify.RETRIED not in first
    assert notify.RETRIED in retry and notify.RETRY_ONCE not in retry
    assert missed_body.startswith("**v50r3 2026-09-30 月末信号错过了**")
    assert first.rstrip().endswith("-->")


def test_a_held_signal_waits_until_its_closes_are_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = {
        "key": "held:SIGNAL:2026-09-30:XYZ", "kind": "held", "purpose": "SIGNAL",
        "as_of": "2026-09-30", "without_as_of_close": ["XYZ"], "without_start_close": [],
    }
    github = _GitHub([(notify.BOT_LOGIN, _marker(**held))])
    monkeypatch.setattr(notify, "urlopen", github)
    asked = []

    def has_close(answer):
        return lambda ticker, session: asked.append((ticker, session)) or answer

    assert notify.still_blocked("2026-09-30", ENV, has_close(False))["blocked"] is True
    assert asked == [("XYZ", "2026-09-30")]
    assert notify.still_blocked("2026-09-30", ENV, has_close(True))["blocked"] is False
    # A failed check never holds the SIGNAL back.
    assert notify.still_blocked("2026-09-30", ENV, has_close("HTTPError: 403"))["blocked"] is False
    # Another session, or nothing reported: no wait.
    assert notify.still_blocked("2026-10-01", ENV, has_close(False))["blocked"] is False
    # Once the retry was held the same way, or a start close is missing, it waits for good.
    github.comments.append({"user": {"login": notify.BOT_LOGIN}, "body": _marker(**held),
                            "created_at": "2026-10-01T02:00:00Z"})
    assert notify.still_blocked("2026-09-30", ENV, has_close(True))["blocked"] is True
    start = {**held, "key": "held:SIGNAL:2026-10-01:OLD", "as_of": "2026-10-01",
             "without_as_of_close": [], "without_start_close": ["OLD"]}
    github.comments.append({"user": {"login": notify.BOT_LOGIN}, "body": _marker(**start),
                            "created_at": "2026-10-01T22:00:00Z"})
    assert notify.still_blocked("2026-10-01", ENV, has_close(True))["blocked"] is True


def test_the_cli_reports_a_failed_run_and_checks_a_held_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    github = _GitHub()
    monkeypatch.setattr(notify, "urlopen", github)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("MAIL_USERNAME", raising=False)
    monkeypatch.delenv("MAIL_PASSWORD", raising=False)
    (tmp_path / "run.json").write_text("", encoding="utf-8")
    (tmp_path / "decision.json").write_text(json.dumps(SIGNAL_CHECK), encoding="utf-8")
    log = tmp_path / "run.log"
    log.write_text(_signal_refusal(
        ["pool_candidates_priced_at_as_of"], candidates_without_as_of_close=["XYZ"]
    ), encoding="utf-8")
    argv = ["--result", "blocked", "--action", "RUN_SIGNAL", "--root", str(tmp_path),
            "--decision", str(tmp_path / "run.json"),
            "--check", str(tmp_path / "decision.json"), "--log", str(log)]

    assert notify.main(["--blocked-by", str(log)]) == 0
    assert capsys.readouterr().out == "blocked\n"
    assert notify.main(argv) == 0
    assert len(github.posted) == 1 and "XYZ" in github.posted[0]
    assert "XYZ" not in capsys.readouterr().out

    monkeypatch.setattr(notify, "_has_close", lambda ticker, session: False)
    assert notify.main(["--still-blocked", "--as-of", "2026-09-30"]) == notify.HELD_EXIT
    github.refuse = True
    assert notify.main(["--still-blocked", "--as-of", "2026-09-30"]) == 0
    assert '"error": "OSError"' in capsys.readouterr().out
