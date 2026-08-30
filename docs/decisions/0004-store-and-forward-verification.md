# ADR 0004: Verify before forwarding

## Context

Cut-through forwarding would let bytes continue through a node before the node
has processed their evidence. A real participant must validate received
material before relying on or propagating it. Treating that work as free would
understate multi-hop delay and remove its contention with proving.

## Decision

The network is store-and-forward. A complete message first traverses sender
egress and link latency. The receiver then pays `verify_recv` on its CPU before
admission, relay, re-aggregation, or settlement notification. A configured
zero verification duration deliberately collapses the processing stage without
queueing a zero-cost job.

## Consequences

Verification lies on the propagation critical path and competes with recursive
proving and block construction. CPU queue overflow can consume network bytes
without producing retained evidence. Wrappers travel one hop and spread their
claims only after verified re-aggregation; block notices are believed and
relayed only after verification.

## Where it lives in the code

`starksim.netsim.NetworkSim._egress_done` schedules delivery.
`NetworkSim._deliver_body`, `_deliver_wrapper`, and `_deliver_block` submit
`VERIFY_RECV` jobs. `_verify_done`, `_wrapper_verified`, and `_block_verified`
apply results before subsequent propagation. `starksim.costs.CostTable.duration`
prices the stage.

## Status

Accepted and implemented. Cut-through is not modelled.
