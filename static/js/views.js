'use strict';

/* views.js — the screens that are not the research pane: unlock, onboarding,
   portfolio overview, profile. Kept out of ui.js so that file stays about the
   terminal chrome. */

import * as api from './api.js?v=15';
import { renderMarkdown, escapeHtml } from './md.js?v=15';
import * as ui from './ui.js?v=15';
import * as prefs from './theme.js?v=15';

const $ = ui.$;
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
};

/* ── Unlock ──────────────────────────────────────────────────────────────
   Takes AGENT_SECRET, not a decorative PIN. On success the server sets an
   HttpOnly cookie; nothing sensitive is stored client-side. */

export function showUnlock(onDone) {
  const host = $('gate');
  host.className = 'gate on';
  host.innerHTML = `
    <div class="gate-box">
      <pre class="boot-art" aria-hidden="true"> █████╗ ██████╗  ██████╗ ██╗   ██╗███████╗
██╔══██╗██╔══██╗██╔════╝ ██║   ██║██╔════╝
███████║██████╔╝██║  ███╗██║   ██║███████╗
██╔══██║██╔══██╗██║   ██║██║   ██║╚════██║
██║  ██║██║  ██║╚██████╔╝╚██████╔╝███████║
╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚══════╝</pre>
      <div class="gate-hd">ARGUS://LOCKED</div>
      <div class="fld">
        <div class="fld-l">Agent key</div>
        <input id="gate-key" class="inp" type="password" autocomplete="current-password"
               spellcheck="false" placeholder="AGENT_SECRET from your server .env"/>
        <div id="gate-err" class="gate-err"></div>
      </div>
      <button id="gate-go" class="btn" type="button" style="width:100%;padding:10px">UNLOCK ▸</button>
      <p class="hint" style="margin-top:14px;line-height:1.8">
        This is the <b>AGENT_SECRET</b> you set in <code>.env</code> — not a provider key.
        It is exchanged for a 30-day session cookie your browser stores but page
        scripts cannot read. Quant, charts and P&amp;L work without it; only paid
        research is gated.</p>
    </div>`;

  const input = $('gate-key');
  const err = $('gate-err');
  const box = host.querySelector('.gate-box');
  let busy = false;

  const go = async () => {
    if (busy) return;
    const v = input.value.trim();
    if (!v) { input.focus(); return; }
    busy = true;
    err.textContent = '';
    const res = await api.auth.unlock(v);
    busy = false;
    if (res.ok) { host.className = 'gate'; host.innerHTML = ''; onDone && onDone(); return; }
    // Still deliberately generic about a WRONG key — never hint at how close
    // the attempt was. But a lockout or an unreachable server is not a wrong
    // key, and saying "Rejected." for those sends the user to retry, which on
    // a lockout makes it worse. Distinguish the cause, not the closeness.
    if (res.status === 429) {
      err.textContent = 'Too many attempts. Locked for ~15 min — the key may still be right.';
    } else if (res.status === 0) {
      err.textContent = 'Cannot reach the server. Is uvicorn still running?';
    } else {
      err.textContent = 'Rejected.';
    }
    box.classList.remove('shake'); void box.offsetWidth; box.classList.add('shake');
    input.select();
  };

  $('gate-go').onclick = go;
  input.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
  input.focus();
}

/* ── Onboarding ──────────────────────────────────────────────────────────
   Four steps. Step 3 is the one that matters: buy dates are what make XIRR a
   real number rather than a fabricated one. */

const EXPERIENCE = [['new', 'NEW'], ['intermediate', 'INTERMEDIATE'], ['professional', 'PROFESSIONAL']];
const RISK       = [['conservative', 'CONSERVATIVE'], ['balanced', 'BALANCED'], ['aggressive', 'AGGRESSIVE']];
const HORIZON    = [['under1y', '< 1 YEAR'], ['1to3y', '1–3 YEARS'], ['3to5y', '3–5 YEARS'], ['over5y', '5+ YEARS']];
const SECTORS    = ['AI / Defence', 'Enterprise Software', 'Semiconductors', 'Fintech',
                    'E-Commerce', 'Industrials', 'Energy', 'Healthcare', 'India / EM'];

export function showOnboarding(positions, onDone) {
  const host = $('gate');
  const state = { name: '', experience: 'intermediate', risk: 'balanced', horizon: '1to3y', sectors: [], dates: {} };
  const tickers = Object.keys(positions || {});
  let step = 0;

  const seg = (id, opts, cur) =>
    `<div class="seg" id="${id}">` +
    opts.map(([v, l]) => `<button type="button" data-v="${v}"${v === cur ? ' class="on"' : ''}>${l}</button>`).join('') +
    `</div>`;

  const steps = [
    () => `
      <div class="ob-h">WHO IS THE ANALYST?</div>
      <p class="hint" style="margin-bottom:14px">Used to address you in the terminal. Nothing leaves this machine.</p>
      <div class="fld"><div class="fld-l">Name</div>
        <input id="ob-name" class="inp" autocomplete="off" placeholder="e.g. Ravi" value="${escapeHtml(state.name)}"/></div>`,

    () => `
      <div class="ob-h">HOW DO YOU INVEST?</div>
      <p class="hint" style="margin-bottom:14px">These change the dashboard defaults — chart range, which panels open,
        and which research modules are pre-selected when you spend.</p>
      <div class="fld"><div class="fld-l">Experience</div>${seg('ob-exp', EXPERIENCE, state.experience)}</div>
      <div class="fld"><div class="fld-l">Risk appetite</div>${seg('ob-risk', RISK, state.risk)}</div>
      <div class="fld"><div class="fld-l">Typical holding period</div>${seg('ob-hor', HORIZON, state.horizon)}</div>
      <div class="fld"><div class="fld-l">Sectors you follow</div>
        <div class="chips-w" id="ob-sec">${SECTORS.map(s =>
          `<button type="button" class="chip-t${state.sectors.includes(s) ? ' on' : ''}" data-s="${escapeHtml(s)}">${escapeHtml(s)}</button>`).join('')}</div>
      </div>`,

    () => `
      <div class="ob-h">WHEN DID YOU BUY?</div>
      <p class="hint" style="margin-bottom:14px">
        XIRR — your money-weighted return — needs the date of each purchase.
        <b>An approximate date is fine.</b> Skip any row and that holding is simply
        left out of the XIRR calculation rather than guessed at.</p>
      <div class="ob-rows">
        ${tickers.map(t => {
          const p = positions[t] || {};
          return `<div class="ob-row">
            <span class="ob-t">${escapeHtml(t)}</span>
            <span class="ob-m dim">${p.shares ?? '—'} sh @ ${p.avg_cost ?? '—'}</span>
            <input class="inp ob-d" data-t="${escapeHtml(t)}" type="date" max="${new Date().toISOString().slice(0, 10)}"
                   value="${escapeHtml(state.dates[t] || p.buy_date || '')}"/>
          </div>`;
        }).join('')}
      </div>`,

    () => `
      <div class="ob-h">READY</div>
      <p class="hint" style="line-height:1.9">
        Terminal tailored for <b>${escapeHtml(state.name || 'analyst')}</b> ·
        ${escapeHtml((HORIZON.find(h => h[0] === state.horizon) || [])[1] || '')} horizon ·
        ${escapeHtml((RISK.find(r => r[0] === state.risk) || [])[1] || '')} risk.<br><br>
        Buy dates recorded for <b>${Object.values(state.dates).filter(Boolean).length}</b>
        of ${tickers.length} holdings.<br><br>
        You can change all of this later under <code>profile</code>.</p>`,
  ];

  const render = () => {
    host.className = 'gate on';
    host.innerHTML = `
      <div class="gate-box ob">
        <div class="ob-steps">${steps.map((_, i) =>
          `<span class="ob-dot${i === step ? ' on' : ''}${i < step ? ' done' : ''}"></span>`).join('')}</div>
        <div id="ob-body">${steps[step]()}</div>
        <div class="ob-nav">
          ${step > 0 ? '<button id="ob-back" class="btn-g" type="button" style="padding:8px 16px">◂ BACK</button>' : '<span></span>'}
          <button id="ob-next" class="btn" type="button">${step === steps.length - 1 ? 'ENTER TERMINAL ▸' : 'NEXT ▸'}</button>
        </div>
      </div>`;
    wire();
  };

  const wire = () => {
    const nameIn = $('ob-name');
    if (nameIn) {
      nameIn.oninput = () => { state.name = nameIn.value; };
      nameIn.focus();
      nameIn.addEventListener('keydown', e => { if (e.key === 'Enter') next(); });
    }
    for (const [id, key] of [['ob-exp', 'experience'], ['ob-risk', 'risk'], ['ob-hor', 'horizon']]) {
      const g = $(id);
      if (!g) continue;
      g.querySelectorAll('button').forEach(b => {
        b.onclick = () => {
          state[key] = b.dataset.v;
          g.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
        };
      });
    }
    const sec = $('ob-sec');
    if (sec) sec.querySelectorAll('.chip-t').forEach(b => {
      b.onclick = () => {
        const s = b.dataset.s;
        const i = state.sectors.indexOf(s);
        if (i >= 0) state.sectors.splice(i, 1); else state.sectors.push(s);
        b.classList.toggle('on', i < 0);
      };
    });
    host.querySelectorAll('.ob-d').forEach(inp => {
      inp.onchange = () => { state.dates[inp.dataset.t] = inp.value; };
    });
    const back = $('ob-back');
    if (back) back.onclick = () => { step = Math.max(0, step - 1); render(); };
    $('ob-next').onclick = next;
  };

  const next = async () => {
    if (step < steps.length - 1) { step++; render(); return; }
    const btn = $('ob-next');
    btn.disabled = true;
    btn.textContent = 'SAVING…';
    try {
      await api.saveProfile({
        name: state.name, experience: state.experience,
        risk: state.risk, horizon: state.horizon, sectors: state.sectors,
      });
      for (const [t, d] of Object.entries(state.dates)) {
        if (d) await api.savePosition({ ticker: t, buy_date: d });
      }
    } catch (err) {
      ui.toast(`Could not save profile: ${err.message}`, 'error');
      btn.disabled = false;
      btn.textContent = 'ENTER TERMINAL ▸';
      return;
    }
    host.className = 'gate';
    host.innerHTML = '';
    onDone && onDone();
  };

  render();
}

/* ── Portfolio view ──────────────────────────────────────────────────────
   Free math renders immediately; the paid analyst outlook is a separate,
   explicit action. */

const money = (v, c) => ui.money(v, c);

export async function renderPortfolio(host, { onOutlook } = {}) {
  host.innerHTML = '';
  const title = el('div', 'r-title', 'ARGUS://PORTFOLIO');
  host.appendChild(title);
  const loading = el('div', 'dim', 'Computing…');
  loading.style.marginTop = '10px';
  host.appendChild(loading);

  let d;
  try { d = await api.getAnalytics(); }
  catch (err) { loading.textContent = err.message; return; }
  loading.remove();

  if (!d.totals) {
    host.appendChild(el('div', 'dim', d.xirr_note || 'No positions configured.'));
    return;
  }

  // Totals strip
  const t = d.totals;
  const strip = el('div', 'pf-strip');
  const cell = (label, value, cls, tip) => {
    const c = el('div', 'pf-cell');
    c.appendChild(el('div', 'pf-cl', label));
    const v = el('div', `pf-cv ${cls || ''}`, value);
    if (tip) v.dataset.tip = tip;
    c.appendChild(v);
    return c;
  };
  const tone = (n) => (n === null || n === undefined ? '' : n >= 0 ? 'pos' : 'neg');
  strip.append(
    cell('INVESTED', money(t.invested)),
    cell('MARKET VALUE', money(t.market_value)),
    cell('UNREALISED P&L',
      t.unrealized_pnl === null ? '—'
        : `${t.unrealized_pnl >= 0 ? '+' : '−'}${money(Math.abs(t.unrealized_pnl))}`,
      tone(t.unrealized_pnl)),
    cell('RETURN', ui.pct(t.unrealized_pnl_pct), tone(t.unrealized_pnl_pct)),
    cell('XIRR',
      d.xirr === null ? 'N/A' : ui.pct(d.xirr),
      d.xirr === null ? 'na' : tone(d.xirr),
      'Money-weighted annualised return. Unlike total return it accounts for when each purchase was made, so a recent buy does not distort it.'),
  );
  host.appendChild(strip);

  if (d.xirr_note) {
    const n = el('div', 'pf-note', d.xirr_note);
    host.appendChild(n);
  }

  // Holdings with weight bars
  host.appendChild(el('div', 'r-head', 'HOLDINGS · WEIGHT & CONTRIBUTION'));
  const table = el('div', 'pf-tab');
  const live = (d.positions || []).filter(p => !p.error);
  const maxW = Math.max(1, ...live.map(p => p.weight_pct || 0));

  for (const p of live.sort((a, b) => (b.weight_pct || 0) - (a.weight_pct || 0))) {
    const row = el('div', 'pf-row');
    row.tabIndex = 0;
    row.setAttribute('role', 'button');
    row.title = `${p.ticker} — open research`;

    const idc = el('div', 'pf-id');
    idc.appendChild(el('span', 'pf-t', p.ticker));
    idc.appendChild(el('span', 'pf-s dim', p.sector || ''));
    row.appendChild(idc);

    const bar = el('div', 'pf-bar');
    const fill = el('i');
    fill.style.width = `${((p.weight_pct || 0) / maxW) * 100}%`;
    bar.appendChild(fill);
    bar.appendChild(el('span', 'pf-w', `${(p.weight_pct ?? 0).toFixed(1)}%`));
    row.appendChild(bar);

    row.appendChild(el('div', 'pf-v tabnum', money(p.market_value, p.currency)));
    const pl = el('div', `pf-p tabnum ${tone(p.pnl)}`, ui.pct(p.pnl_pct));
    pl.title = `${p.shares} sh @ ${money(p.avg_cost, p.currency)}`
             + (p.buy_date ? ` · bought ${p.buy_date}` : ' · no buy date');
    row.appendChild(pl);

    row.onclick = () => window.dispatchEvent(new CustomEvent('argus:research', { detail: p.ticker }));
    row.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); row.click(); }
    };
    table.appendChild(row);
  }
  host.appendChild(table);

  const dead = (d.positions || []).filter(p => p.error);
  if (dead.length) {
    host.appendChild(el('div', 'pf-note', `No live price for ${dead.map(p => p.ticker).join(', ')}.`));
  }

  // Outlook
  host.appendChild(el('div', 'r-head', '3–5 YEAR OUTLOOK · ANALYST CONSENSUS'));
  const box = el('div');
  box.id = 'outlook-box';
  host.appendChild(box);
  await paintOutlook(box, onOutlook);
}

export async function paintOutlook(box, onOutlook) {
  box.innerHTML = '';
  let cached = null;
  try { cached = (await api.getOutlook()).outlook; } catch { /* free call; ignore */ }

  if (!cached) {
    const empty = el('div', 'ol-empty');
    empty.innerHTML =
      `<p class="hint" style="margin-bottom:12px;line-height:1.8">
         Searches live coverage for the top three institutional analysts on each
         holding — firm, rating, price target and date — then builds a
         position-weighted bear / base / bull band.<br>
         One provider call covering every holding.</p>`;
    const btn = el('button', 'btn', 'RUN OUTLOOK  ~$0.06');
    btn.type = 'button';
    btn.onclick = () => onOutlook && onOutlook(box);
    empty.appendChild(btn);
    box.appendChild(empty);
    return;
  }

  const head = el('div', 'ol-head');
  const age = cached.age_days === 0 ? 'today'
            : cached.age_days === 1 ? 'yesterday'
            : cached.age_days != null ? `${cached.age_days} days ago` : '';
  head.appendChild(el('span', 'dim', `Generated ${age} · ${cached.provider === 'groq' ? 'GROQ · no live web' : 'Perplexity · live web'}`));
  const refresh = el('button', 'btn-g', '↻ REFRESH  ~$0.06');
  refresh.type = 'button';
  refresh.onclick = () => onOutlook && onOutlook(box);
  head.appendChild(refresh);
  box.appendChild(head);

  const text = cached.outlook || '';
  const m = text.match(/OUTLOOK:\s*\**\s*BASE\s*(-?[\d.]+)%?\s*\|\s*\**\s*BEAR\s*(-?[\d.]+)%?\s*\|\s*\**\s*BULL\s*(-?[\d.]+)%?\s*\|\s*\**\s*HORIZON\s*(\d+)\s*Y/i);
  if (m) {
    const band = el('div', 'ol-band');
    const b = (label, val, cls) => {
      const c = el('div', `ol-b ${cls}`);
      c.appendChild(el('div', 'ol-bl', label));
      c.appendChild(el('div', 'ol-bv', `${Number(val) >= 0 ? '+' : ''}${val}%`));
      c.appendChild(el('div', 'ol-bs dim', 'p.a.'));
      return c;
    };
    band.append(b('BEAR', m[2], 'neg'), b('BASE', m[1], 'base'), b('BULL', m[3], 'pos'));
    const hz = el('div', 'ol-hz dim', `over ${m[4]} years`);
    box.append(band, hz);
  }

  const body = el('div', 'r-body');
  body.innerHTML = renderMarkdown(m ? text.slice(m.index + m[0].length).trim() : text);
  box.appendChild(body);

  const disc = el('div', 'ol-disc');
  disc.textContent =
    'Third-party analyst opinion, reported with attribution — not a recommendation '
    + 'from this application and not a forecast of actual returns. Verify against primary sources before acting.';
  box.appendChild(disc);
}

/* ── Profile view ────────────────────────────────────────────────────────── */

export async function renderProfile(host, { onChanged } = {}) {
  host.innerHTML = '';
  host.appendChild(el('div', 'r-title', 'ARGUS://PROFILE'));

  let profile = null, positions = {}, watch = {};
  try {
    profile   = (await api.getProfile()).profile;
    positions = (await api.getPositions()).positions;
    watch     = (await api.getWatchlist()).watchlist;
  } catch (err) { host.appendChild(el('div', 'dim', err.message)); return; }

  // Tailoring
  host.appendChild(el('div', 'r-head', 'TAILORING'));
  const seg = (id, opts, cur) =>
    `<div class="seg" id="${id}">` +
    opts.map(([v, l]) => `<button type="button" data-v="${v}"${v === cur ? ' class="on"' : ''}>${l}</button>`).join('') +
    `</div>`;
  const tail = el('div');
  tail.innerHTML = `
    <div class="fld"><div class="fld-l">Name</div>
      <input id="pf-name" class="inp" value="${escapeHtml(profile?.name || '')}"/></div>
    <div class="fld"><div class="fld-l">Experience</div>${seg('pf-exp', EXPERIENCE, profile?.experience)}</div>
    <div class="fld"><div class="fld-l">Risk appetite</div>${seg('pf-risk', RISK, profile?.risk)}</div>
    <div class="fld"><div class="fld-l">Holding period</div>${seg('pf-hor', HORIZON, profile?.horizon)}</div>
    <div class="fld"><div class="fld-l">Sectors</div>
      <div class="chips-w" id="pf-sec">${SECTORS.map(s =>
        `<button type="button" class="chip-t${(profile?.sectors || []).includes(s) ? ' on' : ''}" data-s="${escapeHtml(s)}">${escapeHtml(s)}</button>`).join('')}</div></div>`;
  host.appendChild(tail);

  const sel = { experience: profile?.experience || 'intermediate',
                risk: profile?.risk || 'balanced',
                horizon: profile?.horizon || '1to3y',
                sectors: [...(profile?.sectors || [])] };

  for (const [id, key] of [['pf-exp', 'experience'], ['pf-risk', 'risk'], ['pf-hor', 'horizon']]) {
    const g = $(id);
    g.querySelectorAll('button').forEach(b => {
      b.onclick = () => { sel[key] = b.dataset.v; g.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); };
    });
  }
  $('pf-sec').querySelectorAll('.chip-t').forEach(b => {
    b.onclick = () => {
      const s = b.dataset.s, i = sel.sectors.indexOf(s);
      if (i >= 0) sel.sectors.splice(i, 1); else sel.sectors.push(s);
      b.classList.toggle('on', i < 0);
    };
  });

  const save = el('button', 'btn', 'SAVE TAILORING');
  save.type = 'button';
  save.onclick = async () => {
    try {
      await api.saveProfile({ name: $('pf-name').value, ...sel });
      ui.toast('Tailoring saved.', 'success', 2600);
      onChanged && onChanged();
    } catch (err) { ui.toast(err.message, 'error'); }
  };
  host.appendChild(save);

  // Positions
  host.appendChild(el('div', 'r-head', 'POSITIONS'));
  const ptab = el('div', 'ed-tab');
  ptab.appendChild(rowHead(['TICKER', 'SHARES', 'AVG COST', 'BUY DATE', 'STOP', 'TRIM', '']));
  for (const [t, p] of Object.entries(positions)) {
    ptab.appendChild(positionRow(t, p, onChanged));
  }
  host.appendChild(ptab);

  const addP = el('button', 'btn-g', '+ ADD POSITION');
  addP.type = 'button';
  addP.style.marginTop = '8px';
  addP.onclick = () => {
    const blank = el('div');
    ptab.appendChild(positionRow('', {}, onChanged, true));
  };
  host.appendChild(addP);

  // Watchlist
  host.appendChild(el('div', 'r-head', 'WATCHLIST'));
  const wtab = el('div', 'ed-tab');
  for (const t of Object.keys(watch)) {
    const r = el('div', 'ed-row wl');
    r.appendChild(el('span', 'ed-t', t));
    r.appendChild(el('span', 'dim', (watch[t].thesis || '').slice(0, 90)));
    const del = el('button', 'btn-danger', '✕');
    del.type = 'button';
    del.title = `Remove ${t}`;
    del.onclick = async () => {
      try { await api.delWatch(t); r.remove(); ui.toast(`${t} removed.`, 'success', 2200); onChanged && onChanged(); }
      catch (err) { ui.toast(err.message, 'error'); }
    };
    r.appendChild(del);
    wtab.appendChild(r);
  }
  host.appendChild(wtab);

  // Session + appearance
  host.appendChild(el('div', 'r-head', 'SESSION & APPEARANCE'));
  const app = el('div');
  app.innerHTML = `
    <div class="fld"><div class="fld-l">Accent</div>
      <div class="swatch" id="pf-accent">
        <button type="button" data-v="amber"   style="background:#E8900A" aria-label="Amber"></button>
        <button type="button" data-v="cyan"    style="background:#22C7E8" aria-label="Cyan"></button>
        <button type="button" data-v="lime"    style="background:#7FE838" aria-label="Lime"></button>
        <button type="button" data-v="magenta" style="background:#E84AC0" aria-label="Magenta"></button>
      </div></div>
    <div class="fld"><div class="fld-l">Tempo</div>
      <div class="seg" id="pf-tempo">
        <button type="button" data-v="calm">CALM</button>
        <button type="button" data-v="normal">NORMAL</button>
        <button type="button" data-v="volatile">VOLATILE</button></div></div>
    <div class="fld"><div class="fld-l">CRT scanlines</div>
      <div class="seg" id="pf-crt">
        <button type="button" data-v="off">OFF</button>
        <button type="button" data-v="on">ON</button></div></div>`;
  host.appendChild(app);

  for (const [id, key] of [['pf-accent', 'accent'], ['pf-tempo', 'tempo'], ['pf-crt', 'crt']]) {
    const g = $(id);
    const paint = () => g.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === prefs.get(key)));
    g.querySelectorAll('button').forEach(b => { b.onclick = () => { prefs.set(key, b.dataset.v); paint(); }; });
    paint();
  }

  const out = el('button', 'btn-danger', 'SIGN OUT');
  out.type = 'button';
  out.style.marginTop = '10px';
  out.onclick = async () => {
    await api.auth.signOut();
    ui.renderKeyState(false);
    ui.toast('Signed out. Paid research is locked until you unlock again.', 'info', 4000);
    onChanged && onChanged();
  };
  host.appendChild(out);
}

function rowHead(cols) {
  const r = el('div', 'ed-row head');
  for (const c of cols) r.appendChild(el('span', null, c));
  return r;
}

function positionRow(ticker, p, onChanged, isNew = false) {
  const r = el('div', 'ed-row');
  const f = (val, type, ph, w) => {
    const i = el('input', 'inp ed-i');
    i.type = type; i.value = val ?? ''; i.placeholder = ph || '';
    if (type === 'date') i.max = new Date().toISOString().slice(0, 10);
    return i;
  };
  const tIn = f(ticker, 'text', 'TICKER');
  tIn.style.textTransform = 'uppercase';
  tIn.disabled = !isNew;
  const sh   = f(p.shares, 'number', 'shares');
  const cost = f(p.avg_cost, 'number', 'avg cost');
  const date = f(p.buy_date, 'date');
  const stop = f(p.stop_loss, 'number', 'stop');
  const trim = f(p.trim_at, 'number', 'trim');
  r.append(tIn, sh, cost, date, stop, trim);

  const actions = el('div', 'ed-a');
  const save = el('button', 'btn-g', '✓');
  save.type = 'button'; save.title = 'Save';
  save.onclick = async () => {
    const t = (tIn.value || '').trim().toUpperCase();
    if (!t) { ui.toast('Ticker required.', 'warn'); return; }
    const body = { ticker: t };
    if (sh.value !== '')   body.shares    = Number(sh.value);
    if (cost.value !== '') body.avg_cost  = Number(cost.value);
    if (date.value !== '') body.buy_date  = date.value;
    if (stop.value !== '') body.stop_loss = Number(stop.value);
    if (trim.value !== '') body.trim_at   = Number(trim.value);
    try {
      await api.savePosition(body);
      tIn.disabled = true;
      ui.toast(`${t} saved.`, 'success', 2200);
      onChanged && onChanged();
    } catch (err) { ui.toast(err.message, 'error'); }
  };
  const del = el('button', 'btn-danger', '✕');
  del.type = 'button'; del.title = 'Delete';
  del.onclick = async () => {
    const t = (tIn.value || '').trim().toUpperCase();
    if (!t) { r.remove(); return; }
    try { await api.delPosition(t); r.remove(); ui.toast(`${t} removed.`, 'success', 2200); onChanged && onChanged(); }
    catch (err) { ui.toast(err.message, 'error'); }
  };
  actions.append(save, del);
  r.appendChild(actions);
  return r;
}
