# An aggregation strategy: flood the cheap, pull the expensive, aggregate once

Candidate mechanisms, not specification and not implemented unless a section
says so. Measurements cited are in [results.md](../results.md); the semantics
these would change are in [model.md](../model.md).

## Measurements motivating the proposal

Wrapper proof pushes dominate measured bandwidth. One recursive proof to every
peer every interval is a standing 12.58 Mbit/s at degree 6 and a 1 s tick,
before a single transaction moves, and it is 73–98% of all bytes in every
measured configuration — 96% in a post-quantum-only pool. It is paid whether or
not the proof changed and whether or not the peer wanted it.

Deep recursion dominates estimated compute under circuit pricing. Priced by
what the circuit verifies, one nested recursive proof costs about 21 signature
verifications. So the shape of the aggregation tree dominates:

| shape, 1000 signatures | proving time | ratio |
| --- | --- | --- |
| one wide proof over the raw signatures | 20.8 s | 1.00 |
| eight wide shards, merged in one 8-way fold | 24.4 s | 1.17 |
| 1000 single proofs, then 999 pairwise folds | 908.9 s | **44** |

The `2**24` trace ceiling does not interfere: 1000 signatures is 8% of it.

The two transaction kinds have different wire sizes and measured load
frontiers. A privacy witness is 263 kB and breaks the network between 2 and 3
objects per 4 s window at 10 Mbit/s; a
post-quantum signature is 3.2 kB and holds 100% coverage at every load measured.
A single relay policy has to be chosen for the worse one.

For signature witnesses under the default size model, mode 0 is smaller below
103 dependencies. Mode 0 carries raw witnesses, mode 1 one recursive proof, and
for signatures they cross at 103
(`eip-8288.md:244` says "about a hundred"). Ten signatures cost 1.27 Mbit/s as
mode 0 against 12.58 Mbit/s as a recursive proof that should not have existed.
For privacy transactions the crossover is 2, because their witness is itself
256 kB — so the right mode depends on what the pool contains, by a factor of 50.

## The strategy

Five mechanisms. Each states what it buys, what it costs, and what would
falsify it.

### S1 — Do not aggregate below the crossover

Selecting mode 0 only at emission is implemented but saves about 2% on the
cited fixtures. Mode 0 concatenates raw witnesses, so
a node may only choose it for claims whose witnesses it actually holds, and a
witness travels solely inside a raw object: a wrapper carries bodies and hashes.
Once a node folds a peer's aggregate its claim covers dependencies whose
witnesses it never received, and mode 0 stops being available to it. Across all
three relay policies, it fires 4 to 11 times in hundreds of emissions. Under
`wrapper_only` it never fires at all, since nothing ships objects.

The EIP primarily assigns mode 0 to clients and permits mempool use only for
pools containing leanSPHINCS signatures without STARKs (`:244`).

`fold_threshold="crossover"` declines to fold while raw witnesses are smaller
than the recursive proof. It is implemented and measured:
3.6× fewer bytes and 6.4× fewer proofs at 512 transactions with inclusion
untouched, but a third of inclusion lost at 256, because a node deferring below
the threshold needs enough arrivals to cross it before what it holds expires
(see [results.md](../results.md)). Two further conditions bound it: the load must
be able to reach the crossover at all, and something other than wrappers must
carry the transactions, so it stalls `wrapper_only` completely.

A node holding fewer than about a hundred signatures forwards them as they are
and proves nothing; past the crossover it proves once, wide. That keeps
possession and the mode aligned, because a node that has not folded still holds
everything it claims. It is a selection policy rather than a sizing choice.

Cost: a node that declines to fold contributes no aggregate, so if every node
declines, nothing aggregates and the builder receives raw signatures — the
crossover has to be reached by someone. That makes it a coordination question,
and it is the same question S4 asks from the other side. Falsified if the
threshold that maximises inclusion turns out to be far below the byte
crossover, which would mean folding early is worth its bytes for reasons of
propagation rather than size.

### S2 — Announce proofs, pull them on demand

Replace push-to-every-peer with an announcement — a hash plus the coverage mask,
125 B for a thousand transactions — and send the proof only to a peer that asks.
Standing cost falls from **12.58 Mbit/s to 0.01 Mbit/s**. A middle setting
exists: push to two peers and announce to four is 4.20 Mbit/s and keeps most of
the latency.

The justification is that the entity which needs a proof is the builder;
everyone else wants one only to build a bigger one, and under S4 they mostly
should not. Cost: a round trip, which this project prices at roughly 3× latency
for small objects; a request surface, since a node with good coverage gets asked
by everyone and push at least lets the sender control its own rate; and a
load-balancing question nobody has looked at. Falsified if the pull latency
costs more inclusion than the bandwidth buys, which is the same trade
`announce_fetch` already wins for bodies and may lose for proofs.

### S3 — Relay by transaction kind

Flood signatures, never flood witnesses. `relay_policy` is currently global, so
the cheap object pays for the expensive one — announce-fetch costs a signature
about three times its flood latency for robustness it never needed. Falsified if
the mixed configuration's frontier is no better than the worse single policy's,
which would mean the kinds interact through the shared uplink more than they
differ.

### S4 — One recursion level, never a tree

Verify signatures in bulk directly and use recursion only to combine what
*different* provers produced, in a single wide fold. The 1.17× against 44× gap
is the argument. The measured sharded-wide-then-merged shape is 17% above the
estimated wide-proof floor. Cost: coordination and a censorship surface where
a shard's aggregators can exclude. Falsified if
partition boundaries lose more in unclaimed transactions than the shape saves.

### S5 — Trigger on accumulation and deadline, not on a clock

Emission already skips when folds outgrow the interval, and the tick's only
remaining job is to initiate selection. Replace it with: aggregate when enough
has accumulated to beat the mode crossover, or when the slot deadline
approaches. Cost: the emission rate stops being predictable, which is what
makes the standing bandwidth requirement calculable, and synchronised bursts
become possible. Falsified if a swept fixed interval captures the same gain.

## Do they compose?

Each mechanism above is measured alone. `bench.levers` runs the EIP-faithful
configuration, each lever by itself, and all of them together, and the answer on
that fixture is that the bytes very nearly compose — 78.3% saved against 80.2%
if the levers were independent, and the gap shrinks toward zero as seeds are
added. What the byte column hides is more useful than the composition: one lever
supplies 75.7 of those 78.3 points, `cheapest` saves nothing at all because
possession rather than size gates it, and the combination triples mean inclusion
delay. See [results.md](../results.md). Arguing for the whole set from five
separate byte savings would have been arguing for one lever and four
distractions, at a latency price none of them showed alone.

## What this model cannot settle

Every mechanism here lets a node do less work under pressure, and every
participant here is honest, identical, online and altruistic. So the simulator
can measure whether a mechanism works when everyone follows it, and cannot say
whether anyone would. S2 and S4 are the sharp cases: pull makes a node's
outbound cost demand-driven, and one-level aggregation concentrates proving in
fewer provers. Both create incentive and centralisation effects this model
cannot resolve.

Evaluate each feedback mechanism for oscillation, a lower overload threshold,
and unequal effects across nodes.
