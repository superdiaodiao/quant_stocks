"""Reporting and IO helpers shared by the studies: JSON result files, numpy-to-JSON conversion, the run log and the
pre-registration hash (docs/architecture.md section 12).

Every helper reproduces one formatting convention that frozen result files were written with, so a study switching
to it writes the same bytes. The variants that exist are parameters, never silent changes:

- ``write_json``: ``json.dumps(obj, indent=..., default=...)`` (+ ``"\\n"`` with ``newline=True``) written to ``path``.
- ``scalar``: numpy scalars to Python floats (and ``np.bool_`` to ``bool`` unless ``bools=False``); everything else
  unchanged.
- ``public``: a metric dict without its private ``_``-prefixed entries (and the keys in ``drop``), optionally passed
  through ``scalar``.
- ``json_default`` / ``json_rounded``: the two ``default=`` hooks of the studies (exact numpy conversion with
  ``str`` as the fallback; floats rounded to 6 places with non-finite values as ``null``).
- ``RunLog``: prints each message (flushed) and keeps it for ``run_log.txt``.
- ``Preregistration``: the ledger block between ``<!-- PREREG-BEGIN -->`` and ``<!-- PREREG-END -->``, its SHA-256 and
  the frozen-registration file written once before a run (moved here from ``quant/prereg.py``).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

BEGIN, END = "<!-- PREREG-BEGIN -->", "<!-- PREREG-END -->"


def write_json(path: Path, obj, *, indent: int | None = 2, default=str, newline: bool = False) -> None:
    """``path.write_text(json.dumps(obj, indent=indent, default=default) [+ "\\n"])``."""
    Path(path).write_text(json.dumps(obj, indent=indent, default=default) + ("\n" if newline else ""))


def scalar(v, *, bools: bool = True):
    """A numpy float / integer as a Python float; with ``bools`` a numpy bool as ``bool``; anything else as is."""
    if isinstance(v, (np.floating, np.integer)) and not isinstance(v, bool):
        return float(v)
    if bools and isinstance(v, np.bool_):
        return bool(v)
    return v


def public(m: dict, *, drop: tuple = (), convert: bool = False, bools: bool = True) -> dict:
    """``m`` without the ``_``-prefixed keys and the keys in ``drop``; values through ``scalar`` when ``convert``."""
    return {k: (scalar(v, bools=bools) if convert else v) for k, v in m.items()
            if not k.startswith("_") and k not in drop}


def json_default(o):
    """``json.dumps`` hook: numpy integers / floats / bools / arrays exactly, anything else as ``str``."""
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


def json_rounded(x):
    """``json.dumps`` hook: floats rounded to 6 places (non-finite as ``null``), numpy integers / bools exactly,
    anything else as ``str``."""
    if isinstance(x, (np.floating, float)):
        return None if not np.isfinite(x) else round(float(x), 6)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return str(x)


class RunLog:
    """``say(*parts)`` prints the space-joined message (flushed) and records it; ``write(path)`` saves the lines."""

    def __init__(self):
        self.lines: list[str] = []

    def say(self, *parts) -> None:
        msg = " ".join(str(x) for x in parts)
        print(msg, flush=True)
        self.lines.append(msg)

    def write(self, path: Path) -> None:
        Path(path).write_text("\n".join(self.lines) + "\n")


class Preregistration:
    """The pre-registration block of ``ledger``, its SHA-256 and the frozen-registration file ``frozen``.

    ``include_end``: whether the block ends before the END marker (the convention of fundamentals, ml_cross_section,
    short_overlay and index_exclusion) or includes it (osap_backtest)."""

    def __init__(self, ledger: Path, frozen: Path, out: Path, *, include_end: bool = False):
        self.ledger, self.frozen, self.out, self.include_end = ledger, frozen, out, include_end

    def block(self, text: str | None = None) -> str:
        t = text if text is not None else self.ledger.read_text()
        a, b = t.index(BEGIN), t.index(END)
        return t[a:b + len(END)] if self.include_end else t[a:b]

    def hash(self, text: str | None = None) -> str:
        return hashlib.sha256(self.block(text).encode()).hexdigest()

    def register(self) -> None:
        """Write ``{"sha256", "registered_at"}`` once (the convention of the four data-v2 studies)."""
        self.out.mkdir(parents=True, exist_ok=True)
        if self.frozen.exists():
            raise SystemExit(f"already registered: {json.loads(self.frozen.read_text())}")
        self.frozen.write_text(json.dumps({"sha256": self.hash(), "registered_at": pd.Timestamp.now("UTC").isoformat()},
                                          indent=1) + "\n")
        print("registered", self.hash())


# ---- period tables (G1: the ETF / index timing studies and the stock studies that copied their layout)

def write_monthly_csv(path: Path, monthly: dict) -> None:
    """``pd.DataFrame(monthly)`` with the (period) index as ``str``, written with ``float_format="%.6f"``."""
    mk = pd.DataFrame(monthly)
    mk.index = mk.index.astype(str)
    mk.to_csv(path, float_format="%.6f")


def write_period_tables(out: Path, by_year: dict, monthly: dict, prefix: str = "") -> None:
    """``<prefix>by_year.csv`` (``pd.DataFrame(by_year)``) and ``<prefix>monthly_returns.csv``
    (``write_monthly_csv``) in ``out``, both with ``float_format="%.6f"``."""
    pd.DataFrame(by_year).to_csv(Path(out) / f"{prefix}by_year.csv", float_format="%.6f")
    write_monthly_csv(Path(out) / f"{prefix}monthly_returns.csv", monthly)


# ---- ledger freeze blocks (G2: indicators, livermore, oneil): the frozen-rule files store the SHA-256 of the ledger
# text from ``<!-- <tag>-BEGIN -->`` up to (not including) ``<!-- <tag>-END -->``.

def freeze_block(ledger: Path, tag: str = "FREEZE") -> str:
    """The ledger text from the ``<tag>-BEGIN`` marker up to the ``<tag>-END`` marker (END excluded)."""
    text = Path(ledger).read_text()
    a, b = text.index(f"<!-- {tag}-BEGIN -->"), text.index(f"<!-- {tag}-END -->")
    return text[a:b]


def freeze_sha256(ledger: Path, tag: str = "FREEZE") -> str:
    return hashlib.sha256(freeze_block(ledger, tag).encode()).hexdigest()
