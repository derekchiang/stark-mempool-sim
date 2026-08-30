# ADR 0003: Derive randomness from stable identities

## Context

One mutable random stream couples unrelated choices to draw order. Adding a
node, changing topology construction, or inserting a policy-specific draw can
then reshuffle every later value. A seeded run remains repeatable but parameter
sweeps cease to compare the same workload.

## Decision

When an item has a stable identity, derive its random value from the seed and
that identity. Otherwise give the concern a dedicated labelled stream such as
`"{seed}:topology"`, `"{seed}:entry"`, `"{seed}:compute"`, or
`"{seed}:fees"`. Benchmark workloads are functions of their seed alone and do
not consume random values conditionally on the policy being measured.

## Consequences

An edge keeps its latency when unrelated graph details change. Entry choices,
compute jitter, topology generation, and fee generation cannot perturb one
another. Repeating a seed produces byte-identical output, and a one-parameter
sweep remains paired unless that parameter intentionally changes the workload.

## Where it lives in the code

`starksim.derive.stable_fraction` and `stable_index` implement identity-derived
values. `starksim.network.UniformLatency.latency` derives per-edge jitter.
`starksim.netsim.NetworkSim.__init__`, `NetworkSim.schedule_ticks`, and
`NetworkSim._is_followed` create or derive labelled concerns.
`starksim.costs.CostTable.__init__` owns the compute stream. Benchmark workload
construction is isolated from the policy being compared.

## Status

Accepted and implemented; new random choices must follow the same rule.
