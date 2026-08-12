'use strict';

/* theme-boot.js — applies terminal preferences before first paint.

   Loaded as a plain blocking <script> in <head>, NOT type="module": modules are
   deferred, which would paint the default accent first and flash. It has to be
   an external file rather than inline because the CSP is script-src 'self'. */

(function () {
  var d = document.documentElement;
  var get = function (k, fallback) {
    try { return localStorage.getItem(k) || fallback; } catch (e) { return fallback; }
  };
  d.setAttribute('data-accent', get('argus.accent', 'amber'));
  d.setAttribute('data-tempo',  get('argus.tempo',  'normal'));
  d.setAttribute('data-crt',    get('argus.crt',    'off'));
})();
