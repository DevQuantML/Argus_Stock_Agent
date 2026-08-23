'use strict';

/* ui.js — every DOM render for ARGUS://TERMINAL.

   Two rules run through this file:
     1. Every field from the API can be null, and quant keys are only
        conditionally present, so nothing is formatted without a finite check.
        A missing metric reads as "N/A" with a reason, never NaN or undefined.
     2. Any string that originated from an LLM or from yfinance goes through
        textContent or renderMarkdown — never innerHTML with raw input. */

import { renderMarkdown, escapeHtml } from './md.js?v=17';
import { sparkline } from './chart.js?v=17';
import * as prefs from './theme.js?v=17';

export const $  = (id)  => document.getElementById(id);
export const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const RM = window.matchMedia('(prefers-reduced-motion:reduce)').matches;

/* ── Formatting ──────────────────────────────────────────────────────── */

const num = (v) =>
  (v === null || v === undefined || v === '' ? null : (Number.isFinite(Number(v)) ? Number(v) : null));

export function money(v, currency = 'USD') {
  const n = num(v);
  if (n === null) return '—';
  const sym = { USD: '$', INR: '₹', EUR: '€', GBP: '£' }[currency] || '';
  return sym + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function pct(v, digits = 2, signed = true) {
  const n = num(v);
  if (n === null) return '—';
  return `${signed && n > 0 ? '+' : ''}${n.toFixed(digits)}%`;
}

export function compact(v) {
  const n = num(v);
  if (n === null) return '—';
  const a = Math.abs(n);
  if (a >= 1e12) return `$${(n / 1e12).toFixed(2)}T`;
  if (a >= 1e9)  return `$${(n / 1e9).toFixed(2)}B`;
  if (a >= 1e6)  return `$${(n / 1e6).toFixed(1)}M`;
  return `$${n.toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
}

const tone = (v) => (num(v) === null ? '' : num(v) >= 0 ? 'pos' : 'neg');

/* Exported: app.js and views.js both build DOM this way, and three byte-identical
   copies of the same four lines is three places for the textContent rule to be
   quietly relaxed in one of them. */
export const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
};

/* ── Toasts ──────────────────────────────────────────────────────────── */

export function toast(message, kind = 'info', ms = 5200) {
  const host = $('toasts');
  if (!host) return;

  const node = el('div', `toast toast-${kind}`);
  node.setAttribute('role', kind === 'error' ? 'alert' : 'status');

  const ico = el('span', 'i', { error: '✕', success: '✓', warn: '⚠', info: '›' }[kind] || '›');
  const msg = el('span', 'm', message);              // textContent — carries server text
  const x   = el('button', 'x', '✕');
  x.setAttribute('aria-label', 'Dismiss');

  node.append(ico, msg, x);
  host.appendChild(node);

  const die = () => { node.classList.add('out'); setTimeout(() => node.remove(), 220); };
  x.onclick = die;
  if (ms) setTimeout(die, ms);
}

/* ── Boot sequence ───────────────────────────────────────────────────── */
/* Real checks, not theatre — each line reports something actually fetched. */

export function boot(lines, onDone) {
  const log  = $('boot-log');
  const host = $('boot');
  if (!log || !host) { onDone && onDone(); return; }

  let i = 0, finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    document.removeEventListener('keydown', skip);
    host.removeEventListener('click', skip);
    host.classList.add('out');
    setTimeout(() => { host.style.display = 'none'; onDone && onDone(); }, 450);
  };
  const skip = () => {
    if (finished) return;
    while (i < lines.length) emit(lines[i++]);
    finish();
  };

  function emit(l) {
    const row = el('div', 'l');
    row.style.color = `var(--${l.c || 'txt'})`;
    row.textContent = l.t;
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
  }

  const step = () => {
    if (finished) return;
    if (i >= lines.length) { setTimeout(finish, 420); return; }
    emit(lines[i++]);
    setTimeout(step, RM ? 10 : (lines[i - 1].pause || prefs.stepDelay()));
  };

  document.addEventListener('keydown', skip);
  host.addEventListener('click', skip);
  step();
}

/* ── Header status lamps ─────────────────────────────────────────────── */

export function renderStatus(health, providerLabel) {
  const set = (id, ok, label) => {
    const n = $(id);
    if (!n) return;
    n.className = `stat ${ok ? 'ok' : 'bad'}`;
    const t = n.querySelector('.t');
    if (t && label) t.textContent = label;
    n.title = ok ? `${label || id} online` : `${label || id} unavailable`;
  };
  const c = (health && health.checks) || {};
  set('st-provider', Boolean(c.ai_provider), providerLabel || 'PROVIDER');
  set('st-brent', Boolean(c.brent_feed), 'BRENT FEED');
  const geo = $('st-geo');
  if (geo) geo.className = 'stat ok';
}

/* Three tiers, and for a guest three distinct states within their tier.
   The allowance is five separately consumable stages, so "used" is a count,
   never a flag: a guest one stage into a dive still has four, and telling them
   the dive is spent would be false. */
export function renderKeyState(tier, guest) {
  const n = $('key-state');
  if (!n) return;

  if (tier === 'owner') {
    n.textContent = 'AI ARMED';
    n.className = 'hdr-key on';
    n.title = 'Owner session — AI research unlocked, no allowance limit';
    return;
  }

  if (tier === 'guest' && guest) {
    const total = guest.modules_total || 5;
    const used = guest.modules_used || 0;
    const left = Math.max(0, total - used);
    const ms = Date.parse(guest.expires_at || '') - Date.now();
    const hrs = Number.isFinite(ms) ? Math.max(0, ms / 3.6e6) : null;
    const when = hrs === null ? '' : hrs < 1 ? ' · <1H' : ` · ${Math.round(hrs)}H`;

    if (guest.exhausted) {
      n.textContent = 'GUEST · DIVE COMPLETE';
      n.className = 'hdr-key guest off';
      n.title = 'Your included deep dive is complete. Everything free stays '
              + 'unlimited until the key expires.';
    } else if (guest.dive_ticker) {
      n.textContent = `GUEST · ${guest.dive_ticker} ${used}/${total}${when}`;
      n.className = 'hdr-key guest';
      n.title = `Your dive is committed to ${guest.dive_ticker}; ${left} of ${total} `
              + 'stages remain and it cannot be moved to another ticker.';
    } else {
      n.textContent = `GUEST · 1 DIVE${when}`;
      n.className = 'hdr-key guest';
      n.title = `One full ${total}-stage AI deep dive on a single ticker of your `
              + 'choice. Everything else is free and unlimited.';
    }
    return;
  }

  n.textContent = 'FREE MODE';
  n.className = 'hdr-key';
  n.title = 'No key — quant, charts, history, news and the Brent gate are free. '
          + 'Paid AI research is locked.';
}

/* ── Ticker tape ─────────────────────────────────────────────────────── */
/* Duplicated once so the CSS translateX(-50%) loop is seamless. */

export function renderTape(items) {
  const host = $('tape');
  if (!host) return;
  host.innerHTML = '';
  if (!items.length) return;

  const build = () => {
    for (const it of items) {
      const w = el('span', 'tk');
      w.append(el('span', 'tk-s', it.sym));
      w.append(el('span', 'tk-p', it.price === null ? '—' : money(it.price, it.currency)));
      const c = num(it.chg);
      w.append(el('span', `tk-c ${c === null ? 'dim' : tone(c)}`, c === null ? '' : pct(c)));
      host.appendChild(w);
    }
  };
  build(); build();
}

export function setTapePaused(paused) {
  $('tape')?.classList.toggle('paused', paused);
  const b = $('btn-tape');
  if (b) { b.textContent = paused ? '▶' : '⏸'; b.setAttribute('aria-label', paused ? 'Resume ticker' : 'Pause ticker'); }
}

/* ── Brent panel ─────────────────────────────────────────────────────── */

const ZONE_TONE = {
  'AGGRESSIVE BUY': 'z-green', 'ADD': 'z-green',
  'HOLD': '', 'CAUTION': 'z-gold', 'DEFENSIVE': 'z-red',
};

export function renderBrent(d, levels) {
  const live = $('brent-live');
  if (!d || d.error) {
    if (live) { live.className = 'chiplive off'; live.innerHTML = '<span class="d">●</span> DOWN'; }
    return;
  }

  const price  = num(d.brent_price);
  const signal = String(d.signal || '').toUpperCase();

  if (live) { live.className = 'chiplive'; live.innerHTML = '<span class="d">●</span> LIVE'; }

  const px = $('brent-px');
  if (px) px.textContent = price === null ? '$--.--' : `$${price.toFixed(2)}`;

  const zone = $('brent-zone');
  if (zone) {
    zone.textContent = signal || '—';
    zone.className = ZONE_TONE[signal] ?? '';
  }

  const act = $('brent-act');
  if (act) act.textContent = d.action || '';

  // Framework ladder — the active band is highlighted, so the gate is legible
  // as a position rather than as a sentence.
  const fw = $('brent-fw');
  if (fw && levels) {
    fw.innerHTML = '';
    for (const v of Object.values(levels)) {
      const [lo, hi] = v.range;
      const row = el('div', `fw${v.signal === signal ? ' on' : ''}`);
      const label = hi >= 9999 ? `>${lo}` : lo === 0 ? `<${hi}` : `${lo}–${hi}`;
      row.append(el('span', 'fw-r', label));
      row.append(el('span', 'fw-l', v.signal));
      fw.appendChild(row);
    }
    const gate = el('div', 'fw');
    gate.style.marginTop = '7px';
    gate.append(el('span', 'fw-r', 'GATE'));
    const g = el('span', 'fw-l', d.gate_open ? 'OPEN · new positions allowed' : 'CLOSED · hold, no new buys');
    g.style.color = d.gate_open ? 'var(--green)' : 'var(--red)';
    gate.appendChild(g);
    fw.appendChild(gate);
  }
}

/* ── Geo transmission map ────────────────────────────────────────────── */
/* config.py's GEO_TRANSMISSION — a static risk map, not a news feed, so it is
   labelled as transmission rather than dressed up as live headlines. */

const EVENT_TONE = {
  middle_east_conflict: 'red', us_china_tech_tension: 'amber',
  russia_ukraine_escalation: 'red', india_geopolitics: 'amber',
  latam_political_risk: 'amber', eu_auto_regulation: 'red',
  us_fed_hawkish_pivot: 'amber', ai_export_controls: 'amber',
};

let geoPriority = [];

/** Sectors the analyst follows — matching vectors sort first. */
export function setGeoPriority(sectors) { geoPriority = (sectors || []).map(s => s.toLowerCase()); }

export function renderGeo(map, held, onTicker) {
  const host = $('geo');
  const count = $('geo-count');
  if (!host) return;
  host.innerHTML = '';

  const entries = Object.entries(map || {});
  if (count) {
    count.className = entries.length ? 'chiplive' : 'chiplive off';
    count.innerHTML = `<span class="d">●</span> ${entries.length} VECTORS`;
  }
  if (!entries.length) {
    host.appendChild(el('div', 'wl-empty', 'No transmission map configured.'));
    return;
  }

  const heldSet = new Set(held || []);

  /* Sectors the analyst chose at onboarding surface first — that is what makes
     the tailoring questions do something rather than sit in a database. */
  if (geoPriority.length) {
    const score = ([event, effects]) => {
      const hay = (event + ' ' + effects.join(' ')).toLowerCase();
      const hit = geoPriority.some(sec => sec.split(/[^a-z]+/).filter(w => w.length > 3)
        .some(w => hay.includes(w)));
      const holds = effects.some(e => heldSet.has(String(e).split('—')[0].trim()));
      return (hit ? 2 : 0) + (holds ? 1 : 0);
    };
    entries.sort((a, b) => score(b) - score(a));
  }

  /* Entries come in two shapes: a bare ticker ("PLTR") or a ticker with an
     explanation ("MELI — Brazil policy hits GMV"). Both must end up as a
     clickable tag; only the second contributes body text. */
  for (const [event, effects] of entries) {
    const item = el('div', 'geo-i');
    item.append(el('div', 'geo-t', event.replace(/_/g, ' ').toUpperCase()));

    const tags = el('div', 'geo-m');
    for (const raw of effects) {
      const line = String(raw);
      const dash = line.indexOf('—');
      const sym  = (dash === -1 ? line : line.slice(0, dash)).trim();
      const note = dash === -1 ? '' : line.slice(dash + 1).trim();

      if (note) item.append(el('div', 'geo-h', note));

      const isTicker = /^[A-Z][A-Z0-9.=-]{0,11}$/.test(sym);
      const holds = heldSet.has(sym);
      const tag = el('span', `tag t-${holds ? (EVENT_TONE[event] || 'amber') : 'muted'}`, sym);
      tag.title = !isTicker ? `${sym} — whole book`
                : holds ? `${sym} — you hold this · click to research`
                : `${sym} — not held · click to research`;
      if (isTicker) tag.onclick = () => onTicker && onTicker(sym);
      else tag.style.cursor = 'default';
      tags.appendChild(tag);
    }
    item.append(tags);
    host.appendChild(item);
  }
}

/* ── Watchlist ───────────────────────────────────────────────────────── */

function riskState(price, stop, trim) {
  if (price === null || stop === null) return { cls: '', label: '—', dot: '' };
  if (price <= stop) return { cls: 'danger', label: 'AT STOP', dot: 'red' };
  if (trim !== null && price >= trim) return { cls: 'trim', label: 'AT TRIM', dot: 'teal' };
  const head = ((price - stop) / price) * 100;
  if (head < 10) return { cls: 'danger', label: 'NEAR STOP', dot: 'red' };
  if (head < 20) return { cls: 'watch', label: 'WATCH', dot: 'amber' };
  return { cls: 'ok', label: 'HEALTHY', dot: 'green' };
}

let sortKey = 'sym', sortDir = 1;

export function setSort(key) {
  if (sortKey === key) sortDir = -sortDir; else { sortKey = key; sortDir = 1; }
  $$('#watch-head span[data-sort]').forEach(s =>
    s.classList.toggle('a', s.dataset.sort === sortKey));
  return { sortKey, sortDir };
}

export function renderWatchlist(rows, onSelect, onInspect, emptyText) {
  const host = $('watch-rows');
  if (!host) return;
  host.innerHTML = '';

  if (!rows.length) {
    // The caller supplies the reason. An empty panel means something different
    // to a visitor (the book is private) than to the owner (nothing tracked
    // yet), and only the caller knows which.
    host.appendChild(el('div', 'wl-empty', emptyText || 'No instruments loaded.'));
    return;
  }

  const sorted = [...rows].sort((a, b) => {
    const av = a[sortKey], bv = b[sortKey];
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    return (typeof av === 'string' ? av.localeCompare(bv) : av - bv) * sortDir;
  });

  for (const r of sorted) {
    const row = el('div', 'wrow');
    row.tabIndex = 0;
    row.setAttribute('role', 'button');
    row.dataset.sym = r.sym;
    row.setAttribute('aria-label', `${r.sym} — research`);

    const sym = el('div', 'w-sym');
    sym.append(document.createTextNode(r.sym));
    if (r.held) sym.append(el('span', 'h', '●'));
    row.append(sym);

    row.append(el('div', 'w-last tabnum', r.last === null ? '—' : money(r.last, r.currency)));

    const chg = el('div', `w-chg tabnum ${r.chg === null ? 'dim' : tone(r.chg)}`);
    chg.textContent = r.chg === null ? '—' : pct(r.chg);
    row.append(chg);

    const sig = el('div', 'w-sig');
    sig.textContent = r.sig;
    sig.style.color = { HEALTHY: 'var(--green)', WATCH: 'var(--gold)', 'NEAR STOP': 'var(--red)',
                        'AT STOP': 'var(--red)', 'AT TRIM': 'var(--teal)' }[r.sig] || 'var(--txt-muted)';
    row.append(sig);

    const dot = el('div', 'w-dot');
    dot.innerHTML = `<i class="${r.dot}"></i>`;
    row.append(dot);

    const go = () => onSelect && onSelect(r.sym);
    row.addEventListener('click', (e) => {
      if (e.altKey || e.metaKey) { onInspect && onInspect(r.sym); return; }
      go();
    });
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
      if (e.key === 'i') { e.preventDefault(); onInspect && onInspect(r.sym); }
    });
    host.appendChild(row);
  }
}

export function renderWatchFooter(rows, totals) {
  const held = rows.filter(r => r.held).length;
  const tally = $('watch-tally');
  if (tally) tally.textContent = `${held} HELD · ${rows.length - held} WATCHING`;

  const count = $('watch-count');
  if (count) {
    count.className = rows.length ? 'chiplive' : 'chiplive off';
    count.innerHTML = `<span class="d">●</span> ${rows.length} INSTR`;
  }

  const pnl = $('watch-pnl');
  if (pnl && totals) {
    const v = num(totals.unrealized_pnl);
    const p = num(totals.unrealized_pnl_pct);
    pnl.textContent = v === null ? '—'
      : `P&L ${v >= 0 ? '+' : '−'}${money(Math.abs(v))} (${pct(p)})`;
    pnl.className = `tabnum ${tone(v)}`;
  }
}

export function attachSparkline(sym, values, positive) {
  const row = $$('#watch-rows .wrow').find(r => r.dataset.sym === sym);
  if (!row) return;
  const cell = row.querySelector('.w-last');
  if (!cell || cell.querySelector('svg')) return;
  const svg = sparkline(values, { positive, width: 42, height: 13 });
  svg.style.marginRight = '4px';
  svg.style.verticalAlign = 'middle';
  cell.prepend(svg);
}

export function flashRow(sym) {
  const row = $$('#watch-rows .wrow').find(r => r.dataset.sym === sym);
  if (!row) return;
  row.classList.remove('flash');
  void row.offsetWidth;             // restart the animation
  row.classList.add('flash');
}

export function markSelected(sym) {
  $$('#watch-rows .wrow').forEach(r => r.classList.toggle('sel', r.dataset.sym === sym));
}

/* ── Research tabs ───────────────────────────────────────────────────── */

export function renderTabs(tabs, active, onPick, onClose) {
  const host = $('tabs');
  if (!host) return;
  host.innerHTML = '';
  for (const t of tabs) {
    const node = el('button', `tab${t.sym === active ? ' on' : ''}`);
    node.type = 'button';
    node.append(document.createTextNode(t.sym));
    const x = el('span', 'x', '✕');
    x.onclick = (e) => { e.stopPropagation(); onClose && onClose(t.sym); };
    node.appendChild(x);
    node.onclick = () => onPick && onPick(t.sym);
    host.appendChild(node);
  }
}

export function setStatus(text, kind = '') {
  const n = $('res-status');
  if (!n) return;
  n.textContent = text;
  n.className = kind;
}

export const out = () => $('out');

export function clearOut() {
  const o = out();
  if (o) o.innerHTML = '';
}

export function writeLine(text, cls = '') {
  const o = out();
  if (!o) return null;
  const n = el('div', cls, text);
  o.appendChild(n);
  o.scrollTop = o.scrollHeight;
  return n;
}

export function writeTitle(text) { return writeLine(text, 'r-title'); }

/* ── Typewriter ──────────────────────────────────────────────────────── */
/* Reveals rendered markdown by walking text nodes, so tags stay intact and
   the XSS-escaping in md.js is never bypassed. */

/* Hard rule: this is decoration. It must never be able to stall a caller that
   awaits it. Background tabs clamp setTimeout to ~1s, which would otherwise
   turn a 4-second reveal into several minutes and block the pipeline behind
   it — so a hidden tab, reduced motion, or the overall cap all fall through
   to an instant reveal. */
const TYPE_MAX_MS = 6000;

export function typeHTML(container, html, done) {
  const holder = el('div', 'r-body');
  holder.innerHTML = html;
  container.appendChild(holder);

  let settled = false;
  const finish = () => { if (!settled) { settled = true; done && done(); } };

  if (RM || document.hidden) { finish(); return finish; }

  const walker = document.createTreeWalker(holder, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (n.nodeValue.trim()) { nodes.push({ node: n, full: n.nodeValue }); n.nodeValue = ''; }
  }
  if (!nodes.length) { finish(); return finish; }

  const scroller = container.closest('#out') || container;
  const total = nodes.reduce((s, n) => s + n.full.length, 0);
  const delay = prefs.typeDelay();
  // Scale the chunk so even a long module finishes inside the cap.
  const CHUNK = Math.max(3, Math.ceil(total / (TYPE_MAX_MS / delay)));

  let i = 0, ch = 0, timer = null, cancelled = false;
  const started = performance.now();

  const revealAll = () => {
    for (const n of nodes) n.node.nodeValue = n.full;
    scroller.scrollTop = scroller.scrollHeight;
  };
  const stop = () => {
    cancelled = true;
    clearTimeout(timer);
    document.removeEventListener('visibilitychange', onHide);
    revealAll();
    finish();
  };
  const onHide = () => { if (document.hidden) stop(); };
  document.addEventListener('visibilitychange', onHide);

  const tick = () => {
    if (cancelled) return;
    if (performance.now() - started > TYPE_MAX_MS) { stop(); return; }
    const cur = nodes[i];
    ch = Math.min(cur.full.length, ch + CHUNK);
    cur.node.nodeValue = cur.full.slice(0, ch);
    scroller.scrollTop = scroller.scrollHeight;
    if (ch >= cur.full.length) { i++; ch = 0; }
    if (i >= nodes.length) {
      document.removeEventListener('visibilitychange', onHide);
      finish();
      return;
    }
    timer = setTimeout(tick, delay);
  };
  tick();

  return stop;                                    // cancel → reveal instantly
}

/* ── Pipeline log ────────────────────────────────────────────────────── */

export const PIPE_STAGES = [
  ['fundamentals', 'FUNDAMENTALS'], ['quant', 'QUANT ENGINE'],
  ['brent', 'BRENT GATE'],          ['history', 'PRICE HISTORY'],
  ['report', 'AI · RESEARCH BRIEF'], ['context', 'AI · COMPANY CONTEXT'],
  ['policy', 'AI · POLITICIAN TRADES'], ['patterns', 'AI · HIDDEN PATTERNS'],
  ['synthesis', 'AI · VERDICT + DEBATE'],
];

const ICONS = {
  pending: '·', running: '◐', done: '✓', failed: '✕',
  skipped: '–', locked: '⚿', cancelled: '■',
};
const META = {
  pending: '', running: 'running', skipped: 'skipped',
  locked: 'needs agent key', cancelled: 'cancelled', failed: 'failed',
};

export const pipeline = {
  mount(container, locked = false) {
    const box = el('div', 'pipe');
    box.id = 'pipe';
    for (const [key, label] of PIPE_STAGES) {
      const ai = PIPE_STAGES.findIndex(s => s[0] === key) >= 4;
      const row = el('div', `pl ${locked && ai ? 'lock' : 'pending'}`);
      row.id = `pl-${key}`;
      row.append(el('span', 'ic', locked && ai ? ICONS.locked : ICONS.pending));
      row.append(el('span', 'nm', label));
      row.append(el('span', 'mt', locked && ai ? META.locked : ''));
      box.appendChild(row);
    }
    container.appendChild(box);
  },

  set(stage, status, meta) {
    const row = $(`pl-${stage}`);
    if (!row) return;
    row.className = `pl ${status}`;
    const ic = row.querySelector('.ic');
    const mt = row.querySelector('.mt');
    if (ic) {
      ic.textContent = ICONS[status] ?? '·';
      ic.className = status === 'running' ? 'ic spin' : 'ic';
    }
    if (mt) mt.textContent = meta !== undefined ? meta : (META[status] ?? '');
  },

  done(stage, ms) { this.set(stage, 'done', `${(ms / 1000).toFixed(1)}s`); },
};

/* ── Quant grid ──────────────────────────────────────────────────────── */

/* Why a metric is absent, as the four natures the engine reports. The chip
   answers the only question an "N/A" leaves the reader holding: is this worth
   waiting for? Only `transient` is. The other three are answers, not gaps, so
   they are styled as information rather than as damage. */
const NATURE = {
  transient:      { chip: 'TEMPORARY',  cls: 'nat-wait',
                    lead: 'Data gap — expected to clear on its own.' },
  structural:     { chip: 'NOT FORMED', cls: 'nat-struct',
                    lead: 'The business is why this has no value right now.' },
  unsupported:    { chip: 'N/A HERE',   cls: 'nat-none',
                    lead: 'This metric does not apply to this listing.' },
  methodological: { chip: 'WITHHELD',   cls: 'nat-hold',
                    lead: 'Computable, but the number would mislead.' },
};

const statusOf = (q, key) => (q?.metric_status || {})[key] || null;

const METRICS = {
  dcf: {
    key: 'dcf',
    label: 'DCF VALUE',
    help: 'Discounted Cash Flow — what the business is worth based on the cash it should generate over five years. Above the current price suggests undervaluation.',
    read: (q, s) => {
      const v = num(q.dcf_intrinsic_value); if (v === null) return null;
      const up = num(q.dcf_upside_pct);
      return { value: money(v, s?.currency), t: up === null ? '' : up >= 0 ? 'pos' : 'neg',
               sub: up === null ? 'vs price' : `${pct(up)} ${up >= 0 ? 'upside' : 'downside'}`,
               bar: up === null ? 50 : Math.max(4, Math.min(100, 50 + up / 2)) };
    },
    /* No local `absent` guess. Every one written here was a hypothesis about
       what the engine had decided, and the hypotheses drifted: this card told
       MELI's reader "Forward P/E not reported" while its forward P/E sat at
       31.7 and the real reason — contracting earnings — went unsaid. The
       engine knows which branch it took; the card prints that. */
    fallback: 'Needs positive free cash flow and a usable growth rate.',
  },
  roic: {
    key: 'roic',
    label: 'ROIC',
    help: 'Return on Invested Capital — profit per dollar of capital in the business. Above 15% marks a company that compounds well.',
    read: (q) => {
      const v = num(q.roic); if (v === null) return null;
      return { value: `${v.toFixed(1)}%`, t: v >= 15 ? 'pos' : 'warn',
               sub: 'quality above 15%', bar: Math.max(3, Math.min(100, (v / 30) * 100)) };
    },
    fallback: 'Operating income or equity missing from the annual statements.',
  },
  fcf: {
    key: 'fcf_yield',
    label: 'FCF YIELD',
    help: 'Free cash flow as a percentage of market value. Above 4% is attractive — think of it as the cash return you buy.',
    read: (q) => {
      const v = num(q.fcf_yield); if (v === null) return null;
      return { value: `${v.toFixed(2)}%`, t: v >= 4 ? 'pos' : v > 0 ? '' : 'neg',
               sub: 'attractive above 4%', bar: Math.max(3, Math.min(100, (Math.max(v, 0) / 8) * 100)) };
    },
    fallback: 'FCF or market cap missing.',
  },
  peg: {
    key: 'peg_ratio',
    label: 'PEG',
    help: 'P/E divided by growth rate. Below 1.0 means you pay comparatively little for the growth on offer.',
    read: (q) => {
      const v = num(q.peg_ratio); if (v === null) return null;
      return { value: v.toFixed(2), t: v < 1 ? 'pos' : v < 2 ? 'warn' : 'neg',
               sub: 'cheap below 1.0', bar: Math.max(3, Math.min(100, 100 - (v / 5) * 100)) };
    },
    fallback: 'Forward P/E or a usable growth rate was not available.',
  },
  sharpe: {
    key: 'sharpe',
    label: 'SHARPE 1Y',
    help: 'Return earned per unit of volatility over the past year. Above 1.0 is strong; negative means the volatility has not been paying you.',
    read: (q) => {
      const v = num(q.sharpe_ratio); if (v === null) return null;
      return { value: v.toFixed(2), t: v >= 1 ? 'pos' : v >= 0 ? 'warn' : 'neg',
               sub: 'strong above 1.0', bar: Math.max(3, Math.min(100, ((v + 1) / 3) * 100)) };
    },
    fallback: 'Not enough price history.',
  },
  beta: {
    key: 'beta',
    label: 'BETA',
    help: 'How hard the stock moves relative to the market. Above 1 swings harder in both directions — size the position accordingly.',
    read: (q) => {
      const v = num(q.beta); if (v === null) return null;
      return { value: v.toFixed(2), t: '', sub: 'market = 1.00',
               bar: Math.max(3, Math.min(100, (v / 3) * 100)) };
    },
    fallback: 'Beta not reported.',
  },
};

/* ── DCF disclosure ──────────────────────────────────────────────────────
   A single intrinsic value reads as a verdict. It is really a function of two
   assumptions, so show them, show where the inputs came from and which fiscal
   period they cover, and show the value across a spread of both. The reverse
   DCF leads because "the price already assumes 45% growth" is a claim the
   reader can actually assess, where "$106.88 intrinsic" is not. */

const fmtPct1 = (v) => (num(v) === null ? '—' : `${Number(v).toFixed(1)}%`);

/* compact() prefixes a currency symbol. A share count is not money. */
const countCompact = (v) => {
  const n = num(v);
  if (n === null) return '—';
  const a = Math.abs(n);
  if (a >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  return n.toLocaleString('en-US', { maximumFractionDigits: 0 });
};

function sensitivityTable(grid, price, ccy) {
  /* Built as 3 discount rates × 3 growth rates in that nesting order. Anything
     other than a full grid means cells were dropped, and a partial table would
     misalign its own headers — skip it rather than mislabel. */
  if (!Array.isArray(grid) || grid.length !== 9) return null;

  const table = el('table', 'qsens');
  const cap = document.createElement('caption');
  cap.textContent = 'INTRINSIC VALUE — DISCOUNT RATE × GROWTH';
  table.appendChild(cap);

  const head = el('tr');
  head.appendChild(el('th', null, ''));
  for (let g = 0; g < 3; g++) head.appendChild(el('th', null, fmtPct1(grid[g].growth_rate * 100)));
  table.appendChild(head);

  for (let w = 0; w < 3; w++) {
    const tr = el('tr');
    tr.appendChild(el('th', null, fmtPct1(grid[w * 3].discount_rate * 100)));
    for (let g = 0; g < 3; g++) {
      const cell = grid[w * 3 + g];
      const td = el('td', w === 1 && g === 1 ? 'mid' : null, money(cell.intrinsic, ccy));
      if (cell.upside_pct !== undefined) {
        td.title = `${cell.upside_pct >= 0 ? '+' : ''}${cell.upside_pct}% vs ${money(price, ccy)}`;
      }
      tr.appendChild(td);
    }
    table.appendChild(tr);
  }
  return table;
}

export function renderDcfDisclosure(container, quant, stock) {
  const q = quant || {};
  const a = q.dcf_assumptions;
  if (!a) return;

  const ccy = stock?.currency;
  const src = q.sources || {};
  const box = el('div', 'qdisc');

  const head = el('div', 'qdisc-h');
  head.append(el('span', 't', 'HOW THIS DCF WAS BUILT'));
  head.append(el('span', 'g', `${a.projection_years}-year model · terminal ${fmtPct1(a.terminal_growth * 100)}`));
  box.append(head);

  const implied = num(q.dcf_implied_growth_pct);
  if (implied !== null) {
    const used = num(a.growth_uncapped);
    const imp = el('div', 'qdisc-imp');
    imp.append(el('div', 'l', 'IMPLIED GROWTH — REVERSE DCF'));
    imp.append(el('div', 'v', `${implied.toFixed(1)}% / yr`));
    imp.append(el('div', 's',
      used === null
        ? 'The annual free cash flow growth the current price already assumes.'
        : `The current price already assumes this for ${a.projection_years} years. `
          + `The trailing rate was ${(used * 100).toFixed(1)}%.`));
    box.append(imp);
  }

  const rows = el('div', 'qdisc-a');
  const line = (k, v) => { rows.append(el('span', 'k', k)); rows.append(el('span', 'v', v)); };

  const fcfSrc = src.fcf || {};
  line('Free cash flow', `${compact(a.fcf_used)}${fcfSrc.period ? `  ·  FY ${fcfSrc.period}` : ''}`);
  if (fcfSrc.origin) line('FCF source', fcfSrc.origin);

  /* A converted DCF is a different claim from a native one — the reader is
     owed the rate that got it there, not just the result. */
  const fxSrc = src.fx;
  if (fxSrc && num(fxSrc.rate) !== null) {
    line('Currency', `${fxSrc.from} statements → ${fxSrc.to} price`);
    line('FX rate applied', `${Number(fxSrc.rate).toPrecision(6)} ${fxSrc.to}/${fxSrc.from}`
      + (fxSrc.pair ? `  ·  ${fxSrc.pair}` : ''));
  }

  const gSrc = (src.dcf || {}).growth || {};
  line('Growth applied', `${fmtPct1(a.growth_rate_used * 100)} / yr`
    + (a.growth_capped ? `  ·  capped from ${fmtPct1(a.growth_uncapped * 100)}` : ''));
  if (gSrc.origin) {
    line('Growth source', gSrc.years ? `${gSrc.origin} (${gSrc.years}y)` : gSrc.origin);
  }

  const rSrc = src.discount_rate || {};
  line('Discount rate', `${fmtPct1(a.discount_rate * 100)}`
    + (rSrc.method === 'capm' ? `  ·  CAPM, beta ${rSrc.beta}` : '  ·  flat fallback'));
  line('Net debt', compact(a.net_debt_used));
  line('Shares', countCompact(a.shares_used));
  box.append(rows);

  const table = sensitivityTable(q.dcf_sensitivity, num(stock?.price), ccy);
  if (table) box.append(table);

  container.appendChild(box);
}

/* One compact strip above the grid. It counts by nature rather than listing
   metric names, because the reader's next action depends on the nature and not
   on which metric it was: something to wait for, or nothing to wait for. */
function headsUp(q) {
  const entries = Object.values(q.metric_status || {}).filter((s) => s.state === 'unavailable');
  if (!entries.length) return null;

  const counts = entries.reduce((acc, s) => (acc[s.nature] = (acc[s.nature] || 0) + 1, acc), {});
  const waiting = counts.transient || 0;

  const box = el('div', `hup ${waiting ? 'hup-wait' : ''}`);
  box.append(el('span', 'hup-c', waiting ? 'HEADS UP' : 'NOTE'));

  const parts = [];
  if (waiting) parts.push(`${waiting} metric${waiting > 1 ? 's' : ''} temporarily unavailable`);
  if (counts.structural) parts.push(`${counts.structural} not formed by the fundamentals`);
  if (counts.unsupported) parts.push(`${counts.unsupported} not applicable to this listing`);
  if (counts.methodological) parts.push(`${counts.methodological} withheld as misleading`);
  box.append(el('span', 'hup-t', parts.join(' · ')));

  box.append(el('span', 'hup-s', waiting
    ? 'Only the temporary ones are a data problem — the rest are findings. Each card says which.'
    : 'Nothing here is a data failure. Each card gives the reason.'));

  /* A currency conversion changes what the numbers mean, so it is stated
     whether or not anything went missing. */
  const cm = q.currency_mismatch;
  if (cm?.resolved && num(cm.rate) !== null) {
    box.append(el('span', 'hup-s',
      `Statements are in ${cm.financial} and the listing trades in ${cm.market}; `
      + `cash-flow metrics were converted at ${Number(cm.rate).toPrecision(6)} ${cm.market}/${cm.financial}.`));
  }
  return box;
}

export function renderQuant(container, quant, stock) {
  const q = quant || {};
  const grid = el('div', 'qgrid');
  const bars = [];

  for (const [, def] of Object.entries(METRICS)) {
    const d = def.read(q, stock);
    const card = el('div', 'qc');

    const head = el('div', 'qc-l');
    head.append(el('span', null, def.label));
    const help = el('button', null, '?');
    help.type = 'button';
    help.dataset.tip = def.help;
    help.setAttribute('aria-label', `What is ${def.label}?`);
    head.append(help);
    card.append(head);

    if (d) {
      card.append(el('div', `qc-v ${d.t}`, d.value));
      card.append(el('div', 'qc-s', d.sub));
      const bar = el('div', 'qbar');
      const fill = el('i', d.t);
      bar.appendChild(fill);
      card.append(bar);
      bars.push([fill, d.bar]);
    } else {
      const st = statusOf(q, def.key);
      const nat = st && NATURE[st.nature];
      card.classList.add('qc-na');

      const vrow = el('div', 'qc-na-h');
      vrow.append(el('div', 'qc-v na', 'N/A'));
      if (nat) {
        const chip = el('span', `qc-nat ${nat.cls}`, nat.chip);
        chip.dataset.tip = nat.lead;
        vrow.append(chip);
      }
      card.append(vrow);

      card.append(el('div', 'qc-s', st?.reason || def.fallback));
      if (st?.resolves) card.append(el('div', 'qc-fix', st.resolves));
    }
    grid.appendChild(card);
  }

  const hup = headsUp(q);
  if (hup) container.appendChild(hup);
  container.appendChild(grid);

  renderDcfDisclosure(container, q, stock);

  const flags = Array.isArray(q.quant_flags) ? q.quant_flags : [];
  if (flags.length) {
    const box = el('div', 'flags');
    for (const f of flags) {
      const good = /cleared|attractive|strong|undervalued|upside|exceptional/i.test(f);
      const bad  = /weak|negative|expensive|thin|downside|below|skipped|premium/i.test(f);
      const row = el('div', `flag ${good ? 'pos' : bad ? 'neg' : ''}`);
      row.append(el('span', null, f));
      box.appendChild(row);
    }
    container.appendChild(box);
  }

  requestAnimationFrame(() => requestAnimationFrame(() => {
    for (const [fill, w] of bars) fill.style.width = `${w}%`;
  }));
}

/* ── Verdict ─────────────────────────────────────────────────────────── */

/* The synthesis prompt pins line 1 to a fixed shape so the headline numbers
   can be lifted out. Tolerant on purpose: models bold the line, and on a SELL
   they reasonably relabel ENTRY as EXIT. */
const VERDICT_RE =
  /VERDICT:\s*\**\s*(BUY|SELL|HOLD)\s*\**\s*\|\s*\**\s*CONVICTION:\s*\**\s*(HIGH|MEDIUM|LOW)\s*\**\s*\|\s*\**\s*(?:ENTRY|EXIT|ACTION|TARGET|LEVEL)[^:|]*:\s*(.+)/i;

export function renderVerdict(container, text) {
  if (!text) return;
  const box = el('div', 'verdict');
  box.append(el('div', 'l', 'VERDICT'));

  const m = text.match(VERDICT_RE);
  const call = m ? m[1].toUpperCase() : null;

  box.append(el('div', `v ${call ? call.toLowerCase() : ''}`, call || '—'));

  const meta = el('div', 'meta');
  const cell = (k, v) => {
    const d = el('div');
    d.append(el('div', 'k', k));
    d.append(el('div', 'val', v));
    return d;
  };
  meta.append(cell('CONVICTION', m ? m[2].toUpperCase() : '—'));
  meta.append(cell('ACTION LEVEL', m ? m[3].replace(/\*+/g, '').trim() : '—'));
  box.append(meta);

  const body = el('div', 'body');
  body.innerHTML = renderMarkdown(m ? text.slice(m.index + m[0].length).trim() : text);
  body.classList.add('r-body');
  box.append(body);

  container.appendChild(box);
  container.scrollTop = container.scrollHeight;
}

/* ── Inspector ───────────────────────────────────────────────────────── */

export function openInspector(sym, stock, quant) {
  const panel = $('insp');
  const body  = $('insp-body');
  const title = $('insp-title');
  if (!panel || !body) return;

  title.textContent = `INSPECTOR://${sym}`;
  body.innerHTML = '';

  const mp    = stock?.my_position;
  const watch = stock?.watchlist;
  const price = num(stock?.price);
  const ccy   = stock?.currency;

  const row = (k, v, cls) => {
    const r = el('div', 'ins-row');
    r.append(el('span', 'k', k));
    r.append(el('span', `v ${cls || ''}`, v));
    return r;
  };

  body.append(el('div', 'ins-sec', 'MARKET'));
  body.append(row('Last', money(price, ccy)));
  body.append(row('52w high', money(stock?.['52w_high'], ccy)));
  body.append(row('52w low', money(stock?.['52w_low'], ccy)));
  body.append(row('From high', pct(stock?.pct_from_52w_high), tone(stock?.pct_from_52w_high)));
  body.append(row('Market cap', compact(stock?.market_cap)));
  body.append(row('Analyst target', money(stock?.analyst_target, ccy)));

  if (mp) {
    const stop = num(mp.stop_loss), trim = num(mp.trim_at);
    body.append(el('div', 'ins-sec', 'POSITION'));
    body.append(row('Shares', mp.shares ?? '—'));
    body.append(row('Avg cost', money(mp.avg_cost, ccy)));
    body.append(row('Unrealized', pct(mp.unrealized_pnl_pct), tone(mp.unrealized_pnl_pct)));
    body.append(row('Stop-loss', money(mp.stop_loss, ccy)));
    const toStop = num(mp.distance_to_stop);
    body.append(row('To stop', pct(toStop), toStop !== null && toStop <= 10 ? 'neg' : ''));
    body.append(row('Trim at', money(mp.trim_at, ccy)));
    body.append(row('To trim', pct(mp.distance_to_trim)));

    if (price !== null && stop !== null && trim !== null && trim > stop) {
      const off = Math.max(0, Math.min(100, ((price - stop) / (trim - stop)) * 100));
      const st = riskState(price, stop, trim);
      const bar = el('div', 'risk');
      bar.innerHTML = `<i class="${st.cls}" style="width:${off.toFixed(1)}%"></i><b style="left:${off.toFixed(1)}%"></b>`;
      bar.title = `stop ${money(stop, ccy)} → price ${money(price, ccy)} → trim ${money(trim, ccy)}`;
      body.append(bar);
      const legend = el('div', 'ins-row');
      legend.append(el('span', 'k', money(stop, ccy)));
      legend.append(el('span', 'v', money(trim, ccy)));
      legend.style.borderBottom = 'none';
      body.append(legend);
    }

    if (mp.thesis) {
      body.append(el('div', 'ins-sec', 'THESIS'));
      body.append(el('div', 'ins-th', mp.thesis));
    }
    renderLadder(body, mp.tranches, price, ccy, 'TRANCHE LADDER');
  } else if (watch) {
    body.append(el('div', 'ins-sec', 'WATCHLIST'));
    body.append(row('Brent gated', watch.brent_gated ? 'Yes' : 'No'));
    if (watch.exchange) body.append(row('Exchange', watch.exchange));
    if (watch.thesis) {
      body.append(el('div', 'ins-sec', 'THESIS'));
      body.append(el('div', 'ins-th', watch.thesis));
    }
    renderLadder(body, watch.tranches, price, ccy, 'ENTRY LEVELS');
  } else {
    body.append(el('div', 'ins-sec', 'POSITION'));
    body.append(el('div', 'hint', 'Not held and not on the watchlist — market data only.'));
  }

  const q = quant || {};
  if (Object.keys(q).length) {
    body.append(el('div', 'ins-sec', 'QUANT'));
    body.append(row('DCF value', money(q.dcf_intrinsic_value, ccy)));
    body.append(row('DCF upside', pct(q.dcf_upside_pct), tone(q.dcf_upside_pct)));
    body.append(row('Implied growth', q.dcf_implied_growth_pct === undefined || q.dcf_implied_growth_pct === null
      ? '—' : `${Number(q.dcf_implied_growth_pct).toFixed(1)}%`));
    body.append(row('ROIC', q.roic === undefined || q.roic === null ? '—' : `${Number(q.roic).toFixed(1)}%`));
    body.append(row('FCF yield', q.fcf_yield === undefined || q.fcf_yield === null ? '—' : `${Number(q.fcf_yield).toFixed(2)}%`));
    body.append(row('Sharpe', q.sharpe_ratio === undefined || q.sharpe_ratio === null ? '—' : Number(q.sharpe_ratio).toFixed(2)));
    body.append(row('Beta', q.beta === undefined || q.beta === null ? '—' : Number(q.beta).toFixed(2)));

    /* The dashes above are not self-explanatory. Same reasons as the cards,
       so the two surfaces cannot drift apart the way the old hardcoded
       strings did. */
    const gaps = Object.entries(q.metric_status || {}).filter(([, s]) => s.state === 'unavailable');
    if (gaps.length) {
      body.append(el('div', 'ins-sec', 'WHY THOSE ARE BLANK'));
      for (const [metric, s] of gaps) {
        const nat = NATURE[s.nature];
        body.append(row(metric.replace(/_/g, ' '), nat ? nat.chip : 'N/A'));
        body.append(el('div', 'ins-th', s.reason));
      }
    }

    /* Provenance. A metric the reader cannot trace back to a filing and a
       fiscal period is one they have to take on trust — which is the thing a
       deterministic engine is supposed to remove. */
    const src = q.sources || {};
    const roicSrc = src.roic;
    if (roicSrc) {
      body.append(el('div', 'ins-sec', 'ROIC INPUTS'));
      /* These come off the filings, so they are in the reporting currency —
         which is not the trading currency for an ADR. Say so, because
         compact() stamps a dollar sign on everything. */
      if (q.currency_mismatch?.financial) {
        body.append(row('Reported in', q.currency_mismatch.financial));
      }
      body.append(row('Operating income', compact(roicSrc.operating_income)));
      body.append(row('Invested capital', compact(roicSrc.invested_capital)));
      body.append(row('Tax rate', roicSrc.tax_rate === undefined ? '—' : `${(roicSrc.tax_rate * 100).toFixed(1)}%`));
      if (roicSrc.tax_rate_source) body.append(row('Tax basis', roicSrc.tax_rate_source));
      if (roicSrc.operating_income_period) body.append(row('Fiscal period', roicSrc.operating_income_period));
    }

    if (q.dcf_assumptions) renderDcfDisclosure(body, q, stock);
  }

  panel.classList.add('open');
}

function renderLadder(host, tranches, price, ccy, title) {
  const levels = Array.isArray(tranches) ? tranches.map(num).filter(v => v !== null) : [];
  host.append(el('div', 'ins-sec', title));
  if (!levels.length) {
    host.append(el('div', 'hint', 'No tranche levels set — monitoring only.'));
    return;
  }
  for (const lv of [...levels].sort((a, b) => b - a)) {
    const hit = price !== null && price <= lv;
    const r = el('div', `rung${hit ? ' hit' : ''}`);
    r.append(el('i'));
    r.append(el('span', 'px', money(lv, ccy)));
    // A level below the price needs a fall to trigger — say how far, rather
    // than a negative "distance" that reads like a loss.
    r.append(el('span', 'tg',
      price === null ? 'awaiting price'
        : hit ? 'buy zone'
        : `needs ${(((price - lv) / price) * 100).toFixed(1)}% fall`));
    host.append(r);
  }
  if (price !== null) {
    const now = el('div', 'rung now');
    now.append(el('i'));
    now.append(el('span', 'px', money(price, ccy)));
    now.append(el('span', 'tg', 'current'));
    host.append(now);
  }
}

export function closeInspector() { $('insp')?.classList.remove('open'); }

/* ── Modals ──────────────────────────────────────────────────────────── */

function modal(innerHTML, { wide = true } = {}) {
  const ov = el('div', 'ov');
  const box = el('div', `mdl${wide ? '' : ' sm'}`);
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  box.innerHTML = innerHTML;
  ov.appendChild(box);
  document.body.appendChild(ov);

  const close = () => { document.removeEventListener('keydown', onKey, true); ov.remove(); };
  const onKey = (e) => {
    if (e.key !== 'Escape') return;
    e.stopPropagation();                 // must not also close the inspector
    close();
  };
  document.addEventListener('keydown', onKey, true);
  ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
  box.querySelector('[data-close]')?.addEventListener('click', close);
  return { ov, box, close };
}

const MODULES = [
  { key: 'report',    name: 'RESEARCH BRIEF',    cost: 0.04, desc: 'Situation, quant read, live catalysts, thesis check.' },
  { key: 'context',   name: 'COMPANY CONTEXT',   cost: 0.04, desc: 'Business model, revenue and EBITDA, customers, competitors, future fit.' },
  { key: 'policy',    name: 'POLITICIAN TRADES', cost: 0.04, desc: 'Congressional and official disclosures, overlap with your book, policy catalysts.' },
  { key: 'patterns',  name: 'HIDDEN PATTERNS',   cost: 0.04, desc: 'Insider, 13F, options, supply-chain and hiring signals cross-referenced.' },
  { key: 'synthesis', name: 'VERDICT + DEBATE',  cost: 0.05, desc: 'Weighs every module into one BUY/SELL/HOLD call, plus bull and bear.' },
];

const MODULES_KEY = 'argus.modules';

function loadSelection() {
  try {
    const s = JSON.parse(localStorage.getItem(MODULES_KEY) || 'null');
    if (s && typeof s === 'object') return Object.fromEntries(MODULES.map(m => [m.key, s[m.key] !== false]));
  } catch { /* corrupt entry — fall through to the default */ }
  return Object.fromEntries(MODULES.map(m => [m.key, true]));
}

export function showCostModal(sym, provider, { tier = 'owner', guest = null } = {}) {
  return new Promise((resolve) => {
    const isGuest = tier === 'guest';

    /* A guest's dive is indivisible.
       The owner picks modules à la carte because they pay per call and can run
       the rest tomorrow. A guest's allowance is one dive: unchecking three
       modules still binds the key to this ticker on the first consumed stage,
       and the forfeited stages can never be recovered or moved elsewhere. So
       for a guest the selection is the full pipeline, fixed and disabled —
       "one deep dive" then means exactly one thing, and the forfeiture case
       stops existing rather than being explained. */
    const sel = isGuest
      ? MODULES.reduce((o, m) => (o[m.key] = true, o), { synthesis: true })
      : loadSelection();

    const rows = MODULES.map(m => `
      <label class="cf${sel[m.key] ? ' on' : ''}" data-k="${m.key}">
        <input type="checkbox" ${sel[m.key] ? 'checked' : ''} ${isGuest ? 'disabled' : ''}
               aria-label="${escapeHtml(m.name)}">
        <span><span class="n">${escapeHtml(m.name)}</span><span class="d">${escapeHtml(m.desc)}</span></span>
        ${isGuest ? '' : `<span class="c">~$${m.cost.toFixed(2)}</span>`}
      </label>`).join('');

    const note = provider === 'groq'
      ? 'Running on GROQ — fast and free, but with no live web search the politician-trade and news modules fall back to model knowledge.'
      : 'Running on PERPLEXITY sonar-pro with live web search. Costs are estimates and vary with response length.';

    // The dollar column is the owner's concern. Showing "$0.21" to a guest who
    // is not paying reads as a charge about to be made to them.
    const guestStrip = isGuest ? `
      <div class="gk-note">
        <span class="gk-c">GUEST KEY</span>
        <span class="gk-t">This runs your <b>single included deep dive</b> — the full
        ${MODULES.length}-stage pipeline, at no charge to you.<br>
        It permanently commits your key to <b>${escapeHtml(sym)}</b>; you cannot
        switch tickers afterwards. Quant, charts, history and news stay free and
        unlimited either way.</span>
      </div>` : '';

    const totalRow = isGuest
      ? `<div class="cf-tot"><span class="l">YOUR ALLOWANCE</span><span class="v">1 of 1 deep dive</span></div>`
      : `<div class="cf-tot"><span class="l">ESTIMATED TOTAL</span><span class="v" id="cf-total">$0.00</span></div>`;

    const { box, close } = modal(`
      <div class="mdl-hd"><span>ARGUS://CONFIRM · ${escapeHtml(sym)}</span><button data-close type="button">✕</button></div>
      <div class="mdl-b">
        ${guestStrip}
        <p class="hint" style="margin:0 0 12px">${isGuest
          ? 'Your included dive runs every stage. Quant, chart and P&amp;L are already loaded and always free.'
          : 'Each module is a separate paid call. Uncheck anything you do not need — quant, chart and P&amp;L are already loaded and always free.'}</p>
        ${rows}
        ${totalRow}
        <p class="hint" style="margin-top:12px">${escapeHtml(note)}</p>
      </div>
      <div class="mdl-f">
        <button class="btn-g" data-cancel type="button" style="padding:7px 14px">CANCEL</button>
        <button class="btn" data-run type="button">${isGuest ? 'USE MY DEEP DIVE ▸' : 'RUN RESEARCH ▸'}</button>
      </div>`);

    const totalEl = box.querySelector('#cf-total');
    const runBtn  = box.querySelector('[data-run]');
    const recalc = () => {
      if (!totalEl) return;                 // guest: no dollar column to update
      const t = MODULES.filter(m => sel[m.key]).reduce((s, m) => s + m.cost, 0);
      totalEl.textContent = `$${t.toFixed(2)}`;
      runBtn.disabled = t === 0;
    };
    recalc();

    if (!isGuest) {
      box.querySelectorAll('.cf').forEach(rowEl => {
        const cb = rowEl.querySelector('input');
        cb.addEventListener('change', () => {
          sel[rowEl.dataset.k] = cb.checked;
          rowEl.classList.toggle('on', cb.checked);
          recalc();
        });
      });
    }

    const finish = (v) => { close(); resolve(v); };
    box.querySelector('[data-cancel]').onclick = () => finish(null);
    box.querySelector('[data-close]').onclick  = () => finish(null);
    runBtn.onclick = () => {
      // A guest's forced selection is not a preference — never persist it over
      // the owner's own choices in this browser.
      if (!isGuest) {
        try { localStorage.setItem(MODULES_KEY, JSON.stringify(sel)); } catch { /* private mode */ }
      }
      finish(sel);
    };
    runBtn.focus();
  });
}

/* ── News ─────────────────────────────────────────────────────────────────
   Every value here was written by a third party. Titles and publisher names go
   in as text nodes, and an href is only built after re-checking the scheme —
   the server already drops non-http(s) links, and this is the second lock on
   the same door rather than a substitute for it. */

function relTime(v) {
  if (!v) return '';
  const t = Date.parse(v);
  if (!Number.isFinite(t)) return '';
  const mins = Math.round((Date.now() - t) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

export function renderNews(host, items) {
  if (!host) return;
  host.innerHTML = '';
  if (!items || !items.length) { host.remove(); return; }

  host.appendChild(el('div', 'r-head', 'NEWS · HEADLINES'));
  const list = el('div', 'news');

  for (const it of items.slice(0, 8)) {
    const row = el('div', 'news-i');
    const link = String(it.link || '');

    if (/^https?:\/\//i.test(link)) {
      const a = el('a', 'news-t');
      a.textContent = String(it.title || '');   // text node — never parsed as markup
      a.href = link;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      row.appendChild(a);
    } else {
      row.appendChild(el('span', 'news-t', String(it.title || '')));
    }

    const meta = [it.publisher, relTime(it.published_at)].filter(Boolean).join(' · ');
    if (meta) row.appendChild(el('span', 'news-m dim', meta));
    list.appendChild(row);
  }

  host.appendChild(list);
}

/* ── Owner: one-time key reveal ───────────────────────────────────────────
   Only the hash is stored, so this is the single moment the key exists in
   readable form. The mailto template deliberately does NOT carry the key —
   a URL ends up in history, logs and the OS handler chain. */

export function showKeyReveal({ email, key, expiresAt }) {
  const { box, close } = modal(`
    <div class="mdl-hd"><span>GUEST KEY · SHOWN ONCE</span><button data-close type="button">✕</button></div>
    <div class="mdl-b">
      <p class="hint" id="kr-for" style="margin:0 0 10px"></p>
      <input id="kr-val" class="inp key-reveal" readonly>
      <div style="display:flex;gap:8px">
        <button id="kr-copy" class="btn" type="button" style="flex:1">COPY KEY</button>
        <a id="kr-mail" class="btn-g" style="flex:1;text-align:center;padding:8px 12px;text-decoration:none">DRAFT EMAIL</a>
      </div>
      <p class="hint" id="kr-warn" style="margin-top:12px"></p>
    </div>
    <div class="mdl-f"><button id="kr-done" class="btn" type="button">DONE</button></div>`);

  // Untrusted: the address came from a public form.
  box.querySelector('#kr-for').textContent =
    `Issued to ${email}. Valid 24 hours from now${expiresAt ? ` (until ${expiresAt})` : ''}.`;
  box.querySelector('#kr-warn').textContent =
    'Copy it now — only a hash is stored, so it cannot be shown again. Deliver it '
    + 'yourself; this app sends no email. If you lose it, deny and re-approve the '
    + 'request to mint a new one.';

  const input = box.querySelector('#kr-val');
  input.value = key;                       // .value — immune to markup injection

  const mail = box.querySelector('#kr-mail');
  mail.href = 'mailto:' + encodeURIComponent(email)
    + '?subject=' + encodeURIComponent('Your ARGUS guest key')
    + '&body=' + encodeURIComponent(
        'Your ARGUS guest key is below, pasted separately.\n\n'
        + 'It is valid for 24 hours and includes one full AI deep dive on a single '
        + 'ticker. Everything else in the terminal is free and unlimited.\n\n'
        + 'Key: ');

  const copy = box.querySelector('#kr-copy');
  copy.onclick = async () => {
    /* Verify before claiming. navigator.clipboard is undefined on plain HTTP —
       which this app explicitly supports — and the execCommand fallback
       returns FALSE on failure rather than throwing, so the old catch never
       fired and the button went green regardless.

       This modal is the only moment the key exists in readable form; only its
       hash is stored. A false "COPIED ✓" means the operator pastes whatever
       was already on their clipboard, the request is already marked approved
       and gone from the pending list, and the person who asked never gets a
       key — with no signal to anyone. */
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(key);
        ok = true;
      }
    } catch { ok = false; }

    if (!ok) {
      input.select();
      try { ok = document.execCommand('copy'); } catch { ok = false; }
    }

    copy.textContent = ok ? 'COPIED ✓' : 'COPY FAILED — PRESS CTRL+C';
    copy.classList.toggle('btn-danger', !ok);
    if (!ok) input.select();     // leave it selected so Ctrl+C works
    setTimeout(() => {
      copy.textContent = 'COPY KEY';
      copy.classList.remove('btn-danger');
    }, ok ? 1500 : 6000);
  };

  // modal() wires only the FIRST [data-close], and this is the only dialog in
  // the file with two. DONE is the button an operator actually clicks after
  // copying the key, so an inert one reads as "the key was not issued".
  box.querySelector('#kr-done').onclick = () => close();

  input.focus();
  input.select();
}

/* A plain yes/no. confirmSpend() is for money and says so; this is for
   destructive-but-free actions like denying a request or revoking a key. */
export function confirmAction(title, detail, confirmLabel = 'CONFIRM') {
  return new Promise((resolve) => {
    const { box, close } = modal(`
      <div class="mdl-hd"><span>${escapeHtml(title)}</span><button data-close type="button">✕</button></div>
      <div class="mdl-b"><p class="hint" id="ca-d" style="margin:0;line-height:1.8"></p></div>
      <div class="mdl-f">
        <button class="btn-g" data-cancel type="button" style="padding:7px 14px">CANCEL</button>
        <button class="btn-danger" data-ok type="button">${escapeHtml(confirmLabel)}</button>
      </div>`);
    box.querySelector('#ca-d').textContent = detail;
    const done = (v) => { close(); resolve(v); };
    box.querySelector('[data-cancel]').onclick = () => done(false);
    box.querySelector('[data-close]').onclick  = () => done(false);
    box.querySelector('[data-ok]').onclick     = () => done(true);
    box.querySelector('[data-ok]').focus();
  });
}

export function showDataModal(health, info) {
  const c = (health && health.checks) || {};
  const line = (n, ok, src) =>
    `<span class="n">${escapeHtml(n)}</span><span class="s ${ok ? 'ok' : 'no'}">${ok ? '● LIVE' : '⚠ NOT SET'}</span><span class="src">${escapeHtml(src)}</span>`;

  modal(`
    <div class="mdl-hd"><span>DATA STATUS · ADAPTERS &amp; PROVENANCE</span><button data-close type="button">✕</button></div>
    <div class="mdl-b">
      <div class="dtab">
        <span class="h">ADAPTER</span><span class="h">STATUS</span><span class="h">SOURCE</span>
        ${line('brent_crude', c.brent_feed, 'BZ=F futures · yfinance')}
        ${line('equity_data', true, 'yfinance fundamentals')}
        ${line('quant_engine', true, 'local Python · deterministic')}
        ${line('perplexity_api', c.perplexity_key, 'sonar-pro · live web search')}
        ${line('groq_api', c.groq_key, 'llama-3.3-70b · no web search')}
      </div>
      <p class="hint" style="border-top:1px solid var(--border);margin-top:12px;padding-top:10px">
        Overall: <b>${escapeHtml(String(health?.status || 'unknown').toUpperCase())}</b> ·
        one AI provider is sufficient — both are listed so you can see which will serve a run.
        Provider keys live in <code>.env</code> on the server and are never sent to this browser.
      </p>
      <p class="hint">Tracking <b>${(info?.portfolio || []).length}</b> holdings ·
        <b>${Object.keys(info?.watchlist || {}).length}</b> watchlist ·
        <b>${Object.keys(info?.geo_transmission || {}).length}</b> geo vectors.</p>
    </div>`);
}

export function showConfigModal(authenticated, onAction, onPref) {
  const { box, close } = modal(`
    <div class="mdl-hd"><span>ARGUS://CONFIG</span><button data-close type="button">✕</button></div>
    <div class="mdl-b">
      <div class="fld">
        <div class="fld-l">Session</div>
        <p class="hint" style="margin-bottom:8px;line-height:1.8">
          ${authenticated
            ? 'Unlocked — paid research is armed. The credential is held in an <b>HttpOnly</b> cookie this page cannot read.'
            : 'Locked — quant, charts and P&amp;L still work. Unlock with your <b>AGENT_SECRET</b> to run paid research.'}
        </p>
        <button class="${authenticated ? 'btn-danger' : 'btn'}" data-session type="button"
                style="padding:8px 16px">${authenticated ? 'SIGN OUT' : 'UNLOCK ▸'}</button>
      </div>
      <div class="fld">
        <div class="fld-l">Accent</div>
        <div class="swatch" id="cfg-accent">
          <button type="button" data-v="amber"   style="background:#E8900A" aria-label="Amber"></button>
          <button type="button" data-v="cyan"    style="background:#22C7E8" aria-label="Cyan"></button>
          <button type="button" data-v="lime"    style="background:#7FE838" aria-label="Lime"></button>
          <button type="button" data-v="magenta" style="background:#E84AC0" aria-label="Magenta"></button>
        </div>
      </div>
      <div class="fld">
        <div class="fld-l">Tempo · typing &amp; tape speed</div>
        <div class="seg" id="cfg-tempo">
          <button type="button" data-v="calm">CALM</button>
          <button type="button" data-v="normal">NORMAL</button>
          <button type="button" data-v="volatile">VOLATILE</button>
        </div>
      </div>
      <div class="fld">
        <div class="fld-l">CRT scanlines</div>
        <div class="seg" id="cfg-crt">
          <button type="button" data-v="off">OFF</button>
          <button type="button" data-v="on">ON</button>
        </div>
      </div>
    </div>
    <div class="mdl-f"><button class="btn-g" data-cancel type="button" style="padding:7px 14px">CLOSE</button></div>`);

  const wire = (id, key) => {
    const host = box.querySelector(id);
    const paint = () => host.querySelectorAll('button')
      .forEach(b => b.classList.toggle('on', b.dataset.v === prefs.get(key)));
    host.querySelectorAll('button').forEach(b => {
      b.onclick = () => { prefs.set(key, b.dataset.v); paint(); onPref && onPref(key, b.dataset.v); };
    });
    paint();
  };
  wire('#cfg-accent', 'accent');
  wire('#cfg-tempo', 'tempo');
  wire('#cfg-crt', 'crt');

  box.querySelector('[data-cancel]').onclick = close;
  box.querySelector('[data-session]').onclick = () => {
    close();
    onAction && onAction(authenticated ? 'signout' : 'unlock');
  };
}

/* Generic spend confirmation, for paid actions outside the research pipeline. */
export function confirmSpend(title, detail, cost) {
  return new Promise((resolve) => {
    const { box, close } = modal(`
      <div class="mdl-hd"><span>ARGUS://CONFIRM</span><button data-close type="button">✕</button></div>
      <div class="mdl-b">
        <div style="font-size:12px;color:var(--white);font-weight:700;letter-spacing:.06em">${escapeHtml(title)}</div>
        <p class="hint" style="margin-top:8px;line-height:1.8">${escapeHtml(detail)}</p>
        <div class="cf-tot"><span class="l">ESTIMATED COST</span><span class="v">~$${cost.toFixed(2)}</span></div>
      </div>
      <div class="mdl-f">
        <button class="btn-g" data-cancel type="button" style="padding:7px 14px">CANCEL</button>
        <button class="btn" data-run type="button">RUN ▸</button>
      </div>`, { wide: false });
    const done = (v) => { close(); resolve(v); };
    box.querySelector('[data-cancel]').onclick = () => done(false);
    box.querySelector('[data-close]').onclick  = () => done(false);
    box.querySelector('[data-run]').onclick    = () => done(true);
    box.querySelector('[data-run]').focus();
  });
}

/* Small ticker prompt for the watchlist + button. */
export function promptTicker() {
  return new Promise((resolve) => {
    const { box, close } = modal(`
      <div class="mdl-hd"><span>ADD INSTRUMENT</span><button data-close type="button">✕</button></div>
      <div class="mdl-b">
        <input id="pt-in" class="inp" placeholder="TICKER e.g. TSLA" autocomplete="off"
               spellcheck="false" style="text-transform:uppercase"/>
        <p class="hint" style="margin-top:8px">Indian listings need <code>.NS</code> (NSE) or <code>.BO</code> (BSE).</p>
      </div>
      <div class="mdl-f">
        <button class="btn-g" data-cancel type="button" style="padding:7px 14px">CANCEL</button>
        <button class="btn" data-go type="button">ADD ▸</button>
      </div>`, { wide: false });
    const input = box.querySelector('#pt-in');
    const done = (v) => { close(); resolve(v); };
    box.querySelector('[data-cancel]').onclick = () => done(null);
    box.querySelector('[data-close]').onclick  = () => done(null);
    box.querySelector('[data-go]').onclick     = () => done((input.value || '').trim().toUpperCase() || null);
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') done((input.value || '').trim().toUpperCase() || null);
    });
    input.focus();
  });
}

/* ── Tooltips ────────────────────────────────────────────────────────── */

export function initTooltips() {
  const tip = el('div', 'tip-pop');
  tip.hidden = true;
  document.body.appendChild(tip);

  const show = (btn) => {
    tip.textContent = btn.dataset.tip;
    tip.hidden = false;
    const b = btn.getBoundingClientRect();
    const w = Math.min(300, window.innerWidth - 24);
    tip.style.width = `${w}px`;
    let left = b.left + b.width / 2 - w / 2;
    left = Math.max(10, Math.min(left, window.innerWidth - w - 10));
    tip.style.left = `${left}px`;
    const below = b.bottom + 8;
    const fits = below + tip.offsetHeight < window.innerHeight - 10;
    tip.style.top = `${(fits ? below : b.top - tip.offsetHeight - 8) + window.scrollY}px`;
  };
  const hide = () => { tip.hidden = true; };

  document.addEventListener('pointerover', (e) => {
    const btn = e.target.closest('[data-tip]');
    if (btn) show(btn); else if (!e.target.closest('.tip-pop')) hide();
  });
  document.addEventListener('focusin', (e) => {
    const btn = e.target.closest('[data-tip]');
    if (btn) show(btn);
  });
  document.addEventListener('focusout', hide);
  window.addEventListener('scroll', hide, { passive: true });
}

export { riskState };
