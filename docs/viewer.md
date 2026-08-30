# Trace viewer

`viewer.html` replays `trace.jsonl` as three pages: charts, the network, and
one transaction's life on its own clock. It
can read a completed trace or follow one while a simulation appends to it. The
viewer reduces events in the browser; it does not import simulator state or a
bench CSV.

The page is one self-contained HTML file on purpose. There is no application
build step, generated JavaScript bundle, or runtime package dependency. That
makes a saved trace and viewer easy to inspect together and keeps the reducer
code directly testable. Node and Playwright are needed only for verification.

## Produce a trace

Pass a writable text stream to `NetworkSim(trace=...)`, run the simulation, and
call `finish_trace` when no more events will be scheduled:

```python
import random

from starksim.arena import KIND_PQ
from starksim.netsim import NetworkSim
from starksim.network import Overlay

overlay = Overlay.random_regular(20, 4, random.Random("trace-example"),
                                 base_latency=100.0)
with open("trace.jsonl", "w") as stream:
    sim = NetworkSim(overlay, seed=1, trace=stream,
                     trace_follow_rate=0.1,
                     trace_counter_interval=500.0)
    sim.schedule_birth(0.0, KIND_PQ)
    sim.schedule_ticks(8)
    sim.schedule_slots(2)
    sim.run()
    sim.finish_trace()
```

`Tracer.emit` flushes each emitted event record, so a follower can see the file
grow. `finish_trace("complete")` writes the terminal `end` record and seals the
engine. Use `cancelled` when intentionally stopping with work pending or
`error` when the caller is recording a handled failure.

Trace controls on `NetworkSim` include:

- `trace_focus`, a node subset whose node-scoped events are retained;
- `trace_sample`, which keeps every kth `snd` event;
- `trace_counter_interval`, the exact per-node counter grid;
- `trace_follow` for explicit leaf indices; and
- `trace_follow_rate` plus `trace_follow_seed` for independent Bernoulli
  selection of transactions whose knowledge transitions are recorded.

The header records all of these choices. See
[trace-format.md](trace-format.md) before interpreting sampled or focused data.

## Serve and replay

From the repository root, place the completed trace at `trace.jsonl` and run:

```sh
python3 serve.py
```

Open <http://127.0.0.1:8000/viewer.html>. The viewer loads the file, replays it
against a wall-clock control, and can pause, change speed, scrub, or restart.
The three pages are views of one reducer state.

For a completed trace, an ordinary static server can return the whole file. For
a live or large trace, use `serve.py`. Python's `http.server` does not implement
the open-ended byte ranges the follower needs, so each poll would download the
entire file again. `serve.py` supplies `Range`, `206`, `416`, `HEAD`, a stable
per-run `ETag`, and no-cache headers, and it measures response length before
reading a file that may still be growing.

It also publishes only `viewer.html` and `*.jsonl` files in the directory it is
started from — no directory listings, and no traversal into subdirectories. The
directory being served is normally this source tree, and a general-purpose
static server pointed at it would publish the whole repository, `.git`
included, to anything that can reach the port. The bare root redirects to the
viewer.

The viewer always requests `trace.jsonl` relative to itself. To replay another
file, make that file available under this name in the directory being served,
then use the replay button or reload the page.

## Follow a live run

`tools.live_run` schedules the simulation in chunks and paces simulated time
against wall time. Run it and the server in separate terminals:

```sh
python3 -m tools.live_run
python3 serve.py
```

The runner writes `trace.jsonl` by default. `--speed 0.25` runs in slow motion,
`--duration 120` stops after 120 simulated seconds, and `--out PATH` changes the
output location. With the default path, opening the viewer URL above is enough;
it polls for appended byte ranges and applies only complete JSONL records.

Trace volume depends mainly on the egress buffer and counter interval:

- `--egress-bytes` defaults to two wrapper **emission rounds**, derived from the
  degree, because one round is `degree x recursive_proof` — 2.1 MB at degree 8 —
  and a buffer below that overflows on every tick whatever the load. Pass a
  number to bound it explicitly, or `0` for unbounded.
- `--counter-interval` (default 500 ms) sets the per-node counter grid, producing
  `nodes / interval_seconds` counter records per simulated second. Only `snd`
  can be thinned with `trace_sample`; `tools.live_run` does not currently expose
  that option.

Ctrl-C or SIGTERM seals a live trace as `cancelled`. A duration-bounded run
seals it as `complete`. If `end` is absent, the format cannot distinguish a run
that is active, idle, crashed, or killed.

## What the viewer shows

The charts page has two sections. The first is about the whole network:
residency by birth cohort, mean residency by hop distance from each object's
own entry, utilization and queue dispersion across nodes, per-kind
offered/sent/ingress bytes, and drops. The second is about one transaction
and one object: the followed transaction's knowledge classes, the selected
object's reach and residency over every other object's residency, and its
residency by hop distance. The bar above the second section carries the two
selectors and one note for the section. Following a transaction also selects
its own proof object until an object is chosen by hand, so the section
describes one thing unless the reader asks otherwise.

The two hop-distance panels put age on the x-axis and draw one line per ring
around the entry, so a propagation wave reads as the outer rings rising after
the inner ones. They need the topology from `cfg`, and per-hop residency is
kept only up to 4096 nodes, because the reducer caches one distance array per
entry node; above either bound the panels say so. The all-objects panel is a
mean against age rather than the clock, for the reason the tiles carry no
mean-propagation figure: averaging objects born seconds apart on one clock
mixes the wave with the arrival process. Its age axis stops at the 90th
percentile of observed ages, and the readout says how many objects each mean
is over.

The selection is also placed on the whole-network panels rather than
confined to its own section: every time-axis panel carries a vertical at the
selected object's birth and, when its transaction is followed, at the first
`settled` transition, and the block ribbon outlines the block that settled
the followed transaction. That block is the latest one built no later than
the first `settled` transition, which is exact when the proposer is traced;
the section is unavailable under a focus subset for the same reason its
panels are.

The network view uses the topology in `cfg` for layout, colors nodes by
the followed transaction's knowledge class by default or by a selected
metric, and shows recent admissions as ripples. Magnitudes use one
sequential ramp; the legend states the highest value present, and the scale
control stretches the ramp to that maximum when a fixed ceiling such as 100%
utilization would leave a lightly loaded network uniformly pale. The followed
transaction and the follow mode are offered on its bar as well, because it
colors nodes by their knowledge of that transaction.

Colouring by knowledge also draws how that transaction spread: an edge from
the peer that delivered the evidence to each node, at the node's first
transition out of `none`, in green — the shade is the form the evidence
took: its own object darkest, then a wrapper, then an announcement; a block
notice is grey. A node whose first
transition carries no `peer` knew without being told — the entry at birth,
the proposer at its block — and gets no edge. The edges come from the `peer`
and `o` fields of `knw`, so the tree needs a schema-2 trace and the legend
says so on an older one; like the rest of the network view it needs every
node traced. It is a record of successful spread only: a node is joined
once, at its first knowing, and a later delivery of the same transaction is
not an edge. Duplicates, refusals and the traffic a transaction cost are a
different question that this picture does not answer. The ring layout is
centred on the transaction's entry, so the tree grows outward across the
rings.

The transaction page shares the network page's canvas and bar, and puts time
on the x-axis — the followed transaction's own clock, birth to settlement,
or, while it is unsettled, a whole number of slots reaching past now, so the
axis holds still within each slot instead of sliding under the eye — placing
each node at the moment it first came to know the transaction;
nodes that have not are not yet part of the picture, and the note counts
them. Its two layouts: `rows by first knowing` gives each
such node a row, in the order they learned, with a bar showing its knowledge
class over time: a sequence diagram of the transaction's life, with the
spread edges standing at their delivery time from the peer's row to the
node's, and a dashed line at settlement. `lanes by hop distance` lays the
rings flat, one lane per hop from the entry, so the wave reads as arrows
climbing lane to lane. Only the arrival of a delivery is on record, so an
edge stands at that moment rather than sloping from a send time the trace
does not have. Rows grow thin past a hundred or so nodes; lanes are the
layout for a large network.

What the spread cost is drawn in red: every delivery of the followed
transaction that taught its receiver nothing, from the `dup` records, as a
thin arrow from the peer to the node. On the network page each flares at
its moment and fades over a second, since a flood of them held forever
would bury the tree; on the transaction page they stand at their time. The
`duplicates` checkbox on the bar turns them off. The knowledge panel's note
counts them with their bytes, and a node's tooltip says how many it
received and how many it sent. A trace older than schema 3 recorded none,
and the legend says so.

Every panel has a `?` that opens a paragraph on what is drawn, how it is
computed and what to read from it, and a tabular reading so exact values are
not available only through pixels. The transaction and object selects hold
still while open, and their step buttons move through the list without
opening it.

In a live run the transaction worth watching keeps changing, so the section
bar has a follow mode. `auto`, the default, holds a transaction until its
story ends — settled, or forgotten by every node — or until the chosen
number of slots has passed, whichever comes first, then takes the youngest
followed transaction whose story has not ended. Three slots, the default,
tells each story to the end — spread, inclusion, retirement; one slot keeps
to the front of the load. `pinned` is the reader's own choice, and any pick
from the select, the step buttons or the ticker pins. Switching runs on the replay
clock, so pause freezes it and speed scales it, and the ticker under the bar
shows the last eight followed transactions with what became of each —
spreading, settled, gone — the active one marked. The knowledge panel draws
against the transaction's own clock, from its birth to its settlement or to
whole slots covering now, so the story fills the panel however long the run
has been going and the axis rescales only at slot boundaries; on
the network view the previous transaction's spread tree fades over a slot
while the next one grows.

The viewer is a projection of the trace. It cannot recover unsampled `snd`
events, transactions not selected for `knw`, or topology omitted above the
trace edge limit.

## Verification

The viewer has three verification layers.

First, the Python unit suite checks trace emission, accounting, lifecycle, and
server behavior:

```sh
python3 -m unittest discover -s tests
```

Second, a deterministic fixture is replayed through the actual JavaScript
reducers. The check compares reconstructed residency, knowledge partitions,
bytes, drops, topology, and lifecycle with the simulator's expected values:

```sh
python3 -m tools.make_viewer_fixture .render-fixture
node tools/check_viewer.mjs .render-fixture viewer.html
```

Third, Playwright checks that reducer output reaches visible panels in Chromium
without console errors and that both themes render meaningful colors:

```sh
npm ci
npx playwright install chromium
npx playwright test
```

Reducer checks catch wrong numbers even when the page looks plausible. Browser
checks catch blank canvases, hidden-panel CSS errors, and data that never reaches
the screen even when the reducer state is correct.
