# Results

What the simulator has measured, with the command that reproduces each number.

Every entry states its **evidence class**, because they are not equally strong:

- **bench** — reproducible by the command shown. The number in the table came
  from that command.
- **test** — asserted by a unit test, which pins the mechanism but not a
  magnitude.
- **probe** — measured once, in conversation, with no committed harness. Cited
  here so it is not mistaken for the other two, and a candidate for promotion
  to a bench.

Confidence is about how far a result travels, not how carefully it was run:
**high** (multi-seed, drift-checked, mechanism understood), **medium–high**
(mechanism understood, one fixture), **medium** (few seeds or one operating
point), **null result** (a difference was looked for and not found).

Model semantics are in [model.md](model.md); open questions in
[roadmap.md](roadmap.md). Nothing here is validated against a client
implementation or a testnet, because none exists yet — that caveat applies to
every row.

## Standing at a glance

One row per objective ([model.md](model.md) states them in full). Deliberately
qualitative: the numbers live in the sections below, so that this table cannot
drift away from them.

| | Question | Standing | Biggest gap |
| --- | --- | --- | --- |
| **O1** | What load fits an ordinary uplink? | Frontier located for one fixture, and the wrapper cadence — not the load — is what sets it. At 25 Mbit/s the EIP as written includes every PQ transaction to 128 tx/s by overspending the uplink; the levers without `capped8` include the same for half the bytes, and `capped8` caps inclusion. Under a mainnet-shaped mixture the binding resource moves off the wire entirely — see O2 | The frontier as a function of tick interval and degree; bursty arrivals |
| **O2** | What proving rate does keeping pace require? | Answered for one fixture on calibrated costs: the single CPU binds at mainnet privacy shares — proving takes 82–87% of it, receive verification a material rest. A fast desktop clears ~5 privacy tx/s within two slots of load end at 12 s slots; one with 2.4× slower proving clears none. Plan shape still decides throughput (binary halves wide's inclusion) | Native verification of the real message mix and the leanSTARK witness weight — both external measurements that move the frontier |
| **O3** | Does folding deliver the density the design assumes? | **Not measured.** What stands here is an illustration whose two numbers are both set by the fixture | Claimed transactions per selected object, as a function of load and budget |
| **O4** | Where is capacity, and what happens past it? | Two phenomena found and understood: a clearance-time threshold, and retry-induced collapse below capacity | Portability, which is blocked on the offered-load metric rather than on effort |
| **O5** | Which of the EIP's open choices matter? | Machinery complete and most choices priced, the supersede policies included. Amortizing the crossover threshold (k of 8 or 16) repairs its aggregation-off collapse; the byte savings measured alongside belong to the bandwidth levers it rides with | A gossipsub-shaped relay; the supersede race repeated where bandwidth binds |
| **O6** | Does position decide inclusion? | Answered for one topology family: delay attaches to place once diffusion-limited, and deadlines turn that into exclusion | Other topologies, degree heterogeneity, and placement under `wrapper_only` |

Two things are true of every row: nothing is validated against a client or a
testnet, and every participant modelled is honest, identical and altruistic.

## O1 — Bandwidth: what load fits an ordinary uplink?

**The frontier is under one privacy transaction per second per 10 Mbit/s of
mempool budget, and the two relay policies fail differently.** Sweeping offered
load at fixed capacity locates where sustained coverage stops holding. Judge it
by drift — late births covered as well as early ones — rather than by coverage
alone, since coverage inside a finite window is right-censored.

| privacy objects per 4 s | flood | drift | announce-fetch | drift |
| --- | --- | --- | --- | --- |
| 2 | 98.4% | −3.2% | 100% | 0.0% |
| 3 | 90.2% | −14.7% | 99.4% | +0.9% |
| 4 | 72.1% | −54.2% | 97.1% | −3.3% |
| 8 | 46.5% | −64.8% | 93.8% | −6.7% |
| 12 | 34.5% | −54.9% | 88.2% | −1.2% |

```sh
python3 -m bench.propagation --uplink-mbps 10 --objects 2   # and 3, 4, 8, 12
```

200 nodes, degree 8, 10 Mbit/s, 3 seeds. **bench**, **medium–high**. Near-full
coverage — 98.4% and 99.4%, not 100% — survives to 2 objects per window under
flood and 3 under announce-fetch — 0.5 and 0.75 privacy transactions per second. Past that the
policies diverge in kind, not just degree: flood's drift runs to −65%, which is
a backlog growing throughout the run, while announce-fetch settles at partial
coverage with drift near zero, so 88% at 3 per second is a steady state rather
than a collapse in progress. Post-quantum signatures at 3.2 kB hold 100% with
zero drift across the entire sweep, so their frontier is far higher and remains
unlocated. The binding object is therefore the privacy transaction carrying its
own 263 kB witness, and this frontier is for privacy-only and PQ-only
workloads on a wire-only cost model — the mixed-workload frontier with
verification priced is measured under O2, where the binding resource turns
out not to be the wire at all.

**One 256 kB proof crosses a 200-node overlay inside a slot; twenty per slot do
not.** Uncontended, a single privacy object reaches every node in 3.4 s at
10 Mbit/s against a latency floor near 0.4 s. Twenty objects born over one 4 s
window on the same links reach 28% of nodes under flood, and the negative
drift — late births covered far worse than early ones — says that number
describes a collapse in progress, not a steady state.

| uplink | one proof, t100 | twenty/slot, flood | drift | twenty/slot, announce-fetch | drift |
| --- | --- | --- | --- | --- | --- |
| 10 Mbit/s | 3401 ms | 27.8% | −47.9% | 68.3% | −22.6% |
| 25 Mbit/s | 1684 ms | 68.5% | −45.7% | 100% | 0.0% |
| 100 Mbit/s | 749 ms | 100% | 0.0% | 100% | 0.0% |

```sh
python3 -m bench.propagation --uplink-mbps 10
python3 -m bench.propagation --uplink-mbps 25
python3 -m bench.propagation --uplink-mbps 100
```

200 nodes, degree 8, 100 ms latency ±10%, 3 seeds. **bench**, **high**. The
sustained rows are right-censored: a threshold no object reaches prints as
`--`, never as a large delay. Twenty 256 kB proofs per slot is unsustainable at
10 Mbit/s, policy-dependent at 25, and comfortable at 100 — so the failure is a
statement about capacity and policy together, not about the design alone.

**Wrappers dominate the byte budget, and the wrapper cost is set by the
cadence, not by the transaction count.** The EIP has each node send one wrapper
per peer per interval carrying one recursive proof. A node with nothing to claim
emits nothing — an empty selection returns before emission, so an idle network
costs zero — but from the first live transaction onward the cost is one proof
per peer per interval regardless of how many transactions that proof covers.
The requirement is therefore **degree × 256 kB ÷ interval**, which is
12.6 Mbit/s of the mempool's *share* of the uplink at degree 6 and a 1 s tick.
Across a tenfold capacity increase, absolute wrapper bytes move by about a
fifth while witness bytes quadruple.

| uplink | flood wrapper | flood witness | wrapper share, flood | share, announce-fetch | share, wrapper-only |
| --- | --- | --- | --- | --- | --- |
| 10 Mbit/s | 1066 MB | 403 MB | 73% | 98% | 100% |
| 25 Mbit/s | 1069 MB | 1292 MB | 45% | 89% | 100% |
| 100 Mbit/s | 887 MB | 1655 MB | 35% | 84% | 100% |

```sh
python3 -m bench.economy --uplink-mbps 10
python3 -m bench.economy --uplink-mbps 25
python3 -m bench.economy --uplink-mbps 100
```

40 nodes, degree 6, 32 privacy transactions over 16 s plus a 16 s drain, 2
seeds. **bench**, **high** for the constant, **medium–high** for the shares.
Under flood the wrapper share falls as headroom grows, because at 10 Mbit/s
congestion was discarding the witness flood that the wrapper share was measured
against; wrappers dominate at every measured capacity only under announce-fetch
and wrapper-only. Every uplink figure here grants the mempool the *entire*
link, so "10 Mbit/s" means 10 Mbit/s of mempool budget — a stricter persona
than it reads, since a real node also carries blocks, attestations and sync.

**`wrapper_only` with the `lean` economy is always cheapest in bytes, and
faster only under congestion.** Never shipping the witness and letting claims
spread by re-aggregation costs the fewest bytes at every capacity, but
propagation is then quantised by the tick: one fold per hop. Under congestion
that is cheaper than the flood it avoids; with headroom it is pure added
latency.

| uplink | flood/full delay | announce-fetch/lean | wrapper-only/lean | wrapper-only/lean bytes |
| --- | --- | --- | --- | --- |
| 10 Mbit/s | 2.28 | 1.70 | **1.47** | 1007 MB |
| 25 Mbit/s | 1.05 | 0.88 | 0.94 | 830 MB |
| 100 Mbit/s | **0.58** | 0.73 | 0.83 | 835 MB |

Delay is mean inclusion delay in slots; all rows include 32 of 32. Same
commands as above. **bench**, **medium–high**. The crossover sits between 10
and 25 Mbit/s.


**Throughput is constant in node count.** The same offered rate carries at 40,
100 and 200 nodes with only latency growing, which follows from a replicated
broadcast medium: the ceiling is per-node uplink ÷ per-transaction bytes, not a
function of `N`. **probe** — single seed, no committed harness. Do not cite it
as measured until it is a bench.

**At 25 Mbit/s the EIP as written includes every PQ transaction up to
128 tx/s, past the uplink's ceiling, by overspending it; the bandwidth levers
without `capped8` include the same from 64 tx/s up for about half the bytes
and no drops, and are late rather than lossy below that; `capped8` caps
inclusion at a few dozen transactions per block.** The `high_throughput`
preset is the levers bench's set minus that one plan, and this is the
measurement that decided it. Inclusion is counted after an 8 s drain; delay
is in slots.

| tx/s | offered / ceiling | configuration | offered | included | delay | drops |
| --- | --- | --- | --- | --- | --- | --- |
| 8 | 0.64 | the EIP as written | 1279 MB | 128/128 | 0.64 | 0 |
| | | `high_throughput` | 82 MB | 128/128 | 2.16 | 0 |
| | | `high_throughput` + `capped8` | 122 MB | 128/128 | 2.93 | 0 |
| 32 | 0.78 | the EIP as written | 1566 MB | 512/512 | 0.73 | 0 |
| | | `high_throughput` | 619 MB | 486/512 | 1.03 | 0 |
| | | `high_throughput` + `capped8` | 534 MB | **251/512** | 2.64 | 2 |
| 64 | 0.97 | the EIP as written | 1932 MB | 1024/1024 | 0.80 | 0 |
| | | `high_throughput` | 1042 MB | 1024/1024 | 0.95 | 0 |
| | | `high_throughput` + `capped8` | 443 MB | **216/1024** | 2.83 | 0 |
| 128 | 1.37 | the EIP as written | 2743 MB | 2048/2048 | 1.11 | **24 447** |
| | | `high_throughput` | 1459 MB | 2048/2048 | 1.00 | 0 |
| | | `high_throughput` + `capped8` | 388 MB | **235/2048** | 2.76 | 0 |

```sh
python3 -m bench.throughput --presets eip_faithful,high_throughput,high_throughput+fold_plan=capped8
```

**bench**, **medium** — 40 nodes, degree 6, 25 Mbit/s, 100 ms, PQ
signatures born over 16 s with a flat 200 ms per proving job, 2 seeds; the
16 tx/s row is omitted above and printed by the command. Three things to read
off it. The EIP as written never loses a transaction on this fixture, even
offering 1.37× what the uplinks carry with 24 447 messages refused at full
send buffers: a wide fold per node per interval claims everything the node
holds, so a block needs only a few of them. The levers' value is therefore
bytes and drops, not inclusion — 46% fewer bytes at 64 tx/s, 47% at 128
(arithmetic from the table), and no drops — and below 64 tx/s they cost
delay, because `crossover` defers folding while a claim's witnesses are
smaller than the proof, and until a node folds, its transactions reach a
block only as raw objects against the five-object budget. The 220/256 at
16 tx/s and 486/512 at 32 are late, not lost: with the drain extended to
128 s both rows reach every transaction in both seeds
(`python3 -m bench.throughput --tps 16,32 --presets high_throughput --drain 128000 --seeds 2`),
at a mean delay two to three times the baseline's.

`capped8` is the exclusion. With a flat cost per job, a capped tree over
*n* inputs is about *n*/7 jobs where a wide fold is one, and the proving
budget of one interval holds five such jobs; the roots a node emits then
claim what five jobs can fold, a block carries 36 transactions on average
where the baseline's carries 171 (the `txs/blk` column at 64 tx/s), and
inclusion stays between 216 and 251 over the run however high the rate. The
same happens with `capped8` alone on the EIP as written — 258/1024 at
64 tx/s, 43 per block, 4 417 proofs
(`python3 -m bench.throughput --presets eip_faithful+fold_plan=capped8 --tps 64 --seeds 1`)
— so it is the plan and not the combination. The per-block and proof counts
(4 187 jobs against 689) are measured; that the budget truncates the plan is
the inferred mechanism, **probe**, **low**, and the fixture's flat cost is what
makes a capped plan expensive — under a per-input cost the picture may
differ, which [O2](#o2--compute-what-proving-rate-does-keeping-pace-require)
is the place to settle.

## O2 — Compute: what proving rate does keeping pace require?

**With circuit-based pricing, binary folding includes about half as much as wide
on this fixture because nested recursive proofs cost more than signatures.**

| fold plan | proofs | CPU busy | included | delay | offered |
| --- | --- | --- | --- | --- | --- |
| wide | 990 | 66.2% | 50/64 | 1.44 | 1696 MB |
| capped8 | 1001 | 60.9% | 50/64 | 1.44 | 1696 MB |
| binary | 2778 | 66.7% | **27/64** | 1.54 | 1551 MB |

```sh
python3 -m bench.sweep --axis fold_plan --values wide,capped8,binary \
    --set relay_policy=flood --set proving_budget=1000000000 \
    --nodes 40 --degree 6 --uplink-mbps 25 \
    --transactions 64 --ticks 32 --slots 8 --seeds 1 \
    --prove-cost-circuit 889
```

**bench**, **medium** — reproduced by the command above, though on one fixture
and one seed, and the recursive-verification weight is an estimate. Binary runs 2.8× the proofs to include 54% of what wide
includes, because each pairwise level re-verifies nested proofs at 21× a
signature's price while wide verifies the signatures once. Wide and `capped8` reach the same
inclusion and the same delay, which is what the fan-in cap should look like when
it is not binding — they are not identical in cost (990 proofs against 1001,
66.2% CPU against 60.9%). `proof_jobs_over_capacity` is zero in all three rows,
so no job exceeded the `2**24` trace ceiling; how close any came is not
something that counter reports.

**Declining to fold below the crossover saves four fifths of the bandwidth and
six sevenths of the proving — and whether it costs throughput depends on the
load.** A node whose claim's own witnesses are smaller than the recursive proof
that would replace them emits nothing and proves nothing, on the grounds that a
256 kB proof asserting 2.6 kB of evidence is worse than the evidence:

| transactions | threshold | offered | proofs | deferred | included | delay |
| --- | --- | --- | --- | --- | --- | --- |
| 256 | always | 2197 MB | 1273 | 0 | 252/256 | 0.66 |
| 256 | **crossover** | **441 MB** | **165** | 1114 | **173/256** | 2.17 |
| 512 | always | 2384 MB | 1277 | 0 | 499/512 | 0.70 |
| 512 | **crossover** | **664 MB** | **199** | 1079 | 499/512 | 1.12 |

```sh
python3 -m bench.sweep --axis fold_threshold --values always,crossover \
    --set relay_policy=flood --nodes 40 --degree 6 --uplink-mbps 100 \
    --transactions 256 --ticks 32 --slots 8 --seeds 1
# and again with --transactions 512
```

**bench**, **medium** — reproduced by the commands above, on one fixture and one
seed per row. At 512 transactions the
saving is 3.6× the bytes and 6.4× the proofs for 0.4 slots of delay, with
inclusion identical to `always`. At 256 the same policy loses a third of
inclusion and 1.5 slots, because a node deferring below the threshold needs
enough arrivals to cross it before the transactions it is holding expire. The
saving is real; its cost is a function of load, and a single row would have
shown either the good case or the bad one.

Two further conditions bound it. The threshold needs a load that can reach it at
all: with only 64 transactions in existence no node accumulates 103 signatures,
so the policy degenerates into never folding. And it needs something other than
wrappers to carry transactions — under `wrapper_only` nothing does, so deferring
stalls propagation. It is a policy for signature-carrying pools under flood or
announce-fetch, not a general one.

Privacy transactions are unaffected at any load: one leanSTARK witness already
exceeds a recursive proof, so the threshold never defers.

**Choosing the cheaper wrapper mode saves almost nothing, because a node
rarely holds the witnesses its claim covers.** Mode 0 concatenates raw
witnesses and is 99× cheaper than a recursive proof for a single signature,
but its use is constrained by witness possession: wrappers carry bodies and
hashes, and folded peer aggregates can cover witnesses the node never received.

| relay | mode-0 wrappers | offered MB, recursive | offered MB, cheapest | saved |
| --- | --- | --- | --- | --- |
| flood | 26 | 1916.4 | 1886.6 | 1.6% |
| announce-fetch | 2 | 1876.7 | 1840.3 | 1.9% |
| wrapper-only | 0 | 1788.9 | 1788.9 | 0% |

```sh
python3 -m bench.sweep --axis wrapper_mode --values recursive,cheapest \
    --set relay_policy=flood --nodes 40 --degree 6 --uplink-mbps 25 \
    --transactions 64 --ticks 32 --slots 8 --seeds 1
# and again with --set relay_policy=announce_fetch and =wrapper_only
```

**bench** and **test**, **medium–high** for the mechanism, which is pinned by
tests rather than resting on the table. Around 2% saved in the two policies where
it fires at all, and nothing under `wrapper_only`, where no object ships so a
node holds witnesses only for what it originated. Inclusion is unchanged in
every row; delay moves in the fourth decimal (0.9375 against 0.9208 under
flood), which is a different fold order, not an effect. Privacy claims never choose mode 0 at any width, since
one leanSTARK witness already costs a recursive proof — a constants argument,
pinned by `test_privacy_claims_never_choose_mode_zero` rather than by a row here,
because this harness births signatures only.

One thing the table cannot show, because the model cannot do it: choosing mode 0
does not save the *proving*. The mode is decided at emission and an aggregate
exists only once proved, so a node that emits witnesses has already paid for the
proof whose bytes it is replacing. The EIP's mode 0 avoids both. So these
savings are a lower bound, and the compute half of the lever is unmeasured —
see [roadmap.md](roadmap.md). The useful form of this lever is therefore not
choosing a mode but declining to fold below the crossover, which keeps
possession and mode aligned; see
[proposals/aggregation-strategy.md](proposals/aggregation-strategy.md).

**The input-based fold cost is calibrated from three folds measured on one
machine.** A 2-to-1 fold took 889 ms, 4-to-1 1,664 ms, and 8-to-1 3,824 ms —
ratios `1 : 1.87 : 4.30`. The benches model cost as linear in input count, an
`m`-input fold costing `m / 2` times its 2-to-1 anchor, which is cheaper than
charging it as `m - 1` pairwise folds. The measurement covers fan-in only
through eight, and wider folds need more prover memory, so input-based benches
use `fold_plan="capped8"`.

**probe**, **low** — one machine, one prover implementation, no committed
harness. Every input-based result and the arithmetic below inherit it.

**A fold fits the emission interval only while it stays about two inputs wide.**
With cost linear in inputs and an anchor of `a` ms per pairwise fold, an
`m`-input fold takes `a·m/2`, so fitting inside an interval `T` needs
`m ≤ 2T/a`. At the EIP's 1000 ms interval and a measured 889 ms pairwise fold
that is **2.25 inputs**: a node can afford one two-input fold per interval. The
specification asks each node to fold its whole view every interval, which is
achievable only while that view is two objects wide. Arithmetic from the
measured fold ratios, not a simulation result.

**Emission frequency falls when folds exceed the interval.** The
tick initiates selection; the wrapper is emitted when the proof completes, so a
node whose folds take longer simply emits less often rather than emitting
something degenerate. Measured under `wrapper_only` at 25 Mbit/s with an
unbinding proving budget:

| ms per pairwise fold | mean fold ms | wrappers per tick | included |
| --- | --- | --- | --- |
| 0 | — | 0.80 | 64/64 |
| 889 | 1217 | 0.83 | 62/64 |
| 1500 | 1922 | 0.63 | 39/64 |
| 3000 | 3538 | 0.40 | 16/64 |

```
40 nodes, degree 6, 25 Mbit/s, wrapper_only, 64 privacy transactions over 32 s,
proving_budget unbinding, fold_plan capped8, seed 0.
```

**probe**, **medium** — one fixture, one seed. Two things follow. Emission rate
falls roughly in proportion to fold duration, which is the throughput-for-delay
trade happening without any policy deciding it, so "let a node skip an interval"
is already the behaviour rather than a change to propose. And inclusion falls
faster than emission does: at a 3000 ms anchor a node still emits every 2.5
intervals but carries a quarter of the load, because claims go stale while the
fold runs.

**The CPU queueing discipline is not neutral once proving contends, though what
it buys is queue depth rather than throughput.** With a flat 900 ms proving cost,
priority (verify > build > prove, non-preemptive) cuts peak CPU queue depth
against FIFO by about two thirds — 106 jobs to 29 on the first seed — while
including exactly the same transactions and finishing them marginally *later*
on average.

| discipline | included | mean delay (slots) | peak CPU queue |
| --- | --- | --- | --- |
| fifo | 36 / 38 | 2.432 / 2.450 | 106 / 91 |
| priority | 36 / 38 | 2.653 / 2.497 | 29 / 28 |

```sh
python3 -m bench.sweep --axis cpu_discipline --values fifo,priority --prove-cost 900 --seeds 2
```

**bench**, **medium** — two seeds, one fixture, per-seed values shown rather
than averaged. Priority reduced queue depth but did not improve inclusion or
delay on this fixture.

**At mainnet privacy shares the node's single CPU is what binds, and
proving takes the larger share of it: on leanVM-b-calibrated costs a fast
desktop clears about five privacy transactions per second within two slots
of the load ending, and one whose proving is 2.4× slower clears none.**
Proving is priced in leanVM-b cycles anchored on its measured 417 ms 2-to-1
fold; receive verification by what each delivery carries — 15.6 ms per
STARK-class object, extrapolated from leanVM-b's one measured 14.1–17.1 ms
native verification, and an unmeasured 0.1 ms per bare signature.
Twelve-second slots, births over 5 slots, mixed kinds at a stated share,
40 nodes at degree 6 on 25 Mbit/s uplinks. Inclusion is read two quiet
slots after the last birth — the horizon is part of every number below. A
cell is *held* when every seed includes ≥ 95% of arrivals within it,
*collapsed* when every seed includes < 60%, *marginal* otherwise:

| tx/s | privacy share | eip_faithful | delay | CPU | high_throughput |
| --- | --- | --- | --- | --- | --- |
| 5 | 0 – 0.5 | held, 300/300 at every share | ≤ 0.78 | 45–81% | held from 0.2 up |
| 15 | 0.2 | held, 900/900 | 0.95 | 93% | held, 900/900 |
| 15 | 0.35 | held, 893/900 | 1.48 | 95% | marginal, 540/900 |
| 15 | 0.5 | marginal, 730/900 | 1.64 | 96% | collapsed, 380/900 |
| 30 | 0 | held, 1800/1800 | 0.69 | 71% | held, 1760/1800 |
| 30 | 0.2 | marginal, 1230/1800 | 1.61 | 96% | collapsed, 702/1800 |
| 30 | 0.35 | collapsed, 1006/1800 | 1.82 | 97% | collapsed, 473/1800 |

Two pre-registered caveat rows move the classification. Hardware: with
proving 2.4× slower (a 1000 ms fold anchor — the anchor scales proving
only), 20% privacy collapses at every rate measured — 152/300 at 5 tx/s,
171/900 at 15. The unmeasured leanSTARK witness weight: at twice the
nested-proof weight, 15 tx/s at 20% privacy falls from held to marginal
(725/900).

```sh
python3 -m bench.mainnet --tps 5,15,30 --privacy-share 0,0.2,0.35,0.5 \
    --leanstark-factor 1 --anchor-ms 417 --seeds 8
python3 -m bench.mainnet --tps 5,15 --privacy-share 0.2 \
    --leanstark-factor 1 --anchor-ms 1000 --seeds 8
python3 -m bench.mainnet --tps 15 --privacy-share 0.2,0.35 \
    --leanstark-factor 2 --anchor-ms 417 --seeds 8
```

**bench**, **medium** where the per-seed classification is unanimous,
**low** in the four cells where seeds straddle a boundary
(`high_throughput` at 5 tx/s share 0, at 15/0.35 factor 1, and at 15/0.2
factor 2; `eip_faithful` at 15/0.35 factor 2). The mechanism, from the
engine's per-kind CPU accounting at the failing cells: proving consumes
82–87% of total CPU capacity and receive verification 9–13%, and with
both verify prices set to zero the cells stay broken (768–778/900 at
15/0.5, 175–187/300 at the slow anchor) — so proving binds independently,
and verification moves the frontier on top of it rather than causing it.
Root claims stay wide at the fast anchor (75–115 transactions) and
collapse at the slow one (8–12), the tell that the proving budget starves.
Five caveats bound the claim's reach. Inclusion is horizon-dependent:
lengthening the drain from 0 to 4 quiet slots moves 15 tx/s share 0.35
from 669/900 to 900/900, so held-versus-marginal boundaries are claims
about clearing within two slots, not about steady state — and delay
averages only what was included, a right-censored statistic. The STARK
verify price is an extrapolation from one measured proof size and the
signature price is unmeasured; a 3-seed probe found the latter material —
raising it 0.1 → 0.9 ms cut inclusion 27% at 30 tx/s — and the measured
14.1–17.1 band flips marginal cells, so native verification of the real
message mix is the single most valuable external measurement. Arrivals
are uniformly spaced; bursts at 95% CPU would break cells uniform holds.
The default instant chain notification carries no block messages, so
block verification is uncharged. And the two circuits leanVM-b did
measure disagree by 25% on milliseconds per cycle, a floor on the time
scale's error that the anchor axis is designed to absorb.

## O3 — Aggregation leverage: what the block budget binds on

**`block_budget` limits objects, not transactions, so what a block settles
depends on how wide the claims in the proposer's view are.** A budget of 5 folds
admits at most 6 objects. With no aggregation those objects are single
transactions, so a block settles 6 — that is arithmetic, `block_budget + 1`, and
cannot come out otherwise. With nodes folding once per interval, one object
claims many transactions and the budget stops being the binding constraint at
this load: all 60 offered transactions were included across three slots, so the
ceiling became the workload rather than the budget.

```python
# 20 nodes, degree 4, 60 PQ transactions over 6 s, block_budget=5.
# Comment out schedule_ticks to remove aggregation from the run entirely.
import random
from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay

overlay = Overlay.random_regular(20, 4, random.Random("0:topology"),
                                 base_latency=50.0, jitter_frac=0.1,
                                 latency_seed=0)
sim = NetworkSim(overlay, seed=0, slot_length=2000.0, block_budget=5,
                 proposer_id=0,
                 costs=CostTable(verify_recv=1.0, prove_recursive=30.0))
for index in range(60):
    sim.schedule_birth(index * 100.0, KIND_PQ)
sim.schedule_ticks(6)
sim.schedule_slots(3)
sim.run(until=6000.0)
print(sim.metrics()["included_txs"],
      [block.included.bit_count() for block in sim.chain.blocks])
```

With ticks: 60 of 60, at 19, 20 and 21 transactions per block. Without them:
18 of 60, at 6, 6 and 6.

**probe**, and read it as an illustration rather than a measurement of what
aggregation is worth. Both numbers are set by the fixture — one by the budget,
the other by the offered load — so the ratio between them says nothing
transferable. Removing the aggregation ticks is also not a protocol variant:
there is no configuration that disables folding, only the absence of scheduled
ticks, and the pre-EIP baseline it would need to be compared against (a block
paying per-transaction proof costs) is not represented in this model at all.
What the run does establish is that folding produced claims wide enough for the
budget to stop binding at this load. The transferable question — how wide
claims actually get, and at what offered load the budget resumes binding — is
[roadmap.md](roadmap.md) item 5.

## O4 — Limits: what happens at capacity, and past it

**Overload has a clearance-time threshold near 1.1× capacity, and end-of-load
queue size does not predict which side of it you are on.** Below roughly 1.1×,
the backlog clears within a 16 s drain. Above it, it does not clear in that
window — but it is not permanent: with the drain extended to 64 s, both the
1.18× and 1.24× rows clear completely in both seeds
(`python3 -m bench.overload --uplink-mbps 10 --drain 64000 --tps 5,10`). What the
threshold locates is the point where clearance stops fitting in twice the load
window, which is the operationally relevant question; calling it collapse would
overstate it. The queue at the end of the load window grows smoothly across the
boundary, so it is not the signal.

| offered / ceiling | queue at load | queue after drain | drained | residency |
| --- | --- | --- | --- | --- |
| 0.55 | 16.7 MB | 0.0 MB | 2/2 | 100% |
| 0.88 | 60.0 MB | 0.0 MB | 2/2 | 100% |
| 1.10 | 108.8 MB | 0.0 MB | 2/2 | 95.1% |
| 1.18 | 126.6 MB | 67.4 MB | **0/2** | 90.9% |
| 1.24 | 137.7 MB | 121.4 MB | **0/2** | 64.1% |

```sh
python3 -m bench.overload --uplink-mbps 10
```

40 nodes, degree 6, 10 Mbit/s, flood, 8 s load and a 16 s drain, 2 seeds.
**bench**, **medium**. The x-axis is a known weakness: "offered" counts bytes
the send buffers *accepted*, so with fixed buffers a faster pipe cannot reach
the same ratios, and the cliff's position cannot yet be compared across
capacities. Fixing that metric is a prerequisite for asking whether 1.1–1.2 is
a number or an artefact of this fixture.

**A retry timeout shorter than the real fetch latency turns recovery into
collapse below capacity.** With announce-fetch and no timeout, the backlog
drains at 0.89× capacity. Add a 1500 ms request timeout — shorter than a
contended fetch actually takes — and re-requests pile onto the queue they are
waiting for: at 0.96× capacity the backlog grows during the drain (81.6 MB →
101.1 MB) and never clears.

```sh
python3 -m bench.overload --uplink-mbps 10 --policy announce_fetch
python3 -m bench.overload --uplink-mbps 10 --policy announce_fetch --request-timeout 1500
```

**bench** and **test** (the mechanism is asserted by a unit test), **medium–high**.
The two runs sit at slightly different x-positions for the same reason the
cliff is not yet portable. There is no recommended default timeout; deriving
one from measured fetch latency is open work.

## O5 — Protocol choices: which matter, and which way

**Retiring a peer's older wrapper by provenance alone loses more than it
saves.** The three policies were compared on an aggregation-limited fixture
with unlimited bandwidth, continuous folding, uniform pair selection, and a
dedicated builder.

| supersede | included | per block | delay | held aggregates | of those, subsumed |
| --- | --- | --- | --- | --- | --- |
| `keep_all` | 381/1520 | 19.05 | 6.90 | 995 | 242 (24%) |
| **`newest_if_covered`** | **401/1520** | **20.05** | 6.83 | 471 | 67 (14%) |
| `newest_per_peer` | 282/1520 | 14.10 | 5.16 | **20** | 0 |

```sh
python3 -m bench.v1_calibration
```

**bench**, **medium** — one fixture, one seed. `newest_if_covered` is the only
one that pays: it halves what a node retains and improves inclusion, because
the thing it drops is a claim some other held claim already contains.
`newest_per_peer` keeps exactly one aggregate per peer — 20 of the 1001 proved
— and inclusion falls by a quarter against keeping everything. The mechanism
is that a newer claim may be narrower after settled dependencies are removed,
so provenance-only supersession can discard wider evidence.
Delay falls in that row for the same reason it does whenever throughput
collapses — fewer transactions get included, and the ones that do were never
waiting.

Scope: an aggregation-limited fixture with no bandwidth constraint. Under
`flood` at a real uplink the retained set also costs bytes, which is the axis
this fixture deliberately removes.

**On this fixture the five levers nearly compose, but `crossover` supplies most
of the byte saving and the combined configuration increases delay.**

| configuration | offered | saved | included | delay | proofs |
| --- | --- | --- | --- | --- | --- |
| the EIP as written | 1379.9 MB | — | 256/256 | 0.69 | 666 |
| `announce_fetch` | 1261.2 MB | 8.6% | 256/256 | 0.88 | 719 |
| `lean` | 1232.4 MB | 10.7% | 256/256 | 0.69 | 666 |
| **`crossover`** | **335.1 MB** | **75.7%** | 256/256 | 1.09 | **102** |
| `capped8` | 1380.6 MB | −0.0% | 256/256 | 0.93 | 1934 |
| `cheapest` | 1379.9 MB | 0.0% | 256/256 | 0.69 | 666 |
| all five together | 299.8 MB | 78.3% | **248/256** | **2.18** | 2332 |

```sh
python3 -m bench.levers
```

**bench**, **medium** — one fixture, two seeds. Independence would predict
80.2% against the measured 78.3%, so the levers overlap by about two points.
The independence gap is −7.2 points at one seed, −1.9 at two, and −0.4 at
three. More seeds are needed before attributing the low-seed difference to
lever interaction.

`crossover` supplies 75.7 of the combined 78.3 percentage-point saving; the
other levers contribute little additional byte reduction on this fixture.
`cheapest` saves nothing at all, because under `flood` a node rarely holds the
witnesses its claim covers — the possession precondition, not the size
comparison, is what gates it. And the combination triples mean delay, 0.69 to
2.18 slots, while losing eight transactions: deferring folds and fetching on
demand both trade latency for bytes, and measuring each alone never charged for
it twice.

**Neither relay policy dominates.** Flood is faster for small objects and
collapses first under load; announce-fetch pays a round trip per object and
survives. At 10 Mbit/s under sustained load, flood delivers 27.8% coverage
against announce-fetch's 68.3%, while for 3.2 kB signatures flood is roughly
three times faster (373 ms vs 1020 ms to 90%) and both sustain the load
completely. The crossover is a function of object size and headroom.
**bench**, **high** — same propagation commands as above.

**Wrapper economies are worth 17.5% of wrapper bytes, and `proof_by_ref` does
nearly all of it.** At 10 Mbit/s, `lean` (both economies) and
`proof_by_ref` alone produce identical byte totals — substituting a 32-byte
hash for a proof the peer already holds is the whole effect at this operating
point, and `skip_unchanged` contributes separately mostly during the drain,
when a node's aggregate stops changing identity every fold. **bench**,
**medium** — `python3 -m bench.economy --uplink-mbps 10`, isolation rows included.

**Binary fold plans cost about four times wide's proving for no delay benefit.**
Folding a selection as a chain of pairs completes 3521 proofs where one wide
fold completes 928, and lifts CPU utilisation from 8.7% to 23.8%, while mean
inclusion delay is unchanged (0.69 vs 0.70 slots). The direction is structural:
a wide fold asserts the same coverage in one job.

```sh
python3 -m bench.sweep --axis fold_plan --values wide,binary
```

**bench**, **medium**. The figures are seed 0; the command emits two seeds, and
the second agrees in direction (3494 against 928). Read the ratio, not the
counts. This fixture prices proving as a flat cost per job, so it is unaffected
by how a job's dependency count is measured.

**Under a binding proving budget, the selection rule moves more fee than the
eviction rule, and ordering by urgency costs fee without buying throughput.**
In a deliberately starved regime — 60 ms per dependency against a 1 s tick,
roughly 20 arrivals per second, candidate limit 16 — the choice of selection is
worth up to 12.5 points of included fee, against a largest eviction effect of
11.8 — close enough that neither dominates, and the mean effects (12.5 against
7.1) flatter selection by comparing a spread with an average. `fee_density` wins on both counts at once, which is unusual here:
most pairs trade fee share against inclusion.

| selection | eviction | included | included fee | fee share | expired | delay |
| --- | --- | --- | --- | --- | --- | --- |
| eip_priority | usefulness | 165/480 | 2483 | 38.0% | 298 | 2.40 |
| eip_priority | fee_value | 146/480 | 2911 | 44.6% | 312 | 2.25 |
| fee_density | usefulness | **176/480** | 2938 | 45.0% | 289 | 2.25 |
| **fee_density** | **fee_value** | 172/480 | **3119** | **47.8%** | 290 | **2.15** |
| deadline | usefulness | 155/480 | 2121 | 32.5% | 298 | 2.35 |
| deadline | fee_value | 146/480 | 2894 | 44.3% | 304 | 2.30 |

```sh
python3 -m bench.triage --uplink-mbps 10 --seeds 10
```

Under either eviction rule, deadline-first selection has the lowest fee share
and no throughput advantage on this fixture: under `usefulness` it includes 155
transactions versus `eip_priority`'s 165, and under `fee_value` both include
146.

**bench**, **medium–high** for the eviction effect and the deadline result,
**medium** for the identity of the best row. Ten seeds. Note the trade-off
rather than a winner: `fee_density`+`usefulness` includes the most
transactions, `fee_density`+`fee_value` earns the most fee.

Replacing the balanced fee cycle with drawn lognormal fees separates what is
robust from what is not:

| fee model | best fee row | its share | eviction effect, `eip_priority` | `deadline`+`usefulness` |
| --- | --- | --- | --- | --- |
| cycle {1,2,5,10,50} | fee_density + fee_value | 47.8% | +6.6 pts | 32.5% |
| lognormal σ = 1 | fee_density + fee_value | 44.9% | +5.1 pts | 31.6% |
| lognormal σ = 2 | fee_density + fee_value | 45.6% | +9.5 pts | 28.2% |

```sh
python3 -m bench.triage --uplink-mbps 10 --seeds 10 --fee-model lognormal --fee-sigma 1
python3 -m bench.triage --uplink-mbps 10 --seeds 10 --fee-model lognormal --fee-sigma 2
```

What survives the change of fee distribution: `fee_density`+`fee_value` is the
best fee row under all three, and deadline-first triage is worst on fee under
all three. What does not: the eviction effect swings from +5.1 to +9.5 points
across the two lognormal widths, so its size is a property of the distribution
rather than of the rule, and the largest eviction effect exceeds the
selection spread at σ = 2 — 15.2 points against 14.8 — so which lever is larger
is itself a property of the distribution, not a finding.

Scope: one topology and one starved regime. Treat the winning row as
fixture-specific and the two axis effects as the result.

**Requiring body possession for inclusion changes nothing on the fixture that
can be reproduced.** Hash substitution keeps a proposer's bodies level with its
claims, so `body_required` (the faithful reading of the EIP, and the default)
and `claim_only` produce identical inclusion counts and delays.

```sh
python3 -m bench.sweep --axis inclusion_policy --values body_required,claim_only
```

**bench**, **null result**. No difference was observed at these operating
points.

**Amortizing the crossover threshold repairs its failure outright — with
it, the `high_throughput` configuration matches or beats the EIP's
always-fold inclusion in every paired seed at every rate at the measured
anchor for 17–22% fewer bytes; single-variable controls attribute those
bytes to the configuration's bandwidth levers and the repair itself to the
factor.** The local crossover rule compares a fold's proof bytes against
the witnesses it replaces at the folder's own uplink; `fold_amortize`
scales the witnesses' side to stand in for the fold's benefits beyond that
uplink — a block slot carrying the whole claim, downstream receivers
verifying one proof instead of every witness, nobody proving the coverage
twice. All-signature load (the regime where plain crossover defers),
share 0, factor 1, verification priced as in the frontier entry above;
included transactions and offered MB per configuration — note these are
whole-configuration rows, differing in relay policy, wrapper economy and
mode as well as threshold:

| tx/s | always (eip_faithful) | crossover (k=1) | k=8 | k=16 |
| --- | --- | --- | --- | --- |
| 5 | 300 / 4698 | 256 / 449 | 300 / 3325 | 300 / 3655 |
| 15 | 900 / 5240 | 891 / 1664 | 900 / 4240 | 900 / 4311 |
| 30 | 1800 / 6025 | 1760 / 2312 | 1800 / 4774 | 1800 / 4814 |
| 50 | 3000 / 6918 | 2983 / 4828 | 3000 / 5755 | 3000 / 5740 |

At the 2.4× slower proving anchor plain crossover proves nothing at all —
the one-interval budget plus the local rule switch aggregation off, and
inclusion falls to the block budget (42) at every rate — while the
amortized rule keeps folding. It escapes that structural floor rather than
the overload: at 50 tx/s every seed of every folding configuration remains
collapsed under this entry's own rule.

| tx/s | always (eip_faithful) | crossover (k=1) | k=8 | k=16 |
| --- | --- | --- | --- | --- |
| 5 | 300 / 4427 | 42 / 39 | 300 / 2987 | 300 / 3276 |
| 15 | 900 / 5051 | 42 / 118 | 900 / 4068 | 889 / 4156 |
| 30 | 933 / 5711 | 42 / 236 | 990 / 4068 | 929 / 3995 |
| 50 | 795 / 6343 | 42 / 394 | 1002 / 4337 | 898 / 4171 |

```sh
python3 -m bench.mainnet --tps 5,15,30,50 --privacy-share 0 \
    --leanstark-factor 1 --anchor-ms 417,1000 --seeds 8 \
    --presets eip_faithful,high_throughput,high_throughput+fold_amortize=8,high_throughput+fold_amortize=16,high_throughput+fold_amortize=32,high_throughput+fold_amortize=64
```

**bench**, **medium** — the dominance claim was pre-registered as a
per-seed paired rule (k=16 inclusion ≥ always in at least 7 of 8 seeds,
bytes lower in 8 of 8) and passes 8-of-8 on both counts at every rate at
the 417 ms anchor; the same rule also passes for k=8 at every rate, so the
evidence selects the amortization, not one factor. At the 1000 ms anchor
the rule fails at 15–50 tx/s and the table stands without a dominance
claim: the folding rows at 30 and 50 tx/s are seed-noisy (always spans
733–918 included at 50 tx/s, k=16 spans 715–1152) while at 5 and 15 tx/s
they are exact, k=8 matches always seed-for-seed at 15 tx/s with 20% fewer
bytes there, and the apparent decline of the mean with larger k dissolves
into overlapping seed ranges, per the pre-registered rule for that
observation. Attribution comes from single-variable controls (3 seeds,
**probe**): on either base, changing only the threshold moves offered
bytes by at most 1% — the 17–22% in the tables belongs to
`high_throughput`'s relay, economy and mode levers — and wrapper emissions
are not fewer under amortization (3 015 against always's 2 795 cores at
50 tx/s). What the factor itself buys is the repair — 42 to 900 on the
same base — plus, in the same controls, a consistent same-base edge over
`always` at the slow anchor (778 → 849 on the EIP base, 767 → 924 on
`high_throughput` at 50 tx/s), which suggests deferring the narrowest
folds helps when proving is scarce but is probe-grade. The thresholds in
bytes: k = 16 first folds a claim of seven signatures where plain
crossover waits for one hundred and three (witness bytes only, 2 560 per
signature against the 262 144-byte proof), and a lone signature's re-proof
is still declined. Measured on all-signature load, where crossover's
pathology lives; on mixed loads any claim holding one privacy witness
already clears the local threshold, so the factor has little left to
decide there.


## O6 — Position: does where you are decide what happens to you?

**Delay attaches to place only once the network is diffusion-limited, and
rotating the proposer collapses the effect.** Sweeping latency so that the time
to reach 90% of nodes grows from 0.17 to 1.71 slots, the correlation between a
transaction's hop distance from the proposer and its inclusion delay rises
monotonically to +0.67 under a fixed proposer, while a rotating proposer holds
it near zero. Rotation redistributes the disadvantage rather than removing it:
mean delay barely improves.

| t90 / slot | fixed: corr(delay, hops) | rotating |
| --- | --- | --- |
| 0.17 | +0.033 | −0.016 |
| 0.50 | +0.232 | −0.020 |
| 0.92 | +0.391 | +0.025 |
| 1.72 | **+0.668** | +0.072 |

```sh
python3 -m bench.placement
```

60 nodes, degree 4, 4 seeds. **bench**, **medium**.

**With deadlines, distance also decides who is excluded, not only who is
late.** The delay penalty does not go away — the fixed-proposer delay
correlation is +0.63 with deadlines against +0.67 without — but a second,
separate effect appears on top of it: −0.57 correlation between distance and
*inclusion* under a fixed proposer, against −0.07 rotating. Rotation changes
who loses far more than it changes the aggregate: at the 1.71-slot point, fixed
includes 17 transactions at 1.20 slots mean delay and rotating 24 at 1.12, so
the correlation collapse is about distributing the loss rather than removing
it.

```sh
python3 -m bench.placement --lifetime 1500
```

**bench**, **medium**. Aggregation is what usually hides this: one fold carries
many transactions, so the block budget seldom binds and there is no exclusion
to correlate. It takes a deadline to make the block scarce again.

## What is not here

Heterogeneous links and CPUs, the supersede-policy comparison *where bandwidth
binds* — it is raced above only where aggregation does — and everything about
adversarial or self-interested participation.
[roadmap.md](roadmap.md) lists these with what each would settle.
