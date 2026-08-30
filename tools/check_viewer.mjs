// Drives viewer.html's reducers against a real trace and checks them against the
// simulator's own answers.
//
// NOT part of `python -m unittest discover -s tests`: it needs node, and the
// simulator is stdlib-Python only. Run it by hand:
//
//     python3 -m tools.make_viewer_fixture /tmp/vf
//     node tools/check_viewer.mjs /tmp/vf viewer.html
//
// It exists because the reducers are the part of a viewer that can be wrong in
// ways no amount of looking at the page would reveal: residency rebuilt from
// admissions and departures either equals what the simulator holds or it does
// not. Mutation-tested -- breaking the drop handler, the inferred `none` count,
// or the boundary-completeness rule each makes it fail.
import fs from "node:fs";
import vm from "node:vm";

const dir = process.argv[2];
const html = fs.readFileSync(process.argv[3], "utf8");
const script = html.match(/<script>\n([\s\S]*)\n<\/script>/)[1];

const noop = () => {};
const element = () => ({
  innerHTML: "", textContent: "", className: "", hidden: false,
  dataset: {}, style: {}, value: "", onclick: null, onchange: null,
  oninput: null, classList: { toggle: noop }, appendChild: noop,
  parentElement: { clientWidth: 400, clientHeight: 190 },
  getContext: () => new Proxy({}, { get: () => noop }),
  width: 0, height: 0,
});
const sandbox = {
  document: { getElementById: element, createElement: element,
              documentElement: { getAttribute: () => null, setAttribute: noop } },
  getComputedStyle: () => ({ getPropertyValue: () => "#000" }),
  requestAnimationFrame: noop, setInterval: noop,
  performance: { now: () => 0 }, matchMedia: () => ({ matches: false }),
  fetch: () => Promise.reject(new Error("no network in harness")),
  TextDecoder, console, devicePixelRatio: 1,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(script + "\nglobalThis.__probe = { feed, player, applyEvent, " +
  "quantiles, cohortGrid, layoutNodes, hopDistancesFrom, firstKnowing, " +
  "spreadEntry, networkDisabledReason, nodeColour, NET_METRICS, " +
  "setNetLayout: v => { netLayout = v; }, stateRef: () => state };", sandbox);

const DROP_REASONS_KNOWN = new Set(["egress_full", "verify_recv",
                                    "verify_wrapper", "verify_block"]);
const probe = sandbox.__probe;
const trace = fs.readFileSync(dir + "/trace.jsonl");
probe.feed.consume(new Uint8Array(trace), trace.length);
probe.player.simT = Infinity;
probe.player.step(0);
const state = probe.stateRef();
const expected = JSON.parse(fs.readFileSync(dir + "/expected.json", "utf8"));

let failures = 0;
const check = (name, ok, detail) => {
  if (!ok) { failures++; console.log("FAIL " + name + (detail ? " — " + detail : "")); }
  else console.log("ok   " + name);
};

check("events parsed", probe.feed.events.length > 100,
      probe.feed.events.length + " events");
check("cfg captured", !!probe.feed.cfg);
check("node count from cfg", state.nodeCount === expected.nodes,
      state.nodeCount + " vs " + expected.nodes);
check("included txs match", state.included === expected.included,
      state.included + " vs " + expected.included);

// A1: residency rebuilt from arr + adm + drop must equal the simulator's.
let residencyBad = [];
for (const [id, record] of state.objects) {
  const mine = record.resident.size / expected.nodes;
  const theirs = expected.residency[String(id)];
  if (theirs === undefined) continue;
  if (Math.abs(mine - theirs) > 1e-9) residencyBad.push(id + ": " + mine + " vs " + theirs);
}
check("residency matches the simulator", residencyBad.length === 0,
      residencyBad.slice(0, 3).join("; "));

// A1: reach must never be below residency.
let reachBad = 0, strict = 0;
for (const record of state.objects.values()) {
  if (record.reach.size < record.resident.size) reachBad++;
  if (record.reach.size > record.resident.size) strict++;
}
check("reach >= residency everywhere", reachBad === 0);
check("reach strictly above somewhere", strict > 0, strict + " objects");

// F/H: residency split by hop distance from the entry must re-add to residency,
// for every object and in every sample, and the sample must have a hop for
// every resident node -- an admission with no distance would vanish from the
// hop panels while still counting in the total.
let hopBad = [];
for (const record of state.objects.values()) {
  const byHop = record.byHop.reduce((sum, n) => sum + (n || 0), 0);
  if (byHop !== record.resident.size) hopBad.push(record.id + ": " + byHop + " vs " + record.resident.size);
  for (const bin of record.bins) {
    const held = bin.hops.reduce((sum, n) => sum + (n || 0), 0);
    if (held !== bin.res) { hopBad.push(record.id + "@" + bin.age + ": " + held + " vs " + bin.res); break; }
  }
}
check("residency by hop re-adds to residency", hopBad.length === 0,
      hopBad.slice(0, 3).join("; "));

// A2: the class counts must partition the node set at every sample.
let sumBad = [];
for (const [tx, series] of state.knowledgeBins) {
  for (const point of series) {
    let total = 0;
    for (const value of Object.values(point.counts)) total += value;
    if (total !== expected.nodes) sumBad.push("tx " + tx + " t " + point.t + " = " + total);
  }
}
check("knowledge classes sum to N", sumBad.length === 0,
      sumBad.slice(0, 3).join("; "));
check("some transactions were followed", state.knowledgeBins.size > 0,
      state.knowledgeBins.size + " followed");
check("body_only observed", [...state.knowledgeBins.values()].some(s =>
      s.some(p => p.counts.body_only)), "the monotonic-bodies state");

// Spread: for every followed transaction the attributed transitions form a
// tree from the entry -- each peer is a neighbour and was itself reached
// earlier, no edge points at the entry or at a node already joined -- with
// at most N - 1 edges, and no transaction is left with none.
const neighbours = new Map();
for (const edge of probe.feed.cfg.topology) {
  if (!neighbours.has(edge[0])) neighbours.set(edge[0], new Set());
  if (!neighbours.has(edge[1])) neighbours.set(edge[1], new Set());
  neighbours.get(edge[0]).add(edge[1]);
  neighbours.get(edge[1]).add(edge[0]);
}
const entryOf = new Map();
for (const record of state.objects.values())
  if (record.tx !== undefined) entryOf.set(record.tx, record.entry);
let treeBad = [], edges = 0;
for (const [tx, tree] of state.spread) {
  const attributed = [...tree.entries()].filter(([_n, edge]) => edge);
  if (attributed.length > expected.nodes - 1) treeBad.push("tx " + tx + " has " + attributed.length + " edges");
  const reached = new Set([entryOf.get(tx)]);
  const inOrder = attributed.sort((a, b) => a[1].t - b[1].t);
  for (const [node, edge] of inOrder) {
    edges += 1;
    if (!neighbours.get(edge.peer) || !neighbours.get(edge.peer).has(node))
      treeBad.push("tx " + tx + ": " + edge.peer + " is not adjacent to " + node);
    if (!reached.has(edge.peer))
      treeBad.push("tx " + tx + ": " + edge.peer + " reached " + node + " before being reached");
    if (reached.has(node))
      treeBad.push("tx " + tx + ": " + node + " joined twice");
    reached.add(node);
  }
  if (tree.size > 1 && !attributed.length)
    treeBad.push("tx " + tx + " spread with no attributed edge");
}
// Duplicates: every one is from a neighbour, names followed transactions,
// and the fixture floods, so there are some.
let dupBad = [], dupCount = 0;
for (const [tx, list] of state.dups) {
  if (!state.knowledgeBins.has(tx)) dupBad.push("tx " + tx + " is not followed");
  for (const dup of list) {
    dupCount += 1;
    if (!neighbours.get(dup.peer) || !neighbours.get(dup.peer).has(dup.n))
      dupBad.push("tx " + tx + ": " + dup.peer + " is not adjacent to " + dup.n);
  }
}
check("duplicates come from neighbours of followed transactions",
      dupBad.length === 0 && dupCount > 0,
      dupBad.length ? dupBad.slice(0, 3).join("; ") : dupCount + " duplicates");
// And the attribution is exact: the reduced records, fanned back out, are the
// trace's `dup` records and nothing else. A reducer filing every duplicate
// under every followed transaction still passes the shape checks above; only
// an oracle built from the raw events catches it.
{
  const key = (tx, record) =>
      [tx, record.n, record.t, record.peer, record.k, record.b].join("|");
  const wanted = new Map();
  let rawCount = 0;
  for (const event of probe.feed.events) {
    if (event.e !== "dup") continue;
    for (const tx of event.txs) {
      rawCount += 1;
      const k = key(tx, event);
      wanted.set(k, (wanted.get(k) || 0) + 1);
    }
  }
  let reduced = 0, attrBad = [];
  for (const [tx, list] of state.dups) {
    for (const dup of list) {
      reduced += 1;
      const k = key(tx, dup);
      if (!wanted.get(k)) attrBad.push(k + " has no trace record");
      else wanted.set(k, wanted.get(k) - 1);
    }
  }
  check("duplicates are attributed exactly as traced",
        attrBad.length === 0 && reduced === rawCount,
        attrBad.length ? attrBad.slice(0, 3).join("; ")
                       : reduced + " records, " + rawCount + " in the trace");
}
check("spread is a tree from the entry", treeBad.length === 0 && edges > 0,
      treeBad.length ? treeBad.slice(0, 3).join("; ") : edges + " edges");

// D: per-kind byte totals must match the simulator's final accounting.
const lastBytes = state.bytes[state.bytes.length - 1];
let kindBad = [];
if (lastBytes) {
  for (const [kind, value] of Object.entries(expected.sent_kind_total)) {
    if ((lastBytes.sbk[kind] || 0) !== value)
      kindBad.push(kind + ": " + (lastBytes.sbk[kind] || 0) + " vs " + value);
  }
}
check("per-kind sent bytes match", !!lastBytes && kindBad.length === 0,
      kindBad.join("; "));

// B: only complete boundaries are published.
check("dispersion points exist", state.dispersion.length > 2,
      state.dispersion.length + " boundaries");
check("no partial boundaries left unpublished as data",
      state.dispersion.every(d => d.eq && d.cq));

// Lifecycle.
check("segment recorded", state.segments.length === 1,
      JSON.stringify(state.segments));
check("trace recognised as finalised", state.finished === "complete",
      String(state.finished));

// Losses: the panel that exists because bounded queues made overload
// expressible and nothing in the viewer could see it.
check("drops observed in the fixture", expected.dropped > 0,
      expected.dropped + " dropped");
check("drop total matches the simulator",
      state.drops.total === expected.dropped,
      state.drops.total + " vs " + expected.dropped);
check("dropped bytes match", state.drops.bytes === expected.dropped_bytes,
      state.drops.bytes + " vs " + expected.dropped_bytes);
let nodeDropBad = [];
for (const [node, count] of Object.entries(expected.dropped_by_node)) {
  if ((state.drops.byNode[node] || 0) !== count)
    nodeDropBad.push(node + ": " + (state.drops.byNode[node] || 0) + " vs " + count);
}
check("drops attributed to the right nodes", nodeDropBad.length === 0,
      nodeDropBad.slice(0, 3).join("; "));
check("every drop has a known reason",
      Object.keys(state.drops.byReason).every(r => DROP_REASONS_KNOWN.has(r)),
      Object.keys(state.drops.byReason).join(","));

// The bound has to reach the *header*, not just the Python object: the panel
// draws occupancy as a fraction of it, and without it in cfg the axis silently
// falls back to "unbounded" and says so while a bound is in force. Read from the
// trace, deliberately, because reading it from `expected` is what hid this.
check("the queue bound is in the trace header",
      probe.feed.cfg.egress_queue_bytes === expected.egress_queue_bytes,
      probe.feed.cfg.egress_queue_bytes + " vs " + expected.egress_queue_bytes);

// Buffer occupancy must never exceed the bound it is drawn against.
const overFull = state.dispersion.filter(d =>
  d.eqb && d.eqb.max > expected.egress_queue_bytes);
check("queued bytes never exceed the bound", overFull.length === 0,
      overFull.length + " boundaries over " + expected.egress_queue_bytes);
check("the buffer actually filled",
      state.dispersion.some(d => d.eqb && d.eqb.max > 0));

// The cohort grid: a cell is a mean residency or nothing, never a NaN and never
// out of range -- an empty cell must not read as zero coverage.
const grid = probe.cohortGrid();
check("cohort grid built", !!grid);
let cellBad = [];
if (grid) {
  for (const row of grid.cells) {
    for (const value of row) {
      if (value === null) continue;
      if (!(value >= 0 && value <= 1)) cellBad.push(String(value));
    }
  }
}
check("every cohort cell is a fraction or empty", cellBad.length === 0,
      cellBad.slice(0, 3).join(", "));
// The range check alone has no teeth: summing instead of averaging still lands
// in [0,1] when every cell holds one sample. This asserts the division is
// load-bearing -- at least one cell whose undivided sum would be out of range.
let loadBearing = 0;
if (grid) {
  for (let r = 0; r < grid.cells.length; r++) {
    for (let c = 0; c < grid.cells[r].length; c++) {
      if (grid.cells[r][c] !== null && grid.cells[r][c] * grid.counts[r][c] > 1)
        loadBearing++;
    }
  }
}
check("cells are means, not sums", loadBearing > 0,
      "no cell aggregates enough to tell the difference");

// The network view: a deterministic inspector, so its layout is checkable.
check("network view is enabled for this fixture",
      probe.networkDisabledReason() === null,
      String(probe.networkDisabledReason()));
const frame = { w: 800, h: 600, pad: { l: 40, r: 10, t: 14, b: 18 },
                ctx: new Proxy({}, { get: () => () => {} }) };
for (const layout of ["ring", "circle"]) {
  probe.setNetLayout(layout);
  const positions = probe.layoutNodes(frame);
  const placed = positions.filter(Boolean).length;
  check(layout + " layout places every node", placed === expected.nodes,
        placed + "/" + expected.nodes);
  const inside = positions.filter(p => p && p[0] >= 0 && p[0] <= frame.w &&
                                       p[1] >= 0 && p[1] <= frame.h).length;
  check(layout + " layout stays inside the canvas", inside === placed,
        inside + "/" + placed);
  // Deterministic: the same inputs must give the same picture, or a reader
  // cannot compare two runs -- or two frames of one run.
  const again = probe.layoutNodes(frame);
  check(layout + " layout is deterministic",
        JSON.stringify(again) === JSON.stringify(positions));
}
// The timeline layouts place the nodes that came to know the followed
// transaction, at its own clock, and nothing else.
const followedTx = [...state.knowledgeBins.keys()].sort((a, b) => a - b)[0];
const knowing = state.classHistory.get(followedTx).size;
for (const layout of ["rows", "lanes"]) {
  probe.setNetLayout(layout);
  const positions = probe.layoutNodes(frame);
  const placed = positions.filter(Boolean).length;
  check(layout + " layout places every node that came to know tx " + followedTx,
        placed === knowing, placed + "/" + knowing);
  const inside = positions.filter(p => p && p[0] >= frame.pad.l && p[0] <= frame.w &&
                                       p[1] >= 0 && p[1] <= frame.h).length;
  check(layout + " layout stays inside the canvas", inside === placed,
        inside + "/" + placed);
  const again = probe.layoutNodes(frame);
  check(layout + " layout is deterministic",
        JSON.stringify(again) === JSON.stringify(positions));
}
// The axis is an oracle too. Count, bounds and determinism all survive a
// time axis that puts every node in one column; only comparing the picture
// with the times and distances it claims to draw catches that.
probe.setNetLayout("rows");
{
  const order = probe.firstKnowing(followedTx);
  const positions = probe.layoutNodes(frame);
  let rowsBad = [];
  const distinctX = new Set();
  for (let i = 0; i < order.length; i++) {
    const x = positions[order[i][1]][0];
    distinctX.add(Math.round(x));
    if (i && order[i][0] >= order[i - 1][0] &&
        x < positions[order[i - 1][1]][0])
      rowsBad.push("node " + order[i][1] + " learned later than " +
                   order[i - 1][1] + " yet sits left of it");
  }
  check("rows x follows first-knowledge time",
        rowsBad.length === 0 && distinctX.size > 1,
        rowsBad.length ? rowsBad.slice(0, 3).join("; ")
                       : distinctX.size + " distinct columns");
}
probe.setNetLayout("lanes");
{
  const entry = probe.spreadEntry(followedTx);
  const hops = probe.hopDistancesFrom(entry);
  const positions = probe.layoutNodes(frame);
  const byHop = new Map();
  for (const pair of probe.firstKnowing(followedTx)) {
    const hop = hops.get(pair[1]);
    if (!byHop.has(hop)) byHop.set(hop, []);
    byHop.get(hop).push(positions[pair[1]][1]);
  }
  const laneHops = [...byHop.keys()].sort((a, b) => a - b);
  let lanesBad = [];
  for (let i = 1; i < laneHops.length; i++) {
    const above = Math.max(...byHop.get(laneHops[i - 1]));
    const below = Math.min(...byHop.get(laneHops[i]));
    if (above >= below)
      lanesBad.push("hop " + laneHops[i - 1] + " overlaps hop " + laneHops[i]);
  }
  check("lanes separate by hop distance from the entry",
        lanesBad.length === 0 && laneHops.length > 1,
        lanesBad.length ? lanesBad.join("; ") : laneHops.length + " lanes");
}
probe.setNetLayout("ring");
const hops = probe.hopDistancesFrom(0);
check("hop distances reach every node", hops.size === expected.nodes,
      hops.size + "/" + expected.nodes);
check("every metric yields a colour",
      probe.NET_METRICS.every(spec =>
        !!probe.nodeColour(0, spec, spec.top ? spec.top() : null)));
check("per-node state covers every node",
      state.nodeState.size === expected.nodes,
      state.nodeState.size + "/" + expected.nodes);

/* An author `display` beats the UA stylesheet's `[hidden] { display: none }`, so
   an element the script hides by setting `.hidden` stays visible unless the CSS
   says otherwise. That is not a JS bug and no amount of reducer checking finds
   it: the tab switch appeared to do nothing because #panels kept its grid. This
   is the static form of that check. */
const styles = html.slice(0, html.indexOf("</style>"));
const hiddenTargets = new Set();
for (const match of script.matchAll(/\$\("([^"]+)"\)\.hidden\s*=/g)) {
  hiddenTargets.add(match[1]);
}
const unhideable = [];
for (const id of hiddenTargets) {
  const rule = new RegExp("#" + id + "\\s*\\{[^}]*display\\s*:", "m");
  if (!rule.test(styles)) continue;              // no author display, UA rule wins
  const override = new RegExp("#" + id + "\\[hidden\\]");
  if (!override.test(styles)) unhideable.push(id);
}
check("every element the script hides can actually be hidden",
      unhideable.length === 0,
      unhideable.length ? "#" + unhideable.join(", #") +
        " has an author display and no [hidden] override" : "");

/* A custom property the script reads but the stylesheet never defines resolves
   to the empty string, and an empty fillStyle is *silently ignored* by canvas:
   the mark keeps the previous colour, which at first paint is black. The heat
   ramp shipped that way -- every heat-coloured node and cohort cell drew black
   while every numeric check stayed green, because wrong colours are still ink.
   The runtime harness cannot see it either (it stubs getComputedStyle), so the
   check is static: every "--name" string the script mentions must be defined. */
/* Per *theme block*, not per stylesheet: a variable defined only for dark
   passed the whole-stylesheet form of this check while light-theme users got
   canvas-default black -- the exact failure the check was written for, one
   theme over. The media-query dark block mirrors the
   [data-theme] one, so checking the bare light root and the forced-dark root
   covers all three. */
function definitionsIn(block) {
  const defined = new Set();
  for (const match of block.matchAll(/(--[a-z0-9-]+)\s*:/g)) {
    defined.add(match[1]);
  }
  return defined;
}
const lightBlock = styles.match(/:root\s*\{([\s\S]*?)\}/)[1];
const darkBlock = styles.match(/:root\[data-theme="dark"\]\s*\{([\s\S]*?)\}/)[1];
const themes = [["light", definitionsIn(lightBlock)],
                ["dark", definitionsIn(darkBlock)]];
const undefinedVars = new Set();
for (const match of script.matchAll(/"(--[a-z0-9-]+)"/g)) {
  for (const [theme, defined] of themes) {
    if (!defined.has(match[1])) undefinedVars.add(match[1] + " (" + theme + ")");
  }
}
check("every custom property the script reads is defined in every theme",
      undefinedVars.size === 0,
      undefinedVars.size ? [...undefinedVars].join(", ") : "");

console.log(failures ? "\n" + failures + " FAILURES" : "\nall checks passed");
process.exit(failures ? 1 : 0);
