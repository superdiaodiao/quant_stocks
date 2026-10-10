"""Command line: ``python -m quant list`` and ``python -m quant study <name> [study arguments]``.

``study`` runs ``studies/<name>.py``'s ``main`` with the remaining arguments, exactly as the legacy
``scripts/research_<name>.py`` command does. (``run <strategy>`` arrives with the strategy registry in phase 2.)
"""
from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys

import studies


def study_names() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules(studies.__path__) if not m.name.startswith("_"))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="python -m quant", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the migrated studies")
    st = sub.add_parser("study", help="run one study (remaining arguments go to the study)")
    st.add_argument("name", choices=study_names())
    a, rest = ap.parse_known_args(argv)
    if a.cmd == "list":
        for name in study_names():
            doc = (importlib.import_module(f"studies.{name}").__doc__ or "").strip().split("\n")[0]
            print(f"{name:16s} {doc}")
        return 0
    importlib.import_module(f"studies.{a.name}").main(rest)
    return 0
