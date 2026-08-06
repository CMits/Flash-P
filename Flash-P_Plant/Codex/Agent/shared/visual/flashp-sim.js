/*
 * flashp-sim.js — in-browser perturbation engine for the FLASH-P Studio.
 *
 * A faithful vanilla-JS port of the website's client-side propagation engine
 *   FLASHP_WEBSITE/Flash-P-AI/src/lib/traits/propagation/{algebraic,rwr,ode,common}.ts
 * which themselves port the Python validators (flashp_validator / rwr_validator /
 * ode_validator). All three methods are pure arithmetic loops — no backend, no deps.
 *
 * Every node is normalised so the wild-type steady state is 1 (RWR signals are 0 at
 * WT); a perturbation is read as a fold change (RWR: a signed Δ) against that baseline.
 *
 * Data shapes (embedded by network_to_studio.py, matching the website types):
 *   net      = { nodes:[{id, ty, fn?, src?}], edges:[{s, t, x}] }   // ty = FULL type
 *   algEq    = { equations:[{n, a:[...], inh:[...]}] } | null
 *   perturb  = { geneModifiers:{id:mult}, exogenous:{id:value} }
 *   params   = { alpha?, K?, n? }  (best-of-sweep from accuracy_metrics.json)
 *
 * Usage:
 *   var res = FLASHPSIM.runSimulation('algebraic', { net, algEq, perturbation, baseline, params });
 *   // res = { method, iterations:[{step,values}], baselineIterations, wtValues,
 *   //         baselineLabel, phenotype, phenotypeValue, wtPhenotypeValue, ratio,
 *   //         predictedDirection, converged, convergedAt }
 */
(function (global) {
  'use strict';

  var METHOD_LABELS = {
    algebraic: 'Algebraic (Flash-P)',
    rwr: 'RWR',
    ode: 'ODE (Hill)',
  };

  // Gene perturbation modifiers (match the GUI / validators).
  var MODIFIERS = { KO: 0, KD: 0.5, WT: 1, OE: 2 };
  var MODIFIER_LABELS = [
    { value: 0, label: 'KO (knockout)' },
    { value: 0.5, label: 'KD (knockdown)' },
    { value: 2, label: 'OE (overexpression)' },
  ];

  var EMPTY = { geneModifiers: {}, exogenous: {} };

  // ---- shared helpers (common.ts) ----------------------------------------

  function nodeTypes(net) {
    var m = {};
    for (var i = 0; i < net.nodes.length; i++) m[net.nodes[i].id] = net.nodes[i].ty || 'GENE';
    return m;
  }

  /** The phenotype node (simulation target): the PHENOTYPE node, else the last node. */
  function phenotypeNode(net) {
    for (var i = 0; i < net.nodes.length; i++) {
      if ((net.nodes[i].ty || '') === 'PHENOTYPE') return net.nodes[i].id;
    }
    return net.nodes.length ? net.nodes[net.nodes.length - 1].id : null;
  }

  /**
   * Per-node activator/inhibitor lists. Prefers the validated equation file
   * (faithful to FlashP); falls back to deriving them from the signed edges.
   */
  function buildEquations(net, file) {
    var types = nodeTypes(net);
    var eqs = {};
    for (var i = 0; i < net.nodes.length; i++) {
      var id = net.nodes[i].id;
      eqs[id] = { nodeType: types[id] || 'GENE', activators: [], inhibitors: [] };
    }
    var rows = file && file.equations;
    if (rows && rows.length) {
      for (var r = 0; r < rows.length; r++) {
        var row = rows[r];
        var nid = row.n != null ? row.n : row.node;
        if (nid == null || !eqs[nid]) continue;
        eqs[nid].activators = row.a || row.activators || [];
        eqs[nid].inhibitors = row.inh || row.inhibitors || [];
      }
    } else {
      for (var e = 0; e < net.edges.length; e++) {
        var ed = net.edges[e];
        if (!eqs[ed.t]) continue;
        (ed.x > 0 ? eqs[ed.t].activators : eqs[ed.t].inhibitors).push(ed.s);
      }
    }
    return eqs;
  }

  function classifyDirection(ratio, threshold) {
    threshold = threshold == null ? 0.05 : threshold;
    if (ratio > 1 + threshold) return 'increased';
    if (ratio < 1 - threshold) return 'decreased';
    return 'unchanged';
  }

  /** Nodes a user can perturb / treat, sorted by id (for the builder UI). */
  function perturbableNodes(net) {
    return net.nodes
      .map(function (n) { return { id: n.id, ty: n.ty || 'GENE', fn: n.fn }; })
      .sort(function (a, b) { return a.id < b.id ? -1 : a.id > b.id ? 1 : 0; });
  }

  // ---- Algebraic (Flash-P): geometric activation, bounded inhibition -------

  var ALG = {
    epsilon: 0.1, K: 10, activatorFloor: 0.01,
    damping: 0.7, maxIter: 100, tol: 1e-4, directionThreshold: 0.05,
  };

  function runAlgebraic(net, file, perturbation, baseline, baselineLabel) {
    baseline = baseline || EMPTY;
    baselineLabel = baselineLabel || 'Wild type';
    var eqs = buildEquations(net, file);
    var ids = net.nodes.map(function (n) { return n.id; });
    var sourceNodes = {};
    for (var i = 0; i < ids.length; i++) {
      var id = ids[i];
      if (eqs[id].activators.length === 0 && eqs[id].inhibitors.length === 0) sourceNodes[id] = true;
    }
    var order = ids.filter(function (id) { return sourceNodes[id]; })
      .concat(ids.filter(function (id) { return !sourceNodes[id]; }));

    // value = activation × inhibition × gene_modifier + exogenous_supply (all node types alike).
    function computeNode(id, eq, values, gm, exo) {
      var geneMod = gm[id] != null ? gm[id] : 1;
      var activation = 1, inhibition = 1, k;
      if (eq.activators.length) {
        var prod = 1;
        for (k = 0; k < eq.activators.length; k++) {
          prod *= Math.max(values[eq.activators[k]] != null ? values[eq.activators[k]] : 1, ALG.activatorFloor);
        }
        activation = Math.pow(prod, 1 / eq.activators.length);
      }
      if (eq.inhibitors.length) {
        var iprod = 1;
        for (k = 0; k < eq.inhibitors.length; k++) {
          iprod *= values[eq.inhibitors[k]] != null ? values[eq.inhibitors[k]] : 1;
        }
        inhibition = Math.min(1 / Math.max(iprod, ALG.epsilon), ALG.K);
      }
      return Math.max(activation * inhibition * geneMod + (exo[id] != null ? exo[id] : 0), 0);
    }

    function simulate(gm, exo) {
      var values = {}, id, it;
      for (id in eqs) values[id] = 1;
      for (id in sourceNodes) values[id] = computeNode(id, eqs[id], values, gm, exo);
      var trace = [{ step: 0, values: copy(values), maxChange: null }];
      var cur = values, converged = false, convergedAt = ALG.maxIter;
      for (it = 0; it < ALG.maxIter; it++) {
        var next = {}, maxChange = 0;
        for (var o = 0; o < order.length; o++) {
          id = order[o];
          var computed = computeNode(id, eqs[id], cur, gm, exo);
          if (sourceNodes[id]) {
            next[id] = computed;
            maxChange = Math.max(maxChange, Math.abs(computed - cur[id]));
          } else {
            var old = cur[id];
            var nv = (1 - ALG.damping) * computed + ALG.damping * old;
            next[id] = nv;
            maxChange = Math.max(maxChange, Math.abs(nv - old));
          }
        }
        cur = next;
        trace.push({ step: it + 1, values: copy(cur), maxChange: maxChange });
        if (maxChange < ALG.tol) { converged = true; convergedAt = it + 1; break; }
      }
      return { values: cur, trace: trace, converged: converged, convergedAt: convergedAt };
    }

    var base = simulate(baseline.geneModifiers, baseline.exogenous);
    var pert = simulate(perturbation.geneModifiers, perturbation.exogenous);
    return finishRatio('algebraic', net, pert, base, baselineLabel, ALG.directionThreshold);
  }

  // ---- RWR: signed signal propagation -------------------------------------

  var RWR = { defaultAlpha: 0.85, maxIter: 50, tol: 1e-4, dirThreshold: 1e-5 };

  function modifierToInitialSignal(m) {
    return m === 0 ? -1 : m === 0.5 ? -0.5 : m >= 2 ? 1 : m - 1;
  }

  function runRWR(net, perturbation, baseline, baselineLabel, params) {
    baseline = baseline || EMPTY;
    baselineLabel = baselineLabel || 'Wild type';
    var ALPHA = params && params.alpha != null ? params.alpha : RWR.defaultAlpha;
    var ids = net.nodes.map(function (n) { return n.id; });
    var idSet = {};
    for (var i = 0; i < ids.length; i++) idSet[ids[i]] = true;

    var regulatorsOf = {};
    for (i = 0; i < ids.length; i++) regulatorsOf[ids[i]] = [];
    for (var e = 0; e < net.edges.length; e++) {
      var ed = net.edges[e];
      if (regulatorsOf[ed.t]) regulatorsOf[ed.t].push([ed.s, ed.x > 0 ? 1 : -1]);
    }

    function initialFrom(p) {
      var init = {}, g, v;
      for (g in p.geneModifiers) { if (idSet[g]) init[g] = modifierToInitialSignal(p.geneModifiers[g]); }
      for (v in p.exogenous) { if (idSet[v]) init[v] = (init[v] || 0) + Math.min(p.exogenous[v], 1); }
      return init;
    }

    function run(init) {
      var signals = {}, id;
      for (var j = 0; j < ids.length; j++) signals[ids[j]] = init[ids[j]] != null ? init[ids[j]] : 0;
      var trace = [{ step: 0, values: copy(signals), maxChange: null }];
      var converged = false, convergedAt = RWR.maxIter;
      for (var it = 0; it < RWR.maxIter; it++) {
        var next = {}, maxChange = 0;
        for (var k = 0; k < ids.length; k++) {
          id = ids[k];
          var regs = regulatorsOf[id];
          if (regs.length === 0) {
            next[id] = init[id] != null ? init[id] : 0;
          } else {
            var sum = 0;
            for (var rr = 0; rr < regs.length; rr++) sum += regs[rr][1] * (signals[regs[rr][0]] || 0);
            var mean = sum / regs.length;
            next[id] = ALPHA * mean + (1 - ALPHA) * (init[id] != null ? init[id] : 0);
          }
          maxChange = Math.max(maxChange, Math.abs(next[id] - signals[id]));
        }
        signals = next;
        trace.push({ step: it + 1, values: copy(signals), maxChange: maxChange });
        if (maxChange < RWR.tol) { converged = true; convergedAt = it + 1; break; }
      }
      return { values: signals, trace: trace, converged: converged, convergedAt: convergedAt };
    }

    var base = run(initialFrom(baseline));
    var pert = run(initialFrom(perturbation));

    var phenotype = phenotypeNode(net);
    var phenotypeValue = phenotype ? (pert.values[phenotype] || 0) : 0;
    var basePhenotype = phenotype ? (base.values[phenotype] || 0) : 0;
    var diff = phenotypeValue - basePhenotype;
    var direction = diff > RWR.dirThreshold ? 'increased' : diff < -RWR.dirThreshold ? 'decreased' : 'unchanged';
    return {
      method: 'rwr', iterations: pert.trace, baselineIterations: base.trace,
      wtValues: base.values, baselineLabel: baselineLabel, phenotype: phenotype,
      phenotypeValue: phenotypeValue, wtPhenotypeValue: basePhenotype, ratio: diff,
      predictedDirection: direction, converged: pert.converged, convergedAt: pert.convergedAt,
    };
  }

  // ---- ODE: Hill-function dynamics, explicit Euler ------------------------

  var ODE = { K: 1.0, n: 2, activatorFloor: 0.01, dt: 0.1, maxTime: 50, tol: 1e-4, directionThreshold: 0.05 };

  function hillActivation(x, K, n) {
    if (x <= 0) return 0;
    var Kn = Math.pow(K, n), xn = Math.pow(x, n);
    return (xn * (Kn + 1)) / (Kn + xn);
  }
  function hillInhibition(x, K, n) {
    var Kn = Math.pow(K, n);
    if (x <= 0) return (Kn + 1) / Kn;
    var xn = Math.pow(x, n);
    return (Kn + 1) / (Kn + xn);
  }

  function runODE(net, file, perturbation, baseline, baselineLabel, params) {
    baseline = baseline || EMPTY;
    baselineLabel = baselineLabel || 'Wild type';
    var K = params && params.K != null ? params.K : ODE.K;
    var n = params && params.n != null ? params.n : ODE.n;
    var eqs = buildEquations(net, file);
    var ids = net.nodes.map(function (nd) { return nd.id; });
    var sourceNodes = {};
    for (var i = 0; i < ids.length; i++) {
      var id = ids[i];
      if (eqs[id].activators.length === 0 && eqs[id].inhibitors.length === 0) sourceNodes[id] = true;
    }

    function production(id, eq, values, gm, exo) {
      var geneMod = gm[id] != null ? gm[id] : 1, k;
      var activation = 1;
      for (k = 0; k < eq.activators.length; k++) {
        var v = Math.max(values[eq.activators[k]] != null ? values[eq.activators[k]] : 1, ODE.activatorFloor);
        activation *= hillActivation(v, K, n);
      }
      var inhibition = 1;
      for (k = 0; k < eq.inhibitors.length; k++) {
        inhibition *= hillInhibition(values[eq.inhibitors[k]] != null ? values[eq.inhibitors[k]] : 1, K, n);
      }
      return Math.max(activation * inhibition * geneMod + (exo[id] != null ? exo[id] : 0), 0);
    }

    function simulate(gm, exo) {
      var values = {}, id;
      for (id in eqs) values[id] = 1;
      for (id in sourceNodes) values[id] = production(id, eqs[id], values, gm, exo);
      var trace = [{ step: 0, values: copy(values), maxChange: null }];
      var cur = values, converged = false;
      var numSteps = Math.round(ODE.maxTime / ODE.dt), convergedAt = numSteps;
      for (var step = 0; step < numSteps; step++) {
        var next = {}, maxChange = 0;
        for (var k = 0; k < ids.length; k++) {
          id = ids[k];
          var prod = production(id, eqs[id], cur, gm, exo), nv;
          if (sourceNodes[id]) {
            nv = prod;
          } else {
            var x = cur[id];
            nv = Math.max(0, x + (prod - x) * ODE.dt);
          }
          next[id] = nv;
          maxChange = Math.max(maxChange, Math.abs(nv - cur[id]));
        }
        cur = next;
        trace.push({ step: step + 1, values: copy(cur), maxChange: maxChange });
        if (maxChange < ODE.tol) { converged = true; convergedAt = step + 1; break; }
      }
      return { values: cur, trace: trace, converged: converged, convergedAt: convergedAt };
    }

    var base = simulate(baseline.geneModifiers, baseline.exogenous);
    var pert = simulate(perturbation.geneModifiers, perturbation.exogenous);
    return finishRatio('ode', net, pert, base, baselineLabel, ODE.directionThreshold);
  }

  // ---- shared finalisation for fold-ratio methods (algebraic, ode) --------

  function finishRatio(method, net, pert, base, baselineLabel, threshold) {
    var phenotype = phenotypeNode(net);
    var phenotypeValue = phenotype ? (pert.values[phenotype] != null ? pert.values[phenotype] : 0) : 0;
    var wtPhenotypeValue = phenotype ? (base.values[phenotype] != null ? base.values[phenotype] : 0) : 0;
    var ratio = wtPhenotypeValue !== 0 ? phenotypeValue / wtPhenotypeValue : phenotypeValue > 0 ? Infinity : 1;
    return {
      method: method, iterations: pert.trace, baselineIterations: base.trace,
      wtValues: base.values, baselineLabel: baselineLabel, phenotype: phenotype,
      phenotypeValue: phenotypeValue, wtPhenotypeValue: wtPhenotypeValue, ratio: ratio,
      predictedDirection: classifyDirection(ratio, threshold),
      converged: pert.converged, convergedAt: pert.convergedAt,
    };
  }

  function copy(o) {
    var out = {};
    for (var k in o) out[k] = o[k];
    return out;
  }

  // ---- dispatcher ---------------------------------------------------------

  function runSimulation(method, inputs) {
    var net = inputs.net, pert = inputs.perturbation;
    var base = inputs.baseline, label = inputs.baselineLabel, params = inputs.params;
    switch (method) {
      case 'rwr': return runRWR(net, pert, base, label, params);
      case 'ode': return runODE(net, inputs.algEq || null, pert, base, label, params);
      case 'algebraic':
      default: return runAlgebraic(net, inputs.algEq || null, pert, base, label);
    }
  }

  global.FLASHPSIM = {
    runSimulation: runSimulation,
    runAlgebraic: runAlgebraic,
    runRWR: runRWR,
    runODE: runODE,
    perturbableNodes: perturbableNodes,
    phenotypeNode: phenotypeNode,
    buildEquations: buildEquations,
    classifyDirection: classifyDirection,
    METHOD_LABELS: METHOD_LABELS,
    MODIFIERS: MODIFIERS,
    MODIFIER_LABELS: MODIFIER_LABELS,
    EMPTY_PERTURBATION: EMPTY,
  };
})(typeof window !== 'undefined' ? window : this);
