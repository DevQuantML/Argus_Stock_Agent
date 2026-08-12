'use strict';

/* chart.js — SVG price chart and sparkline. No charting library.

   The main chart overlays reference lines (DCF intrinsic value, analyst
   target, your average cost, your stop loss) on the price series, so the
   valuation numbers are legible as positions rather than as bare figures. */

const NS = 'http://www.w3.org/2000/svg';

function el(tag, attrs = {}) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  return node;
}

const fmtPrice = (v) =>
  v >= 1000 ? v.toLocaleString('en-US', { maximumFractionDigits: 0 })
            : v.toFixed(2);

function fmtDate(iso, long = false) {
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('en-US',
    long ? { year: 'numeric', month: 'short', day: 'numeric' }
         : { month: 'short', year: '2-digit' });
}

/* Axis granularity follows the span. On a one-month chart every tick would
   otherwise render the same "Jul 26" and carry no information. */
function axisFormatter(points) {
  const start = new Date(points[0].t + 'T00:00:00');
  const end   = new Date(points[points.length - 1].t + 'T00:00:00');
  const days  = (end - start) / 86400000;

  if (days <= 120) {
    return (iso) => new Date(iso + 'T00:00:00')
      .toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }
  if (days <= 800) {
    return (iso) => new Date(iso + 'T00:00:00')
      .toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
  }
  return (iso) => new Date(iso + 'T00:00:00')
    .toLocaleDateString('en-US', { year: 'numeric' });
}

/* ── Sparkline ───────────────────────────────────────────────────────────
   Tiny inline trend line for portfolio rows. Returns an <svg> element. */

export function sparkline(values, { width = 96, height = 30, positive = true } = {}) {
  const svg = el('svg', {
    class: 'spark', width, height,
    viewBox: `0 0 ${width} ${height}`, 'aria-hidden': 'true',
  });
  if (!values || values.length < 2) return svg;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 3;
  const stepX = width / (values.length - 1);
  const y = (v) => height - pad - ((v - min) / span) * (height - pad * 2);

  const pts = values.map((v, i) => `${(i * stepX).toFixed(2)},${y(v).toFixed(2)}`);
  const tone = positive ? 'pos' : 'neg';

  // Colours come from CSS classes, not attributes — presentation attributes
  // don't resolve var(), so attribute colours would not follow the theme.
  svg.appendChild(el('polyline', {
    class: `spark-${tone}-fill`,
    points: `0,${height} ${pts.join(' ')} ${width},${height}`,
    stroke: 'none',
  }));
  svg.appendChild(el('polyline', {
    class: `spark-${tone}`,
    points: pts.join(' '),
    fill: 'none', 'stroke-width': 1.6,
    'stroke-linejoin': 'round', 'stroke-linecap': 'round',
  }));
  return svg;
}

/* ── Main price chart ────────────────────────────────────────────────────
   drawChart(container, history, { refs }) where refs is
   [{ label, value, color, key }]. Re-renders on container resize. */

export function drawChart(container, history, { refs = [] } = {}) {
  if (!container) return;
  container.innerHTML = '';

  const points = (history && history.points) || [];
  if (points.length < 2) {
    container.innerHTML = '<div class="chart-empty">No price history available for this ticker.</div>';
    return;
  }

  const W = Math.max(container.clientWidth || 720, 320);
  const H = 320;
  const M = { top: 16, right: 62, bottom: 26, left: 12 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;

  const closes = points.map(p => p.c);
  let lo = Math.min(...closes);
  let hi = Math.max(...closes);

  /* Reference lines expand the y-domain, but only within half the price
     range on either side. A DCF value 4x the price would otherwise flatten
     the actual series into a straight line — those refs are reported as
     off-scale in the legend instead. */
  const priceSpan = hi - lo || hi * 0.1 || 1;
  const allowLo = lo - priceSpan * 0.5;
  const allowHi = hi + priceSpan * 0.5;

  const usable = [];
  for (const r of refs) {
    const v = Number(r.value);
    if (!Number.isFinite(v)) continue;
    if (v >= allowLo && v <= allowHi) {
      usable.push({ ...r, value: v, onScale: true });
      lo = Math.min(lo, v);
      hi = Math.max(hi, v);
    } else {
      usable.push({ ...r, value: v, onScale: false });
    }
  }

  const padY = (hi - lo) * 0.08 || hi * 0.05 || 1;
  const yMin = lo - padY;
  const yMax = hi + padY;
  const ySpan = yMax - yMin || 1;

  const x = (i) => M.left + (i / (points.length - 1)) * plotW;
  const y = (v) => M.top + (1 - (v - yMin) / ySpan) * plotH;

  const svg = el('svg', {
    class: 'pchart', width: '100%', height: H,
    viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': `Price history, ${points.length} sessions`,
  });

  // Gradient under the line — stop colours themed via .gstop-a/.gstop-b
  const defs = el('defs');
  const grad = el('linearGradient', { id: 'chartFill', x1: '0', y1: '0', x2: '0', y2: '1' });
  grad.appendChild(el('stop', { class: 'gstop-a', offset: '0%' }));
  grad.appendChild(el('stop', { class: 'gstop-b', offset: '100%' }));
  defs.appendChild(grad);
  svg.appendChild(defs);

  // ── Y gridlines + right-hand price labels ─────────────────────────────
  const TICKS = 5;
  for (let i = 0; i <= TICKS; i++) {
    const v  = yMin + (ySpan * i) / TICKS;
    const yy = y(v);
    svg.appendChild(el('line', {
      class: 'grid-line',
      x1: M.left, y1: yy, x2: M.left + plotW, y2: yy,
    }));
    const lbl = el('text', {
      x: M.left + plotW + 8, y: yy + 3.5,
      class: 'ax-lbl', 'text-anchor': 'start',
    });
    lbl.textContent = fmtPrice(v);
    svg.appendChild(lbl);
  }

  // ── X date labels ─────────────────────────────────────────────────────
  const fmtAxis = axisFormatter(points);
  const xTicks = Math.min(6, points.length);
  for (let i = 0; i < xTicks; i++) {
    const idx = Math.round((i / (xTicks - 1)) * (points.length - 1));
    const t = el('text', {
      x: x(idx), y: H - 8, class: 'ax-lbl',
      'text-anchor': i === 0 ? 'start' : i === xTicks - 1 ? 'end' : 'middle',
    });
    t.textContent = fmtAxis(points[idx].t);
    svg.appendChild(t);
  }

  // ── Reference lines ───────────────────────────────────────────────────
  for (const r of usable) {
    if (!r.onScale) continue;
    const yy = y(r.value);
    const cls = r.key ? `ref-${r.key}` : '';
    svg.appendChild(el('line', {
      class: cls,
      x1: M.left, y1: yy, x2: M.left + plotW, y2: yy,
      'stroke-width': 1.2, 'stroke-dasharray': '5 4', opacity: '.8',
    }));
    const tag = el('text', { x: M.left + 6, y: yy - 6, class: `ref-lbl ${cls}` });
    tag.textContent = `${r.label} ${fmtPrice(r.value)}`;
    svg.appendChild(tag);
  }

  // ── Price series ──────────────────────────────────────────────────────
  const linePts = points.map((p, i) => `${x(i).toFixed(2)},${y(p.c).toFixed(2)}`).join(' ');
  const baseY = M.top + plotH;
  svg.appendChild(el('polygon', {
    points: `${M.left},${baseY} ${linePts} ${M.left + plotW},${baseY}`,
    fill: 'url(#chartFill)', stroke: 'none',
  }));
  svg.appendChild(el('polyline', {
    class: 'price-line', points: linePts,
    'stroke-linejoin': 'round', 'stroke-linecap': 'round',
  }));

  // Last-price dot
  const lastX = x(points.length - 1);
  const lastY = y(points[points.length - 1].c);
  svg.appendChild(el('circle', { class: 'last-halo', cx: lastX, cy: lastY, r: 4.5 }));
  svg.appendChild(el('circle', { class: 'last-dot',  cx: lastX, cy: lastY, r: 2.5 }));

  // ── Hover crosshair ───────────────────────────────────────────────────
  const cross = el('g', { class: 'crosshair', opacity: '0' });
  const vline = el('line', {
    class: 'cross-line',
    y1: M.top, y2: M.top + plotH, 'stroke-dasharray': '3 3',
  });
  const dot = el('circle', { class: 'cross-dot', r: 4 });
  cross.appendChild(vline);
  cross.appendChild(dot);
  svg.appendChild(cross);

  const hit = el('rect', {
    x: M.left, y: M.top, width: plotW, height: plotH,
    fill: 'transparent', style: 'cursor:crosshair',
  });
  svg.appendChild(hit);

  const tip = document.createElement('div');
  tip.className = 'chart-tip';
  tip.hidden = true;

  const wrap = document.createElement('div');
  wrap.className = 'chart-inner';
  wrap.appendChild(svg);
  wrap.appendChild(tip);
  container.appendChild(wrap);

  const move = (evt) => {
    const box = svg.getBoundingClientRect();
    const clientX = evt.touches ? evt.touches[0].clientX : evt.clientX;
    // Map screen px → viewBox units; the SVG scales to 100% width.
    const vbX = ((clientX - box.left) / box.width) * W;
    const ratio = (vbX - M.left) / plotW;
    const idx = Math.max(0, Math.min(points.length - 1, Math.round(ratio * (points.length - 1))));
    const p = points[idx];

    cross.setAttribute('opacity', '1');
    vline.setAttribute('x1', x(idx));
    vline.setAttribute('x2', x(idx));
    dot.setAttribute('cx', x(idx));
    dot.setAttribute('cy', y(p.c));

    tip.hidden = false;
    tip.innerHTML =
      `<span class="tip-date">${fmtDate(p.t, true)}</span>` +
      `<span class="tip-price">${fmtPrice(p.c)}</span>`;

    const leftPx = (x(idx) / W) * box.width;
    const flip = leftPx > box.width - 130;
    tip.style.left = `${flip ? leftPx - 12 : leftPx + 12}px`;
    tip.style.transform = flip ? 'translateX(-100%)' : 'none';
    tip.style.top = `${Math.max(4, (y(p.c) / H) * box.height - 46)}px`;
  };

  const leave = () => { cross.setAttribute('opacity', '0'); tip.hidden = true; };

  hit.addEventListener('mousemove', move);
  hit.addEventListener('mouseleave', leave);
  hit.addEventListener('touchmove', move, { passive: true });
  hit.addEventListener('touchend', leave);

  // ── Legend, including any off-scale references ────────────────────────
  if (usable.length) {
    const legend = document.createElement('div');
    legend.className = 'chart-legend';
    for (const r of usable) {
      const item = document.createElement('span');
      item.className = 'cl-item' + (r.onScale ? '' : ' off');
      item.innerHTML =
        `<i class="cl-swatch ref-${r.key || ''}"></i>${r.label} ` +
        `<b>${fmtPrice(r.value)}</b>` +
        (r.onScale ? '' : ' <em>(off scale)</em>');
      legend.appendChild(item);
    }
    container.appendChild(legend);
  }
}

/* Redraw on resize — the SVG is laid out in absolute viewBox units, so it
   needs a real re-render rather than a CSS stretch. */
export function makeResponsive(container, redraw) {
  let last = container.clientWidth;
  let timer = null;
  const onResize = () => {
    const now = container.clientWidth;
    if (Math.abs(now - last) < 24) return;
    last = now;
    clearTimeout(timer);
    timer = setTimeout(redraw, 120);
  };
  window.addEventListener('resize', onResize, { passive: true });
  return () => window.removeEventListener('resize', onResize);
}
