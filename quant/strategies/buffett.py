"""Buffett-related rules (docs/research_ledger_buffett.md, section 0): the Berkshire 13F clone targets (B1), the
textbook quality screen on the point-in-time Nasdaq panel (B2), the Frazzini-Kabiller-Pedersen factor replication
and the ETF version (B3), and the A / B evaluation against ONEQ (and, for reporting, QQQ and SPY).

Nothing here reads files or holds study state: the 13F states, price matrices, factor tables and fundamentals come in
as arguments; every threshold is a parameter or a module constant written in the ledger.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant.evaluation.criteria import ab_criteria
from quant.evaluation.metrics import max_drawdown, monthly, yearly
from quant.evaluation.periods import segment_metrics
from quant.strategies import fundamentals as fs

# ======================================================================== B1: Berkshire 13F clone

B1_RULES = ("top10_ew", "all_vw", "top10_vw")      # the first two are the trials B1a / B1b, the third a sensitivity
B1_HALF_SPREAD = 5e-4
DECIMALIZATION = pd.Timestamp("2001-04-09")
TICK_PRE, TICK_POST = 1 / 16, 0.01


def b1_half_spread(price: float, day: pd.Timestamp, base: float = B1_HALF_SPREAD) -> float:
    """5 bp, but at least half a tick over the unadjusted price (1/16 dollar before decimalisation, 1 cent after)."""
    tick = TICK_PRE if day < DECIMALIZATION else TICK_POST
    if not np.isfinite(price) or price <= 0:
        return base
    return max(base, tick / 2 / price)


def b1_weights(positions: pd.DataFrame, priced: set, rule: str) -> tuple[list[tuple[str, float]], dict]:
    """Target weights of one 13F state and its coverage. ``positions``: equity_positions (cusip, value, sorted by
    value). top10_ew: the 10 largest positions of all, the priced ones equal-weighted; all_vw: every priced position
    weighted by reported value; top10_vw: the 10 largest, priced ones weighted by value."""
    tot = float(positions["value"].sum())
    ok = positions["cusip"].isin(priced)
    top = positions.head(10)
    top_ok = top[top["cusip"].isin(priced)]
    cov = {"names": int(len(positions)), "names_priced": int(ok.sum()),
           "value_share_priced": float(positions.loc[ok, "value"].sum() / tot) if tot > 0 else np.nan,
           "top10_priced": int(len(top_ok)),
           "top10_value_share_priced": float(top_ok["value"].sum() / top["value"].sum()) if len(top) else np.nan}
    if rule == "top10_ew":
        w = [(c, 1.0 / len(top_ok)) for c in top_ok["cusip"]] if len(top_ok) else []
    elif rule == "top10_vw":
        s = top_ok["value"].sum()
        w = [(c, v / s) for c, v in zip(top_ok["cusip"], top_ok["value"])] if s > 0 else []
    elif rule == "all_vw":
        p = positions[ok]
        s = p["value"].sum()
        w = [(c, v / s) for c, v in zip(p["cusip"], p["value"])] if s > 0 else []
    else:
        raise ValueError(rule)
    return w, cov


def b1_targets(states: dict, sessions: pd.DatetimeIndex, priced_on, rule: str) -> tuple[dict, pd.DataFrame]:
    """Engine targets {signal session: [(cusip, weight, nan, nan, '')]} for the states {trade session: event}: the
    signal session is the one before the trade session (the engine trades at the next close). ``priced_on(cusip,
    trade_session)`` says whether a CUSIP can be bought that day. Also the coverage table, one row per state."""
    out, cov_rows = {}, []
    for t, e in states.items():
        k = sessions.get_loc(t)
        if k == 0:
            continue
        priced = {c for c in e["positions"]["cusip"] if priced_on(c, t)}
        w, cov = b1_weights(e["positions"], priced, rule)
        out[sessions[k - 1]] = [(c, x, np.nan, np.nan, "") for c, x in w]
        cov_rows.append({"trade_session": str(t.date()), "filing_date": e["filing_date"], "accession": e["accession"],
                         "report_date": e["report_date"], "kind": e["kind"], **cov})
    return out, pd.DataFrame(cov_rows)


# ======================================================================== B2: textbook quality screen

ROE_MIN = 0.15
DE_MAX = 0.5
MAX_DECLINES = 1
YEARS = 5
STATE_COLS = [f"{k}{j}" for j in range(YEARS) for k in ("ni", "be")] + ["debt0"]


def buffett_states(facts: pd.DataFrame) -> pd.DataFrame:
    """One row per filing date of one CIK's annual facts (``fundamentals.extract_all`` rows): FY0 end and NI, BE of
    FY0..FY-4 plus DEBT of FY0, from facts filed up to that date (usable strictly later; the fundamentals
    ledger's 1.2 / 1.3 rules via ``fiscal_years`` / ``year_inputs``)."""
    if facts.empty:
        return pd.DataFrame()
    known: dict = {}
    ends_by_group: dict = {}
    rows = []
    cik = int(facts["cik"].iloc[0])
    for filed, g in facts.sort_values("filed", kind="stable").groupby("filed", sort=True):
        for r in g.itertuples():
            known.setdefault((r.group, r.end), {})[r.prio] = r.val
            if r.kind == "S":
                ends_by_group.setdefault(r.group, set()).add(r.end)
        fys = fs.fiscal_years(known)
        if not fys:
            continue
        row = {"cik": cik, "filed": filed, "fy0_end": fys[0]}
        for j, e in enumerate(fys[:YEARS]):
            y = fs.year_inputs(known, e, ends_by_group) if e is not None else None
            row[f"ni{j}"] = None if y is None else y["ni"]
            row[f"be{j}"] = None if y is None else y["be_any"]
            if j == 0:
                row["debt0"] = None if y is None else y["debt"]
        rows.append(row)
    return pd.DataFrame(rows)


def asof_buffett(rows: pd.DataFrame, states: pd.DataFrame, max_fy_age: int = fs.MAX_FY_AGE) -> pd.DataFrame:
    """For each (s, cik) row the latest state filed strictly before s; states whose FY0 ended more than
    ``max_fy_age`` days before s are dropped (all their columns empty). Asserted point in time."""
    left = rows.copy()
    left["cik_i"] = pd.to_numeric(left["cik"], errors="coerce")
    left = left[left["cik_i"].notna()].copy()
    left["cik_i"] = left["cik_i"].astype("int64")
    left["s"] = pd.to_datetime(left["s"]).astype("datetime64[ns]")
    st = states.copy()
    st["usable_from"] = (pd.to_datetime(st["filed"]) + pd.Timedelta(days=1)).astype("datetime64[ns]")
    st["cik_i"] = st["cik"].astype("int64")
    st = st.drop(columns=["cik"]).sort_values("usable_from")
    m = pd.merge_asof(left.sort_values("s"), st, left_on="s", right_on="usable_from", by="cik_i", direction="backward")
    used = m["filed"].notna()
    if not (pd.to_datetime(m.loc[used, "filed"]) < m.loc[used, "s"]).all():
        raise AssertionError("annual fact used on or before its filing date")
    stale = ~((m["s"] - pd.to_datetime(m["fy0_end"])).dt.days <= max_fy_age)
    m.loc[stale, STATE_COLS] = np.nan
    return m


def qualifies(m: pd.DataFrame) -> pd.DataFrame:
    """Screen columns: roe_min (lowest ROE of FY0..FY-4), de, declines, and the three tests q_roe, q_de, q_growth."""
    ni = m[[f"ni{j}" for j in range(YEARS)]].astype(float).to_numpy()
    be = m[[f"be{j}" for j in range(YEARS)]].astype(float).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        roe = np.where(be > 0, ni / be, np.nan)
    complete = np.isfinite(ni).all(axis=1) & np.isfinite(be).all(axis=1) & (be > 0).all(axis=1)
    out = pd.DataFrame(index=m.index)
    out["roe_min"] = np.where(complete, np.nanmin(np.where(np.isfinite(roe), roe, np.inf), axis=1), np.nan)
    debt = pd.to_numeric(m["debt0"], errors="coerce").fillna(0.0).to_numpy()
    out["de"] = np.where(be[:, 0] > 0, debt / be[:, 0], np.nan)
    declines = (ni[:, :-1] < ni[:, 1:]).sum(axis=1)      # FY-1 -> FY0, ..., FY-4 -> FY-3
    out["declines"] = np.where(complete, declines, np.nan)
    out["q_roe"] = complete & (np.nan_to_num(roe, nan=-np.inf) > ROE_MIN).all(axis=1)
    out["q_de"] = np.isfinite(out["de"]) & (out["de"] < DE_MAX)
    out["q_growth"] = complete & (ni > 0).all(axis=1) & (ni[:, 0] > ni[:, -1]) & (declines <= MAX_DECLINES)
    return out


# B2 sensitivity added 2026-10-23 after the review (not a trial, the registered DEBT is unchanged): the registered DEBT
# (fundamentals: LongTermDebtNoncurrent or LongTermDebt, plus DebtCurrent or LongTermDebtCurrent + ShortTermBorrowings,
# missing = 0) misses common XBRL debt tags, so some leveraged companies show DEBT = 0. The sensitivity uses the
# largest of the registered value and these alternative definitions (each a sum of the tags found).
DEBT_ALT = {
    "ltdcl": ("LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent",
              "CommercialPaper", "ShortTermBorrowings", "OtherShortTermBorrowings"),
    "combined": ("DebtLongtermAndShorttermCombinedAmount",),
    "ltdcl_incl_current": ("LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",),
    "notes": ("LongTermNotesPayable", "NotesPayableCurrent", "ConvertibleNotesPayable",
              "ConvertibleLongTermNotesPayable", "SeniorLongTermNotes", "OtherLongTermDebtNoncurrent",
              "LongTermLineOfCredit"),
}
DEBT_ALT_CONCEPTS = tuple(sorted({c for v in DEBT_ALT.values() for c in v} | {"CommercialPaper", "DebtCurrent"}))


def debt_extended(rows: pd.DataFrame, facts: pd.DataFrame, max_days: int = 7) -> pd.DataFrame:
    """Per row (s, cik_i, fy0_end, debt0): each alternative debt definition from ``facts`` (cik, concept, end, filed,
    val: 10-K instants) filed strictly before s and dated within ``max_days`` of FY0's end (the last filed wins), and
    ``debt_ext`` = the largest of the registered DEBT (missing = 0), the registered DEBT + CommercialPaper when no
    DebtCurrent is tagged, and the alternatives. Same index as ``rows``."""
    f = facts.assign(end_t=pd.to_datetime(facts["end"]))
    by_cik = {c: g for c, g in f.groupby("cik")}
    out = []
    for r in rows.itertuples():
        g = by_cik.get(int(r.cik_i))
        s = str(pd.Timestamp(r.s).date())
        if g is not None and pd.notna(r.fy0_end):
            g = g[(g["filed"].astype(str) < s) & ((g["end_t"] - pd.Timestamp(r.fy0_end)).abs().dt.days <= max_days)]
            v = g.sort_values("filed").groupby("concept")["val"].last().to_dict()
        else:
            v = {}
        reg = float(r.debt0) if pd.notna(r.debt0) else 0.0
        alt = {"registered": reg,
               "registered_plus_cp": reg + (v["CommercialPaper"] if "CommercialPaper" in v and "DebtCurrent" not in v
                                            else 0.0)}
        for k, cons in DEBT_ALT.items():
            found = [v[c] for c in cons if c in v]
            alt[k] = float(sum(found)) if found else np.nan
        best = max(alt, key=lambda k: alt[k] if np.isfinite(alt[k]) else -np.inf)
        out.append({**alt, "debt_ext": alt[best], "debt_ext_source": best})
    return pd.DataFrame(out, index=rows.index)


def b2_select(g: pd.DataFrame, rule: str, n: int, y10: float) -> pd.DataFrame:
    """The names chosen at one signal from its universe rows ``g`` (with ni0, mcap and the ``qualifies`` columns):
    EY = earnings yield above the 10-year Treasury yield ``y10``, then the n highest E/P; PE = the n highest E/P
    (E/P > 0) among the qualifiers. Ties broken by security id."""
    ep = pd.to_numeric(g["ni0"], errors="coerce") / g["mcap"].where(g["mcap"] > 0)
    ok = g["q_roe"] & g["q_de"] & g["q_growth"] & np.isfinite(ep) & (ep > 0)
    if rule == "EY":
        ok &= ep > y10
    elif rule != "PE":
        raise ValueError(rule)
    c = g.loc[ok].assign(ep=ep[ok]).sort_values(["ep", "security_id"], ascending=[False, True])
    return c.head(n)


# ======================================================================== B3: factor replication

# NBER w19681 (2013), Table 4 column 3 (Berkshire stock 1976-2011) and column 6 (13F portfolio 1980-2011); Table 2
FKP_COL3 = {"MKT": 0.95, "SMB": -0.15, "HML": 0.46, "UMD": -0.05, "BAB": 0.29, "QMJ": 0.43}
FKP_COL6 = {"MKT": 0.98, "SMB": 0.00, "HML": 0.31, "UMD": -0.10, "BAB": 0.15, "QMJ": 0.44}
FKP_BETA, FKP_SIGMA_I, FKP_LEVERAGE = 0.68, 0.224, 1.6
FKP_SCALE_WINDOW = ("1976-01", "2011-12")


def fkp_section7(f: pd.DataFrame, coefs: dict = FKP_COL3, beta: float = FKP_BETA, sigma_i: float = FKP_SIGMA_I,
                 window: tuple = FKP_SCALE_WINDOW) -> tuple[pd.Series, float]:
    """R1 = RF + beta x MKT + k x r^A, r^A = (m - beta) MKT + s SMB + h HML + u UMD + b BAB + q QMJ, with k fixed so
    that r^A's annualised volatility over ``window`` is ``sigma_i`` (the paper's section 7). Returns (R1, k)."""
    ra = (coefs["MKT"] - beta) * f["MKT"] + sum(coefs[k] * f[k] for k in ("SMB", "HML", "UMD", "BAB", "QMJ"))
    w = ra[(ra.index >= pd.Period(window[0], "M")) & (ra.index <= pd.Period(window[1], "M"))]
    k = sigma_i / (w.std(ddof=1) * math.sqrt(12))
    return f["RF"] + beta * f["MKT"] + k * ra, float(k)


def fkp_levered(f: pd.DataFrame, coefs: dict, leverage: float, spread: float) -> pd.Series:
    """R2 = RF + L x sum(coef x factor) - (L - 1) x spread / 12 (borrowing at T-bill + spread)."""
    ex = sum(c * f[k] for k, c in coefs.items())
    return f["RF"] + leverage * ex - (leverage - 1) * spread / 12.0


def monthly_stats(r: pd.Series, rf: pd.Series) -> dict:
    """CAGR, vol, Sharpe, max drawdown of a monthly return series (month-end periods)."""
    r = r.dropna()
    if len(r) < 2:
        return {"months": int(len(r))}
    v = pd.concat([pd.Series([1.0]), (1 + r).cumprod().reset_index(drop=True)])
    ex = r - rf.reindex(r.index).fillna(0.0)
    return {"months": int(len(r)), "first": str(r.index[0]), "last": str(r.index[-1]),
            "total_return": float(np.prod(1 + r.values) - 1),
            "cagr": float(np.prod(1 + r.values) ** (12 / len(r)) - 1), "vol": float(r.std() * math.sqrt(12)),
            "sharpe": float(ex.mean() / ex.std() * math.sqrt(12)) if ex.std() > 0 else np.nan,
            "max_dd": max_drawdown(v)}


def ols_multi(y: pd.Series, X: pd.DataFrame) -> dict:
    """Monthly OLS y = a + X b (added 2026-10-23: the paper's Table 4 regressions): annualised intercept, its t, the
    loadings ``b_<column>`` and R squared."""
    j = pd.concat([y.rename("_y"), X], axis=1).dropna()
    n, k = len(j), X.shape[1]
    if n < 12 + k:
        return {"months": n}
    A = np.column_stack([np.ones(n), j[list(X.columns)].to_numpy()])
    beta, *_ = np.linalg.lstsq(A, j["_y"].to_numpy(), rcond=None)
    res = j["_y"].to_numpy() - A @ beta
    s2 = res @ res / (n - k - 1)
    se = np.sqrt(np.diag(s2 * np.linalg.inv(A.T @ A)))
    return {"months": n, "alpha_ann": float(beta[0] * 12), "t_alpha": float(beta[0] / se[0]),
            **{f"b_{c}": float(b) for c, b in zip(X.columns, beta[1:])},
            "r2": float(1 - res.var() / j["_y"].var())}


def ols(y: pd.Series, x: pd.Series) -> dict:
    """Monthly OLS y = a + b x: annualised intercept, its t, slope, correlation."""
    j = pd.concat([y, x], axis=1, keys=["y", "x"]).dropna()
    n = len(j)
    if n < 12:
        return {"months": n}
    X = np.column_stack([np.ones(n), j["x"].to_numpy()])
    beta, *_ = np.linalg.lstsq(X, j["y"].to_numpy(), rcond=None)
    res = j["y"].to_numpy() - X @ beta
    s2 = res @ res / (n - 2)
    cov = s2 * np.linalg.inv(X.T @ X)
    return {"months": n, "alpha_ann": float(beta[0] * 12), "t_alpha": float(beta[0] / math.sqrt(cov[0, 0])),
            "slope": float(beta[1]), "corr": float(j["y"].corr(j["x"]))}


# ======================================================================== evaluation

def level_from_returns(r: pd.Series, base: float = 1.0) -> pd.Series:
    return base * (1 + r.fillna(0.0)).cumprod()


def vs(nav: pd.Series, bench: pd.Series, rf: pd.Series, start: str, end: str, res: dict | None = None,
       account_start: str | None = None, account: float = 10_000.0) -> dict:
    """``segment_metrics`` of ``nav`` against one benchmark level (its keys say 'oneq' whatever the benchmark is);
    the base is the last close before ``start``."""
    return segment_metrics(nav, bench, bench, rf, start, end, res, account_start=account_start or start,
                           account=account)


def judge(parts: list[dict], full: dict) -> dict:
    """A / B (``ab_criteria`` on the ONEQ-style keys of ``vs``) and the full-period t."""
    a, b = ab_criteria(parts, full, cagr="cagr", bench="oneq_cagr", t="t_monthly_excess_vs_oneq",
                       dd="dd_shallower_than_oneq_pp")
    return {"A": a, "B": b, "pass": bool(a or b), "t_full": full["t_monthly_excess_vs_oneq"]}


def halves_by_sessions(nav: pd.Series, start: str, end: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """Two halves of the sessions with a return in [start, end] (the first return session is ``start``)."""
    s = nav.index[(nav.index >= pd.Timestamp(start)) & (nav.index <= pd.Timestamp(end))]
    k = len(s) // 2
    return (str(s[0].date()), str(s[k - 1].date())), (str(s[k].date()), str(s[-1].date()))


def window_return(level: pd.Series, a: str, b: str) -> float:
    """Growth from the last close before ``a`` to the last close on or before ``b`` (NaN without a base)."""
    pre = level[level.index < pd.Timestamp(a)]
    inn = level[(level.index >= pd.Timestamp(a)) & (level.index <= pd.Timestamp(b))]
    if pre.empty or inn.empty:
        return np.nan
    return float(inn.iloc[-1] / pre.iloc[-1] - 1)


def window_max_dd(level: pd.Series, a: str, b: str) -> float:
    pre = level[level.index < pd.Timestamp(a)].iloc[-1:]
    inn = level[(level.index >= pd.Timestamp(a)) & (level.index <= pd.Timestamp(b))]
    v = pd.concat([pre, inn])
    return max_drawdown(v) if len(v) > 1 else np.nan


def yearly_table(levels: dict, first: str, last: str) -> pd.DataFrame:
    """Calendar-year returns of each level series (first year from its first close in range)."""
    out = {}
    for k, s in levels.items():
        s = s[(s.index >= pd.Timestamp(first)) & (s.index <= pd.Timestamp(last))]
        out[k] = yearly(s.pct_change().dropna())
    return pd.DataFrame(out)


def monthly_table(levels: dict, first: str, last: str) -> pd.DataFrame:
    out = {}
    for k, s in levels.items():
        s = s[(s.index >= pd.Timestamp(first)) & (s.index <= pd.Timestamp(last))]
        out[k] = monthly(s.pct_change().dropna())
    t = pd.DataFrame(out)
    t.index = t.index.astype(str)
    return t
