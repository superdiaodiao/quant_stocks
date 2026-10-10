"""Fundamental-factor composites on the point-in-time Nasdaq panel: SEC annual facts, point-in-time company states,
accounting / market factors, z-score composites and the investable universes.

Extracted unchanged from scripts/research_fundamentals.py (the data assembly ``extract_all`` / ``build_states`` /
``asof_states``, the factors ``accounting_factors`` / ``market_factors`` / ``composites`` and the universe helpers
``load_all`` / ``sic_map`` / ``universes`` / ``reject_small_mcaps``). Also used by research_ml_cross_section and the
S-MISP forward signal (quant.observation.smisp).
"""
from __future__ import annotations

import gzip
import json
import math
from datetime import date
from functools import lru_cache

import numpy as np
import pandas as pd

from quant.data import eps as pit_eps
from quant.data import panel as stock_panel
from quant.data import version as dv
from quant.paths import CACHE_ROOT
from quant.strategies import canslim as cs

FCACHE = dv.versioned(CACHE_ROOT / "fundamentals")
SECFACTS = dv.CACHE / "prefilter/security_facts.csv.gz"
SUBMISSIONS = dv.CACHE / "raw/sec/submissions"
PRICE_START, PERF_START, END = "2011-06-01", "2014-01-01", "2026-08-31"
MAX_FY_AGE = 550
MAX_Q_AGE = 200
MIN_MCAP_WIDE = 3e8
MIN_PRICE_WIDE = 5.0
INT_COV_CAP = 1000.0
SMALL_MCAP_RATIO = 30.0
FLOW = {  # annual duration facts (USD unless noted)
    "rev": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet",
            "SalesRevenueServicesNet"),
    "cogs": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices",
             "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"),
    "gross": ("GrossProfit",),
    "oi": ("OperatingIncomeLoss",),
    "ni": ("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"),
    "da": ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
           "DepreciationAmortizationAndAccretionNet", "Depreciation"),
    "rd": ("ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
    "int": ("InterestExpense", "InterestExpenseDebt", "InterestExpenseNonoperating"),
    "pti": ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
    "cfo": ("NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
    "div": ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
    "buy": ("PaymentsForRepurchaseOfCommonStock",),
}
FLOW_PS = {"eps": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic")}
FLOW_SH = {"sh": ("WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic")}
STOCK = {  # instants (USD)
    "ta": ("Assets",), "ca": ("AssetsCurrent",), "cl": ("LiabilitiesCurrent",), "tl": ("Liabilities",),
    "be": ("StockholdersEquity",), "be_nci": ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",),
    "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
             "Cash"),
    "debt_cur": ("DebtCurrent",), "ltd_cur": ("LongTermDebtCurrent",), "stb": ("ShortTermBorrowings",),
    "ltd_nc": ("LongTermDebtNoncurrent",), "ltd_all": ("LongTermDebt",),
    "re": ("RetainedEarningsAccumulatedDeficit",), "ar": ("AccountsReceivableNetCurrent",), "inv": ("InventoryNet",),
    "ap": ("AccountsPayableCurrent",), "tp": ("TaxesPayableCurrent", "AccruedIncomeTaxesCurrent"),
}
EPS_Q = pit_eps.EPS_CONCEPTS
ANNUAL_FORMS = ("10-K",)          # 10-K, 10-K/A, 10-K405, 10-KT ...
FY0_GROUPS = ("ni", "rev", "oi", "cfo")
FAMILIES = {
    "value": ("ep", "bm", "sp", "cfp", "ebit_ev", "fcf_yield", "payout_yield"),
    "profitability": ("roe", "roa", "roic", "gpa", "opa", "gross_margin", "op_margin", "cbop"),
    "growth": ("sales_g1", "sales_g3", "eps_g1", "eps_g3", "q_eps_g", "opinc_g1"),
    "quality": ("accruals", "sloan", "cash_conv", "piotroski", "altman_z", "earn_var", "noa"),
    "investment": ("asset_g", "capex_assets", "share_iss"),
    "leverage": ("de", "nd_ebitda", "current_ratio", "int_cov"),
    "efficiency": ("asset_turn", "d_asset_turn", "rd_sales", "rd_mcap"),
    "size": ("size",),
}
SIGN = {f: 1 for fam in FAMILIES.values() for f in fam}
FACTORS = tuple(f for fam in FAMILIES.values() for f in fam)
COMPOSITE_FAMILIES = tuple(k for k in FAMILIES if k != "size")
COMPOSITES = tuple(f"C_{k}" for k in COMPOSITE_FAMILIES) + ("C_ALL",)


def _payloads(cik: int) -> list:
    out = []
    for d in pit_eps.CF_DIRS:
        p = d / f"CIK{int(cik):010d}.json.gz"
        if p.is_file():
            env = json.loads(gzip.decompress(p.read_bytes()))
            out.append(env.get("payload", env))
    return out


def facts_from_payload(cik: int, payload: dict) -> tuple[list, list]:
    """(annual rows, quarterly-EPS rows) of one companyfacts payload.
    annual rows: (cik, group, prio, kind, end, filed, val) with kind 'F' (annual duration) or 'S' (instant), 10-K forms.
    EPS rows follow ``research_canslim_dev.extract_eps_facts`` (every form, durations kept for eps_states)."""
    gaap = payload.get("facts", {}).get("us-gaap", {})
    rows, eps = [], []

    def scan(groups: dict, unit_want: str, kind: str):
        for group, concepts in groups.items():
            for prio, concept in enumerate(concepts):
                for unit, items in gaap.get(concept, {}).get("units", {}).items():
                    if unit != unit_want:
                        continue
                    for f in items:
                        if f.get("val") is None or not f.get("filed") or not f.get("end"):
                            continue
                        if not str(f.get("form", "")).startswith(ANNUAL_FORMS):
                            continue
                        if kind == "F":
                            if "start" not in f:
                                continue
                            dur = (pd.Timestamp(f["end"]) - pd.Timestamp(f["start"])).days
                            if not 340 <= dur <= 390:
                                continue
                        elif "start" in f:
                            continue
                        rows.append((int(cik), group, prio, kind, f["end"], f["filed"], float(f["val"])))

    scan(FLOW, "USD", "F")
    scan(FLOW_PS, "USD/shares", "F")
    scan(FLOW_SH, "shares", "F")
    scan(STOCK, "USD", "S")
    for pri, concept in enumerate(EPS_Q):
        for unit, items in gaap.get(concept, {}).get("units", {}).items():
            if unit != "USD/shares":
                continue
            for f in items:
                if "start" not in f or f.get("val") is None:
                    continue
                eps.append((int(cik), concept, pri, f["start"], f["end"], float(f["val"]), f["filed"],
                            f.get("form", ""), f.get("accn", "")))
    return rows, eps


ANNUAL_COLS = ["cik", "group", "prio", "kind", "end", "filed", "val"]
EPS_COLS = ["cik", "concept", "priority", "start", "end", "val", "filed", "form", "accn"]


def extract_all(ciks, refresh: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Annual facts and point-in-time quarterly EPS states for every CIK (all local companyfacts copies merged,
    duplicates dropped). Cached under research_cache/fundamentals/."""
    FCACHE.mkdir(parents=True, exist_ok=True)
    fa, fe, fd = FCACHE / "annual_facts.csv.gz", FCACHE / "eps_states.csv.gz", FCACHE / "annual_facts.ciks.json"
    want = sorted(c for c in set(int(c) for c in ciks) if pit_eps._cf_path(c) is not None)
    if not refresh and fa.exists() and fe.exists() and fd.exists() and set(want) <= set(json.loads(fd.read_text())):
        return (pd.read_csv(fa, dtype={"end": str, "filed": str}),
                pd.read_csv(fe, dtype={"filed": str, "avail": str, "q_end": str, "q_ya_end": str, "fy0_end": str}))
    ann, states = [], []
    for k, cik in enumerate(want):
        rows, eps = [], []
        for pl in _payloads(cik):
            r, e = facts_from_payload(cik, pl)
            rows += r
            eps += e
        if rows:
            ann.append(pd.DataFrame(rows, columns=ANNUAL_COLS).drop_duplicates(["group", "prio", "kind", "end",
                                                                                "filed", "val"]))
        if eps:
            ef = pd.DataFrame(eps, columns=EPS_COLS).drop_duplicates(["concept", "start", "end", "val", "filed"])
            st = pit_eps.eps_states(ef)
            if len(st):
                states.append(st)
        if k % 250 == 0:
            print(f"  companyfacts: {k}/{len(want)} CIKs read", flush=True)
    a = pd.concat(ann, ignore_index=True)
    s = pd.concat(states, ignore_index=True)
    a.to_csv(fa, index=False, compression="gzip")
    s.to_csv(fe, index=False, compression="gzip")
    fd.write_text(json.dumps(want))
    (FCACHE / "factor_states.csv.gz").unlink(missing_ok=True)     # derived from the annual facts: rebuild
    return a, s


@lru_cache(maxsize=None)
def _ord(d: str) -> int:
    return date.fromisoformat(str(d)[:10]).toordinal()


def _days(a, b) -> int:
    return _ord(b) - _ord(a)


def pick(known: dict, group: str, end: str):
    """Value of ``group`` for period ``end``: the first concept by priority among values already filed."""
    vals = known.get((group, end))
    if not vals:
        return None
    return vals[min(vals)]


def pick_stock(known: dict, group: str, fy_end: str, ends_by_group: dict):
    """Instant value of ``group`` dated within 7 days of ``fy_end`` (closest end wins)."""
    best = None
    for e in ends_by_group.get(group, ()):
        d = abs(_days(e, fy_end))
        if d <= 7 and (best is None or d < best[0]):
            best = (d, e)
    return None if best is None else pick(known, group, best[1])


def year_inputs(known: dict, fy_end: str, ends_by_group: dict) -> dict:
    """Raw inputs of one fiscal year (section 1.3 rules: first concept by priority; derived items)."""
    f = {g: pick(known, g, fy_end) for g in list(FLOW) + list(FLOW_PS) + list(FLOW_SH)}
    s = {g: pick_stock(known, g, fy_end, ends_by_group) for g in STOCK}
    x = {**f, **s}
    rev_ = x["rev"]
    gross = x["gross"]
    if gross is None and rev_ is not None and x["cogs"] is not None:
        gross = rev_ - x["cogs"]
    if gross is not None and rev_ is not None and gross > rev_ * 1.0001:
        gross = None
    x["gp"] = gross
    x["be_any"] = x["be"] if x["be"] is not None else x["be_nci"]
    if x["tl"] is None and x["ta"] is not None:
        eq = x["be_nci"] if x["be_nci"] is not None else x["be"]
        x["tl"] = None if eq is None else x["ta"] - eq
    if x["debt_cur"] is not None:
        std = x["debt_cur"]
    else:
        std = (x["ltd_cur"] or 0.0) + (x["stb"] or 0.0)
    x["std"] = std
    if x["ltd_nc"] is not None:
        x["ltd"] = x["ltd_nc"]
        x["debt"] = x["ltd_nc"] + std
    else:
        x["ltd"] = x["ltd_all"] or 0.0
        x["debt"] = (x["ltd_all"] or 0.0) + (x["stb"] or 0.0)
    x["cash0"] = x["cash"] or 0.0
    x["da0"] = x["da"] or 0.0
    x["ebit"] = x["oi"] if x["oi"] is not None else (
        x["pti"] + (x["int"] or 0.0) if x["pti"] is not None else None)
    return x


def _ratio(a, b, pos_den: bool = True):
    if a is None or b is None or not np.isfinite(a) or not np.isfinite(b):
        return np.nan
    if pos_den and b <= 0:
        return np.nan
    if b == 0:
        return np.nan
    return a / b


def _growth_abs(a, b):
    if a is None or b is None or b == 0:
        return np.nan
    return (a - b) / abs(b)


def accounting_factors(ys: list) -> dict:
    """Factor inputs that need no market data. ``ys`` = [FY0, FY-1, ..., FY-4] input dicts (None when unknown).
    Market factors are completed at the signal date (``market_factors``); this returns their accounting parts."""
    y0 = ys[0]
    y1 = ys[1] if len(ys) > 1 else None
    y3 = ys[3] if len(ys) > 3 else None
    g = lambda y, k: None if y is None else y.get(k)  # noqa: E731
    ta0, ta1 = y0["ta"], g(y1, "ta")
    avg_ta = (ta0 + ta1) / 2 if ta0 is not None and ta1 is not None else ta0
    out = {}
    # ---- market-factor numerators (completed later)
    out["ni0"], out["be0"], out["rev0"], out["cfo0"] = y0["ni"], y0["be_any"], y0["rev"], y0["cfo"]
    out["ebit0"], out["debt0"], out["cash0"], out["rd0"], out["tl0"] = y0["ebit"], y0["debt"], y0["cash0"], y0["rd"], y0["tl"]
    out["fcf0"] = None if y0["cfo"] is None else y0["cfo"] - (y0["capex"] or 0.0)
    out["payout0"] = (y0["div"] or 0.0) + (y0["buy"] or 0.0)
    # ---- profitability
    out["roe"] = _ratio(y0["ni"], y0["be_any"])
    out["roa"] = _ratio(y0["ni"], ta0)
    ic = None if y0["ebit"] is None or y0["be_any"] is None else y0["debt"] + y0["be_any"] - y0["cash0"]
    out["roic"] = _ratio(y0["ebit"], ic)
    out["gpa"] = _ratio(y0["gp"], ta0)
    out["opa"] = _ratio(y0["oi"], ta0)
    out["gross_margin"] = _ratio(y0["gp"], y0["rev"])
    om = _ratio(y0["oi"], y0["rev"])
    out["op_margin"] = om if not (np.isfinite(om) and om > 1.0) else np.nan    # OI above revenue: tagging error
    if y0["oi"] is not None and ta0 is not None and ta0 > 0:
        d = lambda k: (y0[k] - y1[k]) if (y1 is not None and y0[k] is not None and y1[k] is not None) else 0.0  # noqa
        out["cbop"] = (y0["oi"] + y0["da0"] + (y0["rd"] or 0.0) - d("ar") - d("inv") + d("ap")) / ta0
    else:
        out["cbop"] = np.nan
    # ---- growth
    r1 = g(y1, "rev")
    out["sales_g1"] = y0["rev"] / r1 - 1 if (y0["rev"] is not None and r1 is not None and r1 > 0) else np.nan
    r3 = g(y3, "rev")
    out["sales_g3"] = ((y0["rev"] / r3) ** (1 / 3) - 1 if (y0["rev"] is not None and r3 is not None and r3 > 0
                                                         and y0["rev"] > 0) else np.nan)
    out["eps_g1"] = _growth_abs(y0["eps"], g(y1, "eps"))
    out["eps_g3"] = _growth_abs(y0["eps"], g(y3, "eps"))
    out["opinc_g1"] = _growth_abs(y0["oi"], g(y1, "oi"))
    # ---- quality / accruals
    out["accruals"] = ((y0["ni"] - y0["cfo"]) / avg_ta if (y0["ni"] is not None and y0["cfo"] is not None and avg_ta
                                                          and avg_ta > 0) else np.nan)
    if (y1 is not None and None not in (y0["ca"], y1["ca"], y0["cl"], y1["cl"]) and avg_ta and avg_ta > 0):
        dca = y0["ca"] - y1["ca"]
        dcash = y0["cash0"] - y1["cash0"]
        dcl = y0["cl"] - y1["cl"]
        dstd = y0["std"] - y1["std"]
        dtp = (y0["tp"] or 0.0) - (y1["tp"] or 0.0)
        out["sloan"] = ((dca - dcash) - (dcl - dstd - dtp) - y0["da0"]) / avg_ta
    else:
        out["sloan"] = np.nan
    out["cash_conv"] = y0["cfo"] / y0["ni"] if (y0["cfo"] is not None and y0["ni"] is not None and y0["ni"] > 0) \
        else np.nan
    out["piotroski"] = piotroski(y0, y1)
    if None not in (y0["ca"], y0["cl"], y0["re"], y0["ebit"], y0["rev"], ta0) and ta0 > 0:
        out["altman_part"] = (1.2 * (y0["ca"] - y0["cl"]) / ta0 + 1.4 * y0["re"] / ta0 + 3.3 * y0["ebit"] / ta0
                              + 1.0 * y0["rev"] / ta0)
    else:
        out["altman_part"] = np.nan
    roas = [y["ni"] / y["ta"] for y in ys if y is not None and y["ni"] is not None and y["ta"] and y["ta"] > 0]
    out["earn_var"] = float(np.std(roas, ddof=1)) if len(roas) >= 3 else np.nan
    if ta1 is not None and ta1 > 0 and ta0 is not None and y0["be_any"] is not None:
        oa = ta0 - y0["cash0"]
        ol = ta0 - y0["debt"] - y0["be_any"]
        out["noa"] = (oa - ol) / ta1
    else:
        out["noa"] = np.nan
    # ---- investment
    out["asset_g"] = ta0 / ta1 - 1 if (ta0 is not None and ta1 is not None and ta1 > 0) else np.nan
    out["capex_assets"] = _ratio(y0["capex"], ta0)
    sh0, sh1 = y0["sh"], g(y1, "sh")
    out["share_iss"] = math.log(sh0 / sh1) if (sh0 and sh1 and sh0 > 0 and sh1 > 0) else np.nan
    # ---- leverage / liquidity
    out["de"] = _ratio(y0["debt"], y0["be_any"])
    ebitda = None if y0["ebit"] is None else y0["ebit"] + y0["da0"]
    out["nd_ebitda"] = _ratio(y0["debt"] - y0["cash0"], ebitda)
    out["current_ratio"] = _ratio(y0["ca"], y0["cl"])
    if y0["int"] is None or y0["ebit"] is None:
        out["int_cov"] = np.nan
    elif y0["int"] > 0:
        out["int_cov"] = min(y0["ebit"] / y0["int"], INT_COV_CAP)
    else:
        out["int_cov"] = INT_COV_CAP if y0["ebit"] > 0 else np.nan
    # ---- efficiency
    out["asset_turn"] = _ratio(y0["rev"], ta0)
    at1 = _ratio(g(y1, "rev"), ta1)
    out["d_asset_turn"] = out["asset_turn"] - at1 if np.isfinite(out["asset_turn"]) and np.isfinite(at1) else np.nan
    out["rd_sales"] = _ratio(y0["rd"], y0["rev"])
    return out


def piotroski(y0: dict, y1: dict | None) -> float:
    """Piotroski (2000) F-score; at least 7 of the 9 signals computable, scaled by 9 / computable."""
    sig = []
    roa0 = _ratio(y0["ni"], y0["ta"])
    sig.append(None if not np.isfinite(roa0) else roa0 > 0)
    sig.append(None if y0["cfo"] is None else y0["cfo"] > 0)
    roa1 = _ratio(y1["ni"], y1["ta"]) if y1 else np.nan
    sig.append(roa0 > roa1 if np.isfinite(roa0) and np.isfinite(roa1) else None)
    sig.append(y0["cfo"] > y0["ni"] if y0["cfo"] is not None and y0["ni"] is not None else None)
    if y1 and y0["ta"] and y1["ta"] and y0["ta"] > 0 and y1["ta"] > 0:
        l0, l1 = y0["ltd"] / y0["ta"], y1["ltd"] / y1["ta"]
        sig.append(l0 < l1 or (y0["ltd"] == 0 and y1["ltd"] == 0))
    else:
        sig.append(None)
    cr0 = _ratio(y0["ca"], y0["cl"])
    cr1 = _ratio(y1["ca"], y1["cl"]) if y1 else np.nan
    sig.append(cr0 > cr1 if np.isfinite(cr0) and np.isfinite(cr1) else None)
    sig.append(y0["sh"] <= y1["sh"] if y1 and y0["sh"] and y1["sh"] else None)
    gm0 = _ratio(y0["gp"], y0["rev"])
    gm1 = _ratio(y1["gp"], y1["rev"]) if y1 else np.nan
    sig.append(gm0 > gm1 if np.isfinite(gm0) and np.isfinite(gm1) else None)
    at0 = _ratio(y0["rev"], y0["ta"])
    at1 = _ratio(y1["rev"], y1["ta"]) if y1 else np.nan
    sig.append(at0 > at1 if np.isfinite(at0) and np.isfinite(at1) else None)
    known = [bool(s) for s in sig if s is not None]
    if len(known) < 7:
        return np.nan
    return sum(known) * 9.0 / len(known)


def fiscal_years(known: dict) -> list:
    """[FY0, FY-1, ..., FY-4] period ends: FY0 = latest annual end among FY0_GROUPS; FY-k = the end of an annual fact
    of those groups closest to FY0 - k years (within 20 days), else None."""
    ends = sorted({e for (g, e) in known if g in FY0_GROUPS})
    if not ends:
        return []
    fy0 = ends[-1]
    out = [fy0]
    t0 = _ord(fy0)
    for k in range(1, 5):
        target = t0 - round(365.25 * k)
        cand = [(abs(_ord(e) - target), e) for e in ends]
        best = min(cand)
        out.append(best[1] if best[0] <= 20 else None)
    return out


def company_states(facts: pd.DataFrame) -> pd.DataFrame:
    """One row per filing date: the accounting factors of the latest fiscal year known from facts filed up to that
    date (usable strictly later). For one (group, concept, period) the most recently filed value wins."""
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
        fys = fiscal_years(known)
        if not fys:
            continue
        ys = [year_inputs(known, e, ends_by_group) if e is not None else None for e in fys]
        row = {"cik": cik, "filed": filed, "fy0_end": fys[0]}
        row.update(accounting_factors(ys))
        rows.append(row)
    return pd.DataFrame(rows)


def build_states(annual: pd.DataFrame, refresh: bool = False) -> pd.DataFrame:
    cache = FCACHE / "factor_states.csv.gz"
    if cache.exists() and not refresh:
        return pd.read_csv(cache, dtype={"filed": str, "fy0_end": str})
    parts = []
    for k, (cik, g) in enumerate(annual.groupby("cik")):
        st = company_states(g)
        if len(st):
            parts.append(st)
        if k % 500 == 0:
            print(f"  states: {k} CIKs", flush=True)
    out = pd.concat(parts, ignore_index=True)
    out.to_csv(cache, index=False, compression="gzip")
    return out


def asof_states(rows: pd.DataFrame, states: pd.DataFrame, eps: pd.DataFrame) -> pd.DataFrame:
    """For each (s, cik) row: the latest factor state filed strictly before s (dropped when FY0 ended more than
    MAX_FY_AGE days before s) and the latest quarterly-EPS state with avail strictly before s (q_end within
    MAX_Q_AGE days). Asserted point in time."""
    left = rows.copy()
    left["cik_i"] = pd.to_numeric(left["cik"], errors="coerce")
    left = left[left["cik_i"].notna()].copy()
    left["cik_i"] = left["cik_i"].astype("int64")
    left["s"] = pd.to_datetime(left["s"]).astype("datetime64[ns]")
    left = left.sort_values("s")
    st = states.copy()
    st["usable_from"] = (pd.to_datetime(st["filed"]) + pd.Timedelta(days=1)).astype("datetime64[ns]")
    st["cik_i"] = st["cik"].astype("int64")
    st = st.drop(columns=["cik"]).sort_values("usable_from")
    m = pd.merge_asof(left, st, left_on="s", right_on="usable_from", by="cik_i", direction="backward")
    used = m["filed"].notna()
    if not (pd.to_datetime(m.loc[used, "filed"]) < m.loc[used, "s"]).all():
        raise cs.DateGuardError("annual fact used on or before its filing date")
    stale = ~((m["s"] - pd.to_datetime(m["fy0_end"])).dt.days <= MAX_FY_AGE)
    acc_cols = [c for c in st.columns if c not in ("usable_from", "cik_i", "filed", "fy0_end")]
    m.loc[stale, acc_cols] = np.nan
    e = eps[["cik", "avail", "q_end", "c_growth"]].copy()
    e = e[e["avail"].notna()]
    e["usable_from"] = (pd.to_datetime(e["avail"]) + pd.Timedelta(days=1)).astype("datetime64[ns]")
    e["cik_i"] = e["cik"].astype("int64")
    e = e.drop(columns=["cik"]).rename(columns={"avail": "eps_avail"}).sort_values("usable_from")
    m = m.sort_values("s")
    m = pd.merge_asof(m, e.rename(columns={"usable_from": "eps_usable_from"}), left_on="s",
                      right_on="eps_usable_from", by="cik_i", direction="backward")
    ok = m["eps_avail"].notna()
    if not (pd.to_datetime(m.loc[ok, "eps_avail"]) < m.loc[ok, "s"]).all():
        raise cs.DateGuardError("quarterly EPS used on or before its availability date")
    fresh = (m["s"] - pd.to_datetime(m["q_end"])).dt.days <= MAX_Q_AGE
    m["q_eps_g"] = m["c_growth"].where(fresh)
    return m


def market_factors(m: pd.DataFrame) -> pd.DataFrame:
    """Complete the market-based factors with the point-in-time market cap ``mcap`` (NaN when unknown)."""
    mcap = m["mcap"].where(m["mcap"] > 0)
    m["ep"] = m["ni0"] / mcap
    m["bm"] = (m["be0"] / mcap).where(m["be0"] > 0)
    m["sp"] = m["rev0"] / mcap
    m["cfp"] = m["cfo0"] / mcap
    ev = mcap + m["debt0"] - m["cash0"]
    m["ebit_ev"] = (m["ebit0"] / ev).where(ev > 0)
    m["fcf_yield"] = m["fcf0"] / mcap
    m["payout_yield"] = m["payout0"] / mcap
    m["altman_z"] = m["altman_part"] + 0.6 * (mcap / m["tl0"]).where(m["tl0"] > 0)
    m["rd_mcap"] = m["rd0"] / mcap
    m["size"] = np.log(mcap)
    for f in FACTORS:
        m[f] = pd.to_numeric(m[f], errors="coerce").replace([np.inf, -np.inf], np.nan)
    return m


def zscores(panel: pd.DataFrame, cols) -> pd.DataFrame:
    """Per signal date: signed value -> cross-sectional percentile rank -> standardised (mean 0, sd 1)."""
    out = pd.DataFrame(index=panel.index)
    for f in cols:
        v = panel[f] * SIGN[f]
        r = v.groupby(panel["s"]).rank(pct=True)
        mu = r.groupby(panel["s"]).transform("mean")
        sd = r.groupby(panel["s"]).transform("std")
        out[f] = (r - mu) / sd
    return out


def composites(panel: pd.DataFrame) -> pd.DataFrame:
    z = zscores(panel, FACTORS)
    out = pd.DataFrame(index=panel.index)
    for fam in COMPOSITE_FAMILIES:
        cols = list(FAMILIES[fam])
        need = math.ceil(len(cols) / 2)
        n = z[cols].notna().sum(axis=1)
        out[f"C_{fam}"] = z[cols].mean(axis=1).where(n >= need)
    fams = [f"C_{k}" for k in COMPOSITE_FAMILIES]
    n = out[fams].notna().sum(axis=1)
    out["C_ALL"] = out[fams].mean(axis=1).where(n >= 4)
    return out


def load_all() -> stock_panel.WinData:
    """``quant.data.panel.load_window`` with every panel security kept (not only the top-300 names)."""
    name = "fundamentals"
    window = {"perf_start": PERF_START, "perf_end": END, "price_start": PRICE_START,
              "universe_start": "2012-01-01", "judged_from": PERF_START}
    # keep every panel id: give load_window a universe frame listing all of them by wrapping read_csv once
    real_read = pd.read_csv

    def read_csv(path, *a, **k):
        df = real_read(path, *a, **k)
        if str(path).endswith("weekly_universe_top300.csv.gz"):
            ids = real_read(dv.CACHE / "universe/weekly_liquidity.csv.gz", usecols=["security_id"],
                            dtype=str)["security_id"].unique()
            extra = sorted(set(ids) - set(df["security_id"]))
            pad = pd.DataFrame({"security_id": extra, "week_end": "2012-01-06"})
            df = pd.concat([df, pad], ignore_index=True)
            df["_pad"] = df["security_id"].isin(extra) & df["ticker"].isna()
        return df

    stock_panel.pd.read_csv = read_csv
    try:
        data = stock_panel.load_window(name, window)
    finally:
        stock_panel.pd.read_csv = real_read
    data.universe = data.universe[~data.universe["_pad"].astype(bool)].drop(columns=["_pad"])
    return data


def sic_map(top: pd.DataFrame) -> dict:
    """CIK -> current SIC from local SEC submissions files (used only where the top-300 file has no SIC)."""
    out = {}
    for c in sorted(set(int(x) for x in top)):
        p = SUBMISSIONS / f"CIK{c:010d}.json.gz"
        if not p.is_file():
            continue
        try:
            d = json.loads(gzip.decompress(p.read_bytes()))
            d = d.get("payload", d)
            if str(d.get("sic", "")).strip():
                out[c] = int(d["sic"])
        except Exception:  # noqa: BLE001
            continue
    return out


def reject_small_mcaps(c: pd.DataFrame, factor: float = SMALL_MCAP_RATIO) -> pd.DataFrame:
    """Data rule 1.1a: a market cap whose market cap / 50-day dollar volume is below 1/``factor`` of that signal's
    median over the top-50 (by dv50 rank) U300 names is a share-unit error (shares filed in thousands): set empty."""
    c = c.copy()
    ratio = c["mcap"] / c["dv50"]
    top = c["in300"] & (c["dv50_rank"] <= 50) & np.isfinite(ratio)
    med = ratio[top].groupby(c.loc[top, "s"]).median()
    lim = c["s"].map(med) / factor
    odd = np.isfinite(ratio) & (ratio < lim)
    c.loc[odd, "mcap"] = np.nan
    c.loc[odd, "mcap_src"] = "rejected_too_small"
    return c


def universes(c: pd.DataFrame) -> dict:
    """U300 and UW (section 1.5), one row per company: non-financial; UW also needs mcap >= $300M and close >= $5."""
    fin = c["sic"].between(6000, 6999)
    out = {}
    u3 = c[c["in300"] & ~fin].copy()
    u3["company"] = u3["multi_class_group"].where(u3["multi_class_group"].notna() & (u3["multi_class_group"] != ""),
                                                  u3["cik"].astype(str))
    u3["company"] = u3["company"].where(u3["company"] != "<NA>", u3["security_id"])
    out["U300"] = u3.sort_values(["s", "company", "dv50_rank", "security_id"]).drop_duplicates(["s", "company"])
    uw = c[~fin & (c["mcap"] >= MIN_MCAP_WIDE) & (c["close_s"] >= MIN_PRICE_WIDE)].copy()
    uw["company"] = uw["cik"].astype(str).where(uw["cik"].notna(), uw["security_id"])
    uw["dvr"] = uw["dv50_rank"].fillna(1e9)
    out["UW"] = uw.sort_values(["s", "company", "dvr", "security_id"]).drop_duplicates(["s", "company"]) \
        .drop(columns=["dvr"])
    return {k: v.reset_index(drop=True) for k, v in out.items()}


def _json(x):
    if isinstance(x, (np.floating, float)):
        return None if not np.isfinite(x) else round(float(x), 6)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return str(x)
