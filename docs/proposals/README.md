# Proposals

Candidate changes to the *protocol*, written so this simulator can evaluate
them. Nothing here is implemented, and nothing here is part of EIP-8288.

The distinction from the neighbouring directories:

- [decisions/](../decisions/) records choices already made about **this
  simulator**, with the code that implements them.
- [roadmap.md](../roadmap.md) lists measurements outstanding on the protocol
  **as specified**.
- This directory holds mechanisms the specification does not contain, each with
  what it would buy, what it would cost, and how a run here could falsify it.

Results for implemented proposals belong in [results.md](../results.md);
architecture rationale belongs in [decisions/](../decisions/).

- [An aggregation strategy](aggregation-strategy.md): flood the cheap, pull the
  expensive, aggregate once and wide — five mechanisms following from the
  measured facts that wrapper proof pushes dominate bandwidth and deep
  recursion dominates estimated compute.
