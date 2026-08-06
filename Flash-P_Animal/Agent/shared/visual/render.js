#!/usr/bin/env node
/*
 * render.js — headless, pixel-perfect static capture for the FLASH-P visualiser.
 *
 * Loads the SAME graph engine (flashp-graph.js) and the SAME Cytoscape /
 * elkjs / dagre / fcose libraries the website uses (resolved from this package's
 * node_modules), lays the graph out, and exports SVG + PNG via the real
 * Cytoscape renderer. Driven by network_to_visual.py.
 *
 * Usage:
 *   node render.js --data <graphdata.json> [--out-png a.png] [--out-svg a.svg]
 *                  [--layout elk] [--dark] [--transparent] [--scale 3]
 *                  [--width 1600] [--height 1200] [--timeout 60000]
 *
 * The default ELK layout is a manual async promise, so we gate capture on the
 * window.__ready flag the engine sets after cy.fit() (success OR dagre fallback),
 * never on 'layoutstop'. A watchdog turns a stuck layout into a non-zero exit
 * instead of a silent hang.
 *
 * Graceful by contract: this script is only invoked when Node is present; if a
 * library or Chromium is missing it exits non-zero and the Python caller keeps
 * the HTML it already wrote and prints an actionable message.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

function parseArgs(argv) {
  const a = { layout: 'elk', dark: false, transparent: false, scale: 3, width: 1600, height: 1200, timeout: 60000 };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    const next = () => argv[++i];
    if (k === '--data') a.data = next();
    else if (k === '--out-png') a.outPng = next();
    else if (k === '--out-svg') a.outSvg = next();
    else if (k === '--layout') a.layout = next();
    else if (k === '--dark') a.dark = true;
    else if (k === '--transparent') a.transparent = true;
    else if (k === '--scale') a.scale = Number(next());
    else if (k === '--width') a.width = Number(next());
    else if (k === '--height') a.height = Number(next());
    else if (k === '--timeout') a.timeout = Number(next());
  }
  return a;
}

// Resolve a vendored library file directly from this package's node_modules.
// (require.resolve on deep subpaths is blocked by modern packages' "exports"
// maps, so we join paths under node_modules and try a few known layouts.)
function resolveLib(candidates) {
  for (const rel of candidates) {
    const p = path.join(__dirname, 'node_modules', rel);
    if (fs.existsSync(p)) return p;
  }
  throw new Error('Could not resolve any of: ' + candidates.join(', '));
}

// Load order matters: deps before dependents (dagre<-cytoscape-dagre,
// layout-base<-cose-base<-cytoscape-fcose), engine last.
function libPaths() {
  return [
    resolveLib(['cytoscape/dist/cytoscape.min.js', 'cytoscape/dist/cytoscape.umd.js']),
    resolveLib(['dagre/dist/dagre.min.js', 'dagre/dist/dagre.js']),
    resolveLib(['cytoscape-dagre/dist/cytoscape-dagre.js', 'cytoscape-dagre/cytoscape-dagre.js']),
    resolveLib(['layout-base/layout-base.js']),
    resolveLib(['cose-base/cose-base.js']),
    resolveLib(['cytoscape-fcose/cytoscape-fcose.js']),
    resolveLib(['elkjs/lib/elk.bundled.js']),
    resolveLib(['cytoscape-svg/cytoscape-svg.js', 'cytoscape-svg/dist/cytoscape-svg.js']),
    path.join(__dirname, 'flashp-graph.js'),
  ];
}

async function launch() {
  let puppeteer;
  try { puppeteer = require('puppeteer'); }
  catch (e) { puppeteer = require('puppeteer-core'); }
  const opts = { headless: 'new', args: ['--no-sandbox', '--disable-gpu', '--force-color-profile=srgb'] };
  // Allow an external Chrome/Edge for environments where the bundled Chromium
  // download is blocked (PUPPETEER_EXECUTABLE_PATH).
  if (process.env.PUPPETEER_EXECUTABLE_PATH) opts.executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
  return puppeteer.launch(opts);
}

async function main() {
  const a = parseArgs(process.argv);
  if (!a.data) throw new Error('--data <graphdata.json> is required');
  const data = JSON.parse(fs.readFileSync(a.data, 'utf8'));
  const bg = a.transparent ? undefined : (a.dark ? '#0e1b1f' : '#fbfdfc');
  const libs = libPaths();

  const browser = await launch();
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: a.width, height: a.height, deviceScaleFactor: 1 });
    page.on('pageerror', (err) => console.error('[page error]', err.message));
    page.on('console', (msg) => { if (msg.type() === 'error') console.error('[page]', msg.text()); });

    // Minimal page: a full-size #cy container with an explicit pixel size so ELK
    // gets real node bounding boxes and cy.fit behaves headless.
    await page.setContent(
      '<!DOCTYPE html><html><head><meta charset="utf-8">' +
      '<style>html,body{margin:0;height:100%}#cy{position:absolute;inset:0;' +
      'width:' + a.width + 'px;height:' + a.height + 'px;font-family:Arial,Helvetica,sans-serif}</style>' +
      '</head><body><div id="cy"></div></body></html>',
      { waitUntil: 'load' }
    );

    // Inject by `path` (reads file content in Node) rather than `url`: Chromium
    // blocks file:// subresources from an about:blank (setContent) page.
    for (const p of libs) {
      await page.addScriptTag({ path: p });
    }

    await page.evaluate((payload, layout, dark) => {
      window.__ready = false;
      window.FLASHP.init({
        container: document.getElementById('cy'),
        data: payload,
        opts: { layout: layout, dark: dark },
      });
    }, data, a.layout, a.dark);

    await page.waitForFunction('window.__ready === true', { timeout: a.timeout, polling: 100 });
    // small settle for the final render pass
    await new Promise((r) => setTimeout(r, 150));

    if (a.outSvg) {
      const svg = await page.evaluate((bg) => {
        if (!window.cy || !window.cy.svg) throw new Error('cytoscape-svg not available');
        return window.cy.svg(bg ? { full: true, bg: bg } : { full: true });
      }, bg);
      fs.writeFileSync(a.outSvg, svg, 'utf8');
      console.log('SVG written: ' + a.outSvg);
    }
    if (a.outPng) {
      const dataUrl = await page.evaluate((scale, bg) => {
        return window.cy.png(bg ? { full: true, scale: scale, bg: bg } : { full: true, scale: scale });
      }, a.scale, bg);
      const b64 = dataUrl.replace(/^data:image\/png;base64,/, '');
      fs.writeFileSync(a.outPng, Buffer.from(b64, 'base64'));
      console.log('PNG written: ' + a.outPng);
    }
  } finally {
    await browser.close();
  }
}

main().catch((err) => { console.error(err && err.stack ? err.stack : String(err)); process.exit(1); });
