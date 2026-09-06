# Explainer

What EIP-8288 proposes, what this simulator makes of it, and how to read a
run. Written for someone weighing the proposal who has not used this
repository, and for the first hour of someone who will.

This page explains; it owns nothing. Semantics belong to [model.md](model.md),
parameters and defaults to [configuration.md](configuration.md), published
measurements to [results.md](results.md), the trace contract to
[trace-format.md](trace-format.md), and the viewer's mechanics to
[viewer.md](viewer.md). Where this page and one of those disagree, that one
is right. [quickstart.md](quickstart.md) is the hands-on companion.

## What EIP-8288 proposes

Post-quantum signatures are large. A hash-based signature is kilobytes where
a curve signature is bytes, and a privacy transaction carries a STARK witness
larger still. Flooding those through a gossip mempool costs bandwidth that
ordinary nodes do not have, and verifying each one at every hop costs time.

The EIP's answer is to make validity a thing that is proven once and
forwarded as a proof. A transaction's signatures and witnesses are its
*dependencies*. A mempool node collects the transactions it holds, proves in
one recursive STARK that every dependency is valid, and sends its peers a
*wrapper*: the transactions, or their hashes if the peer already has them,
plus that one proof. A peer verifies the proof instead of the signatures,
folds what it received into its own next proof, and forwards its own
wrapper. The chain of proofs grows recursively. A block builder does the same
thing one last time over the transactions it includes, and the block carries
a single proof for all of them.

Two design constants shape everything that follows. Nodes emit a wrapper
every `AGGREGATION_INTERVAL`, and each wrapper carries one recursive proof
whose size does not depend on how many transactions it covers. The EIP also
allows a cheaper *mode 0* wrapper that ships raw witnesses instead of a
proof, worthwhile up to about a hundred signatures, and it prescribes what a
node should prioritise when it cannot prove everything it saw. The snapshot
the simulator reads is [eip-8288.md](eip-8288.md); the EIP is a draft and the
published text has moved on.

One thing the snapshot does not settle. It bounds the signature and STARK
dependencies a single wrapper may carry to small constants, and in the same
section has a node broadcast one wrapper covering all of its active
transactions. The simulator does not implement the limits,
[configuration.md](configuration.md#the-eip-as-written) records that choice,
and this page does not resolve the contradiction.

## What the simulator is, and is not

`starksim` is a discrete-event model of that protocol running on a network
of ordinary nodes. Each node has one uplink of finite bandwidth, one CPU,
neighbours on an overlay, latency to each of them, and its own view of what
exists. Time advances only when an event fires; nothing in the model refers
to wall-clock time.

There is no cryptography. A proof is three things: the set of transactions
it asserts, a byte count, and a duration charged to the CPU that makes or
checks it. Nothing is ever invalid. Every participant is honest, identical,
online and altruistic. What the model can say is how long a proof takes to
make and to verify, how many bytes it costs to move, who ends up knowing
what, and what a block built from one node's view contains. What it cannot
say is whether the proofs are sound, what an adversary does to the mempool,
or how a real client behaves, because none of those is in it.
[model.md](model.md#non-goals-and-interpretation-limits) lists the non-goals
in full, and every number in [results.md](results.md) inherits them.

![The protocol on the left, its representation in the model on the right,
and the box of what is deliberately not modelled](images/boundary.svg)

*Left, what the EIP proposes; right, the object the model keeps for it;
below, what is left out on purpose.*

## Six moving parts

Six mechanisms account for nearly everything a run does. Each is given here
as the idea and its consequence; the sequencing, defaults and edge cases are
in the section of [model.md](model.md) it links.

### 1. The aggregation duty cycle

Every node keeps its own clock. Once per interval it chooses what to fold
from what it holds and queues the proving job; the wrapper goes out when the
proof finishes, not at the tick. A fold that runs longer than the interval
therefore delays the next wrapper, and what a prover that falls behind can
claim is bounded by its proving budget rather than by what it holds. Where
the EIP's priority order lives when a node cannot prove everything it saw,
and what the fold plans are:
[model.md](model.md#aggregation-duty-cycle). Code: `NetworkSim._tick`,
`aggregation.py`.

### 2. Wrappers, and what one costs

A wrapper is built separately for every peer, carrying a body or a hash per
transaction according to what the sender believes that peer holds, plus the
proof. Wrappers travel one hop and are never relayed. The proof term is the
wrapper constant: 262,144 bytes under the default size model, per peer, per
emission, the same whether the node claims one transaction or a thousand.
Once a node holds anything to claim, its bandwidth cost is set by degree and
interval before load enters, which is why those two parameters bind the
bandwidth question before the transaction rate does. Per-peer substitution,
the wrapper economies and mode 0:
[model.md](model.md#wrappers-and-per-peer-substitution); the size table:
[configuration.md](configuration.md#message-sizes). Code:
`NetworkSim._emit_wrapper`, `SizeModel`.

### 3. One uplink per node

Bandwidth is metered where it is scarce: at each node's egress. Sending one
payload to six peers serialises six copies through one finite uplink, and a
bounded send buffer refuses what does not fit. A refused message never
crosses the wire. Transport is store-and-forward, so nothing is relayed
before it has arrived whole and been verified. Rationale:
[ADR 0001](decisions/0001-node-egress-bandwidth.md),
[ADR 0004](decisions/0004-store-and-forward-verification.md). Relay
policies and what is metered: [model.md](model.md#network-and-relay).

### 4. One CPU per node

Verifying what arrives, proving what was selected, and building a block all
share one non-preemptive CPU per node. A long fold holds up the
verification of everything that arrived meanwhile, and a burst of arrivals
delays the fold. The abstraction is pessimistic about verification, which a
real multicore node would parallelise, and generous about blocks: the chain
fact is created at the slot boundary and the build cost is charged
afterwards, so contention can shrink what the proposer knew but cannot make
a block late. With `verify_cost` set, an arrival is priced by the evidence
it carries, and the kinds differ by orders of magnitude; which is measured
and which is assumed is stated with the O2 entry in
[results.md](results.md). Rationale:
[ADR 0005](decisions/0005-one-cpu-per-node.md). Cost bases and disciplines:
[model.md](model.md#computation-and-fold-cost).

### 5. Claims are not coverage

An aggregate structurally contains the transactions folded into it: its
*coverage*, a global fact. What its proof lets another node rely on is its
*claim*, frozen when proving started, after removing what the prover then
knew was included or expired. A claim can be narrower than coverage and
never wider. Selection, retention and block inclusion consult claims;
coverage exists for accounting. Reading one where the other is meant is the
model's most inviting mistake, because the two agree until the first
inclusion or expiry. The same separation runs through the whole design: the
arena knows every object that exists, but that provenance grants no node
possession of anything; a node holds what it was born with, produced
itself, or received and verified, and its beliefs about its peers change
only at events that justify them. Definitions:
[model.md](model.md#objects-coverage-claims-and-discards); the trap:
[pitfalls.md](pitfalls.md#model-concepts-that-are-easy-to-conflate);
rationale: [ADR 0002](decisions/0002-global-truth-and-node-belief.md).

### 6. A block from a partial view

At each slot boundary a proposer builds from the objects it happens to hold,
by their claims, within a fold budget. What a block settles is therefore the
distinct transactions the selected claims cover, and how many each object
brings is the design's whole purpose and its least measured quantity.
Inclusion is recorded as both attempted and newly included, so selecting
already-settled work is visible rather than hidden. The body requirement,
the budget arithmetic and chain notification:
[model.md](model.md#block-building-and-chain-knowledge). Code:
`NetworkSim._build_block`.

![One transaction's life across four lanes: the entry node, its uplink, a
peer's CPU and the proposer](images/lifecycle.svg)

*One transaction's life. Fan-out is serialised on the sender's uplink; the
peer verifies, admits and, at its own tick, folds; the claim is frozen when
proving starts; the wrapper leaves when the proof is done; the proposer
builds from what it holds at the slot boundary.*

## What a run produces

`NetworkSim` exposes `variant()`, the configuration that identifies a run,
and `metrics()`, what it measured. The variant travels in the trace header
and in every CSV row so that a number can be attributed to the choices that
produced it. The rule that follows is the one that catches mistakes: a
configuration value that is missing from the variant cannot be attributed
to a run, and a bound filed among the measurements never reaches the trace
header, so the viewer draws as if it did not exist.
[pitfalls.md](pitfalls.md#measurement-and-instrumentation) states the rule.

Some pairs of outputs look alike and are not, and reading one for the other
is how a run gets misreported: offered against wire bytes, reach against
residency, attempted against included, and a `-1` against a zero. The
definitions are in [glossary.md](glossary.md#model-terms); why each pair
matters, and the instruments that lie, in
[pitfalls.md](pitfalls.md#measurement-and-instrumentation).

The benches add one discipline. A workload is a function of its seed alone,
so two policies run on the same seed see the same births, fees and topology,
and a difference between their rows is the policy. That pairing is how every
comparison in [results.md](results.md) is made, and every entry there states
its command, its seed count, its evidence class and how far it travels.

## Reading the viewer

The viewer is a projection of a trace file, not a window on the simulator.
It reduces `trace.jsonl` in the browser and draws three pages from one
state. What was not traced cannot be shown, and a panel says so when it is
unavailable. The `?` beside the page tabs opens a short orientation; every
panel has its own `?` and a `table` view, and those are where the
definitions live. What follows is a reading order, not a description.

The line under the title is the trace header; read it first, since every
panel is conditional on it. The tiles are the run in one glance, and the
block ribbon under them the last few blocks. Most of the viewer is about one
transaction at a time: `auto` follow picks it and moves on when its story
ends or after a set number of slots, choosing a transaction pins it, and
the `‹` `›` buttons step through the list.

### Charts

The whole-network panels answer "what is binding", in this order: the bytes
panel, to see which payload kind dominates this run and how much is queued
against how much is in flight; the two utilisation panels, egress and CPU,
where the band across nodes matters as much as the median; queue depth; then
losses, remembering that no drops does not mean no overload while a backlog
is still growing. The two residency panels, by birth cohort and by hop
distance, answer whether propagation is holding up as the run goes on, and
cells that stay at the legend's high-residency end for later cohorts are one
thing to corroborate against the queues, not a conclusion on their own.

The scoped panels below the bar tell one transaction's story: its knowledge
classes stacked to the node count on its own clock, its object's reach
against residency, and that object's per-hop picture.

![The charts page on the fixture trace at the end of the run, pinned to the
first settled transaction](images/viewer-charts.png)

*The charts page on the fixture trace, at the end of the run, pinned to the
first transaction that settled: tiles, block ribbon, the whole-network
panels, then the scoped panels for that transaction and its proof.*

### Network

Where is this transaction known, and what did it cost to get there. Nodes
sit in rings by hop distance from its entry, coloured by what they know of
it: categories of evidence, not a scale, named in the legend. Green edges
are the tree along which it spread; thin red arrows are its recorded
duplicate deliveries, one kind of waste. What each mark means exactly, and
what the picture cannot show, is in
[viewer.md](viewer.md#what-the-viewer-shows).

![The network page on the fixture trace, just before the followed
transaction settles](images/viewer-network.png)

*The network page on the same transaction, just before it settles, while
the nodes still differ in what they know: rings by hop distance, the spread
tree in green, duplicates in red.*

### Transaction

When did each node come to know it, and from whom. The same transaction
against its own clock, from birth to settlement or to the current
observation horizon while it is unsettled, one row per node in the order
they learned of it: a sequence diagram of one transaction's life. The two
layouts and every mark are defined in
[viewer.md](viewer.md#what-the-viewer-shows).

![The transaction page on the fixture trace, rows by first knowing, the
transaction settled](images/viewer-transaction.png)

*The transaction page on the same transaction at the end of the run: rows in
the order nodes came to know it, deliveries as arrows, duplicates in red,
settlement as the dashed line.*

## What the model will not tell you

Every participant is honest, identical and altruistic, so nothing here
speaks to adversaries, free-riders or heterogeneous nodes. Nothing is
validated against a client or a testnet, because none exists yet. The
in-circuit cost of a privacy witness and the native cost of verifying the
real message mix are swept over bands, not measured, and the mainnet
frontier moves with them. The complete list of non-goals is in
[model.md](model.md#non-goals-and-interpretation-limits); what would move
each limit is in [roadmap.md](roadmap.md).

## The six questions

The model exists to answer six questions: bandwidth, compute, aggregation
leverage, limits and recovery, protocol choices, and position in the
overlay. They are questions, not claims that the simulator has answered
them. [model.md](model.md#questions-the-model-is-built-to-answer) states
them; the standing table in [results.md](results.md#standing-at-a-glance)
says where each one is, in words, and [roadmap.md](roadmap.md) what would
move it.
