# Simulation model

`starksim` is a discrete-event model of the mempool protocol proposed by
[EIP-8288](eip-8288.md). It represents transactions, recursive aggregates,
network transport, local node knowledge, computation, and block inclusion. It
does not execute cryptography: a proof is represented by the transactions it
asserts and the time and bytes charged to produce, verify, and transmit it.

The implementation is authoritative where this document and the code disagree.
The measured outcomes and their confidence are kept separately in
[results.md](results.md).

## Questions the model is built to answer

Six objectives. They are questions, not claims that the simulator has answered
them. [results.md](results.md) opens with a one-row-per-objective standing table
and then gives the measurements behind it; [roadmap.md](roadmap.md) lists the
work outstanding, tagged by objective.

1. **O1 — Bandwidth.** What offered load fits an ordinary uplink, and does a
   mainnet-shaped load fall inside it? Asked as a frontier rather than as
   "does it fit", because the yes/no form only ever describes the fixture that
   was run. The wrapper term is what binds: one recursive proof per peer per
   interval, paid whatever the transaction count, so the frontier is set by
   degree and tick interval before load enters at all.
2. **O2 — Compute.** What proving rate does keeping pace with the emission
   cadence require, and can an ordinary machine sustain it? Also a frontier:
   the operative threshold is whether a fold finishes inside a tick, since a
   prover that falls behind stops widening its claims.
3. **O3 — Aggregation leverage.** Does folding deliver the density the design
   assumes? `block_budget` limits *objects*, so what a block settles is the
   budget times the claimed transactions per selected object. The second factor
   is the design's whole purpose and the least measured quantity here.
4. **O4 — Limits.** Where is capacity, what breaks first past it, and does the
   network recover once load stops? These contain feedback loops — congestion
   causing retries causing congestion — which is what a simulator can answer
   and arithmetic cannot.
5. **O5 — Protocol choices.** Which of the EIP's open choices matter, and which
   way? Relay, wrapper economy, supersession, fold plan, CPU discipline,
   inclusion requirement, selection and eviction are all named, sweepable
   policies rather than branches, so these are measured comparisons.
6. **O6 — Position.** Does where a transaction enters the overlay, or where the
   proposer sits in it, decide whether it is included and when?

O1 and O2 are coupled through emission cadence: proving duration changes wrapper
emission, while congestion changes which arrivals are available to folds.
Results must therefore report the compute and network settings held fixed.

Reproducibility is not on that list. It is a standard every result is held to
rather than a question about the protocol — variant identity on each number, a
stated evidence class and confidence, and checks independent of the reporting
code. [CONTRIBUTING.md](../CONTRIBUTING.md) states it as a practice.

The honest boundary: every participant here is honest, identical, online and
altruistic. Nobody spams, withholds proving effort, or sends invalid evidence.
Whether the design survives participation being a choice is a further
objective, not a parameter sweep of these six.

## Time and the event calendar

The domain uses milliseconds of simulated time. `starksim.engine.Engine.now`
advances only when the engine removes an event from its calendar; it has no
relationship to wall-clock runtime. `HeapCalendar` orders events by
`(time, sequence)`, so events at the same timestamp run in the order in which
they were scheduled. This tie-break is part of the model's deterministic
semantics.

The domain event kinds are births, deliveries, aggregation ticks, slot
boundaries, expiries, and request timeouts. A `Server` adds service-completion
events for egress, optional link serialization, and CPU work. A server owns its
waiting queue; queueing delay follows from the arrival and service schedules
rather than from a separate delay formula. Queue bounds apply only to waiting
work, use tail-drop, and exclude the job already in service.

Periodic trace observations do not enter the event calendar. They read state
after domain events and therefore cannot consume sequence numbers or change
same-time ordering. This is the constraint recorded in
[ADR 0006](decisions/0006-observation-cost.md).

## Objects, coverage, claims, and discards

A leaf is one transaction and owns one dense bit index. Kind `q` represents
a transaction with a bare post-quantum signature; kind `p` represents a privacy
transaction carrying a STARK witness. A leaf has a fee scalar and may have an
expiry time.

An aggregate is an n-ary fold of one or more pool objects. Its children are
global provenance, not information automatically available to nodes. The arena
does not record parent links, because learning that an object was folded into a
new proof would give a node knowledge no message supplied.

Coverage is the immutable set of leaf transactions structurally contained
in an object. `Arena.coverage` represents it as a Python integer bitmask and
memoises aggregate masks behind an optional bounded cache.

A claim is what a leaf or aggregate permits another participant to rely on.
A leaf claims its own bit. An aggregate's claim is frozen when its proving job
starts and can be narrower than its coverage. Selection, retention, and block
inclusion use `Arena.claim`, not structural coverage.

The EIP's `discards` input is represented by removing transactions the prover
knows are no longer includable from the mask asserted by a new proof. The
discard knowledge is local: under delayed chain notification two nodes may
produce different claims from the same structural inputs. Inclusion and expiry
are the two current reasons a transaction enters the canonical discard set.

Global facts and local beliefs are deliberately separate; see
[ADR 0002](decisions/0002-global-truth-and-node-belief.md).

## Network and relay

Nodes occupy an explicit undirected overlay. Each edge has a fixed latency,
optionally with identity-derived jitter. Every node has one egress server, so
sending a payload to degree `d` serializes `d` copies through one finite uplink.
`link_capacity`, when configured, adds a per-direction serialization stage
after node egress; it does not replace the uplink. See
[ADR 0001](decisions/0001-node-egress-bandwidth.md).

Transport is store-and-forward. The complete payload leaves egress, traverses
any link-capacity stage and link latency, and then arrives. Full objects,
wrappers, and block notices pay `verify_recv` on the receiver's CPU before they
are admitted or relayed. Announcements and wants are control messages and do
not create verification jobs. See
[ADR 0004](decisions/0004-store-and-forward-verification.md).

Three relay policies define how raw pool objects move:

- `flood` pushes the full object to each eligible peer. A receiver verifies,
  admits, and forwards it, suppressing duplicates locally.
- `announce_fetch` sends an object identity first. A receiver that wants it
  requests it from the announcing peer; the full response then follows the
  same verify-admit-forward path. Optional request timeouts retry an unanswered
  request.
- `wrapper_only` never sends a raw object. Claims and transaction bodies spread
  through each node's periodic wrapper, so progress across one hop is quantized
  by the aggregation interval.

Ingress bytes are measured but ingress bandwidth is not metered. A bounded
egress queue or CPU queue can refuse work. An egress refusal means the bytes did
not cross the wire; a CPU refusal means they arrived and consumed ingress but
produced no admitted evidence.

## Wrappers and per-peer substitution

Each completed root proof has a `WrapperCore`: wrapper identity, proof object
identity, creator, tick, mode, claimed transaction mask, and dependency count.
The wrapper identity is separate from the proof identity because a later tick
may emit a new wrapper around an unchanged proof.

`aggregation_trigger` is `aligned`, `spread`, or `when_ready`. `aligned` fires
every node's clock together, while `spread` — the default — gives each node a
derived offset within the EIP's interval. `when_ready` has no clock: an idle
node with at least two objects reconsiders aggregation only after an admission,
birth, completed proof, or chain-knowledge change. Its emission rate therefore
depends on load rather than a configured interval.

A tick initiates selection and a wrapper is emitted when its proof completes.
Folds longer than one interval therefore reduce emission frequency. This is an
at-most-once-per-interval model and differs from the EIP's exact periodic
requirement.

A node builds a separate `WrapperPayload` for every peer. For each claimed
transaction it sends the body if that peer is not known to hold it, otherwise a
hash reference. Body beliefs form when the payload physically arrives because
the receiver keeps delivered bodies even if proof verification is refused.
Sender-side proof beliefs form only when the receiver already retains the proof
or its verification job has been accepted. These timings prevent a dropped or
refused proof from suppressing the copy needed to repair the loss.

Wrappers travel one hop and are never relayed. Each receiving node verifies and
retains the proof, may combine it with other evidence at its next tick, and then
emits its own wrappers.

The `wrapper_economy` modes change redundant proof transmission:

- `full` sends a full wrapper to every peer on every emission.
- `skip_unchanged` suppresses a copy when the peer is known to hold the exact
  proof and there are no new bodies for it.
- `proof_by_ref` replaces an already-held recursive proof with its hash.
- `lean` combines suppression and proof-by-reference.

The default `SizeModel` charges 262,144 bytes for the recursive proof, plus
dependency triples and per-peer bodies or hashes. This per-peer, per-tick proof
term is the wrapper constant discussed in [results.md](results.md).

The size model reproduces the EIP's own mode-0 guidance as arithmetic rather
than as a measurement. Concatenating raw leanSPHINCS witnesses costs
`sig_witness` per dependency, while one recursive proof is a flat
`recursive_proof`, so mode 1 first becomes the cheaper encoding at
`262144 / 2560 = 102.4`, meaning 103 dependencies — the EIP's "about a hundred"
(`eip-8288.md:244`).

Both modes are emitted. `wrapper_mode="recursive"` is the default and always
mode 1; `wrapper_mode="cheapest"` decides per emission, and takes mode 0 only
when the node **holds** every claimed dependency as an object, since a witness
travels only inside a raw object. Two questions about a claim are therefore kept
separate: what its raw evidence would *weigh*, which needs only the
dependencies' schemes and which the triples carry (`eip-8288.md:67`), and
whether this node could *send* that encoding, which needs the witnesses in hand.

The mode is chosen when a wrapper is *emitted*, and an aggregate exists only
once it has been proved, so `cheapest` saves the proof's bytes on the wire and
not the proving that produced it. In the protocol a node choosing mode 0 need
not prove at all; here it always has. That makes the measured mode-0 saving a
lower bound on the protocol's, and it is why mode 0 looks like a bandwidth
lever in this model rather than the compute lever the EIP describes.

Mode 0 conveys no recursive proof, and the receiver's bookkeeping says so:
`NodeView.witness_backed` marks a claim evidenced by witnesses. Such a claim
cannot resolve a proof reference, forms no peer belief that a proof is held, and
is charged as its dependencies rather than as one nested proof when it is later
folded. Recording mode 0 as a verified proof instead is a mistake that flatters
it twice over — see [results.md](results.md).

## Aggregation duty cycle

`NetworkSim.schedule_ticks` schedules a tick for every node at each aggregation
interval. `aggregation_trigger="spread"` derives a stable phase offset for each node;
`aligned` schedules the nodes together for controlled tests.

At a tick the node:

1. prunes held objects whose claims contain nothing it still considers live;
2. optionally cancels waiting proof jobs from older ticks under
   `on_tick_overrun="abandon"`;
3. runs the configured selection strategy against its current view and proving
   budget;
4. turns the immutable `Selection` into an `AggregationPlan`; and
5. queues each plan job on its CPU.

Selection includes the node's chain-knowledge generation and the claim of each
chosen input. Planning is separate and pure, so queued work can be cancelled
without embedding event-engine actions in the plan. The built-in fold plans are
one wide fold, a balanced binary tree, and a tree capped at fan-in eight.

A fold takes two inputs or more. A selection that resolves to fewer never
reaches the prover: a lone aggregate is re-wrapped rather than re-proved, since
its coverage is already proven and a second proof over it would invent work and
a fold depth, and a selection whose objects have all left the arena is abandoned
rather than charged for folding nothing. The single exception is a lone leaf,
where one proving job is what gives a bare dependency its first recursive proof
— the cost the EIP's mode 0 exists to avoid for small dependency counts, and
which a node avoids only by choosing that mode (`wrapper_mode="cheapest"`) or by
declining to fold at all below the crossover (`fold_threshold="crossover"`).

Proving consumption and the output claim are booked when a job starts. This
counts in-flight work and freezes the claim against what the node knew at that
instant. Intermediate jobs create internal aggregates; only the root is
retained as the node's latest aggregate and emitted in a wrapper.

## Computation and fold cost

Each node has one non-preemptive CPU shared by receive verification, recursive
proving, and block construction. `cpu_discipline="fifo"` serves waiting jobs in
arrival order. `priority` serves verification before block construction before
proving, with FIFO ties; neither mode preempts a running job or pushes a lower
priority job out of a full queue. The rationale is in
[ADR 0005](decisions/0005-one-cpu-per-node.md).

`CostTable` accepts either a constant duration or a callable of a size argument
for each job kind. Recursive proving has three cost bases:

- `prove_cost_basis="deps"` passes the number of dependencies asserted by that
  plan job.
- `prove_cost_basis="inputs"` passes the number of object or prior-proof inputs
  folded by that job.
- `prove_cost_basis="circuit"` prices the job by what it actually makes the
  circuit verify, and requires a `CircuitCost` rather than a scalar.

The circuit basis exists because the other two cannot express the EIP's own
description of the work. `eip-8288.md:188` has one recursive circuit verifying a
flat list of three input kinds with three different verifiers: leanSPHINCS
signatures, leanSTARK witnesses, and nested recursive STARKs. Those cost very
different amounts, so charging per input bills a signature at a nested proof's
price.

Hashes are the unit, because both sides can be estimated in it and neither can
be measured in milliseconds without a circuit. A SPHINCS-style verification is
about **470 hash evaluations**: the fitted EVM model
`ExecGas = 36118 + 194 * ops` puts the C13 parameter set at 468 from its 127 K
gas, and the scheme's own formulas give FORS 121, WOTS+ 301 average and Merkle
24, for 446. A recursive STARK verification is FRI-dominated — queries times
Merkle depth times commitment rounds — spanning roughly 5 000 to 19 000, of
which **10 000** is the midpoint and an estimate rather than a measurement. So
one nested proof costs about **21 signatures**. A millisecond scale is attached
by `CircuitCost.anchored_on_pairwise_fold`, since a measured 2-to-1 fold is two
recursive verifications. Calibration scope belongs in
[results.md](results.md).

`CircuitCost` also models the protocol's hard ceiling on a single proof: the
`2**24` KoalaBear trace length at a fixed trace width (`eip-8288.md:416`). A job
whose verification work would exceed it is refused rather than slowed, because
such a proof cannot exist, and the refusal is counted in
`proof_jobs_over_capacity` — which reports `-1` under the scalar bases, where
there is no ceiling to exceed. The entire plan is checked before submission, so
a refused plan consumes no proving time. Nothing selects against the ceiling
yet. Rows per
hash is the dominant unknown — a dedicated Poseidon2 AIR is a few rows per
permutation where Keccak-in-AIR is two orders more — and it decides whether one
proof holds a few hundred signatures or tens of thousands.

Input-based benches may use a linear fan-in cost and a capped-eight plan. Their
calibration and scope belong in [results.md](results.md). `NetworkSim` accepts
other cost functions and plan shapes.

For a linear input cost anchored at `a` milliseconds per pairwise fold, an
`m`-input fold takes `a·m/2`; fitting inside interval `T` requires `m ≤ 2T/a`.
Numerical calibrations belong in [results.md](results.md).

The one-CPU abstraction has two signed biases:

- Verification is pessimistic. A mostly sequential verification job is charged
  as if it occupied the whole machine, while a real multicore node may verify
  several messages concurrently.
- Block timing is generous. The chain fact is created at the slot boundary and
  block-build cost occupies the CPU afterwards, so contention can reduce what
  the proposer knew but cannot make the block late or miss its slot.

## Block building and chain knowledge

`ProposerSchedule` chooses a fixed, rotating, or seed-derived random proposer
for each slot. Before building, the simulator expires every transaction whose
deadline is at or before that boundary. The proposer then selects greedily from
objects in its own `NodeView`, using their claims and what that proposer knows
to be discarded.

`block_budget` is a fold budget: selecting `k` objects costs `k - 1` folds, so
the proposer can select at most `block_budget + 1` objects. Under the default
`inclusion_policy="body_required"`, each claimed transaction must also have a
body in the proposer's view because the execution payload must carry it.
`claim_only` is the comparison mode without the body-possession requirement.

`ChainState.include` records both attempted and newly included masks so stale
selection is measurable. It does not model transaction order, nonce validity,
or interactions between transactions. Counts are therefore ordering-blind
upper bounds on what a real builder could include.

With `chain_notify="instant"`, every node reads the canonical discard set as
soon as it changes. With `overlay`, the proposer floods a block notice through
the same metered network and nodes update only after verification. The block
notice is sized with one recursive-proof term, but the simulator does not
construct or validate a block-level recursive proof artefact.

## Expiry and retention

A leaf with a lifetime receives an absolute expiry timestamp and an expiry
event. Expiry is also applied immediately before block construction, so a
transaction cannot win a same-time slot after its deadline. `ChainState` keeps
expired and included masks separate and exposes their union as discarded.

Nodes retain held objects, bodies, verified-proof identities, duplicate
suppression state, and per-peer beliefs separately. Candidate, seen, and
coverage-cache limits are optional and default to unbounded. Supersession,
pruning, eviction, queue refusal, and duplicate forgetting are distinct and
counted; none is treated as successful inclusion.

## Determinism

Stable identities use `starksim.derive.stable_fraction` or `stable_index`.
Other concerns receive dedicated labelled random streams. A benchmark workload
is a function of its seed alone, so changing a policy does not reshuffle births,
fees, topology, or unrelated jitter. The full rule and its consequences are in
[ADR 0003](decisions/0003-deterministic-randomness.md).

## Non-goals and interpretation limits

The model deliberately has:

- no cryptographic proof generation, verification, invalidity, or security
  analysis;
- no FOCIL or inclusion-list mechanics;
- no fee market beyond a scalar fee used by selection and eviction policies;
- no adversarial, selfish, offline, or heterogeneous participants;
- no reorgs or fork choice;
- no account nonces, transaction ordering, state conflicts, or execution; and
- no validation against a production client or testnet.

EIP-8288 is a draft. Simulator outputs compare the mechanisms and parameters
implemented here; they are not protocol guarantees or measurements of an
Ethereum client.
