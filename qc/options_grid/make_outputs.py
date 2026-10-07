"""Render tables + SVG heat-maps from the aggregate JSON produced in the browser by qc/options_grid/analyze.js
(ledger docs/research_ledger_qc_options_grid.md). The per-config surface stays in the QuantConnect chart streams
(backtest ids in the ledger); only aggregates are copied out (hash-checked against the page).

Usage: python3 qc/options_grid/make_outputs.py QQQ_F025 [SPY_F025 QQQ_F050 ...]
Input:  output/research_only/qc_options_grid/agg_<run>.json
Output: output/research_only/qc_options_grid/tables_<run>.md and heat_<run>_<strategy>_<cut>.svg
"""
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "output" / "research_only" / "qc_options_grid"
S = ["CSP", "CC", "WH", "PO"]
D = ["0.05", "0.10", "0.15", "0.20", "0.25", "0.30", "0.40", "0.50"]
T = ["7", "14", "30", "45"]
M = ["H", "TP", "R", "SL"]
F = ["ALL", "V20", "V25", "VT3", "BW", "DD10", "UP"]
CUTS = {  # key -> (row labels, col labels, title, kind)
    "dt_xF": (D, T, "delta x DTE: median full-period excess vs ONEQ (pct pts/yr)", "diff"),
    "dt_x1": (D, T, "delta x DTE: median H1 (2012-03..2018-12) excess vs ONEQ", "diff"),
    "dt_x2": (D, T, "delta x DTE: median H2 (2019-01..2026-05) excess vs ONEQ", "diff"),
    "dt_both": (D, T, "delta x DTE: configs beating ONEQ in BOTH halves (of 28)", "count"),
    "dt_ddF": (D, T, "delta x DTE: median full-period max drawdown (%)", "dd"),
    "fm_xF": (F, M, "filter x management: median full-period excess vs ONEQ", "diff"),
    "fm_both": (F, M, "filter x management: configs beating ONEQ in both halves (of 32)", "count"),
    "fd_xF": (F, D, "filter x delta: median full-period excess vs ONEQ", "diff"),
}


def color(v, kind, ref=None):
    if v is None:
        return "#dddddd"
    if kind == "count":
        x = max(0.0, min(1.0, v / 32.0))
        return f"#{int(255 - 175 * x):02x}{int(255 - 115 * x):02x}ff"
    if kind == "dd":
        x = max(0.0, min(1.0, (v - (ref or 35.2)) / 15.0)) if v > (ref or 35.2) else -max(0.0, min(1.0, ((ref or 35.2) - v) / 30.0))
        v = -x  # deeper than ONEQ -> red
    if v < 0:
        x = max(0.0, min(1.0, -v / 15.0))
        return f"#ff{int(255 - 155 * x):02x}{int(255 - 155 * x):02x}"
    x = max(0.0, min(1.0, v / 5.0))
    return f"#{int(255 - 175 * x):02x}{int(255 - 115 * x):02x}ff"


def heat_svg(title, rows, cols, grid, kind, path, ref=None):
    cw, ch, lw, th = 60, 24, 64, 50
    W, H = max(lw + cw * len(cols) + 10, 520), th + ch * len(rows) + 34
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="Helvetica,Arial" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="white"/>', f'<text x="6" y="18" font-size="13" font-weight="bold">{title}</text>']
    for j, c in enumerate(cols):
        o.append(f'<text x="{lw + j * cw + cw / 2}" y="{th - 8}" text-anchor="middle">{c}</text>')
    for i, r in enumerate(rows):
        o.append(f'<text x="{lw - 6}" y="{th + i * ch + 16}" text-anchor="end">{r}</text>')
        for j in range(len(cols)):
            v = grid[i][j]
            o.append(f'<rect x="{lw + j * cw}" y="{th + i * ch}" width="{cw - 1}" height="{ch - 1}" fill="{color(v, kind, ref)}"/>')
            if v is not None:
                lab = f"{v:d}" if kind == "count" else (f"{v:.1f}" if kind == "dd" else f"{v:+.1f}")
                o.append(f'<text x="{lw + j * cw + cw / 2}" y="{th + i * ch + 16}" text-anchor="middle">{lab}</text>')
    note = {"diff": "blue = beats ONEQ, red = lags", "count": "darker = more configs beat ONEQ in both halves",
            "dd": f"red = deeper than ONEQ ({ref}%)"}[kind]
    o.append(f'<text x="6" y="{H - 10}" font-size="10" fill="#555">{note}; other axes pooled (median)</text>')
    o.append("</svg>")
    path.write_text("\n".join(o))


def md_table(rows, cols, grid, kind):
    out = ["| | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for r, vals in zip(rows, grid):
        cells = [("" if v is None else (f"{v:d}" if kind == "count" else (f"{v:.1f}" if kind == "dd" else f"{v:+.1f}"))) for v in vals]
        out.append(f"| {r} | " + " | ".join(cells) + " |")
    return out


def main(run):
    agg = json.loads((OUT / f"agg_{run}.json").read_text())
    ref = round(agg["benchJ"]["ddF"] * 100, 1)
    L = [f"# Options grid {run}: aggregated surface", "",
         f"Backtest {agg.get('backtest', '?')}, code {agg.get('code_sha16', '?')}. Benchmarks: " +
         "; ".join(f"{k} {v}" for k, v in agg["bench"].items()), "",
         "Counts: " + json.dumps(agg["counts"]), "", "## By strategy", ""] + [f"- {x}" for x in agg["byStrategy"]]
    L += ["", f"Median config: {agg['median']}", f"Median stats: {agg['medianStats']}", f"Deflated Sharpe: {agg['dsr']}", ""]
    if agg.get("oos"):
        L += ["## Two-fold out-of-sample selection", ""] + [f"- {k}: {v}" for k, v in agg["oos"].items() if k != "keys"]
        L += ["", "Persistence: " + json.dumps(agg["persistence"]), ""]
    L += ["## One-axis summaries", ""]
    for ax, rows in agg["one"].items():
        L += [f"- {ax}: " + r for r in rows]
    L += ["", "## A passes / both-halves winners by strategy x filter (A/both, columns ALL V20 V25 VT3 BW DD10 UP)", ""]
    L += [f"- {s}: {x}" for s, x in zip(S, agg["sf"])]
    L += ["", "## Best per strategy", ""]
    for s, b in zip(S, agg["bestPer"]):
        L += [f"- {s} best tF: {b['tF']}", f"- {s} best excess: {b['xF']}", f"- {s} min drawdown: {b['minDD']}"]
    L += ["", "## Named configs (beta = regression on ONEQ excess return, alpha annualised)", ""]
    L += [f"- {k}: {v}" for k, v in agg["namedRows"].items()]
    if agg.get("sel"):
        L += ["", "## Selected configs with beta / alpha", ""] + [f"- {x}" for x in agg["sel"]]
    for k in ("fixedOOS", "fillCompare"):
        if agg.get(k):
            L += ["", f"{k}: {agg[k]}"]
    L += ["", f"Top-tF configs, beta/alpha: {agg['topBeta']}", f"{agg['aKeep']}", "", f"A list ({len(agg['Alist'])}): " + ", ".join(agg["Alist"]), ""]
    n_svg = 0
    for s in S:
        g = agg["grids"][s]
        for key, (rows, cols, title, kind) in CUTS.items():
            L += ["", f"### {s} — {title}", ""] + md_table(rows, cols, g[key], kind)
            if key in ("dt_xF", "dt_both", "dt_ddF", "fm_xF", "fd_xF"):
                heat_svg(f"{run} {s}: {title}", rows, cols, g[key], kind, OUT / f"heat_{run}_{s}_{key}.svg", ref)
                n_svg += 1
    (OUT / f"tables_{run}.md").write_text("\n".join(L) + "\n")
    print(f"{run}: tables_{run}.md + {n_svg} svg")


if __name__ == "__main__":
    for r in sys.argv[1:]:
        main(r)
