"""Machine-learning cross-sectional return prediction, Gu-Kelly-Xiu (2020) style
(pre-registered in docs/research_ledger_ml_cross_section.md, section 0).

Monthly features at each month end (13 price features, the 40 point-in-time fundamental factors of
research_fundamentals, FF12 industry dummies), cross-sectionally rank-normalised in the U300 universe; target = the
next-month total-return rank. Three models (elastic net, histogram gradient-boosted trees, a small MLP ensemble),
implemented in numpy (scikit-learn / LightGBM are not installed in .venv). Expanding-window walk-forward: train on
2012..Y-1 (last 12 months = validation for tuning), predict year Y = 2016..2026, retrain yearly. Long-only Top10 /
Top20 equal weight with a top-2N buffer, $10k IBKR Tiered real-trading simulation (research_megacap.simulate),
judged against ONEQ total return. Frozen data version 2 only.

Usage:
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_ml_cross_section.py --check
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_ml_cross_section.py --smoke
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_ml_cross_section.py --register
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_ml_cross_section.py --run
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")      # the study is defined on frozen data v2 only

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import study_data_version as dv  # noqa: E402



def require_v2():
    if not dv.IS_V2:
        raise SystemExit("research_ml_cross_section is pre-registered on frozen data v2: set REVERSAL_DATA_VERSION=v2")

from scripts import research_canslim_dev as cs  # noqa: E402
from scripts import research_fundamentals as rf  # noqa: E402
from scripts import research_indicators as ind  # noqa: E402
from scripts import research_livermore as lv  # noqa: E402
from scripts import research_megacap as mc  # noqa: E402

OUT = ROOT / "output/research_only/ml_cross_section"
LEDGER = ROOT / "docs/research_ledger_ml_cross_section.md"
FROZEN = OUT / "frozen_prereg.json"
MCACHE = Path("/Users/bytedance/code/quant_stocks/research_cache/ml_cross_section_v2")
PANEL_CACHE = MCACHE / "panel_v1.pkl"

PRICE_START, PERF_START, END = "2011-06-01", "2016-01-01", "2026-08-31"
FIRST_SIGNAL = "2012-01-31"
TEST_YEARS = tuple(range(2016, 2027))
VAL_MONTHS = 12
SEED = 20261009
PERIODS = {"P1 2016-2020": ("2016-01-01", "2020-12-31"),
           "P2 2021-2026-08": ("2021-01-01", "2026-08-31"),
           "Full 2016-2026-08": ("2016-01-01", "2026-08-31")}
JUDGED = ("P1 2016-2020", "P2 2021-2026-08")
FULL = "Full 2016-2026-08"
N_TRIALS = 8
ALPHA_ONE_SIDED = 0.05
BUFFER_MULT = 2
TOP100 = 100

PRICE_FEATURES = ("r_1w", "r_1m", "mom_3m", "mom_6_1", "mom_12_1", "vol_1m", "vol_3m", "max_ret_1m", "beta_1y",
                  "log_dv50", "dist_52wh", "sma50_ratio", "sma200_ratio")
FUND_FEATURES = rf.FACTORS
FF12_IN = ("NoDur", "Durbl", "Manuf", "Enrgy", "Chems", "BusEq", "Telcm", "Utils", "Shops", "Hlth", "Other")
DUMMIES = tuple(f"ff12_{k}" for k in FF12_IN)
CONT_FEATURES = PRICE_FEATURES + FUND_FEATURES
FEATURES = CONT_FEATURES + DUMMIES
assert len(PRICE_FEATURES) == 13 and len(FUND_FEATURES) == 40 and len(FEATURES) == 64

MODELS = ("ENET", "GBRT", "MLP")
ENET_ALPHAS = (1e-2, 1e-3, 1e-4, 1e-5)          # strongest first: ties keep the stronger penalty
ENET_RHOS = (0.9, 0.5, 0.1)
GB = {"bins": 32, "depth": 3, "lr": 0.05, "min_leaf": 100, "lam": 1.0, "subsample": 0.5, "max_trees": 500,
      "check_every": 10}
MLP_CFG = {"hidden": (32, 16), "lr": 1e-3, "batch": 512, "l2": 1e-4, "seeds": 5, "max_epochs": 30}

# ======================================================================== FF12 industries (Ken French definitions)

FF12_RANGES = {
    "NoDur": [(100, 999), (2000, 2399), (2700, 2749), (2770, 2799), (3100, 3199), (3940, 3989)],
    "Durbl": [(2500, 2519), (2590, 2599), (3630, 3659), (3710, 3711), (3714, 3714), (3716, 3716), (3750, 3751),
              (3792, 3792), (3900, 3939), (3990, 3999)],
    "Manuf": [(2520, 2589), (2600, 2699), (2750, 2769), (3000, 3099), (3200, 3569), (3580, 3629), (3700, 3709),
              (3712, 3713), (3715, 3715), (3717, 3749), (3752, 3791), (3793, 3799), (3830, 3839), (3860, 3899)],
    "Enrgy": [(1200, 1399), (2900, 2999)],
    "Chems": [(2800, 2829), (2840, 2899)],
    "BusEq": [(3570, 3579), (3660, 3692), (3694, 3699), (3810, 3829), (7370, 7379)],
    "Telcm": [(4800, 4899)],
    "Utils": [(4900, 4949)],
    "Shops": [(5000, 5999), (7200, 7299), (7600, 7699)],
    "Hlth": [(2830, 2839), (3693, 3693), (3840, 3859), (8000, 8099)],
    "Money": [(6000, 6999)],
}


def ff12(sic) -> str | None:
    """Fama-French 12-industry label of a SIC code (None when the SIC is unknown)."""
    try:
        v = float(sic)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    v = int(v)
    for k, rngs in FF12_RANGES.items():
        if any(a <= v <= b for a, b in rngs):
            return k
    return "Other"


# ======================================================================== price features (section 0.3)

def price_feature_frames(idx: pd.DataFrame, close_adj: pd.DataFrame, vol_adj: pd.DataFrame,
                         mkt_ret: pd.Series) -> dict:
    """Session x security frames of the 13 price features. Every value at session t uses rows <= t only
    (shifts and trailing rolling windows)."""
    r = idx.pct_change(fill_method=None)
    out = {"r_1w": idx / idx.shift(5) - 1, "r_1m": idx / idx.shift(21) - 1, "mom_3m": idx / idx.shift(63) - 1,
           "mom_6_1": idx.shift(21) / idx.shift(126) - 1, "mom_12_1": idx.shift(21) / idx.shift(252) - 1,
           "vol_1m": r.rolling(21, min_periods=15).std(), "vol_3m": r.rolling(63, min_periods=40).std(),
           "max_ret_1m": r.rolling(21, min_periods=15).max()}
    q = pd.DataFrame(np.repeat(mkt_ret.reindex(r.index).to_numpy()[:, None], r.shape[1], axis=1),
                     index=r.index, columns=r.columns).where(r.notna())
    rr = r.where(q.notna())
    w = dict(window=252, min_periods=126)
    cov = (rr * q).rolling(**w).mean() - rr.rolling(**w).mean() * q.rolling(**w).mean()
    var = (q * q).rolling(**w).mean() - q.rolling(**w).mean() ** 2
    out["beta_1y"] = cov / var.where(var > 0)
    dvol = (close_adj * vol_adj).rolling(50, min_periods=20).median()
    out["log_dv50"] = np.log(dvol.where(dvol > 0))
    hi = close_adj.rolling(252, min_periods=126).max()
    out["dist_52wh"] = close_adj / hi - 1
    out["sma50_ratio"] = close_adj / close_adj.rolling(50, min_periods=50).mean() - 1
    out["sma200_ratio"] = close_adj / close_adj.rolling(200, min_periods=200).mean() - 1
    return {k: v.replace([np.inf, -np.inf], np.nan) for k, v in out.items()}


def sample_at(frames: dict, sids, dates) -> pd.DataFrame:
    """Values of each frame at (last session <= date, sid)."""
    return pd.DataFrame({k: mc._value_at(f, sids, dates) for k, f in frames.items()})


def price_features(idx, close_adj, vol_adj, mkt_ret, sids, dates) -> pd.DataFrame:
    return sample_at(price_feature_frames(idx, close_adj, vol_adj, mkt_ret), sids, dates)[list(PRICE_FEATURES)]


# ======================================================================== cross-sectional normalisation

def rank_normalise(values: pd.Series, groups: pd.Series) -> pd.Series:
    """Per group: (rank - 1) / (n - 1) - 0.5 over the non-missing values (average ranks for ties), missing -> 0;
    a group with fewer than 2 values -> 0."""
    v = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
    rk = v.groupby(groups).rank(method="average")
    n = v.notna().groupby(groups).transform("sum")
    out = (rk - 1) / (n - 1) - 0.5
    out = out.where(n >= 2)
    return out.fillna(0.0)


def feature_matrix(panel: pd.DataFrame) -> pd.DataFrame:
    """The 64 model inputs: ranked continuous features (per signal) and FF12 dummies."""
    X = pd.DataFrame(index=panel.index)
    for f in CONT_FEATURES:
        X[f] = rank_normalise(panel[f], panel["s"])
    lab = panel["sic"].map(ff12)
    for k, col in zip(FF12_IN, DUMMIES):
        X[col] = (lab == k).astype(float)
    return X


# ======================================================================== evaluation helper

def monthly_ic(pred, target, month) -> pd.Series:
    """Per month: Spearman correlation of ``pred`` and ``target`` (rows with both finite; >= 10 rows)."""
    d = pd.DataFrame({"p": np.asarray(pred, float), "t": np.asarray(target, float), "m": np.asarray(month)})
    d = d[np.isfinite(d["p"]) & np.isfinite(d["t"])]
    out = {}
    for m, g in d.groupby("m", sort=True):
        if len(g) < 10:
            continue
        a, b = g["p"].rank().to_numpy(), g["t"].rank().to_numpy()
        a, b = a - a.mean(), b - b.mean()
        den = math.sqrt((a * a).sum() * (b * b).sum())
        out[m] = float((a * b).sum() / den) if den > 0 else np.nan
    return pd.Series(out, dtype=float)


def mean_ic(pred, target, month) -> float:
    ic = monthly_ic(pred, target, month).dropna()
    return float(ic.mean()) if len(ic) else -np.inf


# ======================================================================== model 1: elastic net (coordinate descent)

class ElasticNet:
    """min 1/(2n)||y - b0 - Xb||^2 + alpha * (rho ||b||_1 + (1 - rho)/2 ||b||^2), covariance-update coordinate
    descent (same objective as sklearn.linear_model.ElasticNet with l1_ratio = rho)."""

    def __init__(self, alpha: float, rho: float, tol: float = 1e-8, max_iter: int = 5000):
        self.alpha, self.rho, self.tol, self.max_iter = alpha, rho, tol, max_iter

    def fit(self, X: np.ndarray, y: np.ndarray):
        X, y = np.asarray(X, float), np.asarray(y, float)
        n, p = X.shape
        xm, ym = X.mean(0), y.mean()
        Xc, yc = X - xm, y - ym
        G = Xc.T @ Xc / n
        c = Xc.T @ yc / n
        b = np.zeros(p)
        l1, l2 = self.alpha * self.rho, self.alpha * (1 - self.rho)
        for _ in range(self.max_iter):
            delta = 0.0
            for j in range(p):
                den = G[j, j] + l2
                if den <= 0:
                    continue
                rj = c[j] - G[j] @ b + G[j, j] * b[j]
                new = np.sign(rj) * max(abs(rj) - l1, 0.0) / den
                delta = max(delta, abs(new - b[j]))
                b[j] = new
            if delta < self.tol:
                break
        self.coef_, self.intercept_ = b, ym - xm @ b
        return self

    def predict(self, X):
        return np.asarray(X, float) @ self.coef_ + self.intercept_


def fit_enet(Xtr, ytr, Xva, yva, mva, Xall, yall) -> tuple[ElasticNet, dict]:
    best = (-np.inf, None, None)
    for a in ENET_ALPHAS:
        for r in ENET_RHOS:
            m = ElasticNet(a, r).fit(Xtr, ytr)
            ic = mean_ic(m.predict(Xva), yva, mva)
            if ic > best[0] + 1e-12:
                best = (ic, a, r)
    model = ElasticNet(best[1], best[2]).fit(Xall, yall)
    return model, {"alpha": best[1], "rho": best[2], "val_ic": best[0],
                   "n_nonzero": int((np.abs(model.coef_) > 0).sum())}


# ======================================================================== model 2: histogram gradient-boosted trees

def to_bins(X: np.ndarray, bins: int = GB["bins"]) -> np.ndarray:
    """Inputs in [-0.5, 0.5] (ranks; dummies in {0, 1}) -> integer bins 0..bins-1."""
    return np.clip(np.floor((np.asarray(X, float) + 0.5) * bins), 0, bins - 1).astype(np.int16)


class Tree:
    """A depth-limited regression tree on binned inputs, stored as a complete binary tree (feature -1 = leaf)."""

    def __init__(self, depth: int):
        size = 2 ** (depth + 1) - 1
        self.depth = depth
        self.feat = np.full(size, -1, dtype=np.int64)
        self.thr = np.zeros(size, dtype=np.int64)
        self.val = np.zeros(size)

    def predict(self, B: np.ndarray) -> np.ndarray:
        node = np.zeros(len(B), dtype=np.int64)
        rows = np.arange(len(B))
        for _ in range(self.depth):
            f = self.feat[node]
            inner = f >= 0
            if not inner.any():
                break
            go_left = B[rows[inner], f[inner]] <= self.thr[node[inner]]
            node[inner] = np.where(go_left, 2 * node[inner] + 1, 2 * node[inner] + 2)
        return self.val[node]


def grow_tree(B: np.ndarray, g: np.ndarray, depth: int, bins: int, min_leaf: int, lam: float,
              gain_acc: np.ndarray) -> Tree:
    """Fit one squared-loss tree to residuals ``g`` (leaf value = sum g / (n + lam)); best split per node over every
    (feature, bin threshold) with both children >= min_leaf rows. Adds split gains to ``gain_acc``."""
    n, p = B.shape
    t = Tree(depth)
    node = np.zeros(n, dtype=np.int64)
    feat_off = (np.arange(p, dtype=np.int64) * bins)[None, :]
    for d in range(depth + 1):
        first = 2 ** d - 1
        level = np.arange(first, 2 * first + 1)
        local = node - first
        on = (local >= 0) & (local < len(level))
        for k, nd in enumerate(level):          # leaf values of this level (overwritten for split nodes below)
            sel = on & (local == k)
            cnt = sel.sum()
            t.val[nd] = g[sel].sum() / (cnt + lam) if cnt else 0.0
        if d == depth:
            break
        rows = np.nonzero(on)[0]
        if not len(rows):
            break
        nn = len(level)
        keys = (local[rows][:, None] * (p * bins) + feat_off + B[rows].astype(np.int64)).ravel()
        hg = np.bincount(keys, weights=np.repeat(g[rows], p), minlength=nn * p * bins).reshape(nn, p, bins)
        hn = np.bincount(keys, minlength=nn * p * bins).reshape(nn, p, bins).astype(float)
        GL, NL = np.cumsum(hg, 2)[:, :, :-1], np.cumsum(hn, 2)[:, :, :-1]
        Gt, Nt = hg.sum(2)[:, :1, None], hn.sum(2)[:, :1, None]
        GR, NR = Gt - GL, Nt - NL
        gain = GL ** 2 / (NL + lam) + GR ** 2 / (NR + lam) - Gt ** 2 / (Nt + lam)
        gain = np.where((NL >= min_leaf) & (NR >= min_leaf), gain, -np.inf)
        flat = gain.reshape(nn, -1)
        best = flat.argmax(1)
        for k, nd in enumerate(level):
            gk = flat[k, best[k]]
            if not np.isfinite(gk) or gk <= 1e-12:
                continue
            f, th = divmod(int(best[k]), bins - 1)
            t.feat[nd], t.thr[nd] = f, th
            gain_acc[f] += gk
            sel = on & (local == k)
            left = sel & (B[:, f] <= th)
            node[left] = 2 * nd + 1
            node[sel & ~left] = 2 * nd + 2
        # rows of nodes that did not split keep their (leaf) node index, which is below the next level
    return t


class GBRT:
    def __init__(self, n_trees: int, seed: int, **kw):
        self.cfg = {**GB, **kw}
        self.n_trees, self.seed = n_trees, seed

    def fit(self, X, y, X_val=None, on_check=None):
        c = self.cfg
        B = to_bins(X, c["bins"])
        y = np.asarray(y, float)
        rng = np.random.default_rng(self.seed)
        self.base_ = float(y.mean())
        F = np.full(len(y), self.base_)
        Bv = to_bins(X_val, c["bins"]) if X_val is not None else None
        Fv = np.full(len(Bv), self.base_) if Bv is not None else None
        self.trees_, self.gain_ = [], np.zeros(B.shape[1])
        m = max(int(round(len(y) * c["subsample"])), 1)
        for k in range(self.n_trees):
            sub = np.sort(rng.choice(len(y), m, replace=False))
            t = grow_tree(B[sub], y[sub] - F[sub], c["depth"], c["bins"], c["min_leaf"], c["lam"], self.gain_)
            self.trees_.append(t)
            F += c["lr"] * t.predict(B)
            if Bv is not None:
                Fv += c["lr"] * t.predict(Bv)
                if on_check is not None and (k + 1) % c["check_every"] == 0:
                    on_check(k + 1, Fv.copy())
        return self

    def predict(self, X):
        B = to_bins(X, self.cfg["bins"])
        F = np.full(len(B), self.base_)
        for t in self.trees_:
            F += self.cfg["lr"] * t.predict(B)
        return F


def fit_gbrt(Xtr, ytr, Xva, yva, mva, Xall, yall, seed) -> tuple[GBRT, dict]:
    curve = {}
    GBRT(GB["max_trees"], seed).fit(Xtr, ytr, Xva, on_check=lambda k, f: curve.__setitem__(k, mean_ic(f, yva, mva)))
    ks = sorted(curve)
    best_k = ks[int(np.argmax([curve[k] for k in ks]))]       # first maximum = fewest trees on ties
    model = GBRT(best_k, seed).fit(Xall, yall)
    return model, {"n_trees": best_k, "val_ic": curve[best_k], "val_ic_curve": curve}


# ======================================================================== model 3: MLP ensemble (numpy, Adam)

class MLP:
    """Feed-forward net (ReLU hidden layers, linear output), mean squared error + L2 on weights, Adam."""

    def __init__(self, n_in: int, hidden=MLP_CFG["hidden"], seed: int = 0, lr=MLP_CFG["lr"], l2=MLP_CFG["l2"],
                 batch=MLP_CFG["batch"]):
        self.rng = np.random.default_rng(seed)
        sizes = (n_in,) + tuple(hidden) + (1,)
        self.W = [self.rng.normal(0, math.sqrt(2.0 / a), (a, b)) for a, b in zip(sizes[:-1], sizes[1:])]
        self.b = [np.zeros(b) for b in sizes[1:]]
        self.lr, self.l2, self.batch = lr, l2, batch
        self.m = [np.zeros_like(w) for w in self.W + self.b]
        self.v = [np.zeros_like(w) for w in self.W + self.b]
        self.t = 0

    def forward(self, X):
        hs = [X]
        h = X
        for i, (W, b) in enumerate(zip(self.W, self.b)):
            z = h @ W + b
            h = np.maximum(z, 0) if i < len(self.W) - 1 else z
            hs.append(h)
        return hs

    def predict(self, X):
        return self.forward(np.asarray(X, float))[-1][:, 0]

    def epoch(self, X, y):
        X, y = np.asarray(X, float), np.asarray(y, float)
        order = self.rng.permutation(len(y))
        for a in range(0, len(y), self.batch):
            ix = order[a:a + self.batch]
            hs = self.forward(X[ix])
            grad = (hs[-1][:, 0] - y[ix])[:, None] / len(ix)
            gW, gb = [None] * len(self.W), [None] * len(self.b)
            for i in range(len(self.W) - 1, -1, -1):
                gW[i] = hs[i].T @ grad + self.l2 * self.W[i]
                gb[i] = grad.sum(0)
                if i > 0:
                    grad = (grad @ self.W[i].T) * (hs[i] > 0)
            self.t += 1
            params = self.W + self.b
            for k, (pmt, gr) in enumerate(zip(params, gW + gb)):
                self.m[k] = 0.9 * self.m[k] + 0.1 * gr
                self.v[k] = 0.999 * self.v[k] + 0.001 * gr * gr
                mh = self.m[k] / (1 - 0.9 ** self.t)
                vh = self.v[k] / (1 - 0.999 ** self.t)
                pmt -= self.lr * mh / (np.sqrt(vh) + 1e-8)


class MLPEnsemble:
    def __init__(self, n_in: int, seed: int, n_seeds: int = MLP_CFG["seeds"]):
        self.nets = [MLP(n_in, seed=seed + k) for k in range(n_seeds)]

    def train(self, X, y, epochs: int, X_val=None, on_epoch=None):
        for e in range(1, epochs + 1):
            for net in self.nets:
                net.epoch(X, y)
            if on_epoch is not None:
                on_epoch(e, self.predict(X_val))
        return self

    def predict(self, X):
        return np.mean([n.predict(X) for n in self.nets], axis=0)


def fit_mlp(Xtr, ytr, Xva, yva, mva, Xall, yall, seed) -> tuple[MLPEnsemble, dict]:
    curve = {}
    MLPEnsemble(Xtr.shape[1], seed).train(Xtr, ytr, MLP_CFG["max_epochs"], Xva,
                                          lambda e, f: curve.__setitem__(e, mean_ic(f, yva, mva)))
    es = sorted(curve)
    best_e = es[int(np.argmax([curve[e] for e in es]))]
    model = MLPEnsemble(Xall.shape[1], seed).train(Xall, yall, best_e)
    return model, {"epochs": best_e, "val_ic": curve[best_e], "val_ic_curve": curve}


# ======================================================================== walk-forward split (section 0.4)

def split_for_year(sigs: list, year: int, val_months: int = VAL_MONTHS) -> dict:
    """Signal sets for test year Y: test = Dec Y-1 .. Nov Y month ends; training = signals whose next signal is
    <= s0 (the last signal of Dec Y-1), i.e. labels fully realised by s0; validation = the last ``val_months`` of
    the training signals."""
    sigs = sorted(sigs)
    s0 = [s for s in sigs if s.year == year - 1 and s.month == 12]
    if not s0:
        raise ValueError(f"no December {year - 1} signal")
    s0 = s0[0]
    test = [s for s in sigs if s0 <= s and (s.year == year - 1 or (s.year == year and s.month <= 11))]
    nxt = dict(zip(sigs[:-1], sigs[1:]))
    train_all = [s for s in sigs if s in nxt and nxt[s] <= s0]
    return {"s0": s0, "test": test, "train_all": train_all, "val": train_all[-val_months:],
            "train": train_all[:-val_months]}


# ======================================================================== portfolios (section 0.6)

def buffered_targets(scores: pd.DataFrame, n: int, buffer_mult: int = BUFFER_MULT, weight: float | None = None,
                     extra: list | None = None) -> dict:
    """signal -> [(sid, w, dv50_rank, mcap, src)]: rank by score (desc; ties security id); names of the previous
    target still in the top ``buffer_mult * n`` are kept, the rest filled from the top. Equal weight ``weight``
    (default 1/n); ``extra`` entries are appended (e.g. the ONEQ leg of BLEND). Signals with < n names: skipped."""
    w = (1.0 / n) if weight is None else weight
    out, prev = {}, []
    for s, g in scores.dropna(subset=["score"]).groupby("s", sort=True):
        if len(g) < n:
            continue
        g = g.sort_values(["score", "security_id"], ascending=[False, True])
        order = list(g["security_id"])
        top_buf = set(order[:buffer_mult * n])
        keep = [sid for sid in order if sid in top_buf and sid in prev]
        fill = [sid for sid in order if sid not in keep][:n - len(keep)]
        chosen = set(keep + fill)
        gg = g[g["security_id"].isin(chosen)]
        out[s] = [(r.security_id, w, float(r.dv50_rank) if pd.notna(r.dv50_rank) else np.nan,
                   float(r.mcap) if pd.notna(r.mcap) else np.nan, "") for r in gg.itertuples()] + list(extra or [])
        prev = list(chosen)
    return out


def equal_weight_targets(rows: pd.DataFrame) -> dict:
    out = {}
    for s, g in rows.groupby("s", sort=True):
        out[s] = [(r.security_id, 1.0 / len(g), float(r.dv50_rank) if pd.notna(r.dv50_rank) else np.nan,
                   np.nan, "") for r in g.itertuples()]
    return out


def top_by_mcap(panel: pd.DataFrame, k: int = TOP100) -> pd.DataFrame:
    p = panel[panel["mcap"].notna()].sort_values(["s", "mcap", "security_id"], ascending=[True, False, True])
    return p.groupby("s", sort=True).head(k)


# ======================================================================== data

def u300_candidates(data, sigs: list) -> pd.DataFrame:
    """research_fundamentals.candidate_rows restricted to the U300 rows (latest top-300 week <= s)."""
    uni = data.universe
    u3 = uni[uni["dv50_rank"].notna() & uni["security_id"].isin(data.close.columns)].copy()
    weeks = np.array(sorted(u3["week_end"].unique()), dtype="datetime64[ns]")
    by = {w: g for w, g in u3.groupby("week_end")}
    rows = []
    for s in sigs:
        k = weeks.searchsorted(np.datetime64(s), side="right") - 1
        if k < 0:
            continue
        g = by[pd.Timestamp(weeks[k])][["security_id", "ticker", "cik", "dv50_rank", "sic", "multi_class_group"]]
        rows.append(g.assign(in300=True, s=s, universe_week=pd.Timestamp(weeks[k])))
    c = pd.concat(rows, ignore_index=True)
    facts = pd.read_csv(rf.SECFACTS, dtype=str, usecols=["security_id", "cik"]).set_index("security_id")["cik"]
    c["cik"] = c["cik"].fillna(c["security_id"].map(facts)).fillna(c["security_id"].str.split(".").str[0])
    c["cik"] = pd.to_numeric(c["cik"], errors="coerce").astype("Int64")
    px = mc._value_at(data.close, c["security_id"], c["s"])
    lr = pd.to_datetime(c["security_id"].map(data.last_row))
    c["close_s"] = px
    c = c[np.isfinite(px) & (lr.isna() | (lr >= c["s"])).to_numpy()].copy()
    cs.assert_window(c["universe_week"].dropna(), "2012-01-01", END, "universe weeks used")
    c["sic"] = pd.to_numeric(c["sic"], errors="coerce")
    miss = c["sic"].isna() & c["cik"].notna()
    smap = rf.sic_map(c.loc[miss, "cik"].dropna().astype(int).unique())
    c.loc[miss, "sic"] = c.loc[miss, "cik"].map(lambda x: smap.get(int(x), np.nan) if pd.notna(x) else np.nan)
    return c.reset_index(drop=True)


def qqq_total_returns(end: str) -> pd.Series:
    q = pd.read_csv(lv.CACHE / "factors/qqq_joined.csv", dtype={"date": str})
    q = cs.truncate(q, "date", PRICE_START, end).sort_values("date")
    r = (q["close"] + q["dividend"].fillna(0)) / q["close"].shift(1) - 1
    return pd.Series(r.to_numpy(float), index=pd.DatetimeIndex(pd.to_datetime(q["date"])))


def build_panel(refresh: bool = False):
    """Load data v2 and build the U300 monthly panel: fundamentals (point in time), market cap, price features,
    FF12 label, next-month total return. Returns (data, sigs, panel, oneq_lvl, oneq_px)."""
    require_v2()
    rf.PERF_START = PERF_START          # performance index starts 2016 (features use the full history)
    rf.FCACHE = MCACHE                   # derived caches of this study only (fundamentals_v2 cache untouched)
    MCACHE.mkdir(parents=True, exist_ok=True)
    data = rf.load_all()
    print("DATE GUARD:", data.guard["assertion"], flush=True)
    end = data.spec["effective_end"]
    sigs = [s for s in mc.signal_sessions(data.sessions) if s >= pd.Timestamp(FIRST_SIGNAL)]
    oneq_lvl, oneq_px = ind.oneq_on_sessions(data.sessions, PRICE_START, end)
    if PANEL_CACHE.exists() and not refresh:
        panel = pd.read_pickle(PANEL_CACHE)
        if sorted(panel["s"].unique()) == sorted(sigs):
            return data, sigs, panel, oneq_lvl, oneq_px
    cand = u300_candidates(data, sigs)
    ciks = sorted(cand["cik"].dropna().astype(int).unique())
    alias = mc.successor_ciks(data.universe)
    sh = mc.extract_share_facts(sorted(set(ciks) | set(alias)), cache=MCACHE / "sec_share_facts.csv.gz")
    sh = mc.add_predecessor_facts(sh, alias)
    lists = mc.load_company_lists()
    dv50 = (data.close_adj * data.vol_adj).rolling(50, min_periods=20).median()
    saved = mc.SHARES_VS_FLOAT
    mc.SHARES_VS_FLOAT = np.inf          # fundamentals data rule 1.1a
    try:
        capped = mc.market_caps(cand[["s", "security_id", "ticker", "cik", "dv50_rank", "universe_week"]], sh, lists,
                                data.close, data.sig_idx, dv50)
    finally:
        mc.SHARES_VS_FLOAT = saved
    cand["mcap"] = capped["mcap"].to_numpy()
    cand["mcap_src"] = capped["mcap_src"].to_numpy()
    cand.loc[cand["mcap_src"].astype(str).str.startswith("dollar_volume"), "mcap"] = np.nan
    cand["dv50"] = mc._value_at(dv50, cand["security_id"], cand["s"])
    cand = rf.reject_small_mcaps(cand)
    annual, eps = rf.extract_all(ciks)
    annual = annual[annual["filed"].astype(str) <= end]
    eps = eps[eps["avail"].astype(str) <= end]
    states = rf.build_states(annual)
    states = states[states["filed"].astype(str) <= end]
    cs.assert_window(states["filed"], None, end, "fundamental filings used")
    rows = rf.universes(cand)["U300"]
    m = rf.asof_states(rows, states, eps)
    m = rf.market_factors(m)
    m = m.sort_values(["s", "security_id"]).reset_index(drop=True)
    m = pd.concat([m, rf.composites(m)], axis=1)
    # ---- price features (only the securities that ever enter the panel)
    ids = sorted(m["security_id"].unique())
    pf = price_features(data.sig_idx[ids], data.close_adj[ids], data.vol_adj[ids], qqq_total_returns(end),
                        m["security_id"], m["s"])
    for f in PRICE_FEATURES:
        m[f] = pf[f].to_numpy()
    # ---- next-month total return (label; terminal values included through the index)
    nxt = dict(zip(sigs[:-1], sigs[1:]))
    m["s_next"] = m["s"].map(nxt)
    i0 = mc._value_at(data.sig_idx, m["security_id"], m["s"])
    i1 = mc._value_at(data.sig_idx, m["security_id"], m["s_next"].fillna(m["s"]))
    m["fwd_ret"] = np.where(m["s_next"].notna(), i1 / i0 - 1, np.nan)
    m["ff12"] = m["sic"].map(ff12)
    keep = ["s", "s_next", "security_id", "ticker", "cik", "sic", "ff12", "dv50_rank", "mcap", "mcap_src",
            "fwd_ret", "filed", "fy0_end", "eps_avail"] + list(CONT_FEATURES) + list(rf.COMPOSITES)
    panel = m[keep].copy()
    panel.to_pickle(PANEL_CACHE)
    return data, sigs, panel, oneq_lvl, oneq_px


def assert_point_in_time(panel: pd.DataFrame) -> dict:
    ok_f = panel["filed"].notna()
    if not (pd.to_datetime(panel.loc[ok_f, "filed"]) < panel.loc[ok_f, "s"]).all():
        raise cs.DateGuardError("annual fact used on or before its filing date")
    ok_e = panel["eps_avail"].notna()
    if not (pd.to_datetime(panel.loc[ok_e, "eps_avail"]) < panel.loc[ok_e, "s"]).all():
        raise cs.DateGuardError("quarterly EPS used on or before its availability date")
    ok_n = panel["s_next"].notna()
    if not (panel.loc[ok_n, "s_next"] > panel.loc[ok_n, "s"]).all():
        raise cs.DateGuardError("label ends before its signal")
    return {"filed_lt_s_rows": int(ok_f.sum()), "eps_avail_lt_s_rows": int(ok_e.sum()),
            "max_signal": str(panel["s"].max().date()), "max_label_end": str(panel["s_next"].max().date())}


# ======================================================================== the walk-forward run

def walk_forward(panel: pd.DataFrame, sigs: list, years=TEST_YEARS, shuffle_target: bool = False,
                 log=print) -> tuple[pd.DataFrame, list, dict]:
    """Yearly retraining. Returns (predictions frame for the test rows, hyperparameter rows, importance dict)."""
    X = feature_matrix(panel).to_numpy(float)
    y_raw = panel["fwd_ret"].to_numpy(float)
    if shuffle_target:                       # --smoke: labels permuted within each month (no information)
        rng = np.random.default_rng(SEED)
        y_raw = y_raw.copy()
        for _, ix in panel.groupby("s").indices.items():
            y_raw[ix] = rng.permutation(y_raw[ix])
    y = rank_normalise(pd.Series(y_raw, index=panel.index), panel["s"]).to_numpy()
    y = np.where(np.isfinite(y_raw), y, np.nan)
    s_arr = panel["s"].to_numpy()
    month = panel["s"].dt.strftime("%Y-%m").to_numpy()
    preds, hyper = [], []
    imp = {"ENET_coef": [], "GBRT_gain": [], "perm": {m: [] for m in MODELS}}
    for Y in years:
        sp = split_for_year(sigs, Y)
        tr_m = np.isin(s_arr, np.array(sp["train"], dtype="datetime64[ns]")) & np.isfinite(y)
        va_m = np.isin(s_arr, np.array(sp["val"], dtype="datetime64[ns]")) & np.isfinite(y)
        all_m = tr_m | va_m
        te_m = np.isin(s_arr, np.array(sp["test"], dtype="datetime64[ns]"))
        # ---- no-look-ahead guards
        if pd.Timestamp(panel.loc[all_m, "s_next"].max()) > sp["s0"]:
            raise cs.DateGuardError(f"{Y}: a training label ends after the first test signal")
        if pd.Timestamp(panel.loc[te_m, "s"].min()) < sp["s0"]:
            raise cs.DateGuardError(f"{Y}: test rows before s0")
        args = (X[tr_m], y[tr_m], X[va_m], y[va_m], month[va_m], X[all_m], y[all_m])
        t0 = time.time()
        models = {}
        models["ENET"], h_e = fit_enet(*args)
        models["GBRT"], h_g = fit_gbrt(*args, seed=SEED + Y)
        models["MLP"], h_m = fit_mlp(*args, seed=SEED + Y)
        hyper.append({"year": Y, "s0": str(sp["s0"].date()), "train_signals": len(sp["train"]),
                      "val_signals": len(sp["val"]), "train_rows": int(tr_m.sum()), "val_rows": int(va_m.sum()),
                      "test_rows": int(te_m.sum()), "enet_alpha": h_e["alpha"], "enet_rho": h_e["rho"],
                      "enet_nonzero": h_e["n_nonzero"], "enet_val_ic": h_e["val_ic"], "gbrt_trees": h_g["n_trees"],
                      "gbrt_val_ic": h_g["val_ic"], "mlp_epochs": h_m["epochs"], "mlp_val_ic": h_m["val_ic"]})
        Xte = X[te_m]
        pr = panel.loc[te_m, ["s", "security_id", "ticker", "dv50_rank", "mcap", "fwd_ret"]].copy()
        pr["year"] = Y
        for k, mdl in models.items():
            pr[k] = mdl.predict(Xte)
        preds.append(pr)
        imp["ENET_coef"].append(models["ENET"].coef_)
        imp["GBRT_gain"].append(models["GBRT"].gain_ / max(models["GBRT"].gain_.sum(), 1e-300))
        # ---- permutation importance on the test year (diagnostic): IC drop when a feature is shuffled per month
        yt, mt = y_raw[te_m], month[te_m]
        rng = np.random.default_rng(SEED + 7 * Y)
        groups = list(pd.Series(np.arange(te_m.sum())).groupby(mt).indices.values())
        for k, mdl in models.items():
            base = mean_ic(pr[k].to_numpy(), yt, mt)
            drops = np.zeros(len(FEATURES))
            for j in range(len(FEATURES)):
                Xp = Xte.copy()
                for ix in groups:
                    Xp[ix, j] = Xp[rng.permutation(ix), j]
                drops[j] = base - mean_ic(mdl.predict(Xp), yt, mt)
            imp["perm"][k].append(drops)
        log(f"  {Y}: train {tr_m.sum()} val {va_m.sum()} test {te_m.sum()} | ENET a={h_e['alpha']:g} r={h_e['rho']} "
            f"| GBRT {h_g['n_trees']} trees | MLP {h_m['epochs']} epochs | {time.time() - t0:.0f}s")
    return pd.concat(preds, ignore_index=True), hyper, imp


# ======================================================================== diagnostics

def ic_table(pred: pd.DataFrame, cols) -> pd.DataFrame:
    rows = []
    for c in cols:
        ic = monthly_ic(pred[c], pred["fwd_ret"], pred["s"].dt.strftime("%Y-%m"))
        yr = pd.Series(ic.index.str[:4].astype(int), index=ic.index)
        # a December signal belongs to the next test year
        mo = pd.Series(ic.index.str[5:7].astype(int), index=ic.index)
        ty = yr + (mo == 12).astype(int)
        for label, x in [(str(y), ic[ty == y]) for y in sorted(ty.unique())] + [("all", ic)]:
            x = x.dropna()
            rows.append({"model": c, "test_year": label, "months": len(x), "mean_ic": float(x.mean()),
                         "t": float(x.mean() / x.std(ddof=1) * math.sqrt(len(x))) if len(x) > 2 else np.nan,
                         "share_positive": float((x > 0).mean())})
    return pd.DataFrame(rows)


def decile_table(pred: pd.DataFrame, cols) -> pd.DataFrame:
    rows = []
    d = pred.dropna(subset=["fwd_ret"])
    for c in cols:
        q = d.groupby("s")[c].transform(lambda v: pd.qcut(v.rank(method="first"), 10, labels=False)) + 1
        m = d.assign(dec=q).groupby(["s", "dec"])["fwd_ret"].mean().unstack()
        spread = m[10] - m[1]
        r = {"model": c, "months": len(m)}
        for k in range(1, 11):
            r[f"D{k}_mean_monthly"] = float(m[k].mean())
        r["D10_minus_D1_mean"] = float(spread.mean())
        r["D10_minus_D1_t"] = float(spread.mean() / spread.std(ddof=1) * math.sqrt(len(spread)))
        r["D10_minus_D1_ann"] = float((1 + spread).prod() ** (12 / len(spread)) - 1)
        r["monotonic_rank_corr"] = float(pd.Series([m[k].mean() for k in range(1, 11)]).rank()
                                         .corr(pd.Series(range(1, 11), dtype=float)))
        rows.append(r)
    return pd.DataFrame(rows)


def importance_table(imp: dict) -> pd.DataFrame:
    t = pd.DataFrame({"feature": FEATURES, "group": ["price"] * 13 + ["fundamental"] * 40 + ["industry"] * 11})
    t["ENET_coef_mean"] = np.mean(imp["ENET_coef"], axis=0)
    t["ENET_years_nonzero"] = (np.abs(np.array(imp["ENET_coef"])) > 0).sum(0)
    t["GBRT_gain_share"] = np.mean(imp["GBRT_gain"], axis=0)
    for k in MODELS:
        t[f"{k}_perm_ic_drop"] = np.mean(imp["perm"][k], axis=0)
    return t


# ======================================================================== statistics and criteria

def bonferroni_t(df: int, n: int = N_TRIALS, alpha: float = ALPHA_ONE_SIDED) -> float:
    lo, hi = 0.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if rf.t_sf(mid, df) > alpha / n:
            lo = mid
        else:
            hi = mid
    return hi


def criteria(per: dict, n_trials: int = N_TRIALS) -> dict:
    h = [per[p] for p in JUDGED]
    full = per[FULL]
    p = rf.t_sf(full["t_monthly_excess_vs_oneq"], full["months"] - 1)
    a = all(x["cagr"] > x["oneq_cagr"] for x in h) and p < ALPHA_ONE_SIDED / n_trials
    b = all(x["dd_shallower_than_oneq_pp"] >= 10.0 and x["cagr"] >= x["oneq_cagr"] - 0.03 for x in h)
    return {"p_one_sided": p, "bonferroni_t": bonferroni_t(full["months"] - 1, n_trials),
            "A_cagr_above_oneq_both_and_bonferroni_t": bool(a),
            "B_dd_10pp_shallower_and_cagr_within_3pp_both": bool(b), "pass": bool(a or b)}


# ======================================================================== portfolio configurations

def configs(pred: pd.DataFrame, panel: pd.DataFrame) -> list:
    """(name, kind, targets, ONEQ-leg flag). kind = 'trial' (8, section 0.7) or 'reference'."""
    out = []
    for mdl in MODELS:
        sc = pred[["s", "security_id", "dv50_rank", "mcap"]].assign(score=pred[mdl])
        for n in (10, 20):
            out.append((f"{mdl}_Top{n}", "trial", buffered_targets(sc, n), False))
    g = pred[["s", "security_id", "dv50_rank", "mcap"]].assign(score=pred["GBRT"])
    out.append(("V100_GBRT_Top10", "trial", buffered_targets(top_by_mcap(g), 10), False))
    out.append(("BLEND_50ONEQ_GBRT_Top10", "trial",
                buffered_targets(g, 10, weight=0.05, extra=[("QQQ", 0.5, np.nan, np.nan, "oneq")]), True))
    test_sigs = set(pred["s"].unique())
    p = panel[panel["s"].isin(test_sigs)]
    for comp in ("C_profitability", "C_ALL"):
        sc = p[["s", "security_id", "dv50_rank", "mcap"]].assign(score=p[comp])
        for n in (10, 20):
            out.append((f"REF_{comp}_Top{n}", "reference", buffered_targets(sc, n), False))
    out.append(("REF_EW_U300", "reference", equal_weight_targets(p), False))
    out.append(("REF_EW_Top100mcap", "reference", equal_weight_targets(top_by_mcap(p)), False))
    return out


def simulate_all(cfgs: list, data, oneq_lvl, oneq_px, log=print) -> tuple[list, list, dict, dict, list]:
    summary, by_year, navs, results, holds = [], [], {}, {}, []
    for name, kind, tg, oneq_leg in cfgs:
        if oneq_leg:
            saved = mc.QQQ_HS
            mc.QQQ_HS = mc.ONEQ_HS          # the 'QQQ' slot carries ONEQ in BLEND
            try:
                sim = mc.simulate(tg, data.sessions, data.perf_idx, data.close, data.last_row, oneq_lvl, oneq_px)
            finally:
                mc.QQQ_HS = saved
        else:
            sim = mc.simulate(tg, data.sessions, data.perf_idx, data.close, data.last_row, data.qqq_perf_idx,
                              data.qqq_close)
        dates = sim["nav"].index
        oneq = mc.buy_hold(oneq_lvl, oneq_px, dates, mc.ONEQ_HS)
        qqq = mc.buy_hold(data.qqq_perf_idx, data.qqq_close, dates, mc.QQQ_HS)
        per = {pn: mc.window_metrics(sim["nav"], oneq, qqq, a, b, sim["cost"], sim["traded"], sim["orders"])
               for pn, (a, b) in PERIODS.items()}
        for pn, m in per.items():
            summary.append({"config": name, "kind": kind, "period": pn, **m})
        crit = criteria(per)
        results[name] = {"kind": kind, "criteria": crit, "start": str(sim["start"].date()),
                         "avg_names_held": float(sim["names"].mean()),
                         "t_full": per[FULL]["t_monthly_excess_vs_oneq"], "months": per[FULL]["months"]}
        navs[name] = sim["nav"]
        if "ONEQ" not in navs:
            navs["ONEQ"], navs["QQQ"] = oneq, qqq
            for pn, (a, b) in PERIODS.items():
                summary.append({"config": "ONEQ buy-hold", "kind": "benchmark", "period": pn,
                                **mc.window_metrics(oneq, oneq, qqq, a, b)})
                summary.append({"config": "QQQ buy-hold", "kind": "benchmark", "period": pn,
                                **mc.window_metrics(qqq, oneq, qqq, a, b)})
        yo, yq = mc._yearly(oneq.pct_change().dropna()), mc._yearly(qqq.pct_change().dropna())
        for yy, v in mc._yearly(sim["nav"].pct_change().dropna()).items():
            by_year.append({"config": name, "year": yy, "strategy": v, "oneq": yo.get(yy), "qqq": yq.get(yy)})
        if kind == "trial":
            for s, lst in tg.items():
                for sid, w, *_ in lst:
                    holds.append({"config": name, "signal": s.date().isoformat(), "security_id": sid, "weight": w})
        log(f"{name:28s} P1 {per[JUDGED[0]]['cagr']:+.1%} (ONEQ {per[JUDGED[0]]['oneq_cagr']:+.1%}) "
            f"P2 {per[JUDGED[1]]['cagr']:+.1%} (ONEQ {per[JUDGED[1]]['oneq_cagr']:+.1%}) "
            f"t {per[FULL]['t_monthly_excess_vs_oneq']:+.2f} DD {per[FULL]['max_dd']:.0%} pass {crit['pass']}")
    return summary, by_year, navs, results, holds


# ======================================================================== pre-registration hash

def prereg_block(text: str | None = None) -> str:
    t = text if text is not None else LEDGER.read_text()
    a, b = t.index("<!-- PREREG-BEGIN -->"), t.index("<!-- PREREG-END -->")
    return t[a:b]


def prereg_hash(text: str | None = None) -> str:
    return hashlib.sha256(prereg_block(text).encode()).hexdigest()


def register():
    OUT.mkdir(parents=True, exist_ok=True)
    if FROZEN.exists():
        raise SystemExit(f"already registered: {json.loads(FROZEN.read_text())}")
    FROZEN.write_text(json.dumps({"sha256": prereg_hash(), "registered_at": pd.Timestamp.now("UTC").isoformat()},
                                 indent=1) + "\n")
    print("registered", prereg_hash())


# ======================================================================== commands

def coverage(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for y, g in panel.groupby(panel["s"].dt.year):
        r = {"year": int(y), "signals": int(g["s"].nunique()), "avg_names": round(len(g) / g["s"].nunique(), 1),
             "label_share": round(float(g["fwd_ret"].notna().mean()), 3),
             "ff12_known": round(float(g["ff12"].notna().mean()), 3)}
        for f in CONT_FEATURES:
            r[f] = round(float(g[f].notna().mean()), 3)
        rows.append(r)
    return pd.DataFrame(rows)


def check(args):
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, *_ = build_panel(refresh=args.refresh)
    guard = assert_point_in_time(panel)
    cov = coverage(panel)
    cov.to_csv(OUT / "check_coverage.csv", index=False)
    X = feature_matrix(panel)
    rng = X[list(CONT_FEATURES)].agg(["min", "max", "mean"]).T
    assert rng["min"].min() >= -0.5 - 1e-12 and rng["max"].max() <= 0.5 + 1e-12
    pd.set_option("display.width", 250)
    print(json.dumps(guard, indent=1))
    print(cov[["year", "signals", "avg_names", "label_share", "ff12_known", "r_1m", "mom_12_1", "beta_1y",
               "sma200_ratio", "ep", "roe", "q_eps_g", "size", "rd_sales"]].to_string())
    print(panel["ff12"].value_counts(dropna=False).to_string())
    for Y in (2016, 2026):
        sp = split_for_year(sigs, Y)
        print(Y, "s0", sp["s0"].date(), "train", sp["train"][0].date(), "..", sp["train"][-1].date(), "val",
              sp["val"][0].date(), "..", sp["val"][-1].date(), "test", sp["test"][0].date(), "..",
              sp["test"][-1].date())


def smoke(args):
    out = OUT / "smoke"
    out.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, oneq_lvl, oneq_px = build_panel()
    assert_point_in_time(panel)
    years = tuple(int(y) for y in args.years.split(",")) if args.years else (2016, 2017)
    pred, hyper, imp = walk_forward(panel, sigs, years=years, shuffle_target=True)
    ic_table(pred, MODELS).to_csv(out / "ic_shuffled.csv", index=False)
    importance_table(imp)
    decile_table(pred, MODELS)
    cfgs = configs(pred, panel)
    simulate_all(cfgs[:1] + cfgs[7:8], data, oneq_lvl, oneq_px, log=lambda *_: None)
    pd.DataFrame(hyper).to_csv(out / "hyper_shuffled.csv", index=False)
    print("SMOKE OK (labels shuffled within month; results carry no information and are not reported)")


def run(args):
    if not FROZEN.exists():
        raise SystemExit("run --register first")
    if json.loads(FROZEN.read_text())["sha256"] != prereg_hash():
        raise SystemExit("pre-registration text changed since --register")
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, oneq_lvl, oneq_px = build_panel()
    guard = assert_point_in_time(panel)
    pred, hyper, imp = walk_forward(panel, sigs)
    pred.drop(columns=["fwd_ret"]).to_csv(OUT / "predictions.csv.gz", index=False, compression="gzip")
    pd.DataFrame([{k: v for k, v in h.items()} for h in hyper]).to_csv(OUT / "hyperparameters.csv", index=False)
    ic = ic_table(pred, MODELS)
    ic.to_csv(OUT / "ic_by_year.csv", index=False)
    dec = decile_table(pred, MODELS)
    dec.to_csv(OUT / "deciles.csv", index=False)
    impt = importance_table(imp)
    impt.to_csv(OUT / "feature_importance.csv", index=False)
    cfgs = configs(pred, panel)
    summary, by_year, navs, results, holds = simulate_all(cfgs, data, oneq_lvl, oneq_px)
    pd.DataFrame(summary).to_csv(OUT / "summary.csv", index=False)
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    pd.DataFrame(holds).to_csv(OUT / "holdings_by_signal.csv.gz", index=False, compression="gzip")
    pd.DataFrame(navs).to_csv(OUT / "nav_daily.csv.gz", compression="gzip")
    trials = {k: v for k, v in results.items() if v["kind"] == "trial"}
    verdict = any(v["criteria"]["pass"] for v in trials.values())
    out = {"prereg_sha256": prereg_hash(), "guard": data.guard["assertion"], "point_in_time": guard,
           "n_trials": N_TRIALS, "bonferroni_t": bonferroni_t(results[next(iter(trials))]["months"] - 1),
           "nominal_t_ge_2": int(sum(v["t_full"] >= 2 for v in trials.values())),
           "results": results, "verdict_pass": verdict, "first_signal": str(sigs[0].date()),
           "last_signal": str(sigs[-1].date()), "panel_rows": int(len(panel)),
           "terminal_events": int(len(data.terminal_events)),
           "ic_all": ic[ic["test_year"] == "all"].set_index("model")[["mean_ic", "t"]].to_dict("index")}
    (OUT / "results.json").write_text(json.dumps(out, indent=1, default=rf._json) + "\n")
    print(ic[ic["test_year"] == "all"].to_string())
    print(dec[["model", "D1_mean_monthly", "D10_mean_monthly", "D10_minus_D1_mean", "D10_minus_D1_t"]].to_string())
    print("VERDICT:", "PASS" if verdict else "FAIL")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--smoke", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--run", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="rebuild the cached panel (--check)")
    ap.add_argument("--years", default="", help="--smoke only: comma-separated test years")
    a = ap.parse_args(argv)
    if a.register:
        register()
    elif a.check:
        check(a)
    elif a.smoke:
        smoke(a)
    else:
        run(a)


if __name__ == "__main__":
    main()
