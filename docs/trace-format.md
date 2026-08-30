# Trace format

Passing `trace=<text stream>` to `NetworkSim` writes JSON Lines: one compact
JSON object per line. `starksim.trace.Tracer.emit` flushes after each emitted
event record, so following a running simulation does not require the caller to
open a line-buffered stream.

The trace is an observation of one run, not a serialization of all simulator
state. It is designed to reconstruct per-node propagation, residency,
utilization, queues, traffic, selected transaction knowledge, losses, and block
inclusion without consulting the live Python objects.

## Scope and sampling

The `cfg` header records how the trace was observed:

- `trace_focus` is `"all"` or the node ids whose node-scoped events are kept.
- `trace_sample=k` retains every kth `snd` event. No other event kind is
  sampled.
- `trace_counter_interval` sets the exact cumulative-counter grid.
- `trace_follow` records whether knowledge transitions are disabled, selected
  by explicit leaf indices, or selected by independent Bernoulli trials.
- `topology` is the sorted list of canonical undirected edges, or `"omitted"`
  when the directed degree sum exceeds `TOPOLOGY_EDGE_LIMIT`.

The `snd` sample is systematic, not random. Event order is deterministic and
may correlate with payload kind, so multiplying a sampled count by `k` is not
an unbiased estimate. Exact totals come from unsampled `cnt` records.

Focus applies only when an event is emitted with a tracer node id. `blk`, `seg`,
and `end` are global and survive a focused trace. A `blk` record carries its
proposer as data rather than as node scope.

## Schema version

`starksim.trace.SCHEMA_VERSION` is stored as `cfg.trace_schema`. Increment it
before changing a field's meaning or removing a field. Readers may ignore an
additive field they do not understand, but must not infer a changed meaning from
field presence. A trace with no `trace_schema` predates versioning; this includes
old traces that encoded the block proposer as `blk.n`, which cannot be repaired
by feature detection. Version 2 added `o` and `peer` to `knw`; in a version-1
trace their absence says nothing, so a reader draws no spread tree from one.
Version 3 added `dup`; a trace without it recorded no duplicates, not none.

The first line is exactly one `cfg` record. Every later event has a simulated
timestamp `t`; node-scoped events also have `n`.

## Message loss and object departure

Two event names are deliberately similar and must not be conflated:

- `drp` is one dropped message. An egress queue refused bytes before they
  crossed the wire, or a CPU queue refused verification after they arrived.
- `drop` is one or more objects leaving a node's view. The objects were
  previously resident and became superseded, pruned, or evicted.

`drp` contributes to loss counters. `drop` changes residency but says nothing
about whether a wire message was lost. Both kinds are unsampled.

## Event records

### `cfg` — run and observation configuration

`cfg` contains `NetworkSim.variant`: network size and latency identity,
capacities, wire sizes, bounds, policy and mode names, cost samples, and seed.
It then adds the observation fields listed above.

The variant does not identify the workload. Birth count, timing, fees, and
lifetimes are scheduled by the harness and appear only through later events.

Example fields:

```json
{"e":"cfg","nodes":20,"degree":4,"seed":"1","trace_schema":2,
 "trace_sample":1,"trace_focus":"all","trace_counter_interval":500.0,
 "trace_follow":{"mode":"none"},"topology":[[0,1],[0,7]]}
```

### `arr` — transaction birth

```json
{"e":"arr","n":7,"t":120.0,"o":42,"tx":11,"k":"q","fee":3.0}
```

`o` is `PoolObject.id`; `tx` is the leaf index used by coverage, settlement,
and followed-transaction masks. The namespaces diverge once aggregates are
created. `n` is the entry node and `k` is `q` or `p`.

Birth admits the leaf at its entry node in the same action. The origin therefore
has no matching `adm`; transaction reach reconstruction must seed the entry node
from `arr` at delay zero.

### `snd` — message accepted by egress

```json
{"e":"snd","n":3,"t":221.4,"to":9,"k":"wrapper","b":263592}
```

`n` and `to` are sender and receiver, `k` is `object`, `announce`, `want`,
`wrapper`, or `block`, and `b` is the payload's byte size. It means the message
entered egress, not that egress completed or the receiver admitted it. This is
the only sampled event kind.

### `drp` — message refused

```json
{"e":"drp","n":3,"t":221.4,"to":9,"k":"wrapper","b":263592,
 "why":"egress_full"}
```

`b` and `k` identify the refused payload. `why` is `egress_full`, `verify_recv`,
`verify_wrapper`, or `verify_block`. An egress refusal includes `to`; a CPU
refusal is scoped to the receiving node and need not include it. The viewer
groups these reasons because an egress refusal sends no bytes, while a
verification refusal occurs after ingress has already been paid.

### `adm` — object admission

```json
{"e":"adm","n":3,"t":221.4,"o":42,"born":120.0}
```

The node admitted an object after delivery and any required verification.
`born` permits direct propagation-delay reconstruction. `via:"wrapper"` marks
admission of an aggregate proof; its `o` is the aggregate object id, not every
transaction it claims, so transaction-level reach must exclude those records.

### `drop` — objects departed a view

```json
{"e":"drop","n":3,"t":800.0,"why":"pruned","o":[42,57]}
```

`o` is a batch of object ids. `why` is `superseded`, `pruned`, or `evicted`.
Applying `arr`, transaction `adm`, and `drop` in line order reconstructs current
residency; omitting `drop` reconstructs cumulative reach instead.

### `knw` — followed-transaction knowledge transition

```json
{"e":"knw","n":3,"t":52.5,"tx":11,"from":"verifying",
 "to":"held_and_body","by":"service_done","o":42,"peer":1}
```

`tx` is a leaf index. The knowledge classes are `settled`, `held_and_body`,
`held_no_body`, `body_only`, `verifying`, `requested`, and `none`, in that
precedence order. `from` and `to` differ; each `from` continues the previous
`to` for the same `(n, tx)` pair.

`o` is the object the new class rests on: the transaction's own object when
the node has it, else the lowest id among the objects that match. What
matches depends on the class — a held object must *claim* the transaction;
an object in flight or requested may claim or merely cover it, since a full
transfer delivers the bodies its structure covers; and a wrapper being
verified matches when the transaction's body is riding on it, whether or
not its proof is known. `o` is absent for `settled`, `body_only` and `none`,
which no single object evidences. Whether `o` is the transaction's own object
or a wrapper is the *form* in which the node knows it.

`peer` is the node whose message caused the transition: present when the
event delivered `o` to `n`, delivered the transaction's body to a node that
lacked it (a wrapper by reference, or one whose proof was refused and whose
bodies were kept), or delivered the block notice that settled `tx`. It is
absent when the node knew without being told — the entry node at birth, the
proposer building a block, instant notification, expiry. One event can change
several transactions at a node, since admitting one object can evict another,
and the peer is attributed only to the transitions its cargo appears in. When
both causes apply to one transition — a proof one peer's copy is verifying and
a body a second peer's copy brought, merged into that verification — the
transition lands when the verification completes and carries the first peer;
the body's deliverer gets no transition of its own, because the merge is not
one.

For one transaction, each node's first transition out of `none` is where it
came to know it; where that transition carries a `peer`, the edge from the
peer to the node belongs to the transaction's spread tree, which is rooted at
the entry. A node's later transitions, and a later delivery after it forgot,
are not edges. The tree is complete only when every node is traced.

`KnowledgeTracer.flush` emits one net transition after a complete engine event
and its synchronous callbacks. Intermediate states inside one handler are not
observable. `by` is the engine event kind responsible for the boundary.

Only explicitly or Bernoulli-selected transactions produce `knw`. Nodes with no
transition are implicitly in `none`; a reducer must include them when building
a partition over the traced node set.

### `dup` — a delivery that taught the receiver nothing

```json
{"e":"dup","n":3,"t":221.4,"peer":9,"o":42,"k":"object","b":3308,
 "txs":[11,12]}
```

The node already held, was verifying, or had asked for what `peer` sent:
an object, a wrapper naming a proof it has, or an announcement of something
already requested. `txs` are the followed transactions the delivery named:
the object's structural coverage, except for a wrapper, whose copy names
its own claim — a mask that can be narrower than the structure of the
aggregate behind it. A record exists only when there is at least one, so
this is bounded by the follow rate the way `knw` is, not by traffic the
way `snd` is. It is not sampled. Block notices are not recorded here: a
block covers everything it settled rather than any one transaction. The
simulator's `duplicates` metric is wider, counting objects that cover no
followed transaction; a `dup` count is a lower bound on it and exact for
the followed set.

One wrapper copy that verifies nothing and brings no fresh body is still
not a duplicate: a recursive proof arriving while mode-0 witnesses for the
same claim are being verified upgrades the pending evidence — the claim it
leaves behind resolves by reference where the one it found did not — and
is recorded nowhere, matching the `duplicates` metric, which skips it too.

The spread tree in `knw` is what a transaction's propagation achieved; the
`dup` records are what it cost, and the viewer draws the two together.

### `wrp` — wrapper emission

```json
{"e":"wrp","n":3,"t":500.0,"w":11,"o":88,"deps":34,"tick":1}
```

`w` is wrapper identity, `o` the root proof object, `deps` its claimed
dependency count, and `tick` the node's aggregation round. One core may produce
several differently sized peer payloads, which appear separately as `snd`.

### `blk` — block construction

```json
{"e":"blk","t":2000.0,"slot":1,"proposer":0,"txs":32,
 "objs":5,"wasted":3}
```

`txs` is newly included transaction count, `objs` is the number of pool objects
selected, and `wasted` is attempted claim coverage already discarded by
canonical chain truth. The record has no `n`; `proposer` is data so focus cannot
remove blocks from the chain history.

### `cnt` — exact per-node counters and gauges

```json
{"e":"cnt","n":3,"t":2000.0,"eb":812.4,"cb":140.0,
 "ob":9812345,"sb":9700000,"ib":8123456,"idb":204800,
 "obk":{"object":9700000,"wrapper":112345},
 "sbk":{"object":9600000,"wrapper":100000},
 "ibk":{"object":8000000,"wrapper":123456},
 "eq":2,"eqb":263592,"cq":0,"dr":1,"drb":263592,
 "held":41,"stale":12}
```

The cumulative counters are egress busy time `eb`, CPU busy time `cb`, offered
bytes `ob`, completed-egress bytes `sb`, ingress bytes `ib`, duplicate ingress
bytes `idb`, drops `dr`, and dropped bytes `drb`. `obk`, `sbk`, and `ibk` split
the byte counters by payload kind; absent kinds have not occurred at that node.

The gauges are waiting egress messages `eq`, waiting egress bytes `eqb`, waiting
CPU jobs `cq`, and held-object count `held`. `stale` is cumulative stale
dependencies proved, or `-1` under instant chain notification where delayed
knowledge cannot be expressed.

Records appear at global multiples of `trace_counter_interval` and at the start
and end of each `NetworkSim.run` segment. Segment-boundary counters carry
`seg:"segment_start"` or `seg:"segment_end"`. There is at most one `cnt` for a
node at a timestamp.

Rates and utilization are reconstructed by differencing cumulative counters.
For example, egress utilization between two samples is
`(later.eb - earlier.eb) / (later.t - earlier.t)`. Counter observation uses
`Engine.observe_every`, not calendar events, and is present whether or not a
trace stream is enabled.

### `seg` — run segment boundary

```json
{"e":"seg","t":1200.0,"start":500.0,"why":"until","events":41}
```

Each call to `NetworkSim.run` emits one `seg`. `why` is `drained` when the
calendar emptied or `until` when a horizon stopped the call with later work
pending. The engine also supports `max_events`, but `NetworkSim.run` does not
expose that bound. `events` is the number processed in this segment.

### `end` — terminal trace status

```json
{"e":"end","t":1200.0,"status":"complete"}
```

`NetworkSim.finish_trace` accepts `complete`, `error`, or `cancelled`, emits
`end`, and seals the engine against further scheduling or running. A drained
`seg` is not terminal because the caller may schedule another segment.

If `end` is absent, the trace alone cannot distinguish an active, cleanly idle,
crashed, or killed process. A growing file may end in a partial JSON line; a
live reader buffers it until a newline arrives and must not infer a crash.

## Time and line order

Event timestamps are rounded to three decimal places, giving the trace a
0.001 ms quantum. Apply records in file order when timestamps are equal. The
trace makes no claim about intermediate state within one quantum, and a
comparison against simulator state must use the same quantization.

Line order is also meaningful across different event kinds. In particular, an
`adm` is emitted before any immediate `drop` caused by enforcing the candidate
bound, so a residency reducer first adds and then removes the object.

## Reconstruction examples

Transaction propagation delay requires both the entry event and non-wrapper
admissions:

```python
born = {}
delays = []
for event in events:
    if event["e"] == "arr":
        born[event["o"]] = event["t"]
        delays.append(0.0)
    elif (event["e"] == "adm" and "via" not in event
          and event["o"] in born):
        delays.append(event["t"] - event["born"])
```

Current leaf residency starts with `arr`, adds matching transaction `adm`, and
removes ids listed by `drop`. Keeping a separate set that never removes ids
gives reach. `tests.test_trace` checks both reconstructions against live
simulator state.

## Precision limit

Timestamp quantization remains a format limit. Coverage or state at an age
inside one 0.001 ms interval cannot be reconstructed more precisely than line
order within that quantum.

See [viewer.md](viewer.md) for live following, replay, and the reducer and
render verification commands.
