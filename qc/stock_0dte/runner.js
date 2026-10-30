// Browser-side runner of the registered formal runs (ledger docs/research_ledger_stock_0dte.md, 0.7). Runs in a
// QuantConnect page that holds the session, after qc/stock_0dte/fetch.js (window.SX) is loaded; api = POST
// /api/v2/<path> with the session cookie (window.__api).
// FR.go(sha, chunks, frozen, pid): fetches the four engine files at commit `sha` from GitHub, checks sim / simx /
//   optmath / main against the frozen hashes (main before its BOOK and MECH lines are replaced), uploads them, and for
//   each chunk [first, last] in turn replaces the two lines, uploads main.py, checks the project's files byte for byte,
//   compiles, runs the backtest "stock_0dte <first>..<last>" and waits for it. While the next chunk runs it reads the
//   finished one's 100 charts (SX.fetch / SX.stream) over the chunk's own time range and, when the stream is complete
//   (no bad slot, the run's last sequence number, as many values as the run emitted), saves
//   stock_0dte_<first>_<last>.json.gz (SX.save) with meta = {name, bt, pid, book_line, mech_line, hashes, rs}. A chunk
//   that fails is recorded and skipped; an incomplete read is recorded and can be repeated with FR.read(label).
//   (The chart API returns one point per (end - start) / count seconds: a 1970-2033 range merged the one-minute emits.)
// FR.st[label]: status, backtest id, progress, read / save result; FR.log: errors; FR.stop = true ends after the current
//   chunk.
window.FR = {
  st: {},
  log: [],
  stop: false,
  done: false,
  async sha16(text) {
    const h = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
  },
  ymd(s) {
    const [y, m, d] = s.split('-').map(Number);
    return `date(${y}, ${m}, ${d})`;
  },
  sleep(ms) { return new Promise(z => setTimeout(z, ms)); },
  async src(sha) {
    const out = {};
    for (const f of ['main.py', 'sim.py', 'simx.py', 'optmath.py']) {
      const r = await fetch(`https://raw.githubusercontent.com/superdiaodiao/quant_stocks/${sha}/qc/stock_0dte/${f}`, { cache: 'no-store' });
      if (!r.ok) throw new Error(`${f}: HTTP ${r.status}`);
      out[f] = await r.text();
    }
    return out;
  },
  async compile(pid) {
    const c = await __api('compile/create', { projectId: pid });
    if (!c.compileId) throw new Error('compile/create ' + JSON.stringify(c).slice(0, 200));
    for (let i = 0; i < 200; i++) {
      await this.sleep(3000);
      const r = await __api('compile/read', { projectId: pid, compileId: c.compileId });
      if (r.state === 'BuildSuccess') return c.compileId;
      if (r.state === 'BuildError') throw new Error('build error ' + JSON.stringify(r.logs || r).slice(0, 300));
    }
    throw new Error('compile timeout');
  },
  async runChunk(pid, files, frozen, a, b) {
    const label = `${a}..${b}`;
    const st = this.st[label] = { status: 'prep', t0: Date.now() };
    const book = `BOOK = (${this.ymd(a)}, ${this.ymd(b)})`;
    const mech = 'MECH = False';
    let nb = 0, nm = 0;
    const main = files['main.py'].split('\n')
      .map(l => l.startsWith('BOOK = ') ? (nb++, book) : (l.startsWith('MECH = ') ? (nm++, mech) : l)).join('\n');
    if (nb !== 1 || nm !== 1) throw new Error(`BOOK / MECH lines ${nb} ${nm}`);
    const hashes = { main: await this.sha16(main) };
    for (const f of ['sim', 'simx', 'optmath']) hashes[f] = await this.sha16(files[f + '.py']);
    const up = await __api('files/update', { projectId: pid, name: 'main.py', content: main });
    if (!up.success) throw new Error('files/update main.py ' + JSON.stringify(up).slice(0, 200));
    const rd = await __api('files/read', { projectId: pid });
    const want = { 'main.py': main, 'sim.py': files['sim.py'], 'simx.py': files['simx.py'], 'optmath.py': files['optmath.py'] };
    for (const f of rd.files || []) {
      if (f.name in want && f.content !== want[f.name]) throw new Error('project file differs: ' + f.name);
      if (!(f.name in want) && /\.py$/.test(f.name)) throw new Error('unexpected project file: ' + f.name);
    }
    st.status = 'compile';
    const cid = await this.compile(pid);
    st.status = 'run';
    const bt = await __api('backtests/create', { projectId: pid, compileId: cid, backtestName: `stock_0dte ${label}` });
    const bid = bt.backtest && bt.backtest.backtestId;
    if (!bid) throw new Error('backtests/create ' + JSON.stringify(bt).slice(0, 200));
    st.bt = bid;
    for (;;) {
      await this.sleep(30000);
      const q = await __api('backtests/read', { projectId: pid, backtestId: bid });
      const x = q.backtest || {};
      st.progress = x.progress;
      if (x.error || x.stacktrace) throw new Error('backtest error ' + String(x.error || x.stacktrace).slice(0, 300));
      if (x.completed) {
        st.rs = x.runtimeStatistics || {};
        break;
      }
    }
    st.status = 'done';
    st.meta = { name: `stock_0dte ${label}`, bt: bid, pid, book_line: book, mech_line: mech, hashes, rs: st.rs };
    st.t1 = Date.now();
    return st;
  },
  range(label) {
    const [a, b] = label.split('..');
    const t0 = Date.UTC(+a.slice(0, 4), +a.slice(5, 7) - 1 - 4, 1) / 1000;                  // before the warm-up
    const t1 = Date.UTC(+b.slice(0, 4), +b.slice(5, 7) - 1, +b.slice(8, 10)) / 1000 + 40 * 86400;   // after the drain
    return [t0, t1];
  },
  async read(label, nch = 100) {
    const st = this.st[label];
    st.read = 'reading';
    const [t0, t1] = this.range(label);
    let store = await SX.fetch(__api, st.meta.pid, st.meta.bt, t0, t1, nch);
    const s = SX.stream(store, nch);
    store = null;
    const run = (st.meta.rs || {}).RUN || '';
    const seq = +((run.match(/ seq (\d+)/) || [])[1]), emitted = +((run.match(/ emitted (\d+)/) || [])[1]);
    st.read = `bad ${s.bad} vals ${s.vals.length} lastseq ${s.lastseq} maxPts ${s.maxPts} want seq ${seq} emitted ${emitted}`;
    if (s.bad !== 0 || s.lastseq !== seq || s.vals.length !== emitted) {
      st.read = 'INCOMPLETE ' + st.read;
      this.log.push(label + ' read incomplete');
      return st;
    }
    const name = `stock_0dte_${label.replace('..', '_')}.json.gz`;
    st.saved = await SX.save({ runs: [{ vals: s.vals, bad: s.bad, nts: s.nts, lastseq: s.lastseq, meta: st.meta }] }, name);
    st.file = name;
    return st;
  },
  async go(sha, chunks, frozen, pid) {
    const files = await this.src(sha);
    for (const f of ['main', 'sim', 'simx', 'optmath'])
      if (await this.sha16(files[f + '.py']) !== frozen[f]) throw new Error(`${f}.py at ${sha} is not the frozen file`);
    for (const f of ['sim.py', 'simx.py', 'optmath.py']) {
      const r = await __api('files/update', { projectId: pid, name: f, content: files[f] });
      if (!r.success) throw new Error('files/update ' + f);
    }
    const reads = [];
    for (const [a, b] of chunks) {
      if (this.stop) break;
      const label = `${a}..${b}`;
      try {
        await this.runChunk(pid, files, frozen, a, b);
        reads.push(this.read(label).catch(e => { this.st[label].read = 'ERR ' + e.message; this.log.push(`${label} read: ${e.message}`); }));
      } catch (e) {
        this.st[label] = Object.assign(this.st[label] || {}, { status: 'ERR ' + e.message });
        this.log.push(`${label}: ${e.message}`);
      }
    }
    await Promise.all(reads);
    this.done = true;
  },
};
