// Decoder + analysis for qc/leaps/main.py (ledger docs/research_ledger_qc_leaps.md). Runs in the QC browser page.
// LP.parse(vals) -> run; LP.stats(run, x) -> st; LP.report(run, st) -> summary. Fetch/decode: reuse SP.fetch / SP.decode.
(function () {
  const L = [1.25, 1.5, 2.0], E = ['3m', '6m', '12m', '24m'], DL = [0.6, 0.7, 0.8, 0.9], R = ['R13', 'R60', 'R30', 'RB'], RB = ['MON', 'QTR'];
  const K = 384, NO = 768, NI = 384, CLEV = Array.from({ length: 33 }, (_, i) => +(1 + 0.05 * i).toFixed(2)), NCL = 132, NBK = NO + NI + NCL, NOB = NO + NI;
  const HA = 3.83, HOOS = 2.24;
  const cfg = k => ({ rb: k % 2, r: Math.floor(k / 2) % 4, dl: Math.floor(k / 8) % 4, e: Math.floor(k / 32) % 4, l: Math.floor(k / 128) });
  const name = k => { const c = cfg(k); return `L${L[c.l]}-${E[c.e]}-d${DL[c.dl]}-${R[c.r]}-${RB[c.rb]}`; };
  const clIdx = (li, reb, fin) => NOB + (li * 2 + reb) * 2 + fin;
  const unpack = (x, k, base) => { const o = new Array(k); for (let i = k - 1; i >= 0; i--) { o[i] = x % base; x = Math.floor(x / base); } return o; };
  const inH1 = ym => ym >= 201203 && ym <= 201812, inH2 = ym => ym >= 201901 && ym <= 202605;
  function parse(v) {
    const run = { months: [] }; let i = 0; const nP = Math.ceil(NBK / 3), nD = Math.ceil(NBK / 5);
    const dd = at => { const o = new Float64Array(NBK); for (let q = 0; q < nD; q++) { const a = unpack(v[at + q], 5, 1000); for (let z = 0; z < 5; z++) { const j = q * 5 + z; if (j < NBK) o[j] = a[z] / 1000; } } return o; };
    while (i < v.length) {
      const x = v[i];
      if (x >= 9e14 && x < 9.1e14) {
        const bench = v.slice(i + 1, i + 4).map(y => (y - 5e9) / 1e8), r = new Float64Array(NBK);
        for (let q = 0; q < nP; q++) { const a = unpack(v[i + 4 + q], 3, 100000); for (let z = 0; z < 3; z++) { const j = q * 3 + z; if (j < NBK) r[j] = (a[z] - 50000) / 1e4; } }
        run.months.push({ ym: x - 9e14, bench, r }); i += 4 + nP;
      } else if (x === 9.3e14) { run.ddH1 = dd(i + 1); const b = v[i + 1 + nD]; run.bddH1 = [Math.floor(b / 1e4) / 1e4, (b % 1e4) / 1e4]; i += 2 + nD; }
      else if (x === 9.5e14) {
        run.ddH2 = dd(i + 1); run.ddF = dd(i + 1 + nD); const b0 = v[i + 1 + 2 * nD], b1 = v[i + 2 + 2 * nD];
        run.bdd2 = [[Math.floor(b0 / 1e4) / 1e4, (b0 % 1e4) / 1e4], [Math.floor(b1 / 1e4) / 1e4, (b1 % 1e4) / 1e4]]; i += 3 + 2 * nD;
        if (v[i] !== 9.6e14) throw new Error('diag marker missing ' + i);
        run.diag = []; for (let b = 0; b < NOB; b++) { const a = v[i + 1 + b], c = v[i + 1 + NOB + b]; run.diag.push({ ex: Math.floor(a / 1e10) / 1e4, bor: Math.floor((a % 1e10) / 1e5) / 1e4, theta: ((a % 1e5) - 50000) / 1e5, cost: Math.floor(c / 1e7) / 1e5, zero: Math.floor((c % 1e7) / 1e4) / 1e3, rolls: c % 1e4 }); }
        i += 1 + 2 * NOB; if (v[i] !== 9.7e14) throw new Error('end marker missing ' + i); run.complete = true; break;
      } else throw new Error('unexpected ' + x + ' at ' + i);
    }
    return run;
  }
  const mean = a => a.reduce((x, y) => x + y, 0) / a.length;
  const sd = a => { const m = mean(a); return Math.sqrt(a.reduce((x, y) => x + (y - m) ** 2, 0) / (a.length - 1)); };
  const ann = a => Math.pow(a.reduce((x, y) => x * (1 + y), 1), 12 / a.length) - 1;
  const tstat = a => { const s = sd(a); return s > 0 ? mean(a) / (s / Math.sqrt(a.length)) : 0; };
  const median = a => { const b = a.slice().sort((x, y) => x - y), n = b.length; return n % 2 ? b[(n - 1) / 2] : (b[n / 2 - 1] + b[n / 2]) / 2; };
  function reg(y, x, rf) { const Y = y.map((v, i) => v - rf[i]), X = x.map((v, i) => v - rf[i]), n = Y.length, mx = mean(X), my = mean(Y); let sxy = 0, sxx = 0; for (let i = 0; i < n; i++) { sxy += (X[i] - mx) * (Y[i] - my); sxx += (X[i] - mx) ** 2; } const b = sxy / sxx, a = my - b * mx; let se = 0; for (let i = 0; i < n; i++) se += (Y[i] - a - b * X[i]) ** 2; return { b, a: a * 12, ta: a / Math.sqrt(se / (n - 2) * (1 / n + mx * mx / sxx)) }; }
  function stats(run, x) {
    x = x || 0;
    const ms = run.months.filter(m => inH1(m.ym) || inH2(m.ym)), n1 = ms.filter(m => inH1(m.ym)).length;
    const ser = b => ms.map(m => m.r[b]), J = ms.map(m => m.bench[0]), Q = ms.map(m => m.bench[1]), RF = ms.map(m => m.bench[2]);
    const sum = (r, b) => ({ a1: ann(r.slice(0, n1)), a2: ann(r.slice(n1)), aF: ann(r), dd1: b === undefined ? NaN : run.ddH1[b], dd2: b === undefined ? NaN : run.ddH2[b], ddF: b === undefined ? NaN : run.ddF[b] });
    const B = { ONEQ: { ...sum(J), dd1: run.bddH1[0], dd2: run.bdd2[0][0], ddF: run.bdd2[0][1] }, QQQ: { ...sum(Q), dd1: run.bddH1[1], dd2: run.bdd2[1][0], ddF: run.bdd2[1][1] }, BIL: sum(RF) };
    const yrs = [...new Set(ms.map(m => Math.floor(m.ym / 100)))];
    const yearly = r => yrs.map(y => ms.reduce((a, m, i) => Math.floor(m.ym / 100) === y ? a * (1 + r[i]) : a, 1) - 1);
    const clSer = {}; for (let li = 0; li < 33; li++) for (let rb = 0; rb < 2; rb++) for (let f = 0; f < 2; f++) clSer[`${li}_${rb}_${f}`] = ser(clIdx(li, rb, f));
    const interp = (lev, rb, f) => { const p = Math.max(0, Math.min(31.999, (lev - 1) / 0.05)), lo = Math.floor(p), w = p - lo; const a = clSer[`${lo}_${rb}_${f}`], b = clSer[`${lo + 1}_${rb}_${f}`]; return a.map((v, i) => v * (1 - w) + b[i] * w); };
    const cf = [];
    for (let k = 0; k < K; k++) {
      const b = 2 * k + x, r = ser(b), e = r.map((v, i) => v - J[i]), dg = run.diag[b], c = cfg(k);
      const o = { k, ...sum(r, b), t1: tstat(e.slice(0, n1)), t2: tstat(e.slice(n1)), tF: tstat(e), e, ex: dg.ex, bor: dg.bor, theta: dg.theta, cost: dg.cost, rolls: dg.rolls };
      o.x1 = o.a1 - B.ONEQ.a1; o.x2 = o.a2 - B.ONEQ.a2; o.xF = o.aF - B.ONEQ.aF; o.both = o.x1 > 0 && o.x2 > 0; o.A = o.both && o.tF >= HA;
      o.B = B.ONEQ.dd1 - o.dd1 >= 0.10 && o.a1 >= B.ONEQ.a1 - 0.03 && B.ONEQ.dd2 - o.dd2 >= 0.10 && o.a2 >= B.ONEQ.a2 - 0.03;
      const cl0 = interp(o.ex, c.rb, 0), cl1 = interp(o.ex, c.rb, 1);
      o.clF0 = ann(cl0); o.clF1 = ann(cl1); o.gap0 = o.aF - o.clF0; o.gap1 = o.aF - o.clF1;
      o.gap0_1 = ann(r.slice(0, n1)) - ann(cl0.slice(0, n1)); o.gap0_2 = ann(r.slice(n1)) - ann(cl0.slice(n1));
      o.rate = o.bor > 0 ? o.theta / o.bor : NaN;
      const rq = reg(r, Q, RF), rj = reg(r, J, RF); o.bQ = rq.b; o.aQ = rq.a; o.taQ = rq.ta; o.bJ = rj.b; o.aJ = rj.a; o.taJ = rj.ta;
      const yy = yearly(r); o.worst = Math.min(...yy); o.worstY = yrs[yy.indexOf(o.worst)];
      // integer 10k book
      if (x === 0) { const bi = NO + k, ri = ser(bi), di = run.diag[bi]; o.int = { aF: ann(ri), a1: ann(ri.slice(0, n1)), a2: ann(ri.slice(n1)), ddF: run.ddF[bi], zero: di.zero, ex: di.ex }; }
      cf.push(o);
    }
    const rfAnn = ann(RF);
    B.CL = {}; for (const lev of [1.0, 1.25, 1.5, 2.0]) { const li = Math.round((lev - 1) / 0.05); for (const f of [0, 1]) { const b = clIdx(li, 0, f), r = ser(b); B.CL[`L${lev}_MON_${f ? 'fin1.5' : 'free'}`] = { ...sum(r, b), worst: Math.min(...yearly(r)) }; } }
    B.yearsONEQ = yearly(J); B.yearsQQQ = yearly(Q); B.yrs = yrs; B.rfAnn = rfAnn;
    return { B, cf, x, n1 };
  }
  const p = v => (v * 100).toFixed(1);
  const row = o => `${name(o.k)} H1 ${p(o.a1)}/${p(-o.dd1)} H2 ${p(o.a2)}/${p(-o.dd2)} F ${p(o.aF)}/${p(-o.ddF)} tF ${o.tF.toFixed(2)} ex ${o.ex.toFixed(2)} vsCLfree ${p(o.gap0)} (H1 ${p(o.gap0_1)} H2 ${p(o.gap0_2)}) vsCL+1.5 ${p(o.gap1)} theta ${p(o.theta)} cost ${p(o.cost)} bor ${o.bor.toFixed(2)} rate ${p(o.rate)} worst ${o.worstY}:${p(o.worst)} bQ ${o.bQ.toFixed(2)} aQ ${p(o.aQ)}(t${o.taQ.toFixed(1)})` + (o.int ? ` | 10k F ${p(o.int.aF)} dd ${p(-o.int.ddF)} zero ${p(o.int.zero)}% ex ${o.int.ex.toFixed(2)}` : '');
  function report(run, st) {
    const C = st.cf, J = st.B.ONEQ, rep = {};
    const bs = b => `H1 ${p(b.a1)}/${p(-b.dd1)} H2 ${p(b.a2)}/${p(-b.dd2)} F ${p(b.aF)}/${p(-b.ddF)}` + (b.worst !== undefined ? ` worst ${p(b.worst)}` : '');
    rep.bench = { ONEQ: bs(st.B.ONEQ), QQQ: bs(st.B.QQQ), BIL: bs(st.B.BIL), ...Object.fromEntries(Object.entries(st.B.CL).map(([k, b]) => [k, bs(b)])), rf: p(st.B.rfAnn), years: st.B.yrs.join(' '), ONEQy: st.B.yearsONEQ.map(p).join(' '), QQQy: st.B.yearsQQQ.map(p).join(' ') };
    rep.counts = { n: C.length, A: C.filter(o => o.A).length, B: C.filter(o => o.B).length, both: C.filter(o => o.both).length, beatCLfree: C.filter(o => o.gap0 > 0).length, beatCLfin: C.filter(o => o.gap1 > 0).length, aQ_t_ge_2: C.filter(o => o.taQ >= 2).length, aQ_t_le_m2: C.filter(o => o.taQ <= -2).length };
    rep.median = `medF ${p(median(C.map(o => o.aF)))} medDD ${p(-median(C.map(o => o.ddF)))} medGapFree ${p(median(C.map(o => o.gap0)))} medGapFin ${p(median(C.map(o => o.gap1)))} medTheta ${p(median(C.map(o => o.theta)))} medCost ${p(median(C.map(o => o.cost)))} medRate ${p(median(C.map(o => o.rate)))} medEx ${median(C.map(o => o.ex)).toFixed(2)}`;
    const ax = { l: L, e: E, dl: DL, r: R, rb: RB }; rep.axes = {};
    for (const [a, vals] of Object.entries(ax)) rep.axes[a] = vals.map((v, i) => { const c = C.filter(o => cfg(o.k)[a] === i); return `${v}: F ${p(median(c.map(o => o.aF)))} DD ${p(-median(c.map(o => o.ddF)))} ex ${median(c.map(o => o.ex)).toFixed(2)} gapFree ${p(median(c.map(o => o.gap0)))} gapFin ${p(median(c.map(o => o.gap1)))} theta ${p(median(c.map(o => o.theta)))} cost ${p(median(c.map(o => o.cost)))} rate ${p(median(c.map(o => o.rate)))} worst ${p(median(c.map(o => o.worst)))} both ${c.filter(o => o.both).length}/${c.length} A ${c.filter(o => o.A).length}`; });
    rep.le = []; for (let l = 0; l < 3; l++) for (let e = 0; e < 4; e++) { const c = C.filter(o => cfg(o.k).l === l && cfg(o.k).e === e); rep.le.push(`L${L[l]} ${E[e]}: F ${p(median(c.map(o => o.aF)))} DD ${p(-median(c.map(o => o.ddF)))} ex ${median(c.map(o => o.ex)).toFixed(2)} gapFree ${p(median(c.map(o => o.gap0)))} theta ${p(median(c.map(o => o.theta)))} cost ${p(median(c.map(o => o.cost)))} rate ${p(median(c.map(o => o.rate)))}` + (c[0].int ? ` 10k F ${p(median(c.map(o => o.int.aF)))} zero ${p(median(c.map(o => o.int.zero)))}%` : '')); }
    rep.top = C.slice().sort((a, b) => b.tF - a.tF).slice(0, 4).map(row);
    rep.bestGap = C.slice().sort((a, b) => b.gap0 - a.gap0).slice(0, 4).map(row);
    rep.worstGap = C.slice().sort((a, b) => a.gap0 - b.gap0).slice(0, 2).map(row);
    if (st.x === 0) {
      const f1 = C.slice().sort((a, b) => (b.x1 - a.x1) || (b.t1 - a.t1))[0], f2 = C.slice().sort((a, b) => (b.x2 - a.x2) || (b.t2 - a.t2))[0];
      const pooled = f1.e.slice(st.n1).concat(f2.e.slice(0, st.n1)), tp = tstat(pooled);
      const b1 = C.filter(o => o.a1 >= J.a1 - 0.03).sort((a, b) => a.dd1 - b.dd1)[0], b2 = C.filter(o => o.a2 >= J.a2 - 0.03).sort((a, b) => a.dd2 - b.dd2)[0];
      rep.oos = { fold1: row(f1), fold2: row(f2), pooled_t: tp.toFixed(2), passA: f1.x2 > 0 && f2.x1 > 0 && tp >= HOOS, foldB1: b1 ? row(b1) : 'none', foldB2: b2 ? row(b2) : 'none',
        passB: !!(b1 && b2 && J.dd2 - b1.dd2 >= 0.1 && b1.a2 >= J.a2 - 0.03 && J.dd1 - b2.dd1 >= 0.1 && b2.a1 >= J.a1 - 0.03), keys: [f1.k, f2.k] };
    }
    return rep;
  }
  window.LP = { parse, stats, report, name, cfg, row, K };
})();
