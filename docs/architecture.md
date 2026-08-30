# Architecture

The simulator separates a domain-agnostic event engine from pure aggregation
decisions and from the wiring that schedules network and compute work. This
keeps local node decisions from reaching into the global calendar or canonical
state.

## Repository map

| Path | Responsibility |
| --- | --- |
| `starksim/engine.py` | Ordered event calendar, single-server queues, cancellation, utilization accounting, and observation hooks. |
| `starksim/derive.py` | Seeded values derived from stable identities. |
| `starksim/arena.py` | Global pool-object truth, claims, coverage bitmasks, and the coverage cache. |
| `starksim/chain.py` | Canonical inclusion and expiry, node notification, blocks, and proposer schedules. |
| `starksim/network.py` | Overlay construction, link latency, wire-size rules, and typed payload construction. |
| `starksim/payload.py` | Immutable message payloads and byte decompositions. |
| `starksim/node.py` | `Node`, `NodeView`, body and proof beliefs, retention, supersession, eviction, and block selection. |
| `starksim/aggregation.py` | Pure selection and fold-plan construction. |
| `starksim/strategies.py` | Registries for swappable selection, fold-plan, supersede, and eviction callables. |
| `starksim/presets.py` | Named configurations — the EIP as written, v1, the levers that pay — as constructor keyword arguments. |
| `starksim/costs.py` | Configurable durations for verification, proving, and block construction. |
| `starksim/trace.py` | Versioned JSONL output, focus, sampling, topology, and follow metadata. |
| `starksim/netsim.py` | `NetworkSim`: domain wiring, event handlers, and the accounting those paths update. |
| `starksim/reporting.py` | Analytical queries, `variant()`, `metrics()`, and the CSV contract. |
| `starksim/knowledge.py` | Per-node knowledge classification for followed transactions. |
| `bench/` | Reproducible experiment entry points. |
| `tests/` | Unit and characterization tests grouped by concern. |
| `tools/` | Viewer fixture generation, reducer checks, render checks, and the paced live runner. |
| `serve.py` | Static server with byte-range support for a growing trace. |
| `viewer.html` | Self-contained trace replay and live-follow application. |

The simulator and benchmarks use only the Python standard library. Node and
Playwright are development dependencies for viewer verification, not runtime
dependencies of `starksim`.

## Layering rules

Three boundaries define the design:

1. `engine.py` is domain-agnostic. It knows time, events, queues, jobs, and
   observers, but has no mempool imports or event semantics.
2. `node.py` schedules nothing. A node decides what to retain, relay, select,
   supersede, or evict; `NetworkSim` turns those decisions into timed work.
3. `aggregation.py` is pure. A selection and a fold plan are immutable values,
   so waiting proof work can be cancelled and recomputed without undoing domain
   mutations.

These rules also establish dependency direction: the engine can be tested in
isolation, domain objects do not know about the calendar, and the wiring layer
is the only place where a protocol action becomes an event or server job.

Cross-cutting choices are recorded in short decision documents rather than
duplicated here. Start with [node egress bandwidth](decisions/0001-node-egress-bandwidth.md),
[global truth and node belief](decisions/0002-global-truth-and-node-belief.md),
[deterministic randomness](decisions/0003-deterministic-randomness.md),
[store-and-forward verification](decisions/0004-store-and-forward-verification.md),
[the one-CPU model](decisions/0005-one-cpu-per-node.md), and
[observation cost](decisions/0006-observation-cost.md).

## Where state lives

| State | Owner | Visibility |
| --- | --- | --- |
| Object identity, children, coverage, and frozen claim | `Arena` and `PoolObject` | Global fact used for representation and accounting; not automatically known to nodes. |
| Canonical inclusion, expiry, blocks, and revision | `ChainState` | Global chain fact; nodes reach it only through a notifier. |
| Held objects, bodies, verifying work, verified proofs, requests, and duplicate suppression | `NodeView` | Local to one node. |
| Per-peer body and proof beliefs | `Node` | Local sender belief, updated only at events that justify it. |
| Waiting and in-service work | `Server` | Owned by one egress, link, or CPU server. |
| Scheduled future actions | `Calendar` | Owned by the engine and ordered by time and insertion sequence. |
| Measurements and trace lifecycle | `NetworkSim`, `Tracer`, and `KnowledgeTracer` | Observation state; it must not alter the simulated trajectory. |

The arena is intentionally an oracle of facts, not an oracle available to a
protocol participant. `Node` methods receive the arena when they need to
evaluate a locally held identity, but they do not infer receipt, settlement, or
peer knowledge from arena contents.

## Life of one transaction

The following walkthrough names the event and method boundaries worth tracing
in the code.

1. A harness calls `NetworkSim.schedule_birth`. At `EV_BIRTH`, `_birth` creates
   a leaf in `Arena`, admits it at its entry node, records its body, and starts
   relay according to `relay_policy`.
2. `_send` submits a typed `WireMessage` to the sender's egress `Server`. The
   server serializes the bytes; an optional link server adds a second
   serialization stage, then overlay latency schedules `EV_DELIVER`.
3. `_on_deliver` records physical arrival and delivered bodies. It dispatches
   by payload type. Full objects, wrappers, and block notices queue a
   `VERIFY_RECV` job unless their configured cost is zero.
4. A verification callback admits usable evidence into the receiver's
   `NodeView`. Raw objects may be advertised onward; wrappers are retained but
   never relayed. Candidate bounds may evict other held objects after admission.
5. At `EV_TICK`, `_tick` prunes dead claims, calls the configured selection
   strategy, calls the configured fold planner, and queues `PROVE_RECURSIVE`
   jobs. `_proof_started` freezes claims and accounts for consumed inputs.
6. `_proof_done` creates internal aggregates. When the plan root finishes it
   becomes the node's current aggregate and `_emit_wrapper` builds a separate
   payload for each peer from that peer's body and proof beliefs.
7. At `EV_SLOT`, `_build_block` expires due leaves, asks `ProposerSchedule` for
   the proposer, and selects claims from that node's view. `ChainState.include`
   records attempted and newly included masks. Under overlay notification, a
   block payload then follows the same metered and verified propagation path.
8. Inclusion or expiry eventually makes a held claim useless. Nodes prune when
   their notifier exposes the change; trace `drop` records make that departure
   distinguishable from cumulative reach.

`Server` completion callbacks run before the next waiting job starts. Events
created during a callback receive fresh sequence numbers, preserving the
calendar's same-time ordering contract.

## Extension points

There are two kinds of configurable choice:

- Registered callables cover selection, fold planning, supersession, and
  eviction. They are resolved through `starksim.strategies`.
- String-validated modes cover relay, wrapper economy, inclusion, CPU
  discipline, proving-cost basis, chain notification, tick phase, overrun, and
  entry policy. `ProposerSchedule` separately validates proposer mode.

Every choice that changes a run belongs in `NetworkSim.variant`; every measured
quantity belongs in `NetworkSim.metrics`. Adding a metric also requires an
append-only change to `CSV_COLUMNS` if it belongs in CSV output. Reordering that
list breaks the output contract.

Payload extensions start in `payload.py`, acquire a size in `SizeModel`, and
need a delivery path in `NetworkSim`. Computation extensions start in
`costs.py` and need an explicit job kind and CPU scheduling rule. Trace field
changes must follow the schema-version contract in
[trace-format.md](trace-format.md).

See [adding-a-policy.md](adding-a-policy.md) for exact callable signatures and a
runnable example.

## Where to start reading

Read [model.md](model.md) first for the semantics. Then read `engine.py` to learn
the time and queue contracts, followed by `arena.py`, `node.py`, and
`aggregation.py` for the domain model. Read `netsim.py` last because it joins
the event, network, aggregation, block, and accounting paths. See
[ADR 0007](decisions/0007-netsim-stays-one-module.md) for the module-boundary
decision.

For a concrete execution path, keep the transaction walkthrough above beside
`NetworkSim._birth`, `_send`, `_on_deliver`, `_tick`, `_emit_wrapper`, and
`_build_block`. Use the focused tests for each concern before reading a bench;
the benches add measurement windows and reporting decisions on top of the same
core.
