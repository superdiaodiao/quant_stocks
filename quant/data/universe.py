"""The common-stock filter of a Nasdaq listing snapshot (preferreds, warrants, units, rights, debt, ETFs and test
issues removed).

``investable_common_equities`` lives in src/io/security_universe.py, which is in the frozen v50 / sue_lt_v1 import
closures, so it is re-exported here rather than copied: the forward observation's S-MISP line (B3) is
pre-registered on that exact function. This module and quant/data/sources/sec.py are the only two places where
quant imports src (tests/quant/test_study_library_surface.py checks it).
"""
from __future__ import annotations

from src.io.security_universe import NON_COMMON_SECURITY_PATTERN, investable_common_equities  # noqa: F401
