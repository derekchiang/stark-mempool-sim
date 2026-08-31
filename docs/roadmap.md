# Roadmap

This file lists open questions and known limitations. It does not summarize
measurements; see [results.md](results.md) for current findings and confidence.
Ranked items are tagged with the objective they serve
([model.md](model.md) defines them).

## Ranked experiments

1. **(O1, O2)** Deepen the mainnet-shaped workload. `bench.mainnet` now runs
   12-second slots, a mixed transaction population, and leanVM-b-calibrated
   proving and verification, and [results.md](results.md) carries the first
   frontier from it. What remains: bursty arrivals (uniform spacing is a
   stated limitation, and the frontier-adjacent cells sit at 93–96% CPU),
   block messages
   under `chain_notify="overlay"` so block verification is charged, a
   justified mix beyond the swept share axis, and the two external
   measurements the frontier hinges on — the leanSTARK witness weight
   in-circuit and native verification of the real message mix.

2. **(O1)** Sweep tick interval and degree while holding offered workload,
   capacities, and observation windows fixed. The recursive proof is paid per
   peer per wrapper emission, so both parameters directly set the bandwidth
   floor.

3. **(O5)** Compare supersede policies under finite egress capacity, measuring
   retained evidence, bandwidth, and inclusion.

4. **(O4)** Repair the overload x-axis before testing portability. Current offered
   bytes count messages accepted by bounded buffers. Faster pipes paired with
   fixed buffers can refuse demand before that metric reaches the intended load
   ratio. Record attempted demand or scale buffers with capacity, then vary
   degree, node count, object size, and drain horizon.

5. **(O3)** Measure claimed transactions per selected object across offered
   load, block budget, tick interval, and proving cost. Use a
   protocol-expressible baseline such as `fold_threshold="crossover"` and report
   where the block budget resumes binding. The model has no pre-EIP
   per-transaction-proof baseline.

## Protocol and policy questions

- Close the fold-amortization residue. The factor question is answered at
  one fixture — [results.md](results.md): amortization (k of 8 or 16)
  repairs crossover's aggregation-off collapse, the byte savings measured
  alongside belong to the bandwidth levers, and at the slow anchor k=8
  matches `always` seed-for-seed at 15 tx/s for 20% fewer bytes — but two
  threads remain. Single-variable controls show a consistent same-base
  edge for amortized crossover over `always` under slow proving
  (probe-grade): more seeds or a paired analysis would say whether
  deferring the narrowest folds genuinely spares constrained proving. And
  the successor question is incentives, outside the current honest-uniform
  model: with heterogeneous costs a rational node free-rides on upstream
  folding whatever the threshold, and the measured inclusion gap between
  `always` and `crossover` prices that problem.
- Add a gossipsub-shaped relay that pushes bodies on a small mesh and announces
  to other peers. The current `flood` and `announce_fetch` policies bracket this
  design but do not represent it.
- Establish a request-timeout recommendation and validity range from measured
  fetch-latency distributions. A timeout must distinguish useful recovery from
  retries caused by congestion-delayed responses.
- Exercise CPU-bound overload with mixed verification and long-fold queues to
  measure CPU discipline and bounded CPU admission under load.
- Locate overload and recovery behavior under `wrapper_only`, whose propagation
  and feedback mechanisms differ from raw-object relay.
- Re-run proving-cost sensitivity for triage, fold-plan, placement, and CPU-
  discipline comparisons. In particular, input-based costs and long folds may
  create queue interactions absent from cheaper fixtures. The `capped8`
  inclusion cap in [results.md](results.md) was measured under a flat cost
  per job, which is what makes a capped tree many times a wide fold's price;
  under per-input or circuit pricing the cap may move or vanish.
- Add a deadline selector with a lost-cause cutoff: do not spend proving budget
  on transactions that cannot reach a block before expiry.
- Compare additional topology families, degree heterogeneity, and a deliberately
  well-connected proposer. Repeat placement experiments under `wrapper_only`,
  where each hop also waits for a tick.
- Model heterogeneous link and CPU capacities. Feasibility for identical nodes
  does not establish accessibility for the weakest participant.

- Publish duplicate deliveries per followed transaction, from the `dup`
  records, for each relay policy: the direct measure of what `announce_fetch`
  buys, on the transaction that paid for it, checked against the run's
  `duplicate_bytes`. The viewer counts them; no result table does yet.
- Decide the attribution of a body merged into another peer's in-flight
  verification. The rule as written credits the peer whose copy completed
  verification and gives the body's deliverer no transition; crediting the
  deliverer needs `begin_wrapper` to keep the sender of what it merges.
- Animate message flight on the network view. `snd` is stamped at enqueue and
  is sampled, so an honest animation needs an unsampled egress-completion
  event carrying the arrival time; with it, the queue wait becomes visible as
  messages piling up at a sender. The spread tree answers the narrower
  question of how a transaction spread; this would answer what it cost.

## Engineering limits

- Several per-node structures and the arena grow with simulated duration.
  Before long CPU-overload runs, add a committed memory benchmark and
  reclamation or bounded-state policy for verified-proof identities, seen
  blocks, bodies, peer beliefs, and arena objects.
- Candidate state, duplicate-suppression state, and the coverage cache have
  optional bounds but default to unbounded. Experiments using bounds must report
  eviction and forgetting because tighter bounds can reduce apparent protocol
  performance.
- Calibrate the circuit cost weights. `prove_cost_basis="circuit"` prices a job
  by the signatures, leanSTARK witnesses and nested proofs it verifies, but only
  the signature count (~470 hashes) has two independent derivations. The
  recursive-verification weight is a FRI estimate spanning 5 000 to 19 000
  hashes, the leanSTARK weight is assumed equal to it, and rows-per-hash — which
  decides whether one proof holds hundreds or tens of thousands of signatures —
  depends on which hash leanSPHINCS uses in-circuit. Every conclusion drawn on
  this basis inherits those three.
- **Make `wrapper_mode="cheapest"` a compute lever.** The mode is decided when a
  wrapper is emitted, by which point the aggregate has been proved and the fold
  has retired the very witnesses mode 0 would carry. So the model spends the
  proving either way and only saves the proof's bytes, while the EIP's mode 0
  avoids the proving too. Deciding before planning needs a wrapper that
  references a claim rather than a proof object, and a rule for what the emitter
  retains afterwards. Until then every mode-0 saving reported here is a lower
  bound.
- Teach selection about the trace ceiling. A job whose verification work exceeds
  `2**24` rows is refused and counted, because such a proof cannot exist, but
  nothing selects against the limit — so a workload that reaches it loses whole
  plans rather than proving what would fit.
- Run a fold-cost sweep using basis-matched selection estimates and the
  minimum-two-input budget rule; no current result exists.
- Sweep proving budget across multiple loads and seeds, reporting throughput
  and delay separately.
- `eviction_policy` reaches the CLI and the variant but cannot move a metric on
  the sweep fixture, which retains candidates without limit. It needs a workload
  with a bounded candidate set before any result there means anything.
- The priority CPU discipline changes service order only. A full bounded queue
  still tail-drops an arriving verification even if a lower-priority proof is
  waiting; priority push-out would require cancellation accounting and owner
  notification.
- Block construction cost occupies the CPU after the chain fact is created.
  The model can represent reduced proposer knowledge but not a late or missed
  slot caused by build work.
- Add a large viewer performance fixture only where it answers an active
  analysis question; require a trace field, reducer invariant, and render check
  for each new panel.

## Beyond the specification

The items above measure EIP-8288 as written. Where a measurement suggests the
specification itself could be changed — the emission cadence setting a bandwidth
floor independent of load, a single relay policy having to serve two object
kinds whose frontiers differ by an order of magnitude, and the absence of any
congestion response — the candidate mechanisms are written up in
[proposals/aggregation-strategy.md](proposals/aggregation-strategy.md) with what
each would buy and how a run here could falsify it. Those are proposals, not
plans.

## Permanent interpretation caveats

Nothing has been validated against a production client or testnet. The protocol
itself is a draft, and compute and size inputs are model parameters rather than
client measurements unless a bench states a calibration.

All participants are currently honest, identical, online, and altruistic. No
node sends invalid evidence, withholds proving effort, spams peers, or chooses
participation according to private cost. There are no reorgs, adversarial
topologies, account nonces, transaction-order constraints, state conflicts, or
execution-level validity. Inclusion counts are therefore ordering-blind upper
bounds within the modeled workload.

The next research boundary is participation under heterogeneous costs and
adversarial incentives. That is a new model dimension, not a parameter sweep of
the current honest-uniform one.
