"""The contact string SEC asks every automated client to send as its User-Agent.

SEC's fair-access policy wants a real contact. The owner's address lives
outside git: ``$SEC_USER_AGENT``, else ``SEC_USER_AGENT=...`` in the nearest
``.env.sec`` above this file (the main checkout's, also from a worktree).
"""
from __future__ import annotations

import os
from pathlib import Path

PLACEHOLDER = "quant_stocks research data@example.com"


def sec_user_agent(start: str | Path | None = None) -> str:
    if os.environ.get("SEC_USER_AGENT", "").strip():
        return os.environ["SEC_USER_AGENT"].strip()
    for folder in Path(start or __file__).resolve().parents:
        path = folder / ".env.sec"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if key.strip() == "SEC_USER_AGENT" and value:
                return value
    return PLACEHOLDER
