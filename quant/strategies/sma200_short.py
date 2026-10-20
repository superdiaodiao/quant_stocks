"""Shorting below the 200-day SMA (docs/research_ledger_sma200_short.md, section 0): the signal variants, the position
maps, the trial grid and the inverse-fund leg.

Signal (only data up to and including the close of t): the frozen ``T01_L200_b2`` state machine of
docs/research_ledger_qqq_timing.md on a level series P: on when P > 1.02 x SMA200, off when P < 0.98 x SMA200, else
the previous state (``S2``); the plain cross with no band (``S0``). ``quant.signals.technical.trend_state``.

Positions (exposure to the index when the state is on / off): LS1 = +1 / -1, FS1 = 0 / -1 (short only), LS0.5 =
+1 / -0.5; the long/flat comparator T01 = +1 / 0. Engines: quant.backtest.long_short.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from quant.data import inverse_etfs
from quant.evaluation.criteria import ab_verdict, bonferroni_t
from quant.evaluation.equity import equity0, period_metrics, t_monthly_excess
from quant.evaluation.metrics import cagr_of, max_drawdown
from quant.evaluation.periods import halves
from quant.signals.technical import trend_state

SMA_LEN = 200
SIGNALS = {"S2": 0.02, "S0": 0.0}
POSITIONS = {"LS1": (1.0, -1.0), "FS1": (0.0, -1.0), "LS0.5": (1.0, -0.5)}
LONG_FLAT = {"T01": (1.0, 0.0)}
IMPLS = ("margin", "psq")
INDEX_SYMBOLS = ("^NDX", "^IXIC", "^GSPC")


@dataclass(frozen=True)
class Trial:
    signal: str
    position: str
    impl: str          # "margin" / "psq" (QQQ family) or an index symbol (index level)

    @property
    def name(self) -> str:
        return f"{self.position}_{self.signal}_{self.impl}"


def qqq_trials() -> list[Trial]:
    return [Trial(s, p, i) for i in IMPLS for s in SIGNALS for p in POSITIONS]


def index_trials() -> list[Trial]:
    return [Trial(s, p, x) for x in INDEX_SYMBOLS for s in SIGNALS for p in POSITIONS]


def state(level: pd.Series, signal: str) -> pd.Series:
    """1.0 on / 0.0 off / NaN before the SMA exists, decided at each close."""
    return trend_state(level, SMA_LEN, SIGNALS[signal])


def target(st: pd.Series, position: str) -> pd.Series:
    on, off = {**POSITIONS, **LONG_FLAT}[position]
    return st.map({1.0: on, 0.0: off})


def inverse_leg(r1: pd.Series, rf: pd.Series, real: pd.Series, leverage: float, er: float, spread: float,
                synthetic_only: bool = False) -> pd.Series:
    """Daily return of the inverse fund on r1's sessions: the real fund's total return from its first return on,
    the synthetic daily-reset model (``inverse_etfs.synthetic_inverse`` at ``spread``) before (or everywhere)."""
    syn = inverse_etfs.synthetic_inverse(r1, rf, leverage, er, spread)
    if synthetic_only:
        return syn
    out = syn.copy()
    ok = real.reindex(r1.index).notna()
    out[ok] = real.reindex(r1.index)[ok]
    return out


def calibrate(real: pd.Series, r1: pd.Series, rf: pd.Series, leverage: float, er: float, start: str,
              end: str) -> dict:
    """Spread of the synthetic fund calibrated on the real fund over [start, end] (CAGR match), with the tracking
    statistics at spread 0 and at the calibrated spread."""
    x = real.loc[start:end].dropna()
    r, f = r1.reindex(x.index), rf.reindex(x.index)
    s = inverse_etfs.calibrate_spread(x, r, f, leverage, er)
    return {"window": [str(x.index[0].date()), str(x.index[-1].date())], "spread_calibrated": s,
            "spread_0": inverse_etfs.tracking(x, inverse_etfs.synthetic_inverse(r, f, leverage, er, 0.0), f),
            "calibrated": inverse_etfs.tracking(x, inverse_etfs.synthetic_inverse(r, f, leverage, er, s), f)}


def validate(real: pd.Series, r1: pd.Series, rf: pd.Series, leverage: float, er: float, spread: float,
             start: str, end: str) -> dict:
    x = real.loc[start:end].dropna()
    r, f = r1.reindex(x.index), rf.reindex(x.index)
    return inverse_etfs.tracking(x, inverse_etfs.synthetic_inverse(r, f, leverage, er, spread), f)


# ======================================================================== judgement (section 0.6)

def judge(r: pd.Series, bench: pd.Series, qqq: pd.Series, longflat: pd.Series, rf: pd.Series, n_trials: int) -> dict:
    """A / B against ``bench`` (ONEQ*, or an index's own buy-and-hold) on the full period and both halves (by session
    count), A with the Bonferroni hurdle, the same A / B against ``qqq`` (report only), and C: CAGR above the long/flat
    comparator in all three parts and the full-period t of the monthly difference >= 2. All series on r's index."""
    i1, i2 = halves(r.index)
    parts = {"full": r.index, "H1": i1, "H2": i2}
    m = {}
    for k, ix in parts.items():
        x = period_metrics(r.loc[ix], bench.loc[ix], qqq.loc[ix], rf)
        lf = longflat.loc[ix]
        x["longflat_cagr"] = cagr_of(lf)
        x["cagr_minus_longflat"] = x["cagr"] - x["longflat_cagr"]
        x["longflat_max_dd"] = max_drawdown(equity0(lf))
        x["dd_shallower_than_longflat_pp"] = (abs(x["longflat_max_dd"]) - abs(x["max_dd"])) * 100
        x["t_monthly_excess_vs_longflat"] = t_monthly_excess(r.loc[ix], lf)
        m[k] = x
    v = ab_verdict(m["full"], m["H1"], m["H2"])
    v["A_bonferroni"] = bool(all(m[h]["cagr"] > m[h]["oneq_cagr"] for h in m)
                             and m["full"]["t_monthly_excess_vs_oneq"] >= bonferroni_t(n_trials))
    rk = lambda x: {"cagr": x["cagr"], "oneq_cagr": x["qqq_cagr"], "t_monthly_excess_vs_oneq": x["t_monthly_excess_vs_qqq"],
                    "dd_shallower_than_oneq_pp": x["dd_shallower_than_qqq_pp"]}
    vq = ab_verdict(rk(m["full"]), rk(m["H1"]), rk(m["H2"]))
    c = bool(all(m[h]["cagr"] > m[h]["longflat_cagr"] for h in m) and m["full"]["t_monthly_excess_vs_longflat"] >= 2.0)
    return {"metrics": m, "verdict": v, "verdict_vs_qqq": vq, "C_short_leg_adds_value": c, "split": str(i2[0].date())}
