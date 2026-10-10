"""Reusable research core (docs/architecture.md).

Layers, each importing only the ones below it:

    studies/            one thin file per pre-registered study (not part of this package)
    quant.strategies    frozen rules built from signals (phase 2)
    quant.signals       pure signal functions
    quant.backtest      engines, execution conventions, the IBKR cost model
    quant.evaluation    metrics, pass criteria, multiple-testing helpers
    quant.data          data versions, date guards, loaders, vendor parsers

Every function here was extracted from an October-2026 study script without changing its arithmetic; the tests in
tests/quant/ compare each one with the original it came from.
"""
