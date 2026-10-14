"""Combining near-miss signals (docs/research_ledger_signal_combo.md, section 0): component runners on their frozen
engines at any dollar size, the fixed-weight rules (equal risk with a tracking-error target, equal notional), the
"book" (a $10k account holding an ONEQ base plus every component at its real size, with the overlays' financing and
a daily maintenance-margin check), its signal-off twin, and the fold statistics.

Engines reused unchanged: quant.strategies.megacap.simulate (stock sleeves: SP top-10, IDX X10 / B100),
quant.strategies.short_overlay.simulate (MN short overlays: S-MISP, S-PROF, S-ML), quant.strategies.t_grid.exact_run
(the QQQ RSI2-dip configuration) and quant.strategies.selective_t.simulate (S3-Yb on 18 stocks); the two T engines
take the account size as ``equity``. Every study-specific number (sizes, caps, folds, rates) is a parameter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quant.evaluation.metrics import max_drawdown
from quant.evaluation.stats import t_of_mean
from quant.strategies import fundamentals as fs
from quant.strategies import index_exclusion as ix
from quant.strategies import megacap as mc
from quant.strategies import selective_t as st
from quant.strategies import short_overlay as so
from quant.strategies import t_grid as tg

MN_PARTS = ("MISP", "PROF", "ML")       # short overlays (margin account only)
T_PARTS = ("RSI2", "S3Yb")              # T-trade overlays (margin-financed, or from a cash reserve in the cash book)
SLEEVES = ("SP", "IDX")                 # long-only stock sleeves that replace part of the ONEQ base


@dataclass(frozen=True)
class Fold:
    name: str
    signal: str        # first signal date (month end)
    trade: str         # first trade session (the close where every part is set up)
    end: str           # last session


def fold_index(sessions: pd.DatetimeIndex, fold: Fold) -> pd.DatetimeIndex:
    """Sessions from the trade session to the fold end (the book's daily index)."""
    return sessions[(sessions >= pd.Timestamp(fold.trade)) & (sessions <= pd.Timestamp(fold.end))]


def first_return_day(sessions: pd.DatetimeIndex, fold: Fold) -> pd.Timestamp:
    i = int(sessions.searchsorted(pd.Timestamp(fold.trade)))
    if sessions[i] != pd.Timestamp(fold.trade):
        raise ValueError(f"{fold.trade} is not a session")
    return sessions[i + 1]


def month_returns(level: pd.Series) -> pd.Series:
    """Calendar-month returns; the first month from the first value (short_overlay.month_returns)."""
    return so.month_returns(level)


def level_of(returns: pd.Series, first_day: pd.Timestamp, base: float = 1.0) -> pd.Series:
    """A value series from daily returns, with ``base`` on ``first_day`` (the day before the first return)."""
    v = base * (1 + returns.fillna(0.0)).cumprod()
    return pd.concat([pd.Series([base], index=[first_day]), v])


# ======================================================================== panel-side parts (data v2 U300 panel)

@dataclass
class PanelInputs:
    data: object               # quant.data.panel.WinData
    panel: pd.DataFrame
    scores: pd.DataFrame
    mk: so.Market
    oneq_lvl: pd.Series
    oneq_px: pd.Series
    qqq_lvl: pd.Series


def load_panel(cache, pred_path, effr_path) -> PanelInputs:
    """short_overlay.scored_panel + the EFFR + the short engine's market (as short_overlay.load, keeping ONEQ's
    price for the buy order)."""
    data, sigs, panel, oneq_lvl, oneq_px, end, qtr, qqq_lvl, scores = so.scored_panel(cache, pred_path)
    effr = so.load_effr(effr_path)
    effr = effr[effr.index <= pd.Timestamp(end)]
    mk = so.build_market(data, list(scores["security_id"].unique()), effr, qtr)
    if pd.Series(data.sessions).max() > pd.Timestamp(end):
        raise RuntimeError("sessions beyond the panel end")
    return PanelInputs(data, panel, scores, mk, oneq_lvl, oneq_px, qqq_lvl)


def _window_targets(targets: dict, fold: Fold) -> dict:
    a, b = pd.Timestamp(fold.signal), pd.Timestamp(fold.end)
    return {s: v for s, v in targets.items() if a <= s < b}


def stock_sleeve(pi: PanelInputs, targets: dict, fold: Fold, account: float, band: float) -> dict:
    """megacap.simulate of ``targets`` restricted to the fold (fresh ``account`` at the first execution)."""
    tgt = _window_targets(targets, fold)
    sids = sorted({sid for lst in tgt.values() for sid, *_ in lst})
    d = pi.data
    sim = mc.simulate(tgt, d.sessions, d.sig_idx[sids], d.close[sids], d.last_row, pi.qqq_lvl, d.qqq_close,
                      account=account, band=band, end=fold.end)
    if sim["start"] != pd.Timestamp(fold.trade):
        raise ValueError(f"sleeve starts {sim['start'].date()}, fold trades {fold.trade}")
    return sim


def sp_targets(pi: PanelInputs, col: str, n: int) -> dict:
    """The fundamentals study's top-n equal-weight targets of factor ``col`` (higher = better)."""
    return fs.top_targets(pi.panel, col, n, {col: 1})


def idx_targets(pi: PanelInputs, signal: str | None, variant: str, topn: int, lam: float) -> dict:
    """index_exclusion targets on the B100 members (``signal`` None / variant 'BASE' = the replica itself)."""
    members = ix.base_members(pi.scores, "B100", topn)
    return ix.make_targets(members, signal, variant, lam)


def mn_overlay(pi: PanelInputs, signal: str, n: int, k: float, account: float, fold: Fold) -> dict:
    """short_overlay MN account (QQQ (1 + k), short k) over the fold; returns the engine result and the 100% QQQ
    buy-and-hold of the same account (the matched base)."""
    sched = so.schedule(pi.mk, pi.scores, signal, n)
    start, end, entries = so.fold_bounds(pi.mk, sched, fold.trade, fold.end)
    if pi.mk.sessions[start] != pd.Timestamp(fold.trade):
        raise ValueError(f"{signal}: first trade {pi.mk.sessions[start].date()} != {fold.trade}")
    res = so.simulate(pi.mk, entries, so.Cfg(signal=signal, n=n, k=k, mode="MN", account=account), start, end)
    nav = res["nav"]
    qqq_hold = mc.buy_hold(pi.qqq_lvl, pi.data.qqq_close, nav.index, mc.QQQ_HS, account)
    return {"res": res, "nav": nav, "qqq_hold": qqq_hold}


# ======================================================================== T-trade parts (Yahoo daily data)

def rsi2_run(tdata: dict, config_id: int, reserve_code: int, first_ret: pd.Timestamp, end: str,
             equity: float) -> dict:
    """t_grid exact run of one QQQ configuration with fresh ``equity`` (base bought at the close before
    ``first_ret``): configuration and H_base value series, their stock exposures, the H_match returns."""
    ex = tg.exact_run(tdata, "QQQ", [config_id], str(first_ret.date()), end, tg.CostModel(), equity=equity)
    sess = ex["sessions"]
    prev = tdata["master"][int(tdata["master"].searchsorted(sess[0])) - 1]
    rec = ex["sim"]["rec"]
    r_cfg = ex["R"][config_id]
    r_hb = tg.hbase_of(ex, reserve_code)
    x_cfg = pd.Series(rec["x"][:, 0, 0], index=sess)
    x_hb = pd.Series(rec["x"][:, 0, 1 + reserve_code], index=sess)
    v_cfg, v_hb = level_of(r_cfg, prev, equity), level_of(r_hb, prev, equity)
    trades = int(ex["sim"]["acc"]["nT"][:, 0].sum())
    return {"v_cfg": v_cfg, "v_hb": v_hb, "x_cfg": x_cfg, "x_hb": x_hb, "r_cfg": r_cfg,
            "r_hmatch": tg.hmatch_series(ex, 0), "trades": trades}


def s3_run(markets: dict, config: str, first_ret: pd.Timestamp, end: str, equity_each: float) -> dict:
    """selective_t cash accounts of ``config`` (fresh ``equity_each`` per stock) and their H_base twins: summed
    dollar values, the extra (T-trade) long value per close, the basket and basket-H_match daily returns."""
    spec = st.variant_spec(st.fams_of(config, "stock"), "net", st.S2_SLIP["stock"])
    hb_spec = st.ref_specs("net")["hbase"]
    v_cfg, v_hb, extra, r_cfg, r_hm = [], [], [], [], []
    n_trips, zero_q = 0, 0
    for sym, m in markets.items():
        sess = m.s.sessions
        s = int(sess.searchsorted(first_ret))
        if sess[s] != first_ret:
            raise ValueError(f"{sym}: no session {first_ret.date()}")
        e = int(sess.searchsorted(pd.Timestamp(end), side="right")) - 1
        sim = st.simulate(m, s, e, spec, None, equity=equity_each)
        hb = st.simulate(m, s, e, hb_spec, None, equity=equity_each)
        v_cfg.append(sim["value"])
        v_hb.append(hb["value"])
        r_cfg.append(sim["ret"])
        r_hm.append(st.match_returns(m, s, e, sim["exposure"]))
        x = pd.Series(0.0, index=sim["value"].index)
        close = pd.Series(m.s.close[s - 1: e + 1], index=sim["value"].index)
        for tr in sim["trips"].itertuples():
            n_trips += 1
            held = (x.index >= tr.entry) & (x.index < tr.exit)
            x[held] += tr.q * close[held]
        extra.append(x)
        b = math.floor(st.BASE_FRAC * (equity_each - st.BUFFER) / (m.s.close[s - 1] * (1 + st.hs_of(sym, m.s.close[s - 1]))))
        zero_q += int(b // 3 == 0)
    tot = lambda xs: pd.concat(xs, axis=1).sum(axis=1)  # noqa: E731
    return {"v_cfg": tot(v_cfg), "v_hb": tot(v_hb), "extra": tot(extra), "r_basket": st.basket_mean(r_cfg),
            "r_hmatch": st.basket_mean(r_hm), "trips": n_trips, "stocks_with_zero_q_at_start": zero_q,
            "n_stocks": len(markets)}


# ======================================================================== weights (section 0.5)

def regt_initial(h: dict, mn_parts=MN_PARTS, t_parts=T_PARTS) -> float:
    """Worst-case Reg T initial requirement / equity: 0.5 x (gross long + short) with the ONEQ base and sleeves
    (1 in total), the MN overlays' extra QQQ and every T overlay fully added."""
    mn = sum(h.get(p, 0.0) for p in mn_parts)
    tt = sum(h.get(p, 0.0) for p in t_parts)
    return 0.5 * ((1.0 + mn + tt) + mn)


def equal_risk_weights(sigma: pd.Series, corr: pd.DataFrame, te_target: float, caps: dict,
                       regt_cap: float = 0.95, mn_parts=MN_PARTS, t_parts=T_PARTS) -> tuple[dict, dict]:
    """h_i = c / sigma_i with c = te_target / sqrt(sum_ij rho_ij); then the caps (no redistribution); then the
    levered parts (MN + T) scaled down together until the worst-case Reg T requirement is ``regt_cap``.
    Returns (h, info with c, ex-ante TE before / after the caps)."""
    names = list(sigma.index)
    rho = corr.loc[names, names].to_numpy(float)
    c = te_target / math.sqrt(float(rho.sum()))
    raw = {n: c / float(sigma[n]) for n in names}
    h = {n: min(raw[n], caps.get(n, np.inf)) for n in names}
    scale = 1.0
    req = regt_initial(h, mn_parts, t_parts)
    if req > regt_cap:
        mn = sum(h.get(p, 0.0) for p in mn_parts)
        tt = sum(h.get(p, 0.0) for p in t_parts)
        # 0.5 (1 + s (2 mn + tt)) = cap
        scale = (2 * regt_cap - 1) / (2 * mn + tt)
        for p in (*mn_parts, *t_parts):
            if p in h:
                h[p] *= scale
    cov = np.outer(sigma.to_numpy(float), sigma.to_numpy(float)) * rho
    hv = np.array([h[n] for n in names])
    rv = np.array([raw[n] for n in names])
    info = {"c": c, "te_ex_ante_uncapped": float(math.sqrt(rv @ cov @ rv)), "te_ex_ante": float(math.sqrt(hv @ cov @ hv)),
            "regt_scale": scale, "regt_initial": regt_initial(h, mn_parts, t_parts),
            "raw": raw, "capped": [n for n in names if raw[n] > caps.get(n, np.inf)]}
    return h, info


# ======================================================================== the book (section 0.6)

def financing(extra: pd.Series, effr: pd.Series, spread: float, daycount: float = 360.0) -> pd.Series:
    """Cumulative margin interest on a margin-financed long value: day t pays max(extra_{t-1}, 0) x (EFFR_{t-1} +
    spread) x calendar days / daycount."""
    e = extra.fillna(0.0).clip(lower=0.0)
    r = effr.reindex(extra.index).ffill().fillna(0.0)
    days = pd.Series(extra.index, index=extra.index).diff().dt.days.fillna(0.0)
    pay = (e.shift(1).fillna(0.0) * (r.shift(1).fillna(0.0) + spread) * days / daycount)
    return pay.cumsum()


def maintenance_breaches(nav: pd.Series, long_mv: pd.Series, short_mv: pd.Series, long_req: float = 0.25,
                         short_req: float = 0.30) -> pd.Series:
    """Days on which the equity is below long_req x long value + short_req x short value."""
    need = long_req * long_mv + short_req * short_mv
    return nav < need


@dataclass
class Part:
    name: str
    value: pd.Series                    # dollars the part adds to the book (sleeve value or overlay P&L)
    twin: pd.Series                     # dollars it adds to the signal-off twin
    long_mv: pd.Series                  # long market value it adds (margin check)
    short_mv: pd.Series
    info: dict = field(default_factory=dict)


def _on(index: pd.DatetimeIndex, s: pd.Series, name: str) -> pd.Series:
    out = s.reindex(index)
    if out.isna().any():
        miss = out.index[out.isna()]
        raise ValueError(f"{name}: {len(miss)} book sessions missing (first {miss[0].date()})")
    return out


def assemble(index: pd.DatetimeIndex, parts: list, cash: float = 0.0) -> dict:
    """Book and twin dollar values = the sum of the parts (+ idle ``cash``), with the margin series."""
    z = pd.Series(0.0, index=index)
    book, twin, lng, sht = z + cash, z + cash, z.copy(), z.copy()
    for p in parts:
        book = book + _on(index, p.value, p.name)
        twin = twin + _on(index, p.twin, p.name)
        lng = lng + _on(index, p.long_mv, p.name)
        sht = sht + _on(index, p.short_mv, p.name)
    return {"book": book, "twin": twin, "long": lng, "short": sht,
            "breach": maintenance_breaches(book, lng, sht)}


# ======================================================================== fold statistics (section 0.7)

def fold_stats(book: pd.Series, oneq: pd.Series, twin: pd.Series) -> tuple[dict, pd.Series, pd.Series]:
    """CAGR, drawdown, monthly excess vs ONEQ and vs the twin; A / B (short_overlay.fold_criteria).
    Returns (metrics, monthly excess vs ONEQ, monthly excess vs twin)."""
    years = (book.index[-1] - book.index[0]).days / 365.25
    cg = lambda v: (v.iloc[-1] / v.iloc[0]) ** (1 / years) - 1 if v.iloc[-1] > 0 else -1.0  # noqa: E731
    mb, mo, mt = month_returns(book), month_returns(oneq), month_returns(twin)
    ex, ext = mb - mo, mb - mt
    dd, odd = max_drawdown(book.where(book > 0).ffill().fillna(0)), max_drawdown(oneq)
    r, ro = book.pct_change().iloc[1:], oneq.pct_change().iloc[1:]
    m = {"start": str(book.index[0].date()), "end": str(book.index[-1].date()), "years": round(years, 2),
         "cagr": cg(book), "oneq_cagr": cg(oneq), "twin_cagr": cg(twin), "excess_vs_oneq": cg(book) - cg(oneq),
         "t_ex_oneq": t_of_mean(ex), "ex_oneq_ann": float(ex.mean() * 12),
         "te_vs_oneq": float((r - ro).std(ddof=1) * math.sqrt(252)),
         "ex_twin_ann": float(ext.mean() * 12), "t_ex_twin": t_of_mean(ext),
         "max_dd": dd, "oneq_max_dd": odd, "dd_shallower_pp": (abs(odd) - abs(dd)) * 100,
         "beta_vs_oneq": float(np.cov(r, ro, ddof=1)[0, 1] / ro.var(ddof=1)), "months": int(len(ex)),
         "final_value": float(book.iloc[-1])}
    m.update(so.fold_criteria(m))
    return m, ex, ext


def moments(x: pd.Series) -> dict:
    """Per-period Sharpe (mean / sd), skewness and raw (non-excess) kurtosis of a return series."""
    x = pd.Series(x).dropna().to_numpy(float)
    mu, sd = x.mean(), x.std(ddof=1)
    z = (x - mu) / x.std(ddof=0)
    return {"sr": float(mu / sd), "skew": float((z ** 3).mean()), "kurt": float((z ** 4).mean()), "n": int(len(x))}
