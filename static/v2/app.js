'use strict';

// ── State & Helpers ──────────────────────────────────────────────────────
const S = { key: localStorage.getItem('agentKey') || '' };
const $ = id => document.getElementById(id);
const $$ = sel => document.querySelectorAll(sel);

// ── Topographic canvas ──────────────────────────────────────────────────
function bootTopo() {
  const cv = $('topo-canvas');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  let w, h;

  function resize() {
    w = cv.parentElement.offsetWidth;
    h = cv.parentElement.offsetHeight;
    cv.width = w;
    cv.height = h;
  }
  resize();
  window.addEventListener('resize', resize);

  let offset = 0;
  function draw() {
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = '#C9963A';
    ctx.lineWidth = 1;

    const spacing = 48;
    const rows = Math.ceil(h / spacing) + 2;

    for (let i = 0; i < rows; i++) {
      ctx.beginPath();
      const baseY = i * spacing + (offset % spacing);
      for (let x = 0; x <= w; x += 4) {
        const y = baseY + Math.sin(x * 0.008 + i * 1.2 + offset * 0.0003) * 12
                        + Math.sin(x * 0.003 + i * 0.5) * 8;
        x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.stroke();
    }

    offset += 0.4;
    requestAnimationFrame(draw);
  }
  draw();
}

// ── Nav ──────────────────────────────────────────────────────────────────
function initNav() {
  const nav = $('nav');
  window.addEventListener('scroll', () => {
    nav.classList.toggle('scrolled', window.scrollY > 40);
  }, { passive: true });

  // Mobile hamburger
  const ham = $('hamburger');
  const links = $('nav-links');
  ham.addEventListener('click', () => {
    ham.classList.toggle('open');
    links.classList.toggle('open');
  });

  // Close on link click
  $$('.nav-link, .nav-cta').forEach(a => {
    a.addEventListener('click', () => {
      ham.classList.remove('open');
      links.classList.remove('open');
    });
  });
}

// ── Scroll reveal ───────────────────────────────────────────────────────
function initReveal() {
  const obs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) { e.target.classList.add('vis'); obs.unobserve(e.target); }
    });
  }, { threshold: 0.12 });
  $$('.fade-up').forEach(el => obs.observe(el));
}

// ── Brent + portfolio for hero panel ────────────────────────────────────
const SIGNAL_MAP = {
  'AGGRESSIVE BUY': { dot: 'signal-green', text: '🟢 AGGRESSIVE BUY · Deploy capital' },
  'ADD':            { dot: 'signal-green', text: '🟢 ADD · Accumulate on dips' },
  'HOLD':           { dot: 'signal-amber', text: '⚪ HOLD · No new positions' },
  'CAUTION':        { dot: 'signal-amber', text: '🟡 CAUTION · Reduce exposure' },
  'DEFENSIVE':      { dot: 'signal-red',   text: '🔴 DEFENSIVE · Hedge or exit' },
};

async function fetchHeroData() {
  // Brent
  try {
    const br = await fetch('/api/brent');
    if (br.ok) {
      const d = await br.json();
      if (!d.error) {
        const price = parseFloat(d.brent_price || 0);
        $('hp-price').textContent = `$${price.toFixed(2)}`;
        const sig = (d.signal || '').toUpperCase();
        const info = SIGNAL_MAP[sig] || { dot: 'signal-neutral', text: `⚪ ${sig}` };
        const dotEl = document.querySelector('.signal-dot');
        dotEl.className = 'signal-dot ' + info.dot;
        $('hp-signal-text').textContent = info.text;
      }
    }
  } catch (_) {}

  // Portfolio snapshot
  try {
    const pr = await fetch('/api/portfolio');
    if (pr.ok) {
      const d = await pr.json();
      const positions = (d.positions || []).slice(0, 3);
      const container = $('hp-portfolio');
      container.innerHTML = '';

      positions.forEach(p => {
        const price = p.price;
        const pctHigh = p.pct_from_52w_high;
        const isUp = pctHigh != null ? parseFloat(pctHigh) > -5 : true;

        const row = document.createElement('div');
        row.className = 'port-row';
        row.innerHTML = `
          <span class="port-ticker">${p.ticker}</span>
          <span class="port-price">$${price != null ? parseFloat(price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'}</span>
          <span class="port-change ${isUp ? 'up' : 'down'}">${pctHigh != null ? parseFloat(pctHigh).toFixed(1) + '%' : '—'}</span>
          <span class="port-status">${isUp ? '🟢' : '🟡'}</span>
        `;
        container.appendChild(row);
      });
    }
  } catch (_) {}
}

// ── Code tabs ───────────────────────────────────────────────────────────
function initCodeTabs() {
  $$('.code-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      $$('.code-tab').forEach(t => t.classList.remove('active'));
      $$('.code-content').forEach(c => c.classList.remove('active'));
      tab.classList.add('active');
      const target = $(`code-${tab.dataset.tab}`);
      if (target) target.classList.add('active');
    });
  });
}

// ── Key drawer ──────────────────────────────────────────────────────────
function initKeyDrawer() {
  const drawer  = $('key-drawer');
  const overlay = $('key-overlay');
  const input   = $('kd-input');
  const msg     = $('kd-msg');

  if (S.key) input.value = S.key;

  function open()  { drawer.classList.add('open'); overlay.classList.add('open'); }
  function close() { drawer.classList.remove('open'); overlay.classList.remove('open'); }

  // No settings button in nav for v2 — opened via demo hint
  overlay.addEventListener('click', close);
  $('kd-close').addEventListener('click', close);

  $('kd-save').addEventListener('click', () => {
    const k = input.value.trim();
    S.key = k;
    if (k) { localStorage.setItem('agentKey', k); msg.style.color = 'var(--teal)'; msg.textContent = '✓ Key saved'; }
    else { localStorage.removeItem('agentKey'); msg.style.color = 'var(--muted)'; msg.textContent = 'Key cleared'; }
    setTimeout(() => { msg.textContent = ''; }, 3000);
  });

  // Public hook so demo section can open it
  window._openKeyDrawer = open;
}

// ── Demo — run analysis ─────────────────────────────────────────────────
async function runDemo(ticker, question) {
  ticker = ticker.trim().toUpperCase().replace(/[^A-Z0-9.=-]/g, '');
  if (!ticker) return;

  const body = $('output-body');
  const title = $('output-title');
  title.textContent = `research/${ticker}`;

  // Show shimmer
  body.innerHTML = `
    <div class="shimmer" style="height:18px;width:80%;margin-bottom:14px;border-radius:3px;background:linear-gradient(90deg,#1a1d28 25%,#242836 50%,#1a1d28 75%);background-size:200% 100%;animation:shimmer 1.5s infinite"></div>
    <div class="shimmer" style="height:18px;width:65%;margin-bottom:14px;border-radius:3px;background:linear-gradient(90deg,#1a1d28 25%,#242836 50%,#1a1d28 75%);background-size:200% 100%;animation:shimmer 1.5s infinite"></div>
    <div class="shimmer" style="height:18px;width:70%;margin-bottom:14px;border-radius:3px;background:linear-gradient(90deg,#1a1d28 25%,#242836 50%,#1a1d28 75%);background-size:200% 100%;animation:shimmer 1.5s infinite"></div>
    <div class="shimmer" style="height:18px;width:50%;margin-bottom:14px;border-radius:3px;background:linear-gradient(90deg,#1a1d28 25%,#242836 50%,#1a1d28 75%);background-size:200% 100%;animation:shimmer 1.5s infinite"></div>
    <div class="shimmer" style="height:18px;width:75%;border-radius:3px;background:linear-gradient(90deg,#1a1d28 25%,#242836 50%,#1a1d28 75%);background-size:200% 100%;animation:shimmer 1.5s infinite"></div>
  `;

  // Scroll demo into view
  $('demo').scrollIntoView({ behavior: 'smooth', block: 'center' });

  try {
    // Always fetch demo first
    const demoRes = await fetch(`/api/demo/${ticker}`);
    const demo = await demoRes.json();

    if (demo.error && !demo.quant) {
      body.innerHTML = `<div class="output-section"><span class="output-key">ERROR</span><span class="output-val" style="color:var(--red)">${demo.error}</span></div>`;
      return;
    }

    const stock = demo.stock || {};
    const quant = demo.quant || {};

    // Build output
    let html = '';

    // SITUATION
    const price = stock.price ? `$${parseFloat(stock.price).toFixed(2)}` : '—';
    const pe = stock.pe_forward || stock.pe_trailing || '—';
    const revGrowth = quant.revenue_growth || '—';
    html += `<div class="output-section">
      <span class="output-key">SITUATION</span>
      <span class="output-val">${stock.name || ticker} at <span class="mono">${price}</span>. Forward P/E <span class="mono">${pe}</span>.<br>Revenue growth <span class="mono">${typeof revGrowth === 'number' ? revGrowth.toFixed(1) + '%' : revGrowth}</span>. Sector: ${stock.sector || '—'}.</span>
    </div>`;

    // BRENT GATE
    try {
      const br = await fetch('/api/brent');
      if (br.ok) {
        const bd = await br.json();
        if (!bd.error) {
          const bp = parseFloat(bd.brent_price || 0);
          const sig = bd.signal || '—';
          const gateOpen = bd.gate_open;
          html += `<div class="output-section">
            <span class="output-key">BRENT GATE</span>
            <span class="output-val"><span class="mono">$${bp.toFixed(2)}</span> · ${sig} zone · New positions: <span class="output-amber">${gateOpen ? 'OPEN' : 'BLOCKED'}</span><br>${bd.action || ''}</span>
          </div>`;
        }
      }
    } catch (_) {}

    // QUANT READ
    const dcf = quant.dcf_intrinsic_value != null ? `$${parseFloat(quant.dcf_intrinsic_value).toFixed(2)}` : '—';
    const dcfUp = quant.dcf_upside_pct != null ? `${parseFloat(quant.dcf_upside_pct).toFixed(1)}% ${parseFloat(quant.dcf_upside_pct) >= 0 ? 'upside' : 'downside'}` : '';
    const roic = quant.roic != null ? `${parseFloat(quant.roic).toFixed(1)}%` : '—';
    const sharpe = quant.sharpe_ratio != null ? parseFloat(quant.sharpe_ratio).toFixed(2) : '—';
    const beta = quant.beta != null ? parseFloat(quant.beta).toFixed(2) : '—';

    html += `<div class="output-section">
      <span class="output-key">QUANT READ</span>
      <span class="output-val">DCF Value: <span class="mono">${dcf}</span> (${dcfUp}). ROIC: <span class="mono">${roic}</span>.<br>Sharpe: <span class="mono">${sharpe}</span>. Beta: <span class="mono">${beta}</span>.</span>
    </div>`;

    // FLAGS
    const flags = quant.quant_flags || [];
    if (flags.length > 0) {
      const flagHtml = flags.slice(0, 4).map(f => {
        const isGood = /cleared|attractive|strong|undervalued|upside/i.test(f);
        const isBad = /weak|negative|expensive|thin|downside|below/i.test(f);
        const pill = isGood ? 'pill-green' : isBad ? 'pill-red' : 'pill-amber';
        const icon = isGood ? '✅' : isBad ? '⚠️' : '—';
        return `${icon} ${f}`;
      }).join('<br>');
      html += `<div class="output-section">
        <span class="output-key">SIGNALS</span>
        <span class="output-val">${flagHtml}</span>
      </div>`;
    }

    // If we have a key, try full research
    if (S.key) {
      const url = `/api/research/${ticker}${question ? `?question=${encodeURIComponent(question)}` : ''}`;
      try {
        const rr = await fetch(url, { headers: { 'X-Agent-Key': S.key } });
        if (rr.ok) {
          const res = await rr.json();
          if (res.report) {
            html += `<div class="output-section" style="grid-template-columns:1fr;border-top:1px solid #1E2535;padding-top:18px;margin-top:8px">
              <span class="output-val" style="color:rgba(255,255,255,.65)">${typeof marked !== 'undefined' ? marked.parse(res.report) : res.report.replace(/\n/g, '<br>')}</span>
            </div>`;
          }
        } else if (rr.status === 401) {
          html += `<div class="output-section"><span class="output-key">AUTH</span><span class="output-val" style="color:var(--red)">Invalid agent key.</span></div>`;
        }
      } catch (_) {}
    } else {
      html += `<div class="output-section">
        <span class="output-key">LOCKED</span>
        <span class="output-val" style="color:rgba(255,255,255,.3)">Full AI report requires an agent key. Quant data shown above is free.</span>
      </div>`;
    }

    html += '<div class="output-cursor"></div>';

    // Typewriter render
    body.innerHTML = '';
    const temp = document.createElement('div');
    temp.innerHTML = html;
    const sections = temp.children;

    let i = 0;
    function addSection() {
      if (i < sections.length) {
        const section = sections[0]; // always 0 because we move it
        section.style.opacity = '0';
        section.style.transform = 'translateY(8px)';
        body.appendChild(section);
        requestAnimationFrame(() => {
          section.style.transition = 'opacity .35s, transform .35s';
          section.style.opacity = '1';
          section.style.transform = 'none';
        });
        i++;
        setTimeout(addSection, 250);
      }
    }
    addSection();

  } catch (err) {
    body.innerHTML = `<div class="output-section"><span class="output-key">ERROR</span><span class="output-val" style="color:var(--red)">Analysis failed. Is the server running?</span></div>`;
  }
}

// ── Event wiring ────────────────────────────────────────────────────────
function wire() {
  // Demo inputs
  $('demo-run').addEventListener('click', () => {
    runDemo($('demo-ticker').value, $('demo-question').value);
  });
  $('demo-ticker').addEventListener('keydown', e => {
    if (e.key === 'Enter') runDemo($('demo-ticker').value, $('demo-question').value);
  });
  $('demo-ticker').addEventListener('input', function () {
    const p = this.selectionStart;
    this.value = this.value.toUpperCase().replace(/[^A-Z0-9.=-]/g, '');
    this.setSelectionRange(p, p);
  });

  // Closing CTA
  $('closing-btn').addEventListener('click', () => {
    const t = $('closing-ticker').value.trim().toUpperCase();
    if (t) {
      $('demo-ticker').value = t;
      runDemo(t, '');
    }
  });
  $('closing-ticker').addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      const t = $('closing-ticker').value.trim().toUpperCase();
      if (t) { $('demo-ticker').value = t; runDemo(t, ''); }
    }
  });
  $('closing-ticker').addEventListener('input', function () {
    const p = this.selectionStart;
    this.value = this.value.toUpperCase().replace(/[^A-Z0-9.=-]/g, '');
    this.setSelectionRange(p, p);
  });
}

// ── Init ────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  bootTopo();
  initNav();
  initReveal();
  initCodeTabs();
  initKeyDrawer();
  wire();
  fetchHeroData();
  setInterval(fetchHeroData, 60 * 1000);
});
