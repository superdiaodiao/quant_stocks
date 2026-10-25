"""A many-angle scorecard of one strategy's return series: ten families of metrics (return, risk-adjusted return,
drawdown pain, tails, consistency, market relationship, investor utility, path experience, practicality,
statistical confidence) in one tidy table. Every metric's definition, formula, plain-Chinese meaning, how to read it
and its caveats are in docs/evaluation_scorecard.md.

The scorecard DESCRIBES a strategy; it is not a pass criterion. The pre-registered A / B rule against ONEQ
(``quant.evaluation.criteria``) stays the only verdict, so a strategy cannot be promoted after the fact by picking
whichever of these numbers looks best.

Pure functions (no files, no network, no module state). Each family function takes a DAILY or MONTHLY return series
(decimal return per period, no NaN, sorted) plus optional inputs and returns ``{key: value}``; ``scorecard`` runs all
ten and returns one row per metric (family, key, value, unit, frequency, one-line Chinese meaning, note);
``compare`` puts several scorecards side by side.

Conventions:

- frequency: ``freq="D"`` (252 sessions a year) or ``"M"`` (12 a year), inferred from the index spacing when not
  given; a monthly series may carry a month-end DatetimeIndex or a monthly PeriodIndex.
- ``rf``: the T-bill return of each period at the strategy's frequency (a daily rf is compounded to months for a
  monthly strategy); it must cover every period. Without it the risk-free rate is 0 (the table says so).
- benchmarks ``oneq`` / ``qqq``: return series (NaNs before inception are dropped). Relative metrics use the periods
  both series have: the common sessions when both are daily, otherwise the common months.
- value paths start at 1.0 before the first return, so a loss in the first period is a drawdown (as
  ``metrics.core_metrics``); drawdown episodes use the tolerance of ``metrics.longest_drawdown`` (more than 1e-12
  below the running peak) so the longest episode is exactly ``longest_drawdown``. Durations: ``longest_underwater``
  counts the periods below the peak (as ``longest_drawdown``); the max-drawdown duration and the recovery times run
  from the peak to the recovery (one period more for a recovered episode).
- a daily series compared with a monthly one: the daily side's partly covered first / last month is dropped.
- distribution, consistency, market, Kelly and t metrics use calendar-month returns (comparable between daily and
  monthly inputs); return, drawdown, Sharpe / Sortino and CRRA metrics use the input frequency.
- bootstrap: stationary bootstrap (``stats.stationary_bootstrap_index``), mean block ``block_months`` months, seeded,
  so a scorecard is reproducible.

Reused unchanged: ``metrics.cagr_of / max_drawdown / monthly / yearly / longest_drawdown / t_and_ir``,
``equity.ulcer_index``, ``criteria.deflated_sharpe_n``, ``stats.t_sf / stationary_bootstrap_index``. The Sharpe,
Sortino, Calmar, beta, monthly t / IR and share-of-years-beating formulas are written in the same floating-point
order as ``metrics.core_metrics`` / ``relative_metrics``, and tests/quant/test_scorecard.py checks they agree exactly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.evaluation.criteria import deflated_sharpe_n
from quant.evaluation.equity import ulcer_index
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, longest_drawdown, max_drawdown, monthly, t_and_ir, yearly
from quant.evaluation.stats import stationary_bootstrap_index, t_sf

NAN = float("nan")
PERIODS = {"D": TRADING_DAYS, "M": 12}
SEED = 20261026
GAMMAS = (1, 2, 3, 5)
# (name, first day, last day): calendar windows, as the owner listed them (2000-02, 2008, 2020-Q1, 2022)
CRISES = (("dotcom_2000_2002", "2000-01-01", "2002-12-31"), ("gfc_2008", "2008-01-01", "2008-12-31"),
          ("covid_2020q1", "2020-01-01", "2020-03-31"), ("bear_2022", "2022-01-01", "2022-12-31"))
CRISIS_EDGE_DAYS = 4          # daily data covers a window when its first / last return is within 4 days of the ends
DD_TOL = 1e-12                # the tolerance of metrics.longest_drawdown
DD_LEVELS = (20, 30, 50)      # drawdowns deeper than these percentages are counted
PATH_YEARS = (5, 10)
PATH_LEVELS = (30, 50)
RECOVERY_DEPTH = 0.10         # time-to-recover statistics use drawdowns deeper than 10%
MAINTENANCE_MARGIN = 0.25     # Reg T maintenance requirement for long stock (IBKR's minimum)
PERMISSIONS = ("margin", "short", "options", "futures", "leveraged_etf", "inverse_etf", "crypto")
OOS_STATUS = ("passed", "failed", "none")

FAMILIES = {"return": "收益", "risk_adjusted": "风险调整后收益", "drawdown": "回撤之痛", "tails": "尾部风险",
            "consistency": "稳定性", "market": "与大盘的关系", "utility": "投资者效用", "path": "路径体验",
            "practicality": "可操作性", "confidence": "统计可信度"}
COLUMNS = ["family", "family_zh", "key", "value", "unit", "freq", "meaning_zh", "note"]


@dataclass(frozen=True)
class Spec:
    family: str
    unit: str       # frac (0.10 = 10%), frac/yr, pct (10 = 10%), ratio, x, usd, years, count, count/yr, prob, t, ...
    freq: str       # input | common | M | Y | D | boot-input | boot-M | trades | meta | rule
    meaning: str    # one plain-Chinese line


SPECS: dict[str, Spec] = {
    # 1. return
    "cagr": Spec("return", "frac/yr", "input", "年化复合收益率：把整段收益摊成“每年利滚利涨多少”"),
    "mean_return_ann": Spec("return", "frac/yr", "input", "平均每期收益按复利折成一年：(1 + 每期平均收益)^每年期数 − 1（不会低于 CAGR）"),
    "terminal_wealth": Spec("return", "usd", "input", "一开始放 1 万美元、中途不加不取，最后变成多少钱"),
    "excess_cagr_vs_oneq": Spec("return", "frac/yr", "common", "年化收益比 ONEQ（纳斯达克综合指数基金）多几个百分点"),
    "excess_cagr_vs_qqq": Spec("return", "frac/yr", "common", "年化收益比 QQQ（纳斯达克 100 基金）多几个百分点"),
    # 2. risk-adjusted
    "volatility_ann": Spec("risk_adjusted", "frac/yr", "input", "年化波动率：收益每年上下晃动的典型幅度（标准差）"),
    "sharpe": Spec("risk_adjusted", "ratio", "input", "夏普比率：比国债多赚的部分 ÷ 波动，每扛一份晃动换来多少超额收益"),
    "sortino": Spec("risk_adjusted", "ratio", "input", "索提诺比率：同夏普，但只把“跌”的晃动算作风险"),
    "calmar_mar": Spec("risk_adjusted", "ratio", "input", "卡玛比率（MAR）：年化收益 ÷ 最大回撤，每吃一份最大跌幅换来多少年收益"),
    "ir_vs_oneq": Spec("risk_adjusted", "ratio", "M", "信息比率：相对 ONEQ 的月度超额收益是否又多又稳（年化）"),
    "omega_0": Spec("risk_adjusted", "ratio", "M", "欧米伽比率：所有赚钱月份赚的总和 ÷ 所有亏钱月份亏的总和"),
    "deflated_sharpe": Spec("risk_adjusted", "prob", "M", "打折夏普：扣掉“试了很多次挑最好”的运气后，真实夏普大于 0 的概率"),
    # 3. drawdown pain
    "max_drawdown": Spec("drawdown", "frac", "input", "最大回撤：从最高点到之后最低点，账户最多缩水多少"),
    "max_dd_duration_years": Spec("drawdown", "years", "input", "最深那次回撤，从前高到重新回到前高用了几年（没回本就算到数据末尾）"),
    "max_dd_recovered": Spec("drawdown", "bool", "input", "最深那次回撤到数据结束时是否已经回本"),
    "longest_underwater_years": Spec("drawdown", "years", "input", "任何一次回撤里，最长的一次“低于前高”持续了几年"),
    "ulcer_index": Spec("drawdown", "pct", "input", "溃疡指数：每个时点离前高差多少的均方根，跌得深、套得久都让它变大"),
    "pain_index": Spec("drawdown", "pct", "input", "痛苦指数：平均每个时点离前高差多少（回撤的平均深度）"),
    "drawdowns_over_20": Spec("drawdown", "count", "input", "跌幅超过 20% 的回撤发生过几次"),
    "drawdowns_over_30": Spec("drawdown", "count", "input", "跌幅超过 30% 的回撤发生过几次"),
    "drawdowns_over_50": Spec("drawdown", "count", "input", "跌幅超过 50% 的回撤发生过几次"),
    # 4. tails
    "worst_day": Spec("tails", "frac", "D", "最糟的一天跌了多少"),
    "worst_month": Spec("tails", "frac", "M", "最糟的一个自然月跌了多少"),
    "worst_year": Spec("tails", "frac", "Y", "最糟的一个自然年跌了多少（首尾不满一年的年份也算）"),
    "var_5_monthly": Spec("tails", "frac", "M", "5% 风险价值：大约每 20 个月会有 1 个月比这个数更差"),
    "cvar_5_monthly": Spec("tails", "frac", "M", "5% 条件风险价值：最差的那 5% 月份平均跌多少"),
    "skew_monthly": Spec("tails", "number", "M", "偏度：负数＝偶尔会大跌，正数＝偶尔会大涨"),
    "excess_kurtosis_monthly": Spec("tails", "number", "M", "超额峰度：越大说明极端月份（大涨或大跌）越常见，0＝正态分布"),
    "tail_ratio_monthly": Spec("tails", "ratio", "M", "尾部比：好的极端月（第 95 百分位）÷ 坏的极端月（第 5 百分位）的大小"),
    # 5. consistency
    "positive_months_share": Spec("consistency", "frac", "M", "赚钱的月份占几成"),
    "positive_years_share": Spec("consistency", "frac", "Y", "赚钱的自然年占几成"),
    "years_beating_oneq_share": Spec("consistency", "frac", "Y", "跑赢 ONEQ 的自然年占几成"),
    "rolling_5y_win_rate_vs_oneq": Spec("consistency", "frac", "M", "任意连续 5 年里，年化收益跑赢 ONEQ 的窗口占几成"),
    # 6. market relationship
    "beta_vs_oneq": Spec("market", "number", "M", "贝塔：ONEQ 涨跌 1%，策略平均跟着涨跌百分之几"),
    "corr_vs_oneq": Spec("market", "number", "M", "相关系数：和 ONEQ 同涨同跌的程度，1＝完全同步，0＝没关系"),
    "up_capture_vs_oneq": Spec("market", "ratio", "M", "上涨捕获：ONEQ 上涨的月份，策略平均拿到 ONEQ 涨幅的几倍"),
    "down_capture_vs_oneq": Spec("market", "ratio", "M", "下跌捕获：ONEQ 下跌的月份，策略平均承受 ONEQ 跌幅的几倍（越小越好）"),
    # 7. investor utility
    "geometric_efficiency": Spec("utility", "ratio", "input", "复利效率：CAGR ÷ 平均收益折年，波动把平均收益“吃掉”后还剩几成（≤ 1）"),
    "volatility_drag": Spec("utility", "frac/yr", "input", "波动拖累：平均收益折年与 CAGR 的对数差，波动每年吃掉约几个百分点（约等于年化波动率² ÷ 2）"),
    "kelly_fraction": Spec("utility", "x", "M", "凯利仓位：让长期复利最快的仓位（1＝满仓，2＝两倍杠杆，0.5＝半仓）"),
    "kelly_ci_lo": Spec("utility", "x", "boot-M", "凯利仓位重抽样 90% 区间的下沿"),
    "kelly_ci_hi": Spec("utility", "x", "boot-M", "凯利仓位重抽样 90% 区间的上沿"),
    # 8. path experience
    "p_dd30_5y": Spec("path", "prob", "boot-input", "随便拿一段 5 年来过，期间碰到超过 30% 回撤的概率"),
    "p_dd50_5y": Spec("path", "prob", "boot-input", "随便拿一段 5 年来过，期间碰到超过 50% 回撤的概率"),
    "p_dd30_10y": Spec("path", "prob", "boot-input", "随便拿一段 10 年来过，期间碰到超过 30% 回撤的概率"),
    "p_dd50_10y": Spec("path", "prob", "boot-input", "随便拿一段 10 年来过，期间碰到超过 50% 回撤的概率"),
    "dd10_count": Spec("path", "count", "input", "跌幅超过 10% 的回撤一共几次（下面回本时间的样本数）"),
    "recovery_years_median": Spec("path", "years", "input", "超过 10% 的回撤，从前高到回本通常要几年（中位数，只算已回本的）"),
    "recovery_years_max": Spec("path", "years", "input", "超过 10% 的回撤，最慢的一次回本用了几年（只算已回本的）"),
    "underwater_now_years": Spec("path", "years", "input", "数据结束时还在水下几年（0＝在新高）"),
    "contrib_invested_usd": Spec("path", "usd", "input", "开头 1 万美元、之后每月月底再投 300 美元，一共投进去多少本金"),
    "contrib_terminal_usd": Spec("path", "usd", "input", "按上面方式定投，最后账户有多少钱"),
    "contrib_max_dd_usd": Spec("path", "usd", "input", "定投账户从最高点最多少掉多少美元（账户上看到的最大亏损额）"),
    "contrib_max_dd_pct": Spec("path", "frac", "input", "定投账户从最高点最多缩水几成（新投入的钱会让百分比看起来小一些）"),
    "lump_max_dd_usd": Spec("path", "usd", "input", "不定投、只放 1 万美元时，账户最多少掉多少美元（对照）"),
    # 9. practicality
    "turnover_one_way_per_year": Spec("practicality", "frac/yr", "trades", "单边换手率：每年买卖的金额 ÷ 2 ÷ 账户规模，1＝一年把仓位全换一遍"),
    "trades_per_year": Spec("practicality", "count/yr", "trades", "每年下几笔单"),
    "cost_drag_per_year": Spec("practicality", "frac/yr", "trades", "交易成本（佣金＋买卖价差）每年吃掉账户的百分之几"),
    "min_account_usd": Spec("practicality", "usd", "meta", "能照规则执行（整股、最低佣金不吃掉太多）所需的最少本金"),
    "permissions": Spec("practicality", "text", "meta", "需要开通的账户权限（保证金、做空、杠杆 ETF、加密货币等），none＝普通现金账户"),
    "operational_complexity": Spec("practicality", "score1-5", "rule", "操作复杂度 1–5：1＝买了放着，5＝频繁、多仓、要特殊权限还要盯时点"),
    "margin_call_drop": Spec("practicality", "frac", "meta", "保证金账户里，持仓一次跌多少就会被追加保证金（强平风险）"),
    "data_dependence": Spec("practicality", "text", "meta", "需要哪些数据才能每期算出信号（只要价格，还是要财报、付费数据）"),
    # 10. statistical confidence
    "t_vs_oneq": Spec("confidence", "t", "M", "月度超额收益的 t 值：超额是不是“真的”而不是运气，2 以上才算有点把握"),
    "p_vs_oneq": Spec("confidence", "prob", "M", "单边 p 值：如果其实没有超额，碰巧看到这么好结果的概率"),
    "excess_cagr_ci_lo": Spec("confidence", "frac/yr", "boot-M", "相对 ONEQ 年化超额的重抽样 95% 区间下沿"),
    "excess_cagr_ci_hi": Spec("confidence", "frac/yr", "boot-M", "相对 ONEQ 年化超额的重抽样 95% 区间上沿"),
    "excess_cagr_boot_share_positive": Spec("confidence", "prob", "boot-M", "重抽样里年化超额大于 0 的比例"),
    "years_of_data": Spec("confidence", "years", "input", "策略收益序列有几年"),
    "years_vs_oneq": Spec("confidence", "years", "common", "和 ONEQ 能对比的重叠年数"),
    "n_trials": Spec("confidence", "count", "meta", "选出这个策略之前一共试了多少个版本（试得越多，越要打折）"),
    "oos_status": Spec("confidence", "text", "meta", "样本外检验：passed＝通过，failed＝没通过，none＝还没做"),
}
for _k in (3, 5, 10):
    SPECS[f"rolling_{_k}y_min"] = Spec("consistency", "frac/yr", "M", f"任意连续 {_k} 年持有，年化收益最差是多少")
    SPECS[f"rolling_{_k}y_p10"] = Spec("consistency", "frac/yr", "M", f"任意连续 {_k} 年持有，年化收益的第 10 百分位（十次里最差那次左右）")
    SPECS[f"rolling_{_k}y_median"] = Spec("consistency", "frac/yr", "M", f"任意连续 {_k} 年持有，年化收益的中位数（典型体验）")


def spec(key: str) -> Spec:
    """The Spec of a metric key, including the generated crisis_* and ce_crra_* keys."""
    if key in SPECS:
        return SPECS[key]
    if key.startswith("crisis_"):
        name = key[len("crisis_"):]
        if name.endswith("_vs_oneq"):
            return Spec("market", "frac", "input", f"危机时段 {name[:-len('_vs_oneq')]}：策略总收益减 ONEQ 总收益")
        return Spec("market", "frac", "input", f"危机时段 {name}：这段时间策略的总收益")
    if key.startswith("ce_crra_"):
        g = key[len("ce_crra_"):]
        what = "对数效用＝长期复利增长率，等于 CAGR" if g == "1" else "越怕风险，γ 越大，这个数越低"
        return Spec("utility", "frac/yr", "input", f"确定性等价收益（风险厌恶 γ={g}）：拿多少无风险年收益你会觉得一样好；{what}")
    raise KeyError(key)


# ======================================================================== inputs

def infer_freq(r: pd.Series) -> str:
    """'D' or 'M' from the index: a monthly PeriodIndex is 'M'; otherwise the median gap between dates (<= 6 days:
    daily, 27-32 days: monthly)."""
    idx = r.index
    if isinstance(idx, pd.PeriodIndex):
        if idx.freqstr.startswith("M"):
            return "M"
        raise ValueError(f"unsupported PeriodIndex frequency {idx.freqstr}")
    if len(idx) < 2:
        raise ValueError("cannot infer the frequency of fewer than two returns: pass freq")
    idx = pd.DatetimeIndex(idx)
    gap = float(np.median((idx[1:] - idx[:-1]) / pd.Timedelta(days=1)))
    if gap <= 6:
        return "D"
    if 27 <= gap <= 32:
        return "M"
    raise ValueError(f"median gap of {gap:.1f} days is neither daily nor monthly: pass freq")


def prepare(r, freq: str | None = None) -> tuple[pd.Series, str, int]:
    """(returns, 'D' / 'M', periods a year). Daily returns keep a DatetimeIndex, monthly ones get a monthly
    PeriodIndex. Raises on NaN, an empty or unsorted series, or two returns in one month."""
    r = pd.Series(r, dtype=float).copy()
    if not len(r):
        raise ValueError("empty return series")
    if r.isna().any():
        raise ValueError(f"{int(r.isna().sum())} NaN returns: trim the series to the periods that have a return")
    f = freq or infer_freq(r)
    if f not in PERIODS:
        raise ValueError(f"freq must be 'D' or 'M', not {f!r}")
    if f == "M" and not isinstance(r.index, pd.PeriodIndex):
        r.index = pd.DatetimeIndex(r.index).to_period("M")
    elif f == "D":
        r.index = pd.DatetimeIndex(r.index)
    if not r.index.is_monotonic_increasing or r.index.has_duplicates:
        raise ValueError("returns must be sorted with one return per period")
    return r, f, PERIODS[f]


def _benchmark(b) -> tuple[pd.Series, str]:
    b, fb, _ = prepare(pd.Series(b, dtype=float).dropna())
    return b, fb


def _monthly(r: pd.Series, f: str) -> pd.Series:
    """Calendar-month returns (monthly PeriodIndex)."""
    return monthly(r) if f == "D" else r


def _full_months(x: pd.Series, f: str) -> pd.Series:
    """Calendar-month returns of ``x``; for daily ``x`` a first or last month it covers only in part is dropped:
    the first month when the first return falls more than ``CRISIS_EDGE_DAYS`` days after the month's first day,
    the last month when the last return falls more than ``CRISIS_EDGE_DAYS`` days before the month's last day."""
    m = _monthly(x, f)
    if f != "D" or not len(m):
        return m
    edge = pd.Timedelta(days=CRISIS_EDGE_DAYS)
    first, last = x.index[0], x.index[-1]
    if first > first.to_period("M").start_time + edge:
        m = m.iloc[1:]
    if len(m) and last < last.to_period("M").end_time.normalize() - edge:
        m = m.iloc[:-1]
    return m


def align(r: pd.Series, f: str, b) -> tuple[pd.Series, pd.Series, str]:
    """Prepared returns ``r`` and benchmark returns ``b`` on the periods both have: (r, b, common frequency). Both
    daily: the common sessions; otherwise both compounded to calendar months and the common months, where a daily
    side's partly covered first / last month is dropped (``_full_months``), so a part month is never compared with
    a whole one."""
    b, fb = _benchmark(b)
    if f == "D" and fb == "D":
        common = r.index.intersection(b.index)
        return r.loc[common], b.loc[common], "D"
    rm, bm = _full_months(r, f), _full_months(b, fb)
    common = rm.index.intersection(bm.index)
    return rm.loc[common], bm.loc[common], "M"


def rf_on(rf, r: pd.Series, f: str) -> pd.Series:
    """T-bill return of each of ``r``'s periods (zeros without ``rf``); a daily rf is compounded to months for a
    monthly ``r``. Raises when rf misses a period."""
    if rf is None:
        return pd.Series(0.0, index=r.index)
    rf = pd.Series(rf, dtype=float).dropna()
    frf = infer_freq(rf)
    if f == "M":
        rf = monthly(rf) if frf == "D" else prepare(rf, "M")[0]
    elif frf == "M":
        raise ValueError("a monthly T-bill series cannot be spread over a daily strategy: pass daily rf")
    out = rf.reindex(r.index)
    if out.isna().any():
        raise ValueError(f"rf misses {int(out.isna().sum())} of the strategy's {len(r)} periods")
    return out


def _rf_monthly(rf, r: pd.Series, f: str) -> pd.Series:
    x = rf_on(rf, r, f)
    return monthly(x) if f == "D" else x


def value_path(r: pd.Series) -> pd.Series:
    """1.0 followed by the compounded value after each return (``metrics.core_metrics``' ``v0``)."""
    return pd.concat([pd.Series([1.0]), (1 + r).cumprod().reset_index(drop=True)])


def _bootstrap(x: np.ndarray, stat, n_boot: int, mean_block: float, seed: int) -> np.ndarray:
    """``stat`` of ``n_boot`` stationary-bootstrap resamples of the rows of ``x``."""
    rng = np.random.default_rng(seed)
    n = len(x)
    return np.array([stat(x[stationary_bootstrap_index(n, mean_block, rng)]) for _ in range(n_boot)])


# ======================================================================== primitives

def mean_return_annualized(r, periods: int) -> float:
    """(1 + mean(r))^periods - 1: the mean per-period return compounded over a year. By the AM-GM inequality it is
    never below the CAGR (equal only without dispersion), and it is the CRRA certainty equivalent at gamma = 0."""
    return float((1 + float(np.mean(np.asarray(r, float)))) ** periods - 1)


def sharpe_of(ex: pd.Series, periods: int) -> float:
    """mean / sd(ddof=1) * sqrt(periods) of per-period excess returns (NaN without spread)."""
    sd = ex.std()
    return float(ex.mean() / sd * math.sqrt(periods)) if sd > 0 else NAN


def sortino_of(ex: pd.Series, periods: int) -> float:
    """mean / sqrt(mean(min(ex, 0)^2)) * sqrt(periods): downside deviation below 0 over ALL periods."""
    down = np.sqrt(np.mean(np.minimum(ex.to_numpy(), 0.0) ** 2))
    return float(ex.mean() / down * math.sqrt(periods)) if down > 0 else NAN


def omega_ratio(x, threshold: float = 0.0) -> float:
    """sum(max(x - threshold, 0)) / sum(max(threshold - x, 0)); inf when nothing falls below the threshold."""
    d = np.asarray(x, float) - threshold
    gain, loss = float(d[d > 0].sum()), float(-d[d < 0].sum())
    if loss > 0:
        return gain / loss
    return math.inf if gain > 0 else NAN


def capture_ratios(r: pd.Series, b: pd.Series) -> tuple[float, float]:
    """(upside, downside) capture: mean of ``r`` over the periods with ``b`` > 0 divided by the mean of ``b`` over
    them, and the same over ``b`` < 0 (arithmetic-mean version; r = k * b gives (k, k))."""
    up, dn = (b > 0).to_numpy(), (b < 0).to_numpy()
    u = float(r[up].mean() / b[up].mean()) if up.any() else NAN
    d = float(r[dn].mean() / b[dn].mean()) if dn.any() else NAN
    return u, d


def rolling_annualized(m: pd.Series, months: int) -> pd.Series:
    """Annualised compound return of every run of ``months`` consecutive monthly returns (windows overlap and step
    one month); empty when the series is shorter than one window."""
    if len(m) < months:
        return pd.Series(dtype=float)
    g = (1 + m).rolling(months).apply(np.prod, raw=True).dropna()
    return g ** (12 / months) - 1


def window_return(r: pd.Series, f: str, start: str, end: str) -> float:
    """Compound return over [start, end]; NaN unless the series covers the whole window (daily: first and last
    return within ``CRISIS_EDGE_DAYS`` calendar days of the ends; monthly: the window's first and last month)."""
    if f == "D":
        a, z = pd.Timestamp(start), pd.Timestamp(end)
        edge = pd.Timedelta(days=CRISIS_EDGE_DAYS)
        x = r.loc[a:z]
        if not len(x) or x.index[0] > a + edge or x.index[-1] < z - edge:
            return NAN
    else:
        a, z = pd.Period(start, "M"), pd.Period(end, "M")
        if r.index[0] > a or r.index[-1] < z:
            return NAN
        x = r[(r.index >= a) & (r.index <= z)]
    return float(np.prod(1 + x.to_numpy()) - 1)


def ce_crra(r, gamma: float, periods: int) -> float:
    """Annualised certainty-equivalent return under CRRA utility with relative risk aversion ``gamma``: the per-period
    CE is exp(mean(log(1 + r))) - 1 for gamma = 1 (log utility, so CE = CAGR) and mean((1 + r)^(1 - gamma))^(1 /
    (1 - gamma)) - 1 otherwise, compounded over ``periods``. -1 when some period loses everything (gamma >= 1)."""
    g = 1 + np.asarray(r, float)
    if (g <= 0).any():
        return -1.0 if gamma >= 1 else NAN
    per = math.exp(float(np.mean(np.log(g)))) if gamma == 1 else float(np.mean(g ** (1 - gamma)) ** (1 / (1 - gamma)))
    return float(per ** periods - 1)


def kelly_fraction(ex) -> float:
    """Growth-optimal fraction of capital in the strategy, the rest earning the T-bill: mean / variance (ddof=1) of
    per-period excess returns (the continuous-time approximation)."""
    x = np.asarray(ex, float)
    if len(x) < 2:
        return NAN
    v = x.var(ddof=1)
    return float(x.mean() / v) if v > 0 else NAN


def drawdown_episodes(r, *, freq: str | None = None) -> pd.DataFrame:
    """One row per drawdown episode of the value path (``value_path``: position 0 is the start, position i the value
    after the i-th return): ``peak`` (last position at the running peak), ``trough``, ``recovery`` (first position
    back at the peak, -1 if never), ``depth`` (trough / peak - 1), ``underwater_periods`` (positions more than 1e-12
    below the peak, i.e. ``recovery - peak - 1`` when recovered), ``underwater_years``, ``peak_to_recovery_years``
    (``(recovery - peak) / P``; the last position minus the peak when not recovered, which then equals
    ``underwater_years``), ``recovered``, ``trough_to_recovery_years`` and date labels."""
    r, f, P = prepare(r, freq)
    v = value_path(r).to_numpy()
    labels = ["start"] + [str(x.date()) if f == "D" else str(x) for x in r.index]
    peak = np.maximum.accumulate(v)
    under = v < peak - DD_TOL
    rows, i, n = [], 0, len(v)
    while i < n:
        if not under[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and under[j + 1]:
            j += 1
        dd = v[i:j + 1] / peak[i:j + 1] - 1
        k = i + int(np.argmin(dd))
        rec = j + 1 < n
        rows.append({"peak": i - 1, "trough": k, "recovery": j + 1 if rec else -1, "depth": float(dd.min()),
                     "underwater_periods": j - i + 1, "underwater_years": (j - i + 1) / P,
                     "peak_to_recovery_years": ((j + 1 if rec else n - 1) - (i - 1)) / P, "recovered": rec,
                     "trough_to_recovery_years": (j + 1 - k) / P if rec else NAN, "peak_label": labels[i - 1],
                     "trough_label": labels[k], "recovery_label": labels[j + 1] if rec else ""})
        i = j + 1
    return pd.DataFrame(rows, columns=["peak", "trough", "recovery", "depth", "underwater_periods", "underwater_years",
                                       "peak_to_recovery_years", "recovered", "trough_to_recovery_years", "peak_label",
                                       "trough_label", "recovery_label"])


def path_max_drawdowns(r, lengths: tuple[int, ...], n_boot: int, mean_block: float, seed: int) -> np.ndarray:
    """(n_boot x len(lengths)) maximum drawdowns of stationary-bootstrap paths: each path has max(lengths) periods
    (value starting at 1.0) and the drawdown for a shorter length is measured on the path's first periods."""
    x = np.asarray(r, float)
    rng = np.random.default_rng(seed)
    out = np.empty((n_boot, len(lengths)))
    for b in range(n_boot):
        v = np.concatenate([[1.0], np.cumprod(1 + x[stationary_bootstrap_index(len(x), mean_block, rng,
                                                                                 size=max(lengths))])])
        dd = v / np.maximum.accumulate(v) - 1
        out[b] = [dd[:n + 1].min() for n in lengths]
    return out


def contribution_path(r, start_capital: float, contribution: float, *, freq: str | None = None) -> np.ndarray:
    """Account values with ``start_capital`` at the start and ``contribution`` deposited at the end of every calendar
    month except the last: the start, the value after each return and, at each month end, the value after the
    deposit (so a drop right after a deposit counts from the deposited value)."""
    r, f, _ = prepare(r, freq)
    x = r.to_numpy()
    mon = r.index.to_period("M") if f == "D" else r.index
    month_end = np.r_[np.asarray(mon[1:] != mon[:-1]), False]
    vals, v = [float(start_capital)], float(start_capital)
    for xi, me in zip(x, month_end):
        v *= 1 + xi
        vals.append(v)
        if me:
            v += contribution
            vals.append(v)
    return np.array(vals)


def dollar_and_pct_drawdown(w: np.ndarray) -> tuple[float, float]:
    """(largest fall in dollars below the running peak, largest fall as a fraction of it) of account values ``w``."""
    peak = np.maximum.accumulate(w)
    return float((peak - w).max()), float((w / peak - 1).min())


def margin_call_drop(leverage: float | None, maintenance: float = MAINTENANCE_MARGIN) -> float:
    """One-step fall of the positions that brings a margin account with gross ``leverage`` (positions / equity) to
    the ``maintenance`` requirement: (1 - m L) / (L (1 - m)); 0 when already below it, NaN without margin
    (``leverage`` missing or <= 1)."""
    if leverage is None or not leverage > 1:
        return NAN
    return max(0.0, (1 - maintenance * leverage) / (leverage * (1 - maintenance)))


def operational_complexity(trades_per_year: float, positions: int | None = None, permissions=(),
                           timing_sensitive: bool = False) -> float:
    """1 plus one point for each of: more than 12 trades a year; more than 52 trades a year; more than 10
    positions held at once; any permission beyond a cash account (``PERMISSIONS``); timing-sensitive execution
    (intraday orders, limit orders, a fixed time of day); capped at 5. NaN when the trade count is unknown."""
    if trades_per_year is None or not np.isfinite(trades_per_year):
        return NAN
    pts = (trades_per_year > 12) + (trades_per_year > 52) + (positions is not None and positions > 10) \
        + bool(len(tuple(permissions or ()))) + bool(timing_sensitive)
    return float(min(5, 1 + int(pts)))


# ======================================================================== the ten families

def return_metrics(r, *, oneq=None, qqq=None, freq: str | None = None, start_capital: float = 10_000.0) -> dict:
    r, f, P = prepare(r, freq)
    out = {"cagr": cagr_of(r, P), "mean_return_ann": mean_return_annualized(r, P),
           "terminal_wealth": float(start_capital * np.prod(1 + r.to_numpy()))}
    for tag, b in (("oneq", oneq), ("qqq", qqq)):
        out[f"excess_cagr_vs_{tag}"] = NAN
        if b is not None:
            rc, bc, fc = align(r, f, b)
            if len(rc) >= 2:
                out[f"excess_cagr_vs_{tag}"] = cagr_of(rc, PERIODS[fc]) - cagr_of(bc, PERIODS[fc])
    return out


def risk_adjusted_metrics(r, *, rf=None, oneq=None, freq: str | None = None, n_trials: int | None = None,
                          sr_trials_var: float | None = None) -> dict:
    """Sharpe / Sortino on excess over ``rf`` at the input frequency; deflated Sharpe (Bailey and Lopez de Prado) of
    the monthly Sharpe when ``n_trials`` >= 2, with the variance of the trials' monthly Sharpe ratios
    ``sr_trials_var`` (default 1 / months, the sampling variance of a Sharpe estimate under a zero true Sharpe)."""
    r, f, P = prepare(r, freq)
    ex = r - rf_on(rf, r, f)
    cagr, mdd = cagr_of(r, P), max_drawdown(value_path(r))
    m = _monthly(r, f)
    out = {"volatility_ann": float(r.std() * math.sqrt(P)), "sharpe": sharpe_of(ex, P), "sortino": sortino_of(ex, P),
           "calmar_mar": cagr / abs(mdd) if mdd < 0 else NAN, "ir_vs_oneq": NAN, "omega_0": omega_ratio(m),
           "deflated_sharpe": NAN}
    if oneq is not None:
        rc, bc, fc = align(r, f, oneq)
        mx = _monthly(rc, fc) - _monthly(bc, fc)
        if len(mx) >= 2:
            out["ir_vs_oneq"] = t_and_ir(mx)[1]
    mex = m - _rf_monthly(rf, r, f)
    if n_trials is not None and n_trials >= 2 and len(mex) >= 4 and mex.std() > 0:
        sr = float(mex.mean() / mex.std())
        var = float(sr_trials_var) if sr_trials_var is not None else 1.0 / len(mex)
        out["deflated_sharpe"] = deflated_sharpe_n(sr, len(mex), float(mex.skew()), float(mex.kurt() + 3),
                                                   int(n_trials), var)["dsr"]
    return out


def drawdown_metrics(r, *, freq: str | None = None) -> dict:
    r, f, P = prepare(r, freq)
    v0 = value_path(r)
    ep = drawdown_episodes(r, freq=f)
    dd = (v0 / v0.cummax() - 1).to_numpy()
    out = {"max_drawdown": max_drawdown(v0), "max_dd_duration_years": 0.0, "max_dd_recovered": True,
           "longest_underwater_years": longest_drawdown(v0) / P, "ulcer_index": ulcer_index(v0),
           "pain_index": float(np.mean(-dd) * 100)}
    if len(ep):
        deep = ep.loc[ep["depth"].idxmin()]
        out["max_dd_duration_years"] = float(deep["peak_to_recovery_years"])
        out["max_dd_recovered"] = bool(deep["recovered"])
    for lvl in DD_LEVELS:
        out[f"drawdowns_over_{lvl}"] = int((ep["depth"] < -lvl / 100).sum())
    return out


def tail_metrics(r, *, freq: str | None = None) -> dict:
    """Worst day / month / year and the monthly distribution: 5% VaR (numpy's linear-interpolation 5th percentile,
    as a return, so negative is a loss), CVaR (mean of the months at or below it), skew and excess kurtosis (pandas'
    bias-corrected estimators), tail ratio |95th percentile| / |5th percentile|."""
    r, f, P = prepare(r, freq)
    m, y = _monthly(r, f), yearly(r)
    q05, q95 = (float(q) for q in np.quantile(m.to_numpy(), [0.05, 0.95]))
    return {"worst_day": float(r.min()) if f == "D" else NAN, "worst_month": float(m.min()), "worst_year": float(y.min()),
            "var_5_monthly": q05, "cvar_5_monthly": float(m[m <= q05].mean()),
            "skew_monthly": float(m.skew()), "excess_kurtosis_monthly": float(m.kurt()),
            "tail_ratio_monthly": abs(q95) / abs(q05) if q05 != 0 else NAN}


def consistency_metrics(r, *, oneq=None, freq: str | None = None) -> dict:
    r, f, P = prepare(r, freq)
    m, y = _monthly(r, f), yearly(r)
    out = {"positive_months_share": float((m > 0).mean()), "positive_years_share": float((y > 0).mean()),
           "years_beating_oneq_share": NAN}
    for k in (3, 5, 10):
        roll = rolling_annualized(m, 12 * k).to_numpy()
        out[f"rolling_{k}y_min"] = float(roll.min()) if len(roll) else NAN
        out[f"rolling_{k}y_p10"] = float(np.quantile(roll, 0.10)) if len(roll) else NAN
        out[f"rolling_{k}y_median"] = float(np.median(roll)) if len(roll) else NAN
    out["rolling_5y_win_rate_vs_oneq"] = NAN
    if oneq is not None:
        rc, bc, fc = align(r, f, oneq)
        if len(rc):
            out["years_beating_oneq_share"] = float((yearly(rc) > yearly(bc)).mean())
            a, b = rolling_annualized(_monthly(rc, fc), 60), rolling_annualized(_monthly(bc, fc), 60)
            out["rolling_5y_win_rate_vs_oneq"] = float((a > b).mean()) if len(a) else NAN
    return out


def market_metrics(r, *, oneq=None, rf=None, freq: str | None = None, crises=CRISES) -> dict:
    """Beta (monthly excess over rf, cov / var with ddof=1), correlation and capture ratios (raw monthly returns) vs
    ONEQ; each crisis window's compound return and its difference from ONEQ's over the same window."""
    r, f, P = prepare(r, freq)
    out = {"beta_vs_oneq": NAN, "corr_vs_oneq": NAN, "up_capture_vs_oneq": NAN, "down_capture_vs_oneq": NAN}
    b = fb = None
    if oneq is not None:
        rc, bc, fc = align(r, f, oneq)
        mr, mb = _monthly(rc, fc), _monthly(bc, fc)
        rfm = _rf_monthly(rf, rc, fc)
        y_, x_ = (mr - rfm).values, (mb - rfm).values
        if len(y_) >= 3:
            out["beta_vs_oneq"] = float(np.cov(y_, x_, ddof=1)[0, 1] / np.var(x_, ddof=1))
            out["corr_vs_oneq"] = float(np.corrcoef(mr.to_numpy(), mb.to_numpy())[0, 1])
        out["up_capture_vs_oneq"], out["down_capture_vs_oneq"] = capture_ratios(mr, mb)
        b, fb = _benchmark(oneq)
    for name, a, z in crises:
        s = window_return(r, f, a, z)
        out[f"crisis_{name}"] = s
        out[f"crisis_{name}_vs_oneq"] = s - window_return(b, fb, a, z) if b is not None else NAN
    return out


def utility_metrics(r, *, rf=None, freq: str | None = None, gammas=GAMMAS, n_boot: int = 2000,
                    block_months: float = 12.0, seed: int = SEED) -> dict:
    """CRRA certainty equivalents at the input frequency, compounding efficiency, and the Kelly fraction of monthly
    excess returns with a 90% stationary-bootstrap interval."""
    r, f, P = prepare(r, freq)
    out = {f"ce_crra_{g:g}": ce_crra(r, g, P) for g in gammas}
    cagr, mean_ann = cagr_of(r, P), mean_return_annualized(r, P)
    out["geometric_efficiency"] = cagr / mean_ann if mean_ann > 0 else NAN
    # P * (log(1 + mean r) - mean log(1 + r)) >= 0 (Jensen), about sigma^2 / 2 a year
    out["volatility_drag"] = math.log1p(mean_ann) - math.log1p(cagr) if mean_ann > -1 and cagr > -1 else NAN
    mex = (_monthly(r, f) - _rf_monthly(rf, r, f)).to_numpy()
    out["kelly_fraction"] = kelly_fraction(mex)
    out["kelly_ci_lo"] = out["kelly_ci_hi"] = NAN
    if len(mex) >= 12 and n_boot:
        k = _bootstrap(mex, kelly_fraction, n_boot, block_months, seed)
        k = k[np.isfinite(k)]
        if len(k):
            out["kelly_ci_lo"], out["kelly_ci_hi"] = (float(q) for q in np.quantile(k, [0.05, 0.95]))
    return out


def path_metrics(r, *, freq: str | None = None, n_boot: int = 2000, block_months: float = 12.0, seed: int = SEED,
                 start_capital: float = 10_000.0, contribution: float = 300.0) -> dict:
    """Bootstrap probabilities of deep drawdowns within 5 / 10 years (paths at the input frequency), the historical
    time to recover from drawdowns deeper than 10%, and the effect of monthly deposits on the drawdown."""
    r, f, P = prepare(r, freq)
    out = {}
    lengths = tuple(y * P for y in PATH_YEARS)
    mdd = path_max_drawdowns(r, lengths, n_boot, block_months * P / 12, seed) if n_boot else None
    for j, yrs in enumerate(PATH_YEARS):
        for lvl in PATH_LEVELS:
            out[f"p_dd{lvl}_{yrs}y"] = float((mdd[:, j] < -lvl / 100).mean()) if mdd is not None else NAN
    ep = drawdown_episodes(r, freq=f)
    deep = ep[ep["depth"] < -RECOVERY_DEPTH]
    rec = deep[deep["recovered"]]
    out["dd10_count"] = int(len(deep))
    out["recovery_years_median"] = float(rec["peak_to_recovery_years"].median()) if len(rec) else NAN
    out["recovery_years_max"] = float(rec["peak_to_recovery_years"].max()) if len(rec) else NAN
    still = ep[~ep["recovered"]]
    out["underwater_now_years"] = float(still["underwater_years"].iloc[-1]) if len(still) else 0.0
    w = contribution_path(r, start_capital, contribution, freq=f)
    deposits = (len(w) - 1 - len(r))
    out["contrib_invested_usd"] = float(start_capital + contribution * deposits)
    out["contrib_terminal_usd"] = float(w[-1])
    out["contrib_max_dd_usd"], out["contrib_max_dd_pct"] = dollar_and_pct_drawdown(w)
    out["lump_max_dd_usd"] = dollar_and_pct_drawdown(contribution_path(r, start_capital, 0.0, freq=f))[0]
    return out


def practicality_metrics(r, *, trades=None, meta: dict | None = None, freq: str | None = None) -> dict:
    """From the trade log (one row per order over the same span as ``r``; optional columns ``traded`` = order value
    / account value, ``cost`` = order cost / account value): trades, one-way turnover and cost drag per year. The
    same keys in ``meta`` fill what the log cannot give; the rest comes from ``meta`` (keys listed in
    ``scorecard``)."""
    r, f, P = prepare(r, freq)
    meta = dict(meta or {})
    years = len(r) / P
    out = {"turnover_one_way_per_year": NAN, "trades_per_year": NAN, "cost_drag_per_year": NAN}
    if trades is not None:
        t = pd.DataFrame(trades)
        out["trades_per_year"] = len(t) / years
        if "traded" in t:
            out["turnover_one_way_per_year"] = float(t["traded"].abs().sum() / 2 / years)
        if "cost" in t:
            out["cost_drag_per_year"] = float(t["cost"].sum() / years)
    for k in list(out):
        if not np.isfinite(out[k]) and meta.get(k) is not None:
            out[k] = float(meta[k])
    perms = meta.get("permissions")
    if perms is not None:
        perms = tuple(perms)
        bad = [p for p in perms if p not in PERMISSIONS]
        if bad:
            raise ValueError(f"unknown permissions {bad}: use {PERMISSIONS}")
    out["min_account_usd"] = float(meta["min_account_usd"]) if meta.get("min_account_usd") is not None else NAN
    out["permissions"] = ("+".join(perms) if perms else "none") if perms is not None else NAN
    out["operational_complexity"] = operational_complexity(out["trades_per_year"], meta.get("positions"), perms or (),
                                                           bool(meta.get("timing_sensitive", False)))
    out["margin_call_drop"] = margin_call_drop(meta.get("margin_leverage"),
                                               meta.get("maintenance_margin", MAINTENANCE_MARGIN))
    out["data_dependence"] = str(meta["data_dependence"]) if meta.get("data_dependence") is not None else NAN
    return out


def confidence_metrics(r, *, oneq=None, meta: dict | None = None, freq: str | None = None, n_boot: int = 2000,
                       block_months: float = 12.0, seed: int = SEED) -> dict:
    """Monthly-excess t vs ONEQ (``metrics.t_and_ir``) and its one-sided Student-t p (n - 1 df); a 95%
    stationary-bootstrap interval of the excess CAGR from paired calendar months; years of data; trial count and
    out-of-sample status from ``meta``."""
    r, f, P = prepare(r, freq)
    meta = dict(meta or {})
    out = {"t_vs_oneq": NAN, "p_vs_oneq": NAN, "excess_cagr_ci_lo": NAN, "excess_cagr_ci_hi": NAN,
           "excess_cagr_boot_share_positive": NAN, "years_of_data": len(r) / P, "years_vs_oneq": NAN}
    if oneq is not None:
        rc, bc, fc = align(r, f, oneq)
        out["years_vs_oneq"] = len(rc) / PERIODS[fc]
        mr, mb = _monthly(rc, fc), _monthly(bc, fc)
        mx = mr - mb
        if len(mx) >= 2:
            t = t_and_ir(mx)[0]
            out["t_vs_oneq"], out["p_vs_oneq"] = t, t_sf(t, len(mx) - 1)
        if len(mx) >= 12 and n_boot:
            def excess(z):
                return cagr_of(z[:, 0], 12) - cagr_of(z[:, 1], 12)
            e = _bootstrap(np.column_stack([mr.to_numpy(), mb.to_numpy()]), excess, n_boot, block_months, seed)
            out["excess_cagr_ci_lo"], out["excess_cagr_ci_hi"] = (float(q) for q in np.quantile(e, [0.025, 0.975]))
            out["excess_cagr_boot_share_positive"] = float((e > 0).mean())
    out["n_trials"] = float(meta["n_trials"]) if meta.get("n_trials") is not None else NAN
    status = meta.get("oos_status", "none")
    if status not in OOS_STATUS:
        raise ValueError(f"oos_status must be one of {OOS_STATUS}")
    out["oos_status"] = status
    return out


# ======================================================================== the table

def _notes(vals: dict, *, f: str, years: float, rf_given: bool, oneq_given: bool, qqq_given: bool,
           meta: dict, trades_given: bool) -> dict:
    """Plain-Chinese notes on how far to trust a value or why it is missing."""
    n = {}
    if not rf_given:
        for k in ("sharpe", "sortino", "deflated_sharpe", "kelly_fraction", "kelly_ci_lo", "kelly_ci_hi", "beta_vs_oneq"):
            n[k] = "未提供无风险利率，按 0 计"
    for k in vals:
        if ("oneq" in k and not oneq_given) or ("qqq" in k and not qqq_given):
            n[k] = "未提供该基准"
    if f == "M":
        n["worst_day"] = "月度输入没有日数据"
        for k in ("max_drawdown", "ulcer_index", "pain_index", "contrib_max_dd_pct"):
            n.setdefault(k, "月度数据看不到月内的回撤，实际更深")
    if not (meta.get("n_trials") or 0) >= 2:
        n.setdefault("deflated_sharpe", "需要 meta['n_trials'] ≥ 2")
    elif meta.get("sr_trials_var") is None:
        n.setdefault("deflated_sharpe", "试验间夏普方差未提供，按 1/月数 假设")
    if vals.get("max_dd_recovered") is False:
        n["max_dd_duration_years"] = "尚未回本，算到数据末尾"
    for y in PATH_YEARS:
        if years < y:
            for lvl in PATH_LEVELS:
                n[f"p_dd{lvl}_{y}y"] = f"数据不足 {y} 年，重抽样反复使用同一段历史"
    for k, v in vals.items():
        if k in n:
            continue
        missing = isinstance(v, float) and math.isnan(v)
        if missing and k.startswith("rolling_"):
            n[k] = "数据不够一个窗口"
        elif missing and k.startswith("crisis_"):
            n[k] = "数据不覆盖该时段"
        elif missing and SPECS.get(k) is not None and SPECS[k].freq in ("trades", "meta", "rule"):
            n[k] = "未提供（meta / 交易记录）" if not trades_given or SPECS[k].freq != "trades" else "交易记录缺少该列"
    perms = meta.get("permissions")
    no_margin = (meta.get("margin_leverage") is not None and not meta["margin_leverage"] > 1) or \
        (meta.get("margin_leverage") is None and perms is not None and "margin" not in perms)
    if no_margin:
        n["margin_call_drop"] = "不用保证金借钱：没有追加保证金的风险"
    n.setdefault("kelly_fraction", "均值/方差近似，肥尾时真实最优更低；实践中最多用一半")
    return n


def _freq_label(code: str, f: str, fc: str) -> str:
    return {"input": f, "common": fc, "boot-input": f"{f} bootstrap", "boot-M": "M bootstrap"}.get(code, code)


def _py(v):
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    return v


def scorecard(r, *, freq: str | None = None, oneq=None, qqq=None, rf=None, trades=None, meta: dict | None = None,
              start_capital: float = 10_000.0, contribution: float = 300.0, n_boot: int = 2000,
              block_months: float = 12.0, seed: int = SEED, crises=CRISES, gammas=GAMMAS) -> pd.DataFrame:
    """All ten families for return series ``r``, one row per metric: family, family_zh, key, value, unit, freq,
    meaning_zh, note.

    ``meta`` keys (all optional): ``n_trials`` and ``sr_trials_var`` (deflated Sharpe, statistical confidence),
    ``oos_status`` ('passed' / 'failed' / 'none'), ``min_account_usd``, ``permissions`` (subset of
    ``PERMISSIONS``; empty = cash account), ``positions`` (typically held), ``timing_sensitive`` (bool),
    ``margin_leverage`` (gross positions / equity in a margin account) and ``maintenance_margin`` (default 0.25),
    ``data_dependence`` (text), and ``trades_per_year`` / ``turnover_one_way_per_year`` / ``cost_drag_per_year``
    when there is no trade log."""
    r, f, P = prepare(r, freq)
    meta = dict(meta or {})
    fc = align(r, f, oneq)[2] if oneq is not None else f
    vals: dict = {}
    vals.update(return_metrics(r, oneq=oneq, qqq=qqq, freq=f, start_capital=start_capital))
    vals.update(risk_adjusted_metrics(r, rf=rf, oneq=oneq, freq=f, n_trials=meta.get("n_trials"),
                                      sr_trials_var=meta.get("sr_trials_var")))
    vals.update(drawdown_metrics(r, freq=f))
    vals.update(tail_metrics(r, freq=f))
    vals.update(consistency_metrics(r, oneq=oneq, freq=f))
    vals.update(market_metrics(r, oneq=oneq, rf=rf, freq=f, crises=crises))
    vals.update(utility_metrics(r, rf=rf, freq=f, gammas=gammas, n_boot=n_boot, block_months=block_months, seed=seed))
    vals.update(path_metrics(r, freq=f, n_boot=n_boot, block_months=block_months, seed=seed,
                             start_capital=start_capital, contribution=contribution))
    vals.update(practicality_metrics(r, trades=trades, meta=meta, freq=f))
    vals.update(confidence_metrics(r, oneq=oneq, meta=meta, freq=f, n_boot=n_boot, block_months=block_months,
                                   seed=seed))
    notes = _notes(vals, f=f, years=len(r) / P, rf_given=rf is not None, oneq_given=oneq is not None,
                   qqq_given=qqq is not None, meta=meta, trades_given=trades is not None)
    rows = []
    for key, value in vals.items():
        s = spec(key)
        rows.append({"family": s.family, "family_zh": FAMILIES[s.family], "key": key, "value": _py(value),
                     "unit": s.unit, "freq": _freq_label(s.freq, f, fc), "meaning_zh": s.meaning,
                     "note": notes.get(key, "")})
    return pd.DataFrame(rows, columns=COLUMNS)


def compare(cards: dict) -> pd.DataFrame:
    """Several scorecards side by side: one row per metric key of the first card (in its order), columns family, unit,
    then one value column per name in ``cards``."""
    base = next(iter(cards.values())).set_index("key")[["family", "unit"]]
    return base.join(pd.DataFrame({n: c.set_index("key")["value"] for n, c in cards.items()}), how="left")
