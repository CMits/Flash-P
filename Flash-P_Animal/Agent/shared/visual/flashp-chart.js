/*
 * flashp-chart.js — minimal dependency-free SVG line chart for the FLASH-P Studio.
 *
 * Replicates the look of the website's Recharts ConvergenceChart
 *   FLASHP_WEBSITE/Flash-P-AI/src/components/traits/simulate/ConvergenceChart.tsx
 * (dashed grid, dashed neutral reference line, the same COLORS palette, a node
 * legend, an "iteration" x-axis, and log₂ / fold / absolute y-modes) using only
 * inline SVG so the Studio stays a single self-contained file.
 *
 * Usage:
 *   FLASHPCHART.render(containerEl, { result, selectedNodes, normalizeBy, log2 });
 *   // result.iterations = [{ step, values:{id:val} }]
 *   // normalizeBy = wtValues (per-node baseline) or null  -> plot fold change
 *   // log2 = true -> plot log₂ of the fold change
 * Call again (e.g. on resize / selection change) to repaint; it clears first.
 */
(function (global) {
  'use strict';

  var COLORS = [
    '#2563eb', '#dc2626', '#16a34a', '#d97706', '#7c3aed',
    '#db2777', '#0891b2', '#ea580c', '#4f46e5', '#65a30d',
  ];
  var NS = 'http://www.w3.org/2000/svg';
  var HEIGHT = 340;

  function el(name, attrs) {
    var n = document.createElementNS(NS, name);
    if (attrs) for (var k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }
  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  function render(container, opts) {
    container.innerHTML = '';
    var result = opts.result;
    var selected = opts.selectedNodes || [];
    var normalizeBy = opts.normalizeBy || null;
    var log2 = !!opts.log2;

    if (!selected.length) {
      var msg = document.createElement('div');
      msg.style.cssText = 'height:' + HEIGHT + 'px;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:13px;';
      msg.textContent = 'Select nodes below to chart their trajectories.';
      container.appendChild(msg);
      return;
    }

    // Build per-step rows (mirrors ConvergenceChart's data transform).
    var rows = result.iterations.map(function (it) {
      var row = { step: it.step };
      for (var s = 0; s < selected.length; s++) {
        var node = selected[s];
        var raw = it.values[node] != null ? it.values[node] : 0;
        if (normalizeBy) {
          var b = normalizeBy[node];
          var ratio = (b != null && Math.abs(b) > 1e-6) ? raw / b : raw;
          row[node] = log2 ? Math.log2(Math.max(ratio, 1e-4)) : ratio;
        } else {
          row[node] = raw;
        }
      }
      return row;
    });

    // Neutral baseline: 0 for log₂, 1 for fold, 0 for RWR signals, 1 otherwise.
    var refY = log2 ? 0 : normalizeBy ? 1 : result.method === 'rwr' ? 0 : 1;

    // y-extent across all selected series (+ the reference line).
    var ymin = refY, ymax = refY;
    for (var i = 0; i < rows.length; i++) {
      for (var j = 0; j < selected.length; j++) {
        var y = rows[i][selected[j]];
        if (y < ymin) ymin = y;
        if (y > ymax) ymax = y;
      }
    }
    if (ymin === ymax) { ymin -= 1; ymax += 1; }
    var pad = (ymax - ymin) * 0.08;
    ymin -= pad; ymax += pad;
    var xmax = rows.length - 1 || 1;

    var width = Math.max(container.clientWidth || 0, 280);
    var legendH = 22;
    var M = { top: 10, right: 16, bottom: 30, left: log2 ? 52 : 46 };
    var plotW = width - M.left - M.right;
    var plotH = HEIGHT - legendH - M.top - M.bottom;

    var border = cssVar('--border', '#d7e0db');
    var muted = cssVar('--muted', '#5b6b63');

    function sx(step) { return M.left + (step / xmax) * plotW; }
    function sy(val) { return M.top + plotH - ((val - ymin) / (ymax - ymin)) * plotH; }

    var svg = el('svg', { width: width, height: HEIGHT, viewBox: '0 0 ' + width + ' ' + HEIGHT });
    svg.style.cssText = 'display:block;font-family:Arial,Helvetica,sans-serif;';

    // grid + y ticks
    var nTicks = 5;
    for (var t = 0; t <= nTicks; t++) {
      var val = ymin + (ymax - ymin) * (t / nTicks);
      var gy = sy(val);
      svg.appendChild(el('line', { x1: M.left, y1: gy, x2: M.left + plotW, y2: gy,
        stroke: border, 'stroke-dasharray': '3 3', 'stroke-width': 1 }));
      var lbl = el('text', { x: M.left - 6, y: gy + 3, 'text-anchor': 'end',
        'font-size': 11, fill: muted });
      lbl.textContent = fmtTick(val);
      svg.appendChild(lbl);
    }

    // x ticks (a handful of evenly spaced iteration labels)
    var xStep = Math.max(1, Math.round(xmax / 6));
    for (var xs = 0; xs <= xmax; xs += xStep) {
      var gx = sx(xs);
      var xl = el('text', { x: gx, y: M.top + plotH + 16, 'text-anchor': 'middle',
        'font-size': 11, fill: muted });
      xl.textContent = xs;
      svg.appendChild(xl);
    }
    var xAxisLabel = el('text', { x: M.left + plotW / 2, y: HEIGHT - legendH - 1,
      'text-anchor': 'middle', 'font-size': 11, fill: muted });
    xAxisLabel.textContent = 'iteration';
    svg.appendChild(xAxisLabel);

    if (log2) {
      var yLab = el('text', { x: 12, y: M.top + plotH / 2, 'text-anchor': 'middle',
        'font-size': 11, fill: muted, transform: 'rotate(-90 12 ' + (M.top + plotH / 2) + ')' });
      yLab.textContent = 'log₂ fold change';
      svg.appendChild(yLab);
    }

    // neutral reference line
    svg.appendChild(el('line', { x1: M.left, y1: sy(refY), x2: M.left + plotW, y2: sy(refY),
      stroke: muted, 'stroke-dasharray': '4 4', 'stroke-width': 1 }));

    // series
    for (var n = 0; n < selected.length; n++) {
      var node = selected[n];
      var d = '';
      for (var p = 0; p < rows.length; p++) {
        d += (p === 0 ? 'M' : 'L') + sx(rows[p].step).toFixed(1) + ' ' + sy(rows[p][node]).toFixed(1) + ' ';
      }
      svg.appendChild(el('path', { d: d, fill: 'none', stroke: COLORS[n % COLORS.length], 'stroke-width': 2 }));
    }

    // hover guideline + tooltip
    var guide = el('line', { x1: 0, y1: M.top, x2: 0, y2: M.top + plotH,
      stroke: muted, 'stroke-width': 1, opacity: 0 });
    svg.appendChild(guide);
    var tip = document.createElement('div');
    tip.style.cssText = 'position:absolute;pointer-events:none;display:none;z-index:20;background:var(--card);' +
      'border:1px solid var(--border);border-radius:8px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);';
    var overlay = el('rect', { x: M.left, y: M.top, width: plotW, height: plotH, fill: 'transparent' });
    overlay.style.cursor = 'crosshair';
    svg.appendChild(overlay);

    overlay.addEventListener('mousemove', function (ev) {
      var rect = svg.getBoundingClientRect();
      var px = ev.clientX - rect.left;
      var step = Math.round(((px - M.left) / plotW) * xmax);
      step = Math.max(0, Math.min(xmax, step));
      var row = rows[step];
      if (!row) return;
      guide.setAttribute('x1', sx(step)); guide.setAttribute('x2', sx(step));
      guide.setAttribute('opacity', 0.4);
      var html = '<div style="color:var(--muted);margin-bottom:2px;">iteration ' + step + '</div>';
      for (var q = 0; q < selected.length; q++) {
        html += '<div style="display:flex;gap:6px;align-items:center;"><span style="width:8px;height:8px;border-radius:2px;background:' +
          COLORS[q % COLORS.length] + ';"></span>' + esc(selected[q]) + ': <b>' + row[selected[q]].toFixed(3) + '</b></div>';
      }
      tip.innerHTML = html;
      tip.style.display = 'block';
      var left = sx(step) + 10;
      if (left + 140 > width) left = sx(step) - 150;
      tip.style.left = left + 'px';
      tip.style.top = (M.top + 4) + 'px';
    });
    overlay.addEventListener('mouseleave', function () {
      guide.setAttribute('opacity', 0);
      tip.style.display = 'none';
    });

    // legend
    var legend = document.createElement('div');
    legend.style.cssText = 'display:flex;flex-wrap:wrap;gap:10px;justify-content:center;font-size:11px;color:var(--fg);padding-top:2px;';
    for (var L = 0; L < selected.length; L++) {
      var item = document.createElement('span');
      item.style.cssText = 'display:inline-flex;align-items:center;gap:4px;';
      item.innerHTML = '<span style="width:12px;height:3px;border-radius:2px;background:' +
        COLORS[L % COLORS.length] + ';display:inline-block;"></span>' + esc(selected[L]);
      legend.appendChild(item);
    }

    var wrap = document.createElement('div');
    wrap.style.position = 'relative';
    wrap.appendChild(svg);
    wrap.appendChild(tip);
    container.appendChild(wrap);
    container.appendChild(legend);
  }

  function fmtTick(v) {
    if (Math.abs(v) >= 100) return v.toFixed(0);
    if (Math.abs(v) >= 10) return v.toFixed(1);
    return v.toFixed(2);
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  global.FLASHPCHART = { render: render, COLORS: COLORS };
})(typeof window !== 'undefined' ? window : this);
