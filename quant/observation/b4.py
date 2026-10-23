"""B4: forward observation of the seven candidates added on 2026-10-24 (docs/forward_observation_checklist.md B4).

Observed, not traded. Rules frozen as registered (quant.strategies.registry):

- **M1, M2, M3, M6** (docs/research_ledger_megacap.md 0.2-0.4): Nasdaq mega-caps by point-in-time market cap, a
  monthly signal at each month's last session from fresh data (``b4_megacap``), traded at the next session's close.
- **LEV-B3** (docs/research_ledger_sector_lev.md 0.3-0.4, B3): QQQ SMA200 with a 2% band, on 1.5x = 50% QQQ + 50% QLD,
  off 1.0x = 100% QQQ; trades at the next close after a state change and at each month end. Key comparison: constant
  1.37x (63% QQQ + 37% QLD, monthly; docs/audit_leverage_b3.md 9).
- **V3** (docs/research_ledger_voltarget.md 0.3-0.4): e = min(1.5, QQQ expanding vol / 20-day vol), decided at each
  month end. Comparison: the constant portfolio at V3's average realised exposure to date (0.7).
- **BT4b** (docs/research_ledger_bitcoin.md 0.2-0.4): 90% QQQ + 10% BTC, rebalanced at the close after each month
  end; comparisons ONEQ and QQQ alone; the BTC leg as IBIT is report only.

No simulator is reimplemented: ``quant.strategies.sector_lev.part_b_targets``, ``voltarget.targets`` /
``constant_targets`` / ``held_exposure``, ``quant.backtest.flagged_weights.simulate``, ``bitcoin.sleeve_targets`` /
``simulate`` / ``buy_hold`` and the mega-cap functions of ``b4_megacap`` are the research code. This module chooses
the forward window, loads the forward data (``b4_data``), turns the research outputs into monthly rows and writes the
B4 part of docs/forward_observation_log.md (after the B1-B3 part, which ``runner.render_log`` writes unchanged).

Forward window: data after the 2026-10-23 close. LEV-B3 and V3 open at the 2026-10-26 close (traded to the target in
force: B3's state at the 10-23 close, V3's exposure decided at the 2026-09-30 month end); the monthly lines take their
first signal at the 2026-10-30 close and open at the next close (2026-11-02). A month's row is ``final`` once the line
itself holds a session of a later month (a mega-cap line stops at a month-end signal whose file is not computed yet,
BT4b at the last complete BTC day); otherwise ``provisional``, replaced by the next run.
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")   # the S-MISP / mega-cap signal inputs are the frozen v2 base

import json  # noqa: E402
import shutil  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant.backtest import flagged_weights as fw  # noqa: E402
from quant.backtest.costs import PAXOS_MONTHLY_FEE, etf_order_cost, ibkr_crypto_cost  # noqa: E402
from quant.data import bitcoin as bd  # noqa: E402
from quant.evaluation.metrics import monthly, t_and_ir  # noqa: E402
from quant.observation import b4_data as bdata  # noqa: E402
from quant.observation import b4_megacap as bm  # noqa: E402
from quant.observation import smisp as fs  # noqa: E402
from quant.strategies import bitcoin as bt  # noqa: E402
from quant.strategies import registry  # noqa: E402
from quant.strategies import sector_lev as sl  # noqa: E402
from quant.strategies import voltarget as vt  # noqa: E402

LEV = registry.get("LEV-B3").frozen
V3 = registry.get("V3").frozen
BT = registry.get("BT4b").frozen
START_CLOSE = registry.B4_START_CLOSE
FIRST_SIGNAL = registry.B4_FIRST_SIGNAL
MEGA = registry.B4_MEGACAP
KEYS = (*MEGA, "LEV-B3", "V3", "BT4b")
DAILY = ("LEV-B3", "V3")
VERDICT_MONTHS = (24, 36)
V3_PARAMS = vt.VolParams(target_vol=V3["target_vol"], vol_short=V3["vol_short"], vol_quarter=V3["vol_quarter"],
                         vol_lr_min=V3["vol_lr_min"], sma_len=V3["sma_len"], sma_band=V3["sma_band"])
assert bt.SPEC["BT4b"].sleeve == BT["sleeve"]
HELD_STALE_SESSIONS = 2          # a target name whose bars stop 1-2 sessions before the data end: the line waits
TITLES = {"M1": "B4 M1 mega-cap top 10, equal weight", "M2": "B4 M2 mega-cap top 5, equal weight",
          "M3": "B4 M3 mega-cap top 20, best 5 by 6-month momentum", "M6": "B4 M6 mega-cap top 10, cap-weighted",
          "LEV-B3": "B4 LEV-B3 QQQ SMA200 1.5x / 1.0x (sector_lev B3)",
          "V3": "B4 V3 QQQ volatility target (long-run vol / 20-day vol, cap 1.5x)",
          "BT4b": "B4 BT4b 90% QQQ + 10% BTC, monthly"}
MATCH = {**{k: "QQQ" for k in MEGA}, "LEV-B3": "constant 1.37x", "V3": "constant ē to date", "BT4b": "QQQ"}


def paths(b4_dir=None, state_dir=None, smisp_dir=None) -> dict:
    base = Path(b4_dir or bdata.cache_dir())
    return {"b4_dir": base, "state_dir": Path(state_dir or bm.STATE), "held_dir": base / "megacap" / "held",
            "smisp_dir": Path(smisp_dir or fs.BASE)}


# ======================================================================== small helpers

def _start_index(sessions: pd.DatetimeIndex, close: str) -> int | None:
    """Index of the opening close; None while the data hold no session after it."""
    ts = pd.Timestamp(close)
    if sessions[-1] <= ts:
        return None
    if ts not in sessions:
        raise ValueError(f"no session on the opening close {close}")
    return int(sessions.get_loc(ts))


def _last_complete(rets: pd.DataFrame, cols, s_i: int) -> int:
    """Last session index from which every leg in ``cols`` has a return on each day since the start."""
    ok = rets[list(cols)].iloc[s_i + 1:].notna().all(axis=1).to_numpy()
    bad = np.flatnonzero(~ok)
    return s_i + (int(bad[0]) if len(bad) else len(ok))


def _hold(leg: str, legs, s_i: int, end_i: int) -> pd.Series:
    """``flagged_weights.buy_hold`` through ``end_i`` (the same one-leg target table, no rebalance flags)."""
    w = pd.DataFrame({leg: 1.0}, index=legs.sessions)
    return fw.simulate(w, pd.Series(False, index=legs.sessions), legs.rets, legs.close, s_i, end_i)["ret"]


def _returns(level: pd.Series) -> pd.Series:
    return (level / level.shift(1) - 1).iloc[1:]


def _cost_fns(etf_close: pd.Series | None = None) -> dict:
    """The bitcoin study's cost functions (studies/bitcoin.cost_fns) with the registered half-spreads."""
    def btc(v, px, sell, ts):
        if etf_close is not None:
            return etf_order_cost(v, float(etf_close.loc[ts]), sell, BT["etf_btc_half_spread"])
        return ibkr_crypto_cost(v, BT["btc_half_spread"])
    return {"BTC": btc,
            "QQQ": lambda v, px, sell, ts: etf_order_cost(v, px, sell, BT["qqq_half_spread"]),
            "ONEQ": lambda v, px, sell, ts: etf_order_cost(v, px, sell, BT["oneq_half_spread"])}


# ======================================================================== the ETF lines

def run_lev_b3(legs) -> dict:
    s_i = _start_index(legs.sessions, LEV["start_close"])
    if s_i is None:
        return {"started": False, "reason": "before_start"}
    w, flag = sl.part_b_targets("B3", legs.p_qqq, {"B3": (LEV["on"], LEV["off"])}, LEV["sma"], LEV["band"])
    end_i = _last_complete(legs.rets, ("QQQ", "QLDX", "BENCH"), s_i)
    if end_i <= s_i:
        raise RuntimeError("no QLD / ONEQ return after the opening close (run --fetch)")
    sim = fw.simulate(w, flag, legs.rets, legs.close, s_i, end_i)
    cw, cf = vt.constant_targets(LEV["matched_exposure"], legs.sessions)
    csim = fw.simulate(cw, cf, legs.rets, legs.close, s_i, end_i)
    r = sim["ret"]
    state = (w["QLDX"] > 0).astype(float).where(w["QLDX"].notna())
    return {"started": True, "ret": r, "match": csim["ret"], "oneq": _hold("BENCH", legs, s_i, end_i),
            "qqq": _hold("QQQ", legs, s_i, end_i), "w": w, "flag": flag, "state": state,
            "exposure": vt.held_exposure(sim["held"]), "sim": sim, "s_i": s_i, "end_i": end_i,
            "last_session": legs.sessions[end_i], "first_return": r.index[0]}


def run_v3(legs) -> dict:
    s_i = _start_index(legs.sessions, V3["start_close"])
    if s_i is None:
        return {"started": False, "reason": "before_start"}
    if legs.rf is None:
        raise RuntimeError("no DTB3 file for the T-bill leg (run --fetch)")
    w, flag, e_t = vt.targets("V3", legs, V3_PARAMS)
    end_i = _last_complete(legs.rets, ("QQQ", "QLDX", "BENCH"), s_i)
    if end_i <= s_i:
        raise RuntimeError("no QLD / ONEQ return after the opening close (run --fetch)")
    sim = fw.simulate(w, flag, legs.rets, legs.close, s_i, end_i)
    r = sim["ret"]
    e_held = vt.held_exposure(sim["held"])
    # the comparison: month m's days from the constant portfolio at the mean exposure realised from the start through
    # m's last session (incremental: no later data enter a month's row)
    per = r.index.to_period("M")
    parts, ebar = [], {}
    for mon in per.unique():
        last = r.index[per == mon][-1]
        eb = float(e_held.loc[:last].mean())
        cw, cf = vt.constant_targets(eb, legs.sessions)
        c = fw.simulate(cw, cf, legs.rets, legs.close, s_i, int(legs.sessions.get_loc(last)))["ret"]
        parts.append(c[c.index.to_period("M") == mon])
        ebar[str(mon)] = eb
    cw, cf = vt.constant_targets(float(e_held.mean()), legs.sessions)    # one e-bar over the whole window (research
    full = fw.simulate(cw, cf, legs.rets, legs.close, s_i, end_i)["ret"]    # definition; console only, as B1 / B2)
    return {"started": True, "ret": r, "match": pd.concat(parts), "ebar": ebar, "match_full_window": full,
            "oneq": _hold("BENCH", legs, s_i, end_i), "qqq": _hold("QQQ", legs, s_i, end_i), "w": w, "flag": flag,
            "e_t": e_t, "exposure": e_held, "sim": sim,
            "s_i": s_i, "end_i": end_i, "last_session": legs.sessions[end_i], "first_return": r.index[0]}


def bt4b_legs(legs, btc: bdata.Btc) -> tuple[pd.DatetimeIndex, pd.DataFrame, pd.DataFrame]:
    """The sleeve legs on US sessions (studies/bitcoin.legs_sessions): QQQ, BTC = the UTC-day-S close of each
    session S, CASH = T-bill, ONEQ; sessions up to the last complete BTC day."""
    ses = legs.sessions[legs.sessions <= btc.close.index[-1]]
    btc_s = bd.on_sessions(btc.close, ses).pct_change(fill_method=None)
    rf = legs.rf.reindex(ses) if legs.rf is not None else pd.Series(0.0, index=ses)
    rets = pd.DataFrame({"QQQ": legs.rets["QQQ"].reindex(ses), "BTC": btc_s, "CASH": rf,
                         "ONEQ": legs.rets["ONEQ"].reindex(ses)}, index=ses)
    prices = pd.DataFrame({"QQQ": legs.close["QQQ"].reindex(ses), "ONEQ": legs.close["ONEQ"].reindex(ses),
                           "BTC": np.nan, "CASH": np.nan}, index=ses)
    return ses, rets, prices


def run_bt4b(legs, btc: bdata.Btc | None) -> dict:
    ses_all = legs.sessions
    first = pd.Timestamp(BT["first_signal"])
    later = ses_all[ses_all > first]
    if ses_all[-1] <= first or len(later) < 2:
        return {"started": False, "reason": "before_first_signal"}
    if btc is None:
        raise RuntimeError("no BTC data (run --fetch)")
    ses, rets, prices = bt4b_legs(legs, btc)
    s0 = later[0]                                     # the first signal's trade close
    if ses[-1] <= s0:
        return {"started": False, "reason": f"no complete BTC day after the {s0.date()} close yet"}
    w, flag = bt.sleeve_targets(bt.SPEC["BT4b"], ses)
    s_i = int(ses.get_loc(s0))
    end_i = _last_complete(rets, ("QQQ", "BTC", "ONEQ"), s_i)
    if end_i <= s_i:
        return {"started": False, "reason": f"no complete QQQ / BTC / ONEQ day after the {s0.date()} close yet"}
    ses, rets, prices, w, flag = ses[:end_i + 1], rets.iloc[:end_i + 1], prices.iloc[:end_i + 1], \
        w.iloc[:end_i + 1], flag.iloc[:end_i + 1]
    sim = bt.simulate(w, flag, rets, prices, s_i, _cost_fns(), monthly_fee=PAXOS_MONTHLY_FEE)
    out = {"started": True, "ret": sim["ret"], "oneq": bt.buy_hold("ONEQ", rets, prices, s_i, _cost_fns())["ret"],
           "qqq": bt.buy_hold("QQQ", rets, prices, s_i, _cost_fns())["ret"], "sim": sim, "btc_ret": rets["BTC"],
           "btc_weight": sim["held"]["BTC"], "s_i": s_i, "last_session": ses[-1], "first_return": sim["ret"].index[0],
           "btc_info": btc.info, "sessions": ses}
    # report only: the BTC leg as IBIT (studies/bitcoin.run_sleeve(..., etf=IBIT): stock costs, 2 bp, IBIT returns)
    try:
        r2 = rets.copy()
        r2["BTC"] = legs.rets["IBIT"].reindex(ses)
        s2 = bt.simulate(w, flag, r2, prices, s_i, _cost_fns(legs.close["IBIT"].reindex(ses)),
                         monthly_fee=PAXOS_MONTHLY_FEE)
        out["ibit"] = s2["ret"]
    except (ValueError, KeyError) as exc:
        out["ibit"] = None
        out["ibit_note"] = f"IBIT variant not computed ({type(exc).__name__})"
    return out


# ======================================================================== the mega-cap lines

def frozen_signals(sessions: pd.DatetimeIndex, state_dir: Path) -> tuple[dict, pd.Timestamp | None]:
    """The consecutive frozen signal files from the first signal on, and the first due signal without one."""
    sigs = fs.signal_dates(sessions, FIRST_SIGNAL)
    out = {}
    for s in sigs:
        p = bm.signal_path(s, state_dir)
        if not p.exists():
            return out, s
        out[s] = bm.load_signal(p)
    return out, None


def run_megacap(legs, state_dir: Path, held_dir: Path) -> dict:
    ses = legs.sessions
    sigs, pending = frozen_signals(ses, state_dir)
    if not sigs:
        why = ("before_first_signal" if pending is None
               else f"signal {pending.date()} not computed yet (run --fetch-smisp)")
        return {k: {"started": False, "reason": why} for k in MEGA}
    end = pending if pending is not None else ses[-1]
    grid = ses[(ses >= min(sigs)) & (ses <= end)]
    if len(grid) < 3:                                   # signal, trade close, one return session
        return {k: {"started": False, "reason": "no return session after the first trade yet"} for k in MEGA}
    fr = bm.frames_of(sigs, held_dir, grid, str(end.date()))
    # a target name of the latest executed signal whose bars stop 1-2 sessions before the end: a vendor delay, the
    # lines wait for it (beyond that the name is treated as delisted: cash at its last value, as the S-MISP line)
    execd = [s for s in sigs if len(grid[grid > s])]
    cur = sigs[max(execd)] if execd else None
    names = set(cur.loc[(cur[[f"w_{k}" for k in MEGA]] > 0).any(axis=1), "security_id"]) if cur is not None else set()
    lr = fr["last_row"]
    late = [lr[n] for n in names if n in lr.index and pd.notna(lr[n])
            and len(grid[(grid > lr[n])]) <= HELD_STALE_SESSIONS]
    if late:
        end = min(late)
        grid = grid[grid <= end]
        fr = bm.frames_of(sigs, held_dir, grid, str(end.date()))
    tick, ymap = {}, {}
    for sc in sigs.values():
        tick.update(dict(zip(sc["security_id"], sc["ticker"])))
        ymap.update(dict(zip(sc["security_id"], sc["yahoo"])))
    out = {}
    for k in MEGA:
        if not bm.targets_of(sigs, k):
            out[k] = {"started": False, "reason": "no target list in the frozen signals yet"}
            continue
        try:                                            # one rule's missing chart does not stop the others
            res = bm.run_rule(k, sigs, legs, held_dir, str(end.date()), fr)
        except Exception as exc:  # noqa: BLE001
            out[k] = {"started": False, "error": f"{type(exc).__name__}: {exc}"}
            continue
        nav = res["nav"]
        if len(nav) < 2:
            out[k] = {"started": False, "reason": "no return session after the first trade yet"}
            continue
        execs = {ses[ses > s][0]: s for s in res["targets"] if len(ses[ses > s]) and ses[ses > s][0] <= end}
        out[k] = {"started": True, "ret": _returns(nav), "oneq": _returns(res["oneq_nav"]),
                  "qqq": _returns(res["qqq_nav"]), "nav": nav, "targets": res["targets"], "execs": execs,
                  "tick": tick, "yahoo": ymap, "names": res["sim"]["names"], "cost": res["sim"]["cost"],
                  "last_session": nav.index[-1], "first_return": nav.index[1],
                  "pending": pending, "waiting_for": sorted(tick.get(n, n) for n in names
                                                            if n in lr.index and pd.notna(lr[n]) and lr[n] == end)}
    return out


# ======================================================================== compute (no file writes)

def compute(as_of: str, raw_dir: Path, b4_dir=None, state_dir=None, smisp_dir=None) -> dict:
    """Every B4 line for one as-of date. A line that cannot be computed (missing data, an error) is reported with
    ``error`` and keeps its rows of the previous log; the other lines and B1-B3 are not affected."""
    p = paths(b4_dir, state_dir, smisp_dir)
    lines = {}
    try:
        btc = bdata.load_btc(p["b4_dir"], as_of)
    except Exception as exc:  # noqa: BLE001
        btc, btc_err = None, f"{type(exc).__name__}: {exc}"
    else:
        btc_err = None
    try:
        legs = bdata.load_legs(raw_dir, as_of, p["b4_dir"])
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        return {"as_of": as_of, "lines": {k: {"started": False, "error": f"{type(exc).__name__}: {exc}"} for k in KEYS},
                "legs": None, "btc": btc, "btc_error": btc_err, "paths": p}
    try:
        lines.update(run_megacap(legs, p["state_dir"], p["held_dir"]))
    except Exception as exc:  # noqa: BLE001
        lines.update({k: {"started": False, "error": f"{type(exc).__name__}: {exc}"} for k in MEGA})
    for key, fn in (("LEV-B3", lambda: run_lev_b3(legs)), ("V3", lambda: run_v3(legs)),
                    ("BT4b", lambda: run_bt4b(legs, btc))):
        try:
            lines[key] = fn()
        except Exception as exc:  # noqa: BLE001
            why = btc_err if key == "BT4b" and btc_err else f"{type(exc).__name__}: {exc}"
            lines[key] = {"started": False, "error": why}
    for k, v in list(lines.items()):
        if v.get("started"):
            try:                                        # a row error stops that line only (B1-B3 and the others go on)
                v["rows"] = month_rows(k, v)
            except Exception as exc:  # noqa: BLE001
                lines[k] = {"started": False, "error": f"monthly rows: {type(exc).__name__}: {exc}"}
    return {"as_of": as_of, "lines": lines, "legs": legs, "btc": btc, "btc_error": btc_err, "paths": p}


# ======================================================================== monthly rows

def _fmt_pct(x, nd=2) -> str:
    return "" if x is None or not np.isfinite(x) else f"{x * 100:+.{nd}f}%"


def _fmt_pp(x) -> str:
    return "" if x is None or not np.isfinite(x) else f"{x * 100:+.2f}"


def _fmt_t(x) -> str:
    return "" if x is None or not np.isfinite(x) else f"{x:+.2f}"


def base_rows(r: pd.Series, benches: dict, last_session) -> list:
    """Monthly strategy and benchmark returns, excess, cumulative excess (chained from the start, pp) and the
    monthly-excess t (from n = 3); status final once the line holds a session of a later month."""
    mr = monthly(r)
    mb = {k: monthly(v.reindex(r.index)) for k, v in benches.items() if v is not None}
    last = pd.Timestamp(last_session).to_period("M")
    rows = []
    for n, mon in enumerate(mr.index, start=1):
        days = r.index[r.index.to_period("M") == mon]
        row = {"month": str(mon), "n": n, "ret": float(mr[mon]),
               "sessions": f"{days[0]:%m-%d}..{days[-1]:%m-%d} ({len(days)})",
               "status": "final" if mon < last else "provisional"}
        for k, m in mb.items():
            ex = mr[:mon] - m[:mon]
            row[k] = float(m[mon])
            row[f"ex_{k}"] = float(mr[mon] - m[mon])
            row[f"cum_{k}"] = float((1 + mr[:mon]).prod() - (1 + m[:mon]).prod())
            row[f"t_{k}"] = t_and_ir(ex)[0] if len(ex) >= 3 else float("nan")
        rows.append(row)
    return rows


def month_rows(key: str, v: dict) -> list:
    r = v["ret"]
    benches = {"oneq": v["oneq"], "qqq": v["qqq"]}
    if key in DAILY:
        benches["match"] = v["match"]
    if key == "BT4b":
        benches["ibit"] = v.get("ibit")
    rows = base_rows(r, benches, v["last_session"])
    per = r.index.to_period("M")
    for row in rows:
        mon = pd.Period(row["month"], "M")
        days = r.index[per == mon]
        if key == "LEV-B3":
            st = v["state"].reindex(v["w"].index)
            first_dec = v["w"].index[v["s_i"] - 1]           # the decision in force at the opening close
            dec = st.loc[first_dec: days[-1]]
            sw = [d for d in dec.index[dec.ne(dec.shift(1)) & dec.shift(1).notna()] if d.to_period("M") == mon]
            row["state"] = ("on" if st.loc[days[-1]] == 1.0 else "off") + (
                " (" + ", ".join(f"→{'on' if st.loc[d] == 1.0 else 'off'} {d:%m-%d}" for d in sw) + ")" if sw else "")
            row["exposure"] = float(v["exposure"].loc[days].mean())
        elif key == "V3":
            e_t = v["e_t"]
            row["target"] = float(e_t.loc[:days[0]].iloc[-2])     # decided by the session before the month (in force)
            me = v["flag"].reindex(e_t.index)
            nxt = [d for d in e_t.index[me.fillna(False).to_numpy()] if d.to_period("M") == mon]
            row["next_target"] = float(e_t.loc[nxt[-1]]) if nxt else float("nan")
            row["exposure"] = float(v["exposure"].loc[days].mean())
            row["ebar"] = v["ebar"].get(row["month"], float("nan"))
        elif key == "BT4b":
            row["btc_w"] = float(v["btc_weight"].loc[days].mean())
            row["btc"] = float((1 + v["btc_ret"].reindex(days)).prod() - 1)
        else:
            ex = [s for d, s in v["execs"].items() if d.to_period("M") == mon]
            names = [v["tick"].get(sid, sid) for sid, *_ in v["targets"][ex[0]]] if ex else []
            row["holdings"] = " ".join(names) if names else "(held from the previous month)"
    return rows


# ======================================================================== the log (B4 part)

COLS = {
    "mega": ("| month | n | holdings after the month's rebalance | strategy | ONEQ | QQQ | excess vs ONEQ (pp) "
             "| excess vs QQQ (pp) | cum. excess vs ONEQ (pp) | t vs ONEQ | cum. excess vs QQQ (pp) | t vs QQQ "
             "| sessions | status |"),
    "LEV-B3": ("| month | n | SMA200 state | exposure (mean held) | strategy | constant 1.37x | excess vs 1.37x (pp) "
               "| ONEQ | QQQ | excess vs ONEQ (pp) | cum. excess vs ONEQ (pp) | t vs ONEQ | cum. excess vs 1.37x (pp) "
               "| t vs 1.37x | sessions | status |"),
    "V3": ("| month | n | target e in force → next | exposure (mean held) | strategy | constant ē | ē to date "
           "| excess vs constant ē (pp) | ONEQ | QQQ | excess vs ONEQ (pp) | cum. excess vs ONEQ (pp) | t vs ONEQ "
           "| cum. excess vs constant ē (pp) | t vs constant ē | sessions | status |"),
    "BT4b": ("| month | n | BTC weight (mean held) | BTC | strategy | ONEQ | QQQ | excess vs ONEQ (pp) "
             "| excess vs QQQ (pp) | IBIT variant (report only) | cum. excess vs ONEQ (pp) | t vs ONEQ "
             "| cum. excess vs QQQ (pp) | t vs QQQ | sessions | status |"),
}


def row_line(key: str, x: dict) -> str:
    P, D, T = _fmt_pct, _fmt_pp, _fmt_t
    tail = f"{x['sessions']} | {x['status']} |"
    if key in MEGA:
        return (f"| {x['month']} | {x['n']} | {x['holdings']} | {P(x['ret'])} | {P(x['oneq'])} | {P(x['qqq'])} | "
                f"{D(x['ex_oneq'])} | {D(x['ex_qqq'])} | {D(x['cum_oneq'])} | {T(x['t_oneq'])} | {D(x['cum_qqq'])} | "
                f"{T(x['t_qqq'])} | {tail}")
    if key == "LEV-B3":
        return (f"| {x['month']} | {x['n']} | {x['state']} | {x['exposure']:.3f} | {P(x['ret'])} | {P(x['match'])} | "
                f"{D(x['ex_match'])} | {P(x['oneq'])} | {P(x['qqq'])} | {D(x['ex_oneq'])} | {D(x['cum_oneq'])} | "
                f"{T(x['t_oneq'])} | {D(x['cum_match'])} | {T(x['t_match'])} | {tail}")
    if key == "V3":
        nxt = f" → {x['next_target']:.3f}" if np.isfinite(x["next_target"]) else ""
        return (f"| {x['month']} | {x['n']} | {x['target']:.3f}{nxt} | {x['exposure']:.3f} | {P(x['ret'])} | "
                f"{P(x['match'])} | {x['ebar']:.3f} | {D(x['ex_match'])} | {P(x['oneq'])} | {P(x['qqq'])} | "
                f"{D(x['ex_oneq'])} | {D(x['cum_oneq'])} | {T(x['t_oneq'])} | {D(x['cum_match'])} | "
                f"{T(x['t_match'])} | {tail}")
    return (f"| {x['month']} | {x['n']} | {x['btc_w']:.3f} | {P(x['btc'])} | {P(x['ret'])} | {P(x['oneq'])} | "
            f"{P(x['qqq'])} | {D(x['ex_oneq'])} | {D(x['ex_qqq'])} | {P(x.get('ibit'))} | {D(x['cum_oneq'])} | "
            f"{T(x['t_oneq'])} | {D(x['cum_qqq'])} | {T(x['t_qqq'])} | {tail}")


B4_INTRO = """## B4: seven candidates added 2026-10-24

Owner decision 2026-10-24 (`docs/forward_observation_checklist.md` section B4): M1, M2, M3, M6
(`docs/research_ledger_megacap.md` 0.4), LEV-B3 (`docs/research_ledger_sector_lev.md` B3; the S-MISP line above
keeps the name B3), V3 (`docs/research_ledger_voltarget.md`) and BT4b (`docs/research_ledger_bitcoin.md`), with the
rules frozen as registered (`quant/strategies/registry.py`), simulated by the research code
(`quant/observation/b4.py`). Observed, not traded. Returns in percent, tickers and ratios only; no price levels.

**Forward window.** Data after the 2026-10-23 close. LEV-B3 and V3 open at the **2026-10-26 close** ($10k, traded to
the target in force: B3's SMA state at the 10-23 close, V3's exposure decided at the 2026-09-30 month end); month 1
is the partial 2026-10 (month 24 = 2028-09, month 36 = 2029-09). The monthly lines take their first signal at the
**2026-10-30 month end** and open at the next close (2026-11-02, $10k); month 1 is the partial 2026-11 (month 24 =
2028-10, month 36 = 2029-10). Months are calendar months, the first one measured from the opening close.

**Columns.** *strategy*: the month's return of the simulated account (research engines, IBKR costs). *ONEQ*, *QQQ*:
buy and hold from the same opening close (one order, research costs). *excess*: strategy − benchmark (pp).
*cum. excess*: (strategy chained from the start) − (benchmark chained from the start), pp. *t*: monthly-excess t
(mean / sd × √n, shown from n = 3). *status*: `final` once the line itself holds a session of a later month (a
mega-cap line waits for its month-end signal file, BT4b for the BTC close of the session, i.e. 00:00 UTC);
otherwise `provisional`, replaced by the next run. Matched comparisons: LEV-B3 vs **constant 1.37x** (63% QQQ +
37% QLD, monthly; the audit's benchmark); V3 vs **the constant portfolio at V3's average realised exposure ē to
date** (month m uses ē from the start through m's last session); BT4b vs QQQ alone; the mega-cap lines are fully
invested, their matched comparison is QQQ.

**Evaluation (fixed 2026-10-24, before any forward data).** No verdict before 24 months. At 36 months a line is
positive only if its cumulative excess over ONEQ is > 0 **and** its monthly-excess t vs ONEQ is ≥ 2. The matched
comparison is reported alongside and decides whether there is any edge beyond leverage / exposure: only with a
cumulative excess > 0 and t ≥ 2 against it too; otherwise a positive result vs ONEQ is recorded as exposure, not an
edge. No early abandon rule. 36 months has low power for these high-tracking-error lines (checklist B4)."""

INTROS = {
    "M1": "Top 10 Nasdaq common stocks by point-in-time market cap (one class per company), equal weight.",
    "M2": "Top 5 by point-in-time market cap, equal weight.",
    "M3": "Among the top 20 by market cap, the 5 with the best 126-session total return, equal weight.",
    "M6": "Top 10 by market cap, weighted by market cap (a sanity line: should track QQQ).",
    "LEV-B3": ("QQQ adjusted close vs its 200-session SMA with a 2% band: on (above 1.02 × SMA) → 50% QQQ + 50% QLD "
               "(1.5x), off (below 0.98 × SMA) → 100% QQQ; traded at the close after a state change and after each "
               "month end. *SMA200 state*: at the month's last session; in parentheses the switches decided in the "
               "month (→on / →off at that close, traded at the next close)."),
    "V3": ("e = min(1.5, σLR / σ20) at each month end (σLR: std of every QQQ daily return since 1999-03-11, σ20: the "
           "last 20, both × √252); e ≥ 1 → (2 − e) QQQ + (e − 1) QLD, e < 1 → e QQQ + T-bill ETF. *target e*: "
           "decided at the previous month end (traded at the month's first close; the first month: the target in "
           "force at the opening close) → decided at this month's end."),
    "BT4b": ("90% QQQ + 10% BTC (Bitstamp close of UTC day S on US session S; IBKR spot crypto costs, Paxos "
             "$0.15 a month), rebalanced at the close after each month end. *IBIT variant*: the BTC leg as IBIT "
             "(stock costs), report only."),
}
MEGA_COMMON = ("Signal at each month's last session from fresh data (Nasdaq listing after the close, SEC filings "
               "before it, Alpaca / Yahoo prices; `state/forward_observation/megacap/m_<date>.csv`), traded at the "
               "next close; 25% rebalance band, IBKR costs. *holdings*: the targets executed in the month.")


def _progress(key: str, rows: list) -> str:
    last = rows[-1]
    t = "" if not np.isfinite(last["t_oneq"]) else f", t {last['t_oneq']:+.2f}"
    tm = "" if not np.isfinite(last["t_match" if key in DAILY else "t_qqq"]) else \
        f", t {last['t_match' if key in DAILY else 't_qqq']:+.2f}"
    cm = last["cum_match" if key in DAILY else "cum_qqq"]
    return (f"- {TITLES[key]}: {last['n']} month(s) through {last['month']}; cumulative excess vs ONEQ "
            f"{_fmt_pp(last['cum_oneq'])} pp{t}; vs {MATCH[key]} {_fmt_pp(cm)} pp{tm}; no verdict before month "
            f"{VERDICT_MONTHS[0]}, verdict at month {VERDICT_MONTHS[1]}.")


def status_line(key: str, v: dict) -> str:
    if v.get("error"):
        return f"- {TITLES[key]}: **not computed this run** ({v['error'].splitlines()[0][:160]}); earlier rows kept."
    if not v.get("started"):
        why = v.get("reason", "")
        if key in DAILY:
            head = f"**opens at the {START_CLOSE} close**; no forward month yet."
        else:
            head = (f"**observation starts at the {FIRST_SIGNAL} month-end signal**, executed at the next close; "
                    "no forward month yet.")
        extra = "" if why in ("", "before_start", "before_first_signal") else f" Pending: {why}."
        return f"- {TITLES[key]}: {head}{extra}"
    tail = ""
    if v.get("pending") is not None:
        tail = f" Pending: signal {v['pending'].date()} not computed (run --fetch-smisp)."
    if v.get("waiting_for"):
        tail += f" Waiting for the bars of {', '.join(v['waiting_for'])}."
    return _progress(key, v["rows"]) + tail


def render(b4res: dict, old_text: str = "") -> tuple[str, list]:
    """The B4 part of the log: status, then one table per line (existing rows kept, rows computed now replace
    them; a final row that changes is reported in the warnings, as ``runner.section``)."""
    from quant.observation.runner import parse_rows
    old = parse_rows(old_text)
    warnings = []
    lines = b4res["lines"]
    parts = [B4_INTRO, "", "### B4 status", "", f"- As-of date of the B4 run: {b4res['as_of']}."]
    parts += [status_line(k, lines.get(k, {"started": False})) for k in KEYS]
    parts.append("")
    for key in KEYS:
        head = COLS["mega"] if key in MEGA else COLS[key]
        rows = dict(old.get(key, {}))
        for x in lines.get(key, {}).get("rows", []) if not lines.get(key, {}).get("error") else []:
            new = row_line(key, x)
            prev = rows.get(x["month"])
            if prev is not None and prev != new and prev.rstrip().endswith("| final |"):
                warnings.append(f"{key} {x['month']}: a final row changed (vendor data revision?)\n  old {prev}\n"
                                f"  new {new}")
            rows[x["month"]] = new
        intro = INTROS[key] + (" " + MEGA_COMMON if key in MEGA else "")
        sep = "|" + "---|" * (head.count("|") - 1)
        parts.append(f"### {TITLES[key]}\n\n{intro}\n\n<!-- table:{key}:start -->\n{head}\n{sep}\n"
                     + "".join(rows[mo] + "\n" for mo in sorted(rows)) + f"<!-- table:{key}:end -->\n")
    return "\n".join(parts), warnings


def line_ends(b4res: dict) -> dict:
    """{table key: last session of that line} for the log-status check."""
    return {k: v["last_session"] for k, v in b4res["lines"].items() if v.get("started")}


# ======================================================================== details (local) and replay numbers

def _by_date(s: pd.Series | None) -> dict:
    return {} if s is None else {str(i.date()): float(x) for i, x in s.dropna().items()}


def series_of(b4res: dict) -> dict:
    """{'<line>.<kind>': {date: daily return}} of every started line (returns only, no price levels). V3's
    comparison is month-level (its ē uses the month to date), so it is named 'hmatch' like the B1 / B2 H_match."""
    out = {}
    for k, v in b4res["lines"].items():
        if not v.get("started"):
            continue
        out[f"{k}.ret"] = _by_date(v["ret"])
        out[f"{k}.oneq"] = _by_date(v["oneq"])
        out[f"{k}.qqq"] = _by_date(v["qqq"])
        if k == "LEV-B3":
            out[f"{k}.match"] = _by_date(v["match"])
        if k == "V3":
            out[f"{k}.hmatch"] = _by_date(v["match"])
        if k == "BT4b" and v.get("ibit") is not None:
            out[f"{k}.ibit"] = _by_date(v["ibit"])
    return out


def run_details(b4res: dict) -> dict:
    out = {"as_of": b4res["as_of"], "series": series_of(b4res), "lines": {}}
    for k, v in b4res["lines"].items():
        if not v.get("started"):
            out["lines"][k] = {"started": False, "reason": v.get("reason"), "error": v.get("error")}
            continue
        d = {"last_session": str(v["last_session"].date())}
        if k in MEGA:
            d["targets"] = {str(s.date()): [[v["tick"].get(sid, sid), w] for sid, w, *_ in t]
                            for s, t in v["targets"].items()}
        elif k == "LEV-B3":
            d["state"] = _by_date(v["state"].iloc[v["s_i"] - 1: v["end_i"] + 1])
            d["exposure"] = _by_date(v["exposure"])
        elif k == "V3":
            d["e_t"] = _by_date(v["e_t"].iloc[v["s_i"] - 1: v["end_i"] + 1])
            d["exposure"] = _by_date(v["exposure"])
            d["ebar"] = v["ebar"]
        else:
            d["btc_weight"] = _by_date(v["btc_weight"])
            d["costs"] = {"trades": v["sim"]["trades"], "cost_dollars": v["sim"]["cost_dollars"],
                          "paxos_fee_dollars": v["sim"]["fee_dollars"]}
        out["lines"][k] = d
    return out


# ======================================================================== fetch (BTC, T-bill, mega-cap signals, charts)

def fetch(b4_dir=None, getter=None, sleep=time.sleep, pacer=None) -> dict:
    """BTC (four sources) and the T-bill (FRED DTB3). QLD / IBIT are fetched by ``runner.fetch(b4_data.ETFS)``.
    ``pacer``: the run's shared Yahoo pace and stop flag (Yahoo BTC-USD)."""
    p = paths(b4_dir)
    out = {"btc": bdata.fetch_btc(p["b4_dir"], getter=getter, sleep=sleep, pacer=pacer)}
    try:
        bdata.fetch_dtb3(p["b4_dir"], getter)
        out["dtb3"] = "ok"
    except Exception as exc:  # noqa: BLE001 - the previous file stays
        out["dtb3"] = f"error {type(exc).__name__}"
    return out


def megacap_fetch(sessions: pd.DatetimeIndex, b4_dir=None, state_dir=None, smisp_dir=None, getters=None,
                  sleep=time.sleep) -> dict:
    """Every due mega-cap signal (after the S-MISP fetch of the same run filled its signal folder), then the charts
    of the target names that can still change a row. Returns {"signals": {signal: 'ok'}, "notes": [...]} (a signal
    computed now without the real-close check, a signal that stopped)."""
    p = paths(b4_dir, state_dir, smisp_dir)
    sigs, notes = {}, []
    for s in fs.signal_dates(sessions, FIRST_SIGNAL):
        new = not bm.signal_path(s, p["state_dir"]).exists()
        try:
            sc = bm.prepare_signal(s, sessions, p["smisp_dir"], p["state_dir"], fetch=True, getters=getters,
                                   sleep=sleep)
        except (RuntimeError, OSError) as exc:      # network / SEC errors: nothing frozen; the next run resumes
            print(f"mega-cap signal {s.date()}: {exc}")
            notes.append(f"mega-cap signal {s.date()}: {type(exc).__name__} (the next run resumes)")
            sc = None
        if sc is None:
            break
        sigs[s] = sc
        meta = bm.signal_path(s, p["state_dir"]).with_name(f"m_{s.date()}_meta.json")
        check = json.loads(meta.read_text()).get("real_close_check") if new and meta.exists() else None
        if check and check != "checked":
            notes.append(f"mega-cap signal {s.date()}: real close at the signal {check}")
    if sigs:
        p["held_dir"].mkdir(parents=True, exist_ok=True)
        print("mega-cap held charts:", bm.fetch_held(sigs, p["held_dir"], getters, sleep, sessions=sessions))
    return {"signals": {str(s.date()): "ok" for s in sigs}, "notes": notes}


def scratch_state(state_dir=None, b4_dir=None) -> Path:
    """A local copy of the committed mega-cap state (frozen signals, first listings) for a dry run: a due signal it
    computes and the listings it records stay in the cache (``b4/dry_run_megacap_state``), never in the
    working tree, so a later ``git pull`` of the job's commit is not blocked."""
    src = Path(state_dir or bm.STATE)
    dst = Path(b4_dir or bdata.cache_dir()) / "dry_run_megacap_state"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    if src.exists():
        for f in src.iterdir():
            if f.is_file():
                shutil.copy2(f, dst / f.name)
    return dst


# ======================================================================== the log file: the B4 part

B4_MARKER = B4_INTRO.splitlines()[0]


def split_log(text: str) -> tuple[str, str]:
    """(B1-B3 part, B4 part) of a log text; the B4 part starts at its heading (empty when there is none)."""
    k = (text or "").find("\n" + B4_MARKER)
    return (text, "") if k < 0 else (text[:k + 1], text[k + 1:])
