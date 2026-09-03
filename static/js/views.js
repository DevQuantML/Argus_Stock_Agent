'use strict';

/* views.js — the screens that are not the research pane: unlock, onboarding,
   portfolio overview, profile. Kept out of ui.js so that file stays about the
   terminal chrome. */

import * as api from './api.js?v=35';
import { renderMarkdown, escapeHtml } from './md.js?v=35';
import * as ui from './ui.js?v=35';
import * as prefs from './theme.js?v=35';
import { DISCLAIMER, PRIVACY_NOTE, FRESHNESS_NOTE, guestAllowanceLine } from './copy.js?v=35';

const $ = ui.$;
const el = ui.el;

/* ── Landing ─────────────────────────────────────────────────────────────
   The front door. It used to be a bare password box for a secret that only the
   operator could possibly hold, with no way past it — a visitor arriving from a
   link saw a locked door and nothing else, under hint text promising that
   "quant, charts and P&L work without it" which had stopped being true when
   those routes gained require_session.

   Three ways in now: browse free, ask for a key, or use one.

   The panes live in one gate box and swap by mode, the same render()/wire()
   shape showOnboarding() uses. */

const ART = ` █████╗ ██████╗  ██████╗ ██╗   ██╗███████╗
██╔══██╗██╔══██╗██╔════╝ ██║   ██║██╔════╝
███████║██████╔╝██║  ███╗██║   ██║███████╗
██╔══██║██╔══██╗██║   ██║██║   ██║╚════██║
██║  ██║██║  ██║╚██████╔╝╚██████╔╝███████║
╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚══════╝`;

const LAST_TIER_KEY = 'argus.lastTier';

/* ── BYOK provider walkthrough ──────────────────────────────────────────
   A guided coach-mark over the three provider buttons, one at a time —
   same interaction pattern as tour.js's #tour (spotlight, tooltip, dots,
   Next/Back/Esc), reimplemented rather than imported: tour.js's own
   startTour() deliberately refuses to run while the landing gate is open
   (its docstring: never launch on top of a decision the reader is mid-way
   through), and this walkthrough IS a screen inside that gate — the two
   are mutually exclusive by construction, so there is nothing to share
   beyond the visual language, which lives in matching CSS classes instead
   (#byok-tour mirrors #tour at a higher z-index; see style.css). */
const BYOK_TOUR_STEPS = [
  { target: null,
    title: 'PICK A PROVIDER',
    body: 'Three ways to fund research here — free and instant, free with a '
        + 'quota, or paid with the best quality. Step through each, or press '
        + 'Esc to go straight back to pasting a key.' },
  { target: '[data-v="groq"]',
    title: 'GROQ — FREE, FASTEST',
    body: "No live web search — answers come from the model's training data, "
        + 'which can be months stale. Best for trying the app at zero cost, '
        + 'not a real decision. Get one (~1 min): console.groq.com/keys → '
        + 'sign up → Create API Key (starts gsk_).' },
  { target: '[data-v="gemini"]',
    title: 'GEMINI — FREE QUOTA, LIVE SEARCH',
    body: 'Live web search via Google, free within ~5,000 searches/month, '
        + 'then paid. Answers cite the pages they used, though coverage is '
        + 'usually thinner than Perplexity\'s. The best free option if '
        + 'freshness matters. Get one: aistudio.google.com/apikey → sign in '
        + 'with Google → Create API Key (starts AIza).' },
  { target: '[data-v="perplexity"]',
    title: 'PERPLEXITY — RECOMMENDED',
    body: 'Paid, ~$0.04–0.21 per run — live web search with full citations. '
        + 'Best when result quality matters most. Get one: '
        + 'perplexity.ai/settings/api → sign up, add a payment method '
        + '→ generate a key (starts pplx-).' },
];

function startByokTour(seg) {
  if (document.getElementById('byok-tour')) return; // already open — ignore a double-click

  const RM = window.matchMedia('(prefers-reduced-motion:reduce)').matches;
  let step = 0;

  const root = document.createElement('div');
  root.id = 'byok-tour';
  if (RM) root.classList.add('rm');

  const spot = document.createElement('div');
  spot.id = 'byok-tour-spot';

  const tip = document.createElement('div');
  tip.id = 'byok-tour-tip';
  tip.setAttribute('role', 'dialog');
  tip.setAttribute('aria-modal', 'true');
  tip.setAttribute('aria-labelledby', 'byok-tour-title');

  const dots = document.createElement('div');
  dots.className = 'ob-steps';

  const title = document.createElement('div');
  title.className = 'gate-hd';
  title.id = 'byok-tour-title';

  const body = document.createElement('p');
  body.id = 'byok-tour-body';

  const nav = document.createElement('div');
  nav.className = 'tour-nav';
  const skip = document.createElement('button');
  skip.className = 'btn-g'; skip.type = 'button';
  skip.textContent = 'SKIP (ESC)';
  const next = document.createElement('button');
  next.className = 'btn'; next.type = 'button';
  nav.append(skip, next);

  tip.append(dots, title, body, nav);
  root.append(spot, tip);
  document.body.appendChild(root);

  const resolveTarget = (sel) => (sel && seg) ? seg.querySelector(sel) : null;

  const position = (node) => {
    if (!node) {
      spot.style.cssText = `top:${innerHeight / 2}px;left:${innerWidth / 2}px;width:0;height:0`;
      tip.style.top = `${Math.max(12, innerHeight / 2 - tip.offsetHeight / 2)}px`;
      tip.style.left = `${Math.max(10, innerWidth / 2 - tip.offsetWidth / 2)}px`;
      return;
    }
    const r = node.getBoundingClientRect();
    spot.style.cssText = `top:${r.top - 4}px;left:${r.left - 4}px;`
                       + `width:${r.width + 8}px;height:${r.height + 8}px`;
    const h = tip.offsetHeight || 160;
    const w = tip.offsetWidth || 300;
    const below = r.bottom + 14;
    const top = (below + h > innerHeight - 10) ? Math.max(10, r.top - h - 14) : below;
    tip.style.top = `${top}px`;
    tip.style.left = `${Math.min(Math.max(10, r.left), innerWidth - w - 10)}px`;
  };

  const paint = () => {
    const s = BYOK_TOUR_STEPS[step];
    const node = resolveTarget(s.target);
    dots.innerHTML = '';
    BYOK_TOUR_STEPS.forEach((_, i) => {
      const d = document.createElement('span');
      d.className = `ob-dot${i === step ? ' on' : ''}${i < step ? ' done' : ''}`;
      dots.appendChild(d);
    });
    title.textContent = s.title;
    body.textContent = s.body;
    next.textContent = step === BYOK_TOUR_STEPS.length - 1 ? 'DONE ▸' : 'NEXT ▸';
    // The trigger button sits below the provider row in the (internally
    // scrolling) gate box, so the very first spotlighted button can start
    // off-screen — scrollIntoView on .gate as the nearest scrollable
    // ancestor, then position() runs again via the scroll listener below.
    if (node) node.scrollIntoView({ block: 'center', behavior: RM ? 'auto' : 'smooth' });
    position(node);
    next.focus();
  };

  const advance = () => {
    if (step >= BYOK_TOUR_STEPS.length - 1) end(); else { step += 1; paint(); }
  };

  function end() {
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', onMove);
    document.removeEventListener('scroll', onMove, true);
    root.remove();
    $('byok-tour-btn')?.focus();
  }

  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); end(); }
    else if (e.key === 'Enter' || e.key === 'ArrowRight') {
      e.preventDefault(); e.stopPropagation(); advance();
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault(); e.stopPropagation();
      step = Math.max(0, step - 1); paint();
    } else if (e.key === 'Tab') {
      // Two-element focus trap, same as #tour's.
      e.preventDefault();
      (document.activeElement === next ? skip : next).focus();
    }
  }
  const onMove = () => position(resolveTarget(BYOK_TOUR_STEPS[step].target));

  skip.onclick = end;
  next.onclick = advance;
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', onMove);
  // Capture phase: .gate.on scrolls internally (overflow-y:auto), so a
  // bubbling listener on window would never see it — same reasoning
  // tour.js's own onMove uses for #out and the side panels.
  document.addEventListener('scroll', onMove, true);

  paint();
}

export function showLanding({ mode = 'landing', onUnlocked, onVisitor, onSelfKey } = {}) {
  const host = $('gate');
  let cur = mode;

  /* A browser that has unlocked as the operator before goes straight to the key
     field. Only the WORD 'owner' is remembered — never a secret. Without this
     the person who uses this terminal daily pays a click and a context switch
     on every single visit to accommodate first-time visitors. */
  if (cur === 'landing') {
    try {
      if (localStorage.getItem(LAST_TIER_KEY) === 'owner') cur = 'unlock';
    } catch { /* private mode — just show the landing */ }
  }

  const render = () => {
    host.className = 'gate on';
    host.innerHTML =
      `<div class="gate-box ld">
         <pre class="boot-art" aria-hidden="true">${ART}</pre>
         ${cur === 'landing' ? paneLanding()
           : cur === 'request' ? paneRequest()
           : cur === 'byok'    ? paneByok()
           : paneUnlock()}
       </div>`;
    wire();
  };

  /* Order is deliberate. For the audience of this screen — someone who arrived
     with no key — the free path is the only one that works right now, so it is
     the primary action. "I have a key" is last because almost nobody does. */
  const paneLanding = () => `
    <div class="gate-hd">ARGUS://TERMINAL</div>
    <p class="ld-pitch">Free live valuation, price history and a macro gate for any
      listed company. Deeper AI research runs on the owner's account and needs a key.</p>
    <div class="ld-actions">
      <button id="ld-free" class="btn" type="button">CONTINUE WITHOUT A KEY ▸</button>
      <button id="ld-request" class="btn-g" type="button">REQUEST ACCESS</button>
      <button id="ld-byok" class="btn-g" type="button">BRING YOUR OWN KEY</button>
      <button id="ld-unlock" class="btn-g" type="button">I HAVE A KEY</button>
    </div>
    <p class="ld-disc" id="ld-disc"></p>`;

  /* A visitor's own Groq, Gemini, or Perplexity key — never the owner's
     book, never a session, never stored anywhere on this end. May be off on
     this particular instance (ALLOW_BYOK_VISITORS) — the server says so
     plainly if so, rather than this pane pretending it always works. */
  const paneByok = () => `
    <div class="gate-hd">ARGUS://OWN KEY</div>
    <div class="fld">
      <div class="fld-l">Provider</div>
      <div class="seg" id="byok-provider">
        <button type="button" data-v="groq">GROQ</button>
        <button type="button" data-v="gemini">GEMINI</button>
        <button type="button" data-v="perplexity">PERPLEXITY</button>
      </div>
    </div>
    <div class="fld">
      <div class="fld-l">Key</div>
      <input id="byok-key" class="inp" type="password" autocomplete="off"
             spellcheck="false" placeholder="paste your Groq, Gemini or Perplexity key"/>
      <div id="byok-err" class="gate-err"></div>
    </div>
    <button id="byok-go" class="btn" type="button" style="width:100%;padding:10px">CONTINUE ▸</button>

    <div class="fld byok-compare" style="margin-top:16px">
      <p class="hint" style="line-height:1.8">
        Not sure which to use? <b>Perplexity</b> is recommended for depth of
        sourcing. <b>Gemini</b> also searches the live web and is free within
        a monthly quota. <b>Groq</b> is instant and free but has no web
        search, so it can be stale.
      </p>
      <button id="byok-tour-btn" class="btn-g" type="button"
              style="width:100%;padding:9px;margin-top:8px">
        ▸ COMPARE ALL THREE · 30 SEC
      </button>
      <p class="hint" style="margin-top:8px">
        Prefer reading? Full walkthrough: <code>docs/SETUP.md</code>.
      </p>
    </div>

    <p class="hint" style="margin-top:14px;line-height:1.8">
      Used only for research you run this session — held in this tab's memory,
      never written to disk or browser storage, and gone the moment you
      refresh or close the tab. This does not unlock the owner's book or any
      admin action, only AI research funded by your own key. Not every ARGUS
      instance has this turned on — you'll be told plainly if this one hasn't.</p>
    <button id="ld-back" class="btn-g ld-back" type="button">◂ OTHER OPTIONS</button>`;

  const paneUnlock = () => `
    <div class="gate-hd">ARGUS://UNLOCK</div>
    <div class="fld">
      <div class="fld-l">Key</div>
      <input id="gate-key" class="inp" type="password" autocomplete="off"
             spellcheck="false" placeholder="Owner key, or a guest key (gk_…)"/>
      <button id="gate-eye" class="btn-g gate-eye" type="button"
              aria-label="Show or hide the key">SHOW</button>
      <div id="gate-err" class="gate-err"></div>
    </div>
    <button id="gate-go" class="btn" type="button" style="width:100%;padding:10px">UNLOCK ▸</button>
    <p class="hint" style="margin-top:14px;line-height:1.8">
      The owner's <b>AGENT_SECRET</b>, or a <b>guest key</b> they issued you — not a
      provider key. It is exchanged for a session cookie your browser stores but
      page scripts cannot read. Free quant, charts and the Brent gate work without
      any key; the owner's book and paid AI research do not.</p>
    <p class="hint" style="margin-top:10px;line-height:1.8">
      Running your own copy of ARGUS for the first time? Your
      <b>AGENT_SECRET</b> was generated automatically and printed to the
      server's console when it started — unlock with that here, then add a
      Groq or Perplexity key from <b>◈ CONFIG</b>, entirely in the browser.
      No terminal needed after this step. See <code>docs/SETUP.md</code> for
      the full walkthrough, including the CLI alternative.</p>
    <button id="ld-back" class="btn-g ld-back" type="button">◂ OTHER OPTIONS</button>`;

  const paneRequest = () => `
    <div class="gate-hd">ARGUS://REQUEST ACCESS</div>
    <div class="fld">
      <div class="fld-l">Email</div>
      <input id="rq-email" class="inp" type="email" autocomplete="email"
             spellcheck="false" placeholder="you@example.com"/>
    </div>
    <div class="fld">
      <div class="fld-l">Who are you? (optional)</div>
      <textarea id="rq-note" class="inp" rows="3" maxlength="280"
                placeholder="A line about why you'd like access."></textarea>
    </div>
    <p class="hint ld-priv" id="rq-priv"></p>
    <div id="rq-err" class="gate-err"></div>
    <button id="rq-send" class="btn" type="button" style="width:100%;padding:10px">SEND REQUEST ▸</button>
    <button id="ld-back" class="btn-g ld-back" type="button">◂ OTHER OPTIONS</button>`;

  const go = (m) => { cur = m; render(); };

  function wire() {
    // textContent for every standing disclosure — one definition in copy.js.
    const disc = $('ld-disc'); if (disc) disc.textContent = DISCLAIMER;
    const priv = $('rq-priv'); if (priv) priv.textContent = PRIVACY_NOTE;

    $('ld-request')?.addEventListener('click', () => go('request'));
    $('ld-unlock')?.addEventListener('click',  () => go('unlock'));
    $('ld-byok')?.addEventListener('click',    () => go('byok'));
    $('ld-back')?.addEventListener('click',    () => go('landing'));

    $('ld-free')?.addEventListener('click', () => {
      // Remembered for this tab only. A brand-new tab gets the landing again,
      // which is the right default for a shared or public machine.
      try { sessionStorage.setItem('argus.visitor', '1'); } catch { /* fine */ }
      host.className = 'gate';
      host.innerHTML = '';
      onVisitor && onVisitor();
    });

    wireUnlock();
    wireRequest();
    wireByok();
  }

  function wireByok() {
    const seg = $('byok-provider');
    if (!seg) return;
    let provider = 'groq';
    const paint = () => seg.querySelectorAll('button')
      .forEach(b => b.classList.toggle('on', b.dataset.v === provider));
    seg.querySelectorAll('button').forEach(b => {
      b.onclick = () => { provider = b.dataset.v; paint(); };
    });
    paint();

    const tourBtn = $('byok-tour-btn');
    if (tourBtn) tourBtn.onclick = () => startByokTour(seg);

    const input = $('byok-key');
    const err = $('byok-err');

    // Client-side sanity check only, to save a round trip on an obvious
    // mistake — the server re-validates on every single call regardless
    // (soft_validate, same function the CLI wizard and CONFIG modal use),
    // since this key is never stored here for the server to have already
    // checked once and remembered.
    const submit = () => {
      const v = input.value.trim();
      if (!v) { err.textContent = 'Paste a key first.'; input.focus(); return; }
      if (v.length < 20) { err.textContent = 'That looks too short to be a real key.'; return; }
      host.className = 'gate';
      host.innerHTML = '';
      onSelfKey && onSelfKey(provider, v);
    };
    $('byok-go').onclick = submit;
    input.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
  }

  function wireUnlock() {
    const input = $('gate-key');
    if (!input) return;
    const err = $('gate-err');
    const box = host.querySelector('.gate-box');
    let busy = false;

    // A long opaque guest key pasted into a masked field is exactly how someone
    // burns the 10-failures-per-15-minutes lockout — which is keyed on the
    // socket peer, so it takes out everyone behind the same NAT, the operator
    // included. Let them see what they pasted.
    $('gate-eye').onclick = () => {
      const showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      $('gate-eye').textContent = showing ? 'SHOW' : 'HIDE';
      input.focus();
    };

    const submit = async () => {
      if (busy) return;
      const v = input.value.trim();     // trimmed: a trailing newline off a
      if (!v) { input.focus(); return; } // paste is a free failed attempt
      busy = true;
      err.textContent = '';
      const res = await api.auth.unlock(v);
      busy = false;

      if (res.ok) {
        try { localStorage.setItem(LAST_TIER_KEY, res.tier || 'owner'); } catch { /* fine */ }
        host.className = 'gate';
        host.innerHTML = '';
        onUnlocked && onUnlocked(res);
        return;
      }

      // Still deliberately generic about a WRONG key — never hint at how close
      // the attempt was. But a lockout, a dead guest key and an unreachable
      // server are not wrong keys, and reporting them as "Rejected." sends the
      // user to retry, which on a lockout makes it worse.
      if (res.status === 429) {
        err.textContent = 'Too many attempts. Locked for ~15 min — the key may still be right.';
      } else if (res.status === 0) {
        err.textContent = 'Cannot reach the server. Is it still running?';
      } else if (res.code === 'guest_expired') {
        err.textContent = res.message || 'That guest key has expired. Ask the owner for a new one.';
      } else {
        err.textContent = 'Rejected.';
      }
      box.classList.remove('shake'); void box.offsetWidth; box.classList.add('shake');
      input.select();
    };

    $('gate-go').onclick = submit;
    input.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
    input.focus();
  }

  function wireRequest() {
    const send = $('rq-send');
    if (!send) return;
    const email = $('rq-email');
    const note = $('rq-note');
    const err = $('rq-err');
    let busy = false;

    // fetch(), never a form submit: _PAGE_CSP sets form-action 'none', so a
    // native <form action=…> is silently blocked by the browser.
    const submit = async () => {
      if (busy) return;
      const v = (email.value || '').trim();
      if (!/.+@.+\..+/.test(v)) {
        err.textContent = 'Enter a valid email address.';
        email.focus();
        return;
      }
      busy = true;
      err.textContent = '';
      send.disabled = true;
      send.textContent = 'SENDING…';
      try {
        await api.requestAccess(v, (note.value || '').trim());
        host.querySelector('.gate-box').innerHTML =
          `<pre class="boot-art" aria-hidden="true">${ART}</pre>
           <div class="gate-hd">ARGUS://REQUEST SENT</div>
           <p class="ld-pitch" id="rq-done"></p>
           <button id="ld-back" class="btn-g" type="button"
                   style="width:100%;padding:10px">◂ BACK</button>`;
        // Say what actually happens. There is no mail server here: a person
        // reads this and writes back by hand, which can take a day or two.
        $('rq-done').textContent =
          'Recorded. The owner reviews requests personally and will email your key '
          + 'from their own address — nothing is sent automatically, so it may take '
          + 'a day or two. Nothing else will ever be sent to you. In the meantime '
          + 'you can keep using the free tools.';
        $('ld-back').onclick = () => go('landing');
      } catch (e) {
        busy = false;
        send.disabled = false;
        send.textContent = 'SEND REQUEST ▸';
        err.textContent = e.kind === 'ratelimit'
          ? 'Too many requests from this connection. Try again later.'
          : e.message || 'Could not send the request.';
      }
    };

    send.onclick = submit;
    email.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
    email.focus();
  }

  render();
}

/* Kept for the re-unlock path used by the config modal. */
export function showUnlock(onDone) {
  showLanding({ mode: 'unlock', onUnlocked: onDone });
}

/* Shown where a view needs the book but the viewer has no claim to it. Better
   than letting the view 401 and print an error — the reader is not doing
   anything wrong, they simply are not the owner. */
export function renderLockedView(host, { title = 'ARGUS://LOCKED', body = '', onUnlock, onRequest } = {}) {
  host.innerHTML = '';
  host.appendChild(el('div', 'r-title', title));
  const p = el('p', 'hint');
  p.style.cssText = 'line-height:1.9;margin:10px 0 16px';
  p.textContent = body;
  host.appendChild(p);

  const row = el('div', 'ld-actions');
  row.style.maxWidth = '360px';
  const req = el('button', 'btn', 'REQUEST ACCESS');
  req.type = 'button';
  req.onclick = () => onRequest && onRequest();
  const unl = el('button', 'btn-g', 'I HAVE A KEY');
  unl.type = 'button';
  unl.onclick = () => onUnlock && onUnlock();
  row.append(req, unl);
  host.appendChild(row);

  const d = el('p', 'ld-disc');
  d.textContent = DISCLAIMER;
  host.appendChild(d);
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

export async function renderPortfolio(host, { onOutlook, tier = 'owner' } = {}) {
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
  await paintOutlook(box, onOutlook, tier);
}

export async function paintOutlook(box, onOutlook, tier = 'owner') {
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
    // Owner only. The button is hidden rather than disabled for a guest: an
    // inert control invites a click and then explains why it did nothing.
    if (tier === 'owner') {
      const btn = el('button', 'btn', 'RUN OUTLOOK  ~$0.06');
      btn.type = 'button';
      btn.onclick = () => onOutlook && onOutlook(box);
      empty.appendChild(btn);
    } else {
      empty.appendChild(el('p', 'hint',
        'No outlook has been generated yet. Only the terminal owner can run one.'));
    }
    box.appendChild(empty);
    return;
  }

  const head = el('div', 'ol-head');
  const age = cached.age_days === 0 ? 'today'
            : cached.age_days === 1 ? 'yesterday'
            : cached.age_days != null ? `${cached.age_days} days ago` : '';
  head.appendChild(el('span', 'dim', `Generated ${age} · ${cached.provider === 'groq' ? 'GROQ · no live web' : 'Perplexity · live web'}`));
  if (tier === 'owner') {
    const refresh = el('button', 'btn-g', '↻ REFRESH  ~$0.06');
    refresh.type = 'button';
    refresh.onclick = () => onOutlook && onOutlook(box);
    head.appendChild(refresh);
  }
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

export async function renderProfile(host, { onChanged, tier = 'owner' } = {}) {
  host.innerHTML = '';
  host.appendChild(el('div', 'r-title', 'ARGUS://PROFILE'));

  let profile = null, positions = {}, watch = {};
  try {
    profile   = (await api.getProfile()).profile;
    positions = (await api.getPositions()).positions;
    watch     = (await api.getWatchlist()).watchlist;
  } catch (err) { host.appendChild(el('div', 'dim', err.message)); return; }

  /* A guest reads this page; they do not edit it. The server refuses their
     writes with a 403 regardless, so this is about not offering a control that
     cannot work — every save button here would fail, and an interface that
     invites an action it will then refuse is worse than one that does not
     offer it. Appearance settings stay live: those are localStorage, theirs. */
  const ro = tier !== 'owner';
  if (ro) {
    const banner = el('p', 'hint');
    banner.style.cssText = 'margin:6px 0 4px;line-height:1.8';
    banner.textContent =
      'Guest access is read-only. This is the owner\'s book — you can see it, '
      + 'but positions, watchlist and tailoring cannot be changed from here. '
      + 'Appearance settings below are yours and are saved in this browser.';
    host.appendChild(banner);
  }

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

  if (ro) {
    // Freeze the tailoring controls rather than hiding them: the guest should
    // still be able to SEE how this terminal is configured.
    host.querySelectorAll('#pf-name, .seg button, .chip-t')
        .forEach(n => { n.disabled = true; });
  }

  const save = el('button', 'btn', 'SAVE TAILORING');
  save.type = 'button';
  save.onclick = async () => {
    try {
      await api.saveProfile({ name: $('pf-name').value, ...sel });
      ui.toast('Tailoring saved.', 'success', 2600);
      onChanged && onChanged();
    } catch (err) { ui.toast(err.message, 'error'); }
  };
  if (!ro) host.appendChild(save);

  // Positions
  host.appendChild(el('div', 'r-head', 'POSITIONS'));
  const ptab = el('div', 'ed-tab');
  ptab.appendChild(rowHead(['TICKER', 'SHARES', 'AVG COST', 'BUY DATE', 'STOP', 'TRIM', '']));
  for (const [t, p] of Object.entries(positions)) {
    ptab.appendChild(positionRow(t, p, onChanged, false, ro));
  }
  host.appendChild(ptab);

  const addP = el('button', 'btn-g', '+ ADD POSITION');
  addP.type = 'button';
  addP.style.marginTop = '8px';
  addP.onclick = () => {
    const blank = el('div');
    ptab.appendChild(positionRow('', {}, onChanged, true));
  };
  if (!ro) host.appendChild(addP);

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
    if (ro) del.style.display = 'none';
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
    const done = await api.auth.signOut();
    ui.renderKeyState('visitor', null);
    ui.toast(done ? 'Signed out. Paid research is locked until you unlock again.'
                  : 'Could not confirm sign-out — the server did not respond. Close this browser to be safe.',
             done ? 'info' : 'warn', done ? 4000 : 9000);
    onChanged && onChanged();
  };
  host.appendChild(out);

  if (tier === 'owner') await renderAdmin(host);
}

/* ── Owner administration ─────────────────────────────────────────────────
   Requests and issued keys.

   SECURITY: `email` and `note` are typed by strangers into a public form and
   land here, inside the owner's authenticated page. Every one of them is
   written with textContent or el(tag, cls, text) — never interpolated into a
   template, escaped or otherwise. The CSP would stop an injected <script> from
   running, but markup injection could still deface this view or smuggle in
   something clickable, and there is no reason to rely on the CSP for a value
   that has no business being markup in the first place. */

async function renderAdmin(host) {
  host.appendChild(el('div', 'r-head', 'ACCESS://REQUESTS'));
  const reqBox = el('div');
  reqBox.id = 'adm-req';
  host.appendChild(reqBox);

  host.appendChild(el('div', 'r-head', 'ACCESS://GUEST KEYS'));
  const keyBox = el('div');
  keyBox.id = 'adm-keys';
  host.appendChild(keyBox);

  const panic = el('button', 'btn-danger', 'REVOKE ALL GUEST ACCESS');
  panic.type = 'button';
  panic.style.marginTop = '10px';
  panic.onclick = async () => {
    const ok = await ui.confirmAction(
      'REVOKE ALL GUEST ACCESS',
      'Revokes every issued guest key and signs out every guest immediately. '
      + 'Your own session is unaffected. Note that rotating AGENT_SECRET does NOT '
      + 'do this — sessions are checked before the secret is, so this is the only '
      + 'way to actually cut guests off.',
      'REVOKE ALL');
    if (!ok) return;
    try {
      const r = await api.adminRevokeAll();
      ui.toast(`Revoked ${r.revoked} key(s). All guest sessions ended.`, 'success', 5000);
      await paintRequests(reqBox);
      await paintKeys(keyBox);
    } catch (err) { ui.toast(err.message, 'error'); }
  };
  host.appendChild(panic);

  await paintRequests(reqBox);
  await paintKeys(keyBox);
}

/* Approve and reveal, shared by the pending row's ✓ and the approved row's +KEY.
   Both mint a key, and the raw key exists in that ONE response and nowhere else
   — two hand-copied reveal calls is one of them quietly losing the reveal and
   minting a key nobody can ever read. Only the repaint differs: re-approving an
   already-approved row does not change its status, so that list stays put. */
async function approveAndReveal(r, box, { repaintRequests }) {
  try {
    const res = await api.adminApprove(r.id);
    ui.showKeyReveal({ email: r.email, key: res.guest_key,
                       expiresAt: (res.expires_at || '').slice(0, 16).replace('T', ' ') });
    if (repaintRequests) await paintRequests(box);
    await paintKeys($('adm-keys'));
  } catch (err) { ui.toast(err.message, 'error'); }
}

async function paintRequests(box) {
  box.innerHTML = '';
  let rows = [];
  try { rows = (await api.adminRequests()).requests || []; }
  catch (err) { box.appendChild(el('div', 'adm-empty', err.message)); return; }

  if (!rows.length) { box.appendChild(el('div', 'adm-empty', 'No requests yet.')); return; }

  const tab = el('div', 'ed-tab');
  tab.appendChild(rowHead(['EMAIL', 'NOTE', 'STATUS', '']));

  for (const r of rows) {
    const row = el('div', 'ed-row rq');
    if (r.status !== 'pending') row.classList.add('gk-dead');

    row.appendChild(el('span', 'ed-t', r.email));            // untrusted → text
    row.appendChild(el('span', 'rq-note', (r.note || '—')));  // untrusted → text
    row.appendChild(el('span', `rq-st ${r.status}`, r.status.toUpperCase()));

    const acts = el('div', 'ed-a');
    if (r.status === 'pending') {
      const ok = el('button', 'btn-g', '✓');
      ok.type = 'button'; ok.title = `Approve ${r.email} and mint a key`;
      // The reveal inside is the only moment this key exists in readable form.
      ok.onclick = () => approveAndReveal(r, box, { repaintRequests: true });
      const no = el('button', 'btn-danger', '✕');
      no.type = 'button'; no.title = `Deny ${r.email}`;
      no.onclick = async () => {
        const sure = await ui.confirmAction('DENY REQUEST',
          `Deny access for ${r.email}? They are not told, and resubmitting will not `
          + 'reach you again — but you can reopen it from this list.', 'DENY');
        if (!sure) return;
        try { await api.adminDeny(r.id); await paintRequests(box); }
        catch (err) { ui.toast(err.message, 'error'); }
      };
      acts.append(ok, no);
    } else if (r.status === 'denied') {
      // Denied rows stay visible and reopenable. Hidden, a misclick becomes an
      // invisible permanent block: their resubmissions collide with the unique
      // email and silently change nothing.
      const re = el('button', 'btn-g', '↺');
      re.type = 'button'; re.title = `Reopen ${r.email}`;
      re.onclick = async () => {
        try { await api.adminReopen(r.id); await paintRequests(box); ui.toast('Reopened.', 'success', 2600); }
        catch (err) { ui.toast(err.message, 'error'); }
      };
      acts.appendChild(re);
    } else {
      const again = el('button', 'btn-g', '+KEY');
      again.type = 'button'; again.title = `Issue ${r.email} a fresh key`;
      again.onclick = () => approveAndReveal(r, box, { repaintRequests: false });
      acts.appendChild(again);
    }
    row.appendChild(acts);
    tab.appendChild(row);
  }
  box.appendChild(tab);
}

async function paintKeys(box) {
  if (!box) return;
  box.innerHTML = '';
  let keys = [];
  try { keys = (await api.adminGuestKeys()).keys || []; }
  catch (err) { box.appendChild(el('div', 'adm-empty', err.message)); return; }

  if (!keys.length) { box.appendChild(el('div', 'adm-empty', 'No keys issued.')); return; }

  const tab = el('div', 'ed-tab');
  tab.appendChild(rowHead(['KEY', 'ISSUED TO', 'EXPIRES', 'DIVE', 'USED', '']));

  for (const k of keys) {
    const row = el('div', 'ed-row gk');
    const dead = k.revoked || k.expired;
    if (dead) row.classList.add('gk-dead');

    // key_prefix only — the full key exists nowhere on the server.
    row.appendChild(el('span', 'ed-t', `${k.key_prefix}…`));
    row.appendChild(el('span', 'rq-note', k.label || '—'));   // untrusted → text

    const when = k.revoked ? 'revoked'
               : k.expired ? 'expired'
               : relWhen(k.expires_at);
    row.appendChild(el('span', 'rq-note', when));
    row.appendChild(el('span', 'rq-note', k.dive_ticker || '—'));
    row.appendChild(el('span', 'rq-note', `${k.modules_used}/${k.modules_total}`));

    const acts = el('div', 'ed-a');
    if (!dead) {
      const rv = el('button', 'btn-danger', '✕');
      rv.type = 'button'; rv.title = 'Revoke this key';
      rv.onclick = async () => {
        const sure = await ui.confirmAction('REVOKE KEY',
          `Revoke the key issued to ${k.label || 'this person'}? Their session ends `
          + 'on their next request.', 'REVOKE');
        if (!sure) return;
        try { await api.adminRevoke(k.id); await paintKeys(box); ui.toast('Key revoked.', 'success', 2600); }
        catch (err) { ui.toast(err.message, 'error'); }
      };
      acts.appendChild(rv);
    }
    row.appendChild(acts);
    tab.appendChild(row);
  }
  box.appendChild(tab);
}

function relWhen(iso) {
  const t = Date.parse(iso || '');
  if (!Number.isFinite(t)) return '—';
  const hrs = (t - Date.now()) / 3.6e6;
  if (hrs <= 0) return 'expired';
  if (hrs < 1) return '<1h left';
  return `${Math.round(hrs)}h left`;
}

function rowHead(cols) {
  const r = el('div', 'ed-row head');
  for (const c of cols) r.appendChild(el('span', null, c));
  return r;
}

function positionRow(ticker, p, onChanged, isNew = false, readOnly = false) {
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

  // A guest sees the book exactly as it is, and cannot edit a field of it.
  if (readOnly) {
    [tIn, sh, cost, date, stop, trim].forEach(n => { n.disabled = true; });
    return r;
  }

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
