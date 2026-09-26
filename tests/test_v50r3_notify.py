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
        "v50r3 2026-09-30 信号：PLTR 20%", "v50r3 邮件通知测试",
    ]
    # The public run log never shows the message or the address.
    log = capsys.readouterr().out
    assert "PLTR" not in log and "example.invalid" not in log
