# region imports
from AlgorithmImports import *
# endregion
# Diagnostic only: does the chart export channel of qc/zero_dte (40 charts x 10 series, 4,000 points each) scale to
# more charts, or to more series per chart? Plots a running sequence number every minute on 100 charts x 10 series
# (C00..C99) and on 4 charts x 25 series (W0..W3); the reader counts what comes back. Ledger:
# docs/research_ledger_stock_0dte.md, section 0.1.


class ChartCap2(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2024, 7, 1)
        self.set_end_date(2024, 7, 26)
        self.set_cash(100_000)
        self.add_equity("QQQ", Resolution.MINUTE)
        self.n = 0

    def on_data(self, data):
        self.n += 1
        base = 900_000_000_000_000.0 + self.n * 10_000
        for c in range(100):
            for s in range(10):
                self.plot(f"C{c:02d}", f"s{s}", base + c * 10 + s)
        for c in range(4):
            for s in range(25):
                self.plot(f"W{c}", f"s{s:02d}", base + 5_000 + c * 25 + s)

    def on_end_of_algorithm(self):
        self.set_runtime_statistic("N", str(self.n))
