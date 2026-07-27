"""Shared return aggregation and uncertainty helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd


def annual_returns(returns: pd.DataFrame) -> pd.DataFrame:
    """Compound strategy and benchmark returns separately by calendar year."""
    annual = (1 + returns[["strategy", "benchmark"]]).groupby(returns.index.year).prod() - 1
    annual["excess"] = annual["strategy"] - annual["benchmark"]
    annual.index.name = "year"
    return annual


def moving_block_bootstrap(
    active_returns: pd.Series,
    block_length: int = 20,
    samples: int = 2_000,
    seed: int = 20260718,
) -> dict:
    """Estimate active-return uncertainty while preserving short dependence."""
    values = active_returns.dropna().to_numpy(dtype=float)
    if len(values) < block_length:
        raise ValueError("Not enough observations for requested bootstrap block")
    rng = np.random.default_rng(seed)
    starts = np.arange(len(values) - block_length + 1)
    annualized = np.empty(samples)
    blocks_needed = int(np.ceil(len(values) / block_length))
    for sample in range(samples):
        chosen = rng.choice(starts, size=blocks_needed, replace=True)
        draw = np.concatenate(
            [values[start : start + block_length] for start in chosen]
        )[: len(values)]
        annualized[sample] = draw.mean() * 252
    return {
        "annualized_active_mean": float(values.mean() * 252),
        "bootstrap_ci_95_low": float(np.quantile(annualized, 0.025)),
        "bootstrap_ci_95_high": float(np.quantile(annualized, 0.975)),
        "bootstrap_probability_nonpositive": float((annualized <= 0).mean()),
        "bootstrap_block_sessions": block_length,
        "bootstrap_samples": samples,
    }
