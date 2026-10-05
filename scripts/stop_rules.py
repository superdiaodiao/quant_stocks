"""Stop-loss rules shared by the stock simulators (research_livermore / research_canslim_dev / research_indicators).

Pre-registered in docs/research_ledger_stops.md. A stop is checked at a session's close (never on the entry fill
session itself) and, when hit, the position is sold at the NEXT session's close. All prices are the total-return
index the simulators already hold positions in (so dividends do not trigger stops).

Kinds:
  none          no stop
  fixed  x      close <= entry * (1 - x)                         (entry = first fill close)
  trail  x      close <= highest close since entry * (1 - x)     (the entry fill close counts as the first high)
  vol    k      close <= entry * (1 - k * sigma20_entry)         (sigma20_entry = standard deviation of the 20 daily
                                                                   total returns ending at the entry fill close,
                                                                   i.e. k x 20-day stdev x entry price)
The simulators accept ``stops`` = one StopSpec, or a sequence with one StopSpec per simulated session (the decision
at close i uses ``stops[i]``; used by the walk-forward to change the stop each January). ``stops=None`` keeps each
simulator's original behaviour exactly.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StopSpec:
    kind: str                 # none | fixed | trail | vol
    x: float | None = None    # fraction for fixed / trail, k for vol

    def __post_init__(self):
        if self.kind not in ("none", "fixed", "trail", "vol"):
            raise ValueError(self.kind)
        if self.kind != "none" and (self.x is None or not self.x > 0):
            raise ValueError(f"{self.kind} needs x > 0")

    @property
    def label(self) -> str:
        if self.kind == "none":
            return "none"
        if self.kind == "vol":
            return f"vol{self.x:g}"
        return f"{self.kind}{int(round(self.x * 100))}"


NONE = StopSpec("none")


def stop_grid() -> list[StopSpec]:
    """The pre-registered grid (28 settings), in the fixed order used for tie-breaks."""
    g = [NONE]
    g += [StopSpec("fixed", p / 100) for p in range(3, 21)]
    g += [StopSpec("trail", p / 100) for p in range(5, 31, 5)]
    g += [StopSpec("vol", float(k)) for k in (2, 3, 4)]
    return g


def parse(label: str) -> StopSpec:
    if label == "none":
        return NONE
    if label.startswith("vol"):
        return StopSpec("vol", float(label[3:]))
    for kind in ("fixed", "trail"):
        if label.startswith(kind):
            return StopSpec(kind, int(label[len(kind):]) / 100)
    raise ValueError(label)


def triggered(spec: StopSpec, price: float, entry: float, peak: float, sigma_entry: float) -> bool:
    """True when the close ``price`` hits the stop. NaN inputs never trigger."""
    if spec.kind == "none" or not np.isfinite(price):
        return False
    if spec.kind == "fixed":
        return bool(price / entry - 1 <= -spec.x)
    if spec.kind == "trail":
        return bool(price / peak - 1 <= -spec.x)
    if not np.isfinite(sigma_entry):
        return False
    return bool(price / entry - 1 <= -spec.x * sigma_entry)


def schedule(stops, n: int) -> list:
    """``stops`` (a StopSpec or a length-n sequence) -> list of n StopSpecs."""
    if isinstance(stops, StopSpec):
        return [stops] * n
    out = list(stops)
    if len(out) != n:
        raise ValueError(f"stop schedule has {len(out)} entries for {n} sessions")
    return out


def sigma20(idx: pd.DataFrame) -> pd.DataFrame:
    """Rolling 20-session standard deviation of daily returns of a total-return index (causal: uses closes <= t)."""
    return idx.pct_change(fill_method=None).rolling(20, min_periods=20).std()
