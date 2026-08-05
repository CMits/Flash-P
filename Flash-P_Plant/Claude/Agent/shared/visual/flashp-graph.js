/*
 * flashp-graph.js — shared Cytoscape graph engine for the FLASH-P standalone
 * visualiser. This is a faithful port of the website renderer
 *   FLASHP_WEBSITE/Flash-P-AI/src/components/traits/NetworkGraph.tsx
 * so the standalone HTML / SVG / PNG match the live site pixel-for-pixel:
 * same stylesheet, same NODE_SCALE, same hand-rolled ELK orthogonal layout
 * (elk.layout -> node positions + per-edge segment routing), same dagre/fcose
 * fallbacks, same selection/inspector behaviour.
 *
 * Environment: plain browser. Expects these globals already loaded:
 *   cytoscape, cytoscapeDagre (cytoscape-dagre), cytoscapeFcose (cytoscape-fcose),
 *   ELK (elkjs/elk.bundled.js). Optionally cytoscapeSvg (cytoscape-svg) for SVG export.
 *
 * Usage (both the artifact HTML and the headless renderer call this):
 *   const app = FLASHP.init({ container, data, opts });
 *   // data = { meta, elements:[...], style:{node,edge}, annById:{id:{...}} }
 *   // opts = { layout:'elk', dark:false, labels:true, onReady:fn, onSelect:fn,
 *   //          onSelectEdge:fn }   onSelectEdge receives {s,t,x,eid,doi} or null
 *   // app exposes: cy, applyLayout(key), toggleLabels(on), fit(), setTheme(dark),
 *   //              clearSelection(), LAYOUT_LABELS, present (node types present)
 *
 * window.__ready is set true once the initial layout has settled (after cy.fit),
 * including the dagre fallback path. Headless capture must gate on that flag —
 * the default ELK layout is a manual async promise, so 'layoutstop' never fires
 * for it.
 */
(function (global) {
  'use strict';

  // Cytoscape desktop node sizes (~65) are scaled down for the canvas density.
  // Must match NetworkGraph.tsx.
  var NODE_SCALE = 0.55;

  var TYPE_LABELS = {
    GENE: 'Gene',
    HORMONE: 'Hormone',
    METABOLITE: 'Metabolite',
    REGULATORY_RNA: 'Regulatory RNA',
    ENVIRONMENT: 'Environment',
    PHENOTYPE: 'Phenotype',
    PROCESS: 'Process',
    PROTEIN_COMPLEX: 'Protein complex',
  };

  var LAYOUT_LABELS = {
    elk: 'Orthogonal (routed)',
    dagreTB: 'Layered ↓ (taxi)',
    dagreLR: 'Layered → (taxi)',
    breadthfirst: 'Hierarchy (BFS)',
    fcose: 'Force-directed',
    cose: 'Force (cose)',
    concentric: 'Concentric (by degree)',
    circle: 'Circle',
  };

  // Resolve a discrete vizmap mapping ({default, by}) for a node type / edge sign.
  function pick(map, key) {
    return map && map.by && map.by[key] != null ? map.by[key] : map.default;
  }
  function signKey(x) {
    return Number(x) < 0 ? 'negative' : 'positive';
  }

  var _registered = false;
  function registerExtensions() {
    if (_registered) return;
    try { if (global.cytoscapeDagre) cytoscape.use(global.cytoscapeDagre); } catch (e) {}
    try { if (global.cytoscapeFcose) cytoscape.use(global.cytoscapeFcose); } catch (e) {}
    try { if (global.cytoscapeSvg) cytoscape.use(global.cytoscapeSvg); } catch (e) {}
    _registered = true;
  }

  function init(cfg) {
    registerExtensions();
    var container = cfg.container;
    var data = cfg.data || {};
    var opts = cfg.opts || {};
    var S = data.style;
    var annById = data.annById || {};
    var onReady = opts.onReady || function () {};
    var onSelect = opts.onSelect || function () {};
    var onSelectEdge = opts.onSelectEdge || function () {};

    var elkInstance = null;
    var currentLayout = opts.layout || 'elk';

    var present = (function () {
      var set = {};
      (data.elements || []).forEach(function (el) {
        if (el.data && el.data.id != null && el.data.source == null) {
          set[el.data.ty || 'GENE'] = true;
        }
      });
      return Object.keys(set);
    })();

    var nodeSize = function (n) {
      return (Number(pick(S.node.size, n.data('ty'))) || Number(S.node.size.default)) * NODE_SCALE;
    };

    var cy = cytoscape({
      container: container,
      elements: data.elements || [],
      wheelSensitivity: 0.25,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': function (n) { return pick(S.node.fill, n.data('ty')); },
            'border-color': function (n) { return pick(S.node.border, n.data('ty')); },
            'border-width': S.node.borderWidth * NODE_SCALE,
            shape: function (n) { return pick(S.node.shape, n.data('ty')); },
            width: nodeSize,
            height: nodeSize,
            label: 'data(label)',
            color: '#13251c',
            'font-family': 'Arial, Helvetica, sans-serif',
            'font-size': 10,
            'font-weight': 600,
            'text-valign': 'bottom',
            'text-margin-y': 3,
            'text-background-color': '#fbfdfc',
            'text-background-opacity': 0.85,
            'text-background-padding': 1.5,
          },
        },
        {
          selector: 'edge',
          style: {
            width: S.edge.width,
            'curve-style': 'bezier',
            'line-color': function (e) { return pick(S.edge.line, e.data('sk')); },
            'target-arrow-color': function (e) { return pick(S.edge.arrowColor, e.data('sk')); },
            'target-arrow-shape': function (e) { return pick(S.edge.arrowShape, e.data('sk')); },
            'arrow-scale': 1.1,
            opacity: 0.9,
          },
        },
        { selector: '.faded', style: { opacity: 0.1 } },
        { selector: 'edge.hl', style: { opacity: 1, width: S.edge.width + 1.5, 'z-index': 99 } },
        {
          selector: 'node.hl',
          style: {
            opacity: 1,
            'border-color': '#d9930b',
            'border-width': Math.max(4, S.node.borderWidth * NODE_SCALE),
            'z-index': 99,
          },
        },
        { selector: '.hidden-labels', style: { label: '' } },
      ],
      layout: { name: 'grid' }, // placeholder; real layout applied below
    });

    global.cy = cy; // expose for headless capture / debugging

    cy.on('tap', 'node', function (evt) { selectNode(evt.target); });
    // An edge is a claim about the literature, so it has to be clickable: this is
    // what opens the evidence drawer. Without this handler an edge tap fell through
    // to the background handler below and cleared the selection instead.
    cy.on('tap', 'edge', function (evt) {
      evt.stopPropagation();
      selectEdge(evt.target);
    });
    cy.on('tap', function (evt) { if (evt.target === cy) clearSelection(); });

    // ---- selection / inspector ----
    function selectNode(node) {
      var neigh = node.closedNeighborhood();
      cy.elements().addClass('faded');
      neigh.removeClass('faded').addClass('hl');
      node.addClass('hl');

      var a = annById[node.id()] || {};
      var toEdge = function (e, dir) {
        return {
          other: dir === 'in' ? e.source().id() : e.target().id(),
          neg: e.data('sign') < 0,
          doi: e.data('doi') || undefined,
        };
      };
      onSelect({
        id: node.id(),
        ty: node.data('ty'),
        fn: node.data('fn') || a.fn || '',
        src: !!node.data('src'),
        desc: a.desc,
        incomers: node.incomers('edge').map(function (e) { return toEdge(e, 'in'); }),
        outgoers: node.outgoers('edge').map(function (e) { return toEdge(e, 'out'); }),
      });
    }
    function selectEdge(edge) {
      var src = edge.source(), tgt = edge.target();
      cy.elements().addClass('faded');
      edge.removeClass('faded').addClass('hl');
      src.removeClass('faded').addClass('hl');
      tgt.removeClass('faded').addClass('hl');
      onSelectEdge({
        s: src.id(),
        t: tgt.id(),
        x: edge.data('sign'),
        eid: edge.data('id'),
        doi: edge.data('doi') || '',
      });
    }
    function clearSelection() {
      cy.elements().removeClass('faded hl');
      onSelect(null);
      onSelectEdge(null);
    }

    // Find-by-id entry point for the View tab's search box: reuses selectNode's fade/.hl
    // highlight + onSelect(...) inspector callback (so a searched node looks and behaves
    // exactly like a clicked one), and additionally pans/zooms the viewport to it.
    function focusNode(id) {
      var node = cy.getElementById(id);
      if (node.empty() || !node.isNode()) return false;
      selectNode(node);
      cy.animate(
        { center: { eles: node }, zoom: Math.min(Math.max(cy.zoom(), 1), 2.2) },
        { duration: 400 }
      );
      return true;
    }

    // ---- edge routing helpers (ported from network.js / NetworkGraph.tsx) ----
    function applyEdgeRouting(mode) {
      cy.edges().removeStyle('segment-weights segment-distances segment-radii');
      if (mode === 'taxi-v' || mode === 'taxi-h') {
        cy.edges().style({
          'curve-style': 'taxi',
          'taxi-direction': mode === 'taxi-h' ? 'rightward' : 'downward',
          'taxi-turn': '45%',
          'taxi-turn-min-distance': '8px',
        });
      } else {
        cy.edges().style({ 'curve-style': 'bezier', 'taxi-direction': 'auto' });
        cy.edges().removeStyle('source-endpoint target-endpoint');
      }
    }

    // Fan parallel taxi edges across a node face so they don't stack on the centre, AND
    // stagger each edge's taxi-turn point so edges spanning the same rank-gap don't run
    // merged on top of each other along their whole middle stretch (a uniform taxi-turn
    // fans endpoints but leaves the long straight run between them identical for every
    // edge sharing that corridor — this is what "endpoint fanning without middle-segment
    // fanning" looks like, and why parallel edges only visually separate near their ends).
    function fanEndpoints(mode) {
      var vert = mode === 'taxi-v';
      if (!vert && mode !== 'taxi-h') {
        cy.edges().removeStyle('source-endpoint target-endpoint taxi-turn');
        return;
      }
      var SPREAD = 90, TURN_MIN = 30, TURN_MAX = 62;
      // taxi-turn is a single scalar per edge (unlike source/target-endpoint, which are
      // distinct properties) — an edge is visited once as an outgoer of its source node
      // and once as an incomer of its target node, so both passes racing to set it would
      // silently clobber each other. First writer wins; deterministic-per-run, not a bug.
      var turned = {};
      var assign = function (edges, which, faceSign, otherPos) {
        var arr = edges.toArray();
        if (!arr.length) return;
        arr.sort(function (a, b) { return otherPos(a) - otherPos(b); });
        var n = arr.length;
        arr.forEach(function (e, i) {
          var off = (n === 1 ? 0 : i / (n - 1) - 0.5) * SPREAD;
          e.style(
            which + '-endpoint',
            vert ? off + '% ' + faceSign * 50 + '%' : faceSign * 50 + '% ' + off + '%'
          );
          if (!turned[e.id()]) {
            var turn = n === 1 ? 45 : TURN_MIN + (i / (n - 1)) * (TURN_MAX - TURN_MIN);
            e.style('taxi-turn', turn.toFixed(0) + '%');
            turned[e.id()] = true;
          }
        });
      };
      cy.nodes().forEach(function (node) {
        assign(node.incomers('edge'), 'target', -1, function (e) {
          return vert ? e.source().position('x') : e.source().position('y');
        });
        assign(node.outgoers('edge'), 'source', +1, function (e) {
          return vert ? e.target().position('x') : e.target().position('y');
        });
      });
    }

    function settle() {
      global.__ready = true;
      try { onReady(cy); } catch (e) {}
    }

    // Layout keys backed by ELK (whole-graph placement + genuine per-edge orthogonal
    // routing, computed together in one pass — see runElkLayered) vs. keys backed by
    // Cytoscape's own layout algorithms + taxi-style edge routing (see layoutOpts below).
    var ELK_DIRECTION = { elk: 'DOWN', dagreTB: 'DOWN', dagreLR: 'RIGHT' };
    var DAGRE_FALLBACK_RANKDIR = { elk: 'TB', dagreTB: 'TB', dagreLR: 'LR' };

    function applyLayout(key) {
      currentLayout = key;
      if (ELK_DIRECTION[key]) {
        var rankDir = DAGRE_FALLBACK_RANKDIR[key];
        runElkLayered(ELK_DIRECTION[key]).then(settle).catch(function (e) {
          if (global.console) console.warn("ELK layered ('" + key + "') failed; falling back to dagre+taxi.", e);
          runDagreTaxiFallback(rankDir);
        });
        return;
      }

      var layoutOpts = {
        breadthfirst: { edge: 'taxi-v', opts: function () { return { name: 'breadthfirst', directed: true, animate: false, padding: 40, spacingFactor: 1.15, roots: cy.nodes('[?src]') }; } },
        fcose: { edge: 'bezier', opts: function () { return { name: 'fcose', quality: 'proof', animate: false, randomize: true, padding: 40, nodeRepulsion: 5500, idealEdgeLength: 75, nodeSeparation: 80, gravity: 0.25, numIter: 2500 }; } },
        cose: { edge: 'bezier', opts: function () { return { name: 'cose', animate: false, padding: 40, nodeRepulsion: 9000, idealEdgeLength: 70, edgeElasticity: 120, gravity: 0.3, numIter: 1200, nodeOverlap: 14 }; } },
        concentric: { edge: 'bezier', opts: function () { return { name: 'concentric', animate: false, padding: 40, minNodeSpacing: 24, concentric: function (n) { return n.degree(false); }, levelWidth: function () { return 2; } }; } },
        circle: { edge: 'bezier', opts: function () { return { name: 'circle', animate: false, padding: 40 }; } },
      };

      var L = layoutOpts[key];
      if (!L) { applyLayout('elk'); return; }
      applyEdgeRouting(L.edge);
      var run = function (o, edge) {
        var lay = cy.layout(o);
        lay.one('layoutstop', function () { fanEndpoints(edge); settle(); });
        lay.run();
      };
      try {
        run(L.opts(), L.edge);
      } catch (e) {
        if (global.console) console.warn("Layout '" + key + "' unavailable, falling back to cose.", e);
        applyEdgeRouting('bezier');
        run(layoutOpts.cose.opts(), 'bezier');
      }
    }

    // Dagre node placement + taxi edge routing — the pre-ELK behaviour for the
    // Layered ↓/→ layouts, kept only as the fallback if ELK itself throws (e.g. elkjs
    // failed to load). Not reachable from the layout dropdown under normal operation.
    function runDagreTaxiFallback(rankDir) {
      var edge = rankDir === 'LR' ? 'taxi-h' : 'taxi-v';
      applyEdgeRouting(edge);
      var opts = rankDir === 'LR'
        ? { name: 'dagre', rankDir: 'LR', nodeSep: 22, edgeSep: 10, rankSep: 75, animate: false, padding: 40, ranker: 'network-simplex' }
        : { name: 'dagre', rankDir: 'TB', nodeSep: 26, edgeSep: 12, rankSep: 60, animate: false, padding: 40, ranker: 'network-simplex' };
      try {
        var lay = cy.layout(opts);
        lay.one('layoutstop', function () { fanEndpoints(edge); settle(); });
        lay.run();
      } catch (e) {
        if (global.console) console.warn('dagre unavailable, falling back to cose.', e);
        applyEdgeRouting('bezier');
        var cy2 = cy.layout({ name: 'cose', animate: false, padding: 40, nodeRepulsion: 9000, idealEdgeLength: 70, edgeElasticity: 120, gravity: 0.3, numIter: 1200, nodeOverlap: 14 });
        cy2.one('layoutstop', function () { fanEndpoints('bezier'); settle(); });
        cy2.run();
      }
    }

    // Node placement AND genuine per-edge orthogonal routing via ELK, computed together
    // in one pass so edges sharing a rank-gap get distinct, non-overlapping polylines
    // along their whole length — not just fanned endpoints. Backs "Orthogonal (routed)"
    // (direction DOWN) and "Layered ↓/→" (DOWN/RIGHT); rendered as Cytoscape `segments`.
    function runElkLayered(direction) {
      if (!elkInstance) elkInstance = new ELK();
      var elk = elkInstance;
      applyEdgeRouting('bezier');
      var lr = direction === 'RIGHT';

      var graph = {
        id: 'root',
        layoutOptions: {
          'elk.algorithm': 'layered',
          'elk.direction': direction,
          'elk.edgeRouting': 'ORTHOGONAL',
          'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
          'elk.layered.spacing.nodeNodeBetweenLayers': lr ? '75' : '55',
          'elk.spacing.nodeNode': lr ? '22' : '38',
          'elk.spacing.edgeNode': '14',
          'elk.spacing.edgeEdge': '12',
          'elk.layered.spacing.edgeEdgeBetweenLayers': '12',
          'elk.layered.spacing.edgeNodeBetweenLayers': '16',
        },
        children: cy.nodes().map(function (n) {
          var bb = n.boundingBox({ includeLabels: false, includeOverlays: false });
          return { id: n.id(), width: Math.max(bb.w, 8), height: Math.max(bb.h, 8) };
        }),
        edges: cy.edges().map(function (e) {
          return { id: e.id(), sources: [e.source().id()], targets: [e.target().id()] };
        }),
      };

      return elk.layout(graph).then(function (res) {
        if (cy.destroyed()) return;
        var center = {};
        res.children.forEach(function (c) { center[c.id] = { x: c.x + c.width / 2, y: c.y + c.height / 2 }; });

        cy.batch(function () {
          cy.nodes().forEach(function (n) {
            var c = center[n.id()];
            if (c) n.position(c);
          });
          (res.edges || []).forEach(function (ed) {
            var e = cy.getElementById(ed.id);
            var sec = ed.sections && ed.sections[0];
            if (!e || e.empty() || !sec) return;
            applyElkRoute(e, sec, center[ed.sources[0]], center[ed.targets[0]]);
          });
        });
        cy.fit(undefined, 40);
      });
    }

    function applyElkRoute(e, sec, sc, tc) {
      var A = sec.startPoint, B = sec.endPoint;
      e.style('source-endpoint', (A.x - sc.x).toFixed(2) + 'px ' + (A.y - sc.y).toFixed(2) + 'px');
      e.style('target-endpoint', (B.x - tc.x).toFixed(2) + 'px ' + (B.y - tc.y).toFixed(2) + 'px');
      var bends = sec.bendPoints || [];
      if (!bends.length) {
        e.style('curve-style', 'straight');
        e.removeStyle('segment-weights segment-distances segment-radii');
        return;
      }
      var dx = tc.x - sc.x, dy = tc.y - sc.y;
      var len2 = dx * dx + dy * dy || 1;
      var len = Math.sqrt(len2);
      var weights = [], dists = [];
      bends.forEach(function (p) {
        weights.push(+(((p.x - sc.x) * dx + (p.y - sc.y) * dy) / len2).toFixed(4));
        dists.push(+(((p.y - sc.y) * dx - (p.x - sc.x) * dy) / len).toFixed(2));
      });
      e.style({
        'curve-style': 'segments',
        'edge-distances': 'node-position',
        'segment-weights': weights.join(' '),
        'segment-distances': dists.join(' '),
        'segment-radii': '0',
      });
    }

    // ---- theme ----
    function setTheme(dark) {
      cy.nodes().style({
        color: dark ? '#e7efe9' : '#13251c',
        'text-background-color': dark ? '#0e1b1f' : '#fbfdfc',
        'text-background-opacity': dark ? 0.7 : 0.85,
      });
    }

    function toggleLabels(on) { cy.nodes().toggleClass('hidden-labels', !on); }
    function fit() { cy.fit(cy.elements(), 40); }

    // kick off the initial layout + theme
    applyLayout(currentLayout);
    setTheme(!!opts.dark);

    return {
      cy: cy,
      applyLayout: applyLayout,
      toggleLabels: toggleLabels,
      fit: fit,
      setTheme: setTheme,
      clearSelection: clearSelection,
      focusNode: focusNode,
      present: present,
      LAYOUT_LABELS: LAYOUT_LABELS,
      TYPE_LABELS: TYPE_LABELS,
      pick: pick,
    };
  }

  global.FLASHP = {
    init: init,
    pick: pick,
    signKey: signKey,
    TYPE_LABELS: TYPE_LABELS,
    LAYOUT_LABELS: LAYOUT_LABELS,
    NODE_SCALE: NODE_SCALE,
  };
})(typeof window !== 'undefined' ? window : this);
