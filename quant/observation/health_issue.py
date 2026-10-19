"""Open, update or close the GitHub issue "Forward observation health" after a daily run
(.github/workflows/forward_observation_daily.yml, step "Report to the health issue"), and post the weekly warning
digest.

  python -m quant.observation.health_issue --report health_report.json --exit-code 3 --run-url URL \
      --commit-outcome success --mention @owner

- a failed check (exit code 3), a run that stopped before the checks finished (any other code, or no report), or
  results that could not be pushed (``--commit-outcome failure``): open the issue (reopen the bot's last one), or
  update the open one. Its body always shows the latest run; a comment, which is what makes GitHub email the
  mentioned owner, is added only when the set of failures changes (dates and numbers aside), so a failure that
  persists does not email every day;
- every check ok: close the open issue with a comment. Warnings alone close it only on the evening's last run
  (``run.early`` false in the report): an early run's warning may still turn into a failure that evening;
- on the evening's last run of the week's last session (``run.week_last``): when any check warns, open or comment on
  the issue "Forward observation weekly warnings" (one email a week while warnings persist, so they are never
  silent); with no warning it is closed.

Only issues opened by the workflow's bot are touched (anyone can open an issue with that title in a public repo).
Needs ``gh`` with GH_TOKEN and GH_REPO in the environment. Standard library only, so it also runs when the Python
dependencies could not be installed. Bodies hold statuses, dates, symbols and percentages, never price levels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TITLE = "Forward observation health"
TITLE_WARN = "Forward observation weekly warnings"
BOTS = ("app/github-actions", "github-actions[bot]", "github-actions")
_RANK = {"ok": 0, "warn": 1, "fail": 2}


def gh(args: list) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def _table(rep: dict) -> list:
    lines = ["| check | status | message |", "|---|---|---|"]
    order = sorted(rep["checks"], key=lambda c: -_RANK.get(c["status"], 0))
    return lines + [f"| {c['name']} | {c['status']} | {c['message'].replace('|', '/')} |" for c in order]


def _items(rep: dict, status: str) -> list:
    out = []
    for c in rep["checks"]:
        if c["status"] == status and c.get("items"):
            out += ["", f"**{c['name']}**", ""] + [f"- {x}" for x in c["items"]]
    return out


def failure_core(rep: dict | None, exit_code: str = "", commit_outcome: str = "") -> str:
    """The part of the issue body that describes what is wrong (the signature is taken from it)."""
    parts = []
    if commit_outcome == "failure":
        parts.append("## The results were not pushed\n\nThe commit / push of the log and the state failed (see the "
                     "run log): the repository does not hold this run's results, and the next run starts from the "
                     "previous state.")
    if rep is None or str(exit_code) not in ("0", "3"):
        parts.append(f"## The daily run stopped before the health checks finished\n\nExit code: "
                     f"{exit_code or 'unknown'}. Nothing was checked this time; the run log has the error.")
    if rep is not None:
        lines = [f"## {rep['status'].upper()}: forward observation health, session {rep['session']}", ""]
        lines += _table(rep) + _items(rep, "fail")
        warn = _items(rep, "warn")
        if warn:
            lines += ["", "### Warnings"] + warn
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def signature(core: str) -> str:
    """Of the failing checks and their items, with dates and numbers taken out: a failure that persists from day to
    day keeps its signature; a new failing check or a new symbol changes it."""
    keep = []
    for block in core.split("\n## "):
        if block.startswith("## "):
            block = block[3:]
        head = block.split("\n", 1)[0]
        if head.startswith(("FAIL", "WARN", "OK")):
            body = block.split("### Warnings")[0]
            fails = [ln for ln in body.splitlines() if ln.startswith("| ") and "| fail |" in ln]
            names = [ln.split("|")[1].strip() for ln in fails]
            items = [ln for ln in body.splitlines() if ln.startswith("- ")]
            keep.append("fail:" + ",".join(sorted(names)) + "\n" + "\n".join(sorted(items)))
        else:
            keep.append(head)
    text = re.sub(r"\d+(\.\d+)?", "#", "\n".join(keep))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def body_of(rep: dict | None, run_url: str = "", exit_code: str = "", commit_outcome: str = "",
            mention: str = "") -> str:
    core = failure_core(rep, exit_code, commit_outcome)
    head = ("_Opened and updated by `.github/workflows/forward_observation_daily.yml`; closed automatically when "
            "every check passes again. The body shows the latest run; a comment is added when the failures "
            "change._")
    who = f"{mention}\n\n" if mention else ""
    return (f"{who}{core}\n\n{head}\n\nRun: {run_url or 'n/a'}\n\n<!-- signature: {signature(core)} -->\n")


def _sig_of(body: str) -> str | None:
    m = re.search(r"<!-- signature: (\w+) -->", body or "")
    return m.group(1) if m else None


def _by_bot(it: dict) -> bool:
    a = it.get("author")
    if a is None:                      # (an older gh without the author field: the title decides)
        return True
    return (a.get("login") or "") in BOTS or (bool(a.get("is_bot")) and "github-actions" in (a.get("login") or ""))


def find_issue(title: str, state: str = "open", run=gh) -> dict | None:
    """The bot's most recent issue with this title in ``state``."""
    got = json.loads(run(["issue", "list", "--state", state, "--limit", "100",
                          "--json", "number,title,body,author"]) or "[]")
    mine = [it for it in got if it["title"] == title and _by_bot(it)]
    return max(mine, key=lambda it: it["number"]) if mine else None


def find_open(run=gh) -> dict | None:
    return find_issue(TITLE, "open", run)


def _with_file(text: str, fn):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        return fn(path)
    finally:
        os.unlink(path)


def _post(title: str, body: str, comment: str, run, issue: dict | None) -> str:
    """Open (or reopen the last closed) issue with ``body``; on an open one, rewrite the body and add ``comment``."""
    def go(path):
        if issue is None:
            old = find_issue(title, "closed", run)
            if old is None:
                run(["issue", "create", "--title", title, "--body-file", path])
                return "opened"
            run(["issue", "reopen", str(old["number"])])
            run(["issue", "edit", str(old["number"]), "--body-file", path])
            _with_file(comment, lambda c: run(["issue", "comment", str(old["number"]), "--body-file", c]))
            return f"reopened #{old['number']}"
        run(["issue", "edit", str(issue["number"]), "--body-file", path])
        if comment:
            _with_file(comment, lambda c: run(["issue", "comment", str(issue["number"]), "--body-file", c]))
            return f"updated #{issue['number']}"
        return f"#{issue['number']} body refreshed"
    return _with_file(body, go)


def sync(rep: dict | None, exit_code: str, run_url: str = "", run=gh, mention: str = "",
         commit_outcome: str = "") -> str:
    """Bring the health issue in line with this run; returns what was done."""
    early = bool(((rep or {}).get("run") or {}).get("early"))
    status = (rep or {}).get("status")
    failing = (commit_outcome == "failure" or str(exit_code) != "0" or rep is None or status == "fail")
    issue = find_open(run)
    if not failing:
        if issue is None:
            return "nothing to do"
        if status == "warn" and early:
            return f"#{issue['number']} left open (warnings on an early run)"
        run(["issue", "close", str(issue["number"]), "--comment",
             f"All checks pass again (session {rep['session']}{', warnings only' if status == 'warn' else ''}). "
             f"Run: {run_url or 'n/a'}"])
        return f"closed #{issue['number']}"
    body = body_of(rep if str(exit_code) in ("0", "3") else None, run_url, str(exit_code), commit_outcome, mention)
    if issue is not None and _sig_of(issue.get("body", "")) == _sig_of(body):
        if issue.get("body") == body:
            return f"#{issue['number']} unchanged"
        return _post(TITLE, body, "", run, issue)               # same failures: body refreshed, no email
    return _post(TITLE, body, body, run, issue)


def sync_weekly(rep: dict | None, run_url: str = "", run=gh, mention: str = "") -> str:
    """The weekly warning digest (see the module text)."""
    info = (rep or {}).get("run") or {}
    if rep is None or info.get("early") or not info.get("week_last"):
        return "no digest (not the week's last evening run)"
    warn = [c for c in rep["checks"] if c["status"] == "warn"]
    issue = find_issue(TITLE_WARN, "open", run)
    if not warn:
        if issue is None:
            return "no warnings this week"
        run(["issue", "close", str(issue["number"]), "--comment", f"No warnings at session {rep['session']}."])
        return f"closed #{issue['number']}"
    lines = ([mention, ""] if mention else []) + [f"## Warnings at the end of the week, session {rep['session']}", ""]
    for c in warn:
        lines += [f"**{c['name']}**: {c['message']}", ""] + [f"- {x}" for x in c.get("items", [])] + [""]
    lines += ["_Warnings do not fail the daily job; this digest makes sure they are seen once a week. Posted by "
              "`.github/workflows/forward_observation_daily.yml`; closed when a week ends without warnings._",
              "", f"Run: {run_url or 'n/a'}", "", f"<!-- digest: {rep['session']} -->"]
    body = "\n".join(lines) + "\n"
    if issue is not None and f"<!-- digest: {rep['session']} -->" in (issue.get("body") or ""):
        return f"#{issue['number']} digest already posted for {rep['session']}"
    return _post(TITLE_WARN, body, body, run, issue)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", required=True, help="the report JSON of the daily run (may be missing)")
    ap.add_argument("--exit-code", default="", help="exit code of the daily run (empty: it did not run)")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--commit-outcome", default="", help="outcome of the commit / push step (failure: not pushed)")
    ap.add_argument("--mention", default="", help="who to @mention in the issue (GitHub emails a mentioned user)")
    a = ap.parse_args(argv)
    p = Path(a.report)
    rep = json.loads(p.read_text()) if p.exists() else None
    print("health issue:", sync(rep, a.exit_code, a.run_url, mention=a.mention, commit_outcome=a.commit_outcome))
    print("weekly digest:", sync_weekly(rep, a.run_url, mention=a.mention))
    return 0


if __name__ == "__main__":
    sys.exit(main())
