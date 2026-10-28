# region imports
from AlgorithmImports import *
# endregion
# Diagnostic only: how many chart points per series a minute-resolution backtest keeps (export channel for the
# 0DTE grid, since the Object Store is read-only on this account). Plots a running sequence number on 40 charts x
# 10 series every minute; the reader counts what comes back. Ledger: docs/research_ledger_zero_dte.md, section 0.1.


class ChartCap(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2024, 7, 1)
        self.set_end_date(2024, 7, 26)
        self.set_cash(100_000)
        self.add_equity("QQQ", Resolution.MINUTE)
        self.n = 0

    def on_data(self, data):
        self.n += 1
        base = 900_000_000_000_000.0 + self.n * 1000
        for c in range(40):
            for s in range(10):
                self.plot(f"P{c:02d}", f"s{s}", base + c * 10 + s)

    def on_end_of_algorithm(self):
        self.set_runtime_statistic("N", str(self.n))
