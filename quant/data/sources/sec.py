"""SEC EDGAR access rules shared by the data fetchers (moved unchanged from scripts/reversal_data_common.py, phase 3).

Every SEC request sends the owner's contact as its User-Agent (``src/io/sec_contact.sec_user_agent``: ``$SEC_USER_AGENT``
or the main checkout's ``.env.sec``; it is never printed) and waits on one shared limiter of 7 requests a second (SEC
asks for at most ten).

``sec_user_agent`` is re-exported from the frozen src/io/sec_contact.py (in the v50 / sue_lt_v1 import closures, so it
stays there); every other quant / studies module imports it from here. This module and quant/data/universe.py are
the only two places where quant imports src.
"""
from __future__ import annotations

from functools import lru_cache

from quant.data.sources.http import SlidingWindowLimiter
from src.io.sec_contact import sec_user_agent  # noqa: F401  (re-exported)

# SEC asks for at most ten requests a second; stay well under it.
SEC_PER_SECOND = 7
SEC_LIMITER = SlidingWindowLimiter({1: SEC_PER_SECOND})


@lru_cache(maxsize=1)
def user_agent() -> str:
    """The contact string (never printed), read once per process."""
    return sec_user_agent()


def sec_headers(agent: str | None = None) -> dict:
    return {"User-Agent": agent or user_agent(), "Accept-Encoding": "identity"}
