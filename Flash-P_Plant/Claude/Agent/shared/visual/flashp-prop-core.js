/*!
 * flashp-prop-core.js — value/geometry/step model for the Visual Propagation view.
 *
 * A port of the website's src/lib/traits/propagate/{anim,geometry,scale,trace,focus}.ts
 * and src/lib/traits/layout/elk.ts, with the TypeScript types stripped and the ES module
 * boundaries collapsed into one global. Everything here is pure except `tween`, which
 * needs the clock, and `layoutGraph`, which needs ELK.
 *
 * House style is ES5 (var / function) to match flashp-sim.js and flashp-graph.js. The one
 * exception is `tween`: its cancellation idiom unwinds a whole nested animation through
 * try/finally around awaits, and rewriting that as promise chains would obscure the only
 * genuinely subtle control flow in the feature.
 *
 * Depends on: window.ELK (elkjs, already vendored), window.FLASHPSIM.buildEquations.
 */
(function (global) {
  'use strict';

  // =========================================================================
  // anim — a minimal requestAnimationFrame tween
  // =========================================================================

  /*
   * Cancellation is a monotonically increasing integer rather than an AbortController:
   * every awaited tween re-checks the token each frame and throws ABORT the moment it
   * goes stale, which unwinds a whole nested animation sequence in one assignment. The
   * token is bumped on teardown and on every new step, so rapid clicking through steps
   * cannot leave two animations fighting over the same nodes.
   */
  var ABORT = { abort: true };

  function isAbort(e) {
    return e === ABORT;
  }

  /*
   * Wait one frame — but never wait forever.
   *
   * Browsers stop firing requestAnimationFrame in a backgrounded or hidden tab. A plain
   * `await raf()` therefore parks the animation indefinitely, and since the step's real
   * values are only committed once the tween finishes, switching tabs mid-step would
   * leave stale numbers on screen when you came back. Racing rAF against a timer keeps
   * the tween progressing (in coarse jumps) while hidden, so it always reaches its
   * final state.
   */
  function raf() {
    return new Promise(function (resolve) {
      var done = false;
      function finish() {
        if (done) return;
        done = true;
        resolve();
      }
      requestAnimationFrame(finish);
      setTimeout(finish, 100);
    });
  }

  function easeInOutCubic(p) {
    return p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
  }

  /*
   * Run `fn(easedProgress)` every frame for `ms`, then once more with exactly 1.
   *
   * `isCurrent` is re-read each frame; when it returns false the tween throws ABORT and
   * leaves the caller's `finally` blocks to clean up.
   */
  async function tween(ms, isCurrent, fn, opts) {
    opts = opts || {};
    var speed = opts.speed == null ? 1 : opts.speed;
    var instant = !!opts.instant;

    if (instant || ms <= 0) {
      if (!isCurrent()) throw ABORT;
      if (fn) fn(1);
      return;
    }

    var acc = 0;
    var last = performance.now();
    if (fn) fn(0);
    while (acc < ms) {
      await raf();
      if (!isCurrent()) throw ABORT;
      var now = performance.now();
      // Clamped so a backgrounded tab doesn't jump the whole animation on return.
      var dt = Math.min(now - last, 60);
      last = now;
      acc += dt * speed;
      if (fn) fn(easeInOutCubic(Math.min(acc / ms, 1)));
    }
    if (fn) fn(1);
  }

  /** A cancellable pause. */
  function wait(ms, isCurrent, opts) {
    return tween(ms, isCurrent, null, opts);
  }

  /** True when the OS asks for reduced motion. */
  function prefersReducedMotion() {
    if (!global.matchMedia) return false;
    return global.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  // =========================================================================
  // geometry — polyline maths for the canvas
  // =========================================================================

  /*
   * ELK hands back orthogonal routes as a list of bend points, so every edge is a
   * polyline. Everything here is analytic rather than going through the DOM's
   * getPointAtLength(): the comet has to be positioned during the same frame the node
   * radius changes, and re-measuring a <path> that is itself being rewritten each frame
   * forces a layout flush per edge per frame.
   *
   * Because node bodies can grow and shrink (the optional size channel), edges are
   * re-trimmed against the *current* half-extents every frame, which is why trimming is
   * a pure function of the points plus two boxes rather than something baked in at
   * layout time.
   */

  function lerp(a, b, t) {
    return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
  }

  function outside(p, c, box, pad) {
    return Math.abs(p.x - c.x) > box.hw + pad || Math.abs(p.y - c.y) > box.hh + pad;
  }

  /*
   * Fraction along `a->b` at which the segment leaves the padded rectangle around `c`.
   * Returns 1 when the segment never leaves it.
   */
  function exitFraction(a, b, c, box, pad) {
    var hw = box.hw + pad;
    var hh = box.hh + pad;
    var dx = b.x - a.x;
    var dy = b.y - a.y;
    var t = 1;
    var faces, i, tt;
    // Test both vertical faces and both horizontal faces, keep the earliest exit.
    if (dx !== 0) {
      faces = [c.x - hw, c.x + hw];
      for (i = 0; i < 2; i++) {
        tt = (faces[i] - a.x) / dx;
        if (tt >= 0 && tt <= 1 && Math.abs(a.y + dy * tt - c.y) <= hh + 1e-6) t = Math.min(t, tt);
      }
    }
    if (dy !== 0) {
      faces = [c.y - hh, c.y + hh];
      for (i = 0; i < 2; i++) {
        tt = (faces[i] - a.y) / dy;
        if (tt >= 0 && tt <= 1 && Math.abs(a.x + dx * tt - c.x) <= hw + 1e-6) t = Math.min(t, tt);
      }
    }
    return t;
  }

  /*
   * Clip a route so it starts on the source node's boundary and stops `gap` short of the
   * target's, leaving room for the arrowhead.
   */
  function trimRoute(points, source, target, sourceBox, targetBox, gap) {
    if (gap == null) gap = 4;
    if (points.length < 2) return points;

    /*
     * Anchor the route at both node centres before trimming.
     *
     * ELK hands back a route starting on the node border it laid out, which is not the
     * same border once a node has been scaled by the size channel — and if the first
     * point already sits outside the current body, the walk below would find no crossing
     * on that segment and silently skip it, leaving a detached stub. Starting from inside
     * the body guarantees exactly one clean crossing at each end, whatever size the node
     * currently is.
     */
    var pts = [source].concat(points, [target]);

    // Walk forward until we are clear of the source body.
    var head = 0;
    while (head < pts.length - 1 && !outside(pts[head + 1], source, sourceBox, 0)) head++;
    var startT = exitFraction(pts[head], pts[head + 1], source, sourceBox, 0);
    var start = lerp(pts[head], pts[head + 1], startT);

    // Walk backward until we are clear of the target body (plus the arrow gap).
    var tail = pts.length - 1;
    while (tail > head + 1 && !outside(pts[tail - 1], target, targetBox, gap)) tail--;
    var endT = exitFraction(pts[tail], pts[tail - 1], target, targetBox, gap);
    var end = lerp(pts[tail], pts[tail - 1], endT);

    var middle = pts.slice(head + 1, tail);
    var out = [start].concat(middle, [end]);
    // Degenerate routes (nodes almost touching) collapse to a stub rather than inverting
    // and drawing an arrow pointing the wrong way.
    return out.length >= 2 ? out : [start, end];
  }

  /** Cumulative arc lengths; `total` is the last entry. */
  function measure(points) {
    var cum = [0];
    var total = 0;
    for (var i = 1; i < points.length; i++) {
      total += Math.sqrt(
        Math.pow(points[i].x - points[i - 1].x, 2) + Math.pow(points[i].y - points[i - 1].y, 2)
      );
      cum.push(total);
    }
    return { cum: cum, total: total };
  }

  /** Point at fraction `t` (0..1) along the polyline. */
  function pointAt(points, cum, total, t) {
    if (points.length === 0) return { x: 0, y: 0 };
    if (points.length === 1 || total === 0) return points[0];
    var d = Math.min(Math.max(t, 0), 1) * total;
    var i = 1;
    while (i < cum.length - 1 && cum[i] < d) i++;
    var seg = cum[i] - cum[i - 1];
    var f = seg === 0 ? 0 : (d - cum[i - 1]) / seg;
    return lerp(points[i - 1], points[i], f);
  }

  /** Drop consecutive points that are effectively the same spot. */
  function dedupe(points, eps) {
    if (eps == null) eps = 0.6;
    var out = [];
    for (var i = 0; i < points.length; i++) {
      var p = points[i];
      var last = out[out.length - 1];
      if (!last || Math.abs(p.x - last.x) > eps || Math.abs(p.y - last.y) > eps) out.push(p);
    }
    return out.length >= 2 ? out : points.slice(0, 2);
  }

  function f1(v) {
    return v.toFixed(1);
  }

  /*
   * SVG path for a polyline, with the corners rounded off.
   *
   * ELK returns hard 90-degree orthogonal corners. Drawn literally they look brittle, and
   * a corner landing near the arrowhead reads as a kink rather than a turn. Each corner is
   * replaced with a quadratic curve whose radius is capped at a third of the shorter
   * adjoining segment, so short segments simply round less instead of the curve
   * overshooting into the neighbouring one.
   */
  function pathD(points, radius) {
    if (radius == null) radius = 9;
    var pts = dedupe(points);
    var i;
    if (!pts.length) return '';
    if (pts.length === 2 || radius <= 0) {
      var parts = [];
      for (i = 0; i < pts.length; i++) {
        parts.push((i ? 'L' : 'M') + f1(pts[i].x) + ' ' + f1(pts[i].y));
      }
      return parts.join('');
    }

    var d = 'M' + f1(pts[0].x) + ' ' + f1(pts[0].y);
    for (i = 1; i < pts.length - 1; i++) {
      var prev = pts[i - 1];
      var cur = pts[i];
      var next = pts[i + 1];
      var inLen = Math.sqrt(Math.pow(cur.x - prev.x, 2) + Math.pow(cur.y - prev.y, 2));
      var outLen = Math.sqrt(Math.pow(next.x - cur.x, 2) + Math.pow(next.y - cur.y, 2));
      var r = Math.min(radius, inLen / 3, outLen / 3);
      if (r < 0.5) {
        d += 'L' + f1(cur.x) + ' ' + f1(cur.y);
        continue;
      }
      var a = { x: cur.x + ((prev.x - cur.x) / inLen) * r, y: cur.y + ((prev.y - cur.y) / inLen) * r };
      var b = { x: cur.x + ((next.x - cur.x) / outLen) * r, y: cur.y + ((next.y - cur.y) / outLen) * r };
      d += 'L' + f1(a.x) + ' ' + f1(a.y) + 'Q' + f1(cur.x) + ' ' + f1(cur.y) + ' ' + f1(b.x) + ' ' + f1(b.y);
    }
    var end = pts[pts.length - 1];
    return d + 'L' + f1(end.x) + ' ' + f1(end.y);
  }

  /*
   * Direction the edge is travelling as it arrives, in radians.
   *
   * Looks back past any trailing hair-thin segments: an orthogonal route often ends with
   * a 1px jog, and taking that literally points the arrowhead sideways.
   */
  function endAngle(points, minLen) {
    if (minLen == null) minLen = 4;
    var pts = dedupe(points);
    if (pts.length < 2) return 0;
    var b = pts[pts.length - 1];
    for (var i = pts.length - 2; i >= 0; i--) {
      var a = pts[i];
      if (Math.sqrt(Math.pow(b.x - a.x, 2) + Math.pow(b.y - a.y, 2)) >= minLen) {
        return Math.atan2(b.y - a.y, b.x - a.x);
      }
    }
    return Math.atan2(b.y - pts[0].y, b.x - pts[0].x);
  }

  /*
   * Arrowhead for activation (filled triangle) or inhibition (perpendicular bar) — the
   * convention the rest of FLASH-P already uses, drawn as an explicit path rather than an
   * SVG marker so it can follow a node that is changing size.
   */
  function headD(tip, angle, sign) {
    var x = tip.x;
    var y = tip.y;
    var ca = Math.cos(angle);
    var sa = Math.sin(angle);
    if (sign > 0) {
      var L = 9;
      var W = 4.8;
      return (
        'M' + f1(x) + ' ' + f1(y) +
        'L' + f1(x - L * ca + W * sa) + ' ' + f1(y - L * sa - W * ca) +
        'L' + f1(x - L * ca - W * sa) + ' ' + f1(y - L * sa + W * ca) + 'Z'
      );
    }
    var B = 7;
    return (
      'M' + f1(x + B * sa) + ' ' + f1(y - B * ca) +
      'L' + f1(x - B * sa) + ' ' + f1(y + B * ca)
    );
  }

  /** Bounding box of a set of points, padded. */
  function bounds(points, pad) {
    if (pad == null) pad = 0;
    if (!points.length) return { x: 0, y: 0, w: 1, h: 1 };
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (var i = 0; i < points.length; i++) {
      var p = points[i];
      if (p.x < minX) minX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.x > maxX) maxX = p.x;
      if (p.y > maxY) maxY = p.y;
    }
    return { x: minX - pad, y: minY - pad, w: maxX - minX + pad * 2, h: maxY - minY + pad * 2 };
  }

  // =========================================================================
  // scale — value to visual encoding
  // =========================================================================

  /*
   * One signed magnitude `d` drives every channel, so colour and (optional) size can
   * never disagree:
   *
   *   algebraic / ode — d = log2(value / baseline). The wild type is normalised to 1, so
   *                     d = +1 means doubled and d = -1 means halved.
   *   rwr             — d = value - baseline. RWR scores *influence* (a signed signal in
   *                     roughly [-1, 1]), not a concentration, so a fold change is
   *                     meaningless there.
   *
   * The domain is fixed, never auto-fitted to the run: the legend has to mean the same
   * thing across steps, otherwise "getting redder" might just be the domain moving
   * underneath you.
   */

  /* Half-width of the colour/size domain, in the units of `signedMagnitude`. */
  var DOMAIN = {
    // The FLASH-P inhibition term is 2/(1+product(inhibitors)), so a node's
    // de-repression is capped at 2x — log2 fold rarely leaves +/-2 now.
    // Saturating at +/-2 (1/4x .. 4x) still covers the mid-range where nearly all the
    // interesting biology sits.
    algebraic: 2,
    psoup: 2,
    ode: 2,
    rwr: 1
  };

  /* Below this the node counts as unchanged (matches classifyDirection's 0.01). */
  var TOL = {
    algebraic: 0.01,
    psoup: 0.01,
    ode: 0.01,
    // RWR classifies direction at 1e-5, which is far too tight to filter a graph on — it
    // would keep every node in the network.
    rwr: 1e-3
  };

  var EPS = 1e-6;

  function domainOf(method) {
    return DOMAIN[method] == null ? 2 : DOMAIN[method];
  }

  function tolOf(method) {
    return TOL[method] == null ? 0.01 : TOL[method];
  }

  function clamp(v, lo, hi) {
    return Math.min(Math.max(v, lo), hi);
  }

  /** Signed deviation of `value` from `baseline`, in this method's units. */
  function signedMagnitude(value, baseline, method) {
    if (method === 'rwr') return value - baseline;
    return Math.log(Math.max(value, EPS) / Math.max(baseline, EPS)) / Math.LN2;
  }

  /** `signedMagnitude` normalised to [-1, 1] and clamped at the domain edge. */
  function normalized(value, baseline, method) {
    return clamp(signedMagnitude(value, baseline, method) / domainOf(method), -1, 1);
  }

  // ---- colour ----

  /*
   * Five-stop RdBu. The +/-0.5 stops are the same colours the Studio's results table uses
   * for "decreased" / "increased" (--neg / --pos), so a node that reads red here reads red
   * there too.
   */
  var LIGHT_STOPS = [
    { t: -1, hex: '#b2182b' },
    { t: -0.5, hex: '#d7301f' },
    { t: 0, hex: '#e8e8e8' },
    { t: 0.5, hex: '#2c7fb8' },
    { t: 1, hex: '#08519c' }
  ];

  /*
   * Same hue anchors, lifted in lightness and dropped in chroma so they sit on the dark
   * card colour without glowing.
   */
  var DARK_STOPS = [
    { t: -1, hex: '#ff6b52' },
    { t: -0.5, hex: '#f4794f' },
    { t: 0, hex: '#4a5a5e' },
    { t: 0.5, hex: '#4aa8e0' },
    { t: 1, hex: '#7ecbff' }
  ];

  function rampStops(dark) {
    return dark ? DARK_STOPS : LIGHT_STOPS;
  }

  function hexToRgb(hex) {
    return [
      parseInt(hex.slice(1, 3), 16),
      parseInt(hex.slice(3, 5), 16),
      parseInt(hex.slice(5, 7), 16)
    ];
  }

  function rgbToHex(r, g, b) {
    var vals = [r, g, b];
    var out = '#';
    for (var i = 0; i < 3; i++) {
      var s = Math.round(clamp(vals[i], 0, 255)).toString(16);
      out += s.length < 2 ? '0' + s : s;
    }
    return out;
  }

  function srgbToLinear(c) {
    return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  }

  function linearToSrgb(c) {
    return c <= 0.0031308 ? c * 12.92 : 1.055 * Math.pow(c, 1 / 2.4) - 0.055;
  }

  /*
   * Interpolate the ramp at `t` in [-1, 1].
   *
   * Mixing happens in linear-light rather than straight sRGB: blending gamma-encoded
   * channels darkens the midpoint and makes the neutral zone look like a dip in the data
   * rather than the absence of change.
   */
  function rampColor(t, dark) {
    var stops = rampStops(dark);
    var x = clamp(t, -1, 1);
    var lo = stops[0];
    var hi = stops[stops.length - 1];
    for (var i = 0; i < stops.length - 1; i++) {
      if (x >= stops[i].t && x <= stops[i + 1].t) {
        lo = stops[i];
        hi = stops[i + 1];
        break;
      }
    }
    var span = hi.t - lo.t;
    var f = span === 0 ? 0 : (x - lo.t) / span;
    var a = hexToRgb(lo.hex);
    var b = hexToRgb(hi.hex);
    function mix(c0, c1) {
      var l0 = srgbToLinear(c0 / 255);
      var l1 = srgbToLinear(c1 / 255);
      return linearToSrgb(l0 + (l1 - l0) * f) * 255;
    }
    return rgbToHex(mix(a[0], b[0]), mix(a[1], b[1]), mix(a[2], b[2]));
  }

  /** Node fill for a value, straight from the raw numbers. */
  function fillFor(value, baseline, method, dark) {
    return rampColor(normalized(value, baseline, method), dark);
  }

  /*
   * Readable text colour for a given fill.
   *
   * Deliberately NOT --fg: the fill is a data colour, not a theme colour, so the label has
   * to follow the data or it will vanish at one end of the ramp in one of the two themes.
   */
  function labelInk(fill) {
    var rgb = hexToRgb(fill);
    var lum =
      0.2126 * srgbToLinear(rgb[0] / 255) +
      0.7152 * srgbToLinear(rgb[1] / 255) +
      0.0722 * srgbToLinear(rgb[2] / 255);
    return lum > 0.42 ? '#0d1b16' : '#f4f8f6';
  }

  // ---- optional size channel ----

  var SIZE_MIN = 0.62;
  var SIZE_MAX = 1.45;

  /*
   * Optional second channel: scale the node body by its value.
   *
   * Driven by the same clamped, log-scaled magnitude as the colour, so it can neither blow
   * up nor shrink a node out of view. Layout always reserves space at SIZE_MAX regardless
   * of whether this is on, so enabling it can only ever add whitespace — nodes can never
   * collide, and the layout never has to be recomputed when the toggle flips.
   *
   * Anchored so t = -1 lands exactly on SIZE_MIN and t = +1 exactly on SIZE_MAX rather
   * than overshooting and being clipped; a clipped ramp would make every strongly-down
   * node the same size and hide real differences at the bottom end.
   */
  function radiusFactor(value, baseline, method) {
    var t = normalized(value, baseline, method);
    return t >= 0 ? 1 + t * (SIZE_MAX - 1) : 1 + t * (1 - SIZE_MIN);
  }

  // ---- categorical palette (chart series) ----

  /*
   * The diverging ramp above encodes a *value*; this encodes *identity* — which line on
   * the chart is which node. Kept theme-aware for the same reason as the ramp: the light
   * set is too dark to read against the dark card colour, and the dark set washes out on
   * paper-white.
   */
  var SERIES_LIGHT = [
    '#2563eb', '#dc2626', '#15803d', '#b45309', '#7c3aed',
    '#be185d', '#0e7490', '#c2410c', '#4338ca', '#4d7c0f'
  ];
  var SERIES_DARK = [
    '#7aa8ff', '#ff8080', '#5fd18a', '#f0b64d', '#b79bff',
    '#ff8fc0', '#4fc3d9', '#ff9a5c', '#9aa6ff', '#a8cf5f'
  ];

  /** Stable index for a node id, so a colour belongs to the node, not to its position. */
  function hashIndex(id, buckets) {
    var h = 0;
    for (var i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
    return Math.abs(h) % buckets;
  }

  /*
   * Line colour for a node.
   *
   * Keyed to the node id rather than its position in the plotted list: assigning by index
   * means toggling one line off recolours every line after it, which silently breaks the
   * link between the chart and anything else showing that node. `taken` lets the caller
   * resolve the occasional hash collision so two lines on screen at once never share a
   * colour.
   */
  function seriesColor(id, dark, taken) {
    var palette = dark ? SERIES_DARK : SERIES_LIGHT;
    var start = hashIndex(id, palette.length);
    if (!taken) return palette[start];
    for (var i = 0; i < palette.length; i++) {
      var c = palette[(start + i) % palette.length];
      if (!taken[c]) {
        taken[c] = true;
        return c;
      }
    }
    return palette[start];
  }

  /** Assign every plotted node a distinct colour, in one pass. */
  function seriesColors(ids, dark) {
    var taken = {};
    var out = {};
    for (var i = 0; i < ids.length; i++) out[ids[i]] = seriesColor(ids[i], dark, taken);
    return out;
  }

  // ---- formatting ----

  /** Direction glyph, so colour is never the only channel. */
  function directionGlyph(value, baseline, method) {
    var d = signedMagnitude(value, baseline, method);
    var tol = tolOf(method);
    if (d > tol) return '▲';
    if (d < -tol) return '▼';
    return '–';
  }

  /** The value as shown in the third box. RWR signals are signed and never folds. */
  function formatValue(v, method) {
    if (method === 'rwr') return (v >= 0 ? '+' : '') + v.toFixed(3);
    return v.toFixed(2);
  }

  /** Axis/legend tick labels for the current method. */
  function legendTicks(method) {
    if (method === 'rwr') {
      return [
        { t: -1, label: '−1' },
        { t: -0.5, label: '−0.5' },
        { t: 0, label: '0' },
        { t: 0.5, label: '+0.5' },
        { t: 1, label: '+1' }
      ];
    }
    return [
      { t: -1, label: '≤¼×' },
      { t: -0.5, label: '½×' },
      { t: 0, label: '1×' },
      { t: 0.5, label: '2×' },
      { t: 1, label: '≥4×' }
    ];
  }

  /** What the ramp is measuring, for the legend caption. */
  function legendCaption(method) {
    return method === 'rwr'
      ? 'signal Δ vs baseline — RWR scores influence, not concentration. 0 = unaffected.'
      : 'fold change vs baseline (log₂ scale)';
  }

  // =========================================================================
  // trace — turn a raw solver trace into something a person can step through
  // =========================================================================

  /*
   * The engines run tens to hundreds of iterations, so one-click-per-iteration is not a
   * usable control. But the trace is not uniform either — it has two genuinely different
   * regimes, and the view presents them as two labelled phases rather than pretending the
   * whole run is one kind of thing:
   *
   *   Phase A "Spread"  — the perturbation is still reaching nodes it has never reached
   *                       before. Because every engine updates all nodes synchronously
   *                       from the previous iteration's values, a node can only move at
   *                       iteration k if it sits within k edges of the perturbation. So
   *                       there IS a real wavefront here, and animating a signal
   *                       travelling down an edge is truthful.
   *
   *   Phase B "Settle"  — no new nodes are recruited; everything relaxes towards steady
   *                       state together under damping. Presenting this as a continuing
   *                       relay would be a lie, so it is chaptered by *progress* instead
   *                       and labelled as simultaneous.
   */

  /* Hard cap on the wave phase, so a pathological chain can't produce 40 clicks. */
  var MAX_WAVE_STEPS = 15;

  /* Progress checkpoints that chapter the settle phase. */
  var RELAX_CHECKPOINTS = [0.4, 0.55, 0.7, 0.82, 0.9, 0.95, 0.99];

  function buildTrace(result, edges, seeds, method) {
    var iters = result.iterations;
    var last = iters.length - 1;
    var tol = tolOf(method);
    var nodeIds = Object.keys((iters[0] && iters[0].values) || {});
    var i, k, id;

    // The baseline run can be a different length to the perturbed run, so hold its final
    // value once it has finished rather than running off the end.
    var baseIters = result.baselineIterations || [];
    function baselineAt(iter) {
      var row = baseIters[Math.min(iter, baseIters.length - 1)];
      return (row && row.values) || result.wtValues || {};
    }

    // ---- arrival step per node ----
    var arrival = {};
    for (i = 0; i < nodeIds.length; i++) arrival[nodeIds[i]] = Infinity;
    for (k = 0; k <= last; k++) {
      var vals = iters[k].values;
      var base = baselineAt(k);
      for (i = 0; i < nodeIds.length; i++) {
        id = nodeIds[i];
        if (arrival[id] !== Infinity) continue;
        var d = Math.abs(signedMagnitude(vals[id] || 0, base[id] || 0, method));
        if (d > tol) arrival[id] = k;
      }
    }
    /*
     * A perturbed node is the origin of the story, so it arrives at step 0 by definition.
     * Its *value* usually doesn't move until iteration 1 (damping means the modifier takes
     * a step to bite), which would otherwise make it look like the knockout was "reached"
     * one hop from itself.
     */
    for (i = 0; i < seeds.length; i++) arrival[seeds[i]] = 0;

    var movers = [];
    for (i = 0; i < nodeIds.length; i++) {
      if (arrival[nodeIds[i]] !== Infinity) movers.push(nodeIds[i]);
    }
    var rawDepth = 0;
    for (i = 0; i < movers.length; i++) {
      var a = arrival[movers[i]];
      if (isFinite(a) && a > rawDepth) rawDepth = a;
    }
    var depth = Math.min(rawDepth, MAX_WAVE_STEPS, last);

    // ---- progress towards steady state ----
    var final = (iters[last] && iters[last].values) || {};
    function dist(vals) {
      var sum = 0;
      for (var j = 0; j < nodeIds.length; j++) {
        var dd = (vals[nodeIds[j]] || 0) - (final[nodeIds[j]] || 0);
        sum += dd * dd;
      }
      return Math.sqrt(sum);
    }
    var d0 = dist((iters[0] && iters[0].values) || {});
    function progressAt(kk) {
      return d0 <= 1e-12 ? 1 : 1 - dist(iters[kk].values) / d0;
    }

    // ---- assemble the presentation steps ----
    var chosen = [];
    for (k = 0; k <= depth; k++) chosen.push({ iter: k, kind: 'wave' });

    var cursor = depth;
    for (i = 0; i < RELAX_CHECKPOINTS.length; i++) {
      for (k = cursor + 1; k <= last; k++) {
        if (progressAt(k) >= RELAX_CHECKPOINTS[i]) {
          if (k > cursor) chosen.push({ iter: k, kind: 'relax' });
          cursor = k;
          break;
        }
      }
    }
    if (chosen.length && chosen[chosen.length - 1].iter !== last) {
      chosen.push({ iter: last, kind: 'steady' });
    } else if (chosen.length) {
      chosen[chosen.length - 1].kind = 'steady';
    } else {
      chosen.push({ iter: last, kind: 'steady' });
    }

    var steps = [];
    for (i = 0; i < chosen.length; i++) {
      var c = chosen[i];
      var fromIter = i === 0 ? c.iter : chosen[i - 1].iter;
      var cur = iters[c.iter].values;
      var prev = iters[fromIter].values;
      var j, eid;

      var moved = [];
      for (j = 0; j < nodeIds.length; j++) {
        id = nodeIds[j];
        if (Math.abs(signedMagnitude(cur[id] || 0, prev[id] || 0, method)) > tol) moved.push(id);
      }

      var arrived = [];
      if (c.kind === 'wave') {
        for (j = 0; j < nodeIds.length; j++) {
          if (arrival[nodeIds[j]] === c.iter) arrived.push(nodeIds[j]);
        }
      }

      var activeEdges = [];
      if (c.kind === 'wave') {
        // Only edges that actually delivered the news: the source had already moved, and
        // the target moves for the first time now.
        var arrivedSet = {};
        for (j = 0; j < arrived.length; j++) arrivedSet[arrived[j]] = true;
        for (j = 0; j < edges.length; j++) {
          var e = edges[j];
          if (e.carriesSignal && arrival[e.s] <= c.iter - 1 && arrivedSet[e.t]) activeEdges.push(e.id);
        }
      } else {
        // Everything is adjusting at once — so every edge out of a node that moved in this
        // window is live. That is genuinely what the solver does.
        var movedSet = {};
        for (j = 0; j < moved.length; j++) movedSet[moved[j]] = true;
        for (j = 0; j < edges.length; j++) {
          if (edges[j].carriesSignal && movedSet[edges[j].s]) activeEdges.push(edges[j].id);
        }
      }

      steps.push({
        index: i,
        iter: c.iter,
        fromIter: fromIter,
        kind: c.kind,
        arrived: arrived,
        moved: moved,
        activeEdges: activeEdges,
        progress: progressAt(c.iter),
        caption: caption(c.kind, c.iter, fromIter, arrived, moved, progressAt(c.iter), result, seeds)
      });
    }

    return {
      steps: steps,
      arrival: arrival,
      depth: depth,
      movers: movers,
      baselineAt: baselineAt,
      totalIterations: last
    };
  }

  function list(xs, max) {
    if (max == null) max = 3;
    if (!xs.length) return '';
    if (xs.length <= max) {
      return xs.length === 1 ? xs[0] : xs.slice(0, -1).join(', ') + ' and ' + xs[xs.length - 1];
    }
    return xs.slice(0, max).join(', ') + ' and ' + (xs.length - max) + ' more';
  }

  function caption(kind, iter, fromIter, arrived, moved, progress, result, seeds) {
    var origin = seeds.length ? list(seeds, 2) : 'the perturbation';
    var seedSet = {};
    var i;
    for (i = 0; i < seeds.length; i++) seedSet[seeds[i]] = true;

    if (kind === 'wave') {
      if (iter === 0) {
        return 'Starting point — ' + origin + ' ' + (seeds.length > 1 ? 'are' : 'is') +
          ' set, every node still sits at its baseline value.';
      }
      if (!arrived.length) {
        var movedSeeds = [];
        for (i = 0; i < moved.length; i++) {
          if (seedSet[moved[i]]) movedSeeds.push(moved[i]);
        }
        /*
         * Only true while the perturbation is still confined to its own node: damping
         * means the modifier takes one iteration to show up, and until anything else has
         * moved, nothing downstream can have seen it. Once other nodes are moving too,
         * saying this would be plainly false.
         */
        if (movedSeeds.length && movedSeeds.length === moved.length) {
          return 'Iteration ' + iter + ' — the perturbation takes effect on ' +
            list(movedSeeds) + ' itself. Nothing downstream has seen it yet; regulators ' +
            "only read the previous iteration's values.";
        }
        return 'Iteration ' + iter + ' — no new nodes are reached, but ' +
          (moved.length === 1 ? 'one node is' : moved.length + ' nodes are') +
          ' still adjusting to what already arrived.';
      }
      return 'Iteration ' + iter + ' — the change reaches ' + list(arrived) + ', ' +
        iter + ' regulatory ' + (iter === 1 ? 'step' : 'steps') + ' from ' + origin + '.';
    }

    if (kind === 'relax') {
      var span = fromIter + 1 === iter ? 'Iteration ' + iter : 'Iterations ' + (fromIter + 1) + '–' + iter;
      return span + ' — no new nodes are recruited; every node is still adjusting ' +
        'together. ' + Math.round(progress * 100) + '% of the way to steady state.';
    }

    if (!result.converged) {
      return 'Stopped at the ' + iter + '-iteration limit without converging — values ' +
        'were still changing. Treat these numbers as provisional.';
    }
    return 'Steady state, reached after ' + result.convergedAt + ' iterations. Nothing changes from here.';
  }

  /** Header text for the two-phase banner. */
  function phaseLabel(kind) {
    if (kind === 'wave') return 'Phase 1 of 2 · Spread — the perturbation is still reaching new nodes';
    if (kind === 'relax') return 'Phase 2 of 2 · Settle — all nodes update together each iteration';
    return 'Steady state';
  }

  /** Shown once, on first entry to the settle phase. */
  var RELAX_NOTE =
    'From here the model updates every node simultaneously on every iteration — this is ' +
    'not a sequential relay. The animation shows all edges firing at once because that is ' +
    'what the equations do.';

  /** How to describe the x-axis for this method. An iteration is not time. */
  function iterationNote(method) {
    if (method === 'ode') {
      return 'Iterations of an Euler integration — roughly t = iteration × 0.1 model time units.';
    }
    return 'Iterations of a fixed-point solver, not biological time.';
  }

  // =========================================================================
  // focus — which nodes and edges we actually draw
  // =========================================================================

  /*
   * Two jobs, and the first one is a correctness issue rather than a cosmetic one:
   *
   *  1. Work out the real regulatory edges *for this method*. The algebraic and ODE
   *     engines build their equations from algebraic_equations.json when it exists and
   *     only fall back to the signed network edges when it doesn't. Some networks have
   *     edges that never made it into the equation file, and those edges carry no signal
   *     at all under those methods. Drawing a travelling comet down one of them would show
   *     the user something the model never did. RWR is different again: it reads net.edges
   *     directly, so every edge is live.
   *
   *  2. Cut the graph down to the part that actually responds — "hide what didn't change"
   *     alone is not enough of a filter on a hub knockout, hence the magnitude cut and the
   *     connectivity repair below.
   */

  /** Largest focused set we will draw before falling back to a top-N cut. */
  var MAX_FOCUS = 28;

  /** Cap on how many bridge nodes we will add to reconnect a fragmented focus set. */
  var MAX_BRIDGES = 5;

  /*
   * Key for a signed source->target pair.
   *
   * JSON rather than a delimiter-joined string so a node id containing the delimiter
   * cannot collide two different edges into one key.
   */
  function edgeKey(s, t, sign) {
    return JSON.stringify([s, t, sign]);
  }

  /*
   * The edge set the chosen method actually propagates along.
   *
   * Every network edge is returned either way — edges the equations don't use are kept but
   * flagged carriesSignal:false so the canvas can draw them as inert context rather than
   * silently dropping them (a missing edge is its own kind of lie) and never animate them.
   *
   * The Studio's compact `net.edges` carries no edge id (unlike the website's network.json,
   * which has `eid`), so ids are synthesised from the array position. That is stable for a
   * given embedded network, which is all the canvas needs to key its element map on.
   */
  function regulatoryEdges(net, algEq, method) {
    var out = [];
    var i;

    if (method === 'rwr') {
      // RWR walks the signed network graph itself, so every edge is live.
      for (i = 0; i < net.edges.length; i++) {
        out.push({
          id: 'e' + i,
          s: net.edges[i].s,
          t: net.edges[i].t,
          sign: net.edges[i].x > 0 ? 1 : -1,
          carriesSignal: true
        });
      }
      return out;
    }

    var eqs = global.FLASHPSIM.buildEquations(net, algEq);
    // Pairs the equations actually reference, as "source->target" with a sign.
    var used = {};
    var targets = Object.keys(eqs);
    var j;
    for (i = 0; i < targets.length; i++) {
      var eq = eqs[targets[i]];
      for (j = 0; j < eq.activators.length; j++) used[edgeKey(eq.activators[j], targets[i], 1)] = true;
      for (j = 0; j < eq.inhibitors.length; j++) used[edgeKey(eq.inhibitors[j], targets[i], -1)] = true;
    }

    var seen = {};
    for (i = 0; i < net.edges.length; i++) {
      var e = net.edges[i];
      var sign = e.x > 0 ? 1 : -1;
      var key = edgeKey(e.s, e.t, sign);
      seen[key] = true;
      out.push({ id: 'e' + i, s: e.s, t: e.t, sign: sign, carriesSignal: !!used[key] });
    }

    /*
     * An equation can also reference a regulator with no matching network edge. Draw it —
     * it is driving the maths, so hiding it would leave a node changing for no visible
     * reason.
     */
    var usedKeys = Object.keys(used);
    var synthetic = 0;
    for (i = 0; i < usedKeys.length; i++) {
      if (seen[usedKeys[i]]) continue;
      var parsed = JSON.parse(usedKeys[i]);
      if (!eqs[parsed[1]]) continue;
      out.push({ id: 'eq' + synthetic++, s: parsed[0], t: parsed[1], sign: parsed[2], carriesSignal: true });
    }
    return out;
  }

  /** Nodes the user directly perturbed — the origin of the story. */
  function seedNodes(net, perturbation) {
    var ids = {};
    var i;
    for (i = 0; i < net.nodes.length; i++) ids[net.nodes[i].id] = true;

    var seeds = {};
    var gm = perturbation.geneModifiers || {};
    var keys = Object.keys(gm);
    for (i = 0; i < keys.length; i++) {
      if (ids[keys[i]] && gm[keys[i]] !== 1) seeds[keys[i]] = true;
    }
    var ex = perturbation.exogenous || {};
    keys = Object.keys(ex);
    for (i = 0; i < keys.length; i++) {
      if (ids[keys[i]] && ex[keys[i]] !== 0) seeds[keys[i]] = true;
    }
    return Object.keys(seeds);
  }

  /*
   * The focused sub-network: seeds, the biggest responders, and whatever is needed to keep
   * them joined up.
   */
  function activeSubgraph(net, result, trace, edges, seeds, method) {
    var tol = tolOf(method);
    var nodeIds = [];
    var i, j, id;
    for (i = 0; i < net.nodes.length; i++) nodeIds.push(net.nodes[i].id);

    // Peak deviation from the baseline over the whole run, per node.
    var dev = {};
    for (i = 0; i < nodeIds.length; i++) dev[nodeIds[i]] = 0;
    for (var k = 0; k < result.iterations.length; k++) {
      var vals = result.iterations[k].values;
      var base = trace.baselineAt(k);
      for (i = 0; i < nodeIds.length; i++) {
        id = nodeIds[i];
        var d = Math.abs(signedMagnitude(vals[id] || 0, base[id] || 0, method));
        if (d > dev[id]) dev[id] = d;
      }
    }

    var moved = [];
    for (i = 0; i < nodeIds.length; i++) {
      if (dev[nodeIds[i]] > tol) moved.push(nodeIds[i]);
    }

    var core = {};
    for (i = 0; i < seeds.length; i++) core[seeds[i]] = true;
    if (result.phenotype) core[result.phenotype] = true;

    /*
     * Nothing responded — a focused view would just be the seed on its own, which tells the
     * user nothing about why. Show the whole network instead and say so.
     */
    if (!moved.length) {
      var allEdges = {};
      for (i = 0; i < edges.length; i++) allEdges[edges[i].id] = true;
      var allNodes = {};
      for (i = 0; i < nodeIds.length; i++) allNodes[nodeIds[i]] = true;
      return {
        nodes: allNodes,
        edges: allEdges,
        truncated: false,
        responders: 0,
        bridges: {},
        fallback: 'none-changed'
      };
    }

    var keep = {};
    var coreKeys = Object.keys(core);
    for (i = 0; i < coreKeys.length; i++) keep[coreKeys[i]] = true;
    for (i = 0; i < moved.length; i++) keep[moved[i]] = true;
    var truncated = false;

    if (Object.keys(keep).length > MAX_FOCUS) {
      /*
       * Keep the story spine — everything on a shortest path from a seed to the phenotype —
       * then fill the remaining budget with the largest responders.
       */
      var spine = {};
      for (i = 0; i < coreKeys.length; i++) spine[coreKeys[i]] = true;
      if (result.phenotype) {
        for (i = 0; i < seeds.length; i++) {
          var path = shortestPath(edges, seeds[i], result.phenotype);
          for (j = 0; j < path.length; j++) spine[path[j]] = true;
        }
      }
      var ranked = [];
      for (i = 0; i < moved.length; i++) {
        if (!spine[moved[i]]) ranked.push(moved[i]);
      }
      ranked.sort(function (a, b) {
        return dev[b] - dev[a];
      });
      var budget = Math.max(0, MAX_FOCUS - Object.keys(spine).length);
      ranked = ranked.slice(0, budget);

      var cut = {};
      var spineKeys = Object.keys(spine);
      for (i = 0; i < spineKeys.length; i++) cut[spineKeys[i]] = true;
      for (i = 0; i < ranked.length; i++) cut[ranked[i]] = true;
      if (Object.keys(cut).length < Object.keys(keep).length) truncated = true;
      keep = cut;
    }

    /*
     * Reconnect fragments, otherwise an inhibitory chain that cancels out in the middle
     * leaves a hole and the picture stops making sense.
     */
    var bridges = repairConnectivity(edges, keep);
    var bridgeKeys = Object.keys(bridges);
    for (i = 0; i < bridgeKeys.length; i++) keep[bridgeKeys[i]] = true;

    var keptEdges = {};
    for (i = 0; i < edges.length; i++) {
      if (keep[edges[i].s] && keep[edges[i].t]) keptEdges[edges[i].id] = true;
    }

    return {
      nodes: keep,
      edges: keptEdges,
      truncated: truncated,
      responders: moved.length,
      bridges: bridges,
      fallback: null
    };
  }

  /** Directed shortest path (inclusive of both ends); empty if unreachable. */
  function shortestPath(edges, from, to) {
    if (from === to) return [from];
    var out = {};
    var i;
    for (i = 0; i < edges.length; i++) {
      if (!out[edges[i].s]) out[edges[i].s] = [];
      out[edges[i].s].push(edges[i].t);
    }
    var prev = {};
    var seen = {};
    seen[from] = true;
    var queue = [from];
    while (queue.length) {
      var cur = queue.shift();
      var nexts = out[cur] || [];
      for (i = 0; i < nexts.length; i++) {
        var nxt = nexts[i];
        if (seen[nxt]) continue;
        seen[nxt] = true;
        prev[nxt] = cur;
        if (nxt === to) {
          var path = [to];
          var p = to;
          while (prev[p]) {
            p = prev[p];
            path.push(p);
          }
          return path.reverse();
        }
        queue.push(nxt);
      }
    }
    return [];
  }

  /*
   * Add as few nodes as possible to make `keep` a single connected component when edge
   * direction is ignored.
   */
  function repairConnectivity(edges, keep) {
    var bridges = {};
    var undirected = {};
    var i;
    for (i = 0; i < edges.length; i++) {
      if (!undirected[edges[i].s]) undirected[edges[i].s] = [];
      undirected[edges[i].s].push(edges[i].t);
      if (!undirected[edges[i].t]) undirected[edges[i].t] = [];
      undirected[edges[i].t].push(edges[i].s);
    }

    function components() {
      var unvisited = {};
      var keys = Object.keys(keep);
      var n;
      for (n = 0; n < keys.length; n++) unvisited[keys[n]] = true;
      var comps = [];
      var remaining = Object.keys(unvisited);
      while (remaining.length) {
        var start = remaining[0];
        var comp = [];
        var stack = [start];
        delete unvisited[start];
        while (stack.length) {
          var cur = stack.pop();
          comp.push(cur);
          var nbrs = undirected[cur] || [];
          for (n = 0; n < nbrs.length; n++) {
            if (unvisited[nbrs[n]]) {
              delete unvisited[nbrs[n]];
              stack.push(nbrs[n]);
            }
          }
        }
        comps.push(comp);
        remaining = Object.keys(unvisited);
      }
      return comps;
    }

    var comps = components();
    while (comps.length > 1 && Object.keys(bridges).length < MAX_BRIDGES) {
      // Shortest undirected hop from the largest component to any other.
      comps.sort(function (a, b) {
        return b.length - a.length;
      });
      var target = {};
      for (i = 1; i < comps.length; i++) {
        for (var m = 0; m < comps[i].length; m++) target[comps[i][m]] = true;
      }
      var path = undirectedPath(undirected, comps[0], target);
      if (!path.length) break;
      var added = false;
      for (i = 0; i < path.length; i++) {
        if (!keep[path[i]]) {
          bridges[path[i]] = true;
          keep[path[i]] = true;
          added = true;
        }
      }
      if (!added) break;
      comps = components();
    }
    return bridges;
  }

  function undirectedPath(adj, from, to) {
    var prev = {};
    var seen = {};
    var i;
    for (i = 0; i < from.length; i++) seen[from[i]] = true;
    var queue = from.slice();
    while (queue.length) {
      var cur = queue.shift();
      var nbrs = adj[cur] || [];
      for (i = 0; i < nbrs.length; i++) {
        var nxt = nbrs[i];
        if (seen[nxt]) continue;
        seen[nxt] = true;
        prev[nxt] = cur;
        if (to[nxt]) {
          var path = [nxt];
          var p = nxt;
          while (prev[p]) {
            p = prev[p];
            path.push(p);
          }
          return path.reverse();
        }
        queue.push(nxt);
      }
    }
    return [];
  }

  // =========================================================================
  // layout — standalone ELK layered/orthogonal layout
  // =========================================================================

  /*
   * The routing configuration is the one flashp-graph.js already uses (runElkOrthogonal)
   * with the Cytoscape half removed, so the propagation view lays a network out the same
   * way the viewer does and the two read as the same picture. flashp-graph.js is
   * deliberately left alone — this is additive.
   *
   * Unlike the viewer, this returns plain data: node centres plus each edge's orthogonal
   * polyline, ready to render as SVG.
   */

  /** Node body at scale 1. Wide enough for a gene symbol at 11px/600. */
  var BODY_W = 96;
  var BODY_H = 34;

  /** The three-value strip that sits under every node body. */
  var BOX_H = 26;
  var BOX_GAP = 4;

  /*
   * Footprint handed to ELK — the node *body* at its largest, and nothing more.
   *
   * The value strip is deliberately excluded. ELK attaches edge routes to the box it is
   * given, so folding the strip into the box would start every route below the strip,
   * visibly detached from the node it leaves. Instead the strip hangs into the inter-layer
   * gap, which is widened by exactly its height below.
   *
   * The box is always measured at SIZE_MAX whether or not the size channel is on, so
   * spacing is reserved for the largest a node can ever get: enabling "scale size by
   * value" can only ever add whitespace, nodes can never collide, and the layout never
   * needs recomputing when the toggle flips.
   */
  var CELL_W = Math.ceil(BODY_W * SIZE_MAX);
  var CELL_H = Math.ceil(BODY_H * SIZE_MAX);

  /*
   * Distance from the node's centre down to the top of the value strip.
   *
   * Fixed at the max body size so the strip never moves as a node breathes — one that
   * tracked the current size would jitter every frame and make the numbers hard to read.
   */
  var STRIP_TOP = (BODY_H * SIZE_MAX) / 2 + BOX_GAP;

  /** How far the strip hangs below the body box ELK knows about. */
  var STRIP_OVERHANG = STRIP_TOP + BOX_H - CELL_H / 2;

  var elkInstance = null;

  var LAYOUT_OPTIONS = {
    'elk.algorithm': 'layered',
    'elk.direction': 'DOWN',
    'elk.edgeRouting': 'ORTHOGONAL',
    'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
    // The inter-layer gap has to clear the value strip hanging below each node, on top of
    // ordinary breathing room.
    'elk.layered.spacing.nodeNodeBetweenLayers': String(Math.ceil(STRIP_OVERHANG) + 62),
    'elk.spacing.nodeNode': '36',
    'elk.spacing.edgeNode': '18',
    'elk.spacing.edgeEdge': '14',
    'elk.layered.spacing.edgeEdgeBetweenLayers': '14',
    'elk.layered.spacing.edgeNodeBetweenLayers': '20'
  };

  /*
   * Lay out the whole network once.
   *
   * The focused view reuses these same coordinates and simply hides what it doesn't need,
   * so toggling focus never makes a node jump — and the focused picture stays spatially
   * consistent with the full one, which matters when someone is trying to hold the
   * topology in their head.
   */
  function layoutGraph(nodes, edges) {
    var ids = {};
    var i;
    for (i = 0; i < nodes.length; i++) ids[nodes[i].id] = true;

    var usable = [];
    for (i = 0; i < edges.length; i++) {
      if (ids[edges[i].s] && ids[edges[i].t] && edges[i].s !== edges[i].t) usable.push(edges[i]);
    }

    var children = [];
    for (i = 0; i < nodes.length; i++) {
      children.push({ id: nodes[i].id, width: CELL_W, height: CELL_H });
    }
    var elkEdges = [];
    for (i = 0; i < usable.length; i++) {
      elkEdges.push({ id: usable[i].id, sources: [usable[i].s], targets: [usable[i].t] });
    }

    var graph = { id: 'root', layoutOptions: LAYOUT_OPTIONS, children: children, edges: elkEdges };

    if (!elkInstance) elkInstance = new global.ELK();

    return elkInstance.layout(graph).then(function (res) {
      var out = { nodes: {}, edges: {}, width: res.width || 0, height: res.height || 0 };
      var n, ed, sec, src, tgt, points, b;

      // The ELK box *is* the node body, so positions and routes need no adjustment.
      var kids = res.children || [];
      for (n = 0; n < kids.length; n++) {
        out.nodes[kids[n].id] = {
          id: kids[n].id,
          x: (kids[n].x || 0) + (kids[n].width || CELL_W) / 2,
          y: (kids[n].y || 0) + (kids[n].height || CELL_H) / 2
        };
      }

      var routed = res.edges || [];
      for (n = 0; n < routed.length; n++) {
        ed = routed[n];
        sec = ed.sections && ed.sections[0];
        src = out.nodes[(ed.sources || [])[0]];
        tgt = out.nodes[(ed.targets || [])[0]];
        if (!sec) {
          if (src && tgt) out.edges[ed.id] = { id: ed.id, points: [pt(src), pt(tgt)] };
          continue;
        }
        points = [{ x: sec.startPoint.x, y: sec.startPoint.y }];
        var bends = sec.bendPoints || [];
        for (b = 0; b < bends.length; b++) points.push({ x: bends[b].x, y: bends[b].y });
        points.push({ x: sec.endPoint.x, y: sec.endPoint.y });
        out.edges[ed.id] = { id: ed.id, points: points };
      }

      // Self-loops and edges ELK skipped still need something to draw.
      for (n = 0; n < edges.length; n++) {
        if (out.edges[edges[n].id]) continue;
        src = out.nodes[edges[n].s];
        tgt = out.nodes[edges[n].t];
        if (src && tgt) out.edges[edges[n].id] = { id: edges[n].id, points: [pt(src), pt(tgt)] };
      }

      return out;
    });
  }

  function pt(n) {
    return { x: n.x, y: n.y };
  }

  // =========================================================================

  global.FLASHPPROP = {
    // anim
    ABORT: ABORT,
    isAbort: isAbort,
    easeInOutCubic: easeInOutCubic,
    tween: tween,
    wait: wait,
    prefersReducedMotion: prefersReducedMotion,
    // geometry
    trimRoute: trimRoute,
    measure: measure,
    pointAt: pointAt,
    dedupe: dedupe,
    pathD: pathD,
    endAngle: endAngle,
    headD: headD,
    bounds: bounds,
    // scale
    DOMAIN: DOMAIN,
    TOL: TOL,
    clamp: clamp,
    signedMagnitude: signedMagnitude,
    normalized: normalized,
    rampStops: rampStops,
    rampColor: rampColor,
    fillFor: fillFor,
    labelInk: labelInk,
    SIZE_MIN: SIZE_MIN,
    SIZE_MAX: SIZE_MAX,
    radiusFactor: radiusFactor,
    seriesColor: seriesColor,
    seriesColors: seriesColors,
    directionGlyph: directionGlyph,
    formatValue: formatValue,
    legendTicks: legendTicks,
    legendCaption: legendCaption,
    // trace
    buildTrace: buildTrace,
    phaseLabel: phaseLabel,
    RELAX_NOTE: RELAX_NOTE,
    iterationNote: iterationNote,
    MAX_WAVE_STEPS: MAX_WAVE_STEPS,
    // focus
    MAX_FOCUS: MAX_FOCUS,
    regulatoryEdges: regulatoryEdges,
    seedNodes: seedNodes,
    activeSubgraph: activeSubgraph,
    // layout
    BODY_W: BODY_W,
    BODY_H: BODY_H,
    BOX_H: BOX_H,
    BOX_GAP: BOX_GAP,
    CELL_W: CELL_W,
    CELL_H: CELL_H,
    STRIP_TOP: STRIP_TOP,
    STRIP_OVERHANG: STRIP_OVERHANG,
    layoutGraph: layoutGraph
  };
})(typeof window !== 'undefined' ? window : this);
