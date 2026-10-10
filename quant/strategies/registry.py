"""The frozen, named strategies, each declaring its frozen parameters once.

The forward observation (quant.observation) reads the observed lines from here; ``python -m quant strategies``
lists them and ``python -m quant run <name>`` runs the declared entry point. Changing a frozen value here changes a
frozen rule: it needs the owner's re-freeze (docs/architecture.md section 6) and the forward golden test
(tests/golden/forward_observation_synthetic.json) fails until the golden is regenerated on purpose.

Observed candidates (docs/forward_observation_checklist.md section B):
  S3-Yb              B1, 18-stock selective-T basket (quant.strategies.selective_t)
  SEL-A / SEL-P      B2, QQQ RSI2-dip configurations of the T grid (quant.strategies.t_grid)
  S-MISP N10 k20 MN  B3, short-loser overlay on QQQ (quant.strategies.short_overlay)
  S/P top-10         reference only: QuantConnect algorithm qc/sp_top10_trade/main.py (section A; not runnable here)
Mega-cap rules M1-M6 (docs/research_ledger_megacap.md; quant.strategies.megacap).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Kept free of quant imports: quant.strategies.megacap / selective_t read their frozen values from here, and the
# forward runner must be able to set REVERSAL_DATA_VERSION before quant.data.version loads.

U18 = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "COST", "NFLX", "CSCO", "INTC", "ADBE",
       "QCOM", "PEP", "AMD", "CMCSA", "AMGN")         # the 18 large caps of the selective-T study (B1 basket)
MEGACAP = {  # name -> (pool: top N by market cap, hold, momentum, trend filter, cap-weighted)
    "M1": {"pool": 10, "hold": 10, "mom": None, "trend": False, "capw": False},
    "M2": {"pool": 5, "hold": 5, "mom": None, "trend": False, "capw": False},
    "M3": {"pool": 20, "hold": 5, "mom": "m6", "trend": False, "capw": False},
    "M4": {"pool": 20, "hold": 10, "mom": "m12_1", "trend": False, "capw": False},
    "M5": {"pool": 20, "hold": 5, "mom": "m6", "trend": True, "capw": False},
    "M6": {"pool": 10, "hold": 10, "mom": None, "trend": False, "capw": True},
}
FORWARD_START_CLOSE = "2026-10-12"   # B1 / B2 accounts open at this close (in-sample data end at the 2026-10-09 close)


@dataclass(frozen=True)
class Strategy:
    name: str
    family: str                  # the quant.strategies module that implements it ("qc" for QuantConnect code)
    frozen: dict                 # the frozen parameters (the only place they are declared)
    ledger: str                  # where the rule was registered
    status: str
    entry: str | None = None     # "module:function" run by ``python -m quant run <name>`` (None: not runnable here)
    note: str = ""
    tags: tuple = field(default=())

    def describe(self) -> dict:
        return asdict(self)


STRATEGIES: dict[str, Strategy] = {}


def _add(s: Strategy) -> None:
    assert s.name not in STRATEGIES, s.name
    STRATEGIES[s.name] = s


_add(Strategy(
    "S3-Yb", "selective_t",
    {"config": "S3-Yb", "asset_class": "stock", "variant": "net", "universe": U18,
     "start_close": FORWARD_START_CLOSE},
    "docs/research_ledger_selective_t.md", "forward observation B1 (owner decision 2026-10-09)",
    entry="quant.observation.runner:main", tags=("observed",),
    note="one $10k cash account per stock, basket = equal-weight mean of the 18 accounts"))
_add(Strategy(
    "SEL-A", "t_grid",
    {"config_id": 29876, "label": "RSI2 p2 B OPP H10 1/2 res25 ALL", "base": "QQQ", "reserve_code": 2,
     "start_close": FORWARD_START_CLOSE},
    "docs/research_ledger_t_grid.md", "forward observation B2 (owner decision 2026-10-09)",
    entry="quant.observation.runner:main", tags=("observed",)))
_add(Strategy(
    "SEL-P", "t_grid",
    {"config_id": 29916, "label": "RSI2 p2 B OPP H20 1 res25 ALL", "base": "QQQ", "reserve_code": 2,
     "start_close": FORWARD_START_CLOSE},
    "docs/research_ledger_t_grid.md", "forward observation B2 (owner decision 2026-10-09)",
    entry="quant.observation.runner:main", tags=("observed",)))
_add(Strategy(
    "S-MISP N10 k20 MN", "short_overlay",
    {"signal": "S-MISP", "n": 10, "k": 0.20, "mode": "MN", "first_signal": "2026-10-30"},
    "docs/research_ledger_short_overlay.md", "forward observation B3 (owner decision 2026-10-10)",
    entry="quant.observation.runner:main", tags=("observed",),
    note="monthly U300 signal from fresh data (quant.observation.smisp), executed at the next close"))
_add(Strategy(
    "S/P top-10", "qc",
    {"algorithm": "qc/sp_top10_trade/main.py", "factor": "SP", "frozen_commit": "9f4516b22",
     "sha256": "f1362c4cbb3c1a522696ef8a689f18efb15f60a1f9dd1936fdbb33076acc64dd"},
    "docs/research_ledger_qc_factors.md", "forward observation section A (needs the owner's QuantConnect session)",
    tags=("observed", "reference"), note="QuantConnect algorithm; qc/ must stay self-contained, so nothing runs here"))
for _name, _params in MEGACAP.items():
    _add(Strategy(
        _name, "megacap", dict(_params),
        "docs/research_ledger_megacap.md", "tested in sample 2014-2026 and out of sample 1999-2013 (megacap_oos2/3)",
        entry="studies.megacap:main", tags=("megacap",)))


def get(name: str) -> Strategy:
    try:
        return STRATEGIES[name]
    except KeyError:
        raise KeyError(f"unknown strategy {name!r}; known: {', '.join(STRATEGIES)}") from None


def observed() -> list[Strategy]:
    return [s for s in STRATEGIES.values() if "observed" in s.tags]
