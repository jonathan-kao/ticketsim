/* A DOM stub that runs the real page script against the real server.
 *
 *   node tests/ui_harness.js [port]        # with `python3 -m ticketsim serve` running
 *
 * It checks the front-end pipeline end to end without a browser: the page
 * fetches the parameter schema, starts a run, waits for the bundle and then
 * renders the charts, the session cards, the cell strips and the indicators -
 * all locally, without any further network traffic.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PORT = process.argv[2] || "8123";
const BASE = `http://127.0.0.1:${PORT}`;
const page = fs.readFileSync(path.join(__dirname, "..", "ticketsim", "web", "index.html"), "utf8");
const script = page.match(/<script>([\s\S]*?)<\/script>/)[1];

class El {
  constructor(tag) {
    this.tagName = (tag || "div").toUpperCase();
    this._html = "";
    this.textContent = "";
    this.value = "";
    this.checked = false;
    this.style = {};
    this.dataset = {};
    this.children = [];
    this.options = [];
    this.parentNode = null;
    this.classList = { toggle() {}, add() {}, remove() {}, contains() { return false; } };
    this._first = null;
    this.scrollTop = 0;
  }
  // browser semantics: innerHTML = "" removes every child
  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this._html = value;
    if (value === "") { this.children = []; this._first = null; }
  }
  get firstElementChild() { if (!this._first) this._first = new El("i"); return this._first; }
  get firstChild() { return this.children[0]; }
  get scrollHeight() { return 0; }
  get clientHeight() { return 0; }
  setAttribute(key, value) { this[key] = value; }
  getAttribute(key) { return this[key] === undefined ? null : this[key]; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 340 }; }
  addEventListener(type, fn) { (this._listeners = this._listeners || {})[type] = fn; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter((c) => c !== child); return child; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  querySelector(selector) {
    const id = (selector.match(/#([\w-]+)/) || [])[1];
    if (!id) return null;
    let found = null;
    const walk = (el) => {
      for (const child of el.children) {
        if (child.id === id) found = child;
        walk(child);
      }
    };
    walk(this);
    return found;
  }

  querySelectorAll(selector) {
    // supports [data-xxx] and tag names (tr, ...)
    const attr = (selector.match(/\[data-([\w-]+)\]/) || [])[1];
    const tag = attr ? null : selector.toUpperCase();
    const out = [];
    const walk = (el) => {
      for (const child of el.children) {
        if (attr && child.dataset && child.dataset[attr] !== undefined) out.push(child);
        else if (tag && child.tagName === tag) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}

const registry = new Map();
const document = {
  body: { classList: { toggle() {}, contains() { return false; } } },
  getElementById(id) {
    if (!registry.has(id)) registry.set(id, new El("div"));
    return registry.get(id);
  },
  createElement(tag) { return new El(tag); },
};

const ctx = vm.createContext({
  document,
  fetch: (url, opts) => fetch(url.startsWith("http") ? url : BASE + url, opts),
  setInterval: (fn, ms) => setInterval(fn, ms), clearInterval: (id) => clearInterval(id),
  console, JSON, Math, Object, Array, Number,
  String, Date, setTimeout, clearTimeout,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  performance: { now: () => Date.now() },
  window: { innerWidth: 1600, innerHeight: 1000, addEventListener() {} },
  // the address query (the launcher passes ?preset=...)
  location: { search: process.env.UI_QUERY || "" },
  URLSearchParams,
});
vm.runInContext(script, ctx);

const $ = (id) => document.getElementById(id);

function check(name, ok, extra) {
  console.log(`${ok ? "OK  " : "FAIL"}  ${name}${extra ? "  - " + extra : ""}`);
  if (!ok) process.exitCode = 1;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  await sleep(500);
  check("the parameter form is built", $("params").children.length > 0,
        `${$("params").children.length} nodes`);
  check("the session table has two rows", $("sessions-body").children.length === 2);
  const fields = $("params").querySelectorAll("[data-group]");
  const empty = fields.filter((f) => f.type !== "checkbox" && (f.value === "" || f.value === undefined))
                     .map((f) => `${f.dataset.group}.${f.dataset.field}`);
  check("no empty field in the form", empty.length === 0,
        empty.length ? empty.join(", ") : `${fields.length} fields`);
  const shape = fields.find((f) => f.dataset.field === "area_capacity_shape");
  check("select fields stay strings", shape && typeof shape.value === "string",
        `area_capacity_shape = ${JSON.stringify(shape && shape.value)}`);
  const groups = Object.keys(vm.runInContext("SCHEMA", ctx));
  check("the form has the three groups", groups.join(",") === "scenario,behavior,model_constants",
        groups.join(","));
  // the form round-trips the configuration it was filled from
  const differences = [];
  const compare = (x, y, at) => {
    if (typeof x !== "object" || x === null || typeof y !== "object" || y === null) {
      if (JSON.stringify(x) !== JSON.stringify(y)) differences.push(`${at}: ${JSON.stringify(x)} -> ${JSON.stringify(y)}`);
      return;
    }
    for (const key of new Set([...Object.keys(x), ...Object.keys(y)])) compare(x[key], y[key], `${at}.${key}`);
  };
  compare(vm.runInContext("CONFIG", ctx), vm.runInContext("readForm()", ctx), "config");
  check("readForm() returns the loaded scenario unchanged", differences.length === 0,
        differences.slice(0, 5).join("; "));

  // run -> one transfer -> everything local afterwards
  const before = Date.now();
  await ctx.startRun();
  let waited = 0;
  while (waited < 60000) {
    if (vm.runInContext("BUNDLE !== null", ctx)) break;
    await sleep(200); waited += 200;
  }
  check("the run finished and the bundle arrived", vm.runInContext("BUNDLE !== null", ctx),
        `${((Date.now() - before) / 1000).toFixed(1)}s`);
  if (!vm.runInContext("BUNDLE !== null", ctx)) { process.exit(1); }
  const bundle = vm.runInContext("BUNDLE", ctx);
  check("the bundle has every stream", bundle.samples.length > 10 && bundle.markers.length > 0
        && bundle.cells.length > 0 && Object.keys(bundle.cell_labels).length === 2,
        `samples=${bundle.samples.length} markers=${bundle.markers.length} cells=${bundle.cells.length}`);

  // the stock chart
  vm.runInContext("cursor = 15; updateCursorViews(true);", ctx);
  const svg = $("chart").innerHTML;
  const polys = [...svg.matchAll(/<polyline points="([^"]+)"/g)].map((m) => m[1]);
  check("the stock chart draws its line", polys.length >= 1, `${polys.length} lines`);
  const main = polys[polys.length - 1].split(" ");
  check("the line has a bounded number of points", main.length <= 900, `${main.length} points`);
  const xs = main.map((pt) => +pt.split(",")[0]);
  check("the line spans the whole chart", Math.max(...xs) - Math.min(...xs) > 700,
        `x span ${(Math.max(...xs) - Math.min(...xs)).toFixed(0)}px`);
  // markers: the batches and the sell-outs sit on the drawn line
  const linePts = main.map((pt) => pt.split(",").map(Number));
  const lineYAt = (px) => {
    if (px <= linePts[0][0]) return linePts[0][1];
    for (let i = 1; i < linePts.length; i++) {
      if (linePts[i][0] >= px) {
        const a = linePts[i - 1], b = linePts[i];
        return a[1] + (b[1] - a[1]) * (px - a[0]) / (b[0] - a[0]);
      }
    }
    return linePts[linePts.length - 1][1];
  };
  const markers = [...svg.matchAll(/<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"[^>]*data-id/g)]
    .filter((m) => +m[3] !== 11).map((m) => ({ x: +m[1], y: +m[2] }));
  const soldOut = Object.values(bundle.sessions).filter((s) => s.sold_out_at !== null).length;
  const batches = bundle.markers.filter((m) => m.kind === "cleared").length;
  check("one marker per batch and per sell-out", markers.length === soldOut + batches,
        `${markers.length} markers, ${batches} batches, ${soldOut} sell-outs`);
  let worst = 0;
  markers.forEach((c) => { worst = Math.max(worst, Math.abs(lineYAt(c.x) - c.y)); });
  check("the markers sit on the line (< 3px)", markers.length > 0 && worst < 3,
        `worst ${worst.toFixed(1)}px`);
  check("the playhead is drawn", svg.includes('id="playhead"'));

  // the load chart
  const load = $("load").innerHTML;
  const loadLine = [...load.matchAll(/<polyline points="([^"]+)"/g)].map((m) => m[1]);
  check("the load chart draws its line", loadLine.length === 1);
  const series = vm.runInContext("loadSeries", ctx);
  const peak = Math.max(...series.map((p) => p[1]));
  check("the load series is positive and peaks in the opening rush",
        peak > 0 && series.find((p) => p[1] === peak)[0] < 60,
        `peak ${Math.round(peak)} req/s at ${series.find((p) => p[1] === peak)[0]}s`);
  // the load integrates back to the request counters
  const polls = bundle.polls_per_attempt;
  const last = bundle.samples[bundle.samples.length - 1];
  const fromCounters = last.reads + last.attempts * (1 + polls) + last.orders;
  check("the counters behind the load add up to the summary",
        fromCounters === bundle.summary.traffic_total,
        `${fromCounters} vs ${bundle.summary.traffic_total}`);

  // the sessions and the cells
  const sessions = $("sessions").innerHTML;
  check("one card per session", (sessions.match(/class="sess"/g) || []).length === 2);
  check("the session cards show the stock", sessions.includes("on sale"));
  const cells = $("cells").innerHTML;
  const bars = (cells.match(/<i /g) || []).length;
  const cellCount = Object.values(bundle.cell_labels).reduce((s, c) => s + c.length, 0);
  check("one bar per cell", bars === cellCount, `${bars} bars, ${cellCount} cells`);
  vm.runInContext("cursor = BUNDLE.duration; updateCursorViews(true);", ctx);
  const emptyBars = ($("cells").innerHTML.match(/class="empty"/g) || []).length;
  const emptyCells = Object.values(bundle.cells[bundle.cells.length - 1].sessions)
    .reduce((s, row) => s + row.filter((v) => v <= 0).length, 0);
  check("the empty cells are marked", emptyBars === emptyCells, `${emptyBars} vs ${emptyCells}`);

  // the indicators: every chip has a value and agrees with the summary
  const chips = [...$("metrics").innerHTML.matchAll(/<i>([^<]+)<\/i><b>([^<]*)<\/b>/g)]
      .map((m) => [m[1], m[2]]);
  const chip = Object.fromEntries(chips);
  const blank = chips.filter(([, value]) => value === "-" || value === "" || value === "undefined");
  check("no indicator is blank", chips.length >= 15 && blank.length === 0,
        blank.length ? `blank: ${blank.map(([name]) => name).join(", ")}` : `${chips.length} chips`);
  const S = bundle.summary;
  const num = (v) => Number(String(v).replace(/,/g, "").replace("%", ""));
  check("sell-through matches the summary", Math.abs(num(chip["sell-through"]) - S.sell_through * 100) < 0.06,
        `${chip["sell-through"]} vs ${S.sell_through}`);
  check("platform requests match the summary", num(chip["platform requests"]) === S.traffic_total);
  check("requests at the end match the summary", num(chip["requests"]) === S.buyer_attempts,
        `${chip["requests"]} vs ${S.buyer_attempts}`);
  check("refused at the end matches the summary", num(chip["refused"]) === S.buyer_failures);
  check("tickets granted at the end match the summary", num(chip["tickets granted"]) === S.tickets_sold);
  check("conservation is shown", chip["conservation"] === "OK");

  // tooltips
  const batch = bundle.markers.find((m) => m.kind === "cleared");
  vm.runInContext("showTip(" + JSON.stringify(Object.assign({ type: "batch", level: 0 }, batch)) +
                  ", {clientX:100, clientY:100});", ctx);
  check("the batch tooltip names the returned seats", $("tip").innerHTML.includes("seats back on sale"));
  const chart = $("chart");
  const move = chart._listeners && chart._listeners.mousemove;
  if (move) {
    move({ target: chart, clientX: 500, clientY: 120 });
    const lineTip = $("tip").innerHTML;
    check("the line tooltip shows every session and the load",
          lineTip.includes("all sessions") && lineTip.includes("req/s") &&
          Object.keys(bundle.sessions).every((key) => lineTip.includes(key)));
  }

  // zoom: the wheel zooms around the playhead; a double click resets
  const wheel = chart._listeners && chart._listeners.wheel;
  vm.runInContext("cursor = 900;", ctx);
  wheel({ preventDefault() {}, clientX: 500, clientY: 120, deltaY: -120 });
  const zoomObj = JSON.parse(vm.runInContext("zoom ? JSON.stringify(zoom) : 'null'", ctx));
  check("the wheel zooms", zoomObj !== null);
  if (zoomObj) {
    check("the zoom is centred on the playhead",
          Math.abs((zoomObj.tMin + zoomObj.tMax) / 2 - 900) < 1e-6);
  }
  chart._listeners.dblclick({});
  check("a double click resets the zoom", vm.runInContext("zoom === null", ctx));

  // stepping jumps to the next marked moment
  vm.runInContext("cursor = 0;", ctx);
  const next = vm.runInContext("nextEventTime(0)", ctx);
  const firstMark = Math.min(...bundle.markers.filter((m) => m.t > 0).map((m) => m.t),
                             ...Object.values(bundle.sessions).filter((s) => s.sold_out_at)
                               .map((s) => s.sold_out_at));
  check("'next event' jumps to the first marked moment", Math.abs(next - firstMark) < 1e-9,
        `${next} vs ${firstMark}`);

  // playback stays cheap: one cursor update per frame, no network
  const t0 = Date.now();
  for (let i = 0; i < 30; i++) vm.runInContext("cursor = 10 + " + i + "; updateCursorViews();", ctx);
  const per = (Date.now() - t0) / 30;
  check("a frame update is fast (< 8 ms)", per < 8, `${per.toFixed(1)} ms per frame`);

  process.exit(process.exitCode || 0);
})().catch((err) => { console.error(err); process.exit(1); });
