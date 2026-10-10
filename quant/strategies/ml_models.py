"""The numpy models and the yearly walk-forward of the machine-learning study (docs/research_ledger_ml_cross_section.md,
section 0): cross-sectional rank normalisation, monthly rank IC, an elastic net (coordinate descent), histogram
gradient-boosted trees, an MLP ensemble (Adam), the expanding-window split, buffered top-N targets and the IC /
decile / importance diagnostics.

Moved unchanged from studies/ml_cross_section.py. The study's frozen hyper-parameters (seed, validation months,
elastic-net grid, tree and MLP settings, feature lists) are passed in as a ``ModelConfig``; nothing here has a
study default.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.strategies import canslim as cs
from quant.strategies.ml_features import ff12


@dataclass(frozen=True)
class ModelConfig:
    seed: int
    val_months: int
    enet_alphas: tuple            # strongest first: ties keep the stronger penalty
    enet_rhos: tuple
    gb: dict                      # bins, depth, lr, min_leaf, lam, subsample, max_trees, check_every
    mlp: dict                     # hidden, lr, batch, l2, seeds, max_epochs
    cont_features: tuple          # ranked continuous inputs
    ff12_in: tuple                # FF12 industries with a dummy
    dummies: tuple                # their column names
    models: tuple = ("ENET", "GBRT", "MLP")

    @property
    def features(self) -> tuple:
        return self.cont_features + self.dummies


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


def feature_matrix(panel: pd.DataFrame, cfg: ModelConfig) -> pd.DataFrame:
    """The model inputs: ranked continuous features (per signal) and FF12 dummies."""
    X = pd.DataFrame(index=panel.index)
    for f in cfg.cont_features:
        X[f] = rank_normalise(panel[f], panel["s"])
    lab = panel["sic"].map(ff12)
    for k, col in zip(cfg.ff12_in, cfg.dummies):
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


def fit_enet(Xtr, ytr, Xva, yva, mva, Xall, yall, alphas, rhos) -> tuple[ElasticNet, dict]:
    best = (-np.inf, None, None)
    for a in alphas:
        for r in rhos:
            m = ElasticNet(a, r).fit(Xtr, ytr)
            ic = mean_ic(m.predict(Xva), yva, mva)
            if ic > best[0] + 1e-12:
                best = (ic, a, r)
    model = ElasticNet(best[1], best[2]).fit(Xall, yall)
    return model, {"alpha": best[1], "rho": best[2], "val_ic": best[0],
                   "n_nonzero": int((np.abs(model.coef_) > 0).sum())}


# ======================================================================== model 2: histogram gradient-boosted trees

def to_bins(X: np.ndarray, bins: int) -> np.ndarray:
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
    def __init__(self, n_trees: int, seed: int, gb: dict, **kw):
        self.cfg = {**gb, **kw}
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


def fit_gbrt(Xtr, ytr, Xva, yva, mva, Xall, yall, seed, gb: dict) -> tuple[GBRT, dict]:
    curve = {}
    GBRT(gb["max_trees"], seed, gb).fit(Xtr, ytr, Xva,
                                        on_check=lambda k, f: curve.__setitem__(k, mean_ic(f, yva, mva)))
    ks = sorted(curve)
    best_k = ks[int(np.argmax([curve[k] for k in ks]))]       # first maximum = fewest trees on ties
    model = GBRT(best_k, seed, gb).fit(Xall, yall)
    return model, {"n_trees": best_k, "val_ic": curve[best_k], "val_ic_curve": curve}


# ======================================================================== model 3: MLP ensemble (numpy, Adam)

class MLP:
    """Feed-forward net (ReLU hidden layers, linear output), mean squared error + L2 on weights, Adam."""

    def __init__(self, n_in: int, hidden, seed: int, lr: float, l2: float, batch: int):
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
    def __init__(self, n_in: int, seed: int, mlp: dict, n_seeds: int | None = None):
        n_seeds = mlp["seeds"] if n_seeds is None else n_seeds
        self.nets = [MLP(n_in, mlp["hidden"], seed + k, mlp["lr"], mlp["l2"], mlp["batch"]) for k in range(n_seeds)]

    def train(self, X, y, epochs: int, X_val=None, on_epoch=None):
        for e in range(1, epochs + 1):
            for net in self.nets:
                net.epoch(X, y)
            if on_epoch is not None:
                on_epoch(e, self.predict(X_val))
        return self

    def predict(self, X):
        return np.mean([n.predict(X) for n in self.nets], axis=0)


def fit_mlp(Xtr, ytr, Xva, yva, mva, Xall, yall, seed, mlp: dict) -> tuple[MLPEnsemble, dict]:
    curve = {}
    MLPEnsemble(Xtr.shape[1], seed, mlp).train(Xtr, ytr, mlp["max_epochs"], Xva,
                                               lambda e, f: curve.__setitem__(e, mean_ic(f, yva, mva)))
    es = sorted(curve)
    best_e = es[int(np.argmax([curve[e] for e in es]))]
    model = MLPEnsemble(Xall.shape[1], seed, mlp).train(Xall, yall, best_e)
    return model, {"epochs": best_e, "val_ic": curve[best_e], "val_ic_curve": curve}


# ======================================================================== walk-forward split (section 0.4)

def split_for_year(sigs: list, year: int, val_months: int) -> dict:
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

def buffered_targets(scores: pd.DataFrame, n: int, buffer_mult: int, weight: float | None = None,
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


def top_by_mcap(panel: pd.DataFrame, k: int) -> pd.DataFrame:
    p = panel[panel["mcap"].notna()].sort_values(["s", "mcap", "security_id"], ascending=[True, False, True])
    return p.groupby("s", sort=True).head(k)


# ======================================================================== data guard

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

def walk_forward(panel: pd.DataFrame, sigs: list, cfg: ModelConfig, years, shuffle_target: bool = False,
                 log=print) -> tuple[pd.DataFrame, list, dict]:
    """Yearly retraining. Returns (predictions frame for the test rows, hyperparameter rows, importance dict)."""
    X = feature_matrix(panel, cfg).to_numpy(float)
    y_raw = panel["fwd_ret"].to_numpy(float)
    if shuffle_target:                       # --smoke: labels permuted within each month (no information)
        rng = np.random.default_rng(cfg.seed)
        y_raw = y_raw.copy()
        for _, ix in panel.groupby("s").indices.items():
            y_raw[ix] = rng.permutation(y_raw[ix])
    y = rank_normalise(pd.Series(y_raw, index=panel.index), panel["s"]).to_numpy()
    y = np.where(np.isfinite(y_raw), y, np.nan)
    s_arr = panel["s"].to_numpy()
    month = panel["s"].dt.strftime("%Y-%m").to_numpy()
    preds, hyper = [], []
    imp = {"ENET_coef": [], "GBRT_gain": [], "perm": {m: [] for m in cfg.models}}
    for Y in years:
        sp = split_for_year(sigs, Y, cfg.val_months)
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
        models["ENET"], h_e = fit_enet(*args, cfg.enet_alphas, cfg.enet_rhos)
        models["GBRT"], h_g = fit_gbrt(*args, seed=cfg.seed + Y, gb=cfg.gb)
        models["MLP"], h_m = fit_mlp(*args, seed=cfg.seed + Y, mlp=cfg.mlp)
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
        rng = np.random.default_rng(cfg.seed + 7 * Y)
        groups = list(pd.Series(np.arange(te_m.sum())).groupby(mt).indices.values())
        for k, mdl in models.items():
            base = mean_ic(pr[k].to_numpy(), yt, mt)
            drops = np.zeros(len(cfg.features))
            for j in range(len(cfg.features)):
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


def importance_table(imp: dict, cfg: ModelConfig, groups: list) -> pd.DataFrame:
    t = pd.DataFrame({"feature": cfg.features, "group": groups})
    t["ENET_coef_mean"] = np.mean(imp["ENET_coef"], axis=0)
    t["ENET_years_nonzero"] = (np.abs(np.array(imp["ENET_coef"])) > 0).sum(0)
    t["GBRT_gain_share"] = np.mean(imp["GBRT_gain"], axis=0)
    for k in cfg.models:
        t[f"{k}_perm_ic_drop"] = np.mean(imp["perm"][k], axis=0)
    return t
