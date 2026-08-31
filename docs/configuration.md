# Configuration

Everything a run can be told, with its default. `NetworkSim(overlay, **kwargs)`
is the whole surface. Some fields name extensible policy families; others
select fixed modes, numeric values, bounds, or injected objects.

For what each choice *means* mechanically, see [model.md](model.md). For which
ones have been shown to change a measured outcome, and by how much, see
[results.md](results.md) — numbers live there and are deliberately not repeated
here, so that this file cannot disagree with them.

`tests/test_configuration.py` verifies that every constructor parameter is
listed and that documented defaults and choices match the code. It parses the
backticked first and second table columns; descriptions are not checked.

## Three ways to configure a run

- **Directly**, for a test or a one-off script: `NetworkSim(overlay, ...)`, then
  `schedule_birth`, `schedule_ticks`, `schedule_slots`, `run`. There is no CLI
  for the simulator itself; `netsim.py` is wiring.
- **A bench**, for a measurement: modules under `bench/`, each with its own
  workload flags. `bench.sweep` is the general one — it varies any named axis and
  holds the others, and is the path a published number should come from.
- **`tools.live_run`**, for watching a run in the viewer as it happens.

[What the harnesses expose](#what-the-harnesses-expose) lists what each of
those offers.

For direct use, `NetworkSim` is the programmatic entry point:

```python
import random

from starksim.arena import KIND_PQ
from starksim.netsim import NetworkSim
from starksim.network import Overlay

overlay = Overlay.random_regular(20, 4, random.Random("example"),
                                 base_latency=100.0)
sim = NetworkSim(overlay, seed=1)
sim.schedule_birth(0, KIND_PQ)
sim.schedule_ticks(4)
sim.schedule_slots(1)
sim.run()
print(sim.metrics())
```

## The EIP as written

The defaults below represent this simulator's reading of EIP-8288.
`tests/test_eip_faithful.py` checks the documented values against constructor
defaults. Each row either cites the EIP snapshot or says that the EIP does not
specify the choice.

<!-- eip-faithful:start -->

| Field | Value | The EIP |
| --- | --- | --- |
| `aggregation_interval` | `1000.0` | `AGGREGATION_INTERVAL = 1000` (`:53`). |
| `aggregation_trigger` | `spread` | "Every `AGGREGATION_INTERVAL` milliseconds, they generate a new wrapper" (`:264`) — per node, with nothing synchronising them, so each node keeps its own clock. |
| `wrapper_mode` | `recursive` | "Each wrapper contains exactly one STARK in mode 1" (`:250`); mode 0 "is intended to be used primarily by clients" (`:252`), not by mempool nodes in steady state. |
| `fold_threshold` | `always` | "Per `AGGREGATION_INTERVAL`, a mempool node broadcasts exactly one wrapper containing all currently active transactions" (`:250`) — no condition on whether folding is worth it. |
| `fold_plan` | `wide` | "with exactly one STARK covering all their dependencies" (`:250`) — one fold over everything, not a tree. |
| `selection_strategy` | `eip_priority` | The four-tier order at `:266`–`:271`, for a node that cannot aggregate everything it saw. |
| `inclusion_policy` | `body_required` | "Build the execution payload including all included transactions" (`:322`) — the payload carries every one of them, so a proof alone cannot conjure a body. |
| `wrapper_economy` | `full` | The EIP specifies substituting a hash for a transaction already sent (`:264`), which this model always does; it says nothing about withholding *proof* bytes, so nothing is withheld. |
| `supersede_policy` | `keep_all` | Not specified. Retiring a peer's older wrapper is a modelling choice, and an unsafe one by default: a newer claim can be narrower than the one it replaces. |
| `relay_policy` | `flood` | Not specified. The EIP describes what a wrapper contains, not how raw objects reach a node. |
| `prove_cost_basis` | `deps` | Not specified — a cost model, not protocol. Prices a job by its output's dependency count. |
| `proposer_mode` | `fixed` | Not specified. A fixed proposer isolates position effects; rotation hides them by moving the blind spot. |
| `chain_notify` | `instant` | Not specified. Instant settlement knowledge is the optimistic bound; `overlay` is the realistic one and makes stale proving measurable. |
| `cpu_discipline` | `fifo` | Not specified. |
| `eviction_policy` | `usefulness` | Not specified, and inert unless `candidate_limit` is set. |

<!-- eip-faithful:end -->

Three EIP provisions are *not* defaults because this model does not implement
them: the per-wrapper dependency limits (`MAX_LEANSIG_DEPS_PER_WRAPPER = 16`,
`:279`), which are a mode-0 concept the EIP then restates for mode 1; FOCIL; and
anything about fees beyond a scalar. [model.md](model.md) lists the non-goals in
full.

## Named configurations

`starksim.presets` names three configurations as keyword-argument dicts, so
that "the EIP as written", "v1" and "the levers that pay" are objects a bench
or a test can hold to rather than descriptions:

| Preset | What it is | Held to |
| --- | --- | --- |
| `eip_faithful` | The table above, as a dict | The constructor's defaults, by `tests/test_presets.py` |
| `v1_equivalent` | Free bytes, instant settlement, `when_ready` folding, `random_pair` selection, `claim_only` inclusion, a dedicated builder | What `bench.v1_calibration` builds, which adds the complete graph and the constant fold cost |
| `high_throughput` | `announce_fetch`, `lean`, `crossover`, `cheapest` — the levers minus `capped8`, which caps inclusion | `bench.levers`' own lever table, minus that one, by `tests/test_presets.py`; the measurement is in [results.md](results.md) |

A preset names policy fields only; the overlay, the size model and the cost
table stay with the workload. `preset(name)` returns a copy to override:
`NetworkSim(overlay, **preset("v1_equivalent"), seed=3)`. `tools.live_run`
takes `--preset`; any flag given explicitly wins over it — the policy flags,
`--uplink-mbps` and `--egress-bytes` alike — and a flag left out takes the
preset's value or the runner's default.

## Topology and latency

Set on the overlay, not on `NetworkSim`:
`Overlay.random_regular(node_count, degree, rng, base_latency, jitter_frac=0.0,
latency_seed=0)`, or `Overlay.from_adjacency(adjacency, base_latency, ...)` for
an exact graph.

Degree is the parameter to watch: a wrapper emission queues one proof-sized copy
*per peer*, so the standing bandwidth cost is linear in it. Link jitter is fixed
per undirected edge rather than per message, and is derived by hashing
`(seed, u, v)` so that an edge's latency does not depend on the topology around
it.

## Identity and injection

| Field | Default | What it decides |
| --- | --- | --- |
| `seed` | `42` | Every derived stream: tick offsets, peer rotation, entry choice, cost jitter. Two runs with one seed are byte-identical. |
| `engine` | `None` | Supply an `Engine` to share a clock across models. `None` builds one. |
| `chain` | `None` | Supply a `ChainState`. `None` builds one. |
| `size_model` | `None` | Supply a `SizeModel` (see *Message sizes*). `None` builds the default. |
| `costs` | `None` | Supply a `CostTable` (see *Proving cost and CPU*). `None` builds a free one, which is a legal configuration. |
| `verify_cost` | `None` | Supply a `NativeVerifyCost(per_signature, per_stark, per_message=0)` to price receive verification by the evidence a delivery carries: a signature body pays `per_signature`; a privacy body, aggregate body, mode 1 wrapper, or block notice pays `per_stark`; a mode 0 wrapper pays for every witness it ships. `per_message` is overhead per verification performed, so duplicates pay nothing, and `jitter_frac` does not apply. `None` keeps the flat per-message `verify_recv` everywhere. |

## Bandwidth and queues

| Field | Default | What it decides |
| --- | --- | --- |
| `capacity` | `None` | Per-node egress in bytes per ms; `bytes_per_ms(mbps)` converts. `None` is an infinite uplink. This is the primary bandwidth constraint — fan-out shares one sender-side queue. |
| `link_capacity` | `None` | Optional second per-link stage. `None` leaves links latency-only. |
| `egress_queue_bytes` | `None` | Send-buffer bound per node; `None` is unbounded. Must exceed one emission round (`degree × recursive_proof`) or every node overflows whenever it aggregates, whatever the load. |
| `request_timeout` | `None` | Ms before an unanswered `want` is retried under `announce_fetch`. `None` never retries. |

## Relay

| Field | Default | Choices | What it decides |
| --- | --- | --- | --- |
| `relay_policy` | `flood` | `flood`, `announce_fetch`, `wrapper_only` | How raw objects reach nodes: pushed in full, announced then requested, or never — claims and bodies moving only inside periodic wrappers. |

## Aggregation cadence

One question with three answers: a clock shared by the network, a clock each,
or no clock.

| Field | Default | Choices | What it decides |
| --- | --- | --- | --- |
| `aggregation_trigger` | `spread` | `aligned`, `spread`, `when_ready` | What makes a node aggregate. `aligned` fires every node's clock together, which puts every uplink and CPU to work at once and idle between; `spread` gives each node a derived offset in the interval; `when_ready` has no clock at all — a node folds as soon as it is idle and holds two objects. Without a clock nothing schedules ticks and the emission rate becomes a consequence of load rather than a constant. |
| `aggregation_interval` | `1000.0` | | Ms between a node's ticks under a clock; the EIP's `AGGREGATION_INTERVAL`. With degree, this sets the standing bandwidth cost, since the recursive proof is paid per peer per emission. |
| `on_tick_overrun` | `fifo` | `fifo`, `abandon` | Under a clock: whether proof work still waiting from an earlier tick survives a new one, or is cancelled and re-decided. |
| `proving_budget` | `None` | | Ms of estimated proving a selection may claim before it stops taking inputs. **`None` means one `aggregation_interval`, not unbounded** — pass a large number for that. The budget may not refuse a node its first *fold*, so the first two inputs are taken whatever they are estimated to cost — a node that cannot fit one fold overruns rather than stalling, and the overrun is visible in the CPU queue. Past that the budget binds. Check the mean fold width before believing a result under a binding budget. |

A trigger and a phase would be two fields with one meaningless pairing to
refuse, so they are one. Choosing an overrun rule under `when_ready` *is*
refused rather than ignored, and `proving_budget` has no interval to default to
there, so it must be given explicitly.

## Aggregation policy

The four registry families. `strategies.names(family)` lists what is registered;
[adding-a-policy.md](adding-a-policy.md) is how to add one.

| Field | Default | Choices | What it decides |
| --- | --- | --- | --- |
| `selection_strategy` | `eip_priority` | `eip_priority`, `coverage_greedy`, `fee_density`, `deadline`, `random_pair` | Which held objects a node folds this tick, and in what priority. `random_pair` is a kind-, fee-, and budget-blind baseline that selects two objects uniformly. |
| `fold_plan` | `wide` | `wide`, `binary`, `capped8` | The proof tree: one fold over everything, a balanced pairwise tree, or a tree capped at fan-in eight. Under circuit pricing this decides throughput, not just job count. |
| `supersede_policy` | `keep_all` | `keep_all`, `newest_per_peer`, `newest_if_covered` | When a peer's newer wrapper retires its older one. Not free of risk: a newer claim can be *narrower*, so superseding unconditionally can lose evidence. |
| `eviction_policy` | `usefulness` | `usefulness`, `fee_value` | What a full candidate set gives up. Only binds when `candidate_limit` is set. |

## Wrapper encoding and economy

| Field | Default | Choices | What it decides |
| --- | --- | --- | --- |
| `wrapper_economy` | `full` | `full`, `skip_unchanged`, `proof_by_ref`, `lean` | Which redundant wrapper bytes are withheld from a peer known to hold them: nothing, the whole wrapper, the proof as a 32-byte hash, or both. |
| `wrapper_mode` | `recursive` | `recursive`, `cheapest` | `recursive` is always mode 1. `cheapest` emits the EIP's mode 0 — the dependencies' raw witnesses — when the node **holds** every claimed witness and they weigh less than a proof. A mode-0 delivery leaves the receiver witness-backed, never holding a recursive proof. |
| `fold_threshold` | `always` | `always`, `crossover` | `always` folds every interval. `crossover` declines while the claim's own witnesses weigh less than the proof that would replace them. Needs something other than wrappers to carry transactions meanwhile, so it stalls `wrapper_only`. |
| `fold_amortize` | `1.0` | float >= 1 | Scales the witnesses' side of the `crossover` comparison, standing in for a fold's benefits beyond the folder's own uplink: block-slot compression, downstream verification, proving once. `1.0` is the strictly local reading; large values approach `always`. Refused with any other threshold, where it is inert. |

## Proving cost and CPU

| Field | Default | Choices | What it decides |
| --- | --- | --- | --- |
| `prove_cost_basis` | `deps` | `deps`, `inputs`, `circuit` | The size a proving job is priced by: its output's dependency count, how many objects it folds, or what the circuit actually verifies. `circuit` requires a `CircuitCost`. Selection estimates in the same unit — the wiring stamps the basis onto the cost table it hands the strategy — so a budget cannot refuse a selection for a cost nothing will pay. |
| `cpu_discipline` | `fifo` | `fifo`, `priority` | Service order for waiting CPU jobs. Service order only — priority never reorders *admission*, so a full queue still refuses an arriving high-priority job. |
| `cpu_queue_jobs` | `None` | | Waiting jobs allowed per node CPU; `None` is unbounded. |

`costs=CostTable(...)` prices the work itself.

| `CostTable` field | Default | What it prices |
| --- | --- | --- |
| `verify_recv` | `0.0` | Verifying a received proof or object. |
| `prove_recursive` | `0.0` | One recursive proving job. A number, a callable of the size, or a `CircuitCost`. |
| `build_block` | `0.0` | A proposer assembling a block. |
| `jitter_frac` | `0.0` | Uniform jitter added to each duration, as a fraction. |
| `seed` | `0` | The jitter stream. |

Under `prove_cost_basis="circuit"`, `prove_recursive` must be a `CircuitCost`,
normally built as `CircuitCost.anchored_on_pairwise_fold(889)` — one measured
2-to-1 fold in ms, which is two recursive verifications. Calibration scope
belongs in [results.md](results.md).

| `CircuitCost` field | Default | What it sets |
| --- | --- | --- |
| `ms_per_hash` | required | The millisecond scale. The `anchored_on_pairwise_fold` constructor derives it. |
| `overhead_hashes` | `0.0` | Fixed cost per proof, independent of what it verifies. |
| `sphincs_hashes` | `468` | Hashes to verify one leanSPHINCS signature in-circuit. |
| `leanstark_hashes` | `10000` | Hashes to verify one leanSTARK witness. |
| `recursive_hashes` | `10000` | Hashes to verify one nested recursive proof — about 21 signatures. |
| `trace_rows_per_hash` | `3` | Trace rows per hash. The dominant unknown: it decides whether one proof holds hundreds of signatures or tens of thousands. |
| `trace_cap` | `16777216` | The `2**24` KoalaBear trace length. A job needing more is refused as impossible, before any of its plan is charged for. |

## Chain, blocks and the proposer

| Field | Default | Choices | What it decides |
| --- | --- | --- | --- |
| `slot_length` | `4000.0` | | Ms per slot. |
| `block_budget` | `5` | | Objects a block may include. What a block settles is this times the claims per selected object, and the second factor is the one that binds. |
| `proposer_mode` | `fixed` | `fixed`, `rotating`, `random`, `dedicated` | Who builds each slot, and whether that node is a mempool node at all. `dedicated` is a fixed proposer that never folds. Validated by `ProposerSchedule`, not by `NetworkSim`. |
| `proposer_id` | `0` | | The builder under `fixed`. |
| `pin_own_aggregate` | `True` | | Whether a bounded candidate set may evict the node's own aggregate. The EIP's first selection priority is a node's own aggregate from the previous round, which presumes it still holds it; exempting an object means the set really holds one more. Requires `candidate_limit`. |
| `chain_notify` | `instant` | `instant`, `overlay` | Whether nodes learn what a block settled at once, or by metered propagation from the proposer. `instant` makes the stale-work metrics structurally unanswerable, and they report `-1`. |
| `inclusion_policy` | `body_required` | `body_required`, `claim_only` | Whether the proposer must hold a transaction's body to include it. Both must refuse a claim wider than the proof asserts. |

## Workload and entry

| Field | Default | Choices | What it decides |
| --- | --- | --- | --- |
| `entry_subset` | `None` | | How many nodes may receive births. `None` is every node; a count concentrates entry, which is the gateway shape and what makes diffusion partial enough for proposer placement to matter. The subset is derived from the seed rather than the first *k* ids, so it does not sit next to whichever node is proposer zero. |

Transactions are scheduled, not generated:
`schedule_birth(t, kind, node_id=None, fee=0.0, lifetime=None)`, where `kind` is
`'q'` for a post-quantum signature or `'p'` for a privacy transaction carrying a
STARK. `fee` is a scalar with no market behind it, and `lifetime` is ms to
expiry. Then `schedule_ticks(count)`, `schedule_slots(count)`, `run(until=...)`.

## Message sizes

`size_model=SizeModel(...)`; `None` builds the default. Bytes.

| `SizeModel` field | Default | What it sizes |
| --- | --- | --- |
| `triple` | `96` | One `(scheme, data_hash, verification_key)` dependency triple. |
| `hash_ref` | `32` | A hash standing in for a body, or for a proof under `proof_by_ref`. |
| `sig_witness` | `2560` | One leanSPHINCS witness, as mode 0 carries it. |
| `stark_witness` | `262144` | One leanSTARK witness — the size of a whole recursive proof, which is why privacy claims never choose mode 0. |
| `recursive_proof` | `262144` | One recursive proof, whatever it covers. Paid per peer per emission: the wrapper constant. |
| `tx_body` | `512` | One transaction body. |
| `announce_overhead` | `0` | Per-announcement overhead beyond its hashes. |

## Retention bounds

All default to unbounded. A run that sets one must report eviction and
forgetting alongside its result because a tight bound can reduce measured
aggregation performance.

| Field | Default | What it bounds |
| --- | --- | --- |
| `seen_limit` | `None` | The per-node duplicate-suppression window. A forgetful window, so duplicate checks consult held state directly as well. |
| `candidate_limit` | `None` | Objects a node retains as fold candidates. What gets dropped is `eviction_policy`'s decision. |
| `coverage_cache_limit` | `None` | Entries in the arena's coverage cache. An implementation bound; no experiment should be able to observe it except through its own eviction counter. |

## Tracing

| Field | Default | What it decides |
| --- | --- | --- |
| `trace` | `None` | A writable stream. `None` disables tracing, and the instrument then costs one call per observation boundary. |
| `trace_focus` | `None` | Restrict per-node events to a set of node ids. |
| `trace_sample` | `1` | Keep one in N `snd` records — the only sampled event kind, and the largest one in a big trace. |
| `trace_counter_interval` | `1000.0` | Ms between per-node counter samples. One record per node per sample, so trace growth scales with `nodes / interval`. |
| `trace_follow` | `None` | An explicit list of transactions to trace in full. |
| `trace_follow_rate` | `0.0` | Or an independent probability per transaction. Yields `p·T` followed transactions, not a fixed count. |
| `trace_follow_seed` | `None` | The follow stream, when a rate is used. |

[trace-format.md](trace-format.md) is the schema; [viewer.md](viewer.md) covers
producing and watching one.

## What the harnesses expose

<!-- sweep-axes:start -->
**`bench.sweep`** varies one axis and holds the rest:
`--axis` accepts `relay_policy`, `chain_notify`, `proposer_mode`,
`selection_strategy`, `fold_plan`, `supersede_policy`, `eviction_policy`,
`wrapper_economy`, `inclusion_policy`, `cpu_discipline`, `prove_cost_basis`,
`wrapper_mode`, `fold_threshold`, `on_tick_overrun`,
`block_budget`, `entry_subset`, `aggregation_interval`,
`slot_length`, `proving_budget`, `aggregation_trigger`,
`fold_amortize`. `--set KEY=VALUE` holds
any of the same names at
a non-default value, `--prove-cost-circuit MS` selects circuit pricing, and the
axis names are validated so a typo cannot become a row describing a
configuration nobody ran.
<!-- sweep-axes:end -->

**The focused benches** — `propagation`, `economy`, `overload`, `placement`,
`triage`, `v1_calibration`, `levers`, `throughput`, `mainnet` — each fix a scenario and expose the workload knobs that scenario turns.
`--help` on any of them is authoritative.

**`tools.live_run`** exposes `--preset`, `--nodes`, `--degree`, `--uplink-mbps`, `--latency`,
`--tps`, `--lifetime`, `--egress-bytes`, `--candidate-limit`,
`--relay-policy`, `--wrapper-economy`, `--supersede-policy`, `--follow-rate`,
`--counter-interval`, `--speed`, `--duration`, `--seed`, `--out`.

## When a choice is a field, and when it is another value

Extend an enum when new behaviour is another value of an existing choice. Add a
field only for an independent dimension. A guard earns its place when the
invalid region comes from an implementation limit, not when it comes from
splitting one question across two fields.

A configuration option may remain when it represents a real external constraint
or compatibility target that the model does not represent, such as compatibility
with the retired v1 model, even if another value performs better inside the
model. Measurements can choose a default; they do not by themselves eliminate
supported options.

## Settings a run refuses

`NetworkSim` rejects explicitly selected settings that cannot affect the run:

| Choosing | Without | Because |
| --- | --- | --- |
| an `eviction_policy` | `candidate_limit` | it decides what a full candidate set drops, and no set is ever full |
| a `request_timeout` | `relay_policy="announce_fetch"` | it retries an unanswered want, and no other policy sends wants |
| an `on_tick_overrun` | a clocked `aggregation_trigger` | it decides what a new tick does to waiting work, and `when_ready` only folds an idle node |
| a `pin_own_aggregate` | `candidate_limit` | it protects one object from eviction, and nothing is ever evicted |
| a `trace_follow_rate` | no explicit `trace_follow` list | a list decides what is followed, and a rate is consulted only without one |
| an `entry_subset` | fewer nodes than it names | a subset the size of the network is every node, which `None` already means |
| an `aggregation_interval` | a clocked `aggregation_trigger` | it spaces a clock that `when_ready` does not have |

The check applies only to explicitly non-default values that cannot affect the
run. Passing a default explicitly is treated like omitting it; surprising but
effective configurations remain valid.

## What is not configurable

Deliberately, because results have to stay comparable and reconstructable:
`CSV_COLUMNS` order (columns are appended, never reordered), trace event
meanings without a `SCHEMA_VERSION` bump, event tie-breaking at equal
timestamps, seed derivation, and store-and-forward ordering. The layering is
likewise fixed: `engine.py` is domain-independent, `aggregation.py` is pure, and
`node.py` schedules nothing.
