'use strict';

/* tour.js — a five-step coach-mark orientation over the real terminal.

   No library, no network, no imports. It reads the DOM, draws an overlay, and
   writes one localStorage flag. Keeping it dependency-free is deliberate: it
   must never be able to delay or interfere with a run in flight.

   Two hard rules, both learned from the layer it sits next to:

   1. Z-INDEX 55. The modal layer (.ov) is 60 and the cost-confirmation dialog
      lives there. At any value above 60 this overlay would dim that dialog and,
      because the root takes pointer events across the whole viewport, swallow
      the click on CANCEL. That dialog is the project's guarantee against
      unintended spend; nothing may cover it.

   2. It refuses to start while a modal or the gate is open, for the same
      reason. Being below them is not enough if it can be launched on top of a
      decision the reader is in the middle of making. */

const ROOT_ID = 'tour';
const SEEN_KEY = 'argus.toured';

const RM = window.matchMedia('(prefers-reduced-motion:reduce)').matches;

let active = false;
let step = 0;
let steps = [];
let onKey = null;
let onMove = null;

export function isTouring() { return active; }

function seen() {
  try { return localStorage.getItem(SEEN_KEY) === '1'; } catch { return false; }
}
function markSeen() {
  try { localStorage.setItem(SEEN_KEY, '1'); } catch { /* private mode — session only */ }
}

/* Content depends on who is reading it. After onboarding stops auto-firing this
   for a fresh owner, the usual audience is a visitor or a guest — and telling a
   visitor to "click a row in the tracked book" points them at a panel that
   reads "the book is private". The allowance step matters most for a guest and
   does not exist for anyone else. */
function buildSteps(tier) {
  const common = [
    { target: null, title: 'ARGUS://TOUR',
      body: 'A 30-second orientation. Press Esc to skip at any point, or type '
          + '`tour` to replay it later.' },
    { target: '#cmdbar', title: 'COMMAND LINE',
      body: 'Type a ticker and press Enter for a full brief. `quant AAPL` runs the '
          + 'free numbers only, and never costs anything. `help` lists everything.' },
    { target: '#blk-brent', title: 'THE MACRO GATE',
      body: 'Brent crude sets the posture. The highlighted band is the live signal — '
          + 'when the gate reads CLOSED, the framework says hold rather than buy.' },
  ];

  const middle = tier === 'owner'
    ? { target: '#blk-watch', title: 'YOUR BOOK',
        body: 'Every holding and watchlist name. Click a row to research it; '
            + 'Alt-click, or press `i`, opens the inspector with cost basis and stops.' }
    : { target: '#out', title: 'RESEARCH PANE',
        body: 'Valuation, quality metrics and the price chart land here for any ticker '
            + 'you ask about — free, unlimited, and with the reasoning shown rather '
            + 'than a bare score.' };

  // All three point at the same chip, so the selectors are written once.
  const last = { target: '#key-state', fallback: '#btn-cfg', ...lastStepCopy(tier) };

  return [...common, middle, last];
}

function lastStepCopy(tier) {
  switch (tier) {
    case 'guest':
      return { title: 'YOUR GUEST KEY',
               body: 'Your key includes one full five-stage AI deep dive on a single ticker, '
                   + 'and expires 24 hours after it was issued. Everything else here — quant, '
                   + 'charts, history, news — is free and unlimited until then.' };
    case 'owner':
      return { title: 'COST CONTROL',
               body: 'Free tools cost nothing, ever. Paid AI research is gated by this chip '
                   + 'and always shows a dollar estimate for your confirmation first.' };
    default:
      return { title: 'FREE MODE',
               body: 'You are browsing without a key: all the free tools, none of the paid '
                   + 'AI research. Type `request` if you would like the owner to issue you '
                   + 'a guest key.' };
  }
}

/* A step whose target is missing or laid out to zero — the side panels collapse
   into slide-ins under 1180px — is skipped rather than pointed at nothing. */
function resolve(s) {
  for (const sel of [s.target, s.fallback]) {
    if (!sel) continue;
    const n = document.querySelector(sel);
    if (!n) continue;
    const r = n.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && n.offsetParent !== null) return n;
  }
  return null;
}

function build() {
  const root = document.createElement('div');
  root.id = ROOT_ID;
  if (RM) root.classList.add('rm');

  const spot = document.createElement('div');
  spot.id = 'tour-spot';

  const tip = document.createElement('div');
  tip.id = 'tour-tip';
  tip.setAttribute('role', 'dialog');
  tip.setAttribute('aria-modal', 'true');
  tip.setAttribute('aria-labelledby', 'tour-title');

  const dots = document.createElement('div');
  dots.className = 'ob-steps';
  dots.id = 'tour-dots';

  const title = document.createElement('div');
  title.className = 'gate-hd';
  title.id = 'tour-title';

  const body = document.createElement('p');
  body.className = 'hint';
  body.id = 'tour-body';

  const nav = document.createElement('div');
  nav.className = 'tour-nav';
  const skip = document.createElement('button');
  skip.className = 'btn-g'; skip.type = 'button'; skip.id = 'tour-skip';
  skip.textContent = 'SKIP (ESC)';
  const next = document.createElement('button');
  next.className = 'btn'; next.type = 'button'; next.id = 'tour-next';
  nav.append(skip, next);

  tip.append(dots, title, body, nav);
  root.append(spot, tip);
  document.body.appendChild(root);

  skip.onclick = end;
  next.onclick = () => { step += 1; paint(); };
  return root;
}

function position(node) {
  const spot = document.getElementById('tour-spot');
  const tip = document.getElementById('tour-tip');
  if (!spot || !tip) return;

  if (!node) {
    // Centred, zero-size cut-out: the giant box-shadow dims the whole viewport.
    spot.style.cssText = `top:${innerHeight / 2}px;left:${innerWidth / 2}px;width:0;height:0`;
    tip.style.top = `${Math.max(12, innerHeight / 2 - tip.offsetHeight / 2)}px`;
    tip.style.left = `${Math.max(10, innerWidth / 2 - tip.offsetWidth / 2)}px`;
    return;
  }

  const r = node.getBoundingClientRect();
  spot.style.cssText = `top:${r.top - 4}px;left:${r.left - 4}px;`
                     + `width:${r.width + 8}px;height:${r.height + 8}px`;

  // Below the target, flipped above when it would run off the bottom, then
  // clamped horizontally — the same arithmetic initTooltips() uses.
  const h = tip.offsetHeight || 160;
  const w = tip.offsetWidth || 300;
  const below = r.bottom + 14;
  const top = (below + h > innerHeight - 10) ? Math.max(10, r.top - h - 14) : below;
  tip.style.top = `${top}px`;
  tip.style.left = `${Math.min(Math.max(10, r.left), innerWidth - w - 10)}px`;
}

function paint() {
  // Walk past any step whose target is not on screen at this width.
  while (step < steps.length && steps[step].target && !resolve(steps[step])) step += 1;
  if (step >= steps.length) { end(); return; }

  const s = steps[step];
  const node = resolve(s);

  const dots = document.getElementById('tour-dots');
  dots.innerHTML = '';
  steps.forEach((_, i) => {
    const d = document.createElement('span');
    d.className = `ob-dot${i === step ? ' on' : ''}${i < step ? ' done' : ''}`;
    dots.appendChild(d);
  });

  document.getElementById('tour-title').textContent = s.title;
  document.getElementById('tour-body').textContent = s.body;
  document.getElementById('tour-next').textContent =
    step === steps.length - 1 ? 'DONE ▸' : 'NEXT ▸';

  position(node);
  document.getElementById('tour-next').focus();
}

function end() {
  if (!active) return;
  active = false;
  markSeen();
  document.removeEventListener('keydown', onKey, true);
  window.removeEventListener('resize', onMove);
  document.removeEventListener('scroll', onMove, true);
  document.getElementById(ROOT_ID)?.remove();
  document.getElementById('cmd')?.focus();
}

export function startTour(tier = 'visitor') {
  if (active) return;

  /* Never over a dialog. .ov is the cost-confirmation layer; #gate.on is the
     landing. Both are decisions in progress. */
  if (document.querySelector('.ov') || document.getElementById('gate')?.classList.contains('on')) {
    return { blocked: true };
  }

  steps = buildSteps(tier);
  step = 0;
  active = true;
  build();

  onKey = (e) => {
    if (!active) return;
    if (e.key === 'Escape') {
      // Stop it reaching app.js's Escape handler, which would also close panels.
      e.preventDefault(); e.stopPropagation();
      end();
    } else if (e.key === 'Enter' || e.key === 'ArrowRight') {
      e.preventDefault(); e.stopPropagation();
      step += 1; paint();
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault(); e.stopPropagation();
      step = Math.max(0, step - 1); paint();
    } else if (e.key === 'Tab') {
      // Two-element focus trap: the dialog is modal, so focus stays inside it.
      const a = document.getElementById('tour-skip');
      const b = document.getElementById('tour-next');
      if (!a || !b) return;
      e.preventDefault();
      (document.activeElement === b ? a : b).focus();
    }
  };

  // Capture phase, and scroll listened to on capture as well: #out and the side
  // panels scroll internally, so a bubbling listener on window never sees them.
  onMove = () => { if (active) position(resolve(steps[step])); };
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', onMove);
  document.addEventListener('scroll', onMove, true);

  paint();
  return { blocked: false };
}

/* First visit only. Owners who just finished onboarding have already been shown
   these surfaces, and app.js sets the flag for them so this does no work. */
export function maybeOfferTour(tier = 'visitor') {
  if (seen()) return;
  startTour(tier);
}
