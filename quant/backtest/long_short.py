"""Long / flat / short exposure engines for an index ETF (docs/research_ledger_sma200_short.md, section 0).

``simulate_long_short``: a $10,000 IBKR Pro account that holds, per target exposure e decided at the close of t and
traded at the close of t + ``lag``:

- e >= 0: e in the 1x fund (QQQ, total return), 1 - e in a T-bill ETF (the cash leg of quant.backtest.exposure);
- e < 0, ``impl="margin"``: a short sale of |e| x equity of the 1x fund in a Reg T margin account. The short's market
  value moves with the fund's price; substitute dividends are paid out of cash on the ex-dates; the borrow fee is
  charged on the short's market value per calendar day / 360. The owner's equity sits in the T-bill ETF, sized so the
  Reg T initial requirement (50% of the short + 50% of the T-bill ETF) is at most 95% of equity, the rest as cash.
  Interest follows the IBKR rules of docs/research_ledger_short_overlay.md 0.5 (``daily_interest``): a margin loan of
  max(0, short value - cash) at BM + 1.5%; cash above $10k earns BM - 0.5% scaled by NAV / $100k; short proceeds earn
  BM - 0.5% only when NAV >= $100k. Each close the Reg T maintenance requirement (25% of long positions + 30% of the
  short when the price is above $16.67, ``short_maint``) is checked; below it the short is covered at that close with
  0.5% extra slippage, everything goes to the T-bill ETF and no new short is opened until the target leaves the short
  side.
- e < 0, ``impl="inverse"``: |e| x ``inv_scale`` x equity in an inverse fund (PSQ: scale 1; SQQQ, -3x: scale 1/3), the
  rest in the T-bill ETF; no margin.

Positions are not rebalanced while the target is unchanged (they drift), as in quant.backtest.exposure. Orders cost
``etf_order_cost`` (IBKR Tiered per share, pass-through, sell fee, half-spread); a switch is one order per instrument
(a +1 -> -1 switch is one sell of 2 x equity). Fractional shares. With e in {0, 1} the engine reproduces
``quant.backtest.exposure.simulate`` exactly (tests/test_research_sma200_short.py).

``short_maint`` and ``daily_interest`` are copies of quant.strategies.short_overlay's (the backtest layer does not
import strategies); a test asserts that both give the same numbers.

``simulate_index_ls``: the index-level (price index, fractions of 1.0) version for the long-history replication:
long = the index return; flat = cash at ``cash`` (0 on the price basis); a short of |e| earns -r plus ``credit`` (the
T-bill rate on the short notional) minus ``borrow`` per session; ``cost`` per unit of index value traded. With e in
[0, 1] and no credit it reproduces ``quant.strategies.international.simulate_exposure``.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.backtest.costs import NOMINAL_PRICE, START_EQUITY, etf_order_cost

HALF_SPREAD = 1e-4                # QQQ and the T-bill ETF, as quant.backtest.exposure


@dataclass(frozen=True)
class MarginRules:
    borrow_fee: float = 0.0030        # per year on the short's market value (IBKR QQQ fee 0.2553% on 2026-10-20)
    margin_spread: float = 0.015      # IBKR Pro USD tier 1: BM + 1.5%
    credit_spread: float = 0.005      # BM - 0.5% above the first $10k
    credit_free: float = 10_000.0
    full_rate_nav: float = 100_000.0  # below: credit scaled by NAV / 100k and short proceeds earn nothing
    daycount: float = 360.0
    regt_init: float = 0.50
    init_cap: float = 0.95
    maint_long: float = 0.25
    liq_slip: float = 0.005
    collateral_in_bills: bool = True  # False: the owner's equity stays as cash during a short


def short_maint(price: float, shares: float, value: float) -> float:
    """IBKR Reg T maintenance requirement of one short stock position (copy of strategies.short_overlay's)."""
    if price > 16.67:
        return 0.30 * value
    if price >= 5.0:
        return 5.0 * shares
    if price >= 2.5:
        return value
    return 2.5 * shares


def daily_interest(cash: float, short_mv: float, nav: float, effr: float, days: int,
                   rules: MarginRules = MarginRules()) -> tuple[float, float]:
    """(margin interest charged, credit interest earned) for ``days`` calendar days (copy of
    strategies.short_overlay's, the spreads and thresholds as ``rules``)."""
    own = cash - short_mv
    loan = max(0.0, -own)
    debit = loan * (effr + rules.margin_spread) * days / rules.daycount
    cr = max(effr - rules.credit_spread, 0.0)
    scale = min(1.0, max(nav, 0.0) / rules.full_rate_nav)
    credit = max(0.0, own - rules.credit_free) * cr * scale * days / rules.daycount
    if nav >= rules.full_rate_nav:
        credit += short_mv * cr * days / rules.daycount
    return debit, credit


@dataclass
class Market:
    """Daily inputs on one session index."""
    r_long: pd.Series        # 1x fund daily total return (QQQ adjclose)
    r_price: pd.Series       # 1x fund daily price return (split-adjusted close)
    div: pd.Series           # 1x fund cash dividend per share on the ex-date (split-adjusted), 0 elsewhere
    price: pd.Series         # 1x fund split-adjusted close (share counts, maintenance tiers); NaN -> NOMINAL_PRICE
    r_cash: pd.Series        # T-bill ETF daily return
    effr: pd.Series          # BM for the IBKR rates, annual decimal
    r_inv: pd.Series | None = None      # inverse fund daily total return
    inv_price: pd.Series | None = None
    inv_scale: float = 1.0               # fund units per unit of |e| (1/3 for a -3x fund)
    inv_leverage: float = -1.0
    hs_long: float = HALF_SPREAD
    hs_cash: float = HALF_SPREAD
    hs_inv: float = 2e-4


def _px(x: float) -> float:
    return NOMINAL_PRICE if (x is None or np.isnan(x)) else float(x)


def simulate_long_short(target: pd.Series, mk: Market, start: str, end: str, impl: str = "margin",
                        rules: MarginRules = MarginRules(), lag: int = 1, equity: float = START_EQUITY) -> dict:
    """Run the account from the close of ``start`` (all in the T-bill ETF, as quant.backtest.exposure) to ``end``.

    Returns value (NAV at each close from ``start``), ret, state (the target held into the next session),
    exposure_held (net exposure earning each day's return), orders, switches, liquidations and the costs, borrow fees,
    dividends paid, margin interest and credit interest in dollars."""
    if impl not in ("margin", "inverse"):
        raise ValueError(impl)
    idx = mk.r_long.index
    i0 = int(idx.searchsorted(pd.Timestamp(start)))
    i1 = int(idx.searchsorted(pd.Timestamp(end), side="right")) - 1
    tv = target.reindex(idx).values
    RL, RP, DV, PX = mk.r_long.values, mk.r_price.values, mk.div.values, mk.price.values
    RC, BM = mk.r_cash.values, mk.effr.reindex(idx).values
    RI = mk.r_inv.reindex(idx).values if mk.r_inv is not None else np.zeros(len(idx))
    IPX = mk.inv_price.reindex(idx).values if mk.inv_price is not None else np.full(len(idx), np.nan)
    days = np.r_[1, (idx[1:] - idx[:-1]).days]
    q, b, c, s, v = 0.0, float(equity), 0.0, 0.0, 0.0
    cur, blocked = None, False
    acc = {"orders": 0, "switches": 0, "liquidations": 0, "costs": 0.0, "borrow": 0.0, "dividends": 0.0,
           "margin_interest": 0.0, "credit_interest": 0.0, "liquidation_dates": []}
    values, states, expo = [], [], []

    def order(value, price, sell, hs):
        if abs(value) > 1.0:
            acc["orders"] += 1
            return etf_order_cost(abs(value), price, sell, hs)
        return 0.0

    for i in range(i0, i1 + 1):
        if i > i0:
            nav_prev = q + b + c - s + v
            if s > 0 or c != 0:
                debit, credit = daily_interest(c, s, nav_prev, BM[i - 1], int(days[i]), rules)
                c += credit - debit
                acc["margin_interest"] += debit
                acc["credit_interest"] += credit
            if s > 0:
                fee = s * rules.borrow_fee * days[i] / rules.daycount
                dv = s * DV[i] / _px(PX[i - 1])
                c -= fee + dv
                acc["borrow"] += fee
                acc["dividends"] += dv
            q *= 1 + RL[i]
            b *= 1 + RC[i]
            s *= 1 + RP[i]
            v *= 1 + RI[i]
        nav = q + b + c - s + v
        if s > 0:
            pxi = _px(PX[i])
            req = rules.maint_long * (q + b + v) + short_maint(pxi, s / pxi, s)
            if nav < req:
                cost = order(s, pxi, False, mk.hs_long) + rules.liq_slip * s
                c -= s + cost
                s = 0.0
                acc["costs"] += cost
                acc["liquidations"] += 1
                acc["liquidation_dates"].append(str(idx[i].date()))
                nav = q + b + c + v
                cost2 = order(nav - b, NOMINAL_PRICE, False, mk.hs_cash)   # the rest into the T-bill ETF
                acc["costs"] += cost2
                q, b, c, v = 0.0, nav - cost2, 0.0, 0.0
                nav = b
                blocked, cur = True, 0.0
        desired = tv[i - lag] if i - lag >= 0 else np.nan
        if np.isnan(desired):
            desired = 0.0 if cur is None else cur
        if blocked:
            if desired < 0:
                desired = 0.0
            else:
                blocked = False
        if cur is None or desired != cur:
            e = float(desired)
            nq = nb = nc = ns = nv = 0.0
            if e >= 0:
                nq, nb = e * nav, (1 - e) * nav
            elif impl == "margin":
                ns = -e * nav
                wb = min(1.0, (rules.init_cap - rules.regt_init * (-e)) / rules.regt_init) if rules.collateral_in_bills else 0.0
                wb = max(wb, 0.0)
                nb = wb * nav
                nc = nav - nb + ns
            else:
                nv = -e * mk.inv_scale * nav
                nb = nav - nv
            cost = order((nq - ns) - (q - s), _px(PX[i]), (nq - ns) < (q - s), mk.hs_long)
            cost += order(nb - b, NOMINAL_PRICE, nb < b, mk.hs_cash)
            cost += order(nv - v, _px(IPX[i]), nv < v, mk.hs_inv)
            k = 1 - cost / nav
            q, b, c, s, v = nq * k, nb * k, nc * k, ns * k, nv * k
            acc["costs"] += cost
            if cur is not None:
                acc["switches"] += 1
            cur = desired
        nav = q + b + c - s + v
        values.append(nav)
        states.append(cur)
        expo.append((q - s + mk.inv_leverage * v) / nav if nav > 0 else np.nan)
    val = pd.Series(values, index=idx[i0:i1 + 1])
    st = pd.Series(states, index=idx[i0:i1 + 1])
    ex = pd.Series(expo, index=idx[i0:i1 + 1])
    return {"value": val, "ret": val.pct_change().iloc[1:], "state": st, "state_held": st.shift(1).iloc[1:],
            "exposure_held": ex.shift(1).iloc[1:], **acc}


def simulate_index_ls(e: pd.Series, r: pd.Series, start_i: int, cost: float, credit: pd.Series | None = None,
                      borrow: pd.Series | None = None, cash: pd.Series | float = 0.0, lag: int = 1) -> dict:
    """Index-level value path (fractions of 1.0) that starts as cash at the close of ``start_i`` and trades there to
    the target decided ``lag`` sessions earlier, then whenever the target changes. Long part a (negative = short),
    cash part c (the short's proceeds included). Each session: a short earns |a| x (credit - borrow) on the previous
    close's notional, a grows with the index, c with ``cash``. Fee = ``cost`` x |change of a|. Returns value, ret,
    held (a / value earning each day), state (the target executed at each close), switches, turnover."""
    E, R = e.values, r.values
    n = len(r)
    CR = credit.reindex(r.index).values if credit is not None else np.zeros(n)
    BR = borrow.reindex(r.index).values if borrow is not None else np.zeros(n)
    C = cash.reindex(r.index).values if isinstance(cash, pd.Series) else np.full(n, float(cash))
    a, c = 0.0, 1.0
    last = None
    vals, held, states, switches, turnover = [], [], [], 0, 0.0
    for i in range(start_i, n):
        if i > start_i:
            if a != 0 and np.isnan(R[i]):
                raise ValueError(f"missing index return on {r.index[i].date()}")
            if a < 0:
                c += -a * (CR[i] - BR[i])
            a *= 1 + (0.0 if np.isnan(R[i]) else R[i])
            c *= 1 + C[i]
        tot = a + c
        tgt = E[i - lag]
        if np.isnan(tgt):
            raise ValueError(f"no target on {r.index[i - lag].date()}")
        if last is None or tgt != last:
            new_a = tgt * tot
            fee = cost * abs(new_a - a)
            turnover += abs(new_a - a) / tot
            tot -= fee
            a, c = tgt * tot, (1 - tgt) * tot
            if last is not None:
                switches += 1
            last = tgt
        vals.append(a + c)
        held.append(a / (a + c))
        states.append(last)
    idx = r.index[start_i:]
    val = pd.Series(vals, index=idx)
    h = pd.Series(held, index=idx).shift(1).iloc[1:]
    return {"value": val, "ret": val.pct_change().iloc[1:], "held": h, "state": pd.Series(states, index=idx),
            "switches": switches, "turnover": turnover}
