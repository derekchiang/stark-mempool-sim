# Architecture decisions

- [0001 — Meter bandwidth on node egress](0001-node-egress-bandwidth.md): fan-out shares one finite uplink; optional link caps are a tandem stage.
- [0002 — Separate global truth from node belief](0002-global-truth-and-node-belief.md): protocol decisions use only retained local evidence.
- [0003 — Derive randomness from stable identities](0003-deterministic-randomness.md): labelled streams keep seeded comparisons paired.
- [0004 — Verify before forwarding](0004-store-and-forward-verification.md): receive verification is part of the propagation path.
- [0005 — Model one contended CPU per node](0005-one-cpu-per-node.md): verify, prove, and build jobs share one serial server.
- [0006 — Keep observation off the simulated critical path](0006-observation-cost.md): tracing must preserve event order and scale with changed state.
- [0007 — netsim keeps event orchestration in one module](0007-netsim-stays-one-module.md): event paths share state and accounting without a stable ownership boundary.
