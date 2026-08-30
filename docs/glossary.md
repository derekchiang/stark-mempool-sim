# Glossary

## Model terms

**Aggregate**
: A pool object produced by folding one or more objects. Its structural
  coverage and the claim its proof asserts can differ.

**Claim**
: The transaction mask another participant may rely on. An aggregate's claim
  is frozen when proving starts after removing what the prover then knows is
  discarded.

**Coverage**
: The immutable set of leaf transactions structurally folded into an object.
  It is global provenance, not proof of what a node knows or what an aggregate
  currently claims.

**`discards`**
: The EIP circuit input represented by removing locally known included or
  expired transactions from a new proof's claim. It does not mutate the
  aggregate's structural coverage.

**Leaf**
: One user transaction and one bit in every coverage mask. Kind `q` is a bare
  post-quantum signature and kind `p` is a privacy transaction with a STARK.

**Reach**
: The fraction or set of nodes that have ever admitted an object. Reach is
  cumulative and does not decrease.

**Residency**
: The fraction or set of nodes holding an object at a particular time.
  Supersession, pruning, or eviction can make residency lower than reach.

**Witness-backed claim**
: A claim a node received as the EIP's mode 0 — its dependencies' raw witnesses —
  rather than as a recursive proof. Recorded in `NodeView.witness_backed`: it
  cannot resolve a proof reference, forms no peer belief that a proof is held,
  and is folded at the price of its dependencies rather than of one nested proof.

**Wrapper constant**
: The recursive-proof term sent per peer per wrapper emission: 262,144 bytes
  under the default `SizeModel`, before dependency triples, bodies, or hashes.
  It is constant in the number of transactions but scales with node degree and
  inverse tick interval.

**Variant**
: The configuration identity returned by `NetworkSim.variant` and included in
  CSV rows and trace headers. It identifies protocol, network, cost, size, and
  bound choices, but not the workload scheduled by a harness.

**Preset**
: A named dict of `NetworkSim` keyword arguments in `starksim.presets`:
  `eip_faithful`, `v1_equivalent`, `high_throughput`. A preset names policy
  fields only; the overlay, size model and cost table stay with the workload.

**Form (of evidence)**
: How a node knows a transaction, read from `knw.o`: through the
  transaction's own object, through a wrapper claiming it, through an
  announcement (class `requested`), or through a block notice (`settled`).

**Spread tree**
: For one followed transaction, the edges from `knw.peer` to `knw.n` at each
  node's first transition out of `none`, rooted at the entry. Drawn by the
  viewer when colouring by knowledge; a record of successful spread only.

**Offered bytes / wire bytes**
: Offered bytes entered an egress queue; wire bytes completed egress. Queued
  work and tail-dropped messages make them different under load.

## Objective labels

Defined in [model.md](model.md); [results.md](results.md) is organised by them.

**O1 — bandwidth**
: What offered load fits an ordinary node uplink, and whether a mainnet-shaped
  load falls inside it.

**O2 — compute**
: What proving rate keeping pace with the emission cadence requires, and whether
  an ordinary machine sustains it. Coupled to O1 through the cadence, not
  independent of it.

**O3 — aggregation leverage**
: How many transactions one selected object claims, and therefore what a block
  settles for a given fold budget.

**O4 — limits**
: Where capacity is, what breaks first past it, and whether the network recovers
  once load stops.

**O5 — protocol choices**
: Which of the EIP's open choices — relay, wrapper economy, supersession, fold
  plan, CPU discipline, inclusion requirement, selection, eviction — change cost
  or inclusion, and in which direction.

**O6 — position**
: Whether a transaction's entry point or the proposer's location decides
  inclusion and delay.

## Policy names

**`flood`**
: Push each full raw object to peers, then verify, admit, and forward it.

**`announce_fetch`**
: Announce an object identity; interested peers request and receive the full
  object before the usual verification and relay path.

**`wrapper_only`**
: Never relay raw objects. Claims and bodies move through periodic wrappers and
  verified re-aggregation.

**`full`**
: Send the complete wrapper proof to every peer on every emission.

**`skip_unchanged`**
: Suppress a peer copy when that peer is known to retain the exact proof and
  there are no new bodies for it.

**`proof_by_ref`**
: Replace the recursive proof with its hash when the peer is known to retain
  that proof.

**`lean`**
: Combine `skip_unchanged` and `proof_by_ref`.

## Trace event kinds

**`cfg`**
: First record: variant identity, schema version, focus, sampling, counter grid,
  follow selection, and optional topology.

**`arr`**
: A leaf transaction was created and admitted at its entry node.

**`snd`**
: A payload was accepted by a sender's egress queue. This is the only sampled
  event kind.

**`drp`**
: One message was refused by an egress or CPU queue. It records a loss, not
  traffic successfully sent.

**`adm`**
: A node admitted an object after delivery and any required verification.

**`drop`**
: One or more objects left a node's view through supersession, pruning, or
  eviction; unlike `drp`, it is a residency change rather than a message loss.

**`knw`**
: A followed transaction changed knowledge class at a node after a complete
  event boundary.

**`wrp`**
: A node emitted one wrapper core around a root proof.

**`blk`**
: A slot proposer built a block, including counts for selected objects,
  included transactions, and stale attempted coverage.

**`cnt`**
: Exact cumulative per-node counters and queue gauges on the observation grid.

**`seg`**
: One `NetworkSim.run` call ended, with its observed interval and stop reason.

**`end`**
: The trace was explicitly sealed as complete, cancelled, or errored.

The seven knowledge classes used by `knw`, in precedence order, are `settled`,
`held_and_body`, `held_no_body`, `body_only`, `verifying`, `requested`, and
`none`. See [trace-format.md](trace-format.md) for field-level definitions.
