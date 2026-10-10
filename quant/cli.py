"""Command line: ``python -m quant list``, ``python -m quant study <name> [args]``, ``python -m quant strategies`` and
``python -m quant run <strategy> [args]``.

``study`` runs ``studies/<name>.py``'s ``main`` with the remaining arguments, exactly as the legacy
``scripts/research_<name>.py`` command does. ``strategies`` lists the frozen named strategies of
quant.strategies.registry with their frozen parameters; ``run`` calls a strategy's declared entry point with the
remaining arguments (the observed lines: the forward observation, e.g. ``run SEL-A --dry-run``; M1-M6: the megacap
study). This module is the only part of quant that imports studies/ (docs/architecture.md section 3).
"""
from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys

import studies
from quant.strategies import registry


def study_names() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules(studies.__path__) if not m.name.startswith("_"))


def run_strategy(name: str, rest: list) -> int:
    s = registry.get(name)
    print(f"{s.name}: {s.status}; frozen {json.dumps(s.frozen, default=str)}")
    if s.entry is None:
        print(f"not runnable here: {s.note or s.family}")
        return 0
    mod, func = s.entry.split(":")
    getattr(importlib.import_module(mod), func)(rest)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="python -m quant", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the migrated studies")
    st = sub.add_parser("study", help="run one study (remaining arguments go to the study)")
    st.add_argument("name", choices=study_names())
    sub.add_parser("strategies", help="list the frozen named strategies")
    rn = sub.add_parser("run", help="run a frozen strategy's entry point (remaining arguments go to it)")
    rn.add_argument("name", choices=list(registry.STRATEGIES))
    a, rest = ap.parse_known_args(argv)
    if a.cmd == "list":
        for name in study_names():
            doc = (importlib.import_module(f"studies.{name}").__doc__ or "").strip().split("\n")[0]
            print(f"{name:20s} {doc}")
        return 0
    if a.cmd == "strategies":
        for s in registry.STRATEGIES.values():
            print(f"{s.name:20s} {s.family:14s} {s.status}\n{'':20s} {json.dumps(s.frozen, default=str)}")
        return 0
    if a.cmd == "run":
        return run_strategy(a.name, rest)
    importlib.import_module(f"studies.{a.name}").main(rest)
    return 0
