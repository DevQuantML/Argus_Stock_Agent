'use strict';

/* app.js — ARGUS://TERMINAL boot, state, command parser, staged pipeline.

   Version query on each import so a redeploy can never pair a fresh app.js
   with a stale cached sub-module. Bump together with the ?v= in index.html. */

import * as api from './api.js?v=37';
import { drawChart, makeResponsive } from './chart.js?v=37';
import { renderMarkdown, escapeHtml } from './md.js?v=37';
import * as prefs from './theme.js?v=37';
import * as ui from './ui.js?v=37';
import * as views from './views.js?v=37';
import * as tour from './tour.js?v=37';
import { DISCLAIMER, FRESHNESS_NOTE, guestAllowanceLine } from './copy.js?v=37';

const $  = ui.$;
const $$ = ui.$$;

const el = ui.el;

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

  // A visitor's OWN Groq/Perplexity key, from the landing gate's BRING YOUR
  // OWN KEY pane. Deliberately a plain module-scope field, never
  // localStorage/sessionStorage — AGENT_SECRET was moved OFF exactly that
  // kind of storage once its XSS-readability was recognised as a real risk,
  // and a visitor's own paid key gets at least the same caution. Lost on
  // refresh, on purpose: {provider, key} | null.
  selfKey: null,
};

/* The tier is DERIVED, never stored.

   It used to be a `state.tier` field kept in step with api.auth by a syncTier()
   call after every refresh — and one call site forgot. Unlocking through the
   config modal refreshed the auth layer but left the mirror reading 'visitor',
   so the chip said FREE MODE next to a toast reading "AI research armed", and
   the real owner's `add`/`rm` were refused as if they were a stranger's until
   the next reload.

   A getter cannot drift from the thing it reads. Every gate in this file keys
   off tier() rather than a bare auth.has(), because "has a session" no longer
   answers the question a gate is asking. */
const tier      = () => api.auth.effectiveTier();
const isOwner   = () => tier() === 'owner';
const isVisitor = () => tier() === 'visitor';

const FOLD_KEY  = 'argus.folded';
const FOCUS_KEY = 'argus.focus';
const HIST_KEY  = 'argus.selfKeyHistory';
// Newest N per ticker — matches store.RESEARCH_HISTORY_LIMIT's intent
// (bounded growth), kept independently since this cache lives in the
// browser, not the server.
const HIST_LIMIT = 20;

/* Client-side research history for the ONE tier that has no session to save
   against server-side: a self-key (BYOK) visitor. Owner and guest history is
   server-side (store.research_runs) and fetched via api.getResearchHistory —
   this is deliberately NOT used for them, so there is exactly one source of
   truth per tier rather than two caches that can drift apart. localStorage
   is per-browser, so this never survives a device change or a private
   window, unlike the server-side path — an accepted, stated trade-off for a
   tier that by definition has no account to attach permanent history to. */
function loadLocalHistory(sym) {
  try {
    const all = JSON.parse(localStorage.getItem(HIST_KEY) || '{}');
    return Array.isArray(all[sym]) ? all[sym] : [];
  } catch { return []; }
}
function saveLocalHistory(sym, mode, payload) {
  try {
    const all = JSON.parse(localStorage.getItem(HIST_KEY) || '{}');
    const list = Array.isArray(all[sym]) ? all[sym] : [];
    list.unshift({ mode, payload, created_at: new Date().toISOString() });
    all[sym] = list.slice(0, HIST_LIMIT);
    localStorage.setItem(HIST_KEY, JSON.stringify(all));
  } catch { /* private mode, or storage full — history is a convenience, not core */ }
}

/* The watchlist used to live in localStorage. It is server-side now, so an
   add or remove survives a browser wipe and reaches the research prompts. */

/* ── Boot ────────────────────────────────────────────────────────────── */

async function bootSequence() {
  const lines = [{ t: '> ESTABLISHING SESSION…', c: 'txt' }];
  const visitor = isVisitor();

  // Everything the boot log claims is actually fetched here first. A visitor
  // skips the two session-gated calls rather than firing them to be refused —
  // a guaranteed 401 is not a data point, it is noise in the server log.
  // info's failure is captured rather than swallowed to null — an /api/info
  // 500 used to render identically to "you haven't mapped any geo events
  // yet", which is a real backend failure reading as a normal empty state.
  const [infoResult, meta, health, brent, portfolio] = await Promise.all([
    visitor
      ? Promise.resolve({ ok: true, value: null })
      : api.getInfo().then((v) => ({ ok: true, value: v })).catch((err) => ({ ok: false, error: err })),
    api.getMeta().catch(() => null),
    api.getHealth().catch(() => null),
    api.getBrent().catch(() => null),
    visitor ? Promise.resolve(null) : api.getPortfolio().catch(() => null),
  ]);

  const info = infoResult.ok ? infoResult.value : null;
  state.info = info;
  state.meta = meta;
  state.health = health;
  state.brent = brent && !brent.error ? brent : null;
  state.portfolio = portfolio?.positions || [];
  state.totals = portfolio?.totals || null;
  state.provider = health?.checks?.perplexity_key ? 'perplexity' : 'groq';

  // A visitor still gets the Brent ladder: /api/meta carries BRENT_LEVELS,
  // which is framework config and never the book.
  state.levels = info?.brent_levels || meta?.brent_levels || null;

  const ok = (t) => ({ t: `  ✓ ${t}`, c: 'green' });
  const dim = (t) => ({ t: `  · ${t}`, c: 'txt-muted' });
  const warn = (t) => ({ t: `  ⚠ ${t}`, c: 'gold' });
  const bad = (t) => ({ t: `  ✕ ${t}`, c: 'red' });

  lines.push({ t: '> LOADING INVESTMENT FRAMEWORK', c: 'txt' });
  lines.push({ t: '  [████████████████████] 100%', c: 'accent' });
  lines.push({ t: '> CHECKING DATA ADAPTERS…', c: 'txt' });

  lines.push(state.brent
    ? ok(`BRENT_CRUDE_FEED …… LIVE @ $${Number(state.brent.brent_price).toFixed(2)}`)
    : bad('BRENT_CRUDE_FEED …… UNAVAILABLE'));

  /* Tone matters here. A visitor's first impression of the product should not
     be a column of amber warnings about things that are working exactly as
     designed. `warn` is reserved for actual degradation; a private book is not
     degradation, it is the arrangement. */
  if (visitor) {
    lines.push(ok('QUANT_ENGINE ……… READY · FREE FOR ANY TICKER'));
    lines.push(dim('PRIVATE_BOOK …… KEYHOLDERS ONLY'));
    lines.push(dim('GEO_TRANSMISSION …… KEYHOLDERS ONLY'));
  } else {
    lines.push(portfolio
      ? ok(`EQUITY_PRICE_FEED … ${state.portfolio.length} POSITIONS`)
      : bad('EQUITY_PRICE_FEED … UNAVAILABLE'));
    const geoN = Object.keys(info?.geo_transmission || {}).length;
    lines.push(infoResult.ok
      ? (geoN ? ok(`GEO_TRANSMISSION …… ${geoN} VECTORS MAPPED`) : warn('GEO_TRANSMISSION …… NOT CONFIGURED'))
      : bad(`INFO_ENDPOINT ……… ${infoResult.error?.message || 'failed'}`));
  }

  // The two degraded/absent states get an actionable sub-line, not just a
  // status word — a fresh clone with no key should tell the reader what to
  // do next, not just that something is missing.
  if (health?.checks?.perplexity_key) {
    lines.push(ok('PERPLEXITY_API …… CONFIGURED · LIVE WEB'));
  } else if (health?.checks?.groq_key) {
    lines.push(warn('GROQ_API ………… FALLBACK · NO WEB SEARCH'));
    lines.push(dim('  add a PERPLEXITY_API_KEY for live web search · docs/SETUP.md'));
  } else {
    lines.push(warn('AI_PROVIDER ……… NOT CONFIGURED'));
    lines.push(dim('  run  python main.py setup  to connect a free engine · docs/SETUP.md'));
  }

  if (isOwner()) {
    lines.push(ok('SESSION ………… ACTIVE · AI ARMED'));
  } else if (tier() === 'guest') {
    const g = api.auth.guest;
    const left = api.auth.divesLeft();
    lines.push(g && g.exhausted
      ? dim('SESSION ………… GUEST · DIVE COMPLETE · FREE TOOLS')
      : ok(`SESSION ………… GUEST · ${left}/${g?.modules_total ?? 5} DIVE STAGES INCLUDED`));
  } else {
    lines.push(dim('SESSION ………… VISITOR · FREE TOOLS'));
  }

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
  ui.renderKeyState(tier(), api.auth.guest);
  // Adding to the watchlist is an owner action; the server refuses it for
  // anyone else, so do not offer the control.
  $('btn-add')?.classList.toggle('hidden', !isOwner());
  ui.renderBrent(state.brent, state.levels);
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
  ui.renderWatchlist(rows, (s) => runCommand(`research ${s}`), (s) => inspect(s),
    isVisitor() ? 'The book is private. Research any ticker instead — try `quant AAPL`.'
                : 'No instruments loaded.');
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

  /* Tier-aware footer. copy.js declared FRESHNESS_NOTE and guestAllowanceLine
     as belonging here, both modules imported them, and neither rendered them —
     so a guest learned what their key included only from the chip's tooltip or
     the cost modal, and nobody was told how fresh the data is. An unused
     import plus prose asserting it IS used is exactly the drift copy.js was
     created to prevent. */
  if (tier() === 'guest') {
    const g = el('p', 'dim');
    g.textContent = guestAllowanceLine(api.auth.guest);
    if (g.textContent) o.appendChild(g);
  }
  if (!isOwner()) {
    const f = el('p', 'ld-disc');
    f.textContent = FRESHNESS_NOTE;
    o.appendChild(f);
  }
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
    ['tour', 'Replay the 30-second orientation.'],
    ['request', 'Ask the owner to issue you a guest key.'],
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
    if (host) host.innerHTML = `<div class="chart-empty">${escapeHtml(err.message)}</div>`;
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
  if (demo.brent) { state.brent = demo.brent; ui.renderBrent(demo.brent, state.levels); }

  // Identity line
  const s = state.stock;
  const id = document.createElement('div');
  id.className = 'r-body';
  id.id = 'q-ident';          // the earnings chip lands here once it arrives
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

  loadExtras(sym, o);

  // Under the numbers, on every path. A reader who never triggers an AI stage
  // still needs to know what this output is and is not.
  const disc = document.createElement('p');
  disc.className = 'ld-disc';
  disc.textContent = DISCLAIMER;
  o.appendChild(disc);

  return true;
}

/* Earnings date and headlines. Free, and deliberately NOT awaited: they are not
   pipeline stages, and a slow or empty headline feed must never hold up the
   numbers or fail the run. Each removes its own placeholder on failure. */
function loadExtras(sym, o) {
  api.getEarnings(sym).then(d => {
    if (!d || !d.next_earnings || state.ticker !== sym) return;
    const chip = document.createElement('span');
    chip.className = 'chip-earn';
    const days = Math.ceil((Date.parse(d.next_earnings) - Date.now()) / 864e5);
    chip.textContent = Number.isFinite(days) && days >= 0 && days <= 21
      ? `NEXT EARNINGS ${d.next_earnings} · ${days}D`
      : `NEXT EARNINGS ${d.next_earnings}`;
    $('q-ident')?.appendChild(chip);
    cacheTab(sym);
  }).catch(() => { /* a missing date is normal, not an error */ });

  const box = document.createElement('div');
  box.id = 'news-box';
  o.appendChild(box);
  api.getNews(sym).then(d => {
    if (state.ticker !== sym) return;
    ui.renderNews($('news-box'), d && d.items);
    cacheTab(sym);
  }).catch(() => { $('news-box')?.remove(); });
}

/* ── Staged research ─────────────────────────────────────────────────── */

const AI_MODULES = [
  ['report', 'RESEARCH BRIEF'],
  ['context', 'COMPANY CONTEXT'],
  ['policy', 'POLITICIAN & GOVERNMENT TRADES'],
  ['patterns', 'HIDDEN PATTERNS'],
];

/* The note shown where the AI stages would have run. Two different situations,
   two different sentences — "add a key" is wrong advice for a guest who has one
   and has already used it. */
function aiLockedNote(spent) {
  const n = document.createElement('div');
  n.className = 'r-body';
  n.style.marginTop = '14px';

  const p = document.createElement('p');
  p.className = 'dim';
  p.textContent = spent
    ? 'Your included deep dive is complete. Quant, charts, history and news stay '
      + 'free and unlimited until your key expires.'
    : 'AI research is locked. Live web research, politician-trade disclosure, '
      + 'cross-source patterns and the final verdict need a key.';
  n.appendChild(p);

  if (!spent && !isOwner()) {
    const row = document.createElement('div');
    row.className = 'ld-actions';
    row.style.cssText = 'max-width:320px;margin-top:12px';
    const a = document.createElement('button');
    a.className = 'btn'; a.type = 'button'; a.textContent = 'REQUEST ACCESS';
    a.onclick = () => openLanding('request');
    const b = document.createElement('button');
    b.className = 'btn-g'; b.type = 'button'; b.textContent = 'I HAVE A KEY';
    b.onclick = () => openLanding('unlock');
    row.append(a, b);
    n.appendChild(row);
  }

  return n;
}

/* Mark every stage after the one that failed, so a halted run reads as halted
   rather than as four stages that mysteriously never started. */
function markRemainingLocked(failedKey, why) {
  let seen = false;
  for (const [k] of AI_MODULES) {
    if (k === failedKey) { seen = true; continue; }
    if (seen) ui.pipeline.set(k, 'locked', why);
  }
  ui.pipeline.set('synthesis', 'locked', why);
}

/* Re-read the allowance from the server and repaint the chip. The server is
   authoritative about what has been spent; the cached copy is only for display. */
async function refreshTier() {
  try {
    await api.auth.refresh();
    ui.renderKeyState(tier(), api.auth.guest);
  } catch { /* display only — a failure here changes no entitlement */ }
}

async function research(rawSym, { paid = true, question } = {}) {
  if (state.busy) { ui.toast('A run is already in flight.', 'warn'); return; }

  const sym = String(rawSym || '').trim().toUpperCase().replace(/[^A-Z0-9.=-]/g, '');
  if (!sym) { ui.toast('Usage: research <SYM>', 'warn'); return; }

  state.busy = true;
  state.ticker = sym;
  $('btn-exec').disabled = true;
  const started = performance.now();
  // "Can this caller run paid AI?" is no longer "do they hold a session" — a
  // guest holds one and may still have nothing left to spend. A self-key
  // visitor holds no session at all and is still a yes: state.selfKey is a
  // THIRD, independent way to reach this, orthogonal to tier().
  const spent      = tier() === 'guest' && api.auth.divesLeft() <= 0;
  const hasSelfKey = !!state.selfKey;
  const hasKey     = (api.auth.has() && !spent) || hasSelfKey;

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
      if (spent) for (const [k] of AI_MODULES) ui.pipeline.set(k, 'locked', 'allowance used');
      o.appendChild(aiLockedNote(spent));
      cacheTab(sym);
      return;
    }

    /* A self-key visitor is a completely separate path from here on — one
       one-shot call (report + bull/bear), not the 4-module + synthesis
       pipeline the owner/guest path below runs. No session, no allowance,
       no ticker-binding to check — those all belong to the guest-budget
       system this caller was never part of. */
    if (hasSelfKey) {
      await runSelfKeyResearch(sym, question, o);
      cacheTab(sym);
      return;
    }

    /* A dive that cannot finish before the key dies spends the owner's money
       for a partial result and leaves the guest with nothing to show. Refuse
       to start rather than fail three stages in; the server re-checks anyway. */
    if (tier() === 'guest') {
      const hrs = api.auth.hoursLeft();
      if (hrs !== null && hrs < 0.75) {
        ui.setStatus('QUANT ONLY', 'ok');
        ui.toast('Your guest key expires in under 45 minutes — not enough time to '
               + 'finish a full dive. Ask the owner for a fresh key.', 'warn', 8000);
        for (const [k] of AI_MODULES) ui.pipeline.set(k, 'locked', 'key expiring');
        ui.pipeline.set('synthesis', 'locked', 'key expiring');
        cacheTab(sym);
        return;
      }
    }

    const sel = await ui.showCostModal(sym, state.provider, {
      tier: tier(),
      guest: api.auth.guest,
    });
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
        /* Most kinds below are TERMINAL for the whole run, not per-stage
           hiccups, and each sets state.run.cancelled. Without that the loop
           treats a denial like a flaky upstream and carries on to the next
           module — four more paid requests that cannot possibly succeed, four
           more red toasts, and the rate bucket burned. The two exceptions are
           'stage' and the generic fallback, which are genuinely per-stage. */
        if (err.kind === 'aborted') { ui.pipeline.set(key, 'cancelled'); state.run.cancelled = true; }

        /* One stage of the dive was already claimed by an earlier run. The
           rest are still theirs, so skip this one and carry on — halting here
           is what stranded the remaining stages. */
        else if (err.kind === 'stage') {
          ui.pipeline.set(key, 'skipped', 'already run');
          refreshTier();
        }
        /* The provider itself is down. Every remaining stage would call the
           same one, so stop — and say so plainly rather than blaming the
           stage. Anything refused before billing has already been refunded
           server-side, so a guest's allowance survives this. */
        else if (err.kind === 'provider') {
          ui.pipeline.set(key, 'failed');
          state.run.cancelled = true;
          ui.toast(err.message, 'error', 9000);
          markRemainingLocked(key, 'provider down');
          refreshTier();
        }
        else if (err.kind === 'budget' || err.kind === 'bound') {
          ui.pipeline.set(key, 'failed');
          state.run.cancelled = true;
          ui.toast(err.message, 'warn', 9000);
          markRemainingLocked(key, err.kind === 'budget' ? 'allowance used' : 'wrong ticker');
          refreshTier();
        }
        else if (err.kind === 'expired') {
          ui.pipeline.set(key, 'failed');
          state.run.cancelled = true;
          ui.toast(err.message, 'error', 9000);
          markRemainingLocked(key, 'key expired');
          // Never openConfig() here: that asks for an owner secret a guest was
          // never given. Send them where a new credential actually comes from —
          // which differs by who they were. api.auth still holds the pre-401
          // tier; refreshing first would erase the only clue we have.
          openLanding(api.auth.isOwner() ? 'unlock' : 'request');
        }
        else if (err.kind === 'forbidden') {
          ui.pipeline.set(key, 'failed');
          state.run.cancelled = true;
          ui.toast(err.message, 'warn', 8000);
          markRemainingLocked(key, 'owner only');
        }
        else if (err.kind === 'unauthorized') {
          ui.pipeline.set(key, 'failed');
          state.run.cancelled = true;
          ui.toast(err.message, 'error');
          // openConfig() is the OWNER's re-unlock path. For anyone else it
          // opens a dialog demanding AGENT_SECRET, which a guest or visitor
          // cannot possibly supply.
          if (isOwner()) openConfig();
          else openLanding(tier() === 'guest' ? 'request' : 'unlock');
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
    // Also on the happy path. refreshTier() was wired only into the error
    // branches, so after a COMPLETED dive the cached allowance still read 0/5:
    // the chip kept saying "1 DIVE", and the next `research` passed the spent
    // check and showed the full confirmation modal for a dive that then 409'd
    // on every stage. The client had the data to prevent that.
    if (tier() === 'guest') refreshTier();
    $('btn-stop').classList.add('hidden');
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    ui.writeLine(`— run complete in ${secs}s —`, 'dim');
    cacheTab(sym);
    state.run = null;
    state.busy = false;
    $('btn-exec').disabled = false;
  }
}

/* A visitor's own key, the SAME 4-module + synthesis staged pipeline
   research() runs for owner/guest below — à-la-carte module selection via
   ui.showCostModal's owner-style branch, one call per module against
   /api/byok/{ticker}/{module} instead of the session-gated routes.

   Deliberately its own loop rather than a branch inside the main one:
   several of that loop's error kinds (budget/bound/stage/expired) are
   guest-allowance concepts with no meaning for a caller who has no
   allowance at all — reusing it risks one of those firing for the wrong
   reason instead of just not existing here. state.run/$('btn-stop') are
   still wired the same way so Stop and the outer finally in research()
   (state.busy, the exec button, the "run complete" line) work identically
   for both paths without needing to know which one ran. */
async function runSelfKeyResearch(sym, question, o) {
  const { provider, key } = state.selfKey;
  const providerLabel = provider.toUpperCase();

  // tier: 'owner' selects showCostModal's à-la-carte UX (per-module
  // checkboxes, live running total) — it is a UI-mode selector on that
  // function, not a claim about who this caller is. `provider` here is
  // THIS visitor's chosen provider, deliberately not state.provider (the
  // OPERATOR's configured one) — the modal's Groq/Perplexity note must
  // describe the key actually paying for the run.
  const sel = await ui.showCostModal(sym, provider, { tier: 'owner' });
  if (!sel) {
    for (const [k] of AI_MODULES) ui.pipeline.set(k, 'skipped');
    ui.pipeline.set('synthesis', 'skipped');
    ui.setStatus('QUANT ONLY', 'ok');
    ui.toast('AI stages skipped — quant, chart and P&L are loaded.', 'info');
    return;
  }
  for (const [k] of AI_MODULES) if (!sel[k]) ui.pipeline.set(k, 'skipped');
  if (!sel.synthesis) ui.pipeline.set('synthesis', 'skipped');

  const controller = new AbortController();
  state.run = { controller, cancelled: false };
  $('btn-stop').classList.remove('hidden');

  const texts = {};
  for (const [modKey, label] of AI_MODULES) {
    if (!sel[modKey]) continue;
    if (state.run.cancelled) { ui.pipeline.set(modKey, 'skipped'); continue; }

    ui.pipeline.set(modKey, 'running');
    const t = performance.now();
    try {
      const data = await api.getByokResearchModule(sym, provider, key, modKey, {
        question: modKey === 'report' ? question : undefined,
        signal: controller.signal,
      });
      texts[modKey] = data.output || '';
      ui.pipeline.done(modKey, performance.now() - t);

      const head = document.createElement('div');
      head.className = 'r-head';
      head.textContent = `${label}  ·  YOUR ${providerLabel} KEY`;
      o.appendChild(head);
      await typeBlock(o, data.output || '');
    } catch (err) {
      if (err.kind === 'aborted') {
        ui.pipeline.set(modKey, 'cancelled');
        state.run.cancelled = true;
      } else if (err.status === 403) {
        // ALLOW_BYOK_VISITORS got turned off mid-run, or never was on —
        // every remaining stage would hit the identical refusal.
        ui.pipeline.set(modKey, 'failed');
        state.run.cancelled = true;
        ui.toast('This ARGUS instance has not turned on self-funded research.', 'warn', 8000);
        markRemainingLocked(modKey, 'not enabled');
      } else if (err.status === 400) {
        // A malformed key (soft_validate catches it before any provider
        // call) fails identically on every remaining stage — stop rather
        // than burn four more round trips proving it again.
        ui.pipeline.set(modKey, 'failed');
        state.run.cancelled = true;
        ui.toast(err.message || 'That key was rejected — check you copied it correctly.', 'error', 8000);
        markRemainingLocked(modKey, 'key rejected');
      } else if (err.kind === 'provider') {
        // A 502 provider_unavailable — the key passed soft_validate's format
        // check and something went wrong only once it reached the provider.
        // Deliberately NOT worded as "your key was rejected" any more: that
        // is only one of the things this branch catches, and asserting it
        // sent a real user through two good keys chasing what was actually a
        // wrong model id in _GEMINI_MAIN. The server already names the real
        // cause (_PROVIDER_REASON_TEXT, which says outright when the fault is
        // this server's rather than the caller's) — show that and nothing on
        // top of it, per the same rule the quant cards follow: the layer that
        // knows which branch it took is the only layer allowed to say.
        ui.pipeline.set(modKey, 'failed');
        state.run.cancelled = true;
        ui.toast(err.message || 'The provider call failed.', 'error', 8000);
        markRemainingLocked(modKey, 'stopped');
      } else {
        ui.pipeline.set(modKey, 'failed');
        ui.toast(`${modKey}: ${err.message}`, 'warn');
      }
    }
  }

  const any = Object.values(texts).some(Boolean);
  if (sel.synthesis && !state.run.cancelled && any) {
    ui.pipeline.set('synthesis', 'running');
    const t = performance.now();
    try {
      const res = await api.postByokSynthesis(sym, provider, key, {
        report: texts.report || '', context: texts.context || '',
        policy: texts.policy || '', patterns: texts.patterns || '',
      }, { signal: controller.signal });

      // A self-key run has no session, so nothing saved this server-side —
      // the owner/guest loop above doesn't need this call because
      // api_research_synthesis already persists it (store.research_runs).
      saveLocalHistory(sym, 'synthesis', res);

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
}

/* Model Court — Perplexity and Gemini, run in parallel on the caller's own
   two keys, compared. Gated to a real session (owner or guest) before the
   key-entry modal even opens — decided explicitly in chat as the interim
   access boundary; a real paid-membership gate was requested for later,
   not built here. Neither key this modal collects is ever stored: they
   live only as local variables for the one request that uses them. */
async function runModelCourt(sym) {
  if (state.busy) { ui.toast('A run is already in flight.', 'warn'); return; }
  if (!isOwner() && tier() !== 'guest') {
    ui.toast('Model Court needs an unlocked session — the owner\'s key, or a guest key. '
           + 'Type `request` to ask the owner for access.', 'warn', 7000);
    return;
  }

  const keys = await ui.showModelCourtKeys(sym, isOwner());
  if (!keys) return;

  state.busy = true;
  state.ticker = sym;
  $('btn-exec').disabled = true;
  const started = performance.now();

  openTab(sym);
  ui.clearOut();
  ui.writeTitle(`ARGUS://${sym} · MODEL COURT`);
  const o = ui.out();

  try {
    const ok = await loadFree(sym, { withPipeline: false });
    if (!ok) { cacheTab(sym); return; }

    const mode = keys.providerMode || 'both';
    const runningLine = mode === 'both' ? 'Running Perplexity + Gemini in parallel…'
                       : mode === 'perplexity' ? 'Running Perplexity…' : 'Running Gemini…';
    ui.writeLine(runningLine, 'dim');
    const res = await api.runModelCourt(sym, keys.perplexityKey, keys.geminiKey,
                                         { providerMode: mode });

    // Same shape per section: a heading, then either the real output or a
    // plain failure/unavailable line — never both, never neither. A
    // provider not requested this run (single-provider mode) gets no
    // section at all rather than a misleading "failed" line for something
    // that was never asked to run.
    const section = (label) => {
      o.appendChild(Object.assign(document.createElement('div'),
        { className: 'r-head', textContent: label }));
    };
    const failLine = (text, dim) => {
      const p = document.createElement('p');
      p.className = dim ? 'r-body dim' : 'r-body';
      p.textContent = text;
      o.appendChild(p);
    };

    if (res.perplexity) {
      section('PERPLEXITY');
      if (res.perplexity.error) failLine(`Failed: ${res.perplexity.error}`);
      else await typeBlock(o, res.perplexity.report || '');
    }

    if (res.gemini) {
      section('GEMINI');
      if (res.gemini.error) failLine(`Failed: ${res.gemini.error}`);
      else await typeBlock(o, res.gemini.report || '');
    }

    // Single-provider mode never attempts a comparison (see run_model_court's
    // own provider_mode == 'both' gate) — its comparison_note says so
    // plainly, so a real section for it would just repeat that note as a
    // heading over itself.
    if (mode === 'both') {
      section('COMPARISON');
      if (res.comparison) await typeBlock(o, res.comparison);
      else failLine(res.comparison_note || 'Comparison unavailable.', true);
    }

    const ranOk = mode === 'both' ? !!res.comparison
                : mode === 'perplexity' ? !res.perplexity?.error
                : !res.gemini?.error;
    ui.setStatus(ranOk ? 'COMPLETE' : 'PARTIAL', ranOk ? 'ok' : 'err');
  } catch (err) {
    ui.setStatus('FAILED', 'err');
    ui.toast(err.message || 'Model Court failed.', 'error', 8000);
  } finally {
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    ui.writeLine(`— run complete in ${secs}s —`, 'dim');
    cacheTab(sym);
    state.busy = false;
    $('btn-exec').disabled = false;
  }
}

const HIST_MODE_LABEL = { full: 'FULL REPORT', synthesis: 'STAGED VERDICT', model_court: 'MODEL COURT' };

/* Recall list for a ticker's saved research. Owner/guest read the server
   (store.research_runs, via api.getResearchHistory); a self-key visitor —
   the only tier that ever has anything to recall with no session — reads
   its own localStorage cache instead. A plain visitor or guest with no
   self-key and nothing saved yet just sees the empty state; there is no
   third source to check. */
async function showHistory(rawSym) {
  if (state.busy) { ui.toast('A run is already in flight.', 'warn'); return; }
  const sym = String(rawSym || '').trim().toUpperCase().replace(/[^A-Z0-9.=-]/g, '');
  if (!sym) { ui.toast('Usage: history <SYM>', 'warn'); return; }

  openTab(sym);
  ui.clearOut();
  ui.writeTitle(`ARGUS://${sym} · HISTORY`);
  const o = ui.out();

  let runs;
  if (isOwner() || tier() === 'guest') {
    try {
      runs = (await api.getResearchHistory(sym)).runs || [];
    } catch (err) {
      ui.toast(err.message || 'Could not load history.', 'error');
      cacheTab(sym);
      return;
    }
  } else {
    runs = loadLocalHistory(sym);
  }

  if (!runs.length) {
    ui.writeLine(`No saved research for ${sym} yet — run \`research ${sym}\` or `
               + `\`court ${sym}\` first.`, 'dim');
    cacheTab(sym);
    return;
  }

  const list = document.createElement('div');
  list.className = 'hist-list';
  const detail = document.createElement('div');
  detail.className = 'hist-detail';

  const renderDetail = (run) => {
    detail.innerHTML = '';
    const p = run.payload || {};
    const section = (label) => detail.appendChild(Object.assign(
      document.createElement('div'), { className: 'r-head', textContent: label }));
    const body = (text) => {
      const b = document.createElement('div');
      b.className = 'r-body';
      b.innerHTML = renderMarkdown(text || '(not available)');
      detail.appendChild(b);
    };

    if (run.mode === 'synthesis') {
      ui.renderVerdict(detail, p.synthesis || '');
    } else if (run.mode === 'full') {
      section('REPORT');
      body(p.report);
    } else if (run.mode === 'model_court') {
      // Older saved runs predate provider_mode and are always 'both' in
      // effect — default to it so history from before this field existed
      // still renders both sections, same as it always has.
      const pmode = p.provider_mode || 'both';
      if (p.perplexity) { section('PERPLEXITY'); body(p.perplexity.report); }
      if (p.gemini)     { section('GEMINI');     body(p.gemini.report); }
      if (pmode === 'both') { section('COMPARISON'); body(p.comparison || p.comparison_note); }
    } else {
      body('');
    }
  };

  runs.forEach((run, i) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'btn-g hist-row';
    row.style.cssText = 'display:block;width:100%;text-align:left;margin-bottom:4px;padding:8px 10px';
    const when = new Date(run.created_at).toLocaleString();
    row.textContent = `${HIST_MODE_LABEL[run.mode] || run.mode} — ${when}`;
    row.onclick = () => {
      list.querySelectorAll('.hist-row').forEach(n => n.classList.remove('on'));
      row.classList.add('on');
      renderDetail(run);
    };
    list.appendChild(row);
    if (i === 0) { row.classList.add('on'); renderDetail(run); }
  });

  o.append(list, detail);
  cacheTab(sym);
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
      line.innerHTML = `<span class="ic">✕</span><span class="nm">${r.sym}</span><span class="mt">${escapeHtml(err.message)}</span>`;
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
  const levels = state.levels || {};
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
              'portfolio', 'profile', 'focus', 'clear', 'tour', 'request', 'help', 'court',
              'history'];

/* Refuse a book edit before it reaches the network. The server enforces this
   with a 403 regardless; catching it here means the reader gets a sentence
   about whose book it is instead of a bare permission error. */
function refuseBookEdit(verb) {
  if (isOwner()) return false;
  ui.toast(isVisitor()
    ? `The watchlist belongs to the terminal's owner. Type \`request\` to ask for access.`
    : `Guest access is read-only — you cannot ${verb} the owner's book.`, 'warn', 6000);
  return true;
}

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

    case 'tour':    tour.startTour(tier()); return;
    case 'request': openLanding('request');     return;

    case 'add': {
      if (!arg) { ui.toast('Usage: add <SYM>', 'warn'); return; }
      if (refuseBookEdit('add to')) return;
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
      if (refuseBookEdit('remove from')) return;
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

    case 'court':
      if (!arg) { ui.toast('Usage: court <SYM>', 'warn'); return; }
      await runModelCourt(arg); return;

    case 'history':
      if (!arg) { ui.toast('Usage: history <SYM>', 'warn'); return; }
      await showHistory(arg); return;

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

const NO_ARG = ['scan', 'brent', 'portfolio', 'profile', 'focus', 'clear', 'tour', 'request', 'help'];

function wireHints() {
  const input = $('cmd');
  const hint = $('cmd-hint');
  let idx = -1;

  const HINTS = [
    ['research', 'full staged brief'], ['court', 'Perplexity vs Gemini, compared'],
    ['history', 'past research, saved'],
    ['quant', 'free metrics only'],
    ['chart', 'price + reference lines'], ['scan', 'sweep all holdings'],
    ['brent', 'framework detail'], ['pos', 'open inspector'],
    ['portfolio', 'P&L, XIRR, outlook'], ['profile', 'edit positions & tailoring'],
    ['focus', 'reading mode'],
    ['add', 'track a ticker'], ['rm', 'untrack a ticker'],
    ['clear', 'reset output'], ['tour', '30-second orientation'],
    ['request', 'ask the owner for a key'], ['help', 'all commands'],
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

  /* A visitor has no session, so both of these views would 401 and print an
     error. They are not doing anything wrong — they simply are not the owner —
     so say that, and offer the two things that would change it. */
  if (isVisitor()) {
    views.renderLockedView(out, {
      title: VIEW_LABEL[name],
      body: name === 'portfolio'
        ? 'The book — positions, P&L, XIRR and the analyst outlook — belongs to '
          + 'this terminal\'s owner. Research on any ticker stays free and open to you.'
        : 'Profile settings belong to the terminal\'s owner. Your appearance '
          + 'preferences are saved in this browser and need no account.',
      onRequest: () => openLanding('request'),
      onUnlock:  () => openLanding('unlock'),
    });
    return;
  }

  if (name === 'portfolio') {
    await views.renderPortfolio(out, { onOutlook: runOutlook, tier: tier() });
  } else {
    await views.renderProfile(out, { onChanged: refreshBook, tier: tier() });
  }
}

/* One paid call covering every holding — same cost-confirm discipline as the
   research pipeline. */
async function runOutlook(box) {
  /* Owner only, and the server agrees (403). The outlook is unbudgeted spend
     over the owner's whole book, and it overwrites their single cached result —
     neither of which a guest allowance can bound. */
  if (!isOwner()) {
    ui.toast(tier() === 'guest'
      ? 'The portfolio outlook is owner-only. Your guest key covers one deep dive '
        + 'on a single ticker instead.'
      : 'Unlock with a key first — this is a paid call.', 'warn', 6000);
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
  /* AGENT_SECRET itself is never held in the browser any more — it lives in
     an HttpOnly session cookie — so the Session field offers only the two
     actions that remain: sign out, or unlock when the session has lapsed.
     A Groq/Perplexity key is a different kind of secret (a provider
     credential the server calls out with, not something this app compares
     against) and the AI Research field below is owner-only — isOwner gates
     whether it even renders, matching POST /api/settings/provider-key's own
     require_owner on the server. */
  ui.showConfigModal(api.auth.has(), api.auth.isOwner(), state.health?.checks, async (action) => {
    if (action === 'signout') {
      const done = await api.auth.signOut();
      ui.renderKeyState('visitor', null);
      ui.toast(done ? 'Signed out. AI research is locked.'
                    : 'Could not confirm sign-out — the server did not respond. Close this browser to be safe.',
               done ? 'success' : 'warn', done ? 3200 : 9000);
      return;
    }
    views.showUnlock(async () => {
      await api.auth.refresh();
      ui.renderKeyState(tier(), api.auth.guest);
      ui.toast('Unlocked — AI research armed.', 'success', 3200);
    });
  }, (key) => {
    if (key === 'accent') paintChart();
  }, async (provider, key) => {
    try {
      const res = await api.setProviderKey(provider, key);
      // Keep the cached boot-time health snapshot in sync so a second open
      // of this same modal (or the DATA modal) reflects the change without
      // needing a full page reload.
      if (res && res.ok && state.health) {
        state.health.checks = { ...state.health.checks,
          [provider === 'perplexity' ? 'perplexity_key' : 'groq_key']: res.configured };
      }
      return res;
    } catch (err) {
      return { ok: false, message: (err && err.message) || 'Could not reach the server.' };
    }
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
    // The tour owns the keyboard while it is up. It stops propagation on the
    // keys it handles, but this listener is on document too — without the
    // guard, one Escape would both end the tour and close the reader's panels.
    if (tour.isTouring()) return;

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

  // Session first: the boot log reports the tier, so it must know it.
  await api.auth.refresh();

  const enter = async () => {
    const lines = await bootSequence();
    ui.boot(lines, () => startTerminal());
  };

  /* Read the profile and decide: onboarding for a fresh install, terminal for
     everyone else.

     OWNER ONLY. Onboarding ends in POST /api/profile and POST /api/positions,
     which are owner-gated now — walking a guest through four steps and then
     failing the save would be a cruel way to find that out. A visitor has no
     session at all and would simply 401 on the first read. */
  const enterOrOnboard = async () => {
    let profile = null;
    try { profile = (await api.getProfile()).profile; } catch { /* store may be new */ }

    if (!profile && isOwner()) {
      let positions = {};
      try { positions = (await api.getPositions()).positions; } catch { /* empty book */ }
      views.showOnboarding(positions, async () => {
        try { profile = (await api.getProfile()).profile; } catch { /* ignore */ }
        applyTailoring(profile);
        // Onboarding just walked them through the same surfaces the tour
        // covers. Firing a 6-step coach-mark tour immediately after a 4-step
        // wizard is ten modal steps before they can type anything.
        try { localStorage.setItem('argus.toured', '1'); } catch { /* fine */ }
        enter();
      });
      return;
    }

    applyTailoring(profile);
    enter();
  };

  const landing = (mode) => views.showLanding({
    mode,
    onUnlocked: async () => {
      await api.auth.refresh();
      await enterOrOnboard();
    },
    onVisitor: () => enter(),   // no session => tier() is already 'visitor'
    onSelfKey: (provider, key) => {
      // Still no session — tier() stays 'visitor' exactly as onVisitor
      // above. selfKey only ever unlocks AI research specifically; it never
      // touches the owner's book or admin surface.
      state.selfKey = { provider, key };
      const label = provider.charAt(0).toUpperCase() + provider.slice(1);
      ui.toast(`Using your own ${label} key for research.`, 'success', 3200);
      enter();
    },
  });

  // Expose the landing so in-terminal calls to action can reach it too.
  openLanding = landing;

  if (!api.auth.has()) {
    let chose = null;
    try { chose = sessionStorage.getItem('argus.visitor'); } catch { /* fine */ }
    if (chose) { await enter(); return; }
    landing('landing');
    return;
  }

  await enterOrOnboard();
});

/* Assigned during boot; called by the locked views and the `request` command. */
let openLanding = () => {};

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

    /* First visit only, and never for an owner straight out of onboarding —
       enterOrOnboard() sets the flag for them, because a 6-step coach-mark
       tour immediately after a 4-step wizard is ten modal steps before the
       reader can type anything. They get the one-line hint in welcome(). */
    try { tour.maybeOfferTour(tier()); }
    catch (err) { console.error('[argus] tour failed:', err); }

    setInterval(async () => {
      try {
        const b = await api.getBrent();
        state.brent = b;
        ui.renderBrent(b, state.levels);
      } catch { /* the panel keeps the last good value */ }
    }, 5 * 60 * 1000);
  }
}
