"""Pre-registered studies, one module per ledger (docs/architecture.md).

Each module: frozen parameters and rules at the top, ``run(args)`` that loads data, simulates, evaluates and writes
``output/research_only/<name>/``, and ``main(argv)``. Studies import quant only, never another study.
Run one with ``python -m quant study <name>`` (or the legacy ``scripts/research_<name>.py`` wrapper).
"""
