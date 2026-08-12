'use strict';

/* theme.js — terminal preferences: accent colour, tempo, CRT scanlines.

   The initial values are already on <html> by the time this module runs
   (theme-boot.js); this only handles changes after load. */

const KEYS = { accent: 'argus.accent', tempo: 'argus.tempo', crt: 'argus.crt' };
const listeners = [];

export const ACCENTS = ['amber', 'cyan', 'lime', 'magenta'];
export const TEMPOS  = ['calm', 'normal', 'volatile'];

const root = () => document.documentElement;

export const get = (k) => root().getAttribute(`data-${k}`) || '';

export function set(k, value) {
  if (!KEYS[k]) return;
  root().setAttribute(`data-${k}`, value);
  try { localStorage.setItem(KEYS[k], value); } catch { /* private mode — session only */ }
  for (const cb of listeners) {
    try { cb(k, value); } catch { /* one bad listener must not block the rest */ }
  }
}

/* Anything that paints its own colours (the SVG chart) subscribes here. */
export function onChange(cb) {
  listeners.push(cb);
  return () => {
    const i = listeners.indexOf(cb);
    if (i >= 0) listeners.splice(i, 1);
  };
}

/* Typing speed per keystroke, in ms — drives the research reveal. */
export function typeDelay() {
  return { calm: 22, normal: 12, volatile: 5 }[get('tempo')] ?? 12;
}

/* Step delay between pipeline log lines. */
export function stepDelay() {
  return { calm: 320, normal: 190, volatile: 70 }[get('tempo')] ?? 190;
}
