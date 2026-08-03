/*!
 * flashp-prop-view.js — the Visual Propagation view.
 *
 * A port of the website's PropagationCanvas.tsx / PropagateClient.tsx / StepControls.tsx /
 * ColorScaleLegend.tsx from React to plain DOM. The canvas was already imperative there —
 * it writes SVG attributes directly every frame and deliberately bypasses React's render —
 * so the drawing code carries over almost unchanged. What is rewritten is the scaffolding
 * that React used to own: element creation, and a small explicit state object in place of
 * hooks.
 *
 * Usage:
 *   FLASHPPROPVIEW.open(hostEl, {network, result, method, perturbation, style, dark})
 *     -> Promise<{setTheme, destroy}>
 *
 * Depends on: window.FLASHPPROP (flashp-prop-core.js), window.FLASHPSIM.
 */
(function (global) {
  'use strict';

  var P = global.FLASHPPROP;
  var SVGNS = 'http://www.w3.org/2000/svg';
  var MONO = 'ui-monospace, Menlo, Consolas, monospace';

  /*
   * Signal colours.
   *
   * Dark mode keeps the near-white comet, which reads as light travelling down a wire
   * against the dark canvas. That same near-white is invisible on the light canvas, so
   * light mode uses the brand orange instead — bright enough to carry against white while
   * staying on-brand.
   *
   * The travelling pulse doesn't need to encode the edge's sign: the line colour and the
   * arrowhead (triangle vs bar) already do that, and a single accent reads more clearly as
   * "this is the signal moving".
   */
  var EDGE_LIGHT = {
    pos: { line: '#5b93b8', flow: '#ffc08a', spark: '#ff7a1a', ripple: '#e8620a' },
    neg: { line: '#dd6f45', flow: '#ffc08a', spark: '#ff7a1a', ripple: '#e8620a' }
  };
  var EDGE_DARK = {
    pos: { line: '#9ecae1', flow: '#dcf0f9', spark: '#eaf6fc', ripple: '#9ecae1' },
    neg: { line: '#f46d43', flow: '#ffbfa6', spark: '#ffe0d3', ripple: '#f46d43' }
  };

  /** Perturbation ring colours — the same set the Simulate view already uses. */
  var RING = { ko: '#d7301f', kd: '#fc8d59', oe: '#2c7fb8', exo: '#2ca25f' };

  /** Above this many simultaneous comets we fall back to a static highlight. */
  var MAX_COMETS = 60;

  var PULSE_MS = 650;
  var MORPH_MS = 520;
  var MORPH_DELAY = 300;

  /** Wall-clock per step during autoplay, divided by the speed multiplier. */
  var STEP_MS = 1250;
  var SPEEDS = [0.5, 1, 2];

  var LS = 'flashp-prop-';

  // ---- tiny DOM helpers ----------------------------------------------------

  function svg(tag, attrs) {
    var e = document.createElementNS(SVGNS, tag);
    if (attrs) {
      for (var k in attrs) {
        if (attrs[k] != null) e.setAttribute(k, String(attrs[k]));
      }
    }
    return e;
  }

  function hel(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  /** Resolve a discrete vizmap mapping ({default, by}) — same helper as flashp-graph.js. */
  function pick(map, key) {
    return map && map.by && map.by[key] != null ? map.by[key] : map && map.default;
  }

  function lsGet(key, fallback) {
    try {
      var v = global.localStorage.getItem(LS + key);
      return v == null ? fallback : JSON.parse(v);
    } catch (e) {
      return fallback;
    }
  }

  function lsSet(key, value) {
    try {
      global.localStorage.setItem(LS + key, JSON.stringify(value));
    } catch (e) { /* private mode — the toggle still works, it just won't persist */ }
  }

  function clampZoom(k) {
    return Math.min(Math.max(k, 0.25), 4);
  }

  // =========================================================================
  // canvas
  // =========================================================================

  /*
   * cfg: { nodes, edges, layout, style, method, perturbation, onSelect }
   *
   * `nodes` / `edges` are the *visible* sets; `layout` always covers the whole network, so
   * toggling focus hides elements without moving anything that stays.
   */
  function createCanvas(host, cfg) {
    var nodes = cfg.nodes;
    var edges = cfg.edges;
    var layout = cfg.layout;
    var method = cfg.method;
    var perturbation = cfg.perturbation;

    var nodeRefs = {};   // id -> {group, scale, body, label, value, glyph, ring, strip}
    var edgeRefs = {};   // id -> {line, flow, head}
    var geo = {};        // id -> {points, cum, total}

    /*
     * Values currently on screen. Not part of the declarative state: these change every
     * frame, and the DOM writes are targeted and cheap.
     */
    var shown = {};
    var token = 0;

    var dark = !!cfg.dark;
    var sizeByValue = !!cfg.sizeByValue;
    var selected = null;
    var view = { x: 0, y: 0, k: 1 };
    var values = {};
    var baseline = {};
    var drag = null;
    var compact = false;
    var hideStrip = false;

    var palette = dark ? EDGE_DARK : EDGE_LIGHT;
    var inertColor = dark ? '#3b4a50' : '#c3cfc8';

    var nodeById = {};
    var i;
    for (i = 0; i < nodes.length; i++) nodeById[nodes[i].id] = nodes[i];

    var visibleEdges = [];
    for (i = 0; i < edges.length; i++) {
      var e = edges[i];
      if (layout.edges[e.id] && nodeById[e.s] && nodeById[e.t]) visibleEdges.push(e);
    }
    for (i = 0; i < visibleEdges.length; i++) {
      var raw = layout.edges[visibleEdges[i].id].points;
      var m = P.measure(raw);
      geo[visibleEdges[i].id] = { points: raw, cum: m.cum, total: m.total };
    }

    function neutral(id) {
      return baseline[id] != null ? baseline[id] : method === 'rwr' ? 0 : 1;
    }

    // ---- extent -----------------------------------------------------------

    var pts = [];
    for (i = 0; i < nodes.length; i++) {
      var lp = layout.nodes[nodes[i].id];
      if (!lp) continue;
      pts.push({ x: lp.x - P.BODY_W, y: lp.y - P.BODY_H });
      pts.push({ x: lp.x + P.BODY_W, y: lp.y + P.STRIP_TOP + P.BOX_H + 8 });
    }
    for (i = 0; i < visibleEdges.length; i++) {
      var rp = layout.edges[visibleEdges[i].id].points;
      for (var q = 0; q < rp.length; q++) pts.push(rp[q]);
    }
    var extent = P.bounds(pts, 44);
    var cx = extent.x + extent.w / 2;
    var cy = extent.y + extent.h / 2;

    // ---- scaffolding ------------------------------------------------------

    var wrap = hel('div', 'prop-canvas');
    var root = svg('svg', {
      viewBox: extent.x + ' ' + extent.y + ' ' + extent.w + ' ' + extent.h,
      role: 'application',
      'aria-roledescription': 'propagation playground',
      'aria-label': 'Network graph showing how the perturbation propagates'
    });
    root.setAttribute('class', 'prop-svg');

    var defs = svg('defs');
    var filter = svg('filter', { id: 'fp-soft', x: '-80%', y: '-80%', width: '260%', height: '260%' });
    filter.appendChild(svg('feGaussianBlur', { stdDeviation: '3.2', result: 'b' }));
    var merge = svg('feMerge');
    merge.appendChild(svg('feMergeNode', { in: 'b' }));
    merge.appendChild(svg('feMergeNode', { in: 'SourceGraphic' }));
    filter.appendChild(merge);
    defs.appendChild(filter);
    root.appendChild(defs);

    var zoomG = svg('g');
    var edgeG = svg('g');
    var fxG = svg('g', { pointerEvents: 'none' });
    var nodeG = svg('g');
    zoomG.appendChild(edgeG);
    zoomG.appendChild(fxG);
    zoomG.appendChild(nodeG);
    root.appendChild(zoomG);
    wrap.appendChild(root);

    var zoomBar = hel('div', 'prop-zoom');
    zoomBar.appendChild(canvasBtn('Zoom in', '＋', function () { zoom(1.25); }));
    zoomBar.appendChild(canvasBtn('Zoom out', '－', function () { zoom(1 / 1.25); }));
    zoomBar.appendChild(canvasBtn('Fit to view', '⤢', function () { fit(); }));
    wrap.appendChild(zoomBar);

    var compactHint = hel('div', 'prop-hint', 'Zoom in to read the value boxes');
    compactHint.style.display = 'none';
    wrap.appendChild(compactHint);

    host.appendChild(wrap);

    function canvasBtn(label, glyph, fn) {
      var b = hel('button', 'prop-zoombtn', glyph);
      b.type = 'button';
      b.title = label;
      b.setAttribute('aria-label', label);
      b.onclick = fn;
      return b;
    }

    // ---- build edges + nodes once -----------------------------------------

    for (i = 0; i < visibleEdges.length; i++) buildEdge(visibleEdges[i]);
    for (i = 0; i < nodes.length; i++) buildNode(nodes[i]);

    function buildEdge(e) {
      var g = svg('g');
      var line = svg('path', {
        fill: 'none', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'
      });
      if (!e.carriesSignal) {
        line.setAttribute('stroke-dasharray', '4 4');
        var t = svg('title');
        t.textContent = e.s + ' → ' + e.t + ': in the network, but not used by the FLASH-P ' +
          'equations for ' + e.t + ' — no signal travels along it under this method.';
        line.appendChild(t);
      }
      var flow = svg('path', {
        fill: 'none', 'stroke-width': 3.4, 'stroke-linecap': 'round',
        'stroke-linejoin': 'round', opacity: 0, pointerEvents: 'none'
      });
      var head = svg('path', {
        'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
        pointerEvents: 'none'
      });
      g.appendChild(line);
      g.appendChild(flow);
      g.appendChild(head);
      edgeG.appendChild(g);
      edgeRefs[e.id] = { line: line, flow: flow, head: head };
    }

    function buildNode(n) {
      var p = layout.nodes[n.id];
      if (!p) return;
      var g = svg('g', { transform: 'translate(' + p.x + ' ' + p.y + ')', tabindex: 0, role: 'button' });
      g.setAttribute('aria-label', n.id + (n.fn ? ', ' + n.fn : ''));
      g.setAttribute('class', 'prop-node');

      var ring = svg('rect', {
        x: -P.BODY_W / 2 - 6, y: -P.BODY_H / 2 - 6,
        width: P.BODY_W + 12, height: P.BODY_H + 12,
        rx: 12, fill: 'none', stroke: 'var(--primary)', 'stroke-width': 2.5,
        pointerEvents: 'none'
      });
      ring.style.display = 'none';
      g.appendChild(ring);

      var scaleG = svg('g', { transform: 'scale(1)' });
      var body = svg('rect', {
        x: -P.BODY_W / 2, y: -P.BODY_H / 2, width: P.BODY_W, height: P.BODY_H, rx: 8
      });
      var label = svg('text', {
        'text-anchor': 'middle', 'dominant-baseline': 'central',
        'font-size': n.id.length > 13 ? 8.5 : 11, 'font-weight': 600, pointerEvents: 'none'
      });
      if ((n.ty || 'GENE') === 'GENE') label.setAttribute('font-style', 'italic');
      label.textContent = n.id.length > 18 ? n.id.slice(0, 17) + '…' : n.id;
      scaleG.appendChild(body);
      scaleG.appendChild(label);
      g.appendChild(scaleG);

      var strip = svg('g', { pointerEvents: 'none' });
      g.appendChild(strip);

      g.addEventListener('click', function (ev) {
        ev.stopPropagation();
        setSelected(selected === n.id ? null : n.id);
        if (cfg.onSelect) cfg.onSelect(selected);
      });
      g.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          setSelected(selected === n.id ? null : n.id);
          if (cfg.onSelect) cfg.onSelect(selected);
        }
      });

      nodeG.appendChild(g);
      nodeRefs[n.id] = { group: g, scale: scaleG, body: body, label: label, ring: ring, strip: strip };
    }

    /*
     * The three value boxes under a node.
     *
     * Cells 1 and 2 are inputs the user set and that never change for the whole run; cell 3
     * is the state that evolves. Keeping that contrast visible is the whole pedagogical
     * point, so the first two render flat and static while only the third animates.
     */
    function buildStrip(n) {
      var refs = nodeRefs[n.id];
      if (!refs) return;
      var strip = refs.strip;
      while (strip.firstChild) strip.removeChild(strip.firstChild);
      refs.value = null;
      refs.glyph = null;
      if (hideStrip) return;

      var cellFill = dark ? '#16262b' : '#f4f6f4';
      var cellStroke = dark ? '#24383f' : '#dfe6e0';
      var muted = dark ? '#93a7a0' : '#6b7c76';
      var ink = dark ? '#e7efe9' : '#13251c';
      var rwr = method === 'rwr';
      var y = P.STRIP_TOP;

      var mod = perturbation.geneModifiers[n.id];
      var exo = perturbation.exogenous[n.id];

      if (compact) {
        strip.appendChild(svg('rect', {
          x: -32, y: y, width: 64, height: P.BOX_H, rx: 5, fill: cellFill, stroke: cellStroke
        }));
        var cv = svg('text', {
          x: -4, y: y + 17, 'text-anchor': 'middle', 'font-size': 12,
          'font-weight': 700, fill: ink, 'font-family': MONO
        });
        var cg = svg('text', {
          x: 20, y: y + 17, 'text-anchor': 'middle', 'font-size': 10, fill: muted
        });
        strip.appendChild(cv);
        strip.appendChild(cg);
        refs.value = cv;
        refs.glyph = cg;
        return;
      }

      /*
       * RWR has no multiplicative modifier and no additive supply — both are folded into
       * one starting signal — so these cells say what RWR actually does rather than showing
       * a borrowed number that means nothing.
       */
      var seed;
      if (rwr && mod !== undefined) {
        seed = mod === 0 ? -1 : mod === 0.5 ? -0.5 : mod >= 2 ? 1 : mod - 1;
      }
      var cell1 = rwr
        ? { text: seed !== undefined ? (seed >= 0 ? '+' + seed : '' + seed) : '—', caption: 'seed' }
        : { text: '×' + (mod == null ? 1 : mod), caption: 'mod' };
      var cell2 = rwr
        ? { text: exo ? '+' + Math.min(exo, 1) : '—', caption: 'supply' }
        : { text: '+' + (exo == null ? 0 : exo), caption: 'exo' };

      strip.appendChild(cell(-48, y, cell1.text, cell1.caption,
        rwr ? seed !== undefined : (mod == null ? 1 : mod) !== 1, cellFill, cellStroke, muted));
      strip.appendChild(cell(-16, y, cell2.text, cell2.caption,
        (exo == null ? 0 : exo) !== 0, cellFill, cellStroke, muted));

      var g3 = svg('g');
      g3.appendChild(svg('rect', {
        x: 16, y: y, width: 32, height: P.BOX_H, rx: 5, fill: cellFill, stroke: cellStroke
      }));
      var v3 = svg('text', {
        x: 29, y: y + 11.5, 'text-anchor': 'middle', 'font-size': 10,
        'font-weight': 700, fill: ink, 'font-family': MONO
      });
      var g3g = svg('text', {
        x: 43, y: y + 11.5, 'text-anchor': 'middle', 'font-size': 8, fill: muted
      });
      var cap = svg('text', {
        x: 32, y: y + 21, 'text-anchor': 'middle', 'font-size': 6.5, fill: muted
      });
      cap.textContent = rwr ? 'signal' : 'value';
      g3.appendChild(v3);
      g3.appendChild(g3g);
      g3.appendChild(cap);
      strip.appendChild(g3);
      refs.value = v3;
      refs.glyph = g3g;

      function cell(x, yy, text, caption, emphasis, fill, stroke, mutedCol) {
        var g = svg('g');
        g.appendChild(svg('rect', {
          x: x, y: yy, width: 32, height: P.BOX_H, rx: 5, fill: fill, stroke: stroke
        }));
        var t = svg('text', {
          x: x + 16, y: yy + 11.5, 'text-anchor': 'middle', 'font-size': 9.5,
          'font-weight': emphasis ? 700 : 500,
          fill: emphasis ? (dark ? '#ffd7a8' : '#8a4b00') : dark ? '#b9cbc5' : '#4b5b55',
          'font-family': MONO
        });
        t.textContent = text;
        var c = svg('text', {
          x: x + 16, y: yy + 21, 'text-anchor': 'middle', 'font-size': 6.5, fill: mutedCol
        });
        c.textContent = caption;
        g.appendChild(t);
        g.appendChild(c);
        return g;
      }
    }

    // ---- painting ---------------------------------------------------------

    function boxFor(id) {
      var v = shown[id] != null ? shown[id] : neutral(id);
      var s = sizeByValue ? P.radiusFactor(v, neutral(id), method) : 1;
      return { hw: (P.BODY_W * s) / 2, hh: (P.BODY_H * s) / 2 };
    }

    function trimmedFor(e) {
      var g = geo[e.id];
      var sp = layout.nodes[e.s];
      var tp = layout.nodes[e.t];
      if (!g || !sp || !tp) return null;
      return P.trimRoute(g.points, sp, tp, boxFor(e.s), boxFor(e.t), 5);
    }

    /** Write current values straight to the DOM. Called every animation frame. */
    function paint() {
      var j;
      for (j = 0; j < nodes.length; j++) {
        var n = nodes[j];
        var refs = nodeRefs[n.id];
        if (!refs) continue;
        var base = neutral(n.id);
        var v = shown[n.id] != null ? shown[n.id] : base;
        var fill = P.fillFor(v, base, method, dark);

        if (refs.body) refs.body.setAttribute('fill', fill);
        if (refs.label) refs.label.setAttribute('fill', P.labelInk(fill));
        if (refs.value) refs.value.textContent = P.formatValue(v, method);
        if (refs.glyph) refs.glyph.textContent = P.directionGlyph(v, base, method);

        var s = sizeByValue ? P.radiusFactor(v, base, method) : 1;
        if (refs.scale) refs.scale.setAttribute('transform', 'scale(' + s.toFixed(3) + ')');
      }

      // Edges re-trim against the *current* body size, so arrowheads stay flush against the
      // node boundary while nodes breathe.
      for (j = 0; j < visibleEdges.length; j++) {
        var e = visibleEdges[j];
        var er = edgeRefs[e.id];
        var trimmed = trimmedFor(e);
        if (!er || !trimmed) continue;
        var d = P.pathD(trimmed);
        if (er.line) er.line.setAttribute('d', d);
        if (er.flow) er.flow.setAttribute('d', d);
        if (er.head) {
          er.head.setAttribute('d',
            P.headD(trimmed[trimmed.length - 1], P.endAngle(trimmed), e.sign));
        }
      }
    }

    /** Static styling that only changes with theme, zoom or selection. */
    function applyChrome() {
      var j;
      palette = dark ? EDGE_DARK : EDGE_LIGHT;
      inertColor = dark ? '#3b4a50' : '#c3cfc8';

      root.style.backgroundImage = 'radial-gradient(circle at 1px 1px, ' +
        (dark ? 'rgba(120,150,140,0.16)' : '#e3ebe6') + ' 1px, transparent 0)';
      root.style.backgroundSize = '22px 22px';

      for (j = 0; j < visibleEdges.length; j++) {
        var e = visibleEdges[j];
        var er = edgeRefs[e.id];
        if (!er) continue;
        var col = e.sign > 0 ? palette.pos : palette.neg;
        var inert = !e.carriesSignal;
        var stroke = inert ? inertColor : col.line;
        er.line.setAttribute('stroke', stroke);
        er.line.setAttribute('opacity', inert ? 0.55 : 0.68);
        er.flow.setAttribute('stroke', col.flow);
        er.head.setAttribute('fill', e.sign > 0 ? stroke : 'none');
        er.head.setAttribute('stroke', stroke);
        er.head.setAttribute('opacity', inert ? 0.55 : 0.68);
      }

      for (j = 0; j < nodes.length; j++) {
        var n = nodes[j];
        var refs = nodeRefs[n.id];
        if (!refs) continue;
        var mod = perturbation.geneModifiers[n.id];
        var exo = perturbation.exogenous[n.id];
        var ringCol =
          mod === 0 ? RING.ko
          : mod === 0.5 ? RING.kd
          : (mod !== undefined && mod >= 2) ? RING.oe
          : exo ? RING.exo
          : null;
        refs.body.setAttribute('stroke', ringCol || String(pick(cfg.style.node.border, n.ty || 'GENE')));
        refs.body.setAttribute('stroke-width', ringCol ? 3.5 : 1.8);
        refs.ring.style.display = selected === n.id ? '' : 'none';
      }
    }

    function applyZoom() {
      zoomG.setAttribute('transform',
        'translate(' + cx + ' ' + cy + ') scale(' + view.k + ') translate(' +
        (-cx + view.x) + ' ' + (-cy + view.y) + ')');

      // Below these the strip stops being readable, so it degrades rather than turning into
      // unreadable smudges.
      var nextCompact = view.k < 0.55;
      var nextHide = view.k < 0.35;
      if (nextCompact !== compact || nextHide !== hideStrip) {
        compact = nextCompact;
        hideStrip = nextHide;
        for (var j = 0; j < nodes.length; j++) buildStrip(nodes[j]);
        paint();
      }
      compactHint.style.display = compact ? '' : 'none';
    }

    // ---- effects layer, fully imperative ----------------------------------

    function ripple(at, col, isCurrent, speed) {
      var c = svg('circle', { cx: at.x, cy: at.y, fill: 'none', stroke: col });
      fxG.appendChild(c);
      var r0 = P.BODY_W / 2;
      function done() { if (c.parentNode) c.parentNode.removeChild(c); }
      P.tween(620, isCurrent, function (p) {
        c.setAttribute('r', (r0 + 26 * p).toFixed(1));
        c.setAttribute('opacity', (0.75 * (1 - p)).toFixed(2));
        c.setAttribute('stroke-width', (2.4 * (1 - p * 0.65)).toFixed(1));
      }, { speed: speed }).then(done, done);
    }

    async function spark(e, isCurrent, speed) {
      var er = edgeRefs[e.id];
      var trimmed = trimmedFor(e);
      if (!er || !er.flow || !trimmed) return;

      var mm = P.measure(trimmed);
      var col = e.sign > 0 ? palette.pos : palette.neg;
      var flow = er.flow;

      var trail = [];
      var i;
      for (i = 0; i < 5; i++) {
        var c = svg('circle', {
          r: (4.4 - i * 0.72).toFixed(1), fill: col.spark, opacity: 0
        });
        // Only the head glows — blurring all five turns the comet into a smear.
        if (i === 0) c.setAttribute('filter', 'url(#fp-soft)');
        fxG.appendChild(c);
        trail.push(c);
      }

      flow.style.strokeDasharray = String(mm.total);
      flow.style.strokeDashoffset = String(mm.total);
      flow.style.opacity = '0.9';

      try {
        await P.tween(PULSE_MS, isCurrent, function (p) {
          flow.style.strokeDashoffset = String(mm.total * (1 - p));
          for (var j = 0; j < trail.length; j++) {
            var t = Math.min(Math.max(p - j * 0.05, 0), 1);
            var pos = P.pointAt(trimmed, mm.cum, mm.total, t);
            trail[j].setAttribute('cx', pos.x.toFixed(1));
            trail[j].setAttribute('cy', pos.y.toFixed(1));
            var fade = t <= 0 || p >= 1 ? 0 : (1 - j * 0.17) * (p < 0.12 ? p / 0.12 : 1);
            trail[j].setAttribute('opacity', fade.toFixed(2));
          }
        }, { speed: speed });
        ripple(trimmed[trimmed.length - 1], col.ripple, isCurrent, speed);
        P.tween(420, isCurrent, function (p) {
          flow.style.opacity = String(0.9 * (1 - p));
        }, { speed: speed }).catch(function () {});
      } finally {
        for (i = 0; i < trail.length; i++) {
          if (trail[i].parentNode) trail[i].parentNode.removeChild(trail[i]);
        }
      }
    }

    /** Static stand-in for the comet when motion is reduced or there are too many. */
    function flash(ids, isCurrent, speed) {
      function restore() {
        for (var i = 0; i < ids.length; i++) {
          var er = edgeRefs[ids[i]];
          if (!er || !er.line) continue;
          er.line.style.strokeWidth = '';
          er.line.style.opacity = '';
          er.line.style.stroke = '';
        }
      }
      for (var i = 0; i < ids.length; i++) {
        var er = edgeRefs[ids[i]];
        if (!er || !er.line) continue;
        er.line.style.strokeWidth = '3.6';
        er.line.style.opacity = '1';
        // Thickening alone is easy to miss against a pale line, so the highlight borrows
        // the same accent the comet uses.
        er.line.style.stroke = palette.pos.spark;
      }
      P.tween(420, isCurrent, null, { speed: speed }).then(restore, restore);
    }

    // ---- step transition --------------------------------------------------

    /*
     * Everything visible is derived from the current step's values; the animation only
     * paints over the top of it while it runs. That ordering matters: the numbers a
     * biologist reads must be a pure function of the step the controls say we are on,
     * never a side effect of an animation having completed — an interrupted tween, a
     * backgrounded tab or a fast double-click must not be able to leave stale figures on
     * screen.
     */
    function setStep(next) {
      values = next.values || {};
      baseline = next.baseline || {};
      var activeEdges = next.activeEdges || [];
      var reducedMotion = !!next.reducedMotion;
      var speed = next.speed || 1;

      // One generation per step. Bumped only here and on teardown, so an in-flight
      // animation from the previous step unwinds the moment a new one starts.
      token += 1;
      var mine = token;
      function isCurrent() { return token === mine; }

      // Clear anything left over from an interrupted step.
      while (fxG.firstChild) fxG.removeChild(fxG.firstChild);
      var k;
      for (k in edgeRefs) {
        if (!edgeRefs[k].flow || !edgeRefs[k].line) continue;
        edgeRefs[k].flow.style.opacity = '0';
        edgeRefs[k].line.style.strokeWidth = '';
        edgeRefs[k].line.style.opacity = '';
      }

      var from = {};
      var j;
      for (j = 0; j < nodes.length; j++) {
        var id = nodes[j].id;
        from[id] = shown[id] != null ? shown[id]
          : values[id] != null ? values[id]
          : method === 'rwr' ? 0 : 1;
      }

      /** Snap every node to this step's real values. */
      function commit() {
        for (var c = 0; c < nodes.length; c++) {
          var cid = nodes[c].id;
          shown[cid] = values[cid] != null ? values[cid] : from[cid];
        }
        paint();
      }

      var live = [];
      for (j = 0; j < activeEdges.length; j++) {
        for (k = 0; k < visibleEdges.length; k++) {
          if (visibleEdges[k].id === activeEdges[j] && visibleEdges[k].carriesSignal) {
            live.push(visibleEdges[k]);
            break;
          }
        }
      }

      applyChrome();
      paint();

      async function morph() {
        try {
          await P.wait(reducedMotion ? 0 : MORPH_DELAY, isCurrent, { speed: speed });
          await P.tween(MORPH_MS, isCurrent, function (p) {
            for (var c = 0; c < nodes.length; c++) {
              var cid = nodes[c].id;
              var a = from[cid];
              var b = values[cid] != null ? values[cid] : a;
              shown[cid] = a + (b - a) * p;
            }
            paint();
          }, { speed: speed, instant: reducedMotion });
        } finally {
          /*
           * The numbers on screen must match the step the controls say we are on, whether
           * the tween ran to completion, was interrupted by a fast click, or never got to
           * start at all. Correctness of the displayed values is not allowed to depend on
           * an animation succeeding.
           */
          if (isCurrent()) commit();
        }
      }

      async function run() {
        var jobs = [morph()];
        if (reducedMotion || live.length > MAX_COMETS) {
          var ids = [];
          for (var c = 0; c < live.length; c++) ids.push(live[c].id);
          flash(ids, isCurrent, speed);
        } else {
          for (var s = 0; s < live.length; s++) jobs.push(spark(live[s], isCurrent, speed));
        }
        await Promise.all(jobs);
      }

      run().catch(function (err) {
        if (!P.isAbort(err)) throw err;
      });
    }

    // ---- pan / zoom -------------------------------------------------------

    root.addEventListener('wheel', function (ev) {
      ev.preventDefault();
      view.k = clampZoom(view.k * (ev.deltaY < 0 ? 1.12 : 1 / 1.12));
      applyZoom();
    }, { passive: false });

    root.addEventListener('pointerdown', function (ev) {
      if (ev.button !== 0) return;
      if (root.setPointerCapture) root.setPointerCapture(ev.pointerId);
      drag = { px: ev.clientX, py: ev.clientY, vx: view.x, vy: view.y, k: view.k };
      root.style.cursor = 'grabbing';
    });
    root.addEventListener('pointermove', function (ev) {
      if (!drag) return;
      view.x = drag.vx + (ev.clientX - drag.px) / drag.k;
      view.y = drag.vy + (ev.clientY - drag.py) / drag.k;
      applyZoom();
    });
    function endDrag() {
      drag = null;
      root.style.cursor = 'grab';
    }
    root.addEventListener('pointerup', endDrag);
    root.addEventListener('pointercancel', endDrag);
    root.addEventListener('click', function (ev) {
      if (ev.target === root) {
        setSelected(null);
        if (cfg.onSelect) cfg.onSelect(null);
      }
    });

    function zoom(f) {
      view.k = clampZoom(view.k * f);
      applyZoom();
    }
    function fit() {
      view = { x: 0, y: 0, k: 1 };
      applyZoom();
    }
    function setSelected(id) {
      selected = id;
      applyChrome();
    }

    // ---- initial paint ----------------------------------------------------

    for (i = 0; i < nodes.length; i++) buildStrip(nodes[i]);
    root.style.cursor = 'grab';
    applyChrome();
    applyZoom();
    paint();

    return {
      setStep: setStep,
      setTheme: function (d) {
        dark = d;
        for (var j = 0; j < nodes.length; j++) buildStrip(nodes[j]);
        applyChrome();
        paint();
      },
      setSizeByValue: function (on) {
        sizeByValue = on;
        paint();
      },
      setSelected: setSelected,
      fit: fit,
      destroy: function () {
        token += 1;
        if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      }
    };
  }

  // =========================================================================
  // legend
  // =========================================================================

  /*
   * The colour (and optional size) key.
   *
   * Sits above the step controls, because a key you have to scroll past the numbers to
   * find is no key at all. That position means it must stay short, so it is laid out as a
   * horizontal strip rather than a stacked block.
   *
   * The domain is fixed rather than fitted to the current run: a legend that rescaled per
   * run would make "this node got redder" ambiguous between the data moving and the scale
   * moving.
   */
  function createLegend(host, method) {
    var box = hel('div', 'prop-legend');
    var row = hel('div', 'prop-legend-row');
    var main = hel('div', 'prop-legend-main');
    var bar = hel('div', 'prop-legend-bar');
    var ticks = hel('div', 'prop-legend-ticks');
    main.appendChild(bar);
    main.appendChild(ticks);
    row.appendChild(main);

    var sizeKey = hel('div', 'prop-legend-size');
    var factors = [P.SIZE_MIN, 1, P.SIZE_MAX];
    for (var i = 0; i < factors.length; i++) {
      var s = hel('span', 'prop-legend-swatch');
      s.style.width = 14 * factors[i] + 'px';
      s.style.height = 8 * factors[i] + 'px';
      sizeKey.appendChild(s);
    }
    sizeKey.appendChild(hel('span', 'prop-legend-sizelabel', 'size'));
    row.appendChild(sizeKey);

    box.appendChild(row);
    var cap = hel('p', 'prop-legend-cap', P.legendCaption(method));
    box.appendChild(cap);
    host.appendChild(box);

    var tickDefs = P.legendTicks(method);
    for (var t = 0; t < tickDefs.length; t++) {
      ticks.appendChild(hel('span', null, tickDefs[t].label));
    }

    function render(dark, sizeByValue) {
      var steps = 41;
      var stops = [];
      for (var j = 0; j < steps; j++) {
        stops.push(P.rampColor((j / (steps - 1)) * 2 - 1, dark));
      }
      bar.style.background = 'linear-gradient(to right, ' + stops.join(', ') + ')';
      sizeKey.style.display = sizeByValue ? '' : 'none';
    }

    return { render: render, destroy: function () { if (box.parentNode) box.parentNode.removeChild(box); } };
  }

  // =========================================================================
  // transport controls
  // =========================================================================

  function createControls(host, cfg) {
    var box = hel('div', 'prop-controls');

    var banner = hel('div', 'prop-phase');
    box.appendChild(banner);

    var row = hel('div', 'prop-btnrow');
    var bReset = btn('↺', 'Reset', 'Back to the start');
    var bPrev = btn('‹', 'Previous step');
    var bNext = hel('button', 'prop-next');
    bNext.type = 'button';
    bNext.innerHTML = '';
    bNext.appendChild(document.createTextNode('Next step'));
    bNext.appendChild(hel('span', 'prop-next-chev', '›'));
    var bPlay = btn('▶', 'Play through to steady state');
    var bSkip = btn('⇥', 'Jump to steady state');
    row.appendChild(bReset);
    row.appendChild(bPrev);
    row.appendChild(bNext);
    row.appendChild(bPlay);
    row.appendChild(bSkip);

    var speedWrap = hel('div', 'prop-speed');
    speedWrap.appendChild(hel('span', 'prop-speed-label', 'Speed'));
    var speedBtns = [];
    for (var i = 0; i < SPEEDS.length; i++) {
      (function (s) {
        var b = hel('button', 'prop-pill', s + '×');
        b.type = 'button';
        b.onclick = function () { cfg.onSpeed(s); };
        speedBtns.push({ el: b, value: s });
        speedWrap.appendChild(b);
      })(SPEEDS[i]);
    }
    row.appendChild(speedWrap);
    box.appendChild(row);

    var scrubWrap = hel('div', 'prop-scrubwrap');
    var scrub = document.createElement('input');
    scrub.type = 'range';
    scrub.min = 0;
    scrub.className = 'prop-scrub';
    scrub.setAttribute('aria-label', 'Propagation step');
    scrubWrap.appendChild(scrub);
    var readout = hel('div', 'prop-readout');
    var stepText = hel('span', 'prop-steptext');
    var iterText = hel('span', 'prop-itertext');
    readout.appendChild(stepText);
    readout.appendChild(iterText);
    scrubWrap.appendChild(readout);
    box.appendChild(scrubWrap);

    var note = hel('p', 'prop-note', P.iterationNote(cfg.method));
    box.appendChild(note);
    host.appendChild(box);

    bReset.onclick = function () { cfg.onPlaying(false); cfg.onIndex(0); };
    bPrev.onclick = function () { cfg.onPlaying(false); cfg.onIndex(cfg.index() - 1); };
    bNext.onclick = function () { cfg.onPlaying(false); cfg.onIndex(cfg.index() + 1); };
    bPlay.onclick = function () { cfg.onPlaying(!cfg.playing()); };
    bSkip.onclick = function () { cfg.onPlaying(false); cfg.onIndex(cfg.steps().length - 1); };
    scrub.oninput = function () { cfg.onPlaying(false); cfg.onIndex(Number(scrub.value)); };

    function btn(glyph, label, title) {
      var b = hel('button', 'prop-iconbtn', glyph);
      b.type = 'button';
      b.setAttribute('aria-label', label);
      if (title) b.title = title;
      return b;
    }

    function render(steps, index, playing, speed) {
      var step = steps[index];
      if (!step) return;
      var atStart = index === 0;
      var atEnd = index === steps.length - 1;

      banner.textContent = P.phaseLabel(step.kind);
      banner.className = 'prop-phase prop-phase-' + step.kind;

      bReset.disabled = atStart;
      bPrev.disabled = atStart;
      bNext.disabled = atEnd;
      bPlay.disabled = atEnd;
      bSkip.disabled = atEnd;
      bPlay.textContent = playing ? '❚❚' : '▶';
      bPlay.setAttribute('aria-label', playing ? 'Pause' : 'Play through to steady state');

      for (var i = 0; i < speedBtns.length; i++) {
        speedBtns[i].el.className = 'prop-pill' + (speedBtns[i].value === speed ? ' on' : '');
      }

      scrub.max = steps.length - 1;
      scrub.value = index;
      scrub.setAttribute('aria-valuetext', 'Step ' + (index + 1) + ' of ' + steps.length +
        ', iteration ' + step.iter + ', ' +
        (step.kind === 'wave' ? 'spread phase' : step.kind === 'relax' ? 'settle phase' : 'steady state'));

      stepText.textContent = 'Step ' + (index + 1) + ' of ' + steps.length;
      // Always shows the true solver iteration — never a renumbered fiction.
      iterText.textContent = 'solver iteration ' + step.iter + ' of ' + cfg.totalIterations;
    }

    return { render: render, destroy: function () { if (box.parentNode) box.parentNode.removeChild(box); } };
  }

  // =========================================================================
  // narration
  // =========================================================================

  /*
   * Solver constants, mirrored here purely to show the arithmetic. Kept in sync with
   * flashp-sim.js by citation rather than import, because the engines keep them private:
   *   ALG  -> flashp-sim.js "var ALG = {...}"
   *   ODE  -> flashp-sim.js "var ODE = {...}"
   *   RWR  -> flashp-sim.js "defaultAlpha"
   */
  var ALG = { epsilon: 0.1, K: 10, activatorFloor: 0.01, damping: 0.7 };
  var ODEC = { activatorFloor: 0.01, dt: 0.1 };
  var RWR_ALPHA_DEFAULT = 0.85;

  function f1n(n) { return isFinite(n) ? n.toFixed(1) : '∞'; }
  function f2n(n) { return isFinite(n) ? n.toFixed(2) : '∞'; }
  function f3n(n) { return isFinite(n) ? n.toFixed(3) : '∞'; }

  function infoDot(text) {
    var s = hel('span', 'prop-info', 'ⓘ');
    s.title = text;
    return s;
  }

  function createNarration(host, cfg) {
    var eqs = global.FLASHPSIM.buildEquations(cfg.net, cfg.algEq);
    var formulaBy = {};
    var rows = (cfg.algEq && cfg.algEq.equations) || [];
    for (var r = 0; r < rows.length; r++) {
      if (rows[r].n && rows[r].f) formulaBy[rows[r].n] = rows[r].f;
    }

    var pinned = null;

    var box1 = hel('div', 'prop-card');
    var h1 = hel('div', 'prop-cardhead');
    h1.appendChild(hel('h3', null, 'What happens on this step'));
    h1.appendChild(infoDot(
      'The model updates every node at once. On each iteration it recomputes every node ' +
      'from the PREVIOUS iteration\'s values — it is not a relay that visits one node at a ' +
      'time. For the algebraic method the rule is ' +
      'value = activation × inhibition × gene_modifier + exogenous, then the result is ' +
      'damped: next = 0.3 × computed + 0.7 × previous.'));
    box1.appendChild(h1);
    var caption = hel('p', 'prop-caption');
    box1.appendChild(caption);
    var relaxNote = hel('p', 'prop-subnote', P.RELAX_NOTE);
    relaxNote.style.display = 'none';
    box1.appendChild(relaxNote);
    var warn = hel('p', 'prop-warn');
    warn.style.display = 'none';
    warn.textContent = '⚠ This run never settled. The numbers below are where the solver ' +
      'stopped, not a steady state.';
    box1.appendChild(warn);
    host.appendChild(box1);

    var box2 = hel('div', 'prop-card');
    var h2 = hel('div', 'prop-cardhead');
    var h2t = hel('h3', null, 'Biggest movers');
    var unpin = hel('button', 'prop-link', 'show top movers');
    unpin.type = 'button';
    unpin.style.display = 'none';
    unpin.onclick = function () { pinned = null; if (cfg.onPin) cfg.onPin(null); };
    h2.appendChild(h2t);
    h2.appendChild(unpin);
    box2.appendChild(h2);
    var list = hel('div', 'prop-movers');
    box2.appendChild(list);
    host.appendChild(box2);

    function render(step, pin) {
      pinned = pin || null;
      caption.textContent = step.caption;
      relaxNote.style.display = (step.kind === 'relax' && step.index > 0) ? '' : 'none';
      warn.style.display = (step.kind === 'steady' && !cfg.result.converged) ? '' : 'none';

      h2t.textContent = pinned ? 'Why ' + pinned + ' moved' : 'Biggest movers';
      unpin.style.display = pinned ? '' : 'none';

      var iters = cfg.result.iterations;
      var cur = (iters[step.iter] && iters[step.iter].values) || {};
      var prevIter = Math.max(0, step.iter - 1);
      var prev = (iters[prevIter] && iters[prevIter].values) || cur;

      // Which nodes to explain: the pinned one if the user picked one, otherwise the
      // biggest movers into this step.
      var ids;
      if (pinned) {
        ids = [pinned];
      } else {
        ids = step.moved.slice().sort(function (a, b) {
          return Math.abs(P.signedMagnitude(cur[b] || 0, prev[b] || 0, cfg.method)) -
            Math.abs(P.signedMagnitude(cur[a] || 0, prev[a] || 0, cfg.method));
        }).slice(0, 4);
      }

      while (list.firstChild) list.removeChild(list.firstChild);
      if (!ids.length) {
        list.appendChild(hel('p', 'prop-empty', step.index === 0
          ? 'Nothing has changed yet — this is the starting point.'
          : 'No node changed measurably on this step.'));
        return;
      }
      for (var i = 0; i < ids.length; i++) list.appendChild(explain(ids[i], prev, cur));
    }

    function explain(id, prev, cur) {
      var eq = eqs[id];
      var from = prev[id] || 0;
      var to = cur[id] || 0;
      var gm = cfg.perturbation.geneModifiers[id] == null ? 1 : cfg.perturbation.geneModifiers[id];
      var exo = cfg.perturbation.exogenous[id] == null ? 0 : cfg.perturbation.exogenous[id];
      var rwr = cfg.method === 'rwr';
      var rose = to > from;
      var isSource = !eq || (!eq.activators.length && !eq.inhibitors.length);

      var card = hel('div', 'prop-mover');
      var head = hel('div', 'prop-moverhead');
      var name = hel('button', 'prop-movername', id);
      name.type = 'button';
      name.onclick = function () {
        var next = pinned === id ? null : id;
        if (cfg.onPin) cfg.onPin(next);
      };
      head.appendChild(name);
      var delta = hel('span', 'prop-moverdelta');
      delta.appendChild(document.createTextNode((rwr ? f3n(from) : f2n(from)) + ' → '));
      var toEl = hel('span', 'prop-moverto', rwr ? f3n(to) : f2n(to));
      toEl.style.color = rose ? '#2c7fb8' : '#d7301f';
      delta.appendChild(toEl);
      head.appendChild(delta);
      card.appendChild(head);

      if (isSource) {
        var p = hel('p', 'prop-sourcenote');
        p.appendChild(document.createTextNode(
          'Source node — nothing in the network regulates it, so its value is just its own settings: '));
        p.appendChild(hel('code', null,
          rwr ? 'seed ' + f2n(gm === 1 ? 0 : gm) : '×' + gm + ' + ' + exo));
        p.appendChild(document.createTextNode('.'));
        card.appendChild(p);
      } else {
        card.appendChild(arithmetic(eq, gm, exo, from, to, prev));
      }

      if (formulaBy[id]) {
        var det = document.createElement('details');
        det.className = 'prop-formula';
        var sum = document.createElement('summary');
        sum.textContent = 'published equation';
        det.appendChild(sum);
        det.appendChild(hel('code', null, formulaBy[id]));
        card.appendChild(det);
      }
      return card;
    }

    function row(label, text) {
      var d = hel('div', 'prop-arithrow');
      d.appendChild(hel('span', 'prop-arithlabel', label));
      d.appendChild(hel('span', 'prop-arithval', text));
      return d;
    }

    function arithmetic(eq, gm, exo, from, to, vals) {
      var wrap = hel('div', 'prop-arith');
      var i, prod;

      if (cfg.method === 'rwr') {
        var a = cfg.alpha == null ? RWR_ALPHA_DEFAULT : cfg.alpha;
        var regs = [];
        for (i = 0; i < eq.activators.length; i++) regs.push([eq.activators[i], 1]);
        for (i = 0; i < eq.inhibitors.length; i++) regs.push([eq.inhibitors[i], -1]);
        var sum = 0;
        var parts = [];
        for (i = 0; i < regs.length; i++) {
          var rv = vals[regs[i][0]] || 0;
          sum += regs[i][1] * rv;
          parts.push((regs[i][1] > 0 ? '+' : '−') + regs[i][0] + '(' + f3n(rv) + ')');
        }
        var mean = regs.length ? sum / regs.length : 0;
        wrap.appendChild(row('regulators', regs.length ? parts.join(' ') : 'none'));
        wrap.appendChild(row('mean', f3n(mean)));
        wrap.appendChild(row('restart', 'α=' + a + ' · so ' + f3n(a) + '×' + f3n(mean) +
          ' + ' + f3n(1 - a) + '×seed'));
        wrap.appendChild(row('result', f3n(to)));
        return wrap;
      }

      if (cfg.method === 'ode') {
        var K = cfg.hillK == null ? 1 : cfg.hillK;
        var n = cfg.hillN == null ? 2 : cfg.hillN;
        var Kn = Math.pow(K, n);
        function hillA(x) {
          if (x <= 0) return 0;
          var xn = Math.pow(x, n);
          return (xn * (Kn + 1)) / (Kn + xn);
        }
        function hillI(x) {
          if (x <= 0) return (Kn + 1) / Kn;
          return (Kn + 1) / (Kn + Math.pow(x, n));
        }
        var act = 1, aParts = [];
        for (i = 0; i < eq.activators.length; i++) {
          var av = vals[eq.activators[i]] == null ? 1 : vals[eq.activators[i]];
          act *= hillA(Math.max(av, ODEC.activatorFloor));
          aParts.push(eq.activators[i] + '=' + f2n(av));
        }
        var inh = 1, iParts = [];
        for (i = 0; i < eq.inhibitors.length; i++) {
          var iv = vals[eq.inhibitors[i]] == null ? 1 : vals[eq.inhibitors[i]];
          inh *= hillI(iv);
          iParts.push(eq.inhibitors[i] + '=' + f2n(iv));
        }
        var prodO = Math.max(act * inh * gm + exo, 0);
        wrap.appendChild(row('activation', eq.activators.length
          ? '∏ hill⁺(' + aParts.join(', ') + ') = ' + f2n(act) : '1 (no activators)'));
        wrap.appendChild(row('inhibition', eq.inhibitors.length
          ? '∏ hill⁻(' + iParts.join(', ') + ') = ' + f2n(inh) : '1 (no inhibitors)'));
        wrap.appendChild(row('production',
          f2n(act) + ' × ' + f2n(inh) + ' × ' + gm + ' + ' + exo + ' = ' + f2n(prodO)));
        wrap.appendChild(row('euler step',
          f2n(from) + ' + (' + f2n(prodO) + ' − ' + f2n(from) + ') × ' + ODEC.dt + ' = ' + f2n(to)));
        wrap.appendChild(row('hill params', 'K=' + K + ', n=' + n));
        return wrap;
      }

      // algebraic
      var actA = 1, actText = '1 (no activators)';
      if (eq.activators.length) {
        prod = 1;
        var names = [];
        for (i = 0; i < eq.activators.length; i++) {
          var v = vals[eq.activators[i]] == null ? 1 : vals[eq.activators[i]];
          prod *= Math.max(v, ALG.activatorFloor);
          names.push(eq.activators[i] + '=' + f2n(v));
        }
        actA = Math.pow(prod, 1 / eq.activators.length);
        actText = 'geometric mean(' + names.join(', ') + ') = ' + f2n(actA);
      }

      var inhA = 1, inhText = '1 (no inhibitors)';
      if (eq.inhibitors.length) {
        prod = 1;
        for (i = 0; i < eq.inhibitors.length; i++) {
          prod *= vals[eq.inhibitors[i]] == null ? 1 : vals[eq.inhibitors[i]];
        }
        inhA = Math.min(1 / Math.max(prod, ALG.epsilon), ALG.K);
        var capped = 1 / Math.max(prod, ALG.epsilon) > ALG.K;
        inhText = 'min(1 / max(' + f2n(prod) + ', ' + ALG.epsilon + '), ' + ALG.K + ') = ' +
          f2n(inhA) + (capped ? '  ← at the inhibition ceiling' : '');
      }

      var computed = Math.max(actA * inhA * gm + exo, 0);
      wrap.appendChild(row('activation', actText));
      wrap.appendChild(row('inhibition', inhText));
      wrap.appendChild(row('computed',
        f2n(actA) + ' × ' + f2n(inhA) + ' × ' + gm + ' + ' + exo + ' = ' + f2n(computed)));
      // The damping line is the one people miss — without it the shown value looks like it
      // disagrees with the rule above.
      wrap.appendChild(row('damped',
        f1n(1 - ALG.damping) + ' × ' + f2n(computed) + ' + ' + f1n(ALG.damping) + ' × ' +
        f2n(from) + ' = ' + f2n(to)));
      return wrap;
    }

    return {
      render: render,
      destroy: function () {
        if (box1.parentNode) box1.parentNode.removeChild(box1);
        if (box2.parentNode) box2.parentNode.removeChild(box2);
      }
    };
  }

  // =========================================================================
  // value chart
  // =========================================================================

  var CHART_EPS = 1e-4;

  /*
   * Saturation point for log2 fold, in log2 units (+/-8 -> ÷256 … ×256).
   *
   * A node driven to exactly 0 has no finite log fold; flooring the ratio at EPS puts it at
   * log2(1e-4) ~ -13.3, which is not a real measurement — it is the floor — and letting it
   * set the axis range squashes everything that matters into a sliver. Saturating instead
   * keeps the readable range readable, the same trade the colour ramp makes by clamping.
   */
  var LOG2_CLAMP = 8;

  /** Round a raw tick interval up to the nearest 1, 2 or 5 x 10^n. */
  function niceStep(raw) {
    if (!isFinite(raw) || raw <= 0) return 1;
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var norm = raw / mag;
    return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  }

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  function createChart(host, cfg) {
    var rwr = cfg.method === 'rwr';
    var mode = rwr ? 'absolute' : lsGet('values-mode', 'log2');
    var plotted = cfg.nodes.slice();
    var selected = null;
    var dark = !!cfg.dark;
    var currentIter = 0;

    var box = hel('div', 'prop-chart');
    var head = hel('div', 'prop-charthead');
    var titleEl = hel('h3', null, '');
    head.appendChild(titleEl);
    head.appendChild(infoDot(
      'Each node is compared with the baseline run at the SAME iteration, so the chart and ' +
      'the network always agree about what has changed. The line grows as you step; the ' +
      'vertical marker is the iteration currently on the canvas. log₂ fold saturates at ' +
      '±' + LOG2_CLAMP + ' — a line resting on that edge has gone further than the axis can ' +
      'show, and the value boxes on the network always give the true number.'));

    var modeWrap = hel('div', 'prop-modes');
    var MODES = [
      { key: 'log2', label: 'log₂ fold' },
      { key: 'relative', label: 'fold' },
      { key: 'absolute', label: 'absolute' }
    ];
    var modeBtns = [];
    for (var mi = 0; mi < MODES.length; mi++) {
      (function (m) {
        var b = hel('button', 'prop-pill', m.label);
        b.type = 'button';
        b.onclick = function () {
          mode = m.key;
          lsSet('values-mode', m.key);
          render(currentIter);
        };
        modeBtns.push({ el: b, key: m.key });
        modeWrap.appendChild(b);
      })(MODES[mi]);
    }
    if (!rwr) head.appendChild(modeWrap);
    box.appendChild(head);

    var legendRow = hel('div', 'prop-chartlegend');
    box.appendChild(legendRow);

    var plot = hel('div', 'prop-plot');
    box.appendChild(plot);
    host.appendChild(box);

    function yOf(v, b) {
      if (rwr) return v - b;
      if (mode === 'absolute') return v;
      var ratio = Math.max(v, CHART_EPS) / Math.max(b, CHART_EPS);
      if (mode === 'log2') {
        return Math.min(Math.max(Math.log(Math.max(ratio, CHART_EPS)) / Math.LN2, -LOG2_CLAMP), LOG2_CLAMP);
      }
      return ratio;
    }

    function render(iter) {
      currentIter = iter;
      var i, k, n;
      var neutral = rwr ? 0 : mode === 'log2' ? 0 : 1;
      var total = cfg.result.iterations.length - 1;

      titleEl.textContent = rwr
        ? 'signal Δ vs ' + cfg.baselineLabel
        : mode === 'log2' ? 'log₂ fold vs ' + cfg.baselineLabel
        : mode === 'relative' ? 'fold vs ' + cfg.baselineLabel
        : 'model value';

      for (i = 0; i < modeBtns.length; i++) {
        modeBtns[i].el.className = 'prop-pill' + (modeBtns[i].key === mode ? ' on' : '');
      }

      var colors = P.seriesColors(plotted, dark);

      // legend chips
      while (legendRow.firstChild) legendRow.removeChild(legendRow.firstChild);
      for (i = 0; i < plotted.length; i++) {
        (function (id) {
          var chip = hel('button', 'prop-chip');
          chip.type = 'button';
          chip.title = selected === id ? 'Unpin ' + id : 'Pin ' + id;
          if (selected && selected !== id) chip.classList.add('dim');
          var sw = hel('span', 'prop-chipsw');
          sw.style.background = colors[id];
          chip.appendChild(sw);
          var nm = hel('span', 'prop-chipname', id);
          if (selected === id) nm.classList.add('on');
          chip.appendChild(nm);
          chip.onclick = function () {
            if (cfg.onSelect) cfg.onSelect(selected === id ? null : id);
          };
          legendRow.appendChild(chip);
        })(plotted[i]);
      }

      while (plot.firstChild) plot.removeChild(plot.firstChild);
      if (!plotted.length) {
        plot.appendChild(hel('div', 'prop-plotempty',
          'No nodes to plot — click a node in the network to add it.'));
        return;
      }

      var W = plot.clientWidth || 420;
      var H = plot.clientHeight || 220;
      if (H < 90) H = 90;
      var M = { top: 8, right: 14, bottom: 24, left: 46 };
      var pw = Math.max(10, W - M.left - M.right);
      var ph = Math.max(10, H - M.top - M.bottom);

      /*
       * Y range is computed over the WHOLE run, not just the part drawn so far, so the axis
       * is settled before the first step and a line never changes height between clicks.
       */
      var lo = Infinity, hi = -Infinity;
      for (k = 0; k <= total; k++) {
        var vals = cfg.result.iterations[k].values;
        var base = cfg.trace.baselineAt(k);
        for (n = 0; n < plotted.length; n++) {
          var y = yOf(vals[plotted[n]] || 0,
            base[plotted[n]] == null ? (rwr ? 0 : 1) : base[plotted[n]]);
          if (y < lo) lo = y;
          if (y > hi) hi = y;
        }
      }
      if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
      // Always keep the no-change line in view, then pad so lines don't touch the edge.
      lo = Math.min(lo, neutral);
      hi = Math.max(hi, neutral);
      var pad = Math.max((hi - lo) * 0.08, 0.05);
      var step = niceStep((hi + pad - (lo - pad)) / 5);
      var ymin = Math.floor((lo - pad) / step) * step;
      var ymax = Math.ceil((hi + pad) / step) * step;
      if (ymax - ymin < 1e-9) ymax = ymin + 1;

      function sx(it) { return M.left + (total ? it / total : 0) * pw; }
      function sy(v) { return M.top + ph - ((v - ymin) / (ymax - ymin)) * ph; }

      var border = cssVar('--border', '#e6ddd3');
      var muted = cssVar('--muted', '#847b73');
      var primary = cssVar('--primary', '#d14600');

      var s = svg('svg', { width: W, height: H });
      s.setAttribute('class', 'prop-plotsvg');

      // grid + y ticks
      for (var t = ymin; t <= ymax + 1e-9; t += step) {
        var gy = sy(t);
        s.appendChild(svg('line', {
          x1: M.left, y1: gy, x2: M.left + pw, y2: gy,
          stroke: border, 'stroke-dasharray': '3 3'
        }));
        var tl = svg('text', {
          x: M.left - 6, y: gy + 3, 'text-anchor': 'end', 'font-size': 10, fill: muted
        });
        tl.textContent = Math.abs(t) >= 10 ? t.toFixed(0) : t.toFixed(1);
        s.appendChild(tl);
      }

      // x ticks
      var xTicks = 5;
      for (var xt = 0; xt <= xTicks; xt++) {
        var itv = Math.round((total * xt) / xTicks);
        var gx = sx(itv);
        s.appendChild(svg('line', {
          x1: gx, y1: M.top, x2: gx, y2: M.top + ph, stroke: border, 'stroke-dasharray': '3 3'
        }));
        var xl = svg('text', {
          x: gx, y: M.top + ph + 13, 'text-anchor': 'middle', 'font-size': 10, fill: muted
        });
        xl.textContent = String(itv);
        s.appendChild(xl);
      }
      var axLabel = svg('text', {
        x: M.left + pw / 2, y: H - 2, 'text-anchor': 'middle', 'font-size': 10, fill: muted
      });
      axLabel.textContent = 'iteration';
      s.appendChild(axLabel);

      // no-change reference
      s.appendChild(svg('line', {
        x1: M.left, y1: sy(neutral), x2: M.left + pw, y2: sy(neutral),
        stroke: muted, 'stroke-dasharray': '4 4'
      }));

      // the playhead — keeps the chart aligned with the canvas
      s.appendChild(svg('line', {
        x1: sx(currentIter), y1: M.top, x2: sx(currentIter), y2: M.top + ph,
        stroke: primary, 'stroke-width': 1.5, opacity: 0.75
      }));

      /*
       * The line stops at the current step rather than the data being sliced, so the x-axis
       * keeps spanning the whole run and never rescales as you step — only the drawn line
       * grows.
       */
      for (n = 0; n < plotted.length; n++) {
        var id = plotted[n];
        var d = '';
        for (k = 0; k <= Math.min(currentIter, total); k++) {
          var bb = cfg.trace.baselineAt(k);
          var yy = yOf(cfg.result.iterations[k].values[id] || 0,
            bb[id] == null ? (rwr ? 0 : 1) : bb[id]);
          d += (k ? 'L' : 'M') + sx(k).toFixed(1) + ' ' + sy(yy).toFixed(1);
        }
        if (!d) continue;
        s.appendChild(svg('path', {
          d: d, fill: 'none', stroke: colors[id],
          'stroke-width': selected === id ? 3 : 1.8,
          'stroke-opacity': selected && selected !== id ? 0.3 : 1,
          'stroke-linejoin': 'round', 'stroke-linecap': 'round'
        }));
      }

      plot.appendChild(s);
    }

    /*
     * The SVG is sized from a measurement, so anything that changes the plot box after a
     * render would otherwise leave the chart drawn at a stale size — dragging the divider,
     * resizing the window, or the narration panel growing and squeezing the stage. Observing
     * the box removes the timing dependence entirely.
     */
    var ro = null;
    var roFrame = null;
    var lastW = 0;
    var lastH = 0;
    if (global.ResizeObserver) {
      ro = new global.ResizeObserver(function () {
        /*
         * Deferred by a frame, and guarded on the size actually changing. Re-rendering
         * synchronously inside the callback lets the resulting DOM write feed straight back
         * into the observer, which the browser reports as "ResizeObserver loop completed
         * with undelivered notifications".
         */
        if (roFrame) return;
        roFrame = requestAnimationFrame(function () {
          roFrame = null;
          var w = plot.clientWidth;
          var h = plot.clientHeight;
          if (w === lastW && h === lastH) return;
          lastW = w;
          lastH = h;
          render(currentIter);
        });
      });
      ro.observe(plot);
    }

    return {
      render: render,
      setTheme: function (d) { dark = d; render(currentIter); },
      setNodes: function (ids) { plotted = ids.slice(); render(currentIter); },
      setSelected: function (id) { selected = id; render(currentIter); },
      nodes: function () { return plotted; },
      destroy: function () {
        if (ro) ro.disconnect();
        if (roFrame) cancelAnimationFrame(roFrame);
        if (box.parentNode) box.parentNode.removeChild(box);
      }
    };
  }

  // =========================================================================
  // the view itself
  // =========================================================================

  /*
   * opts: { network, result, method, perturbation, style, dark }
   *
   * `network` is a Studio network entry (net / algEq / annById / name / species) and
   * `result` is whatever FLASHPSIM.runSimulation already returned for the Simulate view —
   * the same numbers, presented differently. Nothing is re-solved here.
   */
  function open(host, opts) {
    var net = opts.network.net;
    var algEq = opts.network.algEq || null;
    var method = opts.method;
    var result = opts.result;
    var perturbation = opts.perturbation;
    var ann = opts.network.annById || {};
    var i;

    var edges = P.regulatoryEdges(net, algEq, method);
    var seeds = P.seedNodes(net, perturbation);
    var trace = P.buildTrace(result, edges, seeds, method);
    var focus = P.activeSubgraph(net, result, trace, edges, seeds, method);

    // Node records the canvas understands, with the annotation full-name for the label.
    var allNodes = [];
    for (i = 0; i < net.nodes.length; i++) {
      var n = net.nodes[i];
      allNodes.push({ id: n.id, ty: n.ty || 'GENE', fn: (ann[n.id] && ann[n.id].fn) || '' });
    }

    var state = {
      index: 0,
      playing: false,
      speed: 1,
      focusOn: lsGet('focus', true),
      sizeByValue: lsGet('size-by-value', false),
      chartOn: lsGet('chart', true),
      // "Animate" starts from the OS preference but can be overridden in either direction.
      motion: lsGet('motion', !P.prefersReducedMotion()),
      pinned: null,
      dark: !!opts.dark
    };

    /*
     * Which nodes the chart plots: the phenotype, whatever the user perturbed, then the
     * biggest movers. Same recipe the Simulate view uses to seed its table selection, so
     * the two views open showing the same nodes.
     */
    var plotSeed = {};
    if (result.phenotype) plotSeed[result.phenotype] = true;
    for (i = 0; i < seeds.length; i++) plotSeed[seeds[i]] = true;
    var ranked = trace.movers.slice().sort(function (a, b) {
      var fin = result.iterations[result.iterations.length - 1].values;
      var base = trace.baselineAt(trace.totalIterations);
      return Math.abs(P.signedMagnitude(fin[b] || 0, base[b] || 0, method)) -
        Math.abs(P.signedMagnitude(fin[a] || 0, base[a] || 0, method));
    });
    for (i = 0; i < ranked.length && Object.keys(plotSeed).length < 8; i++) plotSeed[ranked[i]] = true;
    var plotNodes = Object.keys(plotSeed);

    var canvas = null;
    var playTimer = null;
    var destroyed = false;

    // ---- chrome -----------------------------------------------------------

    var root = hel('div', 'prop-root');

    var bar = hel('div', 'prop-toolbar');
    var title = hel('div', 'prop-title');
    title.appendChild(hel('strong', null, opts.network.name || 'Network'));
    var sub = hel('span', 'prop-sub');
    sub.textContent = (opts.network.species ? opts.network.species + ' · ' : '') +
      (global.FLASHPSIM.METHOD_LABELS[method] || method) + ' · vs ' +
      (result.baselineLabel || 'Wild type');
    title.appendChild(sub);
    bar.appendChild(title);

    var toggles = hel('div', 'prop-toggles');
    var cbFocus = checkbox('', state.focusOn, function (on) {
      state.focusOn = on;
      lsSet('focus', on);
      rebuildCanvas();
      applyStep(true);
    });
    var cbSize = checkbox('Scale size by value', state.sizeByValue, function (on) {
      state.sizeByValue = on;
      lsSet('size-by-value', on);
      if (canvas) canvas.setSizeByValue(on);
      legend.render(state.dark, on);
    });
    var cbMotion = checkbox('Animate', state.motion, function (on) {
      state.motion = on;
      lsSet('motion', on);
    });
    var cbChart = checkbox('Value chart', state.chartOn, function (on) {
      state.chartOn = on;
      lsSet('chart', on);
      applySplit();
      if (on) chart.render(currentIter());
    });
    toggles.appendChild(cbFocus.el);
    toggles.appendChild(cbSize.el);
    toggles.appendChild(cbChart.el);
    toggles.appendChild(cbMotion.el);
    bar.appendChild(toggles);
    root.appendChild(bar);

    /*
     * Network left, everything else in a sidebar on the right — split vertically rather than
     * stacked, because a deep cascade is the tall element and cannot afford to lose height,
     * and a transport bar stretched across the full width reads as detached from the canvas
     * it drives.
     */
    var stage = hel('div', 'prop-stage');
    var canvasPane = hel('div', 'prop-canvaspane');
    var divider = hel('div', 'prop-divider');
    divider.setAttribute('role', 'separator');
    divider.setAttribute('aria-orientation', 'vertical');
    divider.setAttribute('aria-label', 'Resize the network and the side panel');
    divider.tabIndex = 0;

    var side = hel('div', 'prop-side');
    var chartPane = hel('div', 'prop-chartpane');
    var panel = hel('div', 'prop-panel');
    side.appendChild(chartPane);
    side.appendChild(panel);

    stage.appendChild(canvasPane);
    stage.appendChild(divider);
    stage.appendChild(side);
    root.appendChild(stage);
    host.appendChild(root);

    var legend = createLegend(panel, method);
    var controls = createControls(panel, {
      method: method,
      totalIterations: trace.totalIterations,
      steps: function () { return trace.steps; },
      index: function () { return state.index; },
      playing: function () { return state.playing; },
      onIndex: setIndex,
      onPlaying: setPlaying,
      onSpeed: function (s) { state.speed = s; render(); }
    });

    var narration = createNarration(panel, {
      net: net,
      algEq: algEq,
      result: result,
      method: method,
      perturbation: perturbation,
      alpha: (opts.network.params || {}).alpha,
      hillK: (opts.network.params || {}).K,
      hillN: (opts.network.params || {}).n,
      onPin: setPinned
    });

    var chart = createChart(chartPane, {
      result: result,
      trace: trace,
      nodes: plotNodes,
      method: method,
      baselineLabel: result.baselineLabel || 'baseline',
      dark: state.dark,
      onSelect: setPinned
    });

    // A screen reader gets one sentence per step rather than a silent canvas.
    var live = hel('p', 'prop-live');
    live.setAttribute('aria-live', 'polite');
    panel.appendChild(live);

    // ---- split -------------------------------------------------------------

    /*
     * The sidebar's share of the stage. Clamped so neither pane can be collapsed to nothing,
     * and held in a plain variable rather than being read back out of the DOM inside
     * pointerup — the live value is needed there before any re-layout has happened.
     */
    var chartPct = Math.min(Math.max(lsGet('split', 38), 20), 70);

    function applySplit() {
      // The sidebar always exists — it carries the transport — so only the chart inside it
      // is toggled, and the divider stays put.
      chartPane.style.display = state.chartOn ? '' : 'none';
      side.style.flex = '0 0 ' + chartPct + '%';
      divider.setAttribute('aria-valuenow', String(Math.round(chartPct)));
      divider.setAttribute('aria-valuemin', '20');
      divider.setAttribute('aria-valuemax', '70');
    }

    var dragSplit = null;
    divider.addEventListener('pointerdown', function (ev) {
      if (ev.button !== 0) return;
      divider.setPointerCapture(ev.pointerId);
      dragSplit = { w: stage.getBoundingClientRect().width };
      divider.classList.add('on');
      ev.preventDefault();
    });
    divider.addEventListener('pointermove', function (ev) {
      if (!dragSplit) return;
      var r = stage.getBoundingClientRect();
      var pct = ((r.right - ev.clientX) / r.width) * 100;
      chartPct = Math.min(Math.max(pct, 20), 70);
      applySplit();
      chart.render(currentIter());
    });
    function endSplit() {
      if (!dragSplit) return;
      dragSplit = null;
      divider.classList.remove('on');
      lsSet('split', chartPct);
      if (canvas) canvas.fit();
      chart.render(currentIter());
    }
    divider.addEventListener('pointerup', endSplit);
    divider.addEventListener('pointercancel', endSplit);
    divider.addEventListener('dblclick', function () {
      chartPct = 38;
      applySplit();
      lsSet('split', chartPct);
      chart.render(currentIter());
    });
    divider.addEventListener('keydown', function (ev) {
      var d = ev.key === 'ArrowLeft' ? 2 : ev.key === 'ArrowRight' ? -2 : 0;
      if (!d) return;
      ev.preventDefault();
      ev.stopPropagation();
      chartPct = Math.min(Math.max(chartPct + d, 20), 70);
      applySplit();
      lsSet('split', chartPct);
      chart.render(currentIter());
    });

    function currentIter() {
      var step = trace.steps[state.index];
      return step ? step.iter : 0;
    }

    /** One pinned node, shared by the canvas, the chart legend and the narration. */
    function setPinned(id) {
      state.pinned = id;
      if (canvas) canvas.setSelected(id);
      chart.setSelected(id);
      narration.render(trace.steps[state.index], id);
    }

    function checkbox(label, checked, onChange) {
      var wrap = hel('label', 'prop-check');
      var input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = !!checked;
      var text = hel('span', null, label);
      wrap.appendChild(input);
      wrap.appendChild(text);
      input.onchange = function () { onChange(input.checked); };
      return { el: wrap, input: input, text: text };
    }

    // ---- canvas lifecycle -------------------------------------------------

    function visibleSet() {
      if (!state.focusOn) {
        return { nodes: allNodes, edges: edges };
      }
      var keepN = [];
      var j;
      for (j = 0; j < allNodes.length; j++) {
        if (focus.nodes[allNodes[j].id]) keepN.push(allNodes[j]);
      }
      var keepE = [];
      for (j = 0; j < edges.length; j++) {
        if (focus.edges[edges[j].id]) keepE.push(edges[j]);
      }
      return { nodes: keepN, edges: keepE };
    }

    function rebuildCanvas() {
      if (canvas) canvas.destroy();
      var vis = visibleSet();
      canvas = createCanvas(canvasPane, {
        nodes: vis.nodes,
        edges: vis.edges,
        layout: layout,
        style: opts.style,
        method: method,
        perturbation: perturbation,
        dark: state.dark,
        sizeByValue: state.sizeByValue,
        onSelect: setPinned
      });
      if (state.pinned) canvas.setSelected(state.pinned);
      updateFocusLabel();
    }

    function updateFocusLabel() {
      var n = Object.keys(focus.nodes).length;
      cbFocus.text.textContent = state.focusOn
        ? 'Focus on what changed (' + n + ')'
        : 'Show all ' + allNodes.length;
      if (focus.fallback === 'none-changed') {
        cbFocus.text.textContent = 'Nothing changed — showing the whole network';
        cbFocus.input.disabled = true;
      }
    }

    // ---- playback ---------------------------------------------------------

    function applyStep(instant) {
      var step = trace.steps[state.index];
      if (!step || !canvas) return;
      canvas.setStep({
        values: result.iterations[step.iter].values,
        baseline: trace.baselineAt(step.iter),
        activeEdges: step.activeEdges,
        reducedMotion: instant ? true : !state.motion,
        speed: state.speed
      });
    }

    function render() {
      controls.render(trace.steps, state.index, state.playing, state.speed);
      legend.render(state.dark, state.sizeByValue);
      var step = trace.steps[state.index];
      if (!step) return;
      narration.render(step, state.pinned);
      if (state.chartOn) chart.render(step.iter);
      live.textContent = step.caption;
    }

    function setIndex(i) {
      var next = Math.min(Math.max(i, 0), trace.steps.length - 1);
      if (next === state.index) return;
      state.index = next;
      applyStep(false);
      render();
      schedule();
    }

    function setPlaying(p) {
      state.playing = p && state.index < trace.steps.length - 1;
      render();
      schedule();
    }

    function schedule() {
      if (playTimer) {
        clearTimeout(playTimer);
        playTimer = null;
      }
      if (!state.playing || destroyed) return;
      if (state.index >= trace.steps.length - 1) {
        state.playing = false;
        render();
        return;
      }
      playTimer = setTimeout(function () {
        if (destroyed || !state.playing) return;
        setIndex(state.index + 1);
      }, STEP_MS / state.speed);
    }

    // ---- keyboard ---------------------------------------------------------

    function onKey(ev) {
      if (destroyed) return;
      if (!root.offsetParent) return;      // the view is not the one on screen
      var t = ev.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
      if (ev.key === 'ArrowRight') { ev.preventDefault(); setPlaying(false); setIndex(state.index + 1); }
      else if (ev.key === 'ArrowLeft') { ev.preventDefault(); setPlaying(false); setIndex(state.index - 1); }
      else if (ev.key === ' ') { ev.preventDefault(); setPlaying(!state.playing); }
      else if (ev.key === 'Home') { ev.preventDefault(); setPlaying(false); setIndex(0); }
      else if (ev.key === 'End') { ev.preventDefault(); setPlaying(false); setIndex(trace.steps.length - 1); }
      else if (ev.key === 'f' || ev.key === 'F') {
        cbFocus.input.checked = !cbFocus.input.checked;
        cbFocus.input.onchange();
      }
    }
    document.addEventListener('keydown', onKey);

    // ---- go ---------------------------------------------------------------

    var layout = null;
    var layoutEdges = [];
    for (i = 0; i < edges.length; i++) {
      layoutEdges.push({ id: edges[i].id, s: edges[i].s, t: edges[i].t });
    }
    var layoutNodes = [];
    for (i = 0; i < allNodes.length; i++) layoutNodes.push({ id: allNodes[i].id });

    /*
     * The whole network is laid out once, and the focused view simply hides what it doesn't
     * need. Toggling focus therefore never makes a node jump, and the focused picture stays
     * spatially consistent with the full one — which matters when someone is trying to hold
     * the topology in their head.
     */
    var resizeTimer = null;
    function onResize() {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        if (!destroyed && state.chartOn) chart.render(currentIter());
      }, 180);
    }
    global.addEventListener('resize', onResize);

    return P.layoutGraph(layoutNodes, layoutEdges).then(function (L) {
      layout = L;
      applySplit();
      rebuildCanvas();
      applyStep(true);
      render();
      return {
        setTheme: function (d) {
          state.dark = d;
          if (canvas) canvas.setTheme(d);
          legend.render(d, state.sizeByValue);
          chart.setTheme(d);
        },
        /** The Studio re-lays the pane out when the view is shown; the chart needs telling. */
        resize: function () {
          if (canvas) canvas.fit();
          if (state.chartOn) chart.render(currentIter());
        },
        destroy: function () {
          destroyed = true;
          if (playTimer) clearTimeout(playTimer);
          if (resizeTimer) clearTimeout(resizeTimer);
          document.removeEventListener('keydown', onKey);
          global.removeEventListener('resize', onResize);
          if (canvas) canvas.destroy();
          narration.destroy();
          chart.destroy();
          legend.destroy();
          controls.destroy();
          if (root.parentNode) root.parentNode.removeChild(root);
        },
        /** Exposed for the smoke test / debugging. */
        _state: state,
        _trace: trace,
        _setIndex: setIndex
      };
    });
  }

  global.FLASHPPROPVIEW = {
    open: open,
    createCanvas: createCanvas,
    createLegend: createLegend,
    createControls: createControls,
    EDGE_LIGHT: EDGE_LIGHT,
    EDGE_DARK: EDGE_DARK,
    RING: RING,
    STEP_MS: STEP_MS,
    SPEEDS: SPEEDS,
    MAX_COMETS: MAX_COMETS,
    PULSE_MS: PULSE_MS,
    MORPH_MS: MORPH_MS,
    MORPH_DELAY: MORPH_DELAY,
    hel: hel,
    svg: svg,
    pick: pick,
    lsGet: lsGet,
    lsSet: lsSet
  };
})(typeof window !== 'undefined' ? window : this);
