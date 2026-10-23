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
  M1 M2 M3 M6        B4, mega-cap rules (quant.strategies.megacap; owner decision 2026-10-24)
  LEV-B3             B4, QQQ SMA200 1.5x / 1.0x via QQQ + QLD (quant.strategies.sector_lev rule B3)
  V3                 B4, QQQ volatility target = expanding long-run vol, cap 1.5x (quant.strategies.voltarget)
  BT4b               B4, 90% QQQ + 10% BTC sleeve, monthly (quant.strategies.bitcoin)
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
# B4 (owner decision 2026-10-24; docs/forward_observation_checklist.md section B4): forward data after the 2026-10-23
# close. The daily lines (LEV-B3, V3) open at the 2026-10-26 close; the monthly lines (M1, M2, M3, M6, BT4b) take
# their first signal at the 2026-10-30 month end and trade at the next session's close (2026-11-02).
B4_START_CLOSE = "2026-10-26"
B4_FIRST_SIGNAL = "2026-10-30"
B4_MEGACAP = ("M1", "M2", "M3", "M6")      # the mega-cap rules that passed in sample (ledger results, section 0.6)


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
    _b4 = _name in B4_MEGACAP
    _add(Strategy(
        _name, "megacap", dict(_params),
        "docs/research_ledger_megacap.md", "tested in sample 2014-2026 and out of sample 1999-2013 (megacap_oos2/3)"
        + ("; forward observation B4 (owner decision 2026-10-24)" if _b4 else ""),
        entry="studies.megacap:main", tags=("megacap", "observed") if _b4 else ("megacap",),
        note="B4: point-in-time Nasdaq market-cap ranking from fresh data at each month end (ledger 0.2 / 0.2a / 0.4; "
             "quant.observation.b4_megacap), traded at the next close with the costs and 25% band of 0.3; first "
             f"signal {B4_FIRST_SIGNAL}" if _b4 else ""))
# B4 lines of other studies: the frozen values below are copied from their ledgers (sections cited) and equal the
# studies' own constants (tests/test_forward_observation_b4.py checks studies.sector_lev / voltarget / bitcoin).
_add(Strategy(
    "LEV-B3", "sector_lev",
    # docs/research_ledger_sector_lev.md 0.4 B3 (exposure via QQQ + QLD leg, e >= 1 -> (2 - e) QQQ + (e - 1) QLD),
    # 0.3 (decide at the close of t, trade at the close of t + 1; rebalance at each month end and on a state change;
    # costs IBKR Tiered, half-spreads QQQ 1 bp / QLD 2 bp); docs/audit_leverage_b3.md 5 / 9: matched benchmark =
    # constant 1.37x (63% QQQ + 37% QLD, monthly rebalance), B3's average exposure in sample
    {"rule": "B3", "on": 1.5, "off": 1.0, "sma": 200, "band": 0.02, "signal": "QQQ adjusted close",
     "legs": ("QQQ", "QLD"), "rebalance": "month end and state change", "matched_exposure": 1.37,
     "start_close": B4_START_CLOSE},
    "docs/research_ledger_sector_lev.md", "forward observation B4 (owner decision 2026-10-24)",
    entry="quant.observation.runner:main", tags=("observed",),
    note="sector_lev B3 (named LEV-B3 here: the log's B3 is the S-MISP line); key comparison constant 1.37x"))
_add(Strategy(
    "V3", "voltarget",
    # docs/research_ledger_voltarget.md 0.4 V3: e = min(1.5, volLR / vol20), volLR = std of every QQQ daily return
    # from 1999-03-11 through t x sqrt(252) (>= 252 returns), vol20 = the last 20 returns; 0.3: month-end decision,
    # traded at the next close, e >= 1 -> QQQ + QLD leg, e < 1 -> QQQ + T-bill ETF (RF - 0.10% a year); 0.7: the
    # comparison is the constant portfolio at the rule's own average realised exposure (here: realised to date)
    {"rule": "V3", "cap": 1.5, "vol_short": 20, "vol_lr_min": 252, "target_vol": 0.20, "vol_quarter": 63,
     "sma_len": 200, "sma_band": 0.02, "rebalance": "month end", "start_close": B4_START_CLOSE},
    "docs/research_ledger_voltarget.md", "forward observation B4 (owner decision 2026-10-24)",
    entry="quant.observation.runner:main", tags=("observed",),
    note="target_vol / vol_quarter / sma_* are the other VolParams fields of the study; V3 reads only cap, vol_short, "
         "vol_lr_min"))
_add(Strategy(
    "BT4b", "bitcoin",
    # docs/research_ledger_bitcoin.md 0.4 BT4b: 90% QQQ + 10% BTC, decided at the last US session of each month and
    # rebalanced at the next session's close; 0.2: BTC close of UTC day S (00:00 UTC candle) on US session S,
    # Bitstamp primary, zero-volume days from Coinbase, Yahoo, Kraken; 0.3: IBKR spot crypto 0.18% (min $1.75, at
    # most 1%) + 5 bp half-spread, Paxos $0.15 a month, QQQ / ONEQ etf_order_cost with 1 / 2 bp; report only: the
    # BTC leg as IBIT (stock costs, 2 bp)
    {"sleeve": 0.10, "rebalance": "month end", "btc_half_spread": 5e-4, "qqq_half_spread": 1e-4,
     "oneq_half_spread": 2e-4, "etf_btc_half_spread": 2e-4, "btc_primary": "bitstamp",
     "btc_fill": ("coinbase", "yahoo", "kraken"), "first_signal": B4_FIRST_SIGNAL},
    "docs/research_ledger_bitcoin.md", "forward observation B4 (owner decision 2026-10-24)",
    entry="quant.observation.runner:main", tags=("observed",),
    note="comparisons ONEQ and QQQ alone; the IBIT-implemented variant is report only"))


def get(name: str) -> Strategy:
    try:
        return STRATEGIES[name]
    except KeyError:
        raise KeyError(f"unknown strategy {name!r}; known: {', '.join(STRATEGIES)}") from None


def observed() -> list[Strategy]:
    return [s for s in STRATEGIES.values() if "observed" in s.tags]
