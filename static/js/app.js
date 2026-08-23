'use strict';

/* app.js — ARGUS://TERMINAL boot, state, command parser, staged pipeline.

   Version query on each import so a redeploy can never pair a fresh app.js
   with a stale cached sub-module. Bump together with the ?v= in index.html. */

import * as api from './api.js?v=17';
import { drawChart, makeResponsive } from './chart.js?v=17';
import { renderMarkdown } from './md.js?v=17';
import * as prefs from './theme.js?v=17';
import * as ui from './ui.js?v=17';
import * as views from './views.js?v=17';

const $  = ui.$;
const $$ = ui.$$;

const state = {
  info: null,          // /api/info — portfolio, watchlist, brent levels, geo map
  health: null,
  brent: null,
  portfolio: [],       // live positions from /api/portfolio
  rows: [],            // merged watchlist rows
  provider: 'perplexity',

  ticker: null,
  stock: null,
  quant: null,
  history: null,
  period: '1y',

  tabs: [],            // [{sym, html}] — rendered output cached per ticker
  active: null,
  busy: false,
  run: null,           // {controller, cancelled}
  cancelType: null,    // aborts an in-flight typewriter
  profile: null,       // onboarding answers — drives the tailored defaults
  view: 'research',    // research | portfolio | profile
};

const FOLD_KEY  = 'argus.folded';
const FOCUS_KEY = 'argus.focus';

/* The watchlist used to live in localStorage. It is server-side now, so an
   add or remove survives a browser wipe and reaches the research prompts. */

/* ── Boot ────────────────────────────────────────────────────────────── */

async function bootSequence() {
  const lines = [{ t: '> ESTABLISHING SESSION…', c: 'txt' }];

  // Everything the boot log claims is actually fetched here first. info's
  // failure is captured rather than swallowed to null — an /api/info 500
  // used to render identically to "you haven't mapped any geo events yet",
  // which is a real backend failure reading as a normal empty state.
  const [infoResult, health, brent, portfolio] = await Promise.all([
    api.getInfo().then((v) => ({ ok: true, value: v })).catch((err) => ({ ok: false, error: err })),
    api.getHealth().catch(() => null),
    api.getBrent().catch(() => null),
    api.getPortfolio().catch(() => null),
  ]);

  const info = infoResult.ok ? infoResult.value : null;
  state.info = info;
  state.health = health;
  state.brent = brent && !brent.error ? brent : null;
  state.portfolio = portfolio?.positions || [];
  state.totals = portfolio?.totals || null;
  state.provider = health?.checks?.perplexity_key ? 'perplexity' : 'groq';

  const ok = (t) => ({ t: `  ✓ ${t}`, c: 'green' });
  const warn = (t) => ({ t: `  ⚠ ${t}`, c: 'gold' });
  const bad = (t) => ({ t: `  ✕ ${t}`, c: 'red' });

  lines.push({ t: '> LOADING INVESTMENT FRAMEWORK', c: 'txt' });
  lines.push({ t: '  [████████████████████] 100%', c: 'accent' });
  lines.push({ t: '> CHECKING DATA ADAPTERS…', c: 'txt' });

  lines.push(state.brent
    ? ok(`BRENT_CRUDE_FEED …… LIVE @ $${Number(state.brent.brent_price).toFixed(2)}`)
    : bad('BRENT_CRUDE_FEED …… UNAVAILABLE'));

  lines.push(portfolio
    ? ok(`EQUITY_PRICE_FEED … ${state.portfolio.length} POSITIONS`)
    : bad('EQUITY_PRICE_FEED … UNAVAILABLE'));

  const geoN = Object.keys(info?.geo_transmission || {}).length;
  lines.push(infoResult.ok
    ? (geoN ? ok(`GEO_TRANSMISSION …… ${geoN} VECTORS MAPPED`) : warn('GEO_TRANSMISSION …… NOT CONFIGURED'))
    : bad(`INFO_ENDPOINT ……… ${infoResult.error?.message || 'failed'}`));

  if (health?.checks?.perplexity_key)      lines.push(ok('PERPLEXITY_API …… CONFIGURED · LIVE WEB'));
  else if (health?.checks?.groq_key)       lines.push(warn('GROQ_API ………… FALLBACK · NO WEB SEARCH'));
  else                                     lines.push(warn('AI_PROVIDER ……… NOT CONFIGURED'));

  lines.push(api.auth.has()
    ? ok('SESSION ………… ACTIVE · AI ARMED')
    : warn('SESSION ………… LOCKED · QUANT ONLY'));

  if (state.brent) {
    lines.push({ t: `> MACRO GATE ${state.brent.gate_open ? 'OPEN' : 'CLOSED'} · ${String(state.brent.signal || '').toUpperCase()}`,
                 c: state.brent.gate_open ? 'green' : 'red' });
  }
  lines.push({ t: '> FRAMEWORK LOADED · 5 VERTICALS ARMED', c: 'accent' });
  lines.push({ t: '> ARGUS SEES ALL. TYPE A TICKER OR COMMAND.', c: 'accent', pause: 500 });

  return lines;
}

/* ── Painting ────────────────────────────────────────────────────────── */

function paintAll() {
  ui.renderStatus(state.health, state.provider === 'groq' ? 'GROQ' : 'PERPLEXITY');
  ui.renderKeyState(api.auth.has());
  ui.renderBrent(state.brent, state.info?.brent_levels);
  ui.renderGeo(state.info?.geo_transmission, state.info?.portfolio || [], (t) => runCommand(`research ${t}`));
  buildRows();
  ui.renderTape(state.rows.filter(r => r.last !== null).map(r => ({
    sym: r.sym, price: r.last, chg: r.chg, currency: r.currency,
  })));
}

function buildRows() {
  const rows = [];

  for (const p of state.portfolio) {
    const mp = p.my_position || {};
    const price = p.price === null || p.price === undefined ? null : Number(p.price);
    const stop = mp.stop_loss ?? null, trim = mp.trim_at ?? null;
    const st = ui.riskState(price, stop === null ? null : Number(stop), trim === null ? null : Number(trim));
    rows.push({
      sym: p.ticker, last: price, currency: p.currency,
      chg: mp.unrealized_pnl_pct ?? null,
      sig: st.label, dot: st.dot, held: true, sector: p.sector,
    });
  }

  const wl = state.info?.watchlist || {};
  for (const sym of Object.keys(wl)) {
    if (rows.some(r => r.sym === sym)) continue;
    rows.push({ sym, last: null, currency: null, chg: null, sig: 'WATCH', dot: 'amber', held: false });
  }
  state.rows = rows;
  ui.renderWatchlist(rows, (s) => runCommand(`research ${s}`), (s) => inspect(s));
  ui.renderWatchFooter(rows, state.totals);
  loadSparklines();
  loadWatchPrices();
}

/* Sparklines land after the table so rows appear immediately. Failures are
   silent — a row without a sparkline is still a complete row. */
function loadSparklines() {
  for (const r of state.rows) {
    if (!r.held) continue;
    api.getHistory(r.sym, '1mo')
      .then(h => {
        const v = (h.points || []).map(p => p.c);
        if (v.length > 1) ui.attachSparkline(r.sym, v, v[v.length - 1] >= v[0]);
      })
      .catch(() => {});
  }
}

/* Watchlist entries have no /api/portfolio row, so their price comes from the
   free quant endpoint — one call each, fired only once per session. */
let pricesFetched = false;
function loadWatchPrices() {
  if (pricesFetched) return;
  pricesFetched = true;
  for (const r of state.rows) {
    if (r.held || r.last !== null) continue;
    api.getQuant(r.sym)
      .then(d => {
        const price = d?.stock?.price;
        if (price === null || price === undefined) return;
        const row = state.rows.find(x => x.sym === r.sym);
        if (!row) return;
        row.last = Number(price);
        row.currency = d.stock.currency;
        ui.renderWatchlist(state.rows, (s) => runCommand(`research ${s}`), (s) => inspect(s));
        ui.renderWatchFooter(state.rows, state.totals);
        ui.renderTape(state.rows.filter(x => x.last !== null)
          .map(x => ({ sym: x.sym, price: x.last, chg: x.chg, currency: x.currency })));
      })
      .catch(() => {});
  }
}

/* ── Tabs ────────────────────────────────────────────────────────────── */

function showTab(sym) {
  const t = state.tabs.find(x => x.sym === sym);
  if (!t) return;
  state.active = sym;
  ui.clearOut();
  $('out').innerHTML = t.html;
  wireChartControls();
  ui.renderTabs(state.tabs, state.active, showTab, closeTab);
  ui.markSelected(sym);
  ui.setStatus('COMPLETE', 'ok');
}

function closeTab(sym) {
  state.tabs = state.tabs.filter(t => t.sym !== sym);
  if (state.active === sym) {
    if (state.tabs.length) showTab(state.tabs[state.tabs.length - 1].sym);
    else { state.active = null; ui.clearOut(); welcome(); ui.setStatus('READY'); }
  }
  ui.renderTabs(state.tabs, state.active, showTab, closeTab);
}

function openTab(sym) {
  if (!state.tabs.some(t => t.sym === sym)) {
    state.tabs.push({ sym, html: '' });
    if (state.tabs.length > 6) state.tabs.shift();
  }
  state.active = sym;
  ui.renderTabs(state.tabs, state.active, showTab, closeTab);
  ui.markSelected(sym);
}

function cacheTab(sym) {
  const t = state.tabs.find(x => x.sym === sym);
  if (t) t.html = $('out').innerHTML;
}

/* ── Welcome / help ──────────────────────────────────────────────────── */

function welcome() {
  const o = ui.out();
  ui.writeTitle('ARGUS://READY');
  const b = document.createElement('div');
  b.className = 'r-body';
  b.style.marginTop = '10px';
  b.innerHTML =
    `<p>Type <code>research &lt;SYM&gt;</code> for the full staged brief, or click any watchlist row.</p>
     <p><code>quant &lt;SYM&gt;</code> runs the free metrics only — no API spend.
        <code>help</code> lists every command.</p>
     <p class="dim">Alt-click a watchlist row (or press <code>i</code>) to open the inspector.</p>`;
  o.appendChild(b);
}

function help() {
  ui.clearOut();
  ui.writeTitle('ARGUS://COMMANDS');
  const rows = [
    ['research <SYM>', 'Full staged brief — quant, chart, then the paid AI modules behind a cost prompt.'],
    ['quant <SYM>', 'Fundamentals, quant grid and chart only. Always free.'],
    ['chart <SYM>', 'Price history with DCF, target, cost and stop reference lines.'],
    ['pos <SYM>', 'Open the position inspector.'],
    ['scan', 'Quant sweep across every holding. Free.'],
    ['brent', 'Brent framework detail and the current gate.'],
    ['add <SYM>', 'Add a ticker to this browser\'s watchlist.'],
    ['rm <SYM>', 'Remove a locally-added ticker.'],
    ['portfolio', 'Invested, P&L, XIRR, weights and the analyst outlook.'],
    ['profile', 'Edit tailoring, positions, buy dates and watchlist.'],
    ['focus', 'Reading mode — hide the side panels. Esc restores.'],
    ['clear', 'Clear the output pane.'],
    ['help', 'This list.'],
  ];
  const b = document.createElement('div');
  b.className = 'r-body';
  b.style.marginTop = '10px';
  b.innerHTML = rows.map(([c, d]) =>
    `<div style="display:flex;gap:14px;margin-bottom:5px">
       <code style="min-width:130px;flex:0 0 130px">${c.replace(/</g, '&lt;')}</code>
       <span class="dim">${d}</span></div>`).join('');
  ui.out().appendChild(b);
  ui.setStatus('READY');
}

/* ── Chart ───────────────────────────────────────────────────────────── */

function chartRefs() {
  const refs = [];
  const q = state.quant || {}, s = state.stock || {}, mp = s.my_position || {};
  const add = (label, v, key) => {
    if (Number.isFinite(Number(v))) refs.push({ label, value: Number(v), key });
  };
  add('DCF', q.dcf_intrinsic_value, 'dcf');
  add('TARGET', s.analyst_target, 'target');
  add('COST', mp.avg_cost, 'cost');
  add('STOP', mp.stop_loss, 'stop');
  return refs;
}

function paintChart() {
  const host = document.getElementById('chart');
  if (host && state.history) drawChart(host, state.history, { refs: chartRefs() });
}

function chartBlock() {
  const box = document.createElement('div');
  box.className = 'chart-box';
  box.innerHTML =
    `<div class="chart-hd">
       <span class="panel-h">PRICE://${state.ticker}</span>
       <div style="display:flex;align-items:center;gap:10px">
         <span id="chart-chg" class="dim tabnum" style="font-size:9px"></span>
         <div class="rng" role="group" aria-label="Chart range">
           ${['1mo', '6mo', '1y', '5y'].map(p =>
             `<button type="button" data-p="${p}"${p === state.period ? ' class="on"' : ''}>${p.toUpperCase()}</button>`).join('')}
         </div>
       </div>
     </div>
     <div id="chart" style="min-height:170px"></div>`;
  return box;
}

function wireChartControls() {
  document.querySelectorAll('.rng button').forEach(b => {
    b.onclick = async () => {
      if (b.classList.contains('on') || !state.ticker) return;
      document.querySelectorAll('.rng button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      state.period = b.dataset.p;
      await loadChart(state.ticker, state.period);
      cacheTab(state.ticker);
    };
  });
}

async function loadChart(sym, period) {
  const host = document.getElementById('chart');
  if (host) host.innerHTML = '<div class="chart-empty">Loading price history…</div>';
  try {
    state.history = await api.getHistory(sym, period);
    paintChart();
    const chg = document.getElementById('chart-chg');
    if (chg) {
      const v = state.history.change_pct;
      chg.textContent = v === null || v === undefined ? '' : `${ui.pct(v)} over period`;
      chg.className = `tabnum ${Number(v) >= 0 ? 'pos' : 'neg'}`;
      chg.style.fontSize = '9px';
    }
    return true;
  } catch (err) {
    state.history = null;
    if (host) host.innerHTML = `<div class="chart-empty">${err.message}</div>`;
    return false;
  }
}

/* ── Free phase: fundamentals + quant + chart ────────────────────────── */

async function loadFree(sym, { withPipeline = true } = {}) {
  const o = ui.out();
  ui.setStatus('RUNNING', 'run');

  let demo;
  const t0 = performance.now();
  try {
    demo = await api.getQuant(sym);
  } catch (err) {
    if (withPipeline) for (const s of ['fundamentals', 'quant', 'brent']) ui.pipeline.set(s, 'failed');
    ui.setStatus('ERROR', 'err');
    ui.toast(err.message, 'error');
    return false;
  }

  state.stock = demo.stock || {};
  state.quant = demo.quant || {};

  /* An unknown symbol does not fail upstream — yfinance answers with a dict of
     nulls, so the API returns 200 and looks valid. A missing price is the only
     reliable "no such ticker" signal. */
  if (state.stock.error || state.stock.price === null || state.stock.price === undefined) {
    if (withPipeline) for (const s of ['fundamentals', 'quant', 'brent']) ui.pipeline.set(s, 'failed');
    ui.setStatus('NO DATA', 'err');
    ui.toast(state.stock.note || state.stock.error ||
      `No market data for ${sym}. Indian listings need .NS (NSE) or .BO (BSE).`, 'error');
    return false;
  }

  const ms = performance.now() - t0;
  if (withPipeline) {
    ui.pipeline.done('fundamentals', ms);
    ui.pipeline.done('quant', ms);
    ui.pipeline.done('brent', ms);
  }
  if (demo.brent) { state.brent = demo.brent; ui.renderBrent(demo.brent, state.info?.brent_levels); }

  // Identity line
  const s = state.stock;
  const id = document.createElement('div');
  id.className = 'r-body';
  id.style.marginTop = '10px';
  const bits = [];
  if (s.pct_from_52w_high !== null && s.pct_from_52w_high !== undefined)
    bits.push(`${ui.pct(s.pct_from_52w_high)} from 52w high`);
  if (s.market_cap) bits.push(`${ui.compact(s.market_cap)} cap`);
  if (s.analyst_rating) bits.push(String(s.analyst_rating).replace(/_/g, ' '));
  id.innerHTML =
    `<div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap">
       <span style="font-size:22px;color:var(--white);font-weight:700" class="tabnum">${ui.money(s.price, s.currency)}</span>
     </div>`;
  const nameLine = document.createElement('div');
  nameLine.className = 'dim';
  nameLine.style.marginTop = '2px';
  nameLine.textContent = [s.name || sym, ...bits].join('  ·  ');
  id.appendChild(nameLine);
  o.appendChild(id);

  ui.renderQuant(o, state.quant, state.stock);

  o.appendChild(chartBlock());
  if (withPipeline) ui.pipeline.set('history', 'running');
  const th = performance.now();
  const okc = await loadChart(sym, state.period);
  if (withPipeline) okc ? ui.pipeline.done('history', performance.now() - th)
                        : ui.pipeline.set('history', 'failed');
  wireChartControls();
  return true;
}

/* ── Staged research ─────────────────────────────────────────────────── */

const AI_MODULES = [
  ['report', 'RESEARCH BRIEF'],
  ['context', 'COMPANY CONTEXT'],
  ['policy', 'POLITICIAN & GOVERNMENT TRADES'],
  ['patterns', 'HIDDEN PATTERNS'],
];

async function research(rawSym, { paid = true, question } = {}) {
  if (state.busy) { ui.toast('A run is already in flight.', 'warn'); return; }

  const sym = String(rawSym || '').trim().toUpperCase().replace(/[^A-Z0-9.=-]/g, '');
  if (!sym) { ui.toast('Usage: research <SYM>', 'warn'); return; }

  state.busy = true;
  state.ticker = sym;
  $('btn-exec').disabled = true;
  const started = performance.now();
  const hasKey = api.auth.has();

  openTab(sym);
  ui.clearOut();
  // A question only narrows the report module's own prompt — context,
  // policy, patterns and synthesis still run (and cost) exactly as before,
  // so the title stays 'STAGED RESEARCH' rather than implying a cheaper or
  // narrower run. The echoed line below is what confirms the question
  // itself was actually captured.
  ui.writeTitle(`ARGUS://${sym} · ${paid ? 'STAGED RESEARCH' : 'QUANT ONLY'}`);
  if (question) ui.writeLine(`❯ ${question}`, 'dim');
  const o = ui.out();
  ui.pipeline.mount(o, !hasKey);
  if (!paid) for (const [k] of AI_MODULES) ui.pipeline.set(k, 'skipped');
  if (!paid) ui.pipeline.set('synthesis', 'skipped');

  ui.pipeline.set('fundamentals', 'running');
  ui.pipeline.set('quant', 'running');
  ui.pipeline.set('brent', 'running');

  try {
    const ok = await loadFree(sym);
    if (!ok) { cacheTab(sym); return; }

    if (!paid) { ui.setStatus('COMPLETE', 'ok'); cacheTab(sym); return; }

    if (!hasKey) {
      ui.setStatus('QUANT ONLY', 'ok');
      const n = document.createElement('div');
      n.className = 'r-body';
      n.style.marginTop = '14px';
      n.innerHTML = `<p class="dim">AI modules locked. Add your agent key in
        <code>◈ CONFIG</code> to unlock live web research, politician-trade disclosure
        analysis, cross-source patterns and the final verdict.</p>`;
      o.appendChild(n);
      cacheTab(sym);
      return;
    }

    const sel = await ui.showCostModal(sym, state.provider);
    if (!sel) {
      for (const [k] of AI_MODULES) ui.pipeline.set(k, 'skipped');
      ui.pipeline.set('synthesis', 'skipped');
      ui.setStatus('QUANT ONLY', 'ok');
      ui.toast('AI stages skipped — quant, chart and P&L are loaded.', 'info');
      cacheTab(sym);
      return;
    }
    for (const [k] of AI_MODULES) if (!sel[k]) ui.pipeline.set(k, 'skipped');
    if (!sel.synthesis) ui.pipeline.set('synthesis', 'skipped');

    const controller = new AbortController();
    state.run = { controller, cancelled: false };
    $('btn-stop').classList.remove('hidden');

    const texts = {};
    for (const [key, label] of AI_MODULES) {
      if (!sel[key]) continue;
      if (state.run.cancelled) { ui.pipeline.set(key, 'skipped'); continue; }

      ui.pipeline.set(key, 'running');
      const t = performance.now();
      try {
        const data = await api.getResearchModule(sym, key, {
          question: key === 'report' ? question : undefined,
          signal: controller.signal,
        });
        texts[key] = data.output || '';
        ui.pipeline.done(key, performance.now() - t);

        const head = document.createElement('div');
        head.className = 'r-head';
        head.textContent = `${label}${data.provider === 'groq' ? '  ·  GROQ · NO LIVE WEB' : ''}`;
        o.appendChild(head);
        await typeBlock(o, data.output || '');
        cacheTab(sym);
      } catch (err) {
        if (err.kind === 'aborted') { ui.pipeline.set(key, 'cancelled'); state.run.cancelled = true; }
        else if (err.kind === 'unauthorized') {
          ui.pipeline.set(key, 'failed');
          state.run.cancelled = true;
          ui.toast(err.message, 'error');
          openConfig();
        } else {
          ui.pipeline.set(key, 'failed');
          ui.toast(`${key}: ${err.message}`, 'warn');
        }
      }
    }

    const any = Object.values(texts).some(Boolean);
    if (sel.synthesis && !state.run.cancelled && any) {
      ui.pipeline.set('synthesis', 'running');
      const t = performance.now();
      try {
        const res = await api.postSynthesis(sym, {
          report: texts.report || '', context: texts.context || '',
          policy: texts.policy || '', patterns: texts.patterns || '',
        }, { signal: controller.signal });

        ui.renderVerdict(o, res.synthesis);
        if (res.bull_case || res.bear_case) {
          o.appendChild(Object.assign(document.createElement('div'),
            { className: 'r-head', textContent: 'BULL vs BEAR' }));
          const grid = document.createElement('div');
          grid.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px';
          for (const [txt, cls, lbl] of [[res.bull_case, 'green', '▲ BULL'], [res.bear_case, 'red', '▼ BEAR']]) {
            const c = document.createElement('div');
            c.style.cssText = `border:1px solid var(--border);border-left:2px solid var(--${cls});padding:9px;background:var(--bg-panel)`;
            c.innerHTML = `<div style="font-size:8px;letter-spacing:.1em;color:var(--${cls});margin-bottom:6px">${lbl}</div>`;
            const b = document.createElement('div');
            b.className = 'r-body';
            b.innerHTML = renderMarkdown(txt || '');
            c.appendChild(b);
            grid.appendChild(c);
          }
          o.appendChild(grid);
          if (window.matchMedia('(max-width:820px)').matches) grid.style.gridTemplateColumns = '1fr';
        }
        ui.pipeline.done('synthesis', performance.now() - t);
      } catch (err) {
        ui.pipeline.set('synthesis', err.kind === 'aborted' ? 'cancelled' : 'failed');
        if (err.kind !== 'aborted') ui.toast(`synthesis: ${err.message}`, 'warn');
      }
    } else if (sel.synthesis) {
      ui.pipeline.set('synthesis', 'skipped');
    }

    ui.setStatus(state.run.cancelled ? 'STOPPED' : 'COMPLETE', state.run.cancelled ? 'err' : 'ok');
    cacheTab(sym);
  } finally {
    $('btn-stop').classList.add('hidden');
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    ui.writeLine(`— run complete in ${secs}s —`, 'dim');
    cacheTab(sym);
    state.run = null;
    state.busy = false;
    $('btn-exec').disabled = false;
  }
}

/* Typewriter that can be interrupted by Stop. */
function typeBlock(container, markdown) {
  /* Belt-and-braces: the reveal is decoration on a paid pipeline, so it
     resolves at most once and is force-completed after a ceiling no matter
     what. A stalled animation must never strand a run that already spent
     money — that is exactly what a background-tab setTimeout clamp did. */
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => { if (!settled) { settled = true; state.cancelType = null; resolve(); } };
    const cancel = ui.typeHTML(container, renderMarkdown(markdown), finish);
    state.cancelType = () => { cancel(); finish(); };
    setTimeout(() => { if (!settled) { cancel(); finish(); } }, 8000);
  });
}

/* ── Scan ────────────────────────────────────────────────────────────── */

async function scan() {
  if (state.busy) { ui.toast('A run is already in flight.', 'warn'); return; }
  state.busy = true;
  $('btn-exec').disabled = true;
  ui.setStatus('SCANNING', 'run');

  openTab('SCAN');
  ui.clearOut();
  ui.writeTitle('ARGUS://SCAN · ALL HOLDINGS');
  ui.writeLine('Free quant sweep — no API spend.', 'dim');
  const o = ui.out();

  const held = state.rows.filter(r => r.held);
  for (const r of held) {
    const line = document.createElement('div');
    line.className = 'pl run';
    line.innerHTML = `<span class="ic spin">◐</span><span class="nm">${r.sym}</span><span class="mt">…</span>`;
    o.appendChild(line);
    o.scrollTop = o.scrollHeight;
    try {
      const d = await api.getQuant(r.sym);
      const q = d.quant || {}, s = d.stock || {};
      const up = q.dcf_upside_pct;
      const flags = (q.quant_flags || []).length;
      line.className = 'pl done';
      line.innerHTML =
        `<span class="ic">✓</span><span class="nm">${r.sym}  ` +
        `<span class="dim">${ui.money(s.price, s.currency)} · ` +
        `DCF ${up === null || up === undefined ? 'n/a' : ui.pct(up)} · ${flags} flags</span></span>` +
        `<span class="mt ${r.chg >= 0 ? 'pos' : 'neg'}">${ui.pct(r.chg)}</span>`;
    } catch (err) {
      line.className = 'pl fail';
      line.innerHTML = `<span class="ic">✕</span><span class="nm">${r.sym}</span><span class="mt">${err.message}</span>`;
    }
  }

  ui.writeLine('— scan complete —', 'dim');
  ui.setStatus('COMPLETE', 'ok');
  cacheTab('SCAN');
  state.busy = false;
  $('btn-exec').disabled = false;
}

/* ── Brent detail ────────────────────────────────────────────────────── */

function brentDetail() {
  openTab('BRENT');
  ui.clearOut();
  ui.writeTitle('ARGUS://BRENT · MACRO GATE');
  const o = ui.out();
  const b = state.brent;
  if (!b) { ui.writeLine('Brent feed unavailable.', 'dim'); ui.setStatus('ERROR', 'err'); return; }

  const box = document.createElement('div');
  box.className = 'r-body';
  box.style.marginTop = '10px';
  const levels = state.info?.brent_levels || {};
  const rows = Object.values(levels).map(v => {
    const [lo, hi] = v.range;
    const on = v.signal === String(b.signal || '').toUpperCase();
    const label = hi >= 9999 ? `>$${lo}` : lo === 0 ? `<$${hi}` : `$${lo}–${hi}`;
    return `<div style="display:flex;gap:12px;padding:5px 6px;${on ? 'background:var(--accent-soft);border-left:2px solid var(--accent)' : 'border-left:2px solid transparent'}">
      <span style="min-width:80px;color:${on ? 'var(--white)' : 'var(--txt-head)'}">${label}</span>
      <span style="min-width:150px;color:${on ? 'var(--accent)' : 'var(--txt-muted)'}">${v.signal}</span>
      <span class="dim">${v.action}</span></div>`;
  }).join('');

  box.innerHTML =
    `<div style="font-size:30px;color:var(--white);font-weight:700" class="tabnum">$${Number(b.brent_price).toFixed(2)}</div>
     <div style="margin:8px 0 14px;color:${b.gate_open ? 'var(--green)' : 'var(--red)'}">
       GATE ${b.gate_open ? 'OPEN — new positions allowed' : 'CLOSED — hold, no new buys'}</div>
     ${rows}
     <p class="dim" style="margin-top:14px">${b.framework_note || ''}</p>`;
  o.appendChild(box);
  ui.setStatus('COMPLETE', 'ok');
  cacheTab('BRENT');
}

/* ── Inspector ───────────────────────────────────────────────────────── */

async function inspect(sym) {
  ui.openInspector(sym, { }, null);
  try {
    const d = await api.getQuant(sym);
    ui.openInspector(sym, d.stock || {}, d.quant || {});
  } catch (err) {
    ui.toast(err.message, 'error');
    ui.closeInspector();
  }
}

/* ── Command parser ──────────────────────────────────────────────────── */

const CMDS = ['research', 'quant', 'chart', 'scan', 'brent', 'pos', 'add', 'rm',
              'portfolio', 'profile', 'focus', 'clear', 'help'];

/* Mirrors tools/validator.py's sanitize_question() cap — kept in sync
   deliberately so a long question gets an instant client-side warning
   instead of being silently truncated by the server with no feedback. */
const MAX_QUESTION_LEN = 300;

/** Anything typed after the ticker becomes a free-text research question,
    instead of being silently dropped — see runCommand's ticker/question
    split below. Returns undefined for no question, never an empty string.
    Strips HTML tags before measuring length, mirroring the order
    sanitize_question() applies server-side (strip, then truncate), so the
    two truncation points can't disagree on a question containing
    HTML-like characters. */
function captureQuestion(words) {
  const q = words.join(' ').replace(/<[^>]+>/g, '').trim();
  if (!q) return undefined;
  if (q.length > MAX_QUESTION_LEN) {
    ui.toast(`Question truncated to ${MAX_QUESTION_LEN} characters.`, 'warn');
    return q.slice(0, MAX_QUESTION_LEN);
  }
  return q;
}

async function runCommand(raw) {
  const line = String(raw || '').trim();
  if (!line) return;
  const [head, ...rest] = line.split(/\s+/);
  const cmd = head.toLowerCase();
  const arg = (rest[0] || '').toUpperCase().replace(/[^A-Z0-9.=-]/g, '');

  switch (cmd) {
    case 'help':  help(); return;
    case 'clear': state.tabs = []; state.active = null; ui.renderTabs([], null); ui.clearOut(); welcome(); ui.setStatus('READY'); return;
    case 'scan':  await scan(); return;
    case 'brent': brentDetail(); return;

    case 'pos':
      if (!arg) { ui.toast('Usage: pos <SYM>', 'warn'); return; }
      await inspect(arg); return;

    case 'add': {
      if (!arg) { ui.toast('Usage: add <SYM>', 'warn'); return; }
      if (state.rows.some(r => r.sym === arg)) { ui.toast(`${arg} is already tracked.`, 'info'); return; }
      try {
        await api.addWatch(arg);
        await refreshBook();
        ui.flashRow(arg);
        ui.toast(`${arg} added to the watchlist.`, 'success');
      } catch (err) { ui.toast(err.message, 'error'); }
      return;
    }
    case 'rm': {
      if (!arg) { ui.toast('Usage: rm <SYM>', 'warn'); return; }
      try {
        // Try the watchlist first, then holdings — both are editable now.
        const w = await api.delWatch(arg).catch(() => ({ ok: false }));
        const ok = w.ok || (await api.delPosition(arg).catch(() => ({ ok: false }))).ok;
        if (!ok) { ui.toast(`${arg} is not tracked.`, 'warn'); return; }
        await refreshBook();
        ui.toast(`${arg} removed.`, 'success');
      } catch (err) { ui.toast(err.message, 'error'); }
      return;
    }
    case 'portfolio': await showView('portfolio'); return;
    case 'profile':   await showView('profile');   return;
    case 'focus':     toggleFocus();               return;

    case 'quant':
      if (!arg) { ui.toast('Usage: quant <SYM>', 'warn'); return; }
      await research(arg, { paid: false }); return;

    case 'chart': {
      if (!arg) { ui.toast('Usage: chart <SYM>', 'warn'); return; }
      state.ticker = arg;
      openTab(arg);
      ui.clearOut();
      ui.writeTitle(`ARGUS://${arg} · PRICE`);
      const o = ui.out();
      try { const d = await api.getQuant(arg); state.stock = d.stock || {}; state.quant = d.quant || {}; }
      catch { state.stock = {}; state.quant = {}; }
      o.appendChild(chartBlock());
      await loadChart(arg, state.period);
      wireChartControls();
      ui.setStatus('COMPLETE', 'ok');
      cacheTab(arg);
      return;
    }

    case 'research':
      if (!arg) { ui.toast('Usage: research <SYM>', 'warn'); return; }
      await research(arg, { question: captureQuestion(rest.slice(1)) }); return;

    default: {
      // Bare ticker is the most common input — treat it as research.
      const guess = head.toUpperCase().replace(/[^A-Z0-9.=-]/g, '');
      if (guess && guess.length <= 12 && !CMDS.includes(cmd)) {
        await research(guess, { question: captureQuestion(rest) }); return;
      }
      ui.toast(`Unknown command "${head}". Type help.`, 'warn');
    }
  }
}

/* ── Command hints ───────────────────────────────────────────────────── */

const NO_ARG = ['scan', 'brent', 'portfolio', 'profile', 'focus', 'clear', 'help'];

function wireHints() {
  const input = $('cmd');
  const hint = $('cmd-hint');
  let idx = -1;

  const HINTS = [
    ['research', 'full staged brief'], ['quant', 'free metrics only'],
    ['chart', 'price + reference lines'], ['scan', 'sweep all holdings'],
    ['brent', 'framework detail'], ['pos', 'open inspector'],
    ['portfolio', 'P&L, XIRR, outlook'], ['profile', 'edit positions & tailoring'],
    ['focus', 'reading mode'],
    ['add', 'track a ticker'], ['rm', 'untrack a ticker'],
    ['clear', 'reset output'], ['help', 'all commands'],
  ];

  const render = (list) => {
    if (!list.length) { hint.classList.add('hidden'); idx = -1; return; }
    hint.innerHTML = list.map(([c, d], i) =>
      `<div class="h${i === idx ? ' sel' : ''}" data-c="${c}"><b>${c}</b><span>${d}</span></div>`).join('');
    hint.classList.remove('hidden');
    hint.querySelectorAll('.h').forEach(n => {
      n.onclick = () => {
        input.value = n.dataset.c + (NO_ARG.includes(n.dataset.c) ? '' : ' ');
        hint.classList.add('hidden');
        input.focus();
      };
    });
  };

  input.addEventListener('input', () => {
    const v = input.value.trim().toLowerCase();
    if (!v || v.includes(' ')) { hint.classList.add('hidden'); idx = -1; return; }
    idx = -1;
    render(HINTS.filter(([c]) => c.startsWith(v)));
  });

  input.addEventListener('keydown', (e) => {
    const open = !hint.classList.contains('hidden');
    const items = Array.from(hint.querySelectorAll('.h'));
    if (e.key === 'ArrowDown' && open) { e.preventDefault(); idx = Math.min(items.length - 1, idx + 1); items.forEach((n, i) => n.classList.toggle('sel', i === idx)); return; }
    if (e.key === 'ArrowUp' && open)   { e.preventDefault(); idx = Math.max(0, idx - 1); items.forEach((n, i) => n.classList.toggle('sel', i === idx)); return; }
    if (e.key === 'Escape') { hint.classList.add('hidden'); idx = -1; return; }
    if (e.key === 'Enter') {
      if (open && idx >= 0) {
        e.preventDefault();
        const c = items[idx].dataset.c;
        input.value = c + (NO_ARG.includes(c) ? '' : ' ');
        hint.classList.add('hidden'); idx = -1;
        return;
      }
      hint.classList.add('hidden');
      const v = input.value; input.value = '';
      runCommand(v);
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('#cmdbar')) hint.classList.add('hidden');
  });
}


/* ── Views ───────────────────────────────────────────────────────────────
   The research column swaps content. Ticker tabs and the inspector belong to
   the research view only, so both are hidden elsewhere. */

const VIEW_LABEL = {
  research:  'ARGUS://RESEARCH',
  portfolio: 'ARGUS://PORTFOLIO',
  profile:   'ARGUS://PROFILE',
};

async function showView(name) {
  state.view = name;
  $$('.vb').forEach(b => b.classList.toggle('on', b.dataset.view === name));
  $('tabs').classList.toggle('hidden', name !== 'research');
  // The pane header is the only thing naming the current view once you scroll.
  const label = $('res-label');
  if (label) label.textContent = VIEW_LABEL[name] || 'ARGUS://RESEARCH';
  ui.closeInspector();

  const out = $('out');
  if (name === 'research') {
    ui.setStatus(state.active ? 'COMPLETE' : 'READY', state.active ? 'ok' : '');
    if (state.active) showTab(state.active); else { ui.clearOut(); welcome(); }
    return;
  }
  ui.setStatus(name.toUpperCase(), '');
  out.innerHTML = '';
  if (name === 'portfolio') {
    await views.renderPortfolio(out, { onOutlook: runOutlook });
  } else {
    await views.renderProfile(out, { onChanged: refreshBook });
  }
}

/* One paid call covering every holding — same cost-confirm discipline as the
   research pipeline. */
async function runOutlook(box) {
  if (!api.auth.has()) {
    ui.toast('Unlock with your agent key first — this is a paid call.', 'warn');
    return;
  }
  const ok = await ui.confirmSpend(
    'RUN PORTFOLIO OUTLOOK',
    'Searches live analyst coverage for every holding and builds a bear / base / bull band.',
    0.06);
  if (!ok) return;

  box.innerHTML = '<div class="dim" style="padding:14px">Searching analyst coverage — 30–60s…</div>';
  try {
    await api.runOutlook();
    await views.paintOutlook(box, runOutlook);
    ui.toast('Outlook updated.', 'success', 3000);
  } catch (err) {
    ui.toast(err.message, 'error');
    await views.paintOutlook(box, runOutlook);
  }
}

/* Re-read the store after any edit so the watchlist, tape and prompts agree. */
async function refreshBook() {
  try {
    const [info, portfolio] = await Promise.all([api.getInfo(), api.getPortfolio()]);
    state.info = info;
    state.portfolio = portfolio.positions || [];
    state.totals = portfolio.totals || null;
    pricesFetched = false;
    buildRows();
    ui.renderTape(state.rows.filter(r => r.last !== null)
      .map(r => ({ sym: r.sym, price: r.last, chg: r.chg, currency: r.currency })));
    ui.renderGeo(state.info?.geo_transmission, Object.keys(state.info?.watchlist || {}).concat(
      (state.portfolio || []).map(p => p.ticker)), (t) => runCommand(`research ${t}`));
  } catch (err) { ui.toast(`Refresh failed: ${err.message}`, 'warn'); }
}

/* ── Reading mode ────────────────────────────────────────────────────────── */

function loadFolds() {
  try { return JSON.parse(localStorage.getItem(FOLD_KEY) || '{}'); } catch { return {}; }
}
function saveFolds(f) {
  try { localStorage.setItem(FOLD_KEY, JSON.stringify(f)); } catch { /* private mode */ }
}

function applyFolds() {
  const folds = loadFolds();
  for (const btn of $$('.chev')) {
    const panel = btn.dataset.panel;
    const block = btn.closest('.blk, .blk-grow');
    if (block) block.classList.toggle('folded', Boolean(folds[panel]));
  }
}

function toggleFold(panel) {
  const folds = loadFolds();
  folds[panel] = !folds[panel];
  saveFolds(folds);
  applyFolds();
}

function toggleFocus(force) {
  const on = force === undefined ? !$('grid').classList.contains('focus') : Boolean(force);
  $('grid').classList.toggle('focus', on);
  $('btn-focus').classList.toggle('on', on);
  // Focus mode hides the side columns outright, so drop any open slide-in
  // rather than leave it to reappear when focus is switched back off.
  if (on) closePanels();
  try { localStorage.setItem(FOCUS_KEY, on ? '1' : '0'); } catch { /* private mode */ }
  if (state.history) paintChart();
}

/* ── Narrow-viewport slide-in panels ─────────────────────────────────────
   Four things can open or close these — the header toggle, the panel's own
   ✕, the scrim, and Escape. They all route through here so aria-expanded
   cannot drift out of step. Only one panel opens at a time; they occupy the
   same corner of the grid.

   This only owns the .open class. The scrim's visibility is CSS's job (see
   "#col1.open ~ #scrim" in style.css) precisely so that resizing past a
   breakpoint needs no listener here to stay correct. */

const PANEL_TOGGLE = { col1: 'btn-brent', col2: 'btn-watch' };

function setPanel(id, open) {
  const el = $(id);
  if (!el) return;
  const on = open === undefined ? !el.classList.contains('open') : Boolean(open);
  if (on) {
    // Opening one closes the other — they overlap at the same left edge.
    Object.keys(PANEL_TOGGLE).filter(k => k !== id).forEach(k => setPanel(k, false));
  }
  el.classList.toggle('open', on);
  const btn = $(PANEL_TOGGLE[id]);
  if (btn) btn.setAttribute('aria-expanded', on ? 'true' : 'false');
}

function closePanels() {
  Object.keys(PANEL_TOGGLE).forEach(k => setPanel(k, false));
}

/* ── Tailoring ───────────────────────────────────────────────────────────
   The onboarding answers have to change something, or they are decoration. */

function applyTailoring(profile) {
  state.profile = profile;
  if (!profile) return;

  state.period = { under1y: '1mo', '1to3y': '1y', '3to5y': '1y', over5y: '5y' }[profile.horizon] || '1y';

  // A long-horizon investor does not need the oil gate expanded every session.
  const folds = loadFolds();
  if (folds.brent === undefined) {
    folds.brent = profile.horizon === 'over5y' || profile.horizon === '3to5y';
    saveFolds(folds);
  }
  applyFolds();

  // Conservative spends less by default; aggressive opts into everything.
  try {
    if (!localStorage.getItem('argus.modules')) {
      const all = { report: true, context: true, policy: true, patterns: true, synthesis: true };
      const lean = { report: true, context: false, policy: false, patterns: false, synthesis: true };
      localStorage.setItem('argus.modules',
        JSON.stringify(profile.risk === 'conservative' ? lean : all));
    }
  } catch { /* private mode */ }

  ui.setGeoPriority(profile.sectors || []);
}

/* ── Config ──────────────────────────────────────────────────────────── */

function openConfig() {
  /* The key itself is never held in the browser any more — it lives in an
     HttpOnly session cookie — so this offers the two actions that remain:
     sign out, or unlock when the session has lapsed. */
  ui.showConfigModal(api.auth.has(), async (action) => {
    if (action === 'signout') {
      await api.auth.signOut();
      ui.renderKeyState(false);
      ui.toast('Signed out. AI research is locked.', 'success', 3200);
      return;
    }
    views.showUnlock(async () => {
      await api.auth.refresh();
      ui.renderKeyState(api.auth.has());
      ui.toast('Unlocked — AI research armed.', 'success', 3200);
    });
  }, (key) => {
    if (key === 'accent') paintChart();
  });
}

/* ── Wiring ──────────────────────────────────────────────────────────── */

function wire() {
  $('btn-exec').onclick = () => { const v = $('cmd').value; $('cmd').value = ''; runCommand(v); };
  $('btn-cfg').onclick  = openConfig;
  $('btn-data').onclick = () => ui.showDataModal(state.health, state.info);

  let paused = false;
  $('btn-tape').onclick = () => { paused = !paused; ui.setTapePaused(paused); };

  $('btn-stop').onclick = () => {
    if (state.cancelType) state.cancelType();
    if (!state.run) return;
    state.run.cancelled = true;
    state.run.controller.abort();
    $('btn-stop').classList.add('hidden');
    ui.toast('Stopping — remaining stages will be skipped. A call already in flight still completes on the server.', 'info', 6000);
  };

  $('insp-close').onclick = ui.closeInspector;
  $('btn-focus').onclick  = () => toggleFocus();
  $('btn-add').onclick    = () => promptAdd();

  $$('.vb').forEach(b => { b.onclick = () => showView(b.dataset.view); });
  $$('.chev').forEach(b => {
    b.onclick = (e) => { e.stopPropagation(); toggleFold(b.dataset.panel); };
  });

  // A watchlist row click in the portfolio view routes back to research.
  window.addEventListener('argus:research', (e) => {
    showView('research').then(() => runCommand(`research ${e.detail}`));
  });
  $('btn-brent').onclick   = () => setPanel('col1');
  $('btn-watch').onclick   = () => setPanel('col2');
  $('col1-close').onclick  = () => setPanel('col1', false);
  $('col2-close').onclick  = () => setPanel('col2', false);
  $('scrim').onclick       = () => closePanels();

  $$('#watch-head span[data-sort]').forEach(s => {
    s.onclick = () => {
      ui.setSort(s.dataset.sort === 'sig' ? 'sig' : s.dataset.sort);
      ui.renderWatchlist(state.rows, (x) => runCommand(`research ${x}`), (x) => inspect(x));
    };
  });

  $('cmdref').querySelectorAll('[data-cmd]').forEach(n => {
    n.onclick = () => { $('cmd').value = n.dataset.cmd; $('cmd').focus(); };
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      ui.closeInspector();
      closePanels();
      if ($('grid').classList.contains('focus')) toggleFocus(false);
    }
    // "/" focuses the command line, as in every terminal worth using.
    if (e.key === '/' && document.activeElement !== $('cmd')) { e.preventDefault(); $('cmd').focus(); }
  });

  makeResponsive(document.body, () => paintChart());
  prefs.onChange((k) => { if (k === 'accent') paintChart(); });
}

/* ── Boot ────────────────────────────────────────────────────────────── */

async function promptAdd() {
  const t = await ui.promptTicker();
  if (t) runCommand(`add ${t}`);
}

document.addEventListener('DOMContentLoaded', async () => {
  ui.initTooltips();

  // Session first: the boot log reports whether AI is armed, so it must know.
  await api.auth.refresh();

  const enter = async () => {
    const lines = await bootSequence();
    ui.boot(lines, () => startTerminal());
  };

  /* Read the profile and decide: onboarding for a fresh install, terminal for
     everyone else. This only runs once a session exists — /api/profile and
     /api/positions now require one, so asking earlier just returns 401 and
     would misread a returning user as brand new. */
  const enterOrOnboard = async () => {
    let profile = null;
    try { profile = (await api.getProfile()).profile; } catch { /* store may be new */ }

    if (!profile) {
      let positions = {};
      try { positions = (await api.getPositions()).positions; } catch { /* empty book */ }
      views.showOnboarding(positions, async () => {
        try { profile = (await api.getProfile()).profile; } catch { /* ignore */ }
        applyTailoring(profile);
        enter();
      });
      return;
    }

    applyTailoring(profile);
    enter();
  };

  if (!api.auth.has()) {
    // No session, no terminal. The gate used to also require `!profile`, so a
    // returning browser walked straight in — which only looked like it worked
    // because the data routes were open to anyone. They now require a session,
    // so unlocking is the front door for every visit, not just the first.
    views.showUnlock(async () => {
      await api.auth.refresh();
      await enterOrOnboard();
    });
    return;
  }

  await enterOrOnboard();
});

function startTerminal() {
  {
    $('term').classList.add('on');

    /* Each stage is isolated: a throw in one used to abort the rest silently —
       a single missing alias in wire() once took the whole command line down
       while every other panel still looked healthy. Fail loudly instead. */
    for (const [name, fn] of [['paint', paintAll], ['wire', wire], ['hints', wireHints]]) {
      try { fn(); }
      catch (err) {
        console.error(`[argus] ${name} failed:`, err);
        ui.toast(`Init step "${name}" failed: ${err.message}`, 'error', 12000);
      }
    }

    applyFolds();
    try { if (localStorage.getItem(FOCUS_KEY) === '1') toggleFocus(true); } catch { /* private mode */ }

    welcome();
    ui.setStatus('READY');
    $('cmd').focus();
    setInterval(async () => {
      try {
        const b = await api.getBrent();
        state.brent = b;
        ui.renderBrent(b, state.info?.brent_levels);
      } catch { /* the panel keeps the last good value */ }
    }, 5 * 60 * 1000);
  }
}
