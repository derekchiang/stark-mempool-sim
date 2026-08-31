> Historical record, not a specification. Current material: see [docs/model.md](../model.md).

> **What this is.** The design as it was written and revised, with its rationale
> and the questions it left open.

# Plan: a networked mempool model (v2)

Today's model (`docs/history/v1-model.md`) has one global object store and derives who can
see what from arithmetic on timestamps. This plan replaces it with real mempool
nodes, a real overlay, real queues and real service times, keeping the object
*semantics* that work (coverage, folding, death) while replacing their
representation.

**Status.** Revised after an external review of the first draft. Two outcomes
reshaped it: v1 is **not** retained — v2 replaces it, so nothing constrains the
object model — and the first draft's "phase 1 reproduces v1" bridge test was
unachievable and has been replaced with analytic criteria. Reversals are recorded
under *Answered questions*.

> This document is the **design**: what gets built and why. For the current state
> and what to do next, see [roadmap.md](../roadmap.md).
>
> The build log keeps each phase's narrative **next to the criteria it belongs
> to**, which is what makes a repeated mistake visible: several corrections there
> were prompted by reading the previous phase's amendment while writing the next
> phase's criteria.

**Part I is normative**: it is what gets built, including the decisions already
taken. **Part II is rationale**: alternatives, trade-offs, and the questions still
open. If the two disagree, Part I is wrong and must be fixed — the first draft had
four such contradictions.

**Design principle: this simulator exists to compare design options.** Anything
that is a protocol or policy choice — fold plan, aggregation strategy, what a node
does when its proving budget runs out, topology, latency model, how chain facts
reach nodes — is a named, swappable strategy selected by configuration, never a
decision welded into the code. Anything that is purely an implementation detail —
how coverage is stored, how the calendar is ordered — is free to be optimised, and
no experiment may be able to observe which choice is in force. Two consequences: a
strategy registry lives with the domain code (not in the engine), and every run
carries its variant identity into the metrics so sweep rows stay attributable.

---

# Part I — Design

## 1. The central break: visibility stops being a formula

Everything in v1 follows from one line:

```python
visible(obj, viewer, now) = obj.creator == viewer or obj.born + l <= now
```

With a multi-hop overlay, queueing and finite bandwidth, arrival time is a
*consequence* of the simulation, not a function of birth time. So:

> **Visibility becomes stored state.** Each node owns what it knows.

This is the change that forces all the others, and no parameterisation preserves
the old formula.

A second, subtler break rides along with it. v1 decides "already folded away" by
looking at an object's `parents` — but **`parents` and `children` are simulator
provenance, not protocol data.** A mode-1 wrapper exposes a dependency list and a
proof, not the identity of the recursive inputs that went into it. A node must not
consult global parent links to decide anything, or it silently becomes omniscient.
Supersession therefore becomes a per-node judgement over what that node received.

## 2. Object model: global truth, chain facts, per-node belief

Three homes, not two. v1 mixes all three on `PoolObject`, which only works because
there is exactly one builder.

| global truth (on the object) | chain facts (`ChainState`) | per-node belief (`NodeView`) |
| --- | --- | --- |
| `id`, `born`, `creator` | which txs are included, and in which block | which objects it holds, and when each arrived |
| `children` (provenance) | canonical block sequence | `known_discarded` mask |
| coverage, `total`, `depth` | | candidate set and its heap state |
| `consumed` (work spent) | | per-peer "peer has the body" bits |

Rules that follow:

- **`useful_count()` becomes node-relative.** It currently reads `tx.included`
  directly. In v2, usefulness is always evaluated against a *node's*
  `known_discarded`, never against global truth. The global flag stays in
  `ChainState`, deliberately out of reach of node code, so accidental omniscience
  takes a visible API call rather than an attribute read.
- **`known_discarded`, not `known_included`.** Inclusion is only one reason a
  dependency stops being worth proving; the EIP also prunes on same-sender
  invalidation and arbitrary expiry (`eip-8288.md:255`). The mask carries all of
  them, even if early phases only populate it from blocks.
- **Coverage is fixed at fold time** from the inputs the node selected, then
  filtered by the querying node's knowledge. v1 does the opposite — it rebuilds
  coverage at completion by consulting current global inclusion (`objects.py:87`) —
  and that is exactly the omniscience to remove.
- **Immutable coverage is load-bearing, not defensive.** In v1, never mutating a
  coverage list was a discipline with no reachable bug behind it. With `N` nodes
  reading the same coverage across simulated time, immutability is what makes
  sharing sound at all.
- **One instance per object, shared by reference.** No node holds a copy. But note
  what sharing does *not* buy: see §3.

## 3. Object lifecycle and retention

Sharing the payload bounds cost per object. It does nothing about the *number* of
objects, nor about per-node bookkeeping — and "prune dead objects" does not save
us, because an aggregate covering any still-pending tx stays alive while every node
emits another one every tick. Left unbounded, `N` nodes × membership + arrival time
+ heap state + seen-bit over ~10^5 aggregates walks straight back to `N × P`
records. Retention is therefore a first-class part of the design, not a cleanup
detail.

Every object a node handles moves through:

```text
delivered -> duplicate-checked -> queued for verification -> admitted
          -> candidate -> superseded | discarded | evicted
```

- **duplicate-checked**: against a compact per-node *ever-seen* structure (ids
  only). A duplicate is counted as bandwidth waste and dropped without
  verification.
- **admitted**: verification finished; the object enters the node's view.
- **candidate**: eligible as a fold input. The candidate set **will be bounded,
  and is not yet**: the intended bound is a node's own previous aggregate, the
  latest wrapper per peer, and loose txs not covered by those, with older peer
  aggregates retired as soon as a newer wrapper from that peer is admitted. But
  per `[D7]`/B1 the bound is chosen from measured growth rather than guessed,
  because too tight a bound makes aggregation look worse than reality. Until that
  measurement exists the set is unbounded and instrumented. Part I said
  "explicitly bounded" while Part II said measure-first; this is the correction.
- **superseded**: the node itself folded it in, or a newer wrapper from the same
  peer replaced it. Per-node, never inferred from `parents`.
- **discarded**: `known_discarded` now covers everything it covered.
- **evicted**: retention policy dropped it while still nominally useful. This is a
  *measured* event, not a silent one — eviction rate is a metric, because it is the
  model admitting it lost information.

Two stores, distinguished on purpose: a global **arena** that owns objects and
their provenance DAG, and per-node **views** that reference it. Structural sharing
(§7) keeps ancestors reachable from the arena long after every view has moved on,
so "removed from all views" and "collectable" are different questions. Only the
arena may free.

## 4. Entities

**`Node`** — owns a `NodeView` and its per-peer body beliefs. Its CPU and egress
servers live in `netsim`, *not* on the node: `node.py` is deliberately
calendar-free, so anything that schedules belongs to the wiring layer. Receives
messages, verifies, admits, and on its own tick selects, proves and fans out. **Every node is an ordinary mempool node**; building is not a
static role but a per-slot assignment (see `ProposerSchedule` below and `[D18]`),
so the node that builds also carries out normal gossip duty before and after.

**`EgressServer`** — **this is where bandwidth is enforced**: one server per node,
capacity in **bytes per unit of simulated time** (bytes/ms, with ms as the unit —
`network.bytes_per_ms()` converts from Mbit/s), FIFO deque, serialising every
outgoing message across all peers. Fan-out therefore costs `degree × size`, which is the cost the EIP's
bandwidth claim lives or dies on. Ingress is **measured, not metered** (§Part II
[D2]).

**`Link`** — a *directed* edge `u → v` carrying latency only: a base value plus
jitter. **No capacity and no queue**, unless the optional per-link cap is enabled,
which makes it a second serialising stage in tandem with egress and gives it its
own server and completion event. Default: off.

**`Overlay`** — builds the graph from `(N, degree, seed)`; random regular by
construction-then-randomisation, with a declared valid `(N, d)` domain.

**`Message`** — typed, with a **size in bytes**: `wrapper` (mode 0 witnesses or
mode 1 recursive proof, bodies or hashes) and `block` (see `chain_notify`).

**`Computation`** — typed work with a duration from an external table.

**`ChainState`** — the canonical block sequence and inclusion truth, plus the
`chain_notify` strategy that decides how nodes learn of it (`instant` or
`overlay`).

**`ProposerSchedule`** — which node builds slot `k`. `N` counts *every* node; there
is no extra, privileged builder entity, because decentralised building means the
proposer is an ordinary node with an ordinary node's view and degree. Selection is
a strategy: `fixed(id)`, `rotating`, or `random(seed)`. `[D18]` explains why this
is a first-class axis rather than a detail.

**`Engine`** — calendar and run loop, domain-agnostic. Knows nothing of mempools.

## 5. The engine: a calendar plus servers that own deques

> Each server — a node's CPU, a node's egress, optionally a link — owns a
> `deque` of **waiting** work plus a separate `current_job`, and has at most one
> outstanding completion event in the calendar.

Queueing delay is then emergent: nothing computes it, it falls out of arrival rate
versus service rate.

Precise contract, because the first draft was vague enough to cause a rewrite:

- `current_job` is **not** in the deque. So "busy" is `current_job is not None`,
  and a busy server can have an empty deque. (The first draft's invariant "deques
  are empty iff idle" was simply false.)
- **Queue depth** means *waiting* jobs. Jobs-in-system is `waiting + busy`, named
  separately wherever it is reported.
- **Utilisation** is busy-time integrated over the interval, never a sample of
  `len(deque)` at slot boundaries.
- A completion **applies its result before** starting the next job.
- **Cancellation is part of the phase-0 API**: `cancel_waiting(predicate)` plus
  generation-stamped jobs. Waiting work is removable; work already in service is
  not preemptible. This exists because `[D7]` needs it — contrary to the first
  draft's claim that nothing depends on that open question.
- Zero-duration jobs complete at the current timestamp without reordering.

**What the calendar holds**: server completions, arrivals, ticks, slots, *and one
`DELIVER` per message in propagation*. The first draft claimed the calendar avoids
holding messages in flight; that is wrong — with latency-only links every message
is a live event, up to `N × degree` per tick.

Ordering stays exact `(t, seq)`. A binary heap is the reference implementation;
`[D10]` covers when a timing wheel becomes worth it.

## 6. Event catalogue

| event | fires when | effect |
| --- | --- | --- |
| `TX_ARRIVE` | a user broadcasts to its entry node | object created in the arena, admitted at that node |
| `EGRESS_FREE` | a node finished serialising a message | pop egress deque, start the next; schedule its `DELIVER` |
| `LINK_FREE` | *only if* the per-link cap is enabled | second serialising stage before `DELIVER` |
| `DELIVER` | a message's last bit lands at `v` | duplicate check, then enqueue verification on `v`'s CPU |
| `CPU_FREE` | a node finished a computation | apply result, pop the next |
| `TICK` | a node's aggregation interval elapses | select, plan, enqueue proving |
| `SLOT` | slot boundary | the slot's proposer builds from **its own view**; `ChainState` advances |

Store-and-forward throughout: a node cannot relay what it has not fully received
and verified.

## 7. Coverage representation

Settled (rationale in `[D15]`):

- **Cold objects** store an immutable tuple of children and nothing else.
- **Hot objects** memoise one **global coverage bitmask** (a Python `int` over
  dense leaf ids). Coverage is immutable, so this cache never needs invalidating —
  distinct from the *useful count*, which is per node and per discard generation
  and must not share the same cache.
- A node holds `known_discarded` as a mask; marginal usefulness within a block is
  `coverage & ~known_discarded & ~chosen`, and its population count is one
  `int.bit_count()`.
- Hot caches are **bounded with an eviction policy**, and the bound is measured.
- **No window rebasing** until measurements demand it: rebasing changes the
  coordinate system of every cached mask and every node's discard mask at once, so
  it is not a local optimisation.

## 8. Node duty cycle

Per tick, following EIP-8288 (`eip-8288.md:252-265`):

1. **Prune** against `known_discarded`.
2. **Select** inputs by the configured strategy — the EIP's order is own previous
   aggregate, then removals, then peers' aggregates by *new* txs contributed, then
   loose txs by fee. Selection stops when estimated proving cost reaches the
   interval budget.
3. **Plan.** The selection becomes an `AggregationPlan`: a tree of proving jobs
   with one root, decided by the `fold_plan` strategy (`binary`, `wide`, or a
   fan-in cap). A plan is not one job — the first draft's "enqueue one
   `prove_recursive`" contradicted its own fold-arity option. The plan declares
   its jobs, their dependency order, and when the root output becomes available.
4. **Emit** one wrapper per peer when the root completes, bodies replaced by
   hashes for peers whose body bit is set, handed to the egress server.

Empty ticks emit nothing. Unary ticks (one selected input) re-emit that input
rather than fabricating a one-child aggregate.

## 9. Module layout, and the retirement of v1

```text
starksim/
  engine.py     NEW  calendar, Server (waiting + current_job + cancel), run loop
  network.py    NEW  Overlay, Link, Message, SizeModel
                     (egress is an engine.Server held by netsim, not a class here)
  node.py       NEW  Node, NodeView, roles, strategies
  chain.py      NEW  ChainState, block facts, chain_notify, ProposerSchedule
  arena.py      NEW  arena objects: children tuple, coverage, no viewer state
  objects.py    v1's, untouched, deleted with the rest of v1 at phase 3
  costs.py      NEW  computation cost table, externally supplied
  strategies.py NEW  the registry (domain-side, not in engine.py)
  netsim.py     NEW  wiring, CLI, metrics, trace
```

**v1 is retired, not preserved.** `sim.py`, `actors.py` and `objects.py` are
replaced; there is no compatibility layer and no bridge test, which is what makes
the object model free to change. The v2 object model landed as **`arena.py`**
rather than as a rewrite of `objects.py` — v1 still imports the latter, and
rewriting it in place would have left v1's tests red for three phases instead of
deleting them cleanly in one commit. The name also matches the arena/view
distinction in §3. Concretely:

- v1 stays on disk, untouched and unreferenced, until **phase 6** (amended from
  phase 3 — see the build log), then it is deleted in one commit.
  Its history remains in git.
- **v1's acceptance thresholds are ported, its implementation is not.** The
  low-load "mean delay around one slot" check and the determinism check become v2
  tests (`[D14]`); the knee checks were already dropped in v1.
- `docs/history/v1-model.md` becomes the historical record of a superseded model, and now
  says so at the top rather than quietly describing code on its way out.

> **Naming note for Part I.** The event catalogue, module map and message list
> above are the *original design vocabulary*, and several names never reached the
> code or have since changed: `TX_ARRIVE`/`EGRESS_FREE`/`LINK_FREE`/`CPU_FREE` are
> `birth`, `deliver`, `tick`, `slot`, `expire` and the engine's own
> `service_done`; `objects.py` was folded into `arena.py` and `netsim.py` is the
> wiring rather than a CLI; and the wire carries objects, announces and wants as
> well as wrappers and blocks. [architecture.md](../architecture.md) is
> authoritative for what exists. Part I stays normative for *intent*; where it
> names something that no longer exists, the name is the stale part, not the
> decision.
>
> One item in Part I was normative and long unimplemented: the rule that a newer
> wrapper from the same peer supersedes the older one. The code superseded a
> node's own selection inputs, not retained aggregates by peer provenance. It is
> now implemented — as a **strategy family rather than the fixed behaviour Part I
> wrote**, because the rule as written is not obviously safe: a prover drops
> dependencies it knows are settled, so a newer claim can be *narrower* than the
> older one it replaces, and superseding unconditionally can lose evidence.
> `supersede_policy` selects `keep_all` (the old behaviour, and the default),
> `newest_per_peer` (Part I as written), or `newest_if_covered` (only when every
> still-live transaction the old claim asserts is also asserted by the new one).
> Which one the design should recommend is a measurement question, still open.

# Part II — Rationale, alternatives, open questions

Settled: `[D2]` egress placement, `[D8]` fold plans, `[D15]` coverage, `[D16]`
chain facts, plus v1's retirement. Deliberately open: `[D7]` overrun policy.
Needs an answer before phase 4: `[D17]` tx schema.

## [D1] Overlay construction

**Do not** generate by configuration-model-with-restart: at `d = 16` the retry
rate on self-loops and parallel edges gets impractical, and connectivity is never
guaranteed anyway — especially at low degree.

Instead: build a simple regular graph (a circulant), then randomise with
**degree-preserving double-edge swaps** — cheap, always simple, and mixes to a
near-uniform random regular graph. Declare the supported domain explicitly
(`N·d` even, `d < N`, and defined behaviour for `d ∈ {0,1,2}`), and state whether
connectivity is asserted or merely measured. Recommendation: assert it, and
resample the swap sequence on failure.

Degree is the knob the whole study turns on — fan-out cost against propagation
depth — so the sweep harness treats it as first-class alongside `N`.

## [D2] Where bandwidth is enforced — **settled: per-node egress**

| | per-link capacity | per-node egress |
| --- | --- | --- |
| models | an independent pipe per peer | one NIC, `d` peers competing |
| fan-out cost | hidden — `d` peers in parallel, free | explicit — `d ×` bytes through one pipe |

Per-link-only would delete the dominant cost of this system exactly where the
EIP's bandwidth claim needs testing. The optional per-link cap remains available
but is **not** free: it is a tandem resource, needing its own server, its own
completion event, and a declared order relative to egress.

**Ingress is measured, not metered.** Recorded: ingress bytes, message count, and
duplicate arrivals. **Not** recorded: "ingress queue depth" — the first draft
promised that metric, but a queue draining at infinite rate has no meaningful
depth; the number would be an event-ordering artefact. The real receive-side
backlog is the **verification queue** on the CPU server, which is already
instrumented. Adding a real ingress capacity later is one more server on a path
that already has a queue.

**Fan-out fairness.** One FIFO egress plus `[D9]`'s sorted peer iteration would
give low-id peers a permanent head start on every tick — determinism and fairness
pulling against each other. Fix: rotate the starting peer per tick (deterministic
rotation, so reproducibility survives), and report each peer's first-position
share as a phase-2 check.

## [D3] Message sizes

From the EIP, in config rather than code:

| component | size | source |
| --- | --- | --- |
| dependency triple | 96 B | `eip-8288.md:57` |
| leanSPHINCS witness | ~2–3 kB | Motivation |
| leanSTARK witness | 128–512 kB | Motivation |
| recursive STARK | ~256 kB | `:394` |
| tx body | `[D17]` | — |
| already-sent body | 32 B hash | `:223` |

The mode-0-versus-mode-1 crossover ("naive concatenation wins up to ~100
signatures", `:244`) then emerges rather than being asserted.

## [D4] Store-and-forward versus cut-through

Store-and-forward: a real client must verify before relaying anyway, and with
256 kB wrappers over multiple hops this dominates propagation. Cut-through stays a
possible per-link flag; not now.

## [D5] Duplicate suppression and body tracking

- Per-node **ever-seen** ids: cheap, and the natural counter for duplicate-arrival
  waste — a real bandwidth metric v1 cannot express.
- Do-not-relay-what-you-have, which is what makes flooding terminate.
- **"Sent" is not "has".** A per-peer bit set when a message is *enqueued* lets a
  later hash-only wrapper overtake or outlive the body that justified it. Set the
  bit on **delivery**, and state the reliability assumption explicitly (ordered,
  lossless FIFO per link, until drops exist).
- **Jitter granularity matters here.** Per-message jitter can reorder two messages
  to the same peer, which breaks hash substitution; per-edge jitter cannot.
  Recommendation: per-edge jitter by default (fixed at overlay construction),
  per-message available as a strategy, and hash substitution only sound under
  ordered delivery.

## [D6] Compute cost model

Keyed by type, injected at construction, overridable from CLI or JSON:

```text
costs = {"verify_recv": ..., "prove_recursive": f(n_deps), "build_block": ...}
```

**There is no verification logic anywhere in the model.** Objects are correct by
construction, so nothing is checked — `verify_recv` is a *duration*, not a
predicate. This collapses what an earlier draft split into `verify_wrapper`,
`verify_sig`, `verify_stark` and `verify_recursive`: those distinctions only matter
if you simulate the internals of verification, and we do not. One cost per received
wrapper is the whole model, and setting it to zero is a legitimate configuration.

It is still not *free*, and that is the point of charging it: store-and-forward
means a node cannot relay until it has finished receiving and processing, so
`verify_recv` sits directly on the propagation path and competes with proving for
the one CPU server. A node taking eight wrappers per tick pays it eight times.

- Values are a constant **or** a function of size. Proving over 500 deps is not
  proving over 2, and the EIP's prioritisation section presumes a proving budget.
  `verify_recv` may likewise scale with dep count; flat is the default.
- Jitter reuses v1's convention: `cost + U(0, frac·cost)`.
- **One CPU server per node** serialises everything: receive processing, proving,
  and block building.
- If invalid objects are ever modelled, `verify_recv` gains an *outcome*
  (accept/reject) carried as a flag on the message — still no cryptography, and
  still one cost.
- Per-node capability multipliers (a slow node in a fast network) are a later
  addition with room left for them.

## [D7] Queue discipline, bounds, drops — **left open on purpose**

FIFO by default, discipline pluggable.

- **Bounded queues and drops.** v1 grows unbounded backlog — 32 454 pending txs in
  a measured overloaded run. With real queues the honest overload response is
  *dropping*: optional caps, tail-drop first, per-node drop counters. Default
  unbounded so early phases stay simple.
- **Proving-budget overrun.** The EIP's priority list exists because a node may not
  have time to aggregate everything it saw (`:258`). Either unstarted work is
  *abandoned* and re-decided against the new view, or it is served FIFO on a stale
  selection.

**This stays a design option to explore, not a question to settle now**
(`on_tick_overrun = abandon | fifo`, with room for "abandon but keep the
removals"). Two consequences, though — and the first draft was wrong to say
nothing depended on it:

1. `Server` needs `cancel_waiting()` and generation-stamped jobs **in phase 0**.
2. Selection and proving must stay separable, so unstarted work is a re-decidable
   queue entry rather than a closure already committed to its inputs.

## [D8] Fold plans — **settled: n-ary objects, plan-shaped folds**

`AggregatedSTARK` generalises from `children = (a, b)` to a tuple of any arity;
the EIP's circuit is n-ary (`:143-186`) and a per-tick aggregate is naturally one
wide fold.

But arity alone is not the option worth comparing. `binary`, `wide` and
"cap fan-in at 8" are different **proof plans**, and a plan must declare:

- the tree shape over the selected inputs;
- whether sibling proofs run sequentially on the one CPU server or are separate
  queued jobs;
- total cost, and **when the root's output becomes available** — the only thing
  the network sees.

Hence `AggregationPlan` in §8 rather than "one `prove_recursive`". Then "is 2-to-1
folding actually worse than one wide fold, and by how much?" is a run rather than
an argument.

Two details the first draft skipped: coverage is fixed from the *selected* inputs
(not rebuilt from global inclusion at completion), and unary/empty ticks need
defined behaviour — v1's `_add_pool` classifies `total == 1` as a singleton
(`sim.py:154`), so a one-child aggregate would have been misfiled.

## [D9] Determinism

One RNG stream per concern, extended: `topology`, `link_jitter`, `compute_jitter`,
`entry_node`, plus arrivals. Plus:

- **Per-link RNG** seeded from `(seed, u, v)`, so adding a node does not reshuffle
  every other link.
- **Deterministic iteration** — sorted scans, stable `(t, seq)` ties — *except*
  where it manufactures unfairness, per `[D2]`'s peer rotation. Determinism must
  not be confused with neutrality.
- Float sums now depend on service order, so metric accumulation order is part of
  the contract.

## [D10] Calendar implementation

A timing wheel — a `deque` of fixed-width buckets plus an overflow heap — gives
`O(1)` insertion for near events, which is nearly all of them. Its one subtlety:
buckets are granular, so each must be sorted by `(t, seq)` on entry to preserve
exact ordering.

With servers owning their queues the live event count is roughly (busy servers +
timers + messages in flight), so the heap's `log n` is not obviously the
bottleneck. **Heap first, behind an interface; wheel when a profile asks.** The
wheel wins as `N × degree` grows, and guessing which side of the crossover we sit
on is not necessary.

## [D11] Metrics

- **propagation**: time-to-`k`%-coverage per object, hop counts, view divergence.
- **bandwidth**: egress bytes per node per interval, duplicate-arrival waste,
  ingress bytes, the mode 0/1 crossover, body-versus-hash share.
- **resources**: egress and CPU utilisation (busy-time integrated), waiting depth,
  proving backlog, drops, **evictions** (§3 — the model admitting it lost
  information).
- **the proposer's real view**: v1's `builder_folds_saved` assumes the builder sees
  everything `l` ago. With gossip it sees a partial, stale view, which is the
  number that actually bears on the EIP's benefit claim.
- **placement fairness** (`[D18]`): correlation between a tx's inclusion delay and
  the hop distance from its entry node to the proposer, plus the per-block content
  variance that rotation buys at the cost of consistency. These are the metrics that
  distinguish a fixed from a rotating proposer; nothing per-slot does.
- **variant identity** as columns: two runs differing only in `fold_plan` or
  `on_tick_overrun` must be distinguishable in a sweep without consulting the
  invocation.

Keep v1's tx-level definitions (delay mean/p95, inclusion counts) unchanged so the
published v1 numbers stay interpretable after v1's code is gone.

## [D12] Trace and viewer — **superseded by the viewer plan**

> The two-stage proposal below is the original one and is kept for the record.
> What happened instead: stage one shipped in phase 6 and was then **deleted**,
> not extended, and the replacement was specified in a separate viewer plan
> after four review rounds. That plan's `[V1]` overrules
> the network graph proposed here — a force-directed picture of a random regular
> overlay is a hairball whose clusters are layout artefacts — in favour of
> coverage by hop distance. Link load and queue depth did arrive, as panels D
> and C.

Trace volume grows roughly `N × degree`; a 72 MB v1 trace becomes gigabytes, so
sampling (every `k`-th message, or a node subset) is designed in, not bolted on.

The viewer's core assumption — one global pool — breaks. Cheapest path first:
trace one selectable **focus node** and reuse today's viewer nearly unchanged
(phases 1–4), then add a network panel (graph, link load, queue depths) once those
metrics exist. A topology-first rebuild is not on the critical path.

## [D13] Scale budget

Messages per tick ≈ `N × degree`; ticks per run = `slots × slot_length /
AGGREGATION_INTERVAL`; ~3 events per message plus one `DELIVER` in flight.

| N | degree | slots | messages | events | rough wall-clock |
| --- | --- | --- | --- | --- | --- |
| 100 | 8 | 100 | 320 k | ~1 M | seconds |
| 500 | 8 | 100 | 1.6 M | ~5 M | ~1 minute |
| 1000 | 16 | 100 | 6.4 M | ~20 M | several minutes |
| 10 000 | 8 | 100 | 32 M | ~100 M | tens of minutes to hours |

**Target: `N` up to 10 000, with runs above 1 000 permitted to be slow.** That
settles the escape-hatch question — **distant-node coarsening is not needed**, so
`Node` stays a single implementation and no stub abstraction enters the design.
Runs at 10 000 are a deliberate, occasional cost, not the working loop; day-to-day
work sits at 100–1 000.

But accepting slow runs does **not** buy the memory, and at 10 000 that becomes the
binding constraint rather than time:

```text
per-node view state ≈ N × V × ~80 B     (V = live objects per node view)
  10 000 × 200   ≈ 160 MB
  10 000 × 1 000 ≈ 800 MB
  10 000 × 5 000 ≈ 4 GB      <- does not run
```

Three consequences, all of which follow from the target rather than from taste:

1. **`[D7]`'s bounded candidate set stops being optional.** Bounding retention is
   what keeps `V` in the low hundreds, so `[D18]`'s measure-then-bound plan must
   finish before the first 10 000-node run, not after.
2. **`[D12]`'s trace sampling becomes mandatory**, not a nicety: an unsampled trace
   at `N = 10 000` is hundreds of gigabytes.
3. **Per-event allocation matters.** `__slots__` throughout, no per-event dicts, and
   no per-message object that outlives its delivery.

Confidence still comes from multiple seeds at moderate `N` rather than from single
large runs; the 10 000-node configuration exists to check that conclusions drawn at
1 000 do not invert at scale.

## [D14] Testing strategy

- **Engine**: a hand-computed queue fixture is the *primary* test — exact arrival
  by arrival. M/D/1 convergence is secondary, and only meaningful with declared
  parameters, which are now fixed in `tests/test_engine.py::TestMD1Convergence`:
  deterministic service `D = 1.0`, offered loads `rho ∈ {0.5, 0.8}`, 200 000 jobs
  per run, first 20 000 discarded as warm-up, 5% relative tolerance, fixed seed
  per load. Theory is the Pollaczek-Khinchine result at zero service variance,
  `Wq = rho·D / (2(1-rho))` — half the M/M/1 wait, since deterministic service
  contributes no variability. Observed: 1.12% off at `rho = 0.5`, 0.21% at
  `rho = 0.8`.
- **Network**: line-graph delivery equals summed hop latencies; complete graph with
  infinite egress reduces to uniform `l`.
- **Representation equivalence**: a small run under two coverage representations
  must produce identical metrics — the check that `[D15]` really is invisible.
- **Ported v1 thresholds** (phase 3): low-load mean delay, and determinism.
- **Invariants**, replacing v1's `paranoid` mode: tx conservation holds globally;
  every view is a subset of the arena; no view holds an unborn object; a server is
  busy iff `current_job is not None`; `waiting` and `current_job` are disjoint.
- **Overlay properties**: degree, connectivity, and swap-mixing across the declared
  domain.

No test compares against v1's implementation — there is no oracle to compare to,
by decision. Criteria are analytic or predeclared instead.

## [D15] Coverage representation — **settled**

The object count, not the object size, is the problem: `N × slots × slot_length /
AGGREGATION_INTERVAL` aggregates ≈ 80 000 at `N=200`, 100 slots, 1 s ticks. v1's
list-of-tx-references per object would be tens of millions of pointers.

Settled design is in §7. What the first draft got wrong and this fixes:

- **"Children plus a small delta" was undefined** for overlapping n-ary inputs —
  it never said what `delta` is relative to, nor how duplicates are detected
  without materialising the union first. Replaced by: immutable child tuples for
  cold objects, one memoised global mask for hot ones.
- **Two caches were conflated.** Coverage is immutable, so its mask caches once and
  never invalidates. Only the *useful count* is per node and per discard
  generation. The first draft's single "generation counter" muddled them.
- **Window rebasing is not a local optimisation** — it re-coordinates every cached
  mask and every node's discard mask simultaneously. Deferred until measured.
- Hot caches need a **bound and an eviction policy**, both measured.

This is an implementation detail, not a design option: no experiment may observe
which representation is in use, hence the equivalence test in `[D14]`.

## [D16] Chain facts and block propagation — **settled: a strategy**

The first draft asserted nodes learn of inclusion with delay but gave blocks no
message type and no event path — while instantaneous global notification would
have contradicted node-relative pruning outright.

Resolved by making it explicit and switchable, per the design principle:

- `chain_notify = instant` — `ChainState` advances at `SLOT` and every node's
  `known_discarded` updates at once. Simple, and correct for phases 1–4.
- `chain_notify = overlay` — the block is a typed `Message` propagated through the
  same overlay and subject to the same egress and verification costs, so nodes
  prune late and waste proving effort on already-included deps. Phase 5.

**Settled: `instant` is the working abstraction through phase 4** — but it is a
*known* simplification with a standing risk, not a solved problem. Under `instant`
every node's `known_discarded` is identical at every moment, so the per-node discard
machinery is exercised without ever diverging: one global generation, and the
`[D15]` useful-count cache keyed on `(node, generation)` never sees the case it
exists for. Left unwatched, that is discovered in phase 5 as a re-keying job.

**Keeping it in sight**, concretely — not as a note:

- the `chain_notify` interface exists from phase 1, so `overlay` is never a
  retrofit;
- **every phase's invariant suite includes at least one `overlay`-mode run**, so
  divergent per-node discard generations are exercised from phase 1 even while
  `instant` is the default for measurements;
- any metric whose value is structurally zero under `instant` — stale-aggregation
  waste is the main one — is **reported as `n/a` rather than `0`**, so no run can
  read as evidence that the effect is absent when the model simply cannot express
  it.

Both details are pinned, by B5 and B4 respectively: the block fact is created at
the `SLOT` event rather than at completion of the proposer's `build_block`
computation, and block propagation shares the egress server with wrappers because
it is the same NIC.

## [D17] Transaction schema — **needed before phase 4**

**Settled: two new fields, and nothing else.**

`PrivacyTx` and `PQSig` carry an id and a birth time. Phase 4 adds exactly:

| field | why | default |
| --- | --- | --- |
| `fee` | the EIP's 4th priority tier is otherwise unimplementable | all equal, so the tier degenerates to arrival order until fee spread is switched on |
| `expiry` | otherwise the 2nd tier ("removals") is vacuous — nothing can ever become unincludable except by inclusion | none (never expires) |

Deliberately **not** added:

- **dependency count and scheme mix** — already implied by the kind. A `PQSig` is
  one leanSPHINCS dep, a `PrivacyTx` is one leanSTARK dep, exactly as v1 has it.
  Multi-dependency transactions are a later variation, not a phase-4 need.
- **body bytes per tx** — a constant per kind in the size model (`[D3]`), not a
  per-tx field. Per-tx body variance buys nothing until it is the thing being
  studied.
- **sender and nonce.** Same-sender invalidation is the EIP's motivating removal
  trigger, but modelling it needs an account population and nonce tracking. An
  `expiry` timestamp exercises the same code path — a tx becoming unincludable
  without being included — at a fraction of the machinery. Add senders only if
  correlated removals turn out to matter.

So the schema stays two scalars, and both default to inert. That keeps every
phase-4 mechanism exercisable while adding no state that is not being measured.

## [D19] Relay policy: what the flood costs

Phase 1 measured the cost of the simplest possible relay rule — forward
everything new to every peer except the sender — and it is exact:

```text
messages = d + (N-1)(d-1)      useful deliveries = N-1
duplicate share = 1 - (N-1)/messages  ->  1 - 1/(d-1)  as N grows
```

50% at degree 3, 67% at degree 4, **86% at degree 8**. Confirmed against the
simulator to nine decimal places, so it is a property of the rule rather than a
measurement artefact.

This matters because phase 2 exists to measure bandwidth, and under naive
flooding **the wrapper size is not what dominates the byte count — the flood
multiplier is.** Reporting "the EIP costs X bytes per node per interval" from a
naive-flood model would be reporting on flooding, not on the EIP. Ethereum's
actual gossip does not work this way: gossipsub maintains a mesh and announces
message ids (IHAVE/IWANT) so that a body crosses each link roughly once.

So relay policy becomes a strategy, and which one is the default matters:

| | `flood` | `announce_fetch` |
| --- | --- | --- |
| what crosses a link | the whole object, `d-1` times over | an id first, the body only if wanted |
| bytes per object | `~N(d-1) × size` | `~N × size + N(d-1) × id_size` |
| latency | one hop per relay | one extra round trip on the fetch |
| models | a worst case, and a useful bound | roughly what a real client does |

**Settled: both, with `flood` the default.** Measured on 200 nodes at degree 8
with 10 Mbit/s uplinks, fetching cuts wire bytes by 7.5x for 3.2 kB signatures
and 1.7x for 256 kB proofs (it is 4x on *offered* bytes, which is the figure not
to quote) — but costs three one-way trips per hop instead of one, so
it reaches 81% of the network within a slot for signatures where flooding reaches
95%. For 256 kB proofs it wins on latency *as well*, because flooding saturates
the uplinks and self-inflicted queueing outweighs the round trips. So the
crossover is real and sits between those sizes; neither policy is a safe default
to assume, and the gap between them is the part of the overhead that belongs to
the gossip layer rather than to EIP-8288.

Still missing, and worth naming: the realistic middle. Gossipsub pushes bodies on
a mesh of a few peers and announces ids to the rest, which gets most of the byte
saving without paying a round trip on the critical path. That is a third policy,
not a tuning of these two.

Note the interaction with `[D5]`: announce-then-fetch changes what "the peer has
the body" means, because a peer may hold an id without the body. The EIP's
hash-substitution rule and this policy are solving the same problem at two
different layers, and the model must not double-count the saving.

## [D18] Proposer placement, connectivity and rotation

`N` counts every node and the proposer is one of them, with an ordinary node's
degree and capacity. That is the **decentralised building** baseline: whoever
builds sees what an ordinary participant sees. The alternative — a distinguished,
well-connected builder — is the centralised/PBS-shaped case, and it is a *result to
measure*, not a default to assume.

This matters more than it looks, because `builder_folds_saved` and inclusion delay
are direct functions of how complete the proposer's view is. Wiring in a
well-connected builder would make the EIP look good **by construction**. Hence:
proposer degree and capacity are explicit configuration whose defaults equal a
normal node's.

## [D20] The CPU model: one server, and the discipline is a policy

One CPU per node, on which verification, recursive proving and block building
compete. The justification: each of these jobs is assumed **perfectly
parallelisable by itself**, and under that assumption sequencing jobs at full
machine speed dominates sharing the machine between them — every job finishes
no later than under any fair-share schedule, and all but the last finish
earlier. Throughput is identical either way (work conservation), so the single
server with `duration = work / whole machine` prices utilisation and backlog
exactly. The tempting alternative — separate servers per job class — would
assume infinite parallel slack and hide the contention entirely, which is the
opposite of what a feasibility model owes its reader.

The argument holds, but it smuggles in two extra assumptions, and both are
known **leaks with a sign**:

- **Verification is priced pessimistically.** Perfect parallelism is credible
  for STARK proving (NTTs, Merkle layers) and wrong for a *single*
  verification, which is a small, largely sequential job that cannot use the
  whole machine. A real 8-core node verifies eight incoming wrappers
  concurrently; the model verifies them one at a time at whole-machine cost.
  Harmless while nothing published is CPU-bound; it stops being harmless in
  any proving-cost sensitivity sweep, where CPU contention is the subject.
- **Block timing is priced generously, and "sequencing is delay-optimal" is
  only true with the right discipline.** The system's delay objective is
  weighted — a wrapper verification delayed half a second before your slot can
  cost block coverage; a fold delayed the same costs almost nothing — and
  non-preemptive FIFO gives exactly the wrong answer to that: head-of-line
  blocking behind a long fold. The model patches around this asymmetrically:
  `proving_budget` bounds how long a fold may occupy the server, and the block
  *fact* is created at the slot boundary regardless of CPU state, with
  `BUILD_BLOCK` charged as occupancy afterwards. So a block can never be
  *late*, only *ill-informed* — CPU contention expresses itself solely as lost
  coverage, never as missed slots.

Resolved per the design principle: the discipline is a named policy rather
than an implicit assumption. `cpu_discipline = fifo` (the original behaviour,
and the default) | `priority` (verify > build > prove, non-preemptive —
roughly what any real implementation would do). Whether the discipline is
load-bearing is a question for the proving-cost sensitivity sweep run under
both; if they agree, FIFO's simplicity is vindicated, and if they diverge,
the artefact was caught before it became a conclusion.

A first probe (40 nodes, rotating proposer, proving swept 20 → 900 ms against
a 1 s tick) found the two disciplines **bit-identical**: at that operating
point a CPU queue almost never holds two jobs of different kinds at once, so
the discipline never gets to choose. A null result worth keeping — it says
the leak is real but currently dry, and names the condition to watch for
(deep mixed CPU queues, e.g. verification storms during long folds).

One boundary named deliberately (review 10, finding 8): `priority` is
**service-order only**. Admission to a bounded queue stays tail-drop, so a
full queue refuses an arriving verification even while a lower-priority fold
waits inside it — at overload, exactly where discipline matters most, the
policy yields to the buffer. Priority push-out (evict the worst waiting job
for a better arrival) would need its own cancellation accounting and an
owner-notification path the model does not have; if the sensitivity sweep
ever shows the discipline is load-bearing, that is the follow-up to build,
not a tweak to sneak in.

There are **two independent effects** here, and they must not be conflated:

**1. Degree — how much the proposer sees.** A higher-degree proposer has a fuller,
fresher view, so it needs fewer of its own folds and includes more per block. At
the limit it approaches v1's near-omniscient builder, which is precisely the
assumption v1 baked in and this model exists to remove. Sweep it.

**2. Identity — whether it is the same node every slot.** Independent of degree,
and the more interesting axis:

| | `fixed` | `rotating` |
| --- | --- | --- |
| view quality per slot | consistent | varies with whoever is proposing |
| block-to-block content | stable | higher variance |
| bias under partial diffusion | **systematic** — txs that diffuse slowly toward *that* node are disadvantaged every single slot | blind spots move, so disadvantage averages out across slots |
| resembles | a single dominant builder | rotating validator-proposers |

The fixed case has a censorship-adjacent failure mode that no single-slot metric
reveals: with partial diffusion — which is the normal state, and is sharpened by
the `entry_subset` entry policy — a transaction entering the overlay far from the
proposer is late *every* slot, not occasionally. Rotation trades that persistent
bias for slot-to-slot inconsistency: fairer in aggregate, noisier per block.

That yields a falsifiable prediction worth testing directly (phase 5b): **under
`fixed`, per-tx inclusion delay correlates with hop distance from entry node to
proposer; under `rotating`, that correlation collapses toward zero while per-block
content variance rises.** If the correlation does *not* collapse, either the
overlay mixes too well for distance to matter at this scale, or diffusion is
faster than the slot and the whole effect is below the resolution of the model —
both of which are findings.

Entry policy is the other half of the same story: `uniform` by default,
`entry_subset(k)` available, because concentrating entry points is what makes
diffusion partial enough for proposer placement to matter at all. The pairing worth
measuring is `entry_subset` × `fixed`, the pessimistic corner.

Open, deliberately: whether a proposer should *also* be sampled by stake or degree
rather than uniformly, and whether FOCIL changes the picture — FOCIL exists partly
to defeat exactly this bias, so it belongs in this discussion when it arrives.

## Answered questions

Kept on the record; reasoning stays in the referenced section. Marked ⟲ where a
later review reversed an earlier answer.

1. **[D2] Per-node egress capacity, per-link, or the hybrid?**
   → **Per-node egress.** Ingress measured, not metered — and ⟲ *not* via an
   "ingress queue depth" metric, which the review correctly called an artefact.
2. **[D8] Generalise `AggregatedSTARK` to n-ary, or keep binary and chain?**
   → **Generalise**, and ⟲ compare *fold plans* rather than arities, since
   "cap fan-in at 8" is a tree shape and a cost profile, not a constructor.
3. **[D15] Coverage as structural sharing plus a bitmask cache?**
   → **Yes**, but ⟲ with the delta scheme dropped as undefined, the two caches
   separated, and rebasing deferred.
4. **[D7] What happens when a node's proving budget is exhausted?**
   → **Deliberately not settled**; both policies implemented. ⟲ But it does
   constrain phase 0: `Server` needs cancellation.
5. **Does the builder become a node with a role?**
   → **It becomes an ordinary node**, ⟲ refined: not a static role and not an
   extra entity. `N` counts every node, the proposer is one of them chosen by
   `ProposerSchedule`, and it runs full gossip duty — so ⟲ *it does* emit wrappers,
   contrary to my earlier recommendation that it should not. Rationale in `[D18]`.
6. **Is v1 retained as a sibling entry point, or replaced?**
   → ⟲ **Replaced.** The first answer was "retained", which the review showed to
   be unbuildable — v1's `Builder` reads the very fields v2 removes
   (`actors.py:53`), so "kept as-is" was never available. Retiring v1 frees the
   object model completely and removes the bridge test; the cost is losing the
   calibrated reference, mitigated by porting its thresholds (§9, `[D14]`).
7. **Does phase 1 reproduce v1?**
   → ⟲ **No — that criterion was unachievable** and has been replaced with
   analytic ones (recorded in the build log). It failed for six independent
   reasons: phase 1 has no
   `r`; a v2 entry node admits instantly where v1 shows nothing before `t + l`;
   v1's `N` aggregators plus a *separate* non-aggregating builder is a different
   cardinality; v1 folds continuously while ticks arrive later; slot-versus-fold
   tie ordering is pinned by `test_sim.py:48`; and changing the object API
   invalidates the oracle anyway.

8. **What does `N` count, and how is the proposer placed?**
   → **`N` counts all nodes**; the proposer is one of them with a normal node's
   degree and capacity by default, because that is what decentralised building
   looks like. Starting configuration is a single proposer; `rotating` is
   implemented alongside it and the comparison is an experiment (`[D18]`, phase
   5b), since the two effects at play — the proposer's degree, and whether its
   identity is stable — are independent and the second carries a systematic-bias
   risk that no per-slot metric exposes. Entry policy: `uniform` default,
   `entry_subset(k)` available.

9. **Is `chain_notify=instant` acceptable through phase 4?**
   → **Yes**, the abstraction is fine to build on — but explicitly kept in sight
   rather than parked: `overlay` is interface-level from phase 1, every phase runs
   at least one `overlay` invariant check so per-node discard generations diverge
   somewhere from the start, and metrics that are structurally zero under `instant`
   report `n/a` instead of `0` (`[D16]`).

## Open items

> **Currency.** This list was written before phases 3–6. Group A is settled and
> group B was implemented as listed. Group C's axes now all exist in the registry
> except the two noted below, so they are experiments to *run* rather than
> mechanisms to build. For work still outstanding, the ranked list in
> [results.md](../results.md) is authoritative — it was written after the phase-6
> review and accounts for what that found. Where the two differ, prefer the
> handover; a stale inventory reading as complete is the failure mode this project
> hit repeatedly.
>
> Deltas since this was written: **B1's bound was never chosen** — retention is
> instrumented and still unbounded, and the `<1%` eviction criterion has not been
> applied. **C2 holds degree fixed**; the placement study varies proposer identity
> only. **C5 is not implemented** at all. And three items not on this list are now
> the top of the queue: unbounded `verified_proofs`/`seen_blocks`/per-peer body
> masks, `R6` request recovery, and overload behaviour with bounded queues.

Complete inventory as of phase 2, in four groups. Group A blocks work; group B is a standing
choice I have made that is cheap to overrule now and expensive later; group C is
open on purpose; group D is a gap in this plan rather than a choice.

### A. Blocks a phase

| # | question | blocks | lands on |
| --- | --- | --- | --- |
*A1–A4 are settled; kept here with their outcomes because each one shaped
something structural. A5 is new, raised by phase 1's measurements.*

| A5 | **Relay policy** → **both implemented, both reported, `flood` the default.** Neither dominates: fetching wins on bytes at realistic body sizes and on latency for large bodies, flooding wins on latency for small ones — and on bytes once a body is no bigger than its id, which the suite checks at 32 bytes. Settled by measurement rather than by choosing. | — | `netsim.py` |

| A1 | **`[D17]` tx schema** → **fee + expiry, both inert by default.** Nothing else: kind already implies the dependency, body bytes live in the size model, and expiry substitutes for sender/nonce. | phase 4 | `arena.py` |
| A2 | **Per-wrapper dependency limits** → **aggregates unbounded** (`mode0_only`). The limits are a mode-0 raw-witness bound; their restatement under mode 1 is an EIP defect, with two further internal contradictions found. | phase 4 | `network.py`, `node.py::select()` |
| A3 | **Target `N`** → **up to 10 000**, slow above 1 000 accepted. Coarsening dropped; memory, not time, becomes binding, which promotes retention bounding and trace sampling to requirements. | phase 1 design, phase 6 | `node.py`, `netsim.py::Config` |
| A4 | **Verification cost** → **one `verify_recv` duration, no logic.** Four cost entries collapse to one; zero is a legal value. | phase 3 | `costs.py` |

### B. My standing choice, overrule cheaply now

| # | choice made | alternative | why it is hard to change later |
| --- | --- | --- | --- |
| B1 | **Retention measured before bounded**: candidate set unbounded and instrumented in phase 1, bound chosen from data, `<1%` eviction as a phase-4 criterion | pick the bound up front | too tight a bound makes aggregation look *worse* than reality — a bias against the EIP, which is as bad as flattering it |
| B2 | **Per-edge jitter** fixed at overlay construction | per-message jitter | per-message jitter reorders messages to the same peer, which breaks hash substitution (`[D5]`) |
| B3 | **Connectivity asserted**, resampling the swap sequence on failure | measure and report it | a disconnected overlay silently invalidates every propagation metric |
| B4 | **Block propagation shares the egress server** with wrappers | its own channel | it is the same NIC; a separate channel would hide block-versus-wrapper contention |
| B5 | **Block fact created at `SLOT`**, not at `build_block` completion | at completion | they diverge once `build_block` costs anything, and the choice shifts every delay measurement by that cost |
| B6 | **Viewer: focus-node trace first**, network panel later | topology-first rebuild | keeps a working debug tool through the risky phases |

### C. Open on purpose — these are the experiments

| # | axis | strategies |
| --- | --- | --- |
| C1 | proving-budget overrun (`[D7]`) | `abandon` / `fifo` |
| C2 | proposer placement (`[D18]`) | `fixed` / `rotating` / `random`, crossed with degree |
| C3 | fold plan (`[D8]`) | `binary` / `wide` / capped fan-in |
| C4 | entry policy (`[D18]`) | `uniform` / `entry_subset(k)` |
| C5 | proposer sampling (`[D18]`) | uniform, or weighted by stake or degree — and how FOCIL changes the bias picture |

### D. Gaps in this plan

- **Drop policy has no default beyond "unbounded"** (`[D7]`). Fine until overload
  studies start; then tail-drop-versus-head-drop needs an answer.

### A2: the per-wrapper limits are a mode-0 concept, misapplied to mode 1

**Settled for the model: aggregate wrappers are unbounded in dependency count.**
The intuition that a node may add as many transactions as it likes is what the
mechanism requires; the constants as written do not permit it, and that is an EIP
defect rather than something for us to honour.

The evidence, because this is worth escalating rather than resolving privately.
The limits are stated as receive-side validity conditions under **both** modes
(`:231` items 2–3, `:239` items 3–4) and defined at `:271`/`:272`. Yet:

1. **The DoS rationale only works for mode 0.** `:274` justifies the limits by
   noting "a sender could alternatively submit a transaction containing the raw
   signatures and STARKs as calldata". Mode 0 carries **raw witnesses**, so
   capping dependency count does cap bytes: 16 signatures ≈ 48 kB, one STARK ≤
   512 kB. Mode 1 carries **one recursive proof of ~256 kB regardless of
   dependency count** (`:394`) — count is decoupled from volume, so a cap there
   bounds nothing DoS-relevant. It only forbids aggregation.
2. **The values are exactly the per-transaction limits** (`MAX_SIGS_PER_TX = 16`,
   `MAX_STARKS_PER_TX = 1`). A mode-0 wrapper capped at 16/1 is precisely *one
   maximally-loaded transaction's worth* of raw witnesses — coherent, given `:244`
   says mode 0 is "intended to be used primarily by clients broadcasting their
   transactions".
3. **`:244` contradicts the limit within mode 0 as well.** The same paragraph says
   a node may rebroadcast a naive mode-0 concatenation because that is more
   space-efficient "up to about a hundred leanSPHINCS signatures" — a wrapper
   holding ~100 signatures, which `MAX_LEANSIG_DEPS_PER_WRAPPER = 16` forbids
   outright.
4. **`:242` and `:256` require the opposite**: one wrapper per interval "containing
   all currently active transactions", with one STARK covering all their
   dependencies.

The coherent reading is therefore: **the limits bound raw witness data in
client-broadcast mode-0 wrappers, and their restatement under mode 1 (`:239`
items 3–4) is the error.** Under that reading everything fits — clients are bounded,
node re-aggregation is not, and the DoS argument holds where the raw bytes actually
are.

Three items to report upstream: the mode-1 restatement, the ~100-signature
contradiction inside `:244`, and the fact that the per-wrapper constants duplicate
the per-transaction ones.
